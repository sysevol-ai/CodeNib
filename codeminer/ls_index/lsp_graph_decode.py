# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Generic LSP documentSymbol decoder.

This decoder intentionally builds the conservative part of an LSP graph:
file nodes, definition symbols, and CONTAIN edges.  Cross-file REFERENCE edges
are server-specific enough that they belong behind a backend-alignment harness
rather than being inferred in this first generic path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Mapping, Optional

from ..graph.code_graph import CodeGraph
from ..types import (
    EDGE_TYPE_CONTAIN,
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
) -> None:
    for symbol in symbols or []:
        name = symbol.get("name") or ""
        kind = int(symbol.get("kind") or 0)

        range_data = symbol.get("range") or {}
        start_line = _line(range_data, "start", default=0)
        end_line = _line(range_data, "end", default=start_line)

        node_type = _node_type(kind)
        if _is_local_symbol(kind, parent_unified_part):
            child_parent = parent_unified_part
            child_parent_vertex = parent_vertex_name
        elif kind in _GRAPH_SYMBOL_KINDS:
            unified_name = _build_unified_name(
                file_path=file_path,
                name=name,
                parent_unified_part=parent_unified_part,
                kind=kind,
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
            graph._add_edge(parent_vertex_name, vertex_name, EDGE_TYPE_CONTAIN)

            if kind == 2:
                child_parent = parent_unified_part
            else:
                child_parent = unified_name.split(":", 1)[1]
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
            )


def _line(range_data: dict, key: str, *, default: int) -> int:
    point = range_data.get(key) or {}
    line = point.get("line")
    return int(line) if isinstance(line, int) else default


def _node_type(kind: int) -> str:
    return _LSP_KIND_TO_NODE_TYPE.get(kind, NODE_TYPE_SYMBOL)


def _is_local_symbol(kind: int, parent_unified_part: str) -> bool:
    return kind in (13, 14) and bool(parent_unified_part.endswith("()"))


def _build_unified_name(
    *,
    file_path: str,
    name: str,
    parent_unified_part: str,
    kind: int,
) -> str:
    if kind == 9 or name in ("<constructor>", "constructor"):
        name = "constructor"

    display = f"{parent_unified_part}.{name}" if parent_unified_part else name
    if _node_type(kind) in (NODE_TYPE_FUNCTION, NODE_TYPE_METHOD):
        if not display.endswith("()"):
            display = f"{display}()"
    return f"{file_path}:{display}"


__all__ = ["GenericLSPGraphDecoder"]
