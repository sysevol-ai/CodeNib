#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""SCIP decoder for Ruby projects indexed by scip-ruby."""

from __future__ import annotations

import re

from ..types import (
    EDGE_TYPE_CONTAIN,
    NODE_TYPE_CLASS,
    NODE_TYPE_FIELD,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
)
from .scip_decode_java import SCIPJavaGraphDecoder
from .scip_indexer_base import extract_symbol


class SCIPRubyGraphDecoder(SCIPJavaGraphDecoder):
    """Decode scip-ruby TextFormat output into CodeGraph."""

    _LOG_LANGUAGE = "Ruby"
    _SOURCE_SUFFIXES = (".rb",)
    _SCIP_SYMBOL_PREFIXES = {"scip-ruby"}
    _REFERENCE_SOURCE_FROM_DEFINITION_LINES = True

    def __init__(self, index_file_path, project_root=None):
        super().__init__(index_file_path, project_root=project_root)
        self._file_definition_keys: dict[tuple[str, str], str] = {}
        self._display_names_by_graph_key: dict[str, str] = {}
        self._source_lines_by_file: dict[str, tuple[str, ...] | None] = {}

    def _process_occurrence(self, occurrence_text: str, file_path: str) -> None:
        ranges = [
            int(value) for value in re.findall(r"range:\s*(\d+)", occurrence_text)
        ]
        if len(ranges) < 3:
            return

        symbol = extract_symbol(occurrence_text)
        key = self._make_symbol_key(symbol) if symbol else None
        if not key:
            return

        roles_match = re.search(r"symbol_roles:\s*(\d+)", occurrence_text)
        symbol_roles = int(roles_match.group(1)) if roles_match else 0
        line = ranges[0]
        enclosing_ranges = [
            int(value)
            for value in re.findall(r"enclosing_range:\s*(\d+)", occurrence_text)
        ]

        self.code_graph.exit_scopes_by_line(line)
        info = self.symbols.get(key) or self._fallback_symbol_info(
            key,
            file_path=file_path,
            original_symbol=symbol or "",
        )
        if symbol_roles & 1:
            self._add_definition(
                info,
                file_path=file_path,
                line=line,
                enclosing_ranges=enclosing_ranges,
            )
        else:
            self._add_reference(info, anchor_line=line)

    def _add_definition(
        self,
        info,
        *,
        line: int,
        enclosing_ranges: list[int],
        file_path: str | None = None,
    ) -> None:
        file_path = file_path or info.file_path
        graph_key = self._definition_graph_key(info.key, info.symbol_type, file_path)
        self._file_definition_keys[(file_path, info.key)] = graph_key

        start_line, end_line = self._scope_lines(line, enclosing_ranges)
        self.code_graph.add_symbol_node(
            graph_key,
            line,
            start_line,
            end_line,
            info.symbol_type,
        )
        display_name = self._definition_display_name(info, file_path, line)
        self._display_names_by_graph_key[graph_key] = display_name
        self._set_unified_name_for_key(
            graph_key,
            info,
            file_path,
            display_name=display_name,
        )
        parent = self._containment_parent_in_file(info.key, file_path)
        self.code_graph._add_edge(parent, graph_key, EDGE_TYPE_CONTAIN)

        if (
            start_line is not None
            and end_line is not None
            and info.symbol_type
            in {NODE_TYPE_CLASS, NODE_TYPE_FUNCTION, NODE_TYPE_METHOD}
        ):
            self.code_graph.update_current_scope(graph_key, start_line, end_line)

    def _reference_source(self, file_path: str, anchor_line: int) -> str | None:
        definitions = self.definition_lines.get(file_path, ())
        for definition in reversed(definitions):
            if definition.line > anchor_line:
                continue
            graph_key = self._file_definition_keys.get(
                (file_path, definition.key),
                definition.key,
            )
            if graph_key not in self.code_graph.name_to_vertex:
                continue
            if definition.symbol_type in {
                NODE_TYPE_METHOD,
                NODE_TYPE_FUNCTION,
                NODE_TYPE_CLASS,
            }:
                return graph_key
        return None

    def _make_symbol_key(self, symbol: str | None) -> str | None:
        if not symbol or symbol.startswith("local "):
            return None
        parts = symbol.split(" ")
        if len(parts) < 5 or parts[0] not in self._SCIP_SYMBOL_PREFIXES:
            return None

        descriptor = parts[-1].replace("`", "").rstrip(".")
        descriptor = _normalize_singleton_class_descriptor(descriptor)
        if descriptor.endswith("()"):
            descriptor = descriptor[:-2]
        if descriptor.endswith("#"):
            descriptor = descriptor[:-1]
        return descriptor or None

    def _set_unified_name(self, info) -> None:
        display_name = self._display_names_by_graph_key.get(info.key)
        if display_name is None:
            return super()._set_unified_name(info)
        self._set_unified_name_for_key(
            info.key,
            info,
            info.file_path,
            display_name=display_name,
        )

    def _symbol_display(
        self,
        key: str,
        symbol_type: str,
        *,
        display_name: str = "",
        signature_text: str = "",
    ) -> str:
        owner, member = self._split_owner_member(key)
        owner_name = self._qualified_name(owner)

        if symbol_type == NODE_TYPE_CLASS:
            if owner_name and member:
                return f"{owner_name}.{member}"
            return member or key
        if symbol_type in {NODE_TYPE_METHOD, NODE_TYPE_FUNCTION}:
            if owner_name and member:
                return f"{owner_name}.{member}()"
            return f"{member or display_name or key}()"
        if symbol_type == NODE_TYPE_FIELD:
            if owner_name and member:
                return f"{owner_name}.{member}"
            return member or display_name or key
        return display_name or member or key

    def _split_owner_member(self, key: str) -> tuple[str, str]:
        if "#" not in key:
            return "", key
        owner, member = key.rsplit("#", 1)
        return owner, member

    def _qualified_name(self, key: str) -> str:
        return key.replace("#", ".").strip(".")

    def _definition_graph_key(
        self,
        key: str,
        symbol_type: str,
        file_path: str,
    ) -> str:
        if symbol_type == NODE_TYPE_CLASS and "#" not in key:
            return f"{file_path}:{key}"
        return key

    def _definition_display_name(self, info, file_path: str, line: int) -> str:
        display_name = info.display_name
        if not display_name.endswith("=()"):
            return display_name
        if info.symbol_type not in {NODE_TYPE_FUNCTION, NODE_TYPE_METHOD}:
            return display_name
        if not re.search(
            r"\battr_(?:accessor|writer)\b", self._source_line(file_path, line)
        ):
            return display_name
        return f"{display_name[:-3]}()"

    def _source_line(self, file_path: str, line: int) -> str:
        if not self.project_root:
            return ""
        if file_path not in self._source_lines_by_file:
            try:
                source_path = self.project_root / file_path
                self._source_lines_by_file[file_path] = tuple(
                    source_path.read_text(encoding="utf-8").splitlines()
                )
            except OSError:
                self._source_lines_by_file[file_path] = None

        lines = self._source_lines_by_file[file_path]
        if lines is None or line < 0 or line >= len(lines):
            return ""
        return lines[line]

    def _set_unified_name_for_key(
        self,
        graph_key: str,
        info,
        file_path: str,
        *,
        display_name: str | None = None,
    ) -> None:
        if graph_key not in self.code_graph.name_to_vertex:
            return
        vid = self.code_graph.name_to_vertex[graph_key]
        display_name = display_name or info.display_name
        self.code_graph.graph.vs[vid]["unified_name"] = (
            f"{file_path}:{display_name}"
            if file_path and display_name
            else display_name or info.key
        )

    def _containment_parent_in_file(self, key: str, file_path: str) -> str:
        owner, member = self._split_owner_member(key)
        if member:
            file_owner = self._file_definition_keys.get((file_path, owner))
            if file_owner in self.code_graph.name_to_vertex:
                return file_owner
            if owner in self.code_graph.name_to_vertex:
                return owner
        return self.code_graph.current_scope


def _normalize_singleton_class_descriptor(descriptor: str) -> str:
    if descriptor.startswith("<Class:") and ">#" in descriptor:
        owner, member = descriptor[len("<Class:") :].split(">#", 1)
        descriptor = f"{owner}#{member}"

    while "#<Class:" in descriptor and ">#" in descriptor:
        prefix, rest = descriptor.split("#<Class:", 1)
        owner, suffix = rest.split(">#", 1)
        descriptor = f"{prefix}#{owner}#{suffix}"

    return descriptor
