# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Test-suite signal: run pytest and report pass/fail counts.

Moved from codeminer.guardian.signals (flat module) into the signals/
sub-package.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from typing import List

from ...log_utils import get_logger
from .types import Signal

logger = get_logger(__name__)


def TestFailure(nodeid: str, message: str = "") -> Signal:
    """Compatibility constructor returning the canonical Signal type."""
    return Signal.create(
        kind="test_failure",
        locus=[nodeid],
        detail=message or f"{nodeid} failed",
        value={"nodeid": nodeid, "message": message},
    )


@dataclass
class TestResult:
    """Outcome of an optional in-cycle test run."""

    passed: int = 0
    failed: int = 0
    errored: int = 0
    failures: List[Signal] = field(default_factory=list)
    summary: str = ""
    ran: bool = False


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
    failures: List[Signal] = []
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
