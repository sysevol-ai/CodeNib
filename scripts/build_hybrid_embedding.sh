#!/usr/bin/env bash
set -euo pipefail

# Build hybrid embeddings using a strong primary model and a lighter secondary one.
# Primary: jinaai/jina-code-embeddings-1.5b
# Secondary: Salesforce/SweRankEmbed-Small

PRIMARY_MODEL="jinaai/jina-code-embeddings-1.5b"
SECONDARY_MODEL="Salesforce/SweRankEmbed-Small"

python scripts/build_embeddings.py \
  --hybrid \
  --embedding-model "${PRIMARY_MODEL}" \
  --embedding-provider "huggingface" \
  --embedding-dimension 1536 \
  --secondary-embedding-model "${SECONDARY_MODEL}" \
  --secondary-embedding-provider "huggingface" \
  --secondary-embedding-dimension 768 \
  --high-importance-ratio 0.3 \
  --min-high-importance-lines 20 \
  "$@"
