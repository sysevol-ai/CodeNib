"""Unit tests for BM25 indexed-text contract on non-Python decoders.

The decoders attach two name attributes per symbol vertex:
- `name`     — identity, semi-raw SCIP for rust/ts/go, USR for clangd, readable
               only on Python. Required by `name_to_vertex` for ROISubgraph
               seed lookup, so the BM25 index must keep returning it.
- `unified_name` — display form, always `file_path:SymbolDisplay` across
               decoders. Cleanly tokenizable.

The indexer used to tokenize raw `name`, which silently degraded recall on
non-Python repos. This file pins the contract: indexed text comes from
`unified_name` when present (so behavioral / readable queries hit it), but
the returned `node_name` stays the raw `name` (graph identity preserved).
"""

from __future__ import annotations

from codeminer.graph.code_graph import CodeGraph
from codeminer.index import BM25CodeIndexer
from codeminer.types import NODE_TYPE_METHOD


def _build_rust_like_graph() -> CodeGraph:
    """Synthetic graph with rust-analyzer-style raw `name` + readable
    `unified_name`. Mirrors what SCIPRustGraphDecoder produces."""
    g = CodeGraph(project_root="/tmp/fake")
    g.add_file_node("src/parser.rs")
    raw_name = (
        "rust-analyzer cargo my_crate 0.1.0 src/`parser.rs`/Parser#parse_quotes()."
    )
    g._add_vertex(
        raw_name,
        {
            "type": NODE_TYPE_METHOD,
            "file": "src/parser.rs",
            "start_line": 10,
            "end_line": 30,
            "unified_name": "src/parser.rs:Parser.parse_quotes",
        },
    )
    return g


def _build_clangd_like_graph() -> CodeGraph:
    """Synthetic graph with clangd-USR-style raw `name`."""
    g = CodeGraph(project_root="/tmp/fake")
    g.add_file_node("src/vector.cpp")
    raw_name = "c:@N@std@C@vector@F@push_back#"
    g._add_vertex(
        raw_name,
        {
            "type": NODE_TYPE_METHOD,
            "file": "src/vector.cpp",
            "start_line": 100,
            "end_line": 120,
            "unified_name": "src/vector.cpp:vector.push_back",
        },
    )
    return g


def _build_no_unified_name_graph() -> CodeGraph:
    """Graph with a vertex that has no unified_name (reference-only edge case
    where the decoder never set it). Indexer must fall back to raw `name`."""
    g = CodeGraph(project_root="/tmp/fake")
    g.add_file_node("a.py")
    g._add_vertex(
        "a.py:legacy_helper",
        {
            "type": NODE_TYPE_METHOD,
            "file": "a.py",
            "start_line": 1,
            "end_line": 5,
        },
    )
    return g


def _vertex_doc(indexer: BM25CodeIndexer, raw_name: str):
    """Return the langchain Document for a given raw `name`."""
    for d in indexer.documents:
        if d.metadata.get("name") == raw_name:
            return d
    return None


def test_indexed_text_uses_unified_name_when_present():
    """Tokens fed to BM25 should come from the readable `unified_name`,
    not the raw SCIP-encoded `name`. Otherwise rust/ts/go/clangd recall
    silently collapses on behavioral queries."""
    g = _build_rust_like_graph()
    indexer = BM25CodeIndexer(code_graph=g)

    raw = "rust-analyzer cargo my_crate 0.1.0 src/`parser.rs`/Parser#parse_quotes()."
    doc = _vertex_doc(indexer, raw)
    assert doc is not None, "vertex did not produce a document"

    content = doc.page_content
    # Tokens from the readable form must be present.
    assert "parser" in content
    assert "parse" in content
    assert "quotes" in content
    # Tokens that only exist in the raw SCIP form must NOT be (they would
    # mean we tokenized the unreadable string).
    assert "cargo" not in content
    assert "rust" not in content


def test_doc_id_and_name_metadata_stay_raw():
    """Identity metadata must stay the raw `name` so the downstream
    `name_to_vertex` lookup in ROISubgraph / graph_expand still works."""
    g = _build_rust_like_graph()
    indexer = BM25CodeIndexer(code_graph=g)

    raw = "rust-analyzer cargo my_crate 0.1.0 src/`parser.rs`/Parser#parse_quotes()."
    doc = _vertex_doc(indexer, raw)
    assert doc is not None
    assert doc.metadata["name"] == raw
    assert doc.metadata["node_id"] == raw


def test_search_returns_raw_name():
    """Queries against the readable form must succeed AND the returned
    `node_name` must be the raw graph identity (not unified_name)."""
    g = _build_rust_like_graph()
    indexer = BM25CodeIndexer(code_graph=g, max_k=5)

    results = indexer.search("parse_quotes", top_k=5)
    assert results, "query against readable form returned nothing"

    raw = "rust-analyzer cargo my_crate 0.1.0 src/`parser.rs`/Parser#parse_quotes()."
    assert results[0].node_name == raw
    assert results[0].node_id == raw


def test_clangd_usr_indexed_text():
    """clangd USRs are particularly bad: `c:@N@std@C@vector@F@push_back#`
    tokenizes to garbage. unified_name path fixes this."""
    g = _build_clangd_like_graph()
    indexer = BM25CodeIndexer(code_graph=g)

    raw = "c:@N@std@C@vector@F@push_back#"
    doc = _vertex_doc(indexer, raw)
    assert doc is not None

    content = doc.page_content
    assert "push" in content
    assert "back" in content
    assert "vector" in content


def test_falls_back_to_name_when_unified_name_missing():
    """Reference-only vertices may not have unified_name yet; indexer must
    fall back to raw `name` rather than crashing or skipping."""
    g = _build_no_unified_name_graph()
    indexer = BM25CodeIndexer(code_graph=g)

    raw = "a.py:legacy_helper"
    doc = _vertex_doc(indexer, raw)
    assert doc is not None
    content = doc.page_content
    assert "legacy" in content
    assert "helper" in content
