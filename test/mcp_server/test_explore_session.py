# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for fingerprint-safe repeated explore-context delivery."""

from __future__ import annotations

from copy import deepcopy

from codenib.mcp.explore_session import ExploreSessionLedger, ExploreSessionRuntime


def _context(
    file_path: str,
    content: str,
    fingerprint: str,
    *,
    verified: bool = True,
    start_line: int = 1,
    end_line: int = 10,
) -> dict:
    return {
        "file": file_path,
        "start_line": start_line,
        "end_line": end_line,
        "content": content,
        "content_fingerprint": fingerprint,
        "content_kind": "live_source" if verified else "indexed_excerpt",
        "verified": verified,
        "anchors": [],
    }


def _response(*contexts: dict) -> dict:
    files = []
    for context in contexts:
        file_path = context.pop("file")
        files.append({"file": file_path, "contexts": [context]})
    return {
        "plan": {"delivery": {}},
        "summary": {
            "returned_content_chars": sum(
                len(item["contexts"][0].get("content", "")) for item in files
            )
        },
        "files": files,
    }


def _contexts(response: dict) -> list[dict]:
    return [context for group in response["files"] for context in group["contexts"]]


def test_repeated_call_keeps_one_body_and_uses_explicit_pointers() -> None:
    ledger = ExploreSessionLedger()
    response = _response(
        _context("src/a.py", "alpha source", "sha256:alpha"),
        _context("src/b.py", "beta source", "sha256:beta"),
    )
    original = deepcopy(response)

    first = ledger.project(response)
    second = ledger.project(response)

    assert response == original
    assert all("content" in context for context in _contexts(first))
    second_contexts = _contexts(second)
    assert sum("content" in context for context in second_contexts) == 1
    pointer = next(context for context in second_contexts if "content" not in context)
    assert pointer["content_kind"] == "session_pointer"
    assert "call 1" in pointer["session_pointer"]
    assert pointer["session_dedup"] == {
        "repeated": True,
        "content_retained": False,
        "source_call": 1,
        "first_seen_call": 1,
        "previous_call": 1,
        "current_call": 2,
    }
    assert second["summary"]["session_call"] == 2
    assert second["summary"]["session_deduplicated_contexts"] == 1
    assert second["summary"]["session_ranges"] == 2
    assert second["summary"]["session_max_ranges"] == 160
    assert second["summary"]["session_remaining_ranges"] == 158
    assert second["summary"]["session_evicted_ranges"] == 0
    assert second["summary"]["session_total_evictions"] == 0
    assert second["plan"]["delivery"]["session_dedup"] is True
    assert second["plan"]["delivery"]["session_scope"] == "stdio_connection"


def test_pointer_always_targets_the_call_that_delivered_source() -> None:
    ledger = ExploreSessionLedger()
    response = _response(
        _context("src/a.py", "alpha source", "sha256:alpha"),
        _context("src/b.py", "beta source", "sha256:beta"),
    )

    ledger.project(response)
    ledger.project(response)
    third = ledger.project(response)

    pointer = next(context for context in _contexts(third) if "content" not in context)
    assert pointer["session_dedup"]["source_call"] == 1
    assert pointer["session_dedup"]["first_seen_call"] == 1
    assert pointer["session_dedup"]["previous_call"] == 2
    assert "call 1" in pointer["session_pointer"]


def test_novel_context_allows_every_repeated_context_to_be_omitted() -> None:
    ledger = ExploreSessionLedger()
    ledger.project(_response(_context("src/a.py", "alpha", "sha256:alpha")))

    projected = ledger.project(
        _response(
            _context("src/a.py", "alpha", "sha256:alpha"),
            _context("src/b.py", "beta", "sha256:beta"),
        )
    )

    contexts = _contexts(projected)
    assert "content" not in contexts[0]
    assert contexts[1]["content"] == "beta"
    assert projected["summary"]["returned_content_chars"] == len("beta")


def test_changed_content_or_projection_is_never_deduplicated() -> None:
    ledger = ExploreSessionLedger()
    ledger.project(_response(_context("src/a.py", "first", "sha256:full-v1")))

    changed = ledger.project(
        _response(_context("src/a.py", "second", "sha256:full-v2"))
    )
    changed_context = _contexts(changed)[0]
    assert changed_context["content"] == "second"
    assert changed_context["session_dedup"] == {
        "repeated": False,
        "content_changed": True,
        "source_call": 2,
        "first_seen_call": 2,
        "previous_call": 1,
        "current_call": 2,
    }

    changed_repeat = ledger.project(
        _response(_context("src/a.py", "second", "sha256:full-v2"))
    )
    assert _contexts(changed_repeat)[0]["session_dedup"]["source_call"] == 2
    assert _contexts(changed_repeat)[0]["session_dedup"]["first_seen_call"] == 2

    projection = ledger.project(
        _response(_context("src/a.py", "different projection", "sha256:full-v2"))
    )
    projection_context = _contexts(projection)[0]
    assert projection_context["content"] == "different projection"
    assert projection_context["session_dedup"] == {
        "repeated": False,
        "projection_changed": True,
        "source_call": 4,
        "first_seen_call": 4,
        "previous_call": 3,
        "current_call": 4,
    }

    projection_repeat = ledger.project(
        _response(_context("src/a.py", "different projection", "sha256:full-v2"))
    )
    assert _contexts(projection_repeat)[0]["session_dedup"]["source_call"] == 4


