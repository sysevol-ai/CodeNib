# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Deterministic "why" evidence for a Wiki page.

A symbol body says *what* the code does. The reasons live elsewhere in the
repository: in the names of the tests that pin its behaviour and in the
subjects of the commits that last touched it. Both are cheap to collect
without a model, and both are ordinary evidence items -- a claim that cites
them is admitted or rejected by the same grounding rules as any other claim.

Everything here is bounded: a fixed number of files, a wall-clock budget, and
short outputs. A repository without tests or without history simply yields
nothing, and the page is generated as before.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Iterable, Sequence

from ..utils import is_test_file

_TEST_DIR_NAMES = frozenset({"test", "tests", "testing", "__tests__", "spec", "specs"})
_SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        "node_modules",
        "target",
        "build",
        "dist",
        "vendor",
        "third_party",
        "__pycache__",
        ".venv",
        "venv",
    }
)
_TEST_FILE_RE = re.compile(
    r"(?:^|[/_.-])(?:test|tests|spec)(?:[/_.-]|$)|_test\.(?:go|rs|c|cc|cpp)$"
    r"|\.(?:test|spec)\.(?:[cm]?[jt]sx?)$",
    re.IGNORECASE,
)
_MAX_TEST_FILE_BYTES = 400_000
_MAX_TEST_FILES = 400
_TEST_TIME_BUDGET_SECONDS = 2.5
_MAX_TEST_NAMES_PER_SYMBOL = 4
_MAX_TEST_ITEMS = 3
_MAX_HISTORY_ITEMS = 4
_MAX_COMMITS_PER_ITEM = 3
_GIT_TIMEOUT_SECONDS = 4.0

# One pattern per language family; each captures the test's own name.
_TEST_DEF_RE = re.compile(
    r"^[ \t]*(?:"
    r"(?:async\s+)?def\s+(?P<py>test\w*)\s*\("  # pytest / unittest
    r"|func\s+(?P<go>Test\w+)\s*\("  # go testing
    r"|fn\s+(?P<rs>\w*test\w*)\s*\("  # rust #[test] fn
    r"|(?:it|test)\s*\(\s*['\"`](?P<js>[^'\"`\n]{3,120})['\"`]"  # jest / mocha
    r"|TEST(?:_F|_P)?\s*\(\s*\w+\s*,\s*(?P<gtest>\w+)\s*\)"  # googletest
    r")",
    re.MULTILINE,
)


@dataclass(frozen=True, slots=True)
class TestReference:
    file: str
    line: int
    name: str
    symbol: str


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    sha: str
    subject: str
    line_count: int


def _leaf(symbol: str) -> str:
    name = (symbol or "").rsplit(":", 1)[-1].strip()
    name = re.sub(r"\([^)]*\)$", "", name)
    return re.split(r"::|\.", name)[-1].strip()


def _humanize(name: str) -> str:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    text = text.replace("_", " ").strip()
    return re.sub(r"\s+", " ", text)


def _iter_test_files(repo_dir: str) -> Iterable[str]:
    """Yield repository-relative test file paths, cheapest directories first."""

    root = os.path.realpath(repo_dir)
    queued: list[tuple[int, str]] = []
    for current, dirs, files in os.walk(root):
        rel_dir = os.path.relpath(current, root)
        parts = [] if rel_dir == "." else rel_dir.replace("\\", "/").split("/")
        dirs[:] = [
            name
            for name in dirs
            if name not in _SKIP_DIR_NAMES and not name.startswith(".")
        ]
        in_test_dir = any(part.lower() in _TEST_DIR_NAMES for part in parts)
        for name in files:
            rel = "/".join([*parts, name])
            if in_test_dir or _TEST_FILE_RE.search(name) or is_test_file(rel):
                queued.append((0 if in_test_dir else 1, rel))
        if len(queued) >= _MAX_TEST_FILES * 4:
            break
    queued.sort()
    for _rank, rel in queued[:_MAX_TEST_FILES]:
        yield rel


def find_test_references(
    repo_dir: str | None,
    symbols: Sequence[str],
    *,
    time_budget: float = _TEST_TIME_BUDGET_SECONDS,
) -> list[TestReference]:
    """Return tests whose body mentions one of ``symbols`` (by leaf name)."""

    if not repo_dir or not os.path.isdir(repo_dir):
        return []
    leaves: dict[str, str] = {}
    for symbol in symbols:
        leaf = _leaf(symbol)
        if len(leaf) >= 4 and leaf not in leaves:
            leaves[leaf] = symbol
    if not leaves:
        return []
    leaf_re = re.compile(
        r"(?<![\w])(" + "|".join(re.escape(leaf) for leaf in leaves) + r")(?![\w])"
    )
    started = time.monotonic()
    found: list[TestReference] = []
    per_symbol: dict[str, int] = {}
    root = os.path.realpath(repo_dir)
    for rel in _iter_test_files(repo_dir):
        if time.monotonic() - started > time_budget:
            break
        path = os.path.join(root, rel)
        try:
            if os.path.getsize(path) > _MAX_TEST_FILE_BYTES:
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        if not leaf_re.search(text):
            continue
        matches = list(_TEST_DEF_RE.finditer(text))
        for index, match in enumerate(matches):
            name = next(value for value in match.groups() if value)
            body_end = (
                matches[index + 1].start() if index + 1 < len(matches) else len(text)
            )
            body = text[match.start() : body_end]
            for hit in dict.fromkeys(leaf_re.findall(body)):
                symbol = leaves[hit]
                if per_symbol.get(symbol, 0) >= _MAX_TEST_NAMES_PER_SYMBOL:
                    continue
                per_symbol[symbol] = per_symbol.get(symbol, 0) + 1
                line = text.count("\n", 0, match.start()) + 1
                found.append(
                    TestReference(file=rel, line=line, name=name, symbol=symbol)
                )
    return found


