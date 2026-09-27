"""Tests for gpclean.takeout.rangefile and gpclean.takeout.zipsource.

Everything runs against a local ``ThreadingHTTPServer`` that serves synthetic bytes with Range
support, logs every request it answers and can inject failures. The rclone test uses a real
``rclone serve http`` when a binary is available and is skipped otherwise.
"""

from __future__ import annotations

import gc
import io
import os
import queue
import random
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
import zipfile
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from gpclean.takeout.rangefile import HTTPRangeFile, RangeReadError
from gpclean.takeout.zipsource import ZipSource, read_tail

KIB = 1024
PHOTOS = "Takeout/Google Photos/Photos from 2019/"


# --------------------------------------------------------------------------- test server


class RangeServer:
    """Serve ``data`` at ``/<name>`` with byte ranges; record requests; inject faults.

    ``faults`` is a FIFO consumed one entry per GET: an HTTP status such as ``"503"``,
    ``"404"`` or ``"429"`` (answer with that error), ``"drop"`` (send part of the body then cut
    the connection), ``"bad_range"`` (wrong Content-Range start), ``"stall"`` (wait longer than
    the client timeout), ``"ignore_range"`` (answer 200) or ``None`` (behave).

    With ``close_after_response`` the server silently closes the connection after every
    response without sending ``Connection: close``, like a keep-alive idle timeout.
    """

    def __init__(
        self,
        data: bytes,
        name: str = "archive.zip",
        stall_s: float = 1.0,
        close_after_response: bool = False,
    ) -> None:
        self.data = data
        self.name = name
        self.stall_s = stall_s
        self.close_after_response = close_after_response
        self.faults: list[str | None] = []
        self.log: list[list[int]] = []  # [start, end exclusive, body bytes sent]
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self) -> None:
                super().setup()
                # Headers and body go out as separate writes; without this, Nagle plus
                # delayed ACKs add ~40 ms to every request.
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            def log_message(self, *args) -> None:  # keep test output clean
                pass

            def do_GET(self) -> None:
                outer._handle(self)

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address) -> None:
                pass  # clients hang up mid-body on purpose; that is not an error here

        self.httpd = Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/{self.name}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def ranges(self) -> list[tuple[int, int]]:
        """Requested [start, end) of every answered GET."""
        with self.lock:
            return [(s, e) for s, e, _ in self.log]

    def _handle(self, h: BaseHTTPRequestHandler) -> None:
        if h.path != "/" + self.name:
            h.send_error(404)
            return
        with self.lock:
            fault = self.faults.pop(0) if self.faults else None
        size = len(self.data)
        m = re.fullmatch(r"bytes=(\d+)-(\d*)", h.headers.get("Range", ""))
        if fault is not None and fault.isdigit():
            h.send_error(int(fault))
            return
        if fault == "stall":
            time.sleep(self.stall_s)
        if m is None or fault == "ignore_range":
            start, end, status = 0, size, 200
        else:
            start = int(m.group(1))
            end = min(int(m.group(2)) + 1, size) if m.group(2) else size
            status = 206
        h.send_response(status)
        h.send_header("Content-Length", str(end - start))
        if status == 206:
            shown = start + 1 if fault == "bad_range" else start
            h.send_header("Content-Range", f"bytes {shown}-{end - 1}/{size}")
        h.end_headers()
        # Log before sending: the client may hang up and move on before the body is out.
        record = [start, end, 0]
        with self.lock:
            self.log.append(record)
        body_end = start + (end - start) // 2 if fault == "drop" else end
        sent = 0
        try:
            pos = start
            while pos < body_end:
                chunk = self.data[pos : min(pos + 64 * KIB, body_end)]
                h.wfile.write(chunk)
                pos += len(chunk)
                sent += len(chunk)
            h.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            h.close_connection = True
        finally:
            with self.lock:
                record[2] = sent
        if fault == "drop":
            h.close_connection = True
            try:
                h.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        elif self.close_after_response:
            h.close_connection = True  # no header told the client: its connection goes stale


@pytest.fixture
def blob() -> bytes:
    return random.Random(7).randbytes(3 * 1024 * KIB)


@pytest.fixture
def server(blob):
    srv = RangeServer(blob)
    yield srv
    srv.close()


