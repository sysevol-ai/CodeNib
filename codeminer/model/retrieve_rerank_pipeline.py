# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

import numpy as np

from ..code_chunker import CodeChunker, RepoChunkingConfig
from ..index.embedding import CodeVectorStore, build_hierarchical_vector_store
from ..index.sparse_idx.bm25_index import BM25CodeIndexer
from ..llm.litellm_chat import LiteLLMChat
from ..log_utils import get_logger
from ..ops.rerank import RerankContext
from ..ops.retrieve import RetrieveContext
from ..types import NodeInfo, QueriedNode

logger = get_logger(__name__)

SUPPORTED_ENGINES = {"dense", "sparse"}
RETRIEVAL_TOP_K = 100


@dataclass(frozen=True)
class RetrieveStageConfig:
    """Declarative configuration for an individual retrieval branch."""

    engine: str = "dense"
    weight: float = 1.0
    top_k: Optional[int] = None
    params: Dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.engine not in SUPPORTED_ENGINES:
            raise ValueError(
                f"Unsupported retrieval engine {self.engine!r}. "
                f"Expected one of: {', '.join(sorted(SUPPORTED_ENGINES))}"
            )
        if self.weight <= 0:
            raise ValueError("Retrieval stage weight must be positive.")


def build_retrieve_plan(mode: str = "dense") -> List[RetrieveStageConfig]:
    """Convenience helper for common retrieval plan presets."""

    normalized = (mode or "dense").strip().lower()
    if normalized == "dense":
        return [RetrieveStageConfig(engine="dense", top_k=RETRIEVAL_TOP_K)]
    if normalized == "sparse":
        return [RetrieveStageConfig(engine="sparse", top_k=RETRIEVAL_TOP_K)]
    if normalized == "hybrid":
        return [
            RetrieveStageConfig(engine="dense", weight=0.6, top_k=RETRIEVAL_TOP_K),
            RetrieveStageConfig(engine="sparse", weight=0.4, top_k=RETRIEVAL_TOP_K),
        ]
    raise ValueError(
        f"Unknown retrieval mode {mode!r}. Choose from: dense, sparse, hybrid."
    )


