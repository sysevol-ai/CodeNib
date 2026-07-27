# Guardian + Coding Agent — Concurrent Architecture

## Purpose

Repository Guardian is a read-only review sidecar for a DeepSWE coding agent.
The coding agent owns the task, edits the repository, runs tests, and creates
commits. Guardian observes those commits and investigates possible regressions
in parallel.

Pier still sees one agent, `GuardianCodingAgent`. That wrapper delegates coding
to the selected solver and makes exactly one Guardian transport available:

- MCP-native solvers, currently Claude Code, use `query_guardian`.
- Codex uses filesystem actions, `guardian-start` and
  `guardian-checkpoint`.

Codex deliberately does not receive the MCP transport as well. Running both
transports for one solver could analyze the same commit twice and double model
spend.

Guardian has four architectural invariants:

1. The checkout present at setup is the **baseline**, or cycle 0. It is never
   analyzed as coding-agent work.
2. Guardian starts lazily when the coding agent first uses its Guardian action.
3. Only a commit after the baseline triggers cycle 1.
4. Guardian never edits the task checkout. Dynamic probes run in disposable
   snapshots.

## Runtime topology

```text
Pier task container
│
├── GuardianCodingAgent
│   ├── setup: record /app HEAD as /app/.guardian/base_commit
│   └── selected coding solver
│
├── Codex transport (Codex only)
│   ├── /app/.guardian/bin/guardian-start
│   ├── /app/.guardian/bin/guardian-checkpoint
│   └── deepsweguardian.codex_bridge
│
├── MCP transport (MCP-native solvers only)
│   └── codeminer.guardian.mcp_server
│       └── query_guardian(...)
│
└── one active Guardian watcher
    └── run_cycle(commit)
        ├── L1: deterministic signal collection
        ├── L2: hypothesis and investigation controller
        ├── L3: typed evidence investigation
        └── report cache and optional cross-cycle memory
```

The coding process and Guardian watcher become concurrent only after the coding
agent starts Guardian. Before then, the wrapper has recorded the baseline and
installed or registered the appropriate action, but no Guardian model work is
running.

## Lifecycle

### 1. Setup records cycle 0

Before the inner coding solver starts, `GuardianCodingAgent.setup()` records
the task repository's current `HEAD` in:

```text
/app/.guardian/base_commit
```

This commit is the repository supplied by the benchmark, not work produced by
the coding agent. Recording it before solver setup also prevents solver startup
hooks from being mistaken for a new coding cycle.

### 2. The wrapper exposes one lazy-start action

For Codex, the wrapper installs `guardian-start` and
`guardian-checkpoint` scripts under `/app/.guardian/bin` and describes them in
the coding prompt.

For an MCP-native solver, the wrapper registers the Guardian MCP server. The
server process may exist as an MCP child, but its watcher and model loop do not
start until the first `query_guardian` call.

### 3. The coding agent starts Guardian

The first Guardian action is idempotent:

- `guardian-start` acquires a lock, validates any recorded PID, and starts the
  Codex bridge only when no healthy bridge is already running.
- `query_guardian` starts the MCP watcher only when it has not already started.

Repeated actions reuse the same watcher.

### 4. Baseline calls do no model work

Immediately after startup, observed `HEAD` still equals `base_commit`. Guardian
reports a baseline or unchanged state and waits. It does not spend an LLM cycle
reviewing the benchmark's initial checkout.

A Codex checkpoint at this point exits with status 8 to distinguish “no
post-baseline commit exists” from a completed review.

### 5. Each later commit triggers one cycle

The watcher polls the task repository. When it observes a commit different
from both the baseline and the last processed commit, it runs one commit-scoped
Guardian cycle.

The first coding-agent commit is cycle 1, the next is cycle 2, and so on.
Working-tree edits alone do not trigger cycles.

### 6. Coding and review proceed concurrently

After a commit, Guardian can investigate it while the coding agent continues
working. During a running cycle:

- the MCP tool can return the most recently cached findings together with
  `cycle_running: true`;
- the Codex bridge keeps the last complete report available and exposes the
  running state in `status.json`.

Reports are advisory. The coding agent decides whether and how to respond.

### 7. Final checkpoint and teardown

The Codex prompt requires `guardian-checkpoint` before finalizing. The
checkpoint starts the bridge if necessary, verifies that a post-baseline commit
exists, waits for the report for the current `HEAD`, and prints its summary.

