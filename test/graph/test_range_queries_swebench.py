# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Integration test: build range indexes on real SWE-bench repos (serial).

Parametrized over 5 languages following the ``test_scip_multilingual`` pattern:

- python via ``SwebenchDataset`` (astropy-12907)
- rust / ts / go / cpp via ``SwebenchMultilingualDataset``

Verifies the new line-range query surface end-to-end: SCIP decode → CodeGraph
→ build_range_indexes → query_range on real source. Uses ``skip_level="graph"``
to fast-path off cached ``graph.pkl`` when present; ``CodeGraph.load_graph``
raises ``ValueError`` on schema mismatch which ``run_pipeline`` catches and
falls back to rebuilding from the cached SCIP decode.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration_serial_consumer

from codeminer.graph.code_graph import CodeGraph, EdgeRef, NodeRef
from codeminer.ls_router import LSIndexer
from codeminer.types import (
    EDGE_TYPE_CONTAIN,
    NODE_TYPE_CLASS,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
)

# ---------------------------------------------------------------------------
# Per-language tooling probes (skip cleanly on machines without indexers)
# ---------------------------------------------------------------------------


def _tool_for(language: str) -> str:
    return {
        "python": "scip-python",
        "rust": "rust-analyzer",
        "ts": "scip-typescript",
        "go": "scip-go",
        "cpp": "clangd",
    }[language]


def _tools_ready(language: str) -> bool:
    return bool(shutil.which(_tool_for(language)))


# ---------------------------------------------------------------------------
# Per-language instance lookup (mirrors test_scip_multilingual.py)
# ---------------------------------------------------------------------------


_REPO_KEYWORDS = {
    "cpp": ["fmtlib/", "google/", "gabime/", "nlohmann/", "catchorg/"],
    "rust": ["BurntSushi/", "tokio-rs/", "clap-rs/", "rust-lang/", "image-rs/"],
    "ts": ["axios/", "expressjs/", "facebook/", "lodash/", "vuejs/"],
    "go": ["caddyserver/", "gin-gonic/", "gohugoio/", "hashicorp/", "prometheus/"],
}


def _pick_python_instance() -> tuple:
    """Return (dataset_obj, instance) for the pinned Python case (astropy)."""
    from codeminer.dataset.swebench import SwebenchDataset

    dataset_obj = SwebenchDataset(
        dataset="princeton-nlp/SWE-bench_Lite",
        split="test",
        filter_instance="^(astropy__astropy-12907)$",
    )
    rows = dataset_obj.load()
    return dataset_obj, dict(next(iter(rows)))


def _pick_multilingual_instance(language: str) -> tuple:
    """Return (dataset_obj, instance) for a SWE-bench_Multilingual instance."""
    from codeminer.dataset.swebench_multilingual import SwebenchMultilingualDataset

    dataset_obj = SwebenchMultilingualDataset(split="test", filter_instance=".*")
    rows = dataset_obj.load()
    for row in rows:
        if any(k in row["repo"] for k in _REPO_KEYWORDS[language]):
            return dataset_obj, dict(row)
    raise RuntimeError(f"No SWE-bench_Multilingual instance for {language}")


def _ensure_cpp_compdb(repo: Path) -> None:
    """Generate compile_commands.json for clangd if missing."""
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


# ---------------------------------------------------------------------------
# Per-language indexed_repo fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", params=["python", "rust", "ts", "go", "cpp"])
def language(request) -> str:
    return request.param


@pytest.fixture(scope="module")
def indexed_repo(language) -> CodeGraph:
    """Load (or build) a CodeGraph at the current schema for ``language``.

    Uses ``skip_level="graph"`` to load a cached ``graph.pkl`` when present
    (fast path). ``load_graph`` raises ``ValueError`` if the cached pickle's
    schema differs from ``_SCHEMA_VERSION``; ``run_pipeline`` catches the
    error and falls back to rebuilding from cached SCIP decode.
    """
    if not _tools_ready(language):
        pytest.skip(f"SCIP tool {_tool_for(language)!r} not installed")

    if language == "python":
        dataset_obj, instance = _pick_python_instance()
    else:
        try:
            dataset_obj, instance = _pick_multilingual_instance(language)
        except Exception as exc:
            pytest.skip(
                f"SWE-bench_Multilingual unavailable / no matching instance: {exc}"
            )

    dataset_obj.process_instance(instance)
    repo_path = Path(dataset_obj.get_repo_path(instance))
    if language == "cpp":
        _ensure_cpp_compdb(repo_path)

    output_dir = Path.home() / ".codeminer" / instance["instance_id"]
    output_dir.mkdir(parents=True, exist_ok=True)

    kwargs = {"infer_tsconfig": True} if language == "ts" else {}
    indexer = LSIndexer(
        project_root=repo_path,
        output_dir=output_dir,
        language=language,
        decoder_backend="serial",
    )
    graph = indexer.run_pipeline(
        skip_level="graph",
        report_profile=False,
        **kwargs,
    )
    assert graph is not None, f"run_pipeline returned None for {language}"
    return graph


