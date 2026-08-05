# Repository Guardian — Prototype Design (one-month build)

> **Archived design.** This document describes the removed
> `codeminer.guardian` prototype. The current direction is the
> local-specification Guardian implemented in `codenib.clients.guardian` and
> described by `docs/guardian/reframed.md`.

> **Status:** prototype definition for the one-month build that follows the
> committed Phase-1 skeleton (`10dd58a feat(guardian): single-cycle Repository
> Guardian skeleton`). Companion to `idea_description.md`, the RFC, and
> `presentation_outline_v3.md`. Format mirrors the repo's issue style
> (`codeminer_baseline_issue.md`): Summary → Motivation → Detailed Design →
> Trade-offs → Plan → Evaluation.
>
> **Decisions locked with the author (2026-07):** (1) the month **proves the
> hypothesis end-to-end** on a narrow slice, not just infrastructure; (2) it runs
> on a **remote Linux/GPU host**; (3) the LLM is a **locally-served model** reached
> through the repo's existing `litellm` layer. Everything tagged *"to discuss"*
> in §8 is still open.

---

## 0 · At a glance

| | |
|---|---|
| **What we build** | A persistent monitor whose **cycle is re-fired by each repo change / time delta — a deterministic trigger, not a loop.** Within a cycle, a model-free **perception** pass (`sync · refresh · observe`) hands context to an **orchestrator agent** whose **outer agent loop** frames and ranks hypotheses and, for each one it commits to, spawns an **investigator sub-agent** whose **inner agent loop** runs the investigation in a sandbox; the orchestrator then integrates the verdicts into a dated report. A cross-cycle **repository memory** is read at cycle start and written back at cycle end — a persistent side store that **accumulates across cycles and keeps updating as the repo evolves (and, in the live-advisory eval, as the solver works)**, sitting alongside the flow rather than being a transient stage in it. |
| **What we prove** | That Guardian's findings **measurably help a downstream coding agent** on **DeepSWE** tasks — raising its task reward over a solo agent (*does Guardian help?*) and over a memoryless Guardian (*does persistent memory add value?*). A single A/B/C ladder (§5.6) carries both: C − A is the product claim, **C − B is the research question**. Memory is accumulated the honest way — by monitoring each task repo's real commit history up to its base commit (§5.2) — so the memory-vs-memoryless contrast is a genuine ablation, not a within-session trick. |
| **Headline number** | **C − A** (Guardian-fed solver vs. solo solver) on **DeepSWE task reward** (§5.6): does Guardian help? The number that supports the memory thesis is **C − B** (memory- vs. memoryless-Guardian findings, same solver, same task) — the research question. Both reported so "gain is all from a second agent, memory adds nothing" (B − A ≈ C − A) cannot hide. |
| **In scope** | RFC Phases 2 (graph-diff drift) + 3 (repository memory) + 5 (evaluation), on **one** repo, ~5–10 cycles. |
| **Out of scope** | Phase 4 candidate patches, multi-repo, production scheduling/daemonization, web UI. |
| **Runs on** | Remote Linux host, 1× H100 80 GB; local model server (vLLM, Qwen3-Coder-Next) behind `litellm`; per-cycle isolation via an **ephemeral container** (worktree checkout mounted read-only). |
| **Builds on** | The committed `codeminer/guardian/` package + reused CodeMiner infra (incremental index, code graph, hybrid retrieval, `litellm`, eval/dataset harness). |

---

## 0.5 · Terms (normative)

This section is normative for the whole document and for the codebase. It was
added after an audit found that "finding" was used 95 times in this file without
ever being defined, and that the implementation had consequently collapsed three
distinct kinds of object into one. The full audit, with `file:line` evidence, is
`design/outer_loop_blueprint.md` §5.0.

**Signal** — a deterministic, cheaply-computed *measurement* about the
repository: churn counts, graph-diff deltas, test-status changes. A signal says
that something changed. It never says something is wrong, and never says what to
do. `A.txt has been modified three times recently` is a signal and cannot be
anything more.

**Hypothesis** — a falsifiable claim that the repository contains a specific
engineering defect or improvement opportunity, plus the sketch of a remedy that
makes it solvable. Three required parts:

- `claim` — a falsifiable statement about behaviour; some experiment could refute it.
- `consequence` — what breaks, degrades, or is lost if the claim holds. This is what makes it a risk or an opportunity rather than a curiosity.
- `remedy` — a concrete engineering change that would resolve it. This is what makes it *solvable*.

A hypothesis **does not require a signal**. Signals are supplementary. A
hypothesis may originate from a signal, from repository memory, from unprompted
exploration of the code, or from a human. The `origin` field records which. This
is not a stylistic point: if hypotheses can only come from signals, Guardian is a
ranking layer over a linter and the proactivity claim of §1.1 is unreachable by
construction.

**Finding** — a hypothesis whose claim has been **verified** and whose remedy is
**actionable**. A finding is *not a separate object*: it is a hypothesis in a
particular state, so `findings ⊆ hypotheses`, and

> `is_finding(h) ≡ verified(h.claim) ∧ actionable(h.remedy)`

Both conjuncts are required. A verified claim with no workable remedy is a
*supported* hypothesis — true but not yet useful. It belongs in the backlog, not
in the report's findings section.

**Worked example.**

| | |
|---|---|
| Signal, not a finding | "A.txt has been modified three times recently." |
| Hypothesis | claim: "`parse_config()` and `normalize_config()` handle empty input inconsistently"; consequence: "downstream callers fail unpredictably"; remedy: "consolidate their validation logic and add regression tests." |
| Finding | the above, once a probe demonstrates the inconsistency and the consolidation is confirmed to be a change someone could make. |

A real finding is typically hundreds of lines; the example is compressed for
illustration.

**Consequences for the rest of this document.** Where §3–§5 say "findings" as a
loose synonym for cycle output, read "hypotheses, of which the reportable subset
are findings." Two places are actually wrong rather than loose, and are corrected
in place: the `findings` table schema in §3.2, and the drift-signal path in §3.3.
§5.5's judging rubric must score the `remedy`, not only the claim.

---

## 1 · Prototype definition

### 1.1 One-sentence definition

