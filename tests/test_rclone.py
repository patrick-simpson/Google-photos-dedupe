"""Tests for gpclean.rclone. A fake rclone script records argv, so no real rclone is needed
(except for tests marked ``rclone``, which use a real binary and a local alias remote)."""

from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path

import pytest

from gpclean.rclone import (ALLOWED_VERBS, DENIED_VERBS, SERVE_HTTP_FLAGS, QuotaError, Rclone,
                            RcloneDenied, RcloneError)

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="fake server process needs POSIX")

FAKE_RCLONE = r'''
import json, os, sys
args = sys.argv[1:]
record = os.environ.get("FAKE_RCLONE_RECORD")
if record:
    with open(record, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(args) + "\n")
log = args[args.index("--log-file") + 1] if "--log-file" in args else None
msg = os.environ.get("FAKE_RCLONE_LOG", "")
if msg:
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(msg + "\n")
    else:
        sys.stderr.write(msg + "\n")
if args and args[0] == "serve" and not os.environ.get("FAKE_RCLONE_RC"):
    import http.server
    os.chdir(args[2])
    srv = http.server.HTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("NOTICE: dir: HTTP Server started on [http://127.0.0.1:%d/]\n" % srv.server_port)
    srv.serve_forever()
sys.stdout.write(os.environ.get("FAKE_RCLONE_STDOUT", ""))
sys.exit(int(os.environ.get("FAKE_RCLONE_RC", "0")))
'''


def make_fake_rclone(directory: Path) -> str:
    """Write the fake rclone and return a command name subprocess can execute."""
    script = directory / "fake_rclone.py"
    script.write_text(FAKE_RCLONE, encoding="utf-8")
    if os.name == "nt":
        wrapper = directory / "rclone.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        wrapper = directory / "rclone"
        wrapper.write_text(f"#!{sys.executable}\n" + FAKE_RCLONE, encoding="utf-8")
        wrapper.chmod(0o755)
    return str(wrapper)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """(Rclone using the fake binary, function returning recorded argv lists)."""
    for var in ("FAKE_RCLONE_RC", "FAKE_RCLONE_LOG", "FAKE_RCLONE_STDOUT", "GPCLEAN_PRIVATE_DIR",
                "GPCLEAN_REMOTE", "GPCLEAN_OUT_ROOT"):
        monkeypatch.delenv(var, raising=False)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    record = tmp_path / "argv.jsonl"
    monkeypatch.setenv("FAKE_RCLONE_RECORD", str(record))
    config = tmp_path / "rc" / "rclone.conf"
    config.parent.mkdir()
    config.write_text("", encoding="utf-8")
    rc = Rclone(config=config, binary=make_fake_rclone(bindir),
                log_file=tmp_path / "private" / "rclone.log")

    def calls() -> list[list[str]]:
        if not record.exists():
            return []
        return [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]

    return rc, calls


# ----- policy ---------------------------------------------------------------------------------

def test_allow_and_deny_lists_are_disjoint():
    assert not ALLOWED_VERBS & DENIED_VERBS


@pytest.mark.parametrize("verb", sorted(DENIED_VERBS) + ["mount", "rcd", "selfupdate", ""])
def test_denied_verbs_never_execute(verb, fake):
    rc, calls = fake
    with pytest.raises(RcloneDenied):
        rc._run(verb, ["gp:gpclean-output/x"])
    assert calls() == []


@pytest.mark.parametrize("flag", ["--dump", "--dump=headers", "--drive-acknowledge-abuse",
                                  "--rc", "--config=/tmp/other.conf", "--log-file=x"])
def test_denied_flags(flag, fake):
    rc, calls = fake
    with pytest.raises(RcloneDenied):
        rc._run("lsjson", ["gp:Takeout", flag])
    assert calls() == []


@pytest.mark.parametrize("dst", [
    "gp:Takeout/x.zip", "gp:gpclean-output-evil/x", "gp:gpclean-output/../Takeout",
    "gp:gpclean-output/./x", "gp:gpclean-output//x", "other:gpclean-output/x", "gp:",
    "gp:/gpclean-output/x", "gp:gpclean-output\\x", "/local/path", "gp:GPCLEAN-OUTPUT/x",
])
def test_writes_outside_out_root_denied(dst, fake, tmp_path):
    rc, calls = fake
    with pytest.raises(RcloneDenied):
        rc.copyto(str(tmp_path / "a"), dst)
    with pytest.raises(RcloneDenied):
        rc.copy(str(tmp_path), dst)
    with pytest.raises(RcloneDenied):
        rc.mkdir(dst)
    assert calls() == []


