# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for deterministic MCP call-result bounds on composed exploration."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any

from mcp.server import MCPServer

from codenib.mcp.explore_bounds import (
    MAX_EXPLORE_CALL_RESULT_BYTES,
    MAX_EXPLORE_PAYLOAD_BYTES,
    MCP_ENVELOPE_RESERVE_BYTES,
    bound_explore_response,
    call_tool_result_bytes,
    explore_call_result_bytes,
)


def _context(
    marker: str,
    *,
    content_chars: int = 2_000,
    anchor_count: int = 4,
    anchor_text: str = "symbol",
) -> dict[str, Any]:
    return {
        "start_line": 1,
        "end_line": 80,
        "verified": True,
        "content_kind": "live_source",
        "content_fingerprint": f"sha256:{marker}",
        "content": (marker + "源🚀") * content_chars,
        "anchors": [
            {
                "symbol": f"{anchor_text}_{index}",
                "kind": "function",
                "origins": ["retrieval", "route"],
            }
            for index in range(anchor_count)
        ],
    }


def _relationship(
    seed: str,
    *,
    node_count: int = 40,
    edge_count: int = 160,
    label: str = "node",
) -> dict[str, Any]:
    nodes = [
        {
            "name": f"{label}_{index}",
            "file": f"src/{label}_{index}.py",
            "line": index + 1,
            "kind": "function",
            "depth": 1,
        }
        for index in range(node_count)
    ]
    return {
        "seed": seed,
        "root": f"{label}_0",
        "direction": "both",
        "nodes": nodes,
        "edges": [
            {
                "source": f"{label}_{index % node_count}",
                "target": f"{label}_{(index + 1) % node_count}",
                "type": "reference",
            }
            for index in range(edge_count)
        ],
        "truncated": False,
        "note": "",
    }


def _response(
    *,
    query: str = "find request routing",
    contexts: list[dict[str, Any]] | None = None,
    relationships: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "query": query,
        "plan": {
            "name": "composed_explore_v1",
            "budget": "thorough",
            "delivery": {"total_content_chars": 32_000},
        },
        "source": {
            "repository": "example/project",
            "commit": "abc123",
            "source_fingerprint": "sha256:checkout",
            "verified": True,
        },
        "summary": {},
        "files": [
            {
                "file": "src/service.py",
                "contexts": (
                    contexts
                    if contexts is not None
                    else [_context("first"), _context("second")]
                ),
            }
        ],
        "relationships": relationships or [],
        "diagnostics": [],
    }


def _all_contexts(response: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        context
        for group in response.get("files", [])
        for context in group.get("contexts", [])
    ]


def test_measurement_matches_real_mcp_structured_dict_conversion() -> None:
    payload = _response(
        contexts=[_context("measure", content_chars=10, anchor_count=1)]
    )
    server = MCPServer("explore-bound-measurement")

    @server.tool(name="probe")
    async def probe() -> dict[str, Any]:
        return payload

    actual = asyncio.run(server.call_tool("probe", {}))

    assert actual.structured_content == payload
    assert call_tool_result_bytes(actual) == explore_call_result_bytes(payload)


def test_adversarial_unicode_response_stays_inside_reserved_call_result_cap() -> None:
    long_label = ("符号🚀路径" * 2_000) + "tail"
    payload = _response(
        query=("查询🚀" * 8_000),
        contexts=[
            _context(
                "甲🚀",
                content_chars=8_000,
                anchor_count=20,
                anchor_text=long_label,
            ),
            _context(
                "乙🚀",
                content_chars=8_000,
                anchor_count=20,
                anchor_text=long_label,
            ),
        ],
        relationships=[
            _relationship(
                f"seed_{index}",
                label=f"{long_label}_{index}",
            )
            for index in range(3)
        ],
    )
    original = deepcopy(payload)

    bounded = bound_explore_response(payload)
    projection = bounded["summary"]["response_projection"]
    encoded_bytes = explore_call_result_bytes(bounded)

    assert payload == original
    assert encoded_bytes == projection["encoded_bytes"]
    assert encoded_bytes <= MAX_EXPLORE_PAYLOAD_BYTES
    assert encoded_bytes + MCP_ENVELOPE_RESERVE_BYTES <= (MAX_EXPLORE_CALL_RESULT_BYTES)
    assert projection["applied"] is True
    assert projection["relationships_summarized"] >= 1
    assert projection["metadata_fields_elided"] >= 1
    assert bounded["query_fingerprint"].startswith("sha256:")
    assert any(
        diagnostic["code"] == "response_budget_applied"
        for diagnostic in bounded["diagnostics"]
    )


