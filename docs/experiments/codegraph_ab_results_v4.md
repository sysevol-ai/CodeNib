<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Code-graph diagnosis A/B, results v4

CodeNib showed no demonstrated benefit on diagnosis tasks built around the path
from an entry point to an injected bug. It accepted 21 of 36 diagnoses against
17 for native tools, and it did better in 4 repositories and worse in none.
That falls one diagnosis short of the pre-registered margin of 5. Path tracing
itself, the ability this round targeted, was almost identical: native tools
named every required step in 27 diagnoses and CodeNib in 28. The design is in
[protocol v4](codegraph_ab_protocol_v4.md). This page reports the one planned
round of 72 workflows, run on 2026-11-03 from 05:17 to 06:06 UTC, at an SDK
cost estimate of $10.64.

## Outcome per arm

A diagnosis is accepted when it meets all four rubric criteria and makes no
serious error: the path (D1), the mechanism (D2), the faulty line (D3) and why
the unaffected inputs are unaffected (D4). The looser count also accepts answers
whose only gaps are elements the grader recorded as open to a looser reading.

| Arm | Accepted, strict (of 36) | Accepted, looser | D1 path | D2 | D3 | D4 | Serious errors |
|---|---|---|---|---|---|---|---|
| native | 17 | 30 | 27 | 33 | 36 | 22 | 0 |
| codenib | 21 | 32 | 28 | 35 | 36 | 26 | 0 |

The decision rule needed all three of these:

1. At least 5 more strict acceptances. CodeNib had 4 more, so this failed.
2. More repositories won than lost. CodeNib won 4 and lost none, so this held.
3. No more serious errors. Neither arm had any, so this held.

The fallback reading needed 6 more D1 passes and got 1. The result is
therefore no demonstrated benefit.

Every diagnosis found the faulty line (D3 in all 72). The criteria that decided
acceptance were the full path with call sites and the scope of the bug. CodeNib's
4 extra acceptances came mostly from D4 (scope) and D2 (mechanism), not from the
path.

## Outcome per repository

Accepted diagnoses of three per arm, strict reading; D1 in parentheses.

| Repository | native | codenib |
|---|---|---|
| alembic | 1 (2) | 1 (3) |
| black | 3 (3) | 3 (3) |
| cookiecutter | 0 (2) | 1 (3) |
| httpie | 0 (0) | 0 (0) |
| isort | 2 (2) | 2 (2) |
| jinja2 | 3 (3) | 3 (3) |
| marshmallow | 1 (3) | 2 (3) |
| mkdocs | 2 (3) | 3 (3) |
| pip-tools | 1 (3) | 1 (3) |
| rich | 3 (3) | 3 (3) |
| tox | 0 (2) | 0 (0) |
| typer | 1 (1) | 2 (2) |

## What CodeNib delivered

- **Calls.** The agent called `explore_context` in 27 of 36 diagnoses, as the
  first tool in 26. Every result carried graph relationships and call chains.
  Diagnoses with a call were accepted 17 of 27 times, and those without 4 of 9.
  The agent chose when to call, so this split is not a causal estimate.
- **Chains.** Only 9 of the 27 results had a call chain that reached the faulty
  function.
  - The agent usually listed its symbols from the entry point down, and the
    chains answer how the first symbol is reached. For marshmallow, the first
    symbol was `Schema.dump` itself, so the chains showed the entry point's
    callers rather than the path down to the bug.
  - The agent often named most of the path in its first call. These are
    well-known projects, and the model appears to know their structure.
- **Graph build.** pyrefly built every graph during setup, in 7–17 s per workflow.

## Time and cost

| Arm | Workflow wall time | Of which setup | Mean diagnosis session | Model tokens | SDK estimate |
|---|---|---|---|---|---|
| native | 1156 s | 0 s | 30.8 s | 2.50 M | $5.13 |
| codenib | 1622 s | 421 s | 28.5 s | 2.73 M | $5.50 |

The diagnosis sessions themselves took about the same time. CodeNib's extra
wall time is graph setup, about 12 s per workflow, which a real repository pays
once rather than per question.

## How to read this round

- **Noise.** Four of the twelve repositories differ, all in CodeNib's favor, by
  one diagnosis each. A sign test over those four gives p = 0.125, and the rule's
  margin was set to need more.
- **The path was not the bottleneck.** Both arms named every required step in
  about three quarters of the diagnoses. On these projects the model traced
  paths well without a graph. httpie defeated both arms on the same hop, a call
  passed in as an argument (`main_program`).
- **Grader calibration.** One grader graded all six answers for a repository,
  so the comparison within a repository is fair. Several graders noted
  line-citation and scope readings that could move whole repositories. The
  looser count is reported for that reason.

## Evidence

[`codegraph_ab_v4.tar.gz`](codegraph_ab_v4.tar.gz) (SHA-256
`49e16698501ac834039b1623fbeb8e22f046b2c4f7afc808007c735661e7716e`) contains:

- the policy, tasks, case table, binding and controller receipt;
- each workflow's receipt and admission receipt;
- the private cases, now public: rubrics, regression patches, repro scripts,
  case notes and validation logs;
- the blinded grades with their strict and looser decisions, and the letter
  mapping;
- per-workflow metrics and outcomes.