def test_flag_injection_in_paths(fake):
    rc, calls = fake
    for bad in ("-R", "--dump=bodies", "", "gp:a\nb"):
        with pytest.raises(ValueError):
            rc.lsjson(bad)
    with pytest.raises(ValueError):
        rc.copyto("--config=x", "gp:gpclean-output/x")
    assert calls() == []


def test_write_inside_out_root(fake, tmp_path):
    rc, calls = fake
    src = str(tmp_path / "a.sqlite")
    rc.copyto(src, "gp:gpclean-output/work/abc/0001.meta.sqlite")
    rc.copy(str(tmp_path), "gp:gpclean-output/bundle")
    rc.mkdir("gp:gpclean-output")
    common = ["--config", str(rc.config), "--log-file", str(rc.log_file), "--log-level", "INFO"]
    assert calls() == [
        ["copyto", src, "gp:gpclean-output/work/abc/0001.meta.sqlite", *common],
        ["copy", str(tmp_path), "gp:gpclean-output/bundle", *common],
        ["mkdir", "gp:gpclean-output", *common],
    ]


@pytest.mark.parametrize("root", ["gp:", "gp", "gp:/", "gp:a/../b", "bad name!:x", "gp:a//b"])
def test_out_root_validation(root):
    with pytest.raises(ValueError):
        Rclone(out_root=root)


def test_out_root_default_from_env(monkeypatch):
    monkeypatch.delenv("GPCLEAN_REMOTE", raising=False)
    monkeypatch.delenv("GPCLEAN_OUT_ROOT", raising=False)
    assert Rclone().out_root == "gp:gpclean-output"
    monkeypatch.setenv("GPCLEAN_REMOTE", "rem")
    monkeypatch.setenv("GPCLEAN_OUT_ROOT", "out/sub")
    rc = Rclone()
    assert rc.out_root == "rem:out/sub"
    assert rc.is_under_out_root("rem:out/sub/x") and not rc.is_under_out_root("rem:out/x")


def test_private_dir_default_log_file(monkeypatch, tmp_path):
    monkeypatch.setenv("GPCLEAN_PRIVATE_DIR", str(tmp_path))
    assert Rclone().log_file == tmp_path / "rclone.log"


# ----- read verbs -----------------------------------------------------------------------------

def test_lsjson_parses_and_passes_flags(fake, monkeypatch):
    rc, calls = fake
    listing = [{"Path": "takeout-001.zip", "Size": 5, "IsDir": False}]
    monkeypatch.setenv("FAKE_RCLONE_STDOUT", json.dumps(listing))
    assert rc.lsjson("gp:Takeout", recursive=True, files_only=True, hash=True,
                     max_depth=1) == listing
    argv = calls()[0]
    assert argv[:7] == ["lsjson", "gp:Takeout", "--recursive", "--files-only", "--hash",
                        "--max-depth", "1"]


def test_cat_range_and_stdout_capture(fake, monkeypatch, capfd):
    rc, calls = fake
    monkeypatch.setenv("FAKE_RCLONE_STDOUT", "PKDATA")
    assert rc.cat("gp:Takeout/a.zip", offset=10, count=20) == b"PKDATA"
    assert calls()[0][:6] == ["cat", "gp:Takeout/a.zip", "--offset", "10", "--count", "20"]
    out, err = capfd.readouterr()
    assert out == "" and err == ""  # nothing reaches the console


def test_cat_to_file(fake, monkeypatch, tmp_path):
    rc, _calls = fake
    monkeypatch.setenv("FAKE_RCLONE_STDOUT", "hello")
    rc.cat_to_file("gp:gpclean-output/x", tmp_path / "deep" / "x")
    assert (tmp_path / "deep" / "x").read_bytes() == b"hello"


def test_stderr_goes_to_log_file_not_console(fake, monkeypatch, capfd):
    rc, _calls = fake
    monkeypatch.setenv("FAKE_RCLONE_LOG", "INFO: Takeout/IMG_0001.jpg: copied")
    rc.version()
    out, err = capfd.readouterr()
    assert out == "" and err == ""
    assert "IMG_0001.jpg" in rc.log_file.read_text(encoding="utf-8")


def test_stat_missing_is_none(fake, monkeypatch):
    rc, _calls = fake
    monkeypatch.setenv("FAKE_RCLONE_RC", "3")
    assert rc.stat("gp:gpclean-output/nope") is None
    monkeypatch.setenv("FAKE_RCLONE_RC", "0")
    monkeypatch.setenv("FAKE_RCLONE_STDOUT", '{"Path": "x", "IsDir": false}')
    assert rc.stat("gp:gpclean-output/x") == {"Path": "x", "IsDir": False}


