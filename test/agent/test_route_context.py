# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for static LSP route startup context helpers."""

from __future__ import annotations

from codeminer.agent.route_context import (
    build_lsp_route_context,
    extract_lsp_symbol_seeds,
    render_lsp_route_context,
)
from codeminer.types import QueriedNode


def test_extract_lsp_symbol_seeds_prefers_explicit_and_code_like_tokens():
    seeds = extract_lsp_symbol_seeds(
        "Fix `HandleRequest` using NewResolver and pkg.DefaultConfig. "
        "Ignore ERROR and plain words.",
        explicit=["ExplicitSeed", "HandleRequest"],
        limit=5,
    )

    assert seeds == [
        "ExplicitSeed",
        "HandleRequest",
        "NewResolver",
        "pkg.DefaultConfig",
    ]


def test_render_lsp_route_context_uses_agent_facing_line_numbers():
    node = QueriedNode(
        node_name="svc.HandleRequest",
        type="method",
        file="svc.py",
        start_line=9,
        end_line=11,
        content="route endpoint: direct seed HandleRequest",
    )

    rendered = render_lsp_route_context(["HandleRequest"], [node])

    assert "# Static LSP route hints" in rendered
    assert "svc.py:10-12 svc.HandleRequest [method]" in rendered
    assert "route endpoint: direct seed HandleRequest" in rendered


def test_build_lsp_route_context_calls_executor_with_extracted_seeds():
    calls = []

    def executor(**kwargs):
        calls.append(kwargs)
        return [
            QueriedNode(
                node_name="svc.NewResolver",
                type="function",
                file="svc.py",
                start_line=20,
                end_line=22,
                content="route bridge: direct seed NewResolver",
            )
        ]

    context = build_lsp_route_context(
        executor,
        "Check `NewResolver` cache directory",
        seed_limit=3,
        top_k=7,
        include_neighbors=False,
    )

    assert context.seeds == ("NewResolver",)
    assert calls == [
        {
            "symbols": ["NewResolver"],
            "query": "Check `NewResolver` cache directory",
            "top_k": 7,
            "include_neighbors": False,
        }
    ]
    assert "svc.py:21-23 svc.NewResolver" in context.text
