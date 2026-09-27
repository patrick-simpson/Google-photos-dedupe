# Download the newest review bundle of your photos from Google Drive and switch gpclean to it.
#
# What it does:
#   1. finds the finished bundles on your Drive (gpclean-output/bundle/<cfg> folders with a
#      manifest.json, which the pipeline writes last), newest first, and skips self-test
#      bundles (the pipeline's selftest mode makes those from synthetic test photos)
#   2. copies exactly the files that bundle's manifest lists into a NEW folder
#      C:\gpclean\bundle\<cfg>-<yyyyMMdd-HHmm>, manifest.json last (so thumbnail packs of zips
#      that are no longer in the export are not downloaded again). rclone copy only: it never
#      changes or removes anything on Drive or on your PC.
#   3. checks every file (gpclean verify-bundle)
#   4. points gpclean at it (gpclean init)
#
# Run it with:
#   powershell -ExecutionPolicy Bypass -File C:\gpclean\app\tools\get-bundle.ps1
# Optional: -Cfg <name> downloads that bundle instead of the newest one (Claude gives you the
# name when it says a bundle is ready). A bundle named this way is used even if it is a
# self-test; the script then says so.
#
# How a self-test bundle is recognised:
#   a) its manifest says so ("mode": "selftest" or "selftest": true), or
#   b) its shard table (gpclean-output/state/<cfg>/shards.json) lists a zip whose Drive file
#      ID is one of the synthetic zips in gpclean-output/selftest. This works for bundles
#      made before the pipeline wrote (a).
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
$Root = "gp:gpclean-output"
$Remote = "$Root/bundle"

function Say([string]$msg) { Write-Host $msg -ForegroundColor Cyan }
function Note([string]$msg) { Write-Host "  NOTE: $msg" -ForegroundColor Yellow }

# A sortable UTC text for an lsjson ModTime. Windows PowerShell 5.1 gives the raw string
# (rclone writes up to 9 decimals; .NET parses at most 7), PowerShell 7 already a DateTime.
function Get-SortKey($t) {
    if ($t -is [datetime]) { return $t.ToUniversalTime().ToString("o") }
    $s = ([string]$t) -replace '(\.\d{7})\d+', '$1'
    return ([datetimeoffset]::Parse($s, [Globalization.CultureInfo]::InvariantCulture)).UtcDateTime.ToString("o")
}

# Run rclone with its messages hidden and return what it printed (stdout) as one string. For
# small reads that may fail on purpose (a file or folder that does not exist). $LASTEXITCODE
# holds rclone's exit code afterwards. In Windows PowerShell 5.1 a redirected error stream
# counts as a script error under "Stop", so relax that inside this function only.
function Invoke-RcloneQuiet([string[]]$RcloneArgs) {
    $ErrorActionPreference = "Continue"
    return (& rclone @RcloneArgs --config $Conf 2>$null | Out-String)
}

# Read a small JSON file from Drive; $null when it is missing or not valid JSON.
function Read-DriveJson([string]$Path) {
    $text = Invoke-RcloneQuiet @("cat", $Path)
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($text)) { return $null }
    try { return ($text | ConvertFrom-Json) } catch { return $null }
}

# Drive file IDs of the synthetic zips the selftest reads (none if no selftest ever ran).
function Get-SelftestZipIds {
    $text = Invoke-RcloneQuiet @("lsjson", "$Root/selftest", "--files-only")
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($text)) { return @() }
    try { $entries = @(($text | ConvertFrom-Json)) } catch { return @() }
    return @($entries | Where-Object { $_.ID } | ForEach-Object { [string]$_.ID })
}

# True when the bundle <Name> (with its parsed manifest) was made by a selftest run.
function Test-SelftestBundle([string]$Name, $Manifest, [string[]]$SelftestIds) {
    if ($Manifest.mode -eq "selftest" -or $Manifest.selftest -eq $true) { return $true }
    if ($SelftestIds.Count -eq 0) { return $false }
    $table = Read-DriveJson "$Root/state/$Name/shards.json"
    if ($null -eq $table) { return $false }
    foreach ($z in @($table.zips)) {
        if ($z.present -and ($SelftestIds -ccontains [string]$z.drive_id)) { return $true }
    }
    return $false
}

# One line saying what a bundle holds, from its manifest (older manifests have no folder).
function Get-BundleSummary($Manifest) {
    $parts = @()
    if ($Manifest.folder) { $parts += "from Drive folder '$($Manifest.folder)'" }
    if ($null -ne $Manifest.counts -and $null -ne $Manifest.counts.items) {
        $parts += "$($Manifest.counts.items) photos"
    }
    if ($Manifest.created_at) {
        $when = [string]$Manifest.created_at
        try {
            $when = ([datetimeoffset]::Parse($when, [Globalization.CultureInfo]::InvariantCulture)).ToLocalTime().ToString("yyyy-MM-dd HH:mm")
        }
        catch { }
        $parts += "made $when"
    }
    if ($Manifest.partial) { $parts += "PARTIAL (some parts of the export are not in it yet)" }
    return ($parts -join ", ")
}

