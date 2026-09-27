"""Tests for tools/check_repo.py on throwaway git repos with planted problems.

Planted secrets are assembled at runtime (string concatenation), so this file itself never
contains a pattern that check_repo would flag in the real repo.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CHECKER = REPO / "tools" / "check_repo.py"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_repo", CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_repo = _load_checker()

GOOD_WORKFLOW = """\
name: ci
on:
  push:
  pull_request:
permissions: {}
jobs:
  test:
    runs-on: ubuntu-24.04
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@0123456789abcdef0123456789abcdef01234567 # v5.1.0
        with:
          persist-credentials: false
      - name: t
        env:
          FOLDER: ${{ inputs.folder }}
        run: |
          echo "$FOLDER"
          uv run pytest -q
"""


def _wf_run(line: str) -> str:
    """GOOD_WORKFLOW with ``line`` added to the block-scalar run: of the last step."""
    return GOOD_WORKFLOW.replace("          uv run pytest -q\n",
                                 f"          uv run pytest -q\n          {line}\n")


def _wf_step(step: str) -> str:
    """GOOD_WORKFLOW with ``step`` (e.g. "- run: x") inserted as a new first-level step."""
    return GOOD_WORKFLOW.replace("      - name: t\n", f"      {step}\n      - name: t\n")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def make_repo(root: Path, files: dict[str, bytes | str]) -> Path:
    """Create a git repo at ``root`` with ``files`` staged (not committed)."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    base: dict[str, bytes | str] = {
        "README.md": "# demo\n",
        "LICENSE": "MIT\n",
        ".gitignore": "__pycache__/\n",
        ".python-version": "3.12\n",
        "src/pkg/mod.py": "def f():\n    return 1\n",
        ".github/workflows/ci.yml": GOOD_WORKFLOW,
    }
    base.update(files)
    for rel, content in base.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    _git(root, "add", "-A", "-f")
    return root


def run_checker(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CHECKER), "--root", str(root)],
                          capture_output=True, text=True, encoding="utf-8")


def test_clean_repo_passes(tmp_path):
    result = run_checker(make_repo(tmp_path / "r", {}))
    assert result.returncode == 0, result.stdout
    assert "ok" in result.stdout


def test_our_ci_workflow_passes(tmp_path):
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_bytes()
    result = run_checker(make_repo(tmp_path / "r", {".github/workflows/ci.yml": ci}))
    assert result.returncode == 0, result.stdout


def test_our_own_files_pass():
    # Files this checker governs that live next to it must pass it themselves.
    for rel in ["tools/check_repo.py", "tools/install_tools.sh", "tools/install_uv.ps1",
                ".githooks/pre-commit", ".github/workflows/ci.yml", "src/gpclean/rclone.py",
                "src/gpclean/store.py", "src/gpclean/publiclog.py",
                "tests/test_repo_hygiene.py", "tests/test_rclone.py", "tests/test_store.py",
                "tests/test_publiclog.py"]:
        path = REPO / rel
        assert check_repo.check_file(rel, "100644", path.read_bytes()) == [], rel


FAKE_TOKEN = "ya" + "29." + "a1B2c3D4" * 5
REAL_ID = "AF1" + "Qip" + "Zq9" * 10

