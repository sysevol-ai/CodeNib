# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Behavioral tests for Guardian's agent-owned L2 loop."""

import json
from types import SimpleNamespace
from unittest.mock import patch

from codeminer.guardian.loop import CycleState, Signal, load_checkpoint
from codeminer.guardian.loop.runtime import LoopContext, run_cycle_loop


def _call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _response(*calls, content="", tokens=10):
    message = SimpleNamespace(content=content, tool_calls=list(calls))
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(
            prompt_tokens=tokens,
            completion_tokens=0,
            total_tokens=tokens,
        ),
    )


class _ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.seen_messages = []

    def _call_raw(self, messages, **kwargs):
        self.seen_messages.append(list(messages))
        return self.responses.pop(0)


def _state():
    signal = Signal.create(
        kind="churn",
        locus=["mod.py"],
        detail="mod.py changed five times",
        value={"commit_count": 5},
    )
    return CycleState(
        cycle_no=1,
        commit="abc123",
        hypotheses=[],
        signals=[signal],
        current=None,
        budget_total=10_000,
        budget_spent=0,
        decision_log=[],
        exit_reason=None,
        carried_from=None,
    )


def test_cycle_reaches_submission_after_three_agent_turns(tmp_path):
    signal_id = _state().signals[0].id
    llm = _ScriptedLLM(
        [
            _response(_call("c1", "list_signals", {})),
            _response(
                _call(
                    "c2",
                    "write_hypothesis",
                    {
                        "claim": "mod.parse returns None for a required field",
                        "consequence": "callers dereference None",
                        "remedy": "reject missing fields and add a regression test",
                        "origin": "signal",
                        "locus": ["mod.py:parse"],
                        "evidence": [f"signal:{signal_id}"],
                        "confidence": 0.6,
                    },
                )
            ),
            _response(
                _call(
                    "c3",
                    "submit_report",
                    {"summary": "One conjecture remains for a later probe."},
                )
            ),
        ]
    )
    checkpoint = tmp_path / "out" / "cycle_state.json"
    state = _state()
    result = run_cycle_loop(
        state,
        LoopContext(
            repo_path=str(tmp_path),
            arm="memory",
            llm=llm,
            retriever=None,
            checkpoint_path=checkpoint,
            observation_dir=tmp_path / "out" / "observations",
        ),
    )

    assert result.exit_reason == "ReportSubmitted"
    assert len(llm.seen_messages) == 3
    assert len(result.hypotheses) == 1
    assert result.hypotheses[0].grade == "conjecture"
    restored, transcript = load_checkpoint(checkpoint)
    assert restored == result
    assert [
        row["tool"] for row in result.decision_log if row["event"] == "tool_call"
    ] == [
        "list_signals",
        "write_hypothesis",
        "submit_report",
    ]
    assert [message["role"] for message in transcript].count("tool") == 2


def test_unlimited_budget_runs_until_agent_submission(tmp_path):
    state = _state()
    state.budget_total = None
    llm = _ScriptedLLM(
        [
            _response(_call("c1", "list_signals", {}), tokens=150_000),
            _response(
                _call("c2", "submit_report", {"summary": "Natural stop."}),
                tokens=150_000,
            ),
        ]
    )

    result = run_cycle_loop(
        state,
        LoopContext(
            repo_path=str(tmp_path),
            arm="memory",
            llm=llm,
            retriever=None,
        ),
    )

    assert result.exit_reason == "ReportSubmitted"
    assert result.budget_total is None
    assert result.budget_spent == 300_000
    assert "Token budget: unlimited" in llm.seen_messages[0][1]["content"]


def test_grade_rejection_is_observation_and_loop_can_recover(tmp_path):
    state = _state()
    signal_id = state.signals[0].id
    llm = _ScriptedLLM(
        [
            _response(
                _call(
                    "c1",
                    "write_hypothesis",
                    {
                        "claim": "mod.parse accepts invalid input",
                        "consequence": "invalid state reaches callers",
                        "remedy": "validate before parsing",
                        "origin": "signal",
                        "locus": ["mod.py:parse"],
                        "evidence": [f"signal:{signal_id}"],
                    },
                )
            ),
            _response(
                _call(
                    "c2",
                    "update_hypothesis",
                    {
                        # Stable hash returned by turn 1 is visible in the transcript,
                        # but compute it through the state written by the first dispatch.
                        "id": "missing",
                        "grade": "finding",
                        "reason": "premature",
                    },
                )
            ),
            _response(_call("c3", "submit_report", {"summary": "Deferred."})),
        ]
    )
    result = run_cycle_loop(
        state,
        LoopContext(
            repo_path=str(tmp_path),
            arm="memoryless",
            llm=llm,
            retriever=None,
            checkpoint_path=tmp_path / "cycle_state.json",
        ),
    )
    update = next(
        row
        for row in result.decision_log
        if row.get("event") == "tool_call" and row.get("tool") == "update_hypothesis"
    )
    assert "tool_error" in update["observation"]
    assert result.exit_reason == "ReportSubmitted"


