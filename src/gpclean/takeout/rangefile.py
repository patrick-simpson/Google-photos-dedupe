"""A seekable, read-only file object over HTTP range requests.

``HTTPRangeFile`` lets ``zipfile`` read a multi-GB Takeout zip served by
``rclone serve http`` on 127.0.0.1 without downloading it. Design points (docs/PLAN.md §3):

* **One streaming GET per span.** A request asks for ``Range: bytes=a-b`` and the body is read
  incrementally as the caller consumes it. ``hint(start, end)`` tells the reader how far the
  next request may reach, so a task's whole run of wanted members comes back in one response.
* **Cheap small hops, reopen for big ones.** Moving forward by less than ``read_through``
  bytes inside the open response just reads and discards the gap (much cheaper than a new
  request through rclone to Drive). Bigger jumps, backward seeks, and ``skip_to()`` (used when
  the gap holds a member nobody asked for, such as a video) close the stream and open a new one.
* **Retries.** Connection errors, timeouts, 5xx / 408 / 429 answers, wrong ``Content-Range``
  headers and short bodies are retried with exponential backoff, reopening at the current
  offset, so a partially read member simply continues. ``zipfile``'s CRC check catches
  anything that slips through.
* **Fail fast once the backend is dead.** When a read uses up all its retries the reader is
  marked ``broken``. Until a read succeeds again, each read makes a single attempt with no
  backoff, so a dead rclone or an exhausted Drive download quota costs one backoff schedule,
  not one per remaining item. Callers seeing ``RangeReadError.retryable`` should stop the
  whole task (it can be resumed later) rather than try the next item.
* **Accounting.** ``bytes_fetched`` (every body byte received, discarded ones included),
  ``bytes_discarded`` and ``requests`` feed the egress budget and the "zero video bytes" test.

Only the standard library is used. Log records carry offsets and exception class names only;
never the URL (it names the user's files).
"""

from __future__ import annotations

import http.client
import io
import logging
import re
import time
from collections.abc import Callable
from urllib.parse import quote, urlsplit

log = logging.getLogger(__name__)

_CONTENT_RANGE_RE = re.compile(r"^\s*bytes\s+(\d+)-(\d+)/(\d+|\*)\s*$", re.IGNORECASE)
_DISCARD_CHUNK = 64 * 1024
# Characters that may stay literal in a URL path. "%" is kept so already-encoded URLs (what
# rclone lists) pass through unchanged; spaces and other unsafe characters get encoded.
_PATH_SAFE = "/%!$&'()*+,;=:@~-._"
# How a request on a kept-alive connection the server has since closed fails. Windows often
# reports ConnectionAbortedError (WinError 10053) where Linux says reset or broken pipe.
# ConnectionRefusedError and timeouts are deliberately absent: those are real failures and
# go through the retry loop.
_STALE_CONNECTION_ERRORS = (
    http.client.RemoteDisconnected,
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
)
# HTTP statuses that are transient by definition, besides 5xx.
_RETRYABLE_4XX = (408, 429)


