#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

SCIP_CLANG_VERSION="${SCIP_CLANG_VERSION:-v0.3.2}"
SCIP_CLANG_DEST="${SCIP_CLANG_DEST:-$HOME/.local/bin/scip-clang}"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

require_cmd curl
require_cmd uname

OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"

if [ "$ARCH" != "x86_64" ]; then
  echo "Unsupported architecture: $ARCH (expected x86_64)" >&2
  exit 1
fi

case "$OS" in
  linux|darwin) ;;
  *)
    echo "Unsupported OS: $OS (expected linux or darwin)" >&2
    exit 1
    ;;
 esac

if ! command -v rustup >/dev/null 2>&1; then
  echo "Installing rustup (stable toolchain + rust-analyzer)..."
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
fi

export PATH="$HOME/.cargo/bin:$PATH"

rustup toolchain install stable
rustup component add rust-analyzer --toolchain stable

if ! command -v npm >/dev/null 2>&1; then
  echo "Missing required command: npm" >&2
  echo "Please install Node.js and npm, then re-run this script." >&2
  exit 1
fi

npm install -g @sourcegraph/scip-typescript
npm install -g @sourcegraph/scip-python

SCIP_CLANG_BIN="scip-clang-x86_64-$OS"
SCIP_CLANG_URL="https://github.com/sourcegraph/scip-clang/releases/download/${SCIP_CLANG_VERSION}/${SCIP_CLANG_BIN}"

mkdir -p "$(dirname "$SCIP_CLANG_DEST")"

curl -L "$SCIP_CLANG_URL" -o "$SCIP_CLANG_DEST"
chmod +x "$SCIP_CLANG_DEST"

echo "Installed rust-analyzer, scip-typescript, scip-python, and scip-clang."
echo "Ensure these are on PATH:"
echo "  $HOME/.cargo/bin"
echo "  $(dirname "$SCIP_CLANG_DEST")"
echo "  $(npm bin -g)"
