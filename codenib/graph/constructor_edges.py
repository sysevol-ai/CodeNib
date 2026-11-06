# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Constructor edges for Python symbol graphs.

A call ``C(...)`` runs C's ``__init__`` and ``__new__``, but the decoded graph
links the call to the class only. A change inside a constructor is then
unreachable from the code that constructs the class. :func:`constructor_edges`
finds each definition that calls a class by name, directly, through an import
alias or through a module-level alias such as ``D = Distance``, and pairs it
with the constructor methods that the call runs: the class's own, or the first
ones found in its base classes.

The edges are derived from a graph and the source it was built from, and are
returned rather than written into the graph. Persisted graphs and the C++
decoder are unchanged; a consumer adds the edges to its in-memory view. The
rules were measured on real agent regressions in
``docs/experiments/guardian_constructor_edges_v1.md``.

Not covered: calls through variables such as ``cls(...)``, dataclass and
metaclass constructors, and operators.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from ..types import (
    NODE_TYPE_CLASS,
    NODE_TYPE_FIELD,
    NODE_TYPE_FILE,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
)

CONSTRUCTORS = ("__init__", "__new__")
_DEFS = {NODE_TYPE_FUNCTION, NODE_TYPE_METHOD}
_OWNERS = _DEFS | {NODE_TYPE_CLASS}
_DISAMBIGUATOR = re.compile(r"\(_codenib_l\d+_c\d+\)|@codenib-l\d+-c\d+")


@dataclass(frozen=True)
class ConstructorEdge:
    """``caller`` calls a class whose call runs ``constructor``."""

    caller: int
    constructor: int
    line: int  # 1-based line of the call in the caller's file


def short_name(name: str) -> str:
    """Last identifier of a graph symbol name: ``a.py:C.m()`` -> ``m``."""

    symbol = _DISAMBIGUATOR.sub("", name.split(":", 1)[-1])
    return symbol.replace("()", "").split(".")[-1]


def _last_name(expr: ast.AST) -> Optional[str]:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return None


class _Source:
    """Import aliases, class statements, module aliases and calls of one file."""

    def __init__(self, view: "_View", path: str):
        self.aliases: Dict[str, str] = {}
        self.classes: List[ast.ClassDef] = []
        self.assigns: Dict[int, Tuple[str, str]] = {}
        # owner vertex (or the file path for module-level code) -> name -> line
        self.calls: Dict[object, Dict[str, int]] = defaultdict(dict)
        try:
            tree = ast.parse((view.root / path).read_bytes())
        except (SyntaxError, ValueError, OSError):
            return
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.asname:
                        self.aliases[alias.asname] = alias.name
            elif isinstance(node, ast.ClassDef):
                self.classes.append(node)
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and _last_name(node.value)
            ):
                value = self.real(_last_name(node.value) or "")
                self.assigns[node.lineno] = (node.targets[0].id, value)

        defs: Dict[str, List[int]] = defaultdict(list)
        for vertex in view.by_file.get(path, ()):
            if view.types[vertex] in _OWNERS:
                defs[short_name(view.names[vertex])].append(vertex)

        def owner_of(node: ast.AST) -> Optional[int]:
            line = node.lineno
            hits = [
                v
                for v in defs.get(node.name, ())
                if view.starts[v] + 1 <= line <= view.ends[v] + 1
            ]
            if not hits:
                return None
            return min(hits, key=lambda v: view.ends[v] - view.starts[v])

        # Decorators and default values belong to the enclosing definition.
        stack: List[Tuple[ast.AST, object]] = [(tree, path)]
        while stack:
            node, owner = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if isinstance(node, ast.ClassDef):
                    outer = list(node.decorator_list)
                    inner = node.bases + node.keywords + node.body
                else:
                    outer = (
                        node.decorator_list
                        + node.args.defaults
                        + [d for d in node.args.kw_defaults if d is not None]
                    )
                    inner = node.body
                own = owner_of(node)
                stack.extend((child, owner) for child in outer)
                stack.extend((child, owner if own is None else own) for child in inner)
                continue
            if isinstance(node, ast.Call):
                name = _last_name(node.func)
                if name:
                    called = self.calls[owner]
                    real = self.real(name)
                    called[real] = min(called.get(real, node.lineno), node.lineno)
            stack.extend((child, owner) for child in ast.iter_child_nodes(node))

    def real(self, name: str) -> str:
        return self.aliases.get(name, name)


