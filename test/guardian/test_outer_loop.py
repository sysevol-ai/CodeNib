# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Behavioral tests for Guardian's agent-owned L2 loop."""

import hashlib
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
                    {"hypothesis_id": hypothesis.id, "budget_tokens": 500},
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
    assert any(ref.startswith("probe:") for ref in result.hypotheses[0].evidence)


def test_externalized_observation_is_rereadable_by_agent(tmp_path):
    observation = "X" * 5_000
    ref = hashlib.sha256(observation.encode("utf-8")).hexdigest()[:20] + ".txt"
    llm = _ScriptedLLM(
        [
            _response(_call("c1", "search_code", {"query": "contract", "top_k": 1})),
            _response(_call("c2", "read_observation", {"ref": ref})),
            _response(_call("c3", "submit_report", {"summary": "recovered"})),
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
            max_context_chars=2_500,
        ),
    )
    reread = next(
        row
        for row in result.decision_log
        if row.get("event") == "tool_call" and row.get("tool") == "read_observation"
    )
    assert reread["observation"] == observation[:2_000]
    assert result.compaction_events >= 1
