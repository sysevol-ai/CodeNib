# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
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

import argparse
import json
import os
import shutil
import sys
import tempfile
from typing import Any, Iterable, Optional

from codenib import compat_pickle

# External experiment artifacts were written with schema 3 before exact SCIP
# declaration lines were persisted. They remain valid for retrieval and graph
# navigation; LSP consumers fall back to the scope start when selection_line is
# absent. Ordinary CodeGraph caches stay strict.
_LEGACY_PREBUILT_SCHEMA_VERSIONS = frozenset({None, 3})


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


def has_required_indexes(
    prebuilt_root: str,
    instance_id: str,
    embedding_model: str,
    index_types: Iterable[str],
) -> bool:
    """Return whether a bundle can satisfy the declared index requirements.

    The repository snapshot is always required because default file tools run
    against it. BM25 may be built from that snapshot at load time, while vector
    and symbol-graph requirements must have their durable artifacts present.
    """

    required = set(index_types)
    instance = instance_dir(prebuilt_root, instance_id)
    paths = [os.path.join(instance, "repo")]
    if "symbol_graph" in required:
        paths.append(os.path.join(instance, "graph.pkl"))
    if "vector" in required:
        suffix = model_suffix(embedding_model)
        paths.extend(
            [
                os.path.join(instance, "l2", f"index_{suffix}.faiss"),
                os.path.join(instance, "l2", f"documents_{suffix}.pkl"),
            ]
        )
    return all(os.path.exists(path) for path in paths)


def _symlink(target: str, link: str) -> None:
    """Create ``link`` -> ``target`` idempotently."""
    if os.path.islink(link) or os.path.exists(link):
        return
    os.makedirs(os.path.dirname(link), exist_ok=True)
    os.symlink(target, link)


def load_code_graph_artifact(path: str, *, project_root: Optional[str] = None):
    """Load a current or legacy ``graph.pkl`` artifact as a ``CodeGraph``.

    Ordinary ``CodeGraph.load_graph`` stays strict for local caches. External
    prebuilt corpora can contain older flat bundles or direct ``CodeGraph``
    pickles, and their persisted ``project_root`` often points at the machine
    that built the artifact. This helper accepts those artifact shapes and
    optionally rebinds the graph to the current repo snapshot.
    """
    from codenib.graph.code_graph import _SCHEMA_VERSION, CodeGraph

    try:
        graph = CodeGraph.load_graph(path)
    except (AttributeError, ValueError):
        graph = None
    else:
        return _normalize_loaded_graph(graph, project_root=project_root)

    with open(path, "rb") as f:
        data = compat_pickle.load(f)

    if isinstance(data, CodeGraph):
        graph = data
    elif isinstance(data, dict) and data.get("graph") is not None:
        schema_version = data.get("schema_version")
        if schema_version not in {
            *_LEGACY_PREBUILT_SCHEMA_VERSIONS,
            _SCHEMA_VERSION,
        }:
            raise ValueError(
                f"prebuilt graph.pkl at {path} has unsupported "
                f"schema_version={schema_version!r}"
            )
        graph = CodeGraph(project_root=project_root or data.get("project_root"))
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

    return _normalize_loaded_graph(graph, project_root=project_root)


def load_prebuilt_code_graph(prebuilt_root: str, instance_id: str):
    """Load ``<prebuilt_root>/<instance_id>/graph.pkl`` as a ``CodeGraph``.

    The prebuilt corpus is older than the current on-disk graph cache schema for
    many instances: those pickles contain the historical flat bundle
    ``{"graph", "name_to_vertex", "symbol_ranges", "project_root"}`` with no
    ``schema_version``. Keep ``CodeGraph.load_graph`` strict for ordinary
    caches, but accept that legacy bundle here because this module's contract is
    explicitly to consume the external prebuilt tree.
    """
    path = os.path.join(instance_dir(prebuilt_root, instance_id), "graph.pkl")
    return load_code_graph_artifact(
        path,
        project_root=repo_path_for(prebuilt_root, instance_id),
    )


def _normalize_loaded_graph(graph: Any, *, project_root: Optional[str]):
    if project_root:
        graph.project_root = os.path.abspath(project_root)
    if not graph.name_to_vertex:
        graph.name_to_vertex = _name_to_vertex(graph.graph)
    if not graph._file_nodes or not graph._unified_to_names:
        graph.build_range_indexes()
    return graph


