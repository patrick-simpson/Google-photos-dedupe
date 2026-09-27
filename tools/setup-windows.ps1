# gpclean one-time Windows setup.
#
# What it does (safe to run again; finished steps are skipped):
#   1. installs Git, GitHub CLI, uv and rclone 1.75.1 with winget
#   2. creates C:\gpclean and downloads the app into C:\gpclean\app (git clone)
#   3. installs the app's Python packages exactly as locked (uv sync --locked)
#   4. downloads and checks the CLIP search model (gpclean fetch-model)
#
# How to run it (see docs\SETUP_WINDOWS.md, Part B):
#   powershell -ExecutionPolicy Bypass -File "$env:TEMP\setup-windows.ps1"
# or, from an existing clone:
#   powershell -ExecutionPolicy Bypass -File C:\gpclean\app\tools\setup-windows.ps1
#
# Windows PowerShell 5.1 compatible. Plain ASCII on purpose (5.1 reads BOM-less files as ANSI).
# It never reads or prints any secret.

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Home0 = "C:\gpclean"
$AppDir = Join-Path $Home0 "app"
$RepoUrl = "https://github.com/patrick-simpson/Google-photos-dedupe"
$RcloneVersion = "1.75.1"

function Say([string]$msg) { Write-Host $msg -ForegroundColor Cyan }
function Ok([string]$msg) { Write-Host "  OK: $msg" -ForegroundColor Green }
function Warn([string]$msg) { Write-Host "  NOTE: $msg" -ForegroundColor Yellow }

# Run a program and stop if it fails. (With "Stop", PowerShell stops on its own errors, but
# NOT when an external program such as git or uv exits with an error code, so check it here.)
function Invoke-Checked {
    param([string]$What, [scriptblock]$Command)
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed (exit code $LASTEXITCODE)."
    }
}

# After winget installs something, this window does not see it until PATH is re-read.
function Update-PathFromRegistry {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Test-Command([string]$name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

function Install-WithWinget {
    param([string]$Id, [string]$Command, [string]$Version = "")
    if (Test-Command $Command) {
        Ok "$Id is already installed"
        return
    }
    Say "Installing $Id (a Windows prompt may ask for permission: click Yes) ..."
    $wingetArgs = @("install", "--id", $Id, "--exact", "--silent",
                    "--accept-package-agreements", "--accept-source-agreements")
    if ($Version) { $wingetArgs += @("--version", $Version) }
    & winget @wingetArgs
    if ($LASTEXITCODE -ne 0) {
        throw "winget could not install $Id (exit code $LASTEXITCODE)."
    }
    Update-PathFromRegistry
    if (-not (Test-Command $Command)) {
        throw "$Id was installed, but '$Command' is not found yet. Close this window, open a new PowerShell window and run the setup again."
    }
    Ok "$Id installed"
}

try {
    Say "=== gpclean setup ==="
    Say "This takes about 10-20 minutes. Keep this window open until it says ALL DONE."
    Write-Host ""

    if (-not (Test-Command "winget")) {
        throw "winget is missing. Open the Microsoft Store, search for 'App Installer', click Get or Update, then run this setup again."
    }

    # --- 1. tools --------------------------------------------------------------------------
    Say "Step 1 of 4: installing tools"
    Install-WithWinget -Id "Git.Git" -Command "git"
    Install-WithWinget -Id "GitHub.cli" -Command "gh"
    Install-WithWinget -Id "astral-sh.uv" -Command "uv"
    # rclone is pinned to the exact version the pipeline was tested with. Match the whole
    # first line of "rclone version" so that e.g. v1.75.10 does not count as v1.75.1.
    $rclonePattern = '^rclone v' + [regex]::Escape($RcloneVersion) + '$'
    $haveRclone = ""
    if (Test-Command "rclone") {
        $haveRclone = "$(& rclone version | Select-Object -First 1)".Trim()
    }
    if ($haveRclone -match $rclonePattern) {
        Ok "rclone $RcloneVersion is already installed"
    }
    else {
        if ($haveRclone) { Warn "found '$haveRclone'; installing version $RcloneVersion instead" }
        Say "Installing rclone $RcloneVersion ..."
        & winget install --id Rclone.Rclone --exact --version $RcloneVersion --silent --force --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) { throw "winget could not install rclone $RcloneVersion (exit code $LASTEXITCODE)." }
        Update-PathFromRegistry
        if (-not (Test-Command "rclone")) {
            throw "rclone was installed, but is not found yet. Close this window, open a new PowerShell window and run the setup again."
        }
        # Check again: an rclone installed another way (scoop, choco, a manual copy) that
        # comes earlier in PATH would otherwise silently win over the pinned one.
        $haveRclone = "$(& rclone version | Select-Object -First 1)".Trim()
        if ($haveRclone -notmatch $rclonePattern) {
            $where = (Get-Command rclone).Source
            throw "Another rclone ($where, '$haveRclone') is found first; uninstall it or remove it from PATH, then run setup again."
        }
        Ok "rclone $RcloneVersion installed"
    }

    # --- 2. folders and app ------------------------------------------------------------------
    Write-Host ""
    Say "Step 2 of 4: getting the app into $AppDir"
    New-Item -ItemType Directory -Force -Path $Home0 | Out-Null
    if (Test-Path (Join-Path $AppDir ".git")) {
        Invoke-Checked "Updating the app (git pull)" { git -C $AppDir pull --ff-only }
        Ok "app updated"
    }
    elseif (Test-Path $AppDir) {
        throw "$AppDir exists but is not a git copy of the app. Rename or delete that folder, then run the setup again."
    }
    else {
        Invoke-Checked "Downloading the app (git clone)" { git clone $RepoUrl $AppDir }
        Ok "app downloaded"
    }

    # --- 3. Python packages --------------------------------------------------------------------
    Write-Host ""
    Say "Step 3 of 4: installing the app's Python packages (a few GB; this is the slow part)"
    Push-Location $AppDir
    try {
        Invoke-Checked "Installing packages (uv sync)" { uv sync --locked --group clip --group mcp }
        Ok "packages installed"

        # --- 4. CLIP model ---------------------------------------------------------------------
        Write-Host ""
        Say "Step 4 of 4: downloading the photo-search model (about 600 MB)"
        Invoke-Checked "Downloading the model (gpclean fetch-model)" { uv run gpclean fetch-model --model b32 }
        Ok "model downloaded and checked"
    }
    finally {
        Pop-Location
    }

    Write-Host ""
    Write-Host "ALL DONE." -ForegroundColor Green
    Write-Host ""
    Write-Host "Next steps:"
    Write-Host "  1. Close this window and open a NEW PowerShell window (so it sees the new tools)."
    Write-Host "  2. Go back to docs\SETUP_WINDOWS.md and continue with Part C (connect Google Drive)."
    Write-Host "     The file is also here on your PC: $AppDir\docs\SETUP_WINDOWS.md"
    exit 0
}
catch {
    Write-Host ""
    Write-Host "SETUP STOPPED: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Nothing is broken. Fix the problem above (or see Troubleshooting in docs\SETUP_WINDOWS.md), then run the same command again." -ForegroundColor Yellow
    exit 1
}
