"""Tests for gpclean.ci / gpclean.probe: the CI steps against a fake Drive.

The fake Drive is a local directory: ``<root>/Takeout`` plays ``gp:Takeout`` and
``<root>/gpclean-output`` the output root. ``FakeRclone`` answers ``lsjson`` from it and
"serves http" with a local ThreadingHTTPServer that supports byte ranges (the same read path
the real ``rclone serve http`` gives), and the store is a ``LocalStore`` on the output root.
``ci.make_rclone`` / ``ci.make_store`` are the seams that get replaced.

The fixture zips are the synthetic ``--small`` export. Everything is fake: no tokens, no
names of real people, no network except 127.0.0.1.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.parse
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from gpclean import ci, publiclog
from gpclean.rclone import QuotaError, RcloneError
from gpclean.store import LocalStore

TESTS_DIR = Path(__file__).resolve().parent
ZIP_NAMES = ("takeout-20260101T000000Z-001.zip", "takeout-20260101T000000Z-002.zip",
             "takeout-20260201T000000Z-001.zip")
ACCESS_1, ACCESS_2, REFRESH, CLIENT_SECRET = "a-fake-1", "a-fake-2", "r-fake", "s-fake"
SCOPES_OK = {"https://www.googleapis.com/auth/drive.readonly",
             "https://www.googleapis.com/auth/drive.file"}

# Every line the public console may show.
PUBLIC_LINE = re.compile(
    r"(PHASE_START|PROGRESS|PHASE_DONE|CHECKPOINT|COUNT|FAILED|OK)"
    r"( [a-z][a-z0-9_]{0,31}=(-?\d+(\.\d+)?(e[-+]?\d+)?|true|false))*"
    r"|FAILED phase=\d+ exc=[A-Za-z_]\w*")


# ------------------------------------------------------------------------ fake Drive


class DirServer:
    """Serve the files of one directory with HTTP byte ranges (like ``rclone serve http``).

    ``status``: answer every GET with this error (used to simulate a quota stop).
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.status: int | None = None
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
                pass

        self.httpd = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def _handle(self, h: BaseHTTPRequestHandler) -> None:
        if self.status is not None:
            h.send_error(self.status)
            return
        name = urllib.parse.unquote(h.path.lstrip("/"))
        path = self.root / name
        if "/" in name or not path.is_file():
            h.send_error(404)
            return
        data = path.read_bytes()
        size = len(data)
        m = re.fullmatch(r"bytes=(\d+)-(\d*)", h.headers.get("Range", ""))
        start = int(m.group(1)) if m else 0
        end = min(int(m.group(2)) + 1, size) if m and m.group(2) else size
        h.send_response(206 if m else 200)
        h.send_header("Content-Length", str(end - start))
        if m:
            h.send_header("Content-Range", f"bytes {start}-{end - 1}/{size}")
        h.end_headers()
        try:
            h.wfile.write(data[start:end])
        except (BrokenPipeError, ConnectionResetError):
            h.close_connection = True


class FakeRclone:
    """The parts of gpclean.rclone.Rclone that gpclean.ci uses, backed by a directory."""

    def __init__(self, root: Path, config: Path | None = None) -> None:
        self.root = root
        self.config = config
        self.quota = False          # what serve_quota_hit() answers
        self.serve_status: int | None = None
        self.refreshes = 0
        self.serves = 0

    def _local(self, remote_path: str) -> Path:
        remote, _, rel = remote_path.partition(":")
        assert remote == "gp"
        return self.root / rel

    def lsjson(self, path: str, *, recursive=False, files_only=False, hash=False,
               max_depth=None) -> list[dict]:
        d = self._local(path)
        if not d.is_dir():
            raise RcloneError("lsjson", 3)
        out = []
        for p in sorted(d.iterdir()):
            if files_only and p.is_dir():
                continue
            mtime = datetime.fromtimestamp(p.stat().st_mtime, UTC)
            entry = {"Path": p.name, "Name": p.name, "Size": p.stat().st_size,
                     "ModTime": mtime.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                     "IsDir": p.is_dir(), "ID": "id-" + p.name}
            if hash and p.is_file():  # like a backend that has md5 and sha256
                data = p.read_bytes()
                entry["Hashes"] = {"md5": hashlib.md5(data).hexdigest(),
                                   "sha256": hashlib.sha256(data).hexdigest()}
            out.append(entry)
        return out

    @contextlib.contextmanager
    def serve_http(self, path: str):
        srv = DirServer(self._local(path))
        srv.status = self.serve_status
        self.serves += 1
        try:
            yield srv.base
        finally:
            srv.close()

    def serve_quota_hit(self) -> bool:
        return self.quota

    def access_token(self, remote: str, *, refresh: bool = False) -> str:
        assert remote == "gp"
        if refresh:
            # Like rclone: a refresh writes the new token back into the config file.
            self.refreshes += 1
            write_conf(self.config, ACCESS_2)
            return ACCESS_2
        return ACCESS_1


def write_conf(path: Path, access: str) -> None:
    """A fake rclone config (built from parts so this source never holds a real-looking one)."""
    token = json.dumps({"access_token": access, "token_type": "Bearer", "refresh_token": REFRESH})
    lines = ["[gp]", "type" + " = " + "drive", "client_id = cid.example",
             "client_secret = " + CLIENT_SECRET, "scope = drive.readonly,drive.file",
             "token = " + token, ""]
    path.write_text("\n".join(lines), encoding="utf-8")


class Drive:
    """Handle for a test's fake Drive and its env."""

    def __init__(self, root: Path, fake: FakeRclone, outputs: Path, private: Path):
        self.root, self.fake, self.outputs_file, self.private = root, fake, outputs, private
        self.store = LocalStore(root / "gpclean-output")

    def outputs(self) -> dict[str, str]:
        """GITHUB_OUTPUT lines written since the last call."""
        text = self.outputs_file.read_text(encoding="utf-8")
        self.outputs_file.write_text("", encoding="utf-8")
        return dict(line.split("=", 1) for line in text.splitlines() if line)


def install_fakes(root: Path, config: Path) -> FakeRclone:
    """Point gpclean.ci's seams at the fake Drive (used by tests and by the child process)."""
    fake = FakeRclone(root, config)
    ci.make_rclone = lambda env: fake
    ci.make_store = lambda env, rc: LocalStore(root / "gpclean-output")
    return fake


@pytest.fixture(scope="module")
def fx(tmp_path_factory) -> Path:
    from gpclean.fixtures.generate import generate

    out = tmp_path_factory.mktemp("fx")
    generate(out, seed=0, small=True)
    return out


