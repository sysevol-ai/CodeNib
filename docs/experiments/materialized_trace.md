<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Mixed-operation materialization trace

## Question

This experiment measures the systems path implied by CodeMiner's central
design: build several views of one exact repository snapshot once, hydrate them
through the production manifest, and serve heterogeneous requests from the
shared artifacts. It asks how large the one-time setup cost is and how that cost
changes per consumer when the same snapshot serves more work.

It is an amortization measurement, not a speedup comparison with a complete
competing system. The rebuild-per-consumer alternative is an accounting
counterfactual only. Static-versus-live LSP compatibility and latency remain a
separate paired experiment.

## Workload

The fixed plan uses all 25 exact snapshots in
`sysevol-ai/codeminer-synthesis`, with 20 source-grounded queries per snapshot.
The source dataset is pinned in the final artifact sidecar to Hub commit
`5ac36c39ef69bbfe2e14dac58b6067b8c350c53e`; all five language-config
fingerprints are recorded under
`materialized_trace_batch_v9/environment/dataset-provenance.json`. The
pre-registered plan predates a first-class dataset-revision field, so the
frozen plan and per-snapshot traces remain the authoritative replay inputs.
Each query emits one Qwen3-Embedding-0.6B L2 request and one BM25 request. Ten
candidate `definition` or `references` requests are available in the
corresponding LSP replay artifact. The plan admits only definition requests
whose static and JSON-RPC location fingerprints were nonempty, stable, and
equal in every measured repetition. Because the minimum eligible definition
count across the 25 snapshots is two, the plan selects exactly two per snapshot
by deterministic hash rank. The final workload therefore contains 42 requests
per snapshot and 1,050 total.

References are intentionally outside the lifecycle-admitted subset. During
resource calibration, an independently rebuilt MicroPython graph changed one
pre-registered reference result from 18 to 16 locations while both selected
definitions retained their exact fingerprints. The calibration run was stopped
before retrieval or latency outcomes were observed. RQ3 still reports reference
compatibility and latency; the lifecycle study does not treat that conditional
result as rebuild stability.

This is a controlled service mix. It is not an empirical distribution of tool
calls from agent trajectories. The 20 synthesis rows are the consumer unit;
one projected session means one source query with the trace's average mix of
one dense request, one BM25 request, and 0.1 admitted definition requests. It is
not a complete agent trajectory. The amortization curve scales this fixed 2.10
requests per session linearly.
The LSP admission decision is made from the preceding cross-provider study,
not from results produced by this materialization run.

## Measured boundary

Cold materialization starts from an already prepared checkout at the exact
`(repository, base_commit)` and builds:

1. the BM25 L2 view;
2. the registered SCIP- or clangd-backed symbol graph and persisted occurrence
   metadata;
3. Qwen3-Embedding-0.6B L0 and L2 vectors plus Flat FAISS indexes; and
4. the repository manifest binding those artifacts to the source commit.

The trace issues L2 dense requests, but the current production vector builder
materializes both L0 and L2. The measured build retains this behavior rather
than adding a benchmark-only level filter.

The Qwen embedding model is pinned to Hugging Face revision
`97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`, which is persisted in the vector
manifest and reused during hydration.
The final physical configuration uses embedding batch size 32 and a 2,048-token
document cap. This configuration was fixed from GPU utilization and memory
calibration before the final batch; retrieval quality is evaluated separately
in RQ1.

The report separates materialization wall time, per-view build time, artifact
bytes, manifest hydration, and steady request execution. Materialization exits
after writing a checkpoint; a fresh Python process then hydrates the manifest
and serves the trace. This removes same-process model/CUDA reuse. Because the
second process follows immediately on the same host, filesystem pages and local
model packages may remain cached; `L` is fresh-process hydration, not cold-disk
I/O. The report also records peak Python RSS, maximum child-process RSS, and
CUDA allocated/reserved memory. Clone, checkout creation, and artifact transfer
are excluded.

## Correctness and failure guards

- Plan and report repository paths and commits must match exactly.
- JSON-RPC LSP lines are converted once from 0-based protocol coordinates to
  the MCP tool's 1-based agent coordinates; characters remain 0-based.
- Independent dense requests clear the one-query embedding cache.
- Each selected definition carries its pre-registered location fingerprint
  from the preceding JSON-RPC comparison. A newly materialized definition must
  reproduce the ordered file/start-line sequence and result count. This is a
  compatibility guard for the admitted requests, not a claim that every LSP
  request or capability is statically replaceable.
- Every measured response is hashed as canonical JSON and must be identical
  across the three repetitions. For retrieval requests this checks replay
  stability rather than relevance; for LSP requests it complements the
  pre-registered cross-provider guard above.
- Error responses fail a snapshot. Empty but valid responses are retained and
  summarized by operation-level nonempty fractions; admitted LSP responses
  cannot be empty.
