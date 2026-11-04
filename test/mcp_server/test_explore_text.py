# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from typing import Any

from codenib.mcp.explore_bounds import (
    TEXT_RESPONSE_MEASURE,
    bound_explore_response,
    call_tool_result_bytes,
    explore_call_result,
)
from codenib.mcp.explore_text import render_explore_text


def _response(**overrides: Any) -> dict[str, Any]:
    response: dict[str, Any] = {
        "query": "how are hooks ordered",
        "plan": {"name": "composed_explore_v1", "delivery": {}},
        "source": {
            "repository": "/work/flask",
            "commit": "c12a5d874c5a014495eb2db8a73f40037bc813ac",
            "source_fingerprint": "sha256-v2:abc",
            "verified": True,
        },
        "summary": {"session_call": 2},
        "files": [
            {
                "file": "src/flask/app.py",
                "contexts": [
                    {
                        "start_line": 10,
                        "end_line": 30,
                        "content": (
                            "def preprocess_request(self):\n"
                            "    names = (None,)\n"
                            "... 15 source lines omitted ...\n"
                            "    return None\n"
                            "... 2 source lines omitted ..."
                        ),
                        "content_kind": "live_source",
                        "verified": True,
                        "content_fingerprint": "sha256:x",
                        "source": {"repository": "/work/flask"},
                        "anchors": [
                            {
                                "symbol": "src/flask/app.py:Flask.preprocess_request()",
                                "kind": "method",
                            }
                        ],
                    },
                    {
                        "start_line": 50,
                        "end_line": 60,
                        "content_kind": "session_pointer",
                        "verified": True,
                        "session_dedup": {"content_retained": False, "source_call": 1},
                        "anchors": [],
                    },
                ],
            }
        ],
        "relationships": [
            {
                "seed": "Flask.preprocess_request",
                "root": "src/flask/app.py:Flask.preprocess_request()",
                "nodes": [
                    {
                        "name": "src/flask/wrappers.py:Request.blueprints()",
                        "file": "src/flask/wrappers.py",
                        "line": 97,
                        "kind": "method",
                        "depth": 1,
                    },
                    {
                        "name": "src/flask/sansio/scaffold.py:"
                        "Scaffold.static_url_path(_codenib_l248_c8)",
                        "file": "src/flask/sansio/scaffold.py",
                        "line": 248,
                        "kind": "property",
                        "depth": 1,
                    },
                    {
                        "name": "src/flask/app.py:Flask.full_dispatch_request()",
                        "file": "src/flask/app.py",
                        "line": 867,
                        "kind": "method",
                        "depth": 1,
                    },
                    {
                        "name": "src/flask/app.py:Flask.wsgi_app()",
                        "file": "src/flask/app.py",
                        "line": 1441,
                        "kind": "method",
                        "depth": 2,
                    },
                    {"name": "src/flask/app.py", "kind": "file", "depth": 2},
                ],
                "edges": [
                    {
                        "source": "src/flask/app.py:Flask.preprocess_request()",
                        "target": "src/flask/wrappers.py:Request.blueprints()",
                    },
                    {
                        "source": "src/flask/app.py:Flask.preprocess_request()",
                        "target": "src/flask/sansio/scaffold.py:"
                        "Scaffold.static_url_path(_codenib_l248_c8)",
                    },
                    {
                        "source": "src/flask/app.py:Flask.full_dispatch_request()",
                        "target": "src/flask/app.py:Flask.preprocess_request()",
                    },
                ],
            },
            {
                "seed": "Blueprint.register",
                "root": "Blueprint.register",
                "nodes": [],
                "edges": [],
                "note": "'Blueprint.register' unresolved",
            },
            {
                "seed": "Flask.request_context",
                "root": "src/flask/app.py:Flask.request_context()",
                "nodes": [],
                "edges": [],
                "truncated": True,
                "response_projection": {"original_nodes": 39, "original_edges": 57},
            },
        ],
        "diagnostics": [
            {"code": "graph_preparing", "detail": "The graph is being prepared."},
            {"code": "route_unavailable", "detail": "symbol_graph index not available"},
            {"code": "response_budget_applied", "detail": "see summary.x"},
        ],
    }
    response.update(overrides)
    return response