# ----- errors and quota -----------------------------------------------------------------------

@pytest.mark.parametrize("code", ["7", "8"])
def test_quota_exit_codes(code, fake, monkeypatch):
    rc, _calls = fake
    monkeypatch.setenv("FAKE_RCLONE_RC", code)
    with pytest.raises(QuotaError) as info:
        rc.cat("gp:Takeout/a.zip")
    assert info.value.returncode == int(code)


@pytest.mark.parametrize("marker", ["downloadQuotaExceeded", "quotaExceeded",
                                    "userRateLimitExceeded"])
def test_quota_markers_in_log(marker, fake, monkeypatch):
    rc, _calls = fake
    monkeypatch.setenv("FAKE_RCLONE_RC", "1")
    monkeypatch.setenv("FAKE_RCLONE_LOG", f"ERROR : a.zip: googleapi: Error 403: {marker}")
    with pytest.raises(QuotaError):
        rc.cat("gp:Takeout/a.zip")


def test_quota_marker_without_log_file(tmp_path, monkeypatch):
    for var in ("GPCLEAN_PRIVATE_DIR", "FAKE_RCLONE_RECORD", "FAKE_RCLONE_STDOUT"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "bin").mkdir()
    rc = Rclone(binary=make_fake_rclone(tmp_path / "bin"))
    assert rc.log_file is None
    monkeypatch.setenv("FAKE_RCLONE_RC", "1")
    monkeypatch.setenv("FAKE_RCLONE_LOG", "Error 403: downloadQuotaExceeded")
    with pytest.raises(QuotaError):
        rc.cat("gp:Takeout/a.zip")


def test_old_log_lines_do_not_count(fake, monkeypatch):
    rc, _calls = fake
    rc.log_file.parent.mkdir(parents=True, exist_ok=True)
    rc.log_file.write_text("old: downloadQuotaExceeded\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_RCLONE_RC", "1")
    with pytest.raises(RcloneError) as info:
        rc.cat("gp:Takeout/a.zip")
    assert not isinstance(info.value, QuotaError)
    assert "Takeout" not in str(info.value)  # messages carry verb + exit code only
    assert str(info.value) == "rclone cat failed with exit code 1"


# ----- access token ---------------------------------------------------------------------------

FAKE_ACCESS = "fake-access-token-" + "A" * 12
OTHER_ACCESS = "other-access-token-" + "B" * 12


def _token(access: str) -> str:
    refresh_value = "fake-refresh-" + "C" * 8
    return json.dumps({"access_token": access, "token_type": "Bearer",
                       "refresh_token": refresh_value, "expiry": "2026-09-27T12:00:00Z"})


