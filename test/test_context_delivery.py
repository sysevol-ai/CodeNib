# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json

from codenib.context_delivery import (
    DEFAULT_METADATA_FIELD_LIMITS,
    DEFAULT_TOTAL_CONTENT_CHARS,
    focus_source_content,
    project_evidence_payloads,
)


def test_focus_source_content_keeps_query_evidence_within_small_budget() -> None:
    content = "\n".join(
        ["header", *(f"filler_{line}" for line in range(200)), "target_marker()"]
    )

    projected = focus_source_content(content, "target_marker", 240)

    assert len(projected) <= 240
    assert "header" in projected
    assert "target_marker" in projected
    assert "source lines omitted" in projected


def test_focus_source_content_honors_tiny_budget() -> None:
    assert len(focus_source_content("x" * 1_000, "x", 10)) <= 10


def test_project_evidence_bounds_single_oversized_hit() -> None:
    payload = {
        "file": "src/large.py",
        "start_line": 1,
        "end_line": 1,
        "content": "x" * 1_000_000,
    }

    [result] = project_evidence_payloads([payload], query="needle")

    assert len(result["content"]) <= 2_400
    assert result["content_projection"] == {
        "truncated": True,
        "original_chars": 1_000_000,
        "returned_chars": len(result["content"]),
        "strategy": "query_focused",
    }
    assert len(payload["content"]) == 1_000_000


def test_project_evidence_bounds_oversized_unicode_and_metadata() -> None:
    payload = {
        "node_name": "symbol_" + "n" * 1_000_000,
        "node_id": "node_" + "i" * 1_000_000,
        "type": "type_" + "t" * 1_000_000,
        "file": "src/large.py",
        "content": "\U0001f600" * 1_000_000,
    }

    [result] = project_evidence_payloads([payload], query="needle")

    for field in ("node_name", "node_id", "type"):
        assert len(result[field]) <= DEFAULT_METADATA_FIELD_LIMITS[field]
        assert result["metadata_projection"]["fields"][field]["original_chars"] > (
            1_000_000
        )
    assert result["file"] == "src/large.py"
    assert len(json.dumps(result).encode("utf-8")) < 40_000
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) < 20_000


def test_project_evidence_retains_all_ranked_rows_under_aggregate_budget() -> None:
    payloads = [
        {
            "node_name": f"symbol_{index}",
            "file": f"src/file_{index}.py",
            "start_line": index,
            "end_line": index + 5,
            "content": f"symbol_{index}\n" + "x" * 10_000,
        }
        for index in range(100)
    ]

    results = project_evidence_payloads(payloads, query="symbol_0")

    assert len(results) == 100
    assert [result["node_name"] for result in results] == [
        payload["node_name"] for payload in payloads
    ]
    assert [
        (result["file"], result["start_line"], result["end_line"]) for result in results
    ] == [
        (payload["file"], payload["start_line"], payload["end_line"])
        for payload in payloads
    ]
    assert sum(len(result.get("content", "")) for result in results) <= (
        DEFAULT_TOTAL_CONTENT_CHARS
    )
    assert len(json.dumps(results).encode("utf-8")) < 64_000
    assert any(
        result["content_projection"]["strategy"] == "metadata_only"
        for result in results
    )
    assert all(
        result.get("content_projection", {}).get("original_chars") == 10_009
        or result.get("content_projection", {}).get("original_chars") == 10_010
        for result in results
    )
