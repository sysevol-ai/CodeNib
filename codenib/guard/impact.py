# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests at risk from a change, found through the symbol graph.

A change is a set of changed lines in the code the graph was built from. Each
line maps to the innermost function or method that contains it; a line outside
any function maps to its class and the class's methods; a line outside any
class maps to every definition in the file. The tests at risk are the test
functions that reach that changed code in at most ``depth`` calls, following
the graph's references plus the constructor edges of
:mod:`codenib.graph.constructor_edges`. Each test keeps the path it was found
by, so a report can say why the test is at risk.

The method and its recall on real agent regressions are in
``docs/experiments/guardian_regression_check_v1.md`` and
``docs/experiments/guardian_constructor_edges_v1.md``.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from ..graph.constructor_edges import constructor_edges, short_name
from ..types import (
    NODE_TYPE_CLASS,
    NODE_TYPE_FILE,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
)
from ..utils import is_test_file

DEFAULT_DEPTH = 3
_DEFS = {NODE_TYPE_FUNCTION, NODE_TYPE_METHOD}
_DISAMBIGUATOR = re.compile(r"\(_codenib_l\d+_c\d+\)|@codenib-l\d+-c\d+")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def symbol_id(name: str) -> str:
    """Stable ``path:Qualified.name`` for a graph symbol name."""

    path, _, symbol = name.partition(":")
    return (
        f"{path}:{_DISAMBIGUATOR.sub('', symbol).replace('()', '')}" if symbol else path
    )


def _base_hunks(
    diff: str,
) -> Iterator[Tuple[str, str, int, int]]:
    """Walk a ``git diff --unified=0``.

    Yields ``("added", path, 0, 0)`` for a file that did not exist at the base,
    ``("file", path, 0, 0)`` for a base file, and ``("hunk", path, start,
    count)`` for each of its hunks in base line numbers.
    """

    old: Optional[str] = None
    current: Optional[str] = None
    for line in diff.splitlines():
        if line.startswith("--- "):
            source = line[4:].split("\t", 1)[0]
            old = (
                None
                if source == "/dev/null"
                else source[2:]
                if source.startswith("a/")
                else source
            )
            continue
        if line.startswith("+++ "):
            target = line[4:].split("\t", 1)[0]
            target = target[2:] if target.startswith("b/") else target
            if old is None:
                if target != "/dev/null":
                    yield "added", target, 0, 0
                current = None
            else:
                current = old
                yield "file", current, 0, 0
            continue
        match = _HUNK.match(line)
        if match and current is not None:
            start = int(match.group(1))
            count = 1 if match.group(2) is None else int(match.group(2))
            yield "hunk", current, start, count


def base_changed_lines(diff: str) -> Tuple[Dict[str, Set[int]], List[str]]:
    """Base-side changed lines of a ``git diff --unified=0``, and new files.

    A changed or deleted line counts as itself; a pure insertion counts as the
    line it follows (or line 1), so the function that gained code counts as
    changed. Files that did not exist at the base are returned separately.
    """

    changed: Dict[str, Set[int]] = {}
    added: List[str] = []
    for kind, path, start, count in _base_hunks(diff):
        if kind == "added":
            added.append(path)
        elif kind == "file":
            changed.setdefault(path, set())
        elif count == 0:
            changed[path].add(max(start, 1))
        else:
            changed[path].update(range(start, start + count))
    return changed, added


def base_rewritten_lines(diff: str) -> Dict[str, Set[int]]:
    """Base lines that a ``git diff --unified=0`` changes or deletes.

    Unlike :func:`base_changed_lines`, a pure insertion counts for no line: a
    new test added after an existing one leaves the existing one unedited.
    """

    rewritten: Dict[str, Set[int]] = {}
    for kind, path, start, count in _base_hunks(diff):
        if kind == "hunk" and count:
            rewritten.setdefault(path, set()).update(range(start, start + count))
    return rewritten


@dataclass(frozen=True)
class Hop:
    """One definition on a path, and the line where it calls the next hop."""

    symbol: str
    file: Optional[str]
    line: Optional[int]  # 1-based definition line
    call_line: Optional[int] = None  # 1-based line of the call to the next hop


