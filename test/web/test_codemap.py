# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the web codemap payload builder."""

import subprocess

from codenib.graph.code_graph import CodeGraph
from codenib.web.codemap import (
    _default_seed,
    _DerivedFiles,
    _enrich,
    build_codemap,
    build_page_subgraph,
)


def _symbol_graph_with_many_call_sites(count: int = 12) -> CodeGraph:
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
    for line in range(count):
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


def test_codemap_bounds_anchor_samples_without_losing_exact_weight():
    graph = _symbol_graph_with_many_call_sites(100)

    result = build_codemap(
        graph, symbol="caller", direction="callees", depth=1, max_nodes=5
    )

    edge = result["edges"][0]
    assert edge["weight"] == 100
    assert len(edge["anchors"]) == 12
    assert edge["hidden_anchors"] == 88
    assert [anchor["line"] for anchor in edge["anchors"]] == list(range(1, 13))


def test_page_subgraph_bounds_repeated_anchor_collection():
    graph = _symbol_graph_with_many_call_sites(100)

    result = build_page_subgraph(
        graph,
        [
            {"file": "src/main.py", "start_line": 1, "node_name": "caller"},
            {"file": "src/helper.py", "start_line": 11, "node_name": "callee"},
        ],
    )

    edge = result["edges"][0]
    assert edge["weight"] == 100
    assert len(edge["anchors"]) == 12
    assert edge["hidden_anchors"] == 88


def test_page_subgraph_ranks_bridges_by_exact_anchor_count():
    graph = CodeGraph()
    vertices = {
        "src/a.py:seed_a()": ("src/a.py", 0),
        "src/b.py:seed_b()": ("src/b.py", 10),
        "src/strong.py:strong()": ("src/strong.py", 20),
        "src/skew.py:skew()": ("src/skew.py", 30),
    }
    for name, (file_path, line) in vertices.items():
        graph._add_vertex(
            name,
            {
                "type": "function",
                "file": file_path,
                "start_line": line,
                "end_line": line + 2,
                "unified_name": name,
            },
        )

    edge_counts = [
        ("src/a.py:seed_a()", "src/strong.py:strong()", 4),
        ("src/strong.py:strong()", "src/b.py:seed_b()", 4),
        ("src/a.py:seed_a()", "src/skew.py:skew()", 1),
        ("src/skew.py:skew()", "src/b.py:seed_b()", 6),
    ]
    anchor_line = 0
    for source, target, count in edge_counts:
        for _ in range(count):
            graph._add_edge(
                source,
                target,
                "reference",
                anchor_file=vertices[source][0],
                anchor_line=anchor_line,
            )
            anchor_line += 1

    result = build_page_subgraph(
        graph,
        [
            {"file": "src/a.py", "start_line": 1, "node_name": "seed_a"},
            {"file": "src/b.py", "start_line": 11, "node_name": "seed_b"},
        ],
        max_nodes=3,
    )

    labels = {node["label"] for node in result["nodes"]}
    assert "src/strong.py:strong()" in labels
    assert "src/skew.py:skew()" not in labels


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


def test_page_subgraph_maps_file_citation_to_representative_symbol(tmp_path):
    source = tmp_path / "src" / "worker.py"
    source.parent.mkdir()
    source.write_text("class Worker:\n    pass\n")
    graph = CodeGraph()
    graph._add_vertex(
        "src/worker.py",
        {
            "type": "file",
            "unified_name": "src/worker.py",
        },
    )
    graph._add_vertex(
        "src/worker.py:CompileInput",
        {
            "type": "class",
            "file": str(source),
            "start_line": 1,
            "end_line": 2,
            "unified_name": "src/worker.py:CompileInput",
        },
    )
    graph._add_vertex(
        "src/worker.py:helper()",
        {
            "type": "function",
            "file": str(source),
            "start_line": 2,
            "end_line": 3,
            "unified_name": "src/worker.py:helper()",
        },
    )
    graph._add_vertex(
        "src/worker.py:Worker",
        {
            "type": "class",
            "file": str(source),
            "start_line": 20,
            "end_line": 40,
            "unified_name": "src/worker.py:Worker",
        },
    )

    result = build_page_subgraph(
        graph,
        [
            {
                "file": "src/worker.py",
                "start_line": 1,
                "node_name": "src/worker.py",
                "type": "file",
            }
        ],
        repo_dir=str(tmp_path),
    )

    assert [node["label"] for node in result["nodes"]] == ["src/worker.py:Worker"]
    assert result["nodes"][0]["file"] == "src/worker.py"
    assert result["nodes"][0]["external"] is False


