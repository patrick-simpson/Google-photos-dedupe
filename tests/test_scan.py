"""Tests for gpclean.scan / gpclean.scanworker: shard planning, shard scanning, resume.

Everything runs on the synthetic ``--small`` fixture export. The HTTP tests serve a fixture
zip from a local ``ThreadingHTTPServer`` with Range support that logs every requested range,
which is how "video and skipped member bytes are never read" is proven for the CI read path.
"""

from __future__ import annotations

import dataclasses
import io
import multiprocessing
import os
import random
import re
import shutil
import sqlite3
import struct
import sys
import threading
import time
import types
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from gpclean import scan, scanworker
from gpclean.config import ScanConfig
from gpclean.fixtures.generate import generate
from gpclean.schema import open_readonly
from gpclean.takeout.members import Entry, classify, list_entries
from gpclean.takeout.zipsource import ZipSource
from gpclean.version import EXTRACT_VERSION

ZIP1 = "takeout-20260101T000000Z-001.zip"  # has videos and skipped members
PHOTOS = "Takeout/Google Photos/Photos from 2020/"
CFG = ScanConfig(clip_model="none", photos_per_shard=1000)
# shard_info values that legitimately differ between two scans of the same shard.
VOLATILE_INFO = {"started", "finished", "bytes_read", "bytes_discarded", "pack_sha256",
                 "pack_size"}


# ------------------------------------------------------------------------------ helpers


@pytest.fixture(scope="module")
def fx(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("fx")
    generate(out, seed=0, small=True)
    return out


def entries_of(path: Path) -> list[Entry]:
    with zipfile.ZipFile(path) as zf:
        return list_entries(zf)


def one_shard(path: Path, cfg: ScanConfig = CFG) -> tuple[list[Entry], scan.ShardSpec]:
    entries = entries_of(path)
    specs = scan.plan_shards(entries, cfg, zipkey="k" * 12, zip_name=path.name)
    assert len(specs) == 1
    return entries, specs[0]


def run_shard(opener, path: Path, out: Path, *, workers: int = 1, embedder: str = "stub",
              cfg: ScanConfig = CFG, deadline: float | None = None) -> tuple[dict, Path, Path]:
    entries, spec = one_shard(path, cfg)
    meta, pack = out / "s.meta.sqlite", out / "s.pack.sqlite"
    info = scan.scan_shard(opener, spec, entries, cfg, meta_path=meta, pack_path=pack,
                           workers=workers, embedder_name=embedder, deadline=deadline)
    return info, meta, pack


def dump(meta: Path) -> dict:
    """Every table of a meta DB as sorted row lists (shard_info as a dict)."""
    conn = open_readonly(meta)
    try:
        out = {"shard_info": {r["key"]: r["value"] for r in conn.execute("SELECT * FROM shard_info")}}
        for table in ("items_raw", "sidecars_raw", "videos_raw", "skipped_raw"):
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY member_idx").fetchall()
            out[table] = [dict(r) for r in rows]
        return out
    finally:
        conn.close()


def pack_rows(pack: Path) -> dict[int, tuple[bytes, bytes]]:
    conn = open_readonly(pack)
    try:
        return {r["member_idx"]: (r["g"], r["p"]) for r in conn.execute("SELECT * FROM t")}
    finally:
        conn.close()


def stable(d: dict) -> dict:
    d = dict(d)
    d["shard_info"] = {k: v for k, v in d["shard_info"].items() if k not in VOLATILE_INFO}
    return d


def member_ranges(path: Path) -> dict[str, tuple[int, int]]:
    """Byte range of every member: its local header up to the next header (or the CD)."""
    entries = entries_of(path)
    with zipfile.ZipFile(path) as zf:
        cd_start = zf.start_dir
    bounds = [e.header_offset for e in entries] + [cd_start]
    return {e.name: (bounds[i], bounds[i + 1]) for i, e in enumerate(entries)}


def unwanted_names(path: Path, cfg: ScanConfig = CFG) -> set[str]:
    return {e.name for e in entries_of(path)
            if classify(e.name, include_albums=cfg.include_albums,
                        file_size=e.file_size).kind in ("video", "skip")}


class RangeServer:
    """Serve one file with HTTP byte ranges; log requested ranges; optional forced status."""

    def __init__(self, data: bytes, name: str = "archive.zip") -> None:
        self.data, self.name = data, name
        self.status: int | None = None  # answer every GET with this error when set
        self.log: list[tuple[int, int]] = []
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:
                pass

            def do_GET(self) -> None:
                outer._handle(self)

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address) -> None:
                pass  # clients hang up mid-body on purpose

        self.httpd = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/{self.name}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def _handle(self, h: BaseHTTPRequestHandler) -> None:
        if self.status is not None:
            h.send_error(self.status)
            return
        size = len(self.data)
        m = re.fullmatch(r"bytes=(\d+)-(\d*)", h.headers.get("Range", ""))
        start = int(m.group(1)) if m else 0
        end = min(int(m.group(2)) + 1, size) if m and m.group(2) else size
        with self.lock:
            self.log.append((start, end))
        h.send_response(206 if m else 200)
        h.send_header("Content-Length", str(end - start))
        if m:
            h.send_header("Content-Range", f"bytes {start}-{end - 1}/{size}")
        h.end_headers()
        try:
            h.wfile.write(self.data[start:end])
        except (BrokenPipeError, ConnectionResetError):
            h.close_connection = True


