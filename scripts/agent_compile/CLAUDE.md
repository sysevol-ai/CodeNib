# scripts/agent_compile/ — rules

Tooling for the **agent-compile RFC (#133)**: does selecting a per-scenario
*subset* of skills (Compile → Agent → Route) change cost/accuracy vs a plain
agent? Start from [`README.md`](./README.md) — it maps every script to its RFC
phase. Findings: [`docs/experiments/agent_compile.md`](../../docs/experiments/agent_compile.md);
design: [`docs/agent_compile_design.md`](../../docs/agent_compile_design.md).

## Things to know before editing

- **Phase names skip 0 → 2 on purpose.** Scripts exist only for phases that run
  data; Phase 1 (`classify()`) is code + a doc, not a sweep. `run_sweep.py` +
  the consolidated `aggregate.py` drive both Phase 0 and the Phase 2 subset
  sweep (the old `aggregate_phase2.py` was evicted in the scripts dedup). Don't
  "fix" the numbering.
- **Indexes reach the agent through `RepoManifest`.**
  `IndexCompiler.compile_repo()` (in `codenib/compiler/`) builds `bm25` /
  `vector` / `symbol_graph` and writes `repo_manifest.json` — the source of
  truth for what exists, its freshness, and status. Skill contexts load either
  JIT (`build_skill_contexts`, builds missing indexes) or AoT
  (`load_contexts_from_manifest`, trusts a prebuilt manifest). `AgentRunner`
  also uses the manifest for a `ResourceGuard` preflight that excludes skills
  whose indexes are missing/stale.
- **The cost/ablation study runs on a separate, simpler track** (prebuilt
  indexes + reps), not the full subset sweep — see the README before assuming a
  script belongs to the main sweep.

## Verified findings live in memory, not here

Sweep results, env specifics (model endpoints, embedding model, prebuilt-index
paths) and known measurement bugs are recorded in the project memory notes on
agent-compile — consult those for "what the numbers were", and keep this file to
*how the code is structured*.
