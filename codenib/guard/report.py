# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""One guard report: what a change puts at risk, and what is known about it."""

from __future__ import annotations

import hashlib
import shlex
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .impact import DEFAULT_DEPTH, Impact, ImpactGraph, base_changed_lines
from .notes import AnchorStatus, Note, anchor_states, load_notes
from .snapshot import (
    SnapshotError,
    build_snapshot,
    cached_snapshot,
    git,
    resolve_commit,
)

SHOWN_TESTS = 10
COMMAND_TESTS = 20
NOT_SEEN = (
    "The graph does not see calls that frameworks make by name, operators such "
    "as == and [], or code reached only from new files. Tests it does not list "
    "can still fail."
)


@dataclass
class GuardReport:
    repository: str
    base: str
    base_commit: str
    impact: Impact
    notes_on_change: List[Tuple[Note, List[str]]] = field(default_factory=list)
    stale_notes: List[Tuple[Note, List[AnchorStatus]]] = field(default_factory=list)
    note_errors: List[str] = field(default_factory=list)
    diff_sha256: str = ""
    graph_seconds: Optional[float] = None

    @property
    def has_findings(self) -> bool:
        return bool(self.impact.tests or self.notes_on_change or self.stale_notes)

    def pytest_command(self, limit: int = COMMAND_TESTS) -> str:
        ids = [t.test_id for t in self.impact.tests[:limit]]
        return (
            "python -m pytest " + " ".join(shlex.quote(i) for i in ids) if ids else ""
        )

    def to_dict(self) -> Dict[str, object]:
        def note(n: Note) -> Dict[str, object]:
            return {
                "path": str(n.path),
                "kind": n.kind,
                "text": n.text,
                "source": n.source,
            }

        return {
            "repository": self.repository,
            "base": self.base,
            "base_commit": self.base_commit,
            "depth": self.impact.depth,
            "changed": [asdict(c) for c in self.impact.changed],
            "tests_at_risk": [asdict(t) for t in self.impact.tests],
            "total_tests": self.impact.total_tests,
            "direct_callers": self.impact.direct_callers,
            "new_files": self.impact.new_files,
            "unmapped_files": self.impact.unmapped_files,
            "notes_on_change": [
                {**note(n), "anchors": symbols} for n, symbols in self.notes_on_change
            ],
            "stale_notes": [
                {
                    **note(n),
                    "anchors": [
                        {"symbol": s.anchor.symbol, "state": s.state} for s in states
                    ],
                }
                for n, states in self.stale_notes
            ],
            "note_errors": self.note_errors,
            "pytest_command": self.pytest_command(),
            "diff_sha256": self.diff_sha256,
            "limits": NOT_SEEN,
        }


def working_tree_diff(repo: Path, commit: str) -> Tuple[str, List[str]]:
    """``git diff --unified=0`` from ``commit`` to the working tree, and new files."""

    diff = git(
        repo,
        "diff",
        "--unified=0",
        "--no-color",
        "--no-ext-diff",
        "--find-renames",
        commit,
        "--",
    )
    untracked = [
        path
        for path in git(repo, "ls-files", "--others", "--exclude-standard", "-z").split(
            "\0"
        )
        if path
    ]
    return diff, untracked


def change_digest(diff: str, untracked: List[str]) -> str:
    """Identify one working-tree change by its diff and the names of new files."""

    payload = diff + "\0" + "\0".join(sorted(untracked))
    return hashlib.sha256(payload.encode("utf-8", "surrogateescape")).hexdigest()


def working_tree_digest(repo: Path, commit: str) -> str:
    """The ``diff_sha256`` a report on ``commit`` would carry, without a graph.

    Two git calls, so a caller can recognise a change it has already reported
    before waiting for a graph build or running the impact query.
    """

    return change_digest(*working_tree_diff(repo.resolve(), commit))


def run_guard(
    repo: Path,
    base: str = "HEAD",
    *,
    depth: int = DEFAULT_DEPTH,
    build: bool = True,
) -> GuardReport:
    """Report the tests and notes that the working tree's changes since ``base`` touch."""

    repo = repo.resolve()
    commit = resolve_commit(repo, base)
    snapshot = cached_snapshot(repo, commit)
    if snapshot is None:
        if not build:
            raise SnapshotError(f"no graph for {commit[:12]} yet")
        snapshot = build_snapshot(repo, commit)
    diff, untracked = working_tree_diff(repo, commit)
    changed, added = base_changed_lines(diff)
    graph = ImpactGraph(snapshot.load_graph(), snapshot.source)
    impact = graph.impact(changed, depth=depth, new_files=sorted({*added, *untracked}))
    report = GuardReport(
        repository=str(repo),
        base=base,
        base_commit=commit,
        impact=impact,
        diff_sha256=change_digest(diff, untracked),
        graph_seconds=snapshot.seconds,
    )
    notes, report.note_errors = load_notes(repo)
    touched = {c.symbol for c in impact.changed}
    near = touched | {
        caller for callers in impact.direct_callers.values() for caller in callers
    }
    for note in notes:
        matched = [a.symbol for a in note.anchors if a.symbol in near]
        if matched:
            report.notes_on_change.append((note, matched))
        states = [s for s in anchor_states(repo, note) if s.state != "current"]
        if states:
            report.stale_notes.append((note, states))
    return report


