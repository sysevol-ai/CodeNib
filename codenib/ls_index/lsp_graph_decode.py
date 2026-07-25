# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Generic LSP documentSymbol decoder.

This decoder always builds the conservative part of an LSP graph: file nodes,
definition symbols, and CONTAIN edges.  Cross-file REFERENCE edges are decoded
only when the index payload explicitly carries LSP reference locations; callers
can keep the backend symbol-only until a language has alignment tolerances.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Mapping, Optional
from urllib.parse import unquote, urlparse

from ..graph.code_graph import CodeGraph
from ..types import (
    EDGE_TYPE_CONTAIN,
    EDGE_TYPE_REFERENCE,
    NODE_TYPE_CLASS,
    NODE_TYPE_DIRECTORY,
    NODE_TYPE_FIELD,
    NODE_TYPE_FILE,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
    NODE_TYPE_SYMBOL,
    ROOT_NODE,
)

_INDEX_SCHEMA_VERSION = 1

_LSP_KIND_TO_NODE_TYPE = {
    2: NODE_TYPE_CLASS,  # Module
    3: NODE_TYPE_CLASS,  # Namespace
    5: NODE_TYPE_CLASS,  # Class
    6: NODE_TYPE_METHOD,  # Method
    7: NODE_TYPE_FIELD,  # Property
    8: NODE_TYPE_FIELD,  # Field
    9: NODE_TYPE_METHOD,  # Constructor
    10: NODE_TYPE_CLASS,  # Enum
    11: NODE_TYPE_CLASS,  # Interface
    12: NODE_TYPE_FUNCTION,  # Function
    13: NODE_TYPE_FIELD,  # Variable
    14: NODE_TYPE_FIELD,  # Constant
    22: NODE_TYPE_CLASS,  # Enum (alternate)
    23: NODE_TYPE_CLASS,  # Struct
    25: NODE_TYPE_FUNCTION,  # Operator
}

_GRAPH_SYMBOL_KINDS = frozenset(_LSP_KIND_TO_NODE_TYPE)


class GenericLSPGraphDecoder:
    """Decode saved LSP ``documentSymbol`` responses into a ``CodeGraph``."""

    def __init__(
        self,
        index_file_path: str,
        project_root: Optional[str] = None,
    ):
        self.index_file_path = Path(index_file_path)
        self.project_root = Path(project_root).resolve() if project_root else None
        self.code_graph = CodeGraph(
            str(self.project_root) if self.project_root else None
        )

    def decode(self) -> CodeGraph:
        payload = json.loads(self.index_file_path.read_text(encoding="utf-8"))
        version = payload.get("schema_version")
        if version != _INDEX_SCHEMA_VERSION:
            raise ValueError(
                f"{self.index_file_path} has schema_version={version!r}, "
                f"expected {_INDEX_SCHEMA_VERSION}"
            )
        language = str(payload.get("language") or "")

        graph = CodeGraph(str(self.project_root) if self.project_root else None)
        graph.add_root_node(ROOT_NODE)

        with graph.batch_edges():
            for entry in payload.get("files", []):
                file_path = entry.get("path")
                if not file_path:
                    continue
                _add_file_container(graph, file_path)
                _process_symbol_tree(
                    graph,
                    file_path=file_path,
                    symbols=entry.get("symbols") or [],
                    parent_vertex_name=file_path,
                    parent_unified_part="",
                    language=language,
                )

        graph.build_range_indexes()
        with graph.batch_edges():
            _add_reference_edges(
                graph,
                payload.get("files", []),
                project_root=self.project_root,
            )
        graph.build_range_indexes()
        self.code_graph = graph
        return graph

    def decode_documents(
        self,
        documents: Mapping[str, Iterable[dict]],
    ) -> CodeGraph:
        """Decode in-memory responses.  Used by unit tests and small callers."""

        graph = CodeGraph(str(self.project_root) if self.project_root else None)
        graph.add_root_node(ROOT_NODE)
        with graph.batch_edges():
            for file_path, symbols in documents.items():
                _add_file_container(graph, file_path)
                _process_symbol_tree(
                    graph,
                    file_path=file_path,
                    symbols=list(symbols),
                    parent_vertex_name=file_path,
                    parent_unified_part="",
                    language="",
                )
        graph.build_range_indexes()
        self.code_graph = graph
        return graph

    def save_graph(self, output_path: str):
        self.code_graph.save_graph(output_path)


def _add_file_container(graph: CodeGraph, file_path: str) -> None:
    graph._add_vertex(file_path, {"type": NODE_TYPE_FILE})

    parent_name = ROOT_NODE
    parts = Path(file_path).parent.parts
    if parts and parts != (".",):
        current = Path()
        for part in parts:
            current = current / part
            directory = current.as_posix()
            graph._add_vertex(directory, {"type": NODE_TYPE_DIRECTORY})
            graph._add_edge(parent_name, directory, EDGE_TYPE_CONTAIN)
            parent_name = directory

    graph._add_edge(parent_name, file_path, EDGE_TYPE_CONTAIN)


