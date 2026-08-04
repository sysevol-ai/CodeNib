# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Manifest-backed repository exploration for agent and benchmark clients."""

from __future__ import annotations

import math
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Mapping, Sequence

from ...git_snapshot import normalize_repository_path
from ...model.retrieval_planner import (
    BudgetInput,
    GraphExpansionPlan,
    QuerySignals,
    RetrievalBudget,
    RetrievalCapabilities,
    RetrievalPathPlan,
    RetrievalPlanner,
    RetrievalStagePlan,
)
from ...types import QueriedNode

REPOSITORY_EXPLORER_POLICIES = frozenset(
    {"auto", "bm25", "dense", "hybrid", "hybrid_rerank", "graph"}
)
_NATIVE_VIEWS = ("bm25", "vector", "symbol_graph")
_POLICY_VIEWS = {
    "bm25": ("bm25",),
    "dense": ("vector",),
    "hybrid": ("bm25", "vector"),
    "hybrid_rerank": ("bm25", "vector"),
    "graph": ("bm25", "symbol_graph"),
}


class RepositoryExplorerCapabilityError(RuntimeError):
    """The selected exploration policy cannot run over the manifest."""


@dataclass(frozen=True, slots=True)
class RepositoryEvidence:
    """One ranked source span in CodeNib's 0-based coordinate system."""

    path: str
    start_line: int
    end_line: int
    score: float
    node_name: str = ""
    node_type: str = ""
    node_id: str | None = None
    content: str | None = None

    def to_record(self) -> dict[str, str | int | float | None]:
        return {
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "score": self.score,
            "node_name": self.node_name,
            "node_type": self.node_type,
            "node_id": self.node_id,
            "content": self.content,
        }


@dataclass(frozen=True, slots=True)
class RepositoryExploreTrace:
    """Planner and execution provenance for one exploration request."""

    policy: str
    plan: RetrievalPathPlan
    signals: QuerySignals
    budget: RetrievalBudget
    capabilities: RetrievalCapabilities
    advertised_views: tuple[str, ...]
    loaded_views: tuple[str, ...]
    stage_candidate_counts: tuple[tuple[str, int], ...]
    retrieved_candidates: int
    returned_evidence: int

    def to_record(self) -> dict[str, Any]:
        graph = self.plan.graph
        return {
            "policy": self.policy,
            "plan": {
                "name": self.plan.name,
                "intent": self.plan.intent,
                "stages": [stage.engine for stage in self.plan.stages],
                "fusion": self.plan.fusion,
                "rerank_strategy": self.plan.rerank_strategy,
                "graph": (
                    {
                        "seed_top_k": graph.seed_top_k,
                        "expand_top_k": graph.expand_top_k,
                        "hops": graph.hops,
                        "direction": graph.direction,
                        "use_ppr": graph.use_ppr,
                    }
                    if graph is not None
                    else None
                ),
                "rationale": self.plan.rationale,
            },
            "signals": {
                "lexical": self.signals.lexical,
                "semantic": self.signals.semantic,
                "structural": self.signals.structural,
                "mixed": self.signals.mixed,
            },
            "budget": {
                "tier": self.budget.tier,
                "retrieve_top_k": self.budget.retrieve_top_k,
                "rerank_candidate_top_k": self.budget.rerank_candidate_top_k,
            },
            "capabilities": {
                "dense": self.capabilities.has_dense,
                "sparse": self.capabilities.has_sparse,
                "graph": self.capabilities.has_graph,
                "embedding_rerank": self.capabilities.has_embedding_rerank,
            },
            "advertised_views": list(self.advertised_views),
            "loaded_views": list(self.loaded_views),
            "stage_candidate_counts": dict(self.stage_candidate_counts),
            "retrieved_candidates": self.retrieved_candidates,
            "returned_evidence": self.returned_evidence,
        }


@dataclass(frozen=True, slots=True)
class RepositoryExplorePreparation:
    """A deterministic query plan with all selected views loaded."""

    query: str
    plan: RetrievalPathPlan
    signals: QuerySignals
    budget: RetrievalBudget
    capabilities: RetrievalCapabilities


@dataclass(frozen=True, slots=True)
class RepositoryExploreResult:
    """Ranked repository evidence and the plan that produced it."""

    evidence: tuple[RepositoryEvidence, ...]
    trace: RepositoryExploreTrace | None

    def to_record(self) -> dict[str, Any]:
        return {
            "evidence": [item.to_record() for item in self.evidence],
            "trace": self.trace.to_record() if self.trace is not None else None,
        }


