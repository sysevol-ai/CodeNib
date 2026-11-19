# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Operator edges for Python symbol graphs.

``a == b``, ``x[i]``, ``len(x)`` and ``for item in x`` run special methods
such as ``__eq__``, ``__getitem__``, ``__len__`` and ``__iter__``. No call
names them, so the decoded graph has no edge to them, and a change inside one
is unreachable from the code that uses the operator.

:func:`operator_edges` approximates the operator's receiver by the classes a
definition refers to: a definition that uses class ``C`` is paired with the
special methods of ``C``, its own or the first ones found in its base classes.
Constructors are left to :mod:`codenib.graph.constructor_edges`; attribute
access, pickling and copying hooks are left out.

Like constructor edges, the edges are derived from a graph and its source and
returned, not written into the graph.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from ..types import NODE_TYPE_CLASS, NODE_TYPE_FUNCTION, NODE_TYPE_METHOD
from .constructor_edges import short_name
from .override_edges import class_bases

_DEFS = {NODE_TYPE_FUNCTION, NODE_TYPE_METHOD}
# Special methods that no operator on an instance runs: constructors (see
# constructor_edges), class creation, attribute access, pickling and copying.
SKIPPED = frozenset(
    {
        "__init__",
        "__new__",
        "__init_subclass__",
        "__class_getitem__",
        "__set_name__",
        "__subclasshook__",
        "__getattr__",
        "__getattribute__",
        "__setattr__",
        "__delattr__",
        "__dir__",
        "__reduce__",
        "__reduce_ex__",
        "__getstate__",
        "__setstate__",
        "__copy__",
        "__deepcopy__",
        "__del__",
    }
)


@dataclass(frozen=True)
class OperatorEdge:
    """``user`` refers to a class whose instances run ``method`` on an operator."""

    user: int
    method: int
    line: Optional[int]  # 1-based line of the reference to the class


def _special(name: str) -> bool:
    return (
        len(name) > 4
        and name.startswith("__")
        and name.endswith("__")
        and name not in SKIPPED
    )


def operator_edges(
    graph, source_root: Path | str, bases: Optional[Dict[int, Set[int]]] = None
) -> List[OperatorEdge]:
    """Edges from each definition that uses a Python class to its special methods.

    ``graph`` is the igraph graph of a :class:`~codenib.graph.code_graph.CodeGraph`
    and ``source_root`` the checkout it was built from. ``bases`` is
    :func:`~codenib.graph.override_edges.class_bases` of the same graph, when
    the caller already has it. Edges that the graph already has are left out.
    """

    names: List[str] = graph.vs["name"]
    types: List[str] = graph.vs["type"]
    files: List[Optional[str]] = graph.vs["file"]
    has_def = [bool(x) for x in graph.vs["has_definition"]]
    anchors = (
        graph.es["anchor_line"] if "anchor_line" in graph.es.attributes() else None
    )
    children: Dict[int, Set[int]] = defaultdict(set)
    users: Dict[int, Dict[int, Optional[int]]] = defaultdict(dict)
    callees: Dict[int, Set[int]] = defaultdict(set)
    for index, ((source, target), kind) in enumerate(
        zip(graph.get_edgelist(), graph.es["type"], strict=True)
    ):
        if kind == "contain":
            children[source].add(target)
        elif kind == "reference":
            callees[source].add(target)
            if types[target] == NODE_TYPE_CLASS and types[source] in _DEFS:
                anchor = None if anchors is None else anchors[index]
                line = None if anchor is None else int(anchor) + 1
                # Keep the earliest known line of the reference.
                if source not in users[target]:
                    users[target][source] = line
                elif line is not None:
                    earlier = users[target][source]
                    users[target][source] = (
                        line if earlier is None else min(earlier, line)
                    )
    if bases is None:
        bases = class_bases(graph, source_root)
    own: Dict[int, Dict[str, int]] = {}
    resolved: Dict[int, Dict[str, int]] = {}

    def own_methods(cls: int) -> Dict[str, int]:
        if cls not in own:
            own[cls] = {
                short_name(names[child]): child
                for child in sorted(children.get(cls, ()))
                if types[child] == NODE_TYPE_METHOD
                and _special(short_name(names[child]))
            }
        return own[cls]

    def methods(cls: int, stack: Tuple[int, ...] = ()) -> Dict[str, int]:
        if cls in resolved:
            return resolved[cls]
        if cls in stack:
            return {}
        found = dict(own_methods(cls))
        for base in sorted(bases.get(cls, ())):
            for name, method in methods(base, stack + (cls,)).items():
                found.setdefault(name, method)
        resolved[cls] = found
        return found

    edges: List[OperatorEdge] = []
    for cls, kind in enumerate(types):
        if kind != NODE_TYPE_CLASS or not has_def[cls] or cls not in users:
            continue
        if not str(files[cls] or "").endswith((".py", ".pyi")):
            continue
        special = methods(cls)
        if not special:
            continue
        for user, line in sorted(users[cls].items()):
            for method in special.values():
                if method != user and method not in callees[user]:
                    edges.append(OperatorEdge(user, method, line))
    return edges
