#!/usr/bin/env bash
set -euo pipefail

# Build full (non-hybrid) embeddings for SWE-bench Lite dev split using two models:
#   - Primary: jinaai/jina-code-embeddings-1.5b
#   - Secondary: Salesforce/SweRankEmbed-Small
#
# Outputs are stored under the specified storage directory, one subdir per instance.

STORAGE_DIR="${STORAGE_DIR:-/mnt/data/codeminer}"
DATASET="princeton-nlp/SWE-bench_Lite"
SPLIT="dev"
LANGS=("python")

PRIMARY_MODEL="jinaai/jina-code-embeddings-1.5b"
PRIMARY_PROVIDER="huggingface"
PRIMARY_DIM=1536

SECONDARY_MODEL="Salesforce/SweRankEmbed-Small"
SECONDARY_PROVIDER="huggingface"
SECONDARY_DIM=768

mkdir -p "${STORAGE_DIR}"

echo "Building primary embeddings (${PRIMARY_MODEL})..."
python scripts/build_embeddings.py \
  --dataset "${DATASET}" \
  --split "${SPLIT}" \
  --storage-dir "${STORAGE_DIR}" \
  --languages "${LANGS[@]}" \
  --embedding-model "${PRIMARY_MODEL}" \
  --embedding-provider "${PRIMARY_PROVIDER}" \
  --embedding-dimension "${PRIMARY_DIM}" \
  "$@"

echo "Building secondary embeddings (${SECONDARY_MODEL})..."
python scripts/build_embeddings.py \
  --dataset "${DATASET}" \
  --split "${SPLIT}" \
  --storage-dir "${STORAGE_DIR}" \
  --languages "${LANGS[@]}" \
  --embedding-model "${SECONDARY_MODEL}" \
  --embedding-provider "${SECONDARY_PROVIDER}" \
  --embedding-dimension "${SECONDARY_DIM}" \
  "$@"

echo "Done. Embeddings stored under ${STORAGE_DIR}"
