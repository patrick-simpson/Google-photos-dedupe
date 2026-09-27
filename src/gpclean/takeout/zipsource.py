"""The file object ``zipfile`` reads Takeout zips through, locally or over HTTP.

``ZipSource`` combines two things (docs/PLAN.md §3, "Read path"):

* the zip's **tail** — central directory, ZIP64 end record and locator, end-of-central-directory
  record and comment — held in memory. The main process fetches it once per zip and hands it
  to worker processes (``tail()`` / ``open_http(..., tail=...)``), so opening a ``ZipFile`` in
  a worker costs no network traffic at all;
* a **backing reader** for everything before the tail: ``HTTPRangeFile`` in CI, a plain file
  locally. Above this class the code path is identical.

It also turns the scanner's knowledge ("these members, in this order") into range-request
spans that never reach into a member nobody asked for, so video bytes are never fetched.
"""

from __future__ import annotations

import errno
import bisect
import io
import logging
import os
import struct
import zipfile
from pathlib import Path

from gpclean.takeout.rangefile import HTTPRangeFile

log = logging.getLogger(__name__)

BUFFER_SIZE = 1024 * 1024

_EOCD_SIG = b"PK\x05\x06"
_EOCD_FMT = "<4s4H2LH"  # sig, disk, cd disk, entries here, entries total, cd size, cd offset, comment len
_EOCD_LEN = struct.calcsize(_EOCD_FMT)  # 22
_ZIP64_LOC_SIG = b"PK\x06\x07"
_ZIP64_LOC_FMT = "<4sLQL"  # sig, disk with record, record offset, total disks
_ZIP64_LOC_LEN = struct.calcsize(_ZIP64_LOC_FMT)  # 20
_ZIP64_REC_SIG = b"PK\x06\x06"
_ZIP64_REC_FMT = "<4sQ2H2L4Q"  # sig, record size, versions, disks, entry counts, cd size, cd offset
_ZIP64_REC_LEN = struct.calcsize(_ZIP64_REC_FMT)  # 56
_MAX_COMMENT = 1 << 16  # zipfile searches this far back (one more than the real maximum)

# Local file header layout, used to estimate where a member's data ends. The local "extra"
# field length is not in the central directory, so allow a generous upper bound; the span end
# is clamped to the next member's header anyway, so over-estimating never over-fetches.
_LOCAL_HEADER_LEN = 30
_LOCAL_EXTRA_MARGIN = 1024
_DATA_DESCRIPTOR_MAX = 24  # signature + CRC + two 8-byte ZIP64 sizes


def read_tail(fobj, size: int) -> bytes:
    """Return the zip's tail: everything from the start of the central directory to the end.

    That covers the central directory, the ZIP64 end record and locator (when present), the
    end-of-central-directory record and the archive comment. If the archive has a comment, the
    tail also reaches back over the whole 64 KiB window ``zipfile`` searches for the end
    record, so ``zipfile`` can open the archive from memory alone.

    ``fobj`` is any seekable binary file of ``size`` bytes (``HTTPRangeFile`` or a local
    file). If it has ``hint()``, each read is announced first, so an HTTP reader asks for
    exactly the bytes needed. Raises ``zipfile.BadZipFile`` when no valid end record is found.
    """
    if size < _EOCD_LEN:
        raise zipfile.BadZipFile("file too small to be a zip")
    # Fast path for archives without a comment (Takeout writes none): the last 98 bytes then
    # hold the end record plus, for ZIP64, the locator and ZIP64 end record. Keeping this
    # first read small keeps it inside the central directory, clear of member data.
    win_len = min(size, _EOCD_LEN + _ZIP64_LOC_LEN + _ZIP64_REC_LEN)
    window = _read_exact(fobj, size - win_len, win_len)
    eocd_idx = _find_eocd(window, lax=False)
    keep_window = False
    if eocd_idx < 0 or eocd_idx + _EOCD_LEN != win_len:
        # There is an archive comment (up to 64 KiB) after the end record. zipfile will then
        # search that whole window itself, so fetch it now and keep it in the tail.
        win_len = min(size, _EOCD_LEN + _MAX_COMMENT)
        window = _read_exact(fobj, size - win_len, win_len)
        eocd_idx = _find_eocd(window, lax=True)
        keep_window = True
        if eocd_idx < 0:
            raise zipfile.BadZipFile("end of central directory record not found")
    win_start = size - win_len
    eocd_pos = win_start + eocd_idx
    _, _, _, _, _, cd_size, _, _ = struct.unpack_from(_EOCD_FMT, window, eocd_idx)
    cd_end = eocd_pos  # where the central directory stops (zip64 record or EOCD begins)

    loc_pos = eocd_pos - _ZIP64_LOC_LEN
    if loc_pos >= 0:
        loc = _read_at(fobj, window, win_start, loc_pos, _ZIP64_LOC_LEN)
        if loc[:4] == _ZIP64_LOC_SIG:
            _, _, rec_offset, _ = struct.unpack(_ZIP64_LOC_FMT, loc)
            rec_pos = _find_zip64_record(fobj, window, win_start, loc_pos, rec_offset)
            rec = _read_at(fobj, window, win_start, rec_pos, _ZIP64_REC_LEN)
            cd_size = struct.unpack(_ZIP64_REC_FMT, rec)[8]
            cd_end = rec_pos

    # Position-based, like zipfile itself: correct even if something was prepended to the zip.
    cd_start = cd_end - cd_size
    if cd_start < 0:
        raise zipfile.BadZipFile("central directory size is larger than the file")
    start = min(cd_start, win_start) if keep_window else cd_start
    if start >= win_start:
        return window[start - win_start :]
    return _read_exact(fobj, start, win_start - start) + window


