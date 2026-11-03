<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Code-graph tool A/B, results v2

CodeNib again showed no demonstrated benefit. This time the agent did get the
graph: 9 of CodeNib's 10 results contained graph relationships, against 3 of
12 in v1. Even so, the CodeNib arm accepted no more tasks than native tools
alone and was the slowest arm, because building the graph during setup added
228 s across the five repositories. The design is in
[protocol v2](codegraph_ab_protocol_v2.md). This page reports the one planned
round of 20 workflows, run on 2026-11-02 from 23:24 to 00:37 UTC, at an SDK
cost estimate of $18.82.

## Outcome per arm

Acceptance follows v1: a task is accepted when the session ends normally and
the answer passes grading. The strict count applies every rubric element. The
looser count also accepts answers whose only gaps are elements the grader
recorded as open to a looser reading.

| Arm | Accepted, strict (of 15) | Diagnose / repair / review | Accepted, looser | Serious errors | Wall time | Of which setup | Model tokens | SDK estimate |
|---|---|---|---|---|---|---|---|---|
| native | 9 | 2 / 5 / 2 | 15 | 0 | 1059 s | 0 s | 3.93 M | $4.40 |
| serena | 8 | 1 / 5 / 2 | 14 | 1 | 1009 s | 21 s | 5.30 M | $4.65 |
| codegraph | 8 | 1 / 5 / 2 | 12 | 2 | 1007 s | 49 s | 4.72 M | $5.30 |
| codenib | 8 | 1 / 4 / 3 | 13 | 0 | 1271 s | 228 s | 4.22 M | $4.47 |

The decision rule needed CodeNib to accept at least as many tasks as the best
other arm and to finish at least 20% faster. Native tools accepted 9 tasks
to CodeNib's 8. CodeNib was 26% slower than the fastest arm, so the rule is
not met. Under the looser reading native tools still lead with 15.

Without setup, CodeNib's workflows took 1043 s, in line with the other arms.
The graph therefore did not shorten the tasks, and building it made the arm
slower overall.

## Outcome per task

Each cell lists diagnose / repair / review under the strict reading.

| Repository | native | serena | codegraph | codenib |
|---|---|---|---|---|
| beets | ok / ok / ok | ok / ok / ok | ok / ok / ok | ok / ok / ok |
| sqlfluff | ok / ok / ok | serious / ok / ok | serious / ok / ok | fail / ok / ok |
| strawberry | fail / ok / fail | fail / ok / fail | fail / ok / serious | fail / ok / fail |
| pdm | fail / ok / fail | fail / ok / fail | fail / ok / fail | fail / ok / ok |
| litestar | fail / ok / fail | fail / ok / fail | fail / ok / fail | fail / fail / none |

Every repair passed all private and selected tests. CodeNib's litestar repair
still counts as failed because its session hit the 24-turn limit, so its
review was not attempted. No other session ended abnormally, and the harness
problems from v1 did not recur.

## What each server delivered

| Arm | Calls | Tasks with a call | Results with graph relationships |
|---|---|---|---|
| serena | 0 | 0 of 15 | — |
| codegraph | 19 | 14 of 15 | — |
| codenib | 10 | 10 of 14 | 9 |

CodeNib's one result without relationships came in a review session after
the agent's edit, while the graph was being rebuilt. No CodeNib call returned
an error. The agent called CodeNib's server first in every task where it
called it, and exactly once per task. Serena's tools were never called.

## How to read this round

- **Noise.** Arms differ by one task under the strict count and by up to
  three under the looser one. Serena's arm made no server calls at all, so
  it is in practice a second native arm. It differed from native by one task.
- **Grader calibration.** The same grader graded all four arms in a
  repository, so the comparison within this round is fair. Graders differed
  between rounds, though. For example, the v2 beets grader accepted every
  answer, and the v1 beets grader rejected two. Absolute counts should not be
  compared across rounds.
- **Cost of the graph.** CodeNib's setup ran 27–71 s per repository. The
  largest single part, 15–25 s, is scip-python's whole-project type analysis.
  The rest is the toolchain download in a fresh container, BM25, identity
  recovery and decoding.

## Evidence

[`codegraph_ab_v2.tar.gz`](codegraph_ab_v2.tar.gz) (SHA-256
`20e2b9c65397299941be6042186ef2d05125e70afbfac2fc7cb56a7417f24913`) contains:

- the policy, tasks, binding and controller receipt;
- each workflow's receipt and captured repair patch;
- repair test results, the blinded rubric grades with their strict and looser
  decisions, and the letter mapping;
- per-workflow metrics and outcomes.

The private grading material is the v1 archive's, unchanged.
