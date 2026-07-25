# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Query- and budget-aware retrieval path planner.

The planner is intentionally deterministic. It does not call an LLM; it turns a
query, a coarse latency/quality budget, and available backend capabilities into
a concrete retrieval path that downstream pipelines can execute.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Optional, Sequence, Tuple, Union

BudgetInput = Union[str, "RetrievalBudget"]


@dataclass(frozen=True)
class RetrievalStagePlan:
    """Declarative retrieval branch selected by the planner."""

    engine: str
    weight: float = 1.0
    top_k: Optional[int] = None
    params: Dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphExpansionPlan:
    """Graph expansion controls for structural retrieval paths."""

    seed_engine: str = "sparse"
    seed_top_k: int = 8
    expand_top_k: int = 80
    hops: int = 2
    direction: str = "both"
    use_ppr: bool = False


@dataclass(frozen=True)
class RetrievalBudget:
    """Coarse retrieval budget used for path selection."""

    tier: str = "balanced"
    retrieve_top_k: int = 100
    rerank_candidate_top_k: Optional[int] = 50
    allow_graph: bool = True
    allow_rerank: bool = True
    allow_llm_rerank: bool = False
    prefer_rrf: bool = True

    def __post_init__(self) -> None:
        normalized = self.tier.strip().lower()
        if normalized not in {"fast", "balanced", "thorough"}:
            raise ValueError(
                "Retrieval budget tier must be one of: fast, balanced, thorough."
            )
        if self.retrieve_top_k <= 0:
            raise ValueError("retrieve_top_k must be positive.")
        if self.rerank_candidate_top_k is not None and self.rerank_candidate_top_k <= 0:
            raise ValueError("rerank_candidate_top_k must be positive when set.")
        object.__setattr__(self, "tier", normalized)


@dataclass(frozen=True)
class RetrievalCapabilities:
    """Backends that a planner may use for this pipeline instance."""

    has_dense: bool = True
    has_sparse: bool = True
    has_graph: bool = False
    has_embedding_rerank: bool = True
    has_llm_rerank: bool = False


@dataclass(frozen=True)
class QuerySignals:
    """Heuristic query classification features."""

    lexical: bool
    semantic: bool
    structural: bool

    @property
    def mixed(self) -> bool:
        return sum([self.lexical, self.semantic, self.structural]) >= 2


@dataclass(frozen=True)
class RetrievalPathPlan:
    """Concrete path selected for a query."""

    name: str
    intent: str
    stages: Tuple[RetrievalStagePlan, ...]
    retrieval_top_k: int
    fusion: str = "weighted"
    enable_rerank: bool = False
    rerank_strategy: Optional[str] = None
    rerank_candidate_top_k: Optional[int] = None
    graph: Optional[GraphExpansionPlan] = None
    rationale: str = ""

    @property
    def uses_dense(self) -> bool:
        return any(stage.engine == "dense" for stage in self.stages)

    @property
    def uses_sparse(self) -> bool:
        return any(stage.engine == "sparse" for stage in self.stages)

    @property
    def uses_graph(self) -> bool:
        return self.graph is not None


