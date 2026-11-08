# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for how a guard report treats tests that the change itself edited."""

from __future__ import annotations

import os
import subprocess
import sys

from codenib.guard.impact import Hop, Impact
from codenib.guard.impact import TestAtRisk as AtRisk
from codenib.guard.report import GuardReport, render_for_agent

EDITED = "tests/test_shop.py::test_value"
OTHER = "tests/test_other.py::test_total"


def _report(edited, base="b" * 40):
    tests = [
        AtRisk(EDITED, "tests/test_shop.py:test_value", 0, ()),
        AtRisk(
            OTHER,
            "tests/test_other.py:test_total",
            1,
            (Hop("tests/test_other.py:test_total", "tests/test_other.py", 1, 2),),
        ),
    ]
    impact = Impact(tests=tests, edited_tests=[EDITED] if edited else [])
    return GuardReport("repo", "HEAD", base, impact)


def test_agent_message_separates_edited_tests():
    report = _report(edited=True)
    text = render_for_agent(report)
    assert "1 of them existed at commit bbbbbbbbbbbb and were edited" in text
    assert f"- {EDITED}" in text
    assert "The other 1 test, each with the call path" in text
    assert report.original_tests_command() in text
    # The plain command runs only the tests the change did not edit.
    assert report.pytest_command() == f"python -m pytest {OTHER}"


def test_agent_message_is_unchanged_without_edited_tests():
    text = render_for_agent(_report(edited=False))
    assert "2 tests reach the changed code within 3 calls. Each line shows" in text
    assert "edited" not in text
    assert _report(edited=False).original_tests_command() == ""


def test_findings_digest_counts_edited_tests():
    assert _report(edited=True).findings_sha256 != _report(False).findings_sha256


def test_original_tests_command_runs_base_tests_and_restores_edits(git_repo):
    base = git_repo.commit(
        {
            "shop.py": "VALUE = 1\n",
            "tests/test_shop.py": "from shop import VALUE\n\n\n"
            "def test_value():\n    assert VALUE == 1\n",
        },
        "init",
    )
    edited_test = (
        "from shop import VALUE\n\n\ndef test_value():\n    assert VALUE == 2\n"
    )
    (git_repo.root / "shop.py").write_text("VALUE = 2\n")
    (git_repo.root / "tests/test_shop.py").write_text(edited_test)
    command = _report(edited=True, base=base).original_tests_command()

    env = {
        **os.environ,
        "PATH": os.path.dirname(sys.executable) + os.pathsep + os.environ["PATH"],
    }
    done = subprocess.run(
        ["bash", "-c", command],
        cwd=git_repo.root,
        env=env,
        capture_output=True,
        text=True,
    )
    # The original assertion fails against the changed code...
    assert "1 failed" in done.stdout, done.stdout + done.stderr
    # ...and the agent's edit is back afterwards.
    assert (git_repo.root / "tests/test_shop.py").read_text() == edited_test
    assert (git_repo.root / "shop.py").read_text() == "VALUE = 2\n"
