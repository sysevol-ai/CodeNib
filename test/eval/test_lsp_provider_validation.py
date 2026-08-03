# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for static-vs-reference LSP provider validation."""

from __future__ import annotations

import pytest

from codenib.agent.lsp_provider import JSON_RPC_LSP_PROVIDER, StaticLSPProvider
from codenib.eval.agent_runner.lsp_provider_validation import (
    LSPProviderRequest,
    compare_static_lsp_provider,
    default_lsp_provider_fingerprint,
    fingerprint_lsp_start_location_set,
    fingerprint_lsp_start_locations,
    render_lsp_provider_validation_markdown,
    summarize_lsp_provider_validation,
)
from codenib.graph.code_graph import CodeGraph
from codenib.types import NODE_TYPE_FUNCTION


def _range_graph() -> CodeGraph:
    graph = CodeGraph()
    graph.add_file_node("caller.py")
    graph.add_symbol_node(
        "caller.run",
        line=0,
        scope_start_line=0,
        scope_end_line=3,
        symbol_type=NODE_TYPE_FUNCTION,
    )
    graph.update_current_scope("caller.run", start_line=0, end_line=3)
    graph.add_symbol_reference(
        "callee.load_config",
        module_path="callee.py",
        symbol_type=NODE_TYPE_FUNCTION,
        anchor_file="caller.py",
        anchor_line=1,
    )
    graph.add_file_node("callee.py")
    graph.add_symbol_node(
        "callee.load_config",
        line=4,
        scope_start_line=4,
        scope_end_line=8,
        symbol_type=NODE_TYPE_FUNCTION,
    )
    graph.graph.vs[graph.name_to_vertex["callee.load_config"]][
        "unified_name"
    ] = "callee.py:load_config()"
    graph.build_range_indexes()
    return graph


def _clock(values):
    iterator = iter(values)
    return lambda: next(iterator)


def test_compare_static_lsp_provider_reports_equivalent_speedup():
    graph = _range_graph()

    def reference_provider(capability, arguments):
        assert capability == "definition"
        assert arguments == {"symbol": "load_config", "top_k": 2}
        return list(StaticLSPProvider(graph).definition(**arguments))

    rows = compare_static_lsp_provider(
        [
            LSPProviderRequest(
                capability="textDocument/definition",
                arguments={"symbol": "load_config", "top_k": 2},
                request_id="jump-load-config",
            )
        ],
        graph=graph,
        reference_provider=reference_provider,
        clock=_clock([10.0, 10.001, 20.0, 20.006]),
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.same_result is True
    assert row.verdict == "equivalent_static_faster"
    assert row.latency_saved_ms == pytest.approx(5.0)
    assert row.speedup_ratio == pytest.approx(6.0)
    assert row.static_call.provider == "codenib_static_index"
    assert row.reference_call.provider == JSON_RPC_LSP_PROVIDER
    assert row.static_call.metadata["lsp_method"] == "textDocument/definition"

    markdown = render_lsp_provider_validation_markdown(rows)
    assert "# LSP provider validation" in markdown
    assert "| jump-load-config | definition | equivalent_static_faster | yes |" in (
        markdown
    )


def test_compare_static_lsp_provider_marks_fallback_when_graph_unavailable():
    calls = []

    def reference_provider(capability, arguments):
        calls.append((capability, arguments))
        return []

    rows = compare_static_lsp_provider(
        [
            {
                "capability": "textDocument/definition",
                "arguments": {"symbol": "load_config"},
                "request_id": "fallback",
            }
        ],
        graph=None,
        reference_provider=reference_provider,
        clock=_clock([30.0, 30.002]),
    )

    row = rows[0]
    assert calls == [("definition", {"symbol": "load_config"})]
    assert row.same_result is None
    assert row.verdict == "fallback_required"
    assert row.static_call.status == "unavailable"
    assert row.static_call.fallback_reason == "symbol_graph_unavailable"
    assert row.reference_call.status == "ok"


def test_summarize_lsp_provider_validation_marks_promotion_ready():
    graph = _range_graph()

    def reference_provider(capability, arguments):
        return list(StaticLSPProvider(graph).definition(**arguments))

    rows = compare_static_lsp_provider(
        [
            LSPProviderRequest(
                capability="definition",
                arguments={"symbol": "load_config"},
                request_id="jump-load-config",
            )
        ],
        graph=graph,
        reference_provider=reference_provider,
        clock=_clock([1.0, 1.001, 2.0, 2.004]),
    )

    summary = summarize_lsp_provider_validation(rows)

    assert summary.total == 1
    assert summary.verdict_counts == {"equivalent_static_faster": 1}
    assert summary.same_result_count == 1
    assert summary.mismatch_count == 0
    assert summary.fallback_count == 0
    assert summary.error_count == 0
    assert summary.promotion_ready is True


def test_start_location_set_fingerprint_ignores_provider_order():
    static_order = [
        {"file": "callee.py", "start_line": 0},
        {"file": "caller.py", "start_line": 3},
        {"file": "caller.py", "start_line": 6},
    ]
    live_order = [
        {"file": "caller.py", "start_line": 3},
        {"file": "caller.py", "start_line": 6},
        {"file": "callee.py", "start_line": 0},
    ]

    assert fingerprint_lsp_start_location_set(static_order) == (
        fingerprint_lsp_start_location_set(live_order)
    )
    assert fingerprint_lsp_start_locations(static_order) != (
        fingerprint_lsp_start_locations(live_order)
    )


def test_default_lsp_provider_fingerprint_uses_set_for_references():
    reference_request = LSPProviderRequest(
        capability="textDocument/references",
        arguments={"file_path": "caller.py", "line": 2},
    )
    definition_request = LSPProviderRequest(
        capability="textDocument/definition",
        arguments={"file_path": "caller.py", "line": 2},
    )

    assert default_lsp_provider_fingerprint(reference_request) is (
        fingerprint_lsp_start_location_set
    )
    assert default_lsp_provider_fingerprint(definition_request) is (
        fingerprint_lsp_start_locations
    )
