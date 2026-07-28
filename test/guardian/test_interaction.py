# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Tests for Guardian's transport-neutral, untrusted message inbox."""

import json

import pytest

from codeminer.guardian import interaction, mcp_server
from codeminer.guardian.interaction import MAX_MESSAGE_CHARS, MessageInbox
from codeminer.guardian.loop import Hypothesis


def test_inbox_assigns_sender_head_and_monotonic_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(interaction, "_head", lambda _repo: "abc123")
    inbox = MessageInbox(str(tmp_path / "messages.jsonl"), repo_path="/repo")

    first = inbox.append("Feature should preserve aliases.", sender="solver:codex")
    second = inbox.append(
        "Metadata emission is uncertain.",
        sender="solver:codex",
        scope=["parser.py"],
    )

    assert first.id == "msg_00000001"
    assert second.sequence == 2
    assert second.observed_head == "abc123"
    assert second.scope == ["parser.py"]
    assert [item.content for item in inbox.read_recent()] == [
        "Feature should preserve aliases.",
        "Metadata emission is uncertain.",
    ]


def test_snapshot_is_stable_while_later_message_is_deferred(tmp_path, monkeypatch):
    monkeypatch.setattr(interaction, "_head", lambda _repo: "abc123")
    inbox = MessageInbox(str(tmp_path / "messages.jsonl"), repo_path="/repo")
    inbox.append("before cycle", sender="solver")
    cycle_snapshot = [item.to_dict() for item in inbox.read_recent()]

    inbox.append("during cycle", sender="solver")

    assert [item["content"] for item in cycle_snapshot] == ["before cycle"]
    assert [item.content for item in inbox.read_recent()] == [
        "before cycle",
        "during cycle",
    ]


def test_mcp_and_filesystem_transport_share_the_same_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(interaction, "_head", lambda _repo: "abc123")
    inbox = MessageInbox(str(tmp_path / "messages.jsonl"), repo_path="/repo")
    monkeypatch.setattr(mcp_server, "_message_inbox", inbox)

    result = json.loads(
        mcp_server._handle_send_guardian_message(
            {"message": "solver explanation", "scope": ["api.py"]}
        )
    )

    assert result["cycle_triggered"] is False
    assert inbox.read_recent()[0].sender == "solver:mcp"
    assert inbox.read_recent()[0].content == "solver explanation"


def test_messages_are_bounded_and_cannot_mint_finding_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(interaction, "_head", lambda _repo: "abc123")
    inbox = MessageInbox(str(tmp_path / "messages.jsonl"), repo_path="/repo")
    with pytest.raises(ValueError, match="character limit"):
        inbox.append("x" * (MAX_MESSAGE_CHARS + 1), sender="solver")

    with pytest.raises(ValueError, match="requires a probe-valid"):
        Hypothesis.create(
            claim="The message says behavior is broken",
            consequence="callers fail",
            remedy="change the code",
            origin="human",
            locus=["api.py"],
            evidence=["msg_00000001"],
            grade="finding",
            cycle_no=1,
        )