def graph_artifact_metadata(path: str) -> dict[str, Any]:
    """Return lightweight metadata for a graph pickle without normalizing it."""

    with open(path, "rb") as f:
        data = compat_pickle.load(f)
    if isinstance(data, dict):
        return {
            "shape": "bundle",
            "schema_version": data.get("schema_version"),
            "project_root": data.get("project_root"),
        }
    return {
        "shape": type(data).__name__,
        "schema_version": None,
        "project_root": getattr(data, "project_root", None),
    }


def normalize_prebuilt_graphs(
    prebuilt_root: str,
    *,
    instance_ids: Optional[list[str]] = None,
    limit: Optional[int] = None,
    write: bool = False,
    backup_suffix: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Normalize prebuilt graph pickles to the current schema and repo root.

    Dry-run by default. When ``write`` is true, each stale ``graph.pkl`` is
    re-serialized with ``CodeGraph.save_graph`` so strict loaders can read it
    directly and ``project_root`` points at ``<prebuilt_root>/<instance>/repo``.
    """

    from codenib.graph.code_graph import _SCHEMA_VERSION

    root = os.path.abspath(prebuilt_root)
    selected = instance_ids or _prebuilt_instance_ids(root)
    if limit is not None:
        selected = selected[: max(0, int(limit))]

    rows: list[dict[str, Any]] = []
    for instance_id in selected:
        graph_path = os.path.join(instance_dir(root, instance_id), "graph.pkl")
        repo_root = repo_path_for(root, instance_id)
        row: dict[str, Any] = {
            "instance_id": instance_id,
            "graph_path": graph_path,
            "repo_root": repo_root,
            "write": bool(write),
        }
        if not os.path.exists(graph_path):
            row["status"] = "missing_graph"
            rows.append(row)
            continue

        try:
            before = graph_artifact_metadata(graph_path)
            expected_root = os.path.abspath(repo_root)
            actual_root = (
                os.path.abspath(before["project_root"])
                if before.get("project_root")
                else None
            )
            needs_update = (
                before.get("schema_version") != _SCHEMA_VERSION
                or actual_root != expected_root
            )
            row.update(
                {
                    "before_schema_version": before.get("schema_version"),
                    "before_project_root": before.get("project_root"),
                    "needs_update": needs_update,
                }
            )
            if not needs_update:
                row["status"] = "current"
                rows.append(row)
                continue
            if not write:
                row["status"] = "would_update"
                rows.append(row)
                continue

            _rewrite_prebuilt_graph(
                graph_path,
                project_root=repo_root,
                backup_suffix=backup_suffix,
            )
            after = graph_artifact_metadata(graph_path)
            row.update(
                {
                    "after_schema_version": after.get("schema_version"),
                    "after_project_root": after.get("project_root"),
                    "status": "updated",
                }
            )
        except Exception as exc:  # noqa: BLE001 - batch audit should continue.
            row.update({"status": "error", "error": f"{type(exc).__name__}: {exc}"})
        rows.append(row)
    return rows


def _prebuilt_instance_ids(prebuilt_root: str) -> list[str]:
    return sorted(
        name
        for name in os.listdir(prebuilt_root)
        if os.path.exists(os.path.join(prebuilt_root, name, "graph.pkl"))
    )


def _rewrite_prebuilt_graph(
    graph_path: str,
    *,
    project_root: str,
    backup_suffix: Optional[str],
) -> None:
    graph = load_code_graph_artifact(graph_path, project_root=project_root)
    if backup_suffix:
        backup_path = graph_path + backup_suffix
        if not os.path.exists(backup_path):
            shutil.copy2(graph_path, backup_path)

    directory = os.path.dirname(graph_path)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".graph.",
        suffix=".pkl.tmp",
        dir=directory,
    )
    os.close(fd)
    try:
        graph.save_graph(tmp_path)
        os.replace(tmp_path, graph_path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _name_to_vertex(igraph_obj: Any) -> dict:
    out = {}
    for v in igraph_obj.vs:
        name = v.attributes().get("name")
        if name:
            out[name] = v.index
    return out


def _stage_symbol_graph(src: str, dst: str, code_graph: Optional[object]) -> object:
    """Ensure ``dst`` is loadable by the current strict ``CodeGraph`` loader."""
    from codenib.graph.code_graph import CodeGraph

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
    required_index_types: Optional[Iterable[str]] = None,
) -> str:
    """Stage declared durable indexes under *cache_dir*.

    Returns ``cache_dir``. Idempotent: re-staging an already-staged cache is
    a no-op except that BM25 is built only when requested and absent. Omitting
    ``required_index_types`` preserves the historical graph+vector+BM25 path.
    """
    inst = instance_dir(prebuilt_root, instance_id)
    if not os.path.isdir(inst):
        raise FileNotFoundError(f"prebuilt instance dir missing: {inst}")

    os.makedirs(cache_dir, exist_ok=True)

    legacy_all = required_index_types is None
    required = (
        {"bm25", "symbol_graph", "vector"}
        if legacy_all
        else set(required_index_types or ())
    )
    graph_src = os.path.join(inst, "graph.pkl")
    graph_dst = os.path.join(cache_dir, "symbol_graph", "graph.pkl")
    graph = None
    graph_available = os.path.isfile(graph_src) or code_graph is not None
    should_stage_graph = "symbol_graph" in required or (
        "bm25" in required and build_bm25 and graph_available
    )
    if should_stage_graph and build_bm25:
        graph = _stage_symbol_graph(graph_src, graph_dst, code_graph)
    elif should_stage_graph or legacy_all:
        # Lightweight staging for tests / callers that only need the path shape.
        _symlink(graph_src, graph_dst)

    # vector -> the instance dir itself (CodeVectorStore.load scans l0/l2)
    if "vector" in required:
        _symlink(inst, os.path.join(cache_dir, "vector"))

    # bm25/ — build fresh from the prebuilt graph (no embedding model needed)
    bm25_dir = os.path.join(cache_dir, "bm25")
    if (
        "bm25" in required
        and build_bm25
        and graph_available
        and not (os.path.isdir(bm25_dir) and os.listdir(bm25_dir))
    ):
        from codenib.index.sparse_idx.bm25_index import BM25CodeIndexer

        graph = (
            graph or code_graph or load_prebuilt_code_graph(prebuilt_root, instance_id)
        )
        os.makedirs(bm25_dir, exist_ok=True)
        BM25CodeIndexer(code_graph=graph).save_index(bm25_dir)

    return cache_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Normalize prebuilt graph.pkl artifacts to the current schema."
    )
    parser.add_argument("prebuilt_root", help="Root containing instance directories")
    parser.add_argument(
        "--instance-id",
        action="append",
        dest="instance_ids",
        help="Instance to normalize. Repeatable. Defaults to every graph.pkl.",
    )
    parser.add_argument(
        "--instances-file",
        help="Optional newline-delimited instance id file.",
    )
    parser.add_argument("--limit", type=int, help="Limit instances after sorting")
    parser.add_argument(
        "--write",
        action="store_true",
        help="Rewrite graph.pkl files. Omit for dry-run audit.",
    )
    parser.add_argument(
        "--backup-suffix",
        help="Optional suffix for one-time backups, for example .legacy.",
    )
    parser.add_argument("--output-json", help="Write per-instance JSON report")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    instance_ids = list(args.instance_ids or [])
    if args.instances_file:
        with open(args.instances_file, encoding="utf-8") as f:
            instance_ids.extend(line.strip() for line in f if line.strip())
    rows = normalize_prebuilt_graphs(
        args.prebuilt_root,
        instance_ids=instance_ids or None,
        limit=args.limit,
        write=args.write,
        backup_suffix=args.backup_suffix,
    )
    summary = _normalize_summary(rows)
    payload = {"summary": summary, "rows": rows}
    if args.output_json:
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
    sys.stdout.write(json.dumps(summary, sort_keys=True) + "\n")
    return 1 if summary.get("error", 0) else 0


def _normalize_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {"total": len(rows)}
    for row in rows:
        status = str(row.get("status") or "unknown")
        out[status] = out.get(status, 0) + 1
    return out


if __name__ == "__main__":
    raise SystemExit(main())
