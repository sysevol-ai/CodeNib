# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""CodeNib symbol and reference lookups for ARB's indexed-interactive mode."""

import json

import pytest

from codenib.eval.benchmarks import _arb_methods as methods
from codenib.eval.benchmarks import agent_retrieval_bench_tools as tools

REPO, COMMIT = "owner/repo", "a" * 40
RELEASED = ["pkg/config.py", "pkg/app.py", "pkg/util.py", "pkg/other.py"]


def _graph():
    from codenib.graph.code_graph import CodeGraph

    graph = CodeGraph("/repo")
    graph.add_root_node("/repo")
    for path, symbols in [
        ("pkg/config.py", [("pkg/config.py:Config", 0, 9, "class")]),
        ("pkg/app.py", [("pkg/app.py:main", 2, 6, "function")]),
        ("pkg/util.py", [("pkg/util.py:helper", 0, 3, "function")]),
        ("pkg/other.py", [("pkg/other.py:Parse", 4, 8, "function")]),
        ("vendor/lib.py", [("vendor/lib.py:lib", 0, 2, "function")]),
    ]:
        graph.add_file_node(path)
        for symbol, start, end, kind in symbols:
            graph.add_symbol_node(symbol, start, start, end, kind)
            graph.add_containment_edge(symbol)
    # Config contains parse, defined on lines 2-5 (0-based).
    graph.current_file, graph.current_scope = "pkg/config.py", "pkg/config.py:Config"
    graph.add_symbol_node("pkg/config.py:Config.parse", 2, 2, 5, "method")
    graph.add_containment_edge("pkg/config.py:Config.parse")
    graph.current_scope = "pkg/config.py:Config.parse"
    graph.add_symbol_reference("pkg/util.py:helper")
    graph.current_file, graph.current_scope = "pkg/app.py", "pkg/app.py:main"
    graph.add_symbol_reference("pkg/config.py:Config.parse")
    graph.add_symbol_reference("pkg/config.py:Config")
    graph.current_file, graph.current_scope = "vendor/lib.py", "vendor/lib.py:lib"
    graph.add_symbol_reference("pkg/config.py:Config")
    return graph


@pytest.fixture
def builds(monkeypatch, tmp_path):
    """Count graph builds; the checkout is never fetched for real."""
    calls = []
    saved = tmp_path / "fixture-graph.pkl"
    _graph().save_graph(str(saved))

    def build(checkout, output, languages):
        calls.append(languages)
        (output / "arb_graph.pkl").write_bytes(saved.read_bytes())
        return {
            "available_languages": languages,
            "graph": str(output / "arb_graph.pkl"),
        }

    monkeypatch.setattr(methods, "fetch_snapshot", lambda *args: tmp_path)
    monkeypatch.setattr(methods, "build_graph", build)
    return calls


def _provider(tmp_path):
    provider = tools.CodeNibToolProvider(str(tmp_path / "ws"), str(tmp_path / "cache"))
    status = provider.prepare(repo=REPO, base_commit=COMMIT, paths=RELEASED)
    return provider, status


def _call(provider, tool, **arguments):
    return provider.call(
        tool=tool, arguments=arguments, repo=REPO, base_commit=COMMIT, limit=10
    )


def test_prepare_builds_once_and_reports_the_original_cost(builds, tmp_path):
    _, status = _provider(tmp_path)
    assert status["available"] and not status["cached"]
    assert status["languages"] == ["python"]
    assert status["size"]["released_symbols"] == 5
    assert status["size"]["released_source_files"] == 4
    assert status["size"]["covered_source_files"] == 4
    _, again = _provider(tmp_path)
    assert again["cached"] and again["build_seconds"] == status["build_seconds"]
    assert builds == [["python"]]


def test_symbol_search_ranks_exact_names_and_reports_one_based_lines(builds, tmp_path):
    provider, _ = _provider(tmp_path)
    hits = _call(provider, "symbol_search", symbol="parse")
    assert [hit["path"] for hit in hits] == ["pkg/config.py", "pkg/other.py"]
    assert hits[0] == {
        "path": "pkg/config.py",
        "symbol": "Config.parse",
        "kind": "method",
        "start_line": 3,
        "end_line": 6,
    }
    assert [
        hit["symbol"]
        for hit in _call(provider, "symbol_search", symbol="Config.parse()")
    ] == ["Config.parse"]
    # Unreleased files never answer a lookup.
    assert _call(provider, "symbol_search", symbol="lib") == []


def test_references_follow_contained_symbols_to_other_released_files(builds, tmp_path):
    provider, _ = _provider(tmp_path)
    hits = _call(provider, "references", path="pkg/config.py", symbol="Config")
    assert [(hit["path"], hit["relation"]) for hit in hits] == [
        ("pkg/app.py", "uses symbol, 2 refs"),
        ("pkg/util.py", "used by symbol, 1 refs"),
    ]
    assert hits[0]["start_line"] == 3 and hits[0]["end_line"] == 7
    assert _call(provider, "references", path="pkg/app.py", symbol="Config") == []


def test_snapshot_without_a_graph_is_unavailable(builds, monkeypatch, tmp_path):
    provider = tools.CodeNibToolProvider(str(tmp_path / "ws"), str(tmp_path / "cache"))
    status = provider.prepare(repo=REPO, base_commit=COMMIT, paths=["README.md"])
    assert not status["available"] and status["reason"] == "no graph language"
    assert builds == []

    def fail(*args):
        raise RuntimeError("indexer crashed")

    monkeypatch.setattr(methods, "build_graph", fail)
    status = provider.prepare(repo=REPO, base_commit="b" * 40, paths=RELEASED)
    assert not status["available"] and "indexer crashed" in status["reason"]
    with pytest.raises(RuntimeError):
        _call(provider, "symbol_search", symbol="parse")
    receipt = tmp_path / "cache" / "owner__repo" / ("b" * 40) / "provider_receipt.json"
    assert json.loads(receipt.read_text())["graph"] is False


def test_describe_binds_the_provider_build(tmp_path):
    described = tools.CodeNibToolProvider(str(tmp_path)).describe()
    assert described["name"] == "codenib"
    assert described["source_access"] == "full-source"
    assert len(described["codenib_sources_sha256"]) == 64
    assert described["scip_python_index_timeout_seconds"] > 0
