#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Go-specific code chunker implementation.
"""

import re
from typing import Dict, List, Optional, Tuple

from .base import BaseCodeChunker


class GoCodeChunker(BaseCodeChunker):
    """
    Code chunker for Go source files.

    L1 entities: top-level functions and type declarations (struct/interface/type).
    L2 entities: methods with receivers.
    Skeleton mode: file skeleton lists L1 declarations and method signatures;
    type skeleton lists receiver-bound methods.
    """

    def __init__(
        self,
        max_lines_per_chunk: Optional[int] = None,
        chunk_depth: int = 2,
        l2_level_exclusive: bool = True,
        **kwargs,
    ):
        super().__init__(
            "go",
            max_lines_per_chunk=max_lines_per_chunk,
            chunk_depth=chunk_depth,
            l2_level_exclusive=l2_level_exclusive,
            **kwargs,
        )
        self._methods_by_type: Dict[str, List[Tuple]] = {}

    def _find_top_level_definitions(
        self, root_node, include_l2_in_file_skeleton: bool = False
    ) -> List[Tuple]:
        definitions: List[Tuple] = []
        self._methods_by_type = {}

        include_type_level = self.chunk_depth < 2 or not self.l2_level_exclusive
        include_methods = self.chunk_depth >= 2 or include_l2_in_file_skeleton

        for child in root_node.children:
            if child.type == "function_declaration":
                name = self._extract_function_name(child)
                if name:
                    definitions.append((child, name, "function"))
                continue

            if child.type == "type_declaration":
                for type_spec in self._find_nodes_by_type(child, "type_spec"):
                    type_name = self._extract_type_name(type_spec)
                    if type_name and include_type_level:
                        type_kind = self._extract_type_kind(type_spec)
                        definitions.append((type_spec, type_name, type_kind))
                continue

            if child.type == "method_declaration":
                method_name = self._extract_method_name(child)
                if not method_name:
                    continue
                receiver_type = self._extract_receiver_type(child)
                full_name = (
                    f"{receiver_type}.{method_name}" if receiver_type else method_name
                )
                if include_methods:
                    definitions.append((child, full_name, "method"))
                if receiver_type:
                    self._methods_by_type.setdefault(receiver_type, []).append(
                        (child, full_name, "method")
                    )
                continue

            if child.type == "var_declaration":
                name = self._extract_var_const_name(child, "var_spec")
                if name:
                    definitions.append((child, name, "var"))
                continue

            if child.type == "const_declaration":
                name = self._extract_var_const_name(child, "const_spec")
                if name:
                    definitions.append((child, name, "const"))
                continue

        definitions.sort(key=lambda entry: entry[0].start_point[0])
        return definitions

    def _extract_signature_text(self, node, def_type: str, code_content: str) -> str:
        stop_types = {
            "function": ("block",),
            "method": ("block",),
            "type": ("field_declaration_list", "method_spec_list"),
            "struct": ("field_declaration_list",),
            "interface": ("method_spec_list",),
            "var": (),
            "const": (),
        }.get(def_type, ("block",))
        return self._extract_signature_text_default(node, stop_types, code_content)

    def _build_file_skeleton(
        self,
        definitions: List[Tuple],
        code_content: str,
        include_l2: Optional[bool] = None,
    ) -> str:
        include_l2 = (
            self.include_l2_in_file_skeleton if include_l2 is None else include_l2
        )
        container_names = {
            name
            for _, name, def_type in definitions
            if def_type in ("type", "struct", "interface")
        }
        entries: List[str] = []
        for node, name, def_type in definitions:
            if def_type == "method":
                if not include_l2:
                    continue
                parent = name.split(".", 1)[0] if "." in name else None
                if parent and parent in container_names:
                    continue
                signature = self._extract_signature_text(node, def_type, code_content)
                if signature:
                    entries.append(self._indent_lines(signature))
                continue

            skeleton = self._build_definition_skeleton(
                node,
                def_type,
                code_content=code_content,
                include_children=include_l2,
            )
            if skeleton:
                entries.append(skeleton)
        return "\n".join(entries)

    def _extract_function_name(self, node) -> Optional[str]:
        for child in node.children:
            if child.type == "identifier":
                return child.text.decode("utf-8")
        return None

    def _extract_class_name(self, node) -> Optional[str]:
        return self._extract_type_name(node)

    def _extract_type_name(self, node) -> Optional[str]:
        for child in node.children:
            if child.type in ("type_identifier", "identifier"):
                return child.text.decode("utf-8")
        return None

    def _extract_method_name(self, node) -> Optional[str]:
        for child in node.children:
            if child.type in ("field_identifier", "identifier"):
                return child.text.decode("utf-8")
        return None

    def _extract_type_kind(self, type_spec_node) -> str:
        for child in type_spec_node.children:
            if child.type == "struct_type":
                return "struct"
            if child.type == "interface_type":
                return "interface"
        return "type"

    def _extract_receiver_type(self, method_node) -> Optional[str]:
        receiver = None
        for child in method_node.children:
            if child.type == "parameter_list":
                receiver = child
                break
        if receiver is None:
            return None

        receiver_text = receiver.text.decode("utf-8")
        candidates = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", receiver_text)
        if not candidates:
            return None
        # Receiver list typically looks like "(r *Type)" -> use trailing type token.
        return candidates[-1]

    def _extract_var_const_name(self, node, spec_type: str) -> Optional[str]:
        """Extract name(s) from a var_declaration or const_declaration node."""
        names = []
        for child in node.children:
            if child.type == spec_type:
                for part in child.children:
                    if part.type == "identifier":
                        names.append(part.text.decode("utf-8"))
                        break
        if not names:
            return None
        if len(names) <= 3:
            return ", ".join(names)
        return ", ".join(names[:3]) + ", ..."

    def _get_child_definitions(self, node, def_type: str):
        if def_type not in ("type", "struct", "interface"):
            return []
        type_name = self._extract_type_name(node)
        if not type_name:
            return []
        return self._methods_by_type.get(type_name, [])
