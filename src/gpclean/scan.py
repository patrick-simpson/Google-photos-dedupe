"""Scan: plan shards over a Takeout zip and turn each shard into a meta DB plus a thumb pack.

A **shard** is a contiguous slice of one zip's offset-sorted central directory holding about
``cfg.photos_per_shard`` photos (PLAN §2 "plan", §4). Scanning one shard:

1. classifies every member of the slice by name (``takeout.members.classify``). Videos and
   skipped members are recorded from the central directory alone; their bytes are never read;
2. cuts the wanted members (photos + sidecar JSON) into tasks of at most 48 members that sit
   next to each other in the file, so one range request serves a task and never reaches into
   an unwanted member;
3. runs the tasks on a :class:`ScanPool`: ``spawn`` worker processes (or in-process for
   ``workers == 1``; the same task function either way, see ``gpclean.scanworker``). A pool
   can be kept for a whole job, so each process loads CLIP once, not once per shard;
4. streams every task result straight into the shard meta DB and the thumb pack as it
   arrives (a shard's photos are never all held in memory), then writes ``shard_info``.

Both files are written under temporary names and renamed into place at the very end, pack
first and meta last: a meta file exists only for a complete shard, so it is the checkpoint.
A backend failure (``ShardAborted``) or an exceeded deadline (``ShardTimeout``) leaves nothing
behind and the shard is simply scanned again by a later pass.

A worker process that dies (a native decoder crash, an out-of-memory kill) does not hang or
fail the shard: the unfinished tasks are re-run one at a time in a fresh process, and the
member that kills it again is recorded with ``err = "WorkerCrash"``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import logging
import multiprocessing
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from bisect import bisect_left
from concurrent.futures import Future, ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from gpclean import scanworker
from gpclean.config import MergeConfig, ScanConfig
from gpclean.schema import PACK_DDL, SHARD_DDL, create_transport_db, finalize_transport_db, open_readonly
from gpclean.takeout.members import Entry, MemberClass, classify, list_entries
from gpclean.takeout.names import export_id as export_id_of
from gpclean.takeout.zipsource import ZipSource
from gpclean.version import CODE_VERSION, EXTRACT_VERSION

log = logging.getLogger(__name__)

TASK_MAX = scanworker.TASK_MAX
# Pool workers are replaced after this many tasks (bounds slow leaks in native decoders).
MAX_TASKS_PER_CHILD = 1000
# More members than this killing a worker in one shard is not bad data but a broken setup
# (e.g. every CLIP batch running out of memory): abort instead of marking the whole shard.
MAX_CRASHES_PER_SHARD = 8
# Default worker cap for a local run with CLIP: each process holds its own model (~1.5 GB).
LOCAL_CLIP_WORKERS_MAX = 4
_GIB = 1024 ** 3
_HASH_CHUNK = 1024 * 1024


class ShardAborted(RuntimeError):
    """The zip backend failed mid-shard (retries exhausted, quota, lost file).

    Nothing was written; the shard can be scanned again later.
    """


class ShardTimeout(RuntimeError):
    """The deadline passed before the shard finished. Nothing was written."""


# ------------------------------------------------------------------------------ planning


@dataclass(frozen=True)
class ShardSpec:
    """One shard: the slice ``[start, end)`` of a zip's offset-sorted entry list."""

    zipkey: str
    zip_name: str
    export_id: str
    shard: int
    start: int
    end: int
    n_images: int


def zipkey_for(drive_id: str, size: int, modtime: str) -> str:
    """Stable 12-hex key of a zip on Drive: ``sha256(drive_id|size|modtime)[:12]``.

    A re-uploaded or changed file gets a new key, so its old shards are never reused.
    """
    blob = f"{drive_id}|{int(size)}|{modtime}".encode()
    return hashlib.sha256(blob).hexdigest()[:12]


def zipkey_local(path: Path) -> str:
    """Key of a local zip from its name, size and (whole-second) modification time."""
    path = Path(path)
    st = path.stat()
    return zipkey_for(f"local:{path.name}", st.st_size, str(int(st.st_mtime)))


def _sorted_entries(entries: list[Entry]) -> list[Entry]:
    # The same order list_entries() uses; re-sorting makes plan and scan agree even if a
    # caller hands over the list in central-directory order.
    return sorted(entries, key=lambda e: (e.header_offset, e.member_idx))


