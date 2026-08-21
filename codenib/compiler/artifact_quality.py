# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Commit-surface and completeness gates for reusable benchmark artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .. import compat_pickle
from .._captured_directory import AuthenticatedSnapshotReader
from ..git_snapshot import GitSourceSurface, normalize_repository_path
from ..graph.code_graph import CodeGraph
from ..index.embedding.artifact_integrity import AuthenticatedVectorView
from ..native_index_authorization import (
    NativeIndexAuthorization,
    require_native_index_authorization,
    require_native_index_authorization_preflight,
)
from ..repository_filters import repository_path_is_visible
from ..repository_source_selection import RepositorySourceSelection
from ..types import (
    NODE_TYPE_DIRECTORY,
    NODE_TYPE_FILE,
    ROOT_NODE,
    is_symbol_node,
    node_has_definition,
)

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


def _graph_paths_and_invalid(
    graph: CodeGraph,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    paths: set[str] = set()
    invalid: set[str] = set()

    def collect(value: object, owner: str, *, required: bool = False) -> None:
        if value is None or value == "":
            if required:
                invalid.add(f"{owner}={value!r}")
            return
        path = _path_or_none(value)
        if path is None:
            invalid.add(f"{owner}={value!r}")
        else:
            paths.add(path)

    for vertex in graph.graph.vs:
        attrs = vertex.attributes()
        node_type = attrs.get("type")
        if node_type == NODE_TYPE_DIRECTORY:
            value = attrs.get("name")
            if _path_or_none(value) is None:
                invalid.add(f"vertex[{vertex.index}].name={value!r}")
        elif node_type == NODE_TYPE_FILE:
            collect(
                attrs.get("name"),
                f"vertex[{vertex.index}].name",
                required=True,
            )
        else:
            collect(attrs.get("file"), f"vertex[{vertex.index}].file")
    for edge in graph.graph.es:
        collect(edge.attributes().get("anchor_file"), f"edge[{edge.index}].anchor_file")
    occurrence_index = getattr(graph, "lsp_occurrence_index", None)
    if occurrence_index is not None:
        for index, occurrence in enumerate(occurrence_index.occurrences):
            collect(
                occurrence.file_path,
                f"occurrence[{index}].file_path",
                required=True,
            )
    return tuple(sorted(paths)), tuple(sorted(invalid))


def graph_source_paths(graph: CodeGraph) -> tuple[str, ...]:
    """Collect every repository path represented by a graph artifact."""

    paths, _invalid = _graph_paths_and_invalid(graph)
    return paths


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
    before_paths, invalid_paths_before = _graph_paths_and_invalid(graph)
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
        name: vertex.index
        for vertex in graph.graph.vs
        if isinstance((name := vertex.attributes().get("name")), str) and name
    }
    graph.symbol_ranges = {
        name: value
        for name, value in graph.symbol_ranges.items()
        if name in graph.name_to_vertex
    }
    graph.invalidate_caches()

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

    after_paths, invalid_paths_after = _graph_paths_and_invalid(graph)
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
        "invalid_paths_before": list(invalid_paths_before),
        "invalid_paths_after": list(invalid_paths_after),
    }


def _canonical_selected_path(
    value: object,
    source_selection: RepositorySourceSelection,
) -> str | None:
    """Return one canonical selected repository path, or ``None``.

    Source-selection identities are defined over canonical POSIX paths. The
    Git-surface normalizer intentionally accepts ``./`` and backslashes for
    older artifacts, but a newly published selected graph must not retain
    those aliases because the selection policy cannot authenticate them.
    """

    if not isinstance(value, str) or not value:
        return None
    try:
        normalized = normalize_repository_path(value)
        if normalized != value or not repository_path_is_visible(
            normalized,
            selection=source_selection,
        ):
            return None
    except (TypeError, ValueError):
        return None
    return normalized


