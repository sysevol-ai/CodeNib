#!/usr/bin/env bash
set -euo pipefail
#
# Run a (small × large) embedding-rerank matrix sweep on the CodeMiner-base
# dataset.
#
# Each pair runs:
#   - retrieval: cached FAISS index built by `build_codeminer_base_embeddings.sh`
#                using the SMALL model (offline, loaded from disk)
#   - rerank:    embedding similarity over top-K candidates using the LARGE
#                model (online; corpus is NEVER re-embedded — only the K
#                candidate snippets per query)
#
# This wrapper hands off to ``examples/codeminer_base_rerank_matrix.py``,
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
#   bash scripts/embeddings/eval_codeminer_base_rerank_matrix.sh
#
#   # Smoke test on a single instance
#   FILTER="^(redis__redis-10095)$" \
#       bash scripts/embeddings/eval_codeminer_base_rerank_matrix.sh
#
#   # Restrict the matrix
#   SMALL_MODELS="Salesforce/SweRankEmbed-Small:768" \
#   LARGE_MODELS="fishmingyu/SweRankEmbed-Large:3584" \
#       bash scripts/embeddings/eval_codeminer_base_rerank_matrix.sh

INDEX_CACHE_DIR="${INDEX_CACHE_DIR:-/mnt/data/codeminer}"
REPO_CACHE_DIR="${REPO_CACHE_DIR:-$HOME/.codeminer}"
RESULTS_DIR="${RESULTS_DIR:-${INDEX_CACHE_DIR}/eval_results}"
PROFILE_DIR="${PROFILE_DIR:-${INDEX_CACHE_DIR}/profile_log/query_runtime}"
SPLIT="${SPLIT:-test}"
FILTER="${FILTER:-.*}"
RERANK_TOP_K="${RERANK_TOP_K:-30}"
RERANK_BATCH_SIZE="${RERANK_BATCH_SIZE:-8}"
SMALL_BATCH_SIZE="${SMALL_BATCH_SIZE:-32}"
MAX_SEQ_LENGTH="${MAX_SEQ_LENGTH:-8192}"
PROFILE_TAG="${PROFILE_TAG:-}"

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
echo "Rerank top-K: ${RERANK_TOP_K}"
echo "Small set   : ${SMALL_MODELS[*]}"
echo "Large set   : ${LARGE_MODELS[*]}"
echo ""

CMD=(python examples/codeminer_base_rerank_matrix.py
  --smalls "${SMALL_MODELS[@]}"
  --larges "${LARGE_MODELS[@]}"
  --split "${SPLIT}"
  --filter-instance "${FILTER}"
  --rerank-top-k "${RERANK_TOP_K}"
  --small-batch-size "${SMALL_BATCH_SIZE}"
  --large-batch-size "${RERANK_BATCH_SIZE}"
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
