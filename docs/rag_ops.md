<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# RAG Ops And Planner

CodeNib keeps RAG behavior in two layers:

- `codenib/ops/` contains small retrieval operators and typed contexts.
- `codenib/model/` chooses and executes pipeline policy.

The ops layer should stay simple. It should normalize, merge, filter, expand, or
rerank candidates without deciding which retrieval strategy a query deserves.
Strategy belongs in a planner or named pipeline.

## Operator Surface

| Module | Role | What belongs here |
|--------|------|-------------------|
| `ops.retrieve` | Candidate normalization and fusion | `RetrieveContext`, `to_queried_nodes`, node/file-level weighted RRF, stable dedup keys |
| `ops.filter` | Explicit candidate predicates | span/content checks, symbol-only checks, include/exclude path filters, test-path dropping |
| `ops.expand` | Compact graph neighborhood expansion | one-hop predecessor/successor expansion from existing seeds, edge-type filtering, optional span content loading |
| `ops.rerank` | Candidate-only reranking | online content embeddings or persisted index vectors over a provided candidate set; lazy LLM reranker context |
| `ops.transform` | Query/code transforms | shared transform resources such as keyword extraction |

This surface is intentionally not a general workflow engine. It does not encode
arbitrary DAGs, loops, index building, or long graph traversals. Those stay in
dedicated pipelines and graph components.

## Expressiveness

The current ops are enough to express the common CodeNib RAG paths:

- dense-only semantic retrieval
- sparse/BM25 lexical retrieval
- dense plus sparse fusion, including RRF in `RetrieveRerankPipeline`
- candidate filtering before rerank
- dense-first graph neighbor augmentation
- file-aware semantic/graph rank fusion in `DenseGraphExpandRerankPipeline`
- sparse-seeded graph expansion through the graph retrieval pipeline
- embedding or LLM reranking over an assembled candidate set

The current deliberate limits are:

- `expand_graph_neighbors` performs one-hop neighbor expansion only; k-hop BFS
  and PPR live in `SparseSeededGraphRetrievePipeline`.
- `RetrieveRerankPipeline` executes dense/sparse stages and, when constructed
  with a loaded `code_graph`, consumes `GraphExpansionPlan` for structural
  queries selected by the planner.
- Index construction is outside ops. Build indexes through compiler/indexer
  surfaces, then pass loaded resources through context objects.

## Validated Models

