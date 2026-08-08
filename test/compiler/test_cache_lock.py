# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import errno
import multiprocessing
import os
import select
import signal
import socket
import stat
import sys
import threading
import time
import warnings
from contextlib import contextmanager
from pathlib import Path

import pytest

from codenib.compiler import cache_lock as lock_module
from codenib.compiler.cache_lock import (
    COMPILER_CACHE_LOCK_FILENAME,
    compiler_cache_lock,
)
from codenib.compiler.index_builders import IndexBuilderRegistry
from codenib.compiler.index_compiler import IndexCompiler, IndexCompilerConfig


def _acquire_in_spawned_process(
    cache_dir: str,
    started: multiprocessing.synchronize.Event,
    acquired: multiprocessing.synchronize.Event,
) -> None:
    started.set()
    with compiler_cache_lock(cache_dir):
        acquired.set()


def _fork_during_post_open_pause_then_crash(cache_dir: str, report) -> None:
    """Exercise the open/register fork boundary in a disposable parent."""

    warnings.filterwarnings(
        "ignore",
        message="This process .* is multi-threaded.*",
        category=DeprecationWarning,
    )
    post_open = threading.Event()
    resume_open = threading.Event()
    fork_waiting = threading.Event()
    fork_returned = threading.Event()
    lock_held = threading.Event()
    child_ready_read, child_ready_write = os.pipe()
    child_pids: list[int] = []
    failures: list[BaseException] = []
    real_validate = lock_module._validate_posix_lock_entry
    validation_calls = 0

    def pause_first_post_open_validation(*args, **kwargs):
        nonlocal validation_calls
        identity = real_validate(*args, **kwargs)
        validation_calls += 1
        if validation_calls == 1:
            post_open.set()
            if not resume_open.wait(timeout=10):
                raise TimeoutError("post-open validation was not resumed")
        return identity

    real_guard = lock_module._POSIX_LOCK_LIFECYCLE_GUARD

    class ObservedGuard:
        def acquire(self, *args, **kwargs):
            if threading.current_thread().name == "forker":
                fork_waiting.set()
            return real_guard.acquire(*args, **kwargs)

        def release(self) -> None:
            real_guard.release()

        def _is_owned(self) -> bool:
            return real_guard._is_owned()

    lock_module._validate_posix_lock_entry = pause_first_post_open_validation
    lock_module._POSIX_LOCK_LIFECYCLE_GUARD = ObservedGuard()

    def hold_lock() -> None:
        try:
            with compiler_cache_lock(cache_dir):
                lock_held.set()
                threading.Event().wait(timeout=30)
        except Exception as exc:  # pragma: no cover - reported to parent
            failures.append(exc)

    def fork_child() -> None:
        try:
            if not post_open.wait(timeout=10):
                raise TimeoutError("lock descriptor was not opened")
            child_pid = os.fork()
            if child_pid == 0:  # pragma: no cover - disposable fork child
                os.close(child_ready_read)
                os.write(child_ready_write, b"1")
                os.close(child_ready_write)
                while True:
                    time.sleep(60)
            child_pids.append(child_pid)
            os.close(child_ready_write)
            fork_returned.set()
        except Exception as exc:  # pragma: no cover - reported to parent
            failures.append(exc)

    try:
        holder = threading.Thread(target=hold_lock, name="holder", daemon=True)
        holder.start()
        if not post_open.wait(timeout=10):
            raise TimeoutError("holder did not reach the post-open pause")
        forker = threading.Thread(target=fork_child, name="forker", daemon=True)
        forker.start()
        if not fork_waiting.wait(timeout=10):
            raise TimeoutError("fork callback did not wait on the lifecycle guard")
        resume_open.set()
        if not fork_returned.wait(timeout=10):
            raise TimeoutError("fork did not return after registration")
        if not lock_held.wait(timeout=10):
            raise TimeoutError("parent did not acquire the compiler cache lock")
        if _read_pipe_byte(child_ready_read, 10) != b"1":
            raise TimeoutError("fork child did not remain alive")
        if failures:
            raise RuntimeError(repr(failures[0]))
        report.send(("ready", child_pids[0]))
        report.close()
        # Simulate the lock-owning parent disappearing without Python cleanup.
        os._exit(0)
    except Exception as exc:  # pragma: no cover - asserted by outer process
        try:
            report.send(("error", repr(exc)))
            report.close()
        finally:
            os._exit(3)


def _metadata_identity(path: Path) -> tuple[int, ...]:
    metadata = path.lstat()
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_rdev,
    )


