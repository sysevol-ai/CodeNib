# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Context ledger contract tests."""

from __future__ import annotations

import pytest

from codenib.agent.runtime import ContextLedger, ContextLedgerEntry


def test_context_ledger_records_provenance_and_cost_fields():
    ledger = ContextLedger()

    entry = ledger.add(
        "grep",
        "offered",
        1,
        summary="grep returned 2 matches",
        provenance={"kind": "tool_result", "tool": "grep"},
        freshness="fresh",
        token_estimate=42,
        cost_estimate=0.001,
        metadata={"result_count": 2},
    )

    assert entry.entry_id == "context_1"
    assert ledger.entries == (entry,)
    assert ledger.to_list() == [
        {
            "source": "grep",
            "state": "offered",
            "turn": 1,
            "summary": "grep returned 2 matches",
            "metadata": {"result_count": 2},
            "entry_id": "context_1",
            "provenance": {"kind": "tool_result", "tool": "grep"},
            "freshness": "fresh",
            "token_estimate": 42,
            "cost_estimate": 0.001,
        }
    ]


def test_context_ledger_tracks_consumed_and_expired_state():
    ledger = ContextLedger()
    first = ledger.add("read", "read", 1, entry_id="read_1")
    second = ledger.add("runtime.compaction", "summarized", 2, entry_id="summary_1")

    consumed = ledger.mark_consumed("read_1", turn=3, consumer="final_answer")
    expired = ledger.mark_expired(
        "summary_1",
        turn=4,
        reason="history_budget",
    )

    assert consumed is first
    assert consumed.state == "consumed"
    assert consumed.consumed_turn == 3
    assert consumed.consumed_by == ["final_answer"]
    assert ledger.by_state("consumed") == [first]

    assert expired is second
    assert expired.state == "expired"
    assert expired.consumed_turn == 4
    assert expired.metadata["expiration_reason"] == "history_budget"


def test_context_ledger_validates_accounting_fields():
    with pytest.raises(ValueError, match="turn"):
        ContextLedgerEntry("tool", "offered", -1)

    with pytest.raises(ValueError, match="token_estimate"):
        ContextLedgerEntry("tool", "offered", 1, token_estimate=-1)

    with pytest.raises(KeyError, match="Unknown context ledger entry"):
        ContextLedger().mark_consumed("missing", turn=1, consumer="answer")