def _find_eocd(window: bytes, *, lax: bool) -> int:
    """Index of the end record in ``window`` (the file's last bytes), or -1.

    Search backwards for a record whose comment ends exactly at end of file (a comment may
    itself contain the signature bytes). With ``lax``, fall back to zipfile's rule of taking
    the last signature whose comment fits.
    """
    fallback = -1
    idx = window.rfind(_EOCD_SIG)
    while idx >= 0:
        if idx + _EOCD_LEN <= len(window):
            comment_len = struct.unpack_from("<H", window, idx + 20)[0]
            if idx + _EOCD_LEN + comment_len == len(window):
                return idx
            if lax and fallback < 0 and idx + _EOCD_LEN + comment_len <= len(window):
                fallback = idx
        idx = window.rfind(_EOCD_SIG, 0, idx)
    return fallback


def _find_zip64_record(fobj, window: bytes, win_start: int, loc_pos: int, rec_offset: int) -> int:
    """Position of the ZIP64 end record: normally right before the locator."""
    for pos in (loc_pos - _ZIP64_REC_LEN, rec_offset):
        if 0 <= pos <= loc_pos - _ZIP64_REC_LEN:
            if _read_at(fobj, window, win_start, pos, 4) == _ZIP64_REC_SIG:
                return pos
    raise zipfile.BadZipFile("ZIP64 end of central directory record not found")


def _read_at(fobj, window: bytes, win_start: int, pos: int, n: int) -> bytes:
    """``n`` bytes at ``pos``, from the already fetched window when possible."""
    if pos >= win_start and pos + n <= win_start + len(window):
        return window[pos - win_start : pos - win_start + n]
    return _read_exact(fobj, pos, n)


def _read_exact(fobj, pos: int, n: int) -> bytes:
    """Read exactly ``n`` bytes at ``pos`` (raw readers may return short reads)."""
    if hasattr(fobj, "hint"):
        fobj.hint(pos, pos + n)
    fobj.seek(pos)
    parts = []
    left = n
    while left > 0:
        chunk = fobj.read(left)
        if not chunk:
            raise zipfile.BadZipFile("unexpected end of file while reading the zip tail")
        parts.append(chunk)
        left -= len(chunk)
    return b"".join(parts)


