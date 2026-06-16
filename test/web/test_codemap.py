# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the web codemap payload builder."""

from codeminer.graph.code_graph import CodeGraph
from codeminer.web.codemap import build_codemap, build_page_subgraph


def _symbol_graph_with_many_call_sites() -> CodeGraph:
    graph = CodeGraph()
    graph._add_vertex(
        "src/main.py:caller()",
        {
            "type": "function",
            "file": "src/main.py",
            "start_line": 0,
            "end_line": 4,
            "unified_name": "src/main.py:caller()",
        },
    )
    graph._add_vertex(
        "src/helper.py:callee()",
        {
            "type": "function",
            "file": "src/helper.py",
            "start_line": 10,
            "end_line": 14,
            "unified_name": "src/helper.py:callee()",
        },
    )
    for line in range(12):
        graph._add_edge(
            "src/main.py:caller()",
            "src/helper.py:callee()",
            "reference",
            anchor_file="src/main.py",
            anchor_line=line,
        )
    return graph


def test_codemap_preserves_all_call_site_anchors():
    graph = _symbol_graph_with_many_call_sites()

    result = build_codemap(
        graph, symbol="caller", direction="callees", depth=1, max_nodes=5
    )

    assert result["available"] is True
    assert len(result["edges"]) == 1
    edge = result["edges"][0]
    assert edge["weight"] == 12
    assert len(edge["anchors"]) == 12
    assert {anchor["file"] for anchor in edge["anchors"]} == {"src/main.py"}
    assert sorted(anchor["line"] for anchor in edge["anchors"]) == list(range(1, 13))
    assert edge["bundle_path"][0] == f"hier::symbol::{edge['source']}"
    assert edge["bundle_path"][-1] == f"hier::symbol::{edge['target']}"
    assert edge["bundle_lca"] == "hier::root"
    assert edge["cross_file"] is True


def test_codemap_returns_explicit_containment_hierarchy():
    graph = CodeGraph()
    graph._add_vertex(
        "src/main.py:caller()",
        {
            "type": "function",
            "file": "src/main.py",
            "start_line": 0,
            "end_line": 4,
            "unified_name": "src/main.py:caller()",
        },
    )
    graph._add_vertex(
        "src/pkg/a.py:alpha()",
        {
            "type": "function",
            "file": "src/pkg/a.py",
            "start_line": 10,
            "end_line": 14,
            "unified_name": "src/pkg/a.py:alpha()",
        },
    )
    graph._add_vertex(
        "src/pkg/b.py:beta()",
        {
            "type": "function",
            "file": "src/pkg/b.py",
            "start_line": 20,
            "end_line": 24,
            "unified_name": "src/pkg/b.py:beta()",
        },
    )
    graph._add_edge(
        "src/main.py:caller()",
        "src/pkg/a.py:alpha()",
        "reference",
        anchor_file="src/main.py",
        anchor_line=1,
    )
    graph._add_edge(
        "src/main.py:caller()",
        "src/pkg/b.py:beta()",
        "reference",
        anchor_file="src/main.py",
        anchor_line=2,
    )

    result = build_codemap(
        graph, symbol="caller", direction="callees", depth=1, max_nodes=5
    )

    assert "hierarchy" in result, result
    hierarchy = result["hierarchy"]
    assert hierarchy["root"] == "hier::root"
    assert hierarchy["source_root"] == "src"
    assert hierarchy["open_files"][0] == "src/main.py"

    nodes_by_id = {node["id"]: node for node in hierarchy["nodes"]}
    assert nodes_by_id["hier::root"]["kind"] == "root"
    assert nodes_by_id["hier::dir::pkg"]["parent"] == "hier::root"
    assert nodes_by_id["hier::dir::pkg"]["symbol_count"] == 2

    file_a = nodes_by_id["hier::file::src/pkg/a.py"]
    assert file_a["parent"] == "hier::dir::pkg"
    assert file_a["kind"] == "file"
    assert file_a["symbol_count"] == 1

    symbol_nodes = [node for node in hierarchy["nodes"] if node["kind"] == "symbol"]
    assert {node["parent"] for node in symbol_nodes} >= {
        "hier::file::src/main.py",
        "hier::file::src/pkg/a.py",
        "hier::file::src/pkg/b.py",
    }
    assert all(isinstance(node["doi"], float) for node in symbol_nodes)

    for edge in result["edges"]:
        assert edge["source_hierarchy"] == f"hier::symbol::{edge['source']}"
        assert edge["target_hierarchy"] == f"hier::symbol::{edge['target']}"
        assert edge["bundle_path"][0] == edge["source_hierarchy"]
        assert edge["bundle_path"][-1] == edge["target_hierarchy"]
        assert edge["bundle_lca"] in edge["bundle_path"]
        assert edge["bundle_lca_kind"] == "root"
        assert edge["cross_file"] is True


def test_page_subgraph_keeps_only_cited_symbols_and_multi_seed_bridges():
    graph = CodeGraph()
    for name, file, line in [
        ("src/a.py:seed_a()", "src/a.py", 0),
        ("src/b.py:seed_b()", "src/b.py", 10),
        ("src/bridge.py:bridge()", "src/bridge.py", 20),
        ("src/noise.py:noise()", "src/noise.py", 30),
    ]:
        graph._add_vertex(
            name,
            {
                "type": "function",
                "file": file,
                "start_line": line,
                "end_line": line + 2,
                "unified_name": name,
            },
        )
    graph._add_edge(
        "src/a.py:seed_a()",
        "src/bridge.py:bridge()",
        "reference",
        anchor_file="src/a.py",
        anchor_line=1,
    )
    graph._add_edge(
        "src/bridge.py:bridge()",
        "src/b.py:seed_b()",
        "reference",
        anchor_file="src/bridge.py",
        anchor_line=20,
    )
    graph._add_edge(
        "src/a.py:seed_a()",
        "src/noise.py:noise()",
        "reference",
        anchor_file="src/a.py",
        anchor_line=2,
    )

    result = build_page_subgraph(
        graph,
        [
            {"file": "src/a.py", "start_line": 1, "node_name": "seed_a"},
            {"file": "src/b.py", "start_line": 11, "node_name": "seed_b"},
        ],
        max_nodes=10,
    )

    assert result["available"] is True
    labels = {node["label"]: node for node in result["nodes"]}
    assert set(labels) == {
        "src/a.py:seed_a()",
        "src/b.py:seed_b()",
        "src/bridge.py:bridge()",
    }
    assert labels["src/a.py:seed_a()"]["is_root"] is True
    assert labels["src/b.py:seed_b()"]["is_root"] is True
    assert labels["src/bridge.py:bridge()"]["is_root"] is False
    assert len(result["edges"]) == 2
    assert {edge["weight"] for edge in result["edges"]} == {1}
