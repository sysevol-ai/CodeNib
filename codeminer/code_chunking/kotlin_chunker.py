#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Kotlin-specific code chunker implementation.
"""

from typing import List, Optional, Tuple

from .base import BaseCodeChunker


class KotlinCodeChunker(BaseCodeChunker):
    """
    Code chunker for Kotlin source files.

    L1 entities: classes, interfaces, enums, objects, top-level functions,
    and top-level properties.
    L2 entities: functions, secondary constructors, and properties inside
    type/object declarations.
    Skeleton mode: file skeleton lists container declarations and member
    signatures.
    """

    _BODY_NODES = {"class_body", "enum_class_body"}
    _CONTAINER_NODES = {"class_declaration", "object_declaration"}
    _MEMBER_NODES = {
        "function_declaration": "method",
        "property_declaration": "property",
        "secondary_constructor": "method",
    }

    def __init__(
        self,
        max_lines_per_chunk: Optional[int] = None,
        chunk_depth: int = 2,
        l2_level_exclusive: bool = True,
        **kwargs,
    ):
        super().__init__(
            "kotlin",
            max_lines_per_chunk=max_lines_per_chunk,
            chunk_depth=chunk_depth,
            l2_level_exclusive=l2_level_exclusive,
            **kwargs,
        )

    def _find_top_level_definitions(
        self, root_node, include_l2_in_file_skeleton: bool = False
    ) -> List[Tuple]:
        definitions: List[Tuple] = []

        include_type_level = self.chunk_depth < 2 or not self.l2_level_exclusive
        include_members = self.chunk_depth >= 2 or include_l2_in_file_skeleton

        for child in root_node.children:
            if child.type in self._CONTAINER_NODES:
                name = self._extract_type_name(child)
                if not name:
                    continue
                def_type = self._definition_type_for_container(child)
                if include_type_level:
                    definitions.append((child, name, def_type))
                if include_members:
                    definitions.extend(self._find_member_definitions(child, (name,)))
                continue

            if child.type == "function_declaration":
                name = self._extract_method_name(child)
                if name:
                    definitions.append((child, name, "function"))
                continue

            if child.type == "property_declaration":
                name = self._extract_property_name(child)
                if name:
                    definitions.append((child, name, "property"))

        definitions.sort(key=lambda entry: entry[0].start_point[0])
        return definitions

    def _extract_signature_text(self, node, def_type: str, code_content: str) -> str:
        stop_types = {
            "class": ("class_body",),
            "interface": ("class_body",),
            "enum": ("enum_class_body",),
            "object": ("class_body",),
            "function": ("function_body",),
            "method": ("function_body",),
            "property": ("=", "property_delegate", "function_body"),
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
        container_types = {"class", "interface", "enum", "object"}
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
        return self._extract_method_name(node)

    def _extract_class_name(self, node) -> Optional[str]:
        return self._extract_type_name(node)

    def _definition_type_for_container(self, node) -> str:
        if node.type == "object_declaration":
            return "object"
        for child in node.children:
            if child.type == "interface":
                return "interface"
            if child.type == "enum":
                return "enum"
        return "class"

    def _extract_type_name(self, node) -> Optional[str]:
        for child in node.children:
            if child.type == "type_identifier":
                return child.text.decode("utf-8")
        return None

    def _extract_method_name(self, node) -> Optional[str]:
        if node.type == "secondary_constructor":
            return "constructor"
        for child in node.children:
            if child.type == "simple_identifier":
                return child.text.decode("utf-8")
        return None

    def _extract_property_name(self, node) -> Optional[str]:
        for child in node.children:
            if child.type == "variable_declaration":
                return self._extract_simple_identifier(child)
        return self._extract_simple_identifier(node)

    def _extract_simple_identifier(self, node) -> Optional[str]:
        for child in node.children:
            if child.type == "simple_identifier":
                return child.text.decode("utf-8")
        return None

    def _find_member_definitions(self, container_node, parents: Tuple[str, ...]):
        definitions = self._find_primary_constructor_properties(container_node, parents)
        for body in container_node.children:
            if body.type not in self._BODY_NODES:
                continue
            definitions.extend(self._find_body_member_definitions(body, parents))
        return definitions

    def _find_primary_constructor_properties(
        self, container_node, parents: Tuple[str, ...]
    ) -> List[Tuple]:
        definitions: List[Tuple] = []
        for child in container_node.children:
            if child.type != "primary_constructor":
                continue
            for parameter in child.children:
                if parameter.type != "class_parameter":
                    continue
                if not self._has_child_type(parameter, "binding_pattern_kind"):
                    continue
                property_name = self._extract_simple_identifier(parameter)
                if property_name:
                    definitions.append(
                        (parameter, ".".join((*parents, property_name)), "property")
                    )
        return definitions

    def _find_body_member_definitions(self, body_node, parents: Tuple[str, ...]):
        definitions: List[Tuple] = []
        for member in body_node.children:
            if member.type in self._MEMBER_NODES:
                member_name = self._extract_member_name(member, parents)
                if member_name:
                    definitions.append(
                        (
                            member,
                            ".".join((*parents, member_name)),
                            self._MEMBER_NODES[member.type],
                        )
                    )
                continue

            if member.type in self._CONTAINER_NODES:
                type_name = self._extract_type_name(member)
                if type_name:
                    definitions.extend(
                        self._find_member_definitions(member, (*parents, type_name))
                    )
                continue

            if member.type == "companion_object":
                definitions.extend(
                    self._find_member_definitions(member, (*parents, "Companion"))
                )
                continue

            if member.type in self._BODY_NODES:
                definitions.extend(self._find_body_member_definitions(member, parents))
        return definitions

    def _extract_member_name(self, node, parents: Tuple[str, ...]) -> Optional[str]:
        if node.type == "property_declaration":
            return self._extract_property_name(node)
        if node.type == "secondary_constructor":
            return parents[-1]
        return self._extract_method_name(node)

    def _has_child_type(self, node, child_type: str) -> bool:
        return any(child.type == child_type for child in node.children)

    def _get_child_definitions(self, node, def_type: str):
        if def_type not in {"class", "interface", "enum", "object"}:
            return []
        name = self._extract_type_name(node)
        if not name:
            return []
        return self._find_member_definitions(node, (name,))
