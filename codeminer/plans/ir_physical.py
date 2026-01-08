from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Dict, Iterable, List, Sequence


class PhysicalOperator(str, Enum):
    """Canonical physical operator types."""

    BM25_TOPK = "bm25.topk"
    FAISS_RETRIEVE = "faiss.retrieve"
    HYBRID_RETRIEVE = "hybrid.retrieve"
    QUERY_TRANSFORM = "query.transform"
    CODE_TO_QUERY_TRANSFORM = "code-to-query.transform"
    REGEX_RETRIEVE = "regex.retrieve"
    GRAPH_WALK = "graph.walk"
    REGEX_FILTER = "regex.filter"
    LLM_RERANK = "llm.rerank"
    EMBEDDING_RERANK = "embedding.rerank"
    PASS_THROUGH = "pass.through"


@dataclass(slots=True)
class PhysicalOp:
    """Physical operator bound to an execution engine."""

    operator: PhysicalOperator
    params: Dict[str, Any] = field(default_factory=dict)
    children: List["PhysicalOp"] = field(default_factory=list)

    def iter_children(self) -> Iterable["PhysicalOp"]:
        yield from self.children

    def with_children(self, children: Sequence["PhysicalOp"]) -> "PhysicalOp":
        return replace(self, children=list(children))


@dataclass(slots=True)
class PhysicalPlan:
    """Physical tree with scheduling metadata (batch size, concurrency)."""

    root: PhysicalOp
    metadata: Dict[str, Any] = field(default_factory=dict)

    def walk(self) -> Iterable[PhysicalOp]:
        stack: List[PhysicalOp] = [self.root]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed(list(node.iter_children())))
