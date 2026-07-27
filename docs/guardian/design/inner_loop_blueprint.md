# Guardian Inner Loop — Blueprint v2

**Scope.** This document specifies **L3, the investigation loop** — the level
that verifies one hypothesis and returns evidence. L2 (the cycle loop) is
specified in `design/outer_loop_blueprint.md` and is treated here as fixed:
this document only states the two-way contract at the `investigate` tool
boundary and does not re-litigate anything above it.

**Why it exists.** A post-implementation audit of one real Pier cycle found
that an environment failure could look like evidence, that pytest discovery
could prevent all investigation, and that retrieval missed source which the
model could have judged directly. This revision keeps the typed execution
boundary while making pytest optional and admitting exact source-grounded
judgments. L3 remains an agent loop; runtime code validates provenance and
execution facts while the model performs semantic review.

**Length.** Deliberately ~1/3 of the outer blueprint. Rationale sections are
compressed to one paragraph each; the specification (§2–§6) is the part that
gets implemented.

**Companions.**

| Document | Answers |
|---|---|
| `design/outer_loop_blueprint.md` | what the *cycle* decides, carries, and when it stops |
| `design/sandbox_runtime_blueprint.md` (v3) | the one-write-boundary invariant; where cycle work executes |
| **This document** | how *one hypothesis* is verified, and what makes its evidence admissible |

**Convention.** `file:line` citations in the defect table identify the
pre-remediation audit snapshot. Unqualified identifiers and the implementation
map describe the current code under `codeminer/guardian/`.

---

## 0 · Vocabulary

This document reuses tool-protocol and CodeMiner names wherever they already
exist. The two new infrastructure payloads, `CommandResult` and
`ProcessStatus`, are explicitly below the agent-protocol layer; they are not
presented as general agent abstractions. §0.1 defines the layers, and §0.2
defines Guardian's domain terms.

### 0.1 The layering

