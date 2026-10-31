# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Retain one process owner until bounded shutdown and pipe cleanup finish."""

from __future__ import annotations

import io
import json
import os
import select
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

import codenib.graph.incremental.lsp_client as lsp_module
from codenib.graph.incremental.lsp_client import LSPClient

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX stdio transport")


class _RetainedProcess:
    def __init__(self):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()
        self.returncode = None
        self.waits = []
        self.kills = 0

    def poll(self):
        return self.returncode

    def kill(self):
        self.kills += 1

    def wait(self, timeout=None):
        self.waits.append(timeout)
        assert timeout is not None, "teardown must not wait without a deadline"
        if self.returncode is None:
            raise subprocess.TimeoutExpired("owned-server", timeout)
        return self.returncode


def _broken_request(*_args, **_kwargs):
    raise ValueError("original initialize failure")


def test_unreaped_server_stays_owned_for_explicit_cleanup_retry(tmp_path, monkeypatch):
    client = LSPClient(["unused"], str(tmp_path), "python")
    process = _RetainedProcess()
    client.process = process
    client._initialize_result_received = True
    client._opened_files.add("file:///owned.py")
    monkeypatch.setattr(client, "_request", _broken_request)

    with pytest.raises(subprocess.TimeoutExpired):
        client.shutdown()

    assert client.process is process
    assert client._opened_files == {"file:///owned.py"}
    assert process.kills == 1
    assert all(timeout is not None for timeout in process.waits)
    process.returncode = -9
    client.shutdown()
    assert client.process is None
    assert not client._opened_files
    assert all(
        stream.closed for stream in (process.stdin, process.stdout, process.stderr)
    )


def test_start_rejects_replacing_a_retained_process(tmp_path, monkeypatch):
    client = LSPClient(["unused"], str(tmp_path), "python")
    process = _RetainedProcess()
    client.process = process
    client._read_buffer = b"retained previous reply"

    def unexpected_spawn(*_args, **_kwargs):
        pytest.fail("start replaced a process before its owner was released")

    monkeypatch.setattr(lsp_module, "_lsp_process_env", lambda *_: {})
    monkeypatch.setattr(
        lsp_module,
        "subprocess",
        SimpleNamespace(Popen=unexpected_spawn, PIPE=subprocess.PIPE),
    )
    with pytest.raises(RuntimeError, match="shutdown"):
        client.start(skip_probe=True)

    assert client.process is process
    assert client._read_buffer == b"retained previous reply"


def test_failed_start_reaps_its_actual_server_before_raising(tmp_path, monkeypatch):
    created = []

    def spawn(*args, **kwargs):
        process = subprocess.Popen(*args, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(lsp_module, "_lsp_process_env", lambda *_: {})
    monkeypatch.setattr(
        lsp_module,
        "subprocess",
        SimpleNamespace(
            Popen=spawn, PIPE=subprocess.PIPE, TimeoutExpired=subprocess.TimeoutExpired
        ),
    )
    client = LSPClient(
        [sys.executable, "-c", "import time; time.sleep(60)"], str(tmp_path), "python"
    )
    monkeypatch.setattr(lsp_module, "_SHUTDOWN_EXIT_SECONDS", 0.03, raising=False)
    monkeypatch.setattr(client, "_request", _broken_request)
    try:
        with pytest.raises(ValueError, match="original initialize failure"):
            client.start(skip_probe=True)
        assert created and created[0].poll() is not None
        assert client.process is None
        assert all(
            stream.closed
            for stream in (created[0].stdin, created[0].stdout, created[0].stderr)
        )
    finally:
        for process in created:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)