def _git(repo_dir: str, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-c", f"safe.directory={repo_dir}", *args],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def repository_has_history(repo_dir: str | None) -> bool:
    """Whether ``git blame`` can attribute lines to real commits here."""

    if not repo_dir or not os.path.isdir(os.path.join(repo_dir, ".git")):
        return False
    shallow = _git(repo_dir, "rev-parse", "--is-shallow-repository")
    return shallow is not None and shallow.strip() == "false"


def blame_history(
    repo_dir: str,
    file: str,
    start_line: int,
    end_line: int,
) -> list[HistoryEntry]:
    """Return the commits that last changed ``file`` lines, most lines first."""

    if start_line < 1 or end_line < start_line:
        return []
    output = _git(
        repo_dir,
        "blame",
        "--porcelain",
        "-L",
        f"{start_line},{end_line}",
        "--",
        file,
    )
    if not output:
        return []
    counts: dict[str, int] = {}
    subjects: dict[str, str] = {}
    current = ""
    for line in output.splitlines():
        header = re.match(r"^([0-9a-f]{40}) \d+ \d+(?: (\d+))?$", line)
        if header:
            current = header.group(1)
            counts[current] = counts.get(current, 0) + 1
            continue
        if line.startswith("summary ") and current:
            subjects.setdefault(current, line[len("summary ") :].strip())
    # An all-zero sha is the working tree: uncommitted lines have no subject
    # worth citing. (Root commits are "boundary" too, and those are real.)
    entries = [
        HistoryEntry(sha=sha[:10], subject=subjects.get(sha, ""), line_count=count)
        for sha, count in counts.items()
        if subjects.get(sha) and sha.strip("0")
    ]
    entries.sort(key=lambda entry: (-entry.line_count, entry.sha))
    return entries[:_MAX_COMMITS_PER_ITEM]


def covering_test_blocks(
    references: Sequence[TestReference],
) -> list[dict[str, object]]:
    """Group test references per file into evidence-item payloads."""

    by_file: dict[str, list[TestReference]] = {}
    for reference in references:
        by_file.setdefault(reference.file, []).append(reference)
    blocks = []
    for file, refs in sorted(by_file.items(), key=lambda item: -len(item[1])):
        refs = sorted(refs, key=lambda ref: ref.line)
        lines = [f"Tests in {file} that exercise the cited code:"]
        for ref in refs:
            lines.append(
                f"- {ref.name} (line {ref.line}) covers `{_leaf(ref.symbol)}`: "
                f"{_humanize(ref.name)}"
            )
        blocks.append(
            {
                "file": file,
                "start_line": refs[0].line,
                "end_line": refs[-1].line,
                "symbol": f"{file}:tests",
                "kind": "test",
                "content": "\n".join(lines),
                "test_names": [ref.name for ref in refs],
            }
        )
        if len(blocks) >= _MAX_TEST_ITEMS:
            break
    return blocks


def history_evidence_blocks(
    repo_dir: str | None,
    spans: Sequence[tuple[str, int | None, int | None, str]],
) -> list[dict[str, object]]:
    """Blame each ``(file, start, end, symbol)`` span into a history payload."""

    if not repository_has_history(repo_dir):
        return []
    assert repo_dir is not None
    blocks = []
    for file, start, end, symbol in spans:
        if start is None or end is None:
            continue
        entries = blame_history(repo_dir, file, start, end)
        if not entries:
            continue
        lines = [f"Commits that last changed `{_leaf(symbol)}` ({file}:{start}-{end}):"]
        for entry in entries:
            lines.append(f"- {entry.sha}: {entry.subject}")
        blocks.append(
            {
                "file": file,
                "start_line": start,
                "end_line": end,
                "symbol": f"{symbol} history",
                "kind": "history",
                "content": "\n".join(lines),
                "commits": [entry.sha for entry in entries],
            }
        )
        if len(blocks) >= _MAX_HISTORY_ITEMS:
            break
    return blocks


__all__ = [
    "HistoryEntry",
    "TestReference",
    "blame_history",
    "find_test_references",
    "history_evidence_blocks",
    "repository_has_history",
    "covering_test_blocks",
]
