"""Native stdout regression: retain bytes while preserving the official executor."""

import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.contextbench_verified_output import captured_verified_output


def strict_executor(container, command, timeout=60):
    assert container.id == "owned-container"
    assert command == "/bin/bash /eval.sh" and timeout == 123
    api = container.client.api
    stream = api.exec_start("owned-exec", stream=True)
    return b"".join(stream).decode(), api.timed_out, 1.25


def fixture(chunks, *, timed_out=False, stream_error=None):
    calls = []

    def stream(exec_id, **kwargs):
        calls.append((exec_id, kwargs))
        yield from chunks
        if stream_error:
            raise stream_error

    api = SimpleNamespace(exec_start=stream, timed_out=timed_out)
    container = SimpleNamespace(id="owned-container", client=SimpleNamespace(api=api))
    module = SimpleNamespace(exec_run_with_timeout=strict_executor)
    return module, container, calls


def capture(module, directory, **kwargs):
    return captured_verified_output(
        module,
        directory,
        executor_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        **kwargs,
    )


@pytest.mark.parametrize(
    "chunks",
    [
        [],
        [b"test_case PASSED\n"],
        [b"\xe4", b"\xb8\xad\n"],
        [b"\xf0\x9f", b"\x98", b"\x80\n"],
        [b"\xc0", b"test_case PASSED\n", b"\xe0"],
        [b"\xef\xbf\xbd", b"\n"],
    ],
)
@pytest.mark.parametrize("timed_out", [False, True])
def test_capture_keeps_raw_bytes_and_original_timing(tmp_path, chunks, timed_out):
    module, container, calls = fixture(chunks, timed_out=timed_out)
    raw = b"".join(chunks)
    with capture(module, tmp_path / "output"):
        result = module.exec_run_with_timeout(container, "/bin/bash /eval.sh", 123)
    assert result == (raw.decode("utf-8", errors="replace"), timed_out, 1.25)
    assert calls == [("owned-exec", {"stream": True})]
    assert module.exec_run_with_timeout is strict_executor
    folder = tmp_path / "output/call-0001"
    assert (folder / "stdout.bin").read_bytes() == raw
    assert (folder / "stdout.utf8").read_text() == result[0]
    proof = json.loads((folder / "capture.json").read_text())
    assert proof["state"] == "complete" and proof["stream_exhausted"]
    assert proof["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert proof["normalization_changed_bytes"] == (raw != result[0].encode())
    assert json.loads((tmp_path / "output/scope.json").read_text())["state"] == "closed"


def test_capture_preserves_partial_output_and_stream_error(tmp_path):
    error = RuntimeError("native transport failure")
    module, container, calls = fixture([b"test_case PASSED\n"], stream_error=error)
    with pytest.raises(RuntimeError, match="native transport failure"):
        with capture(module, tmp_path / "output"):
            module.exec_run_with_timeout(container, "/bin/bash /eval.sh", 123)
    assert len(calls) == 1
    assert module.exec_run_with_timeout is strict_executor
    folder = tmp_path / "output/call-0001"
    assert (folder / "stdout.bin").read_bytes() == b"test_case PASSED\n"
    proof = json.loads((folder / "capture.json").read_text())
    assert proof["state"] == "error" and not proof["stream_exhausted"]


def test_capture_rejects_changed_executor_and_nested_scope(tmp_path):
    module, _, _ = fixture([])
    with pytest.raises(ValueError, match="pinned source"):
        with captured_verified_output(module, tmp_path / "wrong"):
            pytest.fail("changed executor accepted")
    assert not (tmp_path / "wrong").exists()
    with capture(module, tmp_path / "outer"):
        with pytest.raises(RuntimeError, match="already active"):
            with capture(module, tmp_path / "inner"):
                pytest.fail("nested capture accepted")
    assert module.exec_run_with_timeout is strict_executor


def test_capture_rejects_live_timeout_stream_after_cleanup(tmp_path):
    started, release = threading.Event(), threading.Event()
    workers = []
    module, container, _ = fixture([])

    def stream(*args, **kwargs):
        started.set()
        release.wait(5)
        yield b"late native output\n"

    container.client.api.exec_start = stream

    def early_timeout(container, command, timeout=60):
        worker = threading.Thread(
            target=lambda: list(container.client.api.exec_start("exec", stream=True))
        )
        workers.append(worker)
        worker.start()
        assert started.wait(1)
        return "", True, 0.1

    module.exec_run_with_timeout = early_timeout
    try:
        with pytest.raises(RuntimeError, match="remains live"):
            with capture(module, tmp_path / "output", drain_seconds=0):
                module.exec_run_with_timeout(container, "/bin/bash /eval.sh", 123)
        assert module.exec_run_with_timeout is early_timeout
        scope = json.loads((tmp_path / "output/scope.json").read_text())
        assert scope["state"] == "undrained" and scope["module_binding_restored"]
    finally:
        release.set()
        for worker in workers:
            worker.join(1)
            assert not worker.is_alive()
