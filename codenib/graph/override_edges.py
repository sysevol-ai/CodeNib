# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Override pairs for Python symbol graphs.

A call ``shape.area()`` is linked to the ``area`` the analyser resolves it to,
usually the one on the declared base class. At run time it may execute any
subclass's ``area``. A change inside an override is then unreachable from the
code that calls the method through the base.

:func:`override_pairs` lists each method together with the same-named methods
of its class's ancestors. A consumer treats the callers of the ancestor's
method as callers of the override. Like constructor edges, the pairs are
derived from a graph and its source and returned, not written into the graph.

Base classes come from the reference edges that
:func:`~codenib.graph.edge_kinds.reference_kinds` classifies as ``inherit``.
Special methods (``__eq__``, ``__init__``) are left out: constructor edges
cover construction, and operators are not calls the graph records.

On 68 agent patches that broke tests, treating callers this way caught a
broken test for 38, up from 35, and raised the median number of tests flagged
on 35 clean patches from 3 to 11.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Set, Tuple

from ..types import NODE_TYPE_CLASS, NODE_TYPE_METHOD
from .constructor_edges import short_name
from .edge_kinds import INHERIT, file_mentions


def class_bases(graph, source_root: Path | str) -> Dict[int, Set[int]]:
    """``{class: {base class}}`` for the classes ``graph`` defines.

    Only references from one class to another can be base classes, so only
    the files that hold one are parsed.
    """

    root = Path(source_root)
    names: List[str] = graph.vs["name"]
    types: List[str] = graph.vs["type"]
    attributes = graph.es.attributes()
    bases: Dict[int, Set[int]] = defaultdict(set)
    if "anchor_line" not in attributes or "anchor_file" not in attributes:
        return bases
    files = graph.es["anchor_file"]
    lines = graph.es["anchor_line"]
    mentions: Dict[str, Dict[Tuple[int, str], str]] = {}
    for index, ((source, target), kind) in enumerate(
        zip(graph.get_edgelist(), graph.es["type"], strict=True)
    ):
        path, line = files[index], lines[index]
        if (
            kind != "reference"
            or source == target
            or types[source] != NODE_TYPE_CLASS
            or types[target] != NODE_TYPE_CLASS
            or line is None
            or not isinstance(path, str)
            or not path.endswith((".py", ".pyi"))
        ):
            continue
        if path not in mentions:
            mentions[path] = file_mentions(root / path)
        if mentions[path].get((int(line) + 1, short_name(names[target]))) == INHERIT:
            bases[source].add(target)
    return bases


def override_pairs(graph, source_root: Path | str) -> List[Tuple[int, int]]:
    """``(override, overridden)`` method vertices, one pair per ancestor.

    ``graph`` is the igraph graph of a :class:`~codenib.graph.code_graph.CodeGraph`
    and ``source_root`` the checkout it was built from.
    """

    bases = class_bases(graph, source_root)
    if not bases:
        return []
    names: List[str] = graph.vs["name"]
    types: List[str] = graph.vs["type"]
    children: Dict[int, Set[int]] = defaultdict(set)
    for (source, target), kind in zip(
        graph.get_edgelist(), graph.es["type"], strict=True
    ):
        if kind == "contain":
            children[source].add(target)
    methods: Dict[int, Dict[str, int]] = {}

    def methods_of(cls: int) -> Dict[str, int]:
        if cls not in methods:
            methods[cls] = {
                short_name(names[child]): child
                for child in sorted(children.get(cls, ()))
                if types[child] == NODE_TYPE_METHOD
            }
        return methods[cls]

    def ancestors(cls: int) -> Set[int]:
        seen: Set[int] = set()
        stack = [cls]
        while stack:
            for base in bases.get(stack.pop(), ()):
                if base not in seen and base != cls:
                    seen.add(base)
                    stack.append(base)
        return seen

    pairs: List[Tuple[int, int]] = []
    for cls in sorted(bases):
        own = methods_of(cls)
        if not own:
            continue
        for ancestor in sorted(ancestors(cls)):
            inherited = methods_of(ancestor)
            for name, method in own.items():
                if name.startswith("__") and name.endswith("__"):
                    continue
                overridden = inherited.get(name)
                if overridden is not None and overridden != method:
                    pairs.append((method, overridden))
    return pairs
