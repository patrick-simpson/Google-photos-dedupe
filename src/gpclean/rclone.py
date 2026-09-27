"""The only way gpclean runs the rclone binary.

Safety properties enforced here, before anything is executed:

* Verbs are allow-listed (``ALLOWED_VERBS``); the destructive ones in ``DENIED_VERBS`` can
  never be run, whatever the caller passes. ``tools/check_repo.py`` greps the code base for
  them as a second line of defence.
* Write verbs (copyto/copy/mkdir) must target the configured output root
  (default ``gp:gpclean-output``), so the Takeout export and the rest of Drive stay untouched
  even if a caller has a bug. (The OAuth scope ``drive.file`` is the third line of defence.)
* rclone reads every flag from ``RCLONE_<FLAG>`` environment variables too (``RCLONE_DUMP``
  is ``--dump``), so the child gets a copy of the environment without ``RCLONE_*`` variables
  other than the config file location and password (see :func:`child_env`).
* rclone output never reaches the console: stdout is captured, stderr and rclone's own log go
  to the private log file. Nothing here prints; messages carry the verb and exit code only.
* A Drive download-limit/quota failure raises :class:`QuotaError`, so CI can stop cleanly
  and resume after the quota resets. ``rclone serve http`` runs for a whole scan, so it logs
  to its own file (``<log stem>-serve.log``) and HTTP readers ask
  :meth:`Rclone.serve_quota_hit` after a failed read; that way a quota line from the server
  is never blamed on an unrelated command, and the other way round.
"""

from __future__ import annotations

import configparser
import contextlib
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

_log = logging.getLogger(__name__)

ALLOWED_VERBS = frozenset(
    {"lsjson", "lsf", "cat", "copyto", "copy", "mkdir", "about", "serve", "version"}
)
DENIED_VERBS = frozenset(
    {"sync", "move", "moveto", "delete", "deletefile", "purge", "rmdir", "rmdirs",
     "link", "backend", "config", "dedupe", "cleanup", "bisync", "touch"}
)
WRITE_VERBS = frozenset({"copyto", "copy", "mkdir"})

# Flags no caller may add: dumping requests/headers would leak tokens into logs, and
# acknowledge-abuse would download files Google flagged as malware.
_DENIED_FLAG_PREFIXES = ("--dump", "--drive-acknowledge-abuse", "--rc", "--config", "--log-file")

SERVE_HTTP_FLAGS = (
    "--addr", "127.0.0.1:0",
    "--read-only",
    # No read-ahead: rclone's default 16M buffer reads past the end of every requested range
    # and throws those bytes away when the reader closes the connection (+20-50% Drive
    # egress measured). Readers stream sequentially behind their own buffer anyway.
    "--buffer-size", "0",
    "--vfs-cache-mode", "off",
    "--vfs-read-chunk-size", "16M",
    "--vfs-read-chunk-size-limit", "64M",
    # >0 hits an open deadlock bug (rclone #9900).
    "--vfs-read-chunk-streams", "0",
    "--drive-stop-on-download-limit",
    "--dir-cache-time", "1h",
)

# rclone exit codes 7 (fatal, which --drive-stop-on-download-limit produces) and 8
# (transfer limit exceeded) mean "stop for today"; so do these Drive error reasons.
QUOTA_EXIT_CODES = frozenset({7, 8})
_QUOTA_MARKERS = ("downloadQuotaExceeded", "quotaExceeded", "userRateLimitExceeded",
                  "download quota", "downloadLimitExceeded")

# The only RCLONE_* variables handed to rclone: where the config file is and the password of
# an encrypted one. Everything else (RCLONE_DUMP, RCLONE_LOG_LEVEL, RCLONE_RC,
# RCLONE_PASSWORD_COMMAND, RCLONE_CONFIG_<REMOTE>_<OPTION>...) is dropped.
_KEPT_RCLONE_ENV = frozenset({"RCLONE_CONFIG", "RCLONE_CONFIG_PASS"})