@pytest.fixture
def served(fx):
    path = fx / ZIP1
    srv = RangeServer(path.read_bytes(), name=path.name)
    yield path, srv
    srv.close()


def http_opener(srv: RangeServer, path: Path, **kw) -> scan.HttpOpener:
    size = path.stat().st_size
    with ZipSource.open_http(srv.url, size, backoff=0.0) as src:
        tail = src.tail()
    return scan.HttpOpener(srv.url, size, tail, **{"backoff": 0.0, **kw})


@pytest.fixture(scope="module")
def local_w1(fx, tmp_path_factory):
    """Reference scan: fixture zip 1, in-process, stub embedder."""
    out = tmp_path_factory.mktemp("w1")
    info, meta, pack = run_shard(scan.LocalOpener(fx / ZIP1), fx / ZIP1, out, workers=1)
    return info, dump(meta), pack_rows(pack)


# ------------------------------------------------------------------------------ planning


def test_zipkeys(tmp_path):
    k = scan.zipkey_for("drive-id-1", 123, "2026-01-01T00:00:00Z")
    assert re.fullmatch(r"[0-9a-f]{12}", k)
    assert k == scan.zipkey_for("drive-id-1", 123, "2026-01-01T00:00:00Z")
    assert k != scan.zipkey_for("drive-id-1", 124, "2026-01-01T00:00:00Z")
    p = tmp_path / "a.zip"
    p.write_bytes(b"x")
    k1 = scan.zipkey_local(p)
    assert re.fullmatch(r"[0-9a-f]{12}", k1) and k1 == scan.zipkey_local(p)
    p.write_bytes(b"xy")
    assert scan.zipkey_local(p) != k1


def _synthetic(kinds: str) -> list[Entry]:
    """Entries from a kind string: i=image, j=sidecar json, v=video, s=skipped (raw)."""
    ext = {"i": "jpg", "j": "jpg.json", "v": "mp4", "s": "dng"}
    out, off = [], 0
    for i, k in enumerate(kinds):
        out.append(Entry(i, f"{PHOTOS}f{i:06d}.{ext[k]}", off, 100, 100, 0, 0, False))
        off += 200
    return out


def _check_coverage(specs, n_entries):
    assert specs[0].start == 0 and specs[-1].end == n_entries
    for a, b in zip(specs, specs[1:]):
        assert a.end == b.start and a.start < a.end
    assert [s.shard for s in specs] == list(range(len(specs)))


def test_plan_shards_sizes_and_coverage():
    cfg = ScanConfig(clip_model="none", photos_per_shard=10)
    rng = random.Random(1)
    kinds = "".join(rng.choice("iiijjvs") for _ in range(400))
    entries = _synthetic(kinds)
    specs = scan.plan_shards(entries, cfg, zipkey="z" * 12, zip_name="takeout-X-001.zip")
    _check_coverage(specs, len(entries))
    n_img = kinds.count("i")
    assert sum(s.n_images for s in specs) == n_img
    assert len(specs) == n_img // 10
    assert all(s.n_images == 10 for s in specs[:-1])
    assert 10 <= specs[-1].n_images < 20  # the last shard absorbs the remainder
    assert {s.export_id for s in specs} == {"takeout-X"}
    # Deterministic, and independent of the order the entries are handed over in.
    shuffled = entries[:]
    rng.shuffle(shuffled)
    assert scan.plan_shards(shuffled, cfg, zipkey="z" * 12, zip_name="takeout-X-001.zip") == specs


def test_plan_shards_exact_split():
    cfg = ScanConfig(clip_model="none", photos_per_shard=1000)
    specs = scan.plan_shards(_synthetic("i" * 2500), cfg, zipkey="z" * 12, zip_name="t.zip")
    assert [s.n_images for s in specs] == [1000, 1500]


@pytest.mark.parametrize("kinds", ["jjvvs", "", "iii"])
def test_plan_shards_small_or_photo_less_zip_gives_one_shard(kinds):
    entries = _synthetic(kinds)
    specs = scan.plan_shards(entries, CFG, zipkey="z" * 12, zip_name="t.zip")
    assert len(specs) == 1
    assert (specs[0].start, specs[0].end, specs[0].n_images) == (0, len(entries), kinds.count("i"))


def test_plan_shards_on_fixture(fx):
    cfg = ScanConfig(clip_model="none", photos_per_shard=10)
    for path in sorted(fx.glob("*.zip")):
        entries = entries_of(path)
        specs = scan.plan_shards(entries, cfg, zipkey="z" * 12, zip_name=path.name)
        _check_coverage(specs, len(entries))
        images = sum(classify(e.name, include_albums=False, file_size=e.file_size).kind == "image"
                     for e in entries)
        assert sum(s.n_images for s in specs) == images