def rf(server: RangeServer, **kw) -> HTTPRangeFile:
    kw.setdefault("backoff", 0.0)
    return HTTPRangeFile(server.url, len(server.data), **kw)


# ------------------------------------------------------------------ HTTPRangeFile basics


def test_sequential_read_is_one_request(server, blob):
    with rf(server) as f:
        assert f.readable() and f.seekable()
        assert f.readall() == blob
        assert f.requests == 1
        assert f.bytes_fetched == len(blob)
        assert f.read(10) == b""  # at EOF
    assert server.ranges() == [(0, len(blob))]


def test_seek_to_current_position_is_free(server, blob):
    with rf(server) as f:
        assert f.read(1000) == blob[:1000]
        f.seek(f.tell())
        f.seek(0, io.SEEK_CUR)
        assert f.read(1000) == blob[1000:2000]
        assert f.requests == 1
        assert f.bytes_discarded == 0


def test_small_forward_gap_is_read_through(server, blob):
    with rf(server) as f:
        f.read(100)
        f.seek(100 + 5000)
        assert f.read(100) == blob[5100:5200]
        assert f.requests == 1
        assert f.bytes_discarded == 5000
        assert f.bytes_fetched == 5200


def test_large_forward_gap_and_backward_seek_reopen(server, blob):
    with rf(server, read_through=256 * KIB) as f:
        f.read(100)
        f.seek(100 + 300 * KIB)
        assert f.read(100) == blob[100 + 300 * KIB : 200 + 300 * KIB]
        assert f.requests == 2
        f.seek(50)  # backward
        assert f.read(10) == blob[50:60]
        assert f.requests == 3
        assert f.bytes_discarded == 0
    starts = [s for s, _ in server.ranges()]
    assert starts == [0, 100 + 300 * KIB, 50]


def test_skip_to_never_reads_through(server, blob):
    with rf(server) as f:
        f.read(100)
        f.skip_to(200)  # tiny gap, but it holds something we must not fetch
        assert f.tell() == 200
        assert f.read(10) == blob[200:210]
        assert f.requests == 2
        assert f.bytes_discarded == 0
        f.skip_to(210)  # already there: nothing to skip, keep streaming
        assert f.read(10) == blob[210:220]
        assert f.requests == 2


def test_seek_whence_and_errors(server, blob):
    with rf(server) as f:
        assert f.seek(-10, io.SEEK_END) == len(blob) - 10
        assert f.read() == blob[-10:]
        assert f.seek(len(blob) + 5) == len(blob) + 5
        assert f.read(5) == b""  # past EOF reads nothing and sends nothing
        with pytest.raises(ValueError):
            f.seek(-1)
        with pytest.raises(ValueError):
            f.seek(0, 7)
    with pytest.raises(ValueError):
        f.read(1)  # closed


def test_hint_bounds_the_request(server, blob):
    with rf(server) as f:
        f.hint(1000, 2000)
        f.seek(1500)
        assert f.read(5000) == blob[1500:2000]  # stops at the hint's end
        assert f.read(10) == blob[2000:2010]  # outside the hint: new, open-ended request
    assert server.ranges() == [(1500, 2000), (2000, len(blob))]


def test_keep_alive_reuses_connection(server, blob):
    with rf(server) as f:
        for start in (0, 100 * KIB, 200 * KIB):
            f.hint(start, start + 1000)
            f.seek(start)
            assert f.read(1000) == blob[start : start + 1000]
        assert f.requests == 3


# ------------------------------------------------------------------------------ retries


@pytest.mark.parametrize("fault", ["drop", "503", "429", "408", "bad_range", "stall"])
def test_retry_recovers(server, blob, fault):
    server.faults = [fault]
    with rf(server, timeout=0.3) as f:
        f.hint(0, 400 * KIB)
        data = io.BufferedReader(f, 64 * KIB).read(400 * KIB)
        assert data == blob[: 400 * KIB]
        assert f.requests >= 2


def test_drop_mid_body_resumes_at_current_offset(server, blob):
    server.faults = ["drop"]
    with rf(server) as f:
        data = f.readall()
    assert data == blob
    (s1, e1, sent1), (s2, e2, _) = server.log
    assert (s1, e1) == (0, len(blob))
    assert s2 == sent1  # second request starts exactly where the dropped body stopped


