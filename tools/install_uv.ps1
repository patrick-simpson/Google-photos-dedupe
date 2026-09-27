# Install a pinned uv on Windows x64 from the official release zip, verified by SHA-256.
#
# Usage: pwsh -NoProfile -ExecutionPolicy Bypass -File tools/install_uv.ps1 [-Dest DIR]
#   Dest defaults to $env:RUNNER_TEMP\bin in Actions, else $HOME\.local\gpclean-bin.
#
# Prints only "uv ok" / "uv fail" (no paths, no error text) so the public CI log stays clean.
#
# Pin: uv 0.8.24; sha256 from the release's uv-x86_64-pc-windows-msvc.zip.sha256 file,
# re-checked against a fresh download on 2026-09-27. Keep in step with tools/install_tools.sh
# (the 0.8 series matches pyproject's uv_build>=0.8.17,<0.9, so uv builds the project itself
# instead of fetching an unhashed uv-build from PyPI).
param(
    [string]$Dest = ""
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # the progress bar makes Invoke-WebRequest slow
Set-StrictMode -Version Latest

$UvVersion = '0.8.24'
$UvSha256 = '5055be7909a844f703c54e8846d14ab676c34be6ea0d969ee74c5747feaedda0'
$UvAsset = 'uv-x86_64-pc-windows-msvc.zip'

if (-not $Dest) {
    if ($env:RUNNER_TEMP) { $Dest = Join-Path $env:RUNNER_TEMP 'bin' }
    else { $Dest = Join-Path $HOME '.local\gpclean-bin' }
}

$work = Join-Path ([IO.Path]::GetTempPath()) ('gpclean-uv-' + [guid]::NewGuid().ToString('N'))
try {
    New-Item -ItemType Directory -Force -Path $work | Out-Null
    New-Item -ItemType Directory -Force -Path $Dest | Out-Null
    $zip = Join-Path $work $UvAsset
    $url = "https://github.com/astral-sh/uv/releases/download/$UvVersion/$UvAsset"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant()
    if ($actual -ne $UvSha256) { throw 'sha256 mismatch' }
    $unpacked = Join-Path $work 'x'
    Expand-Archive -LiteralPath $zip -DestinationPath $unpacked -Force
    foreach ($exe in 'uv.exe', 'uvx.exe', 'uvw.exe') {
        Copy-Item -LiteralPath (Join-Path $unpacked $exe) -Destination $Dest -Force
    }
    Write-Output 'uv ok'
}
catch {
    Write-Output 'uv fail'
    exit 1
}
finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}
