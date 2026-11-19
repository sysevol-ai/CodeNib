# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""References that register a Python function instead of calling it.

A command-line program built with :mod:`argparse` names each subcommand's
handler once, ``parser.set_defaults(handler=run_index)``, and calls it later
through the parsed arguments. The symbol graph records the name as a
reference from the function that builds the parser, so every test that builds
the parser looks as if it runs every handler, although building a parser runs
none of them.

:func:`registration_references` finds those references: a function named, not
called, in the arguments of a ``set_defaults(...)`` call, or in the table a
loop walks when the loop passes its own variable to ``set_defaults``::

    for command, handler in (("status", run_status), ("install", run_install)):
        sub = subparsers.add_parser(command)
        sub.set_defaults(handler=handler)

A caller relation that leaves them out no longer reaches a handler through
the parser. The edges are found from a graph and its source, like constructor
edges, and the graph is not changed.
"""

from __future__ import annotations

import ast
import warnings
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .identity import short_name

_REGISTER = "set_defaults"


def _named(expression: ast.AST) -> Set[Tuple[int, str]]:
    """``(0-based line, name)`` of each name in ``expression`` that is not called."""

    called = {
        id(node.func) for node in ast.walk(expression) if isinstance(node, ast.Call)
    }
    found = set()
    for node in ast.walk(expression):
        if id(node) in called:
            continue
        if isinstance(node, ast.Name):
            found.add((node.lineno - 1, node.id))
        elif isinstance(node, ast.Attribute):
            found.add((node.end_lineno - 1, node.attr))
    return found


def _set_defaults_calls(tree: ast.AST) -> List[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == _REGISTER
    ]


def _registered_names(source: bytes) -> Set[Tuple[int, str]]:
    """``(0-based line, name)`` of each function a file registers."""

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    found: Set[Tuple[int, str]] = set()
    for call in _set_defaults_calls(tree):
        for value in [*call.args, *(keyword.value for keyword in call.keywords)]:
            found |= _named(value)
    for loop in ast.walk(tree):
        if not isinstance(loop, (ast.For, ast.AsyncFor)):
            continue
        bound = {n.id for n in ast.walk(loop.target) if isinstance(n, ast.Name)}
        passes_variable = any(
            isinstance(keyword.value, ast.Name) and keyword.value.id in bound
            for call in _set_defaults_calls(loop)
            for keyword in call.keywords
        )
        if passes_variable:
            found |= _named(loop.iter)
    return found


def registration_references(graph, source_root: Path | str) -> Set[int]:
    """Indices of reference edges that register a function with ``set_defaults``.

    ``graph`` is the igraph graph of a :class:`~codenib.graph.code_graph.CodeGraph`
    and ``source_root`` the checkout it was built from. An edge without an
    anchor line is never one of them.
    """

    attributes = graph.es.attributes()
    if "anchor_line" not in attributes:
        return set()
    anchors: List[Optional[int]] = graph.es["anchor_line"]
    names: List[str] = graph.vs["name"]
    anchor_files = graph.es["anchor_file"] if "anchor_file" in attributes else None
    files: List[Optional[str]] = graph.vs["file"]
    candidates: Dict[str, List[int]] = defaultdict(list)
    targets: Dict[int, int] = {}
    for index, ((source, target), kind) in enumerate(
        zip(graph.get_edgelist(), graph.es["type"], strict=True)
    ):
        if kind != "reference" or anchors[index] is None:
            continue
        targets[index] = target
        path = (anchor_files[index] if anchor_files is not None else None) or files[
            source
        ]
        if path and path.endswith(".py"):
            candidates[path].append(index)

    root = Path(source_root)
    found: Set[int] = set()
    for path, indices in candidates.items():
        try:
            source = (root / path).read_bytes()
        except OSError:
            continue
        if _REGISTER.encode() not in source:
            continue
        registered = _registered_names(source)
        found.update(
            index
            for index in indices
            if (int(anchors[index]), short_name(names[targets[index]])) in registered
        )
    return found
