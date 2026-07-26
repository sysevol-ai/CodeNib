# Guardian Outer Loop — Blueprint v2

**Scope.** This document specifies **L2, the cycle loop** — the level the design
calls "the outer agent loop" and the level that does not currently exist as a
loop. It also fixes the contract with the level above (L1, the campaign) and the
level below (L3, the investigation loop, which is implemented and stays as-is).

**Companion documents.** `design/sandbox_runtime_blueprint.md` (v3) fixes *where*
the loop's actions execute and who may write what. This document fixes *what the
loop decides, what it carries, and when it stops*. The two are orthogonal by
construction: the sandbox blueprint's one-write-boundary invariant is assumed
here and never re-litigated.

**Status of claims.** Every statement about current behaviour carries a
`file:line` citation into `codeminer/guardian/`. Every borrowed pattern carries a
citation into a named external source read from disk, not from documentation
prose. Claims that could not be verified against source are marked *unverified*.

---

## How to use this document

It has three kinds of section, and they are not read the same way.

| Sections | Kind | How to read |
|---|---|---|
| §0 (with §0.1–§0.3) | **Normative definitions.** Signal / hypothesis / finding, the grade ladder | Read first, in full. Every later section depends on these three words meaning exactly one thing each |
| §1, §2 | **Rationale.** What loop engineering is; the ten questions a loop design must answer, each with the current answer from source | Skim on first pass. Return to a single Q when you disagree with the corresponding decision in §3 |
| §3, §4 | **The specification.** Carried state, tools, exits, loop body, context | This is what gets implemented. §3.1 and §3.5 are the two sections to read twice |
| §5 | **Findings about the current code.** Eleven defects with `file:line` evidence | Read §5.0 (it is a prerequisite, not a defect); use §5.1 as a checklist |
| §6 | **The plan.** Eight steps, each with a done-when test | Follow in order. Step 0 is breaking and comes first |
| §7, §8 | **Consequences and risks.** What changes about the evaluation; ten open risks | Read before running an experiment, not before writing code |
| §9 | **Handoff notes.** What is settled, what is not, what to check first | Read if you are picking this up cold |

**Three reading paths.**

1. *Implementing it.* §0 → §3 → §6, with §5.1 open beside you as the defect
   checklist. §1, §2, §7 and §8 are not needed to write the code.
2. *Reviewing the design.* §0 → §0.3 → §1 → §2, then §3.3 and §4.4, which are the
   two places the design makes a contestable choice and says so.
3. *Running the experiment.* §0 → §7 → §8, then §3.1 for what gets logged and
   §4.3 for what the memory manipulation actually is.

**Prerequisites.** The reader is assumed to know: the Guardian cycle as it
currently runs (`codeminer/guardian/cycle.py`), the sandbox contract
(`design/sandbox_runtime_blueprint.md` v3, §3.1 mounts and §5 probe boundary),
and the A/B/C arm structure (`prototype_design.md` §5). No familiarity with
earlier drafts of this document is needed; positions that were considered and
rejected are listed once, in §9.5.

**Companion documents, and which question each answers.**

| Document | Answers |
|---|---|
| `design/sandbox_runtime_blueprint.md` (v3) | *Where* loop actions execute, and who may write what |
| `design/terminology_migration.md` | *Which files change* to make §0 true in code — the concrete edit list for §6 step 0 |
| `prototype_design.md` | The experiment this loop serves: arms, metrics, schedule |
| **This document** | What the loop decides, what it carries, and when it stops |

**Convention.** Code identifiers in backticks with a line number
(`cycle.py:521`) are *current* code. Code identifiers without one are *proposed*.
Section cross-references are always to this document unless a filename precedes
them.

---

