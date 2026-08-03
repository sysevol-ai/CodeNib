#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
#
# Profiling sweep for the sparse-seeded graph retrieval baseline.
#
# Runs examples/graph_retrieve_baseline.py with profiling enabled across the
# three headline configurations, each over the same fixed corpus
# (examples/selected_instance.csv — 12 SWE-bench Lite instances) so per-stage
# wall-clock / memory / per-query-bucket breakdowns are attributable to the
# pipeline rather than to instance churn:
#
#   1. BFS expansion,  no rerank
#   2. PPR expansion,  no rerank
#   3. BFS expansion + embedding rerank (Qwen3-Embedding-0.6B, dim 1024)
#
# Each run writes one JSON to $PROFILE_DIR:
#   sparse_seed_graph_<expansion>[_plus_<rerank>_rerank]_<dataset>__<tag>.json
#
# Usage:
#   scripts/profiling/profile_graph_rag.sh
#   PROFILE_DIR=/tmp/sparse_seed_graph CSV=examples/selected_instance.csv \
#       scripts/profiling/profile_graph_rag.sh
#
# Optional Phase 4 (LLM rerank) is intentionally NOT run here: LLM listwise
# rerank lives in examples/retrieve_rerank.py and needs a running vLLM server,
# so it is a separate, opt-in measurement rather than part of this sweep.
set -euo pipefail

# Resolve repo root from this script's location so the sweep runs from anywhere.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

# ---- Config (override via environment) ------------------------------------
DATASET="${DATASET:-swebench_lite}"
SPLIT="${SPLIT:-test}"
CSV="${CSV:-examples/selected_instance.csv}"
PROFILE_DIR="${PROFILE_DIR:-${REPO_ROOT}/profile_log/sparse_seed_graph}"
PROFILE_TAG="${PROFILE_TAG:-sweep}"

# Stage parameters (issue #131: stage1 top-K=5, 2-hop, up to 50 nodes).
STAGE1_TOPK="${STAGE1_TOPK:-5}"
STAGE2_TOPK="${STAGE2_TOPK:-50}"
K_HOP="${K_HOP:-2}"

# Stage 3 embedding rerank model (config #3).
EMBED_MODEL="${EMBED_MODEL:-Qwen/Qwen3-Embedding-0.6B}"
EMBED_PROVIDER="${EMBED_PROVIDER:-huggingface}"
EMBED_DIM="${EMBED_DIM:-1024}"

PY="${PYTHON:-python}"
RUNNER="examples/graph_retrieve_baseline.py"

mkdir -p "${PROFILE_DIR}"

# Common args shared by every config. --record-samples / --record-memory turn
# on the p50/p95/p99 and RSS/GPU rollups; --filter-csv pins the corpus.
COMMON_ARGS=(
  --dataset "${DATASET}"
  --split "${SPLIT}"
  --filter-csv "${CSV}"
  --stage1-topk "${STAGE1_TOPK}"
  --stage2-topk "${STAGE2_TOPK}"
  --k-hop "${K_HOP}"
  --enable-profiler
  --record-samples
  --record-memory
  --profile-dir "${PROFILE_DIR}"
  --profile-tag "${PROFILE_TAG}"
)

echo "=================================================================="
echo "sparse-seeded graph profiling sweep"
echo "  dataset    : ${DATASET} (${SPLIT})"
echo "  corpus     : ${CSV}"
echo "  profile dir: ${PROFILE_DIR}"
echo "  tag        : ${PROFILE_TAG}"
echo "=================================================================="

echo
echo "[1/3] BFS expansion, no rerank"
"${PY}" "${RUNNER}" "${COMMON_ARGS[@]}"

echo
echo "[2/3] PPR expansion, no rerank"
"${PY}" "${RUNNER}" "${COMMON_ARGS[@]}" --ppr

echo
echo "[3/3] BFS expansion + embedding rerank (${EMBED_MODEL}, dim ${EMBED_DIM})"
"${PY}" "${RUNNER}" "${COMMON_ARGS[@]}" \
  --embedding \
  --embedding-model "${EMBED_MODEL}" \
  --embedding-provider "${EMBED_PROVIDER}" \
  --embedding-dimension "${EMBED_DIM}"

echo
echo "=================================================================="
echo "Done. Profiler JSONs written to ${PROFILE_DIR}:"
ls -1 "${PROFILE_DIR}"/sparse_seed_graph_*"${PROFILE_TAG}".json 2>/dev/null || true
echo "=================================================================="
