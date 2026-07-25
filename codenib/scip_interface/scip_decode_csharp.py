#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""SCIP decoder for C# projects indexed by scip-dotnet."""

from __future__ import annotations

import re
from pathlib import Path

from ..types import (
    NODE_TYPE_CLASS,
    NODE_TYPE_FIELD,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
)
from .scip_decode_java import SCIPJavaGraphDecoder
from .scip_indexer_base import extract_symbol


class SCIPCSharpGraphDecoder(SCIPJavaGraphDecoder):
    """Decode scip-dotnet TextFormat output into CodeGraph."""

    _LOG_LANGUAGE = "C#"
    _SOURCE_SUFFIXES = (".cs",)
    _SCIP_SYMBOL_PREFIXES = {"scip-dotnet"}
    _EXCLUDED_TOP_LEVEL_DIRS = {"bin", "obj"}
    _REFERENCE_SOURCE_FROM_DEFINITION_LINES = True
    _KEEP_NAMESPACE_SYMBOLS = True
    _NAMESPACE_OCCURRENCES_ARE_DEFINITIONS = True

    def _process_occurrence(self, occurrence_text: str, file_path: str) -> None:
        ranges = [
            int(value) for value in re.findall(r"range:\s*(\d+)", occurrence_text)
        ]
        symbol = extract_symbol(occurrence_text)
        if (
            len(ranges) >= 3
            and self._is_namespace_symbol(symbol)
            and "symbol_roles:" not in occurrence_text
            and self._is_using_namespace_occurrence(file_path, ranges[0])
        ):
            return
        super()._process_occurrence(occurrence_text, file_path)

    def _make_symbol_key(self, symbol: str | None) -> str | None:
        key = super()._make_symbol_key(symbol)
        if key and re.search(r"\)\.\([^)]+\)$", key):
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
        owner_name = owner.replace("/", ".") if owner else ""
        params = self._signature_parameter_types(signature_text)

        if symbol_type == NODE_TYPE_CLASS:
            return key.replace("/", ".")
        if symbol_type in {NODE_TYPE_METHOD, NODE_TYPE_FUNCTION}:
            function_name = display_name or key.rsplit("/", 1)[-1]
            if owner_name and member:
                if params:
                    return f"{owner_name}.{member}({params})()"
                return f"{owner_name}.{member}()"
            if params:
                return f"{function_name}({params})()"
            return f"{function_name}()"
        if symbol_type == NODE_TYPE_FIELD:
            if owner_name and member:
                return f"{owner_name}.{member}"
            return member or display_name or key.rsplit("/", 1)[-1]
        return display_name or member or key.rsplit("/", 1)[-1]

    def _signature_text_from_block(self, block: str) -> str:
        signature_text = super()._signature_text_from_block(block)
        if signature_text:
            return signature_text

        match = re.search(r'documentation:\s*"((?:\\.|[^"])*)"', block)
        if not match:
            return ""
        documentation = (
            match.group(1)
            .replace("\\n", "\n")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )
        fenced = re.search(r"```(?:cs|csharp)?\n(.*?)\n```", documentation, re.DOTALL)
        if fenced:
            return fenced.group(1).strip()
        return documentation.strip()

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
            self._parameter_declaration(param)
            for param in self._split_parameters(params)
            if param.strip()
        )

    def _parameter_declaration(self, param: str) -> str:
        cleaned = re.sub(r"@\w+(?:\([^)]*\))?\s*", "", param).strip()
        cleaned = re.sub(r"\b(?:final|readonly)\s+", "", cleaned)
        return cleaned

    def _containment_parent(self, key: str) -> str:
        owner, member = self._split_owner_member(key)
        if member and owner in self.code_graph.name_to_vertex:
            return owner
        if "/" in key:
            namespace = key.rsplit("/", 1)[0]
            if namespace in self.code_graph.name_to_vertex:
                return namespace
        return self.code_graph.current_scope

    def _is_using_namespace_occurrence(self, file_path: str, line: int) -> bool:
        if self.project_root is None:
            return False
        source_path = Path(self.project_root) / file_path
        try:
            source_line = source_path.read_text(
                encoding="utf-8-sig", errors="replace"
            ).splitlines()[line]
        except (OSError, IndexError):
            return False
        return source_line.strip().startswith("using ")
