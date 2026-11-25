#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
#
# Put a relocatable Python with this checkout's CodeNib into the app, so
# CodeNib.app works on a Mac with nothing else installed. Run on an Apple
# silicon Mac after building the Wiki frontend (cd web && npm ci && npm run
# build); the result lands in src-tauri/resources/python/.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP="$(dirname "$HERE")"
REPO="$(dirname "$DESKTOP")"
TARGET="$DESKTOP/src-tauri/resources/python"

# python-build-standalone, pinned by release and checksum.
RELEASE="20261026"
ARCHIVE="cpython-3.12.16+${RELEASE}-aarch64-apple-darwin-install_only.tar.gz"
SHA256="fbf4afef53c2a6b72bb741bc06d5671a9dfaf87bd84e5aa73ac9b3d0d589f14e"
URL="https://github.com/astral-sh/python-build-standalone/releases/download/${RELEASE}/${ARCHIVE//+/%2B}"

if [[ "$(uname -s)-$(uname -m)" != "Darwin-arm64" ]]; then
  echo "bundle-python.sh builds the Apple silicon runtime; run it on an Apple silicon Mac" >&2
  exit 1
fi
if [[ ! -f "$REPO/web/dist/index.html" ]]; then
  echo "build the Wiki frontend first: cd web && npm ci && npm run build" >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
curl --fail --location --silent --show-error --output "$WORK/$ARCHIVE" "$URL"
echo "$SHA256  $WORK/$ARCHIVE" | shasum -a 256 --check --status

find "$TARGET" -mindepth 1 ! -name .gitkeep -exec rm -rf {} +
tar -xzf "$WORK/$ARCHIVE" -C "$WORK"
cp -R "$WORK/python/." "$TARGET/"

PY="$TARGET/bin/python3.12"
"$PY" -m pip install --quiet --no-cache-dir --upgrade pip
"$PY" -m pip install --quiet --no-cache-dir "$REPO[graph]"

# Drop what the app never runs.
rm -rf "$TARGET"/lib/python3.12/{test,idlelib,tkinter,turtledemo,ensurepip}
rm -rf "$TARGET"/lib/{tcl*,tk*,itcl*,thread*} "$TARGET"/share
find "$TARGET" -name "__pycache__" -type d -prune -exec rm -rf {} +

# Apple silicon runs only signed code: sign every binary ad hoc, as the app is.
find "$TARGET" -type f \( -perm -u+x -o -name "*.so" -o -name "*.dylib" \) -print0 |
  while IFS= read -r -d '' file; do
    if file -b "$file" | grep -q "Mach-O"; then
      codesign --force --sign - "$file"
    fi
  done

"$PY" -m codenib --version
du -sh "$TARGET"