def test_tasks_are_short_adjacent_runs():
    cfg = CFG
    kinds = "ij" * 30 + "v" + "i" * 5 + "s" + "jj" + "vv" + "i"
    members = [(e, scan._classify(e, cfg)) for e in _synthetic(kinds)]
    tasks = scan._build_tasks(members)
    assert [n for n, _ in tasks] == list(range(len(tasks)))
    assert [len(run) for _, run in tasks] == [48, 12, 5, 2, 1]
    for _, run in tasks:
        idx = [e.member_idx for e, _ in run]
        assert idx == list(range(idx[0], idx[0] + len(idx)))  # adjacent in the file
        assert all(mc.kind in ("image", "sidecar") for _, mc in run)


# ------------------------------------------------------------------------------ scanning


def test_scan_shard_contents(fx, local_w1):
    info, d, pack = local_w1
    entries = entries_of(fx / ZIP1)
    kinds = {e.member_idx: classify(e.name, include_albums=False, file_size=e.file_size)
             for e in entries}
    by_kind = lambda k: sorted(i for i, mc in kinds.items() if mc.kind == k)  # noqa: E731
    assert [r["member_idx"] for r in d["items_raw"]] == by_kind("image")
    assert [r["member_idx"] for r in d["sidecars_raw"]] == by_kind("sidecar")
    assert [r["member_idx"] for r in d["videos_raw"]] == by_kind("video")
    assert [r["member_idx"] for r in d["skipped_raw"]] == by_kind("skip")
    assert all(r["reason"] == kinds[r["member_idx"]].reason for r in d["skipped_raw"])

    si = d["shard_info"]
    assert si["cfg"] == CFG.cfg_hash() and si["extract_version"] == str(EXTRACT_VERSION)
    assert int(si["n_items"]) == len(d["items_raw"]) == info["n_items"]
    assert int(si["n_videos"]) == len(d["videos_raw"]) and int(si["n_err"]) == 0
    assert si["clip_model"] == "stub" and si["pack_name"] == "s.pack.sqlite"
    assert int(si["bytes_read"]) > 0
    assert set(si) >= {"zipkey", "zip_name", "export_id", "shard", "code_version", "start",
                       "end", "n_sidecars", "n_skipped", "bytes_discarded", "pack_sha256",
                       "pack_size", "started", "finished"}

    by_idx = {e.member_idx: e for e in entries}
    for row in d["items_raw"]:
        e = by_idx[row["member_idx"]]
        assert row["err"] is None and row["member"] == e.name
        assert row["file_size"] == e.file_size and row["crc32"] == e.crc
        assert len(row["sha256"]) == 32 and len(row["sig"]) == 1152
        emb = np.frombuffer(row["emb"], dtype="<f2")
        assert len(row["emb"]) == 1024 and abs(float(np.linalg.norm(emb.astype(np.float32))) - 1) < 0.01
    assert sum(r["url"] is not None for r in d["sidecars_raw"]) > 0

    # Every photo has decodable thumbnails of the configured sizes.
    assert set(pack) == {r["member_idx"] for r in d["items_raw"]}
    for g, p in pack.values():
        gi, pi = Image.open(io.BytesIO(g)), Image.open(io.BytesIO(p))
        assert gi.format == pi.format == "WEBP"
        assert max(gi.size) <= 160 and max(pi.size) <= 640
        gi.load()
        pi.load()


def test_worker_rows_only_use_schema_columns(fx):
    entries, spec = one_shard(fx / ZIP1)
    members = [(e, scan._classify(e, CFG)) for e in entries]
    task = scan._build_tasks(members)[0]
    worker = scanworker.Worker(CFG, "stub")
    try:
        res = worker.run(scan.LocalOpener(fx / ZIP1), task)
    finally:
        worker.close()
    conn = sqlite3.connect(":memory:")
    from gpclean.schema import SHARD_DDL

    conn.executescript(SHARD_DDL)
    item_cols = set(scan._columns(conn, "items_raw"))
    side_cols = set(scan._columns(conn, "sidecars_raw"))
    assert res["abort"] is None and res["items"] and res["sidecars"]
    for row, _, _ in res["items"]:
        assert set(row) <= item_cols
        assert item_cols - set(row) == set()  # every column is produced for a good photo
    for row in res["sidecars"]:
        assert set(row) <= side_cols


def test_spawn_pool_matches_in_process(fx, tmp_path, local_w1):
    _, ref, ref_pack = local_w1
    info, meta, pack = run_shard(scan.LocalOpener(fx / ZIP1), fx / ZIP1, tmp_path, workers=2)
    assert stable(dump(meta)) == stable(ref)
    assert pack_rows(pack) == ref_pack
    assert info["pack_sha256"] == scan._sha256_file(pack)
    assert not list(tmp_path.glob("*.part"))


