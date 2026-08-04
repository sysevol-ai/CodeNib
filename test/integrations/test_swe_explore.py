# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from codenib.agent.runtime.explorer import (
    RepositoryContextExplorer,
    RepositoryEvidence,
    RepositoryExploreResult,
)
from codenib.compiler.manifest import IndexEntry, RepoManifest
from codenib.integrations.swe_explore import CodeNibSWEExploreExplorer
from codenib.mcp.context import ServerContext
from codenib.types import NodeInfo


def test_explorer_defers_view_loading_until_query_planning(
    integration_manifest,
) -> None:
    with patch.object(ServerContext, "load", wraps=ServerContext.load) as load:
        explorer = CodeNibSWEExploreExplorer.from_manifest(integration_manifest)

    assert explorer.context.bm25 is None
    assert explorer.context.symbol_graph is None
    assert explorer.context.vector is None
    assert load.call_args.kwargs["views"] == ()

    preparation = explorer.prepare("calculate_tax")

    assert preparation.plan.name == "fast_lexical"
    assert explorer.context.bm25 is not None
    assert explorer.context.symbol_graph is None
    assert explorer.context.vector is None


def test_auto_structural_query_loads_graph_route(integration_manifest) -> None:
    explorer = CodeNibSWEExploreExplorer.from_manifest(integration_manifest)

    preparation = explorer.prepare("who calls `calculate_tax`?")
    results = explorer.explore(
        instance_id="iid",
        query="who calls `calculate_tax`?",
        top_k=5,
    )

    assert preparation.plan.name == "structural_graph"
    assert explorer.context.bm25 is not None
    assert explorer.context.symbol_graph is not None
    assert explorer.context.vector is None
    assert results
    assert explorer.last_trace is not None
    assert explorer.last_trace.plan.uses_graph is True


def test_explorer_returns_official_one_based_regions(integration_manifest) -> None:
    explorer = CodeNibSWEExploreExplorer.from_manifest(
        integration_manifest, include_snippets=True
    )

    results = explorer.explore(
        instance_id="project__repo-1",
        query="BillingService calculate_tax",
        top_k=2,
    )

    assert 0 < len(results) <= 2
    assert all(result.instance_id == "project__repo-1" for result in results)
    for result in results:
        assert len(result.regions) == 1
        region = result.regions[0]
        assert not region.path.startswith("/")
        assert region.start >= 1
        assert region.end >= region.start
        assert region.snippet


def test_explorer_filters_invalid_and_duplicate_candidates(
    integration_manifest,
) -> None:
    explorer = CodeNibSWEExploreExplorer.from_manifest(
        integration_manifest, policy="bm25"
    )
    explorer.prepare("tax")
    repo = explorer.runtime.repo_root
    valid = NodeInfo(file="src/service.py", start_line=1, end_line=999, score=0.7)
    explorer.context.bm25 = SimpleNamespace(
        max_k=20,
        search=lambda **_kwargs: [
            NodeInfo(file="../outside.py", start_line=0, end_line=1, score=1.0),
            valid,
            valid,
            NodeInfo(
                file=str(repo / "src/other.py"),
                start_line=0,
                end_line=1,
                score=float("nan"),
            ),
        ],
    )

    results = explorer.explore(instance_id="iid", query="tax", top_k=3)

    assert [
        (r.regions[0].path, r.regions[0].start, r.regions[0].end) for r in results
    ] == [
        ("src/service.py", 2, 7),
        ("src/other.py", 1, 2),
    ]
    assert [result.score for result in results] == [0.7, 0.0]


def test_adapter_only_converts_native_evidence_to_official_regions() -> None:
    evidence = RepositoryEvidence(
        path="src/service.py",
        start_line=2,
        end_line=4,
        score=0.75,
        content="three lines",
    )
    runtime = SimpleNamespace(
        context=SimpleNamespace(),
        explore=lambda *_args, **_kwargs: RepositoryExploreResult(
            evidence=(evidence,), trace=None
        ),
        close=lambda: None,
    )
    explorer = CodeNibSWEExploreExplorer(runtime, include_snippets=True)

    results = explorer.explore(instance_id="iid", query="ignored", top_k=1)

    assert results[0].regions[0].to_record() == {
        "path": "src/service.py",
        "start": 3,
        "end": 5,
        "snippet": "three lines",
    }