def _preact_shaped_graph() -> CodeGraph:
    """A graph shaped like preactjs/preact: a small, dense hand-written core
    plus one huge ``.d.ts`` wall of interfaces referencing a shared type.

    Mirrors issue #390: ``src/dom.d.ts`` declares 127 interfaces over ~1400
    property lines, so raw reference out-degree there dwarfs anything in the
    ~5k lines of hand-written JS.
    """
    graph = CodeGraph()

    def add(name, kind, file, line):
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

    add("src/render.js:render()", "function", "src/render.js", 12)
    add("src/diff/index.js:diff()", "function", "src/diff/index.js", 30)
    add("src/create-element.js:createVNode()", "function", "src/create-element.js", 5)
    for target in ("src/diff/index.js:diff()", "src/create-element.js:createVNode()"):
        graph._add_edge(
            "src/render.js:render()",
            target,
            "reference",
            anchor_file="src/render.js",
            anchor_line=13,
        )

    # The declaration wall: one shared type plus many attribute interfaces.
    add("src/dom.d.ts:Signalish", "class", "src/dom.d.ts", 60)
    add("src/dom.d.ts:SVGAttributes", "class", "src/dom.d.ts", 81)
    for i in range(40):
        member = f"src/dom.d.ts:SVGAttributes.attr{i}"
        add(member, "field", "src/dom.d.ts", 100 + i)
        graph._add_edge(
            "src/dom.d.ts:SVGAttributes",
            member,
            "reference",
            anchor_file="src/dom.d.ts",
            anchor_line=100 + i,
        )
        graph._add_edge(
            member,
            "src/dom.d.ts:Signalish",
            "reference",
            anchor_file="src/dom.d.ts",
            anchor_line=100 + i,
        )
    return graph


def test_default_seed_prefers_hand_written_source_over_declaration_stubs():
    """Issue #390: the busiest symbol by raw out-degree is a ``.d.ts``
    interface, which filled the repo's default codemap with typedefs."""
    graph = _preact_shaped_graph()

    result = build_codemap(graph, symbol=None, depth=2, max_nodes=40)

    assert result["root"] == "src/render.js:render()"
    assert "dom.d.ts" not in result["root_label"]
    assert all(not node["declaration"] for node in result["nodes"])


def test_default_seed_falls_back_to_declarations_when_nothing_else_exists():
    """A repo that really is all declarations still gets a map, not an empty one."""
    graph = CodeGraph()
    for name in ("api.d.ts:Alpha", "api.d.ts:Beta"):
        graph._add_vertex(
            name,
            {
                "type": "class",
                "file": "api.d.ts",
                "start_line": 1,
                "end_line": 3,
                "unified_name": name,
            },
        )
    graph._add_edge("api.d.ts:Alpha", "api.d.ts:Beta", "reference")

    result = build_codemap(graph, symbol=None, depth=1, max_nodes=10)

    assert result["available"] is True
    assert result["root"] == "api.d.ts:Alpha"


def test_default_seed_ranks_all_symbols_when_the_graph_has_no_reference_edges():
    graph = CodeGraph()
    for name, file in (
        ("api.d.ts:Types", "api.d.ts"),
        ("src/runtime.py:serve()", "src/runtime.py"),
    ):
        graph._add_vertex(
            name,
            {
                "type": "class" if file.endswith(".d.ts") else "function",
                "file": file,
                "start_line": 0,
                "end_line": 2,
                "unified_name": name,
            },
        )

    result = build_codemap(graph, symbol=None, depth=1, max_nodes=10)

    assert result["available"] is True
    assert result["root"] == "src/runtime.py:serve()"


