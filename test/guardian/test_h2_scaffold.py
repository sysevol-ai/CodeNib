# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for scripts/guardian_h2_pilot.py."""

import io
import os
import sys

import pytest

# Add scripts/ to path so guardian_h2_pilot can be imported directly.
_scripts_dir = os.path.join(os.path.dirname(__file__), "..", "..", "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from guardian_h2_pilot import (
    ArmResult,
    MockSolver,
    Solver,
    Task,
    _finding_target,
    compare_arms,
    inject_findings,
    run_arm,
)
from codeminer.guardian.report import Finding


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(task_id="T1", repo_path="/repo", base="abc", ref="def"):
    return Task(
        task_id=task_id,
        repo_path=repo_path,
        base_commit=base,
        reference_commit=ref,
    )


def _churn_finding(path, hypothesis="risky change", verdict="confirmed"):
    return Finding(
        kind="churn",
        title=f"High-churn file: {path}",
        detail=f"commit_count=5",
        hypothesis=hypothesis,
        verdict=verdict,
    )


def _drift_finding(path, symbol="Foo"):
    from codeminer.guardian.investigate import Evidence
    return Finding(
        kind="drift",
        title=f"Fan-in spike: {symbol}",
        evidence=[Evidence(file=path, node_name=symbol, type="function",
                           start_line=None, end_line=None, score=0.9)],
    )


def _arm_result(task_id, arm, passed, seed=0):
    return ArmResult(task_id=task_id, arm=arm, passed=passed, steps=1, tokens_used=0, seed=seed)


# ---------------------------------------------------------------------------
# _finding_target
# ---------------------------------------------------------------------------


class TestFindingTarget:
    def test_churn_finding(self):
        f = _churn_finding("src/foo.py")
        assert _finding_target(f) == "src/foo.py"

    def test_drift_finding_uses_evidence(self):
        f = _drift_finding("src/bar.py")
        assert _finding_target(f) == "src/bar.py"

    def test_finding_with_no_evidence_and_wrong_kind(self):
        f = Finding(kind="test_failure", title="Failing test: test_foo.py::test_x")
        assert _finding_target(f) is None


# ---------------------------------------------------------------------------
# TestInjectFindings
# ---------------------------------------------------------------------------


class TestInjectFindings:
    def test_relevant_finding_appears(self):
        task = _make_task()
        findings = [_churn_finding("src/foo.py"), _churn_finding("src/bar.py")]
        result = inject_findings(findings, task, touched_files=["src/foo.py"])
        assert "src/foo.py" in result
        assert "src/bar.py" not in result

    def test_irrelevant_findings_omitted(self):
        task = _make_task()
        findings = [_churn_finding("unrelated.py")]
        result = inject_findings(findings, task, touched_files=["src/foo.py"])
        assert result == ""

    def test_empty_findings_returns_empty(self):
        task = _make_task()
        result = inject_findings([], task, touched_files=["src/foo.py"])
        assert result == ""

    def test_empty_touched_files_returns_empty(self):
        task = _make_task()
        findings = [_churn_finding("src/foo.py")]
        result = inject_findings(findings, task, touched_files=[])
        assert result == ""

    def test_result_contains_hypothesis_and_verdict(self):
        task = _make_task()
        findings = [_churn_finding("src/foo.py", hypothesis="API contract changed", verdict="confirmed")]
        result = inject_findings(findings, task, touched_files=["src/foo.py"])
        assert "API contract changed" in result
        assert "confirmed" in result

    def test_drift_finding_relevant_via_evidence(self):
        task = _make_task()
        findings = [_drift_finding("lib/utils.py")]
        result = inject_findings(findings, task, touched_files=["lib/utils.py"])
        assert "lib/utils.py" in result

    def test_result_has_guardian_header(self):
        task = _make_task()
        findings = [_churn_finding("src/foo.py")]
        result = inject_findings(findings, task, touched_files=["src/foo.py"])
        assert result.startswith("Guardian findings relevant to this task:")

    def test_multiple_relevant_findings_all_appear(self):
        task = _make_task()
        findings = [_churn_finding("a.py"), _churn_finding("b.py"), _churn_finding("c.py")]
        result = inject_findings(findings, task, touched_files=["a.py", "b.py", "c.py"])
        assert "a.py" in result and "b.py" in result and "c.py" in result


# ---------------------------------------------------------------------------
# TestInjectFindingsTopK
# ---------------------------------------------------------------------------


class TestInjectFindingsTopK:
    def test_top_k_limits_included_findings(self):
        task = _make_task()
        findings = [_churn_finding(f"file_{i}.py") for i in range(10)]
        touched = [f"file_{i}.py" for i in range(10)]
        result = inject_findings(findings, task, top_k=3, touched_files=touched)
        # Count bullet points (one per finding).
        bullet_count = result.count("\n-")
        assert bullet_count == 3

    def test_top_k_1_returns_single_finding(self):
        task = _make_task()
        findings = [_churn_finding("a.py"), _churn_finding("b.py")]
        result = inject_findings(findings, task, top_k=1, touched_files=["a.py", "b.py"])
        assert result.count("\n-") == 1

    def test_top_k_larger_than_relevant_returns_all_relevant(self):
        task = _make_task()
        findings = [_churn_finding("a.py")]
        result = inject_findings(findings, task, top_k=10, touched_files=["a.py"])
        assert result.count("\n-") == 1


