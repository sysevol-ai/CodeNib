# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from codenib.compiler.manifest import RepoManifest
from codenib.integrations import IntegrationCapabilityError
from codenib.integrations.orcaloca import (
    ORCALOCA_HISTORY_FIELDS,
    ORCALOCA_MANAGER_HOOKS,
    ORCALOCA_REVISION,
    ORCALOCA_TOOL_NAMES,
    OrcaLocaSearchProvider,
    make_orcaloca_search_manager_factory,
)


@pytest.fixture()
def provider(integration_manifest: Path) -> OrcaLocaSearchProvider:
    return OrcaLocaSearchProvider.from_manifest(integration_manifest)


def _tool_contract(provider: OrcaLocaSearchProvider) -> dict:
    tools = {}
    for name in ORCALOCA_TOOL_NAMES:
        signature = inspect.signature(getattr(provider, name))
        defaults = {
            parameter.name: (None if parameter.default is None else parameter.default)
            for parameter in signature.parameters.values()
            if parameter.default is not inspect.Parameter.empty
        }
        tools[name] = {
            "parameters": list(signature.parameters),
            "defaults": defaults,
        }
    return {
        "revision": ORCALOCA_REVISION,
        "tools": tools,
        "manager_hooks": list(ORCALOCA_MANAGER_HOOKS),
        "history_fields": list(ORCALOCA_HISTORY_FIELDS),
    }


def test_pinned_orcaloca_contract(
    provider: OrcaLocaSearchProvider,
) -> None:
    contract_path = Path(__file__).parent / "contracts" / "orcaloca_37db289.yaml"
    expected = yaml.safe_load(contract_path.read_text(encoding="utf-8"))

    assert _tool_contract(provider) == expected
    assert [function.__name__ for function in provider.get_search_functions()] == list(
        ORCALOCA_TOOL_NAMES
    )
    for hook in ORCALOCA_MANAGER_HOOKS:
        assert callable(getattr(provider, hook))


def test_file_tree_and_contents_use_manifest_source(
    provider: OrcaLocaSearchProvider,
) -> None:
    tree = provider.search_file_tree()
    content = provider.search_file_contents("service.py", "src")

    assert "service.py" in tree
    assert "model.py" in tree
    assert "File Path: src/service.py" in content
    assert "class BillingService" in content
    assert (
        provider.get_query_from_history("search_file_contents", "src/service.py")
        == "src/service.py"
    )


def test_class_decomposition_matches_search_worker_hooks(
    provider: OrcaLocaSearchProvider,
) -> None:
    result = provider.search_class("BillingService")
    frame = provider.get_frame_from_history("search_class", "BillingService")

    assert "File Path: src/service.py" in result
    assert frame is not None
    assert set(frame) == set(ORCALOCA_HISTORY_FIELDS)
    assert frame["query_type"] == "class"
    assert frame["search_query"] == "src/service.py::BillingService"

    method_names, method_sources = provider._get_class_methods(frame["search_query"])
    assert method_names == ["src/service.py::BillingService::calculate_tax"]
    assert "def calculate_tax" in method_sources[0]


def test_method_lookup_produces_one_based_location_and_history(
    provider: OrcaLocaSearchProvider,
) -> None:
    result = provider.search_method_in_class(
        "BillingService",
        "calculate_tax",
        "src/service.py",
    )
    query = "src/service.py::BillingService::calculate_tax"
    location = provider._get_exact_loc(query)

    assert "Method Content" in result
    assert "return helper(amount)" in result
    assert location is not None
    assert location.type == "method"
    assert location.loc.file_name == "src/service.py"
    assert location.loc.node_name == query
    assert (location.loc.start_line, location.loc.end_line) == (2, 4)
    assert provider.get_query_from_history("search_method_in_class", query) == query


def test_callable_disambiguation_preserves_upstream_markers(
    provider: OrcaLocaSearchProvider,
) -> None:
    result = provider.search_callable("helper")

    assert result.startswith("<Disambiguation>")
    assert result.endswith("</Disambiguation>")
    assert "File Path: src/other.py" in result
    assert "File Path: src/service.py" in result
    frame = provider.get_frame_from_history("search_callable", "helper")
    assert frame is not None
    assert frame["query_type"] == "disambiguation"


def test_source_fragment_returns_narrowest_graph_range(
    provider: OrcaLocaSearchProvider,
) -> None:
    result = provider.search_source_code("src/service.py", "return helper(amount)")

    assert "def calculate_tax" in result
    assert "return helper(amount)" in result
    assert "class BillingService" not in result


def test_skeleton_thresholds_use_upstream_line_span(
    integration_manifest: Path,
) -> None:
    at_boundary = OrcaLocaSearchProvider.from_manifest(
        integration_manifest,
        file_skeleton_threshold=6,
        class_skeleton_threshold=3,
    )
    above_boundary = OrcaLocaSearchProvider.from_manifest(
        integration_manifest,
        file_skeleton_threshold=5,
        class_skeleton_threshold=2,
    )

    assert "File Content:" in at_boundary.search_file_contents("service.py", "src")
    assert "Class Content:" in at_boundary.search_class("BillingService")
    assert "File Skeleton:" in above_boundary.search_file_contents("service.py", "src")
    assert "Class Skeleton:" in above_boundary.search_class("BillingService")


def test_file_and_method_decomposition_hooks(
    provider: OrcaLocaSearchProvider,
) -> None:
    functions, sources = provider._get_file_functions("src/service.py")
    methods, method_sources = provider._get_disambiguous_methods(
        "calculate_tax",
        class_name="BillingService",
        file_path="src/service.py",
    )

    assert functions == [
        "src/service.py::BillingService",
        "src/service.py::helper",
    ]
    assert any("class BillingService" in source for source in sources)
    assert methods == ["src/service.py::BillingService::calculate_tax"]
    assert "def calculate_tax" in method_sources[0]
    assert provider._get_disambiguous_classes("TaxRule") == ["src/model.py"]
    assert provider._get_disambiguous_files("service.py") == ["src/service.py"]


