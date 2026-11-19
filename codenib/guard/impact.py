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
:mod:`codenib.graph.constructor_edges`, the alias edges of
:mod:`codenib.graph.alias_edges`, the operator edges of
:mod:`codenib.graph.operator_edges`, the unresolved-call edges of
:mod:`codenib.graph.unresolved_call_edges` and the override pairs of
:mod:`codenib.graph.override_edges`. A reference that only registers a
command-line handler (:mod:`codenib.graph.registration_refs`) is not a call
and is not followed. Each test keeps the path it was found by, so a report
can say why the test is at risk.

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

from ..graph.alias_edges import alias_edges
from ..graph.constructor_edges import constructor_edges
from ..graph.identity import short_name, symbol_id  # noqa: F401 - re-exported
from ..graph.operator_edges import operator_edges
from ..graph.override_edges import class_bases, override_pairs
from ..graph.registration_refs import registration_references
from ..graph.unresolved_call_edges import unresolved_call_edges
from ..types import (
    NODE_TYPE_CLASS,
    NODE_TYPE_FILE,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
)
from ..utils import is_test_file
from .languages import (
    FILE,
    FUNCTION,
    Convention,
    convention_for,
    is_test_definition,
    test_id,
)

DEFAULT_DEPTH = 3
_DEFS = {NODE_TYPE_FUNCTION, NODE_TYPE_METHOD}
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


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
                else source[2:] if source.startswith("a/") else source
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
    # Per changed symbol: the definitions outside it that call it, each with
    # the line of its call, and how many tests reach it within ``depth`` calls.
    callers: Dict[str, List[Hop]] = field(default_factory=dict)
    tests_reaching: Dict[str, int] = field(default_factory=dict)
    # Tests that reach an edited definition outside test files. ``tests`` also
    # holds the tests the change itself touches and everything near a
    # module-level edit; a reviewer needs them apart.
    source_tests: List[TestAtRisk] = field(default_factory=list)


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
        # What a symbol is called in reports. The builder's key can name a
        # file by its module (``pkg.py`` for ``pkg/__init__.py``, no ``src/``
        # or ``lib/`` prefix); the unified name uses the real path, which is
        # what a test runner and a note's anchor need.
        unified = vs["unified_name"] if "unified_name" in vs.attributes() else None
        self.ids: List[str] = (
            [value or name for value, name in zip(unified, self.names, strict=True)]
            if unified is not None
            else self.names
        )
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
        # ``parser.set_defaults(handler=f)`` names ``f`` without running it.
        registrations = registration_references(graph, self.root)
        for index, ((source, target), kind) in enumerate(
            zip(graph.get_edgelist(), graph.es["type"], strict=True)
        ):
            if kind == "reference" and index not in registrations:
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
        # A use of ``pkg.name`` where ``name = obj.name`` is a use of ``obj.name``.
        for alias in alias_edges(graph, self.root):
            self.callers[alias.target].add(alias.user)
            if alias.line is not None:
                self.call_lines.setdefault((alias.user, alias.target), alias.line)
        bases = class_bases(graph, self.root)
        # Code that uses a class runs its operators: ``a == b`` runs ``__eq__``.
        for op in operator_edges(graph, self.root, bases):
            self.callers[op.method].add(op.user)
            if op.line is not None:
                self.call_lines.setdefault((op.user, op.method), op.line)
        # ``node.as_sql()`` on an unknown receiver may run any ``as_sql``.
        for call in unresolved_call_edges(graph, self.root):
            self.callers[call.method].add(call.caller)
            self.call_lines.setdefault((call.caller, call.method), call.line)
        # A call of ``Base.method`` may run any override of it. The callers
        # are those known before this step, so an override two levels down
        # gets its ancestors' callers once, not each other's additions.
        pairs = override_pairs(graph, self.root, bases)
        declared = {
            overridden: set(self.callers.get(overridden, ())) for _, overridden in pairs
        }
        for override, overridden in pairs:
            for caller in declared[overridden]:
                if caller == override:
                    continue
                self.callers[override].add(caller)
                line = self.call_lines.get((caller, overridden))
                if line is not None:
                    self.call_lines.setdefault((caller, override), line)
        self.by_file: Dict[str, List[int]] = defaultdict(list)
        for vertex, path in enumerate(self.files):
            if has_def[vertex] and path and self.starts[vertex] is not None:
                self.by_file[path].append(vertex)
        self._lines: Dict[str, List[str]] = {}
        self.conventions: Dict[int, Convention] = {}
        self.tests: Set[int] = set()
        # Test files that stand for their tests, by path. A file vertex has
        # no ``file`` or line range of its own.
        self.file_tests: Dict[str, int] = {}
        for vertex, kind in enumerate(self.types):
            if kind in _DEFS and has_def[vertex] and self.files[vertex]:
                convention = convention_for(self.files[vertex])
                if (
                    convention is not None
                    and convention.unit == FUNCTION
                    and is_test_definition(
                        convention,
                        # The unified name ends in the identifier in every
                        # language; a Go or Rust key is ``pkg/Type#name``.
                        short_name(self.ids[vertex]),
                        self.files[vertex],
                        self.starts[vertex],
                        self._source_lines,
                        method=kind == NODE_TYPE_METHOD,
                    )
                ):
                    self.tests.add(vertex)
                    self.conventions[vertex] = convention
            elif kind == NODE_TYPE_FILE:
                # Where tests are callbacks, the test file stands for them.
                convention = convention_for(self.names[vertex])
                if (
                    convention is not None
                    and convention.unit == FILE
                    and is_test_file(self.names[vertex])
                ):
                    self.tests.add(vertex)
                    self.conventions[vertex] = convention
                    self.file_tests[self.names[vertex]] = vertex

    def _source_lines(self, path: str) -> List[str]:
        if path not in self._lines:
            try:
                text = (self.root / path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            self._lines[path] = text.splitlines()
        return self._lines[path]

    def _test_id(self, vertex: int) -> str:
        return test_id(self.conventions[vertex], symbol_id(self.ids[vertex]))

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
            symbol=symbol_id(self.ids[vertex]),
            file=self.files[vertex]
            or (self.names[vertex] if self.types[vertex] == NODE_TYPE_FILE else None),
            line=None if start is None else start + 1,
            call_line=call,
        )

    def _walk(
        self, seeds: Set[int], depth: int
    ) -> Tuple[Dict[int, int], Dict[int, Optional[int]]]:
        """Callers of ``seeds`` up to ``depth`` calls away.

        Returns each reached definition's distance, and the definition it
        calls on the way to a seed.
        """

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
        return level, parent

    def _tests(
        self, level: Mapping[int, int], parent: Mapping[int, Optional[int]]
    ) -> List[TestAtRisk]:
        """The reached tests, each with its path to the seed it was found by."""

        found = []
        for test in self.tests & set(level):
            path: List[Hop] = []
            vertex: Optional[int] = test
            while vertex is not None:
                path.append(self.hop(vertex, parent[vertex]))
                vertex = parent[vertex]
            found.append(
                TestAtRisk(
                    test_id=self._test_id(test),
                    symbol=symbol_id(self.ids[test]),
                    depth=level[test],
                    path=tuple(path),
                )
            )
        return found

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
        owned: Dict[str, Set[int]] = defaultdict(set)
        rewritten = {p: set(lines) for p, lines in (rewritten_lines or {}).items()}
        for path in sorted(changed_lines):
            if convention_for(path) is None:
                continue
            # A test file that is the unit is edited as a whole: any rewritten
            # line may be an assertion. Lines only added leave it where its
            # calls put it, with the path that explains it.
            file_test = self.file_tests.get(path)
            if file_test is not None and rewritten.get(path):
                seeds.add(file_test)
            if path not in self.by_file:
                if file_test is None:
                    result.unmapped_files.append(path)
                continue
            for line in sorted(changed_lines[path]):
                found, unit = self.seeds(path, line)
                seeds |= found
                owner = self.innermost(path, line, _DEFS | {NODE_TYPE_CLASS})
                symbol = path if owner is None else symbol_id(self.ids[owner])
                owned[symbol] |= found
                if symbol not in reported:
                    reported.add(symbol)
                    result.changed.append(ChangedSymbol(symbol, path, line, unit))
        level, parent = self._walk(seeds, depth)
        for change in result.changed:
            if change.unit != "function":
                continue
            vertex = next(
                (s for s in seeds if symbol_id(self.ids[s]) == change.symbol), None
            )
            if vertex is not None:
                result.direct_callers[change.symbol] = sorted(
                    symbol_id(self.ids[c]) for c in self.callers.get(vertex, ())
                )
        for symbol, own in owned.items():
            sites: Dict[int, int] = {}
            for vertex in sorted(own):
                for caller in self.callers.get(vertex, ()):
                    if caller not in own:
                        sites.setdefault(caller, vertex)
            result.callers[symbol] = sorted(
                (self.hop(caller, vertex) for caller, vertex in sites.items()),
                key=lambda hop: (hop.file or "", hop.call_line or 0, hop.symbol),
            )
            reached = set(self._walk(own, depth)[0]) - own
            result.tests_reaching[symbol] = len(self.tests & reached)
        result.tests = self._tests(level, parent)
        # A line outside every definition seeds its whole file, which keeps the
        # test list safe but would count every caller of the file as a caller
        # of the change. Only edited definitions outside test files count here.
        source = {
            vertex
            for change in result.changed
            if change.unit != "file" and not is_test_file(change.file)
            for vertex in owned[change.symbol]
        }
        result.source_tests = sorted(
            self._tests(*self._walk(source, depth)),
            key=lambda t: (t.depth, t.test_id),
        )
        # An existing test whose own lines the change rewrote now checks the new
        # behaviour; passing it says little about the behaviour it protected.
        # In a recorded session an agent changed a default message, rewrote the
        # eight assertions that expected the old one, and passed its own run.
        edited: Set[str] = set()
        for test in self.tests & set(level):
            if self.conventions[test].unit == FILE:
                if level[test] == 0:
                    edited.add(self._test_id(test))
                continue
            lines = rewritten.get(self.files[test])
            start, end = self.starts[test], self.ends[test]
            if (
                level[test] == 0
                and lines
                and start is not None
                and end is not None
                and any(start + 1 <= line <= end + 1 for line in lines)
            ):
                edited.add(self._test_id(test))
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
