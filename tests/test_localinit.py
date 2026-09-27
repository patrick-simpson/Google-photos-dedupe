"""Tests for gpclean.localinit (init / mcp-config / verify-bundle) and the Windows helpers.

The PowerShell scripts and the user docs are checked as text here (safety rules that must
never regress); when a PowerShell is installed (always on the Windows CI runner) the
scripts are also parsed with PowerShell's own parser to catch syntax errors.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from bundle_factory import make_bundle

from gpclean import localinit
from gpclean.cli import main
from gpclean.localinit import (REVIEW_SETTINGS, check_bundle_files, cli_init, cli_mcp_config,
                               cli_verify_bundle, current_bundle, gpclean_executable,
                               home_paths, load_manifest, mcp_snippets)
from gpclean.review_db import ReviewDB

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = ["setup-windows.ps1", "get-bundle.ps1", "refresh-secret.ps1"]
DOCS = ["README.md", "docs/SETUP_WINDOWS.md", "docs/MCP_GUIDE.md", "docs/GITHUB_SETTINGS.md"]


@pytest.fixture
def bundle(tmp_path):
    return make_bundle(tmp_path, n_items=30)


# ------------------------------------------------------------------------------- init

def test_init_writes_config_settings_claude_md_and_db(tmp_path, bundle, capsys):
    home = tmp_path / "home"
    assert cli_init(home, bundle) == 0
    paths = home_paths(home)

    cfg = json.loads(paths["config_json"].read_text(encoding="utf-8"))
    assert cfg == {"bundle": str(bundle.resolve())}
    assert paths["logs_dir"].is_dir()
    assert paths["review_db"].is_file()
    with ReviewDB(paths["review_db"]) as db:        # opens cleanly, empty queue
        assert db.list("all") == []

    settings = json.loads((paths["review_dir"] / ".claude" / "settings.json")
                          .read_text(encoding="utf-8"))
    perms = settings["permissions"]
    assert perms["allow"] == ["mcp__gpclean__*"]
    for tool in ("WebFetch", "WebSearch", "Bash", "Write", "Edit", "NotebookEdit"):
        assert tool in perms["deny"]
    assert not any(t.startswith("mcp__gpclean") for t in perms["deny"])
    assert "mcp__claude_ai_*" in perms["deny"]      # claude.ai connectors (Gmail, ...) off
    assert settings == REVIEW_SETTINGS

    claude_md = (paths["review_dir"] / "CLAUDE.md").read_text(encoding="utf-8")
    assert "propose" in claude_md.lower()
    assert "not instructions" in claude_md
    assert "serve --home" in capsys.readouterr().out


def test_init_is_idempotent_and_restores_edited_settings(tmp_path, bundle):
    home = tmp_path / "home"
    assert cli_init(home, bundle) == 0
    settings = home_paths(home)["review_dir"] / ".claude" / "settings.json"
    settings.write_text('{"permissions": {"allow": ["Bash"]}}', encoding="utf-8")
    assert cli_init(home, bundle) == 0
    assert json.loads(settings.read_text(encoding="utf-8")) == REVIEW_SETTINGS
    assert not list(settings.parent.glob("*.tmp"))   # atomic writes leave nothing behind


def test_init_switches_bundle_and_keeps_queue(tmp_path, bundle):
    home = tmp_path / "home"
    assert cli_init(home, bundle) == 0
    with ReviewDB(home_paths(home)["review_db"]) as db:
        db.user_add([("g:AF1QipFAKE" + "1" * 30, "manual pick", None)])
    second = make_bundle(tmp_path, n_items=10, name="bundle2")
    assert cli_init(home, second) == 0
    assert current_bundle(home) == second.resolve()
    with ReviewDB(home_paths(home)["review_db"]) as db:
        assert len(db.list("all")) == 1


def test_init_refuses_incomplete_bundle(tmp_path, bundle, capsys):
    home = tmp_path / "home"
    next(bundle.glob("thumbs/*.sqlite")).unlink()
    assert cli_init(home, bundle) == 1
    assert "missing" in capsys.readouterr().out
    assert not home_paths(home)["config_json"].exists()


def test_init_refuses_file_with_wrong_size(tmp_path, bundle, capsys):
    home = tmp_path / "home"
    npy = bundle / "embeddings.f16.npy"
    npy.write_bytes(npy.read_bytes()[:100])        # an interrupted download
    assert cli_init(home, bundle) == 1
    assert "wrong size" in capsys.readouterr().out
    assert not home_paths(home)["config_json"].exists()


def test_init_refuses_wrong_index_schema(tmp_path, bundle, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setattr("gpclean.version.INDEX_SCHEMA", 999)
    assert cli_init(home, bundle) == 1
    assert "schema version" in capsys.readouterr().out
    assert not home_paths(home)["config_json"].exists()


def test_manifest_without_index_entry_fails(bundle):
    manifest = load_manifest(bundle)
    manifest["files"] = [e for e in manifest["files"] if e["path"] != "index.sqlite"]
    check = check_bundle_files(bundle, manifest, full=False)
    assert not check.passed
    assert any("index.sqlite" in msg for msg in check.errors)


def test_unknown_size_is_checked_by_hash_only(bundle):
    """A manifest entry with "size": null (size unknown) is not treated as damaged."""
    manifest = load_manifest(bundle)
    for entry in manifest["files"]:
        entry["size"] = None
    assert check_bundle_files(bundle, manifest, full=False).passed
    assert check_bundle_files(bundle, manifest, full=True).passed
    pack = next(bundle.glob("thumbs/*.sqlite"))
    pack.write_bytes(pack.read_bytes()[:100])
    check = check_bundle_files(bundle, manifest, full=True)
    assert not check.passed and pack.relative_to(bundle).as_posix() in check.bad


def test_init_refuses_bundle_without_manifest(tmp_path):
    b = make_bundle(tmp_path, n_items=5, with_manifest=False)
    assert cli_init(tmp_path / "home", b) == 1


def test_init_via_cli(tmp_path, bundle):
    home = tmp_path / "home"
    assert main(["init", "--home", str(home), "--bundle", str(bundle)]) == 0
    assert current_bundle(home) == bundle.resolve()


# ---------------------------------------------------------------------- current_bundle

def test_current_bundle_round_trip(tmp_path, bundle):
    home = tmp_path / "home"
    with pytest.raises(FileNotFoundError, match="gpclean init"):
        current_bundle(home)
    assert cli_init(home, bundle) == 0
    assert current_bundle(home) == bundle.resolve()
    # A bundle folder that has since been deleted gives a clear error, not a crash later.
    shutil.rmtree(bundle)
    with pytest.raises(FileNotFoundError, match="index.sqlite"):
        current_bundle(home)


# ---------------------------------------------------------------------- verify-bundle

def test_verify_ok(bundle, capsys):
    assert cli_verify_bundle(bundle) == 0
    out = capsys.readouterr().out
    n = len(load_manifest(bundle)["files"])
    assert f"ok: {n}" in out and "missing: 0" in out and "damaged: 0" in out


def test_verify_detects_corruption_with_same_size(bundle, capsys):
    pack = next(bundle.glob("thumbs/*.sqlite"))
    data = bytearray(pack.read_bytes())
    data[-10] ^= 0xFF                              # same size, different content
    pack.write_bytes(bytes(data))
    assert cli_verify_bundle(bundle) == 1
    out = capsys.readouterr().out
    assert "damaged: 1" in out and pack.name in out
    # init only checks sizes, so it accepts this; verify-bundle is the one that catches it.
    check = check_bundle_files(bundle, load_manifest(bundle), full=False)
    assert check.passed


def test_verify_detects_truncated_file(bundle, capsys):
    npy = bundle / "embeddings.f16.npy"
    npy.write_bytes(npy.read_bytes()[:100])
    assert cli_verify_bundle(bundle) == 1
    assert "damaged: 1" in capsys.readouterr().out


def test_verify_detects_missing_file(bundle, capsys):
    (bundle / "embeddings.f16.npy").unlink()
    assert cli_verify_bundle(bundle) == 1
    out = capsys.readouterr().out
    assert "missing: 1" in out and "embeddings.f16.npy" in out


def test_verify_rejects_unsafe_manifest_paths(bundle, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("x", encoding="utf-8")
    manifest = load_manifest(bundle)
    manifest["files"].append({"path": "../secret.txt", "size": 1, "sha256": "0" * 64})
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    check = check_bundle_files(bundle, manifest, full=True)
    assert check.errors and "../secret.txt" not in check.ok
    assert cli_verify_bundle(bundle) == 1


def test_verify_wrong_schema_version(bundle, monkeypatch, capsys):
    monkeypatch.setattr("gpclean.version.INDEX_SCHEMA", 999)
    assert cli_verify_bundle(bundle) == 1
    out = capsys.readouterr().out
    assert "schema version" in out
    # The fix is a command the docs give, not "git pull".
    assert r"C:\gpclean\app\tools\setup-windows.ps1" in out and "git pull" not in out


def test_verify_missing_manifest(tmp_path):
    b = make_bundle(tmp_path, n_items=5, with_manifest=False)
    assert cli_verify_bundle(b) == 1


def test_verify_partial_bundle_passes_with_note(tmp_path, capsys):
    b = make_bundle(tmp_path, n_items=5, partial=True)
    assert cli_verify_bundle(b) == 0
    assert "PARTIAL" in capsys.readouterr().out


# ------------------------------------------------------------------------- mcp-config

def test_mcp_snippets_windows_paths_are_escaped():
    home = r"C:\gpclean"
    exe = gpclean_executable(r"C:\gpclean\app\.venv\Scripts\python.exe", windows=True)
    assert exe == r"C:\gpclean\app\.venv\Scripts\gpclean.exe"
    cmd, desktop = mcp_snippets(home, exe, windows=True)
    assert cmd == ("claude mcp add gpclean --scope local -e HF_HUB_OFFLINE=1 -- "
                   r"'C:\gpclean\app\.venv\Scripts\gpclean.exe' mcp --home 'C:\gpclean'")
    # Backslashes are doubled in the JSON text and round-trip exactly.
    assert r'"C:\\gpclean\\app\\.venv\\Scripts\\gpclean.exe"' in desktop
    data = json.loads(desktop)
    server = data["mcpServers"]["gpclean"]
    assert server == {"command": exe, "args": ["mcp", "--home", home],
                      "env": {"HF_HUB_OFFLINE": "1"}}


def test_mcp_snippets_quote_odd_paths():
    cmd, _ = mcp_snippets(r"C:\Users\Pat O'Neil\gp", r"C:\x\gpclean.exe", windows=True)
    assert r"'C:\Users\Pat O''Neil\gp'" in cmd
    cmd, _ = mcp_snippets("/home/pat/my photos", "/v/bin/gpclean", windows=False)
    assert cmd.endswith("-- /v/bin/gpclean mcp --home '/home/pat/my photos'")


def test_gpclean_executable_posix():
    assert gpclean_executable("/x/.venv/bin/python", windows=False) == "/x/.venv/bin/gpclean"


def test_cli_mcp_config_prints_valid_json(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(localinit, "_is_windows", lambda: True)
    monkeypatch.setattr(localinit.sys, "executable",
                        r"C:\gpclean\app\.venv\Scripts\python.exe")
    assert cli_mcp_config(tmp_path / "home") == 0
    out = capsys.readouterr().out
    assert "claude mcp add gpclean --scope local -e HF_HUB_OFFLINE=1 -- " in out
    assert "Settings > Developer > Edit Config" in out
    assert "+ button" in out and "Connectors" in out
    block = out[out.index('{\n  "mcpServers"'):]
    block = block[:block.index("\n}\n") + 2]
    data = json.loads(block)
    assert data["mcpServers"]["gpclean"]["command"] == r"C:\gpclean\app\.venv\Scripts\gpclean.exe"
    assert data["mcpServers"]["gpclean"]["args"][:2] == ["mcp", "--home"]
    assert data["mcpServers"]["gpclean"]["env"] == {"HF_HUB_OFFLINE": "1"}


# --------------------------------------------------------------------- Windows scripts

def _script(name: str) -> str:
    return (REPO / "tools" / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", SCRIPTS)
def test_scripts_stop_on_errors_and_are_safe(name):
    text = _script(name)
    assert '$ErrorActionPreference = "Stop"' in text
    assert not re.search(r"rclone(\.exe)?\s+sync\b", text)
    assert "--dump" not in text and "-vv" not in text
    # Windows PowerShell 5.1 reads BOM-less scripts as ANSI: keep them plain ASCII.
    assert text.isascii()


def test_setup_pins_rclone_version():
    text = _script("setup-windows.ps1")
    assert "Rclone.Rclone" in text
    assert re.search(r"--version\s+\$RcloneVersion", text)
    assert '$RcloneVersion = "1.75.1"' in text
    assert "$Uv sync --locked --group clip --group mcp" in text
    assert "https://github.com/patrick-simpson/Google-photos-dedupe" in text


def test_setup_installs_the_hash_pinned_uv_not_winget():
    """A uv outside 0.8.x would fetch an unhashed uv-build from PyPI (pyproject's
    uv_build<0.9), so the PC must use tools/install_uv.ps1, like CI."""
    text = _script("setup-windows.ps1")
    assert "astral-sh.uv\" -Command" not in text          # no Install-WithWinget for uv
    assert not re.search(r"winget\s+install[^\n]*astral-sh\.uv", text)
    assert "tools\\install_uv.ps1" in text and "-Dest $BinDir" in text
    pinned = re.search(r"\$UvVersion = '([\d.]+)'", _script("install_uv.ps1")).group(1)
    assert f'$UvVersion = "{pinned}"' in text             # same pin as install_uv.ps1
    assert pinned.startswith("0.8.")
    # Every uv call in the setup uses the pinned binary, never whatever "uv" is on PATH.
    code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    assert not [ln for ln in code if re.search(r"\{\s*uv\s", ln)]
    assert "Add-ToUserPath $BinDir" in text


def test_setup_makes_home_private_and_refuses_arm():
    text = _script("setup-windows.ps1")
    # icacls by SID (works on non-English Windows): the user, SYSTEM, Administrators only.
    assert "/inheritance:r" in text
    for sid in ("*${sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"):
        assert sid in text
    assert text.index("Protect-Folder $Home0") < text.index("git clone $RepoUrl")
    assert "PROCESSOR_ARCHITEW6432" in text and '$arch -ne "AMD64"' in text


def test_get_bundle_copies_never_syncs():
    text = _script("get-bundle.ps1")
    assert re.search(r"rclone copy ", text)
    assert "verify-bundle" in text and "init" in text
    assert r"C:\gpclean\ci-rclone.conf" in text


def test_get_bundle_downloads_only_manifest_files_and_skips_selftests():
    text = _script("get-bundle.ps1")
    # Only what the manifest lists (no stale thumb packs), the manifest itself last.
    copies = re.findall(r"& rclone copy [^\n]*", text)
    assert copies and all("--files-from $listFile" in c for c in copies)
    assert text.index("manifest.json.part") < text.index("--files-from $listFile")
    assert text.index("--files-from $listFile") < text.index(
        'Move-Item -LiteralPath $manifestPart')
    # A path from the Drive manifest must stay inside the new folder.
    assert r"'(^|/)\.\.?(/|$)'" in text
    # Selftest bundles: marked in the manifest, or their shards.json lists a selftest zip.
    assert '$Manifest.mode -eq "selftest"' in text
    assert "$Root/state/$Name/shards.json" in text and "$Root/selftest" in text
    assert "if ($isSelftest -and -not $Cfg)" in text
    # "$name:" inside a PowerShell string is a scope-qualified variable, not text.
    assert not re.search(r'"[^"\n]*\$(candidate|name|chosen):', text)


def test_refresh_secret_script():
    text = _script("refresh-secret.ps1")
    assert "config reconnect gp:" in text
    assert "gh secret set RCLONE_CONFIG_B64 --env photos" in text
    assert "probe" in text


def test_refresh_secret_checks_the_token_really_changed():
    """rclone exits 0 when "replace it?" is answered n, so the script compares the token."""
    text = _script("refresh-secret.ps1")
    assert "Token already configured - replace it?" in text
    assert text.index("$before = ") < text.index("config reconnect gp:") < text.index("$after = ")
    assert "($after -ne $before)" in text and "NOT renewed" in text
    assert text.index("if (-not $renewed)") < text.index("gh secret set")
    # The token lines are never written anywhere.
    assert not re.search(r"Write-(Host|Output)[^\n]*\$(before|after)", text)


def test_scripts_pass_repo_hygiene_checks():
    """The same checks tools/check_repo.py applies once the files are tracked."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("check_repo", REPO / "tools" / "check_repo.py")
    check_repo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check_repo)
    rels = [f"tools/{n}" for n in SCRIPTS] + DOCS + ["src/gpclean/localinit.py",
                                                     "tests/test_localinit.py"]
    for rel in rels:
        data = (REPO / rel).read_bytes()
        assert check_repo.check_file(rel, "100644", data) == [], rel


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


