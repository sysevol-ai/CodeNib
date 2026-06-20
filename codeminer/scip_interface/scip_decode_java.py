#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""SCIP decoder for JVM projects indexed by scip-java."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Parser
from tree_sitter_language_pack import get_language

from ..graph.code_graph import CodeGraph
from ..log_utils import get_logger, register_scip_logger
from ..types import (
    EDGE_TYPE_CONTAIN,
    NODE_TYPE_CLASS,
    NODE_TYPE_FIELD,
    NODE_TYPE_FILE,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
    NODE_TYPE_SYMBOL,
    ROOT_NODE,
)
from .scip_decode_utils import (
    format_unified_name,
    normalize_scip_descriptor,
    scip_symbol_descriptor,
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
        self.code_graph.graph.vs[vid]["unified_name"] = format_unified_name(
            info.file_path,
            info.display_name,
            info.key,
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
        descriptor = scip_symbol_descriptor(symbol, self._SCIP_SYMBOL_PREFIXES)
        if not descriptor:
            return None
        if descriptor.endswith("/"):
            namespace = descriptor[:-1]
            return namespace if self._KEEP_NAMESPACE_SYMBOLS and namespace else None
        if "/" not in descriptor:
            return None
        descriptor = normalize_scip_descriptor(descriptor)
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


class SCIPKotlinGraphDecoder(SCIPJavaGraphDecoder):
    """Kotlin-specific normalization on top of scip-java's JVM output."""

    _LOG_LANGUAGE = "Kotlin"
    _KOTLIN_PARAMETER_DESCRIPTOR_RE = re.compile(r"(?:\(\)|\(\+\d+\))\.\([^)]+\)$")
    _KOTLIN_TYPE_PARAMETER_DESCRIPTOR_RE = re.compile(r"(?:#|\.)\[[^\]]+\]$")
    _KOTLIN_SYNTHETIC_GETTER_DESCRIPTOR_RE = re.compile(r"#get[A-Z][^/#.]*\(\)\.$")
    _KOTLIN_PROPERTY_ACCESSOR_DESCRIPTOR_RE = re.compile(
        r"(?:^|[#/])(?:get|set)[A-Z_][^/#.]*\(\)\.$"
    )
    _KOTLIN_OVERLOAD_DESCRIPTOR_RE = re.compile(r"\(\+\d+\)\.$")
    _kotlin_parser = None

    def _collect_symbol_info(self, documents: list[str]) -> None:
        self._kotlin_generated_enum_helper_descriptors = (
            self._collect_generated_enum_helper_descriptors(documents)
        )
        self._kotlin_generated_property_accessor_descriptors = (
            self._collect_generated_property_accessor_descriptors(documents)
        )
        super()._collect_symbol_info(documents)

    def decode(self) -> CodeGraph:
        graph = super().decode()
        for file_path in self._decoded_kotlin_files():
            self._add_missing_enum_entries(file_path)
            self._add_missing_top_level_functions(file_path)
            self._add_missing_explicit_property_accessors(file_path)
            self._add_missing_top_level_object_override_aliases(file_path)
        return graph

    def _make_symbol_key(self, symbol: str | None) -> str | None:
        descriptor = self._symbol_descriptor(symbol)
        if not descriptor or self._is_kotlin_non_graph_descriptor(descriptor):
            return None
        key = super()._make_symbol_key(symbol)
        if key and (
            key.endswith("#Companion")
            or key.endswith("#Companion#<init>")
            or key.endswith("#Companion#Companion")
        ):
            return None
        return key

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
        member_name = self._clean_member_display(display_name or member)
        if (
            symbol_type == NODE_TYPE_METHOD
            and owner_name
            and member_name in {"<init>", owner_name.rsplit(".", 1)[-1]}
        ):
            return f"{owner_name}.constructor()"
        return super()._symbol_display(
            key,
            symbol_type,
            display_name=display_name,
            signature_text=signature_text,
        )

    def _owner_display_name(self, owner: str) -> str:
        display_name = super()._owner_display_name(owner)
        parts = [
            part for part in display_name.split(".") if part and part != "Companion"
        ]
        return ".".join(parts)

    def _containment_parent(self, key: str) -> str:
        owner, member = self._split_owner_member(key)
        if member:
            companion_owner = self._companion_containing_owner(owner)
            if companion_owner and companion_owner in self.code_graph.name_to_vertex:
                return companion_owner
        return super()._containment_parent(key)

    def _companion_containing_owner(self, owner: str) -> str | None:
        parts = owner.split("#")
        original_len = len(parts)
        while parts and parts[-1] == "Companion":
            parts.pop()
        if len(parts) == original_len:
            return None
        return "#".join(parts) or None

    def _symbol_descriptor(self, symbol: str | None) -> str:
        return scip_symbol_descriptor(symbol, self._SCIP_SYMBOL_PREFIXES)

    def _is_kotlin_non_graph_descriptor(self, descriptor: str) -> bool:
        normalized = descriptor.rstrip(".")
        return bool(
            self._KOTLIN_PARAMETER_DESCRIPTOR_RE.search(normalized)
            or self._KOTLIN_TYPE_PARAMETER_DESCRIPTOR_RE.search(normalized)
            or self._KOTLIN_SYNTHETIC_GETTER_DESCRIPTOR_RE.search(descriptor)
            or descriptor
            in getattr(self, "_kotlin_generated_enum_helper_descriptors", set())
            or descriptor
            in getattr(
                self,
                "_kotlin_generated_property_accessor_descriptors",
                set(),
            )
        )

    def _symbol_type_from_block(self, block: str, original_symbol: str) -> str:
        if "```kotlin\\nfield:" in block:
            return NODE_TYPE_FIELD
        return super()._symbol_type_from_block(block, original_symbol)

    def _collect_generated_enum_helper_descriptors(
        self, documents: list[str]
    ) -> set[str]:
        descriptors: set[str] = set()
        for document in documents:
            file_path = self._document_file_path(document)
            if not file_path or not self._is_source_file(file_path):
                continue
            for block in extract_scip_blocks(document, "symbols"):
                symbol = extract_symbol(block)
                descriptor = self._symbol_descriptor(symbol)
                if descriptor and self._is_generated_enum_helper_block(
                    descriptor,
                    block,
                ):
                    descriptors.add(descriptor)
        return descriptors

    def _is_generated_enum_helper_block(self, descriptor: str, block: str) -> bool:
        if descriptor.endswith("#entries."):
            return "static val entries: EnumEntries<" in block
        if descriptor.endswith("#values()."):
            return "static fun values(): Array<" in block
        if descriptor.endswith("#valueOf()."):
            return "static fun valueOf(value: String):" in block
        return False

    def _collect_generated_property_accessor_descriptors(
        self, documents: list[str]
    ) -> set[str]:
        descriptors: set[str] = set()
        for document in documents:
            file_path = self._document_file_path(document)
            if not file_path or not self._is_source_file(file_path):
                continue
            for block in extract_scip_blocks(document, "symbols"):
                symbol = extract_symbol(block)
                descriptor = self._symbol_descriptor(symbol)
                if descriptor and self._is_generated_property_accessor_block(
                    descriptor,
                    block,
                ):
                    descriptors.add(descriptor)
        return descriptors

    def _is_generated_property_accessor_block(
        self,
        descriptor: str,
        block: str,
    ) -> bool:
        if not self._KOTLIN_PROPERTY_ACCESSOR_DESCRIPTOR_RE.search(descriptor):
            return False
        display_name = self._display_name_from_block(block)
        if not display_name:
            return False

        accessor_name = descriptor.rstrip(".").rsplit("/", 1)[-1].rsplit("#", 1)[-1]
        accessor_name = accessor_name.removesuffix("()")
        if accessor_name.startswith("get"):
            stem = accessor_name[3:]
            signature_re = r'documentation:\s*"```kotlin\\n[^"]*\bget\(\):'
        elif accessor_name.startswith("set"):
            stem = accessor_name[3:]
            signature_re = r'documentation:\s*"```kotlin\\n[^"]*\bset\(value\b'
        else:
            return False

        if not self._accessor_stem_matches_display_name(stem, display_name):
            return False
        return bool(re.search(signature_re, block))

    def _accessor_stem_matches_display_name(
        self,
        stem: str,
        display_name: str,
    ) -> bool:
        if stem == display_name:
            return True
        if not stem:
            return False
        return stem[:1].lower() + stem[1:] == display_name

    def _is_constructor_key(self, key: str) -> bool:
        return False

    def _symbol_type_from_descriptor(self, symbol: str) -> str:
        descriptor = symbol.split(" ")[-1] if symbol else ""
        if self._KOTLIN_OVERLOAD_DESCRIPTOR_RE.search(descriptor):
            return NODE_TYPE_METHOD if "#" in descriptor else NODE_TYPE_FUNCTION
        return super()._symbol_type_from_descriptor(symbol)

    def _add_missing_enum_entries(self, file_path: str) -> None:
        source_path = self._source_path(file_path)
        if source_path is None:
            return

        try:
            source = source_path.read_bytes()
            root = self._get_kotlin_parser().parse(source).root_node
        except Exception as exc:
            self.logger.warning(
                "Could not parse Kotlin source %s for enum entries: %s",
                file_path,
                exc,
            )
            return

        self.code_graph.current_file = file_path
        for enum_key, enum_display, entry in self._iter_enum_entries(root, source):
            entry_name = self._node_text(
                self._first_child(entry, "simple_identifier"),
                source,
            )
            if not entry_name:
                continue
            base_key = f"{enum_key}#{entry_name}"
            unified_name = f"{file_path}:{enum_display}.{entry_name}"
            if self._has_unified_name(unified_name):
                continue
            key = self._source_supplement_key(
                base_key,
                file_path=file_path,
                display_name=f"{enum_display}.{entry_name}",
                line=entry.start_point[0],
            )
            self.code_graph.add_symbol_node(
                key,
                entry.start_point[0],
                entry.start_point[0],
                entry.end_point[0],
                NODE_TYPE_CLASS,
            )
            vid = self.code_graph.name_to_vertex[key]
            self.code_graph.graph.vs[vid]["unified_name"] = unified_name
            self.code_graph._add_edge(enum_key, key, EDGE_TYPE_CONTAIN)

    def _add_missing_top_level_functions(self, file_path: str) -> None:
        source_path = self._source_path(file_path)
        if source_path is None:
            return

        try:
            source = source_path.read_bytes()
            root = self._get_kotlin_parser().parse(source).root_node
        except Exception as exc:
            self.logger.warning(
                "Could not parse Kotlin source %s for top-level functions: %s",
                file_path,
                exc,
            )
            return

        self.code_graph.current_file = file_path
        for function in self._iter_top_level_functions(root):
            name = self._node_text(
                self._first_child(function, "simple_identifier"),
                source,
            )
            if not name:
                continue
            unified_name = f"{file_path}:{name}()"
            if self._has_unified_name(unified_name):
                continue
            key = self._source_supplement_key(
                f"{file_path}:{name}()",
                file_path=file_path,
                display_name=f"{name}()",
                line=function.start_point[0],
            )
            self.code_graph.add_symbol_node(
                key,
                function.start_point[0],
                function.start_point[0],
                function.end_point[0],
                NODE_TYPE_FUNCTION,
            )
            vid = self.code_graph.name_to_vertex[key]
            self.code_graph.graph.vs[vid]["unified_name"] = unified_name
            self.code_graph._add_edge(file_path, key, EDGE_TYPE_CONTAIN)

    def _add_missing_explicit_property_accessors(self, file_path: str) -> None:
        source_path = self._source_path(file_path)
        if source_path is None:
            return

        try:
            source = source_path.read_bytes()
            root = self._get_kotlin_parser().parse(source).root_node
        except Exception as exc:
            self.logger.warning(
                "Could not parse Kotlin source %s for explicit property accessors: %s",
                file_path,
                exc,
            )
            return

        self.code_graph.current_file = file_path
        for owners, property_node, getter in self._iter_explicit_getter_properties(
            root,
            source,
        ):
            property_name = self._property_name(property_node, source)
            if not property_name:
                continue
            display_name = (
                ".".join((*owners, property_name)) if owners else property_name
            )
            property_unified = f"{file_path}:{display_name}"
            property_key = self._key_for_unified_name(property_unified)
            if property_key is None:
                property_key = self._source_supplement_key(
                    f"{file_path}:{display_name}",
                    file_path=file_path,
                    display_name=display_name,
                    line=property_node.start_point[0],
                )
                self.code_graph.add_symbol_node(
                    property_key,
                    property_node.start_point[0],
                    property_node.start_point[0],
                    property_node.end_point[0],
                    NODE_TYPE_FIELD,
                )
                vid = self.code_graph.name_to_vertex[property_key]
                self.code_graph.graph.vs[vid]["unified_name"] = property_unified
                self.code_graph._add_edge(
                    self._property_owner_key(file_path, owners),
                    property_key,
                    EDGE_TYPE_CONTAIN,
                )

            getter_unified = f"{property_unified}.get()"
            if self._has_unified_name(getter_unified):
                continue
            getter_key = self._source_supplement_key(
                f"{property_key}#get",
                file_path=file_path,
                display_name=f"{display_name}.get()",
                line=getter.start_point[0],
            )
            self.code_graph.add_symbol_node(
                getter_key,
                getter.start_point[0],
                getter.start_point[0],
                getter.end_point[0],
                NODE_TYPE_METHOD if owners else NODE_TYPE_FUNCTION,
            )
            vid = self.code_graph.name_to_vertex[getter_key]
            self.code_graph.graph.vs[vid]["unified_name"] = getter_unified
            self.code_graph._add_edge(property_key, getter_key, EDGE_TYPE_CONTAIN)

    def _add_missing_top_level_object_override_aliases(self, file_path: str) -> None:
        source_path = self._source_path(file_path)
        if source_path is None:
            return

        try:
            source = source_path.read_bytes()
            root = self._get_kotlin_parser().parse(source).root_node
        except Exception as exc:
            self.logger.warning(
                "Could not parse Kotlin source %s for top-level object aliases: %s",
                file_path,
                exc,
            )
            return

        self.code_graph.current_file = file_path
        for object_name, function in self._iter_top_level_object_override_functions(
            root,
            source,
        ):
            name = self._node_text(
                self._first_child(function, "simple_identifier"),
                source,
            )
            if not name:
                continue
            object_unified = f"{file_path}:{object_name}.{name}()"
            if not self._has_unified_name(object_unified):
                continue
            alias_unified = f"{file_path}:{name}()"
            if self._has_unified_name(alias_unified):
                continue
            key = self._source_supplement_key(
                f"{file_path}:{name}()",
                file_path=file_path,
                display_name=f"{name}()",
                line=function.start_point[0],
            )
            self.code_graph.add_symbol_node(
                key,
                function.start_point[0],
                function.start_point[0],
                function.end_point[0],
                NODE_TYPE_FUNCTION,
            )
            vid = self.code_graph.name_to_vertex[key]
            self.code_graph.graph.vs[vid]["unified_name"] = alias_unified
            self.code_graph._add_edge(file_path, key, EDGE_TYPE_CONTAIN)

    def _source_supplement_key(
        self,
        base_key: str,
        *,
        file_path: str,
        display_name: str,
        line: int,
    ) -> str:
        if base_key not in self.code_graph.name_to_vertex:
            return base_key
        return f"{file_path}:{display_name}:{line}"

    def _decoded_kotlin_files(self) -> list[str]:
        return sorted(
            vertex["name"]
            for vertex in self.code_graph.graph.vs
            if vertex.attributes().get("type") == NODE_TYPE_FILE
            and str(vertex["name"]).endswith(".kt")
        )

    def _iter_enum_entries(self, root, source: bytes):
        yield from self._iter_class_nodes(root, source, ())

    def _iter_top_level_functions(self, root):
        for child in root.children:
            if child.type == "function_declaration":
                yield child

    def _iter_top_level_object_override_functions(self, root, source: bytes):
        for child in root.children:
            if child.type != "object_declaration":
                continue
            object_name = self._node_text(
                self._first_child(child, "type_identifier")
                or self._first_child(child, "simple_identifier"),
                source,
            )
            if not object_name:
                continue
            body = self._first_child(child, "class_body")
            if body is None:
                continue
            for member in body.children:
                if member.type == "function_declaration" and self._has_modifier(
                    member,
                    "override",
                    source,
                ):
                    yield object_name, member

    def _iter_explicit_getter_properties(
        self,
        node,
        source: bytes,
        owners: tuple[str, ...] = (),
    ):
        if node.type == "class_declaration":
            name = self._node_text(self._first_child(node, "type_identifier"), source)
            next_owners = (*owners, name) if name else owners
            body = self._first_child(node, "class_body")
            if body is not None:
                yield from self._iter_explicit_getter_properties(
                    body,
                    source,
                    next_owners,
                )
            return
        if node.type == "object_declaration":
            name = self._node_text(self._first_child(node, "type_identifier"), source)
            if not name:
                name = self._node_text(
                    self._first_child(node, "simple_identifier"),
                    source,
                )
            next_owners = (*owners, name) if name else owners
            body = self._first_child(node, "class_body")
            if body is not None:
                yield from self._iter_explicit_getter_properties(
                    body,
                    source,
                    next_owners,
                )
            return

        children = list(node.children)
        for index, child in enumerate(children):
            if child.type == "property_declaration":
                getter = self._first_child(child, "getter")
                if getter is None and index + 1 < len(children):
                    next_child = children[index + 1]
                    if next_child.type == "getter":
                        getter = next_child
                if getter is not None:
                    yield owners, child, getter
                continue
            if child.type != "getter":
                yield from self._iter_explicit_getter_properties(
                    child,
                    source,
                    owners,
                )

    def _iter_class_nodes(self, node, source: bytes, owners: tuple[str, ...]):
        if node.type == "class_declaration":
            name = self._node_text(self._first_child(node, "type_identifier"), source)
            if name:
                display = ".".join((*owners, name))
                key = self._key_for_display(display)
                next_owners = (*owners, name)
                if key and any(child.type == "enum" for child in node.children):
                    body = self._first_child(node, "enum_class_body")
                    if body is not None:
                        for entry in body.children:
                            if entry.type == "enum_entry":
                                yield key, display, entry
                for child in node.children:
                    yield from self._iter_class_nodes(child, source, next_owners)
                return

        for child in node.children:
            yield from self._iter_class_nodes(child, source, owners)

    def _key_for_display(self, display: str) -> str | None:
        suffix = f":{display}"
        for vertex in self.code_graph.graph.vs:
            unified_name = vertex.attributes().get("unified_name")
            if isinstance(unified_name, str) and unified_name.endswith(suffix):
                return vertex["name"]
        return None

    def _key_for_unified_name(self, unified_name: str) -> str | None:
        for vertex in self.code_graph.graph.vs:
            if vertex.attributes().get("unified_name") == unified_name:
                return vertex["name"]
        return None

    def _property_owner_key(self, file_path: str, owners: tuple[str, ...]) -> str:
        if not owners:
            return file_path
        return self._key_for_display(".".join(owners)) or file_path

    def _has_unified_name(self, unified_name: str) -> bool:
        for vertex in self.code_graph.graph.vs:
            attrs = vertex.attributes()
            if (
                attrs.get("unified_name") == unified_name
                and attrs.get("start_line") is not None
            ):
                return True
        return False

    def _source_path(self, file_path: str) -> Path | None:
        if self.project_root is None:
            return None
        source_path = self.project_root / file_path
        return source_path if source_path.is_file() else None

    @classmethod
    def _get_kotlin_parser(cls):
        if cls._kotlin_parser is None:
            language = get_language("kotlin")
            try:
                cls._kotlin_parser = Parser(language)
            except TypeError:
                parser = Parser()
                if hasattr(parser, "set_language"):
                    parser.set_language(language)
                else:
                    parser.language = language
                cls._kotlin_parser = parser
        return cls._kotlin_parser

    def _first_child(self, node, node_type: str):
        for child in node.children:
            if child.type == node_type:
                return child
        return None

    def _has_modifier(self, node, modifier: str, source: bytes) -> bool:
        modifiers = self._first_child(node, "modifiers")
        if modifiers is None:
            return False
        return modifier in self._node_text(modifiers, source).split()

    def _property_name(self, node, source: bytes) -> str:
        declaration = self._first_child(node, "variable_declaration")
        return self._node_text(
            self._first_child(declaration, "simple_identifier"),
            source,
        )

    def _node_text(self, node, source: bytes) -> str:
        if node is None:
            return ""
        return source[node.start_byte : node.end_byte].decode("utf-8")