class _PositionedReader(io.RawIOBase):
    """Seekable raw reader with its own position over a ``_read_at(pos, buf)`` primitive."""

    _pos = 0

    def _total_size(self) -> int:  # pragma: no cover - overridden
        raise NotImplementedError

    def _read_at(self, pos: int, buf: memoryview) -> int:  # pragma: no cover - overridden
        raise NotImplementedError

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        """Set the position; like ``HTTPRangeFile`` no I/O happens until the next read."""
        if whence == io.SEEK_SET:
            new = offset
        elif whence == io.SEEK_CUR:
            new = self._pos + offset
        elif whence == io.SEEK_END:
            new = self._total_size() + offset
        else:
            raise ValueError(f"invalid whence ({whence})")
        if new < 0:
            raise OSError(errno.EINVAL, "negative seek position")  # same as a real file; zipfile catches OSError
        self._pos = new
        return new

    def readinto(self, b) -> int:
        """Read up to ``len(b)`` bytes at the current position (short reads allowed)."""
        if self.closed:
            raise ValueError("I/O operation on closed file")
        n = self._read_at(self._pos, memoryview(b).cast("B"))
        self._pos += n
        return n


class ZipSource(_PositionedReader):
    """A seekable read-only file object for zipfile.ZipFile.

    Serves [cd_offset, size) (EOCD + central directory) from an in-memory tail blob, and the
    rest from a backing file (HTTPRangeFile or a local file).

    Error handling for callers: reading one member can fail with ``OSError`` (which includes
    ``RangeReadError``), ``zipfile.BadZipFile`` (bad CRC-32, bad local header magic, overlapped
    entries) or ``zlib.error`` (corrupt deflate data). Catch all three per item. A
    ``RangeReadError`` with ``retryable`` set, or ``broken`` being true, means the backend
    itself is failing: stop the task instead of moving on to the next item.
    """

    def __init__(self, backing, size: int, tail: bytes) -> None:
        """Wrap ``backing`` (a seekable raw file of ``size`` bytes) with the zip's ``tail``.

        Use ``open_local`` / ``open_http`` rather than calling this directly.
        """
        super().__init__()
        tail = bytes(tail)
        if not tail or len(tail) > size:
            raise ValueError("tail does not fit the file size")
        self._backing = backing
        self._size = size
        self._tail = tail
        self._tail_start = size - len(tail)
        self._spans: list[tuple[int, int]] = []  # current task's planned byte ranges
        self._hinted: tuple[int, int] | None = None  # last span passed to backing.hint()
        # Parsing the central directory from the tail alone also proves the tail belongs to
        # this zip (BadZipFile if not). The ZipFile is kept: building one for 100k members
        # takes about half a second, so it is done once per source, not once per task.
        self._zf, offsets = self._open_zipfile()
        # Sorted local-header offsets of all members, for span planning.
        self._offsets = sorted(o for o in offsets if o < self._tail_start)

    # ------------------------------------------------------------------ constructors

    @classmethod
    def open_local(cls, path: str | Path) -> "ZipSource":
        """Open a zip on local disk. Same interface and counters as the HTTP variant."""
        backing = _LocalFile(Path(path))
        try:
            size = backing.size
            return cls(backing, size, read_tail(backing, size))
        except BaseException:
            backing.close()
            raise

    @classmethod
    def open_http(
        cls,
        url: str,
        size: int,
        tail: bytes | None = None,
        *,
        timeout: float = 60.0,
        retries: int = 5,
        backoff: float = 1.0,
    ) -> "ZipSource":
        """Open a zip served over HTTP with range support (``rclone serve http``).

        Pass ``tail`` (from another ZipSource's ``tail()``) to skip fetching it again.
        """
        backing = HTTPRangeFile(url, size, timeout=timeout, retries=retries, backoff=backoff)
        try:
            if tail is None:
                tail = read_tail(backing, size)
            return cls(backing, size, tail)
        except BaseException:
            backing.close()
            raise

    # -------------------------------------------------------------------- public API

    @property
    def size(self) -> int:
        """Total size of the zip file in bytes."""
        return self._size

    @property
    def broken(self) -> bool:
        """True while the backing reader is failing (an HTTP read exhausted its retries)."""
        return bool(getattr(self._backing, "broken", False))

    def tail(self) -> bytes:
        """The in-memory tail blob, to hand to worker processes."""
        return self._tail

    def zipfile(self, *, fresh: bool = False) -> zipfile.ZipFile:
        """The ``ZipFile`` for this source, reading through a 1 MiB ``io.BufferedReader``.

        The same instance is returned on every call (it is shared per source), so calling
        this once per task costs nothing. Sharing is safe in a single-threaded worker:
        ``zipfile`` seeks before every read. If the shared instance was closed, a new one is
        built. ``fresh=True`` always builds an independent instance (re-parsing the central
        directory). Closing or garbage-collecting a returned ZipFile never closes this source.
        """
        if fresh:
            return zipfile.ZipFile(io.BufferedReader(_View(self), BUFFER_SIZE))
        if self._zf.fp is None:  # ZipFile.close() clears fp
            self._zf, _ = self._open_zipfile()
        return self._zf

    def plan_span(self, entries) -> None:
        """Hint the backing reader with the byte ranges of the members about to be read.

        ``entries`` are central-directory records (anything with ``name``, ``header_offset``
        and ``compress_size``), normally one task's run of consecutive wanted members. Each
        span runs from the first member's local header to the last member's data end, and is
        clamped to the next member's header, so a request never covers a member that was not
        asked for. If the entries are *not* consecutive in the file, they are split into
        several spans, each read with its own request.
        """
        self._spans = []
        self._hinted = None
        if not entries:
            return
        ordered = sorted(entries, key=lambda e: e.header_offset)
        runs: list[list] = [[ordered[0]]]
        for entry in ordered[1:]:
            if self._next_boundary(runs[-1][-1].header_offset) == entry.header_offset:
                runs[-1].append(entry)
            else:
                runs.append([entry])
        if len(runs) > 1:
            log.debug("plan_span: %d entries split into %d spans", len(ordered), len(runs))
        for run in runs:
            first, last = run[0], run[-1]
            estimate = (
                last.header_offset
                + _LOCAL_HEADER_LEN
                + len(last.name.encode("utf-8"))
                + _LOCAL_EXTRA_MARGIN
                + last.compress_size
                + _DATA_DESCRIPTOR_MAX
            )
            end = min(estimate, self._next_boundary(last.header_offset))
            if first.header_offset < end:
                self._spans.append((first.header_offset, end))

    def skip_gap(self, offset: int) -> None:
        """Tell the backing reader the bytes before ``offset`` hold an unwanted member.

        The next read then starts a fresh request at ``offset`` instead of reading through.
        """
        self._backing.skip_to(offset)

    @property
    def stats(self) -> dict:
        """Backing reader counters: bytes_fetched, bytes_discarded, requests."""
        return dict(self._backing.stats)

    def close(self) -> None:
        """Close the shared ZipFile and the backing reader (HTTP connection or local file)."""
        if not self.closed:
            self._zf.close()
            self._backing.close()
        super().close()

    # -------------------------------------------------------------------- internals

    def _total_size(self) -> int:
        return self._size

    def _read_at(self, pos: int, buf: memoryview) -> int:
        """Fill ``buf`` from ``pos``: tail bytes from memory, the rest from the backing file."""
        if self.closed:
            raise ValueError("I/O operation on closed ZipSource")
        if pos >= self._size or len(buf) == 0:
            return 0
        if pos >= self._tail_start:
            chunk = self._tail[pos - self._tail_start : pos - self._tail_start + len(buf)]
            buf[: len(chunk)] = chunk
            return len(chunk)
        # Never let a backing read run into the tail region; a short read here is fine,
        # the caller (BufferedReader or zipfile) simply reads again and gets tail bytes.
        want = min(len(buf), self._tail_start - pos)
        self._hint_for(pos)
        self._backing.seek(pos)
        return self._backing.readinto(buf[:want])

    def _hint_for(self, pos: int) -> None:
        """Announce the span a backing request starting at ``pos`` may cover.

        Inside a planned span that is the span. Outside (the caller did not plan, or a local
        header was bigger than estimated) it is the rest of the member containing ``pos``, so
        even unplanned reads never stream into the next member.
        """
        span = None
        for start, end in self._spans:
            if start <= pos < end:
                span = (start, end)
                break
        if span is None:
            span = (pos, self._next_boundary(pos))
        if span != self._hinted:
            self._backing.hint(*span)
            self._hinted = span

    def _next_boundary(self, pos: int) -> int:
        """Offset of the first member header after ``pos`` (or the tail start)."""
        i = bisect.bisect_right(self._offsets, pos)
        return self._offsets[i] if i < len(self._offsets) else self._tail_start

    def _open_zipfile(self) -> tuple[zipfile.ZipFile, set[int]]:
        """Build a ZipFile from the in-memory tail alone (no I/O); return it and its offsets.

        The view refuses reads outside the tail while the central directory is parsed, then
        is switched to normal reads so the same ZipFile can read member data.
        """
        view = _View(self, tail_only=True)
        try:
            zf = zipfile.ZipFile(io.BufferedReader(view, BUFFER_SIZE))
        except _OutsideTail as exc:
            raise zipfile.BadZipFile("tail blob does not hold this zip's central directory") from exc
        try:
            # zipfile locates the central directory by position and silently shifts every
            # offset when that disagrees with the offset recorded in the end record (its
            # support for data prepended to a zip). A tail from a different zip parses that
            # way too, so insist the two agree. Takeout never prepends anything.
            if zf.start_dir != _recorded_cd_offset(self._tail):
                raise zipfile.BadZipFile("tail blob does not belong to this zip")
            # Offsets at or past the tail cannot be member data; the caller drops them.
            offsets = {info.header_offset for info in zf.infolist()}
        except BaseException:
            zf.close()
            raise
        view._tail_only = False
        return zf, offsets


