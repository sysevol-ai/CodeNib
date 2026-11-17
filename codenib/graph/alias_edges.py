# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Alias edges for Python symbol graphs.

A module can publish a function under a second name with a plain assignment:
``date_input = _main.date_input`` in a package's ``__init__``. Code that calls
``pkg.date_input(...)`` is then linked to the module variable, and only the
module itself is linked to the function. A change inside the function is
unreachable from everything that calls it by its published name.

:func:`alias_edges` pairs each definition that uses such a variable with the
function, method or class the variable names. Like constructor edges, the
edges are derived from a graph and its source and returned, not written into
the graph.

Only a bare name or attribute chain on the right-hand side counts, and only
when its last name is the target's own name. ``app = make_app()`` is a value,
not an alias, and is left alone.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from ..types import (
    NODE_TYPE_CLASS,
    NODE_TYPE_FIELD,
    NODE_TYPE_FILE,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
)
from .constructor_edges import short_name

_TARGETS = {NODE_TYPE_FUNCTION, NODE_TYPE_METHOD, NODE_TYPE_CLASS}


@dataclass(frozen=True)
class AliasEdge:
    """``user`` refers to a module variable that is another name for ``target``."""

    user: int
    target: int
    line: Optional[int]  # 1-based line of the use in the user's file


def _chain_name(expr: ast.AST) -> Optional[str]:
    """Last name of ``a.b.c``, or ``None`` for anything that computes a value."""

    last = None
    while isinstance(expr, ast.Attribute):
        last = last or expr.attr
        expr = expr.value
    if not isinstance(expr, ast.Name):
        return None
    return last or expr.id


def _alias_lines(path: Path) -> Dict[int, Tuple[str, str]]:
    """``{1-based line: (variable, aliased name)}`` of module-level aliases."""

    try:
        tree = ast.parse(path.read_bytes())
    except (SyntaxError, ValueError, OSError):
        return {}
    found: Dict[int, Tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        name = _chain_name(value)
        if isinstance(target, ast.Name) and name:
            found[node.lineno] = (target.id, name)
    return found


def alias_edges(graph, source_root: Path | str) -> List[AliasEdge]:
    """Edges from each user of a module-level alias to what it names.

    ``graph`` is the igraph graph of a :class:`~codenib.graph.code_graph.CodeGraph`
    and ``source_root`` the checkout it was built from.
    """

    root = Path(source_root)
    vs = graph.vs
    names: List[str] = vs["name"]
    types: List[str] = vs["type"]
    files: List[Optional[str]] = vs["file"]
    starts: List[Optional[int]] = vs["start_line"]
    attributes = graph.es.attributes()
    if "anchor_line" not in attributes:
        return []
    anchors = graph.es["anchor_line"]

    # Module-level variables by the file and 0-based line that define them.
    variables: Dict[Tuple[str, int], int] = {}
    for vertex, kind in enumerate(types):
        path = files[vertex]
        if (
            kind == NODE_TYPE_FIELD
            and path
            and path.endswith((".py", ".pyi"))
            and starts[vertex] is not None
            and "." not in names[vertex].split(":", 1)[-1]
        ):
            variables.setdefault((path, starts[vertex]), vertex)

    # A module refers to the aliased definition on the line of the assignment.
    candidates: Dict[str, List[Tuple[int, int]]] = defaultdict(list)
    users: Dict[int, Dict[int, Optional[int]]] = defaultdict(dict)
    for index, ((source, target), kind) in enumerate(
        zip(graph.get_edgelist(), graph.es["type"], strict=True)
    ):
        if kind != "reference":
            continue
        anchor = anchors[index]
        if types[target] == NODE_TYPE_FIELD:
            line = None if anchor is None else int(anchor) + 1
            users[target].setdefault(source, line)
        if (
            anchor is not None
            and types[source] == NODE_TYPE_FILE
            and types[target] in _TARGETS
        ):
            variable = variables.get((names[source], int(anchor)))
            if variable is not None:
                candidates[names[source]].append((variable, target))

    edges: Dict[Tuple[int, int], Optional[int]] = {}
    for path, pairs in candidates.items():
        aliases = _alias_lines(root / path)
        for variable, target in pairs:
            alias = aliases.get(starts[variable] + 1)
            if alias != (short_name(names[variable]), short_name(names[target])):
                continue
            seen: Set[int] = set()
            for user, line in users.get(variable, {}).items():
                if user != target and user not in seen:
                    seen.add(user)
                    edges.setdefault((user, target), line)
    return [AliasEdge(user, target, line) for (user, target), line in edges.items()]
