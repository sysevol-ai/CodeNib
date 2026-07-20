# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

import json
import os
import time
from types import SimpleNamespace

from codeminer.graph.incremental.lsp_client import (
    LSPClient,
    is_lsp_document_unavailable_error,
)


def _frame(payload):
    body = json.dumps(payload).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def test_reader_preserves_coalesced_json_rpc_frames(tmp_path):
    read_fd, write_fd = os.pipe()
    stdout = os.fdopen(read_fd, "rb")
    client = LSPClient(["unused"], str(tmp_path), "python")
    client.process = SimpleNamespace(stdout=stdout)
    try:
        os.write(
            write_fd,
            _frame({"jsonrpc": "2.0", "id": 1, "result": {"first": True}})
            + _frame({"jsonrpc": "2.0", "id": 2, "result": {"second": True}}),
        )

        assert client._read_message(timeout=0.1)["id"] == 1
        assert client._read_message(timeout=0.1)["id"] == 2
    finally:
        os.close(write_fd)
        stdout.close()


def test_notification_drain_consumes_progress_after_buffered_response(tmp_path):
    read_fd, write_fd = os.pipe()
    stdout = os.fdopen(read_fd, "rb")
    client = LSPClient(["unused"], str(tmp_path), "python")
    client.process = SimpleNamespace(stdout=stdout)
    client._active_progress["workspace"] = time.monotonic()
    try:
        os.write(
            write_fd,
            _frame({"jsonrpc": "2.0", "id": 1, "result": {}})
            + _frame(
                {
                    "jsonrpc": "2.0",
                    "method": "$/progress",
                    "params": {
                        "token": "workspace",
                        "value": {"kind": "end"},
                    },
                }
            ),
        )

        assert client._read_message(timeout=0.1)["id"] == 1
        assert "workspace" in client._active_progress
        client.drain_notifications(timeout_s=0.1)
        assert "workspace" not in client._active_progress
    finally:
        os.close(write_fd)
        stdout.close()


def test_sync_document_sends_versioned_full_content_change(tmp_path):
    source = tmp_path / "main.go"
    source.write_text("package main\n")
    client = LSPClient(["unused"], str(tmp_path), "go")
    notifications = []
    client._notify = lambda method, params: notifications.append((method, params))

    client.sync_document("main.go")
    source.write_text("package main\n\nfunc main() {}\n")
    client.sync_document("main.go")

    assert [method for method, _ in notifications] == [
        "textDocument/didOpen",
        "textDocument/didChange",
    ]
    assert notifications[0][1]["textDocument"]["version"] == 1
    change = notifications[1][1]
    assert change["textDocument"]["version"] == 2
    assert change["contentChanges"] == [{"text": source.read_text()}]


def test_close_document_resets_version(tmp_path):
    source = tmp_path / "main.go"
    source.write_text("package main\n")
    client = LSPClient(["unused"], str(tmp_path), "go")
    notifications = []
    client._notify = lambda method, params: notifications.append((method, params))

    client.sync_document("main.go")
    client.close_document("main.go")
    client.sync_document("main.go")

    assert [method for method, _ in notifications] == [
        "textDocument/didOpen",
        "textDocument/didClose",
        "textDocument/didOpen",
    ]
    assert notifications[-1][1]["textDocument"]["version"] == 1


def test_references_uses_client_policy_and_does_not_retry_null(tmp_path):
    source = tmp_path / "main.py"
    source.write_text("value = 1\n")
    client = LSPClient(["unused"], str(tmp_path), "python")
    client.reference_timeout_s = 42.0
    client.reference_retries = 3
    client.open_document = lambda _path: None
    calls = []

    def request(method, params, timeout):
        calls.append((method, timeout))
        client._last_error = {"code": -2, "message": "null result"}
        return None

    client._request = request

    assert client.references(str(source), 0, 0) == []
    assert calls == [("textDocument/references", 42.0)]
    assert client.last_error == {"code": -2, "message": "null result"}


def test_request_batch_preserves_input_order_with_bounded_window(tmp_path):
    client = LSPClient(["unused"], str(tmp_path), "python")
    events = []
    responses = [
        {"jsonrpc": "2.0", "id": 2, "result": ["second"]},
        {"jsonrpc": "2.0", "id": 1, "result": ["first"]},
        {"jsonrpc": "2.0", "id": 3, "result": ["third"]},
    ]

    client._send = lambda message: events.append(("send", message["id"]))

    def read_message(timeout):
        events.append(("read", None))
        return responses.pop(0)

    client._read_message = read_message

    results = client._request_batch(
        "example/method",
        [{"value": 1}, {"value": 2}, {"value": 3}],
        max_in_flight=2,
    )

    assert [result for result, _error, _elapsed in results] == [
        ["first"],
        ["second"],
        ["third"],
    ]
    assert events[:3] == [("send", 1), ("send", 2), ("read", None)]
    assert ("send", 3) in events