try {
    # rclone prints UTF-8; without this, Windows PowerShell 5.1 garbles non-English names.
    try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

    if (-not (Test-Path $Conf)) {
        throw "$Conf is missing. Do Part C of docs\SETUP_WINDOWS.md first (connect Google Drive)."
    }
    if (-not (Test-Path $Gpclean)) {
        throw "The app is not installed yet. Run tools\setup-windows.ps1 first."
    }
    if ($Cfg -and ($Cfg -notmatch '^[A-Za-z0-9_-]{1,64}$')) {
        throw "-Cfg must be a plain bundle name (letters, digits, - and _)."
    }

    # --- 1. find the newest bundle of your photos ------------------------------------------
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
    $candidates = @($manifests | Sort-Object { Get-SortKey $_.ModTime } -Descending)
    $selftestIds = @(Get-SelftestZipIds)
    $name = $null
    $manifest = $null
    $skipped = 0
    foreach ($m in $candidates) {
        $candidate = $m.Path.Split("/")[0]
        $data = Read-DriveJson "$Remote/$candidate/manifest.json"
        if ($null -eq $data) {
            Note "skipping ${candidate}: its manifest.json could not be read"
            continue
        }
        $isSelftest = Test-SelftestBundle $candidate $data $selftestIds
        if ($isSelftest -and -not $Cfg) {
            Note "skipping ${candidate}: a self-test bundle (synthetic test photos, not yours)"
            $skipped++
            continue
        }
        if ($isSelftest) { Note "$candidate is a self-test bundle (synthetic test photos, not yours)" }
        $name = $candidate
        $manifest = $data
        break
    }
    if (-not $name) {
        if ($skipped -gt 0) {
            throw "Only self-test bundles (synthetic test photos) are on Drive so far. Wait until Claude says the run on your Takeout has finished."
        }
        throw "No readable bundle found on Drive. Run this script again in a few minutes; if it keeps failing, tell Claude (in the project chat)."
    }
    $stamp = Get-Date -Format "yyyyMMdd-HHmm"
    $dest = Join-Path $Home0 ("bundle\" + $name + "-" + $stamp)
    if (Test-Path $dest) {
        throw "$dest already exists. Wait a minute and run this again (each download gets its own folder)."
    }
    Say "Newest bundle: $name"
    $summary = Get-BundleSummary $manifest
    if ($summary) { Say "  $summary" }
    Say "(Not the export you expected, for example Takeout-test instead of Takeout? Press Ctrl+C now and ask Claude in the project chat.)"

    # --- 2. copy it ------------------------------------------------------------------------
    Say "Downloading to $dest ..."
    Say "(A big library can be several GB. The progress numbers update below.)"
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    # The manifest first, under a temporary name: the file list comes from this exact copy,
    # and it becomes manifest.json only after everything it lists has arrived.
    $manifestPart = Join-Path $dest "manifest.json.part"
    & rclone copyto "$Remote/$name/manifest.json" $manifestPart --config $Conf
    if ($LASTEXITCODE -ne 0) {
        throw "The download stopped (exit code $LASTEXITCODE). Run this script again; it starts a fresh folder."
    }
    $listed = [IO.File]::ReadAllText($manifestPart, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $paths = @(@($listed.files) | ForEach-Object { [string]$_.path } |
        Where-Object { $_ -and $_ -ne "manifest.json" })
    if ($paths.Count -eq 0) {
        throw "The bundle's manifest.json lists no files. Tell Claude (in the project chat)."
    }
    foreach ($p in $paths) {
        # Plain relative paths only (like thumbs/abc-0001.sqlite): the manifest comes from
        # Drive and must not make rclone write outside the new folder.
        if ($p -notmatch '^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$' -or $p -match '(^|/)\.\.?(/|$)') {
            throw "The bundle's manifest.json lists an unexpected file name. Tell Claude (in the project chat)."
        }
    }
    $listFile = [IO.Path]::GetTempFileName()
    try {
        # WriteAllLines writes UTF-8 without a byte-order mark, which rclone expects.
        [IO.File]::WriteAllLines($listFile, [string[]]$paths)
        & rclone copy "$Remote/$name" $dest --files-from $listFile -P --transfers 4 --config $Conf
        $code = $LASTEXITCODE
    }
    finally {
        Remove-Item -LiteralPath $listFile -Force -ErrorAction SilentlyContinue
    }
    if ($code -ne 0) {
        throw "The download stopped (exit code $code). Run this script again; it starts a fresh folder."
    }
    Move-Item -LiteralPath $manifestPart -Destination (Join-Path $dest "manifest.json")

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
    Write-Host "ALL DONE. The new bundle ($name) is ready." -ForegroundColor Green
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
