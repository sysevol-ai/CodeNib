<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Stale-handoff A/B, results v5

Marking which parts of an earlier diagnosis had gone stale did not help. Every
session in every arm fixed its bug: 33 of 33 without any check, with the
file-level check and with the code-graph check. The agent was told that the
diagnosis might be out of date. With that warning it checked the current code
before editing and found the stale parts on its own, so stale notes caused no
errors that a check could prevent. The design is in
[protocol v5](stale_handoff_ab_protocol_v5.md). This page reports the 99
workflows run on 2026-11-03 from 21:37 to 22:37 UTC, at an SDK cost estimate of
$31.87.

## Outcome per arm

A fix is accepted when the session ends normally and, with its changes applied,
the hidden test and the listed project tests pass.

| Arm | Accepted (of 33) | Local cases (of 30) | Remote case (of 3) |
|---|---|---|---|
| plain | 33 | 30 | 3 |
| file | 33 | 30 | 3 |
| graph | 33 | 30 | 3 |

The decision rule's first condition applies: plain accepted at least 30 of 33,
so stale notes did not cause enough errors to measure. The result is no room
for a staleness check in these cases. Neither check could meet the margin
anyway: each needed 5 more accepted fixes than plain and had none.

The protocol's [post-run interpretation erratum](
stale_handoff_ab_protocol_v5.md#post-run-interpretation-erratum-2026-11-04)
corrects a separate branch that could wrongly call a file-level check sufficient.
It does not affect this ceiling outcome, the frozen policy, or any session grade.

Every one of the 11 cases was fixed in all nine of its sessions. No fix failed,
so no fix failed the way the diagnosis's literal fix does.

## What the checks said

- **Graph check.** It named the consequential change in all 11 cases: the
  changed function, or the new function that now holds the buggy code, with its
  current location.
- **File check.** It flagged every cited file that changed, including files
  whose only change was the behavior-neutral edit. That happened in six local
  cases. In tox, the one remote case, it flagged only such a file, because the
  consequential change was in a file the diagnosis does not cite.
- **The agent without a check.** Sessions in the plain arm found the stale parts
  themselves. In mkdocs, for example, the diagnosis blamed a function that the
  entry point no longer calls; a plain session reported that the call now goes
  through two new functions with the same bug, and fixed both.

The graph check was the more precise of the two, but the agent did not need the
precision to get the fix right.

## Time and cost

| Arm | Median session | Mean session | Median turns | Model tokens | SDK estimate |
|---|---|---|---|---|---|
| plain | 63 s | 110 s | 12 | 4.60 M | $10.82 |
| file | 78 s | 93 s | 12 | 4.48 M | $10.41 |
| graph | 77 s | 102 s | 11 | 4.58 M | $10.63 |

The checks did not make sessions shorter or cheaper. The differences are small
and point in different directions for different measures.

## How to read this round

- **The warning may have done the work.** The plain arm was told the diagnosis
  might be out of date, so it was not a blind handoff. Real notes usually carry
  no such warning, and this round does not test an agent that takes stale notes
  as current.
- **The cases were small.** Each change touched a few functions near the bug,
  in projects the model likely knows. A longer handoff with many claims, a
  larger change or a less familiar codebase could leave more room.
- **It agrees with rounds v1 to v4.** In those rounds, a strong model on
  well-known Python projects traced code about as well with grep as with the
  graph. Here it rechecked a diagnosis about as well without marks as with them.

## Deviations

- **First attempt.** It was stopped after 17 workflows because black's test
  suite hung under the container's process limit. The second attempt raised the
  limit and ran all 99 workflows; see the protocol's
  [Deviation](stale_handoff_ab_protocol_v5.md#deviation) section. Two first-attempt
  cookiecutter patches had already been graded to debug the grader. The repeated
  cohort is therefore not an independent unseen confirmation.
- **Interim look.** At the project owner's request, the first 70 finished
  workflows were graded during the run with the same grading function. All 70
  were accepted. The run was not changed, and the result above comes from the
  sealed grading of all 99 workflows.

## Evidence

[`stale_handoff_ab_v5.tar.gz`](stale_handoff_ab_v5.tar.gz) (SHA-256
`aeacd58647109f3489ed68f0749f8940fc894e3b5a1eba4e5c3843c904e732f8`) contains:

- the policy, tasks, case table, execution binding and controller receipt;
  every file the binding names, with its sealed hash;
- the scripts that built the cases' checks, ran the round and graded it;
- the cases, now public: the case brief, diagnoses, changes, reference and
  literal stale fixes, hidden tests, case notes and validation logs, and each
  case's staleness checks;
- each workflow's receipt, patch and grade, and the admission receipts;
- the stopped first attempt and the interim grades, kept apart from the result.