def _classify(entry: Entry, cfg: ScanConfig) -> MemberClass:
    return classify(entry.name, include_albums=cfg.include_albums, file_size=entry.file_size,
                    max_member_bytes=cfg.max_member_bytes)


def plan_shards(entries: list[Entry], cfg: ScanConfig, *, zipkey: str,
                zip_name: str) -> list[ShardSpec]:
    """Cut a zip's entries into contiguous shards of about ``cfg.photos_per_shard`` photos.

    Every entry (photos, sidecars, videos, skipped members) lands in exactly one shard, so a
    zip's sidecars and videos are recorded even when it holds no photo at all (it then gets a
    single shard). A new shard starts at every ``photos_per_shard``-th photo; the last shard
    absorbs the remainder (it holds between 1x and 2x the target). The result depends only on
    the entries and the config, so re-planning the same zip gives the same table.
    """
    ordered = _sorted_entries(entries)
    image_pos = [i for i, e in enumerate(ordered) if _classify(e, cfg).kind == "image"]
    per = cfg.photos_per_shard
    n_shards = max(1, len(image_pos) // per)
    # Shard k (k >= 1) starts at the entry of photo number k * per.
    starts = [0] + [image_pos[k * per] for k in range(1, n_shards)]
    ends = starts[1:] + [len(ordered)]
    eid = export_id_of(zip_name)
    specs = []
    for k, (start, end) in enumerate(zip(starts, ends, strict=True)):
        # image_pos is sorted, so counting the photos of a slice is two binary searches.
        n_images = bisect_left(image_pos, end) - bisect_left(image_pos, start)
        specs.append(ShardSpec(zipkey=zipkey, zip_name=zip_name, export_id=eid, shard=k,
                               start=start, end=end, n_images=n_images))
    return specs


# ------------------------------------------------------------------------------- openers


@dataclass(frozen=True)
class LocalOpener:
    """Picklable opener for a zip on local disk (``repr`` hides the path)."""

    path: Path = field(repr=False)

    def __call__(self) -> ZipSource:
        return ZipSource.open_local(self.path)


@dataclass(frozen=True)
class HttpOpener:
    """Picklable opener for a zip served over HTTP (``rclone serve http``).

    ``tail`` comes from the parent's ``ZipSource.tail()`` so workers never fetch the central
    directory again. The URL names the user's files, so ``repr`` leaves it out.

    ``tail_file`` is set only by :class:`ScanPool`: the tail (megabytes for a big zip) is then
    written to a private temp file once per zip and read from there by each worker process,
    instead of being pickled into every task.
    """

    url: str = field(repr=False)
    size: int
    tail: bytes = field(repr=False)
    timeout: float = 60.0
    retries: int = 5
    backoff: float = 1.0
    tail_file: str | None = field(default=None, repr=False)

    def __call__(self) -> ZipSource:
        tail = self.tail
        if not tail and self.tail_file is not None:
            tail = Path(self.tail_file).read_bytes()
        return ZipSource.open_http(self.url, self.size, tail or None, timeout=self.timeout,
                                   retries=self.retries, backoff=self.backoff)


# --------------------------------------------------------------------------- worker pool


_FETCHED_MODELS: set[str] = set()


def _prepare_embedder(embedder_name: str) -> None:
    """Fetch and verify real CLIP weights once, in the parent, before any worker starts.

    Workers then only load the verified file (several processes downloading the same 600 MB
    file at once would race and waste egress).
    """
    if embedder_name in ("none", scanworker.STUB_EMBEDDER) or embedder_name in _FETCHED_MODELS:
        return
    from gpclean.clipmodel import fetch_model

    fetch_model(embedder_name)
    _FETCHED_MODELS.add(embedder_name)


def _check_embedder(cfg: ScanConfig, embedder_name: str) -> None:
    # The shard's cfg hash says which model made its embeddings; b32 and b16 are both 512-d,
    # so a mismatch would silently mix two embedding spaces in the merge.
    if embedder_name not in (cfg.clip_model, scanworker.STUB_EMBEDDER):
        raise ValueError("embedder_name does not match cfg.clip_model")


def _remaining(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    left = deadline - time.monotonic()
    if left <= 0:
        raise ShardTimeout("shard deadline passed")
    return left


def _wait(fut: Future, deadline: float | None):
    """``fut.result()`` bounded by the shard deadline."""
    try:
        return fut.result(timeout=_remaining(deadline))
    except TimeoutError:
        raise ShardTimeout("shard deadline passed") from None


def _kill(executor: ProcessPoolExecutor) -> None:
    """Shut ``executor`` down now, terminating its processes (they may be stuck in a task)."""
    terminate = getattr(executor, "terminate_workers", None)  # Python 3.14+
    if terminate is not None:
        with contextlib.suppress(Exception):
            terminate()
    else:
        # No public API before 3.14; ``_processes`` (pid -> Process) exists since 3.2.
        for proc in list((getattr(executor, "_processes", None) or {}).values()):
            with contextlib.suppress(Exception):
                proc.terminate()
    executor.shutdown(wait=True, cancel_futures=True)


class ScanPool:
    """Scan worker process(es) for one ``(workers, cfg, embedder)``, reusable across shards.

    Starting a spawn process, importing torch and loading CLIP takes several seconds per
    process; a pool kept for a whole job (``with ScanPool(...) as pool:``, then
    ``scan_shard(..., pool=pool)`` per shard, across zips) pays that once per process. Each
    process keeps the ZipSource of the zip it saw last, so switching zips costs one central
    directory parse per process.

    ``workers <= 1`` runs tasks in this process. Processes are started only when the first
    task needs one. Not thread-safe: one shard at a time.
    """

    def __init__(self, workers: int, cfg: ScanConfig, embedder_name: str) -> None:
        _check_embedder(cfg, embedder_name)
        self.workers = max(1, int(workers))
        self.cfg = cfg
        self.embedder_name = embedder_name
        self._executor: ProcessPoolExecutor | None = None
        self._local: scanworker.Worker | None = None  # the in-process worker (workers == 1)
        self._tails_dir: Path | None = None
        self._light: dict[tuple, HttpOpener] = {}
        self._closed = False

    def __enter__(self) -> ScanPool:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        """Stop the worker processes and delete the temp tail files (idempotent)."""
        self._closed = True
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._executor = None
        if self._local is not None:
            self._local.close()
            self._local = None
        if self._tails_dir is not None:
            shutil.rmtree(self._tails_dir, ignore_errors=True)
            self._tails_dir = None
            self._light.clear()

    # --------------------------------------------------------------------- internals

    def _new_executor(self, n: int) -> ProcessPoolExecutor:
        # spawn on every OS: identical behaviour on Windows and Linux, and no fork-inherited
        # state (open connections, locks, torch threads) in the workers. Unlike
        # multiprocessing.Pool, a process that dies breaks this executor (BrokenProcessPool)
        # instead of being silently replaced while its task is lost.
        return ProcessPoolExecutor(
            max_workers=n,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=scanworker.init_worker,
            initargs=(self.cfg, self.embedder_name),
            max_tasks_per_child=MAX_TASKS_PER_CHILD,
        )

    def _drop_executor(self) -> None:
        if self._executor is not None:
            _kill(self._executor)
            self._executor = None

    def _worker_opener(self, opener):
        """The opener as sent with every task: an ``HttpOpener``'s tail moves to a temp file."""
        if not isinstance(opener, HttpOpener) or not opener.tail:
            return opener
        key = (dataclasses.replace(opener, tail=b""), hashlib.sha256(opener.tail).digest())
        light = self._light.get(key)
        if light is None:
            if self._tails_dir is None:
                self._tails_dir = Path(tempfile.mkdtemp(prefix="gpclean-tail-"))
            path = self._tails_dir / f"{len(self._light)}.tail"
            path.write_bytes(opener.tail)
            light = dataclasses.replace(opener, tail=b"", tail_file=str(path))
            self._light[key] = light
        return light

    # ------------------------------------------------------------------------ running

    def run(self, opener, tasks: list, deadline: float | None = None):
        """Yield the results of ``tasks`` on ``opener``'s zip as they complete.

        A generator so the caller can write each result before the next one arrives. Raises
        ``ShardTimeout`` past ``deadline`` and ``ShardAborted`` when worker processes cannot
        even start. Closing it early cancels this shard's remaining tasks.
        """
        if self._closed:
            raise RuntimeError("ScanPool is closed")
        if not tasks:
            return
        _remaining(deadline)
        _prepare_embedder(self.embedder_name)
        if self.workers <= 1:
            if self._local is None:
                self._local = scanworker.Worker(self.cfg, self.embedder_name)
            for task in tasks:
                _remaining(deadline)
                yield self._local.run(opener, task)
            return
        yield from self._run_pool(self._worker_opener(opener), tasks, deadline)

    def _run_pool(self, opener, tasks: list, deadline: float | None):
        if self._executor is None:
            self._executor = self._new_executor(self.workers)
        executor = self._executor
        futs: dict[Future, tuple] = {}
        yielded: set[Future] = set()
        finished = False
        try:
            try:
                for task in tasks:
                    futs[executor.submit(scanworker.pool_task, (opener, task))] = task
            except BrokenProcessPool:
                pass  # a process died since the last shard; handled like a death below
            broken = len(futs) < len(tasks)
            if not broken:
                try:
                    for fut in as_completed(futs, timeout=_remaining(deadline)):
                        try:
                            res = fut.result()
                        except BrokenProcessPool:
                            broken = True
                            break
                        yielded.add(fut)
                        yield res
                except TimeoutError:
                    raise ShardTimeout("shard deadline passed") from None
            if broken:
                log.warning("a scan worker process died; re-running unfinished tasks one by one")
                self._drop_executor()
                left = []
                for fut, task in futs.items():
                    if fut in yielded:
                        continue
                    if fut.done() and not fut.cancelled() and fut.exception() is None:
                        yield fut.result()  # finished before the death was noticed
                    else:
                        left.append(task)
                left.extend(tasks[len(futs):])
                yield from self._isolate(opener, left, deadline)
            finished = True
        finally:
            if not finished:
                for fut in futs:
                    fut.cancel()
                if any(not fut.done() for fut in futs):
                    # Tasks of this shard are still running (maybe stuck, after a timeout):
                    # kill them rather than let them hold up the next shard.
                    self._drop_executor()

    def _single(self, opener, deadline: float | None) -> ProcessPoolExecutor:
        """A fresh one-process executor that has opened the zip and loaded the embedder.

        The warm-up makes a later death attributable: if the process dies here, the setup is
        at fault (the shard is aborted); if it dies on a member afterwards, that member is.
        """
        executor = self._new_executor(1)
        try:
            abort = _wait(executor.submit(scanworker.pool_warm_up, opener), deadline)
        except BrokenProcessPool:
            _kill(executor)
            raise ShardAborted("scan worker process died while starting") from None
        except BaseException:
            _kill(executor)
            raise
        if abort:
            _kill(executor)
            raise ShardAborted(f"zip backend failed ({abort})")
        return executor

    def _isolate(self, opener, tasks: list, deadline: float | None):
        """Re-run tasks left over by a dead worker process, finding the member that kills it.

        Tasks run one at a time in a single process, so a death points at the running task;
        that task is then re-run member by member, and a member that kills the process again
        is recorded as ``WorkerCrash`` (the shard still completes). Slower than the pool, but
        only the leftovers of a shard that hit a crash take this path.
        """
        executor: ProcessPoolExecutor | None = None
        crashes = 0

        def attempt(task):
            # The task's result, or None if the process died running it.
            nonlocal executor
            if executor is None:
                executor = self._single(opener, deadline)
            try:
                return _wait(executor.submit(scanworker.pool_task, (opener, task)), deadline)
            except BrokenProcessPool:
                _kill(executor)
                executor = None
                return None

        ok = False
        try:
            for task in sorted(tasks, key=lambda t: t[0]):
                res = attempt(task)
                if res is not None:
                    yield res
                    continue
                task_no, members = task
                for member in members:
                    res = attempt((task_no, [member]))
                    if res is None:
                        crashes += 1
                        log.warning("member %d killed the scan worker process",
                                    member[0].member_idx)
                        if crashes > MAX_CRASHES_PER_SHARD:
                            raise ShardAborted("scan worker processes keep dying")
                        res = scanworker.crashed_result(task_no, [member])
                    yield res
            ok = True
        finally:
            if executor is not None:
                if ok:
                    executor.shutdown(wait=True)
                else:
                    _kill(executor)


# ------------------------------------------------------------------------------ scanning


def _build_tasks(members: list[tuple[Entry, MemberClass]]) -> list[tuple[int, list]]:
    """Runs of at most TASK_MAX wanted members that are adjacent in the file.

    Any unwanted member (video, skipped) ends a run, so no task spans its bytes.
    """
    tasks: list[tuple[int, list]] = []
    run: list[tuple[Entry, MemberClass]] = []
    for entry, mc in members:
        if mc.kind in ("image", "sidecar"):
            run.append((entry, mc))
            if len(run) == TASK_MAX:
                tasks.append((len(tasks), run))
                run = []
        elif run:
            tasks.append((len(tasks), run))
            run = []
    if run:
        tasks.append((len(tasks), run))
    return tasks


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _inserter(conn: sqlite3.Connection, table: str):
    """Return ``insert(row_dict)`` for ``table``; columns missing from the dict are NULL."""
    cols = _columns(conn, table)
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"

    def insert(row: dict) -> None:
        conn.execute(sql, [row.get(c) for c in cols])

    return insert


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_HASH_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _part(path: Path) -> Path:
    return path.with_name(path.name + ".part")


def scan_shard(opener, spec: ShardSpec, entries: list[Entry], cfg: ScanConfig, *,
               meta_path: Path, pack_path: Path, workers: int, embedder_name: str,
               deadline: float | None = None, pool: ScanPool | None = None) -> dict:
    """Scan one shard into ``meta_path`` (SHARD_DDL) and ``pack_path`` (PACK_DDL).

    ``opener`` is a picklable callable returning a ``ZipSource`` (``LocalOpener`` /
    ``HttpOpener``); ``entries`` is the zip's whole entry list (``spec`` slices it).
    ``embedder_name`` is ``cfg.clip_model`` (``"b32"``/``"b16"``/``"none"``) or ``"stub"``.
    ``workers <= 1`` runs in-process; more uses spawn worker processes. ``deadline`` is an
    absolute ``time.monotonic()`` value. ``pool`` is a :class:`ScanPool` made for the same
    ``cfg`` and embedder to reuse across shards (``workers`` is then ignored); without one, a
    pool is made for this shard and closed afterwards.

    Returns the ``shard_info`` dict (native types). Raises ``ShardAborted`` when the zip
    backend fails and ``ShardTimeout`` past the deadline; in both cases neither file exists
    afterwards (an older meta file for this shard is removed up front, since the pack it
    describes is about to be replaced).
    """
    meta_path, pack_path = Path(meta_path), Path(pack_path)
    _check_embedder(cfg, embedder_name)
    if pool is not None and (pool.cfg != cfg or pool.embedder_name != embedder_name):
        raise ValueError("the ScanPool was made for another config or embedder")
    ordered = _sorted_entries(entries)
    if not (0 <= spec.start <= spec.end <= len(ordered)):
        raise ValueError("shard range does not fit the entry list")
    if pool is None:
        with ScanPool(workers, cfg, embedder_name) as own_pool:
            return _scan_into(own_pool, opener, spec, ordered, cfg, meta_path, pack_path,
                              embedder_name, deadline)
    return _scan_into(pool, opener, spec, ordered, cfg, meta_path, pack_path, embedder_name,
                      deadline)


def _scan_into(pool: ScanPool, opener, spec: ShardSpec, ordered: list[Entry], cfg: ScanConfig,
               meta_path: Path, pack_path: Path, embedder_name: str,
               deadline: float | None) -> dict:
    """The body of :func:`scan_shard` (arguments already checked)."""
    started = _utc_now()
    members = [(e, _classify(e, cfg)) for e in ordered[spec.start:spec.end]]
    # Sidecars are tiny; one bigger than the member cap is not a Takeout sidecar. Skip it
    # rather than read an arbitrarily large blob into memory.
    members = [
        (e, MemberClass("skip", mc.folder, mc.folder_kind, mc.filename, "too_large"))
        if mc.kind == "sidecar" and e.file_size > cfg.max_member_bytes else (e, mc)
        for e, mc in members
    ]
    tasks = _build_tasks(members)

    meta_path.unlink(missing_ok=True)  # the checkpoint goes first: it is about to be stale
    meta_tmp, pack_tmp = _part(meta_path), _part(pack_path)
    meta = create_transport_db(meta_tmp, SHARD_DDL)
    try:
        pack = create_transport_db(pack_tmp, PACK_DDL)
    except BaseException:
        meta.close()
        meta_tmp.unlink(missing_ok=True)
        raise
    try:
        # Videos and skipped members: central-directory facts only.
        n_videos = n_skipped = 0
        for e, mc in members:
            if mc.kind == "video":
                meta.execute(
                    "INSERT INTO videos_raw VALUES (?, ?, ?, ?, ?, ?)",
                    (e.member_idx, e.name, mc.folder, mc.folder_kind, mc.filename, e.file_size),
                )
                n_videos += 1
            elif mc.kind == "skip":
                meta.execute("INSERT INTO skipped_raw VALUES (?, ?, ?)",
                             (e.member_idx, e.name, mc.reason or "other"))
                n_skipped += 1

        insert_item = _inserter(meta, "items_raw")
        insert_sidecar = _inserter(meta, "sidecars_raw")
        n_items = n_err = n_sidecars = 0
        bytes_read = bytes_discarded = 0
        with contextlib.closing(pool.run(opener, tasks, deadline)) as results:
            for res in results:
                if res["abort"]:
                    raise ShardAborted(f"zip backend failed ({res['abort']})")
                for row, grid, preview in res["items"]:
                    insert_item(row)
                    n_items += 1
                    if row.get("err"):
                        n_err += 1
                    elif grid is not None and preview is not None:
                        pack.execute("INSERT INTO t VALUES (?, ?, ?)",
                                     (row["member_idx"], grid, preview))
                for row in res["sidecars"]:
                    insert_sidecar(row)
                    n_sidecars += 1
                bytes_read += res["stats"].get("bytes_fetched", 0)
                bytes_discarded += res["stats"].get("bytes_discarded", 0)
                # Commit per task: keeps the SQLite page cache, not the process, holding rows.
                meta.commit()
                pack.commit()
        _remaining(deadline)  # a deadline passed during the last task still counts

        finalize_transport_db(pack)
        pack_sha = _sha256_file(pack_tmp)
        pack_size = pack_tmp.stat().st_size

        info = {
            "zipkey": spec.zipkey, "zip_name": spec.zip_name, "export_id": spec.export_id,
            "shard": spec.shard, "cfg": cfg.cfg_hash(), "extract_version": EXTRACT_VERSION,
            "code_version": CODE_VERSION, "start": spec.start, "end": spec.end,
            "n_items": n_items, "n_err": n_err, "n_sidecars": n_sidecars,
            "n_videos": n_videos, "n_skipped": n_skipped,
            "bytes_read": bytes_read, "bytes_discarded": bytes_discarded,
            "pack_name": pack_path.name, "pack_sha256": pack_sha, "pack_size": pack_size,
            "started": started, "finished": _utc_now(), "clip_model": embedder_name,
        }
        meta.executemany("INSERT INTO shard_info (key, value) VALUES (?, ?)",
                         [(k, str(v)) for k, v in info.items()])
        finalize_transport_db(meta)

        # Pack first, meta last: the meta file is the "this shard is done" checkpoint.
        os.replace(pack_tmp, pack_path)
        os.replace(meta_tmp, meta_path)
    except BaseException:
        # close() is idempotent, so this is right whether or not finalize got to close them;
        # an open connection would make the unlink fail on Windows and hide the real error.
        for conn in (meta, pack):
            with contextlib.suppress(Exception):
                conn.close()
        meta_tmp.unlink(missing_ok=True)
        pack_tmp.unlink(missing_ok=True)
        raise
    log.info("shard %d done: %d items (%d errors), %d sidecars, %d videos, %d skipped",
             spec.shard, n_items, n_err, n_sidecars, n_videos, n_skipped)
    return info


def read_shard_info(meta_path: Path) -> dict[str, str] | None:
    """The ``shard_info`` table of a meta file as ``{key: text}``, or None if unreadable."""
    try:
        conn = open_readonly(meta_path)
    except sqlite3.Error:
        return None
    try:
        return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM shard_info")}
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def shard_is_current(meta_path: Path, pack_path: Path, cfg_hash: str) -> bool:
    """True when a finished shard with this cfg and extract version (and its pack) exists."""
    if not (Path(meta_path).is_file() and Path(pack_path).is_file()):
        return False
    info = read_shard_info(meta_path)
    return bool(info) and info.get("cfg") == cfg_hash \
        and info.get("extract_version") == str(EXTRACT_VERSION)


# ------------------------------------------------------------------------------- run-local


def _say(message: str) -> None:
    """Local progress line on stderr (counts only, never file or folder names)."""
    print(message, file=sys.stderr, flush=True)


def _total_ram_bytes() -> int | None:
    """Physical memory of this machine, or None when it cannot be read cheaply."""
    try:
        if sys.platform == "win32":
            import ctypes

            class MemoryStatusEx(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = MemoryStatusEx()
            status.dwLength = ctypes.sizeof(MemoryStatusEx)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
            return int(status.ullTotalPhys)
        return int(os.sysconf("SC_PHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, ValueError):
        return None


def default_local_workers(model: str) -> int:
    """Worker count for ``run-local --workers 0``.

    Without CLIP, one per CPU. With CLIP every process holds its own model (~1.5 GB), so the
    count is capped at ``LOCAL_CLIP_WORKERS_MAX`` and at one per 3 GB of RAM: a laptop keeps
    to the few GB PLAN budgets instead of swapping (or losing a worker to the OOM killer).
    """
    n = os.cpu_count() or 1
    if model != "none":
        n = min(n, LOCAL_CLIP_WORKERS_MAX)
        ram = _total_ram_bytes()
        if ram:
            n = min(n, int(ram // (3 * _GIB)))
    return max(1, n)


def cli_run_local(zips, out, include_albums, threshold, clip_model, no_clip, workers,
                  photos_per_shard) -> int:
    """``gpclean run-local``: scan every ``*.zip`` in ``zips`` and merge into a bundle at ``out``.

    Layout: ``out/work/<cfg>/<zipkey>/<shard:04d>.meta.sqlite`` (checkpoints),
    ``out/thumbs/<zipkey>-<shard:04d>.sqlite`` (thumb packs) and the bundle files in ``out``.
    Finished shards with the same config are skipped, so an interrupted run resumes.
    ``workers`` 0 picks a memory-aware default (:func:`default_local_workers`).
    """
    zips, out = Path(zips), Path(out)
    model = "none" if no_clip else clip_model
    cfg = ScanConfig(include_albums=bool(include_albums), clip_model=model,
                     photos_per_shard=int(photos_per_shard))
    mcfg = MergeConfig(threshold=int(threshold))
    cfg_hash = cfg.cfg_hash()

    zip_paths = sorted((p for p in zips.glob("*.zip") if p.is_file()), key=lambda p: p.name)
    if not zip_paths:
        _say("run-local: no .zip files found in the --zips folder")
        return 2
    if workers and int(workers) > 0:
        n_workers = int(workers)
    else:
        n_workers = default_local_workers(model)
        _say(f"scan: using {n_workers} worker processes")

    meta_paths: list[Path] = []
    scanned = skipped = 0
    # One pool for the whole run: each worker process loads CLIP once, not once per shard.
    with ScanPool(n_workers, cfg, model) as pool:
        for zi, path in enumerate(zip_paths, 1):
            zipkey = zipkey_local(path)
            with ZipSource.open_local(path) as src:
                entries = list_entries(src.zipfile())
            specs = plan_shards(entries, cfg, zipkey=zipkey, zip_name=path.name)
            for spec in specs:
                meta_path = out / "work" / cfg_hash / zipkey / f"{spec.shard:04d}.meta.sqlite"
                pack_path = out / "thumbs" / f"{zipkey}-{spec.shard:04d}.sqlite"
                meta_paths.append(meta_path)
                label = f"zip {zi}/{len(zip_paths)} shard {spec.shard + 1}/{len(specs)}"
                if shard_is_current(meta_path, pack_path, cfg_hash):
                    skipped += 1
                    _say(f"scan: {label}: already done")
                    continue
                t0 = time.monotonic()
                info = scan_shard(LocalOpener(path), spec, entries, cfg, meta_path=meta_path,
                                  pack_path=pack_path, workers=n_workers, embedder_name=model,
                                  pool=pool)
                scanned += 1
                _say(f"scan: {label}: {info['n_items']} photos ({info['n_err']} errors), "
                     f"{info['n_sidecars']} sidecars, {info['n_videos']} videos "
                     f"in {time.monotonic() - t0:.1f} s")
    _say(f"scan: {scanned} shards scanned, {skipped} already done")

    # Imported here: the merge is only needed once every shard exists.
    from gpclean.merge.bundle import build_bundle

    result = build_bundle(meta_paths, out / "thumbs", out, mcfg, cfg_hash=cfg_hash,
                          clip_model=model)
    counts = result.get("counts", {}) if isinstance(result, dict) else {}
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())
                        if isinstance(v, int) and not isinstance(v, bool))
    _say("merge: bundle written" + (f" ({summary})" if summary else ""))
    return 0