_REMOTE_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\- ]{0,63}")
# rclone 1.75 logs "HTTP Server started on [http://127.0.0.1:PORT/]"; older ones "Serving on".
_SERVING_RE = re.compile(
    r"(?:Server started on|Serving on)\s*\[?\s*https?://127\.0\.0\.1:(\d{1,5})")
_SERVE_START_TIMEOUT_S = 60.0


class RcloneError(RuntimeError):
    """rclone exited non-zero. The message holds only the verb and exit code."""

    def __init__(self, verb: str, returncode: int):
        super().__init__(f"rclone {verb} failed with exit code {returncode}")
        self.verb = verb
        self.returncode = returncode


class QuotaError(RcloneError):
    """Drive download limit or quota hit; retrying today is pointless."""


class RcloneDenied(PermissionError):
    """A call that the safety policy forbids (denied verb, write outside the output root)."""


def _check_arg(value: str, what: str) -> str:
    """Reject values that could be parsed as a flag or break a command line."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what} must be a non-empty string")
    if value.startswith("-"):
        raise ValueError(f"{what} must not start with '-'")
    if any(ch in value for ch in "\0\r\n"):
        raise ValueError(f"{what} contains control characters")
    return value


def child_env() -> dict[str, str]:
    """A copy of ``os.environ`` for the rclone child without flag-setting ``RCLONE_*`` vars.

    Environment variable names are case-insensitive on Windows, hence ``upper()``.
    """
    return {key: value for key, value in os.environ.items()
            if not key.upper().startswith("RCLONE_") or key.upper() in _KEPT_RCLONE_ENV}


def _read_text_since(path: Path, offset: int) -> str:
    """Text appended to ``path`` after byte ``offset`` ("" if it cannot be read)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _default_out_root() -> str:
    remote = os.environ.get("GPCLEAN_REMOTE") or "gp"
    root = os.environ.get("GPCLEAN_OUT_ROOT") or "gpclean-output"
    return f"{remote}:{root}"