> The Repository Guardian prototype is a **persistent, non-modifying repository
> monitor** that, on each cycle, perceives the target repo through **two facets**
> — its *current state* (CodeMiner's index + code graph) and its *evolution over
> time* (a cross-cycle **repository memory**) — and whose defining action is to
> **actively investigate**: form hypotheses about emerging risk and test them
> inside an isolated sandbox, emitting a dated report of findings with evidence
> and reasoning traces but **never touching the production repository**.

This instantiates the **four defining properties** the proposal uses to define a
Repository Guardian:

- **Proactive** — initiates maintenance work without a user request; each cycle
  is fired by a repo change / time delta, not a handed-in task.
- **Persistent** — accumulates repository knowledge across cycles in a
  cross-cycle memory. This is the *central hypothesis* the prototype exists to
  **test**, not assume.
- **Investigative** — its defining action: forms a hypothesis ("this refactor
  broke the contract that module X relied on") and runs an experiment in an
  isolated sandbox to confirm or reject it, rather than merely flagging a churn
  hotspot.
- **Advisory** — produces evidence-backed recommendations rather than modifying
  production code; the non-modifying invariant is enforced structurally (we
  operate only on a throwaway checkout — §3.4, §4.3), an attribute rather than
  the headline.

### 1.2 The testable hypotheses

> **The evaluation (single A/B/C ladder on DeepSWE).** Guardian's findings,
> injected into a separate coding agent that solves a **DeepSWE task**,
> **measurably raise its task reward** over the solo agent (**C − A** — *does
> Guardian help?*) and over an otherwise-identical **memoryless** Guardian
> (**C − B** — *does persistent memory add value?*). C − A is the headline product
> claim; **C − B is the research question** — the direct test of whether
> cross-cycle memory lets the agent surface findings a memoryless run of the same
> agent misses. Run at reduced scale (§5.6); directional.

Memory is built the honest way: Guardian **accumulates memory over the stream of commits
it observes** — one cycle per commit (§5.2). In a DeepSWE run that stream is the solver's
own step/commit trajectory; a task's real pre-base history, if mined, is simply earlier
commits in the same stream. Throughout, Guardian is **never reading the
reference solution, held-out tests, or any post-base commit**. The memory-vs-memoryless
contrast (arms B/C) is therefore an
ablation of that accumulated memory, and it carries the intrinsic memory claim
directly. If the memory arm does **not** beat memoryless (C − B ≈ 0), that is a
real and publishable negative result — the ladder makes that outcome legible
rather than hiding it.

> **On the retired replay diagnostic.** An earlier design added a second,
> *intrinsic* hypothesis (H1) that scored findings against a repo's *post-`t`*
> future — precision / recall / lead-time vs realized issues on `base → reference`
> commit pairs. DeepSWE tasks are **authored single-snapshot tasks with a
> program-based verifier**, not base→future pairs, so there is no post-base future
> timeline to score against. That diagnostic is therefore **retired**; its cycle
> machinery survives as the memory-construction front-end (§5.2), and its signal is
> preserved as **C − B** plus a descriptive count of **memory-unique findings**.

### 1.3 Success criteria (what "done" means at day 28)

1. **Runs end-to-end** on the remote host: one command takes a DeepSWE task,
   mines the task repo's commit history up to its base commit, runs a full cycle
   at one such commit in an isolated sandbox against a locally served model, and
   writes `guardian_report_<date>.md` + `.json`. *(M1)*
2. **Memory persists and is used**: cycle `k` demonstrably reads state written by
   cycle `k−1` (graph snapshot diff + prior findings), verified by an assertion
   in the memory store and by content in the report. *(M2)*
3. **Memory-construction harness yields findings**: for at least one DeepSWE task,
   memory accumulates across ≥2 cycles (solver-step trajectory, optionally seeded from
   pre-base history) and emits a findings shortlist, in both memory and
   memoryless configurations. *(M3)*
4. **Solver scaffold runs a task graded**: a mid-capability solver runs one DeepSWE
   task solo through Pier + mini-swe-agent and is scored by the task's `reward.json`
   verifier — the plumbing that arms A/B/C share. *(M4)*
5. **The A/B/C pilot produces a directional number**: a ~8–12-task run reports
   C − A / C − B / B − A + two-way flips on DeepSWE task reward, with "directional,
   n small" caveats. *(M5)*

A prototype that hits M1–M4 but shows a null or negative result is a
**successful** prototype — it answered the research question. A prototype that
adds a positive M5 downstream signal (even directional, n small) is a
**promising** prototype that scopes month 2 (full ~30–50-task run, more repos,
patches).

### 1.4 Scope — in / out / deferred

| Capability | Month-1 prototype | Rationale |
|---|---|---|
| Single-cycle loop (sync→index→observe→investigate→report) | **Reuse** (already committed, Phase 1) | Skeleton exists; we extend it. |
| Cross-cycle **graph-diff drift** signal (RFC Phase 2) | **In** | Cheap, deterministic, and it's the input the memory hypothesis feeds on. |
| **Repository memory** store + read/write (RFC Phase 3) | **In** | This *is* the hypothesis. |
| LLM-backed **hypothesize + investigate** (hypothesis test in sandbox) | **In** (narrow) | The defining novelty; kept to a bounded prompt/experiment budget. |
| **Pre-base history mining** + memory ablation → **A/B/C ladder** (RFC Phase 5) | **In** | Turns the memory claim into a graded reward delta. |
| **Applied/recommended candidate patches** ("apply this diff") (RFC Phase 4) | **Deferred** | Highest effort, and shifts the eval onto patch *quality*; report stays non-modifying by construction. |
| **Test synthesis as investigation** — agent writes a *new* test whose failure reveals the risk, corroborated by a differential run and/or fix-probe (§3.4) | **In** (core) | The defining investigation action: a risk is confirmed by a runnable reproduction, not by argument. Test + any fix-edit live only on the throwaway overlay, never committed → non-modifying invariant holds. |
| Multi-repo, multi-language sweep | **Deferred** | One Python repo first; the metric must exist before it scales. |
| Production scheduling / long-running daemon / alerting | **Deferred** | Cycles are invoked by the replay harness, not `cron`, this month. |
| Web UI / dashboard | **Out** | Reports are Markdown + JSON. |

**Non-modifying invariant (hard constraint, all phases).** The agent operates
only inside a per-cycle container on a disposable overlay checkout (§4.3): it may
edit-and-test *within* that throwaway layer as evidence (§3.4), but **nothing is
ever applied** to the mirror or production tree, and the report carries **no
applied diffs**. `report.py` already asserts a no-patch invariant in Phase 1; we
keep that test green throughout.

---

## 2 · Motivation (why this shape)

`presentation_outline_v3.md` argues long-horizon coding is the rising frontier,
that agents collapse on it for two reasons — they can't *perceive* what they
break or what to fix, and they act only when *handed a task* — and that the
answer is a proactive monitor that actively investigates on a two-facet
perception layer. The prototype's job is not to re-argue that; it is to
**instantiate the smallest system that lets us test the one load-bearing
assumption**: that the *evolution facet* (memory) actually earns its cost.

Two engineering facts from the codebase make this tractable in a month:

1. **The current-state facet already exists.** CodeMiner gives us incremental
   indexing (`codeminer/index/incremental/`), a persistent code graph with
   `save_graph`/`load_graph` (`codeminer/graph/code_graph.py`), incremental graph
   patching (`codeminer/graph/incremental/`), and hybrid retrieval
   (`HybridRetrievePipeline.query`). We do not build perception from scratch.
2. **The cycle scaffolding already exists.** `codeminer/guardian/cycle.py`
   wires the deterministic perception spine (`sync → index → observe`) and the
   report/persist tail with injectable seams (investigator, manifest) for
   testing. We add memory, graph-diff, and — into the investigator seam — the
   **two nested agent loops**: the orchestrator that hypothesizes and the
   investigator sub-agent it spawns for sandboxed investigation (§3.1, §3.4).
   The scaffolding is a pipeline; the intelligence we drop into its seam is a
   pair of loops.

So the marginal work this month is concentrated exactly on the unproven part.

---

## 3 · Architecture & repository-memory design

Six components: **two model-driven agents** and four deterministic services they call.
The two agents are nested. An **orchestrator agent** runs the cycle — reads memory and
signals, forms and ranks hypotheses, decides what is worth investigating — and for each
hypothesis it commits to, it spawns an **investigator sub-agent** with its own context
and prompt, which runs the investigation loop in the sandbox and returns a verdict. The
four services are the **perception layer** (turns a commit into signals), the **memory
store** (carries state across cycles), the **sandbox runtime** (a throwaway place to
run experiments), and the **report/persist** path (writes the cycle's output and folds
state back into memory). §3.1 then walks the same pieces in time.

| Component | What it is | Talks to | Backed by | Detail |
|---|---|---|---|---|
| **Perception layer** | Model-free pass that turns commit `t` into signals: churn, graph-diff drift, test deltas | Reads repo + prior snapshot from memory; hands signals to the agent | `IncrementalIndexUpdater`, `GraphPatcher`, `churn_hotspots`, `CodeGraph`, `run_test_suite` | §3.1 (steps 1–3), §3.3 |
| **Repository memory** | Cross-cycle store: graph snapshots, findings, symbol/edge history, test deltas | Read at cycle start, written at cycle end; the memoryless arm toggles the read | new `codeminer/guardian/memory.py` + `index.sqlite` | §3.2 |
| **Orchestrator agent** *(outer loop)* | Model-driven: reads memory + signals, forms and ranks hypotheses, decides what to investigate, spawns an investigator per hypothesis, emits findings | Reads perception signals + memory; spawns the investigator sub-agent; writes findings | `llm/litellm_chat.py`, `HybridRetrievePipeline` | §3.1 (toolbox), §3.4 |
| **Investigator sub-agent** *(inner loop)* | Model-driven, one per hypothesis with its own context + prompt: the *investigate* controller — choose probe → run in sandbox → observe → decide, to a verdict or budget stop | Spawned by the orchestrator; runs probes in the sandbox; returns a verdict + trace | `investigate.py` controller | §3.4 |
| **Sandbox runtime** | One isolated container per cycle; RO repo checkout + writable overlay where the investigator's test edits live | Executes the probes the investigator hands it; the overlay diff *is* the evidence, never a patch to the repo | overlayfs container | §3.4, §4.3 |
| **Report / persist** | Renders findings + evidence + reasoning trace to `.md`/`.json`, then writes this cycle's state back to memory | Consumes agent findings; writes memory | `report.py` (`GuardianReport`), `CodeGraph.save_graph` | §3.1 (Emit + step 7) |

### 3.1 Per-cycle dataflow

![Repository Guardian per-cycle dataflow and two-facet perception layer]({{artifact:art_d90f2774-311e-4d59-aba8-b48aa3048e19}})

**The cycle is a thin deterministic shell around a pair of nested agent loops.** Only
three things are code-fixed: a model-free **perception pass** at cycle start (steps 1–3)
that hands the agent a baseline snapshot; a **persist** at cycle end (step 7) that the
emitted report triggers; and the **re-firing** of the whole cycle by the next repo
change or time delta. Everything in between is the agent loops. The "hypothesize → investigate →
report" arc is a default written into the *prompt*, not a sequence wired in code — the
agent may investigate before its hypothesis is firm, revise it as probes come back, or
re-open a finding it had closed.

Inside the loop the orchestrator works from **one open toolbox**, drawing whichever
tool the evidence calls for. The perception tools are **not** sealed inside the
step-1–3 pass: that pass only guarantees a starting snapshot, and the same churn /
graph-diff / retrieval tools stay in the orchestrator's hands across the cycle, so it
can re-diff a subsystem or re-retrieve between investigations — re-ranking what is left
to look at as verdicts come back. It investigates one hypothesis at a time: it spawns a
sub-agent, waits for the verdict, then decides what to do next. Three levels nest here, and only the two agent-driven
ones are loops: a deterministic **trigger** — the next repo change / time delta —
fires a cycle; inside it the **outer agent loop** is the orchestrator picking tools
from the toolbox until it decides the cycle is done; and for each hypothesis it commits
to, the **inner agent loop** is the investigator sub-agent it spawns, running probes in
the sandbox to a verdict (§3.4). The trigger is a scheduler, not a loop.

The two tables separate what is fixed from what is not — the first is ordered and
code-fixed; the second is a set of capabilities the loop calls in evidence-driven
order, not top to bottom.

**Deterministic shell** — fixed order, model-free:

| # | Stage | Does | Reuses | New |
|---|---|---|---|---|
| 1 | **Sync** | Fetch remote, resolve to commit `t` (or checkout `t` in replay) | `git` subprocess patterns from `index/incremental/git_diff.py` | thin wrapper |
| 2 | **Refresh** | Update index + code graph incrementally for `t` | `IncrementalIndexUpdater`, `GraphPatcher`, `IncrementalState` | — |
| 3 | **Observe** | Compute signals: churn + **graph-diff drift** vs. last snapshot + optional test deltas | `churn_hotspots`, `run_test_suite` (Phase 1); `CodeGraph.load_graph` for the prior snapshot | **drift signal** (§3.3) |
| 7 | **Persist** | Write this cycle's graph snapshot, findings, and test state back to memory (triggered by the emitted report) | `CodeGraph.save_graph` | **memory write API** (§3.2) |

**Agent toolbox** — the loop, and the only part that calls the model; called in
whatever order the evidence dictates, not top to bottom:

| Capability | Does | Reuses | New |
|---|---|---|---|
| **Read memory + signals** | Pull paged memory + the step-1–3 signals; rank what is worth investigating into hypotheses | `llm/litellm_chat.py` (`LiteLLMChat`, `ChatMessage`) | **hypothesize prompt + memory read** |
| **Re-perceive** | Re-rank churn hotspots · recompute graph-diff drift · re-run test deltas — mid-loop, not only at entry | `churn_hotspots`, `CodeGraph` diff, `run_test_suite` | — |
| **Retrieve / read history** | Query hybrid retrieval; read prior-cycle findings and symbol history | `HybridRetrievePipeline.query`; memory read API | — |
| **Investigate a hypothesis** | Spawn an investigator sub-agent (own context + prompt) whose inner loop runs probes in the sandbox — synthesize test · differential · fix-probe → observe → verdict; the orchestrator consumes the returned verdict | `run_test_suite`; `investigate.py` seam | **investigator sub-agent** (§3.4) |
| **Emit** | Render findings + evidence + reasoning trace to `.md`/`.json`, **no patches** | `report.py` (`GuardianReport`, `render_markdown`) | extend `Finding` with hypothesis + trace |

Confining the model to the toolbox band keeps the entry snapshot and the persist
reproducible, and lets the memoryless ablation (§5) be a clean toggle on what either
agent may read from memory as it works the loop.

### 3.2 Repository memory store

A **persistent side store**: it lives across cycles and is *continuously updated* — every cycle appends new rows and refreshes lifetime columns, and in the live-advisory eval (§5.6) it also updates during a solve. "Kept aside" (§0) means it sits beside the dataflow as a read/write store, not that it is ever frozen.

**What it holds** (keyed by repo + commit; rows are appended each cycle, lifetime columns such as `last_seen` updated in place):

```
repo_memory/
  <repo_id>/
    graph/<commit>.graph.json        # CodeGraph.save_graph() snapshot per cycle
    cycles/<n>_<commit>.json         # findings + hypotheses + outcomes + test deltas
    index.sqlite                     # lightweight cross-cycle index (see schema)
```

**`index.sqlite` schema (prototype — deliberately small):**

| Table | Key columns | Purpose |
|---|---|---|
| `cycles` | `cycle_no, commit, ts, token_cost, tests_summary` | one row per cycle; cost accounting for the memory ablation |
| `symbols` | `symbol_id, path, first_seen_cycle, last_seen_cycle` | symbol lifetime → detect churn/removal |
| `edges` | `src_symbol, dst_symbol, kind, first_seen, last_seen` | dependency evolution → drift (§3.3) |
| `hypotheses` | `hypothesis_id, cycle_no, claim, consequence, remedy, grade, origin, locus_json, evidence_json, confidence, attempts, spent_tokens, first_seen_cycle, last_touched_cycle, supersedes_json, evidence_test, evidence_diff` | the carried hypothesis set, so cycle `k` can recall / de-dup / escalate / resolve. **Findings are the rows with `grade = 'finding'`** — not a separate table (§0.5). `grade` is the single lifecycle field, written by the agent and validated against an admissibility table; `resolved` closes a claim that held on an earlier commit and was fixed by a later commit. `origin ∈ {signal, memory, exploration, human}`; `evidence_json` holds prefixed refs (`signal:…`, `probe:…`, `cycle:…`, `resolved:…`) and may cite no signal at all. `evidence_test` = the synthesized risk-revealing test, `evidence_diff` = the optional fix-probe diff (§3.4), null if none |
| `signals` | `signal_id, cycle_no, kind, locus, detail` | raw measurements, retained as evidence. `kind ∈ {churn, drift, test_failure}` belongs **here** and nowhere else — it is the signal taxonomy, not a property of hypotheses (§0.5) |
| `test_deltas` | `cycle_no, nodeid, status, changed_from` | test-outcome trajectory over time |

**Read API** (`codeminer/guardian/memory.py`, new). Exposed to the agent as
tools, not as a prompt-assembly helper: `recall(query=…, by=locus|similarity|trajectory)`
over the `hypotheses`/`signals` tables, plus `load_prior_graph(repo, before_commit)`,
`symbol_history(symbol_id)`, `edge_drift(since_cycle)` behind it.

The read model is **compression *and* retrieval**, split by layer (loop blueprint
§4.2). The proposal's Solution 1 said "compression, not retrieval"; read it as
compression of the frame only:

- **Compression** applies to the *frame*: a small, code-owned block that opens
  every cycle (~1–2k tokens) — task, tool list, budget, commit, and a **digest**
  of memory state (counts per grade, loci with open hypotheses, cycles since last
  visit). A digest, not content: it tells the agent what exists and is worth
  asking about, and every entity it names is fetchable by id.
- **Retrieval** applies to the *working set*: everything beyond the frame enters
  because the agent called a tool for it. Context is **pull, not push** — code
  never decides that a particular prior verdict belongs in this cycle's prompt.

A long repository history therefore does not inflate the frame (which grows
logarithmically, as counts) and does not have to be queried from scratch (the
tables persist) — this is what makes the *evolution facet cheap*. **Write API**:
`persist_cycle(report, graph, test_result)`, called by the host after the cycle
exits.

**Backend choice** is intentionally boring for the prototype: SQLite + JSON files
on the sandbox's persistent scratch volume. It's inspectable, needs no service,
and survives across cycles. Swapping to a vector/graph DB is a month-2 decision
(§8).

### 3.3 Graph-diff drift signal (the new deterministic signal)

The Phase-1 `observe` step ranks churn by commit count. We add a **structural**
signal that only the memory facet makes possible: compare the current
`CodeGraph` against the previous cycle's snapshot and surface

- **new / removed edges** on high-fan-in symbols (a widely-depended-on function
  changed its contract);
- **fan-in spikes** (a symbol suddenly acquired many dependents → rising
  blast-radius);
- **API-surface changes** (public symbol signature/arity changed while dependents
  did not) — a concrete "what did this break" hypothesis generator.

Implementation reuses `CodeGraph.load_graph` for the prior snapshot and the graph
layers already in `codeminer/graph/` (`hierarchy.py`, `dependency.py`,
`roi_subgraph.py`); the diff is a set operation over typed edges, no LLM. This
signal is the primary bridge from "we have memory" to "memory produces a finding
memoryless can't."

> **The graph is a detector, not an LLM input.** The model never ingests raw
> graph structure — LLMs reason poorly over that. The graph-diff runs
> deterministically and emits a short, plain-English shortlist ("`parse_config`
> changed arity; 47 dependents; 3 not updated this commit"). Only that prose —
> plus code snippets retrieved in `investigate` — reaches the model. The graph's
> job is *selection*: cheaply and precisely decide **where** to point the
> expensive model on a repo far too large to read whole (cf. the deck's 27M
> tokens/rollout caution). This is also why it's the sharpest test of the memory claim: pure
> cross-file structural drift is exactly what a single-snapshot, memoryless arm
> cannot see.

### 3.4 Active investigation (the defining novelty, bounded)

The core move: **a risk is not confirmed by argument, it is confirmed by a test
the agent writes to expose it.** The orchestrator spawns an **investigator sub-agent**
per committed hypothesis — its own context and prompt — and that sub-agent's loop is
the **inner agent loop** (§3.1): `investigate` is **a true agent loop, not a fixed
pipeline** — a controller that, given the hypothesis, repeatedly
**(a) chooses its next probe from a toolset** based on what the prior probes
returned, **(b) executes it** in the sandbox overlay (§4.3), **(c) observes** the
result, and **(d) decides** whether the evidence settles the verdict or another
probe is warranted. It **loops until the verdict is conclusive or the per-cycle
budget is spent** (§4.5) — the trajectory is chosen at runtime, not wired in
advance. Concretely this is a tool-use agent on the local model: the LLM emits a
tool call, the harness runs it, feeds the observation back, and asks for the next
action.

**The toolset the controller chooses among:**

- **Retrieve evidence** — `HybridRetrievePipeline.query(hypothesis_text, top_k)`
  → `Evidence` rows (already wired in `investigate.py`) plus the relevant source
  spans.
- **Synthesize a risk-revealing test (the primary action)** — the agent writes
  a *new*, targeted test whose **failure demonstrates the risk**. E.g. for a
  suspected contract break it emits a test that calls `parse_config` the way the
  47 dependents still do and asserts the old behaviour; the test **fails on the
  current commit**, and that failure *is* the evidence the risk is real and
  reproducible. Written into the sandbox overlay and run with the repo's own
  `pytest`; **never committed** to the repo.
- **Differential run** — the same synthesized test **passes on the prior-cycle
  checkout** (before the drift) and **fails now** → an agent-generated
  `PASS→FAIL` pair that pins the regression to the interval.
- **Fix-probe (edit-and-test)** — a minimal edit to the overlay that reverts the
  suspected cause makes the synthesized test **go green** → a `FAIL→PASS` pair.
  (Differential + fix-probe together are exactly a SWE-Bench-style `FAIL_TO_PASS`
  demonstration, but *generated by the agent as evidence*, not proposed as a patch.)
- **Cheap first-pass probes (no test written)** — run an *existing* test that
  already covers the symbol, call-site grep, import/typecheck; the controller
  often opens with these before deciding a synthesized test is worth the budget.

Every trajectory ends the same way: the controller **records a verdict + reasoning
trace** — confirmed / rejected / inconclusive, with the synthesized test, the
commands run, their output, and any fix-diff — into the `Finding` (`evidence_test`
+ `evidence_diff`). A red test only counts as confirmation if corroborated (a bad
test guard): the controller is expected to reach for a differential run or fix-probe
before it will mark a hypothesis confirmed. The **common trajectory** —
retrieve → synthesize test → corroborate → verdict — is what the agent usually
does, *not* a script it must follow; a cheap probe that already settles the
question ends the loop early. The point of the prototype is not a rich investigator;
it is to show the *loop closes* and that the agent **drives its own probes**:
hypothesis → agent-selected probes → **synthesized test that reveals the risk** →
corroboration → verdict → memory → next cycle.

> **Why synthesize-and-test stays inside the non-modifying invariant.** The
> invariant forbids changing the *production* repository. The synthesized test and
> any fix-edit live only on the disposable overlay and are thrown away with the
> container (§4.3). They are surfaced in the report as **evidence a risk is real**
> — a runnable reproduction — never as "apply this test/patch." The human still
> decides everything; Phase 4 (proposing patches/tests for adoption) stays
> deferred.

---

## 4 · Environment: how to run & how to dispatch sandboxes

Target deployment: **one remote Linux host with a single NVIDIA H100 PCIe (80 GB
VRAM), CUDA 13.0, driver 580**. The model is served **locally** on that host and
reached through the repo's existing `litellm` layer — no cloud API keys. This
follows the pattern in `docs/running-locally.md`, with one substitution justified
by the hardware: **vLLM** replaces `llama-cpp-python` as the server (see 4.1).

### 4.1 Host & base environment

| Layer | Choice | Notes |
|---|---|---|
| OS / driver | Linux, driver 580, CUDA 13.0 | as reported by `nvidia-smi` on the host |
| GPU | **1× H100 PCIe, 80 GB** | fits a 32B coder model with large KV-cache headroom (below) |
| Python env | conda env `codeminer` (`make dev` / `pip install -e ".[dev]"`) | the repo's standard env |
| Model server | **vLLM** (`pip install vllm`), OpenAI-compatible `serve` endpoint | replaces `llama-cpp-python` — see rationale below |
| Served model | **Qwen3-Coder-Next** (80B-total / 3B-active MoE, 4-bit, ~40 GB weights) | coding-agent specialist, 256K context; MoE → 3B-active inference speed; fp16 **Qwen3.6-27B** dense is the fallback if you want `<think>` traces |
| Embeddings | CodeMiner's configured code-embedding model (e.g. `nomic-ai/CodeRankEmbed`, 768-d, the default in `GuardianConfig`) | used by the index refresh; BM25-only is the cheap fallback |

**Why vLLM, not `llama-cpp-python`.** The `running-locally.md` path assumes a
CPU/consumer GPU serving a small GGUF. On an 80 GB H100 that leaves the card
mostly idle. vLLM uses paged KV-cache and continuous batching, so the same card
serves an 80B-MoE model *and* keeps enough KV headroom for Guardian's long
prompts (repo memory + retrieved code + findings history). It exposes the same
OpenAI-compatible API (add `--tool-call-parser qwen3_coder`), so **nothing in
Guardian changes** — only the `api_base` `litellm` points at.

**Why Qwen3-Coder-Next over a bigger Qwen3.5/3.6 general model.** The pick is the
*coding-agent specialist that fits one H100*, not the highest generation number.
Qwen3-Coder-Next is a sparse 80B-total / **3B-active** MoE, purpose-built for
coding agents and long-horizon repository reasoning, with a **256K** context
window — ideal for feeding memory + retrieved code. Its small active footprint
means it runs at ~3B speed while fitting the card in 4-bit. One caveat for our
design: it runs **non-thinking only** (no `<think>` blocks) — fine here, because
Guardian's reasoning traces come from the hypothesize/investigate *loop* (commands +
verdicts), not from model-internal thinking.

**VRAM budget on 80 GB:**

| Option | Weights (approx.) | KV headroom (80 GB) | Verdict |
|---|---|---|---|
| Qwen3-Coder-Next, fp8 | ~80 GB | ~0 — no room for KV | ❌ won't fit with usable context |
| **Qwen3-Coder-Next, 4-bit** | **~40 GB** | **~35–40 GB — long context, batching** | ✅ **default** |
| Qwen3.6-27B, fp16 (dense) | ~54 GB | ~20 GB | ✅ fallback — adds `<think>` traces if wanted |
| Qwen3.6-35B-A3B, 4-bit (MoE) | ~18 GB | ~55 GB | ✅ leanest option if 4-bit 80B quality regresses |

**Two services, two shells** (Guardian adds no new service of its own):

```bash
# Shell A — model server (GPU): vLLM, OpenAI-compatible endpoint
python -m vllm.entrypoints.openai.api_server \
    --model Qwen/Qwen3-Coder-Next \
    --tool-call-parser qwen3_coder --max-model-len 131072 \
    --gpu-memory-utilization 0.90 --port 8000
    # (add the vendor's 4-bit quant flag/checkpoint; --max-model-len can go to 262144 if KV allows)
# verify the server is up
curl -s http://localhost:8000/v1/models | python -m json.tool
```

`litellm` is pointed at that server via the repo's config (OpenAI-compatible
provider, `api_base=http://localhost:8000/v1`, a dummy key). Guardian's
`hypothesize`/`investigate` construct a `LiteLLMChat` exactly as the rest of the
codebase does, so the model backend is a config change, not new code.

### 4.2 Entry points

- **Existing:** `scripts/guardian_cycle.py` — one cycle, argparse CLI mirroring
  `scripts/index_repo.py`; already writes `guardian_report_<date>.md` + `.json`,
  with `--run-tests` / `--no-investigate` gates.
- **New (thin):** `scripts/guardian_replay.py` — the month-1 driver. Given a
  **DeepSWE task**, it resolves the repo + base commit and runs one Guardian cycle per
  observed commit, appending to the repository memory and emitting a findings shortlist.
  The stream is the **solver's step/commit trajectory** (one cycle per solver step);
  an **optional `--preseed-commits <file>`** prepends the repo's pre-base
  history to the front of that same stream (a cycle per commit `t ≤ base`, memory-knowledge ≤ `t`) to pre-warm memory
  before the solve. Flags: `--task <deepswe-task-id>`, `--arm {memory,memoryless}`,
  `--preseed-commits <file>` (optional), `--budget-tokens N`,
  `--sandbox {container,worktree}` (**default `container`**; `worktree` is the
  checkout-only debug mode).

There is intentionally **no daemon/cron** this month — cycles are driven by the
harness so runs are deterministic and comparable.

### 4.3 Sandbox dispatch — one isolated **container** per cycle

Each cycle runs against a **throwaway checkout at commit `t`**, never the
production working tree. This is what makes "non-modifying" a structural
guarantee rather than a promise. **The prototype uses an ephemeral container per
cycle as its primary and required (M1) isolation mechanism** — the worktree is
retained only as the *checkout step that feeds the container*, not as a standalone
sandbox.

**Layout:**

```
scratch/<repo_id>/
  mirror.git/                 # one bare mirror, fetched once, updated per replay
  wt/<cycle_no>_<commit>/     # git worktree at commit t — the checkout to mount (ephemeral)
  cache/                      # CodeMiner index cache (index_cache_dir), persists
  memory/                     # repository memory (§3.2), persists across cycles
```

**Per cycle** (`--sandbox container`, the default):

1. `git -C mirror.git worktree add --detach wt/<n>_<commit> <commit>` — cheaply
   materialize commit `t` without re-cloning (worktrees share the mirror's object
   store).
2. Launch an ephemeral container from a **pinned image** (`guardian-runtime:<tag>`,
   Python + CodeMiner deps baked in), mounting:
   - `/repo` as an **overlay**: the worktree is the **read-only lower** layer (never
     written), a **tmpfs upper** layer absorbs any edits. Observe/signals treat it
     as read-only; the edit-and-test probe (§3.4) writes to the upper layer only.
     **The upper layer *is* the evidence-diff** — whatever the agent changed to make
     a test pass is captured exactly, then discarded with the container.
   - a small **read-write scratch** tmpfs for test artifacts, discarded on exit;
   - `cache/` and `memory/` mounted **read-only for observe/investigate**; the
     cycle's memory *write* (step 7) happens on the host after the container exits,
     from the report the container emits — the container never holds a writable
     handle to durable memory.
3. Run `sync/refresh/observe/investigate` inside the container with `repo_path=/repo`,
   under `--network none`, a CPU/RAM cap, and a wall-clock timeout.
4. Container exits and is removed (`--rm`); `git worktree remove` cleans the checkout.

- ✅ **Strong isolation** — process, filesystem, and (via `--network none`)
  network are contained; a broken or hostile test cannot reach the host, the
  durable memory, or the model server unless explicitly allowed.
- ✅ **Non-modifying invariant is structural** — the production tree is never
  checked out or written, the mirror is fetch-only, and every writable surface
  (the overlay upper layer, the scratch tmpfs) is discarded with the container.
  Edit-and-test (§3.4) therefore stays inside the invariant: edits touch only the
  disposable upper layer and are surfaced as evidence, never applied.
- ✅ **Reproducible** — the pinned image fixes the toolchain, so a cycle's result
  doesn't drift with whatever happens to be installed on the host.
- ⚙️ Runtime: **Podman** (rootless, daemonless — least host privilege) with Docker
  as a drop-in fallback; the image is built once in W4 and cached on the host.

**Runtime seams:**

- `--sandbox worktree` remains as a **debug/fast-iteration** mode (checkout only,
  no container) — useful while developing the cycle logic, **not** the mode the
  evaluation runs in.
- **GPU note:** the container runs only the deterministic + orchestration work; the
  model stays served on the host (§4.1) and is reached over the OpenAI-compatible
  endpoint, so the per-cycle container needs no GPU and no `--gpus` — it makes an
  HTTP call out to the host model server (the one network exception, via a
  localhost-scoped allow rule rather than full `--network host`).

### 4.4 Concrete run recipe (what M1 looks like)

```bash
# on the remote host, conda env `codeminer`, model server already up in shell A
# 1. one cycle at HEAD (smoke), BM25-only, no LLM — proves plumbing
python scripts/guardian_cycle.py --repo /path/to/target --index-types bm25 \
       --no-investigate --out reports/

# 2. one cycle with retrieval + local model investigation
python scripts/guardian_cycle.py --repo /path/to/target --index-types bm25 \
       --investigate --out reports/

# 3. memory-construction over the solving trajectory (memory arm; optional pre-warm via --preseed-commits <file>)
python scripts/guardian_replay.py --task <deepswe-task-id> \
       --arm memory --budget-tokens 300000 \
       --sandbox container --out runs/target_memory/
```

### 4.5 Resource & cost budget per cycle

Cost is a **measured quantity** for the memory ablation, not an afterthought (the deck flags
27M tokens/rollout for long-horizon SWE as the cautionary number). Controls:

- **Tiered model use** — deterministic signals (churn, graph-diff) run with **no
  model**. `hypothesize` uses the local model to triage; only hypotheses above a
  confidence/importance threshold reach the (more expensive) `investigate`
  prompt. With a locally-served model the "cost" is wall-clock + GPU time rather
  than dollars, but we still cap it to keep cycles comparable across arms.
- **Per-cycle token budget** (`--budget-tokens`) enforced via
  `llm/usage.py` accounting; a cycle that hits the cap stops issuing new
  hypotheses and reports what it has.
- **Paged memory reads** (§3.2) so context stays bounded as history grows.
- **Prompt caching** — `litellm_chat.py` already has `_with_prompt_caching`;
  reused for the stable parts of the hypothesize/investigate prompts.

The replay harness records tokens + wall-clock per cycle into the `cycles` table
so the ablation reports **quality *at a cost***, not quality alone.

---

## 5 · Evaluation design

The evaluation answers **one** question through a single **A/B/C ladder**. Guardian
accumulates memory over the stream of commits it observes — in a DeepSWE run, the solver's
own step/commit trajectory (§5.2). Same DeepSWE task, same solver, same seeds; only the
injected context varies:

- **A** — solver alone (no Guardian).
- **B** — solver + a **memoryless** Guardian's findings.
- **C** — solver + a **memory** Guardian's findings (cross-cycle memory over the
  observed commit stream).

