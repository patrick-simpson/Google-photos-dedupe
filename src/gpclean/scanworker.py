"""Scan worker: turn one task (a run of adjacent wanted zip members) into result rows.

This module is what every ``spawn`` pool process imports, so it stays import-light: Pillow,
numpy and the zip reader, but no torch. ``torch``/``open_clip`` are imported only when a real
CLIP embedder is requested, inside the worker, on its first task.

Two ways in, one code path:

* **pool**: ``init_worker`` is the process-pool initializer; ``pool_task`` runs a task and
  ``pool_warm_up`` loads a process's resources without running one;
* **in-process** (``workers == 1``): the caller builds a :class:`Worker` itself and calls
  :meth:`Worker.run` directly.

A task is ``(task_no, [(Entry, MemberClass), ...])`` with at most ``TASK_MAX`` members that
sit next to each other in the file (no skipped member between them), so a single range
request covers the whole task and never touches bytes of a video or a skipped member. Every
task travels with the opener of its zip, so one process can serve shards of several zips.

The result is a plain dict of picklable values (see :meth:`Worker.run`). Backend failures are
reported as ``result["abort"]`` rather than raised: a ``RangeReadError`` does not survive
pickling with its ``retryable`` flag, and the parent only needs to know "stop this shard".
"""

from __future__ import annotations

import logging
import os
import zipfile
import zlib

import numpy as np
from PIL import Image

from gpclean import publiclog
from gpclean.config import ScanConfig
from gpclean.imaging import configure_pillow, process_image
from gpclean.takeout.names import ext_of
from gpclean.takeout.rangefile import RangeReadError
from gpclean.takeout.sidecar import parse_sidecar

log = logging.getLogger(__name__)

# Upper bound of members per task (PLAN §3: "about 32-48 consecutive wanted members").
TASK_MAX = 48

# The per-item read failures ZipSource documents (bad CRC, corrupt deflate stream, ...).
# ``_read_member`` records any other exception zipfile raises for one member too (encrypted
# member: RuntimeError, unsupported method: NotImplementedError); this tuple stays as the
# documented core. RangeReadError is an OSError too, but it is checked first: it means the
# backend failed.
READ_ERRORS = (OSError, zipfile.BadZipFile, zlib.error)

# Embedder name that selects the deterministic torch-free stub (tests, quick dry runs).
STUB_EMBEDDER = "stub"

# ``err`` of a member whose processing killed the worker process (a native decoder crash or
# an out-of-memory kill). The scan parent records it; no worker ever returns it.
WORKER_CRASH = "WorkerCrash"


