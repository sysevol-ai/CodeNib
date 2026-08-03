#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

CACHE_DIR="${CACHE_DIR:-$HOME/.codenib}"
SELECTED_INSTANCES="${SELECTED_INSTANCES:-$CACHE_DIR/swebench_sampling/selected_instances.json}"
OUTPUT_DIR="${OUTPUT_DIR:-$CACHE_DIR/swebench_locator_dataset}"

cd "${ROOT_DIR}"
PYTHONPATH=. python scripts/build_swebench_locator_hf_dataset.py \
  --selected-instances "${SELECTED_INSTANCES}" \
  --output-dir "${OUTPUT_DIR}" \
  "$@"
