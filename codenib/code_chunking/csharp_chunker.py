#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
C#-specific code chunker implementation.
"""

from typing import List, Optional, Tuple

from .base import BaseCodeChunker


class CSharpCodeChunker(BaseCodeChunker):
    """
    Code chunker for C# source files.

    L1 entities: classes, interfaces, enums, records, structs, and top-level
    local functions.
    L2 entities: methods, constructors, and properties inside type declarations.
    Skeleton mode: file skeleton lists type declarations and member signatures.
    """

    _TYPE_NODES = {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
        "record_declaration": "record",
        "struct_declaration": "struct",
    }
    _DECLARATION_LIST_NODES = {"declaration_list"}
    _MEMBER_NODES = {
        "constructor_declaration": "method",
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
            "csharp",
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

            if child.type == "local_function_statement":
                name = self._extract_method_name(child)
                if name:
                    definitions.append((child, name, "function"))

        definitions.sort(key=lambda entry: entry[0].start_point[0])
        return definitions

    def _extract_signature_text(self, node, def_type: str, code_content: str) -> str:
        stop_types = {
            "class": ("declaration_list",),
            "interface": ("declaration_list",),
            "enum": ("enum_member_declaration_list",),
            "record": ("declaration_list",),
            "struct": ("declaration_list",),
            "function": ("block", "arrow_expression_clause"),
            "method": ("block", "arrow_expression_clause"),
            "property": ("accessor_list", "arrow_expression_clause"),
        }.get(def_type, ("block", "arrow_expression_clause"))
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
        container_types = {"class", "interface", "enum", "record", "struct"}
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
            if child.type == "namespace_declaration":
                yield from self._iter_namespace_declarations(child)
            elif child.type == "global_statement":
                for part in child.children:
                    if part.type == "local_function_statement":
                        yield part
            else:
                yield child

    def _iter_namespace_declarations(self, namespace_node):
        for child in namespace_node.children:
            if child.type in self._DECLARATION_LIST_NODES:
                for member in child.children:
                    if member.type == "namespace_declaration":
                        yield from self._iter_namespace_declarations(member)
                    else:
                        yield member

    def _extract_function_name(self, node) -> Optional[str]:
        return self._extract_method_name(node)

    def _extract_class_name(self, node) -> Optional[str]:
        return self._extract_type_name(node)

    def _extract_type_name(self, node) -> Optional[str]:
        for child in node.children:
            if child.type in ("identifier", "qualified_name"):
                return child.text.decode("utf-8")
        return None

    def _extract_method_name(self, node) -> Optional[str]:
        for child in reversed(node.children):
            if child.type == "identifier":
                return child.text.decode("utf-8")
        return None

    def _find_member_definitions(self, type_node, parents: Tuple[str, ...]):
        definitions: List[Tuple] = []
        for body in type_node.children:
            if body.type not in self._DECLARATION_LIST_NODES:
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

            if member.type in self._TYPE_NODES:
                type_name = self._extract_type_name(member)
                if type_name:
                    definitions.extend(
                        self._find_member_definitions(member, (*parents, type_name))
                    )
                continue

            if member.type in self._DECLARATION_LIST_NODES:
                definitions.extend(self._find_body_member_definitions(member, parents))
        return definitions

    def _get_child_definitions(self, node, def_type: str):
        if def_type not in {"class", "interface", "enum", "record", "struct"}:
            return []
        name = self._extract_type_name(node)
        if not name:
            return []
        return self._find_member_definitions(node, (name,))