@pytest.mark.skipif(_powershell() is None, reason="no PowerShell installed")
@pytest.mark.parametrize("name", SCRIPTS)
def test_scripts_parse_in_powershell(name):
    path = str(REPO / "tools" / name).replace("'", "''")
    probe = ("$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
             f"'{path}', [ref]$null, [ref]$e); if ($e) {{ $e | ForEach-Object "
             "{ $_.Message }; exit 1 }")
    proc = subprocess.run([_powershell(), "-NoProfile", "-NonInteractive", "-Command", probe],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---------------------------------------------------------------------------- docs

@pytest.mark.parametrize("rel", DOCS)
def test_docs_trash_is_30_days(rel):
    text = (REPO / rel).read_text(encoding="utf-8")
    assert not re.search(r"\b60[ -]days?\b", text, re.I)
    assert not re.search(r"^\s*type\s*=\s*drive", text, re.M)


def test_readme_cleanup_order_and_trash():
    text = (REPO / "README.md").read_text(encoding="utf-8")
    assert "30 days" in text
    # Numbered in the order to follow: the offline copy (Step 3) comes before the
    # irreversible Storage saver conversion (Step 4).
    steps = [text.index(f"**Step {n}.") for n in range(1, 6)]
    assert steps == sorted(steps)
    assert text.index("offline copy") < text.index("Storage saver")
    assert not re.search(r"\*\*\([a-e]\)", text)
    for link in ("docs/SETUP_WINDOWS.md", "docs/MCP_GUIDE.md", "docs/GITHUB_SETTINGS.md"):
        assert link in text


def test_mcp_guide_examples():
    text = (REPO / "docs/MCP_GUIDE.md").read_text(encoding="utf-8")
    assert "find screenshots of text conversations older than 2022" in text
    assert "show me blurry photos from 2019 and queue the ones that are clearly accidental" in text
    examples = re.findall(r'^\s*\d+\.\s+"', text, re.M)
    assert len(examples) >= 12


def test_docs_name_the_project_chat_and_cover_updates():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    setup = (REPO / "docs/SETUP_WINDOWS.md").read_text(encoding="utf-8")
    assert "## Two Claudes" in readme and "https://claude.ai/code" in readme
    assert "project chat" in setup
    # Every "tell Claude" in the setup guide says which chat (or the guide's glossary does).
    for m in re.finditer(r"[Tt]ell Claude", setup):
        window = setup[m.start():m.start() + 60]
        assert "project chat" in window or "**Tell Claude**" in setup[m.start() - 2:m.end() + 2], window
    assert "## Updating the app (when Claude asks you to)" in setup
    assert r"C:\gpclean\app\tools\setup-windows.ps1" in setup
    assert "-Cfg" in setup
    assert "Token already configured - replace it?" in setup
    assert "'Tls12'" in setup and "Paste anyway" in setup
    assert "not ARM" in setup and "not ARM" in readme


def test_docs_never_install_uv_from_winget():
    for rel in DOCS + ["docs/PLAN.md"]:
        text = (REPO / rel).read_text(encoding="utf-8")
        assert not re.search(r"winget install[^\n`]*astral-sh\.uv", text), rel


def test_mcp_guide_uses_current_desktop_menu_names():
    text = (REPO / "docs/MCP_GUIDE.md").read_text(encoding="utf-8")
    assert "**Connectors**" in text and "**+**" in text
    assert "under **Desktop app**, click **Developer**" in text
    # "Search and tools" survives only as the older name.
    for m in re.finditer(r"Search and tools", text):
        assert "older versions" in text[max(0, m.start() - 60):m.start()].lower()
