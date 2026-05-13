#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
#
# Build embeddings for the CodeMiner-base dataset with five models:
#   1. Salesforce/SweRankEmbed-Small       (768d,  ~0.1B, encoder)
#   2. fishmingyu/SweRankEmbed-Large       (3584d, ~7B,   encoder)
#   3. jinaai/jina-code-embeddings-1.5b    (1536d, 1.5B,  encoder)
#   4. Qwen/Qwen3-Embedding-0.6B          (1024d, 0.6B,  decoder, last-token pooling)
#   5. Qwen/Qwen3-Embedding-4B            (2560d, 4B,    decoder, last-token pooling)
#
# Usage:
#   # Full run (all instances, all models)
#   bash scripts/embeddings/build_codeminer_base_embeddings.sh
#
#   # Single instance smoke test
#   FILTER="^(astropy__astropy-12907)$" bash scripts/embeddings/build_codeminer_base_embeddings.sh
#
#   # Override storage / dataset
#   STORAGE_DIR=/tmp/emb DATASET=fishmingyu/codeminer-base-dataset \
#     bash scripts/embeddings/build_codeminer_base_embeddings.sh

STORAGE_DIR="${STORAGE_DIR:-/mnt/data/codeminer}"
DATASET="${DATASET:-fishmingyu/codeminer-base-dataset}"
SPLIT="${SPLIT:-test}"
FILTER="${FILTER:-.*}"
PROFILE_TAG="${PROFILE_TAG:-codeminer_base_${SPLIT}}"

# model:dimension:batch_size:fallback_batch_size:max_seq_length
# Batch sizes and max_seq_length tuned for H100 80GB without flash-attn.
# max_seq_length caps tokenization to prevent CUDA OOM on long documents.
# Use 0 for max_seq_length to use the model's default.
#
# NOTE: Models with 32K+ context (jina-code-1.5b, Qwen3-Embedding) have
# tokenizers that do not enforce truncation, and 32K seq OOMs even at batch=1
# without flash-attn. We cap at 8192 which is safe at the listed batch sizes.
# The main offender is hashicorp__terraform-35611 (L0 files up to ~49K tokens,
# 14145 L2 chunks). With flash-attn installed, the cap could be raised.
#
# Qwen3 models use decoder-only (causal LM) backbone with last-token pooling.
# sentence-transformers >= 2.7.0 handles this automatically.
MODELS=(
  "Salesforce/SweRankEmbed-Small:768:8:4:0"
  "fishmingyu/SweRankEmbed-Large:3584:2:1:0"
  "jinaai/jina-code-embeddings-1.5b:1536:4:2:8192"
  "Qwen/Qwen3-Embedding-0.6B:1024:8:4:8192"
  "Qwen/Qwen3-Embedding-4B:2560:2:1:8192"
)

mkdir -p "${STORAGE_DIR}"

for entry in "${MODELS[@]}"; do
  IFS=':' read -r MODEL DIM BATCH FALLBACK_BATCH MAX_SEQ <<< "${entry}"

  SEQ_FLAG=""
  if [ "${MAX_SEQ}" != "0" ]; then
    SEQ_FLAG="--max-seq-length ${MAX_SEQ}"
  fi

  echo ""
  echo "================================================================"
  echo "Building embeddings: ${MODEL} (dim=${DIM}, batch=${BATCH}, max_seq=${MAX_SEQ})"
  echo "  dataset=${DATASET}  split=${SPLIT}  filter=${FILTER}"
  echo "================================================================"
  python scripts/embeddings/build_embeddings.py \
    --dataset-class codeminer_base \
    --dataset "${DATASET}" \
    --split "${SPLIT}" \
    --filter-instance "${FILTER}" \
    --storage-dir "${STORAGE_DIR}" \
    --enable-profiler \
    --profile-tag "${PROFILE_TAG}" \
    --embedding-model "${MODEL}" \
    --embedding-provider huggingface \
    --embedding-dimension "${DIM}" \
    --batch-size "${BATCH}" \
    --trust-remote-code \
    --isolate-instances \
    ${SEQ_FLAG} \
    "$@" || true

  # Retry failed instances with a smaller batch size.
  # Do NOT forward --force-rebuild here: we only want to retry instances
  # that genuinely failed in the primary run above.
  if [ "${FALLBACK_BATCH}" != "${BATCH}" ]; then
    # Strip --force-rebuild from extra args so the retry skips successes.
    RETRY_ARGS=()
    for arg in "$@"; do
      [ "${arg}" != "--force-rebuild" ] && RETRY_ARGS+=("${arg}")
    done
    echo ""
    echo "----------------------------------------------------------------"
    echo "Retrying with fallback batch=${FALLBACK_BATCH} for any failures"
    echo "----------------------------------------------------------------"
    python scripts/embeddings/build_embeddings.py \
      --dataset-class codeminer_base \
      --dataset "${DATASET}" \
      --split "${SPLIT}" \
      --filter-instance "${FILTER}" \
      --storage-dir "${STORAGE_DIR}" \
      --enable-profiler \
      --profile-tag "${PROFILE_TAG}" \
      --embedding-model "${MODEL}" \
      --embedding-provider huggingface \
      --embedding-dimension "${DIM}" \
      --batch-size "${FALLBACK_BATCH}" \
      --trust-remote-code \
      --isolate-instances \
      ${SEQ_FLAG} \
      "${RETRY_ARGS[@]}" || true
  fi
done

echo ""
echo "Done. Embeddings stored under ${STORAGE_DIR}"
echo "Profile logs stored under ${STORAGE_DIR}/profile_log"