def _selection_path_state(
    value: object,
    source_selection: RepositorySourceSelection,
) -> tuple[str | None, str]:
    """Classify a repository path as selected, excluded, absent, or invalid."""

    if value is None or value == "":
        return None, "absent"
    if not isinstance(value, str):
        return None, "invalid"
    try:
        normalized = normalize_repository_path(value)
    except ValueError:
        return None, "invalid"
    if normalized != value:
        return normalized, "invalid"
    try:
        return normalized, (
            "selected"
            if repository_path_is_visible(normalized, selection=source_selection)
            else "excluded"
        )
    except (TypeError, ValueError):
        return normalized, "invalid"


def constrain_graph_to_source_selection(
    graph: CodeGraph,
    source_selection: RepositorySourceSelection,
) -> dict[str, Any]:
    """Enforce an exact source selection over every repository graph record.

    Backend exclusion patterns are only an optimization. This function is the
    publication gate: it removes excluded or non-canonical file/symbol nodes,
    anchored edges, and SCIP/LSP occurrences, prunes directories without a
    selected represented descendant, and rebuilds all derived graph indexes.
    External reference-only symbols without a repository path remain valid.
    """

    if type(source_selection) is not RepositorySourceSelection:
        raise TypeError("source_selection must be a RepositorySourceSelection")
    selection = RepositorySourceSelection(source_selection.exclude_subtrees)

    before_nodes = graph.graph.vcount()
    before_edges = graph.graph.ecount()
    excluded_before: set[str] = set()
    invalid_before: set[str] = set()
    selected_paths: set[str] = set()

    def observe(value: object, owner: str, *, required: bool = False) -> None:
        path, state = _selection_path_state(value, selection)
        if state == "selected" and path is not None:
            selected_paths.add(path)
        elif state == "excluded" and path is not None:
            excluded_before.add(path)
        elif state == "invalid" or (required and state == "absent"):
            invalid_before.add(f"{owner}={value!r}")

    for vertex in graph.graph.vs:
        attrs = vertex.attributes()
        node_type = attrs.get("type")
        if node_type == NODE_TYPE_DIRECTORY:
            observe(attrs.get("name"), f"vertex[{vertex.index}].name", required=True)
        elif node_type == NODE_TYPE_FILE:
            observe(attrs.get("name"), f"vertex[{vertex.index}].name", required=True)
        elif node_type != "root" and attrs.get("name") != ROOT_NODE:
            observe(
                attrs.get("file"),
                f"vertex[{vertex.index}].file",
                required=is_symbol_node(node_type) and node_has_definition(attrs),
            )
    for edge in graph.graph.es:
        observe(edge.attributes().get("anchor_file"), f"edge[{edge.index}].anchor_file")
    occurrence_index = getattr(graph, "lsp_occurrence_index", None)
    if occurrence_index is not None:
        for index, occurrence in enumerate(occurrence_index.occurrences):
            observe(
                occurrence.file_path,
                f"occurrence[{index}].file_path",
                required=True,
            )

    def keep_vertex(attrs: Mapping[str, Any]) -> bool:
        node_type = attrs.get("type")
        name = attrs.get("name")
        if node_type == "root" or name == ROOT_NODE:
            return True
        if node_type == NODE_TYPE_DIRECTORY:
            directory = _canonical_selected_path(name, selection)
            return bool(
                directory
                and any(path.startswith(f"{directory}/") for path in selected_paths)
            )
        if node_type == NODE_TYPE_FILE:
            return _canonical_selected_path(name, selection) is not None

        raw_path = attrs.get("file")
        if raw_path is None or raw_path == "":
            return not (is_symbol_node(node_type) and node_has_definition(attrs))
        return _canonical_selected_path(raw_path, selection) is not None

    keep_vertices = [
        vertex.index for vertex in graph.graph.vs if keep_vertex(vertex.attributes())
    ]
    if len(keep_vertices) != before_nodes:
        graph.graph = graph.graph.subgraph(keep_vertices)

    remove_edges = []
    for edge in graph.graph.es:
        raw_anchor = edge.attributes().get("anchor_file")
        if (
            raw_anchor is not None
            and raw_anchor != ""
            and (_canonical_selected_path(raw_anchor, selection) is None)
        ):
            remove_edges.append(edge.index)
    if remove_edges:
        graph.graph.delete_edges(remove_edges)

    if occurrence_index is not None:
        from ..scip_interface.lsp_occurrence_index import SCIPOccurrenceIndex

        graph.lsp_occurrence_index = SCIPOccurrenceIndex(
            (
                occurrence
                for occurrence in occurrence_index.occurrences
                if _canonical_selected_path(occurrence.file_path, selection) is not None
            ),
            position_encoding=occurrence_index.position_encoding,
        )

    surviving_paths: set[str] = set()
    for vertex in graph.graph.vs:
        attrs = vertex.attributes()
        if attrs.get("type") == NODE_TYPE_FILE:
            value = attrs.get("name")
        elif attrs.get("type") not in {"root", NODE_TYPE_DIRECTORY}:
            value = attrs.get("file")
        else:
            value = None
        if (path := _canonical_selected_path(value, selection)) is not None:
            surviving_paths.add(path)
    for edge in graph.graph.es:
        if (
            path := _canonical_selected_path(
                edge.attributes().get("anchor_file"), selection
            )
        ) is not None:
            surviving_paths.add(path)
    final_occurrence_index = getattr(graph, "lsp_occurrence_index", None)
    if final_occurrence_index is not None:
        surviving_paths.update(
            path
            for occurrence in final_occurrence_index.occurrences
            if (path := _canonical_selected_path(occurrence.file_path, selection))
            is not None
        )
    keep_after_pruning = [
        vertex.index
        for vertex in graph.graph.vs
        if vertex.attributes().get("type") != NODE_TYPE_DIRECTORY
        or (
            (
                directory := _canonical_selected_path(
                    vertex.attributes().get("name"), selection
                )
            )
            is not None
            and any(path.startswith(f"{directory}/") for path in surviving_paths)
        )
    ]
    if len(keep_after_pruning) != graph.graph.vcount():
        graph.graph = graph.graph.subgraph(keep_after_pruning)

    graph.name_to_vertex = {
        name: vertex.index
        for vertex in graph.graph.vs
        if isinstance((name := vertex.attributes().get("name")), str) and name
    }
    graph.symbol_ranges = {
        name: value
        for name, value in graph.symbol_ranges.items()
        if name in graph.name_to_vertex
    }
    graph.invalidate_caches()
    graph.build_range_indexes()

    excluded_after: set[str] = set()
    invalid_after: set[str] = set()

    def verify(value: object, owner: str, *, required: bool = False) -> None:
        path, state = _selection_path_state(value, selection)
        if state == "excluded" and path is not None:
            excluded_after.add(path)
        elif state == "invalid" or (required and state == "absent"):
            invalid_after.add(f"{owner}={value!r}")

    represented_after: set[str] = set()
    for vertex in graph.graph.vs:
        attrs = vertex.attributes()
        node_type = attrs.get("type")
        if node_type == NODE_TYPE_DIRECTORY:
            verify(attrs.get("name"), f"vertex[{vertex.index}].name", required=True)
        elif node_type == NODE_TYPE_FILE:
            value = attrs.get("name")
            verify(value, f"vertex[{vertex.index}].name", required=True)
            if (path := _canonical_selected_path(value, selection)) is not None:
                represented_after.add(path)
        elif node_type != "root" and attrs.get("name") != ROOT_NODE:
            value = attrs.get("file")
            verify(
                value,
                f"vertex[{vertex.index}].file",
                required=is_symbol_node(node_type) and node_has_definition(attrs),
            )
            if (path := _canonical_selected_path(value, selection)) is not None:
                represented_after.add(path)
    for edge in graph.graph.es:
        value = edge.attributes().get("anchor_file")
        verify(value, f"edge[{edge.index}].anchor_file")
        if (path := _canonical_selected_path(value, selection)) is not None:
            represented_after.add(path)
    final_occurrence_index = getattr(graph, "lsp_occurrence_index", None)
    if final_occurrence_index is not None:
        for index, occurrence in enumerate(final_occurrence_index.occurrences):
            value = occurrence.file_path
            verify(value, f"occurrence[{index}].file_path", required=True)
            if (path := _canonical_selected_path(value, selection)) is not None:
                represented_after.add(path)

    dangling_directories = sorted(
        name
        for vertex in graph.graph.vs
        if vertex.attributes().get("type") == NODE_TYPE_DIRECTORY
        and isinstance((name := vertex.attributes().get("name")), str)
        and not any(path.startswith(f"{name}/") for path in represented_after)
    )
    if dangling_directories:
        invalid_after.update(
            f"directory_without_selected_descendant={path!r}"
            for path in dangling_directories
        )

    return {
        "source_selection_digest": selection.digest,
        "nodes_before": before_nodes,
        "nodes_after": graph.graph.vcount(),
        "edges_before": before_edges,
        "edges_after": graph.graph.ecount(),
        "removed_excluded_paths": sorted(excluded_before),
        "excluded_paths_after": sorted(excluded_after),
        "invalid_paths_before": sorted(invalid_before),
        "invalid_paths_after": sorted(invalid_after),
    }


