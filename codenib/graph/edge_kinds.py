# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""What each reference edge of a Python symbol graph is: a call, a base class,
an import, a type annotation or a plain read.

The graph records that one definition mentions another and on which line. It
does not record how. Consumers that need to say "calls" or "inherits" have
each re-read the source for it. :func:`reference_kinds` does that once: it
parses each file an edge is anchored in and classifies the mention of the
target's name on the anchor line.

The kinds are derived from a graph and its source and returned, not written
into the graph, like constructor edges. Persisted graphs and the C++ decoder
are unchanged.

An anchor is a line, not a column. When a line mentions the same name in two
ways, such as ``f(f)``, the edge gets the strongest kind, in the order of
:data:`KINDS`. A mention the parser cannot place, such as a name inside a
string annotation, is ``None``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..types import NODE_TYPE_FILE
from .constructor_edges import short_name

CALL = "call"
INHERIT = "inherit"
IMPORT = "import"
ANNOTATION = "annotation"
READ = "read"
# Strongest first: the kind an edge gets when its line mentions the name twice.
KINDS = (CALL, INHERIT, IMPORT, ANNOTATION, READ)
_RANK = {kind: rank for rank, kind in enumerate(KINDS)}

# (1-based line, name) -> kind, for one file
_Mentions = Dict[Tuple[int, str], str]


class _Classifier:
    def __init__(self) -> None:
        self.mentions: _Mentions = {}

    def record(self, line: int, name: str, kind: str) -> None:
        key = (line, name)
        known = self.mentions.get(key)
        if known is None or _RANK[kind] < _RANK[known]:
            self.mentions[key] = kind

    def name(self, node: ast.AST, kind: str) -> None:
        """Record the last name of ``a.b.c`` as ``kind``; the rest is read."""

        if isinstance(node, ast.Name):
            self.record(node.lineno, node.id, kind)
        elif isinstance(node, ast.Attribute):
            # The attribute name is the last token of the expression.
            self.record(node.end_lineno or node.lineno, node.attr, kind)
            self.visit(node.value, ANNOTATION if kind == ANNOTATION else READ)
        else:
            self.visit(node, ANNOTATION if kind == ANNOTATION else READ)

    def decorators(self, nodes: List[ast.expr]) -> None:
        for node in nodes:
            # ``@cache`` runs ``cache`` just as ``@cache()`` does.
            if isinstance(node, ast.Call):
                self.visit(node, READ)
            else:
                self.name(node, CALL)

    def function(self, node) -> None:
        self.decorators(node.decorator_list)
        arguments = node.args
        for argument in [
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
            arguments.vararg,
            arguments.kwarg,
        ]:
            if argument is not None and argument.annotation is not None:
                self.visit(argument.annotation, ANNOTATION)
        for default in [*arguments.defaults, *arguments.kw_defaults]:
            if default is not None:
                self.visit(default, READ)
        if node.returns is not None:
            self.visit(node.returns, ANNOTATION)
        for statement in node.body:
            self.visit(statement, READ)

    def visit(self, node: ast.AST, kind: str) -> None:
        if isinstance(node, (ast.Name, ast.Attribute)):
            self.name(node, kind)
        elif isinstance(node, ast.Call):
            self.name(node.func, CALL)
            for argument in node.args:
                self.visit(argument, READ)
            for keyword in node.keywords:
                self.visit(keyword.value, READ)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self.function(node)
        elif isinstance(node, ast.ClassDef):
            self.decorators(node.decorator_list)
            for base in node.bases:
                self.name(base, INHERIT)
            for keyword in node.keywords:
                self.visit(keyword.value, READ)
            for statement in node.body:
                self.visit(statement, READ)
        elif isinstance(node, ast.AnnAssign):
            self.visit(node.annotation, ANNOTATION)
            self.visit(node.target, READ)
            if node.value is not None:
                self.visit(node.value, READ)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                for part in alias.name.split("."):
                    self.record(alias.lineno, part, IMPORT)
        elif isinstance(node, ast.ImportFrom):
            for part in (node.module or "").split("."):
                if part:
                    self.record(node.lineno, part, IMPORT)
            for alias in node.names:
                self.record(alias.lineno, alias.name, IMPORT)
        else:
            # Inside an annotation, ``list[Cart] | None`` is annotation all
            # the way down.
            inner = ANNOTATION if kind == ANNOTATION else READ
            for child in ast.iter_child_nodes(node):
                self.visit(child, inner)


def file_mentions(path: Path) -> _Mentions:
    """``{(1-based line, name): kind}`` for every name ``path`` mentions."""

    try:
        tree = ast.parse(path.read_bytes())
    except (SyntaxError, ValueError, OSError, RecursionError):
        return {}
    classifier = _Classifier()
    try:
        classifier.visit(tree, READ)
    except RecursionError:
        return {}
    return classifier.mentions


def _module_name(path: str) -> str:
    """The name an import uses for a file: its stem, or its package directory."""

    parts = path.rsplit(".", 1)[0].split("/")
    if parts[-1] == "__init__" and len(parts) > 1:
        return parts[-2]
    return parts[-1]


def reference_kinds(graph, source_root: Path | str) -> List[Optional[str]]:
    """The kind of each edge of ``graph``, by edge index.

    ``None`` for an edge that is not a reference, is anchored outside Python
    source, or whose mention cannot be placed on its anchor line.
    """

    root = Path(source_root)
    vs = graph.vs
    names: List[str] = vs["name"]
    types: List[str] = vs["type"]
    attributes = graph.es.attributes()
    kinds: List[Optional[str]] = [None] * graph.ecount()
    if "anchor_line" not in attributes or "anchor_file" not in attributes:
        return kinds
    files = graph.es["anchor_file"]
    lines = graph.es["anchor_line"]
    cache: Dict[str, _Mentions] = {}
    for index, ((_, target), kind) in enumerate(
        zip(graph.get_edgelist(), graph.es["type"], strict=True)
    ):
        path, line = files[index], lines[index]
        if (
            kind != "reference"
            or line is None
            or not isinstance(path, str)
            or not path.endswith((".py", ".pyi"))
        ):
            continue
        if path not in cache:
            cache[path] = file_mentions(root / path)
        name = (
            _module_name(names[target])
            if types[target] == NODE_TYPE_FILE
            else short_name(names[target])
        )
        kinds[index] = cache[path].get((int(line) + 1, name))
    return kinds