def _write_config(path: Path) -> None:
    lines = [
        "[other]", "type = local", "",
        "[gp]", "type = " + "drive", "scope = drive.readonly,drive.file",
        "token = " + _token(FAKE_ACCESS), "",
        "[gp2]", "type = " + "drive", "token = " + _token(OTHER_ACCESS), "",
        "[broken]", "type = " + "drive", "token = {not json", "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def test_access_token_parsing(fake):
    rc, calls = fake
    _write_config(rc.config)
    assert rc.access_token("gp") == FAKE_ACCESS
    assert rc.access_token("gp:") == FAKE_ACCESS
    assert rc.access_token("gp2") == OTHER_ACCESS
    assert calls() == []  # no refresh unless asked


def test_access_token_errors_never_show_token(fake):
    rc, _calls = fake
    _write_config(rc.config)
    with pytest.raises(KeyError):
        rc.access_token("other")  # section without a token
    with pytest.raises(KeyError):
        rc.access_token("missing")
    with pytest.raises(ValueError) as info:
        rc.access_token("broken")
    assert "not json" not in str(info.value)
    with pytest.raises(ValueError):
        rc.access_token("bad name!")


def test_access_token_encrypted_config(fake):
    rc, _calls = fake
    rc.config.write_text("# Encrypted rclone configuration File\n\nRCLONE_ENCRYPT_V0:\nabc",
                         encoding="utf-8")
    with pytest.raises(ValueError):
        rc.access_token("gp")


def test_access_token_refresh_runs_about(fake, monkeypatch):
    rc, calls = fake
    _write_config(rc.config)
    monkeypatch.setenv("FAKE_RCLONE_STDOUT", '{"total": 1}')
    assert rc.access_token("gp", refresh=True) == FAKE_ACCESS
    assert calls()[0][:3] == ["about", "gp:", "--json"]


def test_access_token_needs_config():
    with pytest.raises(ValueError):
        Rclone().access_token("gp")


# ----- serve http -----------------------------------------------------------------------------

@POSIX_ONLY
def test_serve_http_fake(fake, tmp_path):
    rc, calls = fake
    served = tmp_path / "served"
    served.mkdir()
    (served / "hello.txt").write_bytes(b"hi there")
    with rc.serve_http(str(served)) as base:
        assert base.startswith("http://127.0.0.1:") and not base.endswith("/")
        with urllib.request.urlopen(base + "/hello.txt", timeout=10) as resp:
            assert resp.read() == b"hi there"
    argv = calls()[0]
    assert argv[:3] == ["serve", "http", str(served)]
    assert list(SERVE_HTTP_FLAGS) == argv[3:3 + len(SERVE_HTTP_FLAGS)]
    assert "--read-only" in argv and "127.0.0.1:0" in argv
    with pytest.raises(OSError):  # server was terminated
        urllib.request.urlopen(base + "/hello.txt", timeout=2)


@POSIX_ONLY
def test_serve_http_has_own_log_and_reports_quota(fake, monkeypatch, tmp_path):
    rc, _calls = fake
    served = tmp_path / "served"
    served.mkdir()
    monkeypatch.setenv("FAKE_RCLONE_LOG", "ERROR : x: downloadQuotaExceeded")
    with rc.serve_http(str(served)):
        serve_log = rc.log_file.with_name("rclone-serve.log")
        assert "downloadQuotaExceeded" in serve_log.read_text(encoding="utf-8")
        assert rc.serve_quota_hit()
        # The server's quota line must not turn an unrelated failure into a QuotaError.
        monkeypatch.setenv("FAKE_RCLONE_LOG", "ERROR : network is unreachable")
        monkeypatch.setenv("FAKE_RCLONE_RC", "1")
        with pytest.raises(RcloneError) as info:
            rc.lsjson("gp:Takeout")
        assert not isinstance(info.value, QuotaError)
    assert rc.serve_quota_hit()  # sticky after the server stopped


@POSIX_ONLY
def test_serve_quota_not_hit(fake, tmp_path):
    rc, _calls = fake
    assert not rc.serve_quota_hit()
    with rc.serve_http(str(tmp_path)):
        assert not rc.serve_quota_hit()
    assert not rc.serve_quota_hit()


@POSIX_ONLY
def test_serve_http_without_log_file_uses_temp_log(fake, monkeypatch, tmp_path):
    rc, _calls = fake
    rc.log_file = None
    monkeypatch.setenv("FAKE_RCLONE_LOG", "ERROR : x: downloadLimitExceeded")
    with rc.serve_http(str(tmp_path)) as base:
        assert base.startswith("http://127.0.0.1:")
        assert rc.serve_quota_hit()
    assert not (tmp_path / "private").exists()


def test_serve_http_early_exit(fake, monkeypatch, tmp_path):
    rc, _calls = fake
    monkeypatch.setenv("FAKE_RCLONE_RC", "7")
    with pytest.raises(QuotaError):
        with rc.serve_http(str(tmp_path)):
            pass  # pragma: no cover


# ----- real rclone (marked) -------------------------------------------------------------------

def _real_rclone() -> str:
    binary = os.environ.get("GPCLEAN_RCLONE") or shutil.which("rclone")
    if not binary:
        pytest.skip("no rclone binary (set GPCLEAN_RCLONE or put rclone on PATH)")
    return binary


@pytest.mark.rclone
def test_real_serve_http_range_read(tmp_path):
    binary = _real_rclone()
    root = tmp_path / "remote"
    (root / "Takeout").mkdir(parents=True)
    payload = bytes(range(256)) * 64
    (root / "Takeout" / "a.zip").write_bytes(payload)
    config = tmp_path / "rclone.conf"
    config.write_text(f"[gp]\ntype = alias\nremote = {root.as_posix()}\n", encoding="utf-8")
    rc = Rclone(config=config, binary=binary, log_file=tmp_path / "private" / "rclone.log")
    assert rc.version().startswith("rclone")
    with rc.serve_http("gp:Takeout") as base:
        req = urllib.request.Request(base + "/a.zip", headers={"Range": "bytes=100-199"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            assert resp.status == 206
            assert resp.read() == payload[100:200]
    names = [e["Name"] for e in rc.lsjson("gp:Takeout")]
    assert names == ["a.zip"]
