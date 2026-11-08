<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Change guard in the loop, A/B v1, round 1

The guard made no difference in this round. On 24 Django cases chosen because
earlier agents broke hidden tests on them, three versions of the same agent each
produced 22 clean fixes and 1 hidden regression, on the same case: one with the
guard, one without any hook, and one with a hook that only names the full test
suite. The rule registered for this outcome says the agent rarely causes what the
guard catches on these cases, and that no more should be spent on this cohort.

The one regression shows a failure the guard cannot reach as it stands. The guard
listed all eight tests the patch broke, but the agent had changed a user-visible
error message and rewritten those tests to expect the new text. Its own test run
passed, and it set the guard's list aside.

## Question

Does `codenib guard` lead the same agent to fewer hidden regressions than (a) no
hook and (b) a Stop hook that names the project's full test suite? A hidden
regression is a patch that passes SWE-bench's own check and breaks a test
elsewhere in the project's full suite.

The [static check](guardian_regression_check_v1.md) measured whether a list
contains a broken test. This round measures what the agent does with the list.

## Setup

The protocol, harness and case list were hashed at 2026-11-06T18:54:37Z, before
any model call on these cases (sysevol-ai/CodeNib#975, phase 5).

- **Cases.** The 25 Django instances of SWE-bench Verified on which one of three
  2024-25 agents passed SWE-bench and broke another test
  ([Wang, Pradel and Liu](https://arxiv.org/abs/2503.15223)), minus
  django__django-11149, which was used to test the plumbing. That leaves 24
  instances, one session per arm, 72 sessions. The cases are enriched for hidden
  regressions and do not represent ordinary patches.
- **Arms.** Same model, prompt, tools and limits; they differ only at Stop.
  - **native:** no hook.
  - **guard:** `codenib guard install --agent claude` at CodeNib `10904dac`, with
    its SessionStart and Stop hooks and its message, which shows 10 tests and a
    command for 20. The graph was built before the session.
  - **suite:** a Stop hook with the guard's event, output shape and once-per-change
    rule. Its text names the number of changed Python files and
    `./tests/runtests.py --settings=test_sqlite --parallel 16`.
- **Agent.** Claude Code 2.1.319 with claude-opus-5-5 at medium effort, no MCP,
  WebFetch and WebSearch disabled, $3 and 1,800 s per session. Each session ran in
  the instance's SWE-bench image (16 CPUs, 48 GB) on a one-commit snapshot of the
  base commit. The prompt is the issue text, then "Fix this issue in the
  repository in the current directory. Run tests with `./tests/runtests.py`."
- **Grading**, with no model and no human judgement:
  1. **Resolved:** SWE-bench's own eval script.
  2. **Full suite:** a fresh container, the patch without its changes under
     `tests/` (so an edited test cannot hide a failure), plus the benchmark's test
     patch, then the whole suite.
  3. **Regression:** a test that fails in two full runs of the patch and in
     neither of two full runs of the gold patch.

Before any model call, the grader reproduced the published labels. The gold patch
resolved 25 of 25. Of 27 older patches labelled as regressions, 26 were reported
as regressions. Of 24 older clean patches, 23 reported none.

## Results

| Arm | Resolved | Clean success | Hidden regressions | Median session | 90th percentile | Median turns | Cost |
|---|---|---|---|---|---|---|---|
| native | 23 | 22 | 1 | 38.7 s | 68.1 s | 8 | $3.23 |
| guard | 23 | 22 | 1 | 63.2 s | 140.7 s | 9.5 | $3.81 |
| suite | 23 | 22 | 1 | 126.2 s | 225.4 s | 11 | $4.11 |

Counts are out of 24 per arm. In every arm the unresolved case is
django__django-11749 and the hidden regression is django__django-13933. No session
timed out, and the round cost $11.15.

- **Guard hook:** fired in all 24 of its sessions, 27 times in all. In 11
  sessions the agent used a tool afterwards (15 calls); in none did it edit a
  file.
- **Suite hook:** led to tool calls in all 24 sessions (61 calls), and also to no
  edits.
- **Flags on the final patches:** a median of 69 tests (90th percentile 447, at
  most 4,199). These are this agent's own patches, mostly clean, so the counts
  are not comparable with the static check's controls.

Against the decision rule:

1. **Nothing to catch (applies).** native had 1 hidden regression of 24, within the
   rule's "at most 1". This agent rarely causes what the guard catches on these
   cases, and no more is spent on this cohort.
2. **Suite against guard.** The suite hook had as many clean successes as the
   guard (22 each), so a one-line hook did at least as well in this round. Its
   median session took twice as long.

## The one regression

django__django-13933 asks `ModelChoiceField` to include the rejected value in its
validation error. The upstream fix passes the value as a parameter and keeps the
default message. In all three arms the agent changed the default message instead,
from "That choice is not one of the available choices" to "%(value)s is not one
of the available choices". It also rewrote the eight existing assertions that
expect the old text, in `admin_widgets`, `model_forms` and `model_formsets`.

In the guard session, the agent's own run of `forms_tests`, `model_forms` and
`admin_widgets` passed (1,018 tests). The guard listed all eight broken tests, at
ranks 44 to 118 of 118, below the ten it shows and the twenty in its command. The
agent answered that the listed tests were "all in modules I'd already run" and
stopped. The grader removes the agent's test edits, and the eight tests fail.

A list of tests at risk cannot catch this: the agent ran those tests after
editing them. The grader's own step would have caught it, running the edited
at-risk tests against their original versions.

## What this means

- **Little to catch.** For this agent on these cases a hidden regression is rare:
  1 of 24, even on an enriched cohort. A guard that only names tests has little to
  catch, and here it added about 25 s to the median session.
- **Edited tests are the gap.** The regression that did occur came with edits to
  existing tests. A next guard version should notice when a change edits tests
  that are on its at-risk list, and offer to run them as they were at the base
  commit. Testing that needs a new cohort and a newly sealed protocol.

## Evidence

[`guard_in_the_loop_ab_v1.tar.gz`](guard_in_the_loop_ab_v1.tar.gz) (SHA-256
`b901d26be671012aedaf3388924d571b4c6d70d00e43897e501138f456a36357`) contains:

- the protocol and its hash file, the harness, the case list and the grader
  validation;
- `results.json`, and each session's metadata, grade, patch and hook log;
- the transcripts and guard reports of django__django-13933.

`MANIFEST.sha256` lists the SHA-256 of every file of the original run, including
the other transcripts and guard reports, which are left out for size. The sealed
files still match their hashes. This write-up adds no session and no regrade; the
figures come from `results.json`.
