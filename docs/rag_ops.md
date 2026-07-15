<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# RAG Ops And Planner

CodeMiner keeps RAG behavior in two layers:

- `codeminer/ops/` contains small retrieval operators and typed contexts.
- `codeminer/model/` chooses and executes pipeline policy.

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

The current ops are enough to express the common CodeMiner RAG paths:

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
built under different checkout roots still fuse correctly. See the
[controlled GraphRAG ablation](experiments/dense_graphrag.md).

## Maintenance Rules

- Add ops only when a repeated pipeline step needs a reusable, typed boundary.
- Keep query classification and budget logic in `RetrievalPlanner`, not in skill
  executors or individual ops.
- Keep long graph traversal policy in graph pipelines unless the same operation
  is reused by multiple retrieval surfaces.
- New GPU/HuggingFace embedding tests belong in the `slow` tier, not the
  parallel `integration` tier.