def _base_env(monkeypatch, tmp_path: Path, **extra) -> tuple[Path, Path]:
    outputs = tmp_path / "github_output.txt"
    outputs.write_text("", encoding="utf-8")
    private = tmp_path / "private"
    private.mkdir()
    config = tmp_path / "rc" / "rclone.conf"
    config.parent.mkdir()
    write_conf(config, ACCESS_1)
    env = {
        "GPCLEAN_FOLDER": "Takeout", "GPCLEAN_THRESHOLD": "3", "GPCLEAN_INCLUDE_ALBUMS": "false",
        "GPCLEAN_MODE": "full", "GPCLEAN_CLIP_MODEL": "none", "GPCLEAN_WORKERS": "2",
        "GPCLEAN_RUN_ID": "1234567890", "GPCLEAN_ATTEMPT": "1", "GPCLEAN_SCAN_PROCS": "1",
        "GPCLEAN_PRIVATE_DIR": str(private), "GITHUB_OUTPUT": str(outputs),
        "RCLONE_CONFIG": str(config), "GPCLEAN_JOB": "test",
    }
    env.update(extra)
    for key in ("GPCLEAN_PASS", "GPCLEAN_PENDING_IDS", "GPCLEAN_SELFTEST_FAIL_SHARD",
                "GPCLEAN_PHOTOS_PER_SHARD", "GPCLEAN_WORKER", "GPCLEAN_OUT_ROOT",
                "GPCLEAN_REMOTE", "GPCLEAN_JOB_BUDGET_MIN", "GPCLEAN_PENDING_QUOTA",
                "GPCLEAN_QUOTA_PLAN", "GPCLEAN_QUOTA1", "GPCLEAN_QUOTA2", "GPCLEAN_QUOTA3",
                "GPCLEAN_BAD_ZIPS", "GPCLEAN_PLAN_RESULT", "GPCLEAN_MERGE_RESULT",
                "GPCLEAN_PARTIAL", "GPCLEAN_MISSING"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return outputs, private


@pytest.fixture
def drive(tmp_path, monkeypatch, fx):
    """A fake Drive with the three fixture zips in ``Takeout``."""
    root = tmp_path / "drive"
    (root / "Takeout").mkdir(parents=True)
    (root / "gpclean-output").mkdir()
    for name in ZIP_NAMES:
        shutil.copy2(fx / name, root / "Takeout" / name)
    outputs, private = _base_env(monkeypatch, tmp_path)
    fake = FakeRclone(root, Path(os.environ["RCLONE_CONFIG"]))
    monkeypatch.setattr(ci, "make_rclone", lambda env: fake)
    monkeypatch.setattr(ci, "make_store", lambda env, rc: LocalStore(root / "gpclean-output"))
    monkeypatch.setattr(ci, "tokeninfo_scopes", lambda token: set(SCOPES_OK))
    yield Drive(root, fake, outputs, private)


@pytest.fixture(autouse=True)
def _restore_process_state(monkeypatch):
    """The CI entry points install hooks and log handlers; undo that after each test."""
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    root = logging.getLogger()
    level = root.level
    yield
    for handler in list(publiclog._our_handlers):
        root.removeHandler(handler)
        handler.close()
    publiclog._our_handlers.clear()
    root.setLevel(level)
    logging.captureWarnings(False)


def public_lines(text: str) -> list[str]:
    lines = [line for line in text.splitlines() if line]
    bad = [line for line in lines if not PUBLIC_LINE.fullmatch(line)]
    assert not bad, "non-numeric public output"
    return lines


def scan_all(of: int) -> list[int]:
    return [ci.cli_scan(worker=w, of=of) for w in range(of)]


def table(d: Drive, cfg: str) -> dict:
    return json.loads((d.store.root / ci.shards_rel(cfg)).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------- inputs


@pytest.mark.parametrize("name,value", [
    ("GPCLEAN_FOLDER", "../Takeout"), ("GPCLEAN_FOLDER", "-rf"), ("GPCLEAN_FOLDER", "a;b"),
    ("GPCLEAN_FOLDER", "Take out/ x"), ("GPCLEAN_FOLDER", ""), ("GPCLEAN_FOLDER", "a\nb"),
    ("GPCLEAN_THRESHOLD", "7"), ("GPCLEAN_THRESHOLD", "1"), ("GPCLEAN_THRESHOLD", "3;x"),
    ("GPCLEAN_MODE", "delete"), ("GPCLEAN_CLIP_MODEL", "vit"), ("GPCLEAN_WORKERS", "0"),
    ("GPCLEAN_WORKERS", "99"), ("GPCLEAN_INCLUDE_ALBUMS", "maybe"), ("GPCLEAN_RUN_ID", "12a"),
    ("GPCLEAN_ATTEMPT", "0"), ("GPCLEAN_REMOTE", "-x"), ("GPCLEAN_OUT_ROOT", "../x"),
    ("GPCLEAN_JOB", "Scan!"), ("GPCLEAN_PASS", "4"),
])
def test_env_validation_rejects_bad_values(tmp_path, monkeypatch, name, value):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        ci.load_env()


def test_env_defaults_and_selftest_knobs(tmp_path, monkeypatch):
    _base_env(monkeypatch, tmp_path, GPCLEAN_PHOTOS_PER_SHARD="25",
              GPCLEAN_SELFTEST_FAIL_SHARD="1", GPCLEAN_PASS="1")
    env = ci.load_env()
    assert env.folder == "Takeout" and env.threshold == 3 and env.workers == 2
    # Test-only knobs are ignored outside selftest mode.
    assert env.photos_per_shard == 1000 and env.fail_shard is None
    monkeypatch.setenv("GPCLEAN_MODE", "selftest")
    env = ci.load_env()
    assert env.folder == "gpclean-output/selftest"  # whatever folder was typed in
    assert env.photos_per_shard == 25 and env.fail_shard == 1
    monkeypatch.setenv("GPCLEAN_PASS", "2")
    assert ci.load_env().fail_shard is None  # only pass 1 fails on purpose
    # Without the knobs (as in the workflows) the selftest uses the code defaults.
    monkeypatch.delenv("GPCLEAN_PHOTOS_PER_SHARD")
    monkeypatch.delenv("GPCLEAN_SELFTEST_FAIL_SHARD")
    monkeypatch.setenv("GPCLEAN_PASS", "1")
    env = ci.load_env()
    assert env.photos_per_shard == ci.SELFTEST_PHOTOS_PER_SHARD
    assert env.fail_shard == ci.SELFTEST_FAIL_SHARD


def test_bad_input_fails_with_numbers_only(drive, monkeypatch, capfd):
    monkeypatch.setenv("GPCLEAN_FOLDER", "../../etc")
    assert ci.cli_plan() == ci.EXIT_FAIL
    out, err = capfd.readouterr()
    assert out == ""
    assert public_lines(err) == [f"FAILED phase={ci.PHASE_PLAN} exc=ValueError"]
    # The traceback went to the private log instead.
    assert "Traceback" in (drive.private / publiclog.PRIVATE_LOG_NAME).read_text(encoding="utf-8")


def test_quota_reset_hour():
    # Midnight Pacific is 07:00 UTC in summer (PDT) and 08:00 UTC in winter (PST).
    assert ci.quota_reset_hour_utc(datetime(2026, 7, 1, 12, tzinfo=UTC)) == 7
    assert ci.quota_reset_hour_utc(datetime(2026, 12, 1, 12, tzinfo=UTC)) == 8


# ----------------------------------------------------------------------- scope check


def test_scope_check_masks_every_secret_first(drive, monkeypatch, capfd):
    masked = []
    monkeypatch.setattr(publiclog, "add_mask", lambda s: masked.append(s))
    seen = []
    monkeypatch.setattr(ci, "tokeninfo_scopes", lambda token: seen.append(token) or set(SCOPES_OK))
    assert ci.cli_scope_check() == ci.EXIT_OK
    # The originals are masked before the refresh, the refreshed token before tokeninfo.
    assert masked[:3] == [CLIENT_SECRET, ACCESS_1, REFRESH]
    assert ACCESS_2 in masked
    assert seen == [ACCESS_2] and drive.fake.refreshes == 1
    out, err = capfd.readouterr()
    assert public_lines(err) == [f"OK phase={ci.PHASE_SCOPE} scope_ok=1"]
    private = (drive.private / publiclog.PRIVATE_LOG_NAME).read_text(encoding="utf-8")
    for secret in (ACCESS_1, ACCESS_2, REFRESH, CLIENT_SECRET):
        assert secret not in out + err + private


@pytest.mark.parametrize("scopes", [
    set(), {"https://www.googleapis.com/auth/drive.readonly"},
    SCOPES_OK | {"https://www.googleapis.com/auth/drive"},
])
def test_scope_check_fails_closed(drive, monkeypatch, capfd, scopes):
    monkeypatch.setattr(ci, "tokeninfo_scopes", lambda token: set(scopes))
    assert ci.cli_scope_check() == ci.EXIT_FAIL
    assert public_lines(capfd.readouterr().err) == [f"FAILED phase={ci.PHASE_SCOPE} scope_ok=0"]


def test_scope_check_without_config_fails(drive, monkeypatch, capfd):
    monkeypatch.setenv("RCLONE_CONFIG", str(drive.root / "missing.conf"))
    assert ci.cli_scope_check() == ci.EXIT_FAIL
    assert public_lines(capfd.readouterr().err) == [
        f"FAILED phase={ci.PHASE_SCOPE} exc=FileNotFoundError"]


def test_tokeninfo_posts_the_token_in_the_body(monkeypatch):
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length", "0"))
            seen.update(path=self.path, body=self.rfile.read(n).decode(),
                        ctype=self.headers.get("Content-Type"))
            body = json.dumps({"scope": " ".join(sorted(SCOPES_OK)), "expires_in": 3000})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body.encode())

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(ci, "TOKENINFO_URL", f"http://127.0.0.1:{httpd.server_address[1]}/ti")
        assert ci.tokeninfo_scopes(ACCESS_1) == SCOPES_OK
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert seen["path"] == "/ti" and ACCESS_1 not in seen["path"]
    assert urllib.parse.parse_qs(seen["body"]) == {"access_token": [ACCESS_1]}
    assert seen["ctype"] == "application/x-www-form-urlencoded"


def test_add_mask_only_on_the_real_console(monkeypatch, capfd):
    publiclog.add_mask("s3cr3t-value")  # fd 3 is not the console in tests: nothing at all
    out, err = capfd.readouterr()
    assert "s3cr3t" not in out + err
    for bad in ("", "a\nb", "a\rb"):
        with pytest.raises(ValueError):
            publiclog.add_mask(bad)


# ---------------------------------------------------------------------------- pending


def test_pending_counts_unfinished_shards(tmp_path, monkeypatch, capfd):
    """Pending needs only shards.json and the checkpoint listing (no scan module)."""
    outputs, _ = _base_env(monkeypatch, tmp_path, GPCLEAN_WORKERS="2")
    store = LocalStore(tmp_path / "out")
    monkeypatch.setattr(ci, "make_rclone", lambda env: None)
    monkeypatch.setattr(ci, "make_store", lambda env, rc: store)
    env = ci.load_env()
    keys = ["a" * 12, "b" * 12, "c" * 12]
    tbl = {"format": 1, "cfg": env.cfg, "zips": [
        {"zipkey": k, "name": f"z{i}.zip", "export_id": f"z{i}", "size": 10, "n_entries": 3,
         "present": i != 2}
        for i, k in enumerate(keys)], "shards": []}
    for key, n in ((keys[0], 3), (keys[1], 2), (keys[2], 2)):
        for shard in range(n):
            tbl["shards"].append({"id": len(tbl["shards"]), "zipkey": key, "shard": shard,
                                  "zip_name": "z.zip", "export_id": "z", "start": 0, "end": 1,
                                  "n_images": 1})
    ci.save_table(store, tbl)
    done = tmp_path / "done.sqlite"
    done.write_bytes(b"x")
    store.put(done, ci.meta_rel(env.cfg, keys[0], 1))
    store.put(done, ci.meta_rel(env.cfg, keys[1], 0))
    store.put(done, f"work/{env.cfg}/{keys[1]}/stray.txt")  # not a checkpoint

    assert ci.cli_pending() == ci.EXIT_OK
    out = dict(line.split("=", 1) for line in outputs.read_text().splitlines())
    # zip 2 is gone from the folder: its shards are ignored.
    assert out == {"count": "3", "of": "2", "workers": "[0,1]", "ids": "[0,2,4]"}
    assert public_lines(capfd.readouterr().err) == [
        f"COUNT phase={ci.PHASE_PENDING} pending=3 workers=2"]

    for shard_id in (0, 2, 4):
        s = tbl["shards"][shard_id]
        store.put(done, ci.meta_rel(env.cfg, s["zipkey"], s["shard"]))
    outputs.write_text("")
    assert ci.cli_pending() == ci.EXIT_OK
    out = dict(line.split("=", 1) for line in outputs.read_text().splitlines())
    assert out == {"count": "0", "of": "0", "workers": "[]", "ids": "[]"}


def test_pending_rejects_a_tampered_table(tmp_path, monkeypatch, capfd):
    _base_env(monkeypatch, tmp_path)
    store = LocalStore(tmp_path / "out")
    monkeypatch.setattr(ci, "make_rclone", lambda env: None)
    monkeypatch.setattr(ci, "make_store", lambda env, rc: store)
    env = ci.load_env()
    ci.save_table(store, {"format": 1, "cfg": env.cfg, "zips": [], "shards": [{"id": 5}]})
    assert ci.cli_pending() == ci.EXIT_FAIL
    assert "exc=ValueError" in capfd.readouterr().err


# ------------------------------------------------------------------------------- plan


def test_plan_builds_and_extends_the_shard_table(drive, capfd):
    pytest.importorskip("gpclean.scan")
    assert ci.cli_plan() == ci.EXIT_OK
    out = drive.outputs()
    assert out["n_zips"] == "3" and out["n_exports"] == "2"
    assert out["pending"] == out["n_shards"] == "3"  # one shard per small zip at 1000/shard
    env = ci.load_env()
    tbl = table(drive, env.cfg)
    assert [s["id"] for s in tbl["shards"]] == [0, 1, 2]
    # The whole tree exists before any fan-out, including one work folder per zip.
    out_root = drive.store.root
    for rel in (f"state/{env.cfg}", f"bundle/{env.cfg}/thumbs", f"logs/{env.run_tag}", "probe"):
        assert (out_root / rel).is_dir()
    for z in tbl["zips"]:
        assert (out_root / "work" / env.cfg / z["zipkey"]).is_dir()
    lines = public_lines(capfd.readouterr().err)
    assert any("exports_warning=true" in line for line in lines)
    assert not any(name in "\n".join(lines) for name in ZIP_NAMES)

    # Re-planning the same folder changes nothing (and reads no tails).
    serves = drive.fake.serves
    assert ci.cli_plan() == ci.EXIT_OK
    assert table(drive, env.cfg) == tbl and drive.fake.serves == serves

    # A new zip is appended; existing ids keep their meaning; a removed zip is ignored.
    shutil.copy2(drive.root / "Takeout" / ZIP_NAMES[2],
                 drive.root / "Takeout" / "takeout-20260301T000000Z-001.zip")
    (drive.root / "Takeout" / ZIP_NAMES[0]).unlink()
    (drive.root / "Takeout" / "notes.txt").write_text("not a zip", encoding="utf-8")
    drive.outputs()
    assert ci.cli_plan() == ci.EXIT_OK
    new = table(drive, env.cfg)
    assert new["shards"][:3] == tbl["shards"]
    assert len(new["shards"]) == 4 and new["shards"][3]["id"] == 3
    assert [z["present"] for z in new["zips"]] == [False, True, True, True]
    assert drive.outputs()["n_shards"] == "3"


# --------------------------------------------------------------- scan / finalize / merge


def _run_pass(drive: Drive, pass_no: int, monkeypatch) -> dict[str, str]:
    """pending -> every scan worker -> finalize, like one scan-pass.yml run."""
    monkeypatch.setenv("GPCLEAN_PASS", str(pass_no))
    monkeypatch.delenv("GPCLEAN_PENDING_IDS", raising=False)
    drive.outputs()
    assert ci.cli_pending() == ci.EXIT_OK
    pending = drive.outputs()
    codes = []
    if pending["count"] != "0":
        monkeypatch.setenv("GPCLEAN_PENDING_IDS", pending["ids"])
        for w in json.loads(pending["workers"]):
            monkeypatch.setenv("GPCLEAN_WORKER", str(w))
            codes.append(ci.cli_scan(worker=w, of=int(pending["of"])))
        monkeypatch.delenv("GPCLEAN_WORKER")
    assert ci.cli_finalize() == ci.EXIT_OK
    return {"pending": pending, "codes": codes, **drive.outputs()}


def test_selftest_forced_failure_then_pass2_completes(drive, monkeypatch, capfd):
    pytest.importorskip("gpclean.scan")
    pytest.importorskip("gpclean.merge.bundle")
    # No selftest knobs in the env: the workflows rely on the code defaults too.
    monkeypatch.setenv("GPCLEAN_MODE", "selftest")
    monkeypatch.setenv("GPCLEAN_WORKERS", "6")

    def check_console() -> None:
        """Everything printed so far is numbers only and names no zip."""
        text = "\n".join(public_lines(capfd.readouterr().err))
        assert not any(name in text for name in ZIP_NAMES)

    assert ci.cli_selftest_upload() == ci.EXIT_OK
    assert sorted(p.name for p in (drive.store.root / "selftest").iterdir()) == sorted(ZIP_NAMES)
    assert ci.cli_plan() == ci.EXIT_OK
    # Shards of 10: the 44- and 33-photo zips are cut into 4 and 3 shards, the 5-photo zip
    # is one, so the selftest covers shards that start mid-zip and several packs per zip.
    assert drive.outputs()["n_shards"] == "8"
    env = ci.load_env()
    tbl = table(drive, env.cfg)
    per_zip = {}
    for s in tbl["shards"]:
        per_zip.setdefault(s["zipkey"], []).append(s)
    assert sorted(len(v) for v in per_zip.values()) == [1, 3, 4]
    assert any(s["start"] > 0 for s in tbl["shards"])

    check_console()
    p1 = _run_pass(drive, 1, monkeypatch)
    check_console()
    assert p1["pending"]["workers"] == "[0,1,2,3,4,5]"
    assert p1["codes"] == [0, 1, 0, 0, 0, 0]  # worker 1 got the shard that fails on purpose
    assert (p1["done"], p1["quota"]) == ("false", "false")
    # The failed shard (the second one, mid-zip) left nothing behind; the others uploaded
    # pack and meta.
    failed = tbl["shards"][ci.SELFTEST_FAIL_SHARD]
    assert failed["shard"] == 1 and failed["start"] > 0
    assert not (drive.store.root / ci.meta_rel(env.cfg, failed["zipkey"], 1)).exists()
    assert len(ci.done_set(drive.store, env.cfg)) == 7

    p2 = _run_pass(drive, 2, monkeypatch)
    check_console()
    assert p2["pending"]["count"] == "1" and p2["codes"] == [0]
    assert (p2["done"], p2["quota"]) == ("true", "false")
    for s in tbl["shards"]:
        assert (drive.store.root / ci.pack_rel(env.cfg, s["zipkey"], s["shard"])).is_file()

    assert ci.cli_merge() == ci.EXIT_OK
    check_console()
    out = drive.outputs()
    assert out == {"partial": "false", "missing": "0"}
    bundle = drive.store.root / "bundle" / env.cfg
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["partial"] is False and manifest["cfg"] == env.cfg
    # get-bundle.ps1 relies on this marker to never download a selftest bundle.
    assert manifest["mode"] == "selftest" and "folder" not in manifest
    assert manifest["counts"]["items"] > 50
    packs = {f["path"] for f in manifest["files"] if f["path"].startswith("thumbs/")}
    assert packs == {f"thumbs/{ci.pack_name(s['zipkey'], s['shard'])}" for s in tbl["shards"]}
    for f in manifest["files"]:  # hashes recorded by the scan match the uploaded packs
        from gpclean.merge.bundle import sha256_file

        assert sha256_file(bundle / f["path"]) == f["sha256"]
    pass3 = _run_pass(drive, 3, monkeypatch)  # nothing left to do
    check_console()
    assert pass3["pending"]["count"] == "0" and pass3["done"] == "true"

    monkeypatch.setenv("GPCLEAN_PLAN_RESULT", "success")
    monkeypatch.setenv("GPCLEAN_MERGE_RESULT", "success")
    monkeypatch.setenv("GPCLEAN_PARTIAL", out["partial"])
    monkeypatch.setenv("GPCLEAN_MISSING", out["missing"])
    monkeypatch.setenv("GPCLEAN_QUOTA1", "false")
    capfd.readouterr()
    assert ci.cli_report() == ci.EXIT_OK
    err = capfd.readouterr().err
    assert public_lines(err)[-1] == f"OK phase={ci.PHASE_REPORT}"


def test_merge_of_a_partial_scan_is_marked_partial(drive, monkeypatch):
    pytest.importorskip("gpclean.scan")
    pytest.importorskip("gpclean.merge.bundle")
    assert ci.cli_merge() == ci.EXIT_FAIL  # no plan yet: no shards.json
    assert ci.cli_plan() == ci.EXIT_OK
    drive.outputs()
    assert ci.cli_merge() == ci.EXIT_OK  # nothing scanned yet
    assert drive.outputs() == {"partial": "true", "missing": "3"}

    monkeypatch.setenv("GPCLEAN_PASS", "1")
    monkeypatch.setenv("GPCLEAN_PENDING_IDS", "[2]")  # only the smallest zip
    assert ci.cli_scan(worker=0, of=1) == ci.EXIT_OK
    assert ci.cli_merge() == ci.EXIT_OK
    assert drive.outputs() == {"partial": "true", "missing": "2"}
    env = ci.load_env()
    manifest = json.loads((drive.store.root / "bundle" / env.cfg / "manifest.json")
                          .read_text(encoding="utf-8"))
    assert manifest["partial"] is True and manifest["missing_shards"] == 2
    assert manifest["mode"] == env.mode and manifest["folder"] == env.folder


def test_quota_stop_exits_3_and_finalize_reports_it(drive, monkeypatch, capfd):
    pytest.importorskip("gpclean.scan")
    assert ci.cli_plan() == ci.EXIT_OK
    drive.outputs()
    env = ci.load_env()
    monkeypatch.setenv("GPCLEAN_PASS", "1")
    monkeypatch.setenv("GPCLEAN_JOB", "scan")
    monkeypatch.setenv("GPCLEAN_WORKER", "0")
    drive.fake.serve_status = 403
    drive.fake.quota = True
    (drive.private / "run.log").write_text("private detail\n", encoding="utf-8")
    capfd.readouterr()

    assert ci.cli_scan(worker=0, of=1) == ci.EXIT_QUOTA
    lines = public_lines(capfd.readouterr().err)
    assert lines[-1] == f"FAILED phase={ci.PHASE_SCAN} quota=true"
    flags = [p.name for p in (drive.store.root / "state" / env.cfg).iterdir()
             if p.suffix == ".flag"]
    assert flags == [f"quota-{env.run_tag}-p1-w0.flag"]
    # Logs went up before the exit; the rclone config never does.
    logs = drive.store.root / "logs" / env.run_tag / "scan-p1-w0"
    assert (logs / "run.log").read_text(encoding="utf-8") == "private detail\n"
    assert not list(logs.rglob("*.conf"))

    assert ci.cli_finalize() == ci.EXIT_OK
    assert drive.outputs() == {"done": "false", "quota": "true"}
    monkeypatch.setenv("GPCLEAN_PASS", "2")  # another pass of the same run saw no quota
    assert ci.cli_finalize() == ci.EXIT_OK
    assert drive.outputs() == {"done": "false", "quota": "false"}

    monkeypatch.setenv("GPCLEAN_PLAN_RESULT", "success")
    monkeypatch.setenv("GPCLEAN_MERGE_RESULT", "success")
    monkeypatch.setenv("GPCLEAN_PARTIAL", "true")
    monkeypatch.setenv("GPCLEAN_MISSING", "3")
    monkeypatch.setenv("GPCLEAN_QUOTA1", "true")
    capfd.readouterr()
    assert ci.cli_report() == ci.EXIT_FAIL
    lines = public_lines(capfd.readouterr().err)
    assert any(re.fullmatch(r"COUNT phase=7 reset_hour_utc=(7|8)", line) for line in lines)


def test_scan_stops_taking_shards_after_the_budget(drive, monkeypatch):
    pytest.importorskip("gpclean.scan")
    assert ci.cli_plan() == ci.EXIT_OK
    monkeypatch.setenv("GPCLEAN_PASS", "1")
    monkeypatch.setenv("GPCLEAN_JOB_BUDGET_MIN", "1")
    clock = iter(range(0, 10**6, 100))  # every monotonic() call advances 100 s
    monkeypatch.setattr(ci, "_clock", lambda: float(next(clock)))
    assert ci.cli_scan(worker=0, of=1) == ci.EXIT_OK
    assert ci.done_set(drive.store, ci.load_env().cfg) == set()


def test_quota_before_the_scan_loop_still_reaches_finalize(drive, monkeypatch, capfd):
    """A quota hit while listing the checkpoints (before any shard) is a quota stop too."""
    pytest.importorskip("gpclean.scan")
    assert ci.cli_plan() == ci.EXIT_OK
    env = ci.load_env()
    monkeypatch.setenv("GPCLEAN_PASS", "1")
    monkeypatch.setenv("GPCLEAN_JOB", "scan")
    monkeypatch.setenv("GPCLEAN_WORKER", "0")
    (drive.private / "run.log").write_text("private detail\n", encoding="utf-8")
    capfd.readouterr()
    drive.outputs()

    def quota(*args, **kwargs):
        raise QuotaError("lsjson", 7)

    with monkeypatch.context() as m:
        m.setattr(ci, "done_set", quota)
        assert ci.cli_scan(worker=0, of=1) == ci.EXIT_QUOTA
    lines = public_lines(capfd.readouterr().err)
    assert lines[-2:] == [
        f"PHASE_DONE phase={ci.PHASE_SCAN} done=0 total=0 errors=0 quota=true",
        f"FAILED phase={ci.PHASE_SCAN} quota=true"]
    assert (drive.store.root / "state" / env.cfg / f"quota-{env.run_tag}-p1-w0.flag").is_file()
    assert (drive.store.root / "logs" / env.run_tag / "scan-p1-w0" / "run.log").is_file()
    drive.outputs()
    assert ci.cli_finalize() == ci.EXIT_OK
    assert drive.outputs() == {"done": "false", "quota": "true"}


def test_quota_in_the_plan_is_a_job_output(drive, capfd):
    pytest.importorskip("gpclean.scan")
    drive.fake.serve_status = 403  # the tail reads fail ...
    drive.fake.quota = True        # ... and the serve log says why
    assert ci.cli_plan() == ci.EXIT_QUOTA
    assert drive.outputs() == {"quota": "true"}
    assert public_lines(capfd.readouterr().err)[-1] == f"FAILED phase={ci.PHASE_PLAN} quota=true"


def test_quota_in_pending_reaches_finalize(drive, monkeypatch):
    pytest.importorskip("gpclean.scan")
    assert ci.cli_plan() == ci.EXIT_OK
    monkeypatch.setenv("GPCLEAN_PASS", "1")
    drive.outputs()
    with monkeypatch.context() as m:
        m.setattr(ci, "done_set", lambda store, cfg: (_ for _ in ()).throw(QuotaError("x", 7)))
        assert ci.cli_pending() == ci.EXIT_QUOTA
    pending = drive.outputs()
    assert pending == {"quota": "true"}
    # scan-pass.yml hands the pending job's quota output to finalize.
    monkeypatch.setenv("GPCLEAN_PENDING_QUOTA", pending["quota"])
    assert ci.cli_finalize() == ci.EXIT_OK
    assert drive.outputs() == {"done": "false", "quota": "true"}


def test_plan_skips_an_unreadable_zip_and_counts_it(drive, capfd):
    pytest.importorskip("gpclean.scan")
    broken = drive.root / "Takeout" / "zz-broken.zip"
    broken.write_bytes(b"this is not a zip file" * 10)
    assert ci.cli_plan() == ci.EXIT_OK
    out = drive.outputs()
    assert (out["n_zips"], out["n_shards"], out["bad_zips"]) == ("3", "3", "1")
    env = ci.load_env()
    assert {z["name"] for z in table(drive, env.cfg)["zips"]} == set(ZIP_NAMES)
    lines = public_lines(capfd.readouterr().err)
    assert f"COUNT phase={ci.PHASE_PLAN} bad_zips=1 unreadable=1 dup_names=0" in lines
    # The next plan tries it again; once it is gone, nothing is bad any more.
    broken.unlink()
    assert ci.cli_plan() == ci.EXIT_OK
    assert drive.outputs()["bad_zips"] == "0"


def test_plan_skips_zips_that_share_a_name(drive, monkeypatch):
    pytest.importorskip("gpclean.scan")
    real = drive.fake.lsjson

    def with_twin(path, **kwargs):
        # Drive allows a second file with the same name (another ID and size).
        out = real(path, **kwargs)
        if path.endswith(":Takeout"):
            twin = dict(out[0], ID="id-twin", Size=out[0]["Size"] + 1)
            out.append(twin)
        return out

    monkeypatch.setattr(drive.fake, "lsjson", with_twin)
    assert ci.cli_plan() == ci.EXIT_OK
    out = drive.outputs()
    assert (out["n_zips"], out["bad_zips"]) == ("2", "2")
    env = ci.load_env()
    assert ZIP_NAMES[0] not in {z["name"] for z in table(drive, env.cfg)["zips"]}


def test_split_duplicates():
    zips = [{"name": "a.zip"}, {"name": "b.zip"}, {"name": "a.zip"}, {"name": "c.zip"}]
    kept, n = ci.split_duplicates(zips)
    assert [z["name"] for z in kept] == ["b.zip", "c.zip"] and n == 2


def _hand_made_table(tmp_path, monkeypatch, n_shards: int):
    """shards.json with ``n_shards`` shards of one zip, on a LocalStore; no scan module."""

    class NoDrive:
        @contextlib.contextmanager
        def serve_http(self, path):
            yield "http://127.0.0.1:9"

        def serve_quota_hit(self):
            return False

    _base_env(monkeypatch, tmp_path)
    store = LocalStore(tmp_path / "out")
    monkeypatch.setattr(ci, "make_rclone", lambda env: NoDrive())
    monkeypatch.setattr(ci, "make_store", lambda env, rc: store)
    env = ci.load_env()
    key = "d" * 12
    tbl = {"format": 1, "cfg": env.cfg, "zips": [
        {"zipkey": key, "name": "z.zip", "export_id": "z", "size": 10, "n_entries": 5,
         "present": True}], "shards": [
        {"id": i, "zipkey": key, "shard": i, "zip_name": "z.zip", "export_id": "z",
         "start": i, "end": i + 1, "n_images": 1} for i in range(n_shards)]}
    ci.save_table(store, tbl)
    monkeypatch.setenv("GPCLEAN_PASS", "1")
    return store, env


def test_scan_stops_after_consecutive_failures(tmp_path, monkeypatch, capfd):
    _hand_made_table(tmp_path, monkeypatch, 5)
    calls = []

    def failing(env, cache, store, s, tmp, *, pool, fail, deadline):
        calls.append(s["id"])
        raise OSError("backend down")

    monkeypatch.setattr(ci, "_scan_one", failing)
    monkeypatch.setenv("GPCLEAN_PENDING_IDS", "[0,1,2,3,4]")
    assert ci.cli_scan(worker=0, of=1) == ci.EXIT_FAIL
    assert calls == [0, 1, 2] and ci.MAX_CONSECUTIVE_ERRORS == 3
    lines = public_lines(capfd.readouterr().err)
    assert lines[-1] == f"PHASE_DONE phase={ci.PHASE_SCAN} done=0 total=5 errors=3 quota=false"


@pytest.mark.parametrize("budget_min", ["", "1"])
def test_scan_passes_the_shard_deadline(tmp_path, monkeypatch, budget_min):
    """Each shard gets min(now + 45 min, hard stop) as its absolute deadline."""
    pytest.importorskip("gpclean.scan")
    import gpclean.scan

    _hand_made_table(tmp_path, monkeypatch, 2)
    monkeypatch.setenv("GPCLEAN_JOB_BUDGET_MIN", budget_min)
    now = 1000.0
    monkeypatch.setattr(ci, "_clock", lambda: now)
    monkeypatch.setattr(ci._ZipCache, "get", lambda self, key: (object(), [None] * 5))
    seen = []

    def recorder(opener, spec, entries, cfg, *, meta_path, pack_path, workers,
                 embedder_name, deadline=None, pool=None):
        seen.append(deadline)
        meta_path.write_bytes(b"m")
        pack_path.write_bytes(b"p")
        return {}

    monkeypatch.setattr(gpclean.scan, "scan_shard", recorder)
    assert ci.cli_scan(worker=0, of=1) == ci.EXIT_OK
    budget = int(budget_min or ci.DEFAULT_JOB_BUDGET_MIN)
    hard_stop = now + budget * 60 + ci.HARD_STOP_EXTRA_MIN * 60
    assert len(seen) == 2
    for deadline in seen:
        assert deadline <= now + ci.SHARD_TIMEOUT_S and deadline <= hard_stop
        assert deadline == min(now + ci.SHARD_TIMEOUT_S, hard_stop)


def test_scan_uses_one_pool_for_the_whole_job(tmp_path, monkeypatch):
    """Every shard of a job runs on the same ScanPool; the model is prepared once, up front."""
    pytest.importorskip("gpclean.scan")
    import gpclean.scan

    _hand_made_table(tmp_path, monkeypatch, 3)
    monkeypatch.setattr(ci._ZipCache, "get", lambda self, key: (object(), [None] * 5))
    events = []

    class FakePool:
        def __init__(self, workers, cfg, embedder_name):
            env = ci.load_env()
            assert (workers, cfg, embedder_name) == (env.scan_procs, env.scan_cfg,
                                                    env.clip_model)
            events.append("open")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            events.append("close")

        def prepare(self):
            events.append("prepare")

    pools = []

    def recorder(opener, spec, entries, cfg, *, meta_path, pack_path, workers,
                 embedder_name, deadline=None, pool=None):
        pools.append(pool)
        events.append("shard")
        meta_path.write_bytes(b"m")
        pack_path.write_bytes(b"p")
        return {}

    monkeypatch.setattr(gpclean.scan, "ScanPool", FakePool)
    monkeypatch.setattr(gpclean.scan, "scan_shard", recorder)
    assert ci.cli_scan(worker=0, of=1) == ci.EXIT_OK
    assert events == ["open", "prepare", "shard", "shard", "shard", "close"]
    assert len(pools) == 3 and isinstance(pools[0], FakePool)
    assert all(p is pools[0] for p in pools)


def test_scan_fails_before_any_shard_when_the_model_cannot_be_prepared(tmp_path, monkeypatch):
    pytest.importorskip("gpclean.scan")
    import gpclean.scan

    _hand_made_table(tmp_path, monkeypatch, 2)
    calls = []
    monkeypatch.setattr(ci, "_scan_one", lambda *a, **k: calls.append(a))

    def broken(self):
        raise RuntimeError("model hash mismatch")

    monkeypatch.setattr(gpclean.scan.ScanPool, "prepare", broken)
    assert ci.cli_scan(worker=0, of=1) == ci.EXIT_FAIL
    assert calls == []


def test_real_scan_checkpoint_records_the_pack_md5(drive, monkeypatch):
    """The checkpoint's pack_md5 is what Drive lists as md5Checksum for the uploaded pack."""
    pytest.importorskip("gpclean.scan")
    from gpclean.scan import read_shard_info

    assert ci.cli_plan() == ci.EXIT_OK
    env = ci.load_env()
    monkeypatch.setenv("GPCLEAN_PASS", "1")
    monkeypatch.setenv("GPCLEAN_PENDING_IDS", "[0]")
    assert ci.cli_scan(worker=0, of=1) == ci.EXIT_OK
    s = table(drive, env.cfg)["shards"][0]
    meta = drive.store.root / ci.meta_rel(env.cfg, s["zipkey"], s["shard"])
    pack = drive.store.root / ci.pack_rel(env.cfg, s["zipkey"], s["shard"])
    info = read_shard_info(meta)
    data = pack.read_bytes()
    assert info["pack_md5"] == hashlib.md5(data).hexdigest()
    # Drive without a sha256Checksum: md5 alone still catches a changed pack.
    listed = {"Size": len(data), "Hashes": {"md5": hashlib.md5(data).hexdigest()}}
    assert ci.pack_matches_listing(info, listed)
    changed = bytes([data[0] ^ 1]) + data[1:]
    listed["Hashes"]["md5"] = hashlib.md5(changed).hexdigest()
    assert not ci.pack_matches_listing(info, listed)


def test_merge_skips_a_bad_checkpoint_or_pack(drive, monkeypatch):
    pytest.importorskip("gpclean.scan")
    pytest.importorskip("gpclean.merge.bundle")
    assert ci.cli_plan() == ci.EXIT_OK
    env = ci.load_env()
    monkeypatch.setenv("GPCLEAN_PASS", "1")
    monkeypatch.setenv("GPCLEAN_PENDING_IDS", "[2]")
    assert ci.cli_scan(worker=0, of=1) == ci.EXIT_OK
    s = table(drive, env.cfg)["shards"][2]
    meta = drive.store.root / ci.meta_rel(env.cfg, s["zipkey"], s["shard"])
    pack = drive.store.root / ci.pack_rel(env.cfg, s["zipkey"], s["shard"])
    good_meta, good_pack = meta.read_bytes(), pack.read_bytes()
    drive.outputs()
    assert ci.cli_merge() == ci.EXIT_OK
    assert drive.outputs() == {"partial": "true", "missing": "2"}

    # An unreadable checkpoint: that shard counts as missing, the merge still publishes,
    # and the checkpoint becomes a 0-byte tombstone (test_a_rejected_checkpoint_is_rescanned
    # shows the next pass rescanning it). Writing the good bytes back stands in for that.
    meta.write_bytes(b"not a database")
    assert ci.cli_merge() == ci.EXIT_OK
    assert drive.outputs() == {"partial": "true", "missing": "3"}
    assert meta.stat().st_size == 0
    meta.write_bytes(good_meta)

    # A pack overwritten after its meta (same size, other bytes): the listed hash differs.
    pack.write_bytes(bytes([good_pack[0] ^ 1]) + good_pack[1:])
    assert ci.cli_merge() == ci.EXIT_OK
    assert drive.outputs() == {"partial": "true", "missing": "3"}
    assert meta.stat().st_size == 0
    assert ci.cli_merge() == ci.EXIT_OK  # a tombstone alone is simply not done
    assert drive.outputs() == {"partial": "true", "missing": "3"}
    pack.write_bytes(good_pack)
    meta.write_bytes(good_meta)
    assert ci.cli_merge() == ci.EXIT_OK
    assert drive.outputs() == {"partial": "true", "missing": "2"}


def _pending_ids_now(drive: Drive) -> list[int]:
    drive.outputs()
    assert ci.cli_pending() == ci.EXIT_OK
    return json.loads(drive.outputs()["ids"])


@pytest.mark.parametrize("damage", ["pack_bytes", "meta_bytes", "no_pack"])
def test_a_rejected_checkpoint_is_rescanned(drive, monkeypatch, capfd, damage):
    """The merge tombstones a checkpoint it cannot use; the next pass rescans that shard."""
    pytest.importorskip("gpclean.scan")
    pytest.importorskip("gpclean.merge.bundle")
    assert ci.cli_plan() == ci.EXIT_OK
    env = ci.load_env()
    assert _run_pass(drive, 1, monkeypatch)["done"] == "true"
    assert ci.cli_merge() == ci.EXIT_OK
    assert drive.outputs() == {"partial": "false", "missing": "0"}

    s = table(drive, env.cfg)["shards"][1]
    meta = drive.store.root / ci.meta_rel(env.cfg, s["zipkey"], s["shard"])
    pack = drive.store.root / ci.pack_rel(env.cfg, s["zipkey"], s["shard"])
    if damage == "pack_bytes":
        data = pack.read_bytes()
        pack.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    elif damage == "meta_bytes":
        meta.write_bytes(b"not a database")
    else:
        pack.unlink()
    capfd.readouterr()
    assert ci.cli_merge() == ci.EXIT_OK
    assert drive.outputs() == {"partial": "true", "missing": "1"}
    assert f"COUNT phase={ci.PHASE_MERGE} rejected=1" in public_lines(capfd.readouterr().err)
    # The checkpoint is now a 0-byte tombstone: pending again, but the file is still there
    # (the merge cannot delete anything on Drive).
    assert meta.is_file() and meta.stat().st_size == 0
    assert (s["zipkey"], s["shard"]) not in ci.done_set(drive.store, env.cfg)
    assert _pending_ids_now(drive) == [s["id"]]

    # The next pass rescans it (overwriting pack and tombstone); the merge is whole again.
    p2 = _run_pass(drive, 2, monkeypatch)
    assert p2["pending"]["count"] == "1" and p2["codes"] == [0] and p2["done"] == "true"
    assert meta.stat().st_size > 0 and pack.is_file()
    assert ci.cli_merge() == ci.EXIT_OK
    assert drive.outputs() == {"partial": "false", "missing": "0"}
    assert _pending_ids_now(drive) == []


def test_done_set_ignores_tombstones_and_other_files(tmp_path):
    store = LocalStore(tmp_path)
    cfg = "c" * 12
    good = tmp_path / ci.meta_rel(cfg, "a" * 12, 0)
    dead = tmp_path / ci.meta_rel(cfg, "a" * 12, 1)
    other = tmp_path / "work" / cfg / ("a" * 12) / "notes.txt"
    for path, data in ((good, b"x"), (dead, b""), (other, b"y")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    assert ci.done_set(store, cfg) == {("a" * 12, 0)}
    assert ci.list_sizes(store, f"work/{cfg}") == {
        f"{'a' * 12}/0000.meta.sqlite": 1, f"{'a' * 12}/0001.meta.sqlite": 0,
        f"{'a' * 12}/notes.txt": 1}
    assert ci.done_set(store, "d" * 12) == set()  # a missing folder lists as empty
    ci.tombstone(store, cfg, "a" * 12, 0)
    assert good.stat().st_size == 0 and ci.done_set(store, cfg) == set()


def test_pack_matches_listing():
    info = {"pack_size": "4", "pack_sha256": "AB", "pack_md5": "cd"}
    assert ci.pack_matches_listing(info, {"Size": 4, "Hashes": {"md5": "CD", "sha256": "x"}})
    assert not ci.pack_matches_listing(info, {"Size": 4, "Hashes": {"md5": "ce"}})
    assert not ci.pack_matches_listing(info, {"Size": 5, "Hashes": {"md5": "cd"}})
    no_md5 = {"pack_size": "4", "pack_sha256": "ab"}
    assert ci.pack_matches_listing(no_md5, {"Size": 4, "Hashes": {"md5": "zz", "sha256": "ab"}})
    assert not ci.pack_matches_listing(no_md5, {"Size": 4, "Hashes": {"sha256": "ac"}})
    # Drive without a sha256Checksum: only the size can be compared.
    assert ci.pack_matches_listing(no_md5, {"Size": 4, "Hashes": {"md5": "zz", "sha256": ""}})
    assert ci.pack_matches_listing(no_md5, {"Size": 4})


@pytest.mark.parametrize("env_extra,code", [
    ({"GPCLEAN_PLAN_RESULT": "success", "GPCLEAN_MERGE_RESULT": "success",
      "GPCLEAN_PARTIAL": "false", "GPCLEAN_MISSING": "0"}, 0),
    ({"GPCLEAN_PLAN_RESULT": "success", "GPCLEAN_MERGE_RESULT": "success",
      "GPCLEAN_PARTIAL": "true", "GPCLEAN_MISSING": "2"}, 1),
    ({"GPCLEAN_PLAN_RESULT": "failure", "GPCLEAN_MERGE_RESULT": "skipped"}, 1),
    ({"GPCLEAN_PLAN_RESULT": "success", "GPCLEAN_MERGE_RESULT": "failure"}, 1),
    ({"GPCLEAN_PLAN_RESULT": "success", "GPCLEAN_MERGE_RESULT": "success",
      "GPCLEAN_QUOTA2": "true"}, 1),
    ({"GPCLEAN_PLAN_RESULT": "success; echo", "GPCLEAN_MERGE_RESULT": "success"}, 1),
    # The plan skipped an unreadable zip: the bundle is complete but lacks that zip.
    ({"GPCLEAN_PLAN_RESULT": "success", "GPCLEAN_MERGE_RESULT": "success",
      "GPCLEAN_PARTIAL": "false", "GPCLEAN_BAD_ZIPS": "1"}, 1),
    ({"GPCLEAN_PLAN_RESULT": "success", "GPCLEAN_MERGE_RESULT": "success",
      "GPCLEAN_BAD_ZIPS": "1;x"}, 1),
])
def test_report(tmp_path, monkeypatch, capfd, env_extra, code):
    _base_env(monkeypatch, tmp_path, **env_extra)
    assert ci.cli_report() == code
    lines = public_lines(capfd.readouterr().err)
    quota = any(v == "true" for k, v in env_extra.items() if k.startswith("GPCLEAN_QUOTA"))
    assert any("reset_hour_utc=" in line for line in lines) == quota


def test_report_shows_the_reset_hour_for_a_quota_stop_in_the_plan(tmp_path, monkeypatch, capfd):
    # The plan hit the quota: no pass ran, so only the plan's output says so.
    _base_env(monkeypatch, tmp_path, GPCLEAN_PLAN_RESULT="failure",
              GPCLEAN_MERGE_RESULT="skipped", GPCLEAN_QUOTA_PLAN="true")
    assert ci.cli_report() == ci.EXIT_FAIL
    lines = public_lines(capfd.readouterr().err)
    assert any(re.fullmatch(r"COUNT phase=7 reset_hour_utc=(7|8)", line) for line in lines)
    assert lines[-1] == f"FAILED phase={ci.PHASE_REPORT} partial=false quota=true"


def test_upload_logs_skips_the_config_and_never_fails(drive, monkeypatch, capfd):
    env = ci.load_env()
    (drive.private / "run.log").write_text("x\n", encoding="utf-8")
    (drive.private / "sub").mkdir()
    (drive.private / "sub" / "rclone-serve.log").write_text("y\n", encoding="utf-8")
    shutil.copy(env.rclone_config, drive.private / "copy.conf")
    monkeypatch.setenv("RCLONE_CONFIG", str(drive.private / "sub" / "live-config"))
    shutil.copy(env.rclone_config, drive.private / "sub" / "live-config")
    assert ci.cli_upload_logs() == ci.EXIT_OK
    up = drive.store.root / "logs" / env.run_tag / "test"
    got = sorted(p.relative_to(up).as_posix() for p in up.rglob("*") if p.is_file())
    assert "run.log" in got and "sub/rclone-serve.log" in got
    assert not any(g.endswith((".conf", "live-config")) for g in got)
    assert capfd.readouterr().err == ""  # output suppressed

    monkeypatch.setattr(ci, "make_store", lambda env, rc: 1 / 0)
    assert ci.cli_upload_logs() == ci.EXIT_OK


def test_selftest_upload_refuses_other_modes(drive, capfd):
    assert ci.cli_selftest_upload() == ci.EXIT_FAIL
    assert not (drive.store.root / "selftest").exists()


# ----------------------------------------------------------- public console (real fd 3)


def child_main(root: str, config: str) -> None:
    """Run in a child process whose fd 3 is a pipe: scope check, plan, pending."""
    install_fakes(Path(root), Path(config))
    ci.tokeninfo_scopes = lambda token: set(SCOPES_OK)
    codes = [ci.cli_scope_check(), ci.cli_plan(), ci.cli_pending()]
    os.environ["GPCLEAN_FOLDER"] = "bad/../x"
    codes.append(ci.cli_plan())
    sys.stdout.write("codes=" + json.dumps(codes) + "\n")


@pytest.mark.skipif(os.name == "nt", reason="fd 3 routing is POSIX-only")
def test_public_console_gets_numbers_and_masks_only(drive):
    pytest.importorskip("gpclean.scan")
    read_fd, write_fd = os.pipe()
    code = (f"import os, sys; os.dup2({write_fd}, 3); os.close({write_fd}); "
            f"sys.path.insert(0, {str(TESTS_DIR)!r}); import test_ci; "
            f"test_ci.child_main({str(drive.root)!r}, {os.environ['RCLONE_CONFIG']!r})")
    env = dict(os.environ, GPCLEAN_PUBLIC_FD="3")
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, pass_fds=(write_fd,))
    os.close(write_fd)
    with os.fdopen(read_fd, "rb") as fh:
        fd3 = fh.read().decode()
    out, err = proc.communicate(timeout=120)
    assert proc.returncode == 0, err.decode()[-2000:]
    assert "codes=[0, 0, 0, 1]" in out.decode()
    masks = [line for line in fd3.splitlines() if line.startswith("::add-mask::")]
    assert {m.split("::", 2)[2] for m in masks} == {CLIENT_SECRET, ACCESS_1, REFRESH, ACCESS_2}
    rest = "\n".join(line for line in fd3.splitlines() if not line.startswith("::add-mask::"))
    lines = public_lines(rest)
    assert lines[0] == f"OK phase={ci.PHASE_SCOPE} scope_ok=1"
    assert lines[-1] == f"FAILED phase={ci.PHASE_PLAN} exc=ValueError"
    for name in ZIP_NAMES + ("Takeout", "Google Photos", "Photos from"):
        assert name not in fd3
    # Nothing public leaked to stderr, and no secret reached the private log.
    assert err.decode() == ""
    private = (drive.private / publiclog.PRIVATE_LOG_NAME).read_text(encoding="utf-8")
    for secret in (ACCESS_1, ACCESS_2, REFRESH, CLIENT_SECRET):
        assert secret not in private


# ------------------------------------------------------------------------------ probe


def test_probe_publishes_aggregates_and_stores_details(drive, monkeypatch, capfd):
    pytest.importorskip("gpclean.scan")
    from gpclean import probe

    monkeypatch.setenv("GPCLEAN_MODE", "probe")
    monkeypatch.setattr(probe, "N_READ", 20)
    monkeypatch.setattr(probe, "N_PROCESS", 20)
    assert probe.cli_probe() == ci.EXIT_OK
    lines = public_lines(capfd.readouterr().err)
    text = "\n".join(lines)
    for key in ("scope_ok=true", "n_zips=3", "n_exports=2", "rw_ok=true", "seq_mb_s=",
                "rnd_photos_s=", "proc_photos_s=", "comp_", "m_image=", "sc_url=", "pair_"):
        assert key in text, key
    assert lines[-1] == f"PHASE_DONE phase={ci.PHASE_PROBE} failed_sections=0"
    env = ci.load_env()
    base = drive.store.root / "probe" / str(env.run_id)
    details = json.loads((base / "probe.json").read_text(encoding="utf-8"))
    assert {z["name"] for z in details["zips"]} == set(ZIP_NAMES)  # names stay private
    assert all(isinstance(v, (int, float)) for v in details["sections"].values())
    urls = (base / "urls-sample.txt").read_text(encoding="utf-8").splitlines()
    assert 0 < len(urls) <= probe.N_URLS
    assert all(u.startswith("https://photos.google.com/photo/AF1QipFAKE") for u in urls)


def test_probe_stops_when_the_scope_is_wrong(drive, monkeypatch, capfd):
    from gpclean import probe

    monkeypatch.setattr(ci, "tokeninfo_scopes", lambda token: {"x"})
    assert probe.cli_probe() == ci.EXIT_FAIL
    assert public_lines(capfd.readouterr().err) == [f"FAILED phase={ci.PHASE_PROBE} scope_ok=0"]
    assert not (drive.store.root / "probe").exists()


# --------------------------------------------------------------------- real rclone binary


@pytest.mark.rclone
def test_real_rclone_plan_scan_merge(tmp_path, monkeypatch, fx):
    """The same chain through the real wrapper and binary, on an alias remote to a folder.

    This checks what the fakes cannot: real ``lsjson`` output, ``serve http`` URLs, and
    RcloneStore uploads/downloads/listing.
    """
    pytest.importorskip("gpclean.scan")
    pytest.importorskip("gpclean.merge.bundle")
    binary = os.environ.get("GPCLEAN_RCLONE") or shutil.which("rc" + "lone")
    if not binary:
        pytest.skip("no rclone binary (set GPCLEAN_RCLONE or put it on PATH)")
    monkeypatch.setenv("GPCLEAN_RCLONE", binary)
    root = tmp_path / "drive"
    (root / "Takeout").mkdir(parents=True)
    for name in ZIP_NAMES:
        shutil.copy2(fx / name, root / "Takeout" / name)
    outputs, _private = _base_env(monkeypatch, tmp_path)
    config = Path(os.environ["RCLONE_CONFIG"])
    config.write_text(f"[gp]\ntype = alias\nremote = {root.as_posix()}\n", encoding="utf-8")

    def out() -> dict:
        text = outputs.read_text(encoding="utf-8")
        outputs.write_text("", encoding="utf-8")
        return dict(line.split("=", 1) for line in text.splitlines() if line)

    assert ci.cli_plan() == ci.EXIT_OK
    assert out() == {"n_zips": "3", "n_shards": "3", "n_exports": "2", "pending": "3",
                     "bad_zips": "0"}
    monkeypatch.setenv("GPCLEAN_PASS", "1")
    assert ci.cli_pending() == ci.EXIT_OK
    assert out()["ids"] == "[0,1,2]"
    monkeypatch.setenv("GPCLEAN_PENDING_IDS", "[2]")  # the smallest zip keeps this quick
    assert ci.cli_scan(worker=0, of=1) == ci.EXIT_OK
    assert ci.cli_finalize() == ci.EXIT_OK
    assert out() == {"done": "false", "quota": "false"}
    assert ci.cli_merge() == ci.EXIT_OK
    assert out() == {"partial": "true", "missing": "2"}
    cfg = ci.load_env().cfg
    bundle = root / "gpclean-output" / "bundle" / cfg
    assert {p.name for p in bundle.iterdir()} >= {"index.sqlite", "embeddings.f16.npy",
                                                  "manifest.json", "thumbs"}

    # A pack changed on Drive: the merge tombstones its meta through RcloneStore, and the
    # real lsjson sizes make the shard pending again.
    s = json.loads((root / "gpclean-output" / ci.shards_rel(cfg)).read_text(encoding="utf-8")
                   )["shards"][2]
    pack = root / "gpclean-output" / ci.pack_rel(cfg, s["zipkey"], s["shard"])
    data = pack.read_bytes()
    pack.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    assert ci.cli_merge() == ci.EXIT_OK
    assert out() == {"partial": "true", "missing": "3"}
    meta = root / "gpclean-output" / ci.meta_rel(cfg, s["zipkey"], s["shard"])
    assert meta.is_file() and meta.stat().st_size == 0
    monkeypatch.delenv("GPCLEAN_PENDING_IDS")
    assert ci.cli_pending() == ci.EXIT_OK
    assert out()["ids"] == "[0,1,2]"
