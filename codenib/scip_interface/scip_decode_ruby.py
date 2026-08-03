#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
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
from .scip_decode_utils import (
    format_unified_name,
    normalize_scip_descriptor,
    scip_symbol_descriptor,
)
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

    def _process_document(self, document_text: str) -> None:
        file_path = self._document_file_path(document_text)
        super()._process_document(document_text)
        if file_path and self._is_source_file(file_path):
            self._add_alias_definitions(file_path)

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
        if symbol and symbol.startswith("local "):
            return None
        descriptor = scip_symbol_descriptor(symbol, self._SCIP_SYMBOL_PREFIXES)
        if not descriptor:
            return None

        descriptor = normalize_scip_descriptor(
            descriptor,
            strip_empty_call=False,
            strip_trailing_hash=False,
        )
        descriptor = _normalize_singleton_class_descriptor(descriptor)
        descriptor = normalize_scip_descriptor(
            descriptor,
            strip_trailing_dot=False,
        )
        return descriptor or None

    def _set_unified_name(self, info) -> None:
        display_name = self._display_names_by_graph_key.get(info.key)
        if display_name is None:
            super()._set_unified_name(info)
            return
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
        nested_display = self._nested_method_display_name(info, file_path)
        if nested_display:
            return nested_display

        display_name = info.display_name
        display_name = self._top_level_singleton_display_name(
            info,
            file_path=file_path,
            line=line,
            display_name=display_name,
        )
        if not display_name.endswith("=()"):
            return display_name
        if info.symbol_type not in {NODE_TYPE_FUNCTION, NODE_TYPE_METHOD}:
            return display_name
        if not re.search(
            r"\battr_(?:accessor|writer)\b", self._source_line(file_path, line)
        ):
            return display_name
        return f"{display_name[:-3]}()"

    def _nested_method_display_name(self, info, file_path: str) -> str | None:
        if info.symbol_type not in {NODE_TYPE_FUNCTION, NODE_TYPE_METHOD}:
            return None
        parent_display = self._current_method_scope_display(file_path)
        if not parent_display:
            return None
        _, member = self._split_owner_member(info.key)
        if not member:
            return None
        return f"{parent_display}.{member}()"

    def _current_method_scope_display(self, file_path: str) -> str | None:
        scope = self.code_graph.current_scope
        if not scope or scope == self.code_graph.current_file:
            return None
        vid = self.code_graph.name_to_vertex.get(scope)
        if vid is None:
            return None
        vertex = self.code_graph.graph.vs[vid]
        attrs = vertex.attributes()
        if attrs.get("type") not in {NODE_TYPE_FUNCTION, NODE_TYPE_METHOD}:
            return None
        unified_name = attrs.get("unified_name")
        prefix = f"{file_path}:"
        if not unified_name or not unified_name.startswith(prefix):
            return None
        return unified_name[len(prefix) :]

    def _top_level_singleton_display_name(
        self,
        info,
        *,
        file_path: str,
        line: int,
        display_name: str,
    ) -> str:
        owner, member = self._split_owner_member(info.key)
        if (
            owner != "Object"
            or not member
            or info.symbol_type not in {NODE_TYPE_FUNCTION, NODE_TYPE_METHOD}
        ):
            return display_name
        source_line = self._source_line(file_path, line)
        if re.search(rf"\bdef\s+[A-Za-z_]\w*\.{re.escape(member)}\b", source_line):
            return f"{member}()"
        return display_name

    def _add_alias_definitions(self, file_path: str) -> None:
        lines = self._source_lines(file_path)
        if not lines:
            return

        alias_lines = [
            (line_index, alias_pair)
            for line_index, line in enumerate(lines)
            if (alias_pair := _parse_ruby_alias(line)) is not None
        ]
        if not alias_lines:
            return

        method_definitions = self._method_definitions_by_member(file_path)
        for line_index, alias_pair in alias_lines:
            alias_member, original_member = alias_pair
            original = self._nearest_definition_before(
                method_definitions.get(original_member, ()),
                line_index,
            )
            if original is None:
                continue

            original_key, original_type = original
            owner, _ = self._split_owner_member(original_key)
            if not owner:
                continue
            parent = self._file_definition_keys.get((file_path, owner))
            if parent not in self.code_graph.name_to_vertex:
                continue

            alias_key = f"{owner}#{alias_member}"
            if alias_key in self.code_graph.name_to_vertex:
                continue
            display_name = self._symbol_display(alias_key, original_type)
            self.code_graph._add_vertex(
                alias_key,
                {
                    "type": original_type,
                    "file": file_path,
                    "start_line": line_index,
                    "end_line": line_index,
                    "selection_line": line_index,
                    "unified_name": f"{file_path}:{display_name}",
                    "has_definition": True,
                },
            )
            self.code_graph.symbol_ranges[alias_key] = (line_index, line_index)
            self.code_graph._add_edge(parent, alias_key, EDGE_TYPE_CONTAIN)

    def _source_lines(self, file_path: str) -> tuple[str, ...] | None:
        if not self.project_root:
            return None
        if file_path not in self._source_lines_by_file:
            try:
                source_path = self.project_root / file_path
                self._source_lines_by_file[file_path] = tuple(
                    source_path.read_text(encoding="utf-8").splitlines()
                )
            except OSError:
                self._source_lines_by_file[file_path] = None
        return self._source_lines_by_file[file_path]

    def _source_line(self, file_path: str, line: int) -> str:
        lines = self._source_lines(file_path)
        if lines is None or line < 0 or line >= len(lines):
            return ""
        return lines[line]

    def _method_definitions_by_member(
        self,
        file_path: str,
    ) -> dict[str, list[tuple[int, str, str]]]:
        definitions: dict[str, list[tuple[int, str, str]]] = {}
        for vertex in self.code_graph.graph.vs:
            attrs = vertex.attributes()
            if attrs.get("file") != file_path:
                continue
            symbol_type = attrs.get("type")
            if symbol_type not in {NODE_TYPE_FUNCTION, NODE_TYPE_METHOD}:
                continue
            start_line = attrs.get("start_line")
            if not isinstance(start_line, int):
                continue
            owner, member = self._split_owner_member(vertex["name"])
            if not owner or not member:
                continue
            definitions.setdefault(member, []).append(
                (start_line, vertex["name"], symbol_type)
            )
        for values in definitions.values():
            values.sort(key=lambda item: item[0])
        return definitions

    def _nearest_definition_before(
        self,
        definitions: tuple[tuple[int, str, str], ...] | list[tuple[int, str, str]],
        line: int,
    ) -> tuple[str, str] | None:
        best: tuple[int, str, str] | None = None
        for definition in definitions:
            if definition[0] >= line:
                break
            best = definition
        if best is None:
            return None
        return best[1], best[2]

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
        self.code_graph.graph.vs[vid]["unified_name"] = format_unified_name(
            file_path,
            display_name,
            info.key,
        )

    def _containment_parent_in_file(self, key: str, file_path: str) -> str:
        method_scope = self._current_method_scope_vertex(file_path)
        if method_scope is not None:
            return method_scope

        owner, member = self._split_owner_member(key)
        if member:
            file_owner = self._file_definition_keys.get((file_path, owner))
            if file_owner in self.code_graph.name_to_vertex:
                return file_owner
        return self.code_graph.current_scope

    def _current_method_scope_vertex(self, file_path: str) -> str | None:
        if self._current_method_scope_display(file_path) is None:
            return None
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


def _parse_ruby_alias(line: str) -> tuple[str, str] | None:
    text = line.split("#", 1)[0].strip()
    if not text.startswith("alias "):
        return None
    parts = text.split()
    if len(parts) != 3:
        return None
    alias_member = _clean_alias_token(parts[1])
    original_member = _clean_alias_token(parts[2])
    if not alias_member or not original_member:
        return None
    return alias_member, original_member


def _clean_alias_token(token: str) -> str:
    token = token.strip()
    if token.startswith(":"):
        token = token[1:]
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}:
        token = token[1:-1]
    return token
