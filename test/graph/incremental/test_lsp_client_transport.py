# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Keep complete and fragmented stdio replies across reads and idle drains."""

from __future__ import annotations

import io
import json
import os
import time
from types import SimpleNamespace

import pytest

import codenib.graph.incremental.lsp_client as lsp_module
from codenib.graph.incremental.lsp_client import LSPClient

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="The stdio select transport requires POSIX pipes"
)


def _frame(message):
    body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode() + body


@pytest.fixture
def transport(tmp_path):
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb") as stdout:
        client = LSPClient(["unused"], str(tmp_path), "python")
        client.process = SimpleNamespace(
            stdin=io.BytesIO(), stdout=stdout, poll=lambda: None
        )
        try:
            yield client, lambda raw: os.write(write_fd, raw)
        finally:
            os.close(write_fd)


def test_coalesced_replies_survive_separate_message_reads(transport):
    client, write = transport
    first = {"jsonrpc": "2.0", "id": 7, "result": ["first"]}
    second = {"jsonrpc": "2.0", "id": 8, "result": ["second"]}
    write(_frame(first) + _frame(second))

    assert client._read_message(timeout=0.02) == first
    assert client._read_message(timeout=0.02) == second


@pytest.mark.parametrize("split", [12, -2], ids=["header", "utf8_body"])
def test_partial_reply_survives_read_timeout(transport, split):
    client, write = transport
    message = {"jsonrpc": "2.0", "id": 3, "result": "a😀"}
    raw = _frame(message)
    cut = split if split > 0 else raw.index("😀".encode()) + 2
    write(raw[:cut])

    assert client._read_message(timeout=0.005) is None
    write(raw[cut:])

    assert client._read_message(timeout=0.02) == message


def test_idle_drain_handles_progress_after_an_already_returned_reply(transport):
    client, write = transport
    reply = {"jsonrpc": "2.0", "id": 1, "result": []}
    progress = {
        "jsonrpc": "2.0",
        "method": "$/progress",
        "params": {"token": "index", "value": {"kind": "begin"}},
    }
    write(_frame(reply) + _frame(progress))

    assert client._read_message(timeout=0.02) == reply
    client.drain_notifications(timeout_s=0.02)

    assert "index" in client._active_progress


@pytest.mark.parametrize("method", ["references", "definition"])
def test_position_retry_reuses_a_late_reply_received_during_idle_drain(
    transport, monkeypatch, tmp_path, method
):
    client, write = transport
    (tmp_path / "app.py").write_text("def target(): pass\n")
    locations = [
        {
            "uri": (tmp_path / "app.py").as_uri(),
            "range": {
                "start": {"line": 0, "character": 4},
                "end": {"line": 0, "character": 10},
            },
        }
    ]

    def deliver_late_reply(_delay):
        write(_frame({"jsonrpc": "2.0", "id": 1, "result": locations}))
        client.drain_notifications(timeout_s=0.02)

    monkeypatch.setattr(
        lsp_module,
        "time",
        SimpleNamespace(monotonic=time.monotonic, sleep=deliver_late_reply),
    )

    assert (
        getattr(client, method)("app.py", 0, 4, timeout=0.005, retries=1) == locations
    )
    assert client._last_error is None
    sent = client.process.stdin.getvalue()
    assert sent.count(f'"method": "textDocument/{method}"'.encode()) == 1


def test_pending_drain_preserves_late_reply_and_following_response(
    transport, monkeypatch
):
    client, write = transport
    client._max_pending = 1
    client._pending_ids.add(7)
    client._next_id = 8
    late = {"jsonrpc": "2.0", "id": 7, "result": ["late"]}
    current = {"jsonrpc": "2.0", "id": 8, "result": ["current"]}
    write(_frame(late))
    send = client._send

    def send_and_deliver(message):
        send(message)
        if message.get("id") == 8:
            write(_frame(current))

    monkeypatch.setattr(client, "_send", send_and_deliver)

    assert client._request("workspace/symbol", {"query": "target"}, timeout=0.02) == [
        "current"
    ]
    assert client._response_buffer[7] == late
    assert not client._pending_ids


def test_new_server_does_not_consume_old_transport_state(transport, monkeypatch):
    client, write = transport
    fresh_process = client.process
    stale = {"jsonrpc": "2.0", "id": 1, "result": {"stale": True}}
    client._read_buffer = _frame(stale)
    client._response_buffer[1] = stale
    client._pending_ids.add(99)
    client._active_progress["old-server"] = time.monotonic()
    write(_frame({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}))
    monkeypatch.setattr(
        lsp_module,
        "subprocess",
        SimpleNamespace(
            PIPE=lsp_module.subprocess.PIPE, Popen=lambda *a, **kw: fresh_process
        ),
    )

    assert client.start(skip_probe=True, initialize_timeout=0.02) == {
        "capabilities": {}
    }
    assert not client._active_progress
    assert not client._pending_ids
