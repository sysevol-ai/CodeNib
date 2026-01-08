#!/usr/bin/env bash
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

EMBEDDING_MODEL="${EMBEDDING_MODEL:-Salesforce/SweRankEmbed-Small}"
EMBEDDING_PROVIDER="${EMBEDDING_PROVIDER:-huggingface}"
EMBEDDING_DIM="${EMBEDDING_DIM:-768}"
BATCH_SIZE="${BATCH_SIZE:-32}"

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
  CMD+=(--result-path "${RESULT_PATH}")
fi

echo "Running SWE-bench Lite dev retrieval eval (filter: ${FILTER_INSTANCE})"
echo "Embedding model: ${EMBEDDING_MODEL}"

"${CMD[@]}"
