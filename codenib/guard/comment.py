# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""A guard report as one pull-request comment, written for the reviewer.

A reviewer reads a diff and cannot see what the diff leaves out: the callers
that were not updated, and the tests that exercise the changed code. The
comment states both from the base commit's code graph, links each to its line,
and keeps the tests the change itself touches apart from the tests that reach
the changed source.
"""

from __future__ import annotations

import shlex
from typing import Dict, List, Optional
from urllib.parse import quote

from ..utils import is_test_file
from .impact import ChangedSymbol, Hop
from .languages import convention_for
from .report import NOT_SEEN, GuardReport

MARKER = "<!-- codenib-impact -->"
SHOWN_CHANGES = 12
SHOWN_CALLERS = 8  # per changed definition
SHOWN_TESTS = 12
# GitHub rejects a comment body over 65,536 characters.
MAX_CHARACTERS = 60000


def _code(text: str) -> str:
    """``text`` as inline code that cannot break a table or open markup."""

    return "`" + text.replace("`", "'").replace("|", "\\|") + "`"


def _plain(text: str) -> str:
    """Free text, such as a note title, with markup and mentions disarmed."""

    escaped = " ".join(text.split())
    for character in "\\`*_<>[]|":
        escaped = escaped.replace(character, "\\" + character)
    return escaped.replace("@", "@​")


def _name(symbol: str) -> str:
    """``pkg/mod.py:Class.method`` -> ``Class.method``; a file stays a path."""

    path, _, qualified = symbol.partition(":")
    return qualified or path


def _count(number: int, noun: str) -> str:
    return f"{number:,} {noun}{'' if number == 1 else 's'}"


class _Links:
    def __init__(self, source_url: str, commit: str):
        self.base = f"{source_url.rstrip('/')}/blob/{commit}/" if source_url else ""

    def to(self, label: str, file: Optional[str], line: Optional[int]) -> str:
        if not self.base or not file:
            return label
        anchor = f"#L{line}" if line else ""
        return f"[{label}]({self.base}{quote(file)}{anchor})"


def _source_changes(report: GuardReport) -> List[ChangedSymbol]:
    """Edited definitions outside test files."""

    return [
        c
        for c in report.impact.changed
        if c.unit != "file" and not is_test_file(c.file)
    ]


def _module_level(report: GuardReport) -> List[str]:
    """Source files with an edit outside every definition, such as an import."""

    return sorted(
        c.file
        for c in report.impact.changed
        if c.unit == "file" and not is_test_file(c.file)
    )


def _outside_callers(report: GuardReport) -> Dict[str, List[Hop]]:
    """Per changed source definition: its callers that this change left alone.

    Callers in test files are left to the test counts.
    """

    changed = {c.symbol for c in report.impact.changed}
    return {
        change.symbol: [
            hop
            for hop in report.impact.callers.get(change.symbol, ())
            # A file node is an import of the name, not a call of it.
            if ":" in hop.symbol
            and hop.symbol not in changed
            and not is_test_file(hop.file or hop.symbol)
        ]
        for change in _source_changes(report)
    }


def _summary(report: GuardReport, callers: Dict[str, List[Hop]]) -> str:
    impact = report.impact
    source = _source_changes(report)
    files = sorted({c.file for c in source})
    where = _code(files[0]) if len(files) == 1 else _count(len(files), "file")
    outside = len({hop.symbol for hops in callers.values() for hop in hops})
    reach = len(impact.source_tests)
    sentence = (
        f"This pull request changes **{_count(len(source), 'definition')}** in "
        f"{where}. "
    )
    sentence += (
        f"**{_count(outside, 'caller')}** outside the change "
        f"{'calls' if outside == 1 else 'call'} that code"
        if outside
        else "Nothing outside the change calls that code"
    )
    sentence += (
        f", and **{reach:,} of {_count(impact.total_tests, 'test')}** "
        f"{'reaches' if reach == 1 else 'reach'} it within "
        f"{_count(impact.depth, 'call')}."
        if reach
        else ", and the graph finds no test within "
        f"{_count(impact.depth, 'call')} of it."
    )
    return sentence


def _table(report: GuardReport, callers: Dict[str, List[Hop]], links: _Links) -> str:
    source = _source_changes(report)
    rows = [
        "| Changed | Callers outside the change | Tests that reach it |",
        "|---|---|---|",
    ]
    for change in source[:SHOWN_CHANGES]:
        label = links.to(_code(_name(change.symbol)), change.file, change.line)
        tests = report.impact.tests_reaching.get(change.symbol, 0)
        rows.append(
            f"| {label} | {len(callers.get(change.symbol, ())):,} | "
            f"{f'{tests:,}' if tests else 'none found'} |"
        )
    if len(source) > SHOWN_CHANGES:
        rows.append(f"| and {len(source) - SHOWN_CHANGES} more | | |")
    return "\n".join(rows)


def _details(summary: str, body: List[str]) -> str:
    return "\n".join(
        ["<details>", f"<summary>{summary}</summary>", "", *body, "", "</details>"]
    )


def _callers_section(callers: Dict[str, List[Hop]], links: _Links) -> str:
    total = len({hop.symbol for hops in callers.values() for hop in hops})
    if not total:
        return ""
    body: List[str] = []
    for symbol, hops in callers.items():
        if not hops:
            continue
        body.append(f"{_code(_name(symbol))} is called from:")
        body.append("")
        for hop in hops[:SHOWN_CALLERS]:
            line = hop.call_line or hop.line
            place = f"{hop.file}:{line}" if hop.file and line else hop.file or ""
            body.append(
                f"- {links.to(_code(_name(hop.symbol)), hop.file, line)}"
                + (f" at {_code(place)}" if place else "")
            )
        if len(hops) > SHOWN_CALLERS:
            body.append(f"- and {len(hops) - SHOWN_CALLERS} more")
        body.append("")
    return _details(
        f"Callers this change does not touch ({total:,})", body[:-1] or body
    )


def _tests_section(report: GuardReport) -> str:
    """Test files that reach the changed source, each with one example path."""

    tests = report.impact.source_tests
    if not tests:
        return ""
    by_file: Dict[str, list] = {}
    for test in tests:
        by_file.setdefault(test.test_id.split("::", 1)[0], []).append(test)
    # Files with the shortest path to the change first, then the most tests.
    files = sorted(
        by_file, key=lambda name: (by_file[name][0].depth, -len(by_file[name]), name)
    )
    body = []
    for name in files[:SHOWN_TESTS]:
        example = by_file[name][0]
        path = " → ".join(_code(_name(hop.symbol)) for hop in example.path)
        body.append(
            f"- {_code(name)}: {_count(len(by_file[name]), 'test')}, such as {path}"
        )
    if len(files) > SHOWN_TESTS:
        body.append(f"- and {_count(len(files) - SHOWN_TESTS, 'more file')}")
    commands = _file_commands(files[:SHOWN_TESTS])
    if commands:
        body += ["", "Run these files:", "", "```bash", *commands, "```"]
    return _details(
        f"Tests that reach the changed code ({len(tests):,} in "
        f"{_count(len(files), 'file')})",
        body,
    )


def _file_commands(files: List[str]) -> List[str]:
    """Commands that run whole test files, one per language that has one."""

    commands = []
    python = [name for name in files if name.endswith((".py", ".pyi"))]
    if python:
        commands.append("python -m pytest " + " ".join(map(shlex.quote, python)))
    go = dict.fromkeys(
        "./" + name.rsplit("/", 1)[0] if "/" in name else "."
        for name in files
        if name.endswith(".go")
    )
    if go:
        commands.append("go test " + " ".join(map(shlex.quote, go)))
    if any(name.endswith(".rs") for name in files):
        commands.append("cargo test")
    return commands


def _edited_section(report: GuardReport) -> str:
    edited = report.impact.edited_tests
    if not edited:
        return ""
    lines = [
        f"**{_count(len(edited), 'existing test')} "
        f"{'was' if len(edited) == 1 else 'were'} rewritten by this change.** "
        "A rewritten test checks the new behaviour, so its passing says little "
        "about the behaviour it used to protect.",
        "",
    ]
    lines += [f"- {_code(test_id)}" for test_id in edited[:SHOWN_TESTS]]
    if len(edited) > SHOWN_TESTS:
        lines.append(f"- and {len(edited) - SHOWN_TESTS:,} more")
    return "\n".join(lines)


def _notes_section(report: GuardReport) -> str:
    lines: List[str] = []
    if report.notes_on_change:
        lines.append("**Recorded notes about this code**")
        lines.append("")
        lines += [
            f"- {_plain(note.title)} ({_code(str(note.path))})"
            for note, _ in report.notes_on_change
        ]
    if report.stale_notes:
        if lines:
            lines.append("")
        lines.append("**Notes that may now be out of date**")
        lines.append("")
        lines += [
            f"- {_plain(note.title)} ({_code(str(note.path))})"
            for note, _ in report.stale_notes
        ]
    return "\n".join(lines)


def _footer(report: GuardReport) -> str:
    impact = report.impact
    parts = [
        f"Read from the code graph of {_code(report.base_commit[:12])}, the "
        "commit this change is compared with. No model wrote this comment."
    ]
    tests = [c for c in impact.changed if is_test_file(c.file)]
    if tests:
        files = len({c.file for c in tests})
        parts.append(
            f"The change also edits {_count(files, 'test file')}; tests it adds "
            "are not in the graph."
        )
    new = [path for path in impact.new_files if convention_for(path) is not None]
    if new:
        parts.append(
            f"{_count(len(new), 'new source file')} "
            f"{'is' if len(new) == 1 else 'are'} not in the graph yet."
        )
    module = _module_level(report)
    if module:
        shown = ", ".join(_code(path) for path in module[:3])
        more = f" and {len(module) - 3} more" if len(module) > 3 else ""
        parts.append(
            f"Edits outside any definition, such as imports, in {shown}{more} "
            "are not traced to callers."
        )
    parts.append(NOT_SEEN)
    return "<sub>" + " ".join(parts) + "</sub>"


def render_comment(report: GuardReport, *, source_url: str = "") -> str:
    """The pull-request comment, or ``""`` when there is nothing to tell.

    ``source_url`` is the repository's web address, such as
    ``https://github.com/owner/name``; with it, each definition links to its
    line at the base commit.
    """

    source = _source_changes(report)
    edited = _edited_section(report)
    notes = _notes_section(report)
    if not (source or edited or notes):
        return ""
    links = _Links(source_url, report.base_commit)
    sections = [MARKER, "### CodeNib impact"]
    if source:
        callers = _outside_callers(report)
        sections += [
            _summary(report, callers),
            _table(report, callers, links),
            _callers_section(callers, links),
            _tests_section(report),
        ]
    else:
        sections.append("This pull request changes no source definitions.")
    sections += [edited, notes]
    body = "\n\n".join(section for section in sections if section)
    footer = _footer(report)
    if len(body) + len(footer) > MAX_CHARACTERS:
        body = body[:MAX_CHARACTERS].rsplit("\n\n", 1)[0] + "\n\nThe rest was cut."
    return body + "\n\n" + footer + "\n"
