# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Stage pre-built per-instance indexes for agent-runner evaluation.

The offline embedding pipeline writes one directory per instance under a
prebuilt tree such as ``<prebuilt-root>/<instance_id>/`` with a flat layout::

    <instance_id>/repo/                          # source @ base_commit
    <instance_id>/graph.pkl                      # symbol graph (CodeGraph)
    <instance_id>/config_<model>.json            # vector-store top-level config
    <instance_id>/l0/index_<model>.faiss + documents_<model>.pkl + config
    <instance_id>/l2/index_<model>.faiss + documents_<model>.pkl + config

``build_skill_contexts`` instead expects a ``<cache_dir>/<index_type>/``
layout (``bm25/``, ``vector/``, ``symbol_graph/``). This module bridges the
two by staging a per-instance cache directory:

* ``symbol_graph/graph.pkl`` — current-schema graph file staged from the
  prebuilt ``graph.pkl`` (legacy prebuilt bundles are normalized here)
* ``vector`` — symlink to the instance dir (``CodeVectorStore.load`` reads
  its top-level ``config_<model>.json`` plus ``l0/`` and ``l2/``)
* ``bm25/`` — built fresh from the prebuilt graph (pure-Python, no model,
  sub-second), since BM25 is not part of the prebuilt artifact set

