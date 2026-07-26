# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for action-driven Guardian MCP startup and baseline handling."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from codeminer.guardian import mcp_server


@pytest.fixture(autouse=True)
def _reset_server_state(monkeypatch):
    monkeypatch.setattr(mcp_server, "_watcher_config", None)
    monkeypatch.setattr(mcp_server, "_watcher_baseline", "")
    monkeypatch.setattr(mcp_server, "_watcher_thread", None)
    monkeypatch.setattr(mcp_server, "_call_counter", 0)
    with mcp_server._lock:
        mcp_server._cache.update(
            {
                "findings": [],
                "commit": "",
                "cycle_no": 0,
                "running": False,
                "started": False,
                "baseline_commit": "",
                "observed_head": "",
            }
        )


def test_query_action_starts_watcher_once(monkeypatch):
    starts = []

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.alive = False

        def start(self):
            starts.append(self.kwargs)
            self.alive = True

        def is_alive(self):
            return self.alive

    monkeypatch.setattr(mcp_server.threading, "Thread", FakeThread)
    monkeypatch.setattr(mcp_server, "_head", lambda _repo: "base000")
    monkeypatch.setattr(
        mcp_server,
        "_watcher_config",
        SimpleNamespace(repo_path="/repo"),
    )
    monkeypatch.setattr(mcp_server, "_watcher_baseline", "base000")

    first = json.loads(mcp_server._handle_query_guardian({"hypothesis": "risk"}))
    second = json.loads(mcp_server._handle_query_guardian({"hypothesis": "risk"}))

    assert len(starts) == 1
    assert first["guardian_started"] is True
    assert first["started_by_this_action"] is True
    assert first["status"] == "baseline_unchanged"
    assert first["cycle_no"] == 0
    assert second["started_by_this_action"] is False


def test_mcp_server_startup_does_not_start_guardian(monkeypatch):
    thread = Mock(side_effect=AssertionError("watcher started eagerly"))
    monkeypatch.setattr(mcp_server.threading, "Thread", thread)
    monkeypatch.setattr(mcp_server, "_stdio_loop", Mock())
    monkeypatch.setattr(mcp_server, "_head", lambda _repo: "base000")

    mcp_server.main(
        [
            "--repo",
            "/repo",
            "--baseline-commit",
            "base000",
            "--no-llm",
        ]
    )

    thread.assert_not_called()
    assert mcp_server._cache["started"] is False
    assert mcp_server._cache["cycle_no"] == 0


def test_baseline_poll_does_not_run_a_guardian_cycle(monkeypatch):
    run_cycle = Mock()
    monkeypatch.setattr(mcp_server, "_head", lambda _repo: "base000")
    monkeypatch.setattr(mcp_server, "run_cycle", run_cycle)
    config = SimpleNamespace(repo_path="/repo")

    observed = mcp_server._poll_once(config, "base000")

    assert observed == "base000"
    assert mcp_server._cache["cycle_no"] == 0
    assert mcp_server._cache["observed_head"] == "base000"
    run_cycle.assert_not_called()


def test_first_post_baseline_commit_is_cycle_one(monkeypatch):
    report = SimpleNamespace(findings=[])
    run_cycle = Mock(return_value=report)
    monkeypatch.setattr(mcp_server, "_head", lambda _repo: "agent001")
    monkeypatch.setattr(mcp_server, "run_cycle", run_cycle)
    config = SimpleNamespace(repo_path="/repo")

    observed = mcp_server._poll_once(config, "base000")

    assert observed == "agent001"
    assert mcp_server._cache["commit"] == "agent001"
    assert mcp_server._cache["cycle_no"] == 1
    assert mcp_server._cache["running"] is False
    run_cycle.assert_called_once_with(config)
