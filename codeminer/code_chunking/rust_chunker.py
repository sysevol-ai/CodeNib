#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Rust-specific code chunker implementation.
"""

from typing import List, Optional, Tuple

from .base import BaseCodeChunker


class RustCodeChunker(BaseCodeChunker):
    """
    Code chunker for Rust source files.

    L1 entities: free functions plus struct/enum/trait/impl blocks.
    L2 entities: functions inside impl blocks.
    Skeleton mode: file skeleton lists L1 declarations and impl member signatures;
    impl skeleton lists method signatures.
    """

    def __init__(
        self,
        max_lines_per_chunk: Optional[int] = 200,
        chunk_depth: int = 2,
        l2_level_exclusive: bool = True,
        **kwargs,
    ):
        super().__init__(
            "rust",
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
        include_methods = self.chunk_depth >= 2 or include_l2_in_file_skeleton

        for child in root_node.children:
            if child.type == "function_item":
                name = self._extract_function_name(child)
                if name:
                    definitions.append((child, name, "function"))
            elif child.type in ("struct_item", "enum_item", "trait_item"):
                name = self._extract_type_name(child)
                if name and include_type_level:
                    chunk_type = child.type.replace("_item", "")
                    definitions.append((child, name, chunk_type))
            elif child.type == "impl_item":
                impl_label, target_name = self._extract_impl_label(child)
                if include_type_level:
                    definitions.append((child, impl_label, "impl"))
                if include_methods:
                    definitions.extend(self._find_impl_methods(child, target_name))
            elif child.type == "const_item":
                name = self._extract_const_static_name(child)
                if name:
                    definitions.append((child, name, "const"))
            elif child.type == "static_item":
                name = self._extract_const_static_name(child)
                if name:
                    definitions.append((child, name, "static"))
            elif child.type == "type_item":
                name = self._extract_type_name(child)
                if name:
                    definitions.append((child, name, "type"))

        definitions.sort(key=lambda entry: entry[0].start_point[0])
        return definitions

    def _extract_signature_text(self, node, def_type: str, code_content: str) -> str:
        """
        Extract a signature for Rust constructs by trimming before bodies/decl lists.
        """
        stop_types = {
            "function": ("block",),
            "method": ("block",),
            "struct": ("field_declaration_list",),
            "enum": ("enum_variant_list", "field_declaration_list"),
            "trait": ("declaration_list",),
            "impl": ("declaration_list",),
            "const": (),
            "static": (),
            "type": (),
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
        container_types = {"struct", "enum", "trait", "impl"}
        container_names = {
            name for _, name, def_type in definitions if def_type in container_types
        }
        entries: List[str] = []
        for node, name, def_type in definitions:
            if def_type == "method":
                if not include_l2:
                    continue
                parent = name.split(".", 1)[0] if "." in name else None
                if parent and parent in container_names:
                    # Already represented inside the impl/trait skeleton.
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

    def _extract_type_name(self, node) -> Optional[str]:
        for child in node.children:
            if child.type in ("type_identifier", "identifier"):
                return child.text.decode("utf-8")
        return None

    def _extract_impl_label(self, node) -> Tuple[str, Optional[str]]:
        target_name = None
        for child in node.children:
            if child.type in (
                "type_identifier",
                "identifier",
                "scoped_identifier",
                "scoped_type_identifier",
            ):
                target_name = child.text.decode("utf-8")
                break
        label = f"impl {target_name}" if target_name else "impl"
        return label, target_name

    def _find_impl_methods(self, impl_node, target_name: Optional[str]) -> List[Tuple]:
        methods: List[Tuple] = []
        for child in impl_node.children:
            if child.type == "declaration_list":
                for decl in child.children:
                    if decl.type == "function_item":
                        method_name = self._extract_function_name(decl)
                        if method_name:
                            if target_name:
                                full_name = f"{target_name}.{method_name}"
                            else:
                                full_name = method_name
                            methods.append((decl, full_name, "method"))
        return methods

    def _extract_const_static_name(self, node) -> Optional[str]:
        """Extract identifier from const_item or static_item."""
        for child in node.children:
            if child.type == "identifier":
                return child.text.decode("utf-8")
        return None

    def _extract_class_name(self, node) -> Optional[str]:
        # Rust does not have traditional classes; reuse struct/enum name extraction.
        return self._extract_type_name(node)

    def _extract_method_name(self, node) -> Optional[str]:
        return self._extract_function_name(node)

    def _get_child_definitions(self, node, def_type: str):
        """Return impl methods for impl skeletons."""
        if def_type != "impl":
            return []
        _, target_name = self._extract_impl_label(node)
        return self._find_impl_methods(node, target_name)
