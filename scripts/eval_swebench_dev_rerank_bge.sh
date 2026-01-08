#!/usr/bin/env bash
set -euo pipefail

# Run retrieval + embedding rerank evaluation on SWE-bench Lite dev split using
# BAAI/bge-code-v1 as the reranker.

DATASET="swebench_lite"
SPLIT="dev"
FILTER_INSTANCE="${FILTER_INSTANCE:-.*}"
INDEX_CACHE_DIR="${INDEX_CACHE_DIR:-/mnt/data/codeminer}"
REPO_CACHE_DIR="${REPO_CACHE_DIR:-~/.codeminer}"
RESULT_PATH="${RESULT_PATH:-}"
EVAL_INSTANCES="${EVAL_INSTANCES:-$HOME/.codeminer/swebench_lite_gt_dev.json}"

EMBEDDING_MODEL="${EMBEDDING_MODEL:-Salesforce/SweRankEmbed-Small}"
EMBEDDING_PROVIDER="${EMBEDDING_PROVIDER:-huggingface}"
EMBEDDING_DIM="${EMBEDDING_DIM:-768}"
BATCH_SIZE="${BATCH_SIZE:-32}"

RERANK_EMBEDDING_MODEL="${RERANK_EMBEDDING_MODEL:-BAAI/bge-code-v1}"
RERANK_EMBEDDING_PROVIDER="${RERANK_EMBEDDING_PROVIDER:-huggingface}"
RERANK_EMBEDDING_DIM="${RERANK_EMBEDDING_DIM:-1536}"
RERANK_BATCH_SIZE="${RERANK_BATCH_SIZE:-8}"
RERANK_TOP_K="${RERANK_TOP_K:-}"

CMD=(python examples/retrieve_rerank.py
  --dataset "${DATASET}"
  --split "${SPLIT}"
  --filter-instance "${FILTER_INSTANCE}"
  --retrieval-mode dense
  --embedding-model "${EMBEDDING_MODEL}"
  --embedding-provider "${EMBEDDING_PROVIDER}"
  --embedding-dimension "${EMBEDDING_DIM}"
  --batch-size "${BATCH_SIZE}"
  --rerank-strategy embedding
  --rerank-embedding-model "${RERANK_EMBEDDING_MODEL}"
  --rerank-embedding-provider "${RERANK_EMBEDDING_PROVIDER}"
  --rerank-embedding-dimension "${RERANK_EMBEDDING_DIM}"
  --rerank-embedding-batch-size "${RERANK_BATCH_SIZE}"
  --eval-instances "${EVAL_INSTANCES}"
  --index-cache-dir "${INDEX_CACHE_DIR}"
  --repo-cache-dir "${REPO_CACHE_DIR}"
)

if [[ -n "${RERANK_TOP_K}" ]]; then
  CMD+=(--rerank-top-k "${RERANK_TOP_K}")
fi

if [[ -n "${RESULT_PATH}" ]]; then
  CMD+=(--result-path "${RESULT_PATH}")
fi

echo "Running SWE-bench Lite dev rerank eval (filter: ${FILTER_INSTANCE})"
echo "Embedding model: ${EMBEDDING_MODEL}"
echo "Rerank embedding model: ${RERANK_EMBEDDING_MODEL}"

"${CMD[@]}"
