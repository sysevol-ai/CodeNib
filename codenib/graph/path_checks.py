# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Deterministic call-path rules over the code graph.

An agent can find a call site, but it cannot promise that it found every path
to one. A path rule asks the graph that question exhaustively: every reference
path from an entry point to a ``to`` symbol must pass a ``through`` symbol, or
a function on the path that also references an ``or_calls`` symbol (such as a
guard factory). Entry points are nodes with no callers left after ``exclude``
removes files such as tests.

The checker never passes a path it cannot see. A call the indexer could not
resolve has no edge, so for Python sources a call site that is not anchored by
any resolved reference is matched by its callee name: first to a ``to``
symbol, then, only when the resolved callers of a node all pass, to that node.
Such hops are marked ``resolved: false``. Guards and ``or_calls`` count only
through resolved references. Both choices can over-report; neither hides a
path. Other languages use resolved references only, which the report states.
"""

from __future__ import annotations

import ast
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from ..types import (
    EDGE_TYPE_REFERENCE,
    NODE_TYPE_CLASS,
    NODE_TYPE_DIRECTORY,
    NODE_TYPE_FILE,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
)
from .code_graph import CodeGraph

RULES_VERSION = 1
_RULE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_RULE_KEYS = {"id", "message", "to", "through", "or_calls", "exclude"}
_CALLER_TYPES = {NODE_TYPE_FUNCTION, NODE_TYPE_METHOD, NODE_TYPE_CLASS}
_NOT_ON_PATHS = {NODE_TYPE_DIRECTORY, "root"}


class PathRuleError(ValueError):
    """A rules file or a rule symbol that cannot be checked as written."""


@dataclass(frozen=True)
class PathRule:
    """Every path to ``to`` passes ``through``, or a node that calls ``or_calls``."""

    id: str
    to: Tuple[str, ...]
    through: Tuple[str, ...] = ()
    or_calls: Tuple[str, ...] = ()
    exclude: Tuple[str, ...] = ()
    message: str = ""


@dataclass(frozen=True)
class PathHop:
    """One node of a witness path; ``resolved`` describes the edge into it."""

    name: str
    file: Optional[str]
    line: Optional[int]  # 1-based
    resolved: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "file": self.file,
            "line": self.line,
            "resolved": self.resolved,
        }


@dataclass(frozen=True)
class PathViolation:
    """A caller of ``to`` that an unguarded entry point reaches."""

    to: str
    caller: str
    file: Optional[str]
    line: Optional[int]  # 1-based line of the call when known
    resolved: bool  # False when the call to ``to`` was matched by name
    path: Tuple[PathHop, ...]  # entry point first, ``to`` last
    changed: Optional[bool] = None  # None when no diff was given

    def to_dict(self) -> Dict[str, Any]:
        return {
            "to": self.to,
            "caller": self.caller,
            "file": self.file,
            "line": self.line,
            "resolved": self.resolved,
            "changed": self.changed,
            "path": [hop.to_dict() for hop in self.path],
        }


@dataclass
class RuleResult:
    rule: PathRule
    resolved: Dict[str, List[str]]  # field -> readable names the symbols resolved to
    violations: List[PathViolation] = field(default_factory=list)
    unchanged_violations: int = 0  # filtered out by the diff

    @property
    def passed(self) -> bool:
        return not self.violations

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.rule.id,
            "message": self.rule.message,
            "passed": self.passed,
            "resolved": self.resolved,
            "exclude": list(self.rule.exclude),
            "violations": [item.to_dict() for item in self.violations],
            "unchanged_violations": self.unchanged_violations,
        }


@dataclass
class PathCheckReport:
    rules: List[RuleResult]
    coverage: Dict[str, Any]

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.rules)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "violations": sum(len(result.violations) for result in self.rules),
            "rules": [result.to_dict() for result in self.rules],
            "coverage": self.coverage,
        }


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def parse_rules(document: object) -> List[PathRule]:
    """Validate a decoded rules document: ``{version: 1, rules: [...]}``."""

    if not isinstance(document, Mapping) or set(document) != {"version", "rules"}:
        raise PathRuleError("a rules file has exactly the keys version and rules")
    if document["version"] != RULES_VERSION:
        raise PathRuleError(f"unsupported rules version: {document['version']!r}")
    items = document["rules"]
    if not isinstance(items, list) or not items:
        raise PathRuleError("rules must be a nonempty list")
    rules: List[PathRule] = []
    for position, item in enumerate(items, 1):
        if not isinstance(item, Mapping):
            raise PathRuleError(f"rule {position} must be a mapping")
        unknown = sorted(set(item) - _RULE_KEYS)
        if unknown:
            raise PathRuleError(f"rule {position}: unknown keys {', '.join(unknown)}")
        rule_id = item.get("id")
        if not isinstance(rule_id, str) or not _RULE_ID.fullmatch(rule_id):
            raise PathRuleError(f"rule {position}: id must match {_RULE_ID.pattern}")
        message = item.get("message", "")
        if not isinstance(message, str):
            raise PathRuleError(f"rule {rule_id}: message must be a string")
        lists = {
            key: _string_list(item.get(key, []), rule_id, key)
            for key in ("to", "through", "or_calls", "exclude")
        }
        if not lists["to"]:
            raise PathRuleError(f"rule {rule_id}: to needs at least one symbol")
        if not lists["through"] and not lists["or_calls"]:
            raise PathRuleError(f"rule {rule_id}: give through, or_calls, or both")
        rules.append(PathRule(id=rule_id, message=message, **lists))
    duplicates = sorted({r.id for r in rules if [x.id for x in rules].count(r.id) > 1})
    if duplicates:
        raise PathRuleError(f"duplicate rule ids: {', '.join(duplicates)}")
    return rules


def load_rules(path: Path) -> List[PathRule]:
    """Read and validate a YAML rules file."""

    import yaml

    try:
        document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise PathRuleError(f"cannot read rules {path}: {exc}") from exc
    return parse_rules(document)


def _string_list(value: object, rule_id: str, key: str) -> Tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(entry, str) and entry.strip() for entry in value
    ):
        raise PathRuleError(f"rule {rule_id}: {key} must be a list of names")
    return tuple(entry.strip() for entry in value)


# ---------------------------------------------------------------------------
# Graph view
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CallSite:
    caller: int
    file: str
    line: int  # 1-based


class _GraphView:
    """Reference adjacency, node locations, and unresolved Python call sites."""

    def __init__(self, graph: CodeGraph, repo_root: Path):
        g = graph.graph
        self.graph = graph
        self.names: List[str] = list(g.vs["name"])
        attributes = set(g.vs.attributes())
        count = g.vcount()
        self.types: List[str] = list(g.vs["type"])
        self.labels: List[str] = (
            [u or n for u, n in zip(g.vs["unified_name"], self.names, strict=True)]
            if "unified_name" in attributes
            else list(self.names)
        )
        files = g.vs["file"] if "file" in attributes else [None] * count
        self.files: List[Optional[str]] = [
            f or (n if t == NODE_TYPE_FILE else None)
            for f, n, t in zip(files, self.names, self.types, strict=True)
        ]
        starts = g.vs["start_line"] if "start_line" in attributes else [None] * count
        ends = g.vs["end_line"] if "end_line" in attributes else [None] * count
        self.starts: List[Optional[int]] = list(starts)
        self.ends: List[Optional[int]] = list(ends)
        self.succ: List[Set[int]] = [set() for _ in range(count)]
        self.pred: List[Set[int]] = [set() for _ in range(count)]
        self.anchors: Set[Tuple[str, int, str]] = set()
        edge_attributes = set(g.es.attributes())
        edge_types = g.es["type"]
        anchor_files = (
            g.es["anchor_file"]
            if "anchor_file" in edge_attributes
            else [None] * len(edge_types)
        )
        anchor_lines = (
            g.es["anchor_line"]
            if "anchor_line" in edge_attributes
            else [None] * len(edge_types)
        )
        for (source, target), kind, anchor_file, anchor_line in zip(
            g.get_edgelist(), edge_types, anchor_files, anchor_lines, strict=True
        ):
            if kind != EDGE_TYPE_REFERENCE:
                continue
            self.succ[source].add(target)
            self.pred[target].add(source)
            if anchor_file is not None and anchor_line is not None:
                self.anchors.add((anchor_file, int(anchor_line), self.bare(target)))
        self.repo_root = repo_root
        self._unresolved: Optional[Dict[str, List[_CallSite]]] = None
        self.coverage: Dict[str, Any] = {}

    def bare(self, node: int) -> str:
        """The last name of a symbol: ``a.py:A.run()`` -> ``run``."""

        label = self.labels[node].rsplit(":", 1)[-1]
        return label.rsplit(".", 1)[-1].split("(", 1)[0]

    def hop(self, node: int, resolved: bool = True) -> PathHop:
        start = self.starts[node]
        line = start + 1 if start is not None else None
        return PathHop(self.labels[node], self.files[node], line, resolved)

    def resolve(self, rule: PathRule, key: str, symbols: Iterable[str]) -> List[int]:
        """Resolve each symbol strictly: a full name, ``Class.method``, or a bare name.

        The first form that matches decides; it must match exactly one node.
        """

        nodes = []
        for symbol in symbols:
            wanted = {symbol, symbol + "()"}
            matches: List[int] = []
            for form in (self._full, self._qualified, self._bare_form):
                matches = [
                    node
                    for node in range(len(self.names))
                    if self.types[node] not in _NOT_ON_PATHS | {NODE_TYPE_FILE}
                    and form(node) & wanted
                ]
                if matches:
                    break
            if len(matches) != 1:
                found = ", ".join(self.labels[n] for n in matches[:8]) or "none"
                raise PathRuleError(
                    f"rule {rule.id}: {key} {symbol!r} must name exactly one "
                    f"symbol; found {found}"
                )
            nodes.append(matches[0])
        return nodes

    def _full(self, node: int) -> Set[str]:
        return {self.names[node], self.labels[node]}

    def _qualified(self, node: int) -> Set[str]:
        return {self.labels[node].rsplit(":", 1)[-1]}

    def _bare_form(self, node: int) -> Set[str]:
        return {self.bare(node), self.bare(node) + "()"}

    def unresolved_calls(self) -> Dict[str, List[_CallSite]]:
        """Python call sites with no resolved reference at their line, by name."""

        if self._unresolved is not None:
            return self._unresolved
        by_file: Dict[str, List[int]] = {}
        file_nodes: Dict[str, int] = {}
        for node, (kind, path) in enumerate(zip(self.types, self.files, strict=True)):
            if path is None:
                continue
            if kind == NODE_TYPE_FILE:
                file_nodes[path] = node
            elif kind in _CALLER_TYPES and self.starts[node] is not None:
                by_file.setdefault(path, []).append(node)
        sites: Dict[str, List[_CallSite]] = {}
        scanned, errors = 0, []
        for path in sorted(set(by_file) | set(file_nodes)):
            if not path.endswith(".py"):
                continue
            try:
                source = (self.repo_root / path).read_text(encoding="utf-8")
                tree = ast.parse(source, filename=path)
            except (OSError, SyntaxError, ValueError) as exc:
                errors.append(f"{path}: {type(exc).__name__}")
                continue
            scanned += 1
            for call in ast.walk(tree):
                if not isinstance(call, ast.Call):
                    continue
                if isinstance(call.func, ast.Attribute):
                    name, line = call.func.attr, call.func.end_lineno or call.lineno
                elif isinstance(call.func, ast.Name):
                    name, line = call.func.id, call.func.lineno
                else:
                    continue
                if (path, line - 1, name) in self.anchors:
                    continue
                caller = self._innermost(by_file.get(path, ()), line - 1)
                if caller is None:
                    caller = file_nodes.get(path)
                if caller is not None:
                    sites.setdefault(name, []).append(_CallSite(caller, path, line))
        others = sorted(
            {p for p in set(by_file) | set(file_nodes) if not p.endswith(".py")}
        )
        self.coverage = {
            "name_fallback": "python",
            "python_files_scanned": scanned,
            "python_parse_errors": errors,
            "files_without_fallback": len(others),
            "unresolved_python_call_sites": sum(len(v) for v in sites.values()),
        }
        self._unresolved = sites
        return sites

    def _innermost(self, nodes: Sequence[int], line: int) -> Optional[int]:
        best, span = None, None
        for node in nodes:
            start, end = self.starts[node], self.ends[node]
            if start is None or end is None or not start <= line <= end:
                continue
            if span is None or end - start < span:
                best, span = node, end - start
        return best


# ---------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------


def check_paths(
    graph: CodeGraph,
    rules: Sequence[PathRule],
    repo_root: Path,
    changed_lines: Optional[Mapping[str, Set[int]]] = None,
) -> PathCheckReport:
    """Check each rule against ``graph``, built from the checkout at ``repo_root``.

    ``changed_lines`` maps repository-relative files to changed 1-based lines.
    When given, a violation is kept only if its unguarded region or its call
    to ``to`` touches a changed line; the rest are counted, not listed. A rule
    symbol that names no single node raises :class:`PathRuleError`.
    """

    view = _GraphView(graph, Path(repo_root))
    unresolved = view.unresolved_calls()
    results = []
    for rule in rules:
        sinks = view.resolve(rule, "to", rule.to)
        guards = set(view.resolve(rule, "through", rule.through))
        sanitizers = set(view.resolve(rule, "or_calls", rule.or_calls))
        result = RuleResult(
            rule,
            {
                "to": [view.labels[n] for n in sinks],
                "through": [view.labels[n] for n in sorted(guards)],
                "or_calls": [view.labels[n] for n in sorted(sanitizers)],
            },
        )
        _check_rule(
            view, rule, sinks, guards, sanitizers, unresolved, changed_lines, result
        )
        results.append(result)
    return PathCheckReport(results, dict(view.coverage))


def _check_rule(
    view: _GraphView,
    rule: PathRule,
    sinks: List[int],
    guards: Set[int],
    sanitizers: Set[int],
    unresolved: Dict[str, List[_CallSite]],
    changed_lines: Optional[Mapping[str, Set[int]]],
    result: RuleResult,
) -> None:
    patterns = [_glob(pattern) for pattern in rule.exclude]

    def excluded(node: int) -> bool:
        path = view.files[node]
        return view.types[node] in _NOT_ON_PATHS or (
            path is not None and any(p.fullmatch(path) for p in patterns)
        )

    def guarded(node: int) -> bool:
        return node in guards or bool(view.succ[node] & sanitizers)

    def callers(node: int, loose: bool) -> List[Tuple[int, bool]]:
        found = [(p, True) for p in sorted(view.pred[node]) if not excluded(p)]
        if loose:
            seen = {p for p, _ in found}
            for site in unresolved.get(view.bare(node), ()):
                if site.caller not in seen and not excluded(site.caller):
                    seen.add(site.caller)
                    found.append((site.caller, False))
        return found

    direct: Dict[Tuple[int, int], Optional[_CallSite]] = {}
    for sink in sinks:
        for caller, _ in callers(sink, loose=False):
            if caller not in sinks:
                direct[(sink, caller)] = None
        for site in unresolved.get(view.bare(sink), ()):
            key = (sink, site.caller)
            if site.caller not in sinks and not excluded(site.caller):
                direct.setdefault(key, site)

    for (sink, caller), site in sorted(direct.items()):
        if guarded(caller):
            continue
        found = None
        for loose in (False, True):
            found = _unguarded_entry(caller, loose, callers, guarded)
            if found[0] is not None:
                break
        entry_path, region = found
        if entry_path is None:
            continue
        hops = [view.hop(node, resolved) for node, resolved in entry_path]
        hops.append(view.hop(sink, site is None))
        file = site.file if site is not None else view.files[caller]
        line = site.line if site is not None else _call_line(view, caller, sink)
        changed = None
        if changed_lines is not None:
            changed = any(_node_changed(view, n, changed_lines) for n in region) or (
                file is not None
                and line is not None
                and line in changed_lines.get(file, ())
            )
        violation = PathViolation(
            to=view.labels[sink],
            caller=view.labels[caller],
            file=file,
            line=line,
            resolved=site is None,
            path=tuple(hops),
            changed=changed,
        )
        if changed is False:
            result.unchanged_violations += 1
        else:
            result.violations.append(violation)


def _unguarded_entry(start, loose, callers, guarded):
    """Shortest path from an entry point to ``start`` that avoids guarded nodes.

    Returns ``(path, region)``: the path as ``(node, resolved_edge)`` pairs,
    entry first, or None; and every node that reaches ``start`` unguarded.
    """

    parents: Dict[int, Tuple[Optional[int], bool]] = {start: (None, True)}
    queue = deque([start])
    entry = None
    while queue:
        node = queue.popleft()
        found = callers(node, loose)
        if not found and entry is None:
            entry = node
        for caller, resolved in found:
            if caller not in parents and not guarded(caller):
                parents[caller] = (node, resolved)
                queue.append(caller)
    if entry is None:
        return None, set(parents)
    nodes, node = [], entry
    while node is not None:
        nodes.append(node)
        node = parents[node][0]
    # parents[a] = (b, resolved) records the edge a -> b; describe it on b's hop.
    hops = [(nodes[0], True)]
    for previous, node in zip(nodes, nodes[1:], strict=False):
        hops.append((node, parents[previous][1]))
    return hops, set(parents)


def _call_line(view: _GraphView, caller: int, sink: int) -> Optional[int]:
    path = view.files[caller]
    start, end = view.starts[caller], view.ends[caller]
    bare = view.bare(sink)
    lines = sorted(
        line
        for file, line, name in view.anchors
        if file == path
        and name == bare
        and start is not None
        and end is not None
        and start <= line <= end
    )
    return lines[0] + 1 if lines else view.hop(caller).line


def _node_changed(view: _GraphView, node: int, changed: Mapping[str, Set[int]]) -> bool:
    path, start, end = view.files[node], view.starts[node], view.ends[node]
    if path is None or path not in changed:
        return False
    if start is None or end is None:
        return view.types[node] == NODE_TYPE_FILE and bool(changed[path])
    return any(start + 1 <= line <= end + 1 for line in changed[path])


def _glob(pattern: str) -> "re.Pattern[str]":
    """``tests`` or ``tests/`` matches the subtree; ``*`` stays in one segment."""

    pattern = pattern.strip()
    if pattern.startswith("./"):
        pattern = pattern[2:]
    if not any(char in pattern for char in "*?["):
        prefix = re.escape(pattern.rstrip("/"))
        return re.compile(f"{prefix}(?:/.*)?")
    out, index = [], 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(out))


# ---------------------------------------------------------------------------
# Diffs
# ---------------------------------------------------------------------------

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_changed_lines(diff: str) -> Dict[str, Set[int]]:
    """New-side changed lines of a ``git diff --unified=0`` by file.

    A pure deletion marks the line after it, so the function that lost code
    still counts as changed. Deleted files are skipped.
    """

    changed: Dict[str, Set[int]] = {}
    current: Optional[str] = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:]
            current = target[2:] if target.startswith("b/") else None
            if current is not None:
                changed.setdefault(current, set())
            continue
        match = _HUNK.match(line)
        if match and current is not None:
            start, count = int(match.group(1)), int(match.group(2) or 1)
            if count == 0:
                changed[current].update({start, start + 1})
            else:
                changed[current].update(range(start, start + count))
    return changed