class RetrieveRerankPipeline:
    r"""Composable code retrieval pipeline built from retrieval + rerank ops.

    The pipeline wires :mod:`codeminer.ops.retrieve` (sparse/dense/hybrid) with
    :mod:`codeminer.ops.rerank` to form a two-stage search baseline. Retrieval
    branches are described through :class:`RetrieveStageConfig` objects, which
    can be combined (e.g., hybrid BM25 + embedding) and weighted before
    feeding results to an LLM reranker.

    Args:
        repo_path: Repository root to index.
        index_path: Directory used for vector index caches.
        retrieval_plan: Optional sequence of :class:`RetrieveStageConfig`.
            When omitted, :func:`build_retrieve_plan` is invoked with the
            provided ``retrieval_mode``.
        retrieval_mode: Shortcut for :func:`build_retrieve_plan`.
        embedding_model / provider / dimension / kwargs: Dense index config.
        rerank_model / provider / temperature / max_tokens: Reranker config.
        languages: Languages to chunk for indexing (default: ["python"]).
        max_lines_per_chunk: Maximum lines per chunk passed to chunker.
        sparse_max_k: Upper bound for BM25 index fan-out; defaults to 128.
        rerank_window_size / rerank_window_step: Sliding window controls for the
            reranker (see :meth:`RerankAgent.rerank_nodes`). When ``None``, the
            reranker considers all candidates at once.
    """

    def __init__(
        self,
        repo_path: str,
        index_path: str,
        *,
        retrieval_plan: Optional[Sequence[RetrieveStageConfig]] = None,
        retrieval_mode: str = "dense",
        retrieval_level: str = "l2",
        embedding_model: str = "nomic-ai/CodeRankEmbed",
        embedding_provider: str = "huggingface",
        embedding_dimension: int = 768,
        embedding_model_kwargs: Optional[dict] = None,
        rerank_model: str = "openai/Qwen/Qwen2.5-Coder-7B",
        rerank_temperature: float = 0.0,
        rerank_max_tokens: int = 2048,
        rerank_strategy: str = "llm",
        rerank_embedding_model: Optional[str] = None,
        rerank_embedding_provider: Optional[str] = None,
        rerank_embedding_dimension: Optional[int] = None,
        rerank_embedding_model_kwargs: Optional[dict] = None,
        rerank_index_metric: str = "ip",
        languages: Optional[List[str]] = None,
        max_lines_per_chunk: int = 100,
        sparse_max_k: int = 128,
        rerank_window_size: Optional[int] = None,
        rerank_window_step: Optional[int] = None,
        rerank_listwise_format: str = "structured",
        enable_rerank: bool = True,
        vector_masks: Optional[Dict[str, Set[str]]] = None,
        rerank_candidate_top_k: Optional[int] = None,
    ) -> None:
        self.repo_path = self._validate_repo(repo_path)
        self.index_path = Path(index_path)
        self.index_path.mkdir(parents=True, exist_ok=True)
        self.languages = languages or ["python"]
        self.max_lines_per_chunk = max_lines_per_chunk
        self._chunks = None
        self.enable_rerank = enable_rerank
        self.index_metric = "ip"
        self.profiler = None
        if retrieval_level not in ("l0", "l2"):
            raise ValueError(
                f"Invalid retrieval_level {retrieval_level!r}. Must be 'l0' or 'l2'."
            )
        self.retrieval_level = retrieval_level

        plan = (
            list(retrieval_plan)
            if retrieval_plan
            else build_retrieve_plan(retrieval_mode)
        )
        if not plan:
            raise ValueError("Retrieval plan must contain at least one stage.")
        self.retrieve_plan: List[RetrieveStageConfig] = plan
        self._default_stage_top_k = (
            max((stage.top_k or 0) for stage in self.retrieve_plan) or 32
        )

        self.vector_store: Optional[CodeVectorStore] = None
        self.bm25_index: Optional[BM25CodeIndexer] = None
        self.rerank_vector_store: Optional[CodeVectorStore] = None

        embedding_kwargs = self._prepare_embedding_kwargs(embedding_model_kwargs)
        if self._needs_engine("dense"):
            self.vector_store = self._initialize_vector_store(
                embedding_model=embedding_model,
                embedding_provider=embedding_provider,
                embedding_dimension=embedding_dimension,
                embedding_kwargs=embedding_kwargs,
            )

        if self._needs_engine("sparse"):
            sparse_cap = max(
                sparse_max_k,
                max(
                    (stage.top_k or self._default_stage_top_k)
                    for stage in self.retrieve_plan
                    if stage.engine == "sparse"
                ),
            )
            self.bm25_index = self._initialize_bm25_index(max_k=sparse_cap)

        strategy = (rerank_strategy or "llm").strip().lower()
        if strategy not in ("llm", "embedding"):
            raise ValueError("rerank_strategy must be 'llm' or 'embedding'.")
        self.rerank_strategy = strategy

        rerank_llm = None
        if strategy == "llm":
            rerank_llm = LiteLLMChat(
                model=rerank_model,
                max_tokens=rerank_max_tokens,
                temperature=rerank_temperature,
            )
        else:
            rerank_model_kwargs = self._prepare_embedding_kwargs(
                rerank_embedding_model_kwargs
            )
            self.rerank_vector_store = self._initialize_rerank_vector_store(
                embedding_model=rerank_embedding_model or embedding_model,
                embedding_provider=rerank_embedding_provider or embedding_provider,
                embedding_dimension=rerank_embedding_dimension or embedding_dimension,
                embedding_kwargs=rerank_model_kwargs,
                index_metric=rerank_index_metric,
            )

        self.rerank_window_size = (
            rerank_window_size
            if rerank_window_size and rerank_window_size > 0
            else None
        )
        self.rerank_window_step = (
            rerank_window_step
            if rerank_window_step and rerank_window_step > 0
            else None
        )
        self.rerank_candidate_top_k = (
            rerank_candidate_top_k
            if rerank_candidate_top_k and rerank_candidate_top_k > 0
            else None
        )

        self.retrieve_context = RetrieveContext(
            bm25=self.bm25_index,
            vector_store=self.vector_store,
            regex_index=None,
            default_top_k=self._default_stage_top_k,
            default_level=self.retrieval_level,
            masks=vector_masks or {},
        )

        if self.enable_rerank:
            self.rerank_context = RerankContext(
                llm=rerank_llm,
                embedding_store=self.rerank_vector_store or self.vector_store,
                candidate_top_k=self.rerank_candidate_top_k,
                window_size=rerank_window_size,
                window_step=rerank_window_step,
                listwise_format=rerank_listwise_format,
            )
        else:
            self.rerank_context = None

        logger.info(
            "RetrieveRerankPipeline initialized",
            extra={
                "repo": self.repo_path,
                "index_path": str(self.index_path),
                "retrieval_plan": [stage.engine for stage in self.retrieve_plan],
                "retrieval_level": self.retrieval_level,
                "dense_index": bool(self.vector_store),
                "sparse_index": bool(self.bm25_index),
                "rerank_strategy": (
                    self.rerank_strategy if self.enable_rerank else "disabled"
                ),
                "enable_rerank": self.enable_rerank,
            },
        )

    def close(self) -> None:
        """Release model/index resources held by the pipeline."""
        if self.vector_store is not None:
            self.vector_store.close()
        if self.rerank_vector_store is not None:
            self.rerank_vector_store.close()
        if self.bm25_index is not None:
            self.bm25_index.documents = []
            self.bm25_index.nodes = []
            self.bm25_index.retriever = None
            self.bm25_index = None

        self.vector_store = None
        self.rerank_vector_store = None
        self._chunks = None

        try:
            import gc

            import torch

            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    def query(self, query: str, top_k: int = 10) -> List[QueriedNode]:
        """Execute retrieve + rerank plan for the provided query.

        Args:
            query: The search query.
            top_k: Number of results to return. If rerank is enabled, this controls
                the rerank output. Retrieval always fetches RETRIEVAL_TOP_K
                candidates internally.
        """
        # Step 1: Run each retrieval branch
        branch_results: List[List[QueriedNode]] = []
        for stage in self.retrieve_plan:
            results = self._run_retrieval_stage(query, stage)
            branch_results.append(results)

        # Step 2: Merge branches (hybrid if multiple)
        if len(branch_results) == 1:
            candidates = branch_results[0]
        else:
            weights = [stage.weight for stage in self.retrieve_plan]
            candidates = self._merge_hybrid(branch_results, weights, RETRIEVAL_TOP_K)

        # Step 3: Rerank if enabled
        if self.enable_rerank and self.rerank_context is not None:
            candidates = self._run_rerank(query, candidates, top_k)

        return candidates[:top_k]

    # --------------------------------------------------------------------- #
    # Retrieval
    # --------------------------------------------------------------------- #

    def _run_retrieval_stage(
        self, query: str, stage: RetrieveStageConfig
    ) -> List[QueriedNode]:
        """Execute a single retrieval branch."""
        stage_top_k = stage.top_k or RETRIEVAL_TOP_K

        if stage.engine == "dense":
            return self._retrieve_dense(query, stage_top_k)
        if stage.engine == "sparse":
            return self._retrieve_sparse(query, stage_top_k)
        raise ValueError(f"Unsupported engine {stage.engine!r}.")

    def _retrieve_dense(self, query: str, top_k: int) -> List[QueriedNode]:
        """Retrieve via vector store (FAISS)."""
        store = self.retrieve_context.vector_store
        if store is None:
            raise RuntimeError("Dense retrieval requested but no vector store.")

        results = store.search_with_content(
            query=query,
            top_k=top_k,
            level=self.retrieval_level,
        )
        return _to_queried_nodes(results)

    def _retrieve_sparse(self, query: str, top_k: int) -> List[QueriedNode]:
        """Retrieve via BM25 index."""
        index = self.retrieve_context.bm25
        if index is None:
            raise RuntimeError("Sparse retrieval requested but no BM25 index.")

        raw_results = index.search(
            query=query,
            top_k=top_k,
            return_code_content=True,
            wrap_with_ln=True,
        )
        return _to_queried_nodes(raw_results)

    @staticmethod
    def _merge_hybrid(
        branches: List[List[QueriedNode]],
        weights: List[float],
        top_k: int,
    ) -> List[QueriedNode]:
        """Merge multiple retrieval branches with weighted scoring."""
        if not weights or len(weights) != len(branches):
            weights = [1.0] * len(branches)

        accumulator: Dict[
            Tuple[Optional[str], str, Optional[int], Optional[int]], QueriedNode
        ] = {}

        for weight, results in zip(weights, branches, strict=True):
            for rank, item in enumerate(results):
                base_score = item.score or 0.0
                if base_score == 0.0:
                    base_score = 1.0 / (rank + 1)

                key = (item.file, item.node_name, item.start_line, item.end_line)
                weighted = weight * base_score

                if key not in accumulator:
                    accumulator[key] = _with_score(item, weighted)
                else:
                    existing = accumulator[key]
                    new_score = existing.score + weighted
                    content = existing.content or item.content
                    accumulator[key] = _with_score(existing, new_score, content)

        merged = sorted(
            accumulator.values(),
            key=lambda node: node.score,
            reverse=True,
        )
        return merged[:top_k]

    # --------------------------------------------------------------------- #
    # Reranking
    # --------------------------------------------------------------------- #

    def _run_rerank(
        self, query: str, candidates: List[QueriedNode], top_k: int
    ) -> List[QueriedNode]:
        """Rerank candidates using the configured strategy."""
        if not candidates:
            return []

        if self.rerank_candidate_top_k is not None:
            candidates = candidates[: self.rerank_candidate_top_k]

        if self.rerank_strategy == "embedding":
            return self._rerank_embedding(query, candidates, top_k)
        return self._rerank_llm(query, candidates, top_k)

    def _rerank_llm(
        self, query: str, candidates: List[QueriedNode], top_k: int
    ) -> List[QueriedNode]:
        """Rerank using LLM listwise reranker."""
        agent = self.rerank_context.ensure_agent()
        logger.info(
            "Running LLM rerank.",
            extra={"candidate_count": len(candidates), "top_k": top_k},
        )

        nodes = [
            NodeInfo(
                node_name=c.node_name,
                type=c.type,
                file=c.file,
                node_id=c.node_id,
                start_line=c.start_line,
                end_line=c.end_line,
                score=c.score,
                content=c.content,
            )
            for c in candidates
        ]

        return agent.rerank_nodes(
            query=query,
            nodes=nodes,
            top_k=top_k,
            window_size=self.rerank_window_size,
            window_step=self.rerank_window_step,
            include_content=True,
        )

    def _rerank_embedding(
        self, query: str, candidates: List[QueriedNode], top_k: int
    ) -> List[QueriedNode]:
        """Rerank using embedding similarity."""
        store = self.rerank_context.embedding_store
        if store is None:
            raise RuntimeError("Embedding rerank requested but no embedding store.")

        candidates_with_content = [c for c in candidates if c.content]
        if not candidates_with_content:
            return []

        query_vec = np.array(store.embedding.embed_query(query), dtype=np.float32)
        doc_vectors = np.array(
            store.embedding.embed_documents(
                [c.content for c in candidates_with_content]
            ),
            dtype=np.float32,
        )

        metric = store.index_metric
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

        return [
            QueriedNode(
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

    # --------------------------------------------------------------------- #
    # Internal helpers
    # --------------------------------------------------------------------- #

    def _validate_repo(self, repo_path: str) -> str:
        resolved = os.path.abspath(repo_path)
        if not (os.path.exists(resolved) and os.path.isdir(resolved)):
            raise ValueError(
                "Repository path is invalid (does not exist or is not a directory): "
                f"{resolved}"
            )
        return resolved

    def _prepare_embedding_kwargs(
        self, embedding_model_kwargs: Optional[dict]
    ) -> Dict[str, object]:
        if not embedding_model_kwargs:
            return {}
        embedding_kwargs: Dict[str, object] = {}
        if "model_kwargs" in embedding_model_kwargs:
            embedding_kwargs["model_kwargs"] = embedding_model_kwargs["model_kwargs"]
        if "encode_kwargs" in embedding_model_kwargs:
            embedding_kwargs["encode_kwargs"] = embedding_model_kwargs["encode_kwargs"]
        if embedding_model_kwargs.get("trust_remote_code"):
            model_kwargs = embedding_kwargs.setdefault("model_kwargs", {})
            model_kwargs["trust_remote_code"] = True
        return embedding_kwargs

    def _needs_engine(self, engine: str) -> bool:
        return any(stage.engine == engine for stage in self.retrieve_plan)

    def _initialize_vector_store(
        self,
        *,
        embedding_model: str,
        embedding_provider: str,
        embedding_dimension: int,
        embedding_kwargs: Dict[str, object],
    ) -> CodeVectorStore:
        vector_store = CodeVectorStore(
            embedding_model=embedding_model,
            embedding_provider=embedding_provider,
            dimension=embedding_dimension,
            store_path=str(self.index_path),
            **embedding_kwargs,
        )
        model_suffix = embedding_model.replace("/", "__")
        config_file = self.index_path / f"config_{model_suffix}.json"
        l0_path = self.index_path / "l0"
        l2_path = self.index_path / "l2"

        cache_exists = config_file.exists() or (l0_path.exists() and l2_path.exists())
        if cache_exists:
            logger.info(
                "Loading hierarchical vector store from cache.",
                extra={"index_path": str(self.index_path)},
            )
            vector_store.load(str(self.index_path))
            missing_levels = []
            if not vector_store.l0_documents:
                missing_levels.append("l0")
            if self.retrieval_level == "l2" and not vector_store.l2_documents:
                missing_levels.append("l2")

            if not missing_levels:
                return vector_store

            logger.info(
                "Cached vector store missing required levels %s; rebuilding.",
                missing_levels,
                extra={"index_path": str(self.index_path)},
            )
            vector_store.clear()

        logger.info("Building hierarchical vector store index.")
        vector_store = build_hierarchical_vector_store(
            repo_path=self.repo_path,
            index_path=str(self.index_path),
            plan_name=None,
            languages=self.languages,
            max_lines_per_chunk=self.max_lines_per_chunk,
            build_levels=["l0", "l2"],
            embedding_model=embedding_model,
            embedding_provider=embedding_provider,
            embedding_dimension=embedding_dimension,
            embedding_kwargs=embedding_kwargs,
            index_metric=self.index_metric,
            profiler=self.profiler,
        )
        return vector_store

    def _initialize_rerank_vector_store(
        self,
        *,
        embedding_model: str,
        embedding_provider: str,
        embedding_dimension: int,
        embedding_kwargs: Dict[str, object],
        index_metric: str,
    ) -> CodeVectorStore:
        if (
            self.vector_store
            and self.vector_store.embedding_model == embedding_model
            and self.vector_store.embedding_provider == embedding_provider
        ):
            return self.vector_store

        logger.info(
            "Initializing rerank embedding store",
            extra={
                "model": embedding_model,
                "provider": embedding_provider,
                "index_metric": index_metric,
            },
        )
        return CodeVectorStore(
            embedding_model=embedding_model,
            embedding_provider=embedding_provider,
            dimension=embedding_dimension,
            index_metric=index_metric,
            store_path=str(self.index_path),
            **embedding_kwargs,
        )

    def _initialize_bm25_index(self, *, max_k: int) -> BM25CodeIndexer:
        logger.info("Building BM25 index.", extra={"max_k": max_k})
        chunks = self._ensure_chunks()
        return BM25CodeIndexer(chunks=chunks, max_k=max_k)

    def _ensure_chunks(self):
        if self._chunks is not None:
            return self._chunks
        primary_language = self.languages[0] if self.languages else "python"
        chunker = CodeChunker(
            language=primary_language,
            repo_config=RepoChunkingConfig(languages=self.languages),
            max_lines_per_chunk=self.max_lines_per_chunk,
        )
        chunks = chunker.chunk_repository(repo_path=self.repo_path)
        if not chunks:
            raise ValueError("No code chunks generated from repository.")
        self._chunks = chunks
        return self._chunks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_queried_nodes(results: Sequence[object]) -> List[QueriedNode]:
    """Normalize retrieval results to QueriedNode."""
    converted: List[QueriedNode] = []
    for rank, item in enumerate(results):
        if isinstance(item, QueriedNode):
            converted.append(item)
            continue
        if isinstance(item, NodeInfo):
            data = _dump_model(item)
            score = data.get("score") or 0.0
            if not score:
                score = 1.0 / (rank + 1)
            data["score"] = score
            converted.append(QueriedNode(**data))
            continue
        if isinstance(item, dict):
            data = dict(item)
            score = data.get("score") or 0.0
            if not score:
                score = 1.0 / (rank + 1)
            data["score"] = score
            data.setdefault("node_name", data.get("name", ""))
            data.setdefault("content", data.get("content"))
            converted.append(QueriedNode(**data))
            continue
        raise TypeError(f"Unsupported result type: {type(item)}")
    return converted


def _with_score(
    item: QueriedNode, score: float, content_override: Optional[str] = None
) -> QueriedNode:
    """Create a copy of a QueriedNode with an updated score."""
    update = {"score": score}
    if content_override is not None:
        update["content"] = content_override
    if hasattr(item, "model_copy"):
        return item.model_copy(update=update)
    if hasattr(item, "copy"):
        return item.copy(update=update)
    data = _dump_model(item)
    data.update(update)
    return QueriedNode(**data)


def _dump_model(model: object) -> Dict[str, object]:
    if hasattr(model, "model_dump"):
        return dict(model.model_dump())
    if hasattr(model, "dict"):
        return dict(model.dict())
    raise TypeError(f"Object {model} does not support model dumping.")