@dataclass(frozen=True)
class TestAtRisk:
    test_id: str  # pytest node id
    symbol: str
    depth: int
    path: Tuple[Hop, ...]  # from the test to the changed code


@dataclass(frozen=True)
class ChangedSymbol:
    symbol: str
    file: str
    line: int
    unit: str  # "function", "class" or "file"


@dataclass
class Impact:
    changed: List[ChangedSymbol] = field(default_factory=list)
    tests: List[TestAtRisk] = field(default_factory=list)
    total_tests: int = 0
    depth: int = DEFAULT_DEPTH
    direct_callers: Dict[str, List[str]] = field(default_factory=dict)
    unmapped_files: List[str] = field(default_factory=list)
    new_files: List[str] = field(default_factory=list)
    # pytest ids of tests at risk whose own base lines the change rewrote
    edited_tests: List[str] = field(default_factory=list)


def pytest_id(name: str) -> str:
    """``tests/a.py:Suite.test_x()`` -> ``tests/a.py::Suite::test_x``."""

    path, _, symbol = symbol_id(name).partition(":")
    return "::".join([path, *symbol.split(".")]) if symbol else path


class ImpactGraph:
    """Reverse reference index of one symbol graph, plus constructor edges."""

    def __init__(self, graph, source_root: Path | str):
        self.root = Path(source_root)
        vs = graph.vs
        self.names: List[str] = vs["name"]
        self.types: List[str] = vs["type"]
        self.files: List[Optional[str]] = vs["file"]
        self.starts: List[Optional[int]] = vs["start_line"]
        self.ends: List[Optional[int]] = vs["end_line"]
        has_def = [bool(x) for x in vs["has_definition"]]
        self.callers: Dict[int, Set[int]] = defaultdict(set)
        self.children: Dict[int, Set[int]] = defaultdict(set)
        # 1-based line of the call from a caller to a callee. Edge anchors are
        # 0-based; constructor edges carry 1-based lines.
        self.call_lines: Dict[Tuple[int, int], int] = {}
        attributes = graph.es.attributes()
        anchors = graph.es["anchor_line"] if "anchor_line" in attributes else None
        for index, ((source, target), kind) in enumerate(
            zip(graph.get_edgelist(), graph.es["type"], strict=True)
        ):
            if kind == "reference":
                self.callers[target].add(source)
                if anchors is not None and anchors[index] is not None:
                    self.call_lines.setdefault(
                        (source, target), int(anchors[index]) + 1
                    )
            elif kind == "contain":
                self.children[source].add(target)
        for edge in constructor_edges(graph, self.root):
            self.callers[edge.constructor].add(edge.caller)
            self.call_lines.setdefault((edge.caller, edge.constructor), edge.line)
        self.by_file: Dict[str, List[int]] = defaultdict(list)
        for vertex, path in enumerate(self.files):
            if has_def[vertex] and path and self.starts[vertex] is not None:
                self.by_file[path].append(vertex)
        self.tests: Set[int] = {
            vertex
            for vertex, kind in enumerate(self.types)
            if kind in _DEFS
            and has_def[vertex]
            and self.files[vertex]
            and is_test_file(self.files[vertex])
            and short_name(self.names[vertex]).startswith("test")
        }

    def innermost(self, path: str, line: int, kinds: Set[str]) -> Optional[int]:
        best = None
        for vertex in self.by_file.get(path, ()):
            start, end = self.starts[vertex], self.ends[vertex]
            if (
                self.types[vertex] in kinds
                and end is not None
                and start + 1 <= line <= end + 1
            ):
                if best is None or end - start < self.ends[best] - self.starts[best]:
                    best = vertex
        return best

    def seeds(self, path: str, line: int) -> Tuple[Set[int], str]:
        vertex = self.innermost(path, line, _DEFS)
        if vertex is not None:
            return {vertex}, "function"
        vertex = self.innermost(path, line, {NODE_TYPE_CLASS})
        if vertex is not None:
            methods = {
                c for c in self.children.get(vertex, ()) if self.types[c] in _DEFS
            }
            return {vertex} | methods, "class"
        return set(self.by_file.get(path, ())), "file"

    def hop(self, vertex: int, next_vertex: Optional[int]) -> Hop:
        start = self.starts[vertex]
        call = (
            None if next_vertex is None else self.call_lines.get((vertex, next_vertex))
        )
        return Hop(
            symbol=symbol_id(self.names[vertex]),
            file=self.files[vertex]
            or (self.names[vertex] if self.types[vertex] == NODE_TYPE_FILE else None),
            line=None if start is None else start + 1,
            call_line=call,
        )

    def impact(
        self,
        changed_lines: Mapping[str, Iterable[int]],
        *,
        depth: int = DEFAULT_DEPTH,
        new_files: Sequence[str] = (),
        rewritten_lines: Optional[Mapping[str, Iterable[int]]] = None,
    ) -> Impact:
        result = Impact(
            depth=depth, total_tests=len(self.tests), new_files=list(new_files)
        )
        seeds: Set[int] = set()
        reported: Set[str] = set()
        for path in sorted(changed_lines):
            if not path.endswith((".py", ".pyi")):
                continue
            if path not in self.by_file:
                result.unmapped_files.append(path)
                continue
            for line in sorted(changed_lines[path]):
                found, unit = self.seeds(path, line)
                seeds |= found
                owner = self.innermost(path, line, _DEFS | {NODE_TYPE_CLASS})
                symbol = path if owner is None else symbol_id(self.names[owner])
                if symbol not in reported:
                    reported.add(symbol)
                    result.changed.append(ChangedSymbol(symbol, path, line, unit))
        parent: Dict[int, Optional[int]] = {seed: None for seed in seeds}
        level: Dict[int, int] = {seed: 0 for seed in seeds}
        queue = deque(sorted(seeds))
        while queue:
            vertex = queue.popleft()
            if level[vertex] >= depth:
                continue
            for caller in sorted(self.callers.get(vertex, ())):
                if caller not in level:
                    level[caller] = level[vertex] + 1
                    parent[caller] = vertex
                    queue.append(caller)
        for change in result.changed:
            if change.unit != "function":
                continue
            vertex = next(
                (s for s in seeds if symbol_id(self.names[s]) == change.symbol), None
            )
            if vertex is not None:
                result.direct_callers[change.symbol] = sorted(
                    symbol_id(self.names[c]) for c in self.callers.get(vertex, ())
                )
        for test in self.tests & set(level):
            path: List[Hop] = []
            vertex: Optional[int] = test
            while vertex is not None:
                path.append(self.hop(vertex, parent[vertex]))
                vertex = parent[vertex]
            result.tests.append(
                TestAtRisk(
                    test_id=pytest_id(self.names[test]),
                    symbol=symbol_id(self.names[test]),
                    depth=level[test],
                    path=tuple(path),
                )
            )
        # An existing test whose own lines the change rewrote now checks the new
        # behaviour; passing it says little about the behaviour it protected.
        # In a recorded session an agent changed a default message, rewrote the
        # eight assertions that expected the old one, and passed its own run.
        rewritten = {p: set(lines) for p, lines in (rewritten_lines or {}).items()}
        edited: Set[str] = set()
        for test in self.tests & set(level):
            lines = rewritten.get(self.files[test])
            start, end = self.starts[test], self.ends[test]
            if (
                level[test] == 0
                and lines
                and start is not None
                and end is not None
                and any(start + 1 <= line <= end + 1 for line in lines)
            ):
                edited.add(pytest_id(self.names[test]))
        result.edited_tests = sorted(edited)
        # Edited existing tests come first, then tests that reach the change
        # through calls. Other tests at depth 0 (in an edited file, or with only
        # code added after them) come last: a real session filled the suggested
        # command with the agent's own new tests and left out the ones that
        # call in.
        result.tests.sort(
            key=lambda t: (t.test_id not in edited, t.depth == 0, t.depth, t.test_id)
        )
        return result
