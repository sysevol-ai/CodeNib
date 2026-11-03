<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Code-graph tool A/B, results v3

For the first time CodeNib accepted the most tasks, 8 of 15 against 7 for
every other arm, with no serious error. It still shows no demonstrated
benefit under the pre-registered rule, because it was again the slowest arm.
Building the graph during setup took 226 s across the five repositories.
Call chains, the one change in this round, did not change how many diagnoses
were accepted under the strict reading. They did put a step into one
diagnosis that every other arm missed. The design is in
[protocol v3](codegraph_ab_protocol_v3.md). This page reports the one planned
round of 20 workflows, run on 2026-11-03 from 02:08 to 03:24 UTC, at an SDK
cost estimate of $18.71.

## Outcome per arm

Acceptance follows v1 and v2: a task is accepted when the session ends
normally and the answer passes grading. The strict count applies every
rubric element. The looser count also accepts answers whose only gaps are
elements the grader recorded as open to a looser reading.

| Arm | Accepted, strict (of 15) | Diagnose / repair / review | Accepted, looser | Looser diagnoses | Serious errors | Wall time | Of which setup | Model tokens | SDK estimate |
|---|---|---|---|---|---|---|---|---|---|
| native | 7 | 1 / 4 / 2 | 10 | 3 | 1 | 1074 s | 0 s | 3.47 M | $3.88 |
| serena | 7 | 1 / 4 / 2 | 12 | 4 | 0 | 1088 s | 23 s | 5.54 M | $4.82 |
| codegraph | 7 | 1 / 4 / 2 | 13 | 4 | 2 | 1122 s | 49 s | 5.42 M | $5.66 |
| codenib | 8 | 1 / 5 / 2 | 14 | 5 | 0 | 1217 s | 226 s | 3.91 M | $4.36 |

The decision rule needed CodeNib to accept at least as many tasks as the best
other arm, add no serious error, finish at least 20% faster than that arm and
use at most 10% more tokens. CodeNib met the first two. It was slower than
every other arm, 13% slower than native tools, and it used 13% more tokens
than native tools. The rule is therefore not met.

Without setup, CodeNib's workflows took 991 s, 8% less than native tools.
That is short of 20%, and setup is part of the measured time.

## Outcome per task

Each cell lists diagnose / repair / review under the strict reading.

| Repository | native | serena | codegraph | codenib |
|---|---|---|---|---|
| beets | ok / ok / ok | ok / ok / ok | ok / ok / ok | ok / ok / ok |
| sqlfluff | fail / ok / ok | fail / fail / none | serious / ok / ok | fail / ok / ok |
| strawberry | fail / ok / fail | fail / ok / ok | fail / ok / fail | fail / ok / fail |
| pdm | fail / ok / fail | fail / ok / fail | fail / ok / fail | fail / ok / fail |
| litestar | fail / serious / none | fail / ok / fail | fail / serious / fail | fail / ok / none |

CodeNib's lead of one task comes from litestar. Native's and codegraph's
repairs both made the inner layer win for a shared parameter. That reverses
existing precedence, which the rubric counts as a serious error, and both
failed the private precedence test. CodeNib's repair kept precedence and
passed. Serena's sqlfluff
repair passed every test but counts as failed, because its session hit the
24-turn limit and so its review was not attempted. Two litestar reviews were
lost to a harness fault, described below.

## Did call chains help diagnosis?

Only beets diagnoses were accepted under the strict reading, in every arm.
The other four repositories show what CodeNib's chains did in each diagnosis:

| Repository | Chain CodeNib returned | Rubric step the chain covers | CodeNib's diagnosis |
|---|---|---|---|
| strawberry | From `from_resolver` through `extension_resolver` and `get_arguments` to `convert_argument` | The resolver hop (D1) | Named the hop. It was the only diagnosis of the four to do so. It failed on an unrelated element. |
| sqlfluff | Through `LintedFile.get_violations` to `ignore_masked_violations` | Where the mask is applied (C4) | Named `ignore_masked_violations` but not `get_violations`, so C4 failed |
| pdm | None: the agent did not call CodeNib in this task | From the command handler through `check_lockfile` (C1) | Missed the hop, like every other arm |
| litestar | None: the symbol the agent asked about has no static references, so there is no chain | — | Missed the request-time step, like every other arm |

So the chain worked as intended once, when the agent read it and used it. In
the other three tasks the agent ignored the chain, made no call, or asked
about a symbol nothing calls. Under the looser reading CodeNib accepted all
five diagnoses, against three for native tools and four for each other arm.

## What each server delivered

| Arm | Calls | Tasks with a call | Results with graph relationships | Results with call chains |
|---|---|---|---|---|
| serena | 0 | 0 of 14 | — | — |
| codegraph | 20 | 13 of 15 | — | — |
| codenib | 10 | 10 of 14 | 8 | 6 |

CodeNib's two results without relationships came in review sessions after
the agent's edit, while the graph was being rebuilt. No CodeNib call returned
an error. Serena's tools were never called, as in v2.

## Harness problems

- **Lost litestar reviews.** v2 left `.hypothesis` caches out of captured
  patches by naming them as an excluded path. When that ignored directory
  exists, `git add` rejects the path and exits with an error. Native's and
  CodeNib's litestar test runs created it, so capturing each repair failed
  after the session had ended normally. Neither review was attempted, and
  both count as failed. Both repairs were rebuilt from the kept workspace
  with the same diff rule and graded the same way. Even if both reviews had
  been accepted, CodeNib would lead native 9 to 8, so the comparison would not
  change.
- Serena's sqlfluff repair session hit the 24-turn limit. This was a
  workflow outcome, not a harness fault.

## How to read this round

- **Noise.** The arms differ by one task under the strict count, and that
  task is a repair, not a diagnosis. Under the looser count CodeNib leads by
  one. Serena's arm again made no server calls, so it is in practice a second
  native arm. It differed from native by two tasks under the looser count.
- **Grader calibration.** The same grader graded all four arms in a
  repository, so the comparison within this round is fair. Graders differed
  between rounds. For example, the v3 and v2 beets graders accepted every
  answer, and the v1 beets grader rejected two. Absolute counts should not be
  compared across rounds.
- **Cost of the graph.** Setup still decides the timing. Without it CodeNib
  would have been the fastest arm, but by 8%, not 20%. After an edit the
  graph is rebuilt, so reviews mostly ran without it.

## Evidence

[`codegraph_ab_v3.tar.gz`](codegraph_ab_v3.tar.gz) (SHA-256
`c1482aab690c9b9c8f639e2fd0796f38d2232cddbbdac0f7e96dd2c0b41b6222`) contains:

- the policy, tasks, binding and controller receipt;
- each workflow's receipt and repair patch, including the two rebuilt
  litestar patches;
- repair test results, the blinded rubric grades with their strict and looser
  decisions, and the letter mapping;
- per-workflow metrics and outcomes.

The private grading material is the v1 archive's, unchanged.