def test_auto_mixed_query_loads_and_fuses_sparse_and_dense_views(tmp_path) -> None:
    repo = tmp_path / "repo"
    source = repo / "src" / "service.py"
    source.parent.mkdir(parents=True)
    source.write_text("def calculate_tax():\n    return 1\n", encoding="utf-8")
    manifest = RepoManifest(
        repo_path=str(repo),
        indexes={
            view: IndexEntry(
                index_type=view,
                path=str(tmp_path / view),
                built_at="2026-08-04T00:00:00Z",
                built_at_epoch=0.0,
                status="fresh",
            )
            for view in ("bm25", "vector")
        },
    )
    sparse = SimpleNamespace(
        max_k=100,
        search=lambda **_kwargs: [
            NodeInfo(
                node_name="calculate_tax",
                type="function",
                file="src/service.py",
                node_id="tax",
                start_line=0,
                end_line=1,
                score=2.0,
            )
        ],
    )

    class Vector:
        def search(self, *_args, **_kwargs):
            return [
                NodeInfo(
                    node_name="calculate_tax",
                    type="function",
                    file="src/service.py",
                    node_id="tax",
                    start_line=0,
                    end_line=1,
                    score=0.8,
                )
            ]

        def search_within_ids(self, *_args, **_kwargs):
            return self.search()

    vector = Vector()

    class LazyContext:
        def __init__(self):
            self.manifest = manifest
            self.bm25 = None
            self.vector = None
            self.symbol_graph = None
            self.errors = {}
            self.requests = []

        @property
        def loaded_views(self):
            return frozenset(
                view
                for view in ("bm25", "vector", "symbol_graph")
                if getattr(self, view) is not None
            )

        def load_views(self, views):
            self.requests.append(frozenset(views))
            if "bm25" in views:
                self.bm25 = sparse
            if "vector" in views:
                self.vector = vector
            return {}

    context = LazyContext()
    explorer = RepositoryContextExplorer(context)

    result = explorer.explore(
        "Explain `calculate_tax` behavior",
        top_k=5,
        include_content=True,
    )

    assert context.requests == [frozenset({"bm25", "vector"})]
    assert result.trace is not None
    assert result.trace.plan.name == "hybrid_fusion"
    assert result.trace.stage_candidate_counts[:2] == (("dense", 1), ("sparse", 1))
    assert result.trace.stage_candidate_counts[2] == ("embedding_rerank", 1)
    assert result.evidence[0].content == "def calculate_tax():\n    return 1"


def test_auto_falls_back_to_an_available_view_after_load_failure(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "service.py").write_text("def price():\n    return 1\n", encoding="utf-8")
    manifest = RepoManifest(
        repo_path=str(repo),
        indexes={
            view: IndexEntry(
                index_type=view,
                path=str(tmp_path / view),
                built_at="2026-08-04T00:00:00Z",
                built_at_epoch=0.0,
                status="fresh",
            )
            for view in ("bm25", "vector")
        },
    )
    sparse = SimpleNamespace(
        max_k=100,
        search=lambda **_kwargs: [
            NodeInfo(
                node_name="price",
                type="function",
                file="service.py",
                start_line=0,
                end_line=1,
                score=1.0,
            )
        ],
    )

    class FailingVectorContext:
        def __init__(self):
            self.manifest = manifest
            self.bm25 = None
            self.vector = None
            self.symbol_graph = None
            self.errors = {}
            self.requests = []

        @property
        def loaded_views(self):
            return frozenset({"bm25"}) if self.bm25 is not None else frozenset()

        def load_views(self, views):
            self.requests.append(frozenset(views))
            if "vector" in views:
                self.errors["vector"] = "embedding backend unavailable"
            if "bm25" in views:
                self.bm25 = sparse
            return {view: self.errors[view] for view in views if view in self.errors}

    context = FailingVectorContext()
    explorer = RepositoryContextExplorer(context)

    result = explorer.explore("How does invoice pricing work?", top_k=5)

    assert context.requests == [frozenset({"vector"}), frozenset({"bm25"})]
    assert result.trace is not None
    assert result.trace.plan.name == "fast_lexical"
    assert result.trace.loaded_views == ("bm25",)


@pytest.mark.parametrize("top_k", [0])
def test_explorer_zero_budget_returns_no_regions(
    integration_manifest, top_k: int
) -> None:
    explorer = CodeNibSWEExploreExplorer.from_manifest(integration_manifest)
    assert explorer.explore(instance_id="iid", query="tax", top_k=top_k) == []


@pytest.mark.parametrize("top_k", [-1, -10])
def test_explorer_rejects_negative_region_budget(
    integration_manifest, top_k: int
) -> None:
    explorer = CodeNibSWEExploreExplorer.from_manifest(integration_manifest)
    with pytest.raises(ValueError, match="non-negative"):
        explorer.explore(instance_id="iid", query="tax", top_k=top_k)


@pytest.mark.parametrize("top_k", [True, 1.5, "5"])
def test_explorer_rejects_non_integer_region_budget(
    integration_manifest, top_k
) -> None:
    explorer = CodeNibSWEExploreExplorer.from_manifest(integration_manifest)
    with pytest.raises(TypeError, match="integer"):
        explorer.explore(instance_id="iid", query="tax", top_k=top_k)
