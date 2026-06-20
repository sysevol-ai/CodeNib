#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Swift-specific code chunker implementation."""

from __future__ import annotations

from typing import List, Optional, Tuple

from .base import BaseCodeChunker


class SwiftCodeChunker(BaseCodeChunker):
    """Code chunker for Swift source files."""

    _CONTAINER_NODES = {
        "class_declaration": "class",
        "protocol_declaration": "protocol",
        "enum_declaration": "enum",
        "extension_declaration": "extension",
    }
    _BODY_NODES = {"class_body", "protocol_body", "enum_class_body"}

    def __init__(
        self,
        max_lines_per_chunk: Optional[int] = None,
        chunk_depth: int = 2,
        l2_level_exclusive: bool = True,
        **kwargs,
    ):
        super().__init__(
            "swift",
            max_lines_per_chunk=max_lines_per_chunk,
            chunk_depth=chunk_depth,
            l2_level_exclusive=l2_level_exclusive,
            **kwargs,
        )

    def _find_top_level_definitions(
        self, root_node, include_l2_in_file_skeleton: bool = False
    ) -> List[Tuple]:
        definitions: List[Tuple] = []
        include_containers = self.chunk_depth < 2 or not self.l2_level_exclusive
        include_members = self.chunk_depth >= 2 or include_l2_in_file_skeleton

        for child in root_node.children:
            if child.type in self._CONTAINER_NODES:
                name = self._extract_type_name(child)
                if not name:
                    continue
                def_type = self._definition_type(child)
                if include_containers:
                    definitions.append((child, name, def_type))
                if include_members:
                    definitions.extend(self._find_member_definitions(child, (name,)))
                continue

            if child.type == "function_declaration":
                name = self._extract_identifier(child, ("simple_identifier",))
                if name:
                    definitions.append((child, name, "function"))

        definitions.sort(key=lambda entry: entry[0].start_point[0])
        return definitions

    def _extract_signature_text(self, node, def_type: str, code_content: str) -> str:
        stop_types = {
            "class": ("class_body",),
            "struct": ("class_body",),
            "protocol": ("protocol_body", "class_body"),
            "enum": ("enum_class_body", "class_body"),
            "extension": ("class_body",),
            "function": ("function_body",),
            "method": ("function_body",),
            "property": ("function_body",),
        }.get(def_type, ("function_body",))
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
        container_types = {"class", "struct", "protocol", "enum", "extension"}
        container_names = {
            name for _, name, def_type in definitions if def_type in container_types
        }
        entries: List[str] = []
        for node, name, def_type in definitions:
            if def_type in {"method", "property"}:
                if not include_l2:
                    continue
                parent = name.split(".", 1)[0] if "." in name else None
                if parent and parent in container_names:
                    continue
                signature = self._extract_signature_text(node, def_type, code_content)
                if signature:
                    entries.append(
                        self._indent_lines(signature) if parent else signature
                    )
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
        return self._extract_identifier(node, ("simple_identifier",))

    def _extract_class_name(self, node) -> Optional[str]:
        return self._extract_type_name(node)

    def _definition_type(self, node) -> str:
        if node.type == "class_declaration":
            for child in node.children:
                if child.type in {"struct", "actor"}:
                    return child.type
            return "class"
        return self._CONTAINER_NODES[node.type]

    def _extract_type_name(self, node) -> Optional[str]:
        return self._extract_identifier(node, ("type_identifier", "simple_identifier"))

    def _extract_identifier(self, node, types: tuple[str, ...]) -> Optional[str]:
        for child in node.children:
            if child.type in types:
                return child.text.decode("utf-8")
        return None

    def _find_member_definitions(self, container_node, parents: Tuple[str, ...]):
        definitions: List[Tuple] = []
        for body in container_node.children:
            if body.type not in self._BODY_NODES:
                continue
            for member in body.children:
                if member.type in {
                    "function_declaration",
                    "protocol_function_declaration",
                }:
                    name = self._extract_function_name(member)
                    if name:
                        definitions.append(
                            (member, ".".join((*parents, name)), "method")
                        )
                    continue
                if member.type == "property_declaration":
                    name = self._extract_identifier(
                        member, ("pattern", "simple_identifier")
                    )
                    if name:
                        definitions.append(
                            (member, ".".join((*parents, name)), "property")
                        )
                    continue
                if member.type in self._CONTAINER_NODES:
                    nested = self._extract_type_name(member)
                    if nested:
                        definitions.extend(
                            self._find_member_definitions(member, (*parents, nested))
                        )
        return definitions

    def _get_child_definitions(self, node, def_type: str):
        if def_type not in {"class", "struct", "protocol", "enum", "extension"}:
            return []
        name = self._extract_type_name(node)
        if not name:
            return []
        return self._find_member_definitions(node, (name,))
