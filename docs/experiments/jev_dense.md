<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
SPDX-License-Identifier: Apache-2.0
-->

# Embedding controls for the Jev route comparison

Active objective: determine whether the embedding-free routes can replace the
measured embedding retrieval plus dedicated reranker configurations. The earlier
100-issue experiment has no dense control; the separate 15-question experiment
cannot fill that gap because its corpus and expected-file-group metric differ.

This protocol is fixed before inspecting new quality results:

- Reuse all 100 frozen CodeNib Base queries and exact Git source snapshots.
  Verify tree, selected-blob hash, chunk count, and candidate text against the
  existing BM25 control. Both indexes read full chunks; keep the shared
  3,000-character visible-code cap for reranking and evaluation.
- Use the production CodeRankEmbed wrapper and pinned model revision, its
  registered query instruction, normalized vectors, and exact FAISS search.
  Embed raw chunk content as the production vector store does. Use the native
  8,192-token context and audit query/document truncation. Bound offline batch
  size by token length without shortening documents to fit a fixed batch.
- Add dense top-100 and BM25+dense RRF top-100 candidate pools. Hybrid uses
  the production merger, equal weights, rank constant 60, and top-100 from each
  branch before fusion; these settings are not selected using target labels.
- Run Qwen3-Reranker-4B and Jev 1.13 on each new frozen pool, using the existing
  inference configurations. Retain no-rerank controls. Reuse the existing BM25
  and model-planned grep runs, preserving recorded failures.
- Evaluate candidate coverage and final Recall@1/5/10 and MRR using the same
  base-aligned labels and overlap-deduplication metric. Compare per issue and
  use paired repository-cluster bootstrap intervals. A non-significant
  difference alone does not establish equivalence or replacement.
- Report warm retrieval, reranking, and per-issue component sums separately
  from model loading and document/index construction. Include grep planning
  in its route latency and cost. Local GPU time is not zero-cost inference.

Work remains until the prepared pools, four reranker sweeps, offline analysis,
English blog update, and relevant local checks are complete. No production
retrieval defaults change as part of this experiment.
