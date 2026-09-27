"""Tests for gpclean.publiclog: the numbers-only public console."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import textwrap
import warnings
from pathlib import Path

import pytest

from gpclean import publiclog
from gpclean.publiclog import Ev, event, github_output

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="fd 3 routing is POSIX-only")
SRC_DIR = str(Path(publiclog.__file__).resolve().parents[1])
SECRET = "IMG_20190704_family-beach.jpg"  # stands in for personal data in messages


@pytest.fixture(autouse=True)
def _no_public_fd(monkeypatch):
    monkeypatch.delenv("GPCLEAN_PUBLIC_FD", raising=False)
    monkeypatch.delenv("GPCLEAN_PRIVATE_DIR", raising=False)
    yield


@pytest.fixture
def restore_logging():
    """Undo setup_logging's changes to the root logger after a test."""
    root = logging.getLogger()
    old_level = root.level
    yield
    for handler in list(publiclog._our_handlers):
        root.removeHandler(handler)
        handler.close()
    publiclog._our_handlers.clear()
    root.setLevel(old_level)
    logging.captureWarnings(False)


# ----- event --------------------------------------------------------------------------------

def test_event_formats_numbers(capsys):
    event(Ev.PROGRESS, phase=2, done=10, total=100, rate=1.5, partial=False, ok=True)
    err = capsys.readouterr().err
    assert err == "PROGRESS phase=2 done=10 total=100 rate=1.5 partial=false ok=true\n"


def test_event_without_values(capsys):
    event(Ev.OK)
    assert capsys.readouterr().err == "OK\n"


@pytest.mark.parametrize("bad", [SECRET, "", b"x", None, [1], {"a": 1}, 1j, Ev.OK])
def test_event_rejects_non_numbers(bad, capsys):
    with pytest.raises(TypeError):
        event(Ev.COUNT, value=bad)
    assert capsys.readouterr().err == ""  # nothing partial was written


def test_event_rejects_int_subclasses(capsys):
    import enum

    class Color(enum.IntEnum):
        RED = 1

    with pytest.raises(TypeError):
        event(Ev.COUNT, n=Color.RED)


@pytest.mark.parametrize("key", ["Bad", "with space", "a" * 40, "x=y", "näme", "_x"])
def test_event_rejects_bad_keys(key):
    with pytest.raises(ValueError):
        event(Ev.COUNT, **{key: 1})


def test_event_requires_enum():
    with pytest.raises(TypeError):
        event("PROGRESS", n=1)  # type: ignore[arg-type]


def test_fd3_ignored_without_env(capsys):
    # Without GPCLEAN_PUBLIC_FD=3 everything goes to stderr even if fd 3 exists.
    event(Ev.CHECKPOINT, shard=3)
    assert capsys.readouterr().err == "CHECKPOINT shard=3\n"


# ----- subprocess helpers -------------------------------------------------------------------

