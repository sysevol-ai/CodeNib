# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path
from threading import Event

import pytest

from codenib.mcp.graph_warmup import (
    CANCELLED,
    FAILED,
    IDLE,
    READY,
    RUNNING,
    GraphTarget,
    GraphWarmup,
)
from codenib.repository_source_selection import DEFAULT_REPOSITORY_SOURCE_SELECTION


def _target(fingerprint: str = "sha256-v2:one") -> GraphTarget:
    return GraphTarget(
        repository=Path("/repository"),
        languages=("python",),
        source_selection=DEFAULT_REPOSITORY_SOURCE_SELECTION,
        source_fingerprint=fingerprint,
    )


def test_build_runs_in_background_and_reports_its_outcome():
    started, release = Event(), Event()
    builds = []

    def prepare(target):
        builds.append(target.source_fingerprint)
        started.set()
        assert release.wait(timeout=5)

    warmup = GraphWarmup(prepare)
    assert warmup.state == IDLE
    assert warmup.start(_target()) == RUNNING
    assert started.wait(timeout=5)
    # A short wait returns while the build continues, and a running build
    # is never replaced.
    assert warmup.wait(0.01) == RUNNING
    assert warmup.start(_target("sha256-v2:two"), force=True) == RUNNING
    status = warmup.status()
    assert status["state"] == RUNNING and status["error"] is None
    release.set()
    assert warmup.wait(5) == READY
    assert builds == ["sha256-v2:one"]


def test_finished_source_is_not_rebuilt_unless_forced_or_changed():
    builds = []
    warmup = GraphWarmup(lambda target: builds.append(target.source_fingerprint))

    warmup.start(_target())
    assert warmup.wait(5) == READY
    assert warmup.start(_target()) == READY
    assert builds == ["sha256-v2:one"]

    warmup.start(_target("sha256-v2:two"))
    assert warmup.wait(5) == READY
    warmup.start(_target("sha256-v2:two"), force=True)
    assert warmup.wait(5) == READY
    assert builds == ["sha256-v2:one", "sha256-v2:two", "sha256-v2:two"]


def test_failure_is_recorded_once_per_source():
    attempts = []

    def prepare(_target):
        attempts.append(True)
        raise RuntimeError("provider\n  unavailable " + "x" * 1000)

    warmup = GraphWarmup(prepare)
    warmup.start(_target())
    assert warmup.wait(5) == FAILED
    status = warmup.status()
    assert status["error"].startswith("provider unavailable x")
    assert len(status["error"]) <= 400
    assert status["elapsed_seconds"] is not None

    assert warmup.start(_target()) == FAILED
    assert len(attempts) == 1
    warmup.start(_target(), force=True)
    assert warmup.wait(5) == FAILED
    assert len(attempts) == 2


def test_close_reaps_the_compiler_before_releasing_its_lock(monkeypatch, tmp_path):
    import subprocess

    from filelock import FileLock, Timeout

    from codenib import _compilation_process

    started = Event()
    killed = []
    lock_path = tmp_path / "compiler.lock"

    class Process:
        pid = 1234

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def communicate(self, timeout=None):
            if timeout is not None:
                started.set()
                raise subprocess.TimeoutExpired(["compiler"], timeout)
            assert killed
            return None, None

        def poll(self):
            return None

    def kill(_pid, _signal):
        with pytest.raises(Timeout), FileLock(lock_path, timeout=0):
            pass
        killed.append(True)

    monkeypatch.setattr(
        _compilation_process.subprocess, "Popen", lambda *a, **k: Process()
    )
    monkeypatch.setattr(_compilation_process.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(_compilation_process.os, "killpg", kill)

    def prepare(_target):
        with FileLock(lock_path):
            _compilation_process.run_compilation_process(["compiler"], timeout=60)

    warmup = GraphWarmup(prepare)
    warmup.start(_target())
    assert started.wait(5)
    warmup.close()

    assert killed == [True]
    assert warmup.state == CANCELLED and not warmup._thread.is_alive()
    with FileLock(lock_path, timeout=0):
        pass
    warmup.close()
    with pytest.raises(RuntimeError, match="owner is closed"):
        warmup.start(_target())
