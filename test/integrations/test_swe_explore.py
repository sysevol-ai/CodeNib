# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from codenib.integrations.swe_explore import CodeNibSWEExploreExplorer
from codenib.mcp.context import ServerContext


def test_explorer_loads_only_bm25(integration_manifest) -> None:
    with patch.object(ServerContext, "load", wraps=ServerContext.load) as load:
        explorer = CodeNibSWEExploreExplorer.from_manifest(integration_manifest)

    assert explorer.context.bm25 is not None
    assert explorer.context.symbol_graph is None
    assert explorer.context.vector is None
    assert load.call_args.kwargs["views"] == frozenset({"bm25"})


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
    explorer = CodeNibSWEExploreExplorer.from_manifest(integration_manifest)
    repo = explorer.repository.repo_root
    valid = SimpleNamespace(
        file="src/service.py", start_line=1, end_line=999, score=0.7
    )
    explorer.bm25 = SimpleNamespace(
        max_k=20,
        search=lambda **_kwargs: [
            SimpleNamespace(file="../outside.py", start_line=0, end_line=1, score=1.0),
            valid,
            valid,
            SimpleNamespace(
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
