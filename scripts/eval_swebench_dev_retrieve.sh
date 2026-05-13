#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

# Run retrieval-only evaluation on SWE-bench Lite dev split using the original
# dense retrieval setup (no rerank).

DATASET="swebench_lite"
SPLIT="dev"
FILTER_INSTANCE="${FILTER_INSTANCE:-.*}"
INDEX_CACHE_DIR="${INDEX_CACHE_DIR:-/mnt/data/codeminer}"
REPO_CACHE_DIR="${REPO_CACHE_DIR:-~/.codeminer}"
RESULT_PATH="${RESULT_PATH:-}"
EVAL_INSTANCES="${EVAL_INSTANCES:-$HOME/.codeminer/swebench_lite_gt_dev.json}"
PROFILE_DIR="${PROFILE_DIR:-}"
PROFILE_TAG_PREFIX="${PROFILE_TAG_PREFIX:-dev_dense_retrieval}"

EMBEDDING_PROVIDER="${EMBEDDING_PROVIDER:-huggingface}"
BATCH_SIZE="${BATCH_SIZE:-32}"

EMBEDDING_MODELS=(
  "Salesforce/SweRankEmbed-Small:768"
  # "BAAI/bge-code-v1:1536"
  "jinaai/jina-code-embeddings-1.5b:1536"
  "fishmingyu/SweRankEmbed-Large:3584"
)

echo "Running SWE-bench Lite dev retrieval eval (filter: ${FILTER_INSTANCE})"

for model_spec in "${EMBEDDING_MODELS[@]}"; do
  EMBEDDING_MODEL="${model_spec%%:*}"
  EMBEDDING_DIM="${model_spec##*:}"

  CMD=(python examples/retrieve_rerank.py
    --dataset "${DATASET}"
    --split "${SPLIT}"
    --filter-instance "${FILTER_INSTANCE}"
    --retrieval-only
    --retrieval-mode dense
    --embedding-model "${EMBEDDING_MODEL}"
    --embedding-provider "${EMBEDDING_PROVIDER}"
    --embedding-dimension "${EMBEDDING_DIM}"
    --batch-size "${BATCH_SIZE}"
    --eval-instances "${EVAL_INSTANCES}"
    --index-cache-dir "${INDEX_CACHE_DIR}"
    --repo-cache-dir "${REPO_CACHE_DIR}"
  )

  if [[ -n "${RESULT_PATH}" ]]; then
    result_suffix="${EMBEDDING_MODEL//\//_}.json"
    CMD+=(--result-path "${RESULT_PATH%/}_${result_suffix}")
  fi
  if [[ -n "${PROFILE_DIR}" ]]; then
    profile_suffix="${EMBEDDING_MODEL//\//_}"
    CMD+=(--profile-dir "${PROFILE_DIR}")
    CMD+=(--enable-profiler)
    CMD+=(--profile-tag "${PROFILE_TAG_PREFIX}_${profile_suffix}")
  fi

  echo "Embedding model: ${EMBEDDING_MODEL}"
  "${CMD[@]}"
done
