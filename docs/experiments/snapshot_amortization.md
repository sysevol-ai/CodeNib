<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Snapshot view amortization

## Question and scope

This analysis asks a narrow accounting question: when several workload records
share the exact `(repository, base_commit)`, how much does one dense-view build
contribute per consumer if that view is materialized once?

It does **not** establish end-to-end system break-even. The measured boundary is
the non-overlapping L2 embedding-plus-FAISS construction timer. It excludes
checkout, chunking, lexical and graph construction, artifact load, and query
execution. Rebuilding the identical view for every consumer is an accounting
counterfactual, not a claim about the strongest competing system.

## Protocol

The analyzer keys reuse by `SourceSnapshot`, requires one complete build profile
per exact snapshot, and rejects mixed embedding identities. Construction time is
read from `vector_store_add_documents_l2` when available, otherwise from
`embedding_encode_l2 + faiss_index_add_l2`. Nested `total_duration` and
`build_vector_store` sections are deliberately ignored.

```bash
HF_HOME=/mnt/conda/huggingface \
python scripts/profiling/analyze_snapshot_amortization.py \
  --dataset sysevol-ai/codeminer-synthesis \
  --profile-dir /mnt/data/codeminer/profile_log \
  --embedding-model Qwen/Qwen3-Embedding-0.6B \
  --profile-tag codeminer_base_test \
  --level l2 \
  --output-json \
    /mnt/data/codeminer/results/snapshot_amortization_qwen06_synth.json \
  --output-markdown \
    /mnt/data/codeminer/results/snapshot_amortization_qwen06_synth.md
```

## Result

The 500 synthesis records map to 25 exact snapshots, with 20 consumers per
snapshot. Building the Qwen3-Embedding-0.6B L2 view once per snapshot takes
1,838.8 seconds in total, or 3.678 seconds per consumer when spread over this
trace. The corresponding rebuild-per-consumer accounting total is 36,776.6
seconds, so exact-snapshot reuse avoids 475 of 500 identical builds (95%).

This result supports only the statement that the synthesis trace contains
enough repeated snapshots to amortize the measured dense view. It does not show
that CodeMiner beats a live service or another persistent index.

## Successor experiment

The paper-level successor replays one fixed mixed-operation trace under a
shared output contract and reports these phases separately:

1. complete production view materialization from a prepared checkout, with
   clone and checkout creation excluded;
2. fresh-process artifact hydration with warm filesystem/model-package caches;
3. steady retrieval and symbol-navigation requests;
4. artifact size, peak resources, and projected cost as sessions per snapshot
   increase.

That experiment is complete over all 25 synthesis snapshots; its protocol and
results are in `docs/experiments/materialized_trace.md`. Median full
materialization, fresh-process hydration, and warm 42-request service are
116.7, 7.40, and 0.727 seconds. The experiment reports process-isolated and
shared-resident deployment accounting but no comparator or crossover: the
mixed static/live operation classes do not have one measured, semantically
admissible non-materialized baseline. The earlier dense-only result above
remains useful component provenance, not the paper headline.