PLANTS = {
    "jpeg magic": ("notes.txt", b"\xff\xd8\xff\xe0\x00\x10JFIF\x00", "JPEG"),
    "jpg extension": ("photo.jpg", b"not really", "file type not allowed"),
    "png magic": ("img.md", b"\x89PNG\r\n\x1a\n rest", "PNG"),
    "sqlite": ("state.txt", b"SQLite format 3\x00" + b"\x00" * 50, "SQLite"),
    "zip": ("a.py", b"PK\x03\x04rest", "ZIP"),
    "heic": ("b.txt", b"\x00\x00\x00\x18ftypheic", "HEIF"),
    "npy": ("c.txt", b"\x93NUMPY\x01\x00", "NPY"),
    "non-utf8": ("d.txt", b"caf\xe9\n", "UTF-8"),
    "too large": ("big.txt", b"x" * (256 * 1024 + 1), "larger than"),
    "unknown ext": ("data.json", b"{}", "file type not allowed"),
    "env file": (".env", b"A=1\n", "environment"),
    "access token": ("src/pkg/cfg.py", f"TOKEN = '{FAKE_TOKEN}'\n", "access token"),
    "refresh token": ("x.md", "1//" + "0" + "Ab" * 20 + "\n", "refresh token"),
    "client secret": ("y.md", "GOCSPX-" + "Q" * 24 + "\n", "client secret"),
    "refresh json": ("z.md", '{"refresh' + '_token": "' + "r" * 20 + '"}\n',
                     "refresh_token"),
    "rclone section": ("conf.txt", "[gp]\n" + "type = " + "drive\n", "rclone drive"),
    "private key": ("k.txt", "-----BEGIN RSA " + "PRIVATE KEY-----\n", "private key"),
    "real photos id": ("tests/t.py", f"URL = 'https://photos.google.com/photo/{REAL_ID}'\n",
                       "Google Photos id"),
    "email": ("docs/a.md", "contact: someone" + "@" + "gmail.com\n", "e-mail"),
    "bad uses": (".github/workflows/ci.yml",
                 GOOD_WORKFLOW.replace("actions/checkout@0123456789abcdef0123456789abcdef01234567",
                                       "actions/setup-python@v5"), "uses:"),
    "unpinned checkout": (".github/workflows/ci.yml",
                          GOOD_WORKFLOW.replace("0123456789abcdef0123456789abcdef01234567",
                                                "v5"), "uses:"),
    "missing permissions": (".github/workflows/ci.yml",
                            GOOD_WORKFLOW.replace("permissions: {}\n", ""), "permissions"),
    "write permissions": (".github/workflows/ci.yml",
                          GOOD_WORKFLOW.replace("contents: read", "contents: write"),
                          "write permission"),
    "inputs in run": (".github/workflows/ci.yml",
                      GOOD_WORKFLOW.replace('echo "$FOLDER"', "echo ${{ inputs.folder }}"),
                      "inside run"),
    "secrets in inline run": (".github/workflows/ci.yml",
                              GOOD_WORKFLOW.replace("      - name: t\n",
                                                    "      - run: echo ${{secrets.X}}\n"
                                                    "      - name: t\n"), "inside run"),
    "event in run": (".github/workflows/ci.yml",
                     GOOD_WORKFLOW.replace('echo "$FOLDER"',
                                           'echo "${{ github.event.issue.title }}"'),
                     "inside run"),
    "pull_request_target": (".github/workflows/ci.yml",
                            GOOD_WORKFLOW.replace("  pull_request:", "  pull_request_target:"),
                            "trigger"),
    "schedule": (".github/workflows/ci.yml",
                 GOOD_WORKFLOW.replace("  pull_request:", "  schedule:\n    - cron: '0 0 * * *'"),
                 "trigger"),
    "pull_request outside ci": (".github/workflows/other.yml", GOOD_WORKFLOW, "trigger"),
    "no persist-credentials": (".github/workflows/ci.yml",
                               GOOD_WORKFLOW.replace("persist-credentials: false",
                                                     "fetch-depth: 1"),
                               "persist-credentials"),
    "set -x": (".github/workflows/ci.yml",
               GOOD_WORKFLOW.replace("uv run pytest -q", "set -eux"), "set -x"),
    "cache": (".github/workflows/ci.yml",
              GOOD_WORKFLOW.replace("      - name: t\n",
                                    "      - uses: actions/cache@" + "ab" * 20 + "\n"
                                    "      - name: t\n"), "cache"),
    "id-token": (".github/workflows/ci.yml",
                 GOOD_WORKFLOW.replace("contents: read", "contents: read\n      id-token: none"),
                 "id-token"),
    "secrets inherit": (".github/workflows/ci.yml",
                        GOOD_WORKFLOW + "  call:\n    uses: ./.github/workflows/x.yml\n"
                        "    secrets: inherit\n", "secrets: inherit"),
    "rclone verb in shell": ("tools/run.sh", "rclone --config c.conf " + "purge gp:x\n",
                             "rclone verb"),
    "rclone verb in workflow": (".github/workflows/ci.yml",
                                GOOD_WORKFLOW.replace("uv run pytest -q",
                                                      "rclone " + "delete gp:x"), "rclone verb"),
    "rclone method": ("src/pkg/bad.py", "def f(self):\n    self.rclone." + "purge('gp:x')\n",
                      "rclone verb"),
    "rclone runner": ("src/pkg/bad2.py", "rc._run(" + "'sync', ['a', 'b'])\n", "rclone verb"),
    "rclone argv": ("src/pkg/bad3.py", "cmd = ['rclone', " + "'moveto', a, b]\n",
                    "rclone verb"),
    # ----- review round 2: forms the first lint missed -----
    "expr in block comment": (".github/workflows/ci.yml", _wf_run("# ${{ inputs.folder }}"),
                              "inside run"),
    "expr in trailing comment": (".github/workflows/ci.yml",
                                 _wf_run('echo "$FOLDER" # ${{ inputs.folder }}'),
                                 "inside run"),
    "expr in inline run comment": (".github/workflows/ci.yml",
                                   _wf_step("- run: pytest -q # ${{ inputs.folder }}"),
                                   "inside run"),
    "format() in run": (".github/workflows/ci.yml",
                        _wf_run("echo ${{ format('{0}', inputs.folder) }}"), "inside run"),
    "toJSON in run": (".github/workflows/ci.yml", _wf_run("echo '${{ toJSON(github) }}'"),
                      "inside run"),
    "github index in run": (".github/workflows/ci.yml",
                            _wf_run("echo ${{ github['event'].issue.title }}"), "inside run"),
    "head_ref in run": (".github/workflows/ci.yml", _wf_run("git log ${{ github.head_ref }}"),
                        "inside run"),
    "env expr in run": (".github/workflows/ci.yml", _wf_run("echo ${{ env.FOLDER }}"),
                        "inside run"),
    "quoted run key": (".github/workflows/ci.yml",
                       _wf_step('- "run": echo ${{ inputs.folder }}'), "inside run"),
    "flow uses": (".github/workflows/ci.yml", _wf_step("- {uses: evil/act@v1}"), "uses:"),
    "quoted uses": (".github/workflows/ci.yml", _wf_step('- "uses": evil/act@v1'), "uses:"),
    "flow write permission": (".github/workflows/ci.yml",
                              GOOD_WORKFLOW.replace("    permissions:\n      contents: read",
                                                    "    permissions: {contents: write}"),
                              "write permission"),
    "quoted write permission": (".github/workflows/ci.yml",
                                GOOD_WORKFLOW.replace("contents: read",
                                                      'contents: read\n      pull-requests: '
                                                      '"write"'), "write permission"),
    "shell bash -x": (".github/workflows/ci.yml",
                      _wf_step("- shell: bash -x {0}\n        run: echo hi"), "set -x"),
    "bash -x script": (".github/workflows/ci.yml", _wf_run("bash -ex tools/x.sh"), "set -x"),
    "rclone multi-line argv": ("tools/bad.py", "subprocess.run(\n    ['rclone',\n     "
                               + "'pur" + "ge', 'gp:x'])\n", "rclone verb"),
    "rclone flags before verb": ("tools/bad2.py", "cmd = ['rclone', '--config', c, "
                                 + "'pur" + "ge', x]\n", "rclone verb"),
    "rclone f-string": ("tools/bad3.py", "cmd = f'rclone {flags} " + "pur" + "ge gp:x'\n",
                        "rclone verb"),
    "rclone shell continuation": ("tools/run.sh", "rclone --config c \\\n  " + "pur"
                                  + "ge gp:x\n", "rclone verb"),
    "rclone ps continuation": ("tools/run.ps1", "rclone --config $c `\r\n  " + "pur"
                               + "ge gp:x\r\n", "rclone verb"),
    "rclone outside wrapper": ("src/pkg/direct.py",
                               "subprocess.run(['rclone', 'lsjson', 'gp:'])\n",
                               "use gpclean.rclone.Rclone"),
    # ----- review round 3: flow run:, shell:, containers, RCLONE_* env -----
    "flow run": (".github/workflows/ci.yml",
                 _wf_step('- {name: leak, run: "echo ${{ github.event.pull_request.title }}"}'),
                 "inside run"),
    "flow run later key": (".github/workflows/ci.yml",
                           _wf_step("- {name: leak, shell: bash, run: echo ${{ inputs.folder }}}"),
                           "inside run"),
    "shell expr": (".github/workflows/ci.yml",
                   _wf_step('- shell: "${{ github.head_ref }} {0}"\n        run: echo hi'),
                   "inside shell"),
    "flow shell expr": (".github/workflows/ci.yml",
                        _wf_step('- {shell: "${{ inputs.folder }} {0}", run: echo hi}'),
                        "inside shell"),
    "run alias": (".github/workflows/ci.yml", _wf_step("- run: *script"), "YAML alias"),
    "container": (".github/workflows/ci.yml",
                  GOOD_WORKFLOW.replace("    runs-on: ubuntu-24.04\n",
                                        "    runs-on: ubuntu-24.04\n    container: evil/image\n"),
                  "container/services"),
    "services": (".github/workflows/ci.yml",
                 GOOD_WORKFLOW.replace("    runs-on: ubuntu-24.04\n",
                                       "    runs-on: ubuntu-24.04\n    services:\n"
                                       "      db:\n        image: evil/db\n"),
                 "container/services"),
    "flow services": (".github/workflows/ci.yml",
                      GOOD_WORKFLOW.replace("  test:\n", "  other: {runs-on: x, services: {}}\n"
                                            "  test:\n"), "container/services"),
    "RCLONE_DUMP env": (".github/workflows/ci.yml",
                        GOOD_WORKFLOW.replace("          FOLDER:",
                                              "          RCLONE_DUMP: headers\n"
                                              "          FOLDER:"), "RCLONE_* flag"),
    "RCLONE_DUMP to GITHUB_ENV": (".github/workflows/ci.yml",
                                  _wf_run('echo "RCLONE_DUMP=headers" >> "$GITHUB_ENV"'),
                                  "RCLONE_* flag"),
    "netrc": (".netrc", "machine x login y password z\n", "dotfile"),
    "DS_Store": ("photos/.DS_Store", "x\n", "dotfile"),
    "data uri image": ("docs/a.md", "![x](data:image/" + "jpeg;base64,AAAA)\n",
                       "embedded image"),
    "base64 jpeg": ("src/site/static/app.js", "const a = '/9j/4AAQ" + "SkZJRgABAQ';\n",
                    "embedded image"),
    "email as url userinfo": ("docs/a.md", "see https://john.smith" + "@" + "gmail.com\n",
                              "e-mail"),
}