class RangeReadError(OSError):
    """A range read failed for good (retries exhausted, or an error retrying cannot fix).

    It is an ``OSError`` so callers that already guard file I/O handle it the same way.
    ``retryable`` says whether the underlying failure was of the transient kind.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class HTTPRangeFile(io.RawIOBase):
    """Read-only, seekable view of one remote file of known ``size``, via HTTP range GETs.

    ``backoff`` is the first retry delay in seconds (delays double: 1, 2, 4, 8, 16 by default)
    and ``sleep`` is the function used to wait; both exist so tests can run without waiting.
    """

    def __init__(
        self,
        url: str,
        size: int,
        *,
        timeout: float = 60.0,
        retries: int = 5,
        read_through: int = 256 * 1024,
        backoff: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__()
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("HTTPRangeFile needs an http(s) URL")
        if size < 0:
            raise ValueError("size must be >= 0")
        self._scheme = parts.scheme
        self._host = parts.hostname
        self._port = parts.port
        self._path = quote(parts.path or "/", safe=_PATH_SAFE)
        if parts.query:
            self._path += "?" + parts.query
        self._size = size
        self._timeout = timeout
        self._retries = retries
        self._read_through = read_through
        self._backoff = backoff
        self._sleep = sleep

        self._pos = 0  # logical position of the next read
        self._hint: tuple[int, int] | None = None
        self._conn: http.client.HTTPConnection | None = None
        self._resp: http.client.HTTPResponse | None = None
        self._stream_pos = 0  # file offset of the next body byte of self._resp
        self._stream_end = 0  # file offset just past the last body byte of self._resp

        # Set when a read exhausted its retries; cleared by the next successful read.
        self._failing = False

        self.bytes_fetched = 0
        self.bytes_discarded = 0
        self.requests = 0

    # ----------------------------------------------------------------- io.RawIOBase API

    @property
    def size(self) -> int:
        """Total size of the remote file in bytes."""
        return self._size

    @property
    def broken(self) -> bool:
        """True after a read gave up following all its retries, until a read succeeds.

        While broken, reads fail after one attempt without waiting (see the module docstring).
        """
        return self._failing

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        """Move the logical position. No I/O happens here; the next read decides what to do.

        That makes ``seek(tell())`` (which ``zipfile`` does before every read) free.
        """
        if whence == io.SEEK_SET:
            new = offset
        elif whence == io.SEEK_CUR:
            new = self._pos + offset
        elif whence == io.SEEK_END:
            new = self._size + offset
        else:
            raise ValueError(f"invalid whence ({whence})")
        if new < 0:
            raise ValueError("negative seek position")
        self._pos = new
        return new

    def readinto(self, b) -> int:
        """Read up to ``len(b)`` bytes at the current position; 0 means end of file.

        May return fewer bytes than asked (at the end of the current span, for instance);
        ``io.BufferedReader`` and ``read()`` loop as needed.
        """
        if self.closed:
            raise ValueError("I/O operation on closed file")
        view = memoryview(b).cast("B")
        if len(view) == 0 or self._pos >= self._size:
            return 0
        # A backend that just failed a full retry schedule gets one quick attempt per read.
        retries = 0 if self._failing else self._retries
        attempt = 0
        while True:
            try:
                self._position_stream()
                assert self._resp is not None
                want = min(len(view), self._stream_end - self._pos)
                n = self._resp.readinto(view[:want])
                if not n:
                    raise RangeReadError("short read", retryable=True)
                self._pos += n
                self._stream_pos += n
                self.bytes_fetched += n
                if self._stream_pos >= self._stream_end:
                    self._close_stream()  # body complete: the connection can be reused
                if self._failing:
                    log.info("range reader recovered at offset %d", self._pos)
                    self._failing = False
                return n
            except (OSError, http.client.HTTPException) as exc:
                # The connection may be half-way through a request or response; never reuse it.
                self._close_stream(drop_connection=True)
                if isinstance(exc, RangeReadError) and not exc.retryable:
                    raise
                if attempt >= retries:
                    if self._failing:
                        msg = (
                            f"range read failed at offset {self._pos}; backend still failing "
                            f"after an earlier read gave up ({type(exc).__name__})"
                        )
                    else:
                        msg = (
                            f"range read failed at offset {self._pos} after {attempt} retries "
                            f"({type(exc).__name__})"
                        )
                        log.error("range reader marked broken at offset %d (%s)",
                                  self._pos, type(exc).__name__)
                    self._failing = True
                    raise RangeReadError(msg, retryable=True) from exc
                delay = self._backoff * (2**attempt)
                attempt += 1
                log.warning(
                    "range read retry %d/%d at offset %d in %.1fs (%s)",
                    attempt, retries, self._pos, delay, type(exc).__name__,
                )
                self._sleep(delay)

    def close(self) -> None:
        """Close the response and the connection."""
        if not self.closed:
            self._close_stream(drop_connection=True)
        super().close()

    # ------------------------------------------------------------------- span control

    def hint(self, start: int, end: int) -> None:
        """Let the next GET that starts inside ``[start, end)`` cover up to ``end``.

        It does not touch an already open response. Without a hint a GET reaches to the end
        of the file, which is fine for sequential reads but wasteful for scattered ones.
        """
        start = max(0, start)
        end = min(end, self._size)
        self._hint = (start, end) if start < end else None

    def skip_to(self, offset: int) -> None:
        """Move to ``offset`` and make sure the gap is never read through.

        Use it when the bytes before ``offset`` belong to a member the caller did not ask for
        (a video, say): even a small gap is then skipped with a fresh request.
        """
        if offset < 0:
            raise ValueError("negative offset")
        self._pos = offset
        if self._resp is not None and self._stream_pos != offset:
            self._close_stream()

    @property
    def stats(self) -> dict:
        """Byte and request counters as a plain dict."""
        return {
            "bytes_fetched": self.bytes_fetched,
            "bytes_discarded": self.bytes_discarded,
            "requests": self.requests,
        }

    # -------------------------------------------------------------------- internals

    def _position_stream(self) -> None:
        """Make the open response's next body byte be the one at ``self._pos``."""
        pos = self._pos
        if self._resp is not None:
            gap = pos - self._stream_pos
            if gap == 0 and pos < self._stream_end:
                return
            if 0 < gap < self._read_through and pos < self._stream_end:
                self._discard(gap)
                return
            self._close_stream()
        self._open(pos)

    def _discard(self, n: int) -> None:
        """Read and drop ``n`` body bytes (a small forward gap inside the current span)."""
        assert self._resp is not None
        scratch = bytearray(min(n, _DISCARD_CHUNK))
        view = memoryview(scratch)
        while n > 0:
            got = self._resp.readinto(view[: min(n, len(scratch))])
            if not got:
                raise RangeReadError("short read while skipping", retryable=True)
            n -= got
            self._stream_pos += got
            self.bytes_fetched += got
            self.bytes_discarded += got

    def _new_conn(self) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        return cls(self._host, self._port, timeout=self._timeout)

    def _send(self, headers: dict) -> http.client.HTTPResponse:
        """Send one GET, reconnecting once if an idle keep-alive connection went stale."""
        reused = self._conn is not None
        if self._conn is None:
            self._conn = self._new_conn()
        try:
            self.requests += 1
            self._conn.request("GET", self._path, headers=headers)
            return self._conn.getresponse()
        except _STALE_CONNECTION_ERRORS:
            self._conn.close()
            self._conn = None
            if not reused:
                raise
        # The server closed the kept-alive connection while it sat idle. That is normal
        # HTTP behaviour, not a failure, so it does not use up a retry.
        self._conn = self._new_conn()
        self.requests += 1
        self._conn.request("GET", self._path, headers=headers)
        return self._conn.getresponse()

    def _open(self, start: int) -> None:
        """Open a ranged GET from ``start`` to the hint's end (or the end of the file)."""
        end = self._size
        if self._hint is not None and self._hint[0] <= start < self._hint[1]:
            end = self._hint[1]
        headers = {
            "Range": f"bytes={start}-{end - 1}",
            "Accept-Encoding": "identity",  # byte offsets must refer to the raw file
            "User-Agent": "gpclean",
        }
        resp = self._send(headers)
        try:
            first, last = self._check_response(resp, start)
        except Exception:
            self._resp = resp
            self._close_stream(drop_connection=True)
            raise
        self._resp = resp
        self._stream_pos = start
        # A server may legally return less than asked; never trust it to return more.
        self._stream_end = min(end, last + 1)

    def _check_response(self, resp: http.client.HTTPResponse, start: int) -> tuple[int, int]:
        """Validate status and Content-Range; return the (first, last) byte offsets served."""
        status = resp.status
        if status != 206:
            retryable = 500 <= status < 600 or status in _RETRYABLE_4XX
            if status == 200:
                msg = "server ignored the Range header (HTTP 200)"
            else:
                msg = f"unexpected HTTP status {status}"
            raise RangeReadError(msg, retryable=retryable)
        match = _CONTENT_RANGE_RE.match(resp.getheader("Content-Range") or "")
        if not match:
            raise RangeReadError("missing or malformed Content-Range", retryable=True)
        first, last = int(match.group(1)), int(match.group(2))
        total = match.group(3)
        if total != "*" and int(total) != self._size:
            # The file changed under us (or it is the wrong file); retrying will not help.
            raise RangeReadError("remote file size does not match", retryable=False)
        if first != start or last < first:
            raise RangeReadError("Content-Range does not match the request", retryable=True)
        return first, last

    def _close_stream(self, drop_connection: bool = False) -> None:
        """Forget the current response (and the connection too if ``drop_connection``).

        A fully read response leaves the connection reusable. Abandoning a response mid-body
        must close the connection too: that is the only way to stop the server sending.
        """
        resp = self._resp
        self._resp = None
        if resp is not None and not resp.isclosed():
            resp.close()
            drop_connection = True
        if drop_connection and self._conn is not None:
            self._conn.close()
            self._conn = None
