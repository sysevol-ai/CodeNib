#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
CODENIB_HOME="${CODENIB_HOME:-$HOME/.codenib}"
CODENIB_PREBUILT_DIR="${CODENIB_PREBUILT_DIR:-${CODENIB_HOME}/prebuilt}"

STORAGE_DIR="${STORAGE_DIR:-${CODENIB_PREBUILT_DIR}}"
DATASET="princeton-nlp/SWE-bench_Lite"
SPLIT="dev"
LANGS=("python")

PRIMARY_MODEL="Salesforce/SweRankEmbed-Small"
PRIMARY_PROVIDER="huggingface"
PRIMARY_DIM=768

SECONDARY_MODEL="${SECONDARY_MODEL:-fishmingyu/SweRankEmbed-Large}"
SECONDARY_PROVIDER="huggingface"
SECONDARY_DIM=3584
PROFILE_TAG="${PROFILE_TAG:-${SPLIT}_emb_build}"

mkdir -p "${STORAGE_DIR}"

# echo "Building primary embeddings (${PRIMARY_MODEL})..."
# python scripts/embeddings/build_embeddings.py \
#   --dataset "${DATASET}" \
#   --split "${SPLIT}" \
#   --storage-dir "${STORAGE_DIR}" \
#   --languages "${LANGS[@]}" \
#   --embedding-model "${PRIMARY_MODEL}" \
#   --embedding-provider "${PRIMARY_PROVIDER}" \
#   --embedding-dimension "${PRIMARY_DIM}" \
#   "$@"

echo "Building secondary embeddings (${SECONDARY_MODEL})..."
python scripts/embeddings/build_embeddings.py \
  --dataset "${DATASET}" \
  --split "${SPLIT}" \
  --storage-dir "${STORAGE_DIR}" \
  --profile-tag "${PROFILE_TAG}" \
  --languages "${LANGS[@]}" \
  --embedding-model "${SECONDARY_MODEL}" \
  --embedding-provider "${SECONDARY_PROVIDER}" \
  --embedding-dimension "${SECONDARY_DIM}" \
  "$@"

echo "Done. Embeddings stored under ${STORAGE_DIR}"
