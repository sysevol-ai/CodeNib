#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

CACHE_DIR="${CACHE_DIR:-$HOME/.codenib}"
OUTPUT_DIR="${OUTPUT_DIR:-$CACHE_DIR/swebench_sampling}"
SELECTED_INSTANCES="${SELECTED_INSTANCES:-$OUTPUT_DIR/selected_instances.json}"
SYNTHESIS_LIMIT="${SYNTHESIS_LIMIT:-1}"
REPEAT_PER_INSTANCE="${REPEAT_PER_INSTANCE:-1}"
INSTANCE_ID="${INSTANCE_ID:-}"
QUERY_TYPES="${QUERY_TYPES:-behavioral}"
SAMPLING_SEED="${SAMPLING_SEED:-}"
BEHAVIORAL_CONSENSUS_RUNS="${BEHAVIORAL_CONSENSUS_RUNS:-3}"
ALLOWED_TOOLS="${ALLOWED_TOOLS:-Read,Grep,Glob,Bash}"

cd "${ROOT_DIR}"

EXTRA_ARGS=()
if [[ -n "${SYNTHESIS_LIMIT}" ]]; then
  EXTRA_ARGS+=(--synthesis-limit "${SYNTHESIS_LIMIT}")
fi
if [[ -n "${INSTANCE_ID}" ]]; then
  EXTRA_ARGS+=(--instance-id "${INSTANCE_ID}")
fi
if [[ -n "${SAMPLING_SEED}" ]]; then
  EXTRA_ARGS+=(--sampling-seed "${SAMPLING_SEED}")
fi
PYTHONPATH=. python scripts/synthesize_swebench.py \
  --cache-dir "${CACHE_DIR}" \
  --repo-cache-dir "${CACHE_DIR}" \
  --output-dir "${OUTPUT_DIR}" \
  --selected-instances "${SELECTED_INSTANCES}" \
  --query-types "${QUERY_TYPES}" \
  --repeat-per-instance "${REPEAT_PER_INSTANCE}" \
  --behavioral-consensus-runs "${BEHAVIORAL_CONSENSUS_RUNS}" \
  --allowed-tools "${ALLOWED_TOOLS}" \
  "${EXTRA_ARGS[@]}"
