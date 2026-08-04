# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from codenib.eval.benchmarks.agentless import AgentlessLocation
from codenib.integrations.agentless import AgentlessRepositoryProvider
from codenib.mcp.context import ServerContext


def test_provider_loads_only_symbol_graph(integration_manifest: Path) -> None:
    with patch.object(ServerContext, "load", wraps=ServerContext.load) as load:
        provider = AgentlessRepositoryProvider.from_manifest(integration_manifest)

    load.assert_called_once_with(
        integration_manifest,
        views=frozenset({"symbol_graph"}),
    )
    assert provider.context.symbol_graph is not None
    assert provider.context.bm25 is None
    assert provider.context.vector is None


def test_project_structure_matches_classic_python_boundary(
    integration_manifest: Path,
) -> None:
    provider = AgentlessRepositoryProvider.from_manifest(integration_manifest)

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


def test_root_prefixed_file_response_resolves_to_repository_path(
    integration_manifest: Path,
) -> None:
    provider = AgentlessRepositoryProvider.from_manifest(integration_manifest)

    assert provider.resolve_files(
        "```\nrepo/src/service.py\nsrc/model.py\n```",
        limit=2,
    ) == ("src/service.py", "src/model.py")


def test_skeleton_uses_source_signatures_without_function_bodies(
    integration_manifest: Path,
) -> None:
    provider = AgentlessRepositoryProvider.from_manifest(integration_manifest)

    result = provider.skeleton_context(["src/service.py"])

    assert "class BillingService:" in result
    assert "def calculate_tax(self, amount):" in result
    assert "def helper(amount):" in result
    assert "return helper(amount)" not in result
    assert result.count("...") == 2


def test_line_context_uses_graph_symbol_range_and_one_based_lines(
    integration_manifest: Path,
) -> None:
    provider = AgentlessRepositoryProvider.from_manifest(integration_manifest)

    result = provider.line_context(
        ["src/service.py"],
        [
            AgentlessLocation(
                file_path="src/service.py",
                kind="function",
                name="BillingService.calculate_tax",
            )
        ],
        context_window=0,
    )

    assert "### File: src/service.py ###" in result
    assert "2|    def calculate_tax" in result
    assert "4|        return helper(amount)" in result
    assert "6|def helper" not in result


def test_unknown_coarse_location_does_not_expand_to_whole_file(
    integration_manifest: Path,
) -> None:
    provider = AgentlessRepositoryProvider.from_manifest(integration_manifest)

    result = provider.line_context(
        ["src/model.py"],
        [
            AgentlessLocation(
                file_path="src/model.py",
                kind="function",
                name="missing",
            )
        ],
        context_window=0,
    )

    assert result == ""


def test_source_ast_resolves_assignment_exposed_by_skeleton(
    integration_manifest: Path,
) -> None:
    provider = AgentlessRepositoryProvider.from_manifest(integration_manifest)

    entity = provider.resolve_entity("src/model.py", "variable", "TaxRule.rate")
    context = provider.line_context(
        ["src/model.py"],
        [
            AgentlessLocation(
                file_path="src/model.py",
                kind="variable",
                name="TaxRule.rate",
            )
        ],
        context_window=0,
    )

    assert entity is not None
    assert entity.qualified_name == "TaxRule.rate"
    assert (entity.start_line, entity.end_line) == (1, 1)
    assert "2|    rate = 0.08" in context
