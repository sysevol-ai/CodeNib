<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
SPDX-License-Identifier: Apache-2.0
-->

# ADR: Frozen 30/70 fitting/held-out partition (issue #151)

Sub-issue of the Agent Router RFC (#133). This freezes the fitting / held-out
split + per-instance scenario labels that every Phase 0/2 experiment cell
consumes. The held-out side must stay untouched through Phase 0 and the
Phase 2 sweep.

## Corpus (reused, not re-collected)

`fishmingyu/codeminer-base-dataset` (split `test`) — the same ~100-instance
corpus the retrieval baselines load (`examples/{bm25,embedding,graph}_retrieve_baseline.py`,
`examples/codeminer_base_rerank_matrix.py`). 100 instances, 5 language groups,
5 repos per group (~4 instances/repo).

## Algorithm

Driver: `scripts/agent_compile/build_codeminer_base_partition.py`. It reuses
the existing repo-level split (`scripts/agent_compile/partition.py:build_partition`)
and the canonical stack-trace detector
(`codeminer.agent.compile.detect_stacktrace`) — no logic is reimplemented.

- **Repo-level (not instance-level) assignment.** Whole repos are
  deterministically shuffled per language and assigned to `fit` until the
  per-language quota (`fit_size / num_languages` ≈ 6) is met; the rest go to
  `heldout`. A repository never spans both splits, so no held-out repo leaks
  into fitting.
- **Scenario-coverage check + reseed.** Each `(language, has_stacktrace)` cell
  the corpus can fill (corpus n ≥ 2) must hold ≥ 2 fitting instances; otherwise
  reseed up to 3× then accept and flag. Cells the corpus cannot fill are
  flagged as scarce.
- **Determinism.** The only entropy is a fixed integer seed literal
  (`DEFAULT_SEED = 12`). Re-running emits byte-identical artifacts.

### Scenario labels

- `has_stacktrace` — `detect_stacktrace` over `problem_statement + hints_text`
  (Python `Traceback`, Go `panic:` / goroutine dump, Rust `thread '..'
  panicked`, JS/Node `at fn (file:line)`, C/C++ backtrace frames).
- `language` — the corpus `language_group` (repo primary language), used
  verbatim as the 5-way partition axis.

## Frozen partition (seed = 12, attempts = 3, 40 fit / 60 held-out)

`DEFAULT_SEED = 12` is chosen because it is the fixed literal that lands both
*satisfiable* stack-trace cells (`C++/C` corpus n=3, `Go` corpus n=2) with ≥ 2
fitting instances. The fit side is ~40 rather than exactly 30 because
whole-repo granularity (repos are ~4 instances) overshoots the per-language
quota of 6 to ~8.

| Language | Fitting repos | Held-out repos |
|----------|---------------|----------------|
| C++/C | fmtlib/fmt, micropython/micropython | jqlang/jq, redis/redis, valkey-io/valkey |
| Go | gin-gonic/gin, prometheus/prometheus | caddyserver/caddy, gohugoio/hugo, hashicorp/terraform |
| Python | astropy/astropy, sympy/sympy | matplotlib/matplotlib, pydata/xarray, scikit-learn/scikit-learn |
| Rust | nushell/nushell, tokio-rs/tokio | astral-sh/ruff, sharkdp/bat, uutils/coreutils |
| TypeScript/JavaScript | axios/axios, facebook/docusaurus | babel/babel, preactjs/preact, vuejs/core |

### Verification log

Scenario cells (fit / held-out / corpus):

| Cell | fit | held-out | corpus |
|------|----:|---------:|-------:|
| C++/C : no_stacktrace | 4 | 13 | 17 |
| C++/C : stacktrace | 3 | 0 | 3 |
| Go : no_stacktrace | 7 | 12 | 19 |
| Go : stacktrace | 2 | 0 | 2 |
| Python : no_stacktrace | 8 | 11 | 19 |
| Python : stacktrace | 0 | 1 | 1 |
| Rust : no_stacktrace | 8 | 12 | 20 |
| TypeScript/JavaScript : no_stacktrace | 8 | 10 | 18 |
| TypeScript/JavaScript : stacktrace | 0 | 1 | 1 |

**Flagged imbalance (`imbalanced: true`)** — structural, not fixable by
reseeding: the corpus has only 1 (`Python`, `TypeScript/JavaScript`) or 0
(`Rust`) stack-trace instances, so those fitting cells cannot reach 2. Every
cell the corpus *can* fill (`C++/C`, `Go` stacktrace) is covered.

## Outputs (both committed)

- `data/agent_compile/partition.json` — the partition in the schema
  `run_sweep.py` consumes (`fit` / `heldout` / `cells` / `warnings` / ...),
  plus a `labels` table and a `scenario` summary. Whitelisted in `.gitignore`
  (global `*.json` ignore).
- `data/agent_compile/partition_labels.csv` — flat per-instance labels
  (`instance_id, repo, language, has_stacktrace, split`) for the eval harness
  + CAR runtime.

## Reproduce

```bash
python scripts/agent_compile/build_codeminer_base_partition.py --source cache
```

`--source cache` (default `auto`) reads the local
`~/.codeminer/fishmingyu__codeminer-base-dataset_test.json` cache with no heavy
dependencies; `--source hf` re-fetches via `CodeMinerBaseDataset`. Invariants
(disjointness, ~30/70 sizes, label completeness, satisfiable-cell coverage) are
checked in `test/agent/test_build_codeminer_base_partition.py`.