def constrain_and_assess_graph_artifact(
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
    if surface_report["invalid_paths_before"]:
        failures.append("invalid_graph_paths")
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
    view: AuthenticatedVectorView,
    level: str,
    model_suffix: str,
) -> tuple[list[Any], int, list[str], bool]:
    import faiss

    failures = []
    config_relative = f"{level}/config_{model_suffix}.json"
    index_relative = f"{level}/index_{model_suffix}.faiss"
    documents_relative = f"{level}/documents_{model_suffix}.pkl"
    for relative in (config_relative, index_relative, documents_relative):
        if not view.has_file(relative):
            failures.append(f"missing:{Path(relative).name}")
    if failures:
        return [], 0, failures, False

    try:
        config = view.load_json_object(
            config_relative,
            label=f"vector {level} quality config",
        )
        with view.authenticated_snapshot(documents_relative) as (snapshot, _record):
            documents = list(compat_pickle.load(AuthenticatedSnapshotReader(snapshot)))
        with view.authenticated_snapshot(index_relative) as (snapshot, _record):
            source = AuthenticatedSnapshotReader(snapshot)
            callback_reader = faiss.PyCallbackIOReader(  # type: ignore[attr-defined]
                source.read
            )
            index = faiss.read_index(callback_reader)
    except Exception as exc:
        return [], 0, [f"unreadable_level:{type(exc).__name__}"], False
    expected = config.get("num_documents")
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
        failures.append("invalid_document_config_count")
    elif expected != len(documents):
        failures.append("document_config_count_mismatch")
    if int(index.ntotal) != len(documents):
        failures.append("faiss_document_count_mismatch")
    return documents, int(index.ntotal), failures, True