def test_backoff_schedule_and_give_up(server):
    delays: list[float] = []
    server.faults = ["503"] * 10
    with rf(server, backoff=1.0, sleep=delays.append) as f:
        with pytest.raises(RangeReadError) as exc:
            f.read(10)
        assert exc.value.retryable
        assert f.requests == 6  # first try + 5 retries
    assert delays == [1, 2, 4, 8, 16]


def test_dead_backend_pays_the_backoff_schedule_once(blob):
    """After one read gives up, later reads fail at once instead of backing off again."""
    srv = RangeServer(blob)
    url = srv.url
    srv.close()  # nothing listens any more: every connect is refused
    delays: list[float] = []
    with HTTPRangeFile(url, len(blob), backoff=1.0, sleep=delays.append) as f:
        assert not f.broken
        for offset in (0, 100 * KIB, 2000 * KIB):
            f.seek(offset)
            with pytest.raises(RangeReadError) as exc:
                f.read(10)
            assert exc.value.retryable
            assert f.broken
        assert f.requests == 6 + 1 + 1  # full schedule once, then one attempt per read
    assert delays == [1, 2, 4, 8, 16]  # in total, not per read


def test_broken_state_clears_after_a_successful_read(server, blob):
    delays: list[float] = []
    server.faults = ["503"] * 6
    with rf(server, backoff=1.0, sleep=delays.append) as f:
        with pytest.raises(RangeReadError):
            f.read(10)
        assert f.broken
        assert f.read(10) == blob[:10]  # the backend is back: one attempt succeeds
        assert not f.broken
        server.faults = ["503"]
        f.skip_to(1000)  # force a new request
        assert f.read(10) == blob[1000:1010]  # a fresh failure gets its retries again
    assert delays == [1, 2, 4, 8, 16, 1]


def test_stale_keep_alive_reconnects_without_retrying(blob):
    """A server that closes idle connections silently costs a reconnect, never a retry."""
    srv = RangeServer(blob, close_after_response=True)
    delays: list[float] = []
    try:
        with HTTPRangeFile(srv.url, len(blob), sleep=delays.append) as f:
            offsets = (0, 100 * KIB, 200 * KIB, 300 * KIB)
            for start in offsets:
                f.hint(start, start + 1000)
                f.seek(start)
                assert f.read(1000) == blob[start : start + 1000]
            assert not f.broken
            # Every read after the first found its connection stale and resent once.
            assert f.requests == 2 * len(offsets) - 1
        assert delays == []
        assert [s for s, _ in srv.ranges()] == list(offsets)
    finally:
        srv.close()


@pytest.mark.parametrize("fault", ["404", "ignore_range"])
def test_non_retryable_errors_fail_fast(server, fault):
    delays: list[float] = []
    server.faults = [fault]
    with rf(server, sleep=delays.append) as f:
        with pytest.raises(RangeReadError) as exc:
            f.read(10)
        assert not exc.value.retryable
    assert delays == []


def test_size_mismatch_is_fatal(server, blob):
    with HTTPRangeFile(server.url, len(blob) + 1, backoff=0.0) as f:
        with pytest.raises(RangeReadError):
            f.read(10)


def test_bad_url_rejected():
    with pytest.raises(ValueError):
        HTTPRangeFile("ftp://example.invalid/x.zip", 10)


# --------------------------------------------------------------------------- zip fixtures


@dataclass(frozen=True)
class E:
    """Duck-typed stand-in for gpclean.takeout.members.Entry."""

    name: str
    header_offset: int
    compress_size: int
    file_size: int


class NonSeekable(io.RawIOBase):
    """Write-only stream without seek/tell, so zipfile writes data descriptors (flag bit 3)."""

    def __init__(self, f) -> None:
        super().__init__()
        self._f = f

    def writable(self) -> bool:
        return True

    def write(self, b) -> int:
        return self._f.write(b)


def build_zip(path: Path, *, comment: bytes = b"", streamed: bool = False) -> dict[str, bytes]:
    """Interleaved photo / sidecar / video members, stored and deflated, one forced ZIP64.

    ``streamed`` writes the zip the way streaming zip writers do: through a non-seekable
    stream, so every member has a data descriptor. It also adds a photo whose local extra
    field (3,000 bytes) is far bigger than the span estimate allows, followed directly by a
    small and a large video.
    """
    with open(path, "wb") as raw:
        with zipfile.ZipFile(NonSeekable(raw) if streamed else raw, "w") as zf:
            members = _fill_zip(zf, comment=comment, streamed=streamed)
    return members


