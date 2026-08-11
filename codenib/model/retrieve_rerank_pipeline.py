# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Set

from ..code_chunker import CodeChunker, RepoChunkingConfig
from ..graph.code_graph import CodeGraph
from ..index.embedding import CodeVectorStore, build_hierarchical_vector_store
from ..index.embedding._lifecycle import close_vector_after_failure
from ..index.embedding.artifact_integrity import require_authorized_vector_view
from ..index.embedding.model_policy import resolve_embedding_load_policy_from_options
from ..index.rerank.cross_encoder import build_reranker
from ..index.sparse_idx.bm25_index import BM25CodeIndexer
from ..llm.litellm_chat import LiteLLMChat
from ..log_utils import get_logger
from ..ops.expand import ExpandContext, expand_retrieval_candidates
from ..ops.rerank import RerankContext, rerank_by_embedding
from ..ops.retrieve import (
    RetrieveContext,
    execute_retrieval_stages,
    merge_hybrid,
    to_queried_nodes,
)
from ..provider_routes import normalize_provider
from ..types import NodeInfo, QueriedNode
from .retrieval_planner import (
    BudgetInput,
    RetrievalBudget,
    RetrievalCapabilities,
    RetrievalPathPlan,
    RetrievalPlanner,
    RetrievalStagePlan,
    build_planner_preload_stages,
)

logger = get_logger(__name__)

if TYPE_CHECKING:
    from ..native_index_authorization import NativeIndexAuthorization

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
    """Convenience helper for common retrieval plan presets.

    ``auto`` returns the dense+sparse preload set used by
    :class:`RetrievalPlanner`; the per-query path is selected at query time.
    """

    normalized = (mode or "dense").strip().lower()
    if normalized == "dense":
        return [RetrieveStageConfig(engine="dense", top_k=RETRIEVAL_TOP_K)]
    if normalized == "sparse":
        return [RetrieveStageConfig(engine="sparse", top_k=RETRIEVAL_TOP_K)]
    if normalized == "auto":
        return [
            _stage_config_from_planner(stage)
            for stage in build_planner_preload_stages(top_k=128)
        ]
    if normalized == "hybrid":
        return [
            RetrieveStageConfig(engine="dense", weight=0.6, top_k=RETRIEVAL_TOP_K),
            RetrieveStageConfig(engine="sparse", weight=0.4, top_k=RETRIEVAL_TOP_K),
        ]
    raise ValueError(
        f"Unknown retrieval mode {mode!r}. Choose from: auto, dense, sparse, hybrid."
    )


