# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Edges for Python method calls the type checker could not resolve.

``node.as_sql(compiler)`` on a parameter with no annotation has no known
receiver, so the decoded graph links the call to nothing, and a change inside
any ``as_sql`` is unreachable from the code that makes the call.

:func:`unresolved_call_edges` pairs such a call with every method of that name
the project defines, when there are only a few. A method name defined in more
than ``max_targets`` places is too common to guess from, and is left alone.
A caller that the graph already links to something of the same name made a
call that was resolved, and gains nothing.

The graph holds only the project's own code, so a call the checker resolved to
a built-in or standard-library type looks unresolved too. Two kinds of call
are therefore never guessed at: one on an imported name, such as
``hashlib.sha256()`` or ``os.path.join()``, whose target the checker knows;
and one whose name is a method of a built-in container, string or number, or
of a standard-library file, path, logger, pattern or process, such as
``rows.append()`` or ``logger.info()``, which far more often runs that method
than a project method of the same name.

Like constructor edges, the edges are derived from a graph and its source and
returned, not written into the graph.
"""

from __future__ import annotations

import ast
import io
import logging
import re
import subprocess
import warnings
from collections import Counter, OrderedDict, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from ..types import NODE_TYPE_FUNCTION, NODE_TYPE_METHOD
from ..utils import is_test_file
from .constructor_edges import short_name

_DEFS = {NODE_TYPE_FUNCTION, NODE_TYPE_METHOD}
MAX_TARGETS = 3
# ``.name`` then ``(``, with whitespace, line continuations or comments between.
_GAP = rb"(?:\s|\\\r?\n|#[^\n]*\n)*"
_CALL = re.compile(rb"\." + _GAP + rb"([A-Za-z_][A-Za-z0-9_]*)" + _GAP + rb"\(")
# Values of these types are what untyped code passes around most, and the
# graph holds none of their methods; a project method that shares a name with
# one of their methods is not a target.
_STANDARD_TYPES = (
    list,
    dict,
    set,
    frozenset,
    tuple,
    str,
    bytes,
    bytearray,
    int,
    float,
    complex,
    io.TextIOWrapper,
    io.BufferedRandom,
    io.BytesIO,
    io.StringIO,
    deque,
    OrderedDict,
    Counter,
    defaultdict,
    Path,
    logging.Logger,
    re.Pattern,
    re.Match,
    subprocess.Popen,
)
STANDARD_METHOD_NAMES = frozenset(
    name for kind in _STANDARD_TYPES for name in dir(kind) if not name.startswith("_")
)


@dataclass(frozen=True)
class UnresolvedCallEdge:
    """``caller`` calls ``.name(...)`` on an unknown receiver; ``method`` is one."""

    caller: int
    method: int
    line: int  # 1-based line of the call


def _attribute_calls(
    path: Path, wanted: Set[str], prefilter: bool = True
) -> List[Tuple[int, str]]:
    """``(line, name)`` of each ``receiver.name(...)`` call in one file.

    With ``prefilter``, a file whose text has no ``.name(`` for a name in
    ``wanted`` is not parsed. The text test is looser than the syntax tree, so
    it never skips a file with such a call. It matches ASCII names only, so
    the caller turns it off when a wanted name is not ASCII.
    """

    try:
        source = path.read_bytes()
    except OSError:
        return []
    if prefilter and not any(
        match.group(1).decode("ascii") in wanted for match in _CALL.finditer(source)
    ):
        return []
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    imported = _imported_names(tree)
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            name = node.func.attr
            if name.startswith("__") and name.endswith("__"):
                continue
            if not _on_imported_name(node.func.value, imported):
                calls.append((node.lineno, name))
    return calls


def _imported_names(tree: ast.AST) -> Dict[str, bool]:
    """Names bound by an import anywhere in a file; ``True`` for a module.

    ``import a.b`` binds the module ``a``; ``from a import b`` binds ``b``,
    which may be a module, a class or a function.
    """

    names: Dict[str, bool] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names[alias.asname or alias.name.partition(".")[0]] = True
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.setdefault(alias.asname or alias.name, False)
    return names


def _on_imported_name(receiver: ast.expr, imported: Dict[str, bool]) -> bool:
    """Whether ``receiver`` is an imported name or an attribute of a module.

    ``re.match()``, ``Path.cwd()`` and ``os.path.join()`` qualify;
    ``Model.objects.get()`` does not, since a class attribute may be of any
    type.
    """

    if isinstance(receiver, ast.Name):
        return receiver.id in imported
    while isinstance(receiver, ast.Attribute):
        receiver = receiver.value
    return isinstance(receiver, ast.Name) and imported.get(receiver.id, False)


def unresolved_call_edges(
    graph, source_root: Path | str, *, max_targets: int = MAX_TARGETS
) -> List[UnresolvedCallEdge]:
    """Edges from unresolved Python method calls to the methods they may run.

    ``graph`` is the igraph graph of a :class:`~codenib.graph.code_graph.CodeGraph`
    and ``source_root`` the checkout it was built from. Methods in test files
    are not targets.
    """

    root = Path(source_root)
    names: List[str] = graph.vs["name"]
    types: List[str] = graph.vs["type"]
    files: List[Optional[str]] = graph.vs["file"]
    starts: List[Optional[int]] = graph.vs["start_line"]
    ends: List[Optional[int]] = graph.vs["end_line"]
    has_def = [bool(x) for x in graph.vs["has_definition"]]
    methods: Dict[str, List[int]] = defaultdict(list)
    definitions: Dict[str, List[int]] = defaultdict(list)
    for vertex, kind in enumerate(types):
        path = files[vertex]
        if (
            kind not in _DEFS
            or not has_def[vertex]
            or not path
            or not path.endswith(".py")
            or starts[vertex] is None
            or ends[vertex] is None
        ):
            continue
        definitions[path].append(vertex)
        if kind == NODE_TYPE_METHOD and not is_test_file(path):
            methods[short_name(names[vertex])].append(vertex)
    callee_names: Dict[int, Set[str]] = defaultdict(set)
    for (source, target), kind in zip(
        graph.get_edgelist(), graph.es["type"], strict=True
    ):
        if kind == "reference":
            callee_names[source].add(short_name(names[target]))

    edges: Dict[Tuple[int, int], int] = {}
    wanted = {
        name
        for name, found in methods.items()
        if len(found) <= max_targets and name not in STANDARD_METHOD_NAMES
    }
    prefilter = all(name.isascii() for name in wanted)
    for path, defs in sorted(definitions.items()):
        calls = [
            (line, name)
            for line, name in _attribute_calls(root / path, wanted, prefilter)
            if name in wanted
        ]
        if not calls:
            continue
        # Innermost definition containing each call: the shortest span wins.
        spans = sorted(defs, key=lambda v: ends[v] - starts[v])
        for line, name in calls:
            caller = next(
                (v for v in spans if starts[v] + 1 <= line <= ends[v] + 1), None
            )
            if caller is None or name in callee_names[caller]:
                continue
            for method in methods[name]:
                if method != caller:
                    key = (caller, method)
                    edges[key] = min(edges.get(key, line), line)
    return [
        UnresolvedCallEdge(caller, method, line)
        for (caller, method), line in sorted(edges.items())
    ]