def _recorded_cd_offset(tail: bytes) -> int | None:
    """The central-directory offset stored in the tail's end record(s), or None.

    Finds the end record the way ``zipfile`` does (no-comment fast path, else the last
    signature in the comment window) and prefers the ZIP64 end record when a locator precedes
    it, again like ``zipfile``.
    """
    eocd = len(tail) - _EOCD_LEN
    if not (eocd >= 0 and tail[eocd : eocd + 4] == _EOCD_SIG and tail[-2:] == b"\0\0"):
        eocd = tail.rfind(_EOCD_SIG, max(0, len(tail) - _EOCD_LEN - _MAX_COMMENT))
        if eocd < 0 or eocd + _EOCD_LEN > len(tail):
            return None
    cd_offset = struct.unpack_from(_EOCD_FMT, tail, eocd)[6]
    loc = eocd - _ZIP64_LOC_LEN
    rec = loc - _ZIP64_REC_LEN
    if rec >= 0 and tail[loc : loc + 4] == _ZIP64_LOC_SIG and tail[rec : rec + 4] == _ZIP64_REC_SIG:
        cd_offset = struct.unpack_from(_ZIP64_REC_FMT, tail, rec)[9]
    return cd_offset


class _OutsideTail(Exception):
    """A tail-only view was asked for bytes the tail does not hold.

    Deliberately not an OSError: zipfile would swallow that into a vaguer message.
    """