# ---------------------------------------------------------------------------
# TestMockSolver / Solver protocol
# ---------------------------------------------------------------------------


class TestMockSolver:
    def test_returns_arm_result(self):
        task = _make_task()
        solver = MockSolver()
        result = solver.solve(task, seed=0)
        assert isinstance(result, ArmResult)
        assert result.task_id == "T1"
        assert result.passed is False
        assert result.seed == 0

    def test_mock_solver_implements_protocol(self):
        assert isinstance(MockSolver(), Solver)

    def test_context_accepted_but_ignored(self):
        task = _make_task()
        solver = MockSolver()
        r1 = solver.solve(task, context="", seed=0)
        r2 = solver.solve(task, context="Guardian says foo.py is risky", seed=0)
        assert r1.passed == r2.passed  # mock ignores context


# ---------------------------------------------------------------------------
# TestRunArmA
# ---------------------------------------------------------------------------


class TestRunArmA:
    def test_arm_a_calls_solver_with_empty_context(self):
        from unittest.mock import MagicMock, call

        task = _make_task()
        mock_solver = MagicMock(spec=Solver)
        mock_solver.solve.return_value = _arm_result("T1", "A", False, seed=0)

        run_arm("A", [task], mock_solver, {}, seeds=[0])

        mock_solver.solve.assert_called_once()
        _, kwargs = mock_solver.solve.call_args
        assert kwargs.get("seed") == 0
        # context should be empty string for arm A
        call_args = mock_solver.solve.call_args[0]
        assert call_args[1] == "" if len(call_args) > 1 else True

    def test_arm_a_produces_one_result_per_seed(self):
        task = _make_task()
        results = run_arm("A", [task], MockSolver(), {}, seeds=[0, 1, 2])
        assert len(results) == 3

    def test_arm_a_results_have_correct_arm_label(self):
        task = _make_task()
        results = run_arm("A", [task], MockSolver(), {}, seeds=[0])
        assert results[0].arm == "A"

    def test_arm_a_multiple_tasks_multiple_seeds(self):
        tasks = [_make_task(f"T{i}") for i in range(3)]
        results = run_arm("A", tasks, MockSolver(), {}, seeds=[0, 1])
        assert len(results) == 6  # 3 tasks × 2 seeds


# ---------------------------------------------------------------------------
# TestRunArmB
# ---------------------------------------------------------------------------


class TestRunArmB:
    def test_arm_b_injects_context_when_findings_relevant(self):
        from unittest.mock import patch

        task = _make_task(task_id="T1")
        findings = [_churn_finding("src/foo.py", hypothesis="arity changed")]
        findings_by_task = {"T1": findings}

        captured_contexts = []

        class CapturingSolver:
            def solve(self, t, context="", *, seed=0):
                captured_contexts.append(context)
                return _arm_result(t.task_id, "B", False, seed=seed)

        # Patch _touched_files_at so we get a real context without a real git repo.
        with patch("guardian_h2_pilot._touched_files_at", return_value=["src/foo.py"]):
            run_arm("B", [task], CapturingSolver(), findings_by_task, seeds=[0], top_k=5)

        assert len(captured_contexts) == 1
        assert "src/foo.py" in captured_contexts[0]

    def test_arm_b_with_empty_findings_gives_empty_context(self):
        captured = []

        class Cap:
            def solve(self, t, context="", *, seed=0):
                captured.append(context)
                return _arm_result(t.task_id, "B", False)

        task = _make_task()
        run_arm("B", [task], Cap(), {"T1": []}, seeds=[0])
        assert captured[0] == ""

    def test_arm_b_results_labeled_b(self):
        task = _make_task()
        results = run_arm("B", [task], MockSolver(), {}, seeds=[0])
        assert results[0].arm == "B"


# ---------------------------------------------------------------------------
# TestRunArmC
# ---------------------------------------------------------------------------


class TestRunArmC:
    def test_arm_c_labeled_c(self):
        task = _make_task()
        results = run_arm("C", [task], MockSolver(), {}, seeds=[0])
        assert results[0].arm == "C"

    def test_arm_c_passes_findings_to_inject(self):
        injected = {}

        from unittest.mock import patch

        task = _make_task(task_id="T1")
        findings = [_churn_finding("x.py")]
        findings_by_task = {"T1": findings}

        with patch("guardian_h2_pilot.inject_findings", return_value="injected") as mock_inject:
            run_arm("C", [task], MockSolver(), findings_by_task, seeds=[0])
            mock_inject.assert_called_once()
            call_findings = mock_inject.call_args[0][0]
            assert call_findings == findings

    def test_arm_c_no_findings_gives_empty_context(self):
        captured = []

        class Cap:
            def solve(self, t, context="", *, seed=0):
                captured.append(context)
                return _arm_result(t.task_id, "C", False)

        task = _make_task()
        run_arm("C", [task], Cap(), {}, seeds=[0])
        assert captured[0] == ""


