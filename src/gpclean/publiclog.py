"""Numbers-only public console, private logging, and the redacting top-level handler.

The GitHub Actions console of this public repo is world-readable, so it must never show a
filename, date, path, or exception message. This module is the ONLY code allowed to write to
it, and it only accepts numbers:

* :func:`event` writes one line ``<EVENT> key=value ...`` whose values must be int/float/bool.
* :func:`install_excepthook` replaces the default traceback printer with a single line
  ``FAILED phase=<n> exc=<Type>``; the full traceback goes to the private log.
* :func:`github_output` appends strictly validated values to ``$GITHUB_OUTPUT``.

Where "public" is: CI steps run ``python -m gpclean ... 3>&1 1>>private/run.log 2>&1``, so fd 3
is the console and stdout/stderr are private. Public lines go to fd 3 only when
``GPCLEAN_PUBLIC_FD=3`` is set *and* fd 3 is an open pipe/terminal; otherwise (locally, on
Windows, in tests) they go to stderr.

That decision is made ONCE, when this module is first imported (import it early, before the
process opens other files), and ``GPCLEAN_PUBLIC_FD`` is then removed from ``os.environ``.
Later in the life of a process, and in any worker it spawns, fd 3 can be an unrelated pipe
(a subprocess PIPE, a multiprocessing queue) that we must never write into; children
therefore never see the variable and fall back to stderr, which is private in CI.
"""

from __future__ import annotations

import enum
import json
import logging
import os
import re
import stat
import sys
import threading
import traceback
from pathlib import Path
from types import TracebackType

_log = logging.getLogger(__name__)

PUBLIC_FD = 3
PRIVATE_LOG_NAME = "gpclean.log"

# Event keys are chosen by code, but validate them anyway so a key can never smuggle text.
_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,31}", re.ASCII)
# GITHUB_OUTPUT values: integers, booleans and JSON integer lists only (see INTERFACES.md).
# re.ASCII so \w cannot match non-ASCII letters; fullmatch so "$" cannot accept a trailing "\n".
_OUTPUT_VALUE_RE = re.compile(r"[\w\[\],.:-]{0,4000}", re.ASCII)
_OUTPUT_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", re.ASCII)
# Exception class names are code-defined, but sanitise before printing them publicly.
_TYPE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}", re.ASCII)

_phase = 0
_public_disabled = False
_PUBLIC_ENV = "GPCLEAN_PUBLIC_FD"
_our_handlers: list[logging.Handler] = []


class Ev(enum.Enum):
    """The only kinds of line the public console may show."""

    PHASE_START = "PHASE_START"
    PROGRESS = "PROGRESS"
    PHASE_DONE = "PHASE_DONE"
    CHECKPOINT = "CHECKPOINT"
    COUNT = "COUNT"
    FAILED = "FAILED"
    OK = "OK"


def _fd3_is_console() -> bool:
    """True when fd 3 was handed to us as the public console.

    Besides the env flag we require fd 3 to be a pipe or character device. That guards against
    a child process inheriting ``GPCLEAN_PUBLIC_FD=3`` while fd 3 happens to be some unrelated
    file it opened (for example a SQLite database), which we must never write into.
    Only called at import time (see the module docstring).
    """
    if os.environ.get(_PUBLIC_ENV, "") != str(PUBLIC_FD):
        return False
    try:
        mode = os.fstat(PUBLIC_FD).st_mode
    except OSError:
        return False
    return stat.S_ISFIFO(mode) or stat.S_ISCHR(mode)


# Decided once, at import, in the process that was handed the console; then hidden from
# every child process (spawned workers, subprocesses) so they can never pick up fd 3.
_PUBLIC_OK = _fd3_is_console()
os.environ.pop(_PUBLIC_ENV, None)


def _write_public(line: str) -> None:
    """Write one already-validated line to the public console."""
    data = (line + "\n").encode("ascii", errors="replace")
    if _PUBLIC_OK and not _public_disabled:
        try:
            os.write(PUBLIC_FD, data)
            return
        except OSError:
            pass  # console went away; fall back to stderr (private in CI)
    try:
        sys.stderr.write(data.decode("ascii"))
        sys.stderr.flush()
    except (OSError, ValueError, AttributeError):
        pass  # stderr closed/None (e.g. pythonw); nothing sensible left to do


def add_mask(secret: str) -> None:
    """Ask the Actions runner to mask ``secret`` in all later console output of this job.

    Writes ``::add-mask::<secret>`` to the public console, and ONLY there: when fd 3 is not
    the console (locally, in tests, in workers) nothing is written at all, because the
    stderr fallback would put the secret into the private log. The value is never logged.
    A value containing a line break (or other control character) is refused: it could not
    be masked as a whole and would end the workflow command early.
    """
    if not isinstance(secret, str) or not secret:
        raise ValueError("add_mask needs a non-empty string")
    if any(ord(ch) < 32 or ch == "\x7f" for ch in secret):
        raise ValueError("add_mask value contains control characters")
    if not _PUBLIC_OK or _public_disabled:
        return
    try:
        os.write(PUBLIC_FD, ("::add-mask::" + secret + "\n").encode("utf-8"))
    except OSError:
        pass  # console gone: nothing more is printed publicly either


