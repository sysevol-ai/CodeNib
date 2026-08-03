#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Ruby-specific code chunker implementation.
"""

from typing import List, Optional, Tuple

from .base import BaseCodeChunker


class RubyCodeChunker(BaseCodeChunker):
    """
    Code chunker for Ruby source files.

    L1 entities: top-level modules, classes, and methods.
    L2 entities: instance and singleton methods inside modules/classes.
    Skeleton mode: file skeleton lists namespaces and member signatures.
    """

    _NAMESPACE_NODES = {
        "class": "class",
        "module": "module",
    }
    _BODY_NODES = {"body_statement"}
    _METHOD_NODES = {"method", "singleton_method"}

    def __init__(
        self,
        max_lines_per_chunk: Optional[int] = None,
        chunk_depth: int = 2,
        l2_level_exclusive: bool = True,
        **kwargs,
    ):
        super().__init__(
            "ruby",
            max_lines_per_chunk=max_lines_per_chunk,
            chunk_depth=chunk_depth,
            l2_level_exclusive=l2_level_exclusive,
            **kwargs,
        )

    def _find_top_level_definitions(
        self, root_node, include_l2_in_file_skeleton: bool = False
    ) -> List[Tuple]:
        definitions: List[Tuple] = []

        include_namespace_level = self.chunk_depth < 2 or not self.l2_level_exclusive
        include_methods = self.chunk_depth >= 2 or include_l2_in_file_skeleton

        for child in root_node.children:
            if child.type in self._NAMESPACE_NODES:
                name = self._extract_namespace_name(child)
                if not name:
                    continue
                if include_namespace_level:
                    definitions.append((child, name, self._NAMESPACE_NODES[child.type]))
                if include_methods:
                    definitions.extend(self._find_member_definitions(child, (name,)))
                continue

            if child.type in self._METHOD_NODES:
                name = self._method_name_with_context(child, ())
                if name:
                    definitions.append((child, name, "function"))

        definitions.sort(key=lambda entry: entry[0].start_point[0])
        return definitions

    def _extract_signature_text(self, node, def_type: str, code_content: str) -> str:
        stop_types = {
            "class": ("body_statement",),
            "module": ("body_statement",),
            "function": ("body_statement",),
            "method": ("body_statement",),
        }.get(def_type, ("body_statement",))
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
            name for _, name, def_type in definitions if def_type in {"class", "module"}
        }
        entries: List[str] = []
        for node, name, def_type in definitions:
            if def_type == "method":
                if not include_l2:
                    continue
                root_parent = name.split(".", 1)[0] if "." in name else None
                if root_parent and root_parent in container_names:
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
        return self._method_name_with_context(node, ())

    def _extract_class_name(self, node) -> Optional[str]:
        return self._extract_namespace_name(node)

    def _extract_namespace_name(self, node) -> Optional[str]:
        for child in node.children:
            if child.type in ("constant", "scope_resolution"):
                return child.text.decode("utf-8").replace("::", ".")
        return None

    def _extract_method_name(self, node) -> Optional[str]:
        for child in reversed(node.children):
            if child.type == "identifier":
                return child.text.decode("utf-8")
        return None

    def _method_name_with_context(
        self, method_node, parents: Tuple[str, ...]
    ) -> Optional[str]:
        method_name = self._extract_method_name(method_node)
        if not method_name:
            return None
        if parents:
            return ".".join((*parents, method_name))

        receiver = self._extract_singleton_receiver(method_node)
        if receiver and receiver != "self":
            return f"{receiver}.{method_name}"
        return method_name

    def _extract_singleton_receiver(self, node) -> Optional[str]:
        if node.type != "singleton_method":
            return None
        for child in node.children:
            if child.type in ("constant", "scope_resolution", "identifier", "self"):
                return child.text.decode("utf-8").replace("::", ".")
            if child.type == ".":
                return None
        return None

    def _find_member_definitions(self, namespace_node, parents: Tuple[str, ...]):
        definitions: List[Tuple] = []
        for body in namespace_node.children:
            if body.type not in self._BODY_NODES:
                continue
            definitions.extend(self._find_body_member_definitions(body, parents))
        return definitions

    def _find_body_member_definitions(self, body_node, parents: Tuple[str, ...]):
        definitions: List[Tuple] = []
        for member in body_node.children:
            if member.type in self._METHOD_NODES:
                method_name = self._method_name_with_context(member, parents)
                if method_name:
                    definitions.append((member, method_name, "method"))
                continue

            if member.type in self._NAMESPACE_NODES:
                namespace_name = self._extract_namespace_name(member)
                if namespace_name:
                    definitions.extend(
                        self._find_member_definitions(
                            member,
                            (*parents, namespace_name),
                        )
                    )
                continue

            if member.type in self._BODY_NODES:
                definitions.extend(self._find_body_member_definitions(member, parents))
        return definitions

    def _get_child_definitions(self, node, def_type: str):
        if def_type not in {"class", "module"}:
            return []
        name = self._extract_namespace_name(node)
        if not name:
            return []
        return self._find_member_definitions(node, (name,))
