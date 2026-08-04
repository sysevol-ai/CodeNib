# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from codenib.integrations.cosil import (
    COSIL_REVISION,
    CoSILRepositoryProvider,
    get_cosil_tool_schemas,
)
from codenib.mcp.context import ServerContext


@pytest.fixture()
def provider(integration_manifest: Path) -> CoSILRepositoryProvider:
    return CoSILRepositoryProvider.from_manifest(integration_manifest)


def test_provider_loads_only_symbol_graph(integration_manifest: Path) -> None:
    with patch.object(ServerContext, "load", wraps=ServerContext.load) as load:
        provider = CoSILRepositoryProvider.from_manifest(integration_manifest)

    load.assert_called_once_with(
        integration_manifest,
        views=frozenset({"symbol_graph"}),
    )
    assert provider.context.symbol_graph is not None
    assert provider.context.bm25 is None
    assert provider.context.vector is None


def test_tool_contract_is_pinned_and_independent() -> None:
    schemas = get_cosil_tool_schemas()
    assert COSIL_REVISION == "0568e423735b399d5b089996961fea9ae142e4c7"
    assert [item["function"]["name"] for item in schemas] == [
        "get_code_of_class",
        "get_code_of_class_function",
        "get_code_of_file_function",
        "exit",
    ]
    assert schemas[0]["function"]["parameters"]["required"] == [
        "file_name",
        "class_name",
    ]

    schemas[0]["function"]["name"] = "changed"
    assert get_cosil_tool_schemas()[0]["function"]["name"] == "get_code_of_class"


def test_project_structure_keeps_cosil_python_boundary(
    provider: CoSILRepositoryProvider,
) -> None:
    assert provider.files == (
        "src/model.py",
        "src/other.py",
        "src/service.py",
    )
    assert provider.project_structure() == (
        "repo/\n"
        "    src/\n"
        "        model.py\n"
        "        other.py\n"
        "        service.py"
    )


def test_candidate_structure_matches_upstream_fields(
    provider: CoSILRepositoryProvider,
) -> None:
    result = provider.candidate_structure(["src/service.py"])

    assert "file: src/service.py" in result
    assert "class: ['BillingService']" in result
    assert "static functions:  ['helper']" in result
    assert "BillingService: ['calculate_tax']" in result
    assert "class fucntions:" in result


def test_four_tool_provider_returns_exact_source(
    provider: CoSILRepositoryProvider,
) -> None:
    class_source = provider.get_code_of_class("src/service.py", "BillingService")
    method_source = provider.get_code_of_class_function(
        "src/service.py",
        "BillingService",
        "calculate_tax",
    )
    function_source = provider.get_code_of_file_function(
        "src/service.py",
        "helper",
    )

    assert class_source.startswith("class BillingService:")
    assert class_source.endswith("return helper(amount)")
    assert method_source.startswith("    def calculate_tax")
    assert method_source.endswith("return helper(amount)")
    assert function_source == "def helper(amount):\n    return amount * 0.08"
    assert provider.exit() == "Exiting tool calls. Now provide your final answer."


def test_json_dispatch_does_not_evaluate_tool_calls(
    provider: CoSILRepositoryProvider,
) -> None:
    result = provider.dispatch(
        "get_code_of_class_function",
        '{"file_name":"src/service.py","class_name":"BillingService",'
        '"func_name":"calculate_tax"}',
    )

    assert "def calculate_tax" in result
    with pytest.raises(KeyError, match="unknown CoSIL tool"):
        provider.dispatch("bash", {})
    with pytest.raises(ValueError, match="valid JSON"):
        provider.dispatch("get_code_of_class", "{")
    with pytest.raises(ValueError, match="decode to an object"):
        provider.dispatch("get_code_of_class", "[]")


def test_invalid_symbol_preserves_upstream_observation(
    provider: CoSILRepositoryProvider,
) -> None:
    assert provider.get_code_of_class("src/service.py", "Missing") == (
        "You provide a wrong file name or class name. Please try another file "
        "name again."
    )
    assert provider.get_code_of_file_function("src/service.py", "Missing") == (
        "You provide a wrong file name or function name. Please try another "
        "file name again. It may be a class function."
    )


def test_outline_preserves_cosil_function_classification() -> None:
    outline = CoSILRepositoryProvider._parse_outline(
        "module.py",
        """
class Service:
    def run(self):
        return helper()

def helper():
    return 1

async def ignored_by_upstream():
    return 2
""".strip(),
    )

    assert outline is not None
    assert [item.name for item in outline.classes] == ["Service"]
    assert [item.name for item in outline.classes[0].methods] == ["run"]
    assert [item.name for item in outline.functions] == ["helper"]


def test_outline_preserves_upstream_traversal_order() -> None:
    outline = CoSILRepositoryProvider._parse_outline(
        "module.py",
        """
def shared():
    return 1

class Service:
    def shared(self):
        return 2
""".strip(),
    )

    assert outline is not None
    assert [item.name for item in outline.classes[0].methods] == ["shared"]
    assert [item.name for item in outline.functions] == ["shared"]