class RetrieveRerankPipeline:
    r"""Composable code retrieval pipeline built from retrieval + rerank ops.

    The pipeline wires :mod:`codenib.ops.retrieve` (sparse/dense/hybrid) with
    :mod:`codenib.ops.rerank` to form a two-stage search baseline. Retrieval
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
        retrieval_level: Context expansion level, either ``"l0"`` or ``"l2"``.
        planning_budget: Budget used by automatic retrieval planning.
        retrieval_planner: Planner used when ``retrieval_mode`` is ``"auto"``.
        planner_capabilities: Optional capability override supplied to the planner.
        code_graph: Graph used to expand retrieved code context.
        fusion_strategy: Strategy used to combine multiple retrieval branches.
        rrf_k: Rank constant used by reciprocal-rank fusion.
        embedding_model: Dense embedding model identifier.
        embedding_provider: Backend serving ``embedding_model``.
        embedding_dimension: Dense vector width.
        embedding_model_kwargs: Extra keyword arguments for the dense encoder.
        rerank_model: Reranker model identifier.
        rerank_temperature: Sampling temperature for the LLM reranker.
        rerank_max_tokens: Token budget for a single rerank call.
        rerank_strategy: Reranking backend: ``"llm"``, ``"embedding"``, or
            ``"crossencoder"``.
        rerank_embedding_model: Model used by embedding-based reranking.
        rerank_embedding_provider: Backend for ``rerank_embedding_model``.
        rerank_embedding_dimension: Vector width for embedding-based reranking.
        rerank_embedding_model_kwargs: Extra keyword arguments for the rerank
            embedding model.
        rerank_index_metric: Similarity metric for the rerank vector index.
        crossencoder_model: Model used by cross-encoder reranking.
        crossencoder_batch_size: Batch size used by the cross-encoder.
        crossencoder_revision: Optional Hugging Face model revision.
        crossencoder_trust_remote_code: Whether to execute model repository
            code. Remote models require an immutable commit revision.
        languages: Languages to chunk for indexing (default: ["python"]).
        max_lines_per_chunk: Maximum lines per chunk passed to chunker.
        sparse_max_k: Upper bound for BM25 index fan-out; defaults to 128.
        rerank_window_size: Sliding window width for the reranker (see
            :meth:`RerankAgent.rerank_nodes`). When ``None``, the reranker
            considers all candidates at once.
        rerank_window_step: Stride between consecutive rerank windows.
        rerank_listwise_format: Output format requested from listwise reranking.
        enable_rerank: Whether to apply the configured reranking stage.
        vector_masks: Optional per-language symbol masks for vector retrieval.
        rerank_candidate_top_k: Maximum candidates passed into reranking.
    """

    def __init__(
        self,
        repo_path: str,
        index_path: str,
        *,
        retrieval_plan: Optional[Sequence[RetrieveStageConfig]] = None,
        retrieval_mode: str = "dense",
        retrieval_level: str = "l2",
        planning_budget: BudgetInput = "balanced",
        retrieval_planner: Optional[RetrievalPlanner] = None,
        planner_capabilities: Optional[RetrievalCapabilities] = None,
        code_graph: Optional[CodeGraph] = None,
        fusion_strategy: str = "weighted",
        rrf_k: int = 60,
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
        crossencoder_model: str = "Qwen/Qwen3-Reranker-0.6B",
        crossencoder_batch_size: int = 8,
        crossencoder_revision: Optional[str] = None,
        crossencoder_trust_remote_code: bool = False,
        languages: Optional[List[str]] = None,
        max_lines_per_chunk: int = 100,
        sparse_max_k: int = 128,
        rerank_window_size: Optional[int] = None,
        rerank_window_step: Optional[int] = None,
        rerank_listwise_format: str = "structured",
        enable_rerank: bool = True,
        vector_masks: Optional[Dict[str, Set[str]]] = None,
        rerank_candidate_top_k: Optional[int] = None,
        native_index_authorization: NativeIndexAuthorization | None = None,
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
        self._native_index_authorization = native_index_authorization
        self.retrieval_mode = (retrieval_mode or "dense").strip().lower()
        self.retrieval_planner = retrieval_planner
        if self.retrieval_planner is None and self.retrieval_mode == "auto":
            self.retrieval_planner = RetrievalPlanner()
        self.planning_budget = planning_budget
        self.fusion_strategy = _normalize_fusion_strategy(fusion_strategy)
        if rrf_k <= 0:
            raise ValueError("rrf_k must be positive.")
        self.rrf_k = rrf_k
        self.last_selected_plan: Optional[RetrievalPathPlan] = None
        self.last_query_signals = None
        self.last_planning_budget: Optional[RetrievalBudget] = None
        self.last_planner_capabilities: Optional[RetrievalCapabilities] = None
        self.last_planner_trace: Dict[str, object] = {}
        self._planner_capabilities_override = planner_capabilities
        self.expand_context = ExpandContext(code_graph=code_graph)
        if retrieval_level not in ("l0", "l2"):
            raise ValueError(
                f"Invalid retrieval_level {retrieval_level!r}. Must be 'l0' or 'l2'."
            )
        self.retrieval_level = retrieval_level

        if retrieval_plan:
            plan = list(retrieval_plan)
        elif self.retrieval_mode == "auto" and self.retrieval_planner is not None:
            preload_top_k = max(
                128,
                self.retrieval_planner.normalize_budget(
                    self.planning_budget
                ).retrieve_top_k,
            )
            plan = [
                _stage_config_from_planner(stage)
                for stage in build_planner_preload_stages(top_k=preload_top_k)
            ]
        else:
            plan = build_retrieve_plan(self.retrieval_mode)
        if not plan:
            raise ValueError("Retrieval plan must contain at least one stage.")
        self.retrieve_plan: List[RetrieveStageConfig] = plan
        self._default_stage_top_k = (
            max((stage.top_k or 0) for stage in self.retrieve_plan) or 32
        )

        self.vector_store: Optional[CodeVectorStore] = None
        self.bm25_index: Optional[BM25CodeIndexer] = None
        self.rerank_vector_store: Optional[CodeVectorStore] = None

        embedding_kwargs = self._prepare_embedding_kwargs(
            embedding_provider,
            embedding_model_kwargs,
        )
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
        if strategy not in ("llm", "embedding", "crossencoder"):
            raise ValueError(
                "rerank_strategy must be 'llm', 'embedding', or 'crossencoder'."
            )
        self.rerank_strategy = strategy

        rerank_llm = None
        self.cross_encoder = None
        if strategy == "llm":
            rerank_llm = LiteLLMChat(
                model=rerank_model,
                max_tokens=rerank_max_tokens,
                temperature=rerank_temperature,
            )
        elif strategy == "crossencoder":
            self.cross_encoder = build_reranker(
                crossencoder_model,
                batch_size=crossencoder_batch_size,
                revision=crossencoder_revision,
                trust_remote_code=crossencoder_trust_remote_code,
            )
            logger.info(
                "Cross-encoder reranker loaded",
                extra={"model": crossencoder_model},
            )
        else:
            selected_rerank_provider = rerank_embedding_provider or embedding_provider
            rerank_model_kwargs = self._prepare_embedding_kwargs(
                selected_rerank_provider, rerank_embedding_model_kwargs
            )
            self.rerank_vector_store = self._initialize_rerank_vector_store(
                embedding_model=rerank_embedding_model or embedding_model,
                embedding_provider=selected_rerank_provider,
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

        self.planner_capabilities = (
            self._planner_capabilities_override
            if self._planner_capabilities_override is not None
            else RetrievalCapabilities(
                has_dense=bool(self.vector_store),
                has_sparse=bool(self.bm25_index),
                has_graph=bool(self.expand_context.code_graph),
                has_embedding_rerank=bool(self.vector_store),
                has_llm_rerank=strategy == "llm",
            )
        )

        logger.info(
            "RetrieveRerankPipeline initialized",
            extra={
                "repo": self.repo_path,
                "index_path": str(self.index_path),
                "retrieval_mode": self.retrieval_mode,
                "retrieval_plan": [stage.engine for stage in self.retrieve_plan],
                "retrieval_level": self.retrieval_level,
                "dense_index": bool(self.vector_store),
                "sparse_index": bool(self.bm25_index),
                "graph_index": bool(self.expand_context.code_graph),
                "planner": bool(self.retrieval_planner),
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

        if self.cross_encoder is not None:
            try:
                self.cross_encoder.close()
            except Exception:
                pass
            self.cross_encoder = None

        try:
            import gc

            import torch

            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    def query(
        self,
        query: str,
        top_k: int = 10,
        *,
        budget: Optional[BudgetInput] = None,
    ) -> List[QueriedNode]:
        """Execute retrieve + rerank plan for the provided query.

        Args:
            query: The search query.
            top_k: Number of results to return. If rerank is enabled, this controls
                the rerank output. Retrieval always fetches RETRIEVAL_TOP_K
                candidates internally.
            budget: Optional per-query planner budget. Only used when a
                retrieval planner is configured, including ``retrieval_mode=auto``.
        """
        selected_plan = self._select_query_plan(query, budget)
        active_plan = (
            [_stage_config_from_planner(stage) for stage in selected_plan.stages]
            if selected_plan is not None
            else self.retrieve_plan
        )
        fusion = selected_plan.fusion if selected_plan is not None else None
        merge_top_k = (
            selected_plan.retrieval_top_k
            if selected_plan is not None
            else RETRIEVAL_TOP_K
        )

        candidates = execute_retrieval_stages(
            query,
            active_plan,
            self._run_retrieval_stage,
            top_k=merge_top_k,
            fusion=fusion or self.fusion_strategy,
            rrf_k=self.rrf_k,
        )

        if selected_plan is not None and selected_plan.graph is not None:
            candidates = self._run_graph_expansion_plan(selected_plan, candidates)

        # Step 3: Rerank if enabled
        plan_allows_rerank = (
            selected_plan.enable_rerank if selected_plan is not None else True
        )
        if (
            self.enable_rerank
            and plan_allows_rerank
            and self.rerank_context is not None
        ):
            candidates = self._run_rerank(
                query,
                candidates,
                top_k,
                strategy=(
                    selected_plan.rerank_strategy
                    if selected_plan is not None
                    else self.rerank_strategy
                ),
                candidate_top_k=self._effective_rerank_candidate_top_k(selected_plan),
            )

        return candidates[:top_k]

    def _effective_rerank_candidate_top_k(
        self, selected_plan: Optional[RetrievalPathPlan]
    ) -> Optional[int]:
        if self.rerank_candidate_top_k is not None:
            return self.rerank_candidate_top_k
        if selected_plan is not None:
            return selected_plan.rerank_candidate_top_k
        return None

    def _select_query_plan(
        self, query: str, budget: Optional[BudgetInput]
    ) -> Optional[RetrievalPathPlan]:
        if self.retrieval_planner is None:
            self.last_selected_plan = None
            self.last_query_signals = None
            self.last_planning_budget = None
            self.last_planner_capabilities = None
            self.last_planner_trace = {}
            return None
        active_budget = self.retrieval_planner.normalize_budget(
            budget or self.planning_budget
        )
        signals = self.retrieval_planner.classify_query(query)
        selected = self.retrieval_planner.select(
            query=query,
            budget=active_budget,
            capabilities=self.planner_capabilities,
        )
        self.last_selected_plan = selected
        self.last_query_signals = signals
        self.last_planning_budget = active_budget
        self.last_planner_capabilities = self.planner_capabilities
        self.last_planner_trace = _planner_trace(
            signals=signals,
            budget=active_budget,
            capabilities=self.planner_capabilities,
            plan=selected,
        )
        logger.debug(
            "Planner selected retrieval path",
            extra={
                "plan": selected.name,
                "intent": selected.intent,
                "fusion": selected.fusion,
                "rerank": selected.rerank_strategy if selected.enable_rerank else None,
                "signals": self.last_planner_trace["signals"],
                "budget": self.last_planner_trace["budget"],
                "capabilities": self.last_planner_trace["capabilities"],
                "rationale": selected.rationale,
            },
        )
        return selected

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

    def _run_graph_expansion_plan(
        self,
        selected_plan: RetrievalPathPlan,
        seeds: List[QueriedNode],
    ) -> List[QueriedNode]:
        """Execute the planner's graph expansion plan over retrieved seeds."""
        graph_plan = selected_plan.graph
        if graph_plan is None:
            return seeds
        return expand_retrieval_candidates(
            self.expand_context,
            seeds,
            seed_top_k=graph_plan.seed_top_k,
            expand_top_k=graph_plan.expand_top_k,
            hops=graph_plan.hops,
            direction=graph_plan.direction,
            use_ppr=graph_plan.use_ppr,
            repo_path=self.repo_path,
            include_content=True,
        )

    @staticmethod
    def _merge_hybrid(
        branches: List[List[QueriedNode]],
        weights: List[float],
        top_k: int,
        *,
        fusion: str = "weighted",
        rrf_k: int = 60,
    ) -> List[QueriedNode]:
        """Merge multiple retrieval branches with weighted or RRF scoring."""
        return merge_hybrid(
            branches,
            weights,
            top_k=top_k,
            fusion=fusion,
            rrf_k=rrf_k,
        )

    @staticmethod
    def _merge_rrf(
        branches: List[List[QueriedNode]],
        weights: List[float],
        top_k: int,
        *,
        rrf_k: int = 60,
    ) -> List[QueriedNode]:
        """Merge branches with weighted reciprocal-rank fusion."""
        return merge_hybrid(
            branches,
            weights,
            top_k=top_k,
            fusion="rrf",
            rrf_k=rrf_k,
        )

    # --------------------------------------------------------------------- #
    # Reranking
    # --------------------------------------------------------------------- #

    def _run_rerank(
        self,
        query: str,
        candidates: List[QueriedNode],
        top_k: int,
        *,
        strategy: Optional[str] = None,
        candidate_top_k: Optional[int] = None,
    ) -> List[QueriedNode]:
        """Rerank candidates using the configured strategy."""
        if not candidates:
            return []

        if candidate_top_k is not None:
            candidates = candidates[:candidate_top_k]

        active_strategy = strategy or self.rerank_strategy
        if active_strategy == "crossencoder":
            return self._rerank_crossencoder(query, candidates, top_k)
        if active_strategy == "embedding":
            return self._rerank_embedding(query, candidates, top_k)
        if active_strategy != "llm":
            raise ValueError(
                "rerank strategy must be 'llm', 'embedding', or 'crossencoder'."
            )
        return self._rerank_llm(query, candidates, top_k)

    def _rerank_crossencoder(
        self, query: str, candidates: List[QueriedNode], top_k: int
    ) -> List[QueriedNode]:
        """Rerank using cross-encoder (STCrossEncoderWrapper or QwenRerankerWrapper)."""
        if self.cross_encoder is None:
            raise RuntimeError(
                "Cross-encoder rerank requested but no wrapper was built."
            )

        candidates_with_content = [c for c in candidates if c.content]
        if not candidates_with_content:
            return candidates[:top_k]

        logger.info(
            "Running cross-encoder rerank.",
            extra={
                "candidate_count": len(candidates_with_content),
                "top_k": top_k,
            },
        )

        docs = [c.content for c in candidates_with_content if c.content]
        scores = self.cross_encoder.score(query, docs)

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
        return rerank_by_embedding(query, candidates, store, top_k=top_k)

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
        self,
        embedding_provider: str,
        embedding_model_kwargs: Optional[dict],
    ) -> Dict[str, object]:
        if not embedding_model_kwargs:
            return {}
        if normalize_provider(embedding_provider) != "huggingface":
            unsupported = sorted(
                set(embedding_model_kwargs)
                & {
                    "config_kwargs",
                    "default_batch_size",
                    "encode_kwargs",
                    "max_seq_length",
                    "model_kwargs",
                    "revision",
                    "tokenizer_kwargs",
                    "trust_remote_code",
                }
            )
            if unsupported:
                raise ValueError(
                    "Hugging Face embedding options require the huggingface "
                    f"provider: {', '.join(unsupported)}"
                )
        embedding_kwargs: Dict[str, object] = {}
        if "model_kwargs" in embedding_model_kwargs:
            embedding_kwargs["model_kwargs"] = embedding_model_kwargs["model_kwargs"]
        if "encode_kwargs" in embedding_model_kwargs:
            embedding_kwargs["encode_kwargs"] = embedding_model_kwargs["encode_kwargs"]
        if "revision" in embedding_model_kwargs:
            embedding_kwargs["revision"] = embedding_model_kwargs["revision"]
        if "trust_remote_code" in embedding_model_kwargs:
            embedding_kwargs["trust_remote_code"] = embedding_model_kwargs[
                "trust_remote_code"
            ]
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
        model_suffix = embedding_model.replace("/", "__")
        config_file = self.index_path / f"config_{model_suffix}.json"
        l0_path = self.index_path / "l0"
        l2_path = self.index_path / "l2"

        cache_exists = config_file.exists() or (l0_path.exists() and l2_path.exists())
        force_rebuild = False
        authorization = getattr(self, "_native_index_authorization", None)
        if cache_exists and authorization is not None:
            require_authorized_vector_view(
                self.index_path,
                authorization,
                {},
            )

        vector_store = CodeVectorStore(
            embedding_model=embedding_model,
            embedding_provider=embedding_provider,
            dimension=embedding_dimension,
            store_path=str(self.index_path),
            **embedding_kwargs,
        )
        if cache_exists and authorization is not None:
            logger.info(
                "Loading hierarchical vector store from cache.",
                extra={"index_path": str(self.index_path)},
            )
            try:
                vector_store.load(
                    str(self.index_path),
                    native_index_authorization=authorization,
                )
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
                force_rebuild = True
            except BaseException as primary:  # noqa: B036 - close partial state
                close_vector_after_failure(vector_store, primary)
                raise
        elif cache_exists:
            logger.warning(
                "Cached vector store at %s has no external native-index "
                "authorization; rebuilding from repository source.",
                self.index_path,
            )
            force_rebuild = True

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
            embedding=vector_store.embedding,
            index_metric=self.index_metric,
            profiler=self.profiler,
            force_rebuild=force_rebuild,
            native_index_authorization=authorization,
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
        canonical_provider = normalize_provider(embedding_provider)
        requested_policy = (
            resolve_embedding_load_policy_from_options(
                embedding_model,
                embedding_kwargs,
            )
            if canonical_provider == "huggingface"
            else None
        )
        if (
            self.vector_store
            and self.vector_store.embedding_model == embedding_model
            and normalize_provider(self.vector_store.embedding_provider)
            == canonical_provider
            and self.vector_store.dimension == embedding_dimension
            and getattr(self.vector_store, "embedding_revision", None)
            == (requested_policy.revision if requested_policy else None)
            and getattr(self.vector_store, "embedding_trust_remote_code", False)
            == bool(requested_policy and requested_policy.trust_remote_code)
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


def _stage_config_from_planner(stage: RetrievalStagePlan) -> RetrieveStageConfig:
    return RetrieveStageConfig(
        engine=stage.engine,
        weight=stage.weight,
        top_k=stage.top_k,
        params=dict(stage.params),
    )


def _normalize_fusion_strategy(value: Optional[str]) -> str:
    normalized = (value or "weighted").strip().lower().replace("-", "_")
    aliases = {
        "linear": "weighted",
        "linear_combination": "weighted",
        "weighted_sum": "weighted",
        "none": "weighted",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"weighted", "rrf"}:
        raise ValueError("fusion_strategy must be 'weighted' or 'rrf'.")
    return normalized


def _to_queried_nodes(results: Sequence[object]) -> List[QueriedNode]:
    """Normalize retrieval results to QueriedNode."""
    return to_queried_nodes(results)


def _planner_trace(
    *,
    signals,
    budget: RetrievalBudget,
    capabilities: RetrievalCapabilities,
    plan: RetrievalPathPlan,
) -> Dict[str, object]:
    return {
        "signals": {
            "lexical": signals.lexical,
            "semantic": signals.semantic,
            "structural": signals.structural,
            "mixed": signals.mixed,
        },
        "budget": {
            "tier": budget.tier,
            "retrieve_top_k": budget.retrieve_top_k,
            "rerank_candidate_top_k": budget.rerank_candidate_top_k,
            "allow_graph": budget.allow_graph,
            "allow_rerank": budget.allow_rerank,
            "allow_llm_rerank": budget.allow_llm_rerank,
            "prefer_rrf": budget.prefer_rrf,
        },
        "capabilities": {
            "has_dense": capabilities.has_dense,
            "has_sparse": capabilities.has_sparse,
            "has_graph": capabilities.has_graph,
            "has_embedding_rerank": capabilities.has_embedding_rerank,
            "has_llm_rerank": capabilities.has_llm_rerank,
        },
        "plan": {
            "name": plan.name,
            "intent": plan.intent,
            "fusion": plan.fusion,
            "enable_rerank": plan.enable_rerank,
            "rerank_strategy": plan.rerank_strategy,
            "uses_graph": plan.uses_graph,
            "rationale": plan.rationale,
        },
    }