def test_failed_start_keeps_primary_error_and_incomplete_cleanup_owner(
    tmp_path, monkeypatch
):
    process = _RetainedProcess()
    monkeypatch.setattr(lsp_module, "_lsp_process_env", lambda *_: {})
    monkeypatch.setattr(
        lsp_module,
        "subprocess",
        SimpleNamespace(
            Popen=lambda *_a, **_kw: process,
            PIPE=subprocess.PIPE,
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
    )
    client = LSPClient(["unused"], str(tmp_path), "python")
    monkeypatch.setattr(client, "_request", _broken_request)

    with pytest.raises(ValueError, match="original initialize failure") as exc:
        client.start(skip_probe=True)

    assert isinstance(exc.value.__cause__, subprocess.TimeoutExpired)
    assert exc.value.lsp_cleanup_owner is client
    assert client.process is process
    process.returncode = -9
    client.shutdown()


def test_shutdown_bypasses_request_backpressure_and_keeps_null_reply_valid(
    tmp_path, monkeypatch
):
    process = _RetainedProcess()
    client = LSPClient(["unused"], str(tmp_path), "python")
    client.process = process
    client._initialize_result_received = True
    client._max_pending = 1
    client._pending_ids.add(1)
    client._next_id = 2
    raw = json.dumps({"jsonrpc": "2.0", "id": 2, "result": None}).encode()
    client._read_buffer = f"Content-Length: {len(raw)}\r\n\r\n".encode() + raw
    sent = []
    monkeypatch.setattr(client, "_send", lambda message: sent.append(message))

    def unexpected_drain():
        raise AssertionError("teardown entered ordinary request backpressure")

    monkeypatch.setattr(client, "_drain_pending", unexpected_drain)

    def exited(timeout=None):
        assert timeout is not None
        process.returncode = 0
        return 0

    process.wait = exited
    client.shutdown()

    assert [message["method"] for message in sent] == ["shutdown", "exit"]
    assert process.kills == 0
    assert client.process is None
    assert all(
        stream.closed for stream in (process.stdin, process.stdout, process.stderr)
    )


def test_null_initialize_result_does_not_publish_a_ready_server(tmp_path, monkeypatch):
    process = _RetainedProcess()
    monkeypatch.setattr(lsp_module, "_lsp_process_env", lambda *_: {})
    monkeypatch.setattr(
        lsp_module,
        "subprocess",
        SimpleNamespace(
            Popen=lambda *_a, **_kw: process,
            PIPE=subprocess.PIPE,
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
    )
    client = LSPClient(["unused"], str(tmp_path), "python")
    notifications = []
    requests = []

    def no_result(method, *_args, **_kwargs):
        requests.append(method)
        return None

    monkeypatch.setattr(client, "_request", no_result)
    monkeypatch.setattr(
        client, "_notify", lambda method, *_: notifications.append(method)
    )

    def exited(timeout=None):
        assert timeout is not None
        process.returncode = 0
        return 0

    process.wait = exited
    try:
        with pytest.raises(RuntimeError, match="initialize"):
            client.start(skip_probe=True)
        assert "initialized" not in notifications
        assert requests == ["initialize"]
        assert client.process is None
        assert all(
            stream.closed for stream in (process.stdin, process.stdout, process.stderr)
        )
    finally:
        process.returncode = 0
        client.shutdown()


def _short_shutdown(monkeypatch):
    # The regression can run against base, where these constants do not exist.
    for name, timeout in (
        ("_SHUTDOWN_GRACE_SECONDS", 0.03),
        ("_SHUTDOWN_EXIT_SECONDS", 0.5),
        ("_SHUTDOWN_KILL_SECONDS", 0.5),
    ):
        monkeypatch.setattr(lsp_module, name, timeout, raising=False)


def test_unread_full_stdin_pipe_cannot_block_shutdown(tmp_path, monkeypatch):
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            "import time; print('ready', flush=True); time.sleep(60)",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    client = LSPClient(["unused"], str(tmp_path), "python")
    client.process = process
    client._initialize_result_received = True
    watchdog = []
    previous = signal.getsignal(signal.SIGALRM)

    def interrupt_blocked_write(*_args):
        watchdog.append(True)
        raise TimeoutError("test watchdog interrupted a blocking shutdown write")

    try:
        assert select.select([process.stdout], [], [], 2)[0]
        assert process.stdout.readline() == b"ready\n"
        fd = process.stdin.fileno()
        os.set_blocking(fd, False)
        try:
            while True:
                os.write(fd, b"x" * 4096)
        except BlockingIOError:
            pass
        finally:
            os.set_blocking(fd, True)
        _short_shutdown(monkeypatch)
        signal.signal(signal.SIGALRM, interrupt_blocked_write)
        signal.setitimer(signal.ITIMER_REAL, 2)
        client.shutdown()
        assert not watchdog, "the shutdown deadline did not bound its pipe write"
        assert process.poll() is not None
        assert client.process is None
        assert all(
            stream.closed for stream in (process.stdin, process.stdout, process.stderr)
        )
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)


def test_real_server_receives_shutdown_then_exit_and_releases_pipes(
    tmp_path, monkeypatch
):
    output = tmp_path / "methods.json"
    server = """
import json, sys
methods = []
while True:
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if line in (b'\\r\\n', b'\\n', b''):
            break
        name, value = line.decode().split(':', 1)
        headers[name.lower()] = value.strip()
    request = json.loads(sys.stdin.buffer.read(int(headers['content-length'])))
    methods.append(request['method'])
    if request['method'] == 'shutdown':
        raw = json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': None}).encode()
        sys.stdout.buffer.write(f'Content-Length: {len(raw)}\\r\\n\\r\\n'.encode() + raw)
        sys.stdout.buffer.flush()
    elif request['method'] == 'exit':
        with open(sys.argv[1], 'w') as stream:
            json.dump(methods, stream)
        break
"""
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", server, str(output)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    client = LSPClient(["unused"], str(tmp_path), "python")
    client.process = process
    client._initialize_result_received = True
    monkeypatch.setattr(lsp_module, "_SHUTDOWN_GRACE_SECONDS", 2.0, raising=False)
    try:
        client.shutdown()
        assert process.returncode == 0
        assert json.loads(output.read_text()) == ["shutdown", "exit"]
        assert client.process is None
        assert all(
            stream.closed for stream in (process.stdin, process.stdout, process.stderr)
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
