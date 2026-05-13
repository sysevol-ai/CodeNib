# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any, Callable, List

import numpy as np


def create_executor(context: Any) -> Callable[..., List[Any]]:
    """Create an embedding rerank executor bound to the given RerankContext."""

    def execute(
        query: str, candidates: List[Any], top_k: int = 5, **kwargs: Any
    ) -> List[Any]:
        store = context.embedding_store
        if store is None:
            raise RuntimeError("Embedding store not available for rerank")

        candidates_with_content = [c for c in candidates if c.content]
        if not candidates_with_content:
            return []

        query_vec = np.array(store.embedding.embed_query(query), dtype=np.float32)
        doc_vecs = np.array(
            store.embedding.embed_documents(
                [c.content for c in candidates_with_content]
            ),
            dtype=np.float32,
        )

        if store.index_metric == "ip":
            scores = np.dot(doc_vecs, query_vec).tolist()
        else:
            scores = (-np.linalg.norm(doc_vecs - query_vec, axis=1)).tolist()

        ranked = sorted(
            zip(scores, candidates_with_content, strict=False),
            key=lambda p: p[0],
            reverse=True,
        )

        from codeminer.types import QueriedNode

        results: List[QueriedNode] = []
        for score, cand in ranked[:top_k]:
            results.append(
                QueriedNode(
                    node_name=cand.node_name,
                    type=cand.type,
                    file=cand.file,
                    node_id=cand.node_id,
                    start_line=cand.start_line,
                    end_line=cand.end_line,
                    score=float(score),
                    content=cand.content,
                )
            )
        return results

    return execute