# ---------------------------------------------------------------------------
# Index integrity (per-language)
# ---------------------------------------------------------------------------


def test_range_indexes_populated_after_pipeline(indexed_repo, language):
    """`process_index` must call `build_range_indexes()` and the persisted
    graph must carry non-empty indexes for both nodes and edges, in every
    language we support."""
    g = indexed_repo

    assert len(g._file_nodes) > 0, f"node_line_index has no files ({language})"
    assert len(g._file_edge_anchors) > 0, (
        f"edge_anchor_index has no files for {language} — anchor info "
        f"isn't being threaded through the {language} decoder"
    )

    # Per-file node entries are well-formed.
    sample_file = next(iter(g._file_nodes))
    nodes = g._file_nodes[sample_file]
    for entry in nodes:
        assert len(entry) == 3
        s, e, vid = entry
        assert isinstance(s, int) and isinstance(e, int) and isinstance(vid, int)
        assert s <= e
        assert 0 <= vid < g.graph.vcount()

    # Edge anchor entries are sorted by line, eids are valid.
    sample_anchor_file = next(iter(g._file_edge_anchors))
    arr = g._file_edge_anchors[sample_anchor_file]
    assert [line for line, _ in arr] == sorted(line for line, _ in arr)
    for _, eid in arr:
        assert 0 <= eid < g.graph.ecount()


def test_edge_anchor_lines_match_edge_attributes(indexed_repo):
    """Cross-check: each (line, eid) in the anchor index agrees with the
    edge's stored ``anchor_file`` / ``anchor_line`` attributes."""
    g = indexed_repo
    checked = 0
    for file, arr in g._file_edge_anchors.items():
        for line, eid in arr:
            edge = g.graph.es[eid]
            assert edge["anchor_file"] == file
            assert edge["anchor_line"] == line
            checked += 1
            if checked >= 50:
                return


# ---------------------------------------------------------------------------
# query_range correctness on real symbols
# ---------------------------------------------------------------------------


def _pick_symbol_node(g, file_path: str, min_span: int = 5):
    """Find a function/method/class in ``file_path`` with a span ≥ ``min_span``."""
    for s, e, vid in g._file_nodes.get(file_path, []):
        if (e - s) < min_span:
            continue
        ntype = g.graph.vs[vid].attributes().get("type")
        if ntype in (NODE_TYPE_FUNCTION, NODE_TYPE_METHOD, NODE_TYPE_CLASS):
            return s, e, vid
    return None


def _pick_file_with_symbols(g) -> str | None:
    """Find any source file with at least one function-shaped symbol."""
    for f in g._file_nodes:
        if _pick_symbol_node(g, f, min_span=3) is not None:
            return f
    return None


def test_query_range_returns_self_for_symbol_def_range(indexed_repo, language):
    """Querying a symbol's own def range must return that symbol in `defined`."""
    g = indexed_repo
    target_file = _pick_file_with_symbols(g)
    if target_file is None:
        pytest.skip(f"no function-shaped symbol found in {language} graph")

    picked = _pick_symbol_node(g, target_file, min_span=5)
    if picked is None:
        pytest.skip(f"no symbol with span ≥ 5 in {target_file}")

    s, e, vid = picked
    r = g.query_range(target_file, s, e)
    defined_vids = [n.vid for n in r.defined]
    assert vid in defined_vids, (
        f"[{language}] {g.graph.vs[vid]['name']} (vid={vid}, lines {s}-{e}) "
        f"missing from defined={defined_vids}"
    )


def test_outgoing_edges_anchor_inside_query_range(indexed_repo):
    """Every edge returned by ``outgoing`` has anchor_line in range and
    anchor_file matches. Picks the densest file so the spot-check window
    is non-trivial."""
    g = indexed_repo
    target_file, arr = max(g._file_edge_anchors.items(), key=lambda kv: len(kv[1]))
    assert len(arr) >= 5, f"densest file has < 5 anchors ({len(arr)} in {target_file})"
    mid = len(arr) // 2
    line_lo = arr[max(mid - 2, 0)][0]
    line_hi = arr[min(mid + 2, len(arr) - 1)][0]
    if line_lo == line_hi:
        line_hi = line_lo + 1

    r = g.query_range(target_file, line_lo, line_hi)
    assert len(r.outgoing) > 0
    for edge_ref in r.outgoing:
        assert edge_ref.anchor_file == target_file
        assert line_lo <= edge_ref.anchor_line <= line_hi