def test_local_scan_never_opens_unwanted_members(fx, tmp_path, monkeypatch):
    opened: list[str] = []
    real_open = zipfile.ZipFile.open

    def spy(self, name, *args, **kw):
        opened.append(name.filename if isinstance(name, zipfile.ZipInfo) else name)
        return real_open(self, name, *args, **kw)

    monkeypatch.setattr(zipfile.ZipFile, "open", spy)
    run_shard(scan.LocalOpener(fx / ZIP1), fx / ZIP1, tmp_path, workers=1, embedder="none")
    unwanted = unwanted_names(fx / ZIP1)
    assert unwanted and opened
    assert not set(opened) & unwanted


@pytest.mark.parametrize("workers", [1, 2])
def test_http_opener_matches_local_and_skips_unwanted_bytes(served, tmp_path, local_w1, workers):
    path, srv = served
    _, ref, ref_pack = local_w1
    opener = http_opener(srv, path)
    srv.log.clear()  # only the scan's own requests (the tail was fetched above)
    info, meta, pack = run_shard(opener, path, tmp_path, workers=workers)
    assert stable(dump(meta)) == stable(ref)
    assert pack_rows(pack) == ref_pack

    ranges = member_ranges(path)
    forbidden = [ranges[n] for n in unwanted_names(path)]
    assert srv.log
    for req in srv.log:
        for lo, hi in forbidden:
            assert not (req[0] < hi and lo < req[1]), "read bytes of a video/skipped member"
    # The workers' byte accounting reaches shard_info.
    assert 0 < info["bytes_read"] <= sum(e - s for s, e in srv.log)


@pytest.mark.rclone
def test_real_rclone_serve_http_matches_local(fx, tmp_path, local_w1):
    binary = os.environ.get("GPCLEAN_RCLONE") or shutil.which("rc" + "lone")
    if not binary or not Path(binary).exists():
        pytest.skip("no rclone binary (set GPCLEAN_RCLONE or put it on PATH)")
    from gpclean.rclone import Rclone

    root = tmp_path / "remote" / "Takeout"
    root.mkdir(parents=True)
    shutil.copy2(fx / ZIP1, root / ZIP1)
    config = tmp_path / "rclone.conf"
    config.write_text(f"[gp]\ntype = alias\nremote = {root.parent.as_posix()}\n",
                      encoding="utf-8")
    rc = Rclone(config=config, binary=binary, log_file=tmp_path / "private" / "rclone.log")
    _, ref, ref_pack = local_w1
    size = (root / ZIP1).stat().st_size
    with rc.serve_http("gp:Takeout") as base:
        url = base + "/" + urllib.parse.quote(ZIP1)
        with ZipSource.open_http(url, size, backoff=0.0) as src:
            tail = src.tail()
        opener = scan.HttpOpener(url, size, tail, backoff=0.0)
        _, meta, pack = run_shard(opener, root / ZIP1, tmp_path, workers=2)
    assert stable(dump(meta)) == stable(ref)
    assert pack_rows(pack) == ref_pack


def test_repr_of_openers_hides_paths_and_urls(tmp_path):
    assert "secret" not in repr(scan.LocalOpener(tmp_path / "secret.zip"))
    assert "secret" not in repr(scan.HttpOpener("http://127.0.0.1/secret.zip", 10, b"secret"))


# ------------------------------------------------------------------------------ failures


def _flip_member_byte(path: Path, name: str) -> None:
    """Corrupt one data byte of a stored member (its CRC-32 check then fails)."""
    with zipfile.ZipFile(path) as zf:
        info = zf.getinfo(name)
    with open(path, "r+b") as f:
        f.seek(info.header_offset + 26)
        name_len, extra_len = struct.unpack("<HH", f.read(4))
        pos = info.header_offset + 30 + name_len + extra_len + info.compress_size // 2
        f.seek(pos)
        b = f.read(1)
        f.seek(pos)
        f.write(bytes([b[0] ^ 0xFF]))


def test_per_item_errors_are_recorded(fx, tmp_path):
    src_zip = fx / "takeout-20260201T000000Z-001.zip"
    path = tmp_path / "takeout-20260301T000000Z-001.zip"
    good = Image.new("RGB", (64, 48), (200, 30, 30))
    buf = io.BytesIO()
    good.save(buf, "JPEG")
    with zipfile.ZipFile(src_zip) as zin, zipfile.ZipFile(path, "w") as zout:
        for info in zin.infolist():
            zout.writestr(info, zin.read(info))
        zout.writestr(PHOTOS + "garbage.jpg", random.Random(3).randbytes(5000))
        zout.writestr(PHOTOS + "badcrc.jpg", buf.getvalue(), compress_type=zipfile.ZIP_STORED)
    _flip_member_byte(path, PHOTOS + "badcrc.jpg")

    info, meta, pack = run_shard(scan.LocalOpener(path), path, tmp_path, workers=1)
    rows = {r["member"]: r for r in dump(meta)["items_raw"]}
    bad = rows[PHOTOS + "badcrc.jpg"]
    assert bad["err"] == "BadZipFile"
    assert bad["file_size"] == len(buf.getvalue()) and bad["crc32"] is not None
    assert bad["sha256"] is None and bad["emb"] is None
    garbage = rows[PHOTOS + "garbage.jpg"]
    assert garbage["err"] and garbage["sha256"] is None  # undecodable, class name only
    assert info["n_err"] == 2
    good_rows = [r for r in rows.values() if r["err"] is None]
    assert len(good_rows) == info["n_items"] - 2 > 0
    assert set(pack_rows(pack)) == {r["member_idx"] for r in good_rows}


