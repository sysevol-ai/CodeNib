# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the composed MCP repository-exploration path."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from codenib.compiler.manifest import RepoManifest
from codenib.mcp.tools.explore import explore_context_impl


def _context(tmp_path, *, verified: bool = True):
    def read_source_bytes(relative: str, *, max_bytes: int) -> bytes:
        payload = (tmp_path / relative).read_bytes()
        if len(payload) > max_bytes:
            raise ValueError("test source exceeds its bound")
        return payload

    return SimpleNamespace(
        manifest=RepoManifest(
            repo_path=str(tmp_path),
            commit="abc123",
            source_fingerprint="sha256:checkout",
            languages=["python"],
        ),
        artifact={"repository": "example/project"},
        source_verified=verified,
        source_error=None if verified else "checkout fingerprint mismatch",
        read_source_bytes=read_source_bytes,
        bm25=object(),
        vector=None,
        symbol_graph=object(),
        lsp_provider=None,
        lsp_provider_selection={
            "backend": "persisted-symbol-graph-v1",
            "status": "available",
        },
    )


def _write_source(tmp_path, relative_path: str, marker: str) -> None:
    destination = tmp_path / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# line {line}\n" for line in range(1, 121)]
    lines[20] = f"def {marker}():\n"
    lines[21] = f"    return {marker!r}\n"
    destination.write_text("".join(lines), encoding="utf-8")


def test_explore_composes_routes_dependencies_and_verified_source(
    tmp_path, monkeypatch
) -> None:
    _write_source(tmp_path, "src/service.py", "process_request")
    _write_source(tmp_path, "src/caller.py", "call_service")
    ctx = _context(tmp_path)

    monkeypatch.setattr(
        "codenib.mcp.tools.explore.search_context_impl",
        lambda *_args, **_kwargs: {
            "plan": {"name": "fast_lexical"},
            "results": [
                {
                    "node_name": "process_request",
                    "type": "function",
                    "file": "src/service.py",
                    "start_line": 21,
                    "end_line": 22,
                    "content": "stale indexed source",
                    "score": 0.9,
                }
            ],
        },
    )
    monkeypatch.setattr(
        "codenib.mcp.tools.explore.lsp_route_impl",
        lambda *_args, **_kwargs: [
            {
                "node_name": "process_request",
                "type": "function",
                "file": "src/service.py",
                "start_line": 21,
                "end_line": 22,
                "lsp_provider": {
                    "backend": "clangd-fact-query-v1",
                    "status": "available",
                },
            }
        ],
    )
    monkeypatch.setattr(
        "codenib.mcp.tools.explore.dependency_subgraph_impl",
        lambda *_args, **_kwargs: {
            "root": "src/service.py:process_request()",
            "direction": "callers",
            "nodes": [
                {
                    "name": "src/caller.py:call_service()",
                    "file": "src/caller.py",
                    "line": 21,
                    "kind": "function",
                    "depth": 1,
                }
            ],
            "edges": [
                {
                    "source": "src/caller.py:call_service()",
                    "target": "src/service.py:process_request()",
                    "type": "reference",
                }
            ],
            "truncated": False,
            "note": "",
        },
    )

    result = explore_context_impl(
        ctx,
        query="who calls process_request",
        symbols=["process_request"],
        direction="impact",
    )

    assert result["plan"]["name"] == "composed_explore_v1"
    assert result["plan"]["retrieval"] == {"name": "fast_lexical"}
    assert result["plan"]["route"]["provider"] == {
        "backend": "clangd-fact-query-v1",
        "status": "available",
    }
    assert result["source"] == {
        "repository": "example/project",
        "commit": "abc123",
        "source_fingerprint": "sha256:checkout",
        "verified": True,
    }
    assert result["summary"]["retrieval_hits"] == 1
    assert result["summary"]["route_hits"] == 1
    assert result["summary"]["dependency_graphs"] == 1
    assert {item["file"] for item in result["files"]} == {
        "src/service.py",
        "src/caller.py",
    }

    service = next(item for item in result["files"] if item["file"] == "src/service.py")
    context = service["contexts"][0]
    assert context["verified"] is True
    assert context["content_kind"] == "live_source"
    assert "def process_request():" in context["content"]
    assert "stale indexed source" not in context["content"]
    assert context["content_fingerprint"].startswith("sha256:")
    assert context["anchors"] == [
        {
            "symbol": "process_request",
            "kind": "function",
            "origins": ["retrieval", "route"],
            "start_line": 21,
            "end_line": 22,
            "score": 0.9,
        }
    ]
    assert result["relationships"][0]["seed"] == "process_request"
    assert result["diagnostics"] == []