def test_python_metadata_matches_pinned_orcaloca_ast_contract(
    orcaloca_contract_manifest: Path,
) -> None:
    provider = OrcaLocaSearchProvider.from_manifest(orcaloca_contract_manifest)
    global_info = provider._get_exact_loc("src/contract.py::MIGRATION_TEMPLATE")
    method_info = provider._get_exact_loc("src/contract.py::Writer::as_string")

    assert global_info is not None
    assert global_info.type == "global_variable"
    assert (global_info.loc.start_line, global_info.loc.end_line) == (1, 3)
    assert method_info is not None
    assert method_info.type == "method"
    assert (method_info.loc.start_line, method_info.loc.end_line) == (9, 11)

    assert provider._direct_get_file_skeleton("src/contract.py") == (
        "\nGlobal_variable: MIGRATION_TEMPLATE\n"
        "Signature: MIGRATION_TEMPLATE\n"
        "\nClass: Writer\n"
        "Signature: Writer\n"
        "Docstring: Render migrations.\n"
        "\nFunction: normalize\n"
        "Signature: normalize(value)\n"
        "Docstring: Normalize one value.\n"
    )
    assert provider._direct_get_class("src/contract.py::Writer") == (
        "Class Signature: Writer\n"
        "Docstring: Render migrations.\n"
        "\nMethod: as_string\n"
        "Method Signature: as_string(cls, value)\n"
        "Docstring: Render one value.\n"
    )

    functions, sources = provider._get_file_functions("src/contract.py")
    methods, method_sources = provider._get_class_methods("src/contract.py::Writer")
    assert functions == ["src/contract.py::Writer", "src/contract.py::normalize"]
    assert "MIGRATION_TEMPLATE" not in functions
    assert not provider.get_node_existence("src/contract.py::IGNORED_ANNOTATION")
    assert provider._get_exact_loc("src/contract.py::IGNORED_ANNOTATION") is None
    assert methods == ["src/contract.py::Writer::as_string"]
    assert sources[0].startswith("class Writer:")
    assert method_sources == [
        "    def as_string(cls, value):\n"
        '        """Render one value."""\n'
        "        return value\n"
    ]


def test_graph_identity_distance_and_duplicate_tracking(
    provider: OrcaLocaSearchProvider,
) -> None:
    method = "src/service.py::BillingService::calculate_tax"
    helper = "src/service.py::helper"
    other_file = "src/other.py::helper"

    assert provider.get_node_existence(method)
    assert provider.get_distance_between_queries(method, helper) == 1
    assert provider.get_distance_between_queries(method, other_file) == 999_999
    assert provider.get_distance_between_queries(method, "missing") == 999_999
    assert provider.get_distance_between_queries(None, helper) == -1
    assert provider.check_and_add_query(method) is False
    assert provider.check_and_add_query(method) is True


def test_bug_location_schema_is_policy_consumable(
    provider: OrcaLocaSearchProvider,
) -> None:
    result = provider._get_bug_location("calculate_tax", file_path="src/service.py")

    assert result == {
        "bug_locations": [
            {
                "file_path": "src/service.py",
                "class_name": "BillingService",
                "method_name": "calculate_tax",
            }
        ]
    }


def test_policy_smoke_sequence_uses_one_provider(
    provider: OrcaLocaSearchProvider,
) -> None:
    provider.search_class("BillingService")
    class_frame = provider.get_frame_from_history("search_class", "BillingService")
    assert class_frame is not None

    method_names, _ = provider._get_class_methods(class_frame["search_query"])
    method_name = method_names[0]
    provider.search_method_in_class(
        "BillingService",
        method_name.split("::")[-1],
        class_frame["file_path"],
    )

    assert provider.get_node_existence(method_name)
    assert (
        provider.get_query_from_history("search_method_in_class", method_name)
        == method_name
    )
    assert provider._get_bug_location("calculate_tax", class_frame["file_path"])[
        "bug_locations"
    ]


def test_factory_reuses_manifest_and_rejects_repo_mismatch(
    integration_manifest: Path,
    tmp_path: Path,
) -> None:
    repo_root = Path(RepoManifest.load(integration_manifest).repo_path)
    before = sorted(path.relative_to(repo_root) for path in repo_root.rglob("*"))
    factory = make_orcaloca_search_manager_factory(integration_manifest)

    provider = factory(str(repo_root))
    after = sorted(path.relative_to(repo_root) for path in repo_root.rglob("*"))

    assert provider.repository.repo_root == repo_root.resolve()
    assert before == after
    assert not (repo_root / "_index_data").exists()
    with pytest.raises(ValueError, match="does not match"):
        factory(str(tmp_path))


def test_missing_graph_fails_without_upstream_fallback(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    context = SimpleNamespace(
        manifest=RepoManifest(repo_path=str(repo), commit="current"),
        symbol_graph=None,
        errors={},
    )

    with pytest.raises(
        IntegrationCapabilityError,
        match="required 'symbol_graph' view",
    ):
        OrcaLocaSearchProvider(context)


def test_adapter_has_no_orcaloca_runtime_dependencies() -> None:
    source_path = Path(__file__).parents[2] / "codenib" / "integrations" / "orcaloca.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported_roots.add(node.module.split(".", 1)[0])

    assert imported_roots.isdisjoint({"Orcar", "llama_index", "networkx", "pandas"})