class RetrievalPlanner:
    """Select one of CodeNib's retrieval paths for a query and budget."""

    _BUDGET_PRESETS = {
        "fast": RetrievalBudget(
            tier="fast",
            retrieve_top_k=32,
            rerank_candidate_top_k=None,
            allow_graph=False,
            allow_rerank=False,
            allow_llm_rerank=False,
            prefer_rrf=True,
        ),
        "balanced": RetrievalBudget(
            tier="balanced",
            retrieve_top_k=100,
            rerank_candidate_top_k=50,
            allow_graph=True,
            allow_rerank=True,
            allow_llm_rerank=False,
            prefer_rrf=True,
        ),
        "thorough": RetrievalBudget(
            tier="thorough",
            retrieve_top_k=128,
            rerank_candidate_top_k=80,
            allow_graph=True,
            allow_rerank=True,
            allow_llm_rerank=True,
            prefer_rrf=True,
        ),
    }

    _STRUCTURAL_PATTERNS = tuple(
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"\bwho\s+calls\b",
            r"\b(callers?|callees?)\b",
            r"\b(call\s+graph|dependency|dependencies)\b",
            r"\b(impact|blast\s+radius|reachable|references?)\b",
            r"\b(definitions?|usages?)\s+of\b",
            r"\b(inherits?|subclasses?|overrides?)\b",
            r"\b(data\s+flow|control\s+flow)\b",
        )
    )
    _LEXICAL_PATTERNS = tuple(
        re.compile(pattern)
        for pattern in (
            r"`[^`]+`",
            r"\b[A-Za-z_][A-Za-z0-9_]*\([^)]*\)",
            r"\b[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\b",
            r"\b[A-Za-z_][A-Za-z0-9_]*::[A-Za-z_][A-Za-z0-9_]*\b",
            r"\b[A-Za-z]+_[A-Za-z0-9_]+\b",
            r"\b[A-Z][A-Za-z0-9]*(Error|Exception|Factory|Manager|Service)\b",
            r"[\w./-]+\.(py|js|ts|go|rs|java|cpp|c|h|hpp|rb|php|kt|scala)\b",
        )
    )
    _SEMANTIC_WORDS = re.compile(
        r"\b(how|why|what|when|where|explain|behavior|logic|flow|handle|support)\b",
        re.IGNORECASE,
    )

    def normalize_budget(self, budget: BudgetInput = "balanced") -> RetrievalBudget:
        """Return a validated budget object from a preset name or custom budget."""
        if isinstance(budget, RetrievalBudget):
            return budget
        key = (budget or "balanced").strip().lower()
        aliases = {
            "cheap": "fast",
            "low": "fast",
            "default": "balanced",
            "quality": "thorough",
            "full": "thorough",
        }
        key = aliases.get(key, key)
        try:
            return self._BUDGET_PRESETS[key]
        except KeyError as exc:
            raise ValueError(
                "Unknown retrieval budget. Choose from: fast, balanced, thorough."
            ) from exc

    def classify_query(self, query: str) -> QuerySignals:
        """Classify a query into lexical, semantic, and structural signals."""
        text = query or ""
        words = re.findall(r"[A-Za-z0-9_]+", text)
        structural = any(pattern.search(text) for pattern in self._STRUCTURAL_PATTERNS)
        lexical = any(pattern.search(text) for pattern in self._LEXICAL_PATTERNS)
        semantic = bool(self._SEMANTIC_WORDS.search(text)) or len(words) >= 10
        if not lexical and not semantic and words:
            lexical = len(words) <= 4
        return QuerySignals(
            lexical=lexical,
            semantic=semantic,
            structural=structural,
        )

    def select(
        self,
        query: str,
        budget: BudgetInput = "balanced",
        capabilities: Optional[RetrievalCapabilities] = None,
    ) -> RetrievalPathPlan:
        """Select a concrete retrieval path for *query* under *budget*."""
        resolved_budget = self.normalize_budget(budget)
        resolved_caps = capabilities or RetrievalCapabilities()
        signals = self.classify_query(query)

        if (
            signals.structural
            and resolved_budget.allow_graph
            and resolved_caps.has_graph
            and resolved_caps.has_sparse
        ):
            return self._structural_graph_plan(resolved_budget, resolved_caps)

        if resolved_budget.tier == "fast":
            if signals.lexical and resolved_caps.has_sparse:
                return self._fast_lexical_plan(resolved_budget)
            if resolved_caps.has_dense:
                return self._semantic_plan(
                    resolved_budget,
                    resolved_caps,
                    enable_rerank=False,
                    rationale="fast budget disables rerank",
                )
            if resolved_caps.has_sparse:
                return self._fast_lexical_plan(resolved_budget)

        if signals.semantic and not signals.lexical and resolved_caps.has_dense:
            return self._semantic_plan(resolved_budget, resolved_caps)

        if resolved_caps.has_dense and resolved_caps.has_sparse:
            return self._hybrid_fusion_plan(resolved_budget, resolved_caps, signals)

        if resolved_caps.has_dense:
            return self._semantic_plan(resolved_budget, resolved_caps)

        if resolved_caps.has_sparse:
            return self._fast_lexical_plan(resolved_budget)

        raise ValueError("No retrieval backend is available for planning.")

    def _fast_lexical_plan(self, budget: RetrievalBudget) -> RetrievalPathPlan:
        return RetrievalPathPlan(
            name="fast_lexical",
            intent="lexical",
            stages=(RetrievalStagePlan("sparse", top_k=budget.retrieve_top_k),),
            retrieval_top_k=budget.retrieve_top_k,
            fusion="none",
            enable_rerank=False,
            rationale="exact-name or low-latency query; BM25 only",
        )

    def _semantic_plan(
        self,
        budget: RetrievalBudget,
        capabilities: RetrievalCapabilities,
        *,
        enable_rerank: Optional[bool] = None,
        rationale: str = "natural-language query; dense retrieval first",
    ) -> RetrievalPathPlan:
        use_rerank = budget.allow_rerank if enable_rerank is None else enable_rerank
        rerank_strategy = (
            self._rerank_strategy(budget, capabilities) if use_rerank else None
        )
        return RetrievalPathPlan(
            name="semantic",
            intent="semantic",
            stages=(RetrievalStagePlan("dense", top_k=budget.retrieve_top_k),),
            retrieval_top_k=budget.retrieve_top_k,
            fusion="none",
            enable_rerank=bool(rerank_strategy),
            rerank_strategy=rerank_strategy,
            rerank_candidate_top_k=budget.rerank_candidate_top_k,
            rationale=rationale,
        )

    def _hybrid_fusion_plan(
        self,
        budget: RetrievalBudget,
        capabilities: RetrievalCapabilities,
        signals: QuerySignals,
    ) -> RetrievalPathPlan:
        rerank_strategy = (
            self._rerank_strategy(budget, capabilities) if budget.allow_rerank else None
        )
        fusion = "rrf" if budget.prefer_rrf else "weighted"
        intent = "hybrid" if signals.mixed else "lexical_semantic"
        return RetrievalPathPlan(
            name="hybrid_fusion",
            intent=intent,
            stages=(
                RetrievalStagePlan("dense", weight=0.6, top_k=budget.retrieve_top_k),
                RetrievalStagePlan("sparse", weight=0.4, top_k=budget.retrieve_top_k),
            ),
            retrieval_top_k=budget.retrieve_top_k,
            fusion=fusion,
            enable_rerank=bool(rerank_strategy),
            rerank_strategy=rerank_strategy,
            rerank_candidate_top_k=budget.rerank_candidate_top_k,
            rationale="mixed or uncertain query; fuse dense and sparse branches",
        )

    def _structural_graph_plan(
        self,
        budget: RetrievalBudget,
        capabilities: RetrievalCapabilities,
    ) -> RetrievalPathPlan:
        rerank_strategy = (
            self._rerank_strategy(budget, capabilities) if budget.allow_rerank else None
        )
        graph = GraphExpansionPlan(
            seed_top_k=min(12, max(5, budget.retrieve_top_k // 12)),
            expand_top_k=budget.retrieve_top_k,
            hops=2 if budget.tier != "fast" else 1,
            use_ppr=budget.tier == "thorough",
        )
        return RetrievalPathPlan(
            name="structural_graph",
            intent="structural",
            stages=(RetrievalStagePlan("sparse", top_k=graph.seed_top_k),),
            retrieval_top_k=budget.retrieve_top_k,
            fusion="none",
            enable_rerank=bool(rerank_strategy),
            rerank_strategy=rerank_strategy,
            rerank_candidate_top_k=budget.rerank_candidate_top_k,
            graph=graph,
            rationale="structural query; seed BM25 and expand over the graph",
        )

    def _rerank_strategy(
        self,
        budget: RetrievalBudget,
        capabilities: RetrievalCapabilities,
    ) -> Optional[str]:
        if budget.allow_llm_rerank and capabilities.has_llm_rerank:
            return "llm"
        if capabilities.has_embedding_rerank:
            return "embedding"
        return None


def build_planner_preload_stages(
    capabilities: Optional[RetrievalCapabilities] = None,
    *,
    top_k: int = 128,
) -> Sequence[RetrievalStagePlan]:
    """Return the index stages worth preloading for dynamic planning."""
    caps = capabilities or RetrievalCapabilities()
    stages = []
    if caps.has_dense:
        stages.append(RetrievalStagePlan("dense", weight=0.6, top_k=top_k))
    if caps.has_sparse:
        stages.append(RetrievalStagePlan("sparse", weight=0.4, top_k=top_k))
    if not stages:
        raise ValueError("At least one retrieval backend must be available.")
    return tuple(stages)