def test_explore_marks_unverified_indexed_fallback(tmp_path, monkeypatch) -> None:
    ctx = _context(tmp_path, verified=False)
    ctx.symbol_graph = None
    monkeypatch.setattr(
        "codenib.mcp.tools.explore.search_context_impl",
        lambda *_args, **_kwargs: {
            "plan": {"name": "fast_lexical"},
            "results": [
                {
                    "node_name": "cached_value",
                    "type": "function",
                    "file": "src/cache.py",
                    "start_line": 7,
                    "end_line": 9,
                    "content": "def cached_value():\n    return 1\n",
                }
            ],
        },
    )

    result = explore_context_impl(
        ctx,
        query="find cached value",
        include_dependencies=False,
    )

    assert result["source"]["verified"] is False
    assert result["source"]["verification_error"] == "checkout fingerprint mismatch"
    context = result["files"][0]["contexts"][0]
    assert context["verified"] is False
    assert context["content_kind"] == "indexed_excerpt"
    assert "def cached_value" in context["content"]
    assert context["content_fingerprint"].startswith("sha256:")
    assert {item["code"] for item in result["diagnostics"]} == {"route_unavailable"}


def test_explore_reports_unavailable_dependency_expansion(
    tmp_path, monkeypatch
) -> None:
    ctx = _context(tmp_path, verified=False)
    ctx.symbol_graph = None
    monkeypatch.setattr(
        "codenib.mcp.tools.explore.search_context_impl",
        lambda *_args, **_kwargs: {"plan": {"name": "fast_lexical"}, "results": []},
    )

    result = explore_context_impl(ctx, query="find callers")

    assert result["relationships"] == []
    assert {item["code"] for item in result["diagnostics"]} == {
        "dependency_unavailable",
        "route_unavailable",
    }


def test_explore_reports_dependency_backend_error(tmp_path, monkeypatch) -> None:
    ctx = _context(tmp_path, verified=False)
    monkeypatch.setattr(
        "codenib.mcp.tools.explore.search_context_impl",
        lambda *_args, **_kwargs: {
            "plan": {"name": "fast_lexical"},
            "results": [{"file": "src/a.py", "node_name": "target", "line": 1}],
        },
    )
    monkeypatch.setattr(
        "codenib.mcp.tools.explore.lsp_route_impl",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "codenib.mcp.tools.explore.dependency_subgraph_impl",
        lambda *_args, **_kwargs: {"error": "ambiguous dependency seed"},
    )

    result = explore_context_impl(ctx, query="find target")

    assert result["relationships"] == []
    assert result["diagnostics"] == [
        {
            "code": "dependency_unavailable",
            "detail": "ambiguous dependency seed",
        }
    ]


def test_explore_source_read_failure_degrades_to_indexed_excerpt(
    tmp_path, monkeypatch
) -> None:
    ctx = _context(tmp_path)
    monkeypatch.setattr(
        "codenib.mcp.tools.explore.search_context_impl",
        lambda *_args, **_kwargs: {
            "plan": {"name": "fast_lexical"},
            "results": [
                {
                    "file": "src/missing.py",
                    "node_name": "missing_target",
                    "start_line": 7,
                    "end_line": 8,
                    "content": "def missing_target():\n    pass\n",
                }
            ],
        },
    )
    monkeypatch.setattr(
        "codenib.mcp.tools.explore.lsp_route_impl",
        lambda *_args, **_kwargs: [],
    )

    result = explore_context_impl(
        ctx,
        query="find missing target",
        include_dependencies=False,
    )

    context = result["files"][0]["contexts"][0]
    assert context["verified"] is False
    assert context["content_kind"] == "indexed_excerpt"
    assert "def missing_target" in context["content"]
    assert result["diagnostics"][0]["code"] == "source_read_failed"


def test_explore_keeps_route_source_when_ranked_retrieval_fails(
    tmp_path, monkeypatch
) -> None:
    _write_source(tmp_path, "src/service.py", "route_only")
    ctx = _context(tmp_path)
    monkeypatch.setattr(
        "codenib.mcp.tools.explore.search_context_impl",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("bm25 failed")),
    )
    monkeypatch.setattr(
        "codenib.mcp.tools.explore.lsp_route_impl",
        lambda *_args, **_kwargs: [
            {
                "node_name": "route_only",
                "type": "function",
                "file": "src/service.py",
                "start_line": 21,
                "end_line": 22,
            }
        ],
    )

    result = explore_context_impl(
        ctx,
        query="route_only",
        include_dependencies=False,
    )

    assert "def route_only():" in result["files"][0]["contexts"][0]["content"]
    assert result["plan"]["route"]["provider"] == {
        "backend": "persisted-symbol-graph-v1",
        "status": "available",
    }
    assert result["diagnostics"] == [
        {"code": "retrieval_failed", "detail": "bm25 failed"}
    ]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"query": ""}, "query must not be empty"),
        ({"query": "x", "top_k": 21}, "top_k must be between"),
        ({"query": "x", "budget": "unbounded"}, "budget must be"),
        ({"query": "x", "direction": "sideways"}, "direction must be"),
        (
            {"query": "x", "symbols": [f"symbol_{index}" for index in range(33)]},
            "at most 32 entries",
        ),
    ],
)
def test_explore_validates_bounded_request(tmp_path, kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        explore_context_impl(_context(tmp_path), **kwargs)
