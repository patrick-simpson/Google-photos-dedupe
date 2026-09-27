#!/usr/bin/env bash
# Install pinned uv (and optionally rclone) from official release files, verified by
# hard-coded SHA-256 values. Linux x86_64 only (CI runners).
#
# Usage: tools/install_tools.sh [--rclone] [DEST]
#   DEST defaults to $RUNNER_TEMP/bin in Actions, else $HOME/.local/gpclean-bin.
#
# Prints only "<tool> ok" / "install_tools fail"; details go to DEST/install.log so nothing
# about the machine or network ends up in the public job log.
#
# Pins (bump deliberately; re-fetch the hashes when you do):
#   uv 0.8.24: sha256 from the release's uv-x86_64-unknown-linux-gnu.tar.gz.sha256 file,
#     re-checked against a fresh download on 2026-09-27. uv stays on the 0.8 series because
#     pyproject.toml pins the build backend to uv_build>=0.8.17,<0.9: a uv outside that range
#     does not use its built-in backend and instead downloads uv-build from PyPI unhashed.
#     Bump both together (and re-lock) when moving to a newer series.
#   rclone v1.75.1: sha256 from https://downloads.rclone.org/v1.75.1/SHA256SUMS, whose PGP
#     signature was verified with gpg against rclone's release key from https://rclone.org/KEYS
#     (primary key fingerprint FBF7 37EC E9F8 AB18 604B D2AC 9393 5E02 FF3B 54FA,
#     "Good signature", 2026-09-27); the zip was re-hashed after download.
set -Eeuo pipefail  # -E: the ERR trap also fires inside functions

UV_VERSION="0.8.24"
UV_SHA256="db8179fffd97b7557b9a519bae82eaa4f499b02ef546f738a35e74e26c47e6b7"
UV_ASSET="uv-x86_64-unknown-linux-gnu"
RCLONE_VERSION="v1.75.1"
RCLONE_SHA256="982b5aa772841168f8e380f139e9e787b2a105403e32b94da8676a0e1c0a13ab"
RCLONE_ASSET="rclone-${RCLONE_VERSION}-linux-amd64"

with_rclone=0
dest=""
for arg in "$@"; do
  case "$arg" in
    --rclone) with_rclone=1 ;;
    -*) echo "install_tools fail"; exit 2 ;;
    *) dest="$arg" ;;
  esac
done
if [ -z "$dest" ]; then
  if [ -n "${RUNNER_TEMP:-}" ]; then dest="$RUNNER_TEMP/bin"; else dest="$HOME/.local/gpclean-bin"; fi
fi

mkdir -p "$dest"
log="$dest/install.log"
work="$(mktemp -d)"
cleanup() { rm -rf "$work"; }
fail() { echo "install_tools fail"; cleanup; exit 1; }
trap fail ERR
trap cleanup EXIT

fetch() {  # fetch URL OUT
  curl --proto '=https' --tlsv1.2 -fsSL --retry 3 --retry-delay 2 -o "$2" "$1" >>"$log" 2>&1
}

verify() {  # verify SHA256 FILE
  echo "$1  $2" | sha256sum --check --strict --status - >>"$log" 2>&1
}

fetch "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${UV_ASSET}.tar.gz" \
  "$work/uv.tar.gz"
verify "$UV_SHA256" "$work/uv.tar.gz"
tar -xzf "$work/uv.tar.gz" -C "$work" >>"$log" 2>&1
install -m 0755 "$work/$UV_ASSET/uv" "$work/$UV_ASSET/uvx" "$dest/" >>"$log" 2>&1
echo "uv ok"

if [ "$with_rclone" -eq 1 ]; then
  fetch "https://downloads.rclone.org/${RCLONE_VERSION}/${RCLONE_ASSET}.zip" "$work/rclone.zip"
  verify "$RCLONE_SHA256" "$work/rclone.zip"
  # Extract only the binary; python's zipfile avoids depending on unzip being installed.
  python3 -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extract(sys.argv[2], sys.argv[3])' \
    "$work/rclone.zip" "$RCLONE_ASSET/rclone" "$work" >>"$log" 2>&1
  install -m 0755 "$work/$RCLONE_ASSET/rclone" "$dest/rclone" >>"$log" 2>&1
  echo "rclone ok"
fi
