#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
C++ specific code chunker implementation.
"""

from typing import List, Optional, Tuple

from .base import BaseCodeChunker


class CppCodeChunker(BaseCodeChunker):
    """
    Code chunker specifically for C++ files.

    L1 entities: free functions and class/struct definitions.
    L2 entities: methods declared/defined inside class bodies.
    Skeleton mode: file skeleton lists L1 declarations and member signatures;
    class skeleton lists method signatures.
    """

    def __init__(
        self,
        max_lines_per_chunk: Optional[int] = None,
        chunk_depth: int = 2,
        l2_level_exclusive: bool = True,
        **kwargs,
    ):
        """Initialize the C++ code chunker."""
        super().__init__(
            "cpp",
            max_lines_per_chunk=max_lines_per_chunk,
            chunk_depth=chunk_depth,
            l2_level_exclusive=l2_level_exclusive,
            **kwargs,
        )

    def _find_top_level_definitions(
        self, root_node, include_l2_in_file_skeleton: bool = False
    ) -> List[Tuple]:
        """
        Find all top-level function and class definitions in C++.

        Args:
            root_node: Root node of the AST
            include_l2_in_file_skeleton: Include methods when building file skeletons.

        Returns:
            List of tuples (node, name, type) for each top-level definition
        """
        definitions = []

        # For C++, look for function_definition and class_specifier
        include_type_level = self.chunk_depth < 2 or not self.l2_level_exclusive
        include_methods = self.chunk_depth >= 2 or include_l2_in_file_skeleton

        for node in self._find_nodes_by_type(root_node, "function_definition"):
            name = self._extract_function_name(node)
            if name:
                definitions.append((node, name, "function"))

        for node in self._find_nodes_by_type(root_node, "class_specifier"):
            name = self._extract_class_name(node)
            if name:
                if include_type_level:
                    definitions.append((node, name, "class"))
                # Extract methods only if chunk_depth >= 2
                if include_methods:
                    methods = self._find_class_methods(node)
                    definitions.extend(methods)

        # Top-level variable/const declarations and macros (direct children only
        # to avoid picking up field declarations inside class bodies).
        for child in root_node.children:
            if child.type == "declaration":
                if self._is_function_prototype(child):
                    continue
                name = self._extract_declaration_name(child)
                if name:
                    definitions.append((child, name, "declaration"))
            elif child.type in ("preproc_def", "preproc_function_def"):
                name = self._extract_macro_name(child)
                if name:
                    definitions.append((child, name, "macro"))

        # Sort by start line
        definitions.sort(key=lambda x: x[0].start_point[0])
        return definitions

    def _extract_signature_text(self, node, def_type: str, code_content: str) -> str:
        """
        Extract a signature for C++ constructs by trimming at compound statements or
        declarations.
        """
        stop_types = {
            "function": ("compound_statement",),
            "method": ("compound_statement",),
            "class": ("field_declaration_list",),
            "declaration": (),
            "macro": (),
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
            name for _, name, def_type in definitions if def_type == "class"
        }
        entries: List[str] = []
        for node, name, def_type in definitions:
            if def_type == "method":
                if not include_l2:
                    continue
                parent = name.split(".", 1)[0] if "." in name else None
                if parent and parent in container_names:
                    # Already represented inside the class skeleton.
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
        """
        Extract function name from C++ function_definition node.

        Args:
            node: AST node representing a C++ function definition

        Returns:
            Function name or None if extraction failed
        """
        for child in node.children:
            if child.type == "function_declarator":
                for grandchild in child.children:
                    if grandchild.type in ("identifier", "field_identifier"):
                        return grandchild.text.decode("utf-8")
        return None

    def _extract_class_name(self, node) -> Optional[str]:
        """
        Extract class name from C++ class_specifier node.

        Args:
            node: AST node representing a C++ class definition

        Returns:
            Class name or None if extraction failed
        """
        for child in node.children:
            if child.type == "type_identifier":
                return child.text.decode("utf-8")
        return None

    def _find_class_methods(self, class_node) -> List[Tuple]:
        """
        Find all method definitions within a C++ class.

        Args:
            class_node: AST node representing a C++ class definition

        Returns:
            List of tuples (node, name, type) for each method definition
        """
        methods = []
        class_name = self._extract_class_name(class_node)

        # Look for the class body (field_declaration_list)
        for child in class_node.children:
            if child.type == "field_declaration_list":
                # Within the class body, look for function definitions (methods)
                for member in child.children:
                    if member.type == "function_definition":
                        method_name = self._extract_method_name(member)
                        if method_name:
                            full_name = (
                                f"{class_name}.{method_name}"
                                if class_name
                                else method_name
                            )
                            methods.append((member, full_name, "method"))
                    # Also look for function declarations that might be methods
                    elif member.type == "declaration":
                        # Check if this declaration contains a function declarator
                        for decl_child in member.children:
                            if decl_child.type == "function_declarator":
                                method_name = self._extract_method_name_from_declarator(
                                    decl_child
                                )
                                if method_name:
                                    full_name = (
                                        f"{class_name}.{method_name}"
                                        if class_name
                                        else method_name
                                    )
                                    methods.append((member, full_name, "method"))

        return methods

    def _extract_method_name(self, node) -> Optional[str]:
        """
        Extract method name from C++ function_definition node within a class.

        Args:
            node: AST node representing a C++ method definition

        Returns:
            Method name or None if extraction failed
        """
        # Method name extraction is the same as function name extraction
        return self._extract_function_name(node)

    def _extract_method_name_from_declarator(self, declarator_node) -> Optional[str]:
        """
        Extract method name from C++ function_declarator node.

        Args:
            declarator_node: AST node representing a C++ function declarator

        Returns:
            Method name or None if extraction failed
        """
        for child in declarator_node.children:
            if child.type in ("identifier", "field_identifier"):
                return child.text.decode("utf-8")
        return None

    def _is_function_prototype(self, decl_node) -> bool:
        """Check if a declaration node is a function prototype."""
        for child in decl_node.children:
            if child.type == "function_declarator":
                return True
        return False

    def _extract_declaration_name(self, node) -> Optional[str]:
        """Extract the declared identifier from a top-level declaration."""
        for child in node.children:
            if child.type == "identifier":
                return child.text.decode("utf-8")
            if child.type == "init_declarator":
                for grandchild in child.children:
                    if grandchild.type == "identifier":
                        return grandchild.text.decode("utf-8")
        return None

    def _extract_macro_name(self, node) -> Optional[str]:
        """Extract the macro name from preproc_def or preproc_function_def."""
        for child in node.children:
            if child.type == "identifier":
                return child.text.decode("utf-8")
        return None

    def _get_child_definitions(self, node, def_type: str):
        """Return method definitions for class skeletons."""
        if def_type != "class":
            return []
        return self._find_class_methods(node)