def _process_symbol_tree(
    graph: CodeGraph,
    *,
    file_path: str,
    symbols: list[dict],
    parent_vertex_name: str,
    parent_unified_part: str,
    language: str = "",
    ruby_scope_vertices: Optional[Mapping[str, str]] = None,
) -> None:
    scope_vertices = ruby_scope_vertices or {}
    for symbol in symbols or []:
        name = symbol.get("name") or ""
        kind = int(symbol.get("kind") or 0)
        child_scope_vertices = scope_vertices

        range_data = symbol.get("range") or {}
        start_line = _line(range_data, "start", default=0)
        end_line = _line(range_data, "end", default=start_line)

        node_type = _node_type(kind)
        if _is_local_symbol(kind, parent_unified_part) or _is_non_graph_scope(
            name,
            language,
        ):
            child_parent = parent_unified_part
            child_parent_vertex = parent_vertex_name
        elif kind in _GRAPH_SYMBOL_KINDS:
            unified_name = _build_unified_name(
                file_path=file_path,
                name=name,
                parent_unified_part=parent_unified_part,
                kind=kind,
                language=language,
            )
            vertex_name = f"{unified_name}:{start_line}"
            graph._add_vertex(
                vertex_name,
                {
                    "type": node_type,
                    "file": file_path,
                    "start_line": start_line,
                    "end_line": end_line,
                    "unified_name": unified_name,
                },
            )
            graph.symbol_ranges[vertex_name] = (start_line, end_line)
            contain_parent = _contain_parent_vertex_name(
                name=name,
                kind=kind,
                language=language,
                parent_vertex_name=parent_vertex_name,
                parent_unified_part=parent_unified_part,
                ruby_scope_vertices=scope_vertices,
            )
            graph._add_edge(contain_parent, vertex_name, EDGE_TYPE_CONTAIN)

            if kind == 2 and language != "ruby":
                child_parent = parent_unified_part
            else:
                child_parent = unified_name.split(":", 1)[1]
            if language == "ruby":
                child_scope_vertices = dict(scope_vertices)
                child_scope_vertices[child_parent] = vertex_name
            child_parent_vertex = vertex_name
        else:
            child_parent = parent_unified_part
            child_parent_vertex = parent_vertex_name

        children = symbol.get("children") or []
        if children:
            _process_symbol_tree(
                graph,
                file_path=file_path,
                symbols=children,
                parent_vertex_name=child_parent_vertex,
                parent_unified_part=child_parent,
                language=language,
                ruby_scope_vertices=child_scope_vertices,
            )


def iter_lsp_symbol_definitions(
    file_path: str,
    symbols: list[dict],
    parent_unified_part: str = "",
    language: str = "",
) -> Iterable[dict]:
    """Yield definition metadata using the generic decoder naming rules."""

    for symbol in symbols or []:
        name = symbol.get("name") or ""
        kind = int(symbol.get("kind") or 0)
        range_data = symbol.get("range") or {}
        sel_range = symbol.get("selectionRange") or range_data
        start_line = _line(range_data, "start", default=0)
        end_line = _line(range_data, "end", default=start_line)

        if _is_local_symbol(kind, parent_unified_part) or _is_non_graph_scope(
            name,
            language,
        ):
            child_parent = parent_unified_part
        elif kind in _GRAPH_SYMBOL_KINDS:
            unified_name = _build_unified_name(
                file_path=file_path,
                name=name,
                parent_unified_part=parent_unified_part,
                kind=kind,
                language=language,
            )
            yield {
                "vertex_name": f"{unified_name}:{start_line}",
                "unified_name": unified_name,
                "kind": kind,
                "start_line": start_line,
                "end_line": end_line,
                "selection_line": _line(sel_range, "start", default=start_line),
                "selection_character": _character(sel_range, "start", default=0),
            }
            child_parent = (
                parent_unified_part
                if kind == 2 and language != "ruby"
                else unified_name.split(":", 1)[1]
            )
        else:
            child_parent = parent_unified_part

        yield from iter_lsp_symbol_definitions(
            file_path,
            symbol.get("children") or [],
            parent_unified_part=child_parent,
            language=language,
        )


