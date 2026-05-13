#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

CACHE_DIR="${CACHE_DIR:-$HOME/.codeminer}"
OUTPUT_DIR="${OUTPUT_DIR:-$CACHE_DIR/swebench_sampling}"
LANGS=("Python" "Rust" "TypeScript/JavaScript" "C++/C" "Go")

cd "${ROOT_DIR}"
PYTHONPATH=. python scripts/collect_swebench.py \
  --cache-dir "${CACHE_DIR}" \
  --output-dir "${OUTPUT_DIR}" \
  --languages "${LANGS[@]}" \
  --print-sample 5
