# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Reuse pre-built per-instance indexes for the agent-compile sample sweep.

The offline embedding pipeline writes one directory per instance under a
prebuilt tree (default ``/mnt/data/codeminer/<instance_id>/``) with a flat
layout::

    <instance_id>/repo/                          # source @ base_commit
    <instance_id>/graph.pkl                      # symbol graph (CodeGraph)
    <instance_id>/config_<model>.json            # vector-store top-level config
    <instance_id>/l0/index_<model>.faiss + documents_<model>.pkl + config
    <instance_id>/l2/index_<model>.faiss + documents_<model>.pkl + config

``build_skill_contexts`` instead expects a ``<cache_dir>/<index_type>/``
layout (``bm25/``, ``vector/``, ``symbol_graph/``). This module bridges the
two by staging a per-instance cache directory:

* ``symbol_graph/graph.pkl`` — symlink to the prebuilt ``graph.pkl``
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
from typing import Optional


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

    # symbol_graph/graph.pkl -> prebuilt graph.pkl
    _symlink(
        os.path.join(inst, "graph.pkl"),
        os.path.join(cache_dir, "symbol_graph", "graph.pkl"),
    )

    # vector -> the instance dir itself (CodeVectorStore.load scans l0/l2)
    _symlink(inst, os.path.join(cache_dir, "vector"))

    # bm25/ — build fresh from the prebuilt graph (no embedding model needed)
    bm25_dir = os.path.join(cache_dir, "bm25")
    if build_bm25 and not (os.path.isdir(bm25_dir) and os.listdir(bm25_dir)):
        from codeminer.graph.code_graph import CodeGraph
        from codeminer.index.sparse_idx.bm25_index import BM25CodeIndexer

        graph = code_graph or CodeGraph.load_graph(os.path.join(inst, "graph.pkl"))
        os.makedirs(bm25_dir, exist_ok=True)
        BM25CodeIndexer(code_graph=graph).save_index(bm25_dir)

    return cache_dir