def test_model_failure_is_explicitly_degraded(tmp_path):
    class _BrokenLLM:
        def _call_raw(self, messages, **kwargs):
            raise RuntimeError("offline")

    result = run_cycle_loop(
        _state(),
        LoopContext(
            repo_path=str(tmp_path),
            arm="memory",
            llm=_BrokenLLM(),
            retriever=None,
            checkpoint_path=tmp_path / "cycle_state.json",
        ),
    )
    assert result.degraded is True
    assert result.exit_reason == "Degraded"


def test_investigation_grade_is_agent_written_not_derived(tmp_path):
    state = _state()
    state.budget_total = None
    # Seed through the canonical constructor so the L3 compatibility properties
    # are exercised by the mocked investigator path.
    from codeminer.guardian.loop import Hypothesis

    hypothesis = Hypothesis.create(
        claim="mod.parse accepts invalid input",
        consequence="invalid state reaches callers",
        remedy="validate before parsing",
        origin="exploration",
        locus=["mod.py:parse"],
        cycle_no=1,
    )
    state.hypotheses.append(hypothesis)
    fake_result = SimpleNamespace(
        tokens_used=20,
        to_dict=lambda: {
            "verdict": "confirmed",
            "reasoning": "probe reproduced the behavior",
            "probe_trace": [],
        },
    )
    llm = _ScriptedLLM(
        [
            _response(
                _call(
                    "c1",
                    "investigate",
                    {"hypothesis_id": hypothesis.id, "budget_tokens": 8_000},
                )
            ),
            _response(_call("c2", "submit_report", {"summary": "Probe complete."})),
        ]
    )
    with patch(
        "codeminer.guardian.investigator.run_investigator",
        return_value=fake_result,
    ):
        result = run_cycle_loop(
            state,
            LoopContext(
                repo_path=str(tmp_path),
                arm="memory",
                llm=llm,
                retriever=None,
                sandbox=SimpleNamespace(repo_path=str(tmp_path)),
            ),
        )
    assert result.hypotheses[0].grade == "conjecture"
    assert any(
        ref.startswith("probe-invalid:") for ref in result.hypotheses[0].evidence
    )


def test_source_grounded_investigation_can_be_promoted_by_agent(tmp_path):
    from codeminer.guardian.loop import Hypothesis

    state = _state()
    state.budget_total = None
    hypothesis = Hypothesis.create(
        claim="mod.parse returns values in the wrong contract order",
        consequence="callers bind the wrong values",
        remedy="restore the documented return order",
        origin="exploration",
        locus=["mod.py:parse"],
        cycle_no=1,
    )
    state.hypotheses.append(hypothesis)
    fake_result = SimpleNamespace(
        evidence_status="source",
        exit_status="submitted",
        budget=SimpleNamespace(actual=20),
        to_dict=lambda: {
            "verdict": "confirmed",
            "evidence_status": "source",
            "exit_status": "submitted",
        },
    )
    llm = _ScriptedLLM(
        [
            _response(
                _call(
                    "c1",
                    "investigate",
                    {
                        "hypothesis_id": hypothesis.id,
                        "budget_tokens": 8_000,
                        "obligation": "Check the return contract.",
                    },
                )
            ),
            _response(
                _call(
                    "c2",
                    "update_hypothesis",
                    {
                        "id": hypothesis.id,
                        "grade": "finding",
                        "reason": "Exact source establishes the mismatch.",
                    },
                )
            ),
            _response(_call("c3", "submit_report", {"summary": "One finding."})),
        ]
    )

    with patch(
        "codeminer.guardian.investigator.run_investigator",
        return_value=fake_result,
    ):
        result = run_cycle_loop(
            state,
            LoopContext(
                repo_path=str(tmp_path),
                arm="memory",
                llm=llm,
                retriever=None,
                sandbox=SimpleNamespace(repo_path=str(tmp_path)),
            ),
        )

    assert result.hypotheses[0].grade == "finding"
    assert "source-valid:1:1" in result.hypotheses[0].evidence


