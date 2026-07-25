# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Cross-encoder rerankers (pairwise (query, doc) → relevance score).

Two backends:
  - ``STCrossEncoderWrapper`` for sentence-transformers ``CrossEncoder`` models
    (mxbai-rerank-*, bge-reranker-*, jina-reranker-*).
  - ``QwenRerankerWrapper`` for the Qwen3-Reranker family — decoder-only
    causal LM that scores via the yes/no-token logit trick.

Both expose a uniform ``score(query, docs) -> List[float]`` API so callers
can rerank a window of candidates without caring which backend is in use.
"""

from .cross_encoder import QwenRerankerWrapper, STCrossEncoderWrapper, build_reranker

__all__ = [
    "QwenRerankerWrapper",
    "STCrossEncoderWrapper",
    "build_reranker",
]
