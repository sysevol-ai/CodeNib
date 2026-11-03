<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Code-graph diagnosis A/B, protocol v4

Status: sealed on 2026-11-03 before any model call. Results are not in this
document yet.

Rounds v1 to v3 found no demonstrated benefit from CodeNib's graph, but they
also showed where a benefit could come from. Under the strict reading, every arm
accepted the same single diagnosis in each round. Most rejected diagnoses had
found the faulty function but left out a step on the way to it from the
command or call the user made. In v3 CodeNib's call chains supplied such a step
once, and in the other repositories the chain was missing or unused
([results v3](codegraph_ab_results_v3.md)). Round v4 tests that one ability
directly. Does CodeNib lead the same agent to more accepted diagnoses when the
task asks for the code path from an entry point to an injected bug?

## Design

- **Arms.** Native tools alone, and native tools plus CodeNib's
  `explore_context`. Serena and codegraph are dropped: Serena's tools were never
  called in v2 or v3, and this round asks about CodeNib only.
- **Repositories.** Twelve Python repositories, none used in v1 to v3 or while
  tuning the tools. Each is pinned at a recent release tag:

  | Repository | Entry point type |
  |---|---|
  | alembic | command line |
  | black | command line |
  | cookiecutter | command line |
  | httpie | command line |
  | isort | command line |
  | jinja2 | public API |
  | marshmallow | public API |
  | mkdocs | command line |
  | pip-tools | command line |
  | rich | public API |
  | tox | command line |
  | typer | command line |

- **Cases.** Each repository has one injected regression of one to three
  lines. The faulty function is at least three project calls away from the
  entry point. The prompt describes the symptom through the entry point, lists
  which inputs are affected and which are not, and asks for the code path from
  the entry point to the responsible code, naming each function and where it
  calls the next. The agent sees a one-commit snapshot with no history.
  - Cases were written by agents that read code with grep and an editor only,
    with no code-graph, call-graph or LSP tool. Path choice therefore does not
    depend on what CodeNib can find.
  - Each case has a script that shows the symptom on the regressed snapshot and
    passes on the upstream release, run in the case's image.
- **Repetitions.** Each arm runs each case three times, in fresh sessions, for
  36 diagnoses per arm. A fixed seed sets the arm order within each repetition.
- **Agent.** Unchanged from v3: Claude Agent SDK 0.2.174, Claude Code 2.1.316,
  `claude-opus-5-5` at medium effort, 24 turns, a 300 s limit per diagnosis, and
  no forced tool use.
- **CodeNib build.** The candidate wheel merges two pull requests:
  - #970: call-chain fallbacks and wording that names call chains for
    diagnosis;
  - #968: pyrefly as the default Python graph provider, including its scope
    fix.

  The graph is built during setup with `codenib codegraph init`, as in v2 and
  v3.
- **Admission.** For each repository and arm, one model-free run prepares the
  workflow. CodeNib's first graph query on the entry point must deliver its
  definition with graph relationships, and the graph must be ready. Call chains
  are recorded but not required, so admission cannot select cases by whether
  CodeNib finds their path.
  - The black probe is the next step on the path, `reformat_one`, because
    black's test data defines `main` many times.
  - The first admission attempt probed black with `main`, and that probe failed
    on the ambiguous name. All 24 admissions were rerun with the final policy
    before any model call.

## Grading

One blinded grader per repository grades its six answers (two arms, three
repetitions) under shuffled letters. Graders see no arm names, traces or tool
output. Each answer is checked against the regressed source and a private rubric
with four mandatory criteria:

- **D1, path:** each step from the entry point to the faulty function, with the
  call site.
- **D2, mechanism:** what the faulty code does and how that produces the
  symptom.
- **D3, pinpoint:** the faulty line and the correct behavior.
- **D4, scope:** why each listed unaffected input is unaffected.

A diagnosis is accepted when it meets all four criteria and makes no serious
error, meaning a wrong root cause or an edit to the source. Graders also record
which readings decided borderline answers, so a looser count is reported next
to the strict one, as in earlier rounds.

## Decision rule

The primary outcome is diagnoses accepted under the strict reading per arm, out
of 36. CodeNib shows a diagnosis benefit only if all three of these hold:

1. It has at least 5 more strictly accepted diagnoses than native tools.
2. In more repositories than the reverse, it accepts more of its three
   repetitions than native tools.
3. It has no more serious errors than native tools.

If these fail but CodeNib meets the path criterion D1 at least 6 more times,
the result is reported as call chains improving path tracing without a
demonstrated acceptance benefit. Anything else is reported as no demonstrated
benefit.

Secondary outcomes:

- the looser count;
- D1 met per arm;
- workflow wall time, including CodeNib's setup;
- session time, model tokens and SDK estimate;
- `explore_context` calls and the first tool used;
- results with graph relationships and with call chains.

The planned SDK estimate is about $16. No new workflow starts once the observed
estimate reaches $25.

## Commitments

All hashes are SHA-256.

| Input | Hash |
|---|---|
| Policy (`ab-policy-v4.json`) | `fefada47bd5e644ca939da8221017c4ebeb8ecd4711ab61c1926c3677cd3a6a6` |
| Agent-visible tasks (`ab-tasks-v4.json`) | `96e7af7650c08e5ebe9fef967df7e2ff140f0dfcf4e10998e80d06431ecf4ecc` |
| Execution binding (every input file, admission receipt, image and source head) | `7a48e4dd6faf095c6c25dd7d32a9b4cf729de8ce12add7b1309aa001e33debbc` |
| CodeNib candidate wheel (local cibuildwheel build of #970 head `60ebb381` merged with #968 head `302b9d09`) | `20e4e21245e8c8f9d89a3571773ec8770896483385719aa4ef25d8b78e2a3aa4` |

The private cases, rubrics and repro scripts are published with the results.
