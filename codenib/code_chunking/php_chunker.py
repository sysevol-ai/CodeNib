#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
PHP-specific code chunker implementation.
"""

from typing import List, Optional, Tuple

from .base import BaseCodeChunker


class PhpCodeChunker(BaseCodeChunker):
    """
    Code chunker for PHP source files.

    L1 entities: classes, interfaces, traits, enums, and top-level functions.
    L2 entities: methods and properties inside type declarations.
    Skeleton mode: file skeleton lists type declarations and member signatures.
    """

    _TYPE_NODES = {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "trait_declaration": "trait",
        "enum_declaration": "enum",
    }
    _BODY_NODES = {"declaration_list", "enum_declaration_list"}
    _MEMBER_NODES = {
        "method_declaration": "method",
        "property_declaration": "property",
    }

    def __init__(
        self,
        max_lines_per_chunk: Optional[int] = None,
        chunk_depth: int = 2,
        l2_level_exclusive: bool = True,
        **kwargs,
    ):
        super().__init__(
            "php",
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

        for child in self._iter_top_level_declarations(root_node):
            if child.type in self._TYPE_NODES:
                name = self._extract_type_name(child)
                if not name:
                    continue
                if include_type_level:
                    definitions.append((child, name, self._TYPE_NODES[child.type]))
                if include_members:
                    definitions.extend(self._find_member_definitions(child, (name,)))
                continue

            if child.type == "function_definition":
                name = self._extract_function_name(child)
                if name:
                    definitions.append((child, name, "function"))

        definitions.sort(key=lambda entry: entry[0].start_point[0])
        return definitions

    def _extract_signature_text(self, node, def_type: str, code_content: str) -> str:
        stop_types = {
            "class": ("declaration_list",),
            "interface": ("declaration_list",),
            "trait": ("declaration_list",),
            "enum": ("enum_declaration_list",),
            "function": ("compound_statement",),
            "method": ("compound_statement",),
            "property": (),
        }.get(def_type, ("compound_statement",))
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
            if def_type in {"class", "interface", "trait", "enum"}
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

    def _iter_top_level_declarations(self, node):
        for child in node.children:
            if child.type in ("php_tag", "ERROR"):
                continue
            if child.type == "namespace_definition":
                yield from self._iter_namespace_declarations(child)
                continue
            yield child

    def _iter_namespace_declarations(self, namespace_node):
        for child in namespace_node.children:
            if child.type == "compound_statement":
                for member in child.children:
                    if member.type not in ("{", "}"):
                        yield member

    def _extract_function_name(self, node) -> Optional[str]:
        return self._extract_name_child(node)

    def _extract_class_name(self, node) -> Optional[str]:
        return self._extract_type_name(node)

    def _extract_type_name(self, node) -> Optional[str]:
        return self._extract_name_child(node)

    def _extract_method_name(self, node) -> Optional[str]:
        if node.type == "property_declaration":
            return self._extract_property_name(node)
        return self._extract_name_child(node)

    def _extract_name_child(self, node) -> Optional[str]:
        for child in node.children:
            if child.type in ("name", "qualified_name"):
                return child.text.decode("utf-8").replace("\\", ".")
        return None

    def _extract_property_name(self, node) -> Optional[str]:
        for prop in self._find_nodes_by_type(node, "property_element"):
            for child in prop.children:
                if child.type == "variable_name":
                    return child.text.decode("utf-8").lstrip("$")
        return None

    def _find_member_definitions(self, type_node, parents: Tuple[str, ...]):
        definitions: List[Tuple] = []
        for body in type_node.children:
            if body.type not in self._BODY_NODES:
                continue
            definitions.extend(self._find_body_member_definitions(body, parents))
        return definitions

    def _find_body_member_definitions(self, body_node, parents: Tuple[str, ...]):
        definitions: List[Tuple] = []
        for member in body_node.children:
            if member.type in self._MEMBER_NODES:
                member_name = self._extract_method_name(member)
                if member_name:
                    definitions.append(
                        (
                            member,
                            ".".join((*parents, member_name)),
                            self._MEMBER_NODES[member.type],
                        )
                    )
                continue

            if member.type in self._BODY_NODES:
                definitions.extend(self._find_body_member_definitions(member, parents))
        return definitions

    def _get_child_definitions(self, node, def_type: str):
        if def_type not in {"class", "interface", "trait", "enum"}:
            return []
        name = self._extract_type_name(node)
        if not name:
            return []
        return self._find_member_definitions(node, (name,))
