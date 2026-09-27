"""Tests for gpclean.store: relative-path validation, LocalStore, RcloneStore."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from gpclean.rclone import Rclone, RcloneDenied
from gpclean.store import LocalStore, RcloneStore, check_rel

BAD_RELS = [
    "/etc/passwd", "../x", "a/../b", "a/./b", "./a", "a//b", "a\\b", "C:/x", "C:x",
    "gp:gpclean-output/x", "-R", "a/--dump", "a\nb", "a\x00b", "\\\\server\\share", "..",
]


@pytest.mark.parametrize("rel", BAD_RELS)
def test_check_rel_rejects(rel):
    with pytest.raises(ValueError):
        check_rel(rel)


@pytest.mark.parametrize("rel,expected", [
    ("a", "a"), ("work/cfg/abc/0001.meta.sqlite", "work/cfg/abc/0001.meta.sqlite"),
    ("dir/", "dir"), ("with space/x y.txt", "with space/x y.txt"), ("a-b/c_d.e", "a-b/c_d.e"),
])
def test_check_rel_accepts(rel, expected):
    assert check_rel(rel) == expected


def test_check_rel_root_only_when_allowed():
    with pytest.raises(ValueError):
        check_rel("")
    assert check_rel("", allow_root=True) == ""
    with pytest.raises(TypeError):
        check_rel(Path("a"))  # type: ignore[arg-type]


# ----- LocalStore -----------------------------------------------------------------------------

def test_local_store_roundtrip(tmp_path):
    store = LocalStore(tmp_path / "out")
    src = tmp_path / "meta.sqlite"
    src.write_bytes(b"shard-meta")
    assert not store.exists("work/c1/z/0001.meta.sqlite")
    assert store.list("work") == []

    store.put(src, "work/c1/z/0001.meta.sqlite")
    store.put(src, "work/c1/z/0002.meta.sqlite")
    store.put(src, "work/c1/y/0001.meta.sqlite")
    assert store.exists("work/c1/z/0001.meta.sqlite")
    assert store.exists("work/c1")
    assert store.list("work/c1") == ["y/0001.meta.sqlite", "z/0001.meta.sqlite",
                                     "z/0002.meta.sqlite"]
    assert store.list() == ["work/c1/y/0001.meta.sqlite", "work/c1/z/0001.meta.sqlite",
                            "work/c1/z/0002.meta.sqlite"]

    dst = tmp_path / "back" / "m.sqlite"
    store.get("work/c1/z/0002.meta.sqlite", dst)
    assert dst.read_bytes() == b"shard-meta"
    # no temp files are left behind
    assert [p.name for p in (tmp_path / "back").iterdir()] == ["m.sqlite"]

    # put overwrites atomically
    src.write_bytes(b"v2")
    store.put(src, "work/c1/z/0002.meta.sqlite")
    store.get("work/c1/z/0002.meta.sqlite", dst)
    assert dst.read_bytes() == b"v2"

    store.mkdirs(["bundle/c1/thumbs", "logs/1-1"])
    assert (tmp_path / "out" / "bundle" / "c1" / "thumbs").is_dir()
    assert (tmp_path / "out" / "logs" / "1-1").is_dir()


@pytest.mark.parametrize("rel", ["../escape", "/abs", "a\\b", "C:/x"])
def test_local_store_rejects_bad_paths(rel, tmp_path):
    store = LocalStore(tmp_path / "out")
    src = tmp_path / "f"
    src.write_bytes(b"x")
    with pytest.raises(ValueError):
        store.put(src, rel)
    with pytest.raises(ValueError):
        store.get(rel, tmp_path / "g")
    with pytest.raises(ValueError):
        store.exists(rel)
    with pytest.raises(ValueError):
        store.list(rel)
    with pytest.raises(ValueError):
        store.mkdirs([rel])
    assert not (tmp_path / "escape").exists()


def test_local_store_get_missing_raises(tmp_path):
    store = LocalStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.get("nope", tmp_path / "x")
    assert not (tmp_path / "x").exists()


# ----- RcloneStore with a recording fake rclone ------------------------------------------------

FAKE = r'''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_RCLONE_RECORD"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(args) + "\n")
sys.stdout.write(os.environ.get("FAKE_RCLONE_STDOUT", ""))
sys.exit(int(os.environ.get("FAKE_RCLONE_RC", "0")))
'''


@pytest.fixture
def fake_rclone(tmp_path, monkeypatch):
    for var in ("FAKE_RCLONE_RC", "FAKE_RCLONE_STDOUT", "GPCLEAN_PRIVATE_DIR"):
        monkeypatch.delenv(var, raising=False)
    record = tmp_path / "argv.jsonl"
    monkeypatch.setenv("FAKE_RCLONE_RECORD", str(record))
    script = tmp_path / "fake_rclone.py"
    script.write_text(FAKE, encoding="utf-8")
    if os.name == "nt":
        binary = tmp_path / "rclone.cmd"
        binary.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        binary = tmp_path / "rclone"
        binary.write_text(f"#!{sys.executable}\n" + FAKE, encoding="utf-8")
        binary.chmod(0o755)
    rc = Rclone(binary=str(binary), log_file=tmp_path / "rclone.log",
                out_root="gp:gpclean-output")

    def calls():
        if not record.exists():
            return []
        # drop the common trailing flags (--log-file ...) that every call carries
        argvs = [json.loads(x) for x in record.read_text(encoding="utf-8").splitlines()]
        return [a[:a.index("--log-file")] for a in argvs]

    return rc, calls


def test_rclone_store_root_must_be_in_out_root(fake_rclone):
    rc, _ = fake_rclone
    for root in ("gp:Takeout", "gp:gpclean-output-x", "other:gpclean-output", "gp:"):
        with pytest.raises(ValueError):
            RcloneStore(rc, root)
    RcloneStore(rc, "gp:gpclean-output")
    RcloneStore(rc, "gp:gpclean-output/selftest/")


def test_rclone_store_commands(fake_rclone, tmp_path, monkeypatch):
    rc, calls = fake_rclone
    store = RcloneStore(rc, "gp:gpclean-output/selftest")
    local = tmp_path / "m.sqlite"
    local.write_bytes(b"x")
    store.put(local, "work/c1/0001.meta.sqlite")
    store.mkdirs(["work/c1/z", "bundle/c1/thumbs", "work/c1", "work/c1/z"])
    assert calls() == [
        ["copyto", str(local), "gp:gpclean-output/selftest/work/c1/0001.meta.sqlite"],
        ["mkdir", "gp:gpclean-output/selftest/work/c1"],
        ["mkdir", "gp:gpclean-output/selftest/bundle/c1/thumbs"],
        ["mkdir", "gp:gpclean-output/selftest/work/c1/z"],
    ]
    with pytest.raises(ValueError):
        store.put(local, "../../Takeout/x")


def test_rclone_store_list_and_exists(fake_rclone, monkeypatch, tmp_path):
    rc, calls = fake_rclone
    store = RcloneStore(rc, "gp:gpclean-output")
    monkeypatch.setenv("FAKE_RCLONE_STDOUT", json.dumps([
        {"Path": "z/0002.meta.sqlite", "IsDir": False},
        {"Path": "a/0001.meta.sqlite", "IsDir": False},
    ]))
    assert store.list("work/c1") == ["a/0001.meta.sqlite", "z/0002.meta.sqlite"]
    assert calls()[-1] == ["lsjson", "gp:gpclean-output/work/c1", "--recursive", "--files-only"]
    monkeypatch.setenv("FAKE_RCLONE_RC", "3")  # directory not found
    assert store.list("work/none") == []
    assert store.exists("work/none") is False
    monkeypatch.setenv("FAKE_RCLONE_RC", "0")
    monkeypatch.setenv("FAKE_RCLONE_STDOUT", '{"Path": "x", "IsDir": false}')
    assert store.exists("work/x") is True


def test_rclone_store_get_uses_cat(fake_rclone, monkeypatch, tmp_path):
    rc, calls = fake_rclone
    store = RcloneStore(rc, "gp:gpclean-output")
    monkeypatch.setenv("FAKE_RCLONE_STDOUT", "remote-bytes")
    store.get("bundle/c1/manifest.txt", tmp_path / "dl" / "manifest.txt")
    assert (tmp_path / "dl" / "manifest.txt").read_bytes() == b"remote-bytes"
    assert calls()[-1] == ["cat", "gp:gpclean-output/bundle/c1/manifest.txt"]
    assert sorted(p.name for p in (tmp_path / "dl").iterdir()) == ["manifest.txt"]


def test_rclone_store_get_failure_leaves_nothing(fake_rclone, monkeypatch, tmp_path):
    rc, _ = fake_rclone
    store = RcloneStore(rc, "gp:gpclean-output")
    monkeypatch.setenv("FAKE_RCLONE_RC", "4")
    with pytest.raises(Exception):
        store.get("missing", tmp_path / "dl" / "f")
    assert list((tmp_path / "dl").iterdir()) == []


def test_rclone_store_cannot_escape_via_rclone(fake_rclone):
    rc, calls = fake_rclone
    with pytest.raises(RcloneDenied):
        rc.copyto("x", "gp:Takeout/x")
    assert calls() == []


# ----- RcloneStore with a real rclone and an alias remote (marked) ------------------------------

@pytest.mark.rclone
def test_rclone_store_real_roundtrip(tmp_path):
    binary = os.environ.get("GPCLEAN_RCLONE") or shutil.which("rclone")
    if not binary:
        pytest.skip("no rclone binary (set GPCLEAN_RCLONE or put rclone on PATH)")
    drive = tmp_path / "drive"
    drive.mkdir()
    config = tmp_path / "rclone.conf"
    config.write_text(f"[gp]\ntype = alias\nremote = {drive.as_posix()}\n", encoding="utf-8")
    rc = Rclone(config=config, binary=binary, log_file=tmp_path / "rclone.log",
                out_root="gp:gpclean-output")
    store = RcloneStore(rc, "gp:gpclean-output")
    local = tmp_path / "meta.sqlite"
    local.write_bytes(b"meta-bytes")

    assert store.list("work") == []
    assert not store.exists("work/c1/0001.meta.sqlite")
    store.mkdirs(["work/c1", "bundle/c1/thumbs"])
    store.put(local, "work/c1/0001.meta.sqlite")
    store.put(local, "work/c1/sub/0002.meta.sqlite")
    assert store.exists("work/c1/0001.meta.sqlite")
    assert store.list("work/c1") == ["0001.meta.sqlite", "sub/0002.meta.sqlite"]
    store.get("work/c1/sub/0002.meta.sqlite", tmp_path / "back.sqlite")
    assert (tmp_path / "back.sqlite").read_bytes() == b"meta-bytes"
    assert (drive / "gpclean-output" / "bundle" / "c1" / "thumbs").is_dir()
