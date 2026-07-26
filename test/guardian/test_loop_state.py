# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Tests for Guardian's carried L2 state and checkpoint contract."""

import json

import pytest

from codeminer.guardian.loop import (
    CycleState,
    Hypothesis,
    Signal,
    StateInconsistent,
    check_invariants,
    load_checkpoint,
    save_checkpoint,
)


def _hypothesis(**overrides):
    values = {
        "claim": "parse_config and normalize_config disagree on empty input",
        "consequence": "callers fail unpredictably",
        "remedy": "share validation and add regression tests",
        "origin": "exploration",
        "locus": ["config.py:parse_config", "config.py:normalize_config"],
        "cycle_no": 3,
    }
    values.update(overrides)
    return Hypothesis.create(**values)


def _state(hypotheses=None):
    signal = Signal.create(
        kind="churn",
        locus=["config.py"],
        detail="config.py changed three times",
        value={"commit_count": 3},
    )
    return CycleState(
        cycle_no=3,
        commit="abc123",
        hypotheses=list(hypotheses or [_hypothesis()]),
        signals=[signal],
        current=None,
        budget_total=10_000,
        budget_spent=125,
        decision_log=[{"event": "tool_call", "tool": "read_code"}],
        exit_reason=None,
        carried_from=2,
    )


def test_signal_and_hypothesis_are_distinct():
    signal = Signal.create(
        kind="churn",
        locus=["config.py"],
        detail="config.py changed three times",
    )
    hypothesis = _hypothesis()
    assert signal.kind == "churn"
    assert hypothesis.claim.startswith("parse_config")
    assert not hasattr(signal, "grade")
    assert not hasattr(hypothesis, "kind") or hypothesis.kind == "exploration"


def test_hypothesis_requires_claim_consequence_and_remedy():
    with pytest.raises(ValueError, match="remedy"):
        _hypothesis(remedy="")


def test_finding_grade_requires_probe_evidence():
    with pytest.raises(ValueError, match="probe"):
        _hypothesis(grade="finding")

    finding = _hypothesis(
        grade="finding",
        evidence=["probe-valid:pytest:test_config_empty"],
    )
    assert finding.grade == "finding"

    for non_admissible in ("probe-invalid:3:1", "env:3:1"):
        with pytest.raises(ValueError, match="probe-valid"):
            _hypothesis(grade="finding", evidence=[non_admissible])


def test_source_grounded_evidence_can_support_a_finding():
    finding = _hypothesis(
        grade="finding",
        evidence=["source-valid:3:1"],
    )

    assert finding.grade == "finding"


def test_checkpoint_round_trip_is_reconstructible(tmp_path):
    state = _state()
    messages = [
        {"role": "system", "content": "frame"},
        {"role": "assistant", "content": "", "tool_calls": []},
    ]
    checkpoint = tmp_path / "out" / "cycle_state.json"

    save_checkpoint(checkpoint, state, messages)
    restored, restored_messages = load_checkpoint(checkpoint)

    assert restored == state
    assert restored_messages == messages
    assert json.loads(checkpoint.read_text())["state"]["budget_spent"] == 125


def test_checkpoint_round_trip_preserves_unlimited_budget(tmp_path):
    state = _state()
    state.budget_total = None
    checkpoint = tmp_path / "cycle_state.json"

    save_checkpoint(checkpoint, state, [])
    restored, _messages = load_checkpoint(checkpoint)

    assert restored.budget_total is None
    assert json.loads(checkpoint.read_text())["state"]["budget_total"] is None
    check_invariants(restored)


def test_checkpoint_replaces_existing_file_atomically(tmp_path):
    checkpoint = tmp_path / "cycle_state.json"
    save_checkpoint(checkpoint, _state(), [])
    changed = _state()
    changed.budget_spent = 500
    save_checkpoint(checkpoint, changed, [{"role": "tool", "content": "observation"}])
    restored, messages = load_checkpoint(checkpoint)
    assert restored.budget_spent == 500
    assert messages[0]["role"] == "tool"


def test_invariant_rejects_duplicate_hypothesis_ids():
    hypothesis = _hypothesis()
    state = _state([hypothesis, hypothesis])
    with pytest.raises(StateInconsistent, match="unique"):
        check_invariants(state)


def test_ledger_allows_atomic_call_to_cross_limit():
    state = _state()
    state.budget_spent = state.budget_total + 1
    check_invariants(state)
