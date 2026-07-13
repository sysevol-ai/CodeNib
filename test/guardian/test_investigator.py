# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for codeminer.guardian.investigator."""

import json
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from codeminer.guardian.investigator import (
    InvestigatorResult,
    ProbeRecord,
    SandboxHandle,
    WorktreeSandbox,
    _parse_verdict,
    run_investigator,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_hypothesis(
    target="mod.py",
    statement="mod.py may break dependents after the recent arity change.",
    kind="churn",
    rank=1,
    confidence=0.75,
):
    from codeminer.guardian.hypothesize import Hypothesis
    return Hypothesis(
        rank=rank,
        target=target,
        kind=kind,
        statement=statement,
        confidence=confidence,
    )


def _resp(content, tool_calls=None):
    """Build a mock litellm response."""
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls or []
    choice = MagicMock()
    choice.message = msg
    r = MagicMock()
    r.choices = [choice]
    r.usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    return r


def _make_tool_call(call_id, name, **kwargs):
    tc = MagicMock()
    tc.id = call_id
    tc.function.name = name
    tc.function.arguments = json.dumps(kwargs)
    return tc


def _make_llm(side_effects):
    llm = MagicMock()
    llm._call_raw.side_effect = side_effects
    return llm


def _fake_retriever(results=None):
    r = MagicMock()
    r.query.return_value = results or []
    return r


def _fake_sandbox(repo_path="/tmp/repo", run_rc=0, run_output="ok"):
    sb = MagicMock(spec=WorktreeSandbox)
    sb.repo_path = repo_path
    sb.run_command.return_value = (run_rc, run_output)
    return sb


# ---------------------------------------------------------------------------
# ProbeRecord
# ---------------------------------------------------------------------------

class TestProbeRecord:
    def test_defaults(self):
        p = ProbeRecord(tool="retrieve_evidence", input_summary="q", output_summary="r")
        assert p.passed is None

    def test_with_passed(self):
        p = ProbeRecord(tool="run_existing_test", input_summary="t", output_summary="PASS", passed=True)
        assert p.passed is True


# ---------------------------------------------------------------------------
# InvestigatorResult
# ---------------------------------------------------------------------------

class TestInvestigatorResult:
    def test_defaults(self):
        r = InvestigatorResult(verdict="inconclusive", reasoning="not sure")
        assert r.evidence_test == ""
        assert r.evidence_diff == ""
        assert r.probe_trace == []
        assert r.tokens_used == 0

    def test_to_dict(self):
        probe = ProbeRecord(tool="retrieve_evidence", input_summary="q", output_summary="r")
        r = InvestigatorResult(
            verdict="confirmed",
            reasoning="risk is real",
            evidence_test="def test_x(): ...",
            probe_trace=[probe],
            tokens_used=42,
        )
        d = r.to_dict()
        assert d["verdict"] == "confirmed"
        assert d["tokens_used"] == 42
        assert len(d["probe_trace"]) == 1
        assert d["probe_trace"][0]["tool"] == "retrieve_evidence"


# ---------------------------------------------------------------------------
# _parse_verdict
# ---------------------------------------------------------------------------

class TestParseVerdict:
    def test_confirmed(self):
        verdict, reasoning = _parse_verdict("verdict: confirmed\nThe risk is real.")
        assert verdict == "confirmed"
        assert "risk" in reasoning

    def test_rejected(self):
        verdict, _ = _parse_verdict("Some analysis.\nverdict: rejected")
        assert verdict == "rejected"

    def test_inconclusive_explicit(self):
        verdict, _ = _parse_verdict("verdict: inconclusive")
        assert verdict == "inconclusive"

    def test_no_verdict_line_defaults_inconclusive(self):
        verdict, reasoning = _parse_verdict("I found nothing conclusive.")
        assert verdict == "inconclusive"
        assert "nothing" in reasoning

    def test_invalid_verdict_value_ignored(self):
        verdict, _ = _parse_verdict("verdict: maybe")
        assert verdict == "inconclusive"

    def test_verdict_case_insensitive(self):
        verdict, _ = _parse_verdict("VERDICT: Confirmed\nRisk.")
        assert verdict == "confirmed"


# ---------------------------------------------------------------------------
# WorktreeSandbox
# ---------------------------------------------------------------------------

class TestWorktreeSandbox:
    def test_run_command_pass(self, tmp_path):
        sb = WorktreeSandbox(str(tmp_path))
        rc, out = sb.run_command(["python", "-c", "print('hello')"])
        assert rc == 0
        assert "hello" in out

    def test_run_command_fail(self, tmp_path):
        sb = WorktreeSandbox(str(tmp_path))
        rc, out = sb.run_command(["python", "-c", "raise SystemExit(1)"])
        assert rc != 0

    def test_run_command_timeout(self, tmp_path):
        sb = WorktreeSandbox(str(tmp_path))
        rc, out = sb.run_command(["python", "-c", "import time; time.sleep(5)"], timeout=1)
        assert rc != 0
        assert "timed out" in out

    def test_write_and_read_file(self, tmp_path):
        sb = WorktreeSandbox(str(tmp_path))
        sb.write_file("sub/test_x.py", "# content\n")
        content = sb.read_file("sub/test_x.py")
        assert "content" in content

    def test_read_missing_file_returns_empty(self, tmp_path):
        sb = WorktreeSandbox(str(tmp_path))
        assert sb.read_file("no_such_file.py") == ""

    def test_sandbox_handle_protocol(self, tmp_path):
        sb = WorktreeSandbox(str(tmp_path))
        assert isinstance(sb, SandboxHandle)


# ---------------------------------------------------------------------------
# run_investigator — budget / no-op paths
# ---------------------------------------------------------------------------

class TestRunInvestigatorBudget:
    def test_budget_already_exhausted_returns_inconclusive(self):
        from codeminer.guardian.llm_investigator import LLMUsage
        hyp = _make_hypothesis()
        llm = MagicMock()
        sandbox = _fake_sandbox()
        usage = LLMUsage(total_tokens=999_999)

        result = run_investigator(
            hyp, llm, _fake_retriever(), sandbox,
            budget_tokens=100,
            usage_acc=usage,
        )

        llm._call_raw.assert_not_called()
        assert result.verdict == "inconclusive"
        assert "exhausted" in result.reasoning.lower()

    def test_budget_exhausted_mid_loop_forces_final_answer(self):
        hyp = _make_hypothesis()
        tc = _make_tool_call("c1", "retrieve_evidence", query="mod.py callers")
        # First call returns a tool call (consumes tokens); second call is the
        # forced final answer after the budget check fires.
        side_effects = [
            _resp("", tool_calls=[tc]),
            _resp("verdict: inconclusive\nRan out of budget."),
        ]
        llm = _make_llm(side_effects)
        sandbox = _fake_sandbox()

        result = run_investigator(
            hyp, llm, _fake_retriever(), sandbox,
            budget_tokens=15,   # exactly one round of 15 tokens
            max_rounds=5,
        )

        assert result.verdict == "inconclusive"


# ---------------------------------------------------------------------------
# run_investigator — tool-use loop
# ---------------------------------------------------------------------------

class TestRunInvestigatorToolLoop:
    def test_single_retrieve_then_verdict(self):
        hyp = _make_hypothesis()
        tc = _make_tool_call("c1", "retrieve_evidence", query="parse_config callers")
        side_effects = [
            _resp("", tool_calls=[tc]),
            _resp("verdict: rejected\nNo evidence of a contract break."),
        ]
        llm = _make_llm(side_effects)

        result = run_investigator(
            hyp, llm, _fake_retriever(), _fake_sandbox(), budget_tokens=100_000
        )

        assert result.verdict == "rejected"
        assert len(result.probe_trace) == 1
        assert result.probe_trace[0].tool == "retrieve_evidence"

    def test_two_probes_then_verdict_confirmed(self):
        hyp = _make_hypothesis()
        tc1 = _make_tool_call("c1", "retrieve_evidence", query="parse_config")
        tc2 = _make_tool_call("c2", "run_existing_test", test_pattern="test/test_config.py")
        side_effects = [
            _resp("", tool_calls=[tc1]),
            _resp("", tool_calls=[tc2]),
            _resp("verdict: confirmed\nExisting test fails on the symbol."),
        ]
        llm = _make_llm(side_effects)
        sandbox = _fake_sandbox(run_rc=1, run_output="FAILED test_config.py::test_parse")

        result = run_investigator(
            hyp, llm, _fake_retriever(), sandbox, budget_tokens=100_000
        )

        assert result.verdict == "confirmed"
        assert len(result.probe_trace) == 2
        assert result.probe_trace[1].tool == "run_existing_test"
        assert result.probe_trace[1].passed is False

    def test_probe_trace_entries_correct(self):
        hyp = _make_hypothesis()
        tc = _make_tool_call("c1", "retrieve_evidence", query="my_function")
        side_effects = [
            _resp("", tool_calls=[tc]),
            _resp("verdict: inconclusive\nNot enough evidence."),
        ]
        llm = _make_llm(side_effects)

        result = run_investigator(
            hyp, llm, _fake_retriever(), _fake_sandbox(), budget_tokens=100_000
        )

        assert result.probe_trace[0].input_summary.startswith("retrieve_evidence(")

    def test_stub_probe_returns_inconclusive_note(self):
        """synthesize_test is not yet in probes.py — should stub gracefully."""
        hyp = _make_hypothesis()
        tc = _make_tool_call("c1", "synthesize_test",
                             description="test arity break",
                             target_symbol="codeminer.guardian.cycle.run_cycle")
        side_effects = [
            _resp("", tool_calls=[tc]),
            _resp("verdict: inconclusive\nTool not available."),
        ]
        llm = _make_llm(side_effects)

        result = run_investigator(
            hyp, llm, _fake_retriever(), _fake_sandbox(), budget_tokens=100_000
        )

        # Stub should not crash the loop.
        assert result.verdict == "inconclusive"
        assert result.probe_trace[0].tool == "synthesize_test"

    def test_unknown_tool_name_handled_gracefully(self):
        hyp = _make_hypothesis()
        tc = _make_tool_call("c1", "nonexistent_tool", foo="bar")
        side_effects = [
            _resp("", tool_calls=[tc]),
            _resp("verdict: inconclusive\nTool not recognised."),
        ]
        llm = _make_llm(side_effects)

        result = run_investigator(
            hyp, llm, _fake_retriever(), _fake_sandbox(), budget_tokens=100_000
        )

        assert result.verdict == "inconclusive"
        assert result.probe_trace[0].tool == "nonexistent_tool"


# ---------------------------------------------------------------------------
# run_investigator — error handling
# ---------------------------------------------------------------------------

class TestRunInvestigatorErrors:
    def test_llm_exception_returns_inconclusive(self):
        hyp = _make_hypothesis()
        llm = MagicMock()
        llm._call_raw.side_effect = RuntimeError("API down")

        result = run_investigator(
            hyp, llm, _fake_retriever(), _fake_sandbox(), budget_tokens=100_000
        )

        assert result.verdict == "inconclusive"
        assert "API down" in result.reasoning

    def test_malformed_tool_arguments_handled(self):
        hyp = _make_hypothesis()
        tc = MagicMock()
        tc.id = "c1"
        tc.function.name = "retrieve_evidence"
        tc.function.arguments = "NOT JSON {"

        side_effects = [
            _resp("", tool_calls=[tc]),
            _resp("verdict: inconclusive\nBad args."),
        ]
        llm = _make_llm(side_effects)

        result = run_investigator(
            hyp, llm, _fake_retriever(), _fake_sandbox(), budget_tokens=100_000
        )

        # Should complete without crashing.
        assert result.verdict == "inconclusive"

    def test_tokens_used_accumulated(self):
        hyp = _make_hypothesis()
        tc = _make_tool_call("c1", "retrieve_evidence", query="q")
        side_effects = [
            _resp("", tool_calls=[tc]),
            _resp("verdict: rejected\nNo risk."),
        ]
        llm = _make_llm(side_effects)

        result = run_investigator(
            hyp, llm, _fake_retriever(), _fake_sandbox(), budget_tokens=100_000
        )

        # Two LLM calls × 15 tokens each = 30.
        assert result.tokens_used == 30

    def test_usage_acc_shared_across_calls(self):
        from codeminer.guardian.llm_investigator import LLMUsage
        hyp = _make_hypothesis()
        tc = _make_tool_call("c1", "retrieve_evidence", query="q")
        side_effects = [
            _resp("", tool_calls=[tc]),
            _resp("verdict: confirmed\nRisk confirmed."),
        ]
        llm = _make_llm(side_effects)
        usage = LLMUsage()

        run_investigator(
            hyp, llm, _fake_retriever(), _fake_sandbox(),
            budget_tokens=100_000, usage_acc=usage,
        )

        assert usage.total_tokens == 30
