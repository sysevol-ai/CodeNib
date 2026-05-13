#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Python-specific code chunker implementation.
"""

from typing import List, Optional, Tuple

from .base import BaseCodeChunker


class PythonCodeChunker(BaseCodeChunker):
    """
    Code chunker specifically for Python files.

    L1 entities: module-level functions and classes.
    L2 entities: methods inside classes (including async and decorated definitions).
    Skeleton mode: module skeleton lists L1 definitions and class member signatures;
    class skeleton lists method signatures.
    """

    def __init__(
        self,
        max_lines_per_chunk: Optional[int] = None,
        chunk_depth: int = 2,
        l2_level_exclusive: bool = True,
        **kwargs,
    ):
        """Initialize the Python code chunker."""
        super().__init__(
            "python",
            max_lines_per_chunk=max_lines_per_chunk,
            chunk_depth=chunk_depth,
            l2_level_exclusive=l2_level_exclusive,
            **kwargs,
        )

    def _find_top_level_definitions(
        self, root_node, include_l2_in_file_skeleton: bool = False
    ) -> List[Tuple]:
        """
        Find all top-level function and class definitions in Python.
        Handles decorated_definition, function_definition, and class_definition nodes.

        Args:
            root_node: Root node of the AST
            include_l2_in_file_skeleton: Include methods when building file skeletons.

        Returns:
            List of tuples (node, name, type) for each top-level definition
        """
        definitions = []

        # For Python, look for function_definition, class_definition, and
        # decorated_definition at module level
        include_type_level = self.chunk_depth < 2 or not self.l2_level_exclusive
        include_methods = self.chunk_depth >= 2 or include_l2_in_file_skeleton

        for child in root_node.children:
            if child.type == "decorated_definition":
                # Extract the actual definition (function or class) from decorated_definition
                actual_def = self._extract_definition_from_decorated(child)
                if actual_def:
                    def_type = actual_def.type
                    if def_type in ("function_definition", "async_function_definition"):
                        name = self._extract_function_name(actual_def)
                        if name:
                            # Use the decorated_definition node (includes decorators)
                            definitions.append((child, name, "function"))
                    elif def_type == "class_definition":
                        name = self._extract_class_name(actual_def)
                        if name:
                            # Use the decorated_definition node (includes decorators)
                            if include_type_level:
                                definitions.append((child, name, "class"))
                            # Extract methods only if chunk_depth >= 2
                            if include_methods:
                                methods = self._find_class_methods(actual_def)
                                definitions.extend(methods)
            elif child.type in ("function_definition", "async_function_definition"):
                name = self._extract_function_name(child)
                if name:
                    definitions.append((child, name, "function"))
            elif child.type == "class_definition":
                name = self._extract_class_name(child)
                if name:
                    if include_type_level:
                        definitions.append((child, name, "class"))
                    # Extract methods only if chunk_depth >= 2
                    if include_methods:
                        methods = self._find_class_methods(child)
                        definitions.extend(methods)

        # Sort by start line
        definitions.sort(key=lambda x: x[0].start_point[0])
        return definitions

    def _extract_signature_text(self, node, def_type: str, code_content: str) -> str:
        """Extract a signature for Python constructs by trimming the block body."""
        stop_types = {
            "function": ("block",),
            "method": ("block",),
            "class": ("block",),
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

    def _extract_definition_from_decorated(self, decorated_node) -> Optional[object]:
        """
        Extract the actual definition node from a decorated_definition node.

        Args:
            decorated_node: AST node representing a decorated_definition

        Returns:
            The function_definition, async_function_definition, or class_definition
            node, or None
        """
        for child in decorated_node.children:
            if child.type in (
                "function_definition",
                "async_function_definition",
                "class_definition",
            ):
                return child
        return None

    def _extract_function_name(self, node) -> Optional[str]:
        """
        Extract function name from Python function_definition or async_function_definition node.

        Args:
            node: AST node representing a Python function definition

        Returns:
            Function name or None if extraction failed
        """
        for child in node.children:
            if child.type == "identifier":
                return child.text.decode("utf-8")
        return None

    def _extract_class_name(self, node) -> Optional[str]:
        """
        Extract class name from Python class_definition node.

        Args:
            node: AST node representing a Python class definition

        Returns:
            Class name or None if extraction failed
        """
        for child in node.children:
            if child.type == "identifier":
                return child.text.decode("utf-8")
        return None

    def _find_class_methods(self, class_node) -> List[Tuple]:
        """
        Find all method definitions within a Python class.
        Handles function_definition, async_function_definition, and decorated_definition nodes.

        Args:
            class_node: AST node representing a Python class definition

        Returns:
            List of tuples (node, name, type) for each method definition
        """
        methods = []

        class_name = self._extract_class_name(class_node)
        if not class_name:
            return methods

        # Look for the class body
        for child in class_node.children:
            if child.type == "block":
                # Within the class body, look for function definitions (methods)
                for stmt in child.children:
                    if stmt.type == "decorated_definition":
                        # Extract the actual method definition from decorated_definition
                        actual_def = self._extract_definition_from_decorated(stmt)
                        if actual_def and actual_def.type in (
                            "function_definition",
                            "async_function_definition",
                        ):
                            method_name = self._extract_method_name(actual_def)
                            if method_name:
                                # Include class name prefix for node_id
                                full_method_name = f"{class_name}.{method_name}"
                                # Use the decorated_definition node (includes decorators)
                                methods.append((stmt, full_method_name, "method"))
                    elif stmt.type in (
                        "function_definition",
                        "async_function_definition",
                    ):
                        method_name = self._extract_method_name(stmt)
                        if method_name:
                            # Include class name prefix for node_id
                            full_method_name = f"{class_name}.{method_name}"
                            methods.append((stmt, full_method_name, "method"))

        return methods

    def _extract_method_name(self, node) -> Optional[str]:
        """
        Extract method name from Python function_definition node within a class.

        Args:
            node: AST node representing a Python method definition

        Returns:
            Method name or None if extraction failed
        """
        # Method name extraction is the same as function name extraction
        return self._extract_function_name(node)

    def _get_child_definitions(self, node, def_type: str):
        """Return class methods for class skeletons."""
        if def_type != "class":
            return []

        target_node = node
        if node.type == "decorated_definition":
            target_node = self._extract_definition_from_decorated(node)
        if not target_node:
            return []
        return self._find_class_methods(target_node)