def normalize_repository_explorer_policy(value: str) -> str:
    """Return the canonical repository-explorer policy name."""

    normalized = str(value or "auto").strip().lower().replace("-", "_")
    aliases = {"sparse": "bm25", "semantic": "dense", "rrf": "hybrid"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in REPOSITORY_EXPLORER_POLICIES:
        supported = ", ".join(sorted(REPOSITORY_EXPLORER_POLICIES))
        raise ValueError(
            f"unknown repository explorer policy; choose from: {supported}"
        )
    return normalized


def repository_explorer_build_views(policy: str) -> tuple[str, ...]:
    """Return the deterministic materialization set for a benchmark policy."""

    normalized = normalize_repository_explorer_policy(policy)
    if normalized == "auto":
        return _NATIVE_VIEWS
    return _POLICY_VIEWS[normalized]


class RepositoryContextExplorer:
    """Plan and execute repository search over manifest-backed CodeNib views.

    ``auto`` plans against manifest-advertised capabilities and loads only the
    selected query path. Explicit policies provide stable ablation arms. The
    class never materializes indexes; callers build views separately.
    """

    def __init__(
        self,
        context: Any,
        *,
        policy: str = "auto",
        budget: BudgetInput = "balanced",
        level: str = "l2",
        _owns_context: bool = False,
    ) -> None:
        manifest = getattr(context, "manifest", None)
        if manifest is None:
            raise RepositoryExplorerCapabilityError("context has no manifest")
        raw_root = str(getattr(manifest, "repo_path", "") or "").strip()
        if not raw_root:
            raise RepositoryExplorerCapabilityError("manifest has no repository path")
        repo_root = Path(raw_root).expanduser().resolve()
        if not repo_root.is_dir():
            raise RepositoryExplorerCapabilityError(
                f"manifest repository is not available: {repo_root}"
            )
        normalized_level = str(level or "l2").strip().lower()
        if normalized_level not in {"l0", "l2"}:
            raise ValueError("repository explorer level must be 'l0' or 'l2'")

        self.context = context
        self.repo_root = repo_root
        self.policy = normalize_repository_explorer_policy(policy)
        self.planner = RetrievalPlanner()
        self.budget = self.planner.normalize_budget(budget)
        self.level = normalized_level
        self._owns_context = _owns_context
        self._source_lines: dict[str, tuple[str, ...]] = {}
        self._lock = RLock()
        self._validate_advertised_policy()

    @classmethod
    def from_manifest(
        cls,
        manifest_path: str | Path,
        *,
        policy: str = "auto",
        budget: BudgetInput = "balanced",
        level: str = "l2",
    ) -> RepositoryContextExplorer:
        """Bind to a manifest without eagerly loading retrieval views."""

        from ...mcp.context import ServerContext

        context = ServerContext.load(manifest_path, views=())
        return cls(
            context,
            policy=policy,
            budget=budget,
            level=level,
            _owns_context=True,
        )

    @classmethod
    def from_repository(
        cls,
        repo_path: str | Path,
        *,
        manifest_path: str | Path | None = None,
        policy: str = "auto",
        budget: BudgetInput = "balanced",
        level: str = "l2",
    ) -> RepositoryContextExplorer:
        """Resolve the manifest bound to one checkout and create an explorer."""

        from ...clients._manifest import select_checkout_manifest

        selected = select_checkout_manifest(
            repo_path,
            integration_name="repository explorer",
            manifest_path=manifest_path,
        )
        return cls.from_manifest(
            selected,
            policy=policy,
            budget=budget,
            level=level,
        )

    def close(self) -> None:
        """Release resources loaded by a manifest-constructed explorer."""

        if self._owns_context:
            close = getattr(self.context, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> RepositoryContextExplorer:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def explore(
        self,
        query: str,
        *,
        top_k: int = 10,
        include_content: bool = False,
        budget: BudgetInput | None = None,
    ) -> RepositoryExploreResult:
        """Return bounded, source-validated evidence for one context request."""

        _validate_top_k(top_k)
        text = str(query or "").strip()
        if top_k == 0 or not text:
            return RepositoryExploreResult(evidence=(), trace=None)

        with self._lock:
            prepared = self.prepare(text, budget=budget)
            active_budget = prepared.budget
            signals = prepared.signals
            plan = prepared.plan
            capabilities = prepared.capabilities
            vector = getattr(self.context, "vector", None)
            reuse_query = getattr(vector, "reuse_query_embedding", None)
            scope = reuse_query() if callable(reuse_query) else nullcontext()
            with scope:
                candidates, stage_counts = self._execute_plan(
                    text,
                    plan,
                    top_k=top_k,
                )
            evidence = self._source_evidence(
                candidates,
                top_k=top_k,
                include_content=include_content,
            )
            trace = RepositoryExploreTrace(
                policy=self.policy,
                plan=plan,
                signals=signals,
                budget=active_budget,
                capabilities=capabilities,
                advertised_views=tuple(sorted(self._advertised_views())),
                loaded_views=tuple(sorted(self._loaded_views())),
                stage_candidate_counts=tuple(stage_counts),
                retrieved_candidates=len(candidates),
                returned_evidence=len(evidence),
            )
            return RepositoryExploreResult(evidence=tuple(evidence), trace=trace)

    def prepare(
        self,
        query: str,
        *,
        budget: BudgetInput | None = None,
    ) -> RepositoryExplorePreparation:
        """Plan a query and load only the views selected for that plan."""

        text = str(query or "").strip()
        if not text:
            raise ValueError("repository explorer query must not be empty")
        with self._lock:
            active_budget = self.planner.normalize_budget(budget or self.budget)
            plan, capabilities = self._select_and_load_plan(text, active_budget)
            return RepositoryExplorePreparation(
                query=text,
                plan=plan,
                signals=self.planner.classify_query(text),
                budget=active_budget,
                capabilities=capabilities,
            )

    def _validate_advertised_policy(self) -> None:
        advertised = self._advertised_views()
        if self.policy == "auto":
            if not advertised.intersection({"bm25", "vector"}):
                raise RepositoryExplorerCapabilityError(
                    "auto exploration requires a current BM25 or vector view"
                )
            return
        missing = set(_POLICY_VIEWS[self.policy]) - advertised
        if missing:
            raise RepositoryExplorerCapabilityError(
                f"policy {self.policy!r} requires unavailable manifest views: "
                + ", ".join(sorted(missing))
            )

    def _select_and_load_plan(
        self,
        query: str,
        budget: RetrievalBudget,
    ) -> tuple[RetrievalPathPlan, RetrievalCapabilities]:
        if self.policy != "auto":
            required = set(_POLICY_VIEWS[self.policy])
            self._load_views(required)
            missing = required - self._loaded_views()
            if missing:
                raise self._capability_error(missing)
            capabilities = self._runtime_capabilities()
            return _explicit_plan(self.policy, budget), capabilities

        advertised = self._advertised_capabilities()
        initial = self.planner.select(query, budget=budget, capabilities=advertised)
        requested = _plan_views(initial)
        self._load_views(requested)

        capabilities = self._runtime_capabilities()
        if not capabilities.has_dense and not capabilities.has_sparse:
            alternatives = self._advertised_views().intersection({"bm25", "vector"})
            self._load_views(alternatives - requested - self._loaded_views())
            capabilities = self._runtime_capabilities()
        if not capabilities.has_dense and not capabilities.has_sparse:
            raise self._capability_error(requested)

        plan = self.planner.select(query, budget=budget, capabilities=capabilities)
        return plan, capabilities

    def _execute_plan(
        self,
        query: str,
        plan: RetrievalPathPlan,
        *,
        top_k: int,
    ) -> tuple[list[QueriedNode], list[tuple[str, int]]]:
        from ...ops.retrieve import merge_hybrid, to_queried_nodes

        branches: list[list[QueriedNode]] = []
        counts: list[tuple[str, int]] = []
        for stage in plan.stages:
            stage_top_k = max(top_k, stage.top_k or plan.retrieval_top_k)
            if stage.engine == "sparse":
                bm25 = getattr(self.context, "bm25", None)
                if bm25 is None:
                    raise RepositoryExplorerCapabilityError("BM25 view is not loaded")
                max_k = getattr(bm25, "max_k", stage_top_k)
                if isinstance(max_k, int) and max_k > 0:
                    stage_top_k = min(stage_top_k, max_k)
                raw = bm25.search(
                    query=query,
                    top_k=stage_top_k,
                    return_code_content=False,
                )
            elif stage.engine == "dense":
                vector = getattr(self.context, "vector", None)
                if vector is None:
                    raise RepositoryExplorerCapabilityError("vector view is not loaded")
                raw = vector.search(query, top_k=stage_top_k, level=self.level)
            else:
                raise ValueError(f"unsupported retrieval engine: {stage.engine!r}")
            branch = to_queried_nodes(raw)
            branches.append(branch)
            counts.append((stage.engine, len(branch)))

        if len(branches) == 1:
            candidates = branches[0]
        else:
            candidates = merge_hybrid(
                branches,
                weights=[stage.weight for stage in plan.stages],
                top_k=plan.retrieval_top_k,
                fusion=plan.fusion,
            )

        if plan.graph is not None:
            candidates = self._expand_graph(plan.graph, candidates)
            counts.append(("graph", len(candidates)))

        if plan.enable_rerank and plan.rerank_strategy == "embedding":
            from ...ops.rerank import rerank_by_indexed_embedding

            candidate_top_k = plan.rerank_candidate_top_k or len(candidates)
            candidates = rerank_by_indexed_embedding(
                query,
                candidates[: max(top_k, candidate_top_k)],
                self.context.vector,
                top_k=max(top_k, candidate_top_k),
                level=self.level,
            )
            counts.append(("embedding_rerank", len(candidates)))
        return candidates, counts

    def _expand_graph(
        self,
        graph_plan: GraphExpansionPlan,
        candidates: Sequence[QueriedNode],
    ) -> list[QueriedNode]:
        from ...ops.expand import ExpandContext, expand_graph_region
        from ...ops.retrieve import dedup_queried_nodes

        seeds = list(candidates[: graph_plan.seed_top_k])
        expanded = expand_graph_region(
            ExpandContext(code_graph=self.context.symbol_graph),
            seeds,
            top_k=graph_plan.expand_top_k,
            hops=graph_plan.hops,
            direction=graph_plan.direction,
            use_ppr=graph_plan.use_ppr,
            repo_path=str(self.repo_root),
            include_content=False,
        )
        return dedup_queried_nodes([*seeds, *expanded])[: graph_plan.expand_top_k]

    def _source_evidence(
        self,
        candidates: Sequence[QueriedNode],
        *,
        top_k: int,
        include_content: bool,
    ) -> list[RepositoryEvidence]:
        evidence: list[RepositoryEvidence] = []
        seen: set[tuple[str, int, int]] = set()
        for candidate in candidates:
            item = self._candidate_evidence(candidate, include_content=include_content)
            if item is None:
                continue
            key = (item.path, item.start_line, item.end_line)
            if key in seen:
                continue
            seen.add(key)
            evidence.append(item)
            if len(evidence) >= top_k:
                break
        return evidence

    def _candidate_evidence(
        self,
        candidate: QueriedNode,
        *,
        include_content: bool,
    ) -> RepositoryEvidence | None:
        path = self._candidate_path(candidate.file)
        if path is None:
            return None
        lines = self._lines(path)
        if not lines:
            return None
        start = _line_number(candidate.start_line)
        end = _line_number(candidate.end_line)
        if start is None or end is None or end < start or start >= len(lines):
            return None
        end = min(end, len(lines) - 1)
        content = "\n".join(lines[start : end + 1]) if include_content else None
        return RepositoryEvidence(
            path=path,
            start_line=start,
            end_line=end,
            score=_finite_score(candidate.score),
            node_name=str(candidate.node_name or ""),
            node_type=str(candidate.type or ""),
            node_id=str(candidate.node_id) if candidate.node_id else None,
            content=content,
        )

    def _candidate_path(self, value: str | None) -> str | None:
        raw_value = str(value or "").strip()
        if not raw_value:
            return None
        try:
            raw = Path(raw_value).expanduser()
            if raw.is_absolute():
                relative = raw.resolve().relative_to(self.repo_root).as_posix()
            else:
                relative = normalize_repository_path(raw_value)
            resolved = (self.repo_root / relative).resolve()
            resolved.relative_to(self.repo_root)
            if not resolved.is_file():
                return None
            return normalize_repository_path(relative)
        except (OSError, ValueError):
            return None

    def _lines(self, path: str) -> tuple[str, ...]:
        lines = self._source_lines.get(path)
        if lines is None:
            try:
                lines = tuple(
                    (self.repo_root / path)
                    .read_text(encoding="utf-8", errors="replace")
                    .splitlines()
                )
            except OSError:
                lines = ()
            self._source_lines[path] = lines
        return lines

    def _advertised_views(self) -> set[str]:
        loaded = self._loaded_views()
        manifest = self.context.manifest
        indexes: Mapping[str, Any] = getattr(manifest, "indexes", {})
        advertised = set(loaded)
        for view in _NATIVE_VIEWS:
            if view not in indexes:
                continue
            try:
                current = bool(manifest.index_is_current(view))
            except (AttributeError, TypeError, ValueError):
                current = getattr(indexes[view], "status", None) == "fresh"
            if current:
                advertised.add(view)
        return advertised

    def _loaded_views(self) -> set[str]:
        loaded = getattr(self.context, "loaded_views", None)
        if loaded is not None:
            return set(loaded)
        return {
            view
            for view in _NATIVE_VIEWS
            if getattr(self.context, view, None) is not None
        }

    def _load_views(self, views: set[str]) -> None:
        if not views:
            return
        load_views = getattr(self.context, "load_views", None)
        if callable(load_views):
            load_views(views)

    def _advertised_capabilities(self) -> RetrievalCapabilities:
        advertised = self._advertised_views()
        return _capabilities(advertised)

    def _runtime_capabilities(self) -> RetrievalCapabilities:
        return _capabilities(self._loaded_views())

    def _capability_error(self, missing: set[str]) -> RepositoryExplorerCapabilityError:
        errors = getattr(self.context, "errors", {})
        details = "; ".join(
            f"{view}: {errors.get(view, 'not loaded')}" for view in sorted(missing)
        )
        return RepositoryExplorerCapabilityError(
            f"repository explorer cannot load required views ({details})"
        )


def _capabilities(views: set[str]) -> RetrievalCapabilities:
    has_vector = "vector" in views
    return RetrievalCapabilities(
        has_dense=has_vector,
        has_sparse="bm25" in views,
        has_graph="symbol_graph" in views,
        has_embedding_rerank=has_vector,
        has_llm_rerank=False,
    )


def _plan_views(plan: RetrievalPathPlan) -> set[str]:
    views: set[str] = set()
    if plan.uses_sparse:
        views.add("bm25")
    if plan.uses_dense or plan.rerank_strategy == "embedding":
        views.add("vector")
    if plan.uses_graph:
        views.add("symbol_graph")
    return views


def _explicit_plan(policy: str, budget: RetrievalBudget) -> RetrievalPathPlan:
    if policy == "bm25":
        return RetrievalPathPlan(
            name="bm25_control",
            intent="lexical",
            stages=(RetrievalStagePlan("sparse", top_k=budget.retrieve_top_k),),
            retrieval_top_k=budget.retrieve_top_k,
            fusion="none",
            rationale="explicit BM25 compatibility control",
        )
    if policy == "dense":
        return RetrievalPathPlan(
            name="dense",
            intent="semantic",
            stages=(RetrievalStagePlan("dense", top_k=budget.retrieve_top_k),),
            retrieval_top_k=budget.retrieve_top_k,
            fusion="none",
            rationale="explicit dense retrieval policy",
        )
    if policy in {"hybrid", "hybrid_rerank"}:
        rerank = policy == "hybrid_rerank"
        return RetrievalPathPlan(
            name=policy,
            intent="hybrid",
            stages=(
                RetrievalStagePlan("dense", weight=0.6, top_k=budget.retrieve_top_k),
                RetrievalStagePlan("sparse", weight=0.4, top_k=budget.retrieve_top_k),
            ),
            retrieval_top_k=budget.retrieve_top_k,
            fusion="rrf",
            enable_rerank=rerank,
            rerank_strategy="embedding" if rerank else None,
            rerank_candidate_top_k=budget.rerank_candidate_top_k if rerank else None,
            rationale="explicit dense and BM25 reciprocal-rank fusion policy",
        )
    if policy == "graph":
        graph = GraphExpansionPlan(
            seed_top_k=min(12, max(5, budget.retrieve_top_k // 12)),
            expand_top_k=budget.retrieve_top_k,
            hops=2 if budget.tier != "fast" else 1,
            use_ppr=budget.tier == "thorough",
        )
        return RetrievalPathPlan(
            name="graph",
            intent="structural",
            stages=(RetrievalStagePlan("sparse", top_k=graph.seed_top_k),),
            retrieval_top_k=budget.retrieve_top_k,
            fusion="none",
            graph=graph,
            rationale="explicit BM25-seeded graph expansion policy",
        )
    raise ValueError(f"unsupported explicit policy: {policy!r}")


def _validate_top_k(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("top_k must be an integer")
    if value < 0:
        raise ValueError("top_k must be non-negative")


def _line_number(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _finite_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return score if math.isfinite(score) else 0.0


__all__ = [
    "REPOSITORY_EXPLORER_POLICIES",
    "RepositoryContextExplorer",
    "RepositoryEvidence",
    "RepositoryExplorePreparation",
    "RepositoryExploreResult",
    "RepositoryExploreTrace",
    "RepositoryExplorerCapabilityError",
    "normalize_repository_explorer_policy",
    "repository_explorer_build_views",
]