class Rclone:
    """Thin, policy-enforcing wrapper around the rclone command line.

    ``config``: rclone config file (passed as ``--config`` on every call when given).
    ``log_file``: private log file for rclone's own log and stderr; defaults to
    ``$GPCLEAN_PRIVATE_DIR/rclone.log`` when that variable is set.
    ``out_root``: the only place write verbs may target, e.g. ``gp:gpclean-output``;
    defaults to ``$GPCLEAN_REMOTE:$GPCLEAN_OUT_ROOT`` (``gp:gpclean-output``).
    """

    def __init__(self, config: Path | None = None, binary: str = "rclone",
                 log_file: Path | None = None, *, out_root: str | None = None):
        self.config = Path(config) if config is not None else None
        self.binary = binary
        if log_file is None and os.environ.get("GPCLEAN_PRIVATE_DIR"):
            log_file = Path(os.environ["GPCLEAN_PRIVATE_DIR"]) / "rclone.log"
        self.log_file = Path(log_file) if log_file is not None else None
        self.out_root = self._validate_out_root(out_root or _default_out_root())
        # Running ``serve http`` processes as (process, log file, start offset), plus a sticky
        # flag, so serve_quota_hit() still answers True after a server has been stopped.
        self._serves: list[tuple[subprocess.Popen, Path, int]] = []
        self._serve_lock = threading.Lock()
        self._serve_quota_seen = False

    # ----- policy -------------------------------------------------------------------------

    @staticmethod
    def _validate_out_root(out_root: str) -> str:
        remote, sep, path = out_root.partition(":")
        path = path.strip("/")
        if not sep or not _REMOTE_NAME_RE.fullmatch(remote) or not path:
            # A bare "gp:" would make the whole Drive writable, so a folder is required.
            raise ValueError("out_root must look like 'remote:folder'")
        if "\\" in path or any(part in ("", ".", "..") for part in path.split("/")):
            raise ValueError("out_root has an invalid path")
        return f"{remote}:{path}"

    def is_under_out_root(self, dst: str) -> bool:
        """True when ``dst`` is the output root or a path inside it."""
        if not isinstance(dst, str) or "\\" in dst:
            return False
        if dst == self.out_root:
            return True
        prefix = self.out_root + "/"
        if not dst.startswith(prefix):
            return False
        rest = dst[len(prefix):].rstrip("/")
        return bool(rest) and all(part not in ("", ".", "..") for part in rest.split("/"))

    def _check_write_dst(self, verb: str, dst: str) -> None:
        _check_arg(dst, "destination")
        if not self.is_under_out_root(dst):
            raise RcloneDenied(f"rclone {verb}: destination is outside the output root")

    @staticmethod
    def _check_verb(verb: str) -> None:
        if verb in DENIED_VERBS or verb not in ALLOWED_VERBS:
            raise RcloneDenied(f"rclone verb not allowed: {verb!r}")

    # ----- running ------------------------------------------------------------------------

    def _common_flags(self, log_file: Path | None) -> list[str]:
        flags = []
        if self.config is not None:
            flags += ["--config", str(self.config)]
        if log_file is not None:
            flags += ["--log-file", str(log_file), "--log-level", "INFO"]
        return flags

    def _command(self, verb: str, args: list[str], log_file: Path | None) -> list[str]:
        self._check_verb(verb)
        for arg in args:
            if not isinstance(arg, str):
                raise TypeError("rclone arguments must be strings")
            if arg.startswith(_DENIED_FLAG_PREFIXES):
                raise RcloneDenied("rclone flag not allowed")
        return [self.binary, verb, *args, *self._common_flags(log_file)]

    def _log_size(self) -> int:
        try:
            return self.log_file.stat().st_size if self.log_file else 0
        except OSError:
            return 0

    def _read_log_since(self, offset: int) -> str:
        return _read_text_since(self.log_file, offset) if self.log_file is not None else ""

    @staticmethod
    def _is_quota(returncode: int | None, log_text: str) -> bool:
        return returncode in QUOTA_EXIT_CODES or any(m in log_text for m in _QUOTA_MARKERS)

    @classmethod
    def _raise_for(cls, verb: str, returncode: int, log_text: str) -> None:
        if cls._is_quota(returncode, log_text):
            raise QuotaError(verb, returncode)
        raise RcloneError(verb, returncode)

    def _run(self, verb: str, args: list[str], *, stdout=None,
             timeout: float | None = None) -> bytes:
        """Run one rclone command; return captured stdout (b"" when ``stdout`` is a file)."""
        cmd = self._command(verb, args, self.log_file)
        offset = self._log_size()
        if self.log_file is not None:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
        # stderr (panics, early errors) is appended to the private log next to rclone's own
        # --log-file output; without a log file it is captured and scanned instead.
        log_fh = open(self.log_file, "ab") if self.log_file is not None else None
        try:
            proc = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,  # never block on an interactive prompt
                stdout=stdout if stdout is not None else subprocess.PIPE,
                stderr=log_fh if log_fh is not None else subprocess.PIPE,
                env=child_env(),
                timeout=timeout,
                check=False,
            )
        finally:
            if log_fh is not None:
                log_fh.close()
        if self.log_file is not None:
            log_text = self._read_log_since(offset) if proc.returncode else ""
        else:
            log_text = (proc.stderr or b"").decode("utf-8", errors="replace")
            if log_text:
                _log.debug("rclone %s stderr:\n%s", verb, log_text)
        _log.debug("rclone %s exit=%d", verb, proc.returncode)
        if proc.returncode != 0:
            self._raise_for(verb, proc.returncode, log_text)
        return proc.stdout if isinstance(proc.stdout, bytes) else b""

    # ----- read verbs ---------------------------------------------------------------------

    def lsjson(self, path: str, *, recursive: bool = False, files_only: bool = False,
               hash: bool = False, max_depth: int | None = None) -> list[dict]:
        """``rclone lsjson`` of a directory; returns the parsed list of entries."""
        args = [_check_arg(path, "path")]
        if recursive:
            args.append("--recursive")
        if files_only:
            args.append("--files-only")
        if hash:
            args.append("--hash")
        if max_depth is not None:
            args += ["--max-depth", str(int(max_depth))]
        out = self._run("lsjson", args)
        return json.loads(out.decode("utf-8")) if out.strip() else []

    def stat(self, path: str) -> dict | None:
        """``rclone lsjson --stat``: the entry for one file/dir, or None if it does not exist."""
        try:
            out = self._run("lsjson", [_check_arg(path, "path"), "--stat"])
        except QuotaError:
            raise
        except RcloneError as exc:
            if exc.returncode in (3, 4):  # directory / file not found
                return None
            raise
        return json.loads(out.decode("utf-8")) if out.strip() else None

    def lsf(self, path: str, *, recursive: bool = False, files_only: bool = False) -> list[str]:
        """``rclone lsf``: names (directories end with '/')."""
        args = [_check_arg(path, "path")]
        if recursive:
            args.append("--recursive")
        if files_only:
            args.append("--files-only")
        out = self._run("lsf", args).decode("utf-8")
        return [line for line in out.splitlines() if line]

    def cat(self, path: str, *, offset: int = 0, count: int | None = None) -> bytes:
        """Bytes of one remote file (optionally a range)."""
        return self._run("cat", self._cat_args(path, offset, count))

    def cat_to_file(self, path: str, local: Path, *, offset: int = 0,
                    count: int | None = None) -> None:
        """Stream a remote file into ``local`` (downloads never need a write verb)."""
        local = Path(local)
        local.parent.mkdir(parents=True, exist_ok=True)
        with open(local, "wb") as fh:
            self._run("cat", self._cat_args(path, offset, count), stdout=fh)

    @staticmethod
    def _cat_args(path: str, offset: int, count: int | None) -> list[str]:
        args = [_check_arg(path, "path")]
        if offset:
            args += ["--offset", str(int(offset))]
        if count is not None:
            args += ["--count", str(int(count))]
        return args

    def about(self, remote: str) -> dict:
        """``rclone about --json`` (quota numbers). Also forces an OAuth token refresh."""
        out = self._run("about", [_check_arg(remote, "remote"), "--json"])
        return json.loads(out.decode("utf-8")) if out.strip() else {}

    def version(self) -> str:
        """First line of ``rclone version``."""
        out = self._run("version", []).decode("utf-8", errors="replace")
        return out.splitlines()[0] if out else ""

    # ----- write verbs (output root only) -------------------------------------------------

    def copyto(self, src: str, dst: str) -> None:
        """Copy one file to ``dst``, which must be under the output root."""
        _check_arg(src, "source")
        self._check_write_dst("copyto", dst)
        self._run("copyto", [src, dst])

    def copy(self, src: str, dst: str) -> None:
        """Copy a directory's contents into ``dst``, which must be under the output root."""
        _check_arg(src, "source")
        self._check_write_dst("copy", dst)
        self._run("copy", [src, dst])

    def mkdir(self, path: str) -> None:
        """Create a directory (and parents) under the output root."""
        self._check_write_dst("mkdir", path)
        self._run("mkdir", [path])

    # ----- serve http ---------------------------------------------------------------------

    @contextlib.contextmanager
    def serve_http(self, path: str) -> Iterator[str]:
        """Run ``rclone serve http <path> --read-only`` on 127.0.0.1; yield its base URL.

        The URL has no trailing slash (``http://127.0.0.1:<port>``); join member paths with
        ``"/" + urllib.parse.quote(name)``. rclone picks a free port (``--addr 127.0.0.1:0``)
        and we read it back from rclone's log. The server is terminated on exit.

        The server logs to its own file: ``<log_file stem>-serve.log`` next to ``log_file``,
        or a temporary file (deleted on exit) when there is no log file. An HTTP read that
        fails should call :meth:`serve_quota_hit` and raise :class:`QuotaError` if it is
        True, because the HTTP response itself does not say why rclone failed.
        """
        _check_arg(path, "path")
        tmp_log: Path | None = None
        if self.log_file is not None:
            log_file = self.log_file.with_name(self.log_file.stem + "-serve.log")
        else:
            # The port is only reported in rclone's log, so a log file is required.
            fd, name = tempfile.mkstemp(prefix="gpclean-rclone-serve-", suffix=".log")
            os.close(fd)
            tmp_log = log_file = Path(name)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        offset = log_file.stat().st_size if log_file.exists() else 0
        cmd = self._command("serve", ["http", path, *SERVE_HTTP_FLAGS], log_file)
        with open(log_file, "ab") as stderr:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=stderr, env=child_env())
        entry = (proc, log_file, offset)
        with self._serve_lock:
            self._serves.append(entry)
        try:
            port = self._wait_for_port(proc, log_file, offset)
            _log.debug("rclone serve http listening")
            yield f"http://127.0.0.1:{port}"
        finally:
            self.serve_quota_hit()  # record a quota hit before the (temp) log goes away
            with self._serve_lock:
                self._serves.remove(entry)
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
            if tmp_log is not None:
                with contextlib.suppress(OSError):
                    tmp_log.unlink()

    def serve_quota_hit(self) -> bool:
        """True once any ``serve_http`` server of this instance has hit a Drive quota.

        Checks the serve logs for Drive's download-limit/quota errors (and a server that
        exited with a quota exit code). The answer is sticky: once True it stays True, also
        after the server has stopped. Safe to call from reader threads.
        """
        with self._serve_lock:
            serves = list(self._serves)
        for proc, log_file, offset in serves:
            if self._serve_quota_seen:
                break
            if self._is_quota(proc.poll(), _read_text_since(log_file, offset)):
                self._serve_quota_seen = True
        return self._serve_quota_seen

    def _wait_for_port(self, proc: subprocess.Popen, log_file: Path, offset: int) -> int:
        deadline = time.monotonic() + _SERVE_START_TIMEOUT_S
        while True:
            text = _read_text_since(log_file, offset)
            match = _SERVING_RE.search(text)
            if match:
                return int(match.group(1))
            if proc.poll() is not None:
                self._raise_for("serve", proc.returncode or 1, text)
            if time.monotonic() > deadline:
                raise RcloneError("serve", -1)
            time.sleep(0.05)

    # ----- token --------------------------------------------------------------------------

    def access_token(self, remote: str, *, refresh: bool = False) -> str:
        """The OAuth access token of ``remote`` from the rclone config file (for tokeninfo).

        With ``refresh=True`` an ``rclone about`` runs first, which makes rclone refresh an
        expiring token and write it back to the (writable) config. The token is never logged
        and never appears in exception messages.
        """
        name = remote[:-1] if remote.endswith(":") else remote
        if not _REMOTE_NAME_RE.fullmatch(name):
            raise ValueError("invalid remote name")
        if self.config is None:
            raise ValueError("access_token needs an explicit rclone config file")
        if refresh:
            self.about(name + ":")
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read_string(self.config.read_text(encoding="utf-8"))
        except (configparser.Error, UnicodeDecodeError):
            # e.g. an encrypted config ("RCLONE_ENCRYPT_V0:"); never echo the content
            raise ValueError("the rclone configuration file is not a readable INI file") from None
        if not parser.has_section(name) or not parser.has_option(name, "token"):
            raise KeyError("remote has no token in the rclone config")
        try:
            token = json.loads(parser.get(name, "token"))
            access = token["access_token"]
        except (ValueError, KeyError, TypeError):
            raise ValueError("rclone token is not valid JSON with an access_token") from None
        if not isinstance(access, str) or not access:
            raise ValueError("rclone token has no access_token")
        return access