class Worker:
    """Per-process scan state: the current zip's ZipSource and one lazily loaded embedder."""

    def __init__(self, cfg: ScanConfig, embedder_name: str, *,
                 threads: int | None = None) -> None:
        """``threads`` is the torch thread count for a real CLIP model (``None`` keeps
        torch's default)."""
        self.cfg = cfg
        self.embedder_name = embedder_name
        self.threads = threads
        self._opener = None
        self._src = None
        self._zf = None
        self._embedder = None
        self._embedder_ready = False

    # ----------------------------------------------------------------- lazy resources

    def _source(self, opener):
        # Kept while tasks keep naming the same zip: building the ZipFile parses the whole
        # central directory, which must happen once per process per zip, not once per task.
        # Openers are compared by value (frozen dataclasses), so an unpickled copy of the
        # same opener counts as the same zip.
        same = self._src is not None and (opener is self._opener or opener == self._opener)
        if not same:
            self.close()
            self._src = opener()
            self._zf = self._src.zipfile()
            self._opener = opener
        return self._src, self._zf

    def _get_embedder(self):
        if not self._embedder_ready:
            if self.embedder_name == STUB_EMBEDDER:
                from gpclean.clipmodel import StubEmbedder

                self._embedder = StubEmbedder()
            elif self.embedder_name != "none":
                from gpclean.clipmodel import get_image_embedder

                self._embedder = get_image_embedder(self.embedder_name, threads=self.threads)
            self._embedder_ready = True
        return self._embedder

    def warm_up(self, opener) -> str | None:
        """Open ``opener``'s zip and load the embedder now; return an abort name or None.

        The scan parent calls this in a fresh process before handing it a member suspected
        of crashing a worker: if the process then dies, it was that member, not the setup.
        A failure to open the zip is returned (the backend is failing); a failure to load the
        embedder is raised (nothing about the zip explains it).
        """
        try:
            self._source(opener)
        except Exception as exc:  # noqa: BLE001 - any failure to open the zip stops the shard
            log.error("cannot open zip source (%s)", type(exc).__name__)
            return type(exc).__name__
        self._get_embedder()
        return None

    def close(self) -> None:
        """Close the zip source (safe to call more than once)."""
        if self._src is not None:
            self._src.close()
        self._src = None
        self._zf = None
        self._opener = None

    # ------------------------------------------------------------------------ the task

    def run(self, opener, task) -> dict:
        """Process one task of ``opener``'s zip and return its rows.

        Returns ``{"task": n, "items": [(row, grid, preview) ...], "sidecars": [row ...],
        "stats": {"bytes_fetched", "bytes_discarded", "requests"}, "abort": None | str}``.
        ``items`` rows carry every ``items_raw`` column that applies (``emb`` included, ``err``
        set on failure, with ``grid``/``preview`` then ``None``). When ``abort`` is set (the
        name of the backend exception), the other fields are meaningless: the shard must stop.
        """
        task_no, members = task
        try:
            src, zf = self._source(opener)
        except Exception as exc:  # noqa: BLE001 - any failure to open the zip stops the shard
            log.error("task %d: cannot open zip source (%s)", task_no, type(exc).__name__)
            return _aborted(task_no, exc)
        before = src.stats
        try:
            items, sidecars = self._read_members(src, zf, members)
        except _Abort as exc:
            # Drop the failing source: this process serves later shards too, and must not
            # keep reading through a connection that has been marked broken.
            self.close()
            return _aborted(task_no, exc.cause)
        after = src.stats
        stats = {k: after.get(k, 0) - before.get(k, 0) for k in after}
        return {"task": task_no, "items": items, "sidecars": sidecars, "stats": stats,
                "abort": None}

    def _read_members(self, src, zf: zipfile.ZipFile, members) -> tuple[list, list]:
        infos = zf.infolist()
        entries = [entry for entry, _ in members]
        # A fresh request at the task's first member: the gap before it may hold a member
        # nobody asked for (a video), which must never be read through.
        src.skip_gap(entries[0].header_offset)
        src.plan_span(entries)

        items: list[tuple[dict, bytes | None, bytes | None]] = []
        work_images: list[tuple[int, Image.Image]] = []  # (index into items, CLIP input)
        sidecars: list[dict] = []
        for entry, mc in members:
            info = infos[entry.member_idx]
            if info.filename != entry.name:
                # The entry list does not describe this zip: a caller bug, not a data problem.
                raise ValueError("entry list does not match the zip's central directory")
            data, err = _read_member(src, zf, info, entry.member_idx)
            if mc.kind == "image":
                row = item_row(entry, mc, err)
                grid = preview = None
                if data is not None:
                    try:
                        values, grid, preview, work = process_image(data, self.cfg)
                    except Exception as exc:  # noqa: BLE001 - one bad photo must not stop a shard
                        # decode() documents OSError / DecompressionBombError; anything else is
                        # unexpected but still about this one member's bytes.
                        _log_item_error(entry.member_idx, exc)
                        row["err"] = type(exc).__name__
                    else:
                        row.update(values)
                        work_images.append((len(items), work))
                items.append((row, grid, preview))
                del data  # never hold more than one member's bytes at a time
            else:  # sidecar
                row = sidecar_row(entry, mc, err)
                if data is not None:
                    row.update(parse_sidecar(data))
                sidecars.append(row)

        embedder = self._get_embedder() if work_images else None
        if embedder is not None:
            # One batch per task: CLIP is much faster batched than image by image.
            vecs = embedder.embed([img for _, img in work_images])
            vecs = np.asarray(vecs, dtype="<f2")
            for (i, _), vec in zip(work_images, vecs, strict=True):
                items[i][0]["emb"] = vec.tobytes()
        return items, sidecars