When the coding solver exits, the wrapper waits only for a bridge that was
actually started, stops it, and writes the Codex token summary. MCP-native
solver teardown is handled through its MCP process lifecycle.

## Solver transports

### MCP-native transport

`codeminer/guardian/mcp_server.py` exposes:

```text
query_guardian(
    hypothesis: str,
    region: list[str] | None
)
```

The query both ensures lazy startup and reads the current cached report.
`hypothesis` and `region` filter the returned findings; they do not create a
second cycle. The result includes lifecycle information as well as findings:

```text
commit
cycle_no
cycle_running
guardian_started
started_by_this_action
baseline_commit
observed_head
status
total_findings
returned_findings
findings
```

`status` is one of `running`, `baseline_unchanged`, `pending`, or `ready`.
Queries are traced to `/logs/agent/guardian_queries.jsonl`.

### Codex filesystem transport

Codex uses shell-visible actions because they integrate reliably with its
normal task workflow and make lifecycle state easy to inspect:

```text
/app/.guardian/bin/guardian-start
/app/.guardian/bin/guardian-checkpoint
```

The lazy-start script launches:

```text
python -m deepsweguardian.codex_bridge
```

The bridge writes atomically replaced outputs under:

```text
/app/.guardian/out/findings.md
/app/.guardian/out/findings.json
/app/.guardian/out/status.json
```

Per-cycle evidence and diagnostics are retained under:

```text
/logs/agent/guardian_episodes/<cycle>_<commit>/
```

The bridge runs with the mounted CodeMiner source at `/codeminer` and the
mounted environment at `/opt/codeminer-env`. Its PID, process marker, and
startup lock prevent duplicate bridge processes.

## Per-commit Guardian cycle

### L1: deterministic context

`codeminer/guardian/cycle.py` compiles the repository index, collects
deterministic change signals, opens Guardian memory for the selected arm, and
prepares prior/current disposable snapshots. L1 supplies grounded context; it
does not decide that a signal is a defect.

### L2: hypothesis controller

`codeminer/guardian/loop/` owns the outer reasoning cycle. Its tools let the
model:

- inspect signals and recall prior memory;
- search and read code;
- create and revise hypotheses;
- dispatch an L3 investigation;
- submit the cycle report.

Hypotheses end as `conjecture`, `supported`, `finding`, `refuted`, `deferred`,
or `resolved`. `resolved` means a claim that held on an earlier commit was fixed
by a later commit; it remains in trajectory memory but leaves the active
backlog. Claims promoted to `supported`, `finding`, or `refuted` require
either `source-valid` evidence for a closed-form claim grounded in exact source
or `probe-valid` evidence from an executed generic probe or classified test
result. L2 state is checkpointed so a partially completed cycle can be
diagnosed.

### L3: evidence investigator

`codeminer/guardian/investigator/` implements the current typed inner loop.
An investigation uses structured tasks, commands, process outcomes, test
outcomes, and an evidence ledger. This separates:

- whether a command ran successfully;
- whether a test passed, failed, or could not be classified;
- whether the result actually supports or refutes the hypothesis.

Before hypothesis-specific work, L3 performs a deterministic environment
prelude. A materialized writable disposable snapshot is required; pytest is an
optional capability. If a test recipe cannot be validated, L3 still exposes
source inspection and a generic model-authored Python probe, records the
capability loss, and marks the investigation degraded. Probes run against
disposable current and prior snapshots, never the coding agent's working
checkout.

L3 deliberately does not expose a growing catalog of defect-specific static
analyzers. The model chooses how to inspect source and may write one small
dependency-light probe when execution would add confidence. Runtime code owns
provenance, process classification, isolation, and evidence labels; the model
owns the semantic judgment.

`codeminer/guardian/investigator/runner.py` remains a compatibility path for
the older narrative investigator; the typed inner loop is the active design.

### Agent-loop sessions and context

Transport sessions follow agent ownership rather than cycle ownership. L2 keeps
one model session across its outer turns. Every L3 investigation opens its own
independent session and closes it with that investigation; multiple L3 agents
therefore never share conversational state merely because they belong to one
Guardian cycle.

L2 and L3 compose the same small mechanical layer in `guardian/llm/`: session
ownership, token-aware transcript accounting, coherent compaction, and typed
loop outcomes. They do not inherit from a common semantic loop. Their prompts,
tools, canonical state, evidence validation, and stopping policies remain
separate.

