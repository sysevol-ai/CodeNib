# Phase 2 agent-compile sample report

Easy instances (A0 files@5 ≥ 0.5): ['astropy__astropy-12907', 'axios__axios-4731', 'caddyserver__caddy-5870']
Hard instances: (none)

## Per-subset metrics

| subset | n | tokens | turns | cost$ | cap% | files@1 | files@3 | files@5 | files@10 | f@5 easy | f@5 hard | eligible |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A0 | 3 | 133954 | 10.0 | 0.1401 | 0% | 0.667 | 1.000 | 1.000 | 1.000 | 1.000 | n/a | yes |
| A1 | 3 | 83501 | 9.7 | 0.0891 | 0% | 0.667 | 1.000 | 1.000 | 1.000 | 1.000 | n/a | yes |
| A2 | 3 | 132951 | 10.0 | 0.1382 | 0% | 0.667 | 1.000 | 1.000 | 1.000 | 1.000 | n/a | yes |
| A3 | 3 | 159188 | 10.0 | 0.1643 | 0% | 0.667 | 1.000 | 1.000 | 1.000 | 1.000 | n/a | yes |
| A4 | 3 | 151818 | 10.0 | 0.1569 | 0% | 0.333 | 1.000 | 1.000 | 1.000 | 1.000 | n/a | yes |
| A5 | 3 | 160295 | 10.0 | 0.1655 | 0% | 0.333 | 1.000 | 1.000 | 1.000 | 1.000 | n/a | yes |
| A6 | 3 | 183319 | 10.0 | 0.1881 | 0% | 0.333 | 1.000 | 1.000 | 1.000 | 1.000 | n/a | yes |

**Pareto front (files@5 ↑ / tokens ↓):** A1

## Skill-invocation histogram

`file_read` / `file_search` are the always-on default tool layer (present in every subset, not swept).

### A0

| skill | invoke_rate | calls/cell | files@5 \| invoked |
| --- | --- | --- | --- |
| bm25_search | 100% | 2.33 | 1.000 |
| file_read *(always-on)* | 100% | 6.33 | 1.000 |
| file_search *(always-on)* | 100% | 4.33 | 1.000 |

### A1  — forced-but-ignored: ['embedding_search']

| skill | invoke_rate | calls/cell | files@5 \| invoked |
| --- | --- | --- | --- |
| file_read *(always-on)* | 100% | 6.67 | 1.000 |
| file_search *(always-on)* | 100% | 4.33 | 1.000 |

### A2  — forced-but-ignored: ['embedding_search']

| skill | invoke_rate | calls/cell | files@5 \| invoked |
| --- | --- | --- | --- |
| bm25_search | 100% | 2.33 | 1.000 |
| file_read *(always-on)* | 100% | 5.00 | 1.000 |
| file_search *(always-on)* | 100% | 4.33 | 1.000 |

### A3  — forced-but-ignored: ['graph_expand']

| skill | invoke_rate | calls/cell | files@5 \| invoked |
| --- | --- | --- | --- |
| bm25_search | 100% | 3.00 | 1.000 |
| file_read *(always-on)* | 100% | 5.33 | 1.000 |
| file_search *(always-on)* | 100% | 4.00 | 1.000 |

### A4  — forced-but-ignored: ['embedding_search', 'graph_expand']

| skill | invoke_rate | calls/cell | files@5 \| invoked |
| --- | --- | --- | --- |
| bm25_search | 100% | 2.33 | 1.000 |
| file_read *(always-on)* | 100% | 5.67 | 1.000 |
| file_search *(always-on)* | 100% | 4.33 | 1.000 |

### A5  — forced-but-ignored: ['embedding_search', 'graph_expand', 'regex_search']

| skill | invoke_rate | calls/cell | files@5 \| invoked |
| --- | --- | --- | --- |
| bm25_search | 100% | 1.67 | 1.000 |
| file_read *(always-on)* | 100% | 6.00 | 1.000 |
| file_search *(always-on)* | 100% | 3.67 | 1.000 |

### A6  — forced-but-ignored: ['regex_search', 'hybrid_search', 'graph_expand', 'embedding_rerank', 'llm_rerank', 'query_transform', 'code_to_query']

| skill | invoke_rate | calls/cell | files@5 \| invoked |
| --- | --- | --- | --- |
| bm25_search | 100% | 2.67 | 1.000 |
| file_read *(always-on)* | 100% | 5.00 | 1.000 |
| file_search *(always-on)* | 100% | 3.67 | 1.000 |
| embedding_search | 33% | 0.33 | 1.000 |

## Per-scenario files@5 / tokens

### go:no_stacktrace

| subset | files@5 | tokens | n |
| --- | --- | --- | --- |
| A0 | 1.000 | 136293 | 1 |
| A1 | 1.000 | 103329 | 1 |
| A2 | 1.000 | 139738 | 1 |
| A3 | 1.000 | 212602 | 1 |
| A4 | 1.000 | 187525 | 1 |
| A5 | 1.000 | 184800 | 1 |
| A6 | 1.000 | 258831 | 1 |

### python:no_stacktrace

| subset | files@5 | tokens | n |
| --- | --- | --- | --- |
| A0 | 1.000 | 141626 | 1 |
| A1 | 1.000 | 79849 | 1 |
| A2 | 1.000 | 126359 | 1 |
| A3 | 1.000 | 128742 | 1 |
| A4 | 1.000 | 126528 | 1 |
| A5 | 1.000 | 152744 | 1 |
| A6 | 1.000 | 158576 | 1 |

### typescript:no_stacktrace

| subset | files@5 | tokens | n |
| --- | --- | --- | --- |
| A0 | 1.000 | 123944 | 1 |
| A1 | 1.000 | 67326 | 1 |
| A2 | 1.000 | 132757 | 1 |
| A3 | 1.000 | 136221 | 1 |
| A4 | 1.000 | 141401 | 1 |
| A5 | 1.000 | 143341 | 1 |
| A6 | 1.000 | 132549 | 1 |

## Derived compile_table

Floor metric: files@5; τ_global = A6 files@5 − 0.05 = 0.950

| scenario | chosen subset | files@5 | skills |
| --- | --- | --- | --- |
| go:no_stacktrace | A1 | 1.000 | embedding_search |
| python:no_stacktrace | A1 | 1.000 | embedding_search |
| typescript:no_stacktrace | A1 | 1.000 | embedding_search |
