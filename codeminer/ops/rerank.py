# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Sequence

import numpy as np

from ..agent.rerank_agent import RerankAgent
from ..index.embedding.vector_store import CodeVectorStore
from ..llm.litellm_chat import LiteLLMChat
from ..log_utils import get_logger
from ..types import QueriedNode

logger = get_logger(__name__)


@dataclass
class CrossEncoderContext:
    """Context for cross-encoder reranking.

    Wraps either :class:`STCrossEncoderWrapper` (sentence-transformers
    CrossEncoder — mxbai-rerank-*, bge-reranker-*, jina-reranker-*) or
    :class:`QwenRerankerWrapper` (Qwen3-Reranker-* yes/no logit scoring).

    The underlying wrapper is loaded lazily on first :meth:`score` call so
    that building a ``CrossEncoderContext`` at startup costs nothing until the
    skill is actually invoked.

    Args:
        model_name: HuggingFace model id (e.g. ``"mixedbread-ai/mxbai-rerank-large-v2"``).
        backend: ``"st"`` (sentence-transformers CrossEncoder), ``"qwen"``
            (Qwen3-Reranker logit trick), or ``None`` to auto-detect from
            the model name (``"Qwen3-Reranker"`` in name → qwen; else st).
        batch_size: Scoring batch size forwarded to the wrapper.
    """

    model_name: str
    backend: Optional[str] = None
    batch_size: int = 16
    _wrapper: Any = field(default=None, init=False, repr=False)

    def ensure_wrapper(self) -> Any:
        if self._wrapper is None:
            from ..index.rerank.cross_encoder import build_reranker
            self._wrapper = build_reranker(
                self.model_name,
                backend=self.backend,
                batch_size=self.batch_size,
            )
            logger.info("Loaded cross-encoder: %s (backend=%s)", self.model_name, self.backend)
        return self._wrapper

    def score(self, query: str, docs: List[str]) -> List[float]:
        """Score (query, doc) pairs; higher = more relevant."""
        return self.ensure_wrapper().score(query, docs)

    def close(self) -> None:
        if self._wrapper is not None:
            try:
                self._wrapper.close()
            except Exception:
                pass
            self._wrapper = None


@dataclass
class RerankContext:
    """Shared context carrying the rerank agent and configuration."""

    llm: Optional[LiteLLMChat] = None
    agent: Optional[RerankAgent] = None
    embedding_store: Optional[CodeVectorStore] = None
    top_k: Optional[int] = None
    candidate_top_k: Optional[int] = None
    window_size: Optional[int] = None
    window_step: Optional[int] = None
    listwise_format: Literal["structured", "rankgpt"] = "structured"

    def ensure_agent(self) -> RerankAgent:
        if self.agent is None:
            if self.llm is None:
                raise RuntimeError("Rerank agent requested but no LLM was provided.")
            logger.info(
                "Creating rerank agent.",
                extra={
                    "model": self.llm.model,
                    "listwise_format": self.listwise_format,
                },
            )
            self.agent = RerankAgent(llm=self.llm, listwise_format=self.listwise_format)
        return self.agent


def rerank_by_embedding(
    query: str,
    candidates: Sequence[QueriedNode],
    embedding_store: CodeVectorStore,
    *,
    top_k: Optional[int] = None,
) -> List[QueriedNode]:
    """Score candidate contents online with an embedding model.

    This operator intentionally does not search the global vector index. It only
    re-embeds the provided candidate contents and sorts those candidates by
    similarity to the query. Use it after retrieval/graph expansion has already
    assembled the candidate set.
    """
    candidates_with_content = [
        candidate for candidate in candidates if candidate.content
    ]
    if not candidates_with_content:
        return []

    query_vec = np.array(embedding_store.embedding.embed_query(query), dtype=np.float32)
    doc_vectors = np.array(
        embedding_store.embedding.embed_documents(
            [candidate.content for candidate in candidates_with_content]
        ),
        dtype=np.float32,
    )

    metric = embedding_store.index_metric
    if metric == "ip":
        scores = np.dot(doc_vectors, query_vec).tolist()
    elif metric == "l2":
        scores = (-np.linalg.norm(doc_vectors - query_vec, axis=1)).tolist()
    else:
        raise ValueError(f"Unsupported index metric: {metric!r}")

    ranked = sorted(
        zip(scores, candidates_with_content, strict=True),
        key=lambda pair: pair[0],
        reverse=True,
    )
    if top_k is not None:
        ranked = ranked[:top_k]
    return [_copy_with_score(candidate, float(score)) for score, candidate in ranked]


def _copy_with_score(candidate: QueriedNode, score: float) -> QueriedNode:
    update = {"score": score}
    if hasattr(candidate, "model_copy"):
        return candidate.model_copy(update=update)
    if hasattr(candidate, "copy"):
        return candidate.copy(update=update)
    data: Dict[str, object]
    if hasattr(candidate, "model_dump"):
        data = dict(candidate.model_dump())
    elif hasattr(candidate, "dict"):
        data = dict(candidate.dict())
    else:
        raise TypeError(f"Object {candidate!r} does not support model dumping.")
    data.update(update)
    return QueriedNode(**data)
