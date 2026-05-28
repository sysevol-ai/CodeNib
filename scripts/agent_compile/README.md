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

## How indexes reach the agent: RepoManifest ↔ IndexCompiler ↔ harness

Three layers, and the cost-study harness deliberately uses only part of them:

- **`IndexCompiler.compile_repo(repo, types)`** (`codeminer/compiler/index_compiler.py`)
  builds the indexes (`bm25`, `vector`, `symbol_graph`) and writes a
  **`RepoManifest`** (`repo_manifest.json`) — the *linking protocol* recording,
  per index: `path`, `config`, `built_at`, `status` (fresh/failed), plus derived
  capabilities. It's the source of truth a later run trusts instead of
  re-deciding what exists.
- **Getting indexes into agent skill `contexts`** has two routes:
  - **JIT** — `build_skill_contexts(skill_ids, cache_dir, rebuild=…)`: infers the
    index types the skills need (from each skill's `index_requirements`), builds
    any missing via `IndexCompiler`, loads from the convention layout
    `cache_dir/<type>`. No manifest needed.
  - **AoT** — `load_contexts_from_manifest(manifest, skill_ids)`: loads index
    paths/configs straight from a prebuilt manifest, validating freshness/status.
- **At runtime**, `AgentRunner(manifest=…)` also uses the manifest for a
  `ResourceGuard` preflight — skills whose indexes are missing/stale are excluded.

**Where our harness sits:** the cost study **bypasses the manifest**.
`prebuilt.py` symlinks the prebuilt `/mnt/data/codeminer` `vector` + `graph.pkl`
into the `cache_dir/<type>` layout and builds BM25 fresh; then `run_agent_sweep`
calls `build_skill_contexts(rebuild=False)` to *load* them via the JIT
convention path. `AgentRunner` runs **without** a manifest (no ResourceGuard) and
gets its skill subset directly via `allow_skills`. So `IndexCompiler` +
`RepoManifest` are the **production AoT path** (Phase 3 "wire runtime"); the
experiments ride the simpler convention loader because the indexes already exist.

The **`compile_table` is orthogonal** to all of this: it maps *scenario → skill
subset* (derived by `partition` + `aggregate_phase2`), i.e. *which skills to
allow* — whereas the manifest is about *which indexes exist*.

## Scripts

| file | purpose | track | status |
|---|---|---|---|
| `partition.py` | split instances into **fit (~30)** / **held-out (~70)** by repo, with per-language + per-scenario quotas. The fit pool is where the `compile_table` is *derived*; held-out *validates* it. **No model training** — it's a lookup-table fit/validate split. | Phase 0/4 | active |
| `run_sweep.py` | original sweep driver: clone + index repos, full skill catalog. | Phase 0/2 | legacy |
| `aggregate.py` | Phase-0 kill-switch report. | Phase 0 | **dead** (superseded) |
| `aggregate_phase2.py` | A0–A6 aggregator: skill-invocation histogram, `compile_table` derivation, easy/hard saturation guard. | Phase 2 | active (sweep) |
| `prebuilt.py` | stage the prebuilt `/mnt/data/codeminer` indexes (vector + graph, fresh BM25) into a cache. Shared by the cost track. | shared | active |
| `run_agent_sweep.py` | **primary runner for the cost study**: one agent cell per (subset × instance × rep) on prebuilt indexes; honors the `cost_*` configs (incl. `default_tool_ids`, `system_prompt`). *(was `run_sample_sweep.py`)* | cost/ablation | active |
| `compare_harnesses.py` | compare two run dirs **accuracy-first**: mean accuracy + median-of-reps tokens, paired per-instance, with a 95% **confidence interval** (Student-t). *(was `aggregate_proof.py`)* | cost/ablation | active |
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

## "CI" disambiguation

In this directory and in `docs/experiments/agent_compile.md`, **CI = confidence
interval** (the 95% band on a mean token-delta), **not** continuous integration.

## Naming cleanup — done + pending

**Done** (files owned by this work, no test coupling):
`aggregate_proof.py → compare_harnesses.py`, `run_sample_sweep.py →
run_agent_sweep.py`.

**Pending — needs maintainer sign-off** (these have their *own tests* and are
not from this work, so they are NOT renamed/deleted here):
`aggregate.py` (Phase-0, superseded — `test_aggregate.py`),
`aggregate_phase2.py → aggregate_subset_sweep.py` (`test_aggregate_phase2.py`),
`run_sweep.py → run_catalog_sweep.py`, `partition.py` (`test_partition.py`),
and deleting the stale `phase0.yaml` / `cost_graphfriendly.yaml`. A dedicated
commit with reference + test updates can land these once an owner confirms.
