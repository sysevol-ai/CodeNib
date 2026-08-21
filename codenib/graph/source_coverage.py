# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Syntax fallback for source files omitted by compiler-backed graph indexes."""

from __future__ import annotations

import fnmatch
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence

from ..code_chunking import create_chunker
from ..git_snapshot import GitSourceSurface, normalize_repository_path
from ..languages import extension_to_language_map
from ..repository_filters import repository_path_is_visible
from ..repository_source_selection import (
    DEFAULT_REPOSITORY_SOURCE_SELECTION,
    RepositorySourceSelection,
)
from ..types import (
    NODE_TYPE_CLASS,
    NODE_TYPE_FIELD,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
    NODE_TYPE_SYMBOL,
)
from .code_graph import CodeGraph

_CONTAINER_TYPES = frozenset(
    {
        "class",
        "enum",
        "extension",
        "impl",
        "interface",
        "module",
        "namespace",
        "protocol",
        "record",
        "struct",
        "trait",
    }
)
_FIELD_TYPES = frozenset(
    {
        "constant",
        "field",
        "property",
        "type_alias",
        "variable",
    }
)


def _matches_any(path: str, patterns: Sequence[str]) -> bool:
    for pattern in patterns:
        normalized = str(pattern).strip().replace("\\", "/").strip("/")
        if not normalized:
            continue
        if fnmatch.fnmatch(path, normalized):
            return True
        if normalized.endswith("/**"):
            prefix = normalized[:-3]
            if path == prefix or path.startswith(f"{prefix}/"):
                return True
    return False


def _node_type(chunk_type: str) -> str:
    normalized = chunk_type.lower()
    if normalized in _CONTAINER_TYPES:
        return NODE_TYPE_CLASS
    if normalized in {"function", "constructor"}:
        return NODE_TYPE_FUNCTION
    if normalized in {"method", "getter", "setter"}:
        return NODE_TYPE_METHOD
    if normalized in _FIELD_TYPES:
        return NODE_TYPE_FIELD
    return NODE_TYPE_SYMBOL


def _root_name(graph: CodeGraph) -> str | None:
    for vertex in graph.graph.vs:
        if vertex.attributes().get("type") == "root":
            return vertex.attributes().get("name")
    return None


def _ensure_file_hierarchy(graph: CodeGraph, file_path: str) -> None:
    path = PurePosixPath(file_path)
    parent = _root_name(graph)
    current_parts: list[str] = []
    for part in path.parent.parts:
        if part == ".":
            continue
        current_parts.append(part)
        directory = PurePosixPath(*current_parts).as_posix()
        graph.add_directory_node(directory)
        if parent is not None:
            graph.current_scope = parent
            graph.add_containment_edge(directory)
        parent = directory

    graph.add_file_node(file_path)
    if parent is not None:
        graph.current_scope = parent
        graph.add_containment_edge(file_path)
    graph.current_scope = file_path


def _containing_parent(
    chunk: Any,
    containers: Sequence[tuple[int, int, str]],
    file_path: str,
) -> str:
    candidates = [
        (end - start, identity)
        for start, end, identity in containers
        if start <= chunk.start_line
        and end >= chunk.end_line
        and (start, end) != (chunk.start_line, chunk.end_line)
    ]
    return min(candidates)[1] if candidates else file_path


def supplement_graph_source_coverage(
    graph: CodeGraph,
    *,
    repo_root: str | Path,
    surface: GitSourceSurface,
    extensions: Iterable[str],
    represented_paths: Iterable[str],
    exclude_patterns: Sequence[str] = (),
    source_selection: RepositorySourceSelection = DEFAULT_REPOSITORY_SOURCE_SELECTION,
) -> dict[str, Any]:
    """Add syntax definitions for tracked source files absent from *graph*.

    The fallback is independent of benchmark labels: it covers every tracked
    source file in the requested language surface. It adds file, definition,
    and containment records only; compiler-derived reference edges remain
    authoritative when available.
    """

    if type(source_selection) is not RepositorySourceSelection:
        raise TypeError("source_selection must be a RepositorySourceSelection")
    selection = RepositorySourceSelection(source_selection.exclude_subtrees)
    root = Path(repo_root).expanduser().resolve()
    accepted = frozenset(extensions)
    represented = {normalize_repository_path(path) for path in represented_paths}
    expected_files = [
        path
        for path in sorted(surface.tracked_files)
        if Path(path).suffix in accepted
        and repository_path_is_visible(path)
        and selection.allows(path)
        and not _matches_any(path, exclude_patterns)
    ]
    missing = [path for path in expected_files if path not in represented]
    chunker_languages = extension_to_language_map("chunker")
    chunkers: dict[str, Any] = {}
    supplemented_files = []
    supplemented_symbols = 0
    unreadable_files = []
    unreadable_errors = {}

    for file_path in missing:
        absolute = root.joinpath(*PurePosixPath(file_path).parts)
        language = chunker_languages.get(Path(file_path).suffix)
        if language is None or not absolute.is_file():
            unreadable_files.append(file_path)
            continue
        try:
            chunker = chunkers.get(language)
            if chunker is None:
                chunker = create_chunker(
                    language,
                    chunk_depth=2,
                    l2_level_exclusive=False,
                    skeleton_mode=False,
                )
                chunkers[language] = chunker
            chunks = chunker.chunk_file(str(absolute), relative_path=file_path)
        # The syntax path supplements a compiler graph. One parser or plugin
        # failure should remain a quality diagnostic, not discard that graph.
        except Exception as exc:
            unreadable_files.append(file_path)
            unreadable_errors[file_path] = f"{type(exc).__name__}: {exc}"
            continue

        _ensure_file_hierarchy(graph, file_path)
        containers: list[tuple[int, int, str]] = []
        seen_symbols: set[str] = set()
        for chunk in sorted(
            chunks,
            key=lambda item: (
                item.start_line,
                -(item.end_line - item.start_line),
                item.node_id,
            ),
        ):
            identity = str(chunk.node_id)
            if identity in seen_symbols:
                continue
            seen_symbols.add(identity)
            parent = _containing_parent(chunk, containers, file_path)
            graph.add_symbol_node(
                identity,
                chunk.start_line,
                chunk.start_line,
                chunk.end_line,
                _node_type(chunk.chunk_type),
            )
            vertex = graph.graph.vs[graph.name_to_vertex[identity]]
            vertex["unified_name"] = chunk.name
            graph.current_scope = parent
            graph.add_containment_edge(identity)
            if chunk.chunk_type.lower() in _CONTAINER_TYPES:
                containers.append((chunk.start_line, chunk.end_line, identity))
            supplemented_symbols += 1
        supplemented_files.append(file_path)

    graph.invalidate_caches()
    graph.build_range_indexes()
    represented_after = represented.union(supplemented_files)
    expected_count = len(expected_files)
    return {
        "tracked_source_files": expected_count,
        "represented_before": expected_count - len(missing),
        "represented_after": len(set(expected_files).intersection(represented_after)),
        "coverage_before": (
            (expected_count - len(missing)) / expected_count if expected_count else 1.0
        ),
        "coverage_after": (
            len(set(expected_files).intersection(represented_after)) / expected_count
            if expected_count
            else 1.0
        ),
        "candidate_files": len(missing),
        "supplemented_files": len(supplemented_files),
        "supplemented_symbols": supplemented_symbols,
        "files": supplemented_files,
        "unreadable_files": unreadable_files,
        "unreadable_errors": unreadable_errors,
        "source_selection_digest": selection.digest,
    }


__all__ = ["supplement_graph_source_coverage"]
