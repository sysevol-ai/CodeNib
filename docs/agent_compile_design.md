<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Agent compile (CAR) — design ADR

Status: **Partly superseded** (was Draft, Phase 1 — issue #133)
Owners: see issue #133 thread
Last revised: 2026-06-01

> ## Status (2026-06)
>
> The live experiment is the **design-space cost study** —
> `scripts/agent_compile/configs/design_space.yaml`, 9 arms isolating one
> skill-axis each, on the full split (see
> [`scripts/agent_compile/README.md`](../scripts/agent_compile/README.md)).
> Relative to the roster/sweep tables below: `graph_expand` and several other
> skills were pruned (#196); the A0–A6 subset sweep is retired in favour of the
> design-space arms; the deterministic `(language, has_stacktrace)` scenario
> classifier is kept (the runner records it per cell). The CAR router runtime
> (`codeminer/agent/compile.py`, `compile_table`) and the frozen fit/held-out
> partition (#190) still exist; no compile_table is fitted today. The rest of
> this document is the historical router design.

This document pins the design decisions agreed in the
[issue #133 RFC](https://github.com/sysevol-ai/CodeMiner/issues/133) thread
so the implementation phases can proceed without re-litigating them.

## Naming

The feature is referred to by two names that mean the same thing:

* **agent compile** / `compile_table` — the descriptive name used in
  the parent RFC and the codebase (`codeminer/agent/compile.py`,
  `load_compile_table`, etc.). Keep this in code.
* **CAR (Compile-Adaptive Router)** — short alias used in
  sub-issue titles, dashboards, and casual references (e.g.
  "the CAR column in the Phase 2 sweep").

The two are interchangeable. Code uses `compile_table`; prose may use
either, but prefer `CAR` for sub-issue cross-references.

## Context

CodeMiner's agent today receives the full skill registry on every turn,
paying tool-choice fan-out cost even when a query is trivially served by a
single skill. The agent-compile RFC adds a small layer that runs **once at
agent entry**: `classify(query, repo_meta) → Scenario`, then
`compile_table[Scenario] → skill subset`, so the LLM only sees the skills
expected to matter for that scenario.

Phase 0 measures whether the savings are worth building the layer. If A6
(full registry) is already cheap and accurate, the RFC closes and we open
a follow-up to reframe as a caching layer (see "Kill switch" below).

## Skill roster (read-only through Phase 2)

The current registry has 9 skills. Phase 2 measures subset utility on the
fitting pool; through Phase 2 the registry is frozen.

| Skill ID            | Type       | Cost   | Role             |
|---------------------|------------|--------|------------------|
| `bm25_search`       | retrieval  | low    | sweep variable   |
| `regex_search`      | retrieval  | low    | sweep variable   |
| `embedding_search`  | retrieval  | high   | sweep variable   |
| `hybrid_search`     | aggregate  | high   | sweep variable   |
| `graph_expand`      | expand     | medium | sweep variable   |
| `embedding_rerank`  | rerank     | medium | sweep variable   |
| `llm_rerank`        | rerank     | high   | sweep variable   |
| `query_transform`   | transform  | medium | sweep variable   |
| `code_to_query`     | transform  | medium | sweep variable   |

`file_read` referenced in the RFC subset table (A0..A6) is the conceptual
always-on substrate (the agent can always read a file by path); it is not
a registered retrieval skill and is not part of the sweep.

### A0..A6 subsets

Per RFC §"Skill subset sweep":

| ID | Skills |
|----|--------|
| A0 | `bm25_search` |
| A1 | `embedding_search` |
| A2 | `bm25_search` + `embedding_search` |
| A3 | `bm25_search` + `graph_expand` |
| A4 | `bm25_search` + `embedding_search` + `graph_expand` |
| A5 | A4 + `regex_search` |
| A6 | full registry (all 9) |

## Scenario dimensions

Per RFC v2 (after the structural review collapsed dimensions from 4 → 2 to
keep cells statistically meaningful at N=30):

| Dimension | Values | Source |
|-----------|--------|--------|
| `language` | python / go / rust / cpp / c / typescript / javascript / unknown | `SessionContext.primary_language` → `normalize_language()` |
| `has_stacktrace` | True / False | `detect_stacktrace(query)` — regex sweep over the query |

Dropped vs v1: `query_length_bucket`, `query_concreteness` (latter was
flagged as fragile in the structural review). Repo-side dimensions
(`repo_size_bucket`, `dir_depth`) are **collected** in Phase 0 metadata
but not in the v1 scenario table — Phase 2 ANOVA decides whether they
explain residual variance worth adding.

### `has_stacktrace` regexes

See `codeminer/agent/compile.py::_STACKTRACE_PATTERNS`. Patterns match
*structural* runtime markers (e.g. Python `Traceback (most recent call
last):` banner, Rust `thread 'X' panicked at`, Go `^panic: ` followed by
a goroutine frame, Node `    at name (file:line)` frames, JVM
`\tat fqn(file:line)` frames, GDB-style `#N 0x... in fn`). Plain prose
mentioning "traceback" or "panic" does **not** flip the flag (tested in
`test/agent/test_compile.py::TestDetectStacktrace::test_prose_does_not_match`).

### Classifier implementation

Deterministic rules per RFC open question 3. LLM-driven classification
is explicitly **out of scope for v1** — revisit after Phase 2 data.

## Partition

* **Fitting pool:** 30 instances on SWE-bench Multilingual.
* **Held-out pool:** 70 instances (validation in Phase 4).
* **Split level:** **repo, not instance**, to avoid intra-repo leakage
  (per RFC v2 partition algorithm and fishmingyu's "Smaller things"-f).
* **Per-language quota:** `fit_size / num_languages`, rounded with the
  first languages getting the remainder.
* **Coverage check:** every reachable `(language, has_stacktrace)` cell
  must carry ≥ 2 fitting instances. Reseed up to 3× on under-fill, then
  accept + warn. Cells the corpus cannot fill (e.g. a language with no
  stacktrace instances) bypass reseed and emit a "corpus too thin"
  warning that the ADR's reviewer must accept.
* **Frozen seed:** `20260515` (today's date as an integer, kept readable).
  Frozen partition lives at `data/agent_compile/partition.json`.

The algorithm + tests live in `scripts/agent_compile/partition.py` and
`test/agent/test_partition.py`.

## Phase 0 — kill switch

**Goal:** measure A6 cost ceiling on the fitting pool. Decide whether
the agent-compile layer is worth building before any classifier or
table work.

* **Subsets:** `{A0, A6}` only.
* **Model:** `claude-sonnet-4-6` (current frontier — replaces the
  `vertex_ai/gemini-2.5-flash` default in `examples/skill_agent_eval.py`,
  flagged stale in the RFC thread).
* **`max_turns`:** **20** (locked per fishmingyu 2026-05-08 — capping
  shorter risks artificial differentiation between subsets purely from
  the cap-hit rate).
* **Repetition:** min-of-3 per (instance, subset), matching MihirJagtap's
  noise-handling ritual from #131. Wall-time / token noise is one-sided
  (GC, network, provider hiccups); the *minimum* across reps is the
  closest estimate to the true lower bound.
* **Metrics:** `files@k`, `symbols@k` (k ∈ {1, 3, 5}), prompt/completion/
  total tokens, total turns, cap-hit rate, wall-time, cost (when
  `litellm.cost_per_token` resolves), per-skill invocation_rate +
  conditional_success_rate.

### Kill thresholds

The RFC closes (and we pivot to a caching follow-up) if **both** of:

* A6 mean total tokens / query < **30 000**
* A6 files@5 ≥ **0.55**

These thresholds are intentionally permissive — agent-compile is
expensive engineering, so the bar to keep it is "A6 already
saturated *or* nearly so." Tighten in a follow-up ADR if Phase 0
data motivates it.

## Wiring decisions

* **BM25 indexing always uses chunks** (`examples/skill_agent_eval.py`
  builds `BM25CodeIndexer(chunks=chunks, ...)` unconditionally). Resolved
  by siriuxyu 2026-05-09. Means Phase 2's `language` dimension is
  **decoupled from #109's BM25 tokenization discussion** — Phase 0 and
  Phase 2 unblocked from that prereq.
* **Empty allowlist falls back to the full registry (A6)** with a
  `WARN` log. `AgentRunner.allow_skills = None` already means
  "no filter"; `agent_compile()` returns `None` for any cache miss
  or empty table, threading the same semantics.

## Phase ordering and #109 prereqs

| Phase | What | #109 prereqs |
|-------|------|---------------|
| 0 — kill switch | A0/A6 on fit pool, kill-threshold check | none (Phase 1 of #109 already shipped: `allow_skills` + token tracking) |
| 1 — ADR + `classify()` | this document + `codeminer/agent/compile.py` + tests | none |
| 2 — sweep | A0..A6 × model_matrix × harnesses | #109 Phase 3 (retry) + Phase 4 (`TokenBudgetedChatHistory`) |
| 3 — wire runtime | `AgentRunner` reads `compile_table` at entry | Phase 2 output |
| 4 — held-out validation | run derived table on 70 held-out | Phase 2 output |

## Out of scope

* LLM-driven scenario classifiers (revisit post-Phase 2).
* Probabilistic / shadow-mode classifier — dissolved by v2's
  deterministic classifier (RFC "Smaller things"-a, -e).
* New skill development — the registry is read-only through Phase 2.
* `query_concreteness` and `query_length_bucket` dimensions — dropped.
* Retrieval matrix sweeps (covered by #131 / PR #128).

## New-skill refresh protocol (Phase 4 onwards)

When a new skill ships:

* Incremental probe — for each scenario, evaluate `A6 ∪ {new_skill}`.
  Add to the per-scenario subset if accuracy lift ≥ 1%.
* Full re-derive — every 6 months, or when the registry changes by
  ≥ 30% (skill add or remove, threshold measured by skill count).
