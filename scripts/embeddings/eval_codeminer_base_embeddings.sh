#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
#
# Evaluate embedding retrieval quality on the CodeMiner-base dataset.
#
# Loads pre-built FAISS indices (produced by build_codeminer_base_embeddings.sh)
# and runs the embedding-only retrieval baseline against the ground-truth
# annotations baked into the dataset.
#
# Usage:
#   # Evaluate all instances with all five models
#   bash scripts/embeddings/eval_codeminer_base_embeddings.sh
#
#   # Evaluate a single model
#   bash scripts/embeddings/eval_codeminer_base_embeddings.sh \
#       --filter-instance "^(redis__redis-1234)$"
#
#   # Override storage / result directories
#   STORAGE_DIR=/mnt/data/codeminer RESULTS_DIR=/tmp/eval_results \
#       bash scripts/embeddings/eval_codeminer_base_embeddings.sh
#
#   # Force rebuild indices instead of loading pre-built ones
#   bash scripts/embeddings/eval_codeminer_base_embeddings.sh --force-rebuild

STORAGE_DIR="${STORAGE_DIR:-/mnt/data/codeminer}"
RESULTS_DIR="${RESULTS_DIR:-${STORAGE_DIR}/eval_results}"
PROFILE_DIR="${PROFILE_DIR:-${STORAGE_DIR}/profile_log/query_runtime}"
SPLIT="${SPLIT:-test}"

# Model entries: "model_name:dim"
# Keep in sync with build_codeminer_base_embeddings.sh
MODELS=(
  "Salesforce/SweRankEmbed-Small:768"
  "fishmingyu/SweRankEmbed-Large:3584"
  "jinaai/jina-code-embeddings-1.5b:1536"
  "Qwen/Qwen3-Embedding-0.6B:1024"
  "Qwen/Qwen3-Embedding-4B:2560"
)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
BASELINE="${REPO_ROOT}/examples/embedding_retrieve_baseline.py"

mkdir -p "${RESULTS_DIR}"

echo "Storage dir : ${STORAGE_DIR}"
echo "Results dir : ${RESULTS_DIR}"
echo "Profile dir : ${PROFILE_DIR}"
echo "Split       : ${SPLIT}"
echo ""

for entry in "${MODELS[@]}"; do
  MODEL="${entry%%:*}"
  DIM="${entry##*:}"

  # Sanitize model name for use in filenames
  MODEL_TAG="${MODEL//\//__}"
  RESULT_FILE="${RESULTS_DIR}/eval_codeminer_base_${SPLIT}_${MODEL_TAG}.json"

  echo "================================================================"
  echo "Model : ${MODEL}  (dim=${DIM})"
  echo "Output: ${RESULT_FILE}"
  echo "================================================================"

  python "${BASELINE}" \
    --dataset codeminer_base \
    --split "${SPLIT}" \
    --embedding-model "${MODEL}" \
    --embedding-dimension "${DIM}" \
    --index-cache-dir "${STORAGE_DIR}" \
    --result-path "${RESULT_FILE}" \
    --enable-profiler \
    --profile-dir "${PROFILE_DIR}" \
    --profile-tag "${MODEL_TAG}" \
    "$@" || true

  echo ""
done

echo "================================================================"
echo "All models evaluated. Results written to ${RESULTS_DIR}/"
echo "================================================================"
