#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Lua-specific code chunker implementation."""

from __future__ import annotations

from typing import List, Optional, Tuple

from .base import BaseCodeChunker


class LuaCodeChunker(BaseCodeChunker):
    """Code chunker for Lua source files."""

    def __init__(
        self,
        max_lines_per_chunk: Optional[int] = None,
        chunk_depth: int = 2,
        l2_level_exclusive: bool = True,
        **kwargs,
    ):
        super().__init__(
            "lua",
            max_lines_per_chunk=max_lines_per_chunk,
            chunk_depth=chunk_depth,
            l2_level_exclusive=l2_level_exclusive,
            **kwargs,
        )

    def _find_top_level_definitions(
        self, root_node, include_l2_in_file_skeleton: bool = False
    ) -> List[Tuple]:
        definitions: List[Tuple] = []
        for child in root_node.children:
            if child.type == "function_declaration":
                name = self._extract_function_name(child)
                if name:
                    definitions.append((child, name, "function"))
            elif child.type in {"variable_declaration", "assignment_statement"}:
                definitions.extend(self._find_assigned_function_definitions(child))
        definitions.sort(key=lambda entry: entry[0].start_point[0])
        return definitions

    def _extract_signature_text(self, node, def_type: str, code_content: str) -> str:
        return self._extract_signature_text_default(node, ("block",), code_content)

    def _build_file_skeleton(
        self,
        definitions: List[Tuple],
        code_content: str,
        include_l2: Optional[bool] = None,
    ) -> str:
        entries: List[str] = []
        for node, _, def_type in definitions:
            signature = self._extract_signature_text(node, def_type, code_content)
            if signature:
                entries.append(signature)
        return "\n".join(entries)

    def _extract_function_name(self, node) -> Optional[str]:
        for child in node.children:
            if child.type in {
                "identifier",
                "dot_index_expression",
                "method_index_expression",
            }:
                return child.text.decode("utf-8").replace(":", ".")
        return None

    def _extract_class_name(self, node) -> Optional[str]:
        return None

    def _find_assigned_function_definitions(self, node) -> List[Tuple]:
        definitions: List[Tuple] = []
        name = self._extract_assignment_target(node)
        if not name:
            return definitions
        function_node = self._find_first_child(node, ("function_definition",))
        if function_node is not None:
            definitions.append((function_node, name, "function"))
        return definitions

    def _extract_assignment_target(self, node) -> Optional[str]:
        target = self._find_first_child(
            node,
            ("dot_index_expression", "method_index_expression", "identifier"),
        )
        if target is None:
            return None
        return target.text.decode("utf-8").replace(":", ".")
