<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Stale-handoff A/B, protocol v5

Status: sealed on 2026-11-03 before any model call. Results are not in this
document yet.

Rounds v1 to v4 asked whether CodeNib's code graph helps an agent find its way
through code it reads for the first time. It did not measurably help: on
well-known Python projects, a strong model traced paths about as well with grep
([results v4](codegraph_ab_results_v4.md)). Round v5 asks a different question,
about work that spans sessions or agents.

An agent often continues from notes that another session wrote: a diagnosis, a
plan, a handoff. When the code changes between the two sessions, some of those
notes stop being true, and nothing tells the next agent which ones. Does it help
to mark the stale parts? And does a code graph mark them better than a check of
which files changed?

## Design

- **Task.** Each workflow is one fresh session that must fix a bug in a Python
  repository. It receives the user's bug report, an earlier agent's diagnosis of
  that bug, and a warning that other changes have been merged since the
  diagnosis was written, so parts of it may be out of date.
- **Arms.** The arms differ only in what follows the diagnosis:
  - **plain:** nothing more;
  - **file:** a file-level check, listing which of the files the diagnosis cites
    have changed since;
  - **graph:** a code-graph check. For every function the diagnosis cites, it
    says whether the function is unchanged, changed, moved or gone, and where it
    is now. It also lists every changed or new function within two calls of it,
    as a callee of a callee or a caller of a caller.
- **Checks.** Both checks are computed before the round from the two versions of
  the code, by one script (`stale_marks.py`). The graph check uses CodeNib's
  symbol graph with the pyrefly provider, from the v4 candidate build. Neither
  check is a tool: the agent sees fixed text, so the arms cannot differ in
  whether the agent chooses to call something. Every arm has the same native
  tools.
- **Cases.** The repositories and injected bugs of round v4, each with a new
  change merged after the diagnosis:
  - **Diagnosis.** The first v4 answer in workflow order whose grade met the
    mechanism and pinpoint criteria with no serious error. These are real agent
    outputs, used verbatim.
  - **Change.** Ordinary work by another contributor, such as a refactor, a
    small feature or a cleanup. It does not fix the bug, and the reported symptom is
    unchanged. It has one consequential part, after which carrying out the
    diagnosis's own fix literally no longer fixes the bug, and one
    behavior-neutral edit in a cited file that shifts cited line numbers.
  - **Kind.** A seeded draw assigns each repository one kind for the
    consequential part, six of each:
    - **local:** inside a function the diagnosis cites;
    - **remote:** only in functions the diagnosis does not cite, within two
      calls of one it does, preferably in files it does not cite.

    A repository that cannot take its assigned kind takes the other one, and
    the case records why. A repository that can take neither is dropped.
  - **Outcome.** Eleven cases were admitted: ten local and one remote (tox).
    - Five of the six repositories drawn as remote took the local kind instead,
      and httpie could take neither, so it was dropped.
    - The reason was the same each time: the diagnosis cites every function on
      the path from the entry point to the bug. No uncited function nearby can
      change the outcome of the diagnosis's fix without planting a second bug.
    - In several local cases the change copies the buggy expression into new
      code, such as a helper, a fast path or a moved rule. The literal fix then
      lands in code the entry point no longer reaches.
  - **Authors.** Cases were written by agents that read code with grep and an
    editor only, with no code-graph, call-graph or LSP tool. Admission does not
    depend on what either check reports.
- **Repetitions.** Each arm runs each case three times, for 33 sessions per arm
  and 99 in all. A fixed seed sets the case order and the arm order within each
  case and repetition. Up to three sessions run at once.
- **Agent.** Claude Agent SDK 0.2.174, Claude Code 2.1.316, `claude-opus-5-5` at
  medium effort, native tools only, 40 turns, 600 s and $3 per session. The
  agent sees a one-commit snapshot of the current code with no history.

## Admission

Each case is admitted before any model call, with no model in the loop, when all
of these hold inside the case's image without network:

- the hidden test fails on the current code;
- the reference fix passes the hidden test and the listed project tests;
- the diagnosis's own fix, carried out literally on the current code, fails the
  hidden test.

The hidden test checks only what the bug report states: the reported cases now
behave correctly, and the inputs the report lists as unaffected still behave as
stated. One prepare-only workflow per case confirms the environment and records
its identity.

## Grading

A fix is accepted when the session ends normally and, with the session's
workspace changes applied to the current code, the hidden test passes and every
listed project test passes. The listed test files are restored first, so a fix
cannot pass by editing them. Grading is automatic; there is no grader judgment.

## Decision rule

The primary outcome is accepted fixes per arm, out of 33. A check helps when it
has at least 5 more accepted fixes than plain, and it wins more cases than it
loses against plain, counting a case's three repetitions.

- If plain accepts at least eight in nine of its sessions (30 of 33), stale
  notes did not cause enough errors to measure. The result is no room for a
  staleness check in these cases.
- If neither check helps, the result is no demonstrated benefit.
- If the graph check helps and has at least 4 more accepted fixes than the file
  check overall, and no fewer in local cases, the result is that the code graph
  is needed. When at least three cases are remote, it must also have at least 3
  more in remote cases.
- If a check helps but the graph condition fails, the result is that a
  file-level check is enough.

Secondary outcomes:

- accepted fixes per arm and kind;
- session time, turns, model tokens and SDK estimate;
- whether the graph check named the consequential change in each case;
- fixes that left the hidden test failing the way the literal stale fix does.

The planned SDK estimate is about $25. No new workflow starts once the observed
estimate reaches $35.

## Limits

- The cases are built so that the stale part matters and lies within reach of
  each check. They measure what a check does when staleness occurs, not how
  often it occurs in real work.
- With one remote case, the round can hardly show the graph finding a change
  that the file check misses. It mainly compares the precision of the two
  checks.
- The agent has no git history. In practice an agent could diff against the
  commit a note was written at, which this round does not test.

## Commitments

All hashes are SHA-256.

| Input | Hash |
|---|---|
| Policy (`ab-policy-v5.json`) | `245add5c5299a0ffc72d00b571a8977fc0ab2b5d0cece4b5b3ef725dc8c2c040` |
| Agent-visible tasks (`ab-tasks-v5.json`) | `c81c7d8e5138930bfa172a7453f72eefb3e851ab04ef61392c7581f7535b209a` |
| Staleness checks (`stale_marks.py`) | `5d4694c60b4a529ab441a854735e9d343d98524b3e9b373b912607bceb4d4788` |
| Execution binding (every input file, admission receipt, image and source head) | `2115188fe0cc2dd131fca3cf63219b012006c9a2e0c1fade01183c4a2fe7b6d1` |

The cases, diagnoses, changes, hidden tests and checks are published with the
results.
