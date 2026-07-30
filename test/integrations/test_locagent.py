# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from codenib.compiler.manifest import IndexEntry, RepoManifest
from codenib.integrations import (
    IntegrationCapabilityError,
    RepositoryAdapter,
    RepositoryPathError,
)
from codenib.integrations._repository import RepositoryEntity, RepositoryRelation
from codenib.integrations.locagent import (
    LOCAGENT_REVISION,
    OPENHANDS_LOCAGENT_REVISION,
    LocAgentToolProvider,
    bind_locagent_tools,
    dispatch_locagent_tool_call,
    get_locagent_tool_schemas,
)
from codenib.types import EDGE_TYPE_REFERENCE, NODE_TYPE_FUNCTION


@pytest.fixture()
def provider(integration_manifest: Path) -> LocAgentToolProvider:
    return LocAgentToolProvider.from_manifest(integration_manifest)


def _schema_contract(schemas: list[dict]) -> dict:
    tools = {}
    for schema in schemas:
        function = schema["function"]
        parameters = function["parameters"]
        tools[function["name"]] = {
            "required": parameters.get("required", []),
            "properties": {
                name: value["type"] for name, value in parameters["properties"].items()
            },
        }
    return {
        "behavior_revision": LOCAGENT_REVISION,
        "integration_revision": OPENHANDS_LOCAGENT_REVISION,
        "tools": dict(sorted(tools.items())),
    }


def test_pinned_openhands_tool_contract() -> None:
    contract_path = (
        Path(__file__).parent / "contracts" / "locagent_openhands_efe287c.yaml"
    )
    expected = yaml.safe_load(contract_path.read_text(encoding="utf-8"))

    assert _schema_contract(get_locagent_tool_schemas()) == expected


def test_tool_schema_results_are_independent() -> None:
    first = get_locagent_tool_schemas()
    first[0]["function"]["name"] = "mutated"

    assert get_locagent_tool_schemas()[0]["function"]["name"] == (
        "search_code_snippets"
    )


def test_exact_entity_returns_one_based_numbered_source(
    provider: LocAgentToolProvider,
) -> None:
    result = provider.get_entity_contents(
        ["src/service.py:BillingService.calculate_tax"]
    )

    assert "Found function `src/service.py:BillingService.calculate_tax`" in result
    assert "(lines 2-4)" in result
    assert "2 |     def calculate_tax" in result
    assert "4 |         return helper(amount)" in result


def test_ambiguous_symbol_lists_repository_relative_candidates(
    provider: LocAgentToolProvider,
) -> None:
    result = provider.get_entity_contents(["helper"])

    assert "Multiple entities match `helper`" in result
    assert "`src/other.py:helper`" in result
    assert "`src/service.py:helper`" in result
    assert "/tmp/" not in result


def test_dotfile_path_keeps_its_leading_period(
    provider: LocAgentToolProvider,
) -> None:
    result = provider.get_entity_contents([".github/workflows/ci.yml."])

    assert "`.github/workflows/ci.yml`" in result
    assert "name: CI" in result


def test_keyword_search_uses_prebuilt_bm25(
    provider: LocAgentToolProvider,
) -> None:
    result = provider.search_code_snippets(
        search_terms=["invoice sales computation"],
        file_path_or_pattern="src/*.py",
    )

    assert "src/service.py:BillingService.calculate_tax" in result
    assert "Compute sales tax for an invoice" in result


def test_file_glob_restricts_symbol_matches(
    provider: LocAgentToolProvider,
) -> None:
    result = provider.search_code_snippets(
        search_terms=["helper"],
        file_path_or_pattern="src/service.py",
    )

    assert "src/service.py:helper" in result
    assert "src/other.py:helper" not in result


def test_line_lookup_returns_narrowest_containing_symbol(
    provider: LocAgentToolProvider,
) -> None:
    result = provider.search_code_snippets(
        line_nums=[3],
        file_path_or_pattern="src/service.py",
    )

    assert "src/service.py:BillingService.calculate_tax" in result
    assert "(lines 2-4)" in result


def test_uncovered_line_uses_bounded_source_window(
    provider: LocAgentToolProvider,
) -> None:
    result = provider.search_code_snippets(
        line_nums=[5],
        file_path_or_pattern="src/service.py",
    )

    assert "Found code snippet in file `src/service.py`" in result
    assert "5 | " in result


def test_reference_traversal_discloses_broader_relation(
    provider: LocAgentToolProvider,
) -> None:
    result = provider.explore_tree_structure(
        ["src/service.py:BillingService.calculate_tax"],
        dependency_type_filter=["invokes"],
        traversal_depth=1,
    )

    assert "invokes -> src/service.py:helper" in result
    assert "conservative superset of calls" in result


def test_upstream_traversal_marks_incoming_direction(
    provider: LocAgentToolProvider,
) -> None:
    result = provider.explore_tree_structure(
        ["src/service.py:helper"],
        direction="upstream",
        dependency_type_filter=["invokes"],
        traversal_depth=1,
    )

    assert "<- invokes - src/service.py:BillingService.calculate_tax" in result


def test_exact_containment_traversal_has_no_fidelity_warning(
    provider: LocAgentToolProvider,
) -> None:
    result = provider.explore_tree_structure(
        ["src/service.py:BillingService"],
        dependency_type_filter=["contains"],
        traversal_depth=1,
    )

    assert "contains -> src/service.py:BillingService.calculate_tax" in result
    assert "Relation fidelity:" not in result