@pytest.mark.parametrize("name", sorted(PLANTS))
def test_planted_violation_fails(name, tmp_path):
    rel, content, reason = PLANTS[name]
    result = run_checker(make_repo(tmp_path / "r", {rel: content}))
    assert result.returncode == 1, result.stdout
    lines = [line for line in result.stdout.splitlines() if line.startswith(rel + ":")]
    assert lines, result.stdout
    assert any(reason in line for line in lines), result.stdout
    # the checker never echoes file content
    text = content if isinstance(content, str) else content.decode("latin-1")
    for secret in (FAKE_TOKEN, REAL_ID, "someone@", "r" * 20):
        if secret in text:
            assert secret not in result.stdout + result.stderr


ALLOWED = [
    ("tests/t.py", "ID = 'AF1QipFAKE" + "x" * 30 + "'\n"),
    ("docs/a.md", "mail: someone@example.com, 1+x@users.noreply.github.com, "
                  "noreply@anthropic.com\n"),
    ("tests/u.py", "URL = 'https://user@photos.google.com/photo/abc'\n"),
    ("src/pkg/ok.py", '"""Reads the rclone config file and runs rclone lsjson."""\n'
                      "rc.copyto(a, b)\nself.rclone.mkdir(x)\nPath(p).touch()\n"),
    ("tools/ok.sh", "rclone copyto a gp:gpclean-output/x  # never sync here\n"),
    ("tools/refresh-secret.ps1", "rclone config reconnect gp: --config $cfg\n"),
    (".githooks/pre-commit", "#!/bin/sh\nexec python tools/check_repo.py\n"),
    ("src/site/static/app.js", "console.log(1);\n"),
    ("uv.lock", "x" * (300 * 1024)),
    ("docs/b.md", "clone with git clone git" + "@github.com:owner/repo.git\n"),
    (".gitattributes", "* text=auto eol=lf\n"),
    ("src/site/static/icon.css", "a { background: url(data:image/svg+xml;base64,PHN2Zz4=); }\n"),
    (".github/workflows/ci.yml", _wf_run("echo shard ${{ matrix.shard }}")),
    # an env: block that follows "- run:" in the same step is not part of the run value
    (".github/workflows/ci.yml", _wf_step(
        "- run: echo \"$X\"\n        env:\n          X: ${{ inputs.folder }}")),
    # the config file location and the secret that carries it are the allowed RCLONE_* names
    (".github/workflows/ci.yml", GOOD_WORKFLOW.replace(
        "          FOLDER:", "          RCLONE_CONFIG_B64: ${{ secrets.RCLONE_CONFIG_B64 }}\n"
        "          FOLDER:").replace(
        "          uv run pytest -q\n",
        "          uv run pytest -q\n"
        '          echo "RCLONE_CONFIG=$RUNNER_TEMP/c" >> "$GITHUB_ENV"\n')),
    # a matrix value in a flow run: and a plain shell: are fine
    (".github/workflows/ci.yml",
     _wf_step("- {name: s, shell: bash, run: echo ${{ matrix.shard }}}")),
    (".github/workflows/scan-pass.yml", GOOD_WORKFLOW.replace(
        "  push:\n  pull_request:\n", "  workflow_call:\n    outputs:\n      done:\n"
        "        value: ${{ jobs.test.outputs.done }}\n")),
]