def test_default_seed_stops_header_scans_after_the_winning_candidate():
    graph = CodeGraph()
    names = []
    for index in range(20):
        name = f"src/mod{index}.py:symbol{index}()"
        names.append(name)
        graph._add_vertex(
            name,
            {
                "type": "function",
                "file": f"src/mod{index}.py",
                "start_line": 0,
                "unified_name": name,
            },
        )
    for target in names[1:]:
        graph._add_edge(names[0], target, "reference")

    scanned = []

    def handwritten(path):
        scanned.append(path)
        return False

    assert _default_seed(graph, derived=handwritten) == names[0]
    assert scanned == ["src/mod0.py"]


def test_file_nodes_use_their_identity_for_declaration_classification():
    graph = CodeGraph()
    nodes = [
        {
            "id": "n0",
            "name": "src/types.d.ts",
            "file": None,
            "kind": "file",
            "is_root": True,
        }
    ]

    enriched, _ = _enrich(graph, nodes, [])

    assert enriched[0]["declaration"] is True


def test_default_seed_skips_generated_source(tmp_path):
    """Path rules miss generated code in ordinary source files (xarray's
    ``_typed_ops.py``); the header banner is what gives it away."""
    (tmp_path / "pkg").mkdir()
    generated = tmp_path / "pkg" / "_typed_ops.py"
    generated.write_text(
        '"""Mixin classes with arithmetic operators."""\n\n'
        "# This file was generated using pkg.util.generate_ops. "
        "Do not edit manually.\n\n"
        "class VariableOpsMixin:\n    pass\n"
    )
    handwritten = tmp_path / "pkg" / "core.py"
    handwritten.write_text("def compute():\n    return 1\n")

    graph = CodeGraph()
    for name, file, line in [
        ("pkg/_typed_ops.py:VariableOpsMixin", "pkg/_typed_ops.py", 4),
        ("pkg/core.py:compute()", "pkg/core.py", 0),
        ("pkg/core.py:helper()", "pkg/core.py", 4),
    ]:
        graph._add_vertex(
            name,
            {
                "type": "class" if "Mixin" in name else "function",
                "file": file,
                "start_line": line,
                "end_line": line + 2,
                "unified_name": name,
            },
        )
    for i in range(9):
        target = f"pkg/_typed_ops.py:op{i}"
        graph._add_vertex(
            target,
            {
                "type": "method",
                "file": "pkg/_typed_ops.py",
                "start_line": 10 + i,
                "end_line": 11 + i,
                "unified_name": target,
            },
        )
        graph._add_edge("pkg/_typed_ops.py:VariableOpsMixin", target, "reference")
    graph._add_edge("pkg/core.py:compute()", "pkg/core.py:helper()", "reference")

    result = build_codemap(
        graph, symbol=None, depth=1, max_nodes=20, repo_dir=str(tmp_path)
    )

    assert result["root"] == "pkg/core.py:compute()"


def test_derived_file_scan_rejects_paths_outside_the_repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("# Generated by an external tool. DO NOT EDIT.\n")
    (repo / "escape.py").symlink_to(outside)
    derived = _DerivedFiles(str(repo))

    assert derived("../outside.py") is False
    assert derived(str(outside)) is False
    assert derived("escape.py") is False


def test_derived_file_scan_reads_the_selected_commit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    source = repo / "generated.py"
    source.write_text("# Generated by schema compiler. DO NOT EDIT.\nvalue = 1\n")
    git("add", "generated.py")
    git("commit", "-qm", "generated")
    generated_commit = git("rev-parse", "HEAD")

    source.write_text("value = 2\n")
    git("commit", "-qam", "handwritten")
    handwritten_commit = git("rev-parse", "HEAD")

    assert _DerivedFiles(str(repo), generated_commit)("generated.py") is True
    assert _DerivedFiles(str(repo), handwritten_commit)("generated.py") is False
    assert _DerivedFiles(str(repo))("generated.py") is False


