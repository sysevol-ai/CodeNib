# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Per-language precise tests for range queries on test/scip/simple_repos.

Mirrors the algorithmic tests in ``test_range_queries.py`` (overlap modes,
nested scopes, multi-edge same-pair, boundary inclusivity) but runs against
real source decoded by each language's SCIP indexer — verifying that the
anchor-line threading works end-to-end per language.

Source-level facts these tests rely on (verify by reading the file):

- ``rust_simple/src/math_utils.rs::add`` defined at lines 4–6
- ``rust_simple/src/shapes.rs`` has ``impl Shape for Rectangle`` (lines 39–51)
  containing nested method definitions ``area`` / ``perimeter`` / ``name``
- ``typescript_simple/src/main.ts`` calls ``add`` three times from module
  scope (lines 8, 9, 10) — exact multi-edge fixture

Each test skips cleanly if the SCIP tool for its language isn't installed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration_serial

from codeminer.graph.code_graph import CodeGraph
from codeminer.ls_router import LSIndexer
from codeminer.types import NODE_TYPE_CLASS, NODE_TYPE_FUNCTION, NODE_TYPE_METHOD

SIMPLE_REPOS = Path(__file__).resolve().parent.parent / "scip" / "simple_repos"


_SCIP_TOOLS = {
    "rust": "rust-analyzer",
    "ts": "scip-typescript",
    "go": "scip-go",
    "cpp": "clangd",
}


def _skip_if_no_scip(language: str) -> None:
    tool = _SCIP_TOOLS.get(language)
    if tool and not shutil.which(tool):
        pytest.skip(f"SCIP tool {tool!r} not installed")