Two deltas, each a distinct claim, both graded by DeepSWE's `reward.json`:

- **C − A** — does the Guardian agent help a solver at all? → the **product/system claim
  (headline number)**.
- **C − B** — does *persistent memory* add value beyond a memoryless investigator? →
  **the research question**.

`B − A` (memoryless investigator alone) is also reported. The finding-level rubric and
memory-unique-finding count (§5.4–5.5) are descriptive support that explains *why* a
delta moves; they are not a separately scored hypothesis.

The evaluation is a single A/B/C ladder (§5.6) on one benchmark substrate (§5.1)
and one Docker harness, so it costs one corpus.

### 5.1 Benchmark substrate — what the solver is scored on

The evaluation's core data need is a set of **authored, gradeable SE tasks** the
downstream solver attempts — the thing C − A / C − B are measured on. Guardian's memory
accumulates over the **commit stream** each task produces as the solver works it (§5.2),
so no extra data source is required. **DeepSWE** serves this directly: it ships authored
long-horizon tasks with program-based verifiers, and every task's `task.toml` carries a
`repository_url` + base commit — so the repo's real pre-base history is also *available*,
should we choose to prepend it to that stream. The survey below is the record of the
decision so it is not re-litigated:

| Benchmark | Origin | Unit of data | Languages | Grading | Public + runnable | Role for Guardian |
|---|---|---|---|---|---|---|
| **DeepSWE** (datacurve-ai) | DataCurve | 113 authored long-horizon tasks @ base commit on active OSS repos | **TS · Go · Py · Rust · JS** | program-based behavior verifier → `reward.json` (binary reward + pass fractions) | **Yes** (GitHub `datacurve-ai/deep-swe`; runs on Pier) | **Primary substrate** — the solver task; memory feeds on the observed commit stream (the solver's trajectory) |
| SWE-CI (arXiv 2603.03823) | Skylenage | base→reference commit pair (avg 233 days / 71 commits apart) | Python | test-report match → ANC | Yes (HF `skylenage/SWE-CI`) | Future substrate — evolution-replay diagnostic if reinstated (§9) |
| SWE-EVO (arXiv 2512.18470) | Fsoft-AIC | release-to-release transition, F2P + P2P tests | Python | F2P/P2P | Yes (HF `Fsoft-AIC/SWE-EVO`) | Future substrate — Python-native evolution pairs (§9) |
| EvoClaw (arXiv 2603.13428) | Hydrapse | milestone DAG from commit history | Python | milestone DAG | Yes (GitHub + HF) | Future — structured evolution decomposition (§9) |
| SWE-bench Pro (arXiv 2509.16941) | Scale AI | issue→patch, copyleft-sourced | multi | F2P+P2P | Public split + Docker | Method-only: **contamination resistance** (→ §5.7) |
| SWE-Marathon (arXiv 2606.07682) | Abundant AI | 20 from-scratch mega-tasks (27M-tok avg) | Python | end-to-end | Partial | Cite + future work; too costly, Story-2 shape |
| Multi-SWE-bench (arXiv 2504.02605) | ByteDance | multilingual issue→patch | multi | F2P+P2P | Full, permissive | Method-only: multilingual harness |

**Decision: build on DeepSWE (primary, sole).** It is the only surveyed benchmark
that is simultaneously (a) authored + program-verified (a clean, structure-agnostic
reward the A/B/C ladder can move), (b) **polyglot** (five languages — none of the
Python-only evolution benchmarks give this), and (c) backed by real OSS repos whose
pre-base history Guardian can mine for honest, over-time memory. Its quickstart
drives the solver with **`mini-swe-agent` + an OpenAI-compatible model** — the same
scaffold as the SWE-CI reproduction route — so the solver plumbing is already
understood. The evolution-pair benchmarks (SWE-CI / SWE-EVO / EvoClaw) are retained
as **future substrates** for a reinstated replay diagnostic (§9), not used this
month. Industry issue→patch benchmarks contribute *method* only (§5.7, §9).
**Validation-first note:** before the scored runs touch DeepSWE, the cycle machinery
is exercised on **CodeMiner's own history** as a warm-up — a known repo whose test
suite runs cleanly on the host — to de-risk the harness; the reported numbers come
from DeepSWE, not the warm-up (§8 Q1).

> **Reuse note.** DeepSWE's reference solutions (`solution/`) are **never** consumed
> — they are held out from the solver exactly as the benchmark intends, and Guardian
> never sees them. Guardian observes only the solver's own trajectory and the task's
> environment — plus, optionally, the repo's public pre-base commit history; grading is
> DeepSWE's verifier, unmodified.

### 5.2 Memory construction — the evolution the cycle sees

Memory accumulates over **the stream of repository states Guardian observes**. One
mechanism, one kind of input: a sequence of commits, one Guardian cycle per commit,
graph-diff + findings + symbol/test history persisted `tᵢ → tᵢ₊₁` (§3.2). A commit is a
commit — where it sits on the timeline changes nothing about how the cycle treats it.

In a DeepSWE run that stream is simply **the solver's own step/commit trajectory**: as the
solver works the task it evolves the working tree, and Guardian runs one cycle per step.
This alone makes arm C (remembers earlier steps) differ from arm B (each step fresh), so
**C − B is well-defined within a single task with no separate replay** — the solver
generates the evolution memory feeds on. If a task's real pre-base history is deep enough
to be worth mining (§8 Q2), those commits can be **prepended to the front of the same
stream** (`--preseed-commits`) — earlier entries in the identical per-commit sequence, not
a second source or a different code path.

**The invariant that matters is the held-out-solution guard, not the timeline** (§5.7):
DeepSWE ships each task's reference solution and held-out test edits in `solution/`,
and Guardian must never read them — the one constraint on *what* it reads. Note this
is a *don't-open-that-directory* rule, not a localization guard: nothing in Guardian
ever holds a reference patch to be guarded against, so there is no gold-region
comparison anywhere in the design. (Any commit fed to the stream stays at or before the base,
so no post-base state leaks in either.) In the memoryless arm the same commits are visited
but each cycle starts fresh (§5.3), so findings reflect no cross-cycle accumulation.

Validation-first: the cycle machinery is exercised on **CodeMiner's own history** as a
warm-up before any scored task (§8 Q1).

Only the symbol-name normalization helper from `codeminer/eval/retrieval_eval.py`
(`normalize_symbol_identifier`) is reused — to canonicalize and dedup the symbol names a
finding references. Nothing derives a ground-truth or post-`t` future label:
`gt_locate.py` (SWE-bench gold-patch localization) is **not** used, and grading is
DeepSWE's verifier on the solver's output (§5.6), never a comparison against the region
the reference patch touched.

### 5.3 The memory-vs-memoryless ablation (arms B/C)

Two arms, **identical** except for what the agents (orchestrator and investigator) may
read from memory:

| | **Memory arm** | **Memoryless arm** |
|---|---|---|
| Current-state facet (index + graph @ `t`) | ✅ | ✅ |
| Graph-**diff** vs. prior snapshot | ✅ | ❌ (no prior snapshot kept) |
| Prior findings / symbol & test history | ✅ | ❌ (each cycle starts fresh) |
| Everything else (model, budget, retrieval, signals) | identical | identical |

The memoryless arm is a **flag** (`--arm memoryless`), not a separate codebase — it
disables the §3.2 memory read API, so **both** agents (the orchestrator's hypothesize
read and the investigator's history/retrieval reads) start each cycle fresh. Any
difference is attributable to memory, not to implementation drift.

### 5.4 Metrics

The primary signal is the **downstream reward delta** graded by DeepSWE's verifier
(§5.6); the finding-level metrics below are descriptive support, not a separate scored
hypothesis.

| Metric | Definition | Answers |
|---|---|---|
| **Task reward Δ (C − A, C − B)** | DeepSWE `reward.json` (binary reward + pass fractions), Guardian-fed vs solo / vs memoryless | does Guardian help, and does memory add value? — **primary** |
| **Finding precision (rubric)** | fraction of findings judged actionable by a 3-level rubric (actionable / plausible / noise), applied **blind to arm** | are findings *real*? |
| **Memory-unique findings** | findings the memory arm produced that the memoryless arm did not | descriptive evidence memory changes the shortlist |
| **Cost** | tokens + wall-clock per cycle (`cycles` table) | quality *at what cost* |
| **Non-modifying invariant** | 100% of reports carry zero applied diffs | safety (must hold) |

**Primary metric** = memory-vs-memoryless on task reward (**C − B**) at matched
finding-budget, with **C − A** as the product number. n is small, so deltas are
reported as **directional** evidence with per-task points, explicitly not a powered
significance test.

### 5.5 Finding judging

- **Admission gate before any rubric.** Per §0.5 a finding requires a verified claim
  *and* an actionable remedy. Anything failing either conjunct is not a low-quality
  finding — it is not a finding, and does not enter the shortlist. `grade` (computed,
  never assigned) is the gate: only `grade = 'finding'` rows are eligible. This gate
  will reduce the reported count relative to earlier runs; report the count under both
  the old and new definitions so a reader can tell measurement change from regression.
- **Rubric** for finding quality, applied to admitted findings only: a 3-level rubric
  applied **blind to arm**, small enough to do by hand for one task's cycles. The
  levels must grade the *remedy* (directly applicable / needs adaptation / too vague to
  act on), because claim correctness is already the admission gate's job and grading it
  twice double-counts. This is the honest way to characterize findings now that there
  is no post-`t` future to score against — the *scored* claim is the solver reward
  delta (§5.6), not the rubric.
- **Finding selection** (automated, solution-blind): the injected shortlist is Guardian's
  own top-k admitted findings. Ranking must **not** be a pure signal score: "drift
  severity × churn × recency" ranks measurements, so a shortlist built that way is a
  linter's output in a different order and cannot support the proactivity claim.
  Rank instead by verification strength × consequence severity × remedy specificity,
  with signal-derived features permitted only as tie-breakers. There is **no
  target region** — Guardian never has, and never reads, the reference solution; its
  findings land near the solver's work only because memory feeds on the solver's own
  trajectory (§5.2). Symbol names are canonicalized/deduped with
  `retrieval_eval.normalize_symbol_identifier`, but no finding is scored against — or
  filtered by — a ground-truth location. The identical shortlist is produced with the
  solution held out (it always is).
- **No solution peeking**: DeepSWE's `solution/` patch is never read — not at selection
  time and not at judging time (held out exactly as the benchmark intends). The only
  oracle in the loop is the task's program-based verifier (`reward.json`).

### 5.6 Downstream companion-agent protocol (the A/B/C ladder)

The evaluation asks whether memory-grounded findings measurably help a real coding
agent. The construction composes cleanly with DeepSWE: **Guardian accumulates memory
over the commit stream it observes — in a DeepSWE run, the solver's own trajectory
(§5.2).** The honesty guard is a
discipline on *what Guardian may read* — never the reference solution or held-out tests,
and (for any pre-base seed) only commits at or before the base commit
(no-peeking-past-base, §5.7) — **not** on *when* Guardian runs relative to the solver.
Guardian advises a separate solver on
the DeepSWE task: its findings enter the solver's context, and the
attempt is graded by the task's program-based verifier (`reward.json`). Relevance is
**structural, not targeted** — Guardian watches the solver's own edits (§5.2), so its
findings concern the code the solver is changing, which is exactly where the task's
authored fix lands; Guardian never needs to know (and never reads) the reference solution. Guardian runs as a **live advisory alongside the solve**
— memory keeps updating and findings are surfaced as the solver works, matching the
real-time-assistant design — with **every surfaced finding logged with a timestamp** so
the memory-vs-memoryless comparison stays attributable (below). Memory is still built
only from the observed commit stream — never from the reference solution, held-out tests,
or post-base commits — so the honest-memory claim is untouched (memory derives from the
commits Guardian legitimately observes as the solver works).

**The solver harness is a controlled variable, not part of Guardian.** Guardian is
its own harness (orchestrator + investigator); it emits findings and never runs
*inside* a solver CLI. The findings→solver contract is therefore **harness-neutral**:
Guardian exposes its findings as **plain structured text over a
defined channel** (a JSON/markdown finding record — path, span, claim, evidence,
suggested probe), which any Harbor-driven agent slot can consume. For the pilot we
fix the solver on **Pier + mini-swe-agent** (the DeepSWE leaderboard default, so arm-A
resolve rates are comparable to published numbers) on a locally served model — *because holding the harness constant is required for
a clean A/B/C comparison*, not because Guardian depends on it. mini-swe-agent (the
DeepSWE leaderboard default), claude-code, codex, and gemini-cli are drop-in
alternatives behind the same contract; supporting a new harness is a thin adapter
(how the finding record is surfaced into that CLI's context — a live channel where
the CLI supports one, an `AGENTS.md`/context-file prepend or MCP resource where it
does not), never a change to Guardian.

**Three-arm ladder.** Same task, same solver; only the injected context varies:

| Arm | Solver receives | Isolates |
|---|---|---|
| **A** | task only (solo) | baseline resolve rate |
| **B** | task + **memoryless** Guardian findings | a memoryless investigating agent |
| **C** | task + **memory** Guardian findings | + cross-cycle memory |

The value is in the deltas, each tied to a distinct claim:

- **C − A** — does the Guardian agent help a solver at all? → **product/system claim (headline number)**
- **C − B** — does persistent memory add value on top of the agent? → **the research question**
- **B − A** — how much is just a memoryless investigating agent? → completes the decomposition

The discipline: **C − A is the number to advertise, C − B is the one that supports the
memory thesis** — both reported, so the failure mode "gain is all from having a second
agent, memory adds nothing" (B − A ≈ C − A) cannot be hidden.

**Confounds & controls.**
- **Ceiling effect** — pair a *mid-capability* solver (open SWE-agent scaffold on
  Qwen2.5-Coder), not a frontier model that already resolves solo; no headroom → Δ≈0
  regardless of memory quality.
- **Variance** — rewards are sampling-noisy; K≥3 seeds per task per arm. Cost is
  3 arms × N tasks × K seeds, hence reduced scale.
- **Help *and* harm** — report two-way flips: fail→pass (helped) and pass→fail (a wrong
  finding distracted the solver). Net Δ hides this; the flip counts make precision matter.
- **Injection interface** — Guardian advises **live**: its findings are
  surfaced into the solver's context as the solve proceeds (an interactive advisory
  channel), and memory updates during the attempt. The finding *record* is harness-neutral
  (structured path/span/claim/evidence); only the *surfacing mechanism* is harness-specific
  — a live channel where the CLI supports one, else a context-file/MCP prepend — so
  swapping mini-swe-agent for opencode/claude-code/codex changes an adapter, not the arms. To keep C − B attributable despite the
  extra moving part, **log every surfaced finding with its timestamp and the solver step it
  landed at**, and hold the advisory *policy* (retrieval rule, budget, cadence) identical
  across arms B and C so the only variable is whether the findings draw on cross-cycle
  memory. A **static top-k** variant (prepend the shortlist once, no live channel)
  is retained as a lower-variance robustness check — if live and static agree on the sign of
  C − B, the headline is not an artifact of the live channel.
- **Efficiency axis** — log steps/tokens-to-solve; memory may not raise the ceiling but
  still get the solver there faster/cheaper, a second Δ worth capturing.
- **Language coverage** — DeepSWE is polyglot; the pilot fixes on **one language
  (Python)** to keep the solver scaffold and warm-up comparable, and notes cross-language
  generalization as month-2 scale-up.

**Prototype scope — committed in-month pilot.** The month commits to a
**directional A/B/C pilot**: a deliberately small slice — **~8–12 DeepSWE tasks**
(single language), one mid-capability solver, arms A/B/C, K=3 seeds — run in Week 4
once the memory-construction harness and solver scaffold have landed. That is
~70–110 solver rollouts, which fits the last days on the single H100. It reports
**C − A, C − B, B − A + two-way flips** with explicit "directional, n small, not
powered" caveats — enough to show the downstream number *moves* (or does not) and to
size the full run. The **full run** (more tasks, more languages) is the month-2
scale-up (§8); the pilot exists so the headline claim is *demonstrated within the
month*, not merely designed.

### 5.7 Threats to validity (stated up front)

- **No-peeking-past-base** — the biggest risk. During memory construction, any
  information after commit `ti` (up to and including the task's base) must be excluded
  from the sandbox and index. Enforced by checking out `ti` in a fetch-only mirror,
  building the index from that tree only; an audit asserts no artifact carries a commit
  > `ti`. Guardian never touches anything after base, and never reads DeepSWE's held-out
  `solution/`. Note this guard is about *what Guardian reads* (commits ≤ base), **not**
  about *when* it runs: Guardian advising live alongside the solver does not weaken it,
  because the solver's in-progress attempt is not future repo state and carries no ground
  truth — observing the solve leaks nothing the honesty guard protects.
- **Live-advisory attribution** — because Guardian advises live (memory updating during
  the solve), the C − B delta could in principle blend *memory's* contribution with *extra
  live-agent reasoning spent mid-solve*. Controlled two ways: (1) the advisory **policy**
  (retrieval rule, budget, cadence, compute) is held identical across arms B and C, so the
  only variable is whether findings draw on cross-cycle memory; (2) every surfaced finding
  is logged with a timestamp and solver step, and a **static top-k** variant (§5.6) re-runs
  the comparison with no live channel — agreement on the sign of C − B between live and
  static rules out a live-channel artifact.
- **Training-data contamination** — a distinct leak: if the LLM **memorized** the public
  target repo, the memoryless arm gets an unearned boost from *parametric* memory,
  confounding the C − B contrast. This is why **SWE-bench Pro** sources from copyleft
  repos as a training-set deterrent. Mitigations: prefer tasks whose repos have history
  past the model's training cutoff, and treat any suspiciously-strong memoryless result
  as a contamination signal to investigate, not a null for the memory claim.
- **Finding-relevance incompleteness** — not every real risk coincides with the code the
  solver ends up editing; a memory-unique finding that lands nowhere near the solver's
  work simply cannot move the solver, so the reward delta is a *lower bound* on memory's
  value, reported as such.
- **Reduced-N pilot** — external validity limited by design this month; the prototype
  shows the metric *moves* and sizes month-2.
- **LLM nondeterminism** — fixed decoding params, seeded where the server allows,
  deterministic signals kept model-free so the comparison isn't swamped by sampling noise.
- **Judging bias** — rubric scoring done blind to arm.
- **Invalid synthesized tests** — a test that fails for the *wrong* reason (import error,
  typo, non-contract assert) is a false positive dressed as evidence. Mitigations: the
  test must import and exercise the *real* symbols under investigation; a bare
  collection/import failure is discarded; and the **differential run** (§3.4) separates
  "fails because the risk is real" from "fails because the test is broken" — a test that
  also fails on the *prior-cycle* checkout proves nothing. We report the validity-gate
  pass rate as a harness-health metric. (Related method: FrontierCode's *reverse-classical*
  check — an agent-written test must fail on the un-fixed code — is the same idea.)

### 5.8 Baselines beyond the ablation

The ablation (§5.3) isolates *memory*; it does not tell us whether the agent beats a
trivial heuristic or existing tooling. So we also score a ladder of **external reference
points**, all run on the **same observed cycles, same task, at matched budget**,
scored the same two ways: the finding rubric (§5.5) and — where cheap enough — as an
extra injection arm fed to the same solver (§5.6). A baseline is just "emit findings in
`Finding` format, reuse the scorer."

| Baseline | Cost | Question it answers | Prototype |
|---|---|---|---|
| **Churn/static ranker** — rank symbols by git churn (+ complexity delta), flag top-k | ~free | Do you need an agent over a heuristic? (just-in-time-defect-prediction floor) | **Include** |
| **Graph-diff-only** — emit the §3.3 drift shortlist directly, no hypothesize/investigate | ~free | Does LLM investigation add precision over the raw detector? | **Include** |
| **Random / oracle** — random-at-budget (floor); an oracle that names the task's own touched region (ceiling on injection relevance) | ~free | Floor & ceiling that make every number interpretable | **Include** |
| **Linter suite** — `ruff`/`mypy`/`bandit` warnings *new at `base`* as findings | low | Would existing CI tooling already catch it? (rubric precision@k) | If the CI story matters |
| **Reactive agent** — same model+retrieval, fed the DeepSWE task's own instruction text to localize | high | Proactive-with-memory vs. conventional reactive | **Stretch** (also §8) |

The three "~free" rows turn the C − B delta from a bare number into a *positioned* one —
target shape a monotone ladder: **memory > memoryless > graph-diff-only > churn-rank >
random**, oracle marking the ceiling. At n = 1 repo the ladder is **directional** (the
ordering is the claim). External systems (RepoAudit, graph localizers, SWT-Bench — §9)
are comparators on *sub-tasks*, not head-to-head on the full persistent-with-memory
setting, since no published system is persistent.

---

## 6 · Task decomposition & roadmap

The itemized task decomposition, the dependency roadmap (with per-task effort
estimates), and the week-by-week schedule now live in a single planning home —
**`project_schedule.md`** — so the plan has one source of truth. This section
states only the *shape*; the schedule doc carries the detail.

Six components, **ordered by research priority** (not build order):

1. **① Loop control** — *primary contribution.* How Guardian's nested loops —
   an outer orchestration loop around an inner investigation loop, plus a third
   loop forked on the solver side for the live advisory channel — are
   **engineered**, and how **context flows** through them. The contribution is
   the control structure and the context discipline, not any single prompt.
2. **② Repository memory** — *the research question.* Held as a **single module
   (internals deferred, ≈1 week)** — the eventual state model is an open design
   question — but with three fixed interface contracts the rest of the project
   depends on: it **accumulates across cycles**, its cross-cycle provenance is
   **demonstrable** (the M2 assertion), and it has a **clean memoryless mode**.
   That memoryless toggle is the lever that makes C−B attributable to memory, so
   it is non-negotiable even while the internals stay open.
3. **③ Tools** — *commodity; hold at a floor.* Graph-diff drift detection
   (largely done) and the investigator's probe toolset. No further investment —
   but no regression below the usefulness floor, or findings become noise and
   both deltas collapse for a boring reason.
4. **④ Evaluation protocol** — *first-class apparatus.* The machinery that turns
   "Guardian produced findings" into a graded move in DeepSWE's own
   `reward.json`, under a controlled A/B/C comparison. **No Guardian-defined
   metric, no localization score, no ground-truth region.**
5. **⑤ Infra** — *enabling.* Local model serving and the per-cycle sandbox
   runtime. Low priority, but the serving slice is a **week-1 prerequisite** —
   ① and ② have nothing to run on without it.
6. **⑥ Paper** — *parallel track.* Methods and related work while ①/② are built,
   results as experiments land, and ≥2 weeks of concentrated writing at the end.
7. **⑦ Experiments** — *the runs that answer the question.* Distinct from ④:
   ④ **builds** the co-working harness and ② supplies the memoryless toggle; ⑦
   **executes** them — the A/B/C ladder (7.1, headline) and the baseline ladder
   (7.2) — and reports the graded deltas. Runs, not builds, but the deliverable
   that carries both claims, so a decomposed workstream of its own.

Priority is **not** sequence: ⑤'s serving slice lands first so ①/② have
something to run on, ⑥ runs concurrently throughout, and ⑦ is the tail — the
headline run is the last link of the build chain. See
**`project_schedule.md`** for the itemized decomposition, the dependency
roadmap, and the full 8-week schedule.

---

## 7 · One-month schedule

![Repository Guardian 4-week prototype schedule]({{artifact:art_5eb56cd2-3868-4d2c-89ef-f3bd0e370770}})

Milestones (also on the chart): **M1** end-to-end cycle on the remote host — mine a
repo's history to a base commit, run a cycle, write a report (≈ day 7)
· **M2** memory + graph-diff persist and are *used* across cycles (≈ day 16)
· **M3** memory-construction harness builds memory over ≥2 commits and emits a findings
shortlist for both arms (≈ day 21) · **M4** solver runs one DeepSWE task solo through
Pier + mini-swe-agent, graded by `reward.json` (≈ day 26) · **M5** directional A/B/C pilot on
~8–12 tasks reporting C−A / C−B / B−A on DeepSWE reward (≈ day 28). The full (more
tasks, more languages) run is the month-2 scale-up.

| Week | Focus | Exit condition |
|---|---|---|
| **1** | Runtime up incl. **container sandbox** (5.2; 5.1 serving off-path) + graph-diff detector (3.1) + memory module bootstrap (2); co-working-harness skeleton (4.2) on top of the proven basic reproduce (4.1) | **M1** — a cycle runs end-to-end **inside a per-cycle container** on the host against whichever endpoint is up (local or subscription), BM25→retrieval path both work, production tree provably untouched. |
| **2** | Memory module (2) — read/write + cross-cycle provenance test + memoryless toggle; outer-loop hypothesis ranking over paged memory (1.1, 1.5); findings shortlist ranking (part of 4.2) | **M2** — cycle `k` provably uses cycle `k−1`'s memory; drift findings appear. |
| **3** | In-sandbox investigation loop over the probe toolset (3.2) — inner loop (emits the evidence/finding record), budget, context management incl. sub-agent isolation (1.2, 1.4, 1.5); **evaluation apparatus already proven (4.1, 4.2, 4.3)** — basic reproduce, co-working harness + injection interface, and output structure all work | **M3** — co-working harness emits findings shortlists for both arms; solo solver runs a DeepSWE task graded by `reward.json`. |
| **4** | Finish both-arm harness + output structure (4.2, 4.3) → M4; then launch the **directional A/B/C pilot** (⑦ 7.1, §5.6 — a run on the apparatus) on ~8–12 tasks; write-up + demo (component ⑥) | **M4** — solver runs a DeepSWE task solo through Pier + mini-swe-agent, graded. **M5** — A/B/C pilot reports C−A / C−B + flips on DeepSWE reward, directional; demo report in hand. |

**Critical path:** **5.2 (container)** → 3.1 → 2 → 1.1 →
4.2. The container is now the head of the critical path (it's an M1 exit
condition), so it's built in week 1 against a HEAD smoke-cycle before the graph/
memory work depends on it. **Local model serving (5.1) is deliberately off the
path** — every arm can run against a subscription endpoint through Pier's
allowlist (as 4.1 already did), so serving buys cost, throughput and
reproducibility rather than gating any experiment; the ceiling-effect confound
(§5.6) is governed by model *capability*, which either endpoint can supply. If
time slips, the **cut order** is: richer
investigation experiments (keep one) → optional pre-base pre-seed (drop first; the solving
trajectory carries memory on its own) →
container **network** hardening can fall back to a localhost-only allow-rule
(never drop the container itself) → **never** cut the memoryless arm (it *is* the
result).

---

## 8 · Open questions — to discuss

These genuinely change the build and I'd like your call before/early in week 1:

1. ~~**Target repo for future-history replay.**~~ **Resolved (your call):
   DeepSWE is the primary and sole substrate; validate on CodeMiner itself first.**
   The cycle machinery is exercised first on **CodeMiner's own history** — we know its
   history and its test suite runs cleanly on the host, which de-risks the harness
   before it meets an external corpus — and the graded A/B/C ladder then runs on
   **DeepSWE** tasks (§5.1): Guardian mines each task repo's real history up to the
   task's base commit, and the solver is graded by the task's `reward.json`. The
   warm-up carries a mild "over-fitting" worry (hand-tuning signals to a codebase we
   wrote), mitigated because the evidence is an *agent-synthesized* test (§3.4), not
   one we curated, and because the reported numbers come from DeepSWE, not CodeMiner.
   *Remaining sub-questions:* (i) which slice of CodeMiner history for the warm-up — a
   dense recent window (more drift per cycle) or a longer sparse span? (ii) which
   **single language** to fix the pilot on (Python is the default; DeepSWE also has
   TypeScript / Go / Rust / JavaScript for month-2 cross-language generalization)?
2. **Commit-stream depth** — memory accumulates over the observed commit stream, which in
   a DeepSWE run is the solver's step/commit trajectory (§5.2); this alone makes C−B
   well-defined within a task. If we choose to prepend a task's pre-base history to that
   stream, how many pre-base commits `k` per task is worth mining without blowing the
   per-task budget — a fixed `k` (e.g. 8) or region-triggered (last commits touching the
   task's files)? Left open because the pilot does not depend on prepending any.
3. **Memory backend depth.** SQLite + JSON + graph snapshots is my prototype
   choice (inspectable, no service). Are you OK deferring a vector/graph-DB memory
   to month 2, or do you want the embedding-indexed memory in from the start?
4. ~~**Which local model.**~~ **Resolved by the confirmed hardware (1× H100 80 GB,
   §4.1):** default is **Qwen3-Coder-Next** (80B-total / 3B-active MoE, 256K
   context) served on vLLM in 4-bit — coding-agent specialist, fits the card with
   ~35–40 GB KV headroom, runs at ~3B active speed. Fallbacks: **Qwen3.6-27B fp16**
   (dense, adds `<think>` traces) or **Qwen3.6-35B-A3B 4-bit** (leanest). Note the
   repo's `#248` backend is wired for the older Qwen2.5-Coder line, so a small
   config bump to the Qwen3 checkpoint is a week-1 task. *Remaining sub-question:*
   run a one-day week-1 spot check (a handful of drift cases, default vs the
   `<think>`-capable 3.6-27B fallback) to confirm the non-thinking model's
   hypothesis quality is adequate before committing the budget — worth doing?
5. **Investigation experiment menu.** Core action is now **test synthesis**
   (agent writes a risk-revealing test), corroborated by differential run and/or
   fix-probe; cheaper first-pass probes are {existing-test run, call-site scan,
   import/typecheck} (§3.4, your call). *Sub-question to freeze the menu:* is
   **differential run** (synthesized test PASS-on-prior / FAIL-on-current) required
   corroboration, or optional when a plain FAIL + clear reasoning is already
   convincing? (It's the strongest signal but doubles the test runs per finding.)
6. **"Actionable" rubric.** For findings not tied to a later concrete fix, who
   scores them and against what 3-level rubric? Affects how much of §5.5 is manual.
7. ~~**Isolation level.**~~ **Resolved (your call):** container isolation is
   **required in M1** — an ephemeral per-cycle container (worktree checkout mounted
   read-only, `--network none` except a localhost allow-rule to the host model
   server). Worktree-only is retained as a debug mode (§4.3). *Remaining
   sub-question:* Podman (rootless, my default) vs. Docker on the host — any
   constraint from how the H100 box is provisioned (rootless cgroups, existing
   Docker daemon)?

---

## 9 · Related work & external baselines

No published system is *persistent* or *memory-carrying* — they are all reactive or
single-shot. That gap is precisely this project's contribution, but it also means
each external system is an alternative **finding-producer**, not a head-to-head on the
full proactive-with-memory setting. We use them in three roles. Because our only oracle
is the solver reward delta (no localization ground truth, §5.5), a runnable comparator
is scored the same way our own findings are: swap its findings into the solver's context
in place of Guardian's and read the reward delta.

**Runnable comparators (drop-in finding-producers, scored by the same reward delta).**
- **RepoAudit** (arXiv 2501.18160) — autonomous LLM-agent for repo-level code
  auditing; the single closest system. It reports **precision at bounded cost**
  (~$2.5 / repo), a useful cost reference, and already benchmarks against two
  industrial static detectors (Meta **Infer**, Amazon **CodeGuru**) — giving us a
  named agent comparator *and* two static-analysis floors.
- **Graph-based localizers** — **LocAgent**, **CoSIL**, **GraphLocator** build a
  repository call graph and reason over it (closest to our Observe / graph-diff
  selection). They are *reactive* and *memoryless*, so they slot in as "does persistent
  memory beat one-shot graph localization at producing findings that help the solver?"
  — the memoryless-graph point on the §5.8 baseline ladder.
- **Static analyzers as findings** — Infer / CodeGuru / `ruff` / `mypy`; the
  linter row of the §5.8 ladder, with RepoAudit's numbers as a reference point.

**Benchmark that validates our core mechanism (reuse the harness).**
- **SWT-Bench** (NeurIPS 2024, arXiv 2406.12952; harness `logic-star-ai/swt-bench`)
  — generates reproducing tests from an issue and judges them by **fail-before /
  pass-after the gold patch**: the formal version of our differential-run
  corroboration (§3.4). Two of its findings de-risk our design — repair-oriented
  agents outperform dedicated test-generators (our investigate-loop-writes-tests
  approach is sound), and generated tests roughly **double** a fix's precision as a
  filter (evidence test-synthesis genuinely sharpens precision). **The distinction
  to state in the writeup:** SWT-Bench synthesizes a test from a *known issue
  description*; Guardian synthesizes one from a *self-discovered drift signal with
  no issue text*. Related: **Issue2Test**, dynamic bug-reproduction-test cogeneration
  in agentic repair (arXiv 2601.19066).

**Framing / cite-only (position, don't run).**
- **SWE-EVO** — coding agents over long-horizon software *evolution*; the closest
  published framing of our "monitor a repo as it evolves" setting.
- **Needle in the Repo** (arXiv 2603.27745) — maintainability of AI-generated repo
  edits; for the architectural-degradation half of the research question.
- **Just-in-time defect prediction** (Kamei et al.; DeepJIT) — the classical
  change-level churn/complexity literature our churn-ranker baseline (§5.8)
  descends from; cite so the deterministic baseline isn't seen as ad hoc.

**Evolution benchmarks (data substrate — §5.1).**
- **SWE-CI** (arXiv 2603.03823, Skylenage) — 100 base→reference commit pairs (avg 233
  days / 71 commits apart), 68 repos, Docker per instance. Its thesis paraphrases our
  RQ: two solutions passing the same tests differ in maintainability only visible when
  the codebase must evolve — the closest prior framing to our memory thesis, but base→reference pairs (no authored task) make it a **future substrate**, not this month's.
- **SWE-EVO** (arXiv 2512.18470, Fsoft-AIC) — 48 release-transition tasks over mature
  Python projects, F2P + P2P tests. **Secondary substrate** (Python-native, matches our
  reuse modules).
- **EvoClaw** (arXiv 2603.13428, Hydrapse) — reconstructs verifiable *Milestone DAGs*
  from commit logs; the structured evolution decomposition is a natural evaluation target
  for our graph-diff drift signal.
- **SWE-Marathon** (arXiv 2606.07682) — billion-token long-horizon tasks; motivates the
  long-horizon/memory framing (§2) but has **no repo-history axis** and costs ~27M tokens
  per rollout, so it is cite + future-work only, not a base.

**Industry benchmarks (method sources — wrong axis, right engineering).** All are
single-commit issue-resolution and cannot test the persistent-memory claim, but each contributes method:
- **SWE-bench Pro** (arXiv 2509.16941, Scale AI) — copyleft-sourced for **contamination
  resistance**; the reference treatment for our §5.7 training-data-contamination threat.
- **SWE-Lancer** (OpenAI, `openai/swelancer-benchmark`) — $1M of real Upwork tasks graded
  by **end-to-end** tests (not unit tests, which memorized models can game) plus a
  manager-choice task; a model for value-weighted finding grading.
- **Multi-SWE-bench** (arXiv 2504.02605, ByteDance) — multilingual (7 languages), fully
  permissive; a per-language runnable harness reference.
- **FrontierCode** (Cognition, hosted) — maintainer-authored blocker/non-blocker rubric
  scoring with LLM-anchored-to-deterministic grading; the model for our §5.5 finding rubric
  and (its *reverse-classical* check) our §5.7 test-validity gate. Data is proprietary and
  single-commit, so method-only.

*Suggested paper structure:* the downstream A/B/C companion-agent gain as the
headline (C − A); memory-vs-memoryless as the research contrast (C − B) that
carries the persistent-memory thesis; the free deterministic baselines (§5.8) as the floor ladder;
RepoAudit / a graph-localizer
as "vs. a reactive agent on the sub-task"; SWT-Bench as validation that the
test-synthesis mechanism measures what we claim. (arXiv IDs are from a 2026 search
pass; verify the exact venue/version before citing.)

---

## Appendix A · Reuse map (design cites real modules)

| Need | Module / symbol (verified in tree) |
|---|---|
| Cycle loop + seams | `codeminer/guardian/cycle.py` (`run_cycle`, `GuardianConfig`) |
| Signals (churn, tests) | `codeminer/guardian/signals.py` (`churn_hotspots`, `run_test_suite`) |
| Evidence + retrieval mapping | `codeminer/guardian/investigate.py` (`Evidence`, `investigate_hotspot`) |
| Report render (no-patch invariant) | `codeminer/guardian/report.py` (`GuardianReport`, `render_markdown`) |
| Incremental index | `codeminer/index/incremental/` (`GitDiffDetector`, `IncrementalIndexUpdater`, `IncrementalState`) |
| Code graph + snapshots | `codeminer/graph/code_graph.py` (`save_graph`, `load_graph`); `graph/dependency.py`, `graph/hierarchy.py`, `graph/roi_subgraph.py` |
| Incremental graph patch | `codeminer/graph/incremental/` (`GraphPatcher`, `change_mgr.detect_changed_files` / `get_changed_line_ranges`, per-language patchers) |
| Hybrid retrieval | `HybridRetrievePipeline.query(query, top_k)` |
| LLM via local model | `codeminer/llm/litellm_chat.py` (`LiteLLMChat`, `ChatMessage`, `_with_prompt_caching`); `codeminer/llm/usage.py` |
| Symbol normalization | `codeminer/eval/retrieval_eval.py` (`normalize_symbol_identifier`) — canonicalize/dedup finding symbol names only; **no** localization scoring, **no** `gt_locate.py` |
| CLI patterns | `scripts/guardian_cycle.py`, `scripts/index_repo.py`; local-model setup `docs/running-locally.md` |

> **Note on a stale path.** The RFC/idea text refers to `codeminer/incremental/`;
> in the current tree that top-level package is empty. The real machinery is split
> across **`codeminer/index/incremental/`** (index/git-diff) and
> **`codeminer/graph/incremental/`** (graph patching). The design above cites the
> real paths.

## Appendix B · Artifacts

- `prototype_design.md` — this document.
- `architecture_diagram.png` — per-cycle dataflow + two-facet perception layer (§3.1).
- `schedule_gantt.png` — 4-week schedule with milestones M1–M5 (§7).
