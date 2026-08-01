# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the module-level dependency map."""

from codenib.graph.code_graph import CodeGraph
from codenib.web.modulemap import build_modulemap


def _add_symbol(graph: CodeGraph, name: str, file: str, line: int, kind="function"):
    graph._add_vertex(
        name,
        {
            "type": kind,
            "file": file,
            "start_line": line,
            "end_line": line + 2,
            "unified_name": name,
        },
    )


def _repo_graph() -> CodeGraph:
    """A small repo: a renderer leaning on two helpers, plus a test and a stub."""
    graph = CodeGraph()
    layout = [
        ("src/render.js:render()", "src/render.js", 0),
        ("src/render.js:hydrate()", "src/render.js", 20),
        ("src/diff/index.js:diff()", "src/diff/index.js", 0),
        ("src/diff/index.js:applyRef()", "src/diff/index.js", 30),
        ("src/create-element.js:createVNode()", "src/create-element.js", 0),
        ("test/render.test.js:it_renders()", "test/render.test.js", 0),
    ]
    for name, file, line in layout:
        _add_symbol(graph, name, file, line)
    _add_symbol(graph, "src/dom.d.ts:Attrs", "src/dom.d.ts", 0, kind="class")

    references = [
        ("src/render.js:render()", "src/diff/index.js:diff()", "src/render.js", 3),
        ("src/render.js:render()", "src/diff/index.js:applyRef()", "src/render.js", 4),
        (
            "src/render.js:hydrate()",
            "src/diff/index.js:diff()",
            "src/render.js",
            22,
        ),
        (
            "src/render.js:render()",
            "src/create-element.js:createVNode()",
            "src/render.js",
            5,
        ),
        (
            "src/diff/index.js:diff()",
            "src/create-element.js:createVNode()",
            "src/diff/index.js",
            8,
        ),
        # A test depending on the renderer, and the renderer touching a typedef.
        (
            "test/render.test.js:it_renders()",
            "src/render.js:render()",
            "test/render.test.js",
            2,
        ),
        ("src/render.js:render()", "src/dom.d.ts:Attrs", "src/render.js", 6),
    ]
    for source, target, anchor_file, anchor_line in references:
        graph._add_edge(
            source,
            target,
            "reference",
            anchor_file=anchor_file,
            anchor_line=anchor_line,
        )
    return graph


def test_modulemap_projects_symbol_references_onto_files():
    result = build_modulemap(_repo_graph(), granularity="file")

    assert result["available"] is True
    assert result["granularity"] == "file"
    paths = {node["path"] for node in result["nodes"]}
    assert paths == {"src/render.js", "src/diff/index.js", "src/create-element.js"}

    by_id = {node["id"]: node["path"] for node in result["nodes"]}
    edges = {
        (by_id[e["source"]], by_id[e["target"]]): e["weight"] for e in result["edges"]
    }
    # render() and hydrate() both reach diff(), and render() also reaches
    # applyRef() — three distinct symbol pairs across the module boundary.
    assert edges[("src/render.js", "src/diff/index.js")] == 3
    assert edges[("src/render.js", "src/create-element.js")] == 1
    assert edges[("src/diff/index.js", "src/create-element.js")] == 1


def test_modulemap_excludes_tests_and_declarations_but_reports_the_count():
    result = build_modulemap(_repo_graph(), granularity="file")

    assert all("test/" not in node["path"] for node in result["nodes"])
    assert all(not node["path"].endswith(".d.ts") for node in result["nodes"])
    assert result["excluded"] == {"test_files": 1, "derived_files": 1}


def test_modulemap_can_include_tests_on_request():
    result = build_modulemap(_repo_graph(), granularity="file", include_tests=True)

    paths = {node["path"] for node in result["nodes"]}
    assert "test/render.test.js" in paths
    assert result["excluded"]["test_files"] == 0


def test_modulemap_carries_call_site_anchors_back_to_source():
    result = build_modulemap(_repo_graph(), granularity="file")

    by_id = {node["id"]: node["path"] for node in result["nodes"]}
    edge = next(
        e
        for e in result["edges"]
        if (by_id[e["source"]], by_id[e["target"]])
        == ("src/render.js", "src/diff/index.js")
    )
    assert edge["call_sites"] == 3
    # anchor_line is stored 0-based and exposed 1-based.
    assert {a["line"] for a in edge["anchors"]} == {4, 5, 23}
    assert all(a["file"] == "src/render.js" for a in edge["anchors"])
    assert edge["hidden_anchors"] == 0


def test_modulemap_rolls_up_to_directories():
    result = build_modulemap(_repo_graph(), granularity="directory")

    assert result["granularity"] == "directory"
    by_id = {node["id"]: node["path"] for node in result["nodes"]}
    assert set(by_id.values()) == {"src", "src/diff"}
    edges = {
        (by_id[e["source"]], by_id[e["target"]]): e["weight"] for e in result["edges"]
    }
    # src/render.js -> src/diff/index.js survives the rollup; the
    # render.js -> create-element.js edge is now intra-directory and drops out.
    assert edges == {("src", "src/diff"): 3, ("src/diff", "src"): 1}


def test_modulemap_focus_walks_the_neighbourhood():
    result = build_modulemap(
        _repo_graph(), focus="src/diff/index.js", granularity="file", depth=1
    )

    assert result["focus"] == "src/diff/index.js"
    root = next(node for node in result["nodes"] if node["is_root"])
    assert root["path"] == "src/diff/index.js"
    assert {node["path"] for node in result["nodes"]} == {
        "src/diff/index.js",
        "src/render.js",
        "src/create-element.js",
    }