def _add_reference_edges(
    graph: CodeGraph,
    file_entries: list[dict],
    *,
    project_root: Optional[Path],
) -> None:
    for entry in file_entries:
        for refs in entry.get("references") or []:
            target = _target_vertex_name(refs)
            if target not in graph.name_to_vertex:
                continue
            target_file = refs.get("target_file")
            target_start = refs.get("target_start_line")
            for location in refs.get("locations") or []:
                rel_path, anchor_line = _location_anchor(location, project_root)
                if rel_path is None or anchor_line is None:
                    continue
                if rel_path == target_file and anchor_line == target_start:
                    continue
                source = _scope_at_line(graph, rel_path, anchor_line)
                if source is None:
                    continue
                graph._add_edge(
                    source,
                    target,
                    EDGE_TYPE_REFERENCE,
                    anchor_file=rel_path,
                    anchor_line=anchor_line,
                )


def _target_vertex_name(refs: dict) -> str:
    return f"{refs.get('target_unified_name')}:{refs.get('target_start_line')}"


def _location_anchor(
    location: dict,
    project_root: Optional[Path],
) -> tuple[Optional[str], Optional[int]]:
    uri = location.get("uri") or location.get("targetUri")
    range_data = location.get("range") or location.get("targetSelectionRange") or {}
    if not uri:
        return None, None
    rel_path = _uri_to_relpath(uri, project_root)
    if rel_path is None:
        return None, None
    return rel_path, _line(range_data, "start", default=-1)


def _uri_to_relpath(uri: str, project_root: Optional[Path]) -> Optional[str]:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    path = Path(unquote(parsed.path))
    if project_root is not None:
        try:
            return path.resolve().relative_to(project_root).as_posix()
        except ValueError:
            return None
    return path.as_posix()


def _scope_at_line(graph: CodeGraph, file_path: str, line: int) -> Optional[str]:
    if line < 0:
        return None
    candidates = []
    for start, end, vid in graph._file_nodes.get(file_path, []):
        if start <= line <= end:
            vertex = graph.graph.vs[vid]
            attrs = vertex.attributes()
            if attrs.get("type") == NODE_TYPE_FILE:
                continue
            candidates.append((end - start, start, vertex["name"]))
    if candidates:
        candidates.sort(key=lambda item: (item[0], -item[1]))
        return candidates[0][2]
    if file_path in graph.name_to_vertex:
        return file_path
    return None


def _line(range_data: dict, key: str, *, default: int) -> int:
    point = range_data.get(key) or {}
    line = point.get("line")
    return int(line) if isinstance(line, int) else default


def _character(range_data: dict, key: str, *, default: int) -> int:
    point = range_data.get(key) or {}
    character = point.get("character")
    return int(character) if isinstance(character, int) else default


def _node_type(kind: int) -> str:
    return _LSP_KIND_TO_NODE_TYPE.get(kind, NODE_TYPE_SYMBOL)


def _is_local_symbol(kind: int, parent_unified_part: str) -> bool:
    return kind in (13, 14) and bool(parent_unified_part.endswith("()"))


def _is_non_graph_scope(name: str, language: str) -> bool:
    return language == "ruby" and name == "<< self"


def _build_unified_name(
    *,
    file_path: str,
    name: str,
    parent_unified_part: str,
    kind: int,
    language: str = "",
) -> str:
    if language == "ruby":
        name = name.replace("::", ".")
        if kind == 9 or name in ("<constructor>", "constructor"):
            name = "initialize"
        if kind == 8 and name.startswith("@"):
            parent_unified_part = _ruby_instance_field_parent(parent_unified_part)
    elif kind == 9 or name in ("<constructor>", "constructor"):
        name = "constructor"
    if language == "ruby" and parent_unified_part and name.startswith("self."):
        name = name.removeprefix("self.")

    display = f"{parent_unified_part}.{name}" if parent_unified_part else name
    if _is_callable_display_symbol(kind, name, language):
        if not display.endswith("()"):
            display = f"{display}()"
    return f"{file_path}:{display}"


def _contain_parent_vertex_name(
    *,
    name: str,
    kind: int,
    language: str,
    parent_vertex_name: str,
    parent_unified_part: str,
    ruby_scope_vertices: Mapping[str, str],
) -> str:
    if language != "ruby" or kind != 8 or not name.startswith("@"):
        return parent_vertex_name
    field_parent = _ruby_instance_field_parent(parent_unified_part)
    return ruby_scope_vertices.get(field_parent, parent_vertex_name)


def _is_callable_display_symbol(kind: int, name: str, language: str) -> bool:
    if _node_type(kind) in (NODE_TYPE_FUNCTION, NODE_TYPE_METHOD):
        return True
    return language == "ruby" and kind == 8 and not name.startswith("@")


def _ruby_instance_field_parent(parent_unified_part: str) -> str:
    parts = parent_unified_part.split(".") if parent_unified_part else []
    while parts and parts[-1].endswith("()"):
        parts.pop()
    return ".".join(parts)


__all__ = ["GenericLSPGraphDecoder", "iter_lsp_symbol_definitions"]