def test_declaration_nodes_are_flagged_and_lose_the_pagerank_restart():
    """Declarations stay in the map (they are real context) but stop
    manufacturing their own importance."""
    graph = _preact_shaped_graph()
    graph._add_edge(
        "src/render.js:render()",
        "src/dom.d.ts:Signalish",
        "reference",
        anchor_file="src/render.js",
        anchor_line=14,
    )

    result = build_codemap(
        graph, symbol="render", direction="callees", depth=2, max_nodes=40
    )

    by_label = {node["label"]: node for node in result["nodes"]}
    assert by_label["src/dom.d.ts:Signalish"]["declaration"] is True
    assert by_label["src/render.js:render()"]["declaration"] is False
    assert (
        by_label["src/diff/index.js:diff()"]["importance"]
        > by_label["src/dom.d.ts:Signalish"]["importance"]
    )


def _entry_repo(tmp_path):
    """A repo whose manifest names a barrel that re-exports the real API.

    The busiest symbol by degree sits in a helper module, so degree alone picks
    the wrong thing — which is what the entry-point tier exists to correct.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.js").write_text("export { render } from './render';\n")
    (tmp_path / "src" / "render.js").write_text("export function render() {}\n")
    (tmp_path / "src" / "util.js").write_text("export function busy() {}\n")
    (tmp_path / "package.json").write_text('{"name": "lib", "source": "src/index.js"}')

    graph = CodeGraph()
    graph._add_vertex("src/index.js", {"type": "file"})
    for name, file, line in [
        ("src/render.js:render()", "src/render.js", 0),
        ("src/util.js:busy()", "src/util.js", 0),
        ("src/util.js:_helper()", "src/util.js", 10),
        ("src/util.js:leaf()", "src/util.js", 20),
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
    # The barrel re-exports render(); util.js:busy() is the degree winner.
    graph._add_edge("src/index.js", "src/render.js:render()", "reference")
    graph._add_edge("src/render.js:render()", "src/util.js:leaf()", "reference")
    for i in range(6):
        graph._add_vertex(
            f"src/util.js:t{i}()",
            {
                "type": "function",
                "file": "src/util.js",
                "start_line": 40 + i,
                "end_line": 41 + i,
                "unified_name": f"src/util.js:t{i}()",
            },
        )
        graph._add_edge("src/util.js:busy()", f"src/util.js:t{i}()", "reference")
    return graph


def test_seed_follows_the_manifest_entry_through_its_re_exports(tmp_path):
    """The declared entry is a barrel that defines nothing, so the seed has to
    come from what its file node re-exports."""
    graph = _entry_repo(tmp_path)

    result = build_codemap(graph, symbol=None, depth=1, repo_dir=str(tmp_path))

    assert result["root"] == "src/render.js:render()"


def test_seed_ignores_entry_points_when_the_repo_declares_none(tmp_path):
    """Without a manifest the original out-degree ranking still applies."""
    graph = _entry_repo(tmp_path)
    (tmp_path / "package.json").unlink()

    result = build_codemap(graph, symbol=None, depth=1, repo_dir=str(tmp_path))

    assert result["root"] == "src/util.js:busy()"


def test_seed_prefers_a_public_symbol_over_a_private_one(tmp_path):
    """matplotlib's entry file offered `_get_executable_info().impl` — public by
    its leaf name, private by its enclosing function."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "pkg"\n')

    graph = CodeGraph()
    for name in ("pkg/__init__.py:_private().impl()", "pkg/__init__.py:public()"):
        graph._add_vertex(
            name,
            {
                "type": "function",
                "file": "pkg/__init__.py",
                "start_line": 0,
                "end_line": 2,
                "unified_name": name,
            },
        )
    graph._add_vertex(
        "pkg/other.py:target()",
        {
            "type": "function",
            "file": "pkg/other.py",
            "start_line": 0,
            "end_line": 2,
            "unified_name": "pkg/other.py:target()",
        },
    )
    # The private one has the higher out-degree, so only the public tier saves it.
    graph._add_edge(
        "pkg/__init__.py:_private().impl()", "pkg/other.py:target()", "reference"
    )
    graph._add_edge("pkg/__init__.py:public()", "pkg/other.py:target()", "reference")

    result = build_codemap(graph, symbol=None, depth=1, repo_dir=str(tmp_path))

    assert result["root"] == "pkg/__init__.py:public()"
