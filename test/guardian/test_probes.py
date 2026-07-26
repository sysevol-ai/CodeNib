# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Tests for typed Guardian probe results and report parsing."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from codeminer.guardian.investigator.environment import TestRecipe as Recipe
from codeminer.guardian.investigator.probes import (
    behavioural_tests,
    run_existing_test,
    run_synthesized_test,
    synthesize_test,
)
from codeminer.guardian.investigator.report_parser import parse_pytest_junit
from codeminer.guardian.investigator.sandbox import WorktreeSandbox
from codeminer.guardian.investigator.types import (
    CommandResult,
    ProcessStatus,
    TestStatus as Status,
)


def _junit(path: Path, body: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'<?xml version="1.0"?><testsuites><testsuite>{body}</testsuite></testsuites>',
        encoding="utf-8",
    )


class ReportSandbox:
    def __init__(self, root: Path, body: str, *, exit_code: int = 0) -> None:
        self.repo_path = str(root)
        self.body = body
        self.exit_code = exit_code
        self.writes = {}

    def run_command(self, command, *, timeout=60, env=None):
        report_arg = next(
            (item for item in command if item.startswith("--junitxml=")), None
        )
        if report_arg:
            report = report_arg.split("=", 1)[1]
            _junit(Path(self.repo_path) / report, self.body)
        return CommandResult(
            status=ProcessStatus.EXITED,
            exit_code=self.exit_code,
            signal=None,
            command=list(command),
            cwd=self.repo_path,
            commit="abc",
        )

    def write_file(self, path, content):
        self.writes[path] = content

    def read_file(self, path):
        return self.writes.get(path, "")


RECIPE = Recipe(("python", "-m", "pytest"), "test")


@pytest.mark.parametrize(
    ("element", "expected"),
    [
        ("", Status.PASSED),
        ("<failure>assert 1 == 2</failure>", Status.FAILED),
        ("<error>ImportError</error>", Status.ERROR),
        ("<skipped />", Status.SKIPPED),
    ],
)
def test_junit_parser_is_the_test_authority(tmp_path, element, expected):
    report = tmp_path / "report.xml"
    _junit(
        report,
        f'<testcase classname="test_mod" name="test_contract">{element}</testcase>',
    )
    parsed = parse_pytest_junit(report)
    assert parsed.parseable is True
    assert parsed.tests == {"test_mod::test_contract": expected}


def test_missing_report_has_no_behavioural_status(tmp_path):
    parsed = parse_pytest_junit(tmp_path / "missing.xml")
    assert parsed.parseable is False
    assert parsed.tests == {}


def test_empty_successful_report_is_not_behavioural_evidence(tmp_path):
    sandbox = ReportSandbox(tmp_path, "", exit_code=5)
    result = run_existing_test("test/missing.py", sandbox, RECIPE)
    assert result.is_error is False
    assert behavioural_tests(result) == {}


def test_error_only_report_is_not_behavioural_evidence(tmp_path):
    body = '<testcase classname="test_bad" name="test_import"><error /></testcase>'
    sandbox = ReportSandbox(tmp_path, body, exit_code=2)
    result = run_existing_test("test/bad.py", sandbox, RECIPE)
    assert result.is_error is False
    assert behavioural_tests(result) == {}
    assert result.structured_content.tests == {
        "test_bad::test_import": Status.ERROR
    }


def test_generic_exit_one_without_report_is_not_a_failed_test(tmp_path):
    sandbox = ReportSandbox(tmp_path, "", exit_code=1)
    # Do not create a testcase: a process code alone cannot manufacture FAILED.
    result = run_existing_test("test/foo.py", sandbox, RECIPE)
    assert behavioural_tests(result) == {}


def test_synthesized_source_is_validated_before_execution(tmp_path):
    accepted = synthesize_test("pkg.mod.target", "def test_contract():\n    assert True")
    rejected = synthesize_test("pkg.mod.target", "def broken(:")
    assert accepted.is_error is False
    assert accepted.structured_content["source"].startswith("def test_contract")
    assert rejected.is_error is True


def test_synthesized_run_writes_source_and_parses_failure(tmp_path):
    body = (
        '<testcase classname="_guardian_synth_test" name="test_contract">'
        "<failure>contract</failure></testcase>"
    )
    sandbox = ReportSandbox(tmp_path, body, exit_code=1)
    result = run_synthesized_test(
        "def test_contract(): assert False", sandbox, RECIPE
    )
    assert sandbox.writes["_guardian_synth_test.py"].startswith("def test_contract")
    assert list(behavioural_tests(result).values()) == [Status.FAILED]


def test_worktree_sandbox_distinguishes_spawn_failure(tmp_path):
    result = WorktreeSandbox(str(tmp_path)).run_command(
        ["definitely-not-a-real-guardian-command"]
    )
    assert result.status is ProcessStatus.SPAWN_FAILED
    assert result.tests == {}


def test_worktree_sandbox_distinguishes_timeout(tmp_path):
    result = WorktreeSandbox(str(tmp_path)).run_command(
        [sys.executable, "-c", "while True: pass"], timeout=1
    )
    assert result.status is ProcessStatus.TIMED_OUT
    assert result.tests == {}


def test_probe_subprocess_has_no_network_or_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("GUARDIAN_TEST_SECRET", "must-not-leak")
    source = (
        "import os, socket\n"
        "print(os.environ.get('GUARDIAN_TEST_SECRET', 'scrubbed'))\n"
        "try:\n"
        "    socket.socket()\n"
        "except OSError:\n"
        "    print('network-blocked')\n"
    )
    result = WorktreeSandbox(str(tmp_path)).run_command(
        [sys.executable, "-c", source]
    )
    assert result.status is ProcessStatus.EXITED
    assert "scrubbed" in result.stdout
    assert "network-blocked" in result.stdout
    assert "must-not-leak" not in result.stdout


def test_sandbox_rejects_path_escape(tmp_path):
    with pytest.raises(ValueError, match="escapes"):
        WorktreeSandbox(str(tmp_path)).write_file("../outside.py", "bad")
