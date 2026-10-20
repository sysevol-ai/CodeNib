# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Preserve native Verified stdout before its strict UTF-8 decoder runs.

The pinned executor, timeout handling, test parser and scorer remain unchanged.
Only its Docker stream is teed to raw bytes and incrementally normalized UTF-8.
One isolated grading process owns the temporary module binding. Binding the
wrapper is its linearization point; restoration and drained stream captures are
the completion boundary. The native caller still owns container cleanup.
"""

from __future__ import annotations

import codecs
import hashlib
import inspect
import json
import threading
from contextlib import contextmanager
from pathlib import Path

EXECUTOR_SHA256 = "20ba635c83d1c5b75cdfb303e8378c4ce82bee003bf7b63e374efeaacc155b21"


def _write_json(target, value):
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(target)


class _Proxy:
    def __init__(self, target, **overrides):
        self._target = target
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(self._target, name)


class _Capture:
    def __init__(self, directory):
        self.directory = directory
        directory.mkdir(exist_ok=False)
        self.started = False
        self.closed = threading.Event()
        self.record = {"state": "not_started", "stream_exhausted": False}
        self.save()

    def save(self):
        _write_json(self.directory / "capture.json", self.record)

    def stream(self, api, *args, **kwargs):
        if kwargs.get("stream") is not True or self.started:
            raise ValueError("Native stdout capture requires one streaming exec")
        self.started = True
        raw_hash, normalized_hash = hashlib.sha256(), hashlib.sha256()
        raw_count = normalized_count = 0
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.record["state"] = "streaming"
        self.save()
        try:
            with (
                (self.directory / "stdout.bin").open("xb") as raw,
                (self.directory / "stdout.utf8").open("xb") as normalized,
            ):
                for chunk in api.exec_start(*args, **kwargs):
                    if not isinstance(chunk, bytes):
                        raise TypeError("Native exec yielded a non-byte chunk")
                    raw.write(chunk)
                    raw_hash.update(chunk)
                    raw_count += len(chunk)
                    converted = decoder.decode(chunk, final=False).encode("utf-8")
                    normalized.write(converted)
                    normalized_hash.update(converted)
                    normalized_count += len(converted)
                    if converted:
                        yield converted
                converted = decoder.decode(b"", final=True).encode("utf-8")
                normalized.write(converted)
                normalized_hash.update(converted)
                normalized_count += len(converted)
                if converted:
                    yield converted
            self.record.update(state="complete", stream_exhausted=True)
        except BaseException as error:
            self.record.update(state="error", error_class=type(error).__name__)
            raise
        finally:
            self.record.update(
                raw_bytes=raw_count,
                raw_sha256=raw_hash.hexdigest(),
                normalized_utf8_bytes=normalized_count,
                normalized_utf8_sha256=normalized_hash.hexdigest(),
                normalization_changed_bytes=raw_hash.digest()
                != normalized_hash.digest(),
            )
            try:
                self.save()
            finally:
                self.closed.set()


@contextmanager
def captured_verified_output(
    run_evaluation,
    directory,
    *,
    executor_sha256=EXECUTOR_SHA256,
    drain_seconds=5,
):
    """Tee one process's native execs without rerunning a failed command.

    Incremental decoding retains valid multibyte characters across Docker chunks.
    Invalid sequences become U+FFFD only in the text view; raw bytes remain exact.
    The original executor returns its own timeout flag and duration. The scope must
    surround run_instance, including its container cleanup, before checking drainage.
    """
    original = run_evaluation.exec_run_with_timeout
    if getattr(original, "_codenib_stdout_capture", False):
        raise RuntimeError("Native stdout capture is already active")
    source = inspect.getsourcefile(original)
    if source is None or hashlib.sha256(Path(source).read_bytes()).hexdigest() != (
        executor_sha256
    ):
        raise ValueError("Native stdout executor differs from its pinned source")
    if not 0 <= drain_seconds <= 30:
        raise ValueError("Native stdout drain wait must be between 0 and 30 seconds")
    directory.mkdir(exist_ok=False)
    captures = []
    scope = {"state": "active", "executor_source_sha256": executor_sha256}
    _write_json(directory / "scope.json", scope)

    def execute(container, *args, **kwargs):
        capture = _Capture(directory / f"call-{len(captures) + 1:04d}")
        captures.append(capture)
        api = container.client.api
        proxy_api = _Proxy(
            api, exec_start=lambda *a, **kw: capture.stream(api, *a, **kw)
        )
        proxy = _Proxy(container, client=_Proxy(container.client, api=proxy_api))
        return original(proxy, *args, **kwargs)

    execute._codenib_stdout_capture = True
    run_evaluation.exec_run_with_timeout = execute
    try:
        yield
    finally:
        run_evaluation.exec_run_with_timeout = original
        live = [
            c.directory.name
            for c in captures
            if c.started and not c.closed.wait(drain_seconds)
        ]
        scope.update(
            state="undrained" if live else "closed",
            module_binding_restored=True,
            captures=len(captures),
            undrained_captures=live,
        )
        _write_json(directory / "scope.json", scope)
        if live:
            raise RuntimeError("Native stdout stream remains live after cleanup")