def test_references_batch_retries_timeout_but_not_null(tmp_path):
    source = tmp_path / "main.py"
    source.write_text("value = 1\n")
    client = LSPClient(["unused"], str(tmp_path), "python")
    client.reference_retries = 1
    client.open_document = lambda _path: None
    batches = []

    def request_batch(method, params, timeout, max_in_flight):
        batches.append(len(params))
        if len(batches) == 1:
            return [
                (None, {"code": -1, "message": "timeout"}, 1.0),
                (None, {"code": -2, "message": "null result"}, 0.1),
            ]
        return [([{"uri": source.as_uri()}], None, 0.2)]

    client._request_batch = request_batch

    outcomes = client.references_batch(
        [(str(source), 0, 0), (str(source), 0, 1)], max_in_flight=4
    )

    assert batches == [2, 1]
    assert outcomes[0][0] == [{"uri": source.as_uri()}]
    assert outcomes[0][1] is None
    assert outcomes[1] == ([], {"code": -2, "message": "null result"})
    assert client.last_error is None


def test_document_symbols_retry_timeout_with_client_policy(tmp_path):
    source = tmp_path / "main.py"
    source.write_text("def run():\n    pass\n")
    client = LSPClient(["unused"], str(tmp_path), "python")
    client.operation_retries = 1
    client.open_document = lambda _path: None
    calls = 0

    def request(method, params, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            client._last_error = {"code": -1, "message": "timeout"}
            return None
        client._last_error = None
        return [{"name": "run"}]

    client._request = request

    assert client.document_symbol(str(source), retry_delay=0) == [{"name": "run"}]
    assert calls == 2


def test_semantic_tokens_raise_after_strict_retry_failure(tmp_path):
    source = tmp_path / "main.py"
    source.write_text("value = 1\n")
    client = LSPClient(["unused"], str(tmp_path), "python")
    client.operation_retries = 1
    client.strict_request_failures = True
    client.open_document = lambda _path: None
    client._request = lambda *args, **kwargs: None
    client._last_error = {"code": -1, "message": "timeout"}

    try:
        client.semantic_tokens_full(str(source))
    except RuntimeError as exc:
        assert "semanticTokens/full failed" in str(exc)
    else:
        raise AssertionError("strict semantic-token failure should raise")


def test_document_unavailable_error_is_a_strict_empty_result(tmp_path):
    error = {
        "code": 0,
        "message": "no package metadata for file file:///repo/fuzz.go",
    }
    assert is_lsp_document_unavailable_error(error)
    assert not is_lsp_document_unavailable_error({"code": -1, "message": "timeout"})

    client = LSPClient(["unused"], str(tmp_path), "go")
    client.strict_request_failures = True
    client._last_error = error
    client._raise_if_strict_failure("textDocument/references", "fuzz.go:1:1")


def test_readiness_probe_does_not_require_nonempty_references(tmp_path):
    source = tmp_path / "main.go"
    source.write_text("package main\n")
    client = LSPClient(["unused"], str(tmp_path), "go")
    client.open_document = lambda _path: None
    calls = []

    def request(method, params, timeout):
        calls.append((method, timeout))
        client._last_error = None
        return []

    client._request = request

    client._wait_for_ready()

    assert calls == [("textDocument/documentSymbol", 60)]


def test_analysis_probe_rejects_fast_request_failure(tmp_path):
    source = tmp_path / "main.py"
    source.write_text("value = 1\n")
    client = LSPClient(["unused"], str(tmp_path), "python")

    def request(*_args, **_kwargs):
        client._last_error = {"code": -32603, "message": "server error"}
        return None

    client._request = request

    assert not client.wait_for_analysis(str(source), max_wait=0.01, poll_interval=0.001)


def test_analysis_probe_accepts_fast_null_hover(tmp_path):
    source = tmp_path / "main.py"
    source.write_text("value = 1\n")
    client = LSPClient(["unused"], str(tmp_path), "python")

    def request(*_args, **_kwargs):
        client._last_error = {"code": -2, "message": "null result"}
        return None

    client._request = request

    assert client.wait_for_analysis(str(source), max_wait=0.01)
