<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# agent_compile scripts — map for researchers

Tooling for the **agent-compile RFC (#133)**: does selecting a per-scenario
*subset* of skills (Compile → Agent → Route) change cost/accuracy vs a plain
agent? The findings live in [`docs/experiments/agent_compile.md`]; the design in
[`docs/agent_compile_design.md`].

## Phase lineage (why names mention "phase 0 / 2")

The RFC was planned in phases. **Scripts exist only for the phases that run
data** — which is why the names appear to jump 0 → 2 (Phase 1 is code + a doc,
not a sweep):

| Phase | What | Artifact |
|---|---|---|
| 0 — kill switch | A0/A6 on the fit pool; is the saving worth building? | `run_sweep.py` + `aggregate.py` |
| 1 — `classify()` | the design doc + `codeminer/agent/compile.py` + tests | *(code/doc, no script)* |
| 2 — subset sweep | A0..A6 × models × harnesses | `run_sweep.py` + `aggregate_phase2.py` |
| 3 — wire runtime | `AgentRunner` reads the `compile_table` | (runtime code) |
| 4 — held-out validation | run the derived table on the 70 held-out | `partition.py` (held-out side) |

The **cost/ablation study** (graph-vs-retrieval, LocAgent) was added later and
runs on a separate, simpler track (prebuilt indexes + reps), below.

## Scripts

| file | purpose | track | status |
|---|---|---|---|
| `partition.py` | split instances into **fit (~30)** / **held-out (~70)** by repo, with per-language + per-scenario quotas. The fit pool is where the `compile_table` is *derived*; held-out *validates* it. **No model training** — it's a lookup-table fit/validate split. | Phase 0/4 | active |
| `run_sweep.py` | original sweep driver: clone + index repos, full skill catalog. | Phase 0/2 | legacy |
| `aggregate.py` | Phase-0 kill-switch report. | Phase 0 | **dead** (superseded) |
| `aggregate_phase2.py` | A0–A6 aggregator: skill-invocation histogram, `compile_table` derivation, easy/hard saturation guard. | Phase 2 | active (sweep) |
| `prebuilt.py` | stage the prebuilt `/mnt/data/codeminer` indexes (vector + graph, fresh BM25) into a cache. Shared by the cost track. | shared | active |
| `run_sample_sweep.py` | **primary runner for the cost study**: one agent cell per (subset × instance × rep) on prebuilt indexes; honors the `cost_*` configs (incl. `default_tool_ids`, `system_prompt`). | cost/ablation | active |
| `compare_harnesses.py` | compare two run dirs **accuracy-first**: mean accuracy + median-of-reps tokens, paired per-instance, with a Student-t 95% CI. *(was `aggregate_proof.py`)* | cost/ablation | active |
| `graph_recall_ablation.py` | retrieval-only (no LLM): does the call-graph recover a target file that **deep search misses**? Scans the dataset. | cost/ablation | active |
| `graphrag_retrieve.py` | run the GraphRAG composer as a **standalone retrieval method** (files@k recall; `--no-graph` to ablate the graph). | cost/ablation | active |

## Configs (`configs/agent_compile/*.yaml`)

| config | harness (agent tools) | result dir | role |
|---|---|---|---|
| `cost_grep` | file_read + file_search (grep) | `proof_grep` | **baseline** (efficient frontier) |
| `cost_context` | `codeminer_context` (GraphRAG) + defaults | `proof_ctx2` | +graph composer (and search-only via `CODEMINER_COMPOSER_NO_GRAPH=1`) |
| `cost_free` | bm25 + embedding + defaults | `cost_free_*` | search-enabled agent |
| `cost_locagent` | bm25 + graph verbs; file_read only (no grep) | `proof_locagent` | LocAgent (content-bm25) — graph unused |
| `cost_locagent_strict` | bm25 + graph + read_code_block; no fs | `proof_locagent_strict` | strict graph-primary |
| `cost_locagent_faithful` | **bm25_names** + graph + read_code_block; no fs | `proof_locagent_faithful` | faithful LocAgent (names-only) |
| `cost_structured` | embedding + graph_expand; defaults withheld | `cost_structured_*` | early cost study |
| `cost_graphfriendly` | bm25 + embedding + callers/callees/trace | — | **stale** (never run) |
| `phase0`, `sample` | A0–A6 full catalog | `sample*` | original Phase-0/2 sweep |

## Proposed cleanup (not yet applied — needs sign-off; touches tests + #172)

Intent-based renames for the dead/confusing files:
`aggregate.py → DELETE` · `aggregate_phase2.py → aggregate_subset_sweep.py` ·
`run_sweep.py → run_catalog_sweep.py` · `run_sample_sweep.py → run_agent_sweep.py`
· delete `phase0.yaml`, `cost_graphfriendly.yaml`. These are cross-referenced by
tests, so they go in a dedicated rename commit with reference updates.
