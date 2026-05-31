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

## Configs (`scripts/agent_compile/configs/*.yaml`)

Default tools are now **`read` / `grep` / `glob` / `bash`** (#184) and are a
separate type from skills — they live in the runner's `ToolRegistry`, not the
skill registry, so they are never narrowed by `allow_skills`.

**`design_space.yaml` is the canonical config**: one config with nine isolating
arms (`defaults_only`, `bm25`, `embedding`, `bm25_embedding`, `hybrid`,
`rerank`, `graphnav`, `composer`, `everything`) on the SAME 8 instances / model
/ reps so every arm is directly co-plottable. It supersedes the fragmented
single-arm `cost_*` configs (which used different instance slices and reps and
could not be compared head-to-head). Prefer it for new runs.

| config | harness (agent tools) | role |
|---|---|---|
| **`design_space`** | all 9 arms, full defaults, neutral prompt | **canonical co-plottable sweep** |
| `cost_grep` | read/grep/glob/bash only | single-arm grep/read baseline (== `defaults_only`) |
| `cost_context` | `codeminer_context` + defaults | graph composer on 3 hard instances (`CODEMINER_COMPOSER_NO_GRAPH=1` to ablate the graph) |
| `cost_free` | bm25 + embedding + defaults | search-enabled agent on 3 hard instances |
| `cost_structured` | embedding + find_callers/callees/trace; defaults withheld | structured (no-fs) path probe |
| `cost_locagent` | bm25 + graph verbs; `read` only (no grep) | LocAgent-style graph-primary harness |
| `phase0`, `sample` | A0–A6 ladder (pruned skill set) | original Phase-0/2 catalog sweep (legacy track) |

Removed in the skill redesign: `cost_everything` (subsumed by
`design_space.everything`), `cost_locagent_strict` / `cost_locagent_faithful`
(relied on the removed `read_code_block` skill; the graph-primary story is now
the single `cost_locagent` arm), and `cost_graphfriendly` (stale, never run).
Cut skills (`graph_expand`, `impact_analysis`, `regex_search`,
`embedding_rerank`, `query_transform`, `read_code_block`, `bm25_names` →
merged into `bm25_search names_only`) no longer appear in any config.

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
`run_sweep.py → run_catalog_sweep.py`, `partition.py` (`test_partition.py`).
(`cost_graphfriendly.yaml` was deleted in the skill redesign; `phase0.yaml` was
kept and its skill ladder updated to the pruned set.) A dedicated commit with
reference + test updates can land the renames once an owner confirms.
