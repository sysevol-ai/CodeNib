<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Code-graph tool A/B, results v1

CodeNib showed no demonstrated benefit in this round. It accepted 8 of 15
tasks. Serena and codegraph each accepted 9, and native tools alone
accepted 7. CodeNib was also the slowest arm. The design, arms and decision
rule were fixed in advance in
[the protocol](codegraph_ab_protocol_v1.md). This page reports the one
planned round of 20 workflows, run on 2026-11-02 from 21:15 to 22:20 UTC.

The more useful finding is about what the agent received from CodeNib. The
agent called CodeNib in 11 of 15 tasks, and in each of those tasks CodeNib
was its first tool. But only 3 of the 12 results contained graph
relationships. The rest came from source search alone, because the graph was
still being built or had failed. This round therefore mostly measured
CodeNib's source search, not its graph.

## Outcome per arm

A task is accepted when the agent's session ends normally and the answer
passes grading. Repairs must pass every private test and every selected
existing test. Diagnoses and reviews must meet every rubric criterion with no
serious error.

| Arm | Accepted (of 15) | Diagnose / repair / review | Serious errors | Wall time | With failure penalty | Model tokens | SDK estimate |
|---|---|---|---|---|---|---|---|
| native | 7 | 1 / 4 / 2 | 2 | 879 s | 3147 s | 3.90 M | $4.16 |
| serena | 9 | 1 / 5 / 3 | 1 | 978 s | 2610 s | 5.30 M | $4.60 |
| codegraph | 9 | 0 / 5 / 4 | 2 | 978 s | 2642 s | 4.45 M | $5.02 |
| codenib | 8 | 0 / 5 / 3 | 0 | 1000 s | 2816 s | 3.99 M | $4.36 |

The failure penalty adds, for each failed or unattempted task, the time left
before that task's deadline, as the protocol specifies. Tokens are the
protocol's normalized count: input, cache creation, cache reads and output,
summed once per session.

The decision rule needed CodeNib to accept at least as many tasks as the best
other arm (9) and to finish at least 20% faster. It accepted one task fewer
and was 2% slower than Serena and codegraph, so the rule is not met.

On the four repositories where every arm finished all three tasks (all but
litestar), CodeNib used the fewest tokens: 2.83 M against 3.07 M for native,
3.66 M for codegraph and 4.34 M for Serena. Its wall time was the longest:
799 s against 740 s, 751 s and 785 s.

## Outcome per task

`ok` is accepted, `fail` is not accepted, `serious` is a serious error as the
rubric defines it, and `none` is a task that was not attempted.

| Repository | native | serena | codegraph | codenib |
|---|---|---|---|---|
| beets | ok / ok / ok | ok / ok / ok | fail / ok / ok | fail / ok / ok |
| sqlfluff | fail / ok / ok | serious / ok / ok | serious / ok / ok | fail / ok / ok |
| strawberry | fail / ok / fail | fail / ok / fail | fail / ok / ok | fail / ok / fail |
| pdm | fail / ok / fail | fail / ok / ok | fail / ok / ok | fail / ok / ok |
| litestar | serious / serious / none | fail / ok / none | serious / ok / none | fail / ok / fail |

Each cell lists diagnose / repair / review.

## How much the arms differ

The differences between arms are one or two tasks, which is within this
round's noise. The Serena arm shows how large that noise is. Serena's server
was called once in 14 tasks, so that arm was in practice a second native arm.
It still accepted two more tasks than native.

Most of the spread comes from the diagnosis rubric. Only 2 of 20 diagnoses
were accepted, both on beets. Most of the others located the right cause but
left out one hop of the call chain that the rubric required. Every arm
repaired every regression except one: native's litestar repair hit the
24-turn limit with one private test still failing.

## What each server delivered

| Arm | Calls | Tasks with a call | Mean characters per result |
|---|---|---|---|
| serena | 1 | 1 of 14 | (one edit call) |
| codegraph | 17 | 11 of 14 | 22,800 |
| codenib | 12 | 11 of 15 | 10,400 |

The agent never called CodeNib on sqlfluff. Of CodeNib's 12 results:

- 7 said the graph was still being prepared, so the answer came from source
  search only. Four of these were the first call of a diagnosis session.
  The other three came in the repair-and-review session while the graph was
  being built again.
- 1 reported that relationships were unavailable, because strawberry's graph
  build failed (first defect below).
- 1 was a tool error after the agent edited strawberry (second defect below).
- 3, all during repairs on pdm and litestar, contained graph relationships.

## Defects found

Both defects were reproduced after the round on a copy of the strawberry
source, in the same image with the same limits.

1. **Python 3.12 generic syntax rejects the whole graph.** scip-python 0.6.6
   gives the type parameter in `class Edge[T]:` (and the name in
   `type Alias = ...`) the owner's own symbol. The decoder then builds a class
   that contains itself, and CodeNib's containment check rejects the entire
   graph. Strawberry has 81 such constructs, all under `tests/python_312/`;
   the other repositories have none. Without that directory the build
   succeeds. The failure is deterministic and unrelated to memory or process
   limits.
2. **An edit after a failed build breaks every later call.** When the
   background build fails, it rewrites the index manifest. The next call
   reloads, finds that the source was edited, and raises a plain
   `RuntimeError` rather than the stale-source error that triggers a refresh.
   The MCP layer reports it as "Error executing tool explore_context", and
   every later call in the connection fails the same way.

## Harness problems

- In litestar, the Serena and codegraph reviews were not attempted. The
  agents' test runs left root-owned `.hypothesis` files in the workspace, and
  the harness crashed while snapshotting the source after the repair. Both
  repairs were rebuilt from the kept workspace with the harness's own diff
  rule and graded the same way. The two lost reviews count against Serena and
  codegraph, not CodeNib, so they do not change the decision.
- Admission checked one lookup per arm but did not wait for CodeNib's graph
  to finish building, so it missed the strawberry failure.
- Two reviews (native and Serena on beets) made temporary edits that they
  reverted before finishing. The source after each review matched the source
  before it byte for byte, so neither counts as an edit.

## Grading

Repairs were run in each repository's image against the private and selected
tests. One grader per repository then reviewed every diagnosis and review
against its rubric and the exact source. The graders saw the answers under
shuffled letters, without arm names or tool traces; the mapping was applied
afterwards. They held criteria strictly and recorded the calls that decided
borderline answers.

Those calls matter, because the ranking by accepted tasks depends on them.
The graders named five answers that a looser reading of one criterion
would accept. Three were CodeNib's (the litestar and sqlfluff diagnoses and
the strawberry review), one was codegraph's and one was native's, and native
had one more that needed two criteria loosened. Under that reading, CodeNib
would accept 11 tasks, codegraph 10, Serena 9 and native 8 or 9. The rule
would still not be met, because CodeNib was slower, not 20% faster.

## Evidence

[`codegraph_ab_v1.tar.gz`](codegraph_ab_v1.tar.gz) (SHA-256
`2bbd3a021f43c0ed6cdade917c06b8134d9f828d3e11ef0ad7f127944dae5f05`) contains:

- the private grading material, byte-identical to the files the sealed
  binding lists;
- the policy, tasks, binding and controller receipt;
- each workflow's receipt and captured repair patch;
- repair test results, blinded rubric grades and the letter mapping;
- per-workflow metrics.

The raw SDK traces are kept outside the repository.
