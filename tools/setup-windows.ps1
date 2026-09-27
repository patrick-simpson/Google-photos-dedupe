# gpclean one-time Windows setup.
#
# What it does (safe to run again; finished steps are skipped, and a re-run updates the app):
#   1. installs Git, GitHub CLI and rclone 1.75.1 with winget
#   2. creates C:\gpclean (private to your Windows account) and downloads the app into
#      C:\gpclean\app (git clone; a re-run does git pull)
#   3. installs uv 0.8.24 into C:\gpclean\bin from its official release zip, checked
#      against a fixed SHA-256 (tools\install_uv.ps1), then the app's Python packages
#      exactly as locked (uv sync --locked)
#   4. downloads and checks the CLIP search model (gpclean fetch-model)
#
# Why uv is pinned (not taken from winget): pyproject.toml asks for uv_build>=0.8.17,<0.9.
# A uv of another series does not use its built-in build backend, and instead downloads
# uv-build from PyPI without a hash check and runs it, on the PC that holds the Drive key.
#
# How to run it (see docs\SETUP_WINDOWS.md, Part B):
#   powershell -ExecutionPolicy Bypass -File "$env:TEMP\setup-windows.ps1"
# or, from an existing clone (this is also how the app is updated):
#   powershell -ExecutionPolicy Bypass -File C:\gpclean\app\tools\setup-windows.ps1
#
# Windows PowerShell 5.1 compatible. Plain ASCII on purpose (5.1 reads BOM-less files as ANSI).
# It never reads or prints any secret.

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Home0 = "C:\gpclean"
$AppDir = Join-Path $Home0 "app"
$BinDir = Join-Path $Home0 "bin"
$Uv = Join-Path $BinDir "uv.exe"
$UvVersion = "0.8.24"   # keep in step with tools\install_uv.ps1
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

# Make a folder private: only this Windows account, SYSTEM and the Administrators group may
# open it. A new folder directly under C:\ inherits C:\'s permissions, which let every other
# account on the PC read it (and change files in it). C:\gpclean holds the Drive key
# (ci-rclone.conf), the photo previews, and the programs Claude runs as you, so it must not.
# The folder's inherited rules are removed (/inheritance:r) and replaced by three grants that
# everything inside inherits, including files that already exist. SIDs instead of names, so
# it works on non-English Windows and with Microsoft accounts.
function Protect-Folder([string]$Path) {
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    Invoke-Checked "Making $Path private (icacls)" {
        icacls $Path /inheritance:r /grant:r "*${sid}:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" /Q | Out-Null
    }
}

# Put a folder first on this account's PATH, for new PowerShell windows. Done the same way as
# the official uv installer: the registry value keeps %VARIABLES% unexpanded, and setting and
# removing a dummy variable makes Windows tell Explorer, so new windows see the change
# without signing out.
function Add-ToUserPath([string]$Dir) {
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey("Environment", $true)
    try {
        $old = [string]$key.GetValue("Path", "", [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        $rest = @($old -split ";" | Where-Object { $_ -and ($_.TrimEnd([char]'\') -ne $Dir) })
        $new = (@($Dir) + $rest) -join ";"
        if ($new -ne $old) {
            $key.SetValue("Path", $new, [Microsoft.Win32.RegistryValueKind]::ExpandString)
            $dummy = "gpclean-" + [guid]::NewGuid().ToString("N")
            [Environment]::SetEnvironmentVariable($dummy, "1", "User")
            [Environment]::SetEnvironmentVariable($dummy, [NullString]::Value, "User")
        }
    }
    finally {
        $key.Close()
    }
}

try {
    Say "=== gpclean setup ==="
    Say "This takes about 10-20 minutes. Keep this window open until it says ALL DONE."
    Write-Host ""

    # The app's locked packages exist only for 64-bit Intel/AMD Windows (pyproject.toml,
    # [tool.uv] environments); on an ARM PC uv sync would fail late with an unclear message.
    # PROCESSOR_ARCHITEW6432 is set when this is a 32-bit PowerShell on 64-bit Windows.
    $arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
    if ($arch -ne "AMD64") {
        throw "This PC has a '$arch' processor. gpclean needs a 64-bit Intel or AMD PC (Windows on ARM is not supported)."
    }

    if (-not (Test-Command "winget")) {
        throw "winget is missing. Open the Microsoft Store, search for 'App Installer', click Get or Update, then run this setup again."
    }

    # --- 1. tools --------------------------------------------------------------------------
    Say "Step 1 of 4: installing tools"
    Install-WithWinget -Id "Git.Git" -Command "git"
    Install-WithWinget -Id "GitHub.cli" -Command "gh"
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
    Protect-Folder $Home0
    Ok "$Home0 is private to your Windows account"
    if (Test-Path (Join-Path $AppDir ".git")) {
        # An older setup used whatever uv winget had, and a uv of another version may have
        # rewritten uv.lock; that local change would block the update. The app folder is
        # never edited by hand, so put the file back as it was downloaded.
        $lockChanged = "$(git -C $AppDir status --porcelain uv.lock)".Trim()
        if ($lockChanged) {
            Warn "uv.lock was changed on this PC; restoring the downloaded version"
            Invoke-Checked "Restoring uv.lock" { git -C $AppDir checkout HEAD uv.lock }
        }
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
    Say "Step 3 of 4: installing uv $UvVersion and the app's Python packages (a few GB; this is the slow part)"
    $uvPattern = '^uv ' + [regex]::Escape($UvVersion) + '( |$)'
    $haveUv = ""
    if (Test-Path -LiteralPath $Uv) { $haveUv = "$(& $Uv --version)".Trim() }
    if ($haveUv -match $uvPattern) {
        Ok "uv $UvVersion is already installed in $BinDir"
    }
    else {
        # Official release zip, checked against a fixed SHA-256 (it prints "uv ok" or "uv fail").
        $installUv = Join-Path $AppDir "tools\install_uv.ps1"
        Invoke-Checked "Installing uv $UvVersion (check the internet connection)" {
            powershell -NoProfile -ExecutionPolicy Bypass -File $installUv -Dest $BinDir
        }
        $haveUv = "$(& $Uv --version)".Trim()
        if ($haveUv -notmatch $uvPattern) { throw "uv in $BinDir reports '$haveUv', not $UvVersion." }
        Ok "uv $UvVersion installed in $BinDir"
    }
    # New windows must find this uv, not another one, for the "uv run ..." lines in the docs.
    Add-ToUserPath $BinDir
    Update-PathFromRegistry
    $foundUv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    if ($foundUv -and ($foundUv -ne $Uv)) {
        throw "Another uv ($foundUv) is found before $BinDir. Uninstall it (for example: winget uninstall astral-sh.uv) or remove it from the system PATH, then run setup again."
    }

    Push-Location $AppDir
    try {
        Invoke-Checked "Installing packages (uv sync)" { & $Uv sync --locked --group clip --group mcp }
        Ok "packages installed"

        # --- 4. CLIP model ---------------------------------------------------------------------
        Write-Host ""
        Say "Step 4 of 4: downloading the photo-search model (about 600 MB)"
        Invoke-Checked "Downloading the model (gpclean fetch-model)" { & $Uv run --locked gpclean fetch-model --model b32 }
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