# ---------------------------------------------------------------------------
# TestCompareArms
# ---------------------------------------------------------------------------


class TestCompareArms:
    def _make_arm(self, task_ids, arm, passed_map, seed=0):
        """Build ArmResult list from {task_id: passed} dict."""
        return [
            _arm_result(tid, arm, passed_map.get(tid, False), seed=seed)
            for tid in task_ids
        ]

    def test_c_better_than_a_positive_delta(self, capsys):
        task_ids = ["T1", "T2", "T3"]
        a = self._make_arm(task_ids, "A", {"T1": False, "T2": False, "T3": False})
        b = self._make_arm(task_ids, "B", {"T1": False, "T2": False, "T3": False})
        c = self._make_arm(task_ids, "C", {"T1": True, "T2": False, "T3": True})
        compare_arms(a, b, c)
        out = capsys.readouterr().out
        assert "C − A" in out
        assert "+0.667" in out

    def test_no_difference_shows_zero_delta(self, capsys):
        task_ids = ["T1"]
        arm = self._make_arm(task_ids, "A", {"T1": False})
        compare_arms(arm, arm, arm)
        out = capsys.readouterr().out
        assert "+0.000" in out or "-0.000" in out or "0.000" in out

    def test_output_contains_all_comparison_labels(self, capsys):
        a = [_arm_result("T1", "A", False)]
        b = [_arm_result("T1", "B", False)]
        c = [_arm_result("T1", "C", False)]
        compare_arms(a, b, c)
        out = capsys.readouterr().out
        assert "C − A" in out
        assert "C − B" in out
        assert "B − A" in out

    def test_empty_results_shows_zero_pairs(self, capsys):
        compare_arms([], [], [])
        out = capsys.readouterr().out
        assert "n=0" in out

    def test_multiple_seeds_all_counted(self, capsys):
        a = [_arm_result("T1", "A", False, seed=s) for s in range(3)]
        b = [_arm_result("T1", "B", False, seed=s) for s in range(3)]
        c = [_arm_result("T1", "C", True, seed=s) for s in range(3)]
        compare_arms(a, b, c)
        out = capsys.readouterr().out
        assert "n=3" in out


# ---------------------------------------------------------------------------
# TestFlipCounting
# ---------------------------------------------------------------------------


class TestFlipCounting:
    def test_fail_to_pass_counted(self, capsys):
        a = [_arm_result("T1", "A", False), _arm_result("T2", "A", False)]
        b = [_arm_result("T1", "B", False), _arm_result("T2", "B", False)]
        c = [_arm_result("T1", "C", True), _arm_result("T2", "C", False)]
        compare_arms(a, b, c)
        out = capsys.readouterr().out
        assert "Fail→Pass (C helped):   1" in out

    def test_pass_to_fail_counted(self, capsys):
        a = [_arm_result("T1", "A", True), _arm_result("T2", "A", False)]
        b = [_arm_result("T1", "B", False), _arm_result("T2", "B", False)]
        c = [_arm_result("T1", "C", False), _arm_result("T2", "C", False)]
        compare_arms(a, b, c)
        out = capsys.readouterr().out
        assert "Pass→Fail (C harmed):   1" in out

    def test_no_flips_shows_zero(self, capsys):
        a = [_arm_result("T1", "A", False)]
        b = [_arm_result("T1", "B", False)]
        c = [_arm_result("T1", "C", False)]
        compare_arms(a, b, c)
        out = capsys.readouterr().out
        assert "Fail→Pass (C helped):   0" in out
        assert "Pass→Fail (C harmed):   0" in out

    def test_both_flip_directions_tracked(self, capsys):
        # T1: A fails, C passes → Fail→Pass
        # T2: A passes, C fails → Pass→Fail
        a = [_arm_result("T1", "A", False), _arm_result("T2", "A", True)]
        b = [_arm_result("T1", "B", False), _arm_result("T2", "B", False)]
        c = [_arm_result("T1", "C", True), _arm_result("T2", "C", False)]
        compare_arms(a, b, c)
        out = capsys.readouterr().out
        assert "Fail→Pass (C helped):   1" in out
        assert "Pass→Fail (C harmed):   1" in out

    def test_delta_arithmetic(self, capsys):
        # 4 pairs: C wins 3, A wins 1 → C-A = (3-1)/4 = 0.5
        pairs = [("T1", True, False), ("T2", True, False), ("T3", True, False), ("T4", False, True)]
        a = [_arm_result(tid, "A", a_pass) for tid, _, a_pass in pairs]
        c = [_arm_result(tid, "C", c_pass) for tid, c_pass, _ in pairs]
        b = [_arm_result(tid, "B", False) for tid, _, _ in pairs]
        compare_arms(a, b, c)
        out = capsys.readouterr().out
        assert "+0.500" in out