@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("status", [503, 404])
def test_backend_failure_aborts_shard_without_writing(served, tmp_path, status, workers):
    path, srv = served
    opener = http_opener(srv, path, retries=0)
    meta, pack = tmp_path / "s.meta.sqlite", tmp_path / "s.pack.sqlite"
    meta.write_bytes(b"stale checkpoint")
    srv.status = status
    with pytest.raises(scan.ShardAborted):
        run_shard(opener, path, tmp_path, workers=workers)
    assert not meta.exists() and not pack.exists()
    assert not list(tmp_path.glob("*.part"))


class _BrokenOpener:
    """Opener whose source fails every data read like a dead backend would."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __call__(self) -> ZipSource:
        src = ZipSource.open_local(self.path)

        def fail(*args, **kw):
            raise OSError("disk went away")

        src._backing.readinto = fail
        src._backing.broken = True
        return src


def test_broken_source_aborts(fx, tmp_path):
    with pytest.raises(scan.ShardAborted):
        run_shard(_BrokenOpener(fx / ZIP1), fx / ZIP1, tmp_path, workers=1)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("workers", [1, 2])
def test_deadline_raises_timeout_without_writing(fx, tmp_path, workers):
    with pytest.raises(scan.ShardTimeout):
        run_shard(scan.LocalOpener(fx / ZIP1), fx / ZIP1, tmp_path, workers=workers,
                  deadline=time.monotonic() - 1)
    assert not list(tmp_path.iterdir())


def _patch_member_header(path: Path, name: str, *, flag_bits: int = 0,
                         method: int | None = None) -> None:
    """Rewrite one member's general-purpose flags / compression method in both its local
    header and its central-directory record (neither field is covered by a CRC)."""
    data = bytearray(path.read_bytes())
    with zipfile.ZipFile(path) as zf:
        lh = zf.getinfo(name).header_offset
        pos = zf.start_dir
    while True:  # find the member's central-directory record
        assert data[pos:pos + 4] == b"PK\x01\x02"
        n_len, x_len, c_len = struct.unpack_from("<HHH", data, pos + 28)
        if data[pos + 46:pos + 46 + n_len].decode() == name:
            break
        pos += 46 + n_len + x_len + c_len
    for flags_at, method_at in ((pos + 8, pos + 10), (lh + 6, lh + 8)):
        struct.pack_into("<H", data, flags_at, struct.unpack_from("<H", data, flags_at)[0] | flag_bits)
        if method is not None:
            struct.pack_into("<H", data, method_at, method)
    path.write_bytes(bytes(data))


@pytest.mark.parametrize("workers", [1, 2])
def test_encrypted_and_unsupported_members_are_item_errors(fx, tmp_path, workers):
    path = tmp_path / "takeout-20260301T000000Z-001.zip"
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 120, 30)).save(buf, "JPEG")
    with zipfile.ZipFile(fx / "takeout-20260201T000000Z-001.zip") as zin, \
            zipfile.ZipFile(path, "w") as zout:
        for info in zin.infolist():
            zout.writestr(info, zin.read(info))
        for name in ("secret.jpg", "d64.jpg", "fine.jpg"):
            zout.writestr(PHOTOS + name, buf.getvalue(), compress_type=zipfile.ZIP_STORED)
    _patch_member_header(path, PHOTOS + "secret.jpg", flag_bits=0x1)  # "encrypted"
    _patch_member_header(path, PHOTOS + "d64.jpg", method=9)  # deflate64: not supported

    info, meta, pack = run_shard(scan.LocalOpener(path), path, tmp_path, workers=workers)
    rows = {r["member"]: r for r in dump(meta)["items_raw"]}
    assert rows[PHOTOS + "secret.jpg"]["err"] == "RuntimeError"
    assert rows[PHOTOS + "d64.jpg"]["err"] == "NotImplementedError"
    assert rows[PHOTOS + "fine.jpg"]["err"] is None
    assert rows[PHOTOS + "secret.jpg"]["file_size"] == len(buf.getvalue())
    assert info["n_err"] == 2 and info["n_items"] == len(rows)


def test_oversized_sidecars_are_skipped_unread(fx, tmp_path):
    cfg = ScanConfig(clip_model="none", photos_per_shard=1000, max_member_bytes=600)
    _, meta, _ = run_shard(scan.LocalOpener(fx / ZIP1), fx / ZIP1, tmp_path, cfg=cfg,
                           embedder="none")
    d = dump(meta)
    sidecars = {e.member_idx for e in entries_of(fx / ZIP1)
                if classify(e.name, include_albums=False).kind == "sidecar"}
    assert sidecars and d["sidecars_raw"] == []
    reasons = {r["member_idx"]: r["reason"] for r in d["skipped_raw"]}
    assert all(reasons.get(i) == "too_large" for i in sidecars)


def test_embedder_must_match_cfg(fx, tmp_path):
    cfg = ScanConfig(clip_model="b32", photos_per_shard=1000)
    with pytest.raises(ValueError):
        run_shard(scan.LocalOpener(fx / ZIP1), fx / ZIP1, tmp_path, cfg=cfg, embedder="b16")
    with pytest.raises(ValueError):
        scan.ScanPool(2, cfg, "none")
    assert not list(tmp_path.iterdir())


# ------------------------------------------------------------------ dying / stuck workers


@dataclasses.dataclass(frozen=True)
class _CrashOpener:
    """Opens a local zip; in a worker process, reading member ``poison`` kills the process
    the way a segfaulting decoder would (``poison=None``: dies as soon as it is opened)."""

    path: Path
    poison: str | None = None

    def __call__(self) -> ZipSource:
        if multiprocessing.parent_process() is None:  # never kill the test process itself
            raise AssertionError("_CrashOpener must only run in a worker process")
        if self.poison is None:
            os._exit(139)
        poison, real_open = self.poison, zipfile.ZipFile.open
        if not getattr(real_open, "_crash_spy", False):

            def crashing_open(zf, name, *args, **kw):
                if (name.filename if isinstance(name, zipfile.ZipInfo) else name) == poison:
                    os._exit(139)
                return real_open(zf, name, *args, **kw)

            crashing_open._crash_spy = True
            zipfile.ZipFile.open = crashing_open
        return ZipSource.open_local(self.path)


def test_member_that_kills_worker_is_recorded_and_shard_completes(fx, tmp_path, local_w1):
    _, ref, ref_pack = local_w1
    images = [r["member"] for r in ref["items_raw"]]
    poison = images[len(images) // 2]
    t0 = time.monotonic()
    info, meta, pack = run_shard(_CrashOpener(fx / ZIP1, poison), fx / ZIP1, tmp_path,
                                 workers=2)
    assert time.monotonic() - t0 < 60  # no waiting for a deadline: there is none
    d = dump(meta)
    rows = {r["member"]: r for r in d["items_raw"]}
    bad = rows[poison]
    ref_bad = next(r for r in ref["items_raw"] if r["member"] == poison)
    assert bad["err"] == scanworker.WORKER_CRASH and bad["sha256"] is None
    assert (bad["file_size"], bad["crc32"]) == (ref_bad["file_size"], ref_bad["crc32"])
    assert info["n_err"] == 1 and info["n_items"] == len(ref["items_raw"])
    # Everything else is exactly what a clean scan produces.
    others = [r for r in d["items_raw"] if r["member"] != poison]
    assert others == [r for r in ref["items_raw"] if r["member"] != poison]
    for table in ("sidecars_raw", "videos_raw", "skipped_raw"):
        assert d[table] == ref[table]
    assert pack_rows(pack) == {k: v for k, v in ref_pack.items() if k != ref_bad["member_idx"]}


def test_worker_dying_at_start_aborts_quickly(fx, tmp_path):
    t0 = time.monotonic()
    with pytest.raises(scan.ShardAborted):
        run_shard(_CrashOpener(fx / ZIP1), fx / ZIP1, tmp_path, workers=2)
    assert time.monotonic() - t0 < 60
    assert not list(tmp_path.iterdir())


@dataclasses.dataclass(frozen=True)
class _StuckOpener:
    """Opens a local zip only after a long sleep (a worker stuck on a dead connection)."""

    path: Path

    def __call__(self) -> ZipSource:
        time.sleep(120)
        return ZipSource.open_local(self.path)


def test_stuck_worker_hits_deadline_and_is_killed(fx, tmp_path):
    cfg, name = CFG, "stub"
    t0 = time.monotonic()
    with scan.ScanPool(2, cfg, name) as pool:
        entries, spec = one_shard(fx / ZIP1)
        with pytest.raises(scan.ShardTimeout):
            scan.scan_shard(_StuckOpener(fx / ZIP1), spec, entries, cfg,
                            meta_path=tmp_path / "m.sqlite", pack_path=tmp_path / "p.sqlite",
                            workers=2, embedder_name=name, deadline=time.monotonic() + 3,
                            pool=pool)
        # The stuck processes were killed; the pool starts fresh ones for the next shard.
        info = scan.scan_shard(scan.LocalOpener(fx / ZIP1), spec, entries, cfg,
                               meta_path=tmp_path / "m.sqlite", pack_path=tmp_path / "p.sqlite",
                               workers=2, embedder_name=name, pool=pool)
    assert info["n_items"] > 0
    assert time.monotonic() - t0 < 60


# --------------------------------------------------------------------------- pool reuse


def test_scan_pool_is_reused_across_shards_and_zips(fx, tmp_path, local_w1):
    _, ref, ref_pack = local_w1
    zip2 = fx / "takeout-20260101T000000Z-002.zip"
    with scan.ScanPool(2, CFG, "stub") as pool:
        results = []
        for i, path in enumerate([fx / ZIP1, zip2, fx / ZIP1]):
            entries, spec = one_shard(path)
            meta, pack = tmp_path / f"{i}.meta.sqlite", tmp_path / f"{i}.pack.sqlite"
            scan.scan_shard(scan.LocalOpener(path), spec, entries, CFG, meta_path=meta,
                            pack_path=pack, workers=2, embedder_name="stub", pool=pool)
            results.append((dump(meta), pack_rows(pack)))
            if i == 0:
                executor = pool._executor
            assert pool._executor is executor  # same processes, not a new pool per shard
    def same(d):  # only the pack's file name differs from the reference scan
        d = stable(d)
        d["shard_info"].pop("pack_name")
        return d

    ref_s = stable(ref)
    ref_s["shard_info"].pop("pack_name")
    for d, p in (results[0], results[2]):
        assert same(d) == ref_s and p == ref_pack
    # The second zip's scan is the same as with a pool of its own.
    own = tmp_path / "own"
    own.mkdir()
    _, own_meta, own_pack = run_shard(scan.LocalOpener(zip2), zip2, own, workers=1)
    assert same(results[1][0]) == same(dump(own_meta))
    assert results[1][1] == pack_rows(own_pack)
    with pytest.raises(RuntimeError):
        next(pool.run(scan.LocalOpener(zip2), [(0, [])]))  # closed


def test_http_tail_is_shipped_to_workers_once(served, tmp_path):
    path, srv = served
    opener = http_opener(srv, path)
    with scan.ScanPool(2, CFG, "stub") as pool:
        light = pool._worker_opener(opener)
        assert light.tail == b"" and Path(light.tail_file).read_bytes() == opener.tail
        assert pool._worker_opener(opener) is light  # one temp file per zip
        tails_dir = Path(light.tail_file).parent
        assert "secret" not in repr(light)
    assert not tails_dir.exists()  # removed with the pool


# ----------------------------------------------------------------------------- real CLIP


def _b32_cached() -> bool:
    try:
        from gpclean.clipmodel import fetch_model

        fetch_model("b32", download=False)
        return True
    except Exception:  # noqa: BLE001 - any failure means "not usable here"
        return False


@pytest.mark.clip
@pytest.mark.slow
@pytest.mark.skipif(os.environ.get("GPCLEAN_TEST_CLIP") != "1", reason="set GPCLEAN_TEST_CLIP=1")
def test_real_clip_fetches_once_in_parent(fx, tmp_path, monkeypatch):
    if not _b32_cached():
        pytest.skip("b32 weights are not cached")
    import gpclean.clipmodel as clipmodel

    calls: list[str] = []
    real_fetch = clipmodel.fetch_model

    def counting_fetch(name, **kw):
        calls.append(name)
        return real_fetch(name, **kw)

    monkeypatch.setattr(clipmodel, "fetch_model", counting_fetch)
    monkeypatch.setattr(scan, "_FETCHED_MODELS", set())
    cfg = ScanConfig(clip_model="b32", photos_per_shard=1000)
    with scan.ScanPool(2, cfg, "b32") as pool:
        for i, path in enumerate([fx / ZIP1, fx / "takeout-20260201T000000Z-001.zip"]):
            entries, spec = one_shard(path, cfg)
            info = scan.scan_shard(scan.LocalOpener(path), spec, entries, cfg,
                                   meta_path=tmp_path / f"{i}.m", pack_path=tmp_path / f"{i}.p",
                                   workers=2, embedder_name="b32", pool=pool)
            d = dump(tmp_path / f"{i}.m")
            assert info["n_err"] == 0 and info["clip_model"] == "b32"
            for row in d["items_raw"]:
                emb = np.frombuffer(row["emb"], dtype="<f2").astype(np.float32)
                assert emb.shape == (512,) and abs(float(np.linalg.norm(emb)) - 1) < 0.01
    assert calls == ["b32"]  # once, in the parent, before any worker started


# ------------------------------------------------------------------------------ run-local


def test_run_local_resumes_and_invalidates(fx, tmp_path, monkeypatch, capsys):
    calls: list[dict] = []

    def fake_build_bundle(meta_paths, pack_dir, out_dir, mcfg, *, cfg_hash, clip_model, **kw):
        calls.append({"meta_paths": list(meta_paths), "pack_dir": pack_dir, "out": out_dir,
                      "threshold": mcfg.threshold, "cfg_hash": cfg_hash, "clip": clip_model})
        return {"counts": {"items": 1}}

    fake = types.ModuleType("gpclean.merge.bundle")
    fake.build_bundle = fake_build_bundle
    monkeypatch.setitem(sys.modules, "gpclean.merge.bundle", fake)

    scanned: list[int] = []
    real_scan = scan.scan_shard

    def counting_scan(*args, **kw):
        scanned.append(1)
        return real_scan(*args, **kw)

    monkeypatch.setattr(scan, "scan_shard", counting_scan)
    zips = tmp_path / "zips"
    shutil.copytree(fx, zips)
    out = tmp_path / "out"
    kw = dict(zips=zips, out=out, include_albums=False, threshold=4, clip_model="b32",
              no_clip=True, workers=1, photos_per_shard=20)

    assert scan.cli_run_local(**kw) == 0
    n_shards = len(scanned)
    assert n_shards == 4  # 44 + 33 + 5 photos at 20 per shard: 2 + 1 + 1
    cfg_hash = ScanConfig(clip_model="none", photos_per_shard=20).cfg_hash()
    metas = calls[0]["meta_paths"]
    assert len(metas) == n_shards and all(p.is_file() for p in metas)
    assert all(p.parent.parent == out / "work" / cfg_hash for p in metas)
    assert calls[0]["pack_dir"] == out / "thumbs" and calls[0]["out"] == out
    assert calls[0]["threshold"] == 4 and calls[0]["clip"] == "none"
    assert calls[0]["cfg_hash"] == cfg_hash
    assert len(list((out / "thumbs").glob("*-0000.sqlite"))) == 3
    conn = open_readonly(metas[0])
    assert conn.execute("SELECT count(*) FROM items_raw WHERE emb IS NOT NULL").fetchone()[0] == 0
    conn.close()
    err = capsys.readouterr().err
    assert "Photos from" not in err and ".jpg" not in err and "takeout-" not in err

    # Second run: everything is already done.
    scanned.clear()
    assert scan.cli_run_local(**kw) == 0
    assert scanned == [] and calls[1]["meta_paths"] == metas

    # A checkpoint from another config is rescanned; only that one.
    conn = sqlite3.connect(metas[1])
    conn.execute("UPDATE shard_info SET value = 'other' WHERE key = 'cfg'")
    conn.commit()
    conn.close()
    assert scan.cli_run_local(**kw) == 0
    assert len(scanned) == 1
    assert scan.read_shard_info(metas[1])["cfg"] == cfg_hash


def test_run_local_rescans_a_shard_whose_pack_is_missing(fx, tmp_path, monkeypatch):
    fake = types.ModuleType("gpclean.merge.bundle")
    fake.build_bundle = lambda *a, **kw: {"counts": {}}
    monkeypatch.setitem(sys.modules, "gpclean.merge.bundle", fake)
    scanned: list[int] = []
    real_scan = scan.scan_shard

    def counting_scan(opener, spec, *args, **kw):
        scanned.append(spec.shard)
        return real_scan(opener, spec, *args, **kw)

    monkeypatch.setattr(scan, "scan_shard", counting_scan)
    zips = tmp_path / "zips"
    zips.mkdir()
    shutil.copy2(fx / ZIP1, zips / ZIP1)
    out = tmp_path / "out"
    kw = dict(zips=zips, out=out, include_albums=False, threshold=3, clip_model="b32",
              no_clip=True, workers=2, photos_per_shard=20)
    assert scan.cli_run_local(**kw) == 0
    assert scanned == [0, 1]
    cfg_hash = ScanConfig(clip_model="none", photos_per_shard=20).cfg_hash()
    zipkey = scan.zipkey_local(zips / ZIP1)
    meta1 = out / "work" / cfg_hash / zipkey / "0001.meta.sqlite"
    pack1 = out / "thumbs" / f"{zipkey}-0001.sqlite"
    assert scan.shard_is_current(meta1, pack1, cfg_hash)
    pack1.unlink()
    assert not scan.shard_is_current(meta1, pack1, cfg_hash)
    scanned.clear()
    assert scan.cli_run_local(**kw) == 0
    assert scanned == [1] and pack1.is_file()


def test_default_local_workers(monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 16)
    monkeypatch.setattr(scan, "_total_ram_bytes", lambda: 8 * 1024 ** 3)
    assert scan.default_local_workers("none") == 16
    assert scan.default_local_workers("b32") == 2  # ~1.5 GB per CLIP process
    monkeypatch.setattr(scan, "_total_ram_bytes", lambda: 64 * 1024 ** 3)
    assert scan.default_local_workers("b32") == scan.LOCAL_CLIP_WORKERS_MAX
    monkeypatch.setattr(scan, "_total_ram_bytes", lambda: None)
    assert scan.default_local_workers("b16") == scan.LOCAL_CLIP_WORKERS_MAX
    monkeypatch.setattr(scan, "_total_ram_bytes", lambda: 2 * 1024 ** 3)
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    assert scan.default_local_workers("b32") == 1


def test_total_ram_is_readable_or_none():
    ram = scan._total_ram_bytes()
    assert ram is None or ram > 0


def test_run_local_without_zips(tmp_path):
    rc = scan.cli_run_local(zips=tmp_path, out=tmp_path / "o", include_albums=False,
                            threshold=3, clip_model="b32", no_clip=True, workers=1,
                            photos_per_shard=1000)
    assert rc == 2