- A successful materialization checkpoint contains its resource peaks. Resume
  combines that checkpoint with the replay process by taking each peak's
  maximum; it never substitutes warm-load memory for build memory.

## Statistics

Each request receives one warmup and three measured repetitions. Repetitions
are reduced to an operation median within a snapshot. Headline estimates are
medians over the 25 repository snapshots with percentile 95% intervals from
10,000 snapshot bootstrap samples (seed `20260713`). Language summaries contain
five snapshots each and are secondary. Request repetitions are never treated
as independent repositories.

Let `B` be materialization, `L` one fresh-process manifest hydration, `S` one measured
42-request service trace, `N=20` source consumers in that trace, and `q` the
projected number of sequential consumer sessions. Two deployment models are
reported rather than silently assuming that one load serves every process:

- **Artifact-shared, process-isolated (primary/current deployment):** total
  `B + qL + (q/N)S`, or `B/q + L + S/N` per consumer.
- **Shared resident runtime (secondary optimistic bound):** total
  `B + L + (q/N)S`, or `B/q + L/q + S/N` per consumer.

The measured `B`, `L`, and `S` are unchanged between models; only the stated
deployment accounting differs. These are cumulative-work projections; they do
not model concurrent arrival rates, queueing, or throughput saturation.

## Results

The final batch completed all 25 snapshots and served 1,050 distinct requests
for three measured repetitions (3,150 rows). An independent audit verifies the
exact 20 dense + 20 BM25 + 2 definition mix per snapshot, complete repetition
sets, stable canonical response fingerprints, and all 50 pre-registered
definition guards. The audit reports zero issues.

Across snapshots, median full production materialization is 116.70 seconds
(95% bootstrap CI 65.61--153.87), fresh-process hydration is 7.40 seconds
(6.99--8.07), and one warm 42-request trace is 0.727 seconds
(0.593--1.079). Median component build times are 0.81 seconds for BM25, 37.97
seconds for the semantic graph, and 59.48 seconds for the L0+L2 vector views.
The median artifact is 160.3 MiB; median peak process RSS is 4.40 GiB and median
peak CUDA allocation is 19.88 GiB. MicroPython and SymPy remain in the sample
as vector- and graph-construction long tails, respectively.

At the measured `q=20`, projected effective cost is 13.20 seconds per session
(10.43--15.96) when each client hydrates independently and 6.24 seconds
(3.66--8.11) for one shared resident runtime. At `q=100`, the corresponding
medians are 8.53 and 1.27 seconds. The difference is an accounting consequence
of repeating the measured hydration step, not a measured speedup against
another system. The experiment does not model concurrency, queueing,
incremental updates, artifact transfer, or end-to-end agent trajectories.

## Reproduce

```bash
python scripts/profiling/build_materialized_trace_plan.py \
  --dataset-revision 5ac36c39ef69bbfe2e14dac58b6067b8c350c53e \
  --artifact-root /mnt/data/codeminer/results/lsp_agent_base_artifacts_v5 \
  --lsp-report-dir /mnt/data/codeminer/results/lsp_replay_base_v3_100/reports \
  --output-dir /mnt/data/codeminer/results/materialized_trace_plan_v3

env \
  PATH="/tmp/codeminer-scip-tools/go-tools/bin:/tmp/codeminer-scip-tools/go/bin:$PATH" \
  PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
  python scripts/profiling/run_materialized_trace_batch.py \
    --plan /mnt/data/codeminer/results/materialized_trace_plan_v3/plan.json \
    --output-root /mnt/data/codeminer/results/materialized_trace_batch_v9 \
    --embedding-model Qwen/Qwen3-Embedding-0.6B \
    --embedding-revision 97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3 \
    --embedding-dimension 1024 \
    --embedding-batch-size 32 \
    --embedding-max-seq-length 2048 \
    --warmup-repetitions 1 \
    --measured-repetitions 3

python scripts/profiling/aggregate_materialized_trace.py \
  --plan /mnt/data/codeminer/results/materialized_trace_plan_v3/plan.json \
  --reports-root /mnt/data/codeminer/results/materialized_trace_batch_v9 \
  --output /mnt/data/codeminer/results/materialized_trace_batch_v9/summary.json
```

The aggregator is strict: it refuses partial batches, identity mismatches,
missing resource checkpoints, incomplete repetitions, or inconsistent
operation sets. Do not use a partial summary as paper evidence.

The batch summary also records the plan hash, run parameters, Git base, and a
content hash over runtime Python sources. The runner rechecks that source hash
before each snapshot and stops if it changes, preventing one long batch from
mixing implementations.
The v9 bundle additionally archives those exact hashed sources as
`environment/runtime-source.tar.gz`, captures Python/CUDA/hardware/toolchain
versions, and copies the plan and traces under `protocol/`; each directory has
a relative-path `SHA256SUMS` file.