CodeNib's Hugging Face and OpenAI-compatible adapters accept more models than
the list below. The matrix records the narrower surface for which this project
has retained end-to-end evidence. **Benchmark** means a complete 100-row
[CodeNib Base](https://huggingface.co/datasets/fishmingyu/codenib-base-dataset)
result artifact exists; **runtime** means the shipped route and model-specific
prompt contract are tested, but the model was not part of that five-model
quality sweep.

### Embedding Models

| Model | Dimension | Roles exercised | Evidence |
| --- | ---: | --- | --- |
| [CodeRankEmbed](https://huggingface.co/nomic-ai/CodeRankEmbed) | 768 | Default local dense and hybrid route | Runtime default; packaging, prompt registry, build, load, and query paths |
| [SweRankEmbed-Small](https://huggingface.co/Salesforce/SweRankEmbed-Small) | 768 | Dense retrieval; first-stage retrieval for rerank | Benchmark, 100/100 rows |
| [SweRankEmbed-Large](https://huggingface.co/fishmingyu/SweRankEmbed-Large) | 3,584 | Dense retrieval; embedding rerank | Benchmark, 100/100 rows |
| [jina-code-embeddings-1.5b](https://huggingface.co/jinaai/jina-code-embeddings-1.5b) | 1,536 | Dense retrieval; embedding rerank | Benchmark, 100/100 rows |
| [Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) | 1,024 | Dense retrieval; first-stage retrieval for rerank | Benchmark, 100/100 rows |
| [Qwen3-Embedding-4B](https://huggingface.co/Qwen/Qwen3-Embedding-4B) | 2,560 | Dense retrieval; embedding rerank | Benchmark, 100/100 rows |

### Rerank Models

| Model or family | Strategy | Retained benchmark coverage |
| --- | --- | --- |
| [SweRankEmbed-Large](https://huggingface.co/fishmingyu/SweRankEmbed-Large), [jina-code-embeddings-1.5b](https://huggingface.co/jinaai/jina-code-embeddings-1.5b), [Qwen3-Embedding-4B](https://huggingface.co/Qwen/Qwen3-Embedding-4B) | Dual-encoder candidate rerank | Complete 2 first-stage models x 3 rerank models matrix, 100 rows per pair |
| [Qwen3-Reranker-0.6B](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B) | Pairwise yes/no scoring | 100 rows at candidate widths 30, 50, and 100 |
| [Qwen3-Reranker-4B](https://huggingface.co/Qwen/Qwen3-Reranker-4B) | Pairwise yes/no scoring | 100-row runs across first-stage models and candidate widths 30, 50, and 100 |
| [Qwen3-Reranker-8B](https://huggingface.co/Qwen/Qwen3-Reranker-8B) | Pairwise yes/no scoring | 100 rows at candidate widths 30, 50, and 100 with SweRankEmbed-Small |
| [mxbai-rerank-large-v2](https://huggingface.co/mixedbread-ai/mxbai-rerank-large-v2) | Sentence-Transformers cross-encoder | 100 rows at candidate width 30 with SweRankEmbed-Small |
| [SweRankLLM-Small](https://huggingface.co/Salesforce/SweRankLLM-Small) | RankGPT-style listwise rerank | 100 rows with SweRankEmbed-Small retrieval |

The reproducible entry points are the
[embedding sweep](https://github.com/sysevol-ai/CodeNib/blob/main/scripts/embeddings/eval_codenib_base_embeddings.sh)
and
[rerank matrix](https://github.com/sysevol-ai/CodeNib/blob/main/scripts/embeddings/eval_codenib_base_rerank_matrix.sh).
For a repository-level invocation rather than a dataset sweep, use the
[SweRank retrieve-rerank recipe](https://github.com/sysevol-ai/CodeNib/blob/main/examples/swerank_retrieve_rerank.py).
It exposes a SweRankEmbed-Small → SweRankLLM-Small listwise path
and a one-process SweRankEmbed-Small → SweRankEmbed-Large path over the same
`RetrieveRerankPipeline` contract.
Historical retrieval artifacts recorded model IDs but not immutable model
revisions; the paper artifact reports that provenance limit. A model not listed
here may still run through a generic adapter, but it is not a validated quality
claim.

Remote model code is disabled by default for cross-encoder rerankers. The
validated Qwen3 and mxbai rerankers load through standard library adapters. A
custom remote model that requires repository code must opt in explicitly and
pin `revision` to its full 40-character commit SHA; local model directories may
opt in without a Hub revision.

## Query-Aware Planner

`RetrievalPlanner` is deterministic. It does not call an LLM. It maps three
inputs to a `RetrievalPathPlan`:

- query signals: lexical, semantic, structural
- budget: `fast`, `balanced`, or `thorough`
- capabilities: dense, sparse, graph, embedding rerank, LLM rerank

The selected plan is declarative:

| Plan | Intent | Typical execution |
|------|--------|-------------------|
| `fast_lexical` | exact names, low latency | BM25 only, no rerank |
| `semantic` | natural-language behavior queries | dense retrieval, optional rerank |
| `hybrid_fusion` | mixed or uncertain queries | dense + sparse, usually RRF, optional rerank |
| `structural_graph` | callers/callees/impact/dependency queries | sparse seeds plus graph expansion, optional rerank |

`RetrieveRerankPipeline(retrieval_mode="auto")` wires the planner into query
execution for dense, sparse, hybrid fusion, graph expansion, and rerank control.
It records the most recent `last_selected_plan` and `last_planner_trace`
(`signals`, `budget`, `capabilities`, and `plan`) for evaluation/debugging.
Use `SparseSeededGraphRetrievePipeline` or `DenseGraphExpandRerankPipeline` when
you need an experiment-specific graph baseline rather than the general auto
pipeline.

The dense GraphRAG path defaults to one-hop `reference` edges. Containment is
excluded from the dependency budget, callers and callees are interleaved, and
multi-edges are collapsed before the per-seed cap. Its optional file-level RRF
uses stable relative paths from node IDs so graph artifacts and vector indexes
built under different checkout roots still fuse correctly.

## Maintenance Rules

- Add ops only when a repeated pipeline step needs a reusable, typed boundary.
- Keep query classification and budget logic in `RetrievalPlanner`, not in skill
  executors or individual ops.
- Keep long graph traversal policy in graph pipelines unless the same operation
  is reused by multiple retrieval surfaces.
- New GPU/HuggingFace embedding tests belong in the `slow` tier, not the
  parallel `integration` tier.
