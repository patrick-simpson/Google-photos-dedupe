# Renew the Google Drive sign-in and send it to GitHub again.
#
# Why: the Google project is in "Testing" mode, so Google ends the sign-in after 7 days.
# Run it right before asking Claude for a full pipeline run, and whenever the last refresh
# is 5 or more days old (also before downloading a bundle).
#
# What it does:
#   1. re-signs in the "gp" Drive connection (a browser window opens; sign in and allow),
#      and checks that the saved sign-in really changed (answering n to rclone's first
#      question, "replace it?", renews nothing although rclone reports success)
#   2. uploads the renewed connection file to GitHub as the secret RCLONE_CONFIG_B64 of the
#      "photos" environment (piped straight into gh, never shown on screen)
#   3. reminds you to ask Claude to run the probe, which checks the new sign-in works
#
# Run it with:
#   powershell -ExecutionPolicy Bypass -File C:\gpclean\app\tools\refresh-secret.ps1
#
# Windows PowerShell 5.1 compatible, plain ASCII. It never prints the token or the secret.

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Conf = "C:\gpclean\ci-rclone.conf"
$Repo = "patrick-simpson/Google-photos-dedupe"

function Say([string]$msg) { Write-Host $msg -ForegroundColor Cyan }

try {
    if (-not (Test-Path $Conf)) {
        throw "$Conf is missing. Do Part C of docs\SETUP_WINDOWS.md first."
    }
    # gh writes its status to the error stream; in Windows PowerShell 5.1 a redirected error
    # stream would count as a script error under "Stop", so relax it for this one check.
    $ErrorActionPreference = "Continue"
    & gh auth status *> $null
    $ghCode = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($ghCode -ne 0) {
        throw "GitHub CLI is not signed in. Run: gh auth login   (see docs\SETUP_WINDOWS.md, Part D), then run this again."
    }

    # --- 1. renew the Google sign-in --------------------------------------------------------
    Say "Step 1 of 2: renewing the Google Drive sign-in."
    Say "This window asks three questions. Type the answer and press Enter (just pressing Enter also"
    Say "picks the right answer each time):"
    Say "  'Token already configured - replace it?'                          ->  y"
    Say "  'Use web browser to automatically authenticate rclone with remote?' ->  y"
    Say "  'Configure this as a Shared Drive (Team Drive)?'                  ->  n"
    Say "After the second question a browser window opens. Sign in with the SAME Google account as"
    Say "before, click Continue on the 'Google hasn't verified this app' page, tick both boxes, and"
    Say "click Continue. The third question comes after the browser says Success."
    Write-Host ""
    # rclone exits 0 even when "replace it?" was answered n and nothing was renewed, so compare
    # the token line before and after. Both copies stay in memory and are never printed.
    $before = (Get-Content -LiteralPath $Conf | Where-Object { $_ -like "token = *" }) -join "`n"
    & rclone config reconnect gp: --config $Conf
    $code = $LASTEXITCODE
    $after = (Get-Content -LiteralPath $Conf | Where-Object { $_ -like "token = *" }) -join "`n"
    $renewed = [bool]$after -and ($after -ne $before)
    $before = $null
    $after = $null
    if ($code -ne 0) {
        throw "The Google sign-in did not finish (exit code $code). Run this script again."
    }
    if (-not $renewed) {
        throw "The sign-in was NOT renewed (answer y to 'Token already configured - replace it?'). Run this script again."
    }

    # --- 2. upload to GitHub ----------------------------------------------------------------
    Write-Host ""
    Say "Step 2 of 2: sending the renewed sign-in to GitHub (it is not shown on screen) ..."
    $b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($Conf))
    $b64 | gh secret set RCLONE_CONFIG_B64 --env photos --repo $Repo
    $code = $LASTEXITCODE
    $b64 = $null
    if ($code -ne 0) {
        throw "gh could not save the secret (exit code $code). Check that the 'photos' environment exists (docs\GITHUB_SETTINGS.md)."
    }

    Write-Host ""
    Write-Host "ALL DONE. The sign-in is renewed until about $((Get-Date).AddDays(7).ToString('ddd MMM d')). Write that date down." -ForegroundColor Green
    Write-Host "Last step: tell Claude in the project chat 'I refreshed the secret, please run the probe'."
    Write-Host "Claude starts a quick check; a green 'scope check' step confirms that GitHub can use"
    Write-Host "the new sign-in (and only with the two allowed permissions)."
    exit 0
}
catch {
    Write-Host ""
    Write-Host "STOPPED: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
