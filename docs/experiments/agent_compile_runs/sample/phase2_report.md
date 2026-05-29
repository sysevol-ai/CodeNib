# Phase 2 agent-compile sample report

Easy instances (A0 files@5 ≥ 0.5): ['astropy__astropy-12907', 'axios__axios-4731', 'scikit-learn__scikit-learn-13142']
Hard instances: ['astral-sh__ruff-15309', 'caddyserver__caddy-5870']

## Per-subset metrics

| subset | n | tokens | turns | cost$ | cap% | files@1 | files@3 | files@5 | files@10 | f@5 easy | f@5 hard | eligible |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A0 | 5 | 93245 | 4.4 | 0.0982 | 0% | 0.200 | 0.200 | 0.600 | 0.600 | 1.000 | 0.000 | yes |
| A1 | 5 | 83209 | 4.4 | 0.0877 | 0% | 0.600 | 0.800 | 0.800 | 1.000 | 1.000 | 0.500 | yes |
| A2 | 5 | 88215 | 4.6 | 0.0933 | 0% | 0.200 | 0.200 | 0.600 | 0.600 | 1.000 | 0.000 | NO |
| A3 | 5 | 93300 | 4.8 | 0.0987 | 0% | 0.200 | 0.200 | 0.600 | 0.600 | 1.000 | 0.000 | NO |
| A4 | 5 | 99851 | 5.0 | 0.1052 | 0% | 0.200 | 0.200 | 0.600 | 0.600 | 1.000 | 0.000 | NO |
| A5 | 5 | 120293 | 5.6 | 0.1262 | 0% | 0.200 | 0.200 | 0.600 | 0.600 | 1.000 | 0.000 | NO |
| A6 | 5 | 93194 | 4.6 | 0.0983 | 0% | 0.200 | 0.200 | 0.600 | 0.600 | 1.000 | 0.000 | NO |

**Pareto front (files@5 ↑ / tokens ↓):** A1

## Skill-invocation histogram

### A0

| skill | invoke_rate | calls/cell | files@5 \| invoked |
| --- | --- | --- | --- |
| bm25_search | 100% | 7.50 | 0.600 |

### A1

| skill | invoke_rate | calls/cell | files@5 \| invoked |
| --- | --- | --- | --- |
| embedding_search | 100% | 5.10 | 0.800 |

### A2

| skill | invoke_rate | calls/cell | files@5 \| invoked |
| --- | --- | --- | --- |
| bm25_search | 100% | 5.40 | 0.600 |
| embedding_search | 100% | 1.30 | 0.600 |

### A3

| skill | invoke_rate | calls/cell | files@5 \| invoked |
| --- | --- | --- | --- |
| bm25_search | 100% | 6.50 | 0.600 |
| graph_expand | 50% | 0.50 | 0.400 |

### A4

| skill | invoke_rate | calls/cell | files@5 \| invoked |
| --- | --- | --- | --- |
| bm25_search | 100% | 5.10 | 0.600 |
| embedding_search | 80% | 1.10 | 0.750 |
| graph_expand | 20% | 0.20 | 1.000 |

### A5

| skill | invoke_rate | calls/cell | files@5 \| invoked |
| --- | --- | --- | --- |
| bm25_search | 100% | 5.40 | 0.600 |
| regex_search | 80% | 0.80 | 0.625 |
| embedding_search | 70% | 0.80 | 0.571 |
| graph_expand | 20% | 0.20 | 0.500 |

### A6  — forced-but-ignored: ['hybrid_search', 'embedding_rerank', 'llm_rerank', 'query_transform', 'code_to_query']

| skill | invoke_rate | calls/cell | files@5 \| invoked |
| --- | --- | --- | --- |
| bm25_search | 100% | 4.80 | 0.600 |
| embedding_search | 90% | 1.00 | 0.667 |
| graph_expand | 30% | 0.30 | 0.667 |
| regex_search | 10% | 0.10 | 0.000 |

## Per-scenario files@5 / tokens

### go:no_stacktrace

| subset | files@5 | tokens | n |
| --- | --- | --- | --- |
| A0 | 0.000 | 193217 | 1 |
| A1 | 1.000 | 42342 | 1 |
| A2 | 0.000 | 37021 | 1 |
| A3 | 0.000 | 160957 | 1 |
| A4 | 0.000 | 123998 | 1 |
| A5 | 0.000 | 123163 | 1 |
| A6 | 0.000 | 43448 | 1 |

### python:no_stacktrace

| subset | files@5 | tokens | n |
| --- | --- | --- | --- |
| A0 | 1.000 | 117168 | 1 |
| A1 | 1.000 | 232056 | 1 |
| A2 | 1.000 | 142283 | 1 |
| A3 | 1.000 | 85411 | 1 |
| A4 | 1.000 | 169230 | 1 |
| A5 | 1.000 | 225327 | 1 |
| A6 | 1.000 | 200575 | 1 |

### python:stacktrace

| subset | files@5 | tokens | n |
| --- | --- | --- | --- |
| A0 | 1.000 | 39030 | 1 |
| A1 | 1.000 | 32765 | 1 |
| A2 | 1.000 | 64873 | 1 |
| A3 | 1.000 | 58029 | 1 |
| A4 | 1.000 | 68993 | 1 |
| A5 | 1.000 | 43379 | 1 |
| A6 | 1.000 | 73544 | 1 |

### rust:no_stacktrace

| subset | files@5 | tokens | n |
| --- | --- | --- | --- |
| A0 | 0.000 | 43182 | 1 |
| A1 | 0.000 | 45752 | 1 |
| A2 | 0.000 | 44630 | 1 |
| A3 | 0.000 | 86150 | 1 |
| A4 | 0.000 | 59472 | 1 |
| A5 | 0.000 | 102869 | 1 |
| A6 | 0.000 | 62611 | 1 |

### typescript:no_stacktrace

| subset | files@5 | tokens | n |
| --- | --- | --- | --- |
| A0 | 1.000 | 73630 | 1 |
| A1 | 1.000 | 63129 | 1 |
| A2 | 1.000 | 152269 | 1 |
| A3 | 1.000 | 75951 | 1 |
| A4 | 1.000 | 77564 | 1 |
| A5 | 1.000 | 106727 | 1 |
| A6 | 1.000 | 85793 | 1 |

## Derived compile_table

Floor metric: files@5; τ_global = A6 files@5 − 0.05 = 0.550

| scenario | chosen subset | files@5 | skills |
| --- | --- | --- | --- |
| go:no_stacktrace | A1 | 1.000 | embedding_search |
| python:no_stacktrace | A0 | 1.000 | bm25_search |
| python:stacktrace | A1 | 1.000 | embedding_search |
| rust:no_stacktrace | A0 | 0.000 | bm25_search |
| typescript:no_stacktrace | A1 | 1.000 | embedding_search |
