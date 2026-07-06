<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

Use for fast, precise reranking after initial retrieval. Scores each (query, candidate) pair jointly using a cross-encoder model — more accurate than embedding similarity and faster than LLM listwise reranking. Best after bm25_search or embedding_search to refine the top results before generating an answer.