The call record and run-result shapes already exist in
`codeminer/agent/agent_types.py`. Tool-result messages exist in
[MCP `CallToolResult`](https://modelcontextprotocol.io/specification/2025-11-25/server/tools),
[Anthropic `tool_result`](https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/implement-tool-use),
and [OpenAI tool-call output](https://platform.openai.com/docs/api-reference/responses);
the proposed `ToolResult` struct gives that protocol concept a typed local
representation.

```
model turn
  └─ ToolCallRecord               # tool_call_id, name, arguments, duration
       └─ result: ToolResult      # content, is_error, structured_content
            └─ CommandResult      # only for command-running tools
                 ├─ ProcessStatus # EXITED | SIGNALED | TIMED_OUT | ...
                 ├─ exit_code
                 └─ tests: dict[node_id, TestStatus]   # parsed report
```

| Term | Meaning here | Already used in |
|---|---|---|
| **tool call** | one invocation request emitted by the model | provider API; `ToolCallRecord` (`agent/agent_types.py:17`) |
| **`ToolCallRecord`** | the audit record for one call: identity, tool, arguments, result, duration, and error | `agent/agent_types.py:17` |
| **`tool_call_id`** | identity of one call | `agent_types.py:20`; already threaded through `investigator/runner.py:186, 302, 1008` |
| **`ToolResult`** | the bounded result returned to the model for one call | MCP `CallToolResult`; Anthropic `tool_result`; OpenAI tool-call output; `runner.py:189` formats the message |
| **`is_error`** | *the tool call itself* failed; never "the tests failed" | MCP `isError` / Anthropic `is_error`; explicit local normalization for other providers |
| **`CommandResult`** | structured command detail inside `ToolResult.structured_content` | proposed infrastructure struct |
| **`ProcessStatus`** | how the child process terminated | POSIX/`subprocess` vocabulary, normalized into an enum |
| **`TestStatus`** | `passed / failed / error / skipped` per test, **parsed from the report** | SWE-bench log parsers |
| **FAIL_TO_PASS / PASS_TO_FAIL** | a named transition of one test's status between two runs | SWE-bench |
| **`exit_status`** | why the *investigation run* ended | mini-swe-agent/SWE-agent run metadata; outer-loop typed-exit convention |
| **`InvestigationRunResult`** | the run's outcome object | `RunResult` pattern; cf. `AgentResult` (`agent_types.py:30`) |

**The lifecycle judgments never collapse.** This is the whole point of the
nesting:

| Question | Field | Example of a "bad" value |
|---|---|---|
| Did the tool call work? | `ToolResult.is_error` | malformed arguments |
| How did the process terminate? | `CommandResult.status` | `TIMED_OUT`, `SPAWN_FAILED` |
| What did the tests do? | `CommandResult.tests` | `{test_x: FAILED}` |
| Is this admissible evidence? | `evidence_status` (§0.2) | — |

**`is_error` is not the evidence gate.** A pytest run that reports failing
tests is a *successful* tool call: `is_error=False`, and that is correct. The
converse also holds and is the trap — `is_error=False` with an **empty `tests`
map** (rc 5, "no tests collected") is a tool call that worked perfectly and
learned nothing. Anyone who wires admissibility to `is_error` re-introduces
defect I-4 through the front door.

**`exit_status`, not `stop_reason`, names run termination.** Provider responses
already use `stop_reason` or `finish_reason` for why one model completion ended.
Reusing it for the whole investigation would overload a familiar protocol
field. L3 persists provider completion metadata in the raw model trace and uses
`exit_status` only for the investigation run.

### 0.2 Guardian's own words

These are domain terms rather than tool-protocol terms.

**Probe** — a tool call made to gather evidence about a hypothesis. Already the
project's word: the evidence prefix `probe:` is what `GRADE_RULES` keys on
(`loop/state.py:104`), and the sandbox blueprint §5 calls the isolation seam the
probe boundary. "Probe" names the *purpose*; `tool call` names the *mechanism*.

**Verdict** — L3's answer about the claim:
`confirmed | refuted | inconclusive`. Current L3 spells the negative verdict
`rejected` (`VALID_VERDICTS`, `runner.py:408`); this blueprint standardizes it
to `refuted` to match L2's grade and remove an otherwise pointless translation.
A verdict is *not* a grade; grading remains L2's (outer §0.2).

**`evidence_status`** — `source | valid | invalid | environment`. `source`
means the model submitted a closed-form judgment grounded in cited source;
`valid` means an executable probe or classified test supplied additional
evidence. `invalid` and `environment` are not grade-admissible. This is
Guardian-specific; nothing in the API layer means it, which is exactly why it
needs its own name.

**Hypothesis / signal / finding** — outer §0, never redefined here.

### 0.3 The one-line contract

> **L3 answers one narrow, pre-stated obligation about one hypothesis, using
> only probes whose tool results it can classify, within a budget it cannot
> exceed, and returns a run result that L2 grades.**

---

## 1 · What is broken, in one table

Each row is a P0 or correctness defect from the audit, with its source
citation and the section that fixes it. This replaces a rationale chapter.

| # | Defect | Evidence | Fixed in |
|---|---|---|---|
| I-1 | **Forced finalization escapes the tool protocol.** On budget/turn exhaustion L3 makes a tool-free model call. The Codex adapter omits the "Guardian owns tool execution" instruction when `tools` is empty, so the nested agent tries its own Bubblewrap sandbox — forbidden in Pier | `investigator/runner.py:1011-1024`; `llm/codex_cli_chat.py:251-256` | §4 (`submit_verdict`), §7 (`exit_status`) |
| I-2 | **Budget grants are advisory.** Limits are checked only *before* a call, and forced finalization ignores exhaustion. A 12K grant spent 65,762 tokens; the 200K cycle cap ended at 247,453 | `runner.py:900-913` (pre-call check), `runner.py:1011` | §6 (reservation ledger) |
| I-3 | **L3 is starved of context.** It receives claim, `locus[0]` via the compat `target` property, confidence, and `origin` disguised as `kind` — not consequence, remedy, full locus, or the evidence L2 already gathered. It re-discovers everything and its retrieval returns nothing | `runner.py:884-893`; compat shims at `loop/state.py:83-101` | §3.1 (the task), §4 (`read_code`) |
| I-4 | **Test outcomes are read off the process exit code instead of parsed from the report.** Every pytest rc ∉ {0, 2} becomes `FAIL (test confirms the risk)`. Timeout, missing interpreter, missing dependency, internal error, usage error, permission failure, sandbox failure and "no tests collected" all become *confirming evidence*. The sandbox makes it worse by returning `1` for both a timeout and a spawn error, so the two most common environment failures reach the classifier already disguised as a test failure | `probes.py:168-176`, `probes.py:308-317`; `investigator/sandbox.py:58-61` | §3.2 (`CommandResult`), §5.1 |
| I-5 | **The test environment is never discovered.** `["python", "-m", "pytest", …]` is hard-coded and takes whatever `python` is on `PATH`; Guardian itself runs under `/opt/codeminer-env/bin/python`, and the DeepSWE image's default python has neither pytest nor joblib | `probes.py:164`, `probes.py:304` | §5.2 (prelude + recipe discovery) |
| I-6 | **Failed investigations mint admissible evidence.** L2 appends `probe:<cycle>:<attempt>` after *any* `run_investigator` return, and `GRADE_RULES` only requires a `probe:`-prefixed string plus a remedy — so the Bubblewrap failure was one model turn away from becoming a finding | `loop/agent.py:219-220`; `loop/state.py:104-112` | §8.1 |
| I-7 | **Corroboration is inconsistent and unbound.** The prompt allows an existing failing test to corroborate; the code accepts only fix-probe or differential. If a synthesized test exists, an existing failure is ignored; if none exists, any `confirmed` passes with no mechanical check. The "latest" synth/fix/differential records are combined without proving they concern the same test | `runner.py:685-736`; `probes.py:446-487`; prompt at `runner.py:626-631` | §8.3 (transitions bound by `parent_tool_call_id`) |
| I-8 | **Disposable ≠ isolated.** Snapshots are throwaway (a real improvement — `investigator/sandbox.py:78`), but `WorktreeSandbox.run_command` executes model-authored Python as the bridge user with inherited environment and network | `investigator/sandbox.py:46-60` | §5.3 |
| I-9 | **The run record is incomplete.** `evidence_diff` is never populated; existing-test calls drop their structured result; retrieval calls lose provenance; records carry no commit, duration, command, cwd, or `ProcessStatus`; there is no per-call checkpoint | `runner.py:423-451`, `runner.py:752-771` | §3.2, §7 |

Defects I-1, I-2 and I-6 compose into the failure mode that matters: **an
environment error produced a plausible-looking evidence reference, and the
budget system did not notice it cost 5× its grant.** Nothing downstream can
distinguish that from a real investigation.

---

## 2 · Invariants

Five. Each is code's job, never the model's, and each maps to a defect above.

1. **Every L3 model call carries the tool protocol.** There is no tool-free
   call anywhere in L3. Termination is a *tool call* (`submit_verdict`), not
   the absence of tool calls. (I-1)
2. **No call is issued that cannot fit its reservation.** Budget is reserved
   before the call at an estimated maximum and reconciled after. Exhaustion
   returns a deterministic typed result with no further model call. (I-2)
3. **A test outcome comes from the parsed report, never from the process exit
   code**, and only parsed `PASSED` or `FAILED` statuses are behavioural
   evidence. An empty map, or one containing only `ERROR`/`SKIPPED`, is not
   behavioural evidence regardless of `is_error` or `ProcessStatus`. (I-4)
4. **Only an admissible run emits a grade-valid reference.** L2 maps
   `evidence_status="source"` to `source-valid:` and
   `evidence_status="valid"` to `probe-valid:`. `GRADE_RULES` accepts exactly
   those two prefixes. (I-6)
5. **Nothing model-authored executes outside the restricted probe subprocess**,
   with no network, no inherited credentials, and enforced CPU/memory/pids/wall
   limits, under a path-validated disposable root. (I-8)

Invariant 3 is the one to defend hardest. A monitoring agent that reports false
risks is worse than one that reports nothing, and every entry in the I-4 list is
a way for the current code to manufacture one.

---

## 3 · Carried state

### 3.1 What L2 hands down — `InvestigationTask`

The current call passes four scalars and two compatibility shims (I-3). It is
replaced by one object, built by L2 from state it already holds.

```python
@dataclass(frozen=True)
class InvestigationTask:
    hypothesis: Hypothesis          # the WHOLE record — claim, consequence,
                                    # remedy, locus, origin, evidence, confidence
    obligation: str                 # the ONE question this run must answer
    excerpts: list[SourceExcerpt]   # bounded spans L2 already read
    commit_diff: str                # bounded diff for the cycle's commit
    prior_attempts: list[dict]      # verdicts from earlier attempts on this id
    grant_tokens: int               # hard, not advisory (§6)
    deadline_s: float
```

**`obligation` is the load-bearing addition.** L2 does not hand L3 "a risk"; it
hands it a falsifiable question — *"does `normalize_config({})` raise where
`parse_config({})` returns a default, at HEAD?"* This is what makes a narrow
probe plan possible and an `inconclusive` interpretable, and it is the cheapest
fix for I-3's symptom: the verbose retrieval query that matched nothing.

`excerpts` and `commit_diff` exist so L3 starts from what L2 already paid for.
Both are bounded at construction, not by the model's discretion.

### 3.2 What a tool call returns

Two result structs sit inside the existing call record. The model receives the
`ToolResult`; command-running tools additionally populate its
`structured_content` with `CommandResult`; the runner retains the enclosing
`ToolCallRecord`.

```python
class ProcessStatus(str, Enum):
    EXITED       = "exited"        # exit_code is available, including non-zero
    SIGNALED     = "signaled"      # killed by signal / OOM / rlimit
    TIMED_OUT    = "timed_out"
    SPAWN_FAILED = "spawn_failed"  # no interpreter, permission denied
    SANDBOX_FAILED = "sandbox_failed"

class TestStatus(str, Enum):       # SWE-bench vocabulary
    PASSED  = "passed"
    FAILED  = "failed"
    ERROR   = "error"              # collected but could not run (import error)
    SKIPPED = "skipped"

@dataclass(frozen=True)
class CommandResult:
    status: ProcessStatus
    exit_code: int | None          # process fact; never test authority
    signal: int | None             # populated when status == SIGNALED
    command: list[str]
    cwd: str
    commit: str                    # which snapshot this ran against
    tests: dict[str, TestStatus]   # node_id → status, from the report parser;
                                   #   EMPTY when nothing could be parsed
    stdout: str                    # bounded
    stderr: str                    # bounded

@dataclass(frozen=True)
class ToolResult:
    content: str                       # what the model sees; bounded
    is_error: bool                     # THE TOOL CALL failed — not the test
    structured_content: dict | CommandResult | None

# Reuse and extend codeminer.agent.agent_types.ToolCallRecord.
@dataclass
class ToolCallRecord:
    tool_call_id: str
    skill_id: str                      # Guardian stores the tool name here
    arguments: dict
    result: ToolResult | None = None
    duration_ms: float = 0.0
    error: str | None = None           # compatibility mirror of tool error
    parent_tool_call_id: str | None = None
```

Call identity, tool name, arguments, parentage, and duration belong to
`ToolCallRecord`; the content returned to the model belongs to `ToolResult`.
Putting call metadata inside `ToolResult` would collapse the request, response,
and audit record into one type and would not match CodeMiner's existing runner.
For compatibility, `ToolCallRecord.error` mirrors
`ToolResult.content` when `is_error=True`; admissibility never reads it.

**The exit code is a hint; the parsed report is the authority.** This is the
SWE-bench rule, and adopting it collapses the whole of I-4 into one sentence:
`tests` is populated by a report parser, and if the parser produced nothing then
no test outcome is known, whatever the process exited with.

| Situation | `is_error` | `ProcessStatus` | `tests` | Evidence about behaviour? |
|---|---|---|---|---|
| report parsed, all `PASSED` | false | `EXITED` | non-empty | yes |
| report parsed, ≥1 `FAILED` | false | `EXITED` | non-empty | yes |
| report parsed, only `ERROR` (import/dependency failure) | false | `EXITED` | non-empty | no — the test environment is broken |
| rc 5 — no tests collected | false | `EXITED` | **empty** | **no** |
| rc 2/4 — usage or collection interrupt | true | `EXITED` | **empty** | **no** |
| rc 3 — pytest internal error | true | `EXITED` | empty | no |
| pytest module missing after Python starts | true | `EXITED` | empty | no |
| timeout | true | `TIMED_OUT` | empty | no |
| missing interpreter / spawn / permission failure | true | `SPAWN_FAILED` | empty | no |
| OOM or resource-limit signal | true | `SIGNALED` | empty | no |
| namespace or sandbox setup failure | true | `SANDBOX_FAILED` | empty | no |
| malformed tool arguments | true | — (no `CommandResult`) | — | no |

Rows 4–11 are what the current code can report as
`FAIL (test confirms the risk)`.
Rows 3 and 4 are the two layering traps: a successful tool call may return a
non-empty map containing no behavioural outcome, or an empty map containing no
outcome at all. That is why invariant 3 checks for parsed `PASSED`/`FAILED`
statuses rather than `is_error` or map non-emptiness.

### 3.3 The run record

`InvestigationRunResult.tool_calls` is the ordered list of
`ToolCallRecord`s — exactly the container shape used by
`AgentResult.tool_calls` (`agent_types.py:33`), so existing serializers can be
extended instead of bypassed. The corresponding model messages remain in the
checkpoint transcript; `trajectory.json` is the audit projection of these call
records.

### 3.4 What L3 returns — `InvestigationRunResult`

```python
@dataclass
class InvestigationRunResult:
    verdict: str               # confirmed | refuted | inconclusive
    exit_status: str           # submitted | budget_exceeded | wall_clock_exceeded
                               #   | no_progress | environment_unavailable
    evidence_status: str       # source | valid | invalid | environment
    reasoning: str
    tool_calls: list[ToolCallRecord]
    cites: list[str]           # tool_call_ids the verdict rests on
    evidence_test: str         # synthesized source, or ""
    evidence_diff: str         # fix-probe diff, or ""   ← actually populated (I-9)
    source_spans: list[SourceExcerpt]
    usage: TokenUsage          # reuses codeminer/llm/usage.py
    budget: BudgetLedger       # granted / reserved / actual / overshoot (§6)
```

`evidence_status` is **derived from cited tool calls, not asserted**:

- `source` iff a submitted non-inconclusive verdict cites exact source from
  `read_code` or `retrieve_evidence` and needs no behavioural interpretation;
- `valid` iff a cited generic probe actually executed, or a cited command
  contains classified `PASSED`/`FAILED` test evidence satisfying §8;
- `environment` iff the snapshot itself is unavailable, the model backend is
  unavailable, or attempted test execution yielded only environment failures;
- `invalid` otherwise.

A non-empty map containing only `ERROR` or `SKIPPED` is not behavioural
evidence. Keeping `evidence_status` separate from `verdict` is deliberate —
"the model concluded X" and "the run produced admissible evidence" are
different facts, and collapsing them is how I-6 happened.

---

## 4 · Tools

Eight, with four pytest-specific tools exposed only when the prelude validates a
recipe. Same design rule as the outer loop: code owns invariants, the model
chooses the call sequence.

| Tool | Signature | Notes |
|---|---|---|
| `read_code` | `(path, start_line, end_line) → ToolResult` | direct, cheap, deterministic; supplies exact source spans |
| `retrieve_evidence` | `(query, top_k) → ToolResult` | records full provenance (path, span, score) (I-9) |
| `run_probe` | `(source, snapshot, timeout) → ToolResult` | generic dependency-light Python chosen by the model; may run on current, prior, or both snapshots; runtime retains exact source and typed process results |
| `run_existing_test` | `(node_id) → ToolResult` | optional; uses the validated recipe (§5.2), never a hard-coded interpreter |
| `synthesize_test` | `(target_symbol, body) → ToolResult` | optional; its `tool_call_id` is what later calls reference |
| `run_synthesized_test` | `(tool_call_id) → ToolResult` | optional; `parent_tool_call_id` = the synthesize call |
| `corroborate` | `(tool_call_id, method="fix"\|"differential") → ToolResult` | optional; `parent_tool_call_id` = the run call |
| `submit_verdict` | `(verdict, reasoning, cites=[tool_call_id,…]) → ToolResult, then exit` | the only normal exit; it must be the turn's only tool call |

**Hard-coded, and each an invariant not a decision:** budget reservation (§6),
the capability prelude (§5.2), command/process classification, report parsing
when pytest is available, evidence-label derivation (§8), per-call
checkpointing, and content bounding. Search strategy, source interpretation,
probe source, and verdict remain the model's choices.

There are intentionally no defect-specific tools such as
`CallReturnOrderProbe` or `BranchApplicabilityProbe`. Those would move semantic
judgment into an expanding hand-written framework. Exact source plus one generic
probe preserves model agency without giving up auditable execution.

**Removed:** the tool-free finalization path. `submit_verdict` makes termination
a tool call inside the protocol, which is the direct fix for I-1 and the reason
no Codex call ever runs without the "Guardian owns tool execution" instruction.

---

## 5 · Tool execution and command results

### 5.1 Two snapshots, one parser

Keep the disposable `CurrentSnapshotSandbox` / `PriorSnapshotSandbox` pair
(`investigator/sandbox.py:78-129`) — it is the part of the current design that is
right, and it makes the non-modifying invariant structural rather than asserted.
The layers are explicit:

1. `run_command` returns a `CommandResult`, never `(int, str)`.
2. The dispatcher wraps it in `ToolResult.structured_content`.
3. The runner stores that result in a `ToolCallRecord` and renders the provider
   tool-result message using the record's `tool_call_id`.
4. The report parser is the only component that writes `CommandResult.tests`.

No call site in `probes.py` interprets a process exit code as a test outcome.

### 5.2 The capability prelude

Before the first model call, L3 runs a fixed, token-free sequence — the audit's
first efficiency item, deterministic work first.

```
1. environment_health()  writable root? snapshot materialized?
2. discover_recipe()     cached recipe → Guardian interpreter → uv/poetry
                         lockfile runner → python/python3 on PATH
3. validate_recipe()     run the recipe's collect-only form; the report parser
                         must produce a parseable (possibly empty) test list
4. load_task()           hypothesis, obligation, excerpts, diff into opening context
```

- **`sys.executable` is the first Python candidate**, then the recipe's own
  interpreter, then `PATH`. Guardian runs under `/opt/codeminer-env/bin/python`;
  probes must not silently land on a different one (I-5).
- A validated recipe is **persisted to repository memory** keyed by
  `(repo, commit_range)` so later cycles skip discovery.
- If operation 1 fails, L3 returns immediately with
  `verdict="inconclusive"`, `exit_status="environment_unavailable"`, and
  `evidence_status="environment"` because no safe investigation surface exists.
- If recipe discovery or validation fails, L3 continues with `read_code`,
  `retrieve_evidence`, `run_probe`, and `submit_verdict`. It records
  `pytest=false`, a capability warning, and `degraded=true`; pytest-specific
  tools are not offered to the model.

### 5.3 Probe isolation

Read-only source handle and writable probe handle are separate objects.
Model-authored code executes in a restricted subprocess: no network, scrubbed
environment (credentials and unrelated vars stripped), `RLIMIT_CPU` / `AS` /
`NPROC` set, wall-clock timeout enforced by the parent, and every path validated
to resolve under the disposable root. This is the L3-local instantiation of the
sandbox blueprint's one-write-boundary invariant; container topology is that
document's business.

---

## 6 · Budget as a reservation ledger

The current check is "am I over yet?" asked before each call
(`runner.py:900-913`), which cannot bound a call whose cost is not yet known, and
is bypassed entirely by forced finalization (I-2).

```python
@dataclass
class BudgetLedger:
    granted: int
    reserved: int      # sum of estimated maxima for in-flight calls
    actual: int        # provider-reported totals
    overshoot: int     # max(0, actual - granted)
```

1. **Reserve before calling.** `estimate = prompt_tokens + max_completion_tokens`.
   If `reserved + estimate > granted`, the call is **not made**.
2. **Reconcile after.** Replace the reservation with the provider's actual count;
   a reservation that under-estimated is recorded as overshoot.
3. **Exhaustion is deterministic.** No model call, no forced finalization: return
   `verdict="inconclusive"`, `exit_status="budget_exceeded"`, and the
   `evidence_status` derived from whatever calls completed.
4. **Report all four numbers** to L2, always. L2 adds `actual` to
   `state.budget_spent`; today it takes `max(tokens_used, after - before)`
   (`loop/agent.py:212-215`), which cannot see an overshoot it was never told
   about.
5. **Overshoot > 0 is a budget-health failure**, logged and surfaced in the cycle
   report — not silently absorbed. It is a property of the provider, and the
   experiment needs to know when it occurred.

The estimator only needs to be conservative, not accurate. An over-reserving
estimator wastes grant; an under-reserving one reproduces I-2.

---

## 7 · The loop body and exit statuses

```python
def run_investigation_agent(task: InvestigationTask, ctx) -> InvestigationRunResult:
    prelude = run_prelude(task, ctx)               # §5.2 — no model calls
    if prelude.blocked:
        return unavailable_result(prelude)         # typed, zero tokens

    state = InvestigationState(task, prelude, ledger=BudgetLedger(task.grant_tokens))
    messages = [system_prompt(), opening_context(task, prelude)]

    while True:
        try:
            check_limits(state)                    # → WallClockExceeded
            reserve_or_stop(state)                 # → BudgetExceeded (no call)

            response = query(messages, TOOLS, state)   # ALWAYS with tools (§2.1)
            reconcile(state, response)
            messages.append(response)              # raw response retains provider
                                                    # finish/stop reason

            if not response.tool_calls:
                raise NoProgress(response)         # not a finalization path

            for call in response.tool_calls:       # → VerdictSubmitted
                result = dispatch(call, state, ctx) # never raises; ToolResult
                record = make_tool_call_record(call, result)
                state.tool_calls.append(record)
                messages.append(as_tool_result_message(call.id, result))
                checkpoint(state)                  # durable after EVERY call
                if accepted_verdict_submission(record):
                    raise VerdictSubmitted.from_record(record)

        except InvestigationInterrupt as e:
            return e.as_run_result(state)
        finally:
            checkpoint(state)                      # /out/investigation_<id>.json
```

Exit statuses mirror the outer loop's typed exits (outer §3.4):

```
InvestigationInterrupt          →  exit_status
├── VerdictSubmitted            →  submitted                 # the normal exit
├── NoProgress                  →  no_progress               # a turn with no tool calls
├── BudgetExceeded              →  budget_exceeded           # no further call issued
├── WallClockExceeded           →  wall_clock_exceeded
└── EnvironmentUnavailable      →  environment_unavailable   # prelude or sandbox
```

`VerdictSubmitted` is first for the same reason `ReportSubmitted` is first in the
outer loop: the normal exit is the model deciding, not code deciding for it.
Everything below it is a guarantee being enforced. **There is no
forced-finalization exit**, and its absence is the fix for I-1.

`dispatch` never raises. A malformed argument, a rejected `submit_verdict` (empty
`cites` on a `confirmed`, or submitted alongside another call), a timed-out
probe — each returns a `ToolResult` with `is_error=True` and content saying
*why*, so the rule teaches rather than merely blocks. An accepted
`submit_verdict` is first recorded with its result and then converted to
`VerdictSubmitted` by the loop. Dispatch exceptions escaping into a generic L2
tool error is part of I-9.

---

## 8 · Evidence admissibility

### 8.1 The L2 boundary

`loop/agent.py:219-220` currently appends `probe:<cycle>:<attempt>`
unconditionally. Replace with a conditional emission keyed on `evidence_status`:

```python
prefix = {
    "source": "source-valid",
    "valid": "probe-valid",
    "invalid": "probe-invalid",
    "environment": "env",
}
ref = f"{prefix[result.evidence_status]}:{state.cycle_no}:{h.attempts}"
h.evidence.append(ref)
```

and tighten `loop/state.py:104-112`:

```python
def _has_valid_evidence(h):
    return any(
        e.startswith(("probe-valid:", "source-valid:"))
        for e in h.evidence
    )

GRADE_RULES = {
    "finding":   lambda h: bool(h.remedy) and _has_valid_evidence(h),
    "supported": _has_valid_evidence,
    "refuted":   _has_valid_evidence,
    ...   # unchanged
}
```

Four prefixes preserve the distinctions between source reasoning, executed
evidence, an uninformative attempt, and unavailable infrastructure. Only
`source-valid:` and `probe-valid:` are grade-admissible. An `env:` reference
tells the next cycle that validation was unavailable rather than silently
turning a failed analysis into a clean review.

### 8.2 Source-grounded and executable evidence

A confirmed or refuted verdict always cites a source-producing call and retains
at least one exact `SourceExcerpt`. This supports two deliberately simple
routes:

1. **Closed-form source route.** The claim follows directly from exact code,
   such as a returned tuple whose caller destructures a different order. The
   model may submit from source alone; L2 records `source-valid:`.
2. **Executable route.** When behavior is not closed-form, the model writes a
   small generic Python probe or uses an available pytest tool. Runtime verifies
   that the process actually ran and retains its typed result; L2 records
   `probe-valid:`.

Runtime does not claim to prove that arbitrary probe output semantically entails
the verdict. That judgment belongs to the model. Runtime does prevent a syntax
failure, missing snapshot, timeout, or unavailable test environment from being
presented as successful execution.

### 8.3 Pytest corroboration as a named transition

`_enforce_corroboration` (`runner.py:685-736`) picks the *latest* synth, fix and
differential record and combines them without proving they concern the same test
(I-7). Replace recency with SWE-bench's transition vocabulary, computed over two
`ToolCallRecord`s linked by `parent_tool_call_id`, each carrying a
`CommandResult`:

> A **transition** is the change in one test's `TestStatus` between a parent call
> and a child call. `FAIL_TO_PASS` and `PASS_TO_FAIL` are admissible
> corroboration; both call records must contain a command result with
> `ProcessStatus.EXITED`, and both must contain that test's `node_id` in their
> `tests` map.

| Method | Admissible transition |
|---|---|
| `fix` | the synthesized test goes `FAILED → PASSED` when the minimal reversal is applied — a **FAIL_TO_PASS** |
| `differential` | the same test is `PASSED` at prior and `FAILED` at current — a **PASS_TO_FAIL** |
| `existing_test` | an existing test is `FAILED` and its failure names a symbol in `hypothesis.locus` |

The third row resolves the prompt/code disagreement in I-7 by making the prompt's
rule real, with a locus check so an unrelated pre-existing failure cannot
corroborate. And **the existing-test route applies whether or not a synthesized
test exists** — the current code ignores it when one does.

Two floors that are not negotiable: no `confirmed` or `refuted` without at least
one cited call containing a parsed `PASSED` or `FAILED` status; and no
behavioural verdict without at least one `source_spans` entry, so a verdict is
always tied to code someone can read.

Because a transition is computed from two parsed `tests` maps rather than from
two process exit codes, it inherits invariant 3 for free: a timeout, missing
dependency, or parsed import error cannot participate in a behavioural
transition.

---

## 9 · Implementation map

The repair landed in eight work areas. The final column is the regression
contract which keeps each area complete.

| # | Work item | Files | Done when |
|---|---|---|---|
| 1 | `submit_verdict` replaces forced finalization; normalize `rejected` → `refuted` | `investigator/agent.py`, `llm/codex_cli_chat.py` | no L3 model call is ever made with empty `tools`; budget exhaustion returns a typed result with zero further calls; L3 and L2 use the same negative-verdict spelling |
| 2 | `ToolResult` / `CommandResult` + report parser | `investigator/sandbox.py`, `probes.py`, new `investigator/report_parser.py` | the §3.2 table holds; timeout, empty collection, and missing interpreter produce no behavioural test status |
| 3 | `InvestigationTask` + `read_code` | `loop/agent.py`, `investigator/agent.py` | L3 receives consequence, remedy, full locus, L2's excerpts and the commit diff; a behavioural verdict without a source span is rejected |
| 4 | Prelude: health check + optional recipe discovery | `investigator/environment.py` | a missing snapshot blocks L3; a repo with no runnable pytest continues with source and generic probes while recording degraded capability |
| 5 | Conditional evidence references | `loop/agent.py`, `loop/state.py` | source and executed evidence receive distinct admissible references; an environment-failed investigation cannot produce a finding |
| 6 | Transitions bound by `parent_tool_call_id` | `probes.py`, `investigator/agent.py` | a fix probe on test A cannot corroborate a failure of test B; an existing failing test corroborates even when a synthesized test exists |
| 7 | Harden and instrument the probe sandbox | `investigator/sandbox.py` | model-authored code runs with no network and scrubbed env; every call record carries duration and every command result carries commit, command, cwd, and `ProcessStatus`; per-call checkpoint exists |
| 8 | One real Pier integration test | `test/guardian/` | a post-commit cycle reaches a parsed test report in the disposable snapshot with no mocks |

The dependency order remains useful when changing this area: evidence-label
gating depends on typed command results, and transition corroboration depends on
both typed results and source-carrying tasks.

---

## 10 · Tests, and the acceptance gate

Current unit tests encode broken behaviour and must change, not just grow:

- a test expects a model call after budget exhaustion → invert it;
- a test treats generic rc == 1 as a confirmed existing-test failure → replace
  with report-parser cases;
- most execution tests are mocked; none runs the Codex adapter under Pier's
  nesting model; none asserts that an environment failure cannot create
  admissible evidence; none checks that a 12K grant stays within its allowed
  overshoot; the Docker smoke tests report container creation but never a real
  probe.

Seven tests are load-bearing — one per invariant of §2, plus the two layering
traps:

1. no L3 model call is issued with empty `tools` (assert on the adapter);
2. a 12K grant produces `actual ≤ granted + allowed_overshoot`;
3. each of {timeout, missing dependency, rc 5, rc 2, rc 3, spawn failure}
   produces no `PASSED`/`FAILED` status and cannot reach `grade == "finding"`;
4. only `probe-valid:` and `source-valid:` references satisfy
   `GRADE_RULES["finding"]`;
5. a probe subprocess cannot open a socket or read a scrubbed credential var;
6. **`is_error=False` with an empty `tests` map is not admissible** — the rc 5
   case, asserted directly against `evidence_status`, so the §0.1 trap is
   regression-tested rather than only documented;
7. **`is_error=False` with a non-empty map containing only `ERROR`/`SKIPPED`
   is not admissible** — non-empty does not by itself mean behavioural evidence.

**Acceptance gate before the next ablation** — unchanged from the audit:

> One non-trivial post-commit cycle reads source, stays within its grant, and
> produces source-grounded evidence, an actually executed generic probe, a
> parsed test result, or a correctly classified degraded/environment outcome —
> with no Bubblewrap error and no fake `probe:` evidence.

---

## 11 · Handoff

**Settled.** The layering of §0.1 — `ToolCallRecord` → `ToolResult` →
`CommandResult` → (`ProcessStatus`, `exit_code`, `TestStatus`) — with
tool failure, process termination, test outcome, evidence admissibility, and run
exit kept separate. `is_error` is explicitly *not* the evidence gate. Names
follow the provider API and `codeminer/agent/agent_types.py`; `ToolResult` is a
local typed projection of the protocol result, while `CommandResult` and
`ProcessStatus` are the new command-specific types.

The obligation-per-run contract (§3.1); the parsed report as the sole authority
on pytest outcomes (§3.2); `submit_verdict` as the only normal exit (§4, §7);
reservation-based budgeting (§6); four-prefix conditional evidence (§8.1);
model-trusted source/probe judgment (§8.2); and pytest corroboration as a
transition bound by `parent_tool_call_id` (§8.3). Disposable snapshots stay;
older notes claiming L3 writes to production are stale.

**Not settled.** (a) The overshoot allowance in test 2 — a number, not a design;
set it from one measured run. (b) `tool_call_id` is provider-assigned and unique
per run, which is all §8.3's parentage needs; **cross-cycle** probe
deduplication would additionally
want a content fingerprint (`sha256(name, arguments, commit)`), deferred until
something needs it. (c) Whether `corroborate` stays one tool with a `method`
argument or splits back into two; one is proposed because it makes the parent
binding unavoidable. (d) Expected-information-gain scoring for probe allocation
(audit efficiency item 6) — deferred until §9 lands. (e) Generalization past
Language-specific test integrations attach at §5.2's recipe discovery and
§3.2's report parser. Source inspection remains language-neutral, while the
current generic executable probe is Python-specific.

**Check first if picking this up cold.** Start at
`investigator/agent.py` (`TOOLS`, `_validate_submission`, and
`run_investigation_agent`), `investigator/probes.py` (`run_python_probe` and report
parsing), `loop/agent.py` (L2/L3 evidence mapping), and `loop/state.py`
(`GRADE_RULES`). Those are the current policy seams.
