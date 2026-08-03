#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
CODENIB_HOME="${CODENIB_HOME:-$HOME/.codenib}"
CODENIB_PREBUILT_DIR="${CODENIB_PREBUILT_DIR:-${CODENIB_HOME}/prebuilt}"
CODENIB_RESULTS_DIR="${CODENIB_RESULTS_DIR:-${CODENIB_HOME}/results}"

# Profile build time for multiple embedding models on a single instance.

DATASET="${DATASET:-princeton-nlp/SWE-bench_Lite}"
SPLIT="${SPLIT:-test}"
INSTANCE_ID="${INSTANCE_ID:-astropy__astropy-12907}"
STORAGE_DIR="${STORAGE_DIR:-${CODENIB_PREBUILT_DIR}}"
PROFILE_DIR="${PROFILE_DIR:-${CODENIB_RESULTS_DIR}/profile_log}"
LANGS=("python")

MODELS=(
  # "Salesforce/SweRankEmbed-Small:768"
  # "jinaai/jina-code-embeddings-0.5b:896"
  # "jinaai/jina-code-embeddings-1.5b:1536"
  # "BAAI/bge-code-v1:1536"
  "fishmingyu/SweRankEmbed-Large:3584"
)

mkdir -p "${STORAGE_DIR}" "${PROFILE_DIR}"

for entry in "${MODELS[@]}"; do
  MODEL="${entry%%:*}"
  DIM="${entry##*:}"
  echo "Profiling ${MODEL} (dim=${DIM}) on ${INSTANCE_ID}"
  python scripts/embeddings/build_embeddings.py \
    --dataset "${DATASET}" \
    --split "${SPLIT}" \
    --filter-instance "^(${INSTANCE_ID})$" \
    --storage-dir "${STORAGE_DIR}" \
    --profile-dir "${PROFILE_DIR}" \
    --enable-profiler \
    --force-rebuild \
    --languages "${LANGS[@]}" \
    --embedding-model "${MODEL}" \
    --embedding-provider "huggingface" \
    --embedding-dimension "${DIM}" \
    "$@"
done

echo "Done. Profiles saved under ${PROFILE_DIR}"