def _hop_label(symbol: str) -> str:
    qualified = symbol.split(":", 1)[-1]
    return ".".join(qualified.split(".")[-2:]) if "." in qualified else qualified


def describe_path(test) -> str:
    """``test_x (tests/a.py:12) -> helper (src/b.py:40) -> changed``."""

    parts = []
    for hop in test.path:
        label = _hop_label(hop.symbol)
        if hop.call_line is not None and hop.file:
            label += f" ({hop.file}:{hop.call_line})"
        parts.append(label)
    return " -> ".join(parts)


def render_text(report: GuardReport, *, shown: int = SHOWN_TESTS) -> str:
    impact = report.impact
    lines = [f"CodeNib guard: changes since {report.base_commit[:12]} ({report.base})"]
    if not impact.changed:
        lines.append("No changed Python definitions.")
    else:
        lines.append(f"Changed code ({len(impact.changed)}):")
        lines.extend(f"  {c.symbol}" for c in impact.changed)
    if impact.new_files:
        lines.append(f"New files, not in the graph: {', '.join(impact.new_files[:8])}")
    if impact.changed:
        lines.append("")
        lines.append(
            f"Tests at risk: {len(impact.tests)} of {impact.total_tests:,}, "
            f"within {impact.depth} calls of the change"
        )
        for test in impact.tests[:shown]:
            lines.append(f"  {test.test_id}")
            lines.append(f"      {describe_path(test)}")
        if len(impact.tests) > shown:
            lines.append(
                f"  ... and {len(impact.tests) - shown} more (--json lists all)"
            )
        command = report.pytest_command()
        if command:
            lines.append("Run them:")
            lines.append(f"  {command}")
    if report.notes_on_change:
        lines.append("")
        lines.append(f"Notes on this code ({len(report.notes_on_change)}):")
        for note, _ in report.notes_on_change:
            lines.append(f"  [{note.kind}] {note.title}  ({note.path})")
    if report.stale_notes:
        lines.append("")
        lines.append(f"Notes that may be out of date ({len(report.stale_notes)}):")
        for note, states in report.stale_notes:
            what = ", ".join(f"{_hop_label(s.anchor.symbol)} {s.state}" for s in states)
            lines.append(f"  [{note.kind}] {note.title}  ({note.path}; {what})")
        lines.append("  After checking one, run `codenib guard note confirm <file>`.")
    for error in report.note_errors:
        lines.append(f"Unreadable note: {error}")
    if impact.changed:
        lines.append("")
        lines.append(NOT_SEEN)
    return "\n".join(lines)


def render_for_agent(report: GuardReport, *, shown: int = SHOWN_TESTS) -> str:
    """The report as factual context for a coding agent that is about to stop."""

    impact = report.impact
    lines = [
        "CodeNib guard compared this session's changes with the code graph of "
        f"commit {report.base_commit[:12]}."
    ]
    if impact.tests:
        lines.append(
            f"{len(impact.tests)} tests reach the changed code within {impact.depth} "
            "calls. Each line shows the call path from the test to the change:"
        )
        for test in impact.tests[:shown]:
            lines.append(f"- {test.test_id}: {describe_path(test)}")
        if len(impact.tests) > shown:
            lines.append(f"- and {len(impact.tests) - shown} more")
        lines.append(f"Command that runs them: {report.pytest_command()}")
        lines.append(
            "If the project does not use pytest, the same tests can be run with "
            "its own test runner. A failure that the change caused needs a fix "
            "before the work is done."
        )
    if report.notes_on_change:
        lines.append("Recorded notes about the changed code:")
        for note, _ in report.notes_on_change:
            lines.append(f"- [{note.kind}] {note.text.strip()} ({note.path})")
    if report.stale_notes:
        lines.append(
            "Notes whose code has changed and may now be wrong; update the note "
            "if it no longer holds:"
        )
        for note, states in report.stale_notes:
            what = ", ".join(f"{s.anchor.symbol} {s.state}" for s in states)
            lines.append(f"- [{note.kind}] {note.title} ({note.path}; {what})")
    return "\n".join(lines)