def _run_child(code: str, *, fd3: str | None, tmp_path: Path, env_extra: dict | None = None,
               as_file: bool = False):
    """Run ``code`` in a child Python with fd 3 set to a pipe ("pipe"), a regular file
    ("file") or left alone (None). Returns (fd3 text, stdout, stderr, returncode).

    ``as_file`` runs the code from a script file instead of ``-c`` (multiprocessing's spawn
    start method has to re-import the main module)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GPCLEAN_")}
    env["PYTHONPATH"] = SRC_DIR + os.pathsep + env.get("PYTHONPATH", "")
    env.update(env_extra or {})
    prelude = ""
    pass_fds: tuple[int, ...] = ()
    read_fd = None
    fd3_file = tmp_path / "fd3.txt"
    if fd3 == "pipe":
        read_fd, write_fd = os.pipe()
        pass_fds = (write_fd,)
        setup = f"os.dup2({write_fd}, 3); os.close({write_fd})"
    elif fd3 == "file":
        setup = f"os.dup2(os.open({str(fd3_file)!r}, os.O_WRONLY | os.O_CREAT), 3)"
    if fd3 is not None:
        # Guarded, because a spawned multiprocessing child re-imports the main module.
        prelude = f"import os\nif __name__ == '__main__':\n    {setup}\n"
    program = prelude + textwrap.dedent(code)
    if as_file:
        script = tmp_path / "child_main.py"
        script.write_text(program, encoding="utf-8")
        argv = [sys.executable, str(script)]
    else:
        argv = [sys.executable, "-c", program]
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, pass_fds=pass_fds,
    )
    fd3_text = ""
    if read_fd is not None:
        os.close(pass_fds[0])
        with os.fdopen(read_fd, "rb") as fh:
            fd3_text = fh.read().decode()
    out, err = proc.communicate(timeout=60)
    if fd3 == "file" and fd3_file.exists():
        fd3_text = fd3_file.read_text()
    return fd3_text, out.decode(), err.decode(), proc.returncode


# ----- fd 3 routing -------------------------------------------------------------------------

@POSIX_ONLY
def test_fd3_routing(tmp_path):
    code = """
        from gpclean.publiclog import Ev, event
        event(Ev.PHASE_START, phase=1)
        event(Ev.PHASE_DONE, phase=1, n=5)
    """
    fd3, out, err, rc = _run_child(code, fd3="pipe", tmp_path=tmp_path,
                                   env_extra={"GPCLEAN_PUBLIC_FD": "3"})
    assert rc == 0
    assert fd3 == "PHASE_START phase=1\nPHASE_DONE phase=1 n=5\n"
    assert out == "" and err == ""


@POSIX_ONLY
def test_fd3_regular_file_is_not_trusted(tmp_path):
    # A child that inherited GPCLEAN_PUBLIC_FD=3 but has some unrelated file on fd 3 must not
    # write into that file.
    code = """
        from gpclean.publiclog import Ev, event
        event(Ev.COUNT, n=1)
    """
    fd3, _out, err, rc = _run_child(code, fd3="file", tmp_path=tmp_path,
                                    env_extra={"GPCLEAN_PUBLIC_FD": "3"})
    assert rc == 0
    assert fd3 == ""
    assert err == "COUNT n=1\n"


@POSIX_ONLY
def test_disable_public_for_workers(tmp_path):
    code = """
        from gpclean import publiclog
        publiclog.disable_public()
        publiclog.event(publiclog.Ev.COUNT, n=2)
    """
    fd3, _out, err, rc = _run_child(code, fd3="pipe", tmp_path=tmp_path,
                                    env_extra={"GPCLEAN_PUBLIC_FD": "3"})
    assert rc == 0 and fd3 == "" and err == "COUNT n=2\n"


@POSIX_ONLY
def test_decision_is_made_once_and_hidden_from_children(tmp_path):
    # The parent owns the console. A spawned worker must not inherit GPCLEAN_PUBLIC_FD, so
    # even when fd 3 in the worker is some pipe of its own, its event goes to stderr.
    code = """
        import os, sys
        import multiprocessing as mp
        from gpclean import publiclog

        def worker():
            from gpclean import publiclog as pl
            assert "GPCLEAN_PUBLIC_FD" not in os.environ
            r, w = os.pipe()
            try:
                saved = os.dup(3)  # fd 3 may belong to multiprocessing itself
            except OSError:
                saved = None
            os.dup2(w, 3)
            try:
                pl.event(pl.Ev.COUNT, worker=1)
            finally:
                if saved is not None:
                    os.dup2(saved, 3)
                    os.close(saved)
                else:
                    os.close(3)
            os.close(w)
            os.set_blocking(r, False)
            try:
                leaked = os.read(r, 100)
            except BlockingIOError:
                leaked = b""
            sys.exit(0 if leaked == b"" else 5)

        if __name__ == "__main__":
            assert "GPCLEAN_PUBLIC_FD" not in os.environ  # popped at import
            publiclog.event(publiclog.Ev.COUNT, parent=1)
            p = mp.get_context("spawn").Process(target=worker)
            p.start()
            p.join(60)
            sys.exit(p.exitcode)
    """
    fd3, out, err, rc = _run_child(code, fd3="pipe", tmp_path=tmp_path, as_file=True,
                                   env_extra={"GPCLEAN_PUBLIC_FD": "3"})
    assert rc == 0, err
    assert fd3 == "COUNT parent=1\n"
    assert err == "COUNT worker=1\n"


@POSIX_ONLY
def test_fd3_opened_after_import_is_not_used(tmp_path):
    # A CI step that sets GPCLEAN_PUBLIC_FD=3 but forgets "3>&1": fd 3 is closed at import,
    # and a pipe the process opens later lands on fd 3. It must not receive public lines.
    code = """
        import os
        from gpclean.publiclog import Ev, event
        r, w = os.pipe()
        if r == 3:
            r = os.dup(r)  # keep the read end off fd 3; dup2 below replaces fd 3
        if w != 3:
            os.dup2(w, 3)
            os.close(w)
        event(Ev.COUNT, n=4)
        os.close(3)
        os.set_blocking(r, False)
        try:
            leaked = os.read(r, 100)
        except BlockingIOError:
            leaked = b""
        assert leaked == b"", leaked
    """
    _fd3, _out, err, rc = _run_child(code, fd3=None, tmp_path=tmp_path,
                                     env_extra={"GPCLEAN_PUBLIC_FD": "3"})
    assert rc == 0, err
    assert err == "COUNT n=4\n"


# ----- excepthook ---------------------------------------------------------------------------

def _crash_code(private: Path) -> str:
    """A child program that sets up logging and then dies with personal data in the message."""
    return f"""
        from pathlib import Path
        from gpclean import publiclog
        publiclog.setup_logging(Path({str(private)!r}))
        publiclog.install_excepthook(phase=4)
        def load(name):
            raise ValueError("cannot decode " + name)
        load({SECRET!r})
    """


@POSIX_ONLY
def test_excepthook_fd3_shows_only_type(tmp_path):
    private = tmp_path / "private"
    fd3, out, err, rc = _run_child(_crash_code(private), fd3="pipe", tmp_path=tmp_path,
                                   env_extra={"GPCLEAN_PUBLIC_FD": "3"})
    assert rc == 1
    assert fd3 == "FAILED phase=4 exc=ValueError\n"
    assert SECRET not in fd3 and "cannot decode" not in fd3
    assert out == "" and err == ""
    log = (private / publiclog.PRIVATE_LOG_NAME).read_text(encoding="utf-8")
    assert SECRET in log and "Traceback" in log


def test_excepthook_stderr_fallback(tmp_path):
    # Windows and local runs: no fd 3, public line on stderr, details only in the private log.
    private = tmp_path / "private"
    _fd3, out, err, rc = _run_child(_crash_code(private), fd3=None, tmp_path=tmp_path)
    assert rc == 1
    assert err.strip() == "FAILED phase=4 exc=ValueError"
    assert SECRET not in err + out
    assert SECRET in (private / publiclog.PRIVATE_LOG_NAME).read_text(encoding="utf-8")


def test_excepthook_in_thread(tmp_path):
    private = tmp_path / "private"
    code = f"""
        import threading
        from pathlib import Path
        from gpclean import publiclog
        publiclog.setup_logging(Path({str(private)!r}))
        publiclog.install_excepthook(phase=7)
        def work():
            raise KeyError({SECRET!r})
        t = threading.Thread(target=work)
        t.start(); t.join()
    """
    _fd3, out, err, rc = _run_child(code, fd3=None, tmp_path=tmp_path)
    assert rc == 0
    assert err.strip() == "FAILED phase=7 exc=KeyError"
    assert SECRET not in err + out


def test_safe_type_name():
    weird = type("Bad Name!", (Exception,), {})
    assert publiclog._safe_type_name(weird) == "Exception"
    assert publiclog._safe_type_name(OSError) == "OSError"


def test_set_phase_requires_int():
    with pytest.raises(TypeError):
        publiclog.set_phase("3")  # type: ignore[arg-type]


# ----- setup_logging ------------------------------------------------------------------------

def test_setup_logging_to_private_file(tmp_path, restore_logging):
    path = publiclog.setup_logging(tmp_path / "priv")
    assert path == tmp_path / "priv" / publiclog.PRIVATE_LOG_NAME
    logging.getLogger("gpclean.test").info("member %s", SECRET)
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.warn("careful: " + SECRET)
    text = path.read_text(encoding="utf-8")
    assert text.count(SECRET) == 2  # the log line and the captured warning


def test_setup_logging_env_and_idempotent(tmp_path, monkeypatch, restore_logging):
    monkeypatch.setenv("GPCLEAN_PRIVATE_DIR", str(tmp_path / "envpriv"))
    first = publiclog.setup_logging(None)
    second = publiclog.setup_logging(None)
    assert first == second == tmp_path / "envpriv" / publiclog.PRIVATE_LOG_NAME
    assert len(publiclog._our_handlers) == 1
    logging.getLogger("x").warning("once")
    assert first.read_text(encoding="utf-8").count("once") == 1


# ----- github_output ------------------------------------------------------------------------

def test_github_output_writes(tmp_path, monkeypatch):
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    github_output(count=3, done=True, quota=False, workers="[0,1,2]", ids=[4, 5])
    assert out.read_bytes() == b"count=3\ndone=true\nquota=false\nworkers=[0,1,2]\nids=[4,5]\n"


@pytest.mark.parametrize("bad", [
    "a\nb", "a\rb", "x y", "done=true", "a<<EOF", "café", "١", "a" * 4001, "'q'",
    "ok\n",
])
def test_github_output_rejects_bad_strings(bad, tmp_path, monkeypatch):
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    with pytest.raises(ValueError):
        github_output(good=1, value=bad)
    assert not out.exists()  # validated before anything is written


@pytest.mark.parametrize("bad", [1.5, None, b"1", ["a"], [True, 1.0]])
def test_github_output_rejects_bad_types(bad, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "o"))
    with pytest.raises(TypeError):
        github_output(value=bad)


@pytest.mark.parametrize("key", ["bad key", "a\nb", "", "1x", "x=y"])
def test_github_output_rejects_bad_keys(key, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "o"))
    with pytest.raises(ValueError):
        github_output(**{key: 1})


def test_github_output_noop_without_env(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    github_output(count=1)  # must not raise or write anywhere
    assert list(tmp_path.iterdir()) == []
