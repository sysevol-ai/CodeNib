#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
#
# Check a built CodeNib.app against what Apple's notary service asks of every
# binary, before anything is sent to it: each Mach-O file is signed, has the
# hardened runtime, does not carry the debugging entitlement, and was built
# with the macOS 10.9 SDK or later. With --release, each must also be signed
# with a Developer ID and carry a secure timestamp.
#
#   scripts/check-notarizable.sh [--release] path/to/CodeNib.app
set -euo pipefail

RELEASE=0
if [[ "${1:-}" == "--release" ]]; then
  RELEASE=1
  shift
fi
APP="${1:?usage: check-notarizable.sh [--release] path/to/CodeNib.app}"

problems=0
checked=0
report() {
  echo "  $1: $2"
  problems=$((problems + 1))
}

# The SDK a binary was built with, as major*100+minor, from its load commands.
sdk_of() {
  local sdk
  sdk="$(vtool -show-build "$1" 2>/dev/null | awk '$1 == "sdk" {print $2; exit}')"
  if [[ -z "$sdk" ]]; then
    sdk="$(otool -l "$1" | awk '$1 == "sdk" {print $2; exit}')"
  fi
  [[ -n "$sdk" && "$sdk" != "n/a" ]] || return 1
  IFS=. read -r major minor _ <<<"$sdk"
  echo $((major * 100 + ${minor:-0}))
}

while IFS= read -r -d '' file; do
  file -b "$file" | grep -q "Mach-O" || continue
  checked=$((checked + 1))
  name="${file#"$APP"/}"
  if ! details="$(codesign --display --verbose=4 "$file" 2>&1)"; then
    report "$name" "not signed"
    continue
  fi
  grep -Eq '^CodeDirectory .*flags=0x[0-9a-f]*\(.*runtime' <<<"$details" ||
    report "$name" "no hardened runtime"
  if codesign --display --entitlements - --xml "$file" 2>/dev/null |
    grep -q "com.apple.security.get-task-allow"; then
    report "$name" "carries get-task-allow"
  fi
  if sdk="$(sdk_of "$file")"; then
    ((sdk >= 1009)) || report "$name" "built with an SDK older than 10.9"
  else
    report "$name" "no SDK version in its load commands"
  fi
  if ((RELEASE)); then
    grep -q '^Authority=Developer ID Application' <<<"$details" ||
      report "$name" "not signed with a Developer ID"
    grep -q '^Timestamp=' <<<"$details" || report "$name" "no secure timestamp"
  fi
done < <(
  # Source, bytecode and web files make up most of the bundle and are never
  # Mach-O; skipping them keeps the check to a few seconds.
  find "$APP" -type f -size +0 \
    ! -name '*.py' ! -name '*.pyc' ! -name '*.pyi' ! -name '*.txt' \
    ! -name '*.md' ! -name '*.json' ! -name '*.js' ! -name '*.css' \
    ! -name '*.map' ! -name '*.html' ! -name '*.svg' ! -name '*.png' -print0
)

echo "checked $checked binaries in $(basename "$APP")"
if ((problems)); then
  echo "$problems problem(s) would stop notarization" >&2
  exit 1
fi
echo "every binary is ready for notarization"