class _View:
    def __init__(self, graph, root: Path):
        self.root = root
        vs = graph.vs
        self.names: List[str] = vs["name"]
        self.types: List[str] = vs["type"]
        self.files: List[Optional[str]] = vs["file"]
        self.starts: List[Optional[int]] = vs["start_line"]
        self.ends: List[Optional[int]] = vs["end_line"]
        self.has_def: List[bool] = [bool(x) for x in vs["has_definition"]]
        self.by_name: Dict[str, int] = {}
        self.by_file: Dict[str, List[int]] = defaultdict(list)
        for vertex, name in enumerate(self.names):
            self.by_name.setdefault(name, vertex)
            if (
                self.has_def[vertex]
                and self.files[vertex]
                and self.starts[vertex] is not None
                and self.ends[vertex] is not None
            ):
                self.by_file[self.files[vertex]].append(vertex)
        self.refs: Dict[int, Set[int]] = defaultdict(set)
        self.callers: Dict[int, Set[int]] = defaultdict(set)
        self.children: Dict[int, Set[int]] = defaultdict(set)
        for (source, target), kind in zip(
            graph.get_edgelist(), graph.es["type"], strict=True
        ):
            if kind == "reference":
                self.refs[source].add(target)
                self.callers[target].add(source)
            elif kind == "contain":
                self.children[source].add(target)
        self._sources: Dict[str, _Source] = {}
        self._constructors: Dict[int, Dict[str, int]] = {}

    def source(self, path: str) -> _Source:
        if path not in self._sources:
            self._sources[path] = _Source(self, path)
        return self._sources[path]

    def file_vertex(self, path: str) -> Optional[int]:
        vertex = self.by_name.get(path)
        if vertex is not None and self.types[vertex] == NODE_TYPE_FILE:
            return vertex
        return None

    def classes_named(self, referrers: Iterable[Optional[int]], name: str) -> List[int]:
        return [
            target
            for referrer in referrers
            if referrer is not None
            for target in self.refs.get(referrer, ())
            if self.types[target] == NODE_TYPE_CLASS
            and short_name(self.names[target]) == name
        ]

    def bases(self, cls: int) -> List[int]:
        path, start, end = self.files[cls], self.starts[cls], self.ends[cls]
        name = short_name(self.names[cls])
        source = self.source(path)
        statements = [
            node
            for node in source.classes
            if node.name == name and start + 1 <= node.lineno <= end + 1
        ]
        if not statements:
            return []
        found: List[int] = []
        for base in min(statements, key=lambda node: node.lineno).bases:
            base_name = source.real(_last_name(base) or "")
            matches = self.classes_named([cls], base_name) or self.classes_named(
                [self.file_vertex(path)], base_name
            )
            found.extend(b for b in matches if b != cls and b not in found)
        return found

    def constructors(self, cls: int, stack: Tuple[int, ...] = ()) -> Dict[str, int]:
        if cls in self._constructors:
            return self._constructors[cls]
        if cls in stack:
            return {}
        found = {
            short_name(self.names[child]): child
            for child in self.children.get(cls, ())
            if self.types[child] == NODE_TYPE_METHOD
            and short_name(self.names[child]) in CONSTRUCTORS
        }
        for base in self.bases(cls):
            if all(method in found for method in CONSTRUCTORS):
                break
            for method, vertex in self.constructors(base, stack + (cls,)).items():
                found.setdefault(method, vertex)
        self._constructors[cls] = found
        return found

    def aliases(self) -> Dict[int, List[int]]:
        """``{class: [field]}`` for module-level ``V = C`` or ``V = mod.C``."""

        out: Dict[int, List[int]] = defaultdict(list)
        for vertex, kind in enumerate(self.types):
            path = self.files[vertex]
            if (
                kind != NODE_TYPE_FIELD
                or not self.has_def[vertex]
                or not path
                or self.starts[vertex] is None
                or "." in self.names[vertex].split(":", 1)[-1]
            ):
                continue
            assign = self.source(path).assigns.get(self.starts[vertex] + 1)
            if assign and assign[0] == short_name(self.names[vertex]):
                for cls in self.classes_named([self.file_vertex(path)], assign[1]):
                    out[cls].append(vertex)
        return out

    def call_line(self, referrer: int, name: str) -> Optional[int]:
        kind = self.types[referrer]
        if kind == NODE_TYPE_FILE:
            path = self.names[referrer]
            return self.source(path).calls.get(path, {}).get(name)
        path = self.files[referrer]
        if kind not in _OWNERS or not path:
            return None
        return self.source(path).calls.get(referrer, {}).get(name)


def constructor_edges(graph, source_root: Path | str) -> List[ConstructorEdge]:
    """Edges from each caller of a Python class to the constructors it runs.

    ``graph`` is the igraph graph of a :class:`~codenib.graph.code_graph.CodeGraph`
    and ``source_root`` the checkout it was built from. Edges that the graph
    already has are left out.
    """

    view = _View(graph, Path(source_root))
    aliases = view.aliases()
    edges: Dict[Tuple[int, int], int] = {}
    for cls, kind in enumerate(view.types):
        if kind != NODE_TYPE_CLASS or not view.has_def[cls] or not view.files[cls]:
            continue
        if not str(view.files[cls]).endswith((".py", ".pyi")):
            continue
        methods = view.constructors(cls)
        if not methods:
            continue
        for target in [cls, *aliases.get(cls, ())]:
            name = short_name(view.names[target])
            for referrer in view.callers.get(target, ()):
                line = view.call_line(referrer, name)
                if line is None:
                    continue
                for method in methods.values():
                    if method == referrer or referrer in view.callers.get(method, ()):
                        continue
                    key = (referrer, method)
                    edges[key] = min(edges.get(key, line), line)
    return [
        ConstructorEdge(caller, method, line)
        for (caller, method), line in edges.items()
    ]
