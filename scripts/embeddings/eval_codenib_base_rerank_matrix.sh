#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
CODENIB_HOME="${CODENIB_HOME:-$HOME/.codenib}"
CODENIB_PREBUILT_DIR="${CODENIB_PREBUILT_DIR:-${CODENIB_HOME}/prebuilt}"
CODENIB_RESULTS_DIR="${CODENIB_RESULTS_DIR:-${CODENIB_HOME}/results}"
#
# Run a (small × large) embedding-rerank matrix sweep on the CodeNib-base
# dataset.
#
# Each pair runs:
#   - retrieval: cached FAISS index built by `build_codenib_base_embeddings.sh`
#                using the SMALL model (offline, loaded from disk)
#   - rerank:    embedding similarity over top-K candidates using the LARGE
#                model (online; corpus is NEVER re-embedded — only the K
#                candidate snippets per query)
#
# This wrapper hands off to ``examples/codenib_base_rerank_matrix.py``,
# which loads each (small, large) pair ONCE and iterates the dataset, instead
# of reloading models per instance. ~4× faster than the per-pair shell loop
# this script previously implemented.
#
# Pre-requisites: the SMALL model's FAISS index already exists under
# ${INDEX_CACHE_DIR}/<instance>/{l0,l2}/index_<small_model>.faiss. The LARGE
# model needs no on-disk index — it scores candidates online.
#
# Usage:
#   # Full sweep (all small × large pairs, all instances)
#   bash scripts/embeddings/eval_codenib_base_rerank_matrix.sh
#
#   # Smoke test on a single instance
#   FILTER="^(redis__redis-10095)$" \
#       bash scripts/embeddings/eval_codenib_base_rerank_matrix.sh
#
#   # Restrict the matrix
#   SMALL_MODELS="Salesforce/SweRankEmbed-Small:768" \
#   LARGE_MODELS="fishmingyu/SweRankEmbed-Large:3584" \
#       bash scripts/embeddings/eval_codenib_base_rerank_matrix.sh

INDEX_CACHE_DIR="${INDEX_CACHE_DIR:-${CODENIB_PREBUILT_DIR}}"
REPO_CACHE_DIR="${REPO_CACHE_DIR:-${CODENIB_HOME}}"
RESULTS_DIR="${RESULTS_DIR:-${CODENIB_RESULTS_DIR}/eval_results}"
PROFILE_DIR="${PROFILE_DIR:-${CODENIB_RESULTS_DIR}/profile_log/query_runtime}"
SPLIT="${SPLIT:-test}"
FILTER="${FILTER:-.*}"
RERANK_TOP_K="${RERANK_TOP_K:-30}"
RERANK_BATCH_SIZE="${RERANK_BATCH_SIZE:-8}"
SMALL_BATCH_SIZE="${SMALL_BATCH_SIZE:-32}"
MAX_SEQ_LENGTH="${MAX_SEQ_LENGTH:-8192}"
PROFILE_TAG="${PROFILE_TAG:-}"
RERANK_STRATEGY="${RERANK_STRATEGY:-embedding}"   # embedding | cross-encoder
CROSS_ENCODER_BATCH_SIZE="${CROSS_ENCODER_BATCH_SIZE:-8}"
CROSS_ENCODER_BACKEND="${CROSS_ENCODER_BACKEND:-auto}"  # auto | st | qwen
CROSS_ENCODER_INSTRUCTION="${CROSS_ENCODER_INSTRUCTION:-Given a github issue, identify the code that needs to be changed to fix the issue.}"

# Auto-suffix the profile tag with the top-K window when non-default so that
# K-sweep runs (K=50, 100, …) write to distinct result/profile files instead
# of overwriting each other. Existing K=30 runs (the default) keep their
# previous filenames untouched.
if [[ "${RERANK_TOP_K}" != "30" ]]; then
  if [[ -n "${PROFILE_TAG}" ]]; then
    PROFILE_TAG="${PROFILE_TAG}_k${RERANK_TOP_K}"
  else
    PROFILE_TAG="k${RERANK_TOP_K}"
  fi
fi

# format: "model_name:dim"
SMALL_MODELS_DEFAULT=(
  "Salesforce/SweRankEmbed-Small:768"
  "Qwen/Qwen3-Embedding-0.6B:1024"
)
LARGE_MODELS_DEFAULT=(
  "fishmingyu/SweRankEmbed-Large:3584"
  "Qwen/Qwen3-Embedding-4B:2560"
  "jinaai/jina-code-embeddings-1.5b:1536"
)

if [[ -n "${SMALL_MODELS:-}" ]]; then
  read -r -a SMALL_MODELS <<< "${SMALL_MODELS}"
else
  SMALL_MODELS=("${SMALL_MODELS_DEFAULT[@]}")
fi
if [[ -n "${LARGE_MODELS:-}" ]]; then
  read -r -a LARGE_MODELS <<< "${LARGE_MODELS}"
else
  LARGE_MODELS=("${LARGE_MODELS_DEFAULT[@]}")
fi

mkdir -p "${RESULTS_DIR}" "${PROFILE_DIR}"

echo "Index cache : ${INDEX_CACHE_DIR}"
echo "Repo cache  : ${REPO_CACHE_DIR}"
echo "Results dir : ${RESULTS_DIR}"
echo "Profile dir : ${PROFILE_DIR}"
echo "Split       : ${SPLIT}"
echo "Filter      : ${FILTER}"
echo "Strategy    : ${RERANK_STRATEGY}"
echo "Rerank top-K: ${RERANK_TOP_K}"
echo "Small set   : ${SMALL_MODELS[*]}"
echo "Large set   : ${LARGE_MODELS[*]}"
echo ""

CMD=(python examples/codenib_base_rerank_matrix.py
  --smalls "${SMALL_MODELS[@]}"
  --larges "${LARGE_MODELS[@]}"
  --split "${SPLIT}"
  --filter-instance "${FILTER}"
  --rerank-strategy "${RERANK_STRATEGY}"
  --rerank-top-k "${RERANK_TOP_K}"
  --small-batch-size "${SMALL_BATCH_SIZE}"
  --large-batch-size "${RERANK_BATCH_SIZE}"
  --cross-encoder-batch-size "${CROSS_ENCODER_BATCH_SIZE}"
  --cross-encoder-backend "${CROSS_ENCODER_BACKEND}"
  --cross-encoder-instruction "${CROSS_ENCODER_INSTRUCTION}"
  --max-seq-length "${MAX_SEQ_LENGTH}"
  --index-cache-dir "${INDEX_CACHE_DIR}"
  --repo-cache-dir "${REPO_CACHE_DIR}"
  --results-dir "${RESULTS_DIR}"
  --profile-dir "${PROFILE_DIR}"
)

if [[ -n "${PROFILE_TAG}" ]]; then
  CMD+=(--profile-tag "${PROFILE_TAG}")
fi

"${CMD[@]}"

echo ""
echo "================================================================"
echo "Matrix sweep complete. Results: ${RESULTS_DIR}"
echo "================================================================"