class _View(_PositionedReader):
    """An independent position over a ZipSource. Closing it leaves the source open.

    ``io.BufferedReader`` closes its raw file when it is closed or garbage-collected; handing
    it a view keeps that from closing the shared source (and its HTTP connection).
    """

    def __init__(self, source: ZipSource, *, tail_only: bool = False) -> None:
        super().__init__()
        self._source = source
        self._tail_only = tail_only

    def _total_size(self) -> int:
        return self._source.size

    def _read_at(self, pos: int, buf: memoryview) -> int:
        if self._tail_only and pos < self._source._tail_start:
            raise _OutsideTail("read outside the tail")
        return self._source._read_at(pos, buf)


class _LocalFile(io.RawIOBase):
    """A local file with the HTTPRangeFile extras (hint, skip_to, counters) as no-ops."""

    broken = False  # a local disk has no retry state

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._f = open(path, "rb", buffering=0)  # noqa: SIM115 - closed in close()
        self.size = os.fstat(self._f.fileno()).st_size
        self.bytes_fetched = 0
        self.bytes_discarded = 0
        self.requests = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._f.tell()

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        return self._f.seek(offset, whence)

    def readinto(self, b) -> int:
        n = self._f.readinto(b) or 0
        self.bytes_fetched += n
        return n

    def hint(self, start: int, end: int) -> None:
        """No-op: local reads need no request planning."""

    def skip_to(self, offset: int) -> None:
        """Just a seek for local files."""
        self._f.seek(offset)

    @property
    def stats(self) -> dict:
        return {
            "bytes_fetched": self.bytes_fetched,
            "bytes_discarded": self.bytes_discarded,
            "requests": self.requests,
        }

    def close(self) -> None:
        if not self.closed:
            self._f.close()
        super().close()