def item_row(entry, mc, err: str | None = None) -> dict:
    """The central-directory part of an ``items_raw`` row (what is known without the bytes)."""
    return {
        "member_idx": entry.member_idx, "member": entry.name,
        "folder": mc.folder, "folder_kind": mc.folder_kind,
        "filename": mc.filename, "ext": ext_of(mc.filename),
        "file_size": entry.file_size, "crc32": entry.crc,
        "emb": None, "err": err,
    }


def sidecar_row(entry, mc, err: str | None = None) -> dict:
    """The central-directory part of a ``sidecars_raw`` row."""
    row = {
        "member_idx": entry.member_idx, "member": entry.name,
        "folder": mc.folder, "folder_kind": mc.folder_kind,
        "json_name": mc.filename,
    }
    if err is not None:
        row["err"] = err
    return row


def crashed_result(task_no: int, members) -> dict:
    """A task result recording every member of ``members`` as ``err = WORKER_CRASH``.

    Built by the scan parent for a member whose processing killed the worker process, so the
    shard can complete with that one member marked instead of failing on every pass.
    """
    items, sidecars = [], []
    for entry, mc in members:
        if mc.kind == "image":
            items.append((item_row(entry, mc, WORKER_CRASH), None, None))
        else:
            sidecars.append(sidecar_row(entry, mc, WORKER_CRASH))
    return {"task": task_no, "items": items, "sidecars": sidecars, "stats": {}, "abort": None}


class _Abort(Exception):
    """Internal: the backend failed; carries the original exception."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(type(cause).__name__)
        self.cause = cause


def _read_member(src, zf: zipfile.ZipFile, info: zipfile.ZipInfo,
                 member_idx: int) -> tuple[bytes | None, str | None]:
    """Read one member's bytes: ``(data, None)`` or ``(None, "<ExceptionClass>")``.

    Raises ``_Abort`` when the failure is the backend's rather than this member's.
    """
    try:
        with zf.open(info) as f:
            return f.read(), None
    except RangeReadError as exc:
        # Retryable or not, a range-read failure is about the connection, the server or the
        # file as a whole (5xx, quota, 404, size change), never about one member's content.
        # Recording it per item would checkpoint a shard full of false errors.
        raise _Abort(exc) from exc
    except Exception as exc:  # noqa: BLE001 - anything zipfile raises for one member
        # Besides READ_ERRORS, zipfile raises RuntimeError for an encrypted member and
        # NotImplementedError for an unsupported compression method (the method field is
        # not covered by any CRC). None of that should fail the whole shard forever.
        if src.broken:
            raise _Abort(exc) from exc
        _log_item_error(member_idx, exc)
        return None, type(exc).__name__


def _log_item_error(member_idx: int, exc: BaseException) -> None:
    # Member index and exception class only: names and messages can carry personal data.
    log.warning("member %d failed: %s", member_idx, type(exc).__name__)


def _aborted(task_no: int, exc: BaseException) -> dict:
    return {"task": task_no, "items": [], "sidecars": [], "stats": {},
            "abort": type(exc).__name__}


# ------------------------------------------------------------------------- pool plumbing

_WORKER: Worker | None = None


def init_worker(cfg: ScanConfig, embedder_name: str) -> None:
    """Process-pool initializer. Only cheap setup happens here.

    Opening a zip and loading CLIP are deferred to the first task (or ``pool_warm_up``):
    this process serves shards of many zips, and a failure there must be reported against
    the shard at hand rather than break the pool.
    """
    global _WORKER
    publiclog.disable_public()  # workers never write to the public CI console
    # One thread per process: the pool already uses every core, and torch reads this at import.
    os.environ["OMP_NUM_THREADS"] = "1"
    configure_pillow(cfg.max_pixels)  # registers the HEIF opener and pixel limits
    _WORKER = Worker(cfg, embedder_name, threads=1)


def _worker() -> Worker:
    if _WORKER is None:  # pragma: no cover - the executor always runs init_worker first
        raise RuntimeError("scan worker used without init_worker")
    return _WORKER


def pool_task(job) -> dict:
    """Pool task function: ``job = (opener, task)``, run on this process's :class:`Worker`."""
    opener, task = job
    return _worker().run(opener, task)


def pool_warm_up(opener) -> str | None:
    """Pool function: :meth:`Worker.warm_up` on this process's worker."""
    return _worker().warm_up(opener)
