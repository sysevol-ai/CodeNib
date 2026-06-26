# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Observe step: lightweight, deterministic repository-health signals.

Phase 1 of the Repository Guardian RFC. Two signals:

* :func:`churn_hotspots` — the most-changed files over a window (git log).
  The primary, always-on signal: cheap, deterministic, no external deps.
* :func:`run_test_suite` — run the target's pytest and report failures. Gated
  behind an explicit flag in the cycle; the default path never shells into
  pytest.

Git interaction mirrors the subprocess patterns in
``codeminer/index/incremental/git_diff.py`` and
``codeminer/graph/incremental/change_mgr.py``.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional, Set

from ..log_utils import get_logger

logger = get_logger(__name__)

# Code-file extensions worth surfacing as hotspots (mirrors scripts/index_repo.py).
_CODE_EXTENSIONS: Set[str] = {
    ".py",
    ".pyi",
    ".go",
    ".rs",
    ".js",
    ".jsx",
    ".mjs",
    ".ts",
    ".tsx",
    ".mts",
    ".cts",
    ".cpp",
    ".cc",
    ".cxx",
    ".c",
    ".h",
    ".hpp",
    ".hxx",
}


@dataclass
class Hotspot:
    """A file ranked by how often it changed in the churn window."""

    path: str
    commit_count: int


@dataclass
class TestFailure:
    """A single failing test node id (and optional one-line message)."""

    nodeid: str
    message: str = ""


@dataclass
class TestResult:
    """Outcome of an optional in-cycle test run."""

    passed: int = 0
    failed: int = 0
    errored: int = 0
    failures: List[TestFailure] = field(default_factory=list)
    summary: str = ""
    ran: bool = False


def churn_hotspots(
    repo_path: str,
    *,
    since: str = "90 days ago",
    top_n: int = 10,
    extensions: Optional[Set[str]] = None,
) -> List[Hotspot]:
    """Return the most-changed code files since ``since``, highest churn first.

    Churn = number of commits in the window that touched the file. Only files
    that still exist on disk and carry a code extension are returned, so deleted
    or renamed-away paths don't pollute the ranking.

    Args:
        repo_path: Repository root (a git checkout).
        since: A git ``--since`` expression (e.g. ``"90 days ago"``, ``"2026-01-01"``).
        top_n: Maximum number of hotspots to return.
        extensions: Override the set of code extensions to consider.

    Returns:
        Up to ``top_n`` :class:`Hotspot` records sorted by descending commit
        count, then by path for stable ordering. Empty if not a git repo.
    """
    exts = extensions if extensions is not None else _CODE_EXTENSIONS
    try:
        # One file per line, commits separated by blank lines. Each appearance of
        # a path == one commit that touched it, so counting lines counts commits.
        output = subprocess.run(
            ["git", "log", f"--since={since}", "--name-only", "--format="],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        logger.warning("churn_hotspots: git log failed in %s: %s", repo_path, exc)
        return []

    counts: dict[str, int] = {}
    for line in output.splitlines():
        rel = line.strip()
        if not rel:
            continue
        if os.path.splitext(rel)[1].lower() not in exts:
            continue
        if not os.path.isfile(os.path.join(repo_path, rel)):
            continue
        counts[rel] = counts.get(rel, 0) + 1

    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [Hotspot(path=path, commit_count=count) for path, count in ranked[:top_n]]


# pytest's terminal summary line, e.g. "3 failed, 412 passed in 12.34s".
_SUMMARY_RE = re.compile(
    r"(?:(\d+) failed)?.*?(?:(\d+) passed)?.*?(?:(\d+) errors?)?", re.IGNORECASE
)
# A failing test node id line, e.g. "FAILED test/foo.py::test_bar - AssertionError: ...".
_FAILED_LINE_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)(?:\s+-\s+(.*))?$")


def run_test_suite(
    repo_path: str,
    *,
    marker: str = "not slow",
    timeout: int = 1800,
) -> TestResult:
    """Run ``pytest`` in ``repo_path`` and summarize pass/fail counts.

    Isolated so the default cycle never invokes it; only called when the cycle
    is configured with ``run_tests=True``. Failures are parsed from pytest's
    short-test-summary lines (``-q -ra``), which is robust across plugins.

    Args:
        repo_path: Repository root to test.
        marker: ``-m`` marker expression (default skips the ``slow`` tier).
        timeout: Hard wall-clock cap in seconds.

    Returns:
        A :class:`TestResult`; ``ran=False`` if pytest could not be launched.
    """
    cmd = ["python", "-m", "pytest", "-q", "-ra", "-m", marker]
    try:
        proc = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("run_test_suite: pytest could not run in %s: %s", repo_path, exc)
        return TestResult(ran=False, summary=f"pytest did not run: {exc}")

    out = proc.stdout + "\n" + proc.stderr
    failures: List[TestFailure] = []
    for line in out.splitlines():
        m = _FAILED_LINE_RE.match(line.strip())
        if m:
            failures.append(TestFailure(nodeid=m.group(1), message=(m.group(2) or "")))

    passed = _count_token(out, "passed")
    failed = _count_token(out, "failed")
    errored = _count_token(out, "error")
    summary = _last_summary_line(out)
    return TestResult(
        passed=passed,
        failed=failed,
        errored=errored,
        failures=failures,
        summary=summary,
        ran=True,
    )


def _count_token(text: str, token: str) -> int:
    """Best-effort scrape of ``<n> <token>`` from a pytest summary line."""
    m = re.search(rf"(\d+)\s+{token}", text)
    return int(m.group(1)) if m else 0


def _last_summary_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if "passed" in line or "failed" in line or "error" in line:
            return line.strip().strip("=").strip()
    return ""