def test_modulemap_focus_accepts_a_suffix_and_falls_back_when_unknown():
    matched = build_modulemap(_repo_graph(), focus="index.js", granularity="file")
    assert matched["focus"] == "src/diff/index.js"

    missing = build_modulemap(_repo_graph(), focus="nope/gone.js", granularity="file")
    assert missing["focus"] == ""
    assert "not a module" in missing["note"]
    assert missing["available"] is True


def test_modulemap_caps_nodes_and_flags_truncation():
    graph = CodeGraph()
    for i in range(30):
        _add_symbol(graph, f"src/m{i}.py:fn()", f"src/m{i}.py", 0)
    for i in range(29):
        graph._add_edge(
            f"src/m{i}.py:fn()",
            f"src/m{i + 1}.py:fn()",
            "reference",
            anchor_file=f"src/m{i}.py",
            anchor_line=1,
        )

    result = build_modulemap(graph, granularity="file", max_nodes=10)

    assert len(result["nodes"]) == 10
    assert result["truncated"] is True
    assert result["total_modules"] == 30


def test_modulemap_auto_granularity_rolls_up_a_wide_repo():
    graph = CodeGraph()
    for i in range(80):
        _add_symbol(graph, f"pkg/sub{i % 4}/m{i}.py:fn()", f"pkg/sub{i % 4}/m{i}.py", 0)
    for i in range(79):
        graph._add_edge(
            f"pkg/sub{i % 4}/m{i}.py:fn()",
            f"pkg/sub{(i + 1) % 4}/m{i + 1}.py:fn()",
            "reference",
            anchor_file=f"pkg/sub{i % 4}/m{i}.py",
            anchor_line=1,
        )

    auto = build_modulemap(graph, granularity="auto")
    assert auto["granularity"] == "directory"
    assert {node["path"] for node in auto["nodes"]} == {f"pkg/sub{i}" for i in range(4)}

    forced = build_modulemap(graph, granularity="file", max_nodes=200)
    assert forced["granularity"] == "file"
    assert len(forced["nodes"]) == 80


def test_modulemap_handles_a_graph_with_no_attributed_symbols():
    graph = CodeGraph()
    graph._add_vertex("src", {"type": "directory"})

    result = build_modulemap(graph)

    assert result["available"] is False
    assert result["nodes"] == []
    assert result["note"]


def test_modulemap_edges_carry_hierarchy_routes():
    result = build_modulemap(_repo_graph(), granularity="file")

    hierarchy_ids = {node["id"] for node in result["hierarchy"]["nodes"]}
    assert "hier::file::src/render.js" in hierarchy_ids
    assert "hier::dir::src/diff" in hierarchy_ids
    for edge in result["edges"]:
        assert edge["source_hierarchy"] in hierarchy_ids
        assert edge["target_hierarchy"] in hierarchy_ids
        assert edge["bundle_path"][0] == edge["source_hierarchy"]
        assert edge["bundle_path"][-1] == edge["target_hierarchy"]
        assert edge["bundle_lca"] in edge["bundle_path"]
    # No view node leaks the internal hierarchy pointer into the payload.
    assert all("hierarchy_id" not in node for node in result["nodes"])


def test_modulemap_surfaces_a_re_export_barrel_through_its_file_node():
    """Issue #390: `codemap?symbol=src/index.js` returns 1 node and 0 edges.

    A barrel declares no symbols of its own, but the TS decoder points its
    re-export references at the target *file* node, so the module view can still
    show where the public API comes from.
    """
    graph = CodeGraph()
    graph._add_vertex("src/index.js", {"type": "file"})
    graph._add_vertex("src/render.js", {"type": "file"})
    _add_symbol(graph, "src/render.js:render()", "src/render.js", 0)
    graph._add_edge(
        "src/index.js",
        "src/render.js",
        "reference",
        anchor_file="src/index.js",
        anchor_line=0,
    )

    result = build_modulemap(graph, granularity="file")

    by_id = {node["id"]: node["path"] for node in result["nodes"]}
    assert set(by_id.values()) == {"src/index.js", "src/render.js"}
    assert [(by_id[e["source"]], by_id[e["target"]]) for e in result["edges"]] == [
        ("src/index.js", "src/render.js")
    ]
    barrel = next(n for n in result["nodes"] if n["path"] == "src/index.js")
    assert barrel["symbol_count"] == 0  # honest: it declares nothing itself


def test_modulemap_drops_files_with_neither_symbols_nor_dependencies():
    """Empty ``__init__.py`` files are real, but a dependency map has nothing
    to say about them — they were 11 of httpie's 96 projected modules."""
    graph = CodeGraph()
    graph._add_vertex("pkg/__init__.py", {"type": "file"})
    graph._add_vertex("pkg/a.py", {"type": "file"})
    _add_symbol(graph, "pkg/a.py:alpha()", "pkg/a.py", 0)
    _add_symbol(graph, "pkg/b.py:beta()", "pkg/b.py", 0)
    graph._add_edge(
        "pkg/a.py:alpha()",
        "pkg/b.py:beta()",
        "reference",
        anchor_file="pkg/a.py",
        anchor_line=1,
    )

    result = build_modulemap(graph, granularity="file")

    assert {node["path"] for node in result["nodes"]} == {"pkg/a.py", "pkg/b.py"}
    assert result["total_modules"] == 2