def test_unverified_indexed_excerpts_are_not_session_deduplicated() -> None:
    ledger = ExploreSessionLedger()
    response = _response(
        _context(
            "src/a.py",
            "indexed source",
            "sha256:indexed",
            verified=False,
        )
    )

    ledger.project(response)
    repeated = ledger.project(response)

    assert _contexts(repeated)[0]["content"] == "indexed source"
    assert repeated["summary"]["session_deduplicated_contexts"] == 0


def test_body_removed_by_response_budget_is_not_recorded_as_delivered() -> None:
    ledger = ExploreSessionLedger()
    first = ledger.project(
        _response(
            _context("src/a.py", "a" * 5_000, "sha256:alpha"),
            _context("src/b.py", "b" * 5_000, "sha256:beta"),
        ),
        max_response_bytes=16_000,
    )

    assert "content" in _contexts(first)[0]
    assert "content" not in _contexts(first)[1]
    assert first["summary"]["session_ranges"] == 1

    later = ledger.project(
        _response(_context("src/b.py", "b" * 5_000, "sha256:beta")),
        max_response_bytes=24_000,
    )

    context = _contexts(later)[0]
    assert context["content"] == "b" * 5_000
    assert context["content_kind"] == "live_source"
    assert "session_pointer" not in context
    assert later["summary"]["session_ranges"] == 2


def test_deterministic_projection_repeat_keeps_original_source_call() -> None:
    ledger = ExploreSessionLedger()
    response = _response(_context("src/a.py", "x" * 5_000, "sha256:full-source"))

    first = ledger.project(response, max_response_bytes=6_000)
    second = ledger.project(response, max_response_bytes=6_000)

    first_context = _contexts(first)[0]
    second_context = _contexts(second)[0]
    assert first_context["content"] == second_context["content"]
    assert second_context["session_dedup"] == {
        "repeated": True,
        "content_retained": True,
        "source_call": 1,
        "first_seen_call": 1,
        "previous_call": 1,
        "current_call": 2,
    }
    assert ledger.stats()["ranges"] == 1


def test_projection_that_removes_retained_repeat_clears_false_annotation() -> None:
    ledger = ExploreSessionLedger()
    response = _response(_context("src/a.py", "x" * 5_000, "sha256:full-source"))
    ledger.project(response, max_response_bytes=20_000)

    omitted = ledger.project(response, max_response_bytes=4_400)

    context = _contexts(omitted)[0]
    assert "content" not in context
    assert context["content_kind"] == "response_budget_location"
    assert "session_dedup" not in context
    assert omitted["summary"]["session_ranges"] == 1

    delivered = ledger.project(response, max_response_bytes=20_000)
    assert _contexts(delivered)[0]["session_dedup"]["source_call"] == 1


def test_projection_that_removes_changed_body_does_not_claim_delivery() -> None:
    ledger = ExploreSessionLedger()
    ledger.project(
        _response(_context("src/a.py", "a" * 5_000, "sha256:version-one")),
        max_response_bytes=20_000,
    )
    changed_response = _response(
        _context("src/a.py", "b" * 5_000, "sha256:version-two")
    )

    omitted = ledger.project(changed_response, max_response_bytes=4_400)

    context = _contexts(omitted)[0]
    assert "content" not in context
    assert "session_dedup" not in context
    assert omitted["summary"]["session_ranges"] == 1

    delivered = ledger.project(changed_response, max_response_bytes=20_000)
    annotation = _contexts(delivered)[0]["session_dedup"]
    assert annotation["content_changed"] is True
    assert annotation["source_call"] == 3
    assert annotation["first_seen_call"] == 3


def test_minimal_budget_envelope_still_reports_session_usage() -> None:
    ledger = ExploreSessionLedger()
    payload = _response(_context("src/a.py", "alpha", "sha256:alpha"))
    payload["irreducible_extension"] = list(range(20_000))

    projected = ledger.project(payload, max_response_bytes=4_000)

    assert projected["files"] == []
    assert projected["summary"]["session_call"] == 1
    assert projected["summary"]["session_ranges"] == 0
    assert projected["summary"]["session_max_ranges"] == 160
    assert projected["summary"]["session_evicted_ranges"] == 0
    assert projected["summary"]["response_projection"]["minimal"] is True
    assert projected["plan"]["delivery"]["session_scope"] == "stdio_connection"


def test_ledger_bounds_range_history_and_reset() -> None:
    ledger = ExploreSessionLedger(max_ranges=2)
    for index in range(3):
        ledger.project(
            _response(
                _context(
                    f"src/{index}.py",
                    f"source {index}",
                    f"sha256:{index}",
                )
            )
        )

    assert ledger.stats() == {
        "calls": 3,
        "ranges": 2,
        "max_ranges": 2,
        "evictions": 1,
    }
    projected = ledger.project(_response(_context("src/3.py", "source 3", "sha256:3")))
    assert projected["summary"]["session_evicted_ranges"] == 1
    assert projected["summary"]["session_total_evictions"] == 2
    ledger.reset()
    assert ledger.stats() == {
        "calls": 0,
        "ranges": 0,
        "max_ranges": 2,
        "evictions": 0,
    }


def test_runtime_close_discards_connection_history() -> None:
    runtime = ExploreSessionRuntime()
    runtime.ledger.project(_response(_context("src/a.py", "alpha", "sha256:alpha")))

    runtime.close()

    assert runtime.ledger.stats() == {
        "calls": 0,
        "ranges": 0,
        "max_ranges": 160,
        "evictions": 0,
    }
