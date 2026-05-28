# Phase 2 agent-compile sample report

Easy instances (A0 files@5 ≥ 0.5): ['astral-sh__ruff-15309', 'astropy__astropy-12907', 'axios__axios-4731', 'caddyserver__caddy-5870', 'scikit-learn__scikit-learn-13142']
Hard instances: (none)

## Per-subset metrics

| subset | n | tokens | turns | cost$ | cap% | files@1 | files@3 | files@5 | files@10 | f@5 easy | f@5 hard | eligible |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A0 | 5 | 200860 | 13.0 | 0.2089 | 20% | 0.700 | 0.900 | 0.900 | 0.900 | 0.900 | n/a | yes |
| A1 | 5 | 202375 | 15.0 | 0.2107 | 40% | 0.700 | 0.900 | 1.000 | 1.000 | 1.000 | n/a | yes |
| A2 | 5 | 206054 | 12.8 | 0.2151 | 40% | 0.900 | 1.000 | 1.000 | 1.000 | 1.000 | n/a | yes |
| A3 | 5 | 265396 | 14.2 | 0.2744 | 40% | 0.700 | 0.900 | 0.900 | 1.000 | 0.900 | n/a | yes |
| A4 | 5 | 311073 | 14.4 | 0.3258 | 20% | 0.700 | 0.900 | 0.900 | 0.900 | 0.900 | n/a | yes |
| A5 | 5 | 240177 | 12.6 | 0.2484 | 40% | 0.700 | 0.900 | 0.900 | 1.000 | 0.900 | n/a | yes |
| A6 | 5 | 258971 | 12.0 | 0.2675 | 20% | 0.900 | 0.900 | 0.900 | 0.900 | 0.900 | n/a | yes |

**Pareto front (files@5 ↑ / tokens ↓):** A0, A1

## Skill-invocation histogram

`file_read` / `file_search` are the always-on default tool layer (present in every subset, not swept).

### A0

| skill | invoke_rate | calls/cell | files@5 \| invoked |
| --- | --- | --- | --- |
| bm25_search | 100% | 1.90 | 0.900 |
| file_read *(always-on)* | 100% | 7.60 | 0.900 |
| file_search *(always-on)* | 100% | 7.00 | 0.900 |

### A1  — forced-but-ignored: ['embedding_search']

| skill | invoke_rate | calls/cell | files@5 \| invoked |
| --- | --- | --- | --- |
| file_read *(always-on)* | 100% | 8.70 | 1.000 |
| file_search *(always-on)* | 100% | 7.70 | 1.000 |

### A2  — forced-but-ignored: ['embedding_search']

| skill | invoke_rate | calls/cell | files@5 \| invoked |
| --- | --- | --- | --- |
| bm25_search | 100% | 2.10 | 1.000 |
| file_read *(always-on)* | 100% | 7.60 | 1.000 |
| file_search *(always-on)* | 100% | 6.00 | 1.000 |

### A3  — forced-but-ignored: ['graph_expand']

| skill | invoke_rate | calls/cell | files@5 \| invoked |
| --- | --- | --- | --- |
| bm25_search | 100% | 2.80 | 0.900 |
| file_read *(always-on)* | 100% | 7.60 | 0.900 |
| file_search *(always-on)* | 100% | 6.30 | 0.900 |

### A4  — forced-but-ignored: ['embedding_search', 'graph_expand']

| skill | invoke_rate | calls/cell | files@5 \| invoked |
| --- | --- | --- | --- |
| bm25_search | 100% | 3.90 | 0.900 |
| file_read *(always-on)* | 100% | 7.10 | 0.900 |
| file_search *(always-on)* | 100% | 5.70 | 0.900 |

### A5  — forced-but-ignored: ['embedding_search', 'graph_expand', 'regex_search']

| skill | invoke_rate | calls/cell | files@5 \| invoked |
| --- | --- | --- | --- |
| bm25_search | 100% | 2.30 | 0.900 |
| file_read *(always-on)* | 100% | 7.30 | 0.900 |
| file_search *(always-on)* | 100% | 5.30 | 0.900 |

### A6  — forced-but-ignored: ['regex_search', 'embedding_search', 'hybrid_search', 'graph_expand', 'embedding_rerank', 'llm_rerank', 'query_transform', 'code_to_query']

| skill | invoke_rate | calls/cell | files@5 \| invoked |
| --- | --- | --- | --- |
| bm25_search | 100% | 2.90 | 0.900 |
| file_read *(always-on)* | 100% | 6.90 | 0.900 |
| file_search *(always-on)* | 100% | 4.90 | 0.900 |

## Per-scenario files@5 / tokens

### go:no_stacktrace

| subset | files@5 | tokens | n |
| --- | --- | --- | --- |
| A0 | 1.000 | 195494 | 1 |
| A1 | 1.000 | 150306 | 1 |
| A2 | 1.000 | 123738 | 1 |
| A3 | 1.000 | 188458 | 1 |
| A4 | 1.000 | 235735 | 1 |
| A5 | 1.000 | 173297 | 1 |
| A6 | 1.000 | 239430 | 1 |

### python:no_stacktrace

| subset | files@5 | tokens | n |
| --- | --- | --- | --- |
| A0 | 1.000 | 373731 | 1 |
| A1 | 1.000 | 255533 | 1 |
| A2 | 1.000 | 288792 | 1 |
| A3 | 1.000 | 390782 | 1 |
| A4 | 1.000 | 386706 | 1 |
| A5 | 0.500 | 463205 | 1 |
| A6 | 0.500 | 540512 | 1 |

### python:stacktrace

| subset | files@5 | tokens | n |
| --- | --- | --- | --- |
| A0 | 1.000 | 133822 | 1 |
| A1 | 1.000 | 106155 | 1 |
| A2 | 1.000 | 167271 | 1 |
| A3 | 1.000 | 256187 | 1 |
| A4 | 1.000 | 353432 | 1 |
| A5 | 1.000 | 206483 | 1 |
| A6 | 1.000 | 175310 | 1 |

### rust:no_stacktrace

| subset | files@5 | tokens | n |
| --- | --- | --- | --- |
| A0 | 0.500 | 131800 | 1 |
| A1 | 1.000 | 422492 | 1 |
| A2 | 1.000 | 244934 | 1 |
| A3 | 0.500 | 220386 | 1 |
| A4 | 0.500 | 384493 | 1 |
| A5 | 1.000 | 194730 | 1 |
| A6 | 1.000 | 157531 | 1 |

### typescript:no_stacktrace

| subset | files@5 | tokens | n |
| --- | --- | --- | --- |
| A0 | 1.000 | 169454 | 1 |
| A1 | 1.000 | 77387 | 1 |
| A2 | 1.000 | 205533 | 1 |
| A3 | 1.000 | 271169 | 1 |
| A4 | 1.000 | 195001 | 1 |
| A5 | 1.000 | 163172 | 1 |
| A6 | 1.000 | 182073 | 1 |

## Derived compile_table

Floor metric: files@5; τ_global = A6 files@5 − 0.05 = 0.850

| scenario | chosen subset | files@5 | skills |
| --- | --- | --- | --- |
| go:no_stacktrace | A2 | 1.000 | bm25_search, embedding_search |
| python:no_stacktrace | A1 | 1.000 | embedding_search |
| python:stacktrace | A1 | 1.000 | embedding_search |
| rust:no_stacktrace | A6 | 1.000 | bm25_search, regex_search, embedding_search, hybrid_search, graph_expand, embedding_rerank, llm_rerank, query_transform, code_to_query |
| typescript:no_stacktrace | A1 | 1.000 | embedding_search |
