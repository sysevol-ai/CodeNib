# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the composed MCP repository-exploration path."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from codenib.compiler.manifest import RepoManifest
from codenib.graph.code_graph import CodeGraph
from codenib.mcp.tools.explore import explore_context_impl
from codenib.types import NODE_TYPE_FUNCTION


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("symbol", ["processRequest", "process_request", "decode"])
def test_explore_preserves_named_query_symbol_before_dense_window_limit(
    tmp_path, monkeypatch, explicit, symbol
):
    """A named definition must survive an otherwise full semantic result page."""
    _write_source(tmp_path, "src/target.py", symbol)
    graph = CodeGraph(str(tmp_path))
    graph.add_file_node("src/target.py")
    graph.add_symbol_node(
        symbol,
        line=20,
        scope_start_line=20,
        scope_end_line=21,
        symbol_type=NODE_TYPE_FUNCTION,
    )
    graph.build_range_indexes()
    ctx = _context(tmp_path)
    ctx.bm25 = None
    ctx.vector = object()
    ctx.symbol_graph = graph
    dense = []
    for i in range(4):
        path = f"src/semantic{i}.py"
        _write_source(tmp_path, path, f"other_{i}")
        dense.append(
            {
                "node_name": f"other_{i}",
                "type": NODE_TYPE_FUNCTION,
                "file": path,
                "start_line": 21,
                "end_line": 22,
            }
        )
    monkeypatch.setattr(
        "codenib.mcp.tools.explore.search_context_impl",
        lambda *_a, **_kw: {"plan": {"name": "semantic"}, "results": dense},
    )
    mention = f"`{symbol}`" if symbol == "decode" else symbol
    result = explore_context_impl(
        ctx,
        query=f"refresh {mention} configuration",
        symbols=[symbol] if explicit else [],
        top_k=4,
        budget="fast",
        include_dependencies=False,
    )
    assert result["files"][0]["file"] == "src/target.py"
    assert result["plan"]["route"]["inferred_symbols"] == ([] if explicit else [symbol])
    assert result["summary"]["source_windows"] == 4
    assert f"def {symbol}" in result["files"][0]["contexts"][0]["content"]


def test_explore_does_not_promote_fuzzy_query_identifier_matches(tmp_path, monkeypatch):
    ctx = _context(tmp_path)
    ctx.symbol_graph = SimpleNamespace(
        resolve_symbol_candidates=lambda *_a: ["different_symbol"],
        get_node_info_by_name=lambda _n: {"type": NODE_TYPE_FUNCTION},
    )
    monkeypatch.setattr(
        "codenib.mcp.tools.explore.search_context_impl",
        lambda *_a, **_kw: {"results": []},
    )
    calls = []
    monkeypatch.setattr(
        "codenib.mcp.tools.explore.lsp_route_impl",
        lambda *_a, **kw: calls.append(kw) or [],
    )
    result = explore_context_impl(
        ctx, query="refresh unknownSymbol behavior", include_dependencies=False
    )
    assert calls[0]["symbols"] == []
    assert result["plan"]["route"]["inferred_symbols"] == []
    assert result["plan"]["route"]["query_fallback"] is True


def test_explore_bounds_inferred_symbol_lookups(tmp_path, monkeypatch):
    from codenib.mcp.tools._validation import MAX_ROUTE_SYMBOLS

    lookups = []
    ctx = _context(tmp_path)
    ctx.symbol_graph = SimpleNamespace(
        resolve_symbol_candidates=lambda seed, _limit: lookups.append(seed) or [],
        get_node_info_by_name=lambda _n: None,
    )
    monkeypatch.setattr(
        "codenib.mcp.tools.explore.search_context_impl",
        lambda *_a, **_kw: {"results": []},
    )
    monkeypatch.setattr(
        "codenib.mcp.tools.explore.lsp_route_impl", lambda *_a, **_kw: []
    )
    explore_context_impl(
        ctx,
        query=" ".join(f"unknownSymbol{i}" for i in range(200)),
        include_dependencies=False,
    )
    assert len(lookups) == MAX_ROUTE_SYMBOLS


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