def disable_public() -> None:
    """Stop this process from ever writing to fd 3 (call it in worker-process initialisers).

    Workers must not touch the console; their public lines, if any, fall back to stderr.
    Spawned workers already cannot see ``GPCLEAN_PUBLIC_FD``; this is belt and braces (for
    example for a forked worker, which inherits the parent's decision).
    """
    global _public_disabled
    _public_disabled = True


def _format_value(value: object) -> str:
    # bool first: bool is a subclass of int.
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if type(value) is float:
        return repr(value)
    raise TypeError("public event values must be int, float or bool")


def event(ev: Ev, **values: int | float | bool) -> None:
    """Write one public line ``<EV> k=v ...``.

    Raises TypeError for any value that is not exactly int, float or bool (strings, None,
    enums, numpy scalars, ...), so personal text can never reach the console by accident.
    """
    if not isinstance(ev, Ev):
        raise TypeError("event type must be an Ev member")
    parts = [ev.value]
    for key, value in values.items():
        if not _KEY_RE.fullmatch(key):
            raise ValueError("invalid public event key")
        parts.append(f"{key}={_format_value(value)}")
    _write_public(" ".join(parts))


def setup_logging(private_dir: Path | None = None, *, level: int = logging.INFO) -> Path | None:
    """Send the root logger to the private log file, or to stderr when there is none.

    ``private_dir`` defaults to ``$GPCLEAN_PRIVATE_DIR``. Returns the log file path (or None).
    Safe to call more than once: handlers added by a previous call are replaced.
    Python warnings are routed into logging too, so they never print on their own.
    """
    if private_dir is None and os.environ.get("GPCLEAN_PRIVATE_DIR"):
        private_dir = Path(os.environ["GPCLEAN_PRIVATE_DIR"])

    root = logging.getLogger()
    for handler in _our_handlers:
        root.removeHandler(handler)
        handler.close()
    _our_handlers.clear()

    log_path: Path | None = None
    handler: logging.Handler
    if private_dir is not None:
        private_dir = Path(private_dir)
        private_dir.mkdir(parents=True, exist_ok=True)
        log_path = private_dir / PRIVATE_LOG_NAME
        handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(process)d %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(level)
    _our_handlers.append(handler)
    logging.captureWarnings(True)
    return log_path


def set_phase(phase: int) -> None:
    """Record the current pipeline phase number used in the public FAILED line."""
    global _phase
    if type(phase) is not int:
        raise TypeError("phase must be an int")
    _phase = phase


def _safe_type_name(exc_type: type[BaseException] | None) -> str:
    name = getattr(exc_type, "__name__", "")
    return name if isinstance(name, str) and _TYPE_NAME_RE.fullmatch(name) else "Exception"


def _report_failure(exc_type, exc, tb: TracebackType | None) -> None:
    """Traceback to the private log; only the type name to the public console."""
    try:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        _log.error("unhandled exception (phase %d)\n%s", _phase, text)
    except Exception:  # never let logging trouble hide the public FAILED line
        pass
    _write_public(f"FAILED phase={_phase} exc={_safe_type_name(exc_type)}")


def install_excepthook(phase: int = 0) -> None:
    """Replace sys/threading excepthooks so an uncaught error prints only
    ``FAILED phase=<n> exc=<Type>`` publicly (message and traceback go to the private log).

    Call again (or use :func:`set_phase`) to update the phase number.
    """
    set_phase(phase)

    def _hook(exc_type, exc, tb):
        _report_failure(exc_type, exc, tb)

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return  # matches the default hook: SystemExit in a thread is silent
        _report_failure(args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = _hook
    threading.excepthook = _thread_hook


def _format_output_value(value: object) -> str:
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if isinstance(value, (list, tuple)) and all(type(v) is int for v in value):
        return json.dumps(list(value), separators=(",", ":"))
    if type(value) is str:
        if not _OUTPUT_VALUE_RE.fullmatch(value):
            raise ValueError("GITHUB_OUTPUT string value has forbidden characters")
        return value
    raise TypeError("GITHUB_OUTPUT values must be int, bool, str or a list of ints")


def github_output(**values: int | bool | str) -> None:
    """Append ``key=value`` lines to ``$GITHUB_OUTPUT`` (no-op when it is not set).

    Every value is validated before anything is written, so a bad value writes nothing.
    Strings must match ``^[\\w\\[\\],.:-]{0,4000}$`` (ASCII): no spaces, quotes or newlines,
    which rules out output/"heredoc" injection. Lists of ints are written as compact JSON.
    """
    lines = []
    for key, value in values.items():
        if not _OUTPUT_KEY_RE.fullmatch(key):
            raise ValueError("invalid GITHUB_OUTPUT key")
        lines.append(f"{key}={_format_output_value(value)}\n")
    target = os.environ.get("GITHUB_OUTPUT")
    if not target:
        _log.debug("GITHUB_OUTPUT not set; %d output(s) dropped", len(lines))
        return
    with open(target, "a", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(lines))