def test_incoming_edges_target_def_inside_query_range(indexed_repo, language):
    """For a popular symbol, every edge returned by ``incoming`` must point
    to a node whose def overlaps the queried range."""
    g = indexed_repo

    target_file = None
    target = None
    for f, nodes in g._file_nodes.items():
        for s, e, vid in nodes:
            if (e - s) < 3:
                continue
            in_count = len(g.graph.incident(vid, mode="in"))
            if in_count > 2:
                target_file = f
                target = (s, e, vid)
                break
        if target:
            break

    if target is None:
        pytest.skip(f"[{language}] no symbol with > 2 incoming edges found")

    s, e, vid = target
    r = g.query_range(target_file, s, e)
    assert len(r.incoming) > 0

    defined_vids = {n.vid for n in r.defined}
    for edge_ref in r.incoming:
        assert edge_ref.target_vid in defined_vids


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_pickled_graph_loads_and_indexes_intact(indexed_repo, tmp_path):
    """save_graph + load_graph round-trips the indexes; verifies the pickle
    written by run_pipeline is at the current schema and reloads cleanly."""
    out = tmp_path / "graph.pkl"
    indexed_repo.save_graph(str(out))
    reloaded = CodeGraph.load_graph(str(out))

    assert reloaded._file_nodes == indexed_repo._file_nodes
    assert reloaded._file_edge_anchors == indexed_repo._file_edge_anchors

    sample_file = next(iter(indexed_repo._file_nodes))
    r1 = indexed_repo.query_range(sample_file, 1, 9999)
    r2 = reloaded.query_range(sample_file, 1, 9999)
    assert r1.defined == r2.defined
    assert r1.outgoing == r2.outgoing
    assert r1.incoming == r2.incoming


def test_unified_to_names_populated_on_real_repo(indexed_repo, language):
    """On a real SWE-bench graph (across all 5 languages), the unified_name
    aux index is populated and every candidate identity is a real vertex.

    Catches: the aux build path is skipped, the indexer doesn't call
    build_range_indexes, or the index falls out of sync with name_to_vertex.
    """
    graph = indexed_repo

    assert graph._unified_to_names, (
        f"[{language}] _unified_to_names empty on a real graph — "
        f"build_range_indexes likely not called by the indexer pipeline"
    )

    # Every entry's identity name must resolve to a real vertex
    for unified, candidates in graph._unified_to_names.items():
        assert (
            candidates
        ), f"[{language}] empty candidate list for unified_name={unified!r}"
        for name in candidates:
            assert name in graph.name_to_vertex, (
                f"[{language}] _unified_to_names[{unified!r}] -> {name!r} "
                f"missing from name_to_vertex"
            )

    # Spot-check: the inverse mapping is consistent — every vertex with a
    # non-empty unified_name appears in the aux index under that key.
    for vid in range(graph.graph.vcount()):
        v = graph.graph.vs[vid]
        u = v.attributes().get("unified_name")
        if u:
            assert v["name"] in graph._unified_to_names.get(u, []), (
                f"[{language}] vertex {v['name']} (unified={u!r}) missing "
                f"from _unified_to_names[{u!r}]"
            )


# ---------------------------------------------------------------------------
# Typed return shape (NodeRef / EdgeRef) + outgoing-default invariant
# ---------------------------------------------------------------------------


def test_query_range_returns_typed_records_on_real_repo(indexed_repo, language):
    """On a real SWE-bench graph, query_range returns NodeRef/EdgeRef records
    (not raw vid/eid ints) and outgoing default excludes CONTAIN edges."""
    g = indexed_repo

    # Pick a file with both nodes and anchored reference edges so the query
    # covers all three result lists.
    target_file = None
    for f in g._file_edge_anchors:
        if f in g._file_nodes:
            target_file = f
            break
    if target_file is None:
        pytest.skip(f"[{language}] no file has both nodes and anchored edges")

    # Query a window covering the densest stretch of anchors in the file.
    arr = g._file_edge_anchors[target_file]
    if not arr:
        pytest.skip(f"[{language}] no anchored edges in {target_file}")
    line_lo = arr[0][0]
    line_hi = arr[-1][0]

    res = g.query_range(target_file, line_lo, line_hi)

    # All defined entries are NodeRef
    for n in res.defined:
        assert isinstance(
            n, NodeRef
        ), f"[{language}] expected NodeRef, got {type(n).__name__}"
    # All edge entries are EdgeRef
    for e in res.outgoing:
        assert isinstance(
            e, EdgeRef
        ), f"[{language}] outgoing has {type(e).__name__}, expected EdgeRef"
    for e in res.incoming:
        assert isinstance(
            e, EdgeRef
        ), f"[{language}] incoming has {type(e).__name__}, expected EdgeRef"

    # Default `kinds` filters CONTAIN edges out of `outgoing`.
    for e in res.outgoing:
        assert (
            e.edge_kind != EDGE_TYPE_CONTAIN
        ), f"[{language}] CONTAIN edge leaked into outgoing default: {e}"


