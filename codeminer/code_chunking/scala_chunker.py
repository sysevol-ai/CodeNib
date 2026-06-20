#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Scala-specific code chunker implementation."""

from __future__ import annotations

from typing import List, Optional, Tuple

from .base import BaseCodeChunker


class ScalaCodeChunker(BaseCodeChunker):
    """Code chunker for Scala source files."""

    _CONTAINER_NODES = {
        "class_definition": "class",
        "object_definition": "object",
        "trait_definition": "trait",
    }
    _BODY_NODES = {"template_body"}

    def __init__(
        self,
        max_lines_per_chunk: Optional[int] = None,
        chunk_depth: int = 2,
        l2_level_exclusive: bool = True,
        **kwargs,
    ):
        super().__init__(
            "scala",
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
                name = self._extract_identifier(child)
                if not name:
                    continue
                if include_containers:
                    definitions.append((child, name, self._CONTAINER_NODES[child.type]))
                if include_members:
                    definitions.extend(self._find_member_definitions(child, (name,)))
                continue

            if child.type == "function_definition":
                name = self._extract_identifier(child)
                if name:
                    definitions.append((child, name, "function"))

        definitions.sort(key=lambda entry: entry[0].start_point[0])
        return definitions

    def _extract_signature_text(self, node, def_type: str, code_content: str) -> str:
        stop_types = {
            "class": ("template_body",),
            "object": ("template_body",),
            "trait": ("template_body",),
            "function": ("=", "template_body"),
            "method": ("=", "template_body"),
            "property": ("=", "template_body"),
        }.get(def_type, ("=", "template_body"))
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
        container_types = {"class", "object", "trait"}
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
        return self._extract_identifier(node)

    def _extract_class_name(self, node) -> Optional[str]:
        return self._extract_identifier(node)

    def _extract_identifier(self, node) -> Optional[str]:
        for child in node.children:
            if child.type == "identifier":
                return child.text.decode("utf-8")
        return None

    def _find_member_definitions(self, container_node, parents: Tuple[str, ...]):
        definitions: List[Tuple] = []
        definitions.extend(self._find_constructor_properties(container_node, parents))
        for body in container_node.children:
            if body.type not in self._BODY_NODES:
                continue
            for member in body.children:
                if member.type in {"function_definition", "function_declaration"}:
                    name = self._extract_identifier(member)
                    if name:
                        definitions.append(
                            (member, ".".join((*parents, name)), "method")
                        )
                    continue
                if member.type in {"val_definition", "var_definition"}:
                    name = self._extract_identifier(member)
                    if name:
                        definitions.append(
                            (member, ".".join((*parents, name)), "property")
                        )
                    continue
                if member.type in self._CONTAINER_NODES:
                    nested = self._extract_identifier(member)
                    if nested:
                        definitions.extend(
                            self._find_member_definitions(member, (*parents, nested))
                        )
        return definitions

    def _find_constructor_properties(
        self, container_node, parents: Tuple[str, ...]
    ) -> List[Tuple]:
        definitions: List[Tuple] = []
        for child in container_node.children:
            if child.type != "class_parameters":
                continue
            for parameter in child.children:
                if parameter.type != "class_parameter":
                    continue
                if not any(part.type in {"val", "var"} for part in parameter.children):
                    continue
                name = self._extract_identifier(parameter)
                if name:
                    definitions.append(
                        (parameter, ".".join((*parents, name)), "property")
                    )
        return definitions

    def _get_child_definitions(self, node, def_type: str):
        if def_type not in {"class", "object", "trait"}:
            return []
        name = self._extract_identifier(node)
        if not name:
            return []
        return self._find_member_definitions(node, (name,))
