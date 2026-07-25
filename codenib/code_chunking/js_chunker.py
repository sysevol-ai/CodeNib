#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
JavaScript/TypeScript code chunker implementation.
"""

from typing import List, Optional, Tuple

from .base import BaseCodeChunker


class JsTsCodeChunker(BaseCodeChunker):
    """
    Chunker for JavaScript and TypeScript source files.

    L1 entities: top-level functions and classes.
    L2 entities: methods within classes.
    Skeleton mode: file skeleton lists L1 declarations and member signatures;
    class skeleton lists method signatures.
    """

    def __init__(
        self,
        language: str = "javascript",
        max_lines_per_chunk: Optional[int] = None,
        chunk_depth: int = 2,
        l2_level_exclusive: bool = True,
        **kwargs,
    ):
        # Always use the TypeScript parser: it is a strict superset of
        # JavaScript and also handles Flow-annotated (.js) files that the
        # JavaScript parser would reject.
        super().__init__(
            "typescript",
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
            target = self._unwrap_export(child)
            if target is None:
                continue

            if target.type == "function_declaration":
                name = self._extract_function_name(target)
                if name:
                    definitions.append((target, name, "function"))
            elif target.type == "class_declaration":
                name = self._extract_class_name(target)
                if name:
                    if include_type_level:
                        definitions.append((target, name, "class"))
                    if include_methods:
                        methods = self._find_class_methods(target)
                        definitions.extend(methods)
            elif target.type in ("lexical_declaration", "variable_declaration"):
                # Support common JS/TS pattern:
                #   const foo = () => {}
                #   export const bar = function() {}
                definitions.extend(self._find_variable_function_defs(target))
            elif target.type == "expression_statement":
                # Support assignment forms such as:
                #   exports.foo = () => {}
                #   module.exports.bar = function() {}
                assigned = self._find_assignment_function_def(target)
                if assigned is not None:
                    definitions.append(assigned)

        definitions.sort(key=lambda entry: entry[0].start_point[0])
        return definitions

    def _unwrap_export(self, node):
        """Peel off export nodes to get the underlying declaration."""
        if node.type in ("export_statement", "export_clause"):
            for child in node.children:
                if child.type in (
                    "function_declaration",
                    "class_declaration",
                    "lexical_declaration",
                    "variable_declaration",
                    "expression_statement",
                ):
                    return child
        return node

    def _extract_signature_text(self, node, def_type: str, code_content: str) -> str:
        stop_types = {
            "function": ("statement_block",),
            "method": ("statement_block",),
            "class": ("class_body",),
            "variable": (),
            "object": (),
        }.get(def_type, ("statement_block",))
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
        for child in node.children:
            if child.type in ("identifier", "type_identifier"):
                return child.text.decode("utf-8")
        return None

    def _find_class_methods(self, class_node) -> List[Tuple]:
        methods: List[Tuple] = []
        class_name = self._extract_class_name(class_node)

        for child in class_node.children:
            if child.type != "class_body":
                continue
            for member in child.children:
                if member.type == "method_definition":
                    method_name = self._extract_method_name(member)
                    if method_name:
                        full_name = (
                            f"{class_name}.{method_name}" if class_name else method_name
                        )
                        methods.append((member, full_name, "method"))
        return methods

    def _extract_method_name(self, node) -> Optional[str]:
        for child in node.children:
            if child.type in (
                "property_identifier",
                "identifier",
                "private_property_identifier",
            ):
                return child.text.decode("utf-8")
        return None

    def _find_variable_function_defs(self, decl_node) -> List[Tuple]:
        defs: List[Tuple] = []
        for child in decl_node.children:
            if child.type != "variable_declarator":
                continue
            name = None
            func_node = None
            obj_node = None
            has_initializer = False
            for part in child.children:
                if part.type in ("identifier", "property_identifier"):
                    name = part.text.decode("utf-8")
                elif part.type in ("arrow_function", "function", "function_expression"):
                    func_node = part
                elif part.type == "object":
                    obj_node = part
                elif part.type not in ("=", "type_annotation"):
                    has_initializer = True
            if not name:
                continue
            if func_node is not None:
                defs.append((func_node, name, "function"))
            elif obj_node is not None:
                # Use the whole declaration so the chunk covers
                # `const name = { ... }` rather than just `{ ... }`.
                defs.append((decl_node, name, "object"))
            elif has_initializer:
                defs.append((decl_node, name, "variable"))
        return defs

    def _find_assignment_function_def(self, expr_stmt_node) -> Optional[Tuple]:
        for child in expr_stmt_node.children:
            if child.type != "assignment_expression":
                continue
            lhs_name: Optional[str] = None
            rhs_func = None
            for part in child.children:
                if part.type in (
                    "member_expression",
                    "identifier",
                    "property_identifier",
                ):
                    if lhs_name is None:
                        lhs_name = self._extract_lhs_name(part)
                elif part.type in ("arrow_function", "function", "function_expression"):
                    rhs_func = part
            if lhs_name and rhs_func is not None:
                return (rhs_func, lhs_name, "function")
        return None

    def _extract_lhs_name(self, node) -> Optional[str]:
        # Prefer terminal property/identifier token for stable naming.
        candidates: List[str] = []
        stack = [node]
        while stack:
            cur = stack.pop()
            if cur.type in ("identifier", "property_identifier"):
                candidates.append(cur.text.decode("utf-8"))
            stack.extend(reversed(cur.children))
        if not candidates:
            return None
        return candidates[-1]

    def _get_child_definitions(self, node, def_type: str):
        """Return method definitions for class skeletons."""
        if def_type != "class":
            return []
        return self._find_class_methods(node)
