#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""SCIP decoder for JVM projects indexed by scip-java."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..graph.code_graph import CodeGraph
from ..log_utils import get_logger, register_scip_logger
from ..types import (
    EDGE_TYPE_CONTAIN,
    NODE_TYPE_CLASS,
    NODE_TYPE_FIELD,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
    NODE_TYPE_SYMBOL,
    ROOT_NODE,
)
from .scip_indexer_base import extract_scip_blocks, extract_symbol


@dataclass(frozen=True, slots=True)
class _SymbolInfo:
    key: str
    file_path: str
    display_name: str
    symbol_type: str


@dataclass(frozen=True, slots=True)
class _DefinitionInfo:
    key: str
    line: int
    symbol_type: str


class SCIPJavaGraphDecoder:
    """Decode scip-java TextFormat output into CodeGraph."""

    _LOG_LANGUAGE = "Java"
    _SOURCE_SUFFIXES = (".java", ".kt", ".scala")
    _SCIP_SYMBOL_PREFIXES = {"semanticdb"}
    _EXCLUDED_TOP_LEVEL_DIRS: set[str] = set()
    _REFERENCE_SOURCE_FROM_DEFINITION_LINES = False
    _KEEP_NAMESPACE_SYMBOLS = False
    _NAMESPACE_OCCURRENCES_ARE_DEFINITIONS = False
    _TYPE_KINDS = {"Class", "Interface", "Enum", "Object", "Trait"}
    _METHOD_KINDS = {"Method", "Constructor"}
    _FIELD_KINDS = {"Field", "Property", "EnumMember", "Constant", "Variable"}

    def __init__(self, index_file_path, project_root=None):
        self.index_file_path = index_file_path
        self.project_root = Path(project_root) if project_root else None
        self.code_graph = CodeGraph(project_root)
        self.indexed_directories: set[str] = set()
        self.symbols: dict[str, _SymbolInfo] = {}
        self.definition_lines: dict[str, list[_DefinitionInfo]] = {}
        self.logger = get_logger(__name__)
        register_scip_logger(__name__)

    def decode(self) -> CodeGraph:
        self.logger.info(
            "Starting SCIP %s decode from %s",
            self._LOG_LANGUAGE,
            self.index_file_path,
        )
        try:
            content = Path(self.index_file_path).read_text(encoding="utf-8")
        except Exception as exc:
            self.logger.error("Error reading SCIP Java index file: %s", exc)
            raise

        document_blocks = extract_scip_blocks(content, "documents")
        self._collect_symbol_info(document_blocks)

        self.code_graph.add_root_node(ROOT_NODE)
        with self.code_graph.batch_edges():
            for document in document_blocks:
                self._process_document(document)
        return self.code_graph

    def _collect_symbol_info(self, documents: list[str]) -> None:
        for document in documents:
            file_path = self._document_file_path(document)
            if not file_path or not self._is_source_file(file_path):
                continue
            for block in extract_scip_blocks(document, "symbols"):
                symbol = extract_symbol(block)
                key = self._make_symbol_key(symbol) if symbol else None
                if not key:
                    continue
                symbol_type = self._symbol_type_from_block(block, symbol or "")
                display_name = self._display_name_from_block(block)
                signature_text = self._signature_text_from_block(block)
                self.symbols[key] = _SymbolInfo(
                    key=key,
                    file_path=file_path,
                    display_name=self._symbol_display(
                        key,
                        symbol_type,
                        display_name=display_name,
                        signature_text=signature_text,
                    ),
                    symbol_type=symbol_type,
                )

    def _process_document(self, document_text: str) -> None:
        file_path = self._document_file_path(document_text)
        if not file_path or not self._is_source_file(file_path):
            return

        if self._REFERENCE_SOURCE_FROM_DEFINITION_LINES:
            self._collect_document_definitions(document_text, file_path)
        self._add_file_hierarchy(file_path)
        for occurrence in self._document_occurrences(document_text):
            self._process_occurrence(occurrence, file_path)

    def _document_occurrences(self, document_text: str) -> list[str]:
        return extract_scip_blocks(document_text, "occurrences")

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
        if self._is_constructor_key(key):
            return

        roles_match = re.search(r"symbol_roles:\s*(\d+)", occurrence_text)
        symbol_roles = int(roles_match.group(1)) if roles_match else 0
        if (
            self._is_namespace_symbol(symbol)
            and self._NAMESPACE_OCCURRENCES_ARE_DEFINITIONS
        ):
            symbol_roles |= 1
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
                line=line,
                enclosing_ranges=enclosing_ranges,
            )
        else:
            self._add_reference(info, anchor_line=line)

    def _add_definition(
        self,
        info: _SymbolInfo,
        *,
        line: int,
        enclosing_ranges: list[int],
    ) -> None:
        start_line, end_line = self._scope_lines(line, enclosing_ranges)
        self.code_graph.add_symbol_node(
            info.key,
            line,
            start_line,
            end_line,
            info.symbol_type,
        )
        self._set_unified_name(info)
        parent = self._containment_parent(info.key)
        self.code_graph._add_edge(parent, info.key, EDGE_TYPE_CONTAIN)

        if (
            start_line is not None
            and end_line is not None
            and info.symbol_type
            in {NODE_TYPE_CLASS, NODE_TYPE_FUNCTION, NODE_TYPE_METHOD}
        ):
            self.code_graph.update_current_scope(info.key, start_line, end_line)

    def _add_reference(self, info: _SymbolInfo, *, anchor_line: int) -> None:
        previous_scope = self.code_graph.current_scope
        source = self._reference_source(info.file_path, anchor_line)
        if source:
            self.code_graph.current_scope = source
        self.code_graph.add_symbol_reference(
            info.key,
            module_path=info.file_path,
            symbol_type=info.symbol_type,
            anchor_line=anchor_line,
        )
        self.code_graph.current_scope = previous_scope
        self._set_unified_name(info)

    def _collect_document_definitions(self, document_text: str, file_path: str) -> None:
        definitions: list[_DefinitionInfo] = []
        for occurrence in extract_scip_blocks(document_text, "occurrences"):
            roles_match = re.search(r"symbol_roles:\s*(\d+)", occurrence)
            symbol_roles = int(roles_match.group(1)) if roles_match else 0
            ranges = [int(value) for value in re.findall(r"range:\s*(\d+)", occurrence)]
            if len(ranges) < 3:
                continue
            symbol = extract_symbol(occurrence)
            key = self._make_symbol_key(symbol) if symbol else None
            if not key or self._is_constructor_key(key):
                continue
            if (
                self._is_namespace_symbol(symbol)
                and self._NAMESPACE_OCCURRENCES_ARE_DEFINITIONS
            ):
                symbol_roles |= 1
            if not symbol_roles & 1:
                continue
            info = self.symbols.get(key) or self._fallback_symbol_info(
                key,
                file_path=file_path,
                original_symbol=symbol or "",
            )
            definitions.append(
                _DefinitionInfo(
                    key=key,
                    line=ranges[0],
                    symbol_type=info.symbol_type,
                )
            )
        self.definition_lines[file_path] = sorted(
            definitions,
            key=lambda definition: definition.line,
        )

    def _reference_source(self, file_path: str, anchor_line: int) -> str | None:
        if not self._REFERENCE_SOURCE_FROM_DEFINITION_LINES:
            return None
        definitions = self.definition_lines.get(file_path, ())
        for definition in reversed(definitions):
            if (
                definition.line <= anchor_line
                and definition.key in self.code_graph.name_to_vertex
            ):
                if definition.symbol_type in {
                    NODE_TYPE_METHOD,
                    NODE_TYPE_FUNCTION,
                    NODE_TYPE_CLASS,
                }:
                    return definition.key
        return None

    def _add_file_hierarchy(self, file_path: str) -> None:
        dir_path = Path(file_path).parent
        while dir_path != dir_path.parent:
            dir_text = str(dir_path)
            if dir_text not in self.indexed_directories:
                self.code_graph.add_directory_node(dir_text)
                self.indexed_directories.add(dir_text)
                self.code_graph._add_edge(
                    str(dir_path.parent),
                    dir_text,
                    EDGE_TYPE_CONTAIN,
                )
            dir_path = dir_path.parent

        self.code_graph.add_file_node(file_path)
        self.code_graph._add_edge(
            str(Path(file_path).parent),
            file_path,
            EDGE_TYPE_CONTAIN,
        )

    def _set_unified_name(self, info: _SymbolInfo) -> None:
        if info.key not in self.code_graph.name_to_vertex:
            return
        vid = self.code_graph.name_to_vertex[info.key]
        self.code_graph.graph.vs[vid]["unified_name"] = (
            f"{info.file_path}:{info.display_name}"
            if info.file_path and info.display_name
            else info.display_name or info.key
        )

    def _fallback_symbol_info(
        self,
        key: str,
        *,
        file_path: str,
        original_symbol: str,
    ) -> _SymbolInfo:
        symbol_type = self._symbol_type_from_descriptor(original_symbol)
        return _SymbolInfo(
            key=key,
            file_path=file_path,
            display_name=self._symbol_display(key, symbol_type),
            symbol_type=symbol_type,
        )

    def _make_symbol_key(self, symbol: str | None) -> str | None:
        if not symbol or symbol.startswith("local "):
            return None
        parts = symbol.split(" ")
        if len(parts) < 5 or parts[0] not in self._SCIP_SYMBOL_PREFIXES:
            return None
        descriptor = parts[-1].replace("`", "")
        if descriptor.endswith("/"):
            namespace = descriptor[:-1]
            return namespace if self._KEEP_NAMESPACE_SYMBOLS and namespace else None
        if "/" not in descriptor:
            return None
        descriptor = descriptor.rstrip(".")
        if descriptor.endswith("()"):
            descriptor = descriptor[:-2]
        if descriptor.endswith("#"):
            descriptor = descriptor[:-1]
        if not descriptor:
            return None
        return descriptor

    def _symbol_display(
        self,
        key: str,
        symbol_type: str,
        *,
        display_name: str = "",
        signature_text: str = "",
    ) -> str:
        owner, member = self._split_owner_member(key)
        owner_name = self._owner_display_name(owner)
        if symbol_type == NODE_TYPE_CLASS:
            member_name = self._clean_member_display(display_name or member)
            if owner_name and member:
                return f"{owner_name}.{member_name}"
            return owner_name or self._clean_member_display(key.rsplit("/", 1)[-1])
        if symbol_type in {NODE_TYPE_METHOD, NODE_TYPE_FUNCTION}:
            member_name = self._clean_member_display(display_name or member)
            if member_name == "<init>":
                return f"{owner_name}()"
            params = self._signature_parameter_types(signature_text)
            if owner_name and member:
                if params:
                    return f"{owner_name}.{member_name}({params})()"
                return f"{owner_name}.{member_name}()"
            function_name = self._clean_member_display(
                display_name or key.rsplit("/", 1)[-1]
            )
            if params:
                return f"{function_name}({params})()"
            return f"{function_name}()"
        if symbol_type == NODE_TYPE_FIELD:
            member_name = self._clean_member_display(display_name or member)
            if owner_name and member:
                return f"{owner_name}.{member_name}"
            return member_name or self._clean_member_display(key.rsplit("/", 1)[-1])
        return display_name or member or key.rsplit("/", 1)[-1]

    def _split_owner_member(self, key: str) -> tuple[str, str]:
        if "#" not in key:
            slash_index = key.rfind("/")
            dot_index = key.rfind(".")
            if dot_index > slash_index:
                return key[:dot_index], key[dot_index + 1 :]
            return key, ""
        owner, member = key.rsplit("#", 1)
        return owner, member

    def _owner_display_name(self, owner: str) -> str:
        if not owner:
            return ""
        return owner.rsplit("/", 1)[-1].replace("#", ".").strip(".")

    def _clean_member_display(self, member: str) -> str:
        member = member.replace("`", "")
        return re.sub(r"\(\+\d+\)$", "", member)

    def _containment_parent(self, key: str) -> str:
        owner, member = self._split_owner_member(key)
        if member and owner in self.code_graph.name_to_vertex:
            return owner
        return self.code_graph.current_scope

    def _is_constructor_key(self, key: str) -> bool:
        return key.endswith("#<init>") or key.endswith("#.ctor")

    def _is_namespace_symbol(self, symbol: str | None) -> bool:
        return bool(symbol and symbol.split(" ")[-1].endswith("/"))

    def _symbol_type_from_block(self, block: str, original_symbol: str) -> str:
        kind_match = re.search(r"kind:\s*(\w+)", block)
        if kind_match:
            kind = kind_match.group(1)
            if kind in self._TYPE_KINDS:
                return NODE_TYPE_CLASS
            if kind in self._METHOD_KINDS:
                return NODE_TYPE_METHOD
            if kind in self._FIELD_KINDS:
                return NODE_TYPE_FIELD
        return self._symbol_type_from_descriptor(original_symbol)

    def _symbol_type_from_descriptor(self, symbol: str) -> str:
        descriptor = symbol.split(" ")[-1] if symbol else ""
        if descriptor.endswith("/"):
            return NODE_TYPE_CLASS
        if descriptor.endswith("#"):
            return NODE_TYPE_CLASS
        if "#" in descriptor and descriptor.endswith("()."):
            return NODE_TYPE_METHOD
        if "#" in descriptor and descriptor.endswith("."):
            return NODE_TYPE_FIELD
        if descriptor.endswith("()."):
            return NODE_TYPE_FUNCTION
        return NODE_TYPE_SYMBOL

    def _display_name_from_block(self, block: str) -> str:
        match = re.search(r'display_name:\s*"([^"]*)"', block)
        return match.group(1) if match else ""

    def _signature_text_from_block(self, block: str) -> str:
        match = re.search(r'text:\s*"((?:\\.|[^"])*)"', block)
        if not match:
            return ""
        return (
            match.group(1)
            .replace("\\n", "\n")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )

    def _signature_parameter_types(self, signature_text: str) -> str:
        if not signature_text:
            return ""
        matches = re.findall(r"\(([^()]*)\)", signature_text)
        if not matches:
            return ""
        params = matches[-1].strip()
        if not params:
            return ""
        return ",".join(
            self._parameter_type(param)
            for param in self._split_parameters(params)
            if param.strip()
        )

    def _split_parameters(self, params: str) -> list[str]:
        result: list[str] = []
        start = 0
        depth = 0
        for index, char in enumerate(params):
            if char == "<":
                depth += 1
            elif char == ">" and depth:
                depth -= 1
            elif char == "," and depth == 0:
                result.append(params[start:index].strip())
                start = index + 1
        result.append(params[start:].strip())
        return result

    def _parameter_type(self, param: str) -> str:
        cleaned = re.sub(r"@\w+(?:\([^)]*\))?\s*", "", param).strip()
        cleaned = re.sub(r"\bfinal\s+", "", cleaned)
        if " " not in cleaned:
            return cleaned
        return cleaned.rsplit(" ", 1)[0].strip()

    def _document_file_path(self, document_text: str) -> str | None:
        match = re.search(r'relative_path:\s*"([^"]+)"', document_text)
        return match.group(1) if match else None

    def _is_source_file(self, file_path: str) -> bool:
        path = Path(file_path)
        if path.parts and path.parts[0] in self._EXCLUDED_TOP_LEVEL_DIRS:
            return False
        return file_path.endswith(self._SOURCE_SUFFIXES)

    def _scope_lines(
        self,
        line: int,
        enclosing_ranges: list[int],
    ) -> tuple[int | None, int | None]:
        if len(enclosing_ranges) >= 4:
            return enclosing_ranges[0], enclosing_ranges[2]
        if len(enclosing_ranges) == 3:
            return enclosing_ranges[0], enclosing_ranges[0]
        return None, None

    def save_graph(self, output_path):
        self.code_graph.save_graph(output_path)
