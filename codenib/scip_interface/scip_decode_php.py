#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""SCIP decoder for PHP projects indexed by scip-php."""

from __future__ import annotations

import re
from pathlib import Path

from tree_sitter import Parser
from tree_sitter_language_pack import get_language

from ..types import EDGE_TYPE_CONTAIN, NODE_TYPE_CLASS, NODE_TYPE_FUNCTION
from .scip_decode_java import SCIPJavaGraphDecoder, _SymbolInfo
from .scip_decode_utils import format_unified_name
from .scip_indexer_base import extract_scip_blocks


class SCIPPHPGraphDecoder(SCIPJavaGraphDecoder):
    """Decode scip-php TextFormat output into CodeGraph."""

    _LOG_LANGUAGE = "PHP"
    _SOURCE_SUFFIXES = (".php", ".phtml")
    _SCIP_SYMBOL_PREFIXES = {"scip-php"}
    _php_parser = None

    def _document_occurrences(self, document_text: str) -> list[str]:
        return sorted(
            extract_scip_blocks(document_text, "occurrences"),
            key=self._occurrence_start_line,
        )

    def _process_document(self, document_text: str) -> None:
        file_path = self._document_file_path(document_text)
        super()._process_document(document_text)
        if file_path and self._is_source_file(file_path):
            self._add_missing_top_level_functions(file_path)

    def _occurrence_start_line(self, occurrence_text: str) -> int:
        match = re.search(r"range:\s*(\d+)", occurrence_text)
        return int(match.group(1)) if match else 0

    def _make_symbol_key(self, symbol: str | None) -> str | None:
        key = super()._make_symbol_key(symbol)
        if key and re.search(r"\(\)\.\(\$[^)]*\)$", key):
            return None
        return key

    def _add_definition(self, info, *, line: int, enclosing_ranges: list[int]) -> None:
        if info.symbol_type == NODE_TYPE_CLASS and "/" in info.key:
            self._ensure_namespace_node(info, line=line)
        super()._add_definition(info, line=line, enclosing_ranges=enclosing_ranges)

    def _ensure_namespace_node(self, info, *, line: int) -> None:
        namespace = info.key.rsplit("/", 1)[0]
        namespace_key = self._namespace_key(info.file_path, namespace)
        if not namespace or namespace_key in self.code_graph.name_to_vertex:
            return
        self.code_graph.add_symbol_node(
            namespace_key,
            line,
            None,
            None,
            NODE_TYPE_CLASS,
        )
        vid = self.code_graph.name_to_vertex[namespace_key]
        namespace_display = namespace.replace("/", "\\")
        self.code_graph.graph.vs[vid]["unified_name"] = format_unified_name(
            info.file_path,
            namespace_display,
            namespace_key,
        )
        self.code_graph._add_edge(info.file_path, namespace_key, EDGE_TYPE_CONTAIN)

    def _namespace_key(self, file_path: str, namespace: str) -> str:
        return f"{file_path}:{namespace}"

    def _containment_parent(self, key: str) -> str:
        owner, member = self._split_owner_member(key)
        if member and owner in self.code_graph.name_to_vertex:
            return owner
        return self.code_graph.current_scope

    def _add_missing_top_level_functions(self, file_path: str) -> None:
        source_path = self._source_path(file_path)
        if source_path is None:
            return

        try:
            source = source_path.read_bytes()
            parser = self._get_php_parser()
            root = parser.parse(source).root_node
        except Exception as exc:
            self.logger.warning(
                "Could not parse PHP source %s for top-level functions: %s",
                file_path,
                exc,
            )
            return

        # scip-php 0.0.2 omits namespace top-level functions from the SCIP index.
        # Synthesize definition nodes only; references still come from SCIP.
        for node, namespace in self._iter_top_level_function_nodes(root, source):
            name = self._node_name(node, source)
            if not name:
                continue
            key = f"{namespace}/{name}" if namespace else name
            if key in self.code_graph.name_to_vertex:
                continue
            info = _SymbolInfo(
                key=key,
                file_path=file_path,
                display_name=f"{name}()",
                symbol_type=NODE_TYPE_FUNCTION,
            )
            self.code_graph.current_file = file_path
            self.code_graph.add_symbol_node(
                key,
                node.start_point[0],
                node.start_point[0],
                node.end_point[0],
                NODE_TYPE_FUNCTION,
            )
            self._set_unified_name(info)
            self.code_graph._add_edge(file_path, key, EDGE_TYPE_CONTAIN)

    def _source_path(self, file_path: str) -> Path | None:
        if self.project_root is None:
            return None
        source_path = self.project_root / file_path
        return source_path if source_path.is_file() else None

    @classmethod
    def _get_php_parser(cls):
        if cls._php_parser is None:
            language = get_language("php")
            try:
                cls._php_parser = Parser(language)
            except TypeError:
                parser = Parser()
                if hasattr(parser, "set_language"):
                    parser.set_language(language)
                else:
                    parser.language = language
                cls._php_parser = parser
        return cls._php_parser

    def _iter_top_level_function_nodes(self, root, source: bytes):
        namespace = ""
        for child in root.children:
            if child.type in {"php_tag", "ERROR"}:
                continue
            if child.type == "namespace_definition":
                namespace = self._namespace_name(child, source)
                compound = self._first_child(child, "compound_statement")
                if compound is not None:
                    yield from self._iter_compound_top_level_functions(
                        compound,
                        namespace,
                    )
                continue
            if child.type == "function_definition":
                yield child, namespace

    def _iter_compound_top_level_functions(self, compound, namespace: str):
        for child in compound.children:
            if child.type == "function_definition":
                yield child, namespace

    def _namespace_name(self, node, source: bytes) -> str:
        namespace = self._first_child(node, "namespace_name")
        if namespace is None:
            return ""
        return (
            source[namespace.start_byte : namespace.end_byte]
            .decode("utf-8")
            .replace("\\", "/")
        )

    def _node_name(self, node, source: bytes) -> str:
        name = self._first_child(node, "name")
        if name is None:
            return ""
        return source[name.start_byte : name.end_byte].decode("utf-8")

    def _first_child(self, node, child_type: str):
        for child in node.children:
            if child.type == child_type:
                return child
        return None