@pytest.mark.parametrize("rel,content", ALLOWED, ids=[rel for rel, _ in ALLOWED])
def test_allowed_content_passes(rel, content, tmp_path):
    result = run_checker(make_repo(tmp_path / "r", {rel: content}))
    assert result.returncode == 0, result.stdout


def test_trigger_forms():
    lines = ["on: [push, workflow_dispatch]"]
    assert check_repo._triggers(lines) == ["push", "workflow_dispatch"]
    assert check_repo._triggers(["on: push"]) == ["push"]
    assert check_repo._triggers(["name: x"]) is None
    block = ["on:", "  workflow_dispatch:", "    inputs:", "      folder:", "jobs:"]
    assert check_repo._triggers(block) == ["workflow_dispatch"]


def test_only_index_content_is_checked(tmp_path):
    # Untracked files are not published, so they are not checked...
    root = make_repo(tmp_path / "r", {})
    (root / "stray.jpg").write_bytes(b"\xff\xd8\xff")
    assert run_checker(root).returncode == 0
    # ...but staged content is, even if the working copy was cleaned afterwards.
    (root / "notes.txt").write_bytes(b"\xff\xd8\xff")
    _git(root, "add", "-f", "notes.txt")
    (root / "notes.txt").write_bytes(b"clean\n")
    assert run_checker(root).returncode == 1


@pytest.mark.skipif(os.name == "nt" or shutil.which("sh") is None, reason="POSIX sh hook test")
def test_pre_commit_hook_blocks_bad_commit(tmp_path):
    root = make_repo(tmp_path / "r", {})
    (root / "tools").mkdir(exist_ok=True)
    shutil.copy(CHECKER, root / "tools" / "check_repo.py")
    (root / ".githooks").mkdir(exist_ok=True)
    shutil.copy(REPO / ".githooks" / "pre-commit", root / ".githooks" / "pre-commit")
    (root / ".githooks" / "pre-commit").chmod(0o755)
    _git(root, "config", "core.hooksPath", ".githooks")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "add", "-A", "-f")
    ok = subprocess.run(["git", "commit", "-q", "-m", "clean"], cwd=root, capture_output=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    (root / "leak.txt").write_bytes(b"SQLite format 3\x00")
    _git(root, "add", "-f", "leak.txt")
    bad = subprocess.run(["git", "commit", "-q", "-m", "bad"], cwd=root, capture_output=True)
    assert bad.returncode != 0
    assert b"leak.txt" in bad.stdout + bad.stderr
