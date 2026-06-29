# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Literal, Optional, Sequence

import numpy as np

from ..agent.rerank_agent import RerankAgent
from ..index.embedding.vector_store import CodeVectorStore
from ..llm.litellm_chat import LiteLLMChat
from ..log_utils import get_logger
from ..types import QueriedNode

logger = get_logger(__name__)


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
