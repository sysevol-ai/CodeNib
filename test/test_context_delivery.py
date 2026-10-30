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


def test_optional_budget_reuse_keeps_a_complete_decision_with_short_peers() -> None:
    body = "\n".join(
        [
            "def accept(value):",
            '    """' + "Documentation. " * 12 + '"""',
            "    if value is None:",
            "        return False",
            "    return bool(value)",
        ]
    )
    payloads = [
        {"file": "decision.py", "start_line": 10, "end_line": 14, "content": body},
        {"file": "small.py", "content": "value = 1\n"},
        {"file": "missing.py", "start_line": 30},
    ]
    settings = dict(
        query="accept", total_chars=350, min_excerpt_chars=40, max_excerpt_chars=320
    )
    legacy = project_evidence_payloads(payloads, **settings)
    result = project_evidence_payloads(payloads, reuse_unused_budget=True, **settings)

    assert legacy[0]["content_projection"]["truncated"]
    assert result[0]["content"] == body
    assert "content_projection" not in result[0]
    assert result[1:] == payloads[1:]
    assert [(r["file"], r.get("start_line"), r.get("end_line")) for r in result] == [
        (p["file"], p.get("start_line"), p.get("end_line")) for p in payloads
    ]
    assert sum(len(r.get("content", "")) for r in result) <= 350
    assert payloads[0]["content"] == body


def test_optional_budget_reuse_keeps_admission_order_and_hard_limits() -> None:
    payloads = [
        {"file": f"{i}.py", "content": "target\n" + "x" * 1000} for i in range(5)
    ]
    payloads[0]["content"] = "a = 1\n"
    results = project_evidence_payloads(
        payloads,
        query="target",
        total_chars=220,
        min_excerpt_chars=80,
        max_excerpt_chars=150,
        reuse_unused_budget=True,
    )

    assert [r["file"] for r in results] == [p["file"] for p in payloads]
    assert results[0]["content"] == payloads[0]["content"]
    assert len(results[1]["content"]) == 150
    assert all("content" not in r for r in results[2:])
    assert sum(len(r.get("content", "")) for r in results) <= 220


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
