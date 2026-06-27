# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, List

if TYPE_CHECKING:
    from ..context import ComposerContexts
    from ....types import QueriedNode


def create_executor(context: "ComposerContexts") -> Callable[..., List["QueriedNode"]]:
    """Create a cross-encoder rerank executor bound to the given ComposerContexts."""

    def execute(
        query: str,
        candidates: List[Any],
        top_k: int = 5,
        candidate_top_k: int = 50,
        **kwargs: Any,
    ) -> List["QueriedNode"]:
        from ....types import QueriedNode as QN

        ctx = context.cross_encoder
        if ctx is None:
            raise RuntimeError(
                "crossencoder_rerank skill: no CrossEncoderContext in skill contexts. "
                "Build a CrossEncoderContext and pass it as contexts['cross_encoder'] "
                "when constructing the AgentRunner."
            )

        candidates = candidates[:candidate_top_k]
        candidates_with_content = [c for c in candidates if getattr(c, "content", None)]
        if not candidates_with_content:
            return list(candidates[:top_k])

        docs = [c.content for c in candidates_with_content]
        scores = ctx.score(query, docs)

        ranked = sorted(
            zip(scores, candidates_with_content),
            key=lambda pair: pair[0],
            reverse=True,
        )

        return [
            QN(
                node_name=c.node_name,
                type=c.type,
                file=c.file,
                node_id=c.node_id,
                start_line=c.start_line,
                end_line=c.end_line,
                score=float(score),
                content=c.content,
            )
            for score, c in ranked[:top_k]
        ]

    return execute