def test_source_is_line_numbered_across_omitted_lines() -> None:
    text = render_explore_text(_response())

    assert (
        "--- src/flask/app.py:10 (lines 10-30) Flask.preprocess_request()\n"
        "    10\tdef preprocess_request(self):\n"
        "    11\t    names = (None,)\n"
        "      ... 15 lines omitted ...\n"
        "    27\t    return None\n"
        "      ... 2 lines omitted ...\n"
    ) in text
    assert "(same source as returned in call 1; not repeated)" in text
    assert "(explore_context call 2 in this session)" in text
    assert "indexed commit c12a5d874c5a" in text and "safe to cite" in text


def test_relationships_list_direct_neighbors_by_direction() -> None:
    text = render_explore_text(_response())

    assert "Flask.preprocess_request() (src/flask/app.py)\n" in text
    assert (
        "  references: Request.blueprints() (src/flask/wrappers.py:97), "
        "Scaffold.static_url_path (src/flask/sansio/scaffold.py:248)\n"
    ) in text
    assert (
        "  referenced by: Flask.full_dispatch_request() (src/flask/app.py:867)\n"
    ) in text
    assert "  1 more symbols two or more steps away" in text
    assert "_codenib_l248" not in text
    assert "  'Blueprint.register' unresolved" in text
    assert "(neighbors omitted to fit the response: 39 symbols, 57 references)" in text


def test_call_chains_list_entry_first_with_reference_sites() -> None:
    step = {"kind": "function"}
    response = _response(
        call_chains=[
            {
                "seed": "preprocess_request",
                "root": "src/flask/app.py:Flask.preprocess_request()",
                "chains": [
                    {
                        "complete": True,
                        "steps": [
                            {
                                **step,
                                "name": "src/flask/cli.py:run()",
                                "file": "src/flask/cli.py",
                                "line": 3,
                            },
                            {
                                **step,
                                "name": "src/flask/app.py:Flask.full_dispatch_request()",
                                "file": "src/flask/app.py",
                                "line": 867,
                                "referenced_at": {
                                    "file": "src/flask/cli.py",
                                    "line": 9,
                                },
                            },
                            {
                                **step,
                                "name": "src/flask/app.py:Flask.preprocess_request()",
                                "file": "src/flask/app.py",
                                "line": 10,
                                "referenced_at": {
                                    "file": "src/flask/app.py",
                                    "line": 880,
                                },
                            },
                        ],
                    },
                    {
                        "complete": False,
                        "steps": [
                            {
                                **step,
                                "name": "src/flask/app.py:Flask.wsgi_app()",
                                "file": "src/flask/app.py",
                                "line": 900,
                            },
                            {
                                **step,
                                "name": "src/flask/app.py:Flask.preprocess_request()",
                                "file": "src/flask/app.py",
                                "line": 10,
                                "referenced_at": {
                                    "file": "src/flask/app.py",
                                    "line": 905,
                                },
                            },
                        ],
                    },
                ],
                "truncated": True,
            },
            {
                "seed": "unused",
                "root": "src/flask/app.py:unused()",
                "chains": [],
                "note": "no indexed references to unused()",
            },
        ]
    )

    text = render_explore_text(response)

    assert text.startswith(
        "1 files, 2 source windows, 3 relationship groups, 2 call chains.\n"
    )
    assert (
        "How Flask.preprocess_request() is reached:\n"
        "  1. run() (src/flask/cli.py:3)\n"
        "     -> Flask.full_dispatch_request() (src/flask/app.py:867), "
        "referenced at src/flask/cli.py:9\n"
        "     -> Flask.preprocess_request() (src/flask/app.py:10), "
        "referenced at src/flask/app.py:880\n"
        "  2. Flask.wsgi_app() (src/flask/app.py:900)\n"
    ) in text
    assert "no entry reached within the search depth" in text
    assert "(search bounds reached; other chains may exist)" in text
    assert "unused(): no indexed references to unused()" in text
    # Chains come before the source windows they help navigate.
    assert text.index("Call chains (entry first") < text.index(
        "--- src/flask/app.py:10"
    )