def test_inferred_calls_use_concrete_definitions_without_neighbor_expansion(
    tmp_path, monkeypatch
):
    from codenib.mcp.tools.explore import _query_symbol_seeds

    definitions = {
        "first.run": {"type": NODE_TYPE_FUNCTION, "has_definition": True},
        "second.run": {"type": NODE_TYPE_FUNCTION, "has_definition": True},
        "external.run": {"type": NODE_TYPE_FUNCTION, "has_definition": False},
    }
    ctx = _context(tmp_path)
    ctx.symbol_graph = SimpleNamespace(
        resolve_symbol_candidates=lambda *_args: list(definitions),
        get_node_info_by_name=lambda name: definitions[name],
    )
    assert _query_symbol_seeds(ctx, "fix `run()` behavior") == [
        "first.run",
        "second.run",
    ]
    calls = []
    monkeypatch.setattr(
        "codenib.mcp.tools.explore.search_context_impl",
        lambda *_args, **_kwargs: {"results": []},
    )
    monkeypatch.setattr(
        "codenib.mcp.tools.explore.lsp_route_impl",
        lambda *_args, **kwargs: calls.append(kwargs) or [],
    )
    dependencies = []
    monkeypatch.setattr(
        "codenib.mcp.tools.explore.dependency_subgraph_impl",
        lambda _ctx, seed, **_kwargs: dependencies.append(seed) or {"nodes": []},
    )
    result = explore_context_impl(ctx, query="fix `run()` behavior", budget="fast")
    assert calls[0]["symbols"] == ["first.run", "second.run"]
    assert calls[0]["include_neighbors"] is False
    assert dependencies == ["first.run"]
    assert result["plan"]["route"]["inferred_symbols"] == ["first.run", "second.run"]


def test_reference_only_inference_keeps_generic_query_fallback(tmp_path, monkeypatch):
    ctx = _context(tmp_path)
    ctx.symbol_graph = SimpleNamespace(
        resolve_symbol_candidates=lambda *_args: ["external.run"],
        get_node_info_by_name=lambda _name: {
            "type": NODE_TYPE_FUNCTION,
            "has_definition": False,
        },
    )
    monkeypatch.setattr(
        "codenib.mcp.tools.explore.search_context_impl",
        lambda *_args, **_kwargs: {"results": []},
    )
    calls = []
    monkeypatch.setattr(
        "codenib.mcp.tools.explore.lsp_route_impl",
        lambda *_args, **kwargs: calls.append(kwargs) or [],
    )
    result = explore_context_impl(
        ctx, query="fix `run()` behavior", include_dependencies=False
    )
    assert calls[0]["symbols"] == []
    assert calls[0]["include_neighbors"] is True
    assert result["plan"]["route"]["query_fallback"] is True


@pytest.mark.parametrize("verified", [True, False])
def test_only_verified_requested_definition_precedes_ranked_hits(
    tmp_path, monkeypatch, verified
):
    ctx = _context(tmp_path, verified=verified)
    graph = CodeGraph(str(tmp_path))
    graph.add_file_node("src/target.py")
    graph.add_symbol_node(
        "target",
        line=20,
        scope_start_line=20,
        scope_end_line=21,
        symbol_type=NODE_TYPE_FUNCTION,
    )
    graph.build_range_indexes()
    ctx.symbol_graph = graph
    _write_source(tmp_path, "src/target.py", "target")
    _write_source(tmp_path, "src/ranked.py", "ranked")
    ranked = {
        "node_name": "ranked",
        "type": NODE_TYPE_FUNCTION,
        "file": "src/ranked.py",
        "start_line": 21,
        "end_line": 22,
        "content": "RANKED_SOURCE_BODY",
    }
    routed = [
        {
            "node_name": "target",
            "type": NODE_TYPE_FUNCTION,
            "file": "src/target.py",
            "start_line": 21,
            "end_line": 22,
        }
    ]
    for index in range(8):
        path = f"src/neighbor{index}.py"
        _write_source(tmp_path, path, f"neighbor{index}")
        routed.append(
            {
                "node_name": f"neighbor{index}",
                "type": NODE_TYPE_FUNCTION,
                "file": path,
                "start_line": 21,
                "end_line": 22,
            }
        )
    monkeypatch.setattr(
        "codenib.mcp.tools.explore.search_context_impl",
        lambda *_args, **_kwargs: {"results": [ranked]},
    )
    monkeypatch.setattr(
        "codenib.mcp.tools.explore.lsp_route_impl", lambda *_args, **_kwargs: routed
    )
    result = explore_context_impl(
        ctx,
        query="fix target",
        symbols=["target"],
        budget="fast",
        include_dependencies=False,
    )
    files = [item["file"] for item in result["files"]]
    assert "src/ranked.py" in files
    assert files[0] == ("src/target.py" if verified else "src/ranked.py")
    if not verified:
        assert "RANKED_SOURCE_BODY" in result["files"][0]["contexts"][0]["content"]


