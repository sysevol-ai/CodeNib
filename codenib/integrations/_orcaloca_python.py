# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Python source metadata used by the pinned OrcaLoca compatibility layer."""

from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PythonSymbol:
    """Source metadata exposed by OrcaLoca's pinned Python AST builder."""

    node_id: str
    parent_id: str
    kind: str
    name: str
    class_name: str | None
    start_line: int
    end_line: int
    signature: str | None
    docstring: str | None
    source: str


@dataclass(frozen=True, slots=True)
class PythonOutline:
    """One parsed Python file in pinned OrcaLoca insertion order."""

    file_path: str
    source: str
    symbols: tuple[PythonSymbol, ...]

    def symbol(self, node_id: str) -> PythonSymbol | None:
        return next(
            (symbol for symbol in self.symbols if symbol.node_id == node_id),
            None,
        )


class _OutlineBuilder(ast.NodeVisitor):
    """Mirror the source metadata contract of OrcaLoca's pinned visitor."""

    def __init__(self, file_path: str, source: str) -> None:
        self.file_path = file_path
        self.source_lines = source.splitlines(keepends=True)
        self.current_class: str | None = None
        self.current_function: str | None = None
        self.symbols: dict[str, PythonSymbol] = {}

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        name = node.name
        parent_id = self.current_class or self.file_path
        node_id = f"{parent_id}::{name}"
        kind = "method" if self.current_class else "function"
        class_name = (
            self.current_class.rsplit("::", 1)[-1] if self.current_class else None
        )
        args = [argument.arg for argument in node.args.args]
        self._record(
            node,
            node_id=node_id,
            parent_id=parent_id,
            kind=kind,
            name=name,
            class_name=class_name,
            signature=f"{name}({', '.join(args)})",
            docstring=ast.get_docstring(node),
        )

        previous_function = self.current_function
        self.current_function = node_id
        self.generic_visit(node)
        self.current_function = previous_function

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        node_id = f"{self.file_path}::{node.name}"
        self._record(
            node,
            node_id=node_id,
            parent_id=self.file_path,
            kind="class",
            name=node.name,
            class_name=None,
            signature=node.name,
            docstring=ast.get_docstring(node),
        )

        previous_class = self.current_class
        self.current_class = node_id
        self.generic_visit(node)
        self.current_class = previous_class

    def visit_Assign(self, node: ast.Assign) -> None:
        if self.current_class is None and self.current_function is None:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    node_id = f"{self.file_path}::{target.id}"
                    self._record(
                        node,
                        node_id=node_id,
                        parent_id=self.file_path,
                        kind="global_variable",
                        name=target.id,
                        class_name=None,
                        signature=target.id,
                        docstring=None,
                    )
        self.generic_visit(node)

    def _record(
        self,
        node: ast.AST,
        *,
        node_id: str,
        parent_id: str,
        kind: str,
        name: str,
        class_name: str | None,
        signature: str | None,
        docstring: str | None,
    ) -> None:
        start_line = int(node.lineno)
        end_line = int(node.end_lineno or start_line)
        self.symbols[node_id] = PythonSymbol(
            node_id=node_id,
            parent_id=parent_id,
            kind=kind,
            name=name,
            class_name=class_name,
            start_line=start_line,
            end_line=end_line,
            signature=signature,
            docstring=docstring,
            source="".join(self.source_lines[start_line - 1 : end_line]),
        )


def parse_python_outline(file_path: str, source: str) -> PythonOutline | None:
    """Parse one file using the semantics of OrcaLoca's pinned AST visitor."""

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    builder = _OutlineBuilder(file_path, source)
    builder.visit(tree)
    return PythonOutline(
        file_path=file_path,
        source=source,
        symbols=tuple(builder.symbols.values()),
    )


__all__ = ["PythonOutline", "PythonSymbol", "parse_python_outline"]
