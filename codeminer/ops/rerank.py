from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np

from ..agent.rerank_agent import RerankAgent
from ..index.embedding.vector_store import CodeVectorStore
from ..llm.llm_config import LLMConfig
from ..log_utils import get_logger
from ..plans.execution import ExecutionEngine
from ..plans.ir_exec import ExecutionNode
from ..plans.ir_physical import PhysicalOperator
from ..types import NodeInfo, QueriedNode

logger = get_logger(__name__)


@dataclass
class RerankContext:
    """Shared context carrying the rerank agent and configuration."""

    llm_config: Optional[LLMConfig] = None
    agent: Optional[RerankAgent] = None
    embedding_store: Optional[CodeVectorStore] = None
    top_k: Optional[int] = None
    candidate_top_k: Optional[int] = None
    window_size: Optional[int] = None
    window_step: Optional[int] = None

    def ensure_agent(self) -> RerankAgent:
        if self.agent is None:
            if self.llm_config is None:
                raise RuntimeError("Rerank agent requested but no LLMConfig provided.")
            logger.info(
                "Creating rerank agent.",
                extra={"model": self.llm_config.model_name},
            )
            self.agent = RerankAgent(llm_config=self.llm_config)
        return self.agent


def register_rerank_ops(engine: ExecutionEngine, context: RerankContext) -> None:
    """Register rerank kernels on the execution engine."""

    engine.register(PhysicalOperator.LLM_RERANK.value, _llm_rerank_kernel(context))
    engine.register(
        PhysicalOperator.EMBEDDING_RERANK.value, _embedding_rerank_kernel(context)
    )


def _llm_rerank_kernel(context: RerankContext):
    def run(node: ExecutionNode, inputs: List[object]) -> List[QueriedNode]:
        query = _extract_query(node.params, inputs)
        candidates = _collect_candidates(inputs)
        if not candidates:
            logger.warning("LLM rerank invoked without candidate nodes.")
            return []

        top_k = _resolve_top_k(node.params, context.top_k)
        candidate_top_k = _resolve_candidate_top_k(node.params, context.candidate_top_k)
        include_content = bool(node.params.get("return_content", False))
        window_size = node.params.get("window_size") or context.window_size
        window_step = node.params.get("window_step") or context.window_step

        agent = context.ensure_agent()
        logger.info(
            "Running LLM rerank.",
            extra={
                "candidate_count": len(candidates),
                "candidate_top_k": candidate_top_k,
                "top_k": top_k,
            },
        )

        if candidate_top_k is not None:
            candidates = candidates[:candidate_top_k]

        ranked = agent.rerank_nodes(
            query=query,
            nodes=candidates,
            top_k=top_k,
            window_size=window_size,
            window_step=window_step,
            include_content=include_content,
        )
        return ranked

    return run


def _embedding_rerank_kernel(context: RerankContext):
    def run(node: ExecutionNode, inputs: List[object]) -> List[QueriedNode]:
        store = context.embedding_store
        if store is None:
            raise RuntimeError(
                "Embedding rerank invoked but no rerank embedding store configured."
            )

        query = _extract_query(node.params, inputs)
        candidates = _collect_candidates(inputs)
        if not candidates:
            logger.warning("Embedding rerank invoked without candidate nodes.")
            return []

        top_k = _resolve_top_k(node.params, context.top_k)
        candidate_top_k = _resolve_candidate_top_k(node.params, context.candidate_top_k)
        if candidate_top_k is not None:
            candidates = candidates[:candidate_top_k]

        # Require content for embedding-based scoring
        candidates_with_content = [c for c in candidates if c.content]
        missing_content = len(candidates) - len(candidates_with_content)
        if missing_content:
            logger.debug(
                "Embedding rerank dropping %d candidates without content",
                missing_content,
            )
        if not candidates_with_content:
            return []

        query_vec = np.array(store.embedding.embed_query(query), dtype=np.float32)
        doc_vectors = np.array(
            store.embedding.embed_documents(
                [cand.content for cand in candidates_with_content]
            ),
            dtype=np.float32,
        )

        scores = _score_embeddings(query_vec, doc_vectors, metric=store.index_metric)
        ranked = sorted(
            zip(scores, candidates_with_content, strict=True),
            key=lambda pair: pair[0],
            reverse=True,
        )

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

    return run


def _extract_query(params: Dict[str, object], inputs: List[object]) -> str:
    for key in ("query", "prompt", "text"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    for payload in inputs:
        if isinstance(payload, dict):
            value = payload.get("query") or payload.get("source_query")
            if isinstance(value, str) and value.strip():
                return value.strip()
    raise ValueError("Rerank operator requires the original query text.")


def _collect_candidates(inputs: List[object]) -> List[NodeInfo]:
    collected: List[NodeInfo] = []
    for payload in inputs:
        if isinstance(payload, NodeInfo) and not isinstance(payload, QueriedNode):
            collected.append(payload)
            continue
        if isinstance(payload, QueriedNode):
            collected.append(
                NodeInfo(
                    node_name=payload.node_name,
                    type=payload.type,
                    file=payload.file,
                    node_id=payload.node_id,
                    start_line=payload.start_line,
                    end_line=payload.end_line,
                    score=payload.score,
                    content=payload.content,
                )
            )
            continue
        if isinstance(payload, dict):
            nodes = (
                payload.get("nodes")
                or payload.get("results")
                or payload.get("candidates")
            )
            if nodes:
                collected.extend(_collect_candidates(list(nodes)))
            continue
        if isinstance(payload, list):
            collected.extend(_collect_candidates(payload))
    return collected


def _resolve_top_k(params: Dict[str, object], default: Optional[int]) -> Optional[int]:
    for key in ("top_k", "limit"):
        value = params.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return default


def _resolve_candidate_top_k(
    params: Dict[str, object], default: Optional[int]
) -> Optional[int]:
    for key in ("candidate_top_k", "candidate_limit"):
        value = params.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return default


def _score_embeddings(
    query_vec: np.ndarray, doc_vecs: np.ndarray, metric: str
) -> List[float]:
    if metric == "ip":
        return np.dot(doc_vecs, query_vec).tolist()
    if metric == "l2":
        return (-np.linalg.norm(doc_vecs - query_vec, axis=1)).tolist()
    raise ValueError(f"Unsupported index metric for rerank: {metric!r}")