After staging, ``build_skill_contexts(cache_dir=staged, rebuild=False)``
finds every requested index present and loads — never rebuilds — them. The
embedding model only loads inside ``CodeVectorStore`` when a subset actually
requires the vector index.
"""

from __future__ import annotations

import os
import pickle
from typing import Any, Optional


def model_suffix(embedding_model: str) -> str:
    """``CodeVectorStore`` filename suffix for an embedding model id."""
    return embedding_model.replace("/", "__")


def instance_dir(prebuilt_root: str, instance_id: str) -> str:
    return os.path.join(os.path.abspath(prebuilt_root), instance_id)


def repo_path_for(prebuilt_root: str, instance_id: str) -> str:
    return os.path.join(instance_dir(prebuilt_root, instance_id), "repo")


def has_full_indexes(
    prebuilt_root: str, instance_id: str, embedding_model: str
) -> bool:
    """True if the instance has repo + graph + (l2) vector for *embedding_model*."""
    d = instance_dir(prebuilt_root, instance_id)
    suffix = model_suffix(embedding_model)
    required = [
        "repo",
        "graph.pkl",
        os.path.join("l2", f"index_{suffix}.faiss"),
        os.path.join("l2", f"documents_{suffix}.pkl"),
    ]
    return all(os.path.exists(os.path.join(d, r)) for r in required)


def _symlink(target: str, link: str) -> None:
    """Create ``link`` -> ``target`` idempotently."""
    if os.path.islink(link) or os.path.exists(link):
        return
    os.makedirs(os.path.dirname(link), exist_ok=True)
    os.symlink(target, link)


def load_prebuilt_code_graph(prebuilt_root: str, instance_id: str):
    """Load ``<prebuilt_root>/<instance_id>/graph.pkl`` as a ``CodeGraph``.

    The prebuilt corpus is older than the current on-disk graph cache schema for
    many instances: those pickles contain the historical flat bundle
    ``{"graph", "name_to_vertex", "symbol_ranges", "project_root"}`` with no
    ``schema_version``. Keep ``CodeGraph.load_graph`` strict for ordinary
    caches, but accept that legacy bundle here because this module's contract is
    explicitly to consume the external prebuilt tree.
    """
    from codeminer.graph.code_graph import _SCHEMA_VERSION, CodeGraph

    path = os.path.join(instance_dir(prebuilt_root, instance_id), "graph.pkl")
    try:
        return CodeGraph.load_graph(path)
    except (AttributeError, ValueError):
        pass

    with open(path, "rb") as f:
        data = pickle.load(f)

    if isinstance(data, CodeGraph):
        graph = data
    elif isinstance(data, dict) and data.get("graph") is not None:
        schema_version = data.get("schema_version")
        if schema_version not in (None, _SCHEMA_VERSION):
            raise ValueError(
                f"prebuilt graph.pkl at {path} has unsupported "
                f"schema_version={schema_version!r}"
            )
        graph = CodeGraph(project_root=data.get("project_root"))
        graph.graph = data["graph"]
        graph.symbol_ranges = data.get("symbol_ranges", {})
        graph.name_to_vertex = data.get("name_to_vertex") or _name_to_vertex(
            graph.graph
        )
        graph._file_nodes = data.get("file_nodes", {})
        graph._file_edge_anchors = data.get("file_edge_anchors", {})
        graph._unified_to_names = data.get("unified_to_names", {})
    else:
        raise ValueError(f"prebuilt graph.pkl at {path} is not a CodeGraph bundle")

    if not graph.name_to_vertex:
        graph.name_to_vertex = _name_to_vertex(graph.graph)
    if not graph._file_nodes or not graph._unified_to_names:
        graph.build_range_indexes()
    return graph


def _name_to_vertex(igraph_obj: Any) -> dict:
    out = {}
    for v in igraph_obj.vs:
        name = v.attributes().get("name")
        if name:
            out[name] = v.index
    return out


def _stage_symbol_graph(src: str, dst: str, code_graph: Optional[object]) -> object:
    """Ensure ``dst`` is loadable by the current strict ``CodeGraph`` loader."""
    from codeminer.graph.code_graph import CodeGraph

    if os.path.exists(dst) or os.path.islink(dst):
        try:
            return CodeGraph.load_graph(dst)
        except Exception:
            os.unlink(dst)

    graph = code_graph
    if graph is None:
        instance_id = os.path.basename(os.path.dirname(src))
        prebuilt_root = os.path.dirname(os.path.dirname(src))
        graph = load_prebuilt_code_graph(prebuilt_root, instance_id)

    os.makedirs(os.path.dirname(dst), exist_ok=True)
    graph.save_graph(dst)
    return graph


def stage_prebuilt_indexes(
    prebuilt_root: str,
    instance_id: str,
    cache_dir: str,
    *,
    build_bm25: bool = True,
    code_graph: Optional[object] = None,
) -> str:
    """Stage prebuilt vector + symbol graph (and build BM25) under *cache_dir*.

    Returns ``cache_dir``. Idempotent: re-staging an already-staged cache is
    a no-op except that BM25 is rebuilt only when its index dir is empty.
    """
    inst = instance_dir(prebuilt_root, instance_id)
    if not os.path.isdir(inst):
        raise FileNotFoundError(f"prebuilt instance dir missing: {inst}")

    os.makedirs(cache_dir, exist_ok=True)

    graph_src = os.path.join(inst, "graph.pkl")
    graph_dst = os.path.join(cache_dir, "symbol_graph", "graph.pkl")
    graph = None
    if build_bm25:
        graph = _stage_symbol_graph(graph_src, graph_dst, code_graph)
    else:
        # Lightweight staging for tests / callers that only need the path shape.
        _symlink(graph_src, graph_dst)

    # vector -> the instance dir itself (CodeVectorStore.load scans l0/l2)
    _symlink(inst, os.path.join(cache_dir, "vector"))

    # bm25/ — build fresh from the prebuilt graph (no embedding model needed)
    bm25_dir = os.path.join(cache_dir, "bm25")
    if build_bm25 and not (os.path.isdir(bm25_dir) and os.listdir(bm25_dir)):
        from codeminer.index.sparse_idx.bm25_index import BM25CodeIndexer

        graph = (
            graph or code_graph or load_prebuilt_code_graph(prebuilt_root, instance_id)
        )
        os.makedirs(bm25_dir, exist_ok=True)
        BM25CodeIndexer(code_graph=graph).save_index(bm25_dir)

    return cache_dir
