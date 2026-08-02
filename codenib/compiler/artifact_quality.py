# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Commit-surface and completeness gates for reusable benchmark artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .. import compat_pickle
from ..git_snapshot import GitSourceSurface, normalize_repository_path
from ..graph.code_graph import CodeGraph
from ..types import NODE_TYPE_DIRECTORY, NODE_TYPE_FILE, ROOT_NODE, is_symbol_node

ARTIFACT_QUALITY_SCHEMA_VERSION = 1


def required_source_files(
    instance: Mapping[str, Any],
    extensions: Iterable[str],
) -> tuple[str, ...]:
    """Return GT target files within one backend's source-language surface."""

    accepted = frozenset(extensions)
    required = set()
    for raw_path in instance.get("gt_target_files") or ():
        path = normalize_repository_path(str(raw_path))
        if Path(path).suffix in accepted:
            required.add(path)
    return tuple(sorted(required))


def _path_or_none(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return normalize_repository_path(value)
    except ValueError:
        return None


def graph_source_paths(graph: CodeGraph) -> tuple[str, ...]:
    """Collect every repository path represented by a graph artifact."""

    paths: set[str] = set()
    for vertex in graph.graph.vs:
        attrs = vertex.attributes()
        node_type = attrs.get("type")
        candidate = (
            attrs.get("name") if node_type == NODE_TYPE_FILE else attrs.get("file")
        )
        path = _path_or_none(candidate)
        if path:
            paths.add(path)
    for edge in graph.graph.es:
        path = _path_or_none(edge.attributes().get("anchor_file"))
        if path:
            paths.add(path)
    occurrence_index = getattr(graph, "lsp_occurrence_index", None)
    if occurrence_index is not None:
        for occurrence in occurrence_index.occurrences:
            path = _path_or_none(occurrence.file_path)
            if path:
                paths.add(path)
    return tuple(sorted(paths))


def graph_file_paths(graph: CodeGraph) -> tuple[str, ...]:
    """Collect repository files materialized as graph file-view nodes."""

    paths = {
        path
        for vertex in graph.graph.vs
        if vertex.attributes().get("type") == NODE_TYPE_FILE
        and (path := _path_or_none(vertex.attributes().get("name"))) is not None
    }
    return tuple(sorted(paths))


def _keep_graph_vertex(attrs: Mapping[str, Any], surface: GitSourceSurface) -> bool:
    node_type = attrs.get("type")
    name = attrs.get("name")
    if node_type == "root" or name == ROOT_NODE:
        return True
    if node_type == NODE_TYPE_DIRECTORY:
        path = _path_or_none(name)
        return bool(path and surface.has_descendant(path))
    if node_type == NODE_TYPE_FILE:
        path = _path_or_none(name)
        return bool(path and surface.contains(path))
    if is_symbol_node(node_type):
        raw_path = attrs.get("file")
        path = _path_or_none(raw_path)
        # External/reference-only symbols may not have a repository definition.
        if path is None:
            return raw_path is None or raw_path == ""
        return surface.contains(path)
    raw_path = attrs.get("file")
    path = _path_or_none(raw_path)
    if path is None:
        return raw_path is None or raw_path == ""
    return surface.contains(path)


def constrain_graph_to_source_surface(
    graph: CodeGraph,
    surface: GitSourceSurface,
) -> dict[str, Any]:
    """Remove graph records not addressable by *surface* and rebuild indexes."""

    before_nodes = graph.graph.vcount()
    before_edges = graph.graph.ecount()
    before_paths = graph_source_paths(graph)
    before_classes = surface.classify(before_paths)

    keep_vertices = [
        vertex.index
        for vertex in graph.graph.vs
        if _keep_graph_vertex(vertex.attributes(), surface)
    ]
    if len(keep_vertices) != before_nodes:
        graph.graph = graph.graph.subgraph(keep_vertices)

    remove_edges = []
    for edge in graph.graph.es:
        raw_anchor = edge.attributes().get("anchor_file")
        anchor = _path_or_none(raw_anchor)
        if (raw_anchor is not None and raw_anchor != "" and anchor is None) or (
            anchor is not None and not surface.contains(anchor)
        ):
            remove_edges.append(edge.index)
    if remove_edges:
        graph.graph.delete_edges(remove_edges)

    graph.name_to_vertex = {
        vertex["name"]: vertex.index
        for vertex in graph.graph.vs
        if "name" in vertex.attributes()
    }
    graph.symbol_ranges = {
        name: value
        for name, value in graph.symbol_ranges.items()
        if name in graph.name_to_vertex
    }
    graph._invalidate_edge_index()

    occurrence_index = getattr(graph, "lsp_occurrence_index", None)
    if occurrence_index is not None:
        from ..scip_interface.lsp_occurrence_index import SCIPOccurrenceIndex

        graph.lsp_occurrence_index = SCIPOccurrenceIndex(
            (
                occurrence
                for occurrence in occurrence_index.occurrences
                if (path := _path_or_none(occurrence.file_path)) is not None
                and surface.contains(path)
            ),
            position_encoding=occurrence_index.position_encoding,
        )
    graph.build_range_indexes()

    after_paths = graph_source_paths(graph)
    after_classes = surface.classify(after_paths)
    return {
        "nodes_before": before_nodes,
        "nodes_after": graph.graph.vcount(),
        "edges_before": before_edges,
        "edges_after": graph.graph.ecount(),
        "paths_before": len(before_paths),
        "paths_after": len(after_paths),
        "tracked_paths": len(after_classes["tracked"]),
        "submodule_paths": len(after_classes["submodule"]),
        "removed_outside_paths": list(before_classes["outside"]),
        "outside_paths_after": list(after_classes["outside"]),
    }


def assess_graph_artifact(
    graph: CodeGraph,
    surface: GitSourceSurface,
    *,
    required_files: Sequence[str] = (),
) -> dict[str, Any]:
    """Constrain a graph and report commit identity plus required-file coverage."""

    surface_report = constrain_graph_to_source_surface(graph, surface)
    represented = set(graph_file_paths(graph))
    required = tuple(sorted(normalize_repository_path(path) for path in required_files))
    missing = tuple(path for path in required if path not in represented)
    failures = []
    if surface_report["outside_paths_after"]:
        failures.append("outside_commit_paths")
    if missing:
        failures.append("missing_required_source_files")
    if graph.graph.vcount() <= 1:
        failures.append("empty_graph")
    return {
        "schema_version": ARTIFACT_QUALITY_SCHEMA_VERSION,
        "kind": "graph",
        "commit": surface.commit,
        "tree": surface.tree,
        "required_source_files": list(required),
        "missing_required_source_files": list(missing),
        "surface": surface_report,
        "failure_names": failures,
        "passed": not failures,
    }


def _load_vector_level(
    root: Path,
    level: str,
    model_suffix: str,
) -> tuple[list[Any], int, list[str]]:
    failures = []
    level_root = root / level
    config_path = level_root / f"config_{model_suffix}.json"
    index_path = level_root / f"index_{model_suffix}.faiss"
    documents_path = level_root / f"documents_{model_suffix}.pkl"
    for path in (config_path, index_path, documents_path):
        if not path.is_file():
            failures.append(f"missing:{path.name}")
    if failures:
        return [], 0, failures

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        with documents_path.open("rb") as handle:
            documents = list(compat_pickle.load(handle))
        import faiss

        index = faiss.read_index(str(index_path))
    except Exception as exc:
        return [], 0, [f"unreadable_level:{type(exc).__name__}"]
    expected = int(config.get("num_documents", -1))
    if expected != len(documents):
        failures.append("document_config_count_mismatch")
    if int(index.ntotal) != len(documents):
        failures.append("faiss_document_count_mismatch")
    return documents, int(index.ntotal), failures


def assess_vector_artifact(
    root: str | Path,
    *,
    embedding_model: str,
    build_levels: Sequence[str],
    surface: GitSourceSurface,
    expected_artifact: Mapping[str, Any],
    required_l0_files: Sequence[str] = (),
) -> dict[str, Any]:
    """Validate vector files, provenance, commit paths, and L0 GT coverage."""

    artifact_root = Path(root)
    model_suffix = embedding_model.replace("/", "__")
    top_config_path = artifact_root / f"config_{model_suffix}.json"
    failures: list[str] = []
    top_config: Mapping[str, Any] = {}
    if not top_config_path.is_file():
        failures.append("missing_top_config")
    else:
        try:
            top_config = json.loads(top_config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            failures.append(f"unreadable_top_config:{type(exc).__name__}")
        else:
            if top_config.get("embedding_model") != embedding_model:
                failures.append("embedding_model_mismatch")
            if top_config.get("artifact") != dict(expected_artifact):
                failures.append("artifact_identity_mismatch")

    levels: dict[str, Any] = {}
    all_paths: set[str] = set()
    l0_paths: set[str] = set()
    invalid_document_paths: set[str] = set()
    for raw_level in build_levels:
        level = raw_level.lower()
        documents, vector_count, level_failures = _load_vector_level(
            artifact_root, level, model_suffix
        )
        paths = set()
        for document in documents:
            metadata = getattr(document, "metadata", {})
            raw_path = metadata.get("file")
            path = _path_or_none(raw_path)
            if path:
                paths.add(path)
            elif raw_path is not None and raw_path != "":
                invalid_document_paths.add(str(raw_path))
        all_paths.update(paths)
        if level == "l0":
            l0_paths.update(paths)
        if not documents:
            level_failures.append("empty_level")
        levels[level] = {
            "documents": len(documents),
            "vectors": vector_count,
            "paths": len(paths),
            "failures": level_failures,
        }
        failures.extend(f"{level}:{failure}" for failure in level_failures)

    classified = surface.classify(all_paths)
    if classified["outside"]:
        failures.append("outside_commit_paths")
    if invalid_document_paths:
        failures.append("invalid_document_paths")
    required = tuple(
        sorted(normalize_repository_path(path) for path in required_l0_files)
    )
    missing = tuple(path for path in required if path not in l0_paths)
    if missing:
        failures.append("missing_required_l0_files")
    return {
        "schema_version": ARTIFACT_QUALITY_SCHEMA_VERSION,
        "kind": "vector",
        "artifact": dict(expected_artifact),
        "commit": surface.commit,
        "tree": surface.tree,
        "levels": levels,
        "paths": {
            "tracked": len(classified["tracked"]),
            "submodule": len(classified["submodule"]),
            "outside": list(classified["outside"]),
            "invalid": sorted(invalid_document_paths),
        },
        "required_l0_files": list(required),
        "missing_required_l0_files": list(missing),
        "failure_names": sorted(set(failures)),
        "passed": not failures,
    }


def write_artifact_quality(path: str | Path, report: Mapping[str, Any]) -> None:
    """Atomically publish a human-readable artifact quality report."""

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)


__all__ = [
    "ARTIFACT_QUALITY_SCHEMA_VERSION",
    "assess_graph_artifact",
    "assess_vector_artifact",
    "constrain_graph_to_source_surface",
    "graph_file_paths",
    "graph_source_paths",
    "required_source_files",
    "write_artifact_quality",
]