def _vector_artifact_relative_paths(
    model_suffix: str,
    build_levels: Sequence[str],
) -> tuple[Path, ...]:
    paths = [Path(f"config_{model_suffix}.json")]
    for level in sorted({raw_level.lower() for raw_level in build_levels}):
        level_root = Path(level)
        paths.extend(
            (
                level_root / f"config_{model_suffix}.json",
                level_root / f"index_{model_suffix}.faiss",
                level_root / f"documents_{model_suffix}.pkl",
            )
        )
    return tuple(paths)


def _file_fingerprint(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
            size += len(block)
    return {"size": size, "sha256": digest.hexdigest()}


def vector_artifact_file_fingerprints(
    root: str | Path,
    *,
    embedding_model: str,
    build_levels: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """Fingerprint every file covered by a vector quality assessment."""

    artifact_root = Path(root)
    model_suffix = embedding_model.replace("/", "__")
    fingerprints: dict[str, dict[str, Any]] = {}
    for relative in _vector_artifact_relative_paths(model_suffix, build_levels):
        path = artifact_root / relative
        try:
            fingerprints[relative.as_posix()] = _file_fingerprint(path)
        except OSError:
            continue
    return fingerprints


def vector_artifact_files_match(
    root: str | Path,
    *,
    embedding_model: str,
    build_levels: Sequence[str],
    expected_fingerprints: object,
) -> bool:
    """Return whether all assessed vector files still match their report."""

    if not isinstance(expected_fingerprints, Mapping):
        return False
    model_suffix = embedding_model.replace("/", "__")
    expected_paths = {
        path.as_posix()
        for path in _vector_artifact_relative_paths(model_suffix, build_levels)
    }
    if set(expected_fingerprints) != expected_paths:
        return False
    return vector_artifact_file_fingerprints(
        root,
        embedding_model=embedding_model,
        build_levels=build_levels,
    ) == dict(expected_fingerprints)


def _captured_vector_artifact_file_fingerprints(
    view: AuthenticatedVectorView,
    *,
    model_suffix: str,
    build_levels: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """Return fingerprints from the exact tree authorized for assessment."""

    fingerprints: dict[str, dict[str, Any]] = {}
    for relative_path in _vector_artifact_relative_paths(model_suffix, build_levels):
        relative = relative_path.as_posix()
        if not view.has_file(relative):
            continue
        record = view.record(relative)
        fingerprints[relative] = {
            "size": record.size,
            "sha256": record.sha256,
        }
    return fingerprints


def _assess_captured_vector_artifact(
    *,
    embedding_model: str,
    build_levels: Sequence[str],
    surface: GitSourceSurface,
    expected_artifact: Mapping[str, Any],
    required_l0_files: Sequence[str] = (),
    authenticated_view: AuthenticatedVectorView,
) -> dict[str, Any]:
    model_suffix = embedding_model.replace("/", "__")
    top_config_relative = f"config_{model_suffix}.json"
    failures: list[str] = []
    top_config: Mapping[str, Any] = {}
    if not authenticated_view.has_file(top_config_relative):
        failures.append("missing_top_config")
    else:
        try:
            top_config = authenticated_view.load_json_object(
                top_config_relative,
                label="vector quality top-level config",
            )
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
    invalid_document_metadata = 0
    normalized_levels = tuple(dict.fromkeys(level.lower() for level in build_levels))
    unexpected_levels = []
    for level in sorted({"l0", "l2"} - set(normalized_levels)):
        if any(
            authenticated_view.has_file(f"{level}/{name}")
            for name in (
                f"config_{model_suffix}.json",
                f"index_{model_suffix}.faiss",
                f"documents_{model_suffix}.pkl",
                f"index_{model_suffix}.pkl",
            )
        ):
            unexpected_levels.append(level)
            failures.append(f"unexpected_level:{level}")
    for level in normalized_levels:
        documents, vector_count, level_failures, loaded = _load_vector_level(
            authenticated_view, level, model_suffix
        )
        paths = set()
        for document_index, document in enumerate(documents):
            metadata = getattr(document, "metadata", {})
            if not isinstance(metadata, Mapping):
                invalid_document_metadata += 1
                continue
            raw_path = metadata.get("file")
            path = _path_or_none(raw_path)
            if path:
                paths.add(path)
            else:
                invalid_document_paths.add(
                    f"{level}[{document_index}].file={raw_path!r}"
                )
        all_paths.update(paths)
        if level == "l0":
            l0_paths.update(paths)
        if loaded and not documents:
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
    if invalid_document_metadata:
        failures.append("invalid_document_metadata")
    required = tuple(
        sorted(normalize_repository_path(path) for path in required_l0_files)
    )
    missing = tuple(path for path in required if path not in l0_paths)
    if missing:
        failures.append("missing_required_l0_files")
    file_fingerprints = _captured_vector_artifact_file_fingerprints(
        authenticated_view,
        model_suffix=model_suffix,
        build_levels=normalized_levels,
    )
    expected_file_count = len(
        _vector_artifact_relative_paths(model_suffix, normalized_levels)
    )
    if len(file_fingerprints) != expected_file_count:
        failures.append("incomplete_file_fingerprints")
    return {
        "schema_version": ARTIFACT_QUALITY_SCHEMA_VERSION,
        "kind": "vector",
        "artifact": dict(expected_artifact),
        "commit": surface.commit,
        "tree": surface.tree,
        "levels": levels,
        "unexpected_levels": unexpected_levels,
        "paths": {
            "tracked": len(classified["tracked"]),
            "submodule": len(classified["submodule"]),
            "outside": list(classified["outside"]),
            "invalid": sorted(invalid_document_paths),
            "invalid_metadata": invalid_document_metadata,
        },
        "required_l0_files": list(required),
        "l0_files": sorted(l0_paths),
        "missing_required_l0_files": list(missing),
        "file_fingerprints": file_fingerprints,
        "failure_names": sorted(set(failures)),
        "passed": not failures,
    }


def assess_vector_artifact(
    root: str | Path,
    *,
    embedding_model: str,
    build_levels: Sequence[str],
    surface: GitSourceSurface,
    expected_artifact: Mapping[str, Any],
    required_l0_files: Sequence[str] = (),
    authenticated_view: AuthenticatedVectorView,
    native_index_authorization: NativeIndexAuthorization | None,
) -> dict[str, Any]:
    """Validate one externally authorized, already-captured vector artifact.

    The assessor is a generic artifact consumer, not an administrative trust
    boundary: callers must supply both the exact captured view and an
    out-of-band capability bound to that view and ``expected_artifact``.  No
    artifact field can authorize the pickle or FAISS parsers used below.
    """

    from .._atomic_directory import lexical_directory_path

    require_native_index_authorization_preflight(
        native_index_authorization,
        view_type="vector",
    )
    artifact_root = lexical_directory_path(Path(root))
    if authenticated_view.root != artifact_root:
        raise ValueError("authenticated vector view does not match assessment root")
    require_native_index_authorization(
        native_index_authorization,
        authenticated_view.ownership,
        view_type="vector",
        semantic_contract=expected_artifact,
    )

    primary_error: BaseException | None = None
    try:
        return _assess_captured_vector_artifact(
            embedding_model=embedding_model,
            build_levels=build_levels,
            surface=surface,
            expected_artifact=expected_artifact,
            required_l0_files=required_l0_files,
            authenticated_view=authenticated_view,
        )
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            authenticated_view.verify_final()
        except BaseException as verification_error:
            if primary_error is None:
                raise
            add_note = getattr(primary_error, "add_note", None)
            if callable(add_note):
                add_note(
                    "secondary authenticated-vector final verification failure: "
                    f"{type(verification_error).__name__}: {verification_error}"
                )


def write_artifact_quality(path: str | Path, report: Mapping[str, Any]) -> None:
    """Atomically publish a human-readable artifact quality report."""

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)


__all__ = [
    "ARTIFACT_QUALITY_SCHEMA_VERSION",
    "assess_vector_artifact",
    "constrain_and_assess_graph_artifact",
    "constrain_graph_to_source_selection",
    "constrain_graph_to_source_surface",
    "graph_file_paths",
    "graph_source_paths",
    "required_source_files",
    "vector_artifact_file_fingerprints",
    "vector_artifact_files_match",
    "write_artifact_quality",
]