# ---------------------------------------------------------------------------
# query_range_by_symbol on a real repo
# ---------------------------------------------------------------------------


def test_query_range_by_symbol_returns_self_on_real_repo(indexed_repo, language):
    """Pick a real symbol from a real repo and verify query_range_by_symbol
    returns a result that includes that symbol in `defined`."""
    g = indexed_repo

    # Find any vertex with a real def range and known identity name.
    target_file = _pick_file_with_symbols(g)
    if target_file is None:
        pytest.skip(f"[{language}] no function-shaped symbol found")

    picked = _pick_symbol_node(g, target_file, min_span=3)
    if picked is None:
        pytest.skip(f"[{language}] no symbol with span >= 3 in {target_file}")

    s, e, vid = picked
    name = g.graph.vs[vid]["name"]

    res = g.query_range_by_symbol(name)
    defined_vids = [n.vid for n in res.defined]
    assert vid in defined_vids, (
        f"[{language}] {name} (vid={vid}) not in its own range query "
        f"(defined={defined_vids})"
    )


def test_query_range_by_symbol_unknown_is_empty(indexed_repo, language):
    """Unknown symbol returns an empty RangeQueryResult (does not raise)."""
    g = indexed_repo
    res = g.query_range_by_symbol("__definitely_not_a_real_symbol_42__")
    assert res.defined == [] and res.outgoing == [] and res.incoming == [], (
        f"[{language}] expected empty result, got "
        f"defined={res.defined}, outgoing={res.outgoing}, incoming={res.incoming}"
    )


# ---------------------------------------------------------------------------
# Anchor invariant ii — CONTAIN edges carry no anchor on a real repo
# ---------------------------------------------------------------------------


def test_incoming_default_is_reference_on_real_repo(indexed_repo, language):
    """On a real graph, incoming default kinds excludes CONTAIN — pick a node
    with known CONTAIN parents and verify default query excludes them."""
    graph = indexed_repo

    # Find a method-like node with a CONTAIN parent edge.
    target_vid = None
    for vid in range(graph.graph.vcount()):
        v = graph.graph.vs[vid]
        attrs = v.attributes()
        if attrs.get("type") not in ("method", "function"):
            continue
        f = attrs.get("file")
        s = attrs.get("start_line")
        e = attrs.get("end_line")
        if f is None or s is None or e is None:
            continue
        # Verify it has at least one inbound CONTAIN edge.
        in_eids = graph.graph.incident(vid, mode="in")
        if any(graph.graph.es[eid]["type"] == EDGE_TYPE_CONTAIN for eid in in_eids):
            target_vid = vid
            break

    if target_vid is None:
        pytest.skip(f"[{language}] no method/function with CONTAIN parent found")

    v = graph.graph.vs[target_vid]
    res = graph.query_range(
        v.attributes()["file"],
        v.attributes()["start_line"],
        v.attributes()["end_line"],
    )

    # Default kinds: incoming must contain ZERO contain edges (the parent
    # CONTAIN that exists structurally is filtered out).
    in_kinds = {e.edge_kind for e in res.incoming}
    assert EDGE_TYPE_CONTAIN not in in_kinds, (
        f"[{language}] incoming default leaked CONTAIN edge into query result: "
        f"kinds={in_kinds}"
    )


def test_contain_edges_have_no_anchor_on_real_repo(indexed_repo, language):
    """Every CONTAIN edge in a real per-language graph must have
    anchor_file=None and anchor_line=None (anchor invariant ii)."""
    g = indexed_repo
    contain_count = 0
    leaked = []
    for e in g.graph.es:
        if e["type"] != EDGE_TYPE_CONTAIN:
            continue
        contain_count += 1
        attrs = e.attributes()
        if attrs.get("anchor_file") is not None or attrs.get("anchor_line") is not None:
            leaked.append(
                (e.source, e.target, attrs.get("anchor_file"), attrs.get("anchor_line"))
            )
            if len(leaked) >= 5:
                break
    assert contain_count > 0, f"[{language}] graph has no CONTAIN edges"
    assert (
        not leaked
    ), f"[{language}] CONTAIN edges leaked anchor data (first 5): {leaked}"