def _install_unsafe_entry(
    cache: Path,
    root: Path,
    kind: str,
) -> tuple[Path | None, socket.socket | None]:
    lock_path = cache / COMPILER_CACHE_LOCK_FILENAME
    victim: Path | None = None
    bound_socket: socket.socket | None = None
    if kind in {"hardlink", "symlink"}:
        victim = root / f"{kind}-victim.txt"
        victim.write_bytes(b"victim must stay intact\n")
        if kind == "hardlink":
            os.link(victim, lock_path)
        else:
            lock_path.symlink_to(victim)
    elif kind == "fifo":
        os.mkfifo(lock_path)
    elif kind == "socket":
        bound_socket = socket.socket(socket.AF_UNIX)
        bound_socket.bind(str(lock_path))
    else:  # pragma: no cover - test helper guard
        raise AssertionError(f"unknown unsafe lock kind: {kind}")
    return victim, bound_socket


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX special files")
@pytest.mark.parametrize("kind", ["hardlink", "symlink", "fifo", "socket"])
def test_rejects_unsafe_lock_without_mutating_victim(
    tmp_path: Path,
    kind: str,
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    victim, bound_socket = _install_unsafe_entry(cache, tmp_path, kind)
    lock_path = cache / COMPILER_CACHE_LOCK_FILENAME
    lock_before = _metadata_identity(lock_path)
    victim_before = victim.read_bytes() if victim is not None else None

    try:
        with pytest.raises(RuntimeError, match="compiler cache lock"):
            with compiler_cache_lock(cache):
                raise AssertionError("unreachable")
    finally:
        if bound_socket is not None:
            bound_socket.close()

    assert _metadata_identity(lock_path) == lock_before
    if victim is not None:
        assert victim.read_bytes() == victim_before


@pytest.mark.skipif(os.name != "posix", reason="requires a POSIX device node")
def test_rejects_device_without_opening_or_mutating_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = Path("/dev/null")
    if not device.exists():
        pytest.skip("/dev/null is unavailable")
    before = _metadata_identity(device)
    real_open = lock_module.os.open

    def reject_device_open(path, *args, **kwargs):
        if os.fspath(path) == "null":
            raise AssertionError("device lock entry must not be opened")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(lock_module.os, "open", reject_device_open)
    monkeypatch.setattr(lock_module, "COMPILER_CACHE_LOCK_FILENAME", "null")
    lock_module.os.supports_dir_fd.add(reject_device_open)
    try:
        with pytest.raises(RuntimeError, match="not a regular file"):
            with compiler_cache_lock("/dev"):
                raise AssertionError("unreachable")
    finally:
        lock_module.os.supports_dir_fd.discard(reject_device_open)

    assert _metadata_identity(device) == before


def test_never_truncates_existing_regular_file(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    lock_path = cache / COMPILER_CACHE_LOCK_FILENAME
    contents = b"diagnostic contents must survive"
    lock_path.write_bytes(contents)

    with compiler_cache_lock(cache):
        assert lock_path.read_bytes() == contents

    assert lock_path.read_bytes() == contents


@pytest.mark.skipif(os.name != "posix", reason="checks POSIX open flags")
def test_posix_lock_is_read_write_without_truncate() -> None:
    flags = lock_module._posix_lock_flags(create=False)

    assert flags & os.O_ACCMODE == os.O_RDWR
    assert flags & os.O_TRUNC == 0
    assert flags & os.O_NOFOLLOW


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Linux uses O_PATH for execute-only directory traversal",
)
def test_linux_directory_open_uses_o_path_through_search_only_ancestor(
    tmp_path: Path,
) -> None:
    ancestor = tmp_path / "search-only"
    cache = ancestor / "cache"
    cache.mkdir(parents=True)
    os.chmod(ancestor, 0o111)
    try:
        assert lock_module._directory_open_flags() & os.O_PATH
        with compiler_cache_lock(cache):
            pass
    finally:
        os.chmod(ancestor, 0o700)


@pytest.mark.skipif(os.name != "posix", reason="checks POSIX creation modes")
def test_creates_bounded_entries_without_chmodding_existing_parent(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "existing"
    existing.mkdir(mode=0o775)
    os.chmod(existing, 0o775)
    cache = existing / "new-parent" / "cache"

    with compiler_cache_lock(cache):
        pass

    assert stat.S_IMODE(existing.stat().st_mode) == 0o775
    assert stat.S_IMODE((existing / "new-parent").stat().st_mode) & 0o022 == 0
    assert stat.S_IMODE(cache.stat().st_mode) & 0o022 == 0
    lock_mode = stat.S_IMODE((cache / COMPILER_CACHE_LOCK_FILENAME).stat().st_mode)
    assert lock_mode & 0o177 == 0


def test_serializes_threads(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    started = threading.Event()
    acquired = threading.Event()
    errors: list[BaseException] = []

    def contender() -> None:
        started.set()
        try:
            with compiler_cache_lock(cache):
                acquired.set()
        except Exception as exc:  # pragma: no cover - asserted in parent
            errors.append(exc)

    with compiler_cache_lock(cache):
        thread = threading.Thread(target=contender)
        thread.start()
        assert started.wait(timeout=5)
        assert not acquired.wait(timeout=0.25)

    assert acquired.wait(timeout=5)
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []


@pytest.mark.skipif(
    os.name not in {"posix", "nt"},
    reason="requires a supported process-locking platform",
)
def test_serializes_spawned_processes(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    context = multiprocessing.get_context("spawn")
    started = context.Event()
    acquired = context.Event()
    process = context.Process(
        target=_acquire_in_spawned_process,
        args=(str(cache), started, acquired),
    )

    try:
        with compiler_cache_lock(cache):
            process.start()
            assert started.wait(timeout=5)
            assert not acquired.wait(timeout=0.25)

        assert acquired.wait(timeout=5)
        process.join(timeout=5)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)


class _AbortLockBody(BaseException):
    pass


def test_base_exception_releases_lock_for_reacquisition(tmp_path: Path) -> None:
    cache = tmp_path / "cache"

    with pytest.raises(_AbortLockBody):
        with compiler_cache_lock(cache):
            raise _AbortLockBody

    with compiler_cache_lock(cache):
        pass


@pytest.mark.skipif(
    not Path("/proc/self/fd").is_dir(),
    reason="requires procfs descriptor accounting",
)
def test_repeated_acquisition_does_not_leak_descriptors(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    before = len(list(Path("/proc/self/fd").iterdir()))

    for _attempt in range(64):
        with compiler_cache_lock(cache):
            pass

    after = len(list(Path("/proc/self/fd").iterdir()))
    assert after == before
    assert lock_module._ACTIVE_POSIX_LOCK_DESCRIPTORS == set()


@pytest.mark.skipif(
    os.name != "posix" or not Path("/proc/self/fd").is_dir(),
    reason="requires POSIX procfs descriptor accounting",
)
def test_getpid_keyboard_interrupt_cannot_leak_a_lock_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = len(list(Path("/proc/self/fd").iterdir()))

    def interrupt_getpid() -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(lock_module.os, "getpid", interrupt_getpid)
    with compiler_cache_lock(tmp_path / "cache"):
        pass

    after = len(list(Path("/proc/self/fd").iterdir()))
    assert after == before
    assert lock_module._ACTIVE_POSIX_LOCK_DESCRIPTORS == set()


@pytest.mark.skipif(
    os.name != "posix" or not Path("/proc/self/fd").is_dir(),
    reason="requires POSIX procfs descriptor accounting",
)
def test_registry_add_keyboard_interrupt_closes_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InterruptingRegistry(set[int]):
        def add(self, descriptor: int) -> None:
            super().add(descriptor)
            raise KeyboardInterrupt

    registry = InterruptingRegistry()
    monkeypatch.setattr(lock_module, "_ACTIVE_POSIX_LOCK_DESCRIPTORS", registry)
    before = len(list(Path("/proc/self/fd").iterdir()))

    with pytest.raises(KeyboardInterrupt):
        with compiler_cache_lock(tmp_path / "cache"):
            raise AssertionError("unreachable")

    after = len(list(Path("/proc/self/fd").iterdir()))
    assert after == before
    assert registry == set()
    assert lock_module._POSIX_LOCK_LIFECYCLE_GUARD.acquire(timeout=1)
    lock_module._POSIX_LOCK_LIFECYCLE_GUARD.release()


@pytest.mark.skipif(
    os.name != "posix" or not Path("/proc/self/fd").is_dir(),
    reason="requires POSIX procfs descriptor accounting",
)
def test_post_open_keyboard_interrupt_closes_owned_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_open = lock_module._open_posix_lock_file
    before = len(list(Path("/proc/self/fd").iterdir()))

    def interrupt_after_open(*args, **kwargs):
        real_open(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(lock_module, "_open_posix_lock_file", interrupt_after_open)
    with pytest.raises(KeyboardInterrupt):
        with compiler_cache_lock(tmp_path / "cache"):
            raise AssertionError("unreachable")

    assert len(list(Path("/proc/self/fd").iterdir())) == before
    assert lock_module._ACTIVE_POSIX_LOCK_DESCRIPTORS == set()


@pytest.mark.skipif(
    os.name != "posix" or not Path("/proc/self/fd").is_dir(),
    reason="requires POSIX procfs descriptor accounting",
)
@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_cleanup_guard_interruption_is_deferred_until_descriptor_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: type[BaseException],
) -> None:
    real_guard = lock_module._PosixLifecycleRLock()

    class InterruptSecondAcquire:
        def __init__(self) -> None:
            self.calls = 0

        def acquire(self, *args, **kwargs):
            self.calls += 1
            acquired = real_guard.acquire(*args, **kwargs)
            if self.calls == 2:
                raise interruption()
            return acquired

        def release(self) -> None:
            real_guard.release()

        def _is_owned(self) -> bool:
            return real_guard._is_owned()

    guard = InterruptSecondAcquire()
    monkeypatch.setattr(lock_module, "_POSIX_LOCK_LIFECYCLE_GUARD", guard)
    before = len(list(Path("/proc/self/fd").iterdir()))

    with pytest.raises(interruption):
        with compiler_cache_lock(tmp_path / "cache"):
            pass

    assert guard.calls == 2
    assert len(list(Path("/proc/self/fd").iterdir())) == before
    assert lock_module._ACTIVE_POSIX_LOCK_DESCRIPTORS == set()
    assert real_guard.acquire(timeout=1)
    real_guard.release()


@pytest.mark.skipif(
    os.name != "posix" or not Path("/proc/self/fd").is_dir(),
    reason="requires POSIX signals and procfs descriptor accounting",
)
def test_contended_cleanup_defers_sigint_until_descriptor_is_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_guard = lock_module._PosixLifecycleRLock()
    observe_cleanup = threading.Event()
    cleanup_waiting = threading.Event()
    holder_ready = threading.Event()
    release_holder = threading.Event()
    failures: list[BaseException] = []

    class ObservedGuard:
        def acquire(self, *args, **kwargs):
            if (
                observe_cleanup.is_set()
                and threading.current_thread() is threading.main_thread()
            ):
                cleanup_waiting.set()
            return real_guard.acquire(*args, **kwargs)

        def release(self) -> None:
            real_guard.release()

        def _is_owned(self) -> bool:
            return real_guard._is_owned()

    monkeypatch.setattr(lock_module, "_POSIX_LOCK_LIFECYCLE_GUARD", ObservedGuard())
    before = len(list(Path("/proc/self/fd").iterdir()))

    def hold_lifecycle_guard() -> None:
        real_guard.acquire()
        holder_ready.set()
        try:
            if not release_holder.wait(timeout=10):
                failures.append(TimeoutError("cleanup guard holder was not released"))
        finally:
            try:
                real_guard.release()
            except RuntimeError as exc:  # pragma: no cover - asserted below
                failures.append(exc)

    def interrupt_cleanup() -> None:
        if not cleanup_waiting.wait(timeout=10):
            failures.append(TimeoutError("cleanup did not wait on lifecycle guard"))
            release_holder.set()
            return
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(0.05)
        release_holder.set()

    holder = threading.Thread(target=hold_lifecycle_guard, daemon=True)
    interrupter = threading.Thread(target=interrupt_cleanup, daemon=True)
    try:
        with pytest.raises(KeyboardInterrupt):
            with compiler_cache_lock(tmp_path / "cache"):
                holder.start()
                assert holder_ready.wait(timeout=10)
                observe_cleanup.set()
                interrupter.start()
    finally:
        release_holder.set()
        holder.join(timeout=10)
        interrupter.join(timeout=10)

    assert not failures
    assert not holder.is_alive()
    assert not interrupter.is_alive()
    assert len(list(Path("/proc/self/fd").iterdir())) == before
    assert lock_module._ACTIVE_POSIX_LOCK_DESCRIPTORS == set()
    with compiler_cache_lock(tmp_path / "cache"):
        pass


@pytest.mark.skipif(os.name != "posix", reason="exercises POSIX error mapping")
def test_posix_open_oserror_preserves_runtime_error_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    real_open = lock_module.os.open

    with lock_module._open_posix_cache_directory(cache) as (
        opened_cache,
        directory_descriptor,
    ):

        def deny_lock_open(path, *args, **kwargs):
            if os.fspath(path) == COMPILER_CACHE_LOCK_FILENAME:
                raise PermissionError("denied")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(lock_module.os, "open", deny_lock_open)
        with pytest.raises(
            RuntimeError,
            match="could not open compiler cache lock",
        ) as captured:
            lock_module._open_posix_lock_file(
                opened_cache,
                directory_descriptor,
                [],
            )

    assert isinstance(captured.value.__cause__, PermissionError)
    assert lock_module._ACTIVE_POSIX_LOCK_DESCRIPTORS == set()


@pytest.mark.skipif(
    os.name != "posix" or not Path("/proc/self/fd").is_dir(),
    reason="requires POSIX procfs descriptor accounting",
)
def test_unlock_keyboard_interrupt_still_closes_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert lock_module.fcntl is not None
    real_flock = lock_module.fcntl.flock
    before = len(list(Path("/proc/self/fd").iterdir()))

    def interrupt_unlock(descriptor: int, operation: int) -> None:
        if operation == lock_module.fcntl.LOCK_UN:
            raise KeyboardInterrupt
        real_flock(descriptor, operation)

    monkeypatch.setattr(lock_module.fcntl, "flock", interrupt_unlock)
    with pytest.raises(KeyboardInterrupt):
        with compiler_cache_lock(tmp_path / "cache"):
            pass

    assert len(list(Path("/proc/self/fd").iterdir())) == before
    assert lock_module._ACTIVE_POSIX_LOCK_DESCRIPTORS == set()
    monkeypatch.setattr(lock_module.fcntl, "flock", real_flock)
    with compiler_cache_lock(tmp_path / "cache"):
        pass


@pytest.mark.skipif(os.name != "posix", reason="exercises POSIX lifecycle cleanup")
def test_close_remains_registered_and_guarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert lock_module.fcntl is not None
    real_flock = lock_module.fcntl.flock
    real_close = lock_module.os.close
    lock_descriptor: int | None = None

    def track_lock(descriptor: int, operation: int) -> None:
        nonlocal lock_descriptor
        if operation == lock_module.fcntl.LOCK_EX:
            lock_descriptor = descriptor
        real_flock(descriptor, operation)

    def check_close(descriptor: int) -> None:
        if descriptor == lock_descriptor:
            assert descriptor in lock_module._ACTIVE_POSIX_LOCK_DESCRIPTORS
            assert lock_module._posix_lifecycle_guard_is_owned()
        real_close(descriptor)

    monkeypatch.setattr(lock_module.fcntl, "flock", track_lock)
    monkeypatch.setattr(lock_module.os, "close", check_close)

    with compiler_cache_lock(tmp_path / "cache"):
        pass

    assert lock_descriptor is not None
    assert lock_module._ACTIVE_POSIX_LOCK_DESCRIPTORS == set()


@pytest.mark.skipif(
    os.name != "posix" or not Path("/proc/self/fd").is_dir(),
    reason="requires POSIX procfs descriptor accounting",
)
def test_close_keyboard_interrupt_is_deferred_and_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_close = lock_module.os.close
    interrupted = False
    lock_descriptor: int | None = None
    assert lock_module.fcntl is not None
    real_flock = lock_module.fcntl.flock
    before = len(list(Path("/proc/self/fd").iterdir()))

    def track_lock(descriptor: int, operation: int) -> None:
        nonlocal lock_descriptor
        if operation == lock_module.fcntl.LOCK_EX:
            lock_descriptor = descriptor
        real_flock(descriptor, operation)

    def interrupt_first_lock_close(descriptor: int) -> None:
        nonlocal interrupted
        if descriptor == lock_descriptor and not interrupted:
            interrupted = True
            real_close(descriptor)
            raise KeyboardInterrupt
        real_close(descriptor)

    monkeypatch.setattr(lock_module.fcntl, "flock", track_lock)
    monkeypatch.setattr(lock_module.os, "close", interrupt_first_lock_close)
    with pytest.raises(KeyboardInterrupt):
        with compiler_cache_lock(tmp_path / "cache"):
            pass

    assert interrupted
    assert len(list(Path("/proc/self/fd").iterdir())) == before
    assert lock_module._ACTIVE_POSIX_LOCK_DESCRIPTORS == set()


@pytest.mark.skipif(
    os.name != "posix" or not Path("/proc/self/fd").is_dir(),
    reason="requires POSIX procfs descriptor accounting",
)
def test_owner_pop_after_successful_interrupt_is_idempotent(tmp_path: Path) -> None:
    class InterruptAfterPop(list[int]):
        interrupted = False

        def pop(self, *args):
            descriptor = super().pop(*args)
            if not self.interrupted:
                self.interrupted = True
                raise KeyboardInterrupt
            return descriptor

    lock_path = tmp_path / "owned.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    owner = InterruptAfterPop([descriptor])
    registry = {descriptor}

    deferred = lock_module._close_owned_descriptor_for_cleanup(
        descriptor,
        owner,
        None,
        active_registry=registry,
    )
    assert deferred is not None
    with pytest.raises(KeyboardInterrupt):
        raise deferred

    assert owner == []
    assert registry == set()
    with pytest.raises(OSError) as captured:
        os.fstat(descriptor)
    assert captured.value.errno == errno.EBADF
    replacement = os.open(lock_path, os.O_RDWR)
    os.close(replacement)


def _read_pipe_byte(descriptor: int, timeout: float) -> bytes:
    readable, _, _ = select.select([descriptor], [], [], timeout)
    return os.read(descriptor, 1) if readable else b""


@pytest.mark.skipif(
    os.name != "posix" or not Path("/proc/self/fd").is_dir(),
    reason="requires POSIX fork and procfs descriptor accounting",
)
def test_open_guard_acquire_then_interrupt_preserves_prior_recursion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_guard = lock_module._PosixLifecycleRLock()
    acquired_by_thread = threading.Event()

    class InterruptSecondAcquire:
        def __init__(self) -> None:
            self.calls = 0

        def acquire(self, *args, **kwargs):
            self.calls += 1
            acquired = real_guard.acquire(*args, **kwargs)
            if self.calls == 2:
                raise KeyboardInterrupt
            return acquired

        def release(self) -> None:
            real_guard.release()

        def _is_owned(self) -> bool:
            return real_guard._is_owned()

    guard = InterruptSecondAcquire()
    monkeypatch.setattr(lock_module, "_POSIX_LOCK_LIFECYCLE_GUARD", guard)
    before = len(list(Path("/proc/self/fd").iterdir()))
    guard.acquire()
    try:
        with pytest.raises(KeyboardInterrupt):
            with compiler_cache_lock(tmp_path / "cache"):
                raise AssertionError("unreachable")
        assert lock_module._posix_lifecycle_guard_depth() == 1
        assert lock_module._posix_lifecycle_guard_is_owned()
    finally:
        guard.release()

    def acquire_after_interrupt() -> None:
        with compiler_cache_lock(tmp_path / "cache"):
            acquired_by_thread.set()

    contender = threading.Thread(target=acquire_after_interrupt)
    contender.start()
    contender.join(timeout=10)
    assert not contender.is_alive()
    assert acquired_by_thread.is_set()

    child_pid = os.fork()
    if child_pid == 0:  # pragma: no cover - disposable fork child
        os._exit(0)
    waited_pid, status = os.waitpid(child_pid, 0)
    assert waited_pid == child_pid
    assert os.waitstatus_to_exitcode(status) == 0
    assert not lock_module._posix_lifecycle_guard_is_owned()
    assert len(list(Path("/proc/self/fd").iterdir())) == before


@pytest.mark.skipif(
    not hasattr(os, "fork") or not hasattr(os, "register_at_fork"),
    reason="requires POSIX fork callbacks",
)
def test_atfork_acquire_then_keyboard_interrupt_releases_owned_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_guard = lock_module._PosixLifecycleRLock()
    interrupted = False
    child_report_read, child_report_write = os.pipe()

    class InterruptAfterAcquire:
        def acquire(self, *args, **kwargs):
            nonlocal interrupted
            acquired = real_guard.acquire(*args, **kwargs)
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            return acquired

        def release(self) -> None:
            real_guard.release()

        def _is_owned(self) -> bool:
            return real_guard._is_owned()

    monkeypatch.setattr(
        lock_module,
        "_POSIX_LOCK_LIFECYCLE_GUARD",
        InterruptAfterAcquire(),
    )
    child_pid: int | None = None
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="This process .* is multi-threaded.*",
                category=DeprecationWarning,
            )
            child_pid = os.fork()
        if child_pid == 0:  # pragma: no cover - disposable fork child
            os.close(child_report_read)
            child_ok = not lock_module._posix_lifecycle_guard_is_owned()
            os.write(child_report_write, b"1" if child_ok else b"0")
            os.close(child_report_write)
            os._exit(0 if child_ok else 3)

        os.close(child_report_write)
        assert _read_pipe_byte(child_report_read, 10) == b"1"
        waited_pid, status = os.waitpid(child_pid, 0)
        child_pid = None
        assert waited_pid > 0
        assert os.waitstatus_to_exitcode(status) == 0
    finally:
        try:
            os.close(child_report_read)
        except OSError:
            pass
        try:
            os.close(child_report_write)
        except OSError:
            pass
        if child_pid not in (None, 0):
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(child_pid, 0)

    assert interrupted
    assert not lock_module._posix_lifecycle_guard_is_owned()
    assert real_guard.acquire(timeout=1)
    real_guard.release()


@pytest.mark.skipif(
    not hasattr(os, "fork") or not hasattr(os, "register_at_fork"),
    reason="requires POSIX fork callbacks",
)
def test_atfork_trace_interrupt_after_success_cannot_escape_callback() -> None:
    acquire_code = lock_module._acquire_posix_lifecycle_guard.__code__
    injected = False
    previous_trace = sys.gettrace()
    child_pid: int | None = None

    def interrupt_after_depth_change(frame, event, _arg):
        nonlocal injected
        if (
            not injected
            and event == "line"
            and frame.f_code is acquire_code
            and "baseline_depth" in frame.f_locals
            and lock_module._posix_lifecycle_guard_depth()
            > frame.f_locals["baseline_depth"]
        ):
            injected = True
            raise KeyboardInterrupt
        return interrupt_after_depth_change

    try:
        sys.settrace(interrupt_after_depth_change)
        child_pid = os.fork()
        sys.settrace(previous_trace)
        if child_pid == 0:  # pragma: no cover - disposable fork child
            child_ok = not lock_module._posix_lifecycle_guard_is_owned()
            os._exit(0 if child_ok else 3)
        waited_pid, status = os.waitpid(child_pid, 0)
        child_pid = None
        assert waited_pid > 0
        assert os.waitstatus_to_exitcode(status) == 0
    finally:
        sys.settrace(previous_trace)
        if child_pid not in (None, 0):
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(child_pid, 0)

    assert injected
    assert not lock_module._posix_lifecycle_guard_is_owned()


@pytest.mark.skipif(
    not hasattr(os, "fork") or not hasattr(os, "register_at_fork"),
    reason="requires POSIX fork callbacks",
)
def test_reentrant_atfork_releases_only_its_recursion_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_guard = lock_module._PosixLifecycleRLock()

    class InterruptSecondAcquire:
        def __init__(self) -> None:
            self.calls = 0

        def acquire(self, *args, **kwargs):
            self.calls += 1
            acquired = real_guard.acquire(*args, **kwargs)
            if self.calls == 2:
                raise KeyboardInterrupt
            return acquired

        def release(self) -> None:
            real_guard.release()

        def _is_owned(self) -> bool:
            return real_guard._is_owned()

    guard = InterruptSecondAcquire()
    monkeypatch.setattr(lock_module, "_POSIX_LOCK_LIFECYCLE_GUARD", guard)
    real_validate = lock_module._validate_posix_lock_entry
    child_report_read, child_report_write = os.pipe()
    child_pid: int | None = None
    forked = False

    def fork_during_validation(descriptor: int, *args, **kwargs):
        nonlocal child_pid, forked
        identity = real_validate(descriptor, *args, **kwargs)
        if forked:
            return identity
        forked = True
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="This process .* is multi-threaded.*",
                category=DeprecationWarning,
            )
            child_pid = os.fork()
        if child_pid == 0:  # pragma: no cover - disposable fork child
            os.close(child_report_read)
            try:
                os.fstat(descriptor)
            except OSError as exc:
                descriptor_closed = exc.errno == errno.EBADF
            else:
                descriptor_closed = False
            owned_before_release = lock_module._posix_lifecycle_guard_is_owned()
            guard.release()
            owned_after_release = lock_module._posix_lifecycle_guard_is_owned()
            child_ok = (
                descriptor_closed
                and owned_before_release
                and not owned_after_release
                and not lock_module._ACTIVE_POSIX_LOCK_DESCRIPTORS
            )
            os.write(child_report_write, b"1" if child_ok else b"0")
            os.close(child_report_write)
            os._exit(0 if child_ok else 3)
        return identity

    monkeypatch.setattr(
        lock_module,
        "_validate_posix_lock_entry",
        fork_during_validation,
    )
    try:
        with compiler_cache_lock(tmp_path / "cache"):
            pass
        os.close(child_report_write)
        assert _read_pipe_byte(child_report_read, 10) == b"1"
        assert child_pid not in (None, 0)
        waited_pid, status = os.waitpid(child_pid, 0)
        child_pid = None
        assert waited_pid > 0
        assert os.waitstatus_to_exitcode(status) == 0
    finally:
        try:
            os.close(child_report_read)
        except OSError:
            pass
        try:
            os.close(child_report_write)
        except OSError:
            pass
        if child_pid not in (None, 0):
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(child_pid, 0)

    assert not lock_module._posix_lifecycle_guard_is_owned()
    assert guard.calls == 3
    assert lock_module._ACTIVE_POSIX_LOCK_DESCRIPTORS == set()


@pytest.mark.skipif(
    not hasattr(os, "fork") or not hasattr(os, "register_at_fork"),
    reason="requires POSIX fork callbacks",
)
def test_contended_atfork_guard_survives_sigint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_guard = lock_module._PosixLifecycleRLock()
    fork_waiting = threading.Event()
    holder_ready = threading.Event()
    release_holder = threading.Event()
    failures: list[BaseException] = []

    class ObservedGuard:
        def acquire(self, *args, **kwargs):
            if threading.current_thread() is threading.main_thread():
                fork_waiting.set()
            return real_guard.acquire(*args, **kwargs)

        def release(self) -> None:
            real_guard.release()

        def _is_owned(self) -> bool:
            return real_guard._is_owned()

    monkeypatch.setattr(lock_module, "_POSIX_LOCK_LIFECYCLE_GUARD", ObservedGuard())

    def hold_lifecycle_guard() -> None:
        real_guard.acquire()
        holder_ready.set()
        try:
            if not release_holder.wait(timeout=10):
                failures.append(TimeoutError("fork guard holder was not released"))
        finally:
            try:
                real_guard.release()
            except RuntimeError as exc:  # pragma: no cover - asserted below
                failures.append(exc)

    def interrupt_fork() -> None:
        if not fork_waiting.wait(timeout=10):
            failures.append(TimeoutError("fork did not wait on lifecycle guard"))
            release_holder.set()
            return
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(0.05)
        release_holder.set()

    holder = threading.Thread(target=hold_lifecycle_guard, daemon=True)
    interrupter = threading.Thread(target=interrupt_fork, daemon=True)
    child_pid: int | None = None
    try:
        holder.start()
        assert holder_ready.wait(timeout=10)
        interrupter.start()
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="This process .* is multi-threaded.*",
                category=DeprecationWarning,
            )
            child_pid = os.fork()
        if child_pid == 0:  # pragma: no cover - disposable fork child
            os._exit(0)
        waited_pid, status = os.waitpid(child_pid, 0)
        child_pid = None
        assert waited_pid > 0
        assert os.waitstatus_to_exitcode(status) == 0
    finally:
        release_holder.set()
        holder.join(timeout=10)
        interrupter.join(timeout=10)
        if child_pid not in (None, 0):
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(child_pid, 0)

    assert not failures
    assert not holder.is_alive()
    assert not interrupter.is_alive()
    assert not lock_module._posix_lifecycle_guard_is_owned()
    assert real_guard.acquire(timeout=1)
    real_guard.release()


@pytest.mark.skipif(
    not hasattr(os, "fork") or not hasattr(os, "register_at_fork"),
    reason="requires POSIX fork callbacks",
)
def test_fork_during_open_cannot_pin_lock_after_parent_crash(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    receive_report, send_report = context.Pipe(duplex=False)
    coordinator = context.Process(
        target=_fork_during_post_open_pause_then_crash,
        args=(str(tmp_path / "cache"), send_report),
    )
    child_pid: int | None = None
    contender = None
    try:
        coordinator.start()
        send_report.close()
        assert receive_report.poll(20)
        status, payload = receive_report.recv()
        assert status == "ready", payload
        child_pid = payload
        os.kill(child_pid, 0)

        started = context.Event()
        acquired = context.Event()
        contender = context.Process(
            target=_acquire_in_spawned_process,
            args=(str(tmp_path / "cache"), started, acquired),
        )
        contender.start()
        assert started.wait(timeout=5)
        assert acquired.wait(timeout=5)
        contender.join(timeout=5)
        assert contender.exitcode == 0
        os.kill(child_pid, signal.SIGKILL)
        child_pid = None
        coordinator.join(timeout=5)
        assert coordinator.exitcode == 0
    finally:
        receive_report.close()
        if coordinator.is_alive():
            coordinator.terminate()
            coordinator.join(timeout=5)
        if contender is not None and contender.is_alive():
            contender.terminate()
            contender.join(timeout=5)
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(
    not hasattr(os, "fork") or not hasattr(os, "register_at_fork"),
    reason="requires POSIX fork callbacks",
)
@pytest.mark.filterwarnings(
    "ignore:This process .* is multi-threaded.*:DeprecationWarning"
)
def test_forked_child_drops_inherited_lock_before_reacquiring(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    ready_read, ready_write = os.pipe()
    acquired_read, acquired_write = os.pipe()
    child_pid: int | None = None
    try:
        with compiler_cache_lock(cache):
            child_pid = os.fork()
            if child_pid == 0:  # pragma: no cover - assertions run in parent
                os.close(ready_read)
                os.close(acquired_read)
                try:
                    os.write(ready_write, b"1")
                    with compiler_cache_lock(cache):
                        os.write(acquired_write, b"1")
                except Exception:
                    os._exit(2)
                os._exit(0)

            os.close(ready_write)
            os.close(acquired_write)
            assert _read_pipe_byte(ready_read, 5) == b"1"
            assert _read_pipe_byte(acquired_read, 0.25) == b""

        assert _read_pipe_byte(acquired_read, 5) == b"1"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            waited, status = os.waitpid(child_pid, os.WNOHANG)
            if waited == child_pid:
                assert os.waitstatus_to_exitcode(status) == 0
                child_pid = None
                break
            time.sleep(0.01)
        assert child_pid is None
    finally:
        for descriptor in (ready_read, acquired_read):
            try:
                os.close(descriptor)
            except OSError:
                pass
        if child_pid is not None:
            os.kill(child_pid, signal.SIGKILL)
            os.waitpid(child_pid, 0)


@pytest.mark.skipif(os.name != "posix", reason="exercises POSIX identity binding")
def test_rejects_lock_entry_swap_before_entering_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    lock_path = cache / COMPILER_CACHE_LOCK_FILENAME
    lock_path.write_bytes(b"original")
    replacement = cache / "replacement"
    replacement.write_bytes(b"replacement")
    stolen = cache / "stolen"
    real_validate = lock_module._validate_posix_lock_entry
    calls = 0

    def swap_after_first_validation(*args, **kwargs):
        nonlocal calls
        identity = real_validate(*args, **kwargs)
        calls += 1
        if calls == 1:
            os.replace(lock_path, stolen)
            os.replace(replacement, lock_path)
        return identity

    monkeypatch.setattr(
        lock_module,
        "_validate_posix_lock_entry",
        swap_after_first_validation,
    )
    entered = False

    with pytest.raises(RuntimeError, match="compiler cache lock changed"):
        with compiler_cache_lock(cache):
            entered = True

    assert entered is False
    assert stolen.read_bytes() == b"original"
    assert lock_path.read_bytes() == b"replacement"


@pytest.mark.skipif(os.name != "posix", reason="exercises POSIX identity binding")
def test_does_not_revalidate_paths_after_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body_finished = False
    real_validate = lock_module._validate_posix_lock_entry

    def reject_post_body_validation(*args, **kwargs):
        assert body_finished is False
        return real_validate(*args, **kwargs)

    monkeypatch.setattr(
        lock_module,
        "_validate_posix_lock_entry",
        reject_post_body_validation,
    )

    with compiler_cache_lock(tmp_path / "cache"):
        body_finished = True


def test_index_compiler_does_not_create_cache_before_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.compiler import index_compiler as compiler_module

    repo = tmp_path / "repo"
    repo.mkdir()
    cache = tmp_path / "cache"
    observed_absent = False

    @contextmanager
    def tracking_lock(cache_dir):
        nonlocal observed_absent
        assert Path(cache_dir) == cache
        observed_absent = not cache.exists()
        cache.mkdir()
        yield

    monkeypatch.setattr(compiler_module, "compiler_cache_lock", tracking_lock)
    compiler = IndexCompiler(
        IndexBuilderRegistry(),
        IndexCompilerConfig(index_types=[]),
    )

    compiler.compile_repo(str(repo), cache_dir=str(cache))

    assert observed_absent is True


@pytest.mark.parametrize("kind", ["hardlink", "symlink"])
def test_windows_static_link_checks_preserve_victims(
    tmp_path: Path,
    kind: str,
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    victim = tmp_path / "victim"
    victim.write_bytes(b"preserve")
    lock_path = cache / COMPILER_CACHE_LOCK_FILENAME
    if kind == "hardlink":
        os.link(victim, lock_path)
    else:
        try:
            lock_path.symlink_to(victim)
        except OSError as exc:
            pytest.skip(f"cannot create a symbolic link: {exc}")
    before = victim.read_bytes()

    with pytest.raises(RuntimeError, match="compiler cache lock"):
        lock_module._open_windows_lock_file(cache)

    assert victim.read_bytes() == before


def test_windows_open_preserves_existing_contents(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    lock_path = cache / COMPILER_CACHE_LOCK_FILENAME
    contents = b"diagnostic contents must survive"
    lock_path.write_bytes(contents)

    descriptor, _identity = lock_module._open_windows_lock_file(cache)
    try:
        assert os.get_inheritable(descriptor) is False
        assert lock_path.read_bytes() == contents
    finally:
        os.close(descriptor)

    assert lock_path.read_bytes() == contents


def test_windows_lock_retries_contention_and_unlocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    class FakeMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2

        @staticmethod
        def locking(_descriptor: int, mode: int, _byte_count: int) -> None:
            calls.append(mode)
            if mode == FakeMsvcrt.LK_NBLCK and calls.count(mode) < 3:
                raise OSError(errno.EACCES, "contended")

    monkeypatch.setattr(lock_module, "msvcrt", FakeMsvcrt)
    monkeypatch.setattr(lock_module.time, "sleep", sleeps.append)
    lock_path = tmp_path / "lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        lock_module._acquire_windows_lock(descriptor)
        lock_module._release_windows_lock(descriptor)
    finally:
        os.close(descriptor)

    assert calls == [1, 1, 1, 2]
    assert sleeps == [
        lock_module._WINDOWS_LOCK_RETRY_SECONDS,
        lock_module._WINDOWS_LOCK_RETRY_SECONDS,
    ]
    assert lock_path.read_bytes() == b""


@pytest.mark.skipif(
    not Path("/proc/self/fd").is_dir(),
    reason="requires procfs descriptor accounting",
)
def test_windows_post_open_keyboard_interrupt_closes_owned_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_open = lock_module._open_windows_lock_file
    before = len(list(Path("/proc/self/fd").iterdir()))

    def interrupt_after_open(*args, **kwargs):
        real_open(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(lock_module, "msvcrt", object())
    monkeypatch.setattr(lock_module, "_open_windows_lock_file", interrupt_after_open)
    with pytest.raises(KeyboardInterrupt):
        with lock_module._windows_compiler_cache_lock(tmp_path / "cache"):
            raise AssertionError("unreachable")

    assert len(list(Path("/proc/self/fd").iterdir())) == before


@pytest.mark.skipif(
    not Path("/proc/self/fd").is_dir(),
    reason="requires procfs descriptor accounting",
)
def test_windows_owner_survives_interrupt_before_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_open = lock_module._open_windows_lock_file
    captured_owner: list[int] | None = None
    released_descriptor: int | None = None
    before = len(list(Path("/proc/self/fd").iterdir()))

    def capture_owner(cache: Path, owner: list[int]):
        nonlocal captured_owner
        captured_owner = owner
        return real_open(cache, owner)

    def acquire_lock(_descriptor: int) -> None:
        return None

    def interrupt_release(descriptor: int) -> None:
        nonlocal released_descriptor
        released_descriptor = descriptor
        assert captured_owner == [descriptor]
        raise KeyboardInterrupt

    monkeypatch.setattr(lock_module, "msvcrt", object())
    monkeypatch.setattr(lock_module, "_open_windows_lock_file", capture_owner)
    monkeypatch.setattr(lock_module, "_acquire_windows_lock", acquire_lock)
    monkeypatch.setattr(lock_module, "_release_windows_lock", interrupt_release)

    with pytest.raises(KeyboardInterrupt):
        with lock_module._windows_compiler_cache_lock(tmp_path / "cache"):
            pass

    assert released_descriptor is not None
    assert captured_owner == []
    assert len(list(Path("/proc/self/fd").iterdir())) == before


@pytest.mark.skipif(
    not Path("/proc/self/fd").is_dir(),
    reason="requires procfs descriptor accounting",
)
def test_windows_close_keyboard_interrupt_is_deferred_and_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_close = lock_module.os.close
    lock_descriptor: int | None = None
    interrupted = False
    before = len(list(Path("/proc/self/fd").iterdir()))

    def acquire_lock(descriptor: int) -> None:
        nonlocal lock_descriptor
        lock_descriptor = descriptor

    def interrupt_first_lock_close(descriptor: int) -> None:
        nonlocal interrupted
        if descriptor == lock_descriptor and not interrupted:
            interrupted = True
            real_close(descriptor)
            raise KeyboardInterrupt
        real_close(descriptor)

    monkeypatch.setattr(lock_module, "msvcrt", object())
    monkeypatch.setattr(lock_module, "_acquire_windows_lock", acquire_lock)
    monkeypatch.setattr(lock_module, "_release_windows_lock", lambda _fd: None)
    monkeypatch.setattr(lock_module.os, "close", interrupt_first_lock_close)

    with pytest.raises(KeyboardInterrupt):
        with lock_module._windows_compiler_cache_lock(tmp_path / "cache"):
            pass

    assert interrupted
    assert lock_descriptor is not None
    assert len(list(Path("/proc/self/fd").iterdir())) == before