def test_projection_is_deterministic_for_identical_unicode_payloads() -> None:
    long_label = "一致🚀" * 1_000
    payload = _response(
        contexts=[
            _context(
                "stable",
                content_chars=4_000,
                anchor_count=12,
                anchor_text=long_label,
            )
        ],
        relationships=[_relationship("stable", label=long_label)],
    )

    first = bound_explore_response(payload, max_bytes=40_000)
    second = bound_explore_response(payload, max_bytes=40_000)

    assert first == second
    assert explore_call_result_bytes(first) == explore_call_result_bytes(second)
    assert first["summary"]["response_projection"]["encoded_bytes"] == (
        explore_call_result_bytes(first)
    )


def test_rebounding_preserves_projection_provenance() -> None:
    payload = _response(
        contexts=[
            _context("first", content_chars=2_000),
            _context("second", content_chars=2_000),
        ]
    )

    first = bound_explore_response(payload, max_bytes=16_000)
    second = bound_explore_response(first, max_bytes=16_000)

    assert first == second
    projection = second["summary"]["response_projection"]
    assert projection["applied"] is True
    assert projection["content_bodies_omitted"] >= 1


def test_exact_cap_is_accepted_and_one_byte_over_is_projected() -> None:
    payload = _response(
        contexts=[_context("boundary", content_chars=20, anchor_count=1)]
    )
    exact_cap = 20_000
    for _ in range(8):
        candidate = bound_explore_response(payload, max_bytes=exact_cap)
        assert candidate["summary"]["response_projection"]["applied"] is False
        measured = explore_call_result_bytes(candidate)
        if measured == exact_cap:
            break
        exact_cap = measured
    else:  # pragma: no cover - integer-width accounting converges immediately
        raise AssertionError("could not find the response's exact byte boundary")

    exact = bound_explore_response(payload, max_bytes=exact_cap)
    one_byte_tight = bound_explore_response(payload, max_bytes=exact_cap - 1)

    assert explore_call_result_bytes(exact) == exact_cap
    assert exact["summary"]["response_projection"]["applied"] is False
    assert explore_call_result_bytes(one_byte_tight) <= exact_cap - 1
    assert one_byte_tight["summary"]["response_projection"]["applied"] is True


def test_relationship_then_anchor_then_body_priority_is_stable() -> None:
    long_label = "dependency🚀" * 500
    payload = _response(
        contexts=[
            _context("a", content_chars=140, anchor_count=4, anchor_text=long_label),
            _context("b", content_chars=200, anchor_count=4, anchor_text=long_label),
        ],
        relationships=[
            _relationship("root", edge_count=80, label=long_label),
        ],
    )

    relationship_only = bound_explore_response(payload, max_bytes=32_000)
    relationship = relationship_only["relationships"][0]
    contexts = _all_contexts(relationship_only)
    assert relationship["nodes"] == []
    assert relationship["edges"] == []
    assert relationship["response_projection"] == {
        "reason": "response_budget",
        "original_nodes": 40,
        "original_edges": 80,
        "returned_nodes": 0,
        "returned_edges": 0,
    }
    assert all("content" in context for context in contexts)
    assert all(len(context["anchors"]) == 4 for context in contexts)

    anchors_trimmed = bound_explore_response(payload, max_bytes=22_000)
    contexts = _all_contexts(anchors_trimmed)
    assert all("content" in context for context in contexts)
    assert all(len(context["anchors"]) == 1 for context in contexts)

    body_trimmed = bound_explore_response(payload, max_bytes=16_000)
    contexts = _all_contexts(body_trimmed)
    assert "content" in contexts[0]
    assert "content" not in contexts[1]
    assert contexts[1]["content_kind"] == "response_budget_location"
    assert body_trimmed["summary"]["response_projection"]["content_bodies_omitted"] == 1


def test_irreducible_extension_uses_bounded_minimal_envelope() -> None:
    payload = _response(
        contexts=[_context("minimal", content_chars=100, anchor_count=1)]
    )
    payload["extension"] = list(range(20_000))

    bounded = bound_explore_response(payload, max_bytes=4_000)
    projection = bounded["summary"]["response_projection"]

    assert projection["minimal"] is True
    assert projection["encoded_bytes"] == explore_call_result_bytes(bounded)
    assert projection["encoded_bytes"] <= 4_000
    assert bounded["files"] == []
    assert bounded["relationships"] == []
    assert bounded["diagnostics"][0]["code"] == "response_budget_exhausted"


def test_ultra_minimal_envelope_is_idempotent() -> None:
    payload = _response(contexts=[_context("ultra", content_chars=100, anchor_count=1)])
    payload["extension"] = list(range(20_000))

    first = bound_explore_response(payload, max_bytes=2_000)
    second = bound_explore_response(first, max_bytes=2_000)

    assert first == second
    assert explore_call_result_bytes(first) <= 2_000
    assert first["summary"]["response_projection"]["minimal"] is True
