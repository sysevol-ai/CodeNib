# LSP replay aggregate

Snapshots: 100. Request equivalence is counted once per request; latency distributions retain measured repetitions.

| capability | equivalent/requests | mismatch | error/fallback | equivalence | static p50 ms | live p50 ms | speedup p50 |
|---|---:|---:|---:|---:|---:|---:|---:|
| overall | 632/1000 | 368 | 0 | 63.2% | 0.62 | 2.30 | 4.71 |
| definition | 437/500 | 63 | 0 | 87.4% | 0.63 | 1.72 | 3.43 |
| references | 195/500 | 305 | 0 | 39.0% | 0.60 | 5.38 | 11.01 |

Warmup diagnostics: 2 errors across 2140 rows (2 snapshots affected).

## Languages

| language | snapshots | definition eq | references eq | overall eq | speedup p50 |
|---|---:|---:|---:|---:|---:|
| cpp | 20 | 87/100 (87%) | 22/100 (22%) | 109/200 (55%) | 43.93 |
| go | 21 | 104/105 (99%) | 77/105 (73%) | 181/210 (86%) | 6.03 |
| python | 20 | 80/100 (80%) | 36/100 (36%) | 116/200 (58%) | 2.74 |
| rust | 20 | 85/100 (85%) | 33/100 (33%) | 118/200 (59%) | 2.70 |
| typescript | 19 | 81/95 (85%) | 27/95 (28%) | 108/190 (57%) | 6.42 |

## Setup

| phase | p50 ms | p95 ms |
|---|---:|---:|
| graph_load_ms | 29.46 | 121.09 |
| static_provider_init_ms | 0.02 | 0.03 |
| live_start_ms | 971.71 | 19010.22 |
| idle_wait_ms | 1139.00 | 33027.91 |
| warmup_wall_ms | 3325.35 | 10767.57 |
