#!/usr/bin/env bash
set -euo pipefail

# Build SWE-bench Lite dev split ground-truth localization metadata.

OUTPUT_PATH="${OUTPUT_PATH:-$HOME/.codeminer/swebench_lite_gt_dev.json}"
FILTER="${FILTER:-.*}"
LIMIT="${LIMIT:-}"

CMD=(python codeminer/dataset/gt_locate.py
  --dataset lite
  --split dev
  --output "${OUTPUT_PATH}"
  --filter "${FILTER}"
)

if [[ -n "${LIMIT}" ]]; then
  CMD+=(--limit "${LIMIT}")
fi

"${CMD[@]}"

echo "Saved dev GT metadata to ${OUTPUT_PATH}"