def _fill_zip(zf: zipfile.ZipFile, *, comment: bytes, streamed: bool) -> dict[str, bytes]:
    """Write the members; see ``build_zip``."""
    rng = random.Random(42)
    members: dict[str, bytes] = {}
    for i in range(12):
        photo = PHOTOS + f"IMG_{i:04d}.jpg"
        members[photo] = rng.randbytes(rng.randint(2 * KIB, 60 * KIB))
        zf.writestr(photo, members[photo], compress_type=zipfile.ZIP_STORED)
        side = photo + ".supplemental-metadata.json"
        members[side] = (
            '{"title": "IMG_%04d.jpg", "photoTakenTime": {"timestamp": "%d"}}' % (i, 1.5e9 + i)
        ).encode()
        zf.writestr(side, members[side], compress_type=zipfile.ZIP_DEFLATED)
        if i % 3 == 1:
            # Videos: a big one (bigger than read_through) and a small one (smaller than
            # read_through, so only correct planning keeps it from being read through).
            vid = PHOTOS + f"VID_{i:04d}.mp4"
            size = 300 * KIB if i % 2 else 3 * KIB
            members[vid] = rng.randbytes(size)
            zf.writestr(vid, members[vid], compress_type=zipfile.ZIP_STORED)
    if streamed:
        extra = PHOTOS + "IMG_EXTRA.jpg"
        members[extra] = rng.randbytes(8 * KIB)
        info = zipfile.ZipInfo(extra, date_time=(2019, 5, 1, 12, 0, 0))
        # One well-formed private extra block (id 0xCAFE), 3,000 bytes in all.
        info.extra = struct.pack("<HH", 0xCAFE, 2996) + bytes(2996)
        zf.writestr(info, members[extra], compress_type=zipfile.ZIP_STORED)
        for vid, size in (("VID_SMALL.mp4", 2 * KIB), ("VID_LARGE.mp4", 400 * KIB)):
            members[PHOTOS + vid] = rng.randbytes(size)
            zf.writestr(PHOTOS + vid, members[PHOTOS + vid], compress_type=zipfile.ZIP_STORED)
    z64 = PHOTOS + "BIG_ZIP64.jpg"
    members[z64] = rng.randbytes(20 * KIB)
    info = zipfile.ZipInfo(z64, date_time=(2019, 5, 1, 12, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    with zf.open(info, "w", force_zip64=True) as w:
        w.write(members[z64])
    last = PHOTOS + "IMG_LAST.jpg"
    members[last] = rng.randbytes(10 * KIB)
    zf.writestr(last, members[last], compress_type=zipfile.ZIP_DEFLATED)
    zf.comment = comment
    return members


def entries_of(path: Path) -> list[E]:
    with zipfile.ZipFile(path) as zf:
        es = [E(i.filename, i.header_offset, i.compress_size, i.file_size) for i in zf.infolist()]
    return sorted(es, key=lambda e: e.header_offset)


def is_video(e: E) -> bool:
    return e.name.endswith(".mp4")


def wanted_runs(entries: list[E]) -> list[list[E]]:
    """Consecutive runs of wanted (non-video) entries, split wherever a video sits."""
    runs: list[list[E]] = []
    prev_wanted = False
    for e in entries:
        if is_video(e):
            prev_wanted = False
            continue
        if not prev_wanted:
            runs.append([])
        runs[-1].append(e)
        prev_wanted = True
    return runs


def video_ranges(path: Path, entries: list[E]) -> list[tuple[int, int]]:
    """[header_offset, next header offset) of each video member."""
    with zipfile.ZipFile(path) as zf:
        cd_start = zf.start_dir
    bounds = [e.header_offset for e in entries] + [cd_start]
    return [(e.header_offset, bounds[i + 1]) for i, e in enumerate(entries) if is_video(e)]


def overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def scan_like(src: ZipSource, entries: list[E], *, plan: str = "runs", skip: bool = True):
    """Read every wanted member the way the scanner does; return {name: bytes}."""
    zf = src.zipfile()
    out = {}
    runs = wanted_runs(entries)
    if plan == "all":  # caller passes non-consecutive entries in one go
        src.plan_span([e for run in runs for e in run])
    for i, run in enumerate(runs):
        if skip and i > 0:
            src.skip_gap(run[0].header_offset)
        if plan == "runs":
            src.plan_span(run)
        for e in run:
            out[e.name] = zf.read(e.name)  # zipfile checks the CRC
    zf.close()
    return out


def _served_takeout(tmp_path: Path, *, streamed: bool = False):
    path = tmp_path / "takeout-20260101T000000Z-001.zip"
    members = build_zip(path, streamed=streamed)  # like Takeout: no archive comment
    srv = RangeServer(path.read_bytes(), name=path.name)
    return path, members, srv


@pytest.fixture
def takeout(tmp_path):
    path, members, srv = _served_takeout(tmp_path)
    yield path, members, srv
    srv.close()


@pytest.fixture(params=[False, True], ids=["plain", "streamed"])
def any_takeout(tmp_path, request):
    """The plain zip, and the streamed one (data descriptors, oversized local extra)."""
    path, members, srv = _served_takeout(tmp_path, streamed=request.param)
    yield path, members, srv, request.param
    srv.close()


def assert_no_video_requests(srv: RangeServer, path: Path, entries: list[E]) -> None:
    """No request the server answered asked for a single byte of a video member."""
    for req in srv.ranges():
        for vr in video_ranges(path, entries):
            assert not overlaps(req, vr), (req, vr)


# ------------------------------------------------------------------------- ZipSource tests


@pytest.mark.parametrize(
    "plan, skip", [("runs", True), ("runs", False), ("all", False), ("none", False)]
)
def test_zip_over_http_reads_members_and_never_touches_videos(any_takeout, plan, skip):
    path, members, srv, streamed = any_takeout
    entries = entries_of(path)
    assert any(is_video(e) for e in entries)
    if streamed:
        with zipfile.ZipFile(path) as zf:
            assert all(i.flag_bits & 0x08 for i in zf.infolist())  # data descriptors
    src = ZipSource.open_http(srv.url, path.stat().st_size, backoff=0.0)
    tail_requests = src.stats["requests"]
    assert tail_requests == 2  # end records, then the central directory
    got = scan_like(src, entries, plan=plan, skip=skip)

    wanted = {e.name for e in entries if not is_video(e)}
    assert set(got) == wanted
    assert all(got[n] == members[n] for n in wanted)

    assert_no_video_requests(srv, path, entries)
    stats = src.stats
    assert stats["bytes_discarded"] == 0
    if plan == "runs":
        # The oversized local extra ends its run past the estimated span end, which costs
        # one extra (member-bounded) request.
        extra = 1 if streamed else 0
        assert stats["requests"] == tail_requests + len(wanted_runs(entries)) + extra
    # Client side too: nothing received can have been a video byte.
    video_bytes = sum(e - s for s, e in video_ranges(path, entries))
    assert stats["bytes_fetched"] <= path.stat().st_size - video_bytes
    src.close()


def test_tail_reuse_needs_no_tail_fetch(takeout):
    path, members, srv = takeout
    size = path.stat().st_size
    with ZipSource.open_http(srv.url, size, backoff=0.0) as first:
        tail = first.tail()
    n_before = len(srv.log)
    src = ZipSource.open_http(srv.url, size, tail=tail, backoff=0.0)
    zf = src.zipfile()
    names = zf.namelist()
    assert len(srv.log) == n_before  # opening the ZipFile read only the in-memory tail
    assert src.stats["requests"] == 0
    assert zf.read(names[0]) == members[names[0]]
    assert src.stats["requests"] == 1
    src.close()


def test_small_gap_inside_a_span_is_read_through(takeout):
    """A planned span covering a JSON the caller then skips streams through it (one request)."""
    path, members, srv = takeout
    entries = entries_of(path)
    photo0, side0, photo1 = entries[0], entries[1], entries[2]
    assert side0.name.endswith(".json") and photo1.name.endswith(".jpg")
    src = ZipSource.open_http(srv.url, path.stat().st_size, backoff=0.0)
    zf = src.zipfile()
    before = src.stats["requests"]
    src.plan_span([photo0, side0, photo1])
    assert zf.read(photo0.name) == members[photo0.name]
    assert zf.read(photo1.name) == members[photo1.name]
    stats = src.stats
    assert stats["requests"] == before + 1
    # The JSON came along inside the one span (here it simply sat in the 1 MiB buffer).
    (span_start, span_end), = srv.ranges()[-1:]
    assert span_start == photo0.header_offset
    assert span_end == entries[3].header_offset  # clamped at the next, unplanned member
    src.close()


def test_retries_recover_inside_zip_reads(any_takeout):
    path, members, srv, _ = any_takeout
    entries = entries_of(path)
    src = ZipSource.open_http(srv.url, path.stat().st_size, backoff=0.0)
    srv.faults = ["drop", "503", "bad_range", None, "drop"]
    got = scan_like(src, entries)
    assert set(got) == {e.name for e in entries if not is_video(e)}
    assert all(got[n] == members[n] for n in got)
    # Reopening after a fault stays inside the planned span: still no video bytes.
    assert_no_video_requests(srv, path, entries)
    assert not src.broken
    src.close()


def test_open_local_parity(takeout):
    path, members, srv = takeout
    entries = entries_of(path)
    with ZipSource.open_http(srv.url, path.stat().st_size, backoff=0.0) as remote, \
            ZipSource.open_local(path) as local:
        assert local.tail() == remote.tail()
        assert local.size == remote.size == path.stat().st_size
        assert scan_like(local, entries) == scan_like(remote, entries)
        assert local.stats["requests"] == 0
        assert local.stats["bytes_fetched"] > 0
        # Raw reads straddling the tail boundary join backing bytes and tail bytes correctly.
        whole = path.read_bytes()
        start = local.size - len(local.tail()) - 10
        for src in (local, remote):
            src.seek(start)
            assert io.BufferedReader(src).read(30) == whole[start : start + 30]


def test_zipfile_views_do_not_close_source(takeout):
    path, members, _ = takeout
    src = ZipSource.open_local(path)
    zf1 = src.zipfile(fresh=True)
    name = zf1.namelist()[0]
    del zf1
    gc.collect()  # BufferedReader.__del__ closes its raw file: must only be the view
    assert not src.closed
    assert src.zipfile().read(name) == members[name]
    src.close()
    assert src.closed


def test_zipfile_is_shared_per_source(takeout):
    path, members, srv = takeout
    src = ZipSource.open_http(srv.url, path.stat().st_size, backoff=0.0)
    zf = src.zipfile()
    assert src.zipfile() is zf  # no re-parse of the central directory per task
    assert src.zipfile(fresh=True) is not zf
    name = zf.namelist()[0]
    zf.close()  # a caller closing the shared instance gets a working new one next time
    again = src.zipfile()
    assert again is not zf
    assert again.read(name) == members[name]
    src.close()


def test_read_tail_finds_central_directory(takeout):
    path, _, _ = takeout
    data = path.read_bytes()
    with zipfile.ZipFile(path) as zf:
        assert read_tail(io.BytesIO(data), len(data)) == data[zf.start_dir :]


def test_archive_with_comment(tmp_path):
    """With a comment, the tail also spans zipfile's 64 KiB end-record search window, so
    zipfile still opens the archive from memory alone."""
    path = tmp_path / "commented.zip"
    members = build_zip(path, comment=b"synthetic comment with PK signature-like bytes PK\x05")
    data = path.read_bytes()
    with zipfile.ZipFile(path) as zf:
        cd_start = zf.start_dir
    tail = read_tail(io.BytesIO(data), len(data))
    assert tail == data[min(cd_start, len(data) - 22 - (1 << 16)) :]
    with ZipSource.open_local(path) as src:
        assert src.tail() == tail
        got = scan_like(src, entries_of(path))
    assert all(got[n] == members[n] for n in got)


def test_zip64_archive(tmp_path, monkeypatch):
    """Tiny ZIP64 limits make zipfile write ZIP64 extras, end record and locator."""
    path = tmp_path / "z64.zip"
    monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 1000)
    monkeypatch.setattr(zipfile, "ZIP_FILECOUNT_LIMIT", 2)
    members = build_zip(path)
    monkeypatch.undo()
    data = path.read_bytes()
    loc = data.rfind(b"PK\x06\x07")
    assert loc > 0 and data.rfind(b"PK\x06\x06") == loc - 56
    with zipfile.ZipFile(path) as zf:
        cd_start = zf.start_dir
    tail = read_tail(io.BytesIO(data), len(data))
    assert tail == data[cd_start:]
    srv = RangeServer(data, name=path.name)
    try:
        src = ZipSource.open_http(srv.url, len(data), backoff=0.0)
        got = scan_like(src, entries_of(path))
        assert all(got[n] == members[n] for n in got)
        src.close()
    finally:
        srv.close()


def test_read_tail_rejects_non_zip():
    junk = random.Random(1).randbytes(100 * KIB)
    with pytest.raises(zipfile.BadZipFile):
        read_tail(io.BytesIO(junk), len(junk))
    with pytest.raises(zipfile.BadZipFile):
        read_tail(io.BytesIO(b"PK"), 2)


def test_wrong_tail_is_rejected(takeout, tmp_path):
    path, _, _ = takeout
    with pytest.raises((zipfile.BadZipFile, ValueError)):
        ZipSource(io.BytesIO(path.read_bytes()), path.stat().st_size, b"not a tail at all" * 3)


def test_tail_of_another_zip_is_rejected(takeout, tmp_path):
    """zipfile would accept a foreign central directory (shifting every offset); we don't."""
    path, _, _ = takeout
    other = tmp_path / "other.zip"
    with zipfile.ZipFile(other, "w") as zf:
        for i in range(30):
            zf.writestr(f"other/file_{i:02d}.txt", f"synthetic member {i}\n" * (i + 1))
    other_data = other.read_bytes()
    foreign_tail = read_tail(io.BytesIO(other_data), len(other_data))
    data = path.read_bytes()
    with pytest.raises(zipfile.BadZipFile, match="does not belong"):
        ZipSource(io.BytesIO(data), len(data), foreign_tail)


def test_empty_zip(tmp_path):
    path = tmp_path / "empty.zip"
    zipfile.ZipFile(path, "w").close()
    with ZipSource.open_local(path) as src:
        assert src.zipfile().namelist() == []
        src.plan_span([])


# ------------------------------------------------------------------------ real rclone


def _rclone_binary() -> str | None:
    candidate = os.environ.get("GPCLEAN_RCLONE") or shutil.which("rclone")
    return candidate if candidate and Path(candidate).exists() else None


@pytest.mark.rclone
def test_real_rclone_serve_http(tmp_path):
    binary = _rclone_binary()
    if binary is None:
        pytest.skip("no rclone binary (set GPCLEAN_RCLONE or put rclone on PATH)")
    root = tmp_path / "served"
    root.mkdir()
    path = root / "takeout-20260101T000000Z-001.zip"
    members = build_zip(path)
    entries = entries_of(path)
    conf = tmp_path / "rclone.conf"
    conf.write_text("", encoding="utf-8")
    proc = subprocess.Popen(
        [binary, "serve", "http", str(root), "--read-only", "--addr", "127.0.0.1:0",
         "--config", str(conf), "--vfs-cache-mode", "off"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: [lines.put(ln) for ln in proc.stderr], daemon=True).start()
    try:
        base = None
        deadline = time.monotonic() + 20
        while base is None and time.monotonic() < deadline:
            try:
                line = lines.get(timeout=0.5)
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue
            m = re.search(r"(http://127\.0\.0\.1:\d+/)", line)
            if m:
                base = m.group(1)
        assert base, "rclone did not report its listening address"
        src = ZipSource.open_http(base + path.name, path.stat().st_size, backoff=0.1)
        got = scan_like(src, entries)
        assert got == {e.name: members[e.name] for e in entries if not is_video(e)}
        stats = src.stats
        assert stats["requests"] == 2 + len(wanted_runs(entries))  # tail, then one per run
        assert stats["bytes_discarded"] == 0
        video_bytes = sum(e - s for s, e in video_ranges(path, entries))
        # Client side: everything received is at most the file minus every video byte.
        assert stats["bytes_fetched"] <= path.stat().st_size - video_bytes
        src.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
