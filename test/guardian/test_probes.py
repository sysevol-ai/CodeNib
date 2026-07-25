# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for codeminer.guardian.probes."""

from unittest.mock import MagicMock

import pytest

from codeminer.guardian.investigator.probes import (
    _check_self_mocking,
    _parse_target_symbol,
    corroboration_policy,
    differential_run,
    dispatch_advanced_probe,
    fix_probe,
    retrieve_evidence,
    run_existing_test,
    run_synthesized_test,
    synthesize_test,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_sandbox(repo_path="/tmp/repo", *, run_rc=0, run_output="ok"):
    sb = MagicMock()
    sb.repo_path = repo_path
    sb.run_command.return_value = (run_rc, run_output)
    return sb


def _fake_retriever(result_text="file.py | my_func (function) | score=0.900"):
    """Fake retriever whose query() returns one fake node."""
    node = MagicMock()
    node.file = "file.py"
    node.node_name = "my_func"
    node.type = "function"
    node.start_line = 10
    node.score = 0.9
    node.content = "def my_func(): pass"
    r = MagicMock()
    r.query.return_value = [node]
    return r


# ---------------------------------------------------------------------------
# retrieve_evidence
# ---------------------------------------------------------------------------


class TestRetrieveEvidence:
    def test_calls_retriever_and_returns_text(self):
        obs, result = retrieve_evidence("parse_config callers", _fake_retriever())
        assert "file.py" in obs
        assert result["query"] == "parse_config callers"
        assert result["top_k"] == 5

    def test_custom_top_k(self):
        _, result = retrieve_evidence("q", _fake_retriever(), top_k=3)
        assert result["top_k"] == 3

    def test_empty_retriever_returns_no_results(self):
        r = MagicMock()
        r.query.return_value = []
        obs, _ = retrieve_evidence("q", r)
        assert "no results" in obs.lower()


# ---------------------------------------------------------------------------
# run_existing_test
# ---------------------------------------------------------------------------


class TestRunExistingTest:
    def test_pass(self):
        obs, result = run_existing_test(
            "test/foo.py", _fake_sandbox(run_rc=0, run_output="1 passed")
        )
        assert result["passed"] is True
        assert "PASS" in obs

    def test_fail(self):
        obs, result = run_existing_test(
            "test/foo.py", _fake_sandbox(run_rc=1, run_output="1 failed")
        )
        assert result["passed"] is False
        assert "FAIL" in obs

    def test_collection_error_sets_passed_none(self):
        obs, result = run_existing_test(
            "test/bad.py", _fake_sandbox(run_rc=2, run_output="ERROR collecting")
        )
        assert result["passed"] is None
        assert "INVALID" in obs

    def test_timeout_respected(self):
        sb = _fake_sandbox()
        run_existing_test("test/foo.py", sb, timeout=30)
        _, kwargs = sb.run_command.call_args
        assert kwargs["timeout"] == 30


# ---------------------------------------------------------------------------
# _parse_target_symbol
# ---------------------------------------------------------------------------


class TestParseTargetSymbol:
    def test_dotted_path(self):
        m, s = _parse_target_symbol("codeminer.guardian.cycle.run_cycle")
        assert m == "codeminer.guardian.cycle"
        assert s == "run_cycle"

    def test_single_component(self):
        m, s = _parse_target_symbol("run_cycle")
        assert m == "run_cycle"
        assert s == "run_cycle"


# ---------------------------------------------------------------------------
# synthesize_test
# ---------------------------------------------------------------------------


class TestSynthesizeTest:
    def test_valid_import_returns_scaffold(self):
        sb = _fake_sandbox(run_rc=0, run_output="ok")
        obs, result = synthesize_test(
            "test arity break", "codeminer.guardian.cycle.run_cycle", sb
        )
        assert result["valid"] is True
        assert "def test_run_cycle" in result["scaffold"]
        assert "scaffold" in obs.lower() or "import" in obs.lower()

    def test_invalid_import_returns_error(self):
        sb = _fake_sandbox(
            run_rc=1, run_output="ModuleNotFoundError: no module named 'nonexistent'"
        )
        obs, result = synthesize_test("test something", "nonexistent.BadSymbol", sb)
        assert result["valid"] is False
        assert "Cannot import" in obs

    def test_scaffold_contains_import_line(self):
        sb = _fake_sandbox(run_rc=0, run_output="ok")
        _, result = synthesize_test("test it", "codeminer.guardian.cycle.run_cycle", sb)
        scaffold = result["scaffold"]
        assert "from codeminer.guardian.cycle import run_cycle" in scaffold

    def test_import_command_is_correct(self):
        sb = _fake_sandbox(run_rc=0, run_output="ok")
        synthesize_test("desc", "pkg.mod.Foo", sb)
        call_args = sb.run_command.call_args[0][0]
        assert "from pkg.mod import Foo" in " ".join(call_args)


# ---------------------------------------------------------------------------
# _check_self_mocking
# ---------------------------------------------------------------------------


class TestCheckSelfMocking:
    def test_no_mocking(self):
        src = "def test_x():\n    assert my_func() == 1\n"
        assert _check_self_mocking(src) is False

    def test_patch_and_magicmock(self):
        src = (
            "@patch('mod.my_func')\ndef test_x(m):\n    m.return_value = MagicMock()\n"
        )
        assert _check_self_mocking(src) is True

    def test_patch_without_magicmock(self):
        src = "@patch('mod.helper')\ndef test_x(m):\n    m.return_value = 1\n"
        assert _check_self_mocking(src) is False


# ---------------------------------------------------------------------------
# run_synthesized_test
# ---------------------------------------------------------------------------


class TestRunSynthesizedTest:
    def test_pass(self):
        sb = _fake_sandbox(run_rc=0, run_output="1 passed")
        obs, result = run_synthesized_test("def test_x(): pass", sb)
        assert result["passed"] is True
        assert result["valid"] is True
        assert "PASS" in obs

    def test_fail(self):
        sb = _fake_sandbox(run_rc=1, run_output="AssertionError")
        obs, result = run_synthesized_test("def test_x(): assert False", sb)
        assert result["passed"] is False
        assert result["valid"] is True
        assert "FAIL" in obs

    def test_collection_error(self):
        sb = _fake_sandbox(run_rc=2, run_output="ERROR collecting")
        obs, result = run_synthesized_test("import no_such_module", sb)
        assert result["passed"] is None
        assert result["valid"] is False
        assert "INVALID" in obs

    def test_writes_file_to_sandbox(self):
        sb = _fake_sandbox(run_rc=0)
        src = "def test_x(): pass"
        run_synthesized_test(src, sb)
        sb.write_file.assert_called_once()
        _, written_src = sb.write_file.call_args[0]
        assert written_src == src

    def test_self_mocking_warning_in_result(self):
        sb = _fake_sandbox(run_rc=1, run_output="FAIL")
        src = "@patch('mod.Foo')\ndef test_x(m):\n    m.return_value = MagicMock()\n    assert False"
        _, result = run_synthesized_test(src, sb)
        assert result["self_mocking_warning"] is True

    def test_custom_filename(self):
        sb = _fake_sandbox(run_rc=0)
        run_synthesized_test("def test_x(): pass", sb, test_filename="custom_test.py")
        sb.write_file.assert_called_once_with("custom_test.py", "def test_x(): pass")


# ---------------------------------------------------------------------------
# fix_probe
# ---------------------------------------------------------------------------


class TestFixProbe:
    def _make_sandbox(self, patch_rc=0, test_rc=0):
        sb = MagicMock()
        sb.repo_path = "/repo"
        # patch returns success, then test run returns test_rc
        sb.run_command.side_effect = [
            (patch_rc, "patching file mod.py"),
            (test_rc, "1 passed" if test_rc == 0 else "1 failed"),
        ]
        return sb

    def test_fail_to_pass_flip(self):
        sb = self._make_sandbox(patch_rc=0, test_rc=0)
        obs, result = fix_probe(
            "--- a/mod.py\n+++ b/mod.py\n", "def test_x(): pass", sb
        )
        assert result["patch_applied"] is True
        assert result["passed"] is True
        assert "FAIL→PASS" in obs

    def test_fail_to_fail(self):
        sb = self._make_sandbox(patch_rc=0, test_rc=1)
        obs, result = fix_probe(
            "--- a/mod.py\n+++ b/mod.py\n", "def test_x(): assert False", sb
        )
        assert result["patch_applied"] is True
        assert result["passed"] is False
        assert "FAIL→FAIL" in obs

    def test_patch_failure(self):
        sb = MagicMock()
        sb.repo_path = "/repo"
        sb.run_command.return_value = (1, "patch: **** malformed patch")
        obs, result = fix_probe("bad diff", "def test_x(): pass", sb)
        assert result["patch_applied"] is False
        assert result["passed"] is None
        assert "PATCH_FAILED" in obs

    def test_writes_patch_file(self):
        sb = self._make_sandbox(patch_rc=0, test_rc=0)
        diff = "--- a/mod.py\n+++ b/mod.py\n@@ -1 +1 @@\n-old\n+new\n"
        fix_probe(diff, "def test_x(): pass", sb)
        sb.write_file.assert_called()
        first_call = sb.write_file.call_args_list[0]
        assert first_call[0][1] == diff  # diff written first


# ---------------------------------------------------------------------------
# differential_run
# ---------------------------------------------------------------------------


class TestDifferentialRun:
    def _make_sandboxes(self, *, prior_rc, current_rc):
        def make(rc):
            sb = MagicMock()
            sb.repo_path = "/repo"
            sb.run_command.return_value = (rc, "output")
            return sb

        return make(prior_rc), make(current_rc)

    def test_regression_pass_to_fail(self):
        prior_sb, current_sb = self._make_sandboxes(prior_rc=0, current_rc=1)
        obs, result = differential_run("def test_x(): pass", current_sb, prior_sb)
        assert result["is_regression"] is True
        assert result["prior_passed"] is True
        assert result["current_passed"] is False
        assert "PASS→FAIL" in obs

    def test_both_fail_not_regression(self):
        prior_sb, current_sb = self._make_sandboxes(prior_rc=1, current_rc=1)
        obs, result = differential_run(
            "def test_x(): assert False", current_sb, prior_sb
        )
        assert result["is_regression"] is False
        assert "FAIL→FAIL" in obs

    def test_both_pass_not_regression(self):
        prior_sb, current_sb = self._make_sandboxes(prior_rc=0, current_rc=0)
        obs, result = differential_run("def test_x(): pass", current_sb, prior_sb)
        assert result["is_regression"] is False
        assert "PASS→PASS" in obs


# ---------------------------------------------------------------------------
# corroboration_policy
# ---------------------------------------------------------------------------


class TestCorroborationPolicy:
    def test_invalid_test_cannot_confirm(self):
        may, reason = corroboration_policy({"valid": False, "passed": None})
        assert may is False
        assert "invalid" in reason.lower()

    def test_passing_test_cannot_confirm(self):
        may, reason = corroboration_policy({"valid": True, "passed": True})
        assert may is False
        assert "did not fail" in reason

    def test_red_test_alone_insufficient(self):
        may, reason = corroboration_policy({"valid": True, "passed": False})
        assert may is False
        assert "insufficient" in reason or "corroboration" in reason.lower()

    def test_fix_probe_flip_confirms(self):
        synth = {"valid": True, "passed": False}
        fix = {"patch_applied": True, "passed": True}
        may, reason = corroboration_policy(synth, fix_result=fix)
        assert may is True
        assert "FAIL→PASS" in reason

    def test_fix_probe_still_red_does_not_confirm(self):
        synth = {"valid": True, "passed": False}
        fix = {"patch_applied": True, "passed": False}
        may, reason = corroboration_policy(synth, fix_result=fix)
        assert may is False

    def test_fix_probe_patch_failed_does_not_confirm(self):
        synth = {"valid": True, "passed": False}
        fix = {"patch_applied": False, "passed": None}
        may, reason = corroboration_policy(synth, fix_result=fix)
        assert may is False
        assert "patch failed" in reason.lower()

    def test_differential_regression_confirms(self):
        synth = {"valid": True, "passed": False}
        diff = {"is_regression": True, "prior_passed": True, "current_passed": False}
        may, reason = corroboration_policy(synth, differential_result=diff)
        assert may is True
        assert "PASS→FAIL" in reason

    def test_differential_non_regression_does_not_confirm(self):
        synth = {"valid": True, "passed": False}
        diff = {"is_regression": False, "prior_passed": False, "current_passed": False}
        may, reason = corroboration_policy(synth, differential_result=diff)
        assert may is False


# ---------------------------------------------------------------------------
# dispatch_advanced_probe
# ---------------------------------------------------------------------------


class TestDispatchAdvancedProbe:
    def test_synthesize_test_valid(self):
        sb = _fake_sandbox(run_rc=0, run_output="ok")
        synth_store: list = []
        obs, record = dispatch_advanced_probe(
            "synthesize_test",
            {
                "description": "test arity",
                "target_symbol": "codeminer.guardian.cycle.run_cycle",
            },
            sb,
            synth_test_store=synth_store,
        )
        assert record.tool == "synthesize_test"
        assert (
            "scaffold" in record.output_summary.lower()
            or "import" in record.output_summary.lower()
        )

    def test_synthesize_test_invalid_import(self):
        sb = _fake_sandbox(run_rc=1, run_output="ImportError")
        obs, record = dispatch_advanced_probe(
            "synthesize_test",
            {"description": "desc", "target_symbol": "no.such.Symbol"},
            sb,
            synth_test_store=[],
        )
        assert "import error" in record.output_summary.lower()

    def test_run_synthesized_test_stores_source(self):
        sb = _fake_sandbox(run_rc=1, run_output="1 failed")
        synth_store: list = []
        src = "def test_x(): assert False"
        dispatch_advanced_probe(
            "run_synthesized_test",
            {"test_source": src},
            sb,
            synth_test_store=synth_store,
        )
        assert synth_store == [src]

    def test_run_synthesized_test_pass(self):
        sb = _fake_sandbox(run_rc=0, run_output="1 passed")
        obs, record = dispatch_advanced_probe(
            "run_synthesized_test",
            {"test_source": "def test_x(): pass"},
            sb,
            synth_test_store=[],
        )
        assert record.passed is True

    def test_fix_probe_uses_synth_store_fallback(self):
        """When test_source is omitted, use the last entry in synth_test_store."""
        sb = MagicMock()
        sb.repo_path = "/repo"
        sb.run_command.side_effect = [
            (0, "patching"),  # patch
            (0, "1 passed"),  # test run
        ]
        store = ["def test_stored(): pass"]
        obs, record = dispatch_advanced_probe(
            "fix_probe",
            {"diff": "--- a\n+++ b\n", "test_source": ""},
            sb,
            synth_test_store=store,
        )
        assert record.tool == "fix_probe"
        # The stored test source should have been used (write_file called twice:
        # once for patch file, once for test file inside run_synthesized_test).
        assert sb.write_file.call_count == 2

    def test_differential_run_is_reachable_with_prior_sandbox(self):
        current = _fake_sandbox(run_rc=1, run_output="1 failed")
        prior = _fake_sandbox(run_rc=0, run_output="1 passed")
        obs, record = dispatch_advanced_probe(
            "differential_run",
            {"test_source": "def test_contract(): assert True"},
            current,
            synth_test_store=[],
            prior_sandbox=prior,
        )
        assert record.tool == "differential_run"
        assert record.result["is_regression"] is True
        assert "PASS→FAIL" in obs

    def test_differential_run_without_prior_is_typed_observation(self):
        obs, record = dispatch_advanced_probe(
            "differential_run",
            {"test_source": "def test_contract(): pass"},
            _fake_sandbox(),
            synth_test_store=[],
        )
        assert "unavailable" in obs
        assert record.tool == "differential_run"

    def test_unknown_tool_returns_error_record(self):
        sb = _fake_sandbox()
        obs, record = dispatch_advanced_probe(
            "totally_unknown",
            {},
            sb,
            synth_test_store=[],
        )
        assert "unknown" in obs.lower()
        assert record.tool == "totally_unknown"