![Guardian's four loop levels: L1 campaign, L0 trigger, L2 cycle, L3 investigation]({{artifact:art_eaf6369a-7a16-4233-967a-693858714102}})

---

## 0 · Terms

These definitions are normative for this document and are proposed as normative
for the project. Before this section existed, `prototype_design.md` used
"finding" 95 times without defining it once, and the consequences of that gap
are catalogued in §5.0.

**Signal** — a deterministic, cheaply-computed observation about the repository.
Churn counts, graph-diff deltas, test-status changes. A signal is a *measurement*.
It has no engineering content: it says something changed, never that something is
wrong or what to do. `A.txt has been modified three times recently` is a signal
and can never be more than one.

**Hypothesis** — a falsifiable claim that the repository contains a specific
engineering defect or improvement opportunity, together with a sketch of the
remedy that makes it solvable. A hypothesis is the unit the loop carries,
schedules, budgets, and verifies. Every hypothesis has three parts, and a
candidate missing any of them is not yet a hypothesis:

| Part | Requirement |
|---|---|
| `claim` | A falsifiable statement about behaviour. There must exist an experiment whose outcome could refute it. |
| `consequence` | What breaks, degrades, or is lost if the claim is true. This is what makes it a *risk* or an *opportunity* rather than a curiosity. |
| `remedy` | A concrete engineering change that would resolve it. This is what makes it *solvable*. |

**Finding** — a hypothesis that has been **verified** and whose **remedy is
actionable**. Not a separate object: a finding is a hypothesis in a particular
state. Formally, `findings ⊆ hypotheses`, and

> `is_finding(h) ≡ verified(h.claim) ∧ actionable(h.remedy)`

Both conjuncts are required. A verified claim with no workable remedy is a
*supported* hypothesis, not a finding — it is true but not yet useful, and it
belongs in the backlog rather than the report. This is the distinction the
current implementation collapses (§5.0).

![Signal, hypothesis, and finding: three distinct kinds of object]({{artifact:art_50bd9514-a23c-4dd4-b1e5-62296300c9f9}})

**Worked example** (the one the rest of this document is calibrated against):

- *Not a finding, and not a hypothesis either* — "A.txt has been modified three
  times recently." A signal. No claim, no consequence, no remedy.
- *A hypothesis* — claim: "`parse_config()` and `normalize_config()` handle
  empty input inconsistently"; consequence: "downstream callers fail
  unpredictably"; remedy: "consolidate their validation logic and add regression
  tests."
- *A finding* — the above, once a probe has demonstrated the inconsistent
  handling and the consolidation is confirmed to be a change someone could
  actually make.

### 0.1 Signals are supplementary, not necessary

A hypothesis may be *suggested* by a signal, but is not defined by one and does
not require one. The `origin` field (§3.1) records where a hypothesis came from:

| Origin | Description |
|---|---|
| `signal` | A churn/drift/test delta drew attention to a region, and reasoning over that region produced a claim. |
| `memory` | Prior cycles' verdicts, recurrences, or refutations imply a new claim. |
| `exploration` | Reasoning over the code or its architecture with no triggering signal — reading a module and noticing an inconsistent contract. |
| `human` | Seeded by a developer. |

This matters beyond tidiness. **If hypotheses can only originate from signals,
the proactivity claim is unreachable.** A signal-gated agent can only ever
surface what a deterministic detector already flagged; the agent is then a
ranking layer over a linter, and no amount of memory changes that. The research
question asks whether persistent memory surfaces opportunities a task-driven
agent would miss — which requires `origin ∈ {memory, exploration}` to be
populated at all. In the current implementation it cannot be, because the
hypothesize prompt instructs the model: `Only reference targets that appear in
SIGNALS; do not invent new paths` (`orchestrator/runner.py:79`).

### 0.2 The grade ladder

A hypothesis carries a `grade` recording how far it has been taken. It is an
epistemic state, not a scheduling state.

| Grade | Meaning | In the report? |
|---|---|---|
| `conjecture` | Claim stated; not yet investigated. | no |
| `supported` | Evidence gathered and the claim holds, but no actionable remedy yet. | backlog only |
| `finding` | Verified **and** actionable. | **yes** |
| `refuted` | An experiment contradicted the claim. | retraction, if previously reported |
| `deferred` | Not worth further budget now; may be revived. | backlog only |

The grade is the *only* lifecycle field. "Open" reads as
`grade ∈ {conjecture, supported}`; "in progress" is one hypothesis at a time and
so belongs to the loop's state (`CycleState.current`) rather than being
replicated on every record. A `supported` hypothesis can be picked up again when a remedy becomes
apparent; a `refuted` one is retained, because refutations are what stop the loop
re-deriving the same wrong claim every cycle.

The grade is **written by the agent and validated by code** — `GRADE_RULES`
(§3.1) makes `finding` unreachable without a remedy and a probe, but which grade
the evidence supports is a reading of the evidence, not an arithmetic function of
it.

---

### 0.3 Summary — the argument in three paragraphs

Guardian has three nested levels and one gate. L3 (investigation) is a real
agent loop. L2 (cycle) — the level this document is about — is **a fixed linear
sequence with two `for` loops inside it**, not a loop: it cannot revisit a
decision, cannot spend a marginal token where it would buy the most evidence,
and carries nothing across iterations except whatever `recent_findings(k=5)`
happens to return. L1 (campaign) and L0 (trigger) do not exist in code at all.

The research question — *can persistent repository memory let an agent
proactively find maintenance opportunities a task-driven agent would not* — is a
claim about **what the agent decides to do**, and it is currently untestable for
three independent reasons. (1) The objects the loop carries do not distinguish a
measurement from a claim from a verified solvable problem (§0, §5.0), so a
memory contrast measured over them answers a much weaker question than the one
the project asks. (2) There is no decision to influence: the outer level makes
one model call and then executes fixed code, so memory can only change the input
to a single ranking (§3.2, §3.3). (3) Memory reaches that one call through one
fixed fetch, and the agent cannot ask for anything else (§4).

All three are the same mistake at different layers — code deciding what the
agent should have decided — which is why the specification is a toolbox and a
turn loop (§3.2, §3.5) rather than a step sequence, why context is pull rather
than push (§4.2), and why §6's plan gives the agent its agency (step 3) before
it gives it memory (step 4).

---

## 1 · What loop engineering is

### 1.1 A loop is not a pipeline

A pipeline is a fixed sequence of stages. State flows forward; each stage runs
once; the output of the last stage is the result. `_run_cycle_inner`
(`cycle.py`) is a pipeline: sync → index → memory-load → observe → hypothesize
→ retrieve → investigate → findings → report → persist. Straight line, no
re-entry.

A loop has four things a pipeline does not:

1. **Carried state** — a value that survives an iteration and is read by the
   next one. Without it, iteration `n+1` is indistinguishable from iteration
   `1`, and "persistent" is a claim about the filesystem rather than about
   behaviour.
2. **A policy** — a decision, taken from the carried state, about what to do
   next. If the next action is a function of position in the sequence rather
   than of state, there is no policy and nothing to evaluate.
3. **A termination predicate** — a condition on state, not on a counter, under
   which iterating stops. "Ran out of list" is a pipeline ending; "no open
   hypothesis is worth its budget" is a loop terminating.
4. **An invariant** — a property true at every iteration boundary, which makes
   the loop safe to stop, resume, or crash inside of.

Loop engineering is the discipline of specifying those four things *before*
writing prompts. The prompt determines how well one step is performed; the loop
determines whether performing steps well accumulates into anything.

### 1.2 Why this matters more for a persistent agent than for a task agent

A task agent is given a goal and terminates when it meets the goal. Its loop
needs only enough structure to stop: SWE-agent-family agents run
think → act → observe until submission or a limit. Carried state is the message
history; the policy is the model; termination is `Submitted` or a limit.

A persistent agent has **no terminating goal**. It runs forever over a moving
target. Three consequences reshape the design:

- **Termination becomes budgeting.** Since the agent never "finishes", every
  loop level needs an explicit resource predicate, and the interesting
  engineering question is *allocation* — which of ten candidate investigations
  gets the marginal probe — not *completion*.
- **Carried state becomes the product.** For a task agent, state is scaffolding
  discarded at submission. For Guardian, the carried state (the backlog, the
  verdict history, the calibration) **is** the persistent memory whose value the
  research question is about.
- **Idempotence and resumability become mandatory.** A loop that runs for weeks
  will be interrupted. If an interrupted iteration corrupts carried state, the
  memory arm degrades for reasons that have nothing to do with memory.

### 1.3 Prior art actually read

**mini-swe-agent v2.4.6** (MIT; source at
`/private/tmp/msa/mini_swe_agent-2.4.6/src/minisweagent`). Two patterns are
worth taking and one is worth explicitly declining.

Its agent decomposes into `run()` / `step()` / `query()` / `execute_actions()`
(`agents/default.py`), with the loop body reduced to a `while True` whose only
exits are typed exceptions. All limit checks live in `query()` — not scattered
through the loop body — and every exit is a subclass of one root
(`exceptions.py:1-26`):

```
InterruptAgentFlow        # root: carries messages to append
├── Submitted             # task complete
├── LimitsExceeded        # cost or step limit
│   └── TimeExceeded      # wall-clock
├── UserInterruption
└── FormatError           # model output unparseable
```

The two patterns to take: **limits are checked in one place and raised as typed
exits**, and **every exit carries messages** so the transcript explains its own
ending. The pattern to decline: their carried state is the flat message list, and
their persistence is `finally: self.save(...)` writing a trajectory each step.
That is right for a bounded task and wrong for us — our carried state is
structured and must outlive the process, which is why §3 specifies a typed
`CycleState` rather than a transcript.

**Generative Agents** (Park et al., 2023) contributes the part mini-swe-agent
has no analogue for: a memory stream whose retrieval scores recency, importance,
and relevance, plus a periodic *reflection* step that synthesizes higher-level
statements from accumulated observations. Guardian has the store and has no
reflection — §4 and §6 treat that as the central gap, not a nice-to-have.

**Voyager** (Wang et al., 2023) contributes the frontier idea: an automatic
curriculum proposing progressively harder tasks, plus a skill library that grows
so later tasks are cheaper. Guardian's analogue of the curriculum is the
hypothesis set carried across cycles, and its analogue of the skill library is
recalled probe cost and outcome history (§4.3, the trajectory query). Voyager's
curriculum is *the* mechanism by which its carried
state changes behaviour, which is precisely the mechanism our C−B contrast is
supposed to measure.

---

## 2 · The questions a loop design must answer

Ten questions. For each: Guardian's current answer, with evidence, and what the
answer must become. This section is the specification's rationale; §3–§6 are the
specification.

### Q1 · What is the unit of iteration?

*Current.* Ambiguous. `run_cycle` is one commit's worth of work
(`cycle.py`), and `guardian_replay.py:173` iterates commits with a plain
`for i, commit in enumerate(commits)`. So the de-facto unit is "one commit,
processed once."

*Must be.* The unit of L2 iteration is **one investigation decision**, not one
commit. A commit that changes nothing interesting should consume no
investigation; a commit that touches a subsystem with three open suspicions
should consume three. Binding iteration to commits forces a uniform spend that
no evidence justifies.

### Q2 · What state is carried, and by whom?

*Current.* Almost nothing. Within a cycle, `_hypotheses` is a local list.
Across cycles, only what `MemoryStore` persists — and the only read into
decision-making is `recent_findings(k=5)` (`memory/store.py:250-272`), a
newest-first page rendered as a text block for the prompt
(`memory/store.py:399`).

*Must be.* A typed `CycleState` (§3.1) carried explicitly across L2 iterations
and serialized at each boundary, containing at minimum the hypothesis set, the
budget ledger, and the verdict/supersession history. "Carried in the prompt
because it happened to be in the last five findings" is not carried state.

### Q3 · Who decides what happens next — and is that decision evaluable?

*Current.* Nobody, in the loop sense. `hypothesize()`
(`orchestrator/runner.py`) makes **one** LLM call returning a JSON array,
with `heuristic_hypotheses()` as a deterministic fallback on API failure or
malformed JSON. The orchestrator's toolbox is a six-line file whose entire
content is a TODO:

```python
# orchestrator/tools.py
# TODO: orchestrator tool schemas (e.g. hypothesize, rank_signals)
# will be added here in a future RFC phase.
```

Then the loop **discards part of that decision**: `cycle.py:521` filters to
`[h for h in _hypotheses if h.kind == "churn"]` before the investigation loop at
`cycle.py:527`. Drift hypotheses — which the orchestrator ranked and assigned
confidence `_DRIFT_CONFIDENCE = 0.60` — are never investigated. They bypass the
agent entirely and become findings by a deterministic call to `drift_findings`
at `cycle.py:702-703`.

*Must be.* **The agent decides, one model turn at a time**, over the toolbox of
§3.2 — including what to recall, what code to read, which hypothesis to
investigate, at what budget, and when to stop. The decision point is not a
function called `select`; it is every turn, and it is evaluable because every
turn's tool calls and their preceding state are logged (Q9). Code's contribution
is to make decisions *possible* (tools) and *admissible* (write validation), not
to make them. The current filter at `cycle.py:521`, which drops two of three
hypothesis kinds before anything is investigated, is code making the largest
decision in the cycle.

### Q4 · When does the loop stop?

*Current.* When the list ends. Inside a cycle, iteration stops when
`_churn_hypotheses` is exhausted; the replay stops when the commits file is
exhausted. `budget_tokens=100_000` exists on `GuardianConfig` and
`run_investigator` takes `budget_tokens=20_000`, so budget is enforced *within*
L3 but never *across* L2's iterations. Nothing stops L2 early, and nothing lets
it continue when evidence is still cheap.

*Must be.* Two different kinds of stopping, and conflating them is how a loop
loses its agency. **The agent stops** by calling `submit_report` when it judges
further work not worth its cost — this is the normal exit, and "worth it" is a
judgement, not a threshold. **Code stops** only to enforce guarantees the agent
cannot be trusted with: cycle budget spent, wall-clock exceeded, invariant
violated, or a turn that produced neither a tool call nor a report. Both are
typed exits (§3.4). Code must not stop the loop on an expected-value floor: that
substitutes an arithmetic proxy for the judgement under study.

### Q5 · How is the budget allocated across iterations?

*Current.* Uniformly and implicitly. Every investigated hypothesis gets the same
`budget_tokens` and `max_rounds` (`max_investigator_rounds=8`). A
near-certain suspicion and a speculative one get identical spend.

*Must be.* The agent passes `budget_tokens` when it calls `investigate`, within a
cycle ceiling code enforces. This is where a memory advantage should be most
visible: an agent that recalls "probes on this module cost 3× the median and
confirmed nothing" should spend differently, and the budget argument it passes is
a directly measurable behavioural difference rather than a prose claim. Note this
only works if allocation is the agent's to make — a code-side allocator, however
sophisticated, would make the cost difference a property of the allocator.

### Q6 · What does the loop do with a result it has seen before?

*Current.* Nothing. There is **no deduplication, no supersession, no
consolidation, and no forgetting** anywhere in the package. Grepping
`codeminer/guardian/` for `reflect|consolidat|dedup|escalat|forget|decay|supersede`
returns four hits, all docstrings in `memory/store.py` referring to a "reflect
prompt" that is not implemented. So a finding re-derived in ten consecutive
cycles is reported ten times as if new, and the daily report's novelty degrades
monotonically with uptime — the opposite of the persistence claim.

*Must be.* The agent must be **able** to recognise repetition and **required** to
record what it recognised. The capability is `recall(query=…)` by similarity
(§4.3): before writing a hypothesis, the agent can find near-duplicate claims and
their outcomes. The record is `update_hypothesis(supersedes=[…])` plus a grade
transition, which is what turns recognition into a durable consequence.

The resolution vocabulary — *novel*, *recurrence* (counter incremented, not
re-reported), *escalation* (recurrence with history as evidence), *supersession*,
*refutation* — is a vocabulary for the agent to use and for the report to render,
not a code-side state machine keyed on thresholds. "Recurrence crossing a
threshold" was the wrong formulation: whether the fifth occurrence of a claim
deserves escalation depends on what the occurrences say, which is a reading task.
Code's part is to make repetition *visible* (the similarity query) and
supersession *representable* (the field), then let the agent judge. Grading and
resolution remain different axes: grading asks "is this a finding yet?",
resolution asks "have we said this before?" A recurring hypothesis can change
grade between cycles —
`supported` in cycle 3, `finding` in cycle 9 when a remedy finally becomes
apparent — and that transition is itself the most reportable event the loop can
produce, because it is a maintenance opportunity that only persistence could
have surfaced. This is the single highest-value addition in this blueprint, and
it is a **loop** requirement, not a memory-store requirement: the store can hold
the rows once step 0's migration adds the columns.

### Q7 · What is the loop's invariant?

*Current.* One, and it is real: the mirror HEAD never moves, asserted by
`_assert_mirror_unchanged` (`guardian_replay.py:49`), plus the sandbox
blueprint's one-write-boundary rule. Nothing about state consistency.

*Must be.* Two more. **(a)** At every L2 iteration boundary, `CycleState` is
serializable and internally consistent — every hypothesis is in exactly one
of {open, in-progress, resolved}, and the budget ledger equals the sum of
recorded spends. **(b)** Durable memory is written only at cycle exit, by the
host, from a validated `CycleState` — never incrementally mid-loop. This makes a
crash lose at most one cycle and never corrupt history.

### Q8 · How does it recover from interruption?

*Current.* It does not. Grepping the package and the replay driver for
`resume|checkpoint|recover` returns nothing. `guardian_replay.py` catches
per-cycle exceptions and logs them (`:225-229`), then continues to the next
commit — so a failed cycle silently contributes nothing, and a run interrupted
mid-cycle loses that cycle's work with no record of where it stopped.

*Must be.* `CycleState` written at each iteration boundary to
`/out/cycle_state.json`, plus a resume path that reconstructs the hypothesis set and
budget ledger. For a loop intended to run for weeks this is load-bearing, not
polish.

### Q9 · Can the loop's decisions be audited?

*Current.* Partially. L3 emits a `probe_trace` and `InvestigatorResult` carries
`reasoning` and `tokens_used`. But L2's own decisions leave no trace: there is
no record of which hypotheses were considered and rejected, why a budget was
set, or why the cycle ended.

*Must be.* A `decision_log` in `CycleState` — **one record per tool call**,
carrying the call, its arguments, the state digest the agent saw, and the
observation returned; plus one record per compaction event (§4.4) and one per
exit. This is a stronger instrument than a per-step log precisely because the
agent is free: what it chose to recall, what code it read, which hypothesis it
picked over which, and what budget it granted are all decisions, and all
recorded. Without it the memory arms can only be compared by outcome, and an
outcome difference with no mechanism is a weak result — which is also the answer
to the variance objection in §3.3.

### Q10 · Where does the human enter?

*Current.* At the report, and only there. Correct per the advisory property, and
worth preserving explicitly.

*Must be.* Unchanged in authority — the loop never writes to the repository —
but the report should expose the loop's own state: what is open, what escalated,
what was dropped and why. A backlog the human can read is a better artifact than
a list of findings, and it costs nothing extra once Q2 and Q6 are done.

---

## 3 · The loop specification

This is the part that gets implemented. Four sub-sections, in dependency order:
**3.1** what the loop carries, **3.2** what it can do, **3.3** why the decisions
are the agent's, **3.4** how it ends, **3.5** the body that ties them together.

### 3.0 The loop at a glance

Before the detail, the shape. One **cycle** is one commit's worth of work. One
**iteration** inside a cycle is one model turn. Code owns the boundary; the agent
owns everything inside it.

![One cycle: code owns the boundary, the agent owns everything inside it]({{artifact:art_efc6d285-666a-45d2-8dae-33d804b6313b}})

The same shape in plain text, for reading in a terminal:

```
  ┌─ host (deterministic) ──────────────────────────────────────────┐
  │  sync repo → refresh views → compute signals → build frame      │
  └────────────────────────────┬────────────────────────────────────┘
                               │ enters the loop once
  ┌─ agent loop (§3.5) ────────▼────────────────────────────────────┐
  │  check invariants and limits                                    │
  │  one model turn  ──►  tool calls  ──►  observations appended     │
  │       ▲                                        │                │
  │       └────────────────────────────────────────┘                │
  │  exits only via §3.4: the agent submits, or a limit fires       │
  └────────────────────────────┬────────────────────────────────────┘
                               │ on exit
  ┌─ host (deterministic) ─────▼────────────────────────────────────┐
  │  render report (filtered by grade) → persist hypotheses+signals │
  └─────────────────────────────────────────────────────────────────┘
```

**What is fixed and what is free.** Five things are hard-coded, and each is an
invariant rather than a decision: limit and invariant checks, write validation
(`GRADE_RULES`), per-iteration checkpointing, signal *computation*, and report
*rendering*. Everything else — what to look at, what to remember, what to
investigate, when the cycle is done — is a tool call the agent chooses to make.

**An illustrative trace.** The following is one sequence a cycle might produce.
It is *not* a specification, and nothing in the code enforces this order; it is
here so the mechanism is legible before §3.5 states it formally.

| Turn | Tool call | Why it might happen |
|---|---|---|
| 1 | `list_signals` | orient: what changed since last cycle |
| 2 | `recall(by="locus", …)` | has this region been visited before, and how did it end |
| 3 | `read_code` | the recalled verdict named a function; read it |
| 4 | `write_hypothesis` | claim + consequence + remedy, `origin="memory"` |
| 5 | `investigate` | spawn L3; get a probe verdict back |
| 6 | `update_hypothesis` | `grade="supported"` — claim held, remedy still vague |
| 7 | `read_code`, `write_hypothesis` | the probe output suggested a second, unrelated defect |
| 8 | `investigate`, `update_hypothesis` | `grade="finding"` |
| 9 | `submit_report` | the agent judges the remaining budget not worth spending |

Three things in that trace are impossible today, and each is a defect in §5.1:
memory read at turn 2 (defect 4), a hypothesis formed *after* an investigation at
turn 7 (defect 1), and the agent — not a `for` loop — ending the cycle at turn 9
(defect 1 again). Turn 6 landing in the backlog rather than the report is the
grade ladder of §0.2 doing its job.

### 3.1 Carried state

One dataclass, serialized at every iteration boundary. This is the answer to Q2
and the object the invariants in Q7 are stated over.

```python
@dataclass
class Hypothesis:
    """A falsifiable claim about a solvable engineering problem.

    `grade` is the single lifecycle field. A *finding* is a hypothesis at
    grade == "finding"; findings ⊆ hypotheses.
    """
    id: str                     # stable hash of (claim, locus)

    # --- the three parts that make it a hypothesis (§0) ---
    claim: str                  # falsifiable statement about behaviour
    consequence: str            # what breaks / degrades if the claim holds
    remedy: str                 # the engineering change that would resolve it

    # --- lifecycle: one field, five values (§0.2) ---
    grade: str                  # conjecture | supported | finding
                                #   | refuted | deferred

    # --- provenance and subject ---
    origin: str                 # signal | memory | exploration | human
    locus: list[str]            # files/symbols/edges the claim concerns
    evidence: list[str]         # prefixed refs: "signal:…", "probe:…", "cycle:…"

    # --- accumulated across cycles ---
    confidence: float
    attempts: int
    spent_tokens: int
    first_seen_cycle: int
    last_touched_cycle: int
    supersedes: list[str]

@dataclass
class CycleState:
    cycle_no: int
    commit: str
    hypotheses: list[Hypothesis]   # findings are the grade == "finding" subset
    signals: list[Signal]          # measurements, retained as evidence
    current: str | None            # hypothesis id under investigation, if any
    budget_total: int
    budget_spent: int
    decision_log: list[dict]
    exit_reason: str | None
    carried_from: int | None       # previous cycle_no, None for a cold start
```

**Thirteen fields, and no field another field determines.** `grade` carries the
whole epistemic state, so there are no verification booleans beside it; scheduling
state lives in `CycleState.current`, not replicated on every record; `confidence`
is one number updated in place, with its history in the decision log;
`spent_tokens` is the lifetime total, since a per-attempt grant belongs to the
decision that made it; and signal references are `evidence` entries with a
`"signal:"` prefix that `signals_of(h)` filters back out. The field-by-field
mapping from the current schema is in `design/terminology_migration.md` §2. One
pair is redundant by that test and kept anyway: `first_seen_cycle` /
`last_touched_cycle` — age and staleness are different inputs to selection, and a
hypothesis can be old but fresh.

**No `kind`, no `target`.** `kind ∈ {churn, drift, test_failure}`
(`orchestrator/runner.py:47`) is the *signal taxonomy*, so typing a hypothesis by it makes every hypothesis a signal
wearing a different name, and forecloses `origin ∈ {memory, exploration}` by
construction. `target: str` assumed one locus, but the worked example spans two
functions and its claim is precisely about the relationship between them.

**Grade is written by the agent and validated by code, never derived by code.**
Deriving it from booleans an investigation set would make the loop's epistemic
judgement a hard-coded expression. The agent records a grade through a
state-write tool, and the shell enforces admissibility rather than deciding:

```python
GRADE_RULES = {
    # grade         required, or the write is rejected
    "finding":   lambda h: h.remedy and any(e.startswith("probe:") for e in h.evidence),
    "supported": lambda h: any(e.startswith("probe:") for e in h.evidence),
    "refuted":   lambda h: any(e.startswith("probe:") for e in h.evidence),
    "conjecture": lambda h: bool(h.claim and h.consequence and h.remedy),
    "deferred":  lambda h: True,
}
```

So `finding` is unreachable without a remedy and without at least one probe —
the mechanical floor of risk 6 — but *which* grade the evidence supports is the
agent's call, recorded with its reasoning in the decision log. Code owns the
invariant; the agent owns the judgement.

**`evidence` with no `"signal:"` ref is the interesting case.** A hypothesis
whose evidence cites no signal, with `origin ∈ {memory, exploration}`, is
exactly what a task-driven agent would not produce. The metric in §7 counts
those.

### 3.2 Tools, not steps

A fixed sequence of steps — `perceive`, `recall`, `hypothesize`, `select`,
`investigate`, `judge`, `report` — is a workflow specification wearing a loop's
vocabulary, and it fails the test in §1.1: if code fixes the order, the agent has
no policy, and the thing memory would have to change is a code path rather than a
decision. Two symptoms make that concrete, and both name a step that looks
harmless:

- **`recall` as a step forces the retrieval decision into code.** A step that
  runs once, before anything is known, must guess what will be relevant. It
  either over-fetches (context bloat, and the ablation is diluted by irrelevant
  history) or under-fetches (the agent is blind to what it would have asked
  for). Either way *code* chose what to remember, and the memory contrast
  measures a fetch policy rather than an agent's use of memory.
- **`hypothesize` as a step forces batch formation.** A cycle would form all
  hypotheses before investigating any, so nothing learned in investigation 1 can
  produce hypothesis 4 — which is precisely how a real maintainer works, and
  precisely the behaviour the persistence claim predicts.

So there are no steps. There is one agent loop and a set of tools, and the
sequence is the agent's output, not the program's structure.

| Tool | Signature | Notes |
|---|---|---|
| `list_signals` | `(kind=None, since=None) → list[Signal]` | measurements are computed eagerly (cheap, deterministic) but *reading* them is the agent's choice |
| `search_code` / `read_code` | `(query \| path, …) → text` | the repo views from the sandbox blueprint; this is how exploration-origin hypotheses become possible |
| `recall` | `(query, kind=None, locus=None, k=…) → list[record]` | **a tool, not a step** — memory at any point, any number of times, scoped by what the agent has learned so far |
| `write_hypothesis` | `(claim, consequence, remedy, origin, locus, evidence) → id` | rejected unless all three §0 parts are non-empty |
| `update_hypothesis` | `(id, grade=…, evidence=…, confidence=…, supersedes=…)` | grade transitions validated against `GRADE_RULES` (§3.1) |
| `investigate` | `(hypothesis_id, budget_tokens) → InvestigatorResult` | delegates to the L3 agent loop; the agent chooses the budget |
| `submit_report` | `(summary) → terminal` | the agent declaring the cycle done — one of the exits in §3.4 |

**What stays hard-coded, and why each is an invariant rather than a decision.**
This list is deliberately short; anything on it that could be a decision should
become a tool instead.

1. **Limit and invariant checks** at the top of each iteration (§3.4). Not a
   decision — a guarantee. The agent cannot be trusted to enforce its own budget,
   and mini-swe-agent puts these in exactly one place for the same reason
   (`agents/default.py`, limits raised from `query()`).
2. **Write validation** on `write_hypothesis` / `update_hypothesis`. Code decides
   whether a record is *admissible*, never whether it is *true*. This is what
   keeps signals out of the findings section without code deciding what a finding
   is about.
3. **Checkpointing** after every iteration (§3.4, Q8).
4. **Signal computation.** Churn, graph-diff, and test deltas are deterministic
   measurements; computing them eagerly costs a few seconds and no tokens.
   Distinguish *deterministic computation*, which may be eager, from
   *deterministic decision*, which may not: the agent still chooses whether and
   when to look.
5. **Report rendering** from whatever `submit_report` was given, filtered by
   `grade`. The filter is the §0 definition, not an editorial judgement: only
   `grade == "finding"` reaches the findings section, `supported`/`deferred` go
   to a backlog section, and `refuted` appears only to retract something
   previously reported.

Two consequences worth stating, because both are currently violated in code.
`perceive` no longer exists as a producer of anything but `Signal`s, which rules
out the `drift_findings` shortcut at `cycle.py:702` that converts drift signals
straight into `Finding` objects. And nothing enters the report because a loop
reached it: today the `hypotheses_only` path emits
`Finding(title=f"[hypothesis] {h.target}")` at `cycle.py:464-469` for hypotheses
never investigated at all.

### 3.3 Agency vs. interpretability — and why agency wins

There is a real argument for making selection **one LLM call over a structured
state summary**: a tool-using selector puts a variable number of tool calls and a
variable context between memory and the outcome, so a C−B difference could come
from memory content or from selector variance, and separating the two costs seeds
this budget does not have. The argument is sound and its conclusion is still
wrong — this is a trap worth naming, because the project will keep walking into
it. Making the selector a single call buys interpretability by **removing the
behaviour under study**. The research question is whether persistent memory lets an agent
*proactively* find maintenance opportunities. Proactivity is the agent choosing
what to look at when nothing asked it to — so a design in which code decides what
to read, when to read it, and in what order to act is one where the interesting
variable has been fixed at zero. A clean measurement of the wrong quantity.

The genuine concern — that agent variance swamps the memory effect — is a
*measurement* problem and gets a measurement answer:

- **The decision log is the instrument, not the architecture.** Every tool call
  is recorded with its arguments and the state that preceded it (Q9). A memory
  effect is then visible as a difference in *which tools were called on what*,
  not merely in the final report. That is a stronger result than a ranking delta,
  because it shows the mechanism.
- **Trace-level metrics beat outcome-level ones under variance.** Counts of
  `recall` calls, the fraction of hypotheses with no `"signal:"` evidence, the
  distribution of investigation budgets. These have far more events per run than
  "number of findings", so they need fewer seeds for the same power.
- **Fix the tool surface across arms, and only the memory tool differs.** Arms B
  and C get identical toolboxes; in arm B, `recall` returns empty. Variance from
  the agent's policy is then common to both arms, which is what the paired
  comparison already handles.

So: the outer level **is** an agent loop, with the toolbox of §3.2, and this
matches `prototype_design.md` §3.1's two-nested-loops claim rather than diverging
from it. The `orchestrator/tools.py` TODO is the work, not a placeholder to
defer.

**The one place a scored fallback survives** is API failure. If the model call
fails, a deterministic ranking may pick a hypothesis so the cycle produces
something — but the cycle must be **flagged as degraded** in its report and
**excluded from the C−B comparison**, because on that path arms B and C are
identical. Today this contamination is silent: `heuristic_hypotheses()` runs on
any API failure or malformed JSON and reads no memory at all, so a run's
effective arm-C sample is unknown.

### 3.4 Termination, as typed exits

Following mini-swe-agent's pattern (`exceptions.py:1-26`), all limits are
checked in one place — the top of the loop body — and every exit is typed and
carries an explanation into the report:

```
CycleInterrupt                 # root; carries a reason string + decision_log tail
├── ReportSubmitted            # the AGENT ended the cycle — the normal exit
├── NoProgress                 # a turn with no tool calls and no report
├── BudgetExceeded             # token ledger spent
│   └── WallClockExceeded
├── Degraded                   # model unavailable; scored fallback ran (§3.3)
└── StateInconsistent          # invariant (a) violated — abort, do not persist
```

The ordering carries a claim: **the normal exit is the agent deciding it is
done**, not code deciding for it. Everything below `ReportSubmitted` is a
guarantee being enforced or a failure being reported. Two plausible exits are
deliberately absent: `HypothesesExhausted` and `ValueFloorReached` would both be
code making the judgement "there is nothing more worth doing", which is the
agent's to make. Exhaustion is not even well-defined once `recall` and code
exploration can produce new hypotheses mid-cycle — the supply is not a list to
run out of.

`StateInconsistent` is the one that must never be swallowed. Every other exit
persists memory and writes a report; that one writes a report and refuses to
persist, because a corrupt hypothesis set propagated into the next cycle is
worse than a lost cycle. `Degraded` persists but marks the cycle excluded from
the arm comparison (§3.3).

Note the contrast with current behaviour: `guardian_replay.py:225-229` catches
bare `Exception` per cycle and logs it, so today a mid-cycle failure is
indistinguishable from a cycle that legitimately found nothing.

### 3.5 The loop body

One iteration is **one model turn**: the agent sees the conversation so far,
emits tool calls, gets observations. There is no `select`-then-`investigate`
sequence in the code, because the sequence is what the agent is for.

```python
def run_cycle_loop(state: CycleState, ctx) -> CycleState:
    messages = [system_prompt(ctx), opening_context(state, ctx)]   # §4
    while True:
        try:
            check_invariants(state)      # → StateInconsistent
            check_limits(state)          # → BudgetExceeded / WallClockExceeded
                                         #   (also raised from inside query())

            response = query(messages, TOOLS, state)   # → Degraded on API failure
            messages.append(response)

            if not response.tool_calls:                # nothing asked for and no
                raise NoProgress(response)             # report → the agent is done
                                                       # or stuck; both terminal

            for call in response.tool_calls:           # → ReportSubmitted
                obs = dispatch(call, state, ctx)       # never raises; typed
                messages.append(obs)                   # observations only

        except CycleInterrupt as e:
            state.exit_reason = type(e).__name__
            state.decision_log.append(e.as_record())
            return state
        finally:
            checkpoint(state, messages)   # /out/cycle_state.json — every iteration
```

Four properties worth naming, three of them borrowed from mini-swe-agent
(`agents/default.py`, read at v2.4.6):

**`dispatch` never raises.** Every tool failure — a probe timeout, a malformed
argument, a rejected grade transition — comes back as a typed *observation* the
agent can react to. This is the contract from the sandbox blueprint §5, and
`WorktreeSandbox.run_command` already honours it by returning `(1, "(command
timed out after {timeout}s)")` instead of raising (`investigator/sandbox.py`). A
rejected `update_hypothesis` is the interesting case: the observation says *why*
(`"grade='finding' requires a probe-valid: or source-valid: evidence
reference"`), so the validation rule teaches rather than merely blocks.

**Limits are checked in one place**, including from inside `query()` so token
accounting cannot be bypassed by a tool that happens to be expensive. Scattering
budget checks through tool implementations is how a loop acquires four different
notions of "done".

**Checkpointing is in `finally`**, so it runs on the exit path too — the same
reason mini-swe-agent calls `save()` in a `finally` every step. A cycle killed by
the sandbox's `pids` cap must still leave a resumable state file.

---

## 4 · Context engineering

Enumerating "memory channels" — blobs that code fetches and pastes into prompts
— specifies context in code, which is the same category error as specifying steps
in code. What matters is not which items get pasted where but **how the agent's
context is constructed, grown, and kept from decaying** across a long cycle.
Memory is one of three sources feeding that construction, not a section
heading.

### 4.1 The problem, stated properly

A cycle may run dozens of model turns. Its context must satisfy four constraints
that pull against each other:

1. **Bounded.** A long cycle cannot accumulate every observation; the repo alone
   exceeds any window.
2. **Sufficient.** The agent must be able to reach anything relevant, so the
   bound cannot be enforced by pre-filtering what it is allowed to see.
3. **Stable under growth.** The tenth turn's decision quality should not be
   worse than the second's because early reasoning has been pushed out.
4. **Attributable.** For the C−B contrast, we must know what memory-derived
   content was in context when each decision was made.

The resolution — the one architectural commitment of this section — is that
context is **pull, not push**: the shell provides a small fixed frame plus a
retrieval surface, and everything else enters because the agent asked for it.

### 4.2 The three layers

| Layer | Size | Who decides content | Lifetime |
|---|---|---|---|
| **Frame** | ~1–2k tokens, fixed | code (it is the invariant part) | whole cycle, never evicted |
| **Working set** | grows | the agent, via tool calls | current cycle, compactable |
| **Retrievable** | unbounded | agent on demand | persistent; only summaries enter context |

**The frame** is what the agent needs to act at all: its task, its toolbox, its
budget ledger, the current commit, and a *digest* of state — hypothesis counts by
grade, the id and one-line claim of anything under investigation, cycle number and
how many cycles this repo has seen. It is a digest, not content: no claim bodies,
no evidence, no signal details. It answers "where is the loop and what can it do", and
everything it mentions is fetchable by id.

**The working set** is the turn-by-turn conversation: tool calls and
observations. This is where signal details, code, recalled records, and probe
results live — each present *because the agent asked*, which is what makes the
attribution in constraint 4 possible.

**The retrievable layer** is the memory store, the repo views, and the signal
table, reachable through `recall`, `search_code`/`read_code`, and
`list_signals`. Unbounded in size, zero cost in context until touched.

This is where the design's "**compression, not retrieval**" claim
(`prototype_design.md` §3.2) needs a correction. Compression is right for the
frame: a digest, not a dump. But it cannot be the whole model, because a fixed
compressed blob is code choosing what matters before the agent knows what it is
looking at. Frame = compression; working set = retrieval. Both, at different
layers.

### 4.3 What memory contributes — three queries, not six channels

The six channels collapse on inspection. Channel 3 (verdict history) was channel
1 (recency) sliced by locus instead of by date — the same rows, a different
`WHERE`. Channel 2 (hypothesis backlog) had no purpose distinct from the
hypothesis table itself, once `grade` is the lifecycle field. Channels 4
(calibration) and 5 (escalation counters) were derived statistics over those same
rows, precomputed on the assumption that code would decide when they mattered —
which is the push model in miniature.

One tool, three query shapes, one table:

| Query | Answers | Example |
|---|---|---|
| **by locus** | "what do we know about *this*?" | `recall(locus="config.py")` → prior claims, grades, probe outcomes, costs |
| **by similarity** | "have we thought this before?" | `recall(query="empty input inconsistency")` → near-duplicate claims → dedup, supersession, escalation |
| **by trajectory** | "what has been happening?" | `recall(kind="trend", since=…)` → grade transitions, recurrence counts, confirmation rate vs. confidence |

Escalation and calibration are not channels; they are what the **trajectory**
query returns, computed at query time. The recency block survives only as a
default: `recall()` with no arguments returns recent activity, which is the
sensible cold-start behaviour.

Every record carries `hypothesis_id`, `cycle_no`, and `grade`, so a memory-derived
claim in a report can be traced to the rows that produced it. That is the
attribution constraint, satisfied by the record format rather than by bookkeeping.

### 4.4 Compaction: preserve a coherent working memory

When the working set approaches the window, something must go, and *how* that
choice is made is the least-settled question in this blueprint.

- **Eviction by recency** (drop oldest observations) is what most agents do. It
  is wrong here: the earliest turns often contain the hypothesis-forming
  reasoning, and dropping it silently converts a persistent agent into a
  short-horizon one mid-cycle.
- **Externalize-and-reference** — write the full observation to `/out`, keep a
  one-line reference the agent can re-read — appears lossless, but makes the
  agent pay another turn to reconstruct its own working memory. In the July
  2026 replay this produced a context-thrashing loop: the same externalized
  observation was recovered nine times without a state transition.
- **Summarize-and-replace** compresses the completed conversation into a
  structured working-memory synopsis. It is lossy, but preserves the reasoning
  thread in one place and avoids observation-by-observation reconstruction.

**Decision:** keep raw tool results in the append-only conversation until the
model's token boundary is genuinely approached, then summarize and replace the
old conversation as one unit. The summary request appends to the existing
conversation so the provider can reuse its cached prefix. The immutable frame
and canonical `CycleState` are re-injected; the full pre-compaction transcript
is archived for audit, and the compaction event records the archive and summary
sizes. Cached-input tokens and compaction counts are reported so the cost and
ablation interaction remain measurable.

Two mechanical requirements follow. Compaction must be **arm-blind** — the same
policy, thresholds, and code path in B and C — or it becomes a second
manipulation riding on the first. And the frame must **never** be compacted; if
the frame plus one turn exceeds the window, that is a `BudgetExceeded` exit, not
a compaction problem.

### 4.5 What the current implementation does

One call, in one place: `recent_findings(k=5)` (`memory/store.py:250-272`),
pasted into the hypothesize prompt. Three properties make it a weak instrument:

1. **Fixed-size newest-first page**, so content is a function of cycle index
   rather than relevance — confounding "memory helps" with "cycle position".
2. **One read site**, so its effect is bounded by how much one prompt's output
   changes the cycle. Since `cycle.py:521` discards all non-churn hypotheses,
   even that influence is partly thrown away.
3. **Bypassed on the fallback path.** When the model call fails or returns
   malformed JSON, `heuristic_hypotheses()` runs and reads no memory at all. On
   that path arm C *is* arm B, silently (§3.3).

The ablation mechanism itself is correct, and this was verified against source
rather than assumed:
`readonly=True` — set from `config.arm == "memoryless"` at `cycle.py:369` — gates
**both** directions. `persist_cycle` returns `-1` as a no-op
(`memory/store.py:151`) and every read method short-circuits to `[]`/`None`/`{}`
(`memory/store.py:256, 279, 301, 316, 332, 344`). Arm B is a true memoryless arm.
The problem is not the ablation; it is that what is ablated barely reaches any
decision.

Under §4.3 the ablation acquires a cleaner meaning: in arm B, `recall` is present
in the toolbox and returns empty. The agent can still *try* to remember, which
holds the tool surface identical across arms and confines the manipulation to the
data (§3.3). It also yields a free behavioural check — if arm B keeps calling
`recall` and getting nothing, its wasted calls are measurable.

![Push versus pull: one code-chosen block today, three layers with agent-driven retrieval proposed]({{artifact:art_5a81f3d0-eb21-42ad-ab02-cd678bb25d9e}})

---

## 5 · Defects in the current loop

### 5.0 Defect 0 — the ontology is collapsed in code, not just in prose

This is the defect the terminology fix exposes, and it outranks everything below
because the others are missing machinery whereas this one is machinery that
actively produces wrong output. Four independent pieces of evidence:

**`Finding` is defined as a signal.** Its docstring reads *"One signal plus the
evidence Guardian gathered around it"* (`report.py:37`), and its fields are
`kind, title, detail, evidence, narrative, hypothesis, verdict, ...`
(`report.py:47-56`). There is no `remedy` field and no `consequence` field. A
type that cannot represent a remedy cannot represent a finding as defined in §0,
so the class currently named `Finding` is a decorated signal.

**Signals become findings with no verification path.** `drift_findings`
(`signals/graph_diff.py:358-370`) constructs `Finding(kind="drift",
title=f"Graph-diff drift: {sig.kind} — ...", detail=sig.detail)` in a loop over
raw signals. No claim, no remedy, no verdict, no investigation. These go into the
report as findings (`cycle.py:702-703`). By §0's definition none of them is a
finding, and most cannot become one — "symbol X was removed" has no engineering
content on its own.

**Titles are loci, not claims.** Every churn finding is titled `f"High-churn
file: {hotspot.path}"` (`cycle.py:559, 588, 609`). That is the user's `A.txt has
been modified three times` example verbatim, in production, as the headline of a
report. The `hypothesis` field carries the actual claim, but the title — the part
a human reads first — is a measurement.

**Hypotheses are structurally forbidden from being non-signal-derived.** The
prompt states: `Only reference targets that appear in SIGNALS; do not invent new
paths` (`orchestrator/runner.py:79`), and `Hypothesis.kind` is typed as the
signal taxonomy (`orchestrator/runner.py:47`). So `origin ∈ {memory,
exploration}` is unreachable, and with it the proactivity claim.

The database schema encodes the same collapse: `findings(id, cycle_no, kind,
target, title, detail, narrative, confidence)` (`memory/store.py:50-59`) has no
`remedy`, no `consequence`, no way to record that a claim was verified, and a
`kind` column drawn from the signal taxonomy. So the memory the loop accumulates cannot represent findings
either, which means the recall queries of §4.3 would return signal records no
matter how well the context is engineered.

*Consequence for the research question.* Reported "findings" are currently
signal reports with an attached sentence. If the C−B contrast is measured over
these, it measures whether memory changes which *files get flagged* — a much
weaker claim than the one the project makes, and one a linter with a ranking
layer could satisfy. **The terminology fix is therefore a prerequisite for the
experiment, not a documentation cleanup.**

### 5.1 The rest

Ordered by whether they block a credible memory result.

| # | Defect | Evidence | Blocks C−B |
|---|---|---|---|
| 0 | Signal/hypothesis/finding collapsed: `Finding` is "one signal plus evidence"; no `remedy` field anywhere; signals promoted directly to findings | `report.py:37,47-56`; `signals/graph_diff.py:358-370`; `cycle.py:559`; `memory/store.py:50-59`; §5.0 | **yes — prerequisite** |
| 1 | The outer level is not an agent loop: one LLM call, no toolbox, no re-perception, so the agent makes no decisions after its first output | `orchestrator/runner.py`; `orchestrator/tools.py` is a 6-line TODO | **yes** |
| 2 | Drift and test_failure hypotheses are ranked then discarded before investigation | `cycle.py:521, 527`; drift bypasses to `drift_findings` at `cycle.py:702-703` | **yes** |
| 3 | No judge step — no dedup, supersession, escalation, or forgetting | grep for `reflect\|consolidat\|dedup\|escalat\|forget\|decay\|supersede` → 4 docstring hits only | **yes** |
| 4 | Memory reaches the loop through exactly one code-chosen fetch, at one point, and the agent cannot ask for anything else | `memory/store.py:250-272` (`recent_findings(k=5)`), read once into the hypothesize prompt; §4.5 | **yes** |
| 5 | Uniform per-hypothesis budget; no allocation decision | `budget_tokens=20_000`, `max_investigator_rounds=8` applied identically | yes |
| 6 | No carried state across L2 iterations or cycles beyond the store | §2 Q2 | yes |
| 7 | No resume/checkpoint; failed cycles logged and skipped | grep `resume\|checkpoint\|recover` → nothing; `guardian_replay.py:225-229` | no, but corrupts long runs |
| 8 | No L2 decision trace | §2 Q9 | no, weakens the mechanism story |
| 9 | `corroboration_policy` is defined but never called — the corroboration rule exists only as prompt text | defined `probes.py:430`; no call site outside `probes.py`; rule stated in prose at `investigator/runner.py:597, 602` | no, but weakens every verdict |
| 10 | `differential_run` is unreachable — no tool schema, no dispatch branch | defined `probes.py:377`; absent from `TOOLS` (`investigator/runner.py:454`) and from `dispatch_advanced_probe` (`probes.py:480`) | no |

Defects 9 and 10 are inherited from L3 rather than introduced by L2, but they
bear on this blueprint because grading (§3.1) consumes probe verdicts.
A verdict whose corroboration rule is advisory prompt text rather than enforced
code is a weak input to a state machine that will act on it, and one of the two
corroboration routes the design specifies cannot currently be taken at all.

---

## 6 · Implementation plan

Eight steps. Step 0 comes first because every later step manipulates objects
whose type is currently wrong.

| # | Step | Files touched | Done when | Unblocks |
|---|---|---|---|---|
| 0 | Split the ontology | `signals/*`, `orchestrator/runner.py`, `report.py`, `memory/store.py`, prompt | a signal alone cannot reach the report; `remedy=""` cannot reach `grade == "finding"` | everything |
| 1 | `CycleState` + checkpointing | new `guardian/loop/state.py` | state round-trips through `/out/cycle_state.json`; ledger sums correctly | 3 |
| 2 | Typed exits | new `guardian/loop/exceptions.py`, `guardian_replay.py` | a failed cycle is distinguishable from an empty one | 3 |
| 3 | The tool-use loop | `cycle.py` (`_run_cycle_inner`) | a cycle reaches `ReportSubmitted` through ≥3 turns; decision log replays | 4, 5, 6 |
| 4 | `recall` as a tool | new `guardian/memory/queries.py` | `--arm memoryless` returns empty and the loop still reports | the research question |
| 5 | Context management | `guardian/loop/context.py`, `guardian/llm/` | each L2/L3 agent loop owns one transport session; raw results remain until a token boundary; compaction retains frame and canonical state in one summary | long cycles |
| 6 | Code exploration tools | new `guardian/loop/tools_code.py` | ≥1 hypothesis per replay carries no `"signal:"` evidence | the proactivity metric |
| 7 | Grading and resolution discipline | `report.py`, prompt, `GRADE_RULES` | a re-derived claim supersedes rather than duplicates | credible C−B |

Steps 1 and 2 are independent of each other and can be done in either order.
Steps 5 and 6 are independent of each other. Everything else is sequential. The
detail for each step follows.

0. **Split the ontology.** Three types where there is now one-and-a-half.
   - New `Signal` dataclass in `guardian/signals/types.py`; `churn.py`,
     `graph_diff.py`, `tests.py` return `Signal`s and nothing else. Delete
     `drift_findings` — drift signals feed `hypothesize` like every other signal.
   - Rename `orchestrator.Hypothesis` → keep the name, change the fields per
     §3.1: drop `kind`, `target`, `rank`, `status`; add `claim`, `consequence`,
     `remedy`, `origin`, `locus`, `grade`, `evidence`. 13 fields, one lifecycle
     field.
   - `report.Finding` becomes a **view**, not a stored type:
     `[h for h in state.hypotheses if h.grade == "finding"]`. If a concrete class
     is kept for rendering, it must carry `remedy` and it must be constructed
     only from a graded hypothesis.
   - Schema migration: `findings` table → `hypotheses` table with `claim`,
     `consequence`, `remedy`, `origin`, `grade`, `locus_json`, `evidence_json`,
     `confidence`, `attempts`, `spent_tokens`, `first_seen_cycle`,
     `last_touched_cycle`. Keep `cycle_no`, drop `kind`. Add a `signals` table so
     signals persist as evidence without being confusable with hypotheses.
   - Rewrite the prompt: **delete** `Only reference targets that appear in
     SIGNALS; do not invent new paths`, replace with a requirement to emit
     `claim` / `consequence` / `remedy` and to set `origin`. Add an explicit
     instruction that a restatement of a signal is not a hypothesis, with the
     `A.txt` example as a negative case.
   - Add `GRADE_RULES` (§3.1) as write validation, so `finding` is unreachable
     without a remedy and a probe ref.
   - Test: a signal alone can never reach the report; a hypothesis with
     `remedy=""` can never reach `grade == "finding"`.
1. **`CycleState` + checkpointing.** Define the dataclasses in
   `guardian/loop/state.py`; write/read `/out/cycle_state.json` at every
   iteration boundary; add `check_invariants`. No behavioural change yet —
   `_run_cycle_inner` populates the state and ignores it. Test: a cycle's state
   round-trips and its ledger sums correctly.
2. **Typed exits.** `guardian/loop/exceptions.py` per §3.4. Replace the bare
   `except Exception` at `guardian_replay.py:225` with typed handling, so a
   failed cycle is distinguishable from an empty one.
3. **The tool-use loop.** Replace `_run_cycle_inner`'s fixed sequence with the
   `while True` of §3.5: a model turn, tool dispatch, checkpoint. Start with a
   minimal toolbox — `list_signals`, `write_hypothesis`, `investigate`,
   `submit_report` — and no memory tool at all, so the loop's mechanics can be
   debugged before memory is a variable. The `kind == "churn"` filter at
   `cycle.py:521` disappears here along with `kind` itself: nothing filters the
   agent's candidates but the agent. Test: a cycle reaches `ReportSubmitted`
   through at least three turns, and its decision log replays.
4. **`recall` as a tool.** The three query shapes of §4.3 over the migrated
   tables. This is the single change that makes the research question
   answerable, and it lands as one tool rather than five channel-wirings. Test:
   with `--arm memoryless` the tool is present and returns empty, and the loop
   still reaches a report.
5. **Context management.** The frame/working-set split of §4.2 and
   summary-and-replace compaction (§4.4), with `compaction_events` and cached
   input tokens logged. L2 and each L3 investigation own separate transport
   sessions. Test: a cycle forced past the token boundary keeps its frame and
   canonical state, archives the old transcript, and continues from one
   structured summary.
6. **Code exploration tools.** `search_code` / `read_code` over the sandbox
   views. This is what makes `origin = "exploration"` reachable — until it lands,
   the proactivity metric of §7 has a structural ceiling. Test: at least one
   hypothesis per replay carries no `"signal:"` evidence.
7. **Grading and resolution discipline.** `GRADE_RULES` enforced on writes, the
   resolution vocabulary of Q6 in the prompt and the report renderer, and the
   three report sections (findings / backlog / retractions). Test: a synthetic
   two-cycle replay where cycle 2 re-derives cycle 1's claim shows a supersession
   rather than a duplicate report; a verified claim with an empty remedy lands in
   the backlog.

Step 0 is a breaking change to the schema and to `report.py`, and it invalidates
any memory accumulated under the old schema — existing `index.sqlite` files
should be discarded rather than migrated, since their rows have no `remedy` and
therefore no way to be graded. Doing it first is much cheaper than doing it after
later steps have built on the wrong types.

The ordering has one property worth defending: **step 3 gives the agent its
agency before step 4 gives it memory.** A memory tool added to a workflow just
makes the workflow's fetches configurable; a memory tool added to an agent that
already chooses its own actions is the experimental manipulation. Steps 5 and 6
then remove the two ceilings on that manipulation — context decay over a long
cycle, and the inability to look anywhere signals did not point.

---

## 7 · What this changes about the evaluation

Two consequences worth stating before running anything.

**What gets counted changes, and the count gets smaller.** Under §0, a finding
requires a verified claim *and* an actionable remedy. Today's reported
"findings" are signal reports, so the corrected definition will cut the reported
count substantially — the drift findings vanish entirely, and churn findings
survive only where an investigation produced a remedy. This is a real result
about the current system, not a regression: **the headline count should be
reported under both definitions in any writeup**, because a reviewer who sees
only the new number cannot tell whether the system got worse or the measurement
got honest.

**The proactivity claim becomes directly measurable.** With `origin` on every
hypothesis, the count of findings whose `origin ∈ {memory, exploration}` and
whose `evidence` cites no signal is exactly the quantity the research
question is about: opportunities surfaced that no deterministic detector
flagged. This is a better headline for C−B than total finding count, because a
task-driven agent's floor on it is zero by construction. Today the metric would
read zero for both arms, since the prompt forbids non-signal targets
(`orchestrator/runner.py:79`).

**The C−B contrast gets a mechanism, not just an outcome.** With a per-tool-call
`decision_log` (Q9), a memory-arm advantage can be attributed: *this* `recall`
returned *this* history, and the next turn investigated *that*. Without it, C−B is
a black-box difference in finding counts, which a reviewer can attribute to
prompt-length variance as easily as to memory. The trace-level metrics of §3.3 —
`recall` call counts, no-signal hypothesis fraction, budget dispersion,
`compaction_events` — carry far more events per run than finding counts, so they
need fewer seeds for the same statistical power.

**Cycle position becomes a covariate that must be handled.** Because the
hypothesis set accumulates, arm C's cycle 20 differs from its cycle 1 in ways arm B's
does not. Reporting a single aggregate over cycles will confound memory with
uptime. The analysis should report the per-cycle trajectory, and the natural
statement of the hypothesis is about the *slope* — memory's advantage should
grow with uptime — rather than about a pooled mean. This is a stronger and more
falsifiable claim than "arm C finds more", and it follows directly from having
built a real loop.

---

## 8 · Open risks

1. **Agent variance may swamp the memory effect.** §3.3 argues the answer is
   trace-level measurement and a fixed tool surface across arms, but this is an
   argument, not a result. The check is cheap and should be run early: two seeds
   of the same arm on the same replay, comparing decision logs. If within-arm
   trace variance is comparable to the between-arm difference on the same
   metrics, the metrics need to move further down the trace (tool-call
   distributions rather than outcomes) before more compute is spent.
2. **Whether the agent reliably recognises recurrence is unknown.** Q6 now puts
   repetition-handling in the agent's hands — the similarity query makes prior
   claims visible, and the agent decides whether this is the same claim. That is
   the right locus for the judgement but an unmeasured capability. Mitigation:
   in the step-7 two-cycle replay, score recognition directly (did it call
   `recall` before writing a near-duplicate? did it set `supersedes`?), and if
   recall-before-write is unreliable, make it a *prompted obligation* rather
   than a code-enforced one before considering a threshold.
3. **The hypothesis set may grow without bound.** No forgetting mechanism is specified
   here beyond supersession, because the growth rate is not yet known.
   Mitigation: measure it once step 4 lands; if unbounded, decay by age ×
   attempts × failure rate — but measure first, and prefer giving the agent a
   `deprecate` write over a code-side decay rule.
4. **Non-uniform budgets make arms harder to compare at fixed cost.** If arm C
   allocates differently, a fixed total-token comparison and a fixed
   per-investigation comparison answer different questions. Decide which is the
   headline before running, and report both.
5. **Defect 9 undermines grading inputs.** Probe verdicts are what `GRADE_RULES`
   requires for `supported` and `finding`; if corroboration is prompt-advisory
   rather than enforced (`corroboration_policy` is defined at `probes.py:430` and
   never called), verdict quality is uncontrolled and grading inherits the noise.
   Fix before step 7.
6. **The `finding` grade rests on a judgement code cannot check.** Claim
   verification has a mechanical test — a probe either demonstrates the behaviour
   or does not, and `GRADE_RULES` enforces the probe ref. Remedy actionability
   does not: if the agent grades its own remedies, the finding count becomes a
   measure of the model's optimism. Mitigation: `GRADE_RULES` provides the floor
   (non-empty remedy, probe ref present); tighten it to require the remedy to
   name a locus that exists and a change class from an enumeration (consolidate /
   add guard / add test / revert), and calibrate the rest by human spot-check on
   a sample. Note this is a *validation* rule, not a decision rule — code checks
   form, the agent judges substance. Weakest joint in §0's definition; resolve
   before step 7.
7. **The corrected definition may leave too few findings to compare.** If
   Guardian produces two findings per cycle instead of twenty signal reports,
   C−B may lack the events for a statistically meaningful contrast. Mitigation:
   report the `supported`-grade count alongside `finding`, since both are
   memory-sensitive, and prefer the origin-based metric of §7 over raw counts.
   Measure this early — it can be checked on a single replay before steps 4–7 are
   built.
8. **The single-write-boundary rule and per-iteration checkpointing must not
   conflict.** `/out` is a tmpfs per the sandbox blueprint §3.1, so
   `cycle_state.json` checkpoints survive within a cycle but not across
   container exit — the durable write is still host-side at cycle end. Resume
   across a container crash therefore recovers to the last *cycle* boundary, not
   the last iteration. That is the right trade, but it means "resumable" in Q8
   is scoped to cycles, and the blueprint should not over-claim.
9. **A free agent may not use its freedom.** Given an open toolbox, a model may
   still fall into a stereotyped sequence — list signals, write hypotheses,
   investigate the first, report — reproducing the old workflow at higher cost
   and with more variance. Then the agency commitment of §3.2 buys nothing and
   loses interpretability. Mitigation: measure sequence entropy over tool calls
   in step 3, before memory is in play. Low entropy is diagnostic of a prompt
   that over-specifies procedure, not of an agent that does not need freedom.
10. **Summarization can distort the memory ablation.** If compaction drops
   memory-derived content, arm C degrades toward arm B as cycles lengthen.
   Mitigation: one arm-blind policy, canonical state re-injection, archived raw
   transcripts, cached-token and `compaction_events` metrics, and replay tests
   that compare pre/post-summary hypothesis state.

---

## 9 · Handoff notes

For whoever picks this up — including the author after a gap.

### 9.1 What is settled

These are decided; reopening them means reopening the argument in the named
section, not just the wording.

| Decision | Where | Short reason |
|---|---|---|
| Three types: signal → hypothesis → finding, with `findings ⊆ hypotheses` | §0 | a finding must name a solvable problem; a signal never does |
| Signals are supplementary, not necessary | §0.1 | otherwise `origin ∈ {memory, exploration}` is unreachable and the proactivity claim is untestable |
| One `grade` field, agent-written and code-validated | §3.1 | the booleans were projections; a derivation function would put the judgement back in code |
| Tools, not steps — L2 is a turn loop | §3.2, §3.5 | a step sequence removes the behaviour the experiment measures |
| `recall` is a tool available on any turn | §3.2, §4.3 | with retrieval in code, the contrast measures a fetch policy, not memory use |
| Context is pull, not push; three layers | §4.1, §4.2 | code cannot know which prior verdict this cycle needs |
| Six memory channels collapse to three query shapes | §4.3 | three of the six were the same query with a different `WHERE`; two were query-time statistics |
| Agency over interpretability, with measurement to compensate | §3.3 | the single-call alternative is a clean measurement of the wrong quantity |
| Step 3 (agency) before step 4 (memory) | §6 | a memory tool bolted onto a workflow only makes its fetches configurable |

### 9.2 What is not settled

Each of these needs a decision before the step named. None of them blocks
starting.

| Open | Blocks | Options on the table |
|---|---|---|
| Whether `GRADE_RULES` should require an enumerated change class in the remedy | step 7 | form-only floor (current) · locus-must-exist · locus + change class from {consolidate, add guard, add test, revert} (§8 risk 6) |
| Compaction summary quality | step 5 | replay exact hypothesis state across the boundary; inspect archived transcript when a summary loses evidence |
| Whether steps 1–4 of the terminology migration land as one commit | step 0 | one commit (the tree is incoherent between them) · four commits with a temporarily broken tree |
| Frame budget | step 5 | ~1–2k tokens is a guess, not a measurement; calibrate on a real cycle |
| Whether the campaign level (L1) is in scope at all | after step 7 | out of scope for the prototype · a thin cross-cycle budget carry |

### 9.3 Check these first

Cheap verifications that would change the plan if they came back differently.

1. **Do long cycles reach the context window at all?** Run one replay with the
   step-3 loop and log token counts per turn. If they do not, step 5 and §8
   risk 10 are deferred rather than solved, which reorders the plan.
2. **Does the corrected definition leave enough findings to compare?** Count
   `grade == "finding"` on a single replay after step 0. This is §8 risk 7 and it
   is answerable before steps 4–7 exist.
3. **Does the agent use its freedom?** Sequence entropy over tool calls, measured
   in step 3 before memory is in play (§8 risk 9). Low entropy diagnoses an
   over-specifying prompt.
4. **Is `/out` persistence what §8 risk 8 assumes?** Confirm against
   `design/sandbox_runtime_blueprint.md` §3.1 that resume is scoped to cycle
   boundaries, not iterations, and that Q8 does not over-claim.

### 9.4 Where the code is read-only

Every code change in this document is a **specification**, not an applied edit.
`codeminer/guardian/` was read-only from the environment that produced this
document; `design/terminology_migration.md` carries the concrete edit list for
step 0, with `file:line` anchors for every site. Nothing here has been applied to
the tree.

### 9.5 Positions considered and rejected

Three designs were specified at some point and then dropped. They are listed
because the counter-argument matters more than the conclusion:

- **Single-call `select`** was recommended for interpretability, then rejected —
  it removes the behaviour under study (§3.3).
- **`grade_of()`** as a derivation function was specified, then removed — it put
  the judgement back in code (§3.1).
- **`ValueFloorReached` / `HypothesesExhausted`** were exits, then removed — both
  were code deciding nothing more was worth doing (§3.4).

If a future draft re-proposes any of them, the counter-argument is in the named
section, not in a changelog.
