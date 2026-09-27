# Download the newest review bundle from Google Drive and switch gpclean to it.
#
# What it does:
#   1. finds the newest gpclean-output/bundle/<cfg> folder on your Drive (by its manifest)
#   2. copies it into a NEW folder C:\gpclean\bundle\<cfg>-<yyyyMMdd-HHmm>
#      (rclone copy only: it never changes or removes anything on Drive or on your PC)
#   3. checks every file (gpclean verify-bundle)
#   4. points gpclean at it (gpclean init)
#
# Run it with:
#   powershell -ExecutionPolicy Bypass -File C:\gpclean\app\tools\get-bundle.ps1
# Optional: -Cfg <name> to download a specific bundle instead of the newest one.
#
# Windows PowerShell 5.1 compatible, plain ASCII. It never prints the Drive token.

param(
    [string]$Cfg = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Home0 = "C:\gpclean"
$Conf = "C:\gpclean\ci-rclone.conf"
$Gpclean = "C:\gpclean\app\.venv\Scripts\gpclean.exe"
$Remote = "gp:gpclean-output/bundle"

function Say([string]$msg) { Write-Host $msg -ForegroundColor Cyan }

# A sortable UTC text for an lsjson ModTime. Windows PowerShell 5.1 gives the raw string
# (rclone writes up to 9 decimals; .NET parses at most 7), PowerShell 7 already a DateTime.
function Get-SortKey($t) {
    if ($t -is [datetime]) { return $t.ToUniversalTime().ToString("o") }
    $s = ([string]$t) -replace '(\.\d{7})\d+', '$1'
    return ([datetimeoffset]::Parse($s, [Globalization.CultureInfo]::InvariantCulture)).UtcDateTime.ToString("o")
}

try {
    if (-not (Test-Path $Conf)) {
        throw "$Conf is missing. Do Part C of docs\SETUP_WINDOWS.md first (connect Google Drive)."
    }
    if (-not (Test-Path $Gpclean)) {
        throw "The app is not installed yet. Run tools\setup-windows.ps1 first."
    }
    if ($Cfg -and ($Cfg -notmatch '^[A-Za-z0-9_-]{1,64}$')) {
        throw "-Cfg must be a plain bundle name (letters, digits, - and _)."
    }

    # --- 1. find the newest bundle ----------------------------------------------------------
    Say "Looking for bundles on your Google Drive ..."
    $json = & rclone lsjson $Remote --recursive --files-only --max-depth 2 --config $Conf
    if ($LASTEXITCODE -ne 0) {
        throw "Could not list gpclean-output/bundle on your Drive. If no pipeline run has finished yet, there is nothing to download. If the message above mentions 'invalid_grant' or 'token', your sign-in is older than 7 days: run tools\refresh-secret.ps1, then try again."
    }
    $manifests = @(($json | Out-String | ConvertFrom-Json) |
        Where-Object { $_.Name -eq "manifest.json" -and $_.Path -match '^[A-Za-z0-9_-]+/manifest\.json$' })
    if ($Cfg) {
        $manifests = @($manifests | Where-Object { $_.Path -eq "$Cfg/manifest.json" })
    }
    if ($manifests.Count -eq 0) {
        throw "No finished bundle found on Drive yet. The pipeline writes manifest.json last, so wait until Claude says the run is finished."
    }
    $newest = $manifests | Sort-Object { Get-SortKey $_.ModTime } -Descending | Select-Object -First 1
    $name = $newest.Path.Split("/")[0]
    $stamp = Get-Date -Format "yyyyMMdd-HHmm"
    $dest = Join-Path $Home0 ("bundle\" + $name + "-" + $stamp)
    if (Test-Path $dest) {
        throw "$dest already exists. Wait a minute and run this again (each download gets its own folder)."
    }
    Say "Newest bundle: $name (finished $($newest.ModTime))"

    # --- 2. copy it ------------------------------------------------------------------------
    Say "Downloading to $dest ..."
    Say "(A big library can be several GB. The progress numbers update below.)"
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    & rclone copy "$Remote/$name" $dest -P --transfers 4 --config $Conf
    if ($LASTEXITCODE -ne 0) {
        throw "The download stopped (exit code $LASTEXITCODE). Run this script again; it starts a fresh folder."
    }

    # --- 3. verify -------------------------------------------------------------------------
    Write-Host ""
    Say "Checking the downloaded files ..."
    & $Gpclean verify-bundle $dest
    if ($LASTEXITCODE -ne 0) {
        throw "The downloaded bundle is incomplete or damaged. Run this script again."
    }

    # --- 4. switch gpclean to it -----------------------------------------------------------
    Write-Host ""
    Say "Switching gpclean to the new bundle ..."
    & $Gpclean init --home $Home0 --bundle $dest
    if ($LASTEXITCODE -ne 0) { throw "gpclean init failed (exit code $LASTEXITCODE)." }

    Write-Host ""
    Write-Host "ALL DONE. The new bundle is ready." -ForegroundColor Green
    Write-Host "Start the review site with these two lines:"
    Write-Host "  cd C:\gpclean\app"
    Write-Host "  uv run gpclean serve --home C:\gpclean"
    Write-Host "If the site or Claude was already open, close and restart them to see the new bundle."
    Write-Host "Your review queue (the 'To delete' list) is kept."
    exit 0
}
catch {
    Write-Host ""
    Write-Host "STOPPED: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