def test_unquoted_calls_are_inferred_and_aggregate_aliases_stay_in_bounds():
    from codenib.mcp.tools._validation import MAX_LSP_ROUTE_TEXT_CHARS
    from codenib.mcp.tools.explore import _query_symbol_seeds
    from codenib.mcp.tools.lsp import normalize_lsp_route_symbols

    names = {f"sym_{i}": f"{i}_" + "x" * 510 for i in range(32)}
    info = {
        name: {"type": NODE_TYPE_FUNCTION, "has_definition": True, "unified_name": seed}
        for seed, name in names.items()
    }
    ctx = SimpleNamespace(
        symbol_graph=SimpleNamespace(
            resolve_symbol_candidates=lambda seed, _limit: [names[seed]],
            get_node_info_by_name=info.get,
        )
    )
    inferred = _query_symbol_seeds(ctx, " ".join(names))
    assert len(inferred) == 31
    assert sum(map(len, inferred)) <= MAX_LSP_ROUTE_TEXT_CHARS
    assert normalize_lsp_route_symbols(inferred) == inferred
    ctx.symbol_graph = SimpleNamespace(
        resolve_symbol_candidates=lambda *_args: ["decode"],
        get_node_info_by_name=lambda _name: {
            "type": NODE_TYPE_FUNCTION,
            "has_definition": True,
        },
    )
    assert _query_symbol_seeds(ctx, "fix decode() behavior") == ["decode"]


def test_explicit_display_alias_prioritizes_merged_canonical_retrieval(
    tmp_path, monkeypatch
):
    ctx = _context(tmp_path)
    canonical = "canonical_target"
    alias = "src/target.py:target()"
    ctx.symbol_graph = SimpleNamespace(
        resolve_symbol_candidates=lambda *_args: [canonical],
        get_node_info_by_name=lambda _name: {
            "type": NODE_TYPE_FUNCTION,
            "has_definition": True,
            "unified_name": alias,
        },
    )
    rows = []
    for i in range(5):
        path = f"src/ranked{i}.py"
        _write_source(tmp_path, path, f"ranked{i}")
        rows.append(
            {
                "node_name": f"ranked{i}",
                "type": NODE_TYPE_FUNCTION,
                "file": path,
                "start_line": 21,
                "end_line": 22,
            }
        )
    _write_source(tmp_path, "src/target.py", "target")
    target = {
        "node_name": canonical,
        "type": NODE_TYPE_FUNCTION,
        "file": "src/target.py",
        "start_line": 21,
        "end_line": 22,
    }
    rows.append(target)
    monkeypatch.setattr(
        "codenib.mcp.tools.explore.search_context_impl",
        lambda *_args, **_kwargs: {"results": rows},
    )
    monkeypatch.setattr(
        "codenib.mcp.tools.explore.lsp_route_impl",
        lambda *_args, **_kwargs: [{**target, "node_name": alias}],
    )
    result = explore_context_impl(
        ctx,
        query="fix target",
        symbols=[alias],
        budget="fast",
        include_dependencies=False,
    )
    assert result["files"][0]["file"] == "src/target.py"