def test_investigator_receives_obligation_diff_and_locus_excerpt(tmp_path):
    from codeminer.guardian.loop import Hypothesis

    (tmp_path / "mod.py").write_text(
        "def parse(value):\n    return value\n",
        encoding="utf-8",
    )
    state = _state()
    state.budget_total = None
    hypothesis = Hypothesis.create(
        claim="mod.parse returns an invalid value",
        consequence="callers receive invalid state",
        remedy="restore the return contract",
        origin="exploration",
        locus=["mod.py:1-2"],
        cycle_no=1,
    )
    state.hypotheses.append(hypothesis)
    fake_result = SimpleNamespace(
        evidence_status="invalid",
        exit_status="submitted",
        budget=SimpleNamespace(actual=20),
        to_dict=lambda: {
            "verdict": "inconclusive",
            "evidence_status": "invalid",
            "exit_status": "submitted",
        },
    )
    llm = _ScriptedLLM(
        [
            _response(
                _call(
                    "c1",
                    "investigate",
                    {
                        "hypothesis_id": hypothesis.id,
                        "budget_tokens": 8_000,
                        "obligation": "Check the changed return contract.",
                    },
                )
            ),
            _response(_call("c2", "submit_report", {"summary": "Investigated."})),
        ]
    )

    with (
        patch(
            "codeminer.guardian.investigator.run_investigator",
            return_value=fake_result,
        ) as investigator,
        patch(
            "codeminer.guardian.loop.runtime._commit_diff",
            return_value="diff --git a/mod.py b/mod.py",
        ),
    ):
        run_cycle_loop(
            state,
            LoopContext(
                repo_path=str(tmp_path),
                arm="memory",
                llm=llm,
                retriever=None,
                sandbox=SimpleNamespace(repo_path=str(tmp_path)),
            ),
        )

    kwargs = investigator.call_args.kwargs
    assert kwargs["obligation"] == "Check the changed return contract."
    assert kwargs["commit_diff"].startswith("diff --git")
    assert kwargs["excerpts"][0].path == "mod.py"
    assert "return value" in kwargs["excerpts"][0].content
    assert kwargs["prior_attempts"] == []


def test_environment_failed_investigation_marks_cycle_degraded(tmp_path):
    from codeminer.guardian.loop import Hypothesis

    state = _state()
    state.budget_total = None
    hypothesis = Hypothesis.create(
        claim="mod.parse accepts invalid input",
        consequence="invalid state reaches callers",
        remedy="validate before parsing",
        origin="exploration",
        locus=["mod.py:parse"],
        cycle_no=1,
    )
    state.hypotheses.append(hypothesis)
    fake_result = SimpleNamespace(
        evidence_status="environment",
        exit_status="environment_unavailable",
        budget=SimpleNamespace(actual=0),
        to_dict=lambda: {
            "evidence_status": "environment",
            "exit_status": "environment_unavailable",
        },
    )
    llm = _ScriptedLLM(
        [
            _response(
                _call(
                    "c1",
                    "investigate",
                    {
                        "hypothesis_id": hypothesis.id,
                        "budget_tokens": 8_000,
                        "obligation": "Check invalid input.",
                    },
                )
            ),
            _response(_call("c2", "submit_report", {"summary": "Degraded."})),
        ]
    )

    with patch(
        "codeminer.guardian.investigator.run_investigator",
        return_value=fake_result,
    ):
        result = run_cycle_loop(
            state,
            LoopContext(
                repo_path=str(tmp_path),
                arm="memory",
                llm=llm,
                retriever=None,
                sandbox=SimpleNamespace(repo_path=str(tmp_path)),
            ),
        )

    assert result.degraded is True
    investigate = next(
        row for row in result.decision_log if row.get("tool") == "investigate"
    )
    assert investigate["state_after"]["grade_counts"]["finding"] == 0
    assert "admissible_grades" in investigate["observation"]


def test_agent_can_inspect_reviewed_commit_diff(tmp_path):
    llm = _ScriptedLLM(
        [
            _response(_call("c1", "read_commit_diff", {})),
            _response(_call("c2", "submit_report", {"summary": "Reviewed diff."})),
        ]
    )

    with patch(
        "codeminer.guardian.loop.runtime._commit_diff",
        return_value="diff --git a/mod.py b/mod.py",
    ):
        result = run_cycle_loop(
            _state(),
            LoopContext(
                repo_path=str(tmp_path),
                arm="memory",
                llm=llm,
                retriever=None,
            ),
        )

    observation = next(
        row for row in result.decision_log if row.get("tool") == "read_commit_diff"
    )["observation"]
    assert observation.startswith("diff --git")


def test_long_context_is_summarized_as_one_coherent_history(tmp_path):
    observation = "X" * 5_000
    llm = _ScriptedLLM(
        [
            _response(_call("c1", "search_code", {"query": "contract", "top_k": 1})),
            _response(
                content=(
                    "The contract search returned one large source observation; "
                    "no hypothesis has been written yet."
                )
            ),
            _response(_call("c2", "submit_report", {"summary": "recovered"})),
        ]
    )
    retriever = SimpleNamespace(query=lambda query, top_k=None: [observation])
    result = run_cycle_loop(
        _state(),
        LoopContext(
            repo_path=str(tmp_path),
            arm="memory",
            llm=llm,
            retriever=retriever,
            observation_dir=tmp_path / "observations",
            max_context_tokens=1_500,
            context_reserve_tokens=300,
        ),
    )
    assert result.exit_reason == "ReportSubmitted"
    assert result.compaction_events == 1
    assert observation in str(llm.seen_messages[1])
    assert "large source observation" in str(llm.seen_messages[2])
    assert observation not in str(llm.seen_messages[2])
