#!/usr/bin/env python3
"""Repo hygiene gate for a PUBLIC repo that handles private photos (stdlib only).

Runs in CI and as the pre-commit hook (``git config core.hooksPath .githooks``). It checks the
*staged/committed* content of every tracked file (``git ls-files``), so it sees exactly what
would be published. It fails when a file:

* has an extension that is not allow-listed, is too large, or is not UTF-8 text;
* starts with the magic bytes of an image, archive, SQLite DB or numpy array;
* contains a secret or personal-data pattern (OAuth tokens, rclone Drive sections,
  private keys, real Google Photos item ids, e-mail addresses);
* is a workflow that breaks the Actions lint (only pinned actions/checkout, safe triggers,
  no ``${{ }}`` expressions inside ``run:`` except ``matrix.*``, ``permissions: {}``, ...);
* calls a destructive rclone verb, or (under ``src/``) names the rclone binary anywhere but
  ``src/gpclean/rclone.py``, so every call has to go through the allow-listing wrapper.

Output is one line per problem: ``<path>: <reason>``. File CONTENT is never printed, because
the thing being caught may be a secret or personal data. Exit code 0 = clean, 1 = problems.

Usage: python tools/check_repo.py [--root DIR]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import PurePosixPath

MAX_BYTES = 256 * 1024
SIZE_EXEMPT = {"uv.lock"}

ALLOWED_EXTS = {".py", ".toml", ".lock", ".md", ".yml", ".yaml", ".sh", ".ps1",
                ".html", ".css", ".js", ".txt"}
ALLOWED_NAMES = {"LICENSE", ".githooks/pre-commit", ".python-version"}
# Dotfiles are allow-listed by name: many others (.netrc, .pgpass, .npmrc, .pypirc) hold
# credentials in a form no secret pattern recognises, and .DS_Store leaks folder names.
ALLOWED_DOTFILES = {".gitignore", ".gitattributes", ".editorconfig", ".gitkeep"}

# (name, test) for binary formats that must never be committed. Checked on the first bytes.
MAGIC = [
    ("JPEG", lambda b: b.startswith(b"\xff\xd8\xff")),
    ("PNG", lambda b: b.startswith(b"\x89PNG\r\n\x1a\n")),
    ("GIF", lambda b: b.startswith(b"GIF8")),
    ("WEBP", lambda b: b.startswith(b"RIFF") and b[8:12] == b"WEBP"),
    ("HEIF/AVIF", lambda b: b[4:8] == b"ftyp"),
    ("ZIP", lambda b: b.startswith(b"PK\x03\x04")),
    ("SQLite", lambda b: b.startswith(b"SQLite format 3\x00")),
    ("NPY", lambda b: b.startswith(b"\x93NUMPY")),
    ("GZIP", lambda b: b.startswith(b"\x1f\x8b")),
    ("7Z", lambda b: b.startswith(b"7z\xbc\xaf\x27\x1c")),
]

# Secrets and personal data. Each is reported by name only.
SECRET_PATTERNS = [
    ("Google access token", re.compile(r"ya29\.[\w-]{20,}")),
    ("Google refresh token", re.compile(r"1//0[\w-]{30,}")),
    ("OAuth client secret", re.compile(r"GOCSPX-[\w-]{20,}")),
    ("refresh_token value", re.compile(r'"refresh_token"\s*:\s*"[^"]{10,}')),
    ("rclone drive config section",
     re.compile(r"^[ \t]*type[ \t]*=[ \t]*drive\b", re.MULTILINE)),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY")),
    ("real Google Photos id", re.compile(r"AF1Qip(?!FAKE)[\w-]{20,}")),
    # A photo pasted into an allowed text file (.md/.html/.js) as base64: data: URIs (SVG is
    # vector art, not a photo) and the base64 of the JPEG/JFIF and PNG headers, which also
    # catch bare base64 blobs. The headers are split so this file does not match itself.
    ("embedded image", re.compile(
        r"data:image/(?!svg)[\w+.-]+;base64,|" + "/9j/4AAQ" + "SkZJRg|" + "iVBORw0" + "KGgo")),
]
# Bounded quantifiers: an unbounded local part makes long runs of word characters
# (lockfile hashes) quadratic to scan.
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]{1,64}@((?:[A-Za-z0-9-]{1,63}\.){1,8}[A-Za-z]{2,24})")
ALLOWED_EMAIL_DOMAINS = ("example.com", "users.noreply.github.com", "anthropic.com")
# URL userinfo (https://user@host/...) is only exempt for these hosts, so that URL-validation
# tests can use it without letting a real address written as a URL through.
USERINFO_HOSTS = ("photos.google.com", "github.com")
# "local part@domain" pairs that are not e-mail addresses, e.g. SSH clone URLs.
ALLOWED_ADDRESSES = {("git", "github.com")}

# ----- workflow lint ---------------------------------------------------------------------
ALLOWED_TRIGGERS = {"push", "pull_request", "workflow_dispatch", "workflow_call"}
CI_ONLY_TRIGGERS = {"pull_request"}  # pull_request only in ci.yml (no secrets there)
USES_OK = re.compile(r"(actions/checkout@[0-9a-f]{40}|\./\.github/workflows/[\w.-]+\.ya?ml)")
# Any key spelling of ``uses`` (plain, quoted, or inside a flow mapping ``{uses: x}``).
USES_RE = re.compile(r"[\"']?\buses[\"']?\s*:\s*[\"']?([^\s\"',}]+)")
# The ``run:`` key, also quoted or as the first key of a list item.
RUN_KEY_RE = re.compile(r"^(\s*(?:-\s+)?)[\"']?run[\"']?\s*:\s*(.*)$")
# Every expression inside run: is rejected: values reach scripts through ``env:`` only.
# Blocklisting contexts does not work (format(), toJSON(github), github['event'],
# github.head_ref, env copied from inputs...). Matrix values are integers set in the
# workflow itself, so a bare ``${{ matrix.x }}`` is the one exception.
EXPR_IN_RUN = re.compile(r"\$\{\{(?!\s*matrix\.[\w-]+\s*\}\})")
WORKFLOW_FORBIDDEN = [
    ("id-token permission", re.compile(r"\bid-token\b")),
    ("actions/cache", re.compile(r"actions/cache\b")),
    ("upload-artifact", re.compile(r"upload-artifact")),
    ("download-artifact", re.compile(r"download-artifact")),
    # set -x / set -o xtrace / shell: bash -x {0} / bash -ex script.sh
    ("set -x", re.compile(
        r"\bset\s+-[a-wyz]*x|\bxtrace\b|\b(?:ba|da|z|k)?sh\s+(?:-\S+\s+)*-[a-wyz]*x")),
    ("--dump", re.compile(r"--dump\b")),
    ("secrets: inherit", re.compile(r"\bsecrets\s*:\s*inherit\b")),
    # "contents: write", quoted forms, and flow mappings "{contents: write, ...}".
    ("write permission", re.compile(
        r"\b[\w-]+[\"']?[ \t]*:[ \t]*[\"']?write[\"']?(?=[ \t]*[,}\n]|[ \t]*$)|\bwrite-all\b",
        re.MULTILINE)),
    ("forbidden trigger", re.compile(
        r"^\s*-?\s*[\"']?(pull_request_target|workflow_run|schedule|issue_comment)[\"']?\s*:",
        re.MULTILINE)),
    ("forbidden trigger", re.compile(r"\b(pull_request_target|workflow_run|issue_comment)\b")),
]

# ----- rclone verb denylist ----------------------------------------------------------------
DENIED_VERBS = {"sync", "move", "moveto", "delete", "deletefile", "purge", "rmdir", "rmdirs",
                "link", "backend", "config", "dedupe", "cleanup", "bisync", "touch"}
_VERB_ALT = "|".join(sorted(DENIED_VERBS))
# Python: method calls on an rclone wrapper object (self.rclone.purge(...), rc.delete(...)),
# verbs handed to a runner (self._run("sync", ...)), argv lists (["rclone", "sync", ...]) and
# string literals that start with an rclone command ("rclone --flag x sync ...").
PY_VERB_RES = [
    re.compile(rf"\b(?:\w*rclone\w*|rc)\s*\.\s*({_VERB_ALT})\s*\(", re.I),
    re.compile(rf"\b_?run\(\s*[\"']({_VERB_ALT})[\"']"),
    re.compile(rf"[\"'][^\"']*rclone(?:\.exe)?[\"']\s*,\s*[\"']({_VERB_ALT})[\"']"),
    # Words inside one string literal: "rclone --config c purge", f"rclone {flags} purge".
    re.compile(rf"[\"']rclone(?:\.exe)?\s+(?:[^\s\"']+\s+)*?({_VERB_ALT})\b"),
]
# Whole-file (multi-line) argv lists, as black formats them:
#   subprocess.run(\n    ["rclone",\n     "--config", c,\n     "purge", x])
PY_ARGV_RE = re.compile(
    rf"[\"']rclone(?:\.exe)?[\"']\s*,[^\]\)]*?[\"']({_VERB_ALT})[\"']", re.S)
# The rclone binary named as a string literal: only the wrapper may do that (see check_file).
PY_RCLONE_LITERAL = re.compile(r"[\"']rclone(?:\.exe)?[\"']")
RCLONE_WRAPPER = "src/gpclean/rclone.py"
VERB_SCAN_PREFIXES = ("src/", "tools/", ".github/workflows/")
# Files that legitimately name the verbs: this checker (it defines the list) and the local
# Windows helper that re-authorises the remote with rclone's config verb.
VERB_EXEMPT = {"tools/check_repo.py": DENIED_VERBS, "tools/refresh-secret.ps1": {"config"}}
_TOKEN_STRIP = "\"'`,;()[]{}"


def git_files(root: str) -> list[tuple[str, str, str]]:
    """(mode, blob sha, path) for every tracked file, from the index."""
    out = subprocess.run(["git", "ls-files", "-z", "--stage"], cwd=root, check=True,
                         capture_output=True).stdout
    files = []
    for record in out.split(b"\0"):
        if not record:
            continue
        meta, _, path = record.partition(b"\t")
        mode, sha, _stage = meta.decode().split(" ")
        files.append((mode, sha, path.decode("utf-8", errors="surrogateescape")))
    return files


def read_blobs(root: str, shas: list[str]) -> dict[str, bytes]:
    """Read blobs with one ``git cat-file --batch`` process."""
    if not shas:
        return {}
    unique = list(dict.fromkeys(shas))
    proc = subprocess.run(["git", "cat-file", "--batch"], cwd=root, check=True,
                          input=("\n".join(unique) + "\n").encode(), capture_output=True)
    data, pos, blobs = proc.stdout, 0, {}
    for sha in unique:
        end = data.index(b"\n", pos)
        header = data[pos:end].split(b" ")
        pos = end + 1
        if len(header) < 3 or header[1] == b"missing":
            blobs[sha] = b""
            continue
        size = int(header[2])
        blobs[sha] = data[pos:pos + size]
        pos += size + 1  # content is followed by a newline
    return blobs


def check_name(path: str) -> str | None:
    if path in ALLOWED_NAMES:
        return None
    name = PurePosixPath(path).name
    if name.startswith(".env"):
        return "environment files are not allowed"
    if name.startswith("."):
        return None if name in ALLOWED_DOTFILES else "dotfile not allowed"
    if PurePosixPath(path).suffix.lower() in ALLOWED_EXTS:
        return None
    return "file type not allowed"


def check_magic(data: bytes) -> str | None:
    head = data[:16]
    for name, test in MAGIC:
        if test(head):
            return f"binary content ({name} magic bytes)"
    return None


def check_secrets(text: str) -> list[str]:
    problems = [f"contains {name}" for name, rx in SECRET_PATTERNS if rx.search(text)]
    for match in EMAIL_RE.finditer(text):
        domain = match.group(1).lower()
        local = match.group(0).rpartition("@")[0]
        if (local, domain) in ALLOWED_ADDRESSES:
            continue
        if (text[max(0, match.start() - 3):match.start()] == "://"
                and _domain_in(domain, USERINFO_HOSTS)):
            continue  # URL userinfo on a known service, e.g. in URL-validation tests
        if not _domain_in(domain, ALLOWED_EMAIL_DOMAINS):
            problems.append("contains an e-mail address")
            break
    return problems


def _domain_in(domain: str, allowed: tuple[str, ...]) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in allowed)


def _strip_comment(line: str) -> str:
    """Drop a YAML/shell comment: '#' at line start or after whitespace (not in '${{')."""
    if line.lstrip().startswith("#"):
        return ""
    return re.split(r"\s#", line, maxsplit=1)[0]


def _triggers(lines: list[str]) -> list[str] | None:
    """Trigger names from the top-level ``on:`` key (None if there is no ``on:``)."""
    for i, line in enumerate(lines):
        m = re.match(r"^[\"']?(on|true)[\"']?\s*:\s*(.*)$", line)
        if not m:
            continue
        value = m.group(2).strip()
        if value.startswith("["):
            return [t.strip().strip("\"'") for t in value.strip("[]").split(",") if t.strip()]
        if value:
            return [value.strip("\"'")]
        names, child_indent = [], None
        for sub in lines[i + 1:]:
            if not sub.strip():
                continue
            indent = len(sub) - len(sub.lstrip())
            if indent == 0:
                break
            if child_indent is None:
                child_indent = indent
            if indent == child_indent:
                key = sub.strip().lstrip("- ").split(":")[0].strip().strip("\"'")
                names.append(key)
        return names
    return None


def _run_blocks(lines: list[str]) -> list[str]:
    """The text of every ``run:`` value (inline or block scalar).

    Must be given RAW lines: inside a block scalar '#' is ordinary text that GitHub still
    expands expressions in, so stripping comments first would hide ``# ${{ ... }}``.
    """
    blocks = []
    for i, line in enumerate(lines):
        m = RUN_KEY_RE.match(line)
        if not m:
            continue
        # Column of the key itself ("- run:" puts it after the dash): block content is
        # indented deeper than that, and the step's next key (env:, shell:) is not.
        key_indent = len(m.group(1))
        body = [m.group(2)]
        for sub in lines[i + 1:]:
            if sub.strip() and len(sub) - len(sub.lstrip()) <= key_indent:
                break
            body.append(sub)
        blocks.append("\n".join(body))
    return blocks


def lint_workflow(path: str, text: str) -> list[str]:
    """Problems in one GitHub Actions workflow file (line/regex based, no YAML parser)."""
    problems = []
    raw = [line.rstrip("\r") for line in text.split("\n")]
    lines = [_strip_comment(line) for line in raw]
    body = "\n".join(lines)

    # Not anchored to line starts, so flow mappings ("- {uses: x}") are seen too.
    for m in USES_RE.finditer(body):
        if not USES_OK.fullmatch(m.group(1)):
            problems.append("uses: something other than a SHA-pinned actions/checkout")
    n_checkout = len(re.findall(r"uses[\"']?\s*:\s*[\"']?actions/checkout@", body))
    n_no_persist = len(re.findall(r"persist-credentials\s*:\s*false\b", body))
    if n_checkout > n_no_persist:
        problems.append("actions/checkout without persist-credentials: false")

    triggers = _triggers(lines)
    if not triggers:
        problems.append("no 'on:' triggers found")
    else:
        is_ci = PurePosixPath(path).name in ("ci.yml", "ci.yaml")
        for trig in triggers:
            if trig not in ALLOWED_TRIGGERS or (trig in CI_ONLY_TRIGGERS and not is_ci):
                problems.append("trigger not allowed")
                break

    if not re.search(r"^permissions\s*:\s*\{\s*\}\s*$", body, re.MULTILINE):
        problems.append("top-level 'permissions: {}' missing")
    for block in _run_blocks(raw):
        if EXPR_IN_RUN.search(block):
            problems.append("${{ }} expression inside run: (pass values through env:)")
            break
    for name, rx in WORKFLOW_FORBIDDEN:
        if rx.search(body):
            problems.append(f"forbidden: {name}")
    return list(dict.fromkeys(problems))


def check_rclone_verbs(path: str, text: str) -> list[str]:
    """Destructive rclone verbs in code.

    Shell, PowerShell and workflow lines are commands, so any denied verb token after an
    ``rclone`` token on the same (continued) line counts. Python needs code-shaped patterns
    instead (see PY_VERB_RES), so that prose such as "the rclone config file" in a docstring
    passes. This is a second line of defence; the runtime allowlist in gpclean.rclone is
    the first, and src/ code must use it (see check_file).
    """
    denied = DENIED_VERBS - VERB_EXEMPT.get(path, set())
    if not denied:
        return []
    is_python = path.endswith(".py")
    if is_python:
        if any(m.group(1).lower() in denied for m in PY_ARGV_RE.finditer(text)):
            return ["uses a denied rclone verb"]
    else:
        # Join shell "\" and PowerShell "`" line continuations into one command line.
        text = re.sub(r"[\\`]\r?\n", " ", text)
    for raw in text.split("\n"):
        line = _strip_comment(raw)
        if is_python:
            for rx in PY_VERB_RES:
                if any(m.group(1).lower() in denied for m in rx.finditer(line)):
                    return ["uses a denied rclone verb"]
            continue
        tokens = [t.strip(_TOKEN_STRIP) for t in line.split()]
        for i, tok in enumerate(tokens):
            base = re.split(r"[\\/]", tok)[-1].lower()
            if base in ("rclone", "rclone.exe") and any(t in denied for t in tokens[i + 1:]):
                return ["uses a denied rclone verb"]
    return []


def check_file(path: str, mode: str, data: bytes) -> list[str]:
    """All problems for one tracked file."""
    if mode == "160000":
        return ["submodules are not allowed"]
    problems = []
    reason = check_name(path)
    if reason:
        problems.append(reason)
    if len(data) > MAX_BYTES and path not in SIZE_EXEMPT:
        problems.append(f"larger than {MAX_BYTES // 1024} KiB")
    magic = check_magic(data)
    if magic:
        return problems + [magic]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return problems + ["not valid UTF-8 text"]
    problems += check_secrets(text)
    if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")):
        problems += lint_workflow(path, text)
    if path.startswith(VERB_SCAN_PREFIXES):
        problems += check_rclone_verbs(path, text)
    if (path.startswith("src/") and path.endswith(".py") and path != RCLONE_WRAPPER
            and PY_RCLONE_LITERAL.search(text)):
        problems.append("runs rclone directly (use gpclean.rclone.Rclone)")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="repository root (default: cwd)")
    args = parser.parse_args(argv)
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=args.root,
                             check=True, capture_output=True, text=True).stdout.strip()
        files = git_files(top)
        blobs = read_blobs(top, [sha for _, sha, _ in files])
    except (OSError, subprocess.CalledProcessError):
        print("check_repo: could not list tracked files with git", file=sys.stderr)
        return 2
    failures = 0
    for mode, sha, path in files:
        for problem in check_file(path, mode, blobs.get(sha, b"")):
            print(f"{path}: {problem}")
            failures += 1
    if failures:
        print(f"check_repo: {failures} problem(s) in {len(files)} tracked files")
        return 1
    print(f"check_repo: ok ({len(files)} tracked files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