def test_dispatch_chains_name_the_method_they_reach() -> None:
    response = _response(
        call_chains=[
            {
                "seed": "opts",
                "root": "h.py:Child.opts()",
                "via": "h.py:Base.opts()",
                "chains": [
                    {
                        "complete": True,
                        "steps": [
                            {"name": "h.py:Base.merge()", "file": "h.py", "line": 1},
                            {
                                "name": "h.py:Base.opts()",
                                "file": "h.py",
                                "line": 11,
                                "referenced_at": {"file": "h.py", "line": 4},
                            },
                        ],
                    }
                ],
            }
        ]
    )

    text = render_explore_text(response)

    assert (
        "How Child.opts() is reached (through the same-named Base.opts(), which "
        "dynamic dispatch may route to it):\n  1. Base.merge() (h.py:1)\n"
    ) in text


def test_notes_explain_without_json_fields_or_redundant_graph_codes() -> None:
    text = render_explore_text(_response())

    assert "Note (graph_preparing): The graph is being prepared." in text
    assert "route_unavailable" not in text
    assert "summary.x" not in text
    assert "Note (response_budget_applied): Some lower-ranked source" in text
    # Provenance internals stay in the structured result.
    assert "sha256" not in text and "/work/flask" not in text


def test_unverified_source_is_marked_before_and_inside_windows() -> None:
    response = _response(source={"verified": False})
    response["files"][0]["contexts"][0]["verified"] = False

    text = render_explore_text(response)

    assert "could not be verified" in text
    assert "(indexed copy; not verified against the current checkout)" in text


def test_manifest_commit_is_not_presented_as_checkout_head() -> None:
    response = _response()
    response["source"].update(commit_verified=False, checkout_commit="d" * 40)
    text = render_explore_text(response)
    assert "current checkout" in text
    assert "indexed commit c12a5d874c5a" in text
    assert "HEAD" not in text


def test_traversal_truncation_is_visible_without_response_projection() -> None:
    response = _response()
    relationship = response["relationships"][0]
    relationship["truncated"] = True
    assert "response_projection" not in relationship
    text = render_explore_text(response)
    assert "references: Request.blueprints()" in text
    assert "relationship result truncated" in text


def test_text_marks_a_line_clipped_by_the_source_reader() -> None:
    response = _response()
    response["files"][0]["contexts"][0].update(
        content="incomplete_call(", source_projection={"line_truncated": True}
    )
    text = render_explore_text(response)
    assert "source line was clipped at the read character limit" in text
    assert "    10\tincomplete_call(" in text


def test_rendered_bound_measures_the_text_it_sends() -> None:
    payload = _response()
    payload["files"][0]["contexts"][0]["content"] = "x = 1\n" * 4_000

    bounded = bound_explore_response(
        payload, max_bytes=16 * 1024, measure=TEXT_RESPONSE_MEASURE
    )
    result = explore_call_result(bounded)

    assert bounded["plan"]["delivery"]["response_measure"] == (TEXT_RESPONSE_MEASURE)
    assert result.content[0].text == render_explore_text(bounded)
    assert result.structured_content is None
    measured = bounded["summary"]["response_projection"]["encoded_bytes"]
    assert measured == call_tool_result_bytes(result) <= 16 * 1024
    # Rebounding keeps the measure the response was bounded with.
    assert bound_explore_response(bounded, max_bytes=16 * 1024) == bounded


def test_default_bound_keeps_the_json_text_copy() -> None:
    bounded = bound_explore_response(_response())

    text = explore_call_result(bounded).content[0].text
    assert json.loads(text) == bounded