def test_root_tree_uses_graph_files(provider: LocAgentToolProvider) -> None:
    result = provider.explore_tree_structure(
        ["/"],
        dependency_type_filter=["contains"],
        traversal_depth=3,
    )

    assert "src" in result
    assert "service.py" in result
    assert "README.md" in result


def test_unlimited_root_tree_is_not_collapsed(
    provider: LocAgentToolProvider,
) -> None:
    result = provider.explore_tree_structure(
        ["/"],
        dependency_type_filter=["contains"],
        traversal_depth=-1,
    )

    assert "service.py" in result


def test_unlimited_traversal_uses_node_budget_not_hidden_depth_cap(
    provider: LocAgentToolProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nodes = [
        RepositoryEntity(
            vertex_id=index,
            canonical_name=f"node-{index}",
            display_name=f"node-{index}",
            kind=NODE_TYPE_FUNCTION,
            file_path="src/service.py",
            qualified_name=f"node_{index}",
            simple_name=f"node_{index}",
            start_line=0,
            end_line=0,
        )
        for index in range(26)
    ]

    def adjacent(entity, **_kwargs):
        if entity.vertex_id + 1 >= len(nodes):
            return []
        return [
            RepositoryRelation(
                source=entity,
                target=nodes[entity.vertex_id + 1],
                edge_type=EDGE_TYPE_REFERENCE,
            )
        ]

    monkeypatch.setattr(provider.repository, "adjacent", adjacent)
    traversed = provider.repository.traverse(
        nodes[0],
        depth=-1,
        max_nodes=len(nodes),
    )

    assert len(traversed) == 25
    assert traversed[-1][0] == 25


def test_openhands_style_bindings_and_json_dispatch(
    provider: LocAgentToolProvider,
) -> None:
    bindings = bind_locagent_tools(provider)

    assert set(bindings) == {
        "search_code_snippets",
        "get_entity_contents",
        "explore_tree_structure",
    }
    result = dispatch_locagent_tool_call(
        provider,
        "get_entity_contents",
        '{"entity_names":["src/model.py:TaxRule"]}',
    )
    search_result = dispatch_locagent_tool_call(
        provider,
        "search_code_snippets",
        {
            "search_terms": ["invoice sales computation"],
            "file_path_or_pattern": "src/*.py",
        },
    )
    graph_result = dispatch_locagent_tool_call(
        provider,
        "explore_tree_structure",
        {
            "start_entities": ["src/service.py:BillingService.calculate_tax"],
            "dependency_type_filter": ["invokes"],
            "traversal_depth": 1,
        },
    )

    assert "src/model.py:TaxRule" in result
    assert "src/service.py:BillingService.calculate_tax" in search_result
    assert "invokes -> src/service.py:helper" in graph_result

    with pytest.raises(KeyError, match="unknown LocAgent tool"):
        dispatch_locagent_tool_call(provider, "bash", {})


def test_repository_path_escape_is_rejected(
    provider: LocAgentToolProvider,
) -> None:
    with pytest.raises(RepositoryPathError, match="traversal"):
        provider.repository.source_path("../outside.py")

    with pytest.raises(RepositoryPathError, match="absolute"):
        provider.repository.source_path("/etc/passwd")


def test_missing_graph_fails_explicitly(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    manifest = RepoManifest(repo_path=str(repo), commit="current")
    context = SimpleNamespace(
        manifest=manifest,
        symbol_graph=None,
        errors={},
    )

    with pytest.raises(
        IntegrationCapabilityError, match="required 'symbol_graph' view"
    ):
        LocAgentToolProvider(context)


def test_optional_adapter_distance_fails_explicitly_without_graph(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    context = SimpleNamespace(
        manifest=RepoManifest(repo_path=str(repo), commit="current"),
        symbol_graph=None,
        errors={},
    )
    adapter = RepositoryAdapter(context, require_graph=False)

    with pytest.raises(
        IntegrationCapabilityError,
        match="required 'symbol_graph' view",
    ):
        adapter.distance("first", "second")


def test_stale_graph_fails_explicitly(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    manifest = RepoManifest(
        repo_path=str(repo),
        commit="new",
        indexes={
            "symbol_graph": IndexEntry(
                index_type="symbol_graph",
                path=str(tmp_path / "graph.pkl"),
                built_at="2026-07-30T00:00:00Z",
                built_at_epoch=1_785_369_600.0,
                status="fresh",
                commit="old",
            )
        },
    )
    context = SimpleNamespace(
        manifest=manifest,
        symbol_graph=None,
        errors={},
    )

    with pytest.raises(IntegrationCapabilityError, match="source identity is stale"):
        RepositoryAdapter(context)


def test_keyword_search_without_bm25_fails_at_operation_boundary(
    integration_manifest: Path,
) -> None:
    provider = LocAgentToolProvider.from_manifest(integration_manifest)
    provider.context.bm25 = None
    provider.context.manifest.indexes.pop("bm25")

    assert "TaxRule" in provider.get_entity_contents(["src/model.py:TaxRule"])
    with pytest.raises(IntegrationCapabilityError, match="required 'bm25' view"):
        provider.search_code_snippets(search_terms=["unindexed prose"])


def test_loading_provider_creates_no_foreign_index_directory(
    integration_manifest: Path,
) -> None:
    repo_root = Path(RepoManifest.load(integration_manifest).repo_path)
    before = sorted(path.relative_to(repo_root) for path in repo_root.rglob("*"))

    LocAgentToolProvider.from_manifest(integration_manifest)

    after = sorted(path.relative_to(repo_root) for path in repo_root.rglob("*"))
    assert after == before
    assert not (repo_root / "_index_data").exists()