Within either kind of agent loop, messages and tool results append to a stable
conversation so provider prompt caching can reuse the preceding prefix. L2
externalizes an individual tool result only when it exceeds the inline
observation limit: the full result is stored with the episode, while the
conversation keeps a bounded preview and a `read_observation` reference. At the
model-token boundary the owning agent still requests one structured
working-memory summary, archives the full prior transcript, re-injects its
immutable frame and canonical state, resets only its own provider session, and
continues. L3 includes its tool schemas in request-size and grant estimates and
writes the actual terminal outcome to its checkpoint. Reports retain prompt,
cached-input, completion, and total-token counts alongside compaction events.

## Concurrency and consistency

Guardian is asynchronous, but commit processing is serialized:

- one transport and one watcher are active for a solver;
- one L2 response may contain several tool calls; contiguous filesystem and
  immutable-state reads execute concurrently, return in request order, and all
  retrieval, memory, mutation, L3, and submission calls remain barriers;
- lazy startup is lock-protected and idempotent;
- a commit is not scheduled again after it becomes the last processed commit;
- readers see the last complete cached report while a new cycle is running;
- filesystem reports are replaced atomically;
- each investigation receives commit-scoped snapshots and artifacts.

This gives the coding agent useful overlap without allowing two Guardian cycles
to mutate shared investigation state at once.

## Evaluation arms

The ablation keeps the coding task and solver fixed while changing Guardian
availability and memory:

| Arm | Pier agent | Guardian | Cross-cycle memory |
| --- | --- | --- | --- |
| A: solo | normal coding agent | absent | absent |
| B: memoryless | `GuardianCodingAgent` | lazy, per-commit | disabled/empty |
| C: memory | `GuardianCodingAgent` | lazy, per-commit | enabled |

- C − A measures the full Guardian contribution.
- C − B isolates the contribution of persistent memory.
- B − A measures the contribution of a second, evidence-seeking reviewer
  without memory.

The ablation launcher runs the solo arm and the selected Guardian arm; selecting
an arm does not implicitly schedule all three.

## Model and budget controls

Guardian controls retrieval, tool use, evidence collection, memory, and stopping.
For `guardian_model=codex:<model>`, model completions use the Codex
SDK/app-server when available and fall back to `codex exec`.

Two budget modes are supported:

- `--guardian-budget-tokens N` enforces a finite Guardian token budget.
- `--guardian-no-budget-limit` disables the token ceiling for profiling.

Unlimited mode is recorded as `budget_total: null`; token accounting remains
enabled so the run still reports actual outer-loop, inner-loop, and total use.
Turn and wall-clock limits remain safety boundaries even without a token limit.

## Implementation map

| Responsibility | Current implementation |
| --- | --- |
| Pier wrapper and solver routing | `deepsweguardian/guardian_coding_agent.py` |
| Codex lazy-start action | `deepsweguardian/lazy_start.py` |
| Codex final checkpoint | `deepsweguardian/checkpoint.py` |
| Codex watcher and report bridge | `deepsweguardian/codex_bridge.py` |
| MCP tool and watcher | `codeminer/guardian/mcp_server.py` |
| Commit-scoped cycle composition | `codeminer/guardian/cycle.py` |
| L2 outer loop | `codeminer/guardian/loop/` |
| L3 typed investigation | `codeminer/guardian/investigator/` |
| Persistent memory | `codeminer/guardian/memory/store.py` |

The concurrency and lazy-start paths are implemented. Reports and checkpoints
also carry `analysis_status`, `degraded`, backlog counts, and high-confidence
backlog counts. A zero-finding degraded report is not a clean review: the coding
agent must inspect the backlog and perform the missing validation before
finishing.

## Pier invocation shape

The exact task, model, mounts, and budget vary by experiment. A Guardian run
uses this integration shape:

```bash
pier run \
  -p deep-swe/tasks/<task> \
  --agent-import-path \
    deepsweguardian.guardian_coding_agent:GuardianCodingAgent \
  --ak solver=codex \
  --ak guardian_arm=memory \
  --ak guardian_model=codex:<model> \
  --ak guardian_budget_tokens=200000 \
  --mounts-json '<CodeMiner source, environment, and log mounts>' \
  -y
```

Use the normal Pier coding agent, without `GuardianCodingAgent`, for the solo
arm. Replace the finite budget setting with `guardian_no_budget_limit=true`
when measuring uncapped consumption.