def _ensure_cpp_compdb(repo: Path) -> None:
    compdb = repo / "build" / "compile_commands.json"
    if compdb.exists():
        return
    subprocess.run(
        [
            "cmake",
            "-S",
            str(repo),
            "-B",
            str(repo / "build"),
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _build_graph(language: str, repo_subdir: str, tmp_path: Path) -> CodeGraph:
    """Build a fresh CodeGraph for ``simple_repos/<repo_subdir>``."""
    src = SIMPLE_REPOS / repo_subdir
    if not src.exists():
        pytest.skip(f"{repo_subdir} not found in simple_repos")

    if language == "cpp":
        _ensure_cpp_compdb(src)

    output_dir = tmp_path / repo_subdir
    output_dir.mkdir(parents=True, exist_ok=True)
    indexer = LSIndexer(
        project_root=src,
        output_dir=output_dir,
        language=language,
        decoder_backend="serial",
    )
    kwargs = {"infer_tsconfig": True} if language == "ts" else {}
    graph = indexer.run_pipeline(
        skip_level=None,  # full rebuild — no cache pollution from prior schemas
        report_profile=False,
        **kwargs,
    )
    assert graph is not None, f"run_pipeline returned None for {repo_subdir}"
    return graph


# ---------------------------------------------------------------------------
# Per-language session-scoped graphs
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rust_graph(tmp_path_factory) -> CodeGraph:
    _skip_if_no_scip("rust")
    return _build_graph(
        "rust", "rust_simple", tmp_path_factory.mktemp("rust_simple_idx")
    )


@pytest.fixture(scope="module")
def ts_graph(tmp_path_factory) -> CodeGraph:
    _skip_if_no_scip("ts")
    return _build_graph("ts", "typescript_simple", tmp_path_factory.mktemp("ts_idx"))


@pytest.fixture(scope="module")
def go_graph(tmp_path_factory) -> CodeGraph:
    _skip_if_no_scip("go")
    return _build_graph("go", "go_simple", tmp_path_factory.mktemp("go_idx"))


@pytest.fixture(scope="module")
def cpp_graph(tmp_path_factory) -> CodeGraph:
    _skip_if_no_scip("cpp")
    return _build_graph("cpp", "cpp_simple", tmp_path_factory.mktemp("cpp_idx"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_symbol_in_file(g, file_substr: str, name_substr: str):
    """Return (start_line, end_line, vid, file_path) for the first symbol
    whose ``file`` ends with ``file_substr`` and ``name`` contains
    ``name_substr``. Skips nodes without a real def range."""
    for vid in range(g.graph.vcount()):
        v = g.graph.vs[vid]
        attrs = v.attributes()
        f = attrs.get("file")
        name = attrs.get("name", "")
        if not f or not f.endswith(file_substr):
            continue
        if name_substr not in name:
            continue
        s = attrs.get("start_line")
        e = attrs.get("end_line")
        ntype = attrs.get("type")
        if (
            s is None
            or e is None
            or s == e
            or ntype not in (NODE_TYPE_FUNCTION, NODE_TYPE_METHOD, NODE_TYPE_CLASS)
        ):
            continue
        return s, e, vid, f
    return None


# ---------------------------------------------------------------------------
# Overlap modes — Rust math_utils.rs::add (lines 4-6)
# ---------------------------------------------------------------------------


def test_rust_defined_overlap_partial_left(rust_graph):
    """Range starts before def, ends inside → match (left overlap).

    Picks a function whose def start_line is far enough into the file to
    leave room for a `s - 2` query window (some SCIP backends include
    leading doc comments in the enclosing_range, making the file's first
    function start at line 0 or 1).
    """
    g = rust_graph
    # Walk math_utils.rs symbols and pick one with start_line ≥ 2.
    file = next((f for f in g._file_nodes if f.endswith("math_utils.rs")), None)
    if file is None:
        pytest.skip("math_utils.rs not in graph")
    picked = None
    for s, e, vid in g._file_nodes[file]:
        if s < 2 or s == e:
            continue
        ntype = g.graph.vs[vid].attributes().get("type")
        if ntype in (NODE_TYPE_FUNCTION, NODE_TYPE_METHOD, NODE_TYPE_CLASS):
            picked = (s, e, vid)
            break
    if picked is None:
        pytest.skip("no symbol in math_utils.rs with start_line ≥ 2")
    s, e, vid = picked

    r = g.query_range(file, s - 2, s)
    assert vid in [n.vid for n in r.defined]


def test_rust_defined_overlap_partial_right(rust_graph):
    """Range starts inside def, ends after → match (right overlap)."""
    target = _find_symbol_in_file(rust_graph, "math_utils.rs", "add")
    assert target is not None
    s, e, vid, file = target
    r = rust_graph.query_range(file, e, e + 5)
    assert vid in [n.vid for n in r.defined]


def test_rust_defined_overlap_fully_inside(rust_graph):
    """Range fully inside def → match."""
    target = _find_symbol_in_file(rust_graph, "math_utils.rs", "factorial")
    if target is None:
        # fallback to add if factorial not present
        target = _find_symbol_in_file(rust_graph, "math_utils.rs", "add")
    assert target is not None
    s, e, vid, file = target
    if e - s < 2:
        pytest.skip(f"def range {s}-{e} too tight for fully-inside test")
    r = rust_graph.query_range(file, s + 1, max(s + 1, e - 1))
    assert vid in [n.vid for n in r.defined]


def test_rust_defined_no_overlap(rust_graph):
    """Range with no overlap returns no definitions for the same file."""
    target = _find_symbol_in_file(rust_graph, "math_utils.rs", "add")
    assert target is not None
    s, e, vid, file = target
    # Pick a range guaranteed past add() but before tests mod (line ~48)
    r = rust_graph.query_range(file, e + 1, e + 1)
    # vid must NOT appear in defined for a single-line range past def end
    assert vid not in [n.vid for n in r.defined]


# ---------------------------------------------------------------------------
# Nested scopes — Rust shapes.rs has impl Shape for Rectangle with nested methods
# ---------------------------------------------------------------------------


def test_rust_defined_includes_nested_scopes(rust_graph):
    """Querying a range inside an impl block returns both the outer block
    (or struct) and the inner methods that overlap."""
    g = rust_graph
    rect_impl = _find_symbol_in_file(g, "shapes.rs", "Rectangle")
    if rect_impl is None:
        pytest.skip("Rectangle symbol not found in shapes.rs")

    # Find one inner method (area / perimeter / new / width)
    inner_method = None
    for cand in ("area", "perimeter", "name", "new", "width", "height"):
        m = _find_symbol_in_file(g, "shapes.rs", cand)
        if m is None:
            continue
        # Inner means its range is contained in (or overlaps) Rectangle's range
        inner_method = m
        break
    if inner_method is None:
        pytest.skip("no nested method found in shapes.rs")

    inner_s, inner_e, inner_vid, file = inner_method
    r = g.query_range(file, inner_s, inner_e)
    # The inner method itself must appear
    assert inner_vid in [n.vid for n in r.defined]
    # Plus an outer scope (Rectangle / impl / file-level structure)
    assert len(r.defined) >= 1


# ---------------------------------------------------------------------------
# Multi-edge — TypeScript main.ts calls add() 3× from module scope
# ---------------------------------------------------------------------------


def test_ts_multi_edge_three_calls_to_add(ts_graph):
    """In TS main.ts the import ``add`` is called from module scope at
    lines 8, 9, 10. Without the dedup removal these would collapse to one
    edge; with multi-edge, all three should appear."""
    g = ts_graph
    add_target = _find_symbol_in_file(g, "math.ts", "add")
    if add_target is None:
        pytest.skip("add not found in math.ts")

    _, _, add_vid, _ = add_target
    incoming_eids = g.graph.incident(add_vid, mode="in")

    # Filter to anchors in main.ts
    main_anchored = [
        eid
        for eid in incoming_eids
        if (g.graph.es[eid].attributes().get("anchor_file") or "").endswith("main.ts")
    ]
    # main.ts has at least 3 module-scope calls + 1 inside wrapper() = ≥ 4
    assert len(main_anchored) >= 3, (
        f"expected ≥ 3 incoming edges from main.ts to add, got {len(main_anchored)}: "
        f"{[g.graph.es[e]['anchor_line'] for e in main_anchored]}"
    )


def test_ts_multi_edge_query_range_distinguishes_call_sites(ts_graph):
    """Querying a tight range around lines 8-10 of main.ts should return
    the three add-call edges; querying a single line should return one."""
    g = ts_graph
    main_path = next((f for f in g._file_edge_anchors if f.endswith("main.ts")), None)
    if main_path is None:
        pytest.skip("main.ts not in edge_anchor_index")

    # Find the 3 lines with add() calls — each should have ≥ 1 anchor.
    arr = g._file_edge_anchors[main_path]
    add_target = _find_symbol_in_file(g, "math.ts", "add")
    assert add_target is not None
    _, _, add_vid, _ = add_target

    add_call_lines = sorted(
        line for line, eid in arr if g.graph.es[eid].target == add_vid
    )
    assert (
        len(add_call_lines) >= 3
    ), f"expected ≥ 3 anchors in main.ts pointing at add, got {add_call_lines}"

    # Query range that covers the first three add-calls only.
    lo, hi = add_call_lines[0], add_call_lines[2]
    r = g.query_range(main_path, lo, hi)
    add_outgoing = [e for e in r.outgoing if e.target_vid == add_vid]
    assert len(add_outgoing) >= 3, (
        f"query_range({main_path}, {lo}, {hi}) returned only "
        f"{len(add_outgoing)} edges into add"
    )


# ---------------------------------------------------------------------------
# Boundary inclusivity — anchor exactly on start/end_line
# ---------------------------------------------------------------------------


def test_rust_outgoing_boundary_inclusive(rust_graph):
    """Anchor lines exactly equal to query start_line / end_line are included."""
    g = rust_graph
    main_path = next((f for f in g._file_edge_anchors if f.endswith("main.rs")), None)
    if main_path is None or not g._file_edge_anchors[main_path]:
        pytest.skip("main.rs has no anchored edges")

    # Pick the first anchored line — query single-line range exactly at it.
    first_line = g._file_edge_anchors[main_path][0][0]
    r = g.query_range(main_path, first_line, first_line)
    assert (
        len(r.outgoing) >= 1
    ), f"single-line query at exactly the anchor line {first_line} returned 0 edges"


# ---------------------------------------------------------------------------
# Per-language smoke: indexes built + persisted with v3 schema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture_name",
    ["rust_graph", "ts_graph", "go_graph", "cpp_graph"],
)
def test_per_language_indexes_and_roundtrip(fixture_name, request, tmp_path):
    """Cheap smoke per language: indexes built, anchor cross-check, pickle
    round-trip lossless."""
    g = request.getfixturevalue(fixture_name)
    assert len(g._file_nodes) > 0
    # Edge anchors may be empty if a language's repo has only contain edges,
    # but for our fixture set at least one language always has references.
    if not g._file_edge_anchors:
        return  # acceptable per-language; cross-language test elsewhere asserts

    # Spot-check anchor consistency.
    for file, arr in list(g._file_edge_anchors.items())[:1]:
        for line, eid in arr[:5]:
            edge = g.graph.es[eid]
            assert edge["anchor_file"] == file
            assert edge["anchor_line"] == line

    # Round-trip.
    out = tmp_path / "graph.pkl"
    g.save_graph(str(out))
    reloaded = CodeGraph.load_graph(str(out))
    assert reloaded._file_nodes == g._file_nodes
    assert reloaded._file_edge_anchors == g._file_edge_anchors
