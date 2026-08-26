# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import errno
import hashlib
import inspect
import os
import select
import signal
import stat
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import codenib._contained_source as contained_source_module
import codenib._windows_fs_authority as windows_fs_module
import codenib.source_fingerprint as source_fingerprint_module
from codenib.artifacts.runtime import SourceBindingCleanupOwner
from codenib.repository_filters import (
    REPOSITORY_FILTER_POLICY_VERSION,
    walk_repository_files,
)
from codenib.repository_source_selection import (
    DEFAULT_REPOSITORY_SOURCE_SELECTION,
    RepositorySourceSelection,
)
from codenib.source_fingerprint import (
    SOURCE_FINGERPRINT_VERSION,
    RepositoryChangedError,
    RepositorySourceBinding,
    RepositorySourceRootAuthority,
    capture_repository_source,
    fingerprint_repository,
    fingerprint_repository_v1_for_diagnostics,
    is_legacy_source_fingerprint_v1,
    is_secure_source_fingerprint_v2,
    pin_repository_source_root,
    repository_source_is_dirty,
    source_fingerprint_version,
)


class _InterruptAfterPopList(list[int]):
    def __init__(self, values: list[int], interruption: BaseException) -> None:
        super().__init__(values)
        self._interruption = interruption
        self._armed = True

    def pop(self, *args):
        value = super().pop(*args)
        if self._armed:
            self._armed = False
            raise self._interruption
        return value


class _AcquireThenInterruptRLock:
    def __init__(self) -> None:
        self.runtime = threading.RLock()
        self.acquire_calls = 0
        self.acquire_interrupted = False
        self.probe_interrupted = False
        self.acquire_failure = KeyboardInterrupt("injected completed acquire")

    def acquire(self, *args, **kwargs):
        self.acquire_calls += 1
        acquired = self.runtime.acquire(*args, **kwargs)
        if not self.acquire_interrupted:
            self.acquire_interrupted = True
            raise self.acquire_failure
        return acquired

    def release(self) -> None:
        self.runtime.release()

    def _is_owned(self) -> bool:
        if self.acquire_interrupted and not self.probe_interrupted:
            self.probe_interrupted = True
            raise SystemExit("injected acquisition recovery cancellation")
        return self.runtime._is_owned()


class _ReleaseThenInterruptRLock:
    def __init__(self) -> None:
        self.runtime = threading.RLock()
        self.release_calls = 0
        self.release_interrupted = False
        self.probe_interrupted = False
        self.release_failure = KeyboardInterrupt("injected completed release")

    def acquire(self, *args, **kwargs):
        return self.runtime.acquire(*args, **kwargs)

    def release(self) -> None:
        self.release_calls += 1
        self.runtime.release()
        if not self.release_interrupted:
            self.release_interrupted = True
            raise self.release_failure

    def _is_owned(self) -> bool:
        if self.release_interrupted and not self.probe_interrupted:
            self.probe_interrupted = True
            raise SystemExit("injected release recovery cancellation")
        return self.runtime._is_owned()


def test_source_lock_double_acquire_cancellation_does_not_add_native_recursion() -> (
    None
):
    lock = source_fingerprint_module._SourceLifecycleRLock()
    runtime = _AcquireThenInterruptRLock()
    lock._lock = runtime

    with pytest.raises(KeyboardInterrupt) as caught:
        with source_fingerprint_module._SourceLockLease(lock) as failure:
            assert failure is runtime.acquire_failure
            raise failure

    assert caught.value is runtime.acquire_failure
    assert runtime.acquire_calls == 1
    assert lock.depth() == 0
    acquired_elsewhere = []

    def acquire_elsewhere() -> None:
        acquired = runtime.runtime.acquire(timeout=1)
        acquired_elsewhere.append(acquired)
        if acquired:
            runtime.runtime.release()

    thread = threading.Thread(target=acquire_elsewhere)
    thread.start()
    thread.join(timeout=2)
    assert acquired_elsewhere == [True]


def test_source_lock_double_release_cancellation_does_not_retry_native_release() -> (
    None
):
    lock = source_fingerprint_module._SourceLifecycleRLock()
    runtime = _ReleaseThenInterruptRLock()
    lock._lock = runtime

    with pytest.raises(KeyboardInterrupt) as caught:
        with source_fingerprint_module._SourceLockLease(lock):
            pass

    assert caught.value is runtime.release_failure
    assert runtime.release_calls == 1
    assert lock.depth() == 0
    assert runtime.runtime.acquire(timeout=1)
    runtime.runtime.release()


def test_source_lock_persistent_probe_failure_is_bounded() -> None:
    class BrokenProbeRLock:
        def acquire(self, *args, **kwargs):
            raise AssertionError("native acquire must wait for a successful probe")

        def release(self) -> None:
            raise AssertionError("unacquired lock must not be released")

        def _is_owned(self) -> bool:
            raise RuntimeError("persistent ownership probe failure")

    lock = source_fingerprint_module._SourceLifecycleRLock()
    lock._lock = BrokenProbeRLock()

    with pytest.raises(RuntimeError, match="persistent ownership probe failure"):
        source_fingerprint_module._SourceLockLease(lock).__enter__()


def test_source_lock_concurrent_child_first_touch_uses_one_process_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = source_fingerprint_module._SourceLifecycleRLock()
    original_rlock = threading.RLock
    construction_barrier = threading.Barrier(2)
    start_barrier = threading.Barrier(3)
    observed = []

    def synchronized_rlock():
        candidate = original_rlock()
        construction_barrier.wait(timeout=2)
        return candidate

    monkeypatch.setattr(
        source_fingerprint_module.os,
        "getpid",
        lambda: lock._pid + 1,
    )
    monkeypatch.setattr(
        source_fingerprint_module.threading,
        "RLock",
        synchronized_rlock,
    )

    def first_touch() -> None:
        start_barrier.wait(timeout=2)
        assert lock.depth() == 0
        observed.append(lock._lock)

    threads = [threading.Thread(target=first_touch) for _ in range(2)]
    for thread in threads:
        thread.start()
    start_barrier.wait(timeout=2)
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert len(observed) == 2
    assert observed[0] is observed[1] is lock._lock


def test_repository_source_binding_reads_exact_captured_records(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_bytes(b"VALUE = 1\n")
    hidden = repo / ".hidden"
    hidden.mkdir()
    (hidden / "target.py").write_bytes(b"TARGET = 2\n")
    (repo / "link.py").symlink_to(".hidden/target.py")
    expected = fingerprint_repository(repo)

    with capture_repository_source(repo) as binding:
        assert binding.fingerprint == expected.value
        assert binding.file_count == expected.file_count
        assert {record.path for record in binding.file_records} == {
            ".hidden/target.py",
            "a.py",
            "link.py",
        }
        assert binding.read_bytes("a.py", max_bytes=1024) == b"VALUE = 1\n"
        assert binding.read_bytes("link.py", max_bytes=1024) == b"TARGET = 2\n"


def test_repository_source_binding_maps_only_captured_paths_and_reads_prefix(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    source = repo / "pkg" / "source.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"prefix-data-that-continues\n")

    with capture_repository_source(repo) as binding:
        assert binding.captured_relative_path("pkg/source.py") == "pkg/source.py"
        assert binding.captured_relative_path(str(source)) == "pkg/source.py"
        assert binding.captured_relative_path("../source.py") is None
        assert binding.captured_relative_path(str(tmp_path / "outside.py")) is None
        assert binding.read_prefix("pkg/source.py", max_bytes=6) == b"prefix"


def test_repository_source_binding_reroots_only_frozen_absolute_suffixes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    for relative in (
        "source.py",
        "pkg/source.py",
        "src/a.py",
        "repo/src/a.py",
        "one/shared.py",
        "two/shared.py",
    ):
        source = repo / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(relative, encoding="utf-8")

    with capture_repository_source(repo) as binding:
        with monkeypatch.context() as no_filesystem:
            no_filesystem.setattr(
                os.path,
                "exists",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("captured path lookup consulted the live filesystem")
                ),
            )
            no_filesystem.setattr(
                Path,
                "exists",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("captured path lookup consulted a Path")
                ),
            )
            observed = (
                binding.captured_relative_path("/old/build/machine/repo/pkg/source.py"),
                binding.captured_relative_path(
                    r"D:\agent\workspace\repo\pkg\source.py"
                ),
                binding.captured_relative_path(str(repo / "src/a.py")),
                binding.captured_relative_path(f"{repo}/pkg/./source.py"),
                binding.captured_relative_path(f"{repo}/pkg//source.py"),
                binding.captured_relative_path(f"{repo}/pkg/../pkg/source.py"),
                binding.captured_relative_path(f"{repo}/../repo/pkg/source.py"),
                binding.captured_relative_path("prefix/pkg/source.py"),
                binding.captured_relative_path("/old/pkg/../pkg/source.py"),
                binding.captured_relative_path(r"C:pkg\source.py"),
                binding.captured_relative_path("/old/build/missing.py"),
                binding.captured_relative_path("/old/build/shared.py"),
                binding.captured_relative_path("pkg/source.py\x00ignored"),
                binding.captured_relative_path("\ud800"),
                binding.captured_relative_path(
                    "/" + "/".join(("part",) * 257) + "/pkg/source.py"
                ),
                binding.captured_relative_path("/" + ("x" * 4096) + "/pkg/source.py"),
            )

        assert observed == (
            "pkg/source.py",
            "pkg/source.py",
            "src/a.py",
            "pkg/source.py",
            "pkg/source.py",
            "pkg/source.py",
            "pkg/source.py",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        with monkeypatch.context() as bounded_label:
            bounded_label.setattr(
                source_fingerprint_module.ntpath,
                "splitdrive",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("oversized source label reached path parsing")
                ),
            )
            bounded_result = binding.captured_relative_path(
                "/" + ("x" * (len(os.fsencode(repo)) + 4097))
            )

        assert bounded_result is None


def test_repository_source_binding_keeps_deep_current_root_absolute_paths(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    relative = Path(*(("d",) * 255), "source.py")
    source = repo / relative
    source.parent.mkdir(parents=True)
    source.write_text("deep", encoding="utf-8")

    with capture_repository_source(repo) as binding:
        expected = relative.as_posix()
        assert len(relative.parts) == 256
        assert binding.captured_relative_path(expected) == expected
        assert binding.captured_relative_path(str(source)) == expected
        assert (
            binding.captured_relative_path(
                "\\\\server\\share\\" + str(relative).replace("/", "\\")
            )
            == expected
        )


def test_repository_source_prefix_authenticates_bytes_beyond_returned_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "source.py"
    source.write_bytes(b"same-prefix: trusted tail\n")
    binding = capture_repository_source(repo)
    source.write_bytes(b"same-prefix: altered tail\n")
    monkeypatch.setattr(
        RepositorySourceBinding,
        "_verify_inventory",
        lambda _binding: None,
    )

    with pytest.raises(RepositoryChangedError):
        binding.read_prefix("source.py", max_bytes=len(b"same-prefix"))

    binding.close()


def test_repository_source_reader_streams_far_line_range_and_is_borrowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "source.py"
    filler = (b"x" * 512) + b"\n"
    source.write_bytes((filler * 20_000) + b"TARGET = 'far-offset'\nTAIL = 1\n")
    binding = capture_repository_source(repo)
    reader = binding.borrow_reader()

    assert reader.file_paths == frozenset({"source.py"})
    assert not hasattr(reader, "close")
    assert (
        reader.read_line_range(
            "source.py",
            start_line=20_001,
            end_line=20_001,
            max_bytes=1024,
        )
        == b"TARGET = 'far-offset'\n"
    )

    current_pid = os.getpid()
    with monkeypatch.context() as process:
        process.setattr(
            source_fingerprint_module.os,
            "getpid",
            lambda: current_pid + 1,
        )
        with pytest.raises(RuntimeError, match="cross processes"):
            reader.captured_relative_path("source.py")

    binding.close()
    with pytest.raises(RuntimeError, match="closed|poisoned"):
        reader.captured_relative_path("source.py")


@pytest.mark.parametrize(
    ("separator", "expected"),
    [
        (b"\n", b"two\n"),
        (b"\r\n", b"two\r\n"),
        (b"\r", b"two\r"),
    ],
)
def test_repository_source_line_range_supports_universal_newlines(
    tmp_path: Path,
    separator: bytes,
    expected: bytes,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "source.py").write_bytes(separator.join((b"one", b"two", b"three")))

    with capture_repository_source(repo) as binding:
        assert (
            binding.read_line_range(
                "source.py",
                start_line=2,
                end_line=2,
                max_bytes=32,
            )
            == expected
        )


def test_authenticated_line_range_handles_crlf_across_chunks() -> None:
    chunks = (b"one\r", b"\ntwo\r", b"three\n")
    authenticated = source_fingerprint_module._AuthenticatedLineRange(2, 2, 32)

    for chunk in chunks:
        authenticated.update(chunk)

    payload = b"".join(chunks)
    assert bytes(authenticated.payload) == b"two\r"
    assert authenticated.byte_count == len(payload)
    assert authenticated.digest.hexdigest() == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize("separator", [b"\n", b"\r"])
def test_authenticated_line_range_scans_dense_newlines_once(
    separator: bytes,
) -> None:
    class NoRepeatedFind(bytes):
        def find(self, *_args, **_kwargs):
            raise AssertionError("universal-newline scan repeated a suffix search")

    line_count = 4096
    payload = NoRepeatedFind((b"x" + separator) * line_count + b"target\r")
    authenticated = source_fingerprint_module._AuthenticatedLineRange(
        line_count + 1,
        line_count + 1,
        32,
    )

    authenticated.update(payload)

    assert bytes(authenticated.payload) == b"target\r"
    assert authenticated.byte_count == len(payload)
    assert authenticated.digest.hexdigest() == hashlib.sha256(payload).hexdigest()


def test_repository_source_line_range_preserves_mixed_and_trailing_cr(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "source.py").write_bytes(b"one\rtwo\r\nthree\nfour\r")

    with capture_repository_source(repo) as binding:
        assert (
            binding.read_line_range(
                "source.py",
                start_line=2,
                end_line=3,
                max_bytes=32,
            )
            == b"two\r\nthree\n"
        )
        assert (
            binding.read_line_range(
                "source.py",
                start_line=4,
                end_line=4,
                max_bytes=32,
            )
            == b"four\r"
        )


def test_repository_source_crlf_limit_does_not_poison_binding(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "source.py").write_bytes(b"one\r\ntwo\r\n")
    binding = capture_repository_source(repo)

    with pytest.raises(ValueError, match="bounded read limit"):
        binding.read_line_range(
            "source.py",
            start_line=1,
            end_line=1,
            max_bytes=4,
        )

    assert binding.usable
    assert (
        binding.read_line_range(
            "source.py",
            start_line=2,
            end_line=2,
            max_bytes=5,
        )
        == b"two\r\n"
    )
    binding.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_repository_source_reader_fork_rejects_before_inherited_lock(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "source.py").write_bytes(b"VALUE = 1\n")
    binding = capture_repository_source(repo)
    reader = binding.borrow_reader()
    locked = threading.Event()
    release = threading.Event()

    def hold_parent_lock() -> None:
        with binding._state_lease():
            locked.set()
            release.wait(timeout=10)

    holder = threading.Thread(target=hold_parent_lock)
    holder.start()
    assert locked.wait(timeout=2)
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no cover - assertions are reported over the pipe
        os.close(read_fd)
        signal.alarm(2)
        outcomes = []
        try:
            reader.captured_relative_path("source.py")
        except BaseException as exc:  # noqa: B036 - report exact boundary
            outcomes.append(f"read:{type(exc).__name__}:{exc}")
        try:
            binding.close()
        except BaseException as exc:  # noqa: B036 - cleanup then report boundary
            outcomes.append(f"close:{type(exc).__name__}:{exc}")
        outcomes.append(f"closed:{binding.closed}")
        os.write(write_fd, "\n".join(outcomes).encode("utf-8"))
        os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    try:
        outcome = os.read(read_fd, 4096).decode("utf-8")
        waited, status = os.waitpid(child, 0)
    finally:
        os.close(read_fd)
        release.set()
        holder.join(timeout=2)
        binding.close()

    assert waited == child
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    assert (
        "read:RuntimeError:repository source binding cannot cross processes" in outcome
    )
    assert (
        "close:RuntimeError:repository source binding cannot cross processes" in outcome
    )
    assert "closed:True" in outcome
    assert not holder.is_alive()


def test_repository_source_line_range_authenticates_unreturned_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "source.py"
    source.write_bytes(b"TARGET = 1\ntrusted tail\n")
    binding = capture_repository_source(repo)
    source.write_bytes(b"TARGET = 1\naltered tail\n")
    monkeypatch.setattr(
        RepositorySourceBinding,
        "_verify_inventory",
        lambda _binding: None,
    )

    with pytest.raises(RepositoryChangedError):
        binding.read_line_range(
            "source.py",
            start_line=1,
            end_line=1,
            max_bytes=1024,
        )

    binding.close()


def test_repository_source_line_range_limit_does_not_poison_binding(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "source.py").write_bytes((b"x" * 128) + b"\nNEXT = 1\n")
    binding = capture_repository_source(repo)

    with pytest.raises(ValueError, match="bounded read limit"):
        binding.read_line_range(
            "source.py",
            start_line=1,
            end_line=1,
            max_bytes=16,
        )

    assert binding.usable
    assert (
        binding.read_line_range(
            "source.py",
            start_line=2,
            end_line=2,
            max_bytes=32,
        )
        == b"NEXT = 1\n"
    )
    binding.close()


def test_repository_source_snapshot_revalidates_excluded_symlink_target(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    hidden = repo / ".codenib-cache"
    hidden.mkdir(parents=True)
    target = hidden / "target.py"
    target.write_bytes(b"VALUE = 1\n")
    link = repo / "visible.py"
    link.symlink_to(".codenib-cache/target.py")
    binding = capture_repository_source(repo)

    target.write_bytes(b"VALUE = 2\n")

    with pytest.raises(RepositoryChangedError):
        binding.verify_snapshot()
    assert not binding.usable


def test_repository_source_snapshot_binds_excluded_symlink_text(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    hidden = repo / ".codenib-cache"
    hidden.mkdir(parents=True)
    (hidden / "first.py").write_bytes(b"SAME\n")
    (hidden / "second.py").write_bytes(b"SAME\n")
    link = repo / "visible.py"
    link.symlink_to(".codenib-cache/first.py")
    binding = capture_repository_source(repo)

    link.unlink()
    link.symlink_to(".codenib-cache/second.py")

    with pytest.raises(RepositoryChangedError):
        binding.verify_snapshot()
    assert not binding.usable


def test_repository_source_snapshot_revalidates_unresolved_excluded_target(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    hidden = repo / ".codenib-cache"
    hidden.mkdir(parents=True)
    (repo / "visible.py").symlink_to(".codenib-cache/later.py")
    binding = capture_repository_source(repo)

    (hidden / "later.py").write_bytes(b"VALUE = 1\n")

    with pytest.raises(RepositoryChangedError):
        binding.verify_snapshot()
    assert not binding.usable


def test_fingerprint_final_gate_revalidates_excluded_link_target_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    hidden = repo / ".codenib-cache"
    hidden.mkdir(parents=True)
    (repo / "visible.py").symlink_to(".codenib-cache/later.py")
    real_scan = source_fingerprint_module._scan_pinned_repository
    changed = False

    def change_before_final_link_check(
        descriptor,
        *,
        excluded,
        selection,
        collect_entries,
    ):
        nonlocal changed
        if not collect_entries and not changed:
            changed = True
            (hidden / "later.py").write_bytes(b"VALUE = 1\n")
        return real_scan(
            descriptor,
            excluded=excluded,
            selection=selection,
            collect_entries=collect_entries,
        )

    monkeypatch.setattr(
        source_fingerprint_module,
        "_scan_pinned_repository",
        change_before_final_link_check,
    )

    with pytest.raises(RepositoryChangedError):
        fingerprint_repository(repo)
    assert changed


@pytest.mark.parametrize("field", ["root", "fingerprint", "file_count", "records"])
def test_repository_source_binding_rejects_public_identity_mutation(
    tmp_path: Path,
    field: str,
) -> None:
    repo = tmp_path / field
    repo.mkdir()
    (repo / "a.py").write_bytes(b"VALUE = 1\n")
    binding = capture_repository_source(repo)
    if field == "root":
        binding.root = tmp_path / "other"
    elif field == "fingerprint":
        binding.fingerprint = "sha256-v2:" + "f" * 64
    elif field == "file_count":
        binding.file_count += 1
    else:
        record = binding.file_records[0]
        object.__setattr__(record, "sha256", "f" * 64)

    with pytest.raises(RepositoryChangedError, match="public"):
        binding.verify_snapshot()
    assert not binding.usable


def test_repository_source_identity_snapshot_is_detached(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_bytes(b"VALUE = 1\n")

    with capture_repository_source(repo) as binding:
        first = binding.authenticated_identity_snapshot()
        object.__setattr__(first.file_records[0], "sha256", "f" * 64)
        second = binding.authenticated_identity_snapshot()

        assert second.file_records[0].sha256 != "f" * 64
        assert binding.read_bytes("a.py", max_bytes=1024) == b"VALUE = 1\n"


def test_repository_source_snapshot_cancellation_stops_inventory_without_poison(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_bytes(b"A\n")
    (repo / "b.py").write_bytes(b"B\n")
    binding = capture_repository_source(repo)
    real_update = source_fingerprint_module._update_inventory_record
    stop = RuntimeError("injected repository inventory stop")
    seen: list[str] = []
    armed = False
    active = True

    def observe_record(*args, **kwargs):
        nonlocal armed
        if active and seen:
            raise AssertionError("cancelled inventory consumed a poisoned next record")
        real_update(*args, **kwargs)
        if active:
            seen.append(kwargs["relative"])
            armed = True

    def check_cancelled() -> None:
        if armed:
            raise stop

    monkeypatch.setattr(
        source_fingerprint_module,
        "_update_inventory_record",
        observe_record,
    )
    with pytest.raises(RuntimeError) as caught:
        binding.authenticated_identity_snapshot(check_cancelled=check_cancelled)

    assert caught.value is stop
    assert seen == ["a.py"]
    assert binding.usable
    active = False
    binding.verify_snapshot()
    binding.close()


@pytest.mark.skipif(
    not contained_source_module.SECURE_CONTAINED_SYMLINKS,
    reason="requires secure POSIX contained symlinks",
)
def test_repository_source_link_hash_cancellation_does_not_poison_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    hidden = repo / ".codenib-cache"
    hidden.mkdir(parents=True)
    target = hidden / "target.py"
    target.write_bytes(b"x" * (contained_source_module._READ_CHUNK_BYTES * 3 + 1))
    (repo / "visible.py").symlink_to(".codenib-cache/target.py")
    binding = capture_repository_source(repo)
    real_update_hash = contained_source_module._BoundRepositoryFile.update_hash
    stop = RuntimeError("injected retained-link hash stop")
    inside_hash = False
    hash_polls = 0

    def observe_hash(bound, hasher, *, check_cancelled=None):
        nonlocal inside_hash
        inside_hash = True
        try:
            if check_cancelled is None:
                return real_update_hash(bound, hasher)
            return real_update_hash(
                bound,
                hasher,
                check_cancelled=check_cancelled,
            )
        finally:
            inside_hash = False

    def check_cancelled() -> None:
        nonlocal hash_polls
        if inside_hash:
            hash_polls += 1
            if hash_polls == 2:
                raise stop

    monkeypatch.setattr(
        contained_source_module._BoundRepositoryFile,
        "update_hash",
        observe_hash,
    )
    with pytest.raises(RuntimeError) as caught:
        binding.verify_snapshot(check_cancelled=check_cancelled)

    assert caught.value is stop
    assert hash_polls == 2
    assert binding.usable
    binding.verify_snapshot()
    binding.close()


def test_repository_source_binding_rejects_retained_link_record_mutation(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "target.py").write_bytes(b"VALUE = 1\n")
    (repo / "link.py").symlink_to("target.py")
    binding = capture_repository_source(repo)
    retained = binding._links["link.py"]
    binding._links["link.py"] = source_fingerprint_module._RepositorySourceLinkRecord(
        path=retained.path,
        lexical_identity=retained.lexical_identity,
        link_target="other.py",
        target_state=retained.target_state,
        windows_reparse_point=retained.windows_reparse_point,
    )

    with pytest.raises(RepositoryChangedError, match="retained link records"):
        binding.verify_snapshot()
    assert not binding.usable
    binding.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_repository_source_child_close_resets_inherited_locked_rlock(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_bytes(b"VALUE = 1\n")
    binding = capture_repository_source(repo)
    held = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        binding._lock.acquire()
        held.set()
        release.wait(timeout=10)
        binding._lock.release()

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert held.wait(timeout=2)
    read_descriptor, write_descriptor = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no cover - assertions are reported through the pipe
        try:
            os.close(read_descriptor)
            try:
                binding.close()
            except RuntimeError as exc:
                if "cannot cross processes" not in str(exc):
                    raise
            os.write(write_descriptor, b"1" if binding.closed else b"0")
        except BaseException:  # noqa: B036 - report child failure through pipe
            os.write(write_descriptor, b"E")
        finally:
            os.close(write_descriptor)
            os._exit(0)

    os.close(write_descriptor)
    try:
        ready, _, _ = select.select([read_descriptor], [], [], 3)
        if not ready:
            os.kill(child, signal.SIGKILL)
            pytest.fail("fork child deadlocked on an inherited source lock")
        assert os.read(read_descriptor, 1) == b"1"
    finally:
        os.close(read_descriptor)
        release.set()
        thread.join(timeout=2)
        os.waitpid(child, 0)
        binding.close()


def test_posix_resolution_cleanup_does_not_close_reused_descriptor(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    cleanup = contained_source_module._PosixDescriptorCleanup()
    descriptor = cleanup.open(first, os.O_RDONLY)
    os.close(descriptor)
    replacement = os.open(second, os.O_RDONLY)
    assert replacement == descriptor

    try:
        with pytest.raises(RuntimeError, match="ownership changed"):
            cleanup.close()
        assert os.read(replacement, 6) == b"second"
    finally:
        os.close(replacement)


def test_posix_resolution_cleanup_commits_close_then_reuse_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    cleanup = contained_source_module._PosixDescriptorCleanup()
    descriptor = cleanup.open(first, os.O_RDONLY)
    real_close = os.close
    replacement = -1
    interruption = KeyboardInterrupt("injected close completion cancellation")

    def close_then_reuse(value: int) -> None:
        nonlocal replacement
        if value == descriptor and replacement < 0:
            real_close(value)
            replacement = os.open(second, os.O_RDONLY)
            assert replacement == descriptor
            raise interruption
        real_close(value)

    monkeypatch.setattr(contained_source_module.os, "close", close_then_reuse)
    with pytest.raises(KeyboardInterrupt) as caught:
        cleanup.close()

    assert caught.value is interruption
    assert cleanup.closed
    assert os.read(replacement, 6) == b"second"
    monkeypatch.setattr(contained_source_module.os, "close", real_close)
    real_close(replacement)


def test_posix_resolution_cleanup_rejects_same_inode_new_ofd_after_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"owned")
    cleanup = contained_source_module._PosixDescriptorCleanup()
    descriptor = cleanup.open(source, os.O_RDONLY)
    real_close = os.close
    replacement = -1
    interruption = KeyboardInterrupt("injected same-inode fd reuse")

    def close_then_reopen_same_inode(value: int) -> None:
        nonlocal replacement
        if value == descriptor and replacement < 0:
            real_close(value)
            replacement = os.open(source, os.O_RDONLY)
            assert replacement == descriptor
            raise interruption
        real_close(value)

    monkeypatch.setattr(
        contained_source_module.os,
        "close",
        close_then_reopen_same_inode,
    )
    with pytest.raises(KeyboardInterrupt) as caught:
        cleanup.close()

    assert caught.value is interruption
    assert cleanup.closed
    monkeypatch.setattr(contained_source_module.os, "close", real_close)
    cleanup.close()
    assert os.read(replacement, 5) == b"owned"
    real_close(replacement)


def test_posix_resolution_cleanup_coordinates_dup_alias_close_cookie(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"owned")
    cleanup = contained_source_module._PosixDescriptorCleanup()
    original = cleanup.open(source, os.O_RDONLY)
    duplicate = cleanup.dup(original)
    real_close = os.close
    interruption = KeyboardInterrupt("injected before dup alias close")
    injected = False

    def interrupt_duplicate(value: int) -> None:
        nonlocal injected
        if value == duplicate and not injected:
            injected = True
            raise interruption
        real_close(value)

    monkeypatch.setattr(contained_source_module.os, "close", interrupt_duplicate)
    with pytest.raises(KeyboardInterrupt) as caught:
        cleanup.close()

    assert caught.value is interruption
    assert cleanup.descriptors == [duplicate]
    with pytest.raises(OSError) as original_error:
        os.fstat(original)
    assert original_error.value.errno == errno.EBADF
    assert os.fstat(duplicate)

    monkeypatch.setattr(contained_source_module.os, "close", real_close)
    cleanup.close()
    assert cleanup.closed
    with pytest.raises(OSError) as duplicate_error:
        os.fstat(duplicate)
    assert duplicate_error.value.errno == errno.EBADF


def test_posix_cleanup_coordinates_close_cookie_across_dup_owners(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"owned")
    original_owner = contained_source_module._PosixDescriptorCleanup()
    alias_owner = contained_source_module._PosixDescriptorCleanup()
    original = original_owner.open(source, os.O_RDONLY)
    duplicate = alias_owner.dup(original)
    baseline = dict(contained_source_module._CLOSE_COOKIE_COUNTS)
    real_close = os.close
    interruption = KeyboardInterrupt("injected before cross-owner alias close")
    injected = False

    def interrupt_duplicate(value: int) -> None:
        nonlocal injected
        if value == duplicate and not injected:
            injected = True
            raise interruption
        real_close(value)

    monkeypatch.setattr(contained_source_module.os, "close", interrupt_duplicate)
    with pytest.raises(KeyboardInterrupt) as caught:
        alias_owner.close()
    assert caught.value is interruption
    assert len(contained_source_module._CLOSE_COOKIE_COUNTS) == len(baseline) + 1

    original_owner.close()
    assert original_owner.closed
    monkeypatch.setattr(contained_source_module.os, "close", real_close)
    alias_owner.close()
    assert alias_owner.closed
    assert contained_source_module._CLOSE_COOKIE_COUNTS == baseline


def test_posix_directory_cleanup_falls_back_when_cookie_seek_is_unsupported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup = contained_source_module._PosixDescriptorCleanup()
    descriptor = cleanup.open(
        tmp_path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    baseline = dict(contained_source_module._CLOSE_COOKIE_COUNTS)
    real_lseek = os.lseek

    def reject_directory_cookie(value: int, offset: int, whence: int) -> int:
        if value == descriptor:
            raise OSError(errno.EINVAL, "directory offsets are opaque")
        return real_lseek(value, offset, whence)

    monkeypatch.setattr(contained_source_module.os, "lseek", reject_directory_cookie)
    cleanup.close()

    assert cleanup.closed
    assert contained_source_module._CLOSE_COOKIE_COUNTS == baseline
    with pytest.raises(OSError) as caught:
        os.fstat(descriptor)
    assert caught.value.errno == errno.EBADF


def test_posix_directory_cleanup_retains_no_cookie_close_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup = contained_source_module._PosixDescriptorCleanup()
    descriptor = cleanup.open(
        tmp_path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    real_lseek = os.lseek
    real_close = os.close
    interruption = KeyboardInterrupt("injected before no-cookie directory close")
    injected = False

    def reject_directory_cookie(value: int, offset: int, whence: int) -> int:
        if value == descriptor:
            raise OSError(errno.ESPIPE, "directory is not seekable")
        return real_lseek(value, offset, whence)

    def interrupt_before_close(value: int) -> None:
        nonlocal injected
        if value == descriptor and not injected:
            injected = True
            raise interruption
        real_close(value)

    monkeypatch.setattr(contained_source_module.os, "lseek", reject_directory_cookie)
    monkeypatch.setattr(contained_source_module.os, "close", interrupt_before_close)
    with pytest.raises(KeyboardInterrupt) as caught:
        cleanup.close()

    assert caught.value is interruption
    assert cleanup.descriptors == [descriptor]
    assert os.fstat(descriptor)
    cleanup.close()
    assert cleanup.closed


def test_posix_resolution_cleanup_closes_owned_file_after_size_change(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"first")
    cleanup = contained_source_module._PosixDescriptorCleanup()
    descriptor = cleanup.open(source, os.O_RDONLY)

    source.write_bytes(b"a longer replacement on the same inode")
    cleanup.close()

    assert cleanup.closed
    with pytest.raises(OSError) as caught:
        os.fstat(descriptor)
    assert caught.value.errno == errno.EBADF


def test_posix_root_authority_cleanup_closes_owned_directory_after_chmod(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    authority = source_fingerprint_module._open_pinned_repository_root(repo)
    descriptors = tuple(authority._resources)

    repo.chmod(0o700)
    authority.close()

    assert authority.closed
    for descriptor in descriptors:
        with pytest.raises(OSError) as caught:
            os.fstat(descriptor)
        assert caught.value.errno == errno.EBADF


def test_posix_root_cleanup_rejects_same_inode_new_ofd_after_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    authority = source_fingerprint_module._open_pinned_repository_root(repo)
    descriptor = authority._resources[-1]
    real_close = os.close
    replacement = -1
    interruption = KeyboardInterrupt("injected same-directory fd reuse")

    def close_then_reopen_same_inode(value: int) -> None:
        nonlocal replacement
        if value == descriptor and replacement < 0:
            real_close(value)
            replacement = os.open(repo, os.O_RDONLY | os.O_DIRECTORY)
            assert replacement == descriptor
            raise interruption
        real_close(value)

    monkeypatch.setattr(
        source_fingerprint_module.os,
        "close",
        close_then_reopen_same_inode,
    )
    with pytest.raises(KeyboardInterrupt) as caught:
        authority.close()

    assert caught.value is interruption
    assert authority.closed
    monkeypatch.setattr(source_fingerprint_module.os, "close", real_close)
    authority.close()
    assert stat.S_ISDIR(os.fstat(replacement).st_mode)
    real_close(replacement)


def test_repository_source_binding_poisoned_by_touched_or_unrelated_change(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "a.py"
    other = repo / "b.py"
    source.write_bytes(b"VALUE = 1\n")
    other.write_bytes(b"OTHER = 1\n")
    binding = capture_repository_source(repo)
    other.write_bytes(b"OTHER = 999\n")

    with pytest.raises(RepositoryChangedError):
        binding.read_bytes("a.py", max_bytes=1024)

    other.write_bytes(b"OTHER = 1\n")
    with pytest.raises(RepositoryChangedError, match="poisoned"):
        binding.read_bytes("a.py", max_bytes=1024)
    binding.close()


def test_repository_source_binding_rejects_legal_replacement_root(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_bytes(b"VALUE = 1\n")
    binding = capture_repository_source(repo)
    pinned = tmp_path / "pinned"
    repo.rename(pinned)
    repo.mkdir()
    (repo / "a.py").write_bytes(b"VALUE = 1\n")

    with pytest.raises(RepositoryChangedError):
        binding.verify_snapshot()
    assert not binding.usable
    binding.close()


def test_source_authority_rejects_preexisting_ancestor_symlink(
    tmp_path: Path,
) -> None:
    foreign = tmp_path / "foreign"
    repo = foreign / "repo"
    repo.mkdir(parents=True)
    (repo / "a.py").write_bytes(b"VALUE = 1\n")
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    (trusted / "alias").symlink_to(foreign, target_is_directory=True)

    with pytest.raises(RepositoryChangedError):
        fingerprint_repository(trusted / "alias" / "repo")


def test_repository_source_binding_rejects_replaced_ancestor(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent"
    repo = parent / "repo"
    repo.mkdir(parents=True)
    (repo / "a.py").write_bytes(b"VALUE = 1\n")
    binding = capture_repository_source(repo)
    saved = tmp_path / "saved-parent"
    parent.rename(saved)
    repo.mkdir(parents=True)
    (repo / "a.py").write_bytes(b"VALUE = 1\n")

    with pytest.raises(RepositoryChangedError):
        binding.verify_snapshot()
    binding.close()


def test_repository_source_read_session_scans_whole_tree_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_bytes(b"A\n")
    (repo / "b.py").write_bytes(b"B\n")
    binding = capture_repository_source(repo)
    real_scan = source_fingerprint_module._scan_pinned_repository
    scans = 0

    def counted_scan(*args, **kwargs):
        nonlocal scans
        scans += 1
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(
        source_fingerprint_module,
        "_scan_pinned_repository",
        counted_scan,
    )
    with binding.read_session():
        assert binding.read_bytes("a.py", max_bytes=16) == b"A\n"
        assert binding.read_bytes("b.py", max_bytes=16) == b"B\n"

    assert scans == 2
    binding.close()


def test_repository_source_read_session_exact_stop_revalidates_without_poison(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_bytes(b"A\n")
    binding = capture_repository_source(repo)
    real_scan = source_fingerprint_module._scan_pinned_repository
    scan_callbacks: list[object | None] = []
    stop = RuntimeError("injected sticky read-session stop")
    armed = False

    def observe_scan(*args, **kwargs):
        scan_callbacks.append(kwargs.get("check_cancelled"))
        return real_scan(*args, **kwargs)

    def check_cancelled() -> None:
        if armed:
            raise stop

    monkeypatch.setattr(
        source_fingerprint_module,
        "_scan_pinned_repository",
        observe_scan,
    )
    with pytest.raises(RuntimeError) as caught:
        with binding.read_session(check_cancelled=check_cancelled):
            armed = True
            check_cancelled()

    assert caught.value is stop
    assert [callback is not None for callback in scan_callbacks] == [True, False]
    assert binding.usable
    armed = False
    binding.verify_snapshot()
    binding.close()


def test_repository_source_read_session_trace_interrupt_releases_lock(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_bytes(b"A\n")
    binding = capture_repository_source(repo)
    wrapped = RepositorySourceBinding.read_session.__wrapped__
    lines, first_line = inspect.getsourcelines(wrapped)
    target_line = first_line + next(
        index
        for index, line in enumerate(lines)
        if "if self._session_depth == 0:" in line
    )
    interrupted = False

    def interrupt_after_acquire(frame, event, _arg):
        nonlocal interrupted
        if (
            not interrupted
            and frame.f_code is wrapped.__code__
            and event == "line"
            and frame.f_lineno == target_line
        ):
            interrupted = True
            raise KeyboardInterrupt("injected after lock acquisition")
        return interrupt_after_acquire

    sys.settrace(interrupt_after_acquire)
    try:
        with pytest.raises(KeyboardInterrupt, match="after lock acquisition"):
            with binding.read_session():
                pytest.fail("interruption must happen before the session body")
    finally:
        sys.settrace(None)

    completed = threading.Event()

    def close_from_another_thread() -> None:
        binding.close()
        completed.set()

    thread = threading.Thread(target=close_from_another_thread)
    thread.start()
    thread.join(timeout=2)
    assert completed.is_set(), "interrupted acquisition stranded the source lock"
    assert binding.closed


def test_repository_source_read_session_exit_interrupt_poison_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_bytes(b"A\n")
    binding = capture_repository_source(repo)
    real_scan = source_fingerprint_module._scan_pinned_repository
    scans = 0
    interruption = KeyboardInterrupt("injected exit inventory interruption")

    def interrupt_exit(*args, **kwargs):
        nonlocal scans
        scans += 1
        if scans == 2:
            raise interruption
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(
        source_fingerprint_module,
        "_scan_pinned_repository",
        interrupt_exit,
    )
    with pytest.raises(KeyboardInterrupt) as caught:
        with binding.read_session():
            assert binding.read_bytes("a.py", max_bytes=16) == b"A\n"

    assert caught.value is interruption
    assert binding.closed
    assert not binding.usable


def test_repository_source_read_session_preserves_body_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_bytes(b"A\n")
    binding = capture_repository_source(repo)
    real_scan = source_fingerprint_module._scan_pinned_repository
    scans = 0
    primary = KeyboardInterrupt("primary session cancellation")

    def interrupt_exit(*args, **kwargs):
        nonlocal scans
        scans += 1
        if scans == 2:
            raise SystemExit("secondary exit cancellation")
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(
        source_fingerprint_module,
        "_scan_pinned_repository",
        interrupt_exit,
    )
    with pytest.raises(KeyboardInterrupt) as caught:
        with binding.read_session():
            raise primary

    assert caught.value is primary
    assert binding.closed


@pytest.mark.parametrize(
    ("timing", "interruption"),
    [
        pytest.param(
            "before",
            KeyboardInterrupt("injected before descriptor close"),
            id="before-close",
        ),
        pytest.param(
            "after",
            SystemExit("injected after descriptor close"),
            id="after-close",
        ),
    ],
)
def test_repository_source_close_finishes_descriptor_chain_before_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timing: str,
    interruption: BaseException,
) -> None:
    descriptor_root = Path("/proc/self/fd")
    if not descriptor_root.is_dir():
        pytest.skip("descriptor accounting needs /proc/self/fd")
    repo = tmp_path / "one" / "two" / "repo"
    repo.mkdir(parents=True)
    (repo / "a.py").write_bytes(b"A\n")
    before = len(tuple(descriptor_root.iterdir()))
    binding = capture_repository_source(repo)
    authority = binding._posix_authority
    assert authority is not None
    targets = set(authority._resources)
    real_close = os.close
    calls = 0
    injected = False
    seen: set[int] = set()

    def interrupt_one_close(descriptor: int) -> None:
        nonlocal calls, injected
        if descriptor in targets:
            seen.add(descriptor)
            calls += 1
            if calls == 2 and not injected:
                injected = True
                if timing == "before":
                    raise interruption
                real_close(descriptor)
                raise interruption
        real_close(descriptor)

    monkeypatch.setattr(source_fingerprint_module.os, "close", interrupt_one_close)
    with pytest.raises(type(interruption)) as caught:
        binding.close()

    assert caught.value is interruption
    assert injected
    assert binding.closed
    assert targets <= seen
    after = len(tuple(descriptor_root.iterdir()))
    assert after <= before + 1


def test_repository_source_close_commits_interrupted_owner_pop(
    tmp_path: Path,
) -> None:
    descriptor_root = Path("/proc/self/fd")
    if not descriptor_root.is_dir():
        pytest.skip("descriptor accounting needs /proc/self/fd")
    repo = tmp_path / "parent" / "repo"
    repo.mkdir(parents=True)
    (repo / "a.py").write_bytes(b"A\n")
    before = len(tuple(descriptor_root.iterdir()))
    binding = capture_repository_source(repo)
    authority = binding._posix_authority
    assert authority is not None
    interruption = KeyboardInterrupt("injected after descriptor owner pop")
    authority._resources = _InterruptAfterPopList(
        authority._resources,
        interruption,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        binding.close()

    assert caught.value is interruption
    assert binding.closed
    assert len(tuple(descriptor_root.iterdir())) <= before + 1


def test_repository_source_close_persistent_eio_keeps_owner_and_closes_rest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "one" / "two" / "repo"
    repo.mkdir(parents=True)
    (repo / "a.py").write_bytes(b"A\n")
    binding = capture_repository_source(repo)
    authority = binding._posix_authority
    assert authority is not None
    targets = tuple(authority._resources)
    failed = targets[-1]
    real_close = os.close

    def fail_one_descriptor(descriptor: int) -> None:
        if descriptor == failed:
            raise OSError(errno.EIO, "injected persistent close EIO")
        real_close(descriptor)

    monkeypatch.setattr(source_fingerprint_module.os, "close", fail_one_descriptor)
    with pytest.raises(OSError, match="persistent close EIO"):
        binding.close()

    assert not binding.closed
    assert not binding.usable
    assert authority._resources == [failed]
    for descriptor in targets[:-1]:
        with pytest.raises(OSError) as caught:
            os.fstat(descriptor)
        assert caught.value.errno == errno.EBADF

    monkeypatch.setattr(source_fingerprint_module.os, "close", real_close)
    binding.close()
    assert binding.closed


def test_repository_source_close_preflight_eio_keeps_owner_and_closes_rest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "one" / "two" / "repo"
    repo.mkdir(parents=True)
    (repo / "a.py").write_bytes(b"A\n")
    binding = capture_repository_source(repo)
    authority = binding._posix_authority
    assert authority is not None
    targets = tuple(authority._resources)
    failed = targets[-1]
    real_fstat = os.fstat

    def fail_one_descriptor(descriptor: int):
        if descriptor == failed:
            raise OSError(errno.EIO, "injected persistent fstat EIO")
        return real_fstat(descriptor)

    monkeypatch.setattr(source_fingerprint_module.os, "fstat", fail_one_descriptor)
    with pytest.raises(OSError, match="persistent fstat EIO"):
        binding.close()

    assert not binding.closed
    assert not binding.usable
    assert authority._resources == [failed]
    for descriptor in targets[:-1]:
        with pytest.raises(OSError) as caught:
            real_fstat(descriptor)
        assert caught.value.errno == errno.EBADF

    monkeypatch.setattr(source_fingerprint_module.os, "fstat", real_fstat)
    binding.close()
    assert binding.closed


@pytest.mark.parametrize(
    "interruption",
    [
        pytest.param(KeyboardInterrupt("root open interrupted"), id="keyboard"),
        pytest.param(SystemExit("root open exited"), id="system-exit"),
    ],
)
def test_repository_root_open_failure_closes_owned_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: BaseException,
) -> None:
    descriptor_root = Path("/proc/self/fd")
    if not descriptor_root.is_dir():
        pytest.skip("descriptor accounting needs /proc/self/fd")
    repo = tmp_path / "parent" / "repo"
    repo.mkdir(parents=True)
    (repo / "a.py").write_bytes(b"A\n")
    before = len(tuple(descriptor_root.iterdir()))
    real_fstat = os.fstat
    calls = 0

    def interrupt_after_child_open(descriptor: int):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise interruption
        return real_fstat(descriptor)

    monkeypatch.setattr(
        source_fingerprint_module.os,
        "fstat",
        interrupt_after_child_open,
    )
    with pytest.raises(type(interruption)) as caught:
        fingerprint_repository(repo)

    assert caught.value is interruption
    after = len(tuple(descriptor_root.iterdir()))
    assert after <= before + 1


def test_capture_root_construction_eio_keeps_preinstalled_retry_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "parent" / "repo"
    repo.mkdir(parents=True)
    (repo / "source.py").write_bytes(b"VALUE = 1\n")
    owner = SourceBindingCleanupOwner()
    interruption = KeyboardInterrupt("injected POSIX root construction failure")
    real_open = os.open
    real_fstat = os.fstat
    real_close = os.close
    opened: list[int] = []
    fstat_calls = 0

    def record_open(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def interrupt_metadata(descriptor: int):
        nonlocal fstat_calls
        fstat_calls += 1
        if fstat_calls == 2:
            raise interruption
        return real_fstat(descriptor)

    failed = -1

    def persistent_close(descriptor: int) -> None:
        nonlocal failed
        if descriptor in opened and failed < 0:
            failed = descriptor
        if descriptor == failed:
            raise OSError(errno.EIO, "injected POSIX construction close EIO")
        real_close(descriptor)

    monkeypatch.setattr(source_fingerprint_module.os, "open", record_open)
    monkeypatch.setattr(source_fingerprint_module.os, "fstat", interrupt_metadata)
    monkeypatch.setattr(source_fingerprint_module.os, "close", persistent_close)

    with pytest.raises(KeyboardInterrupt) as caught:
        capture_repository_source(repo, _source_owner=owner.retain)

    assert caught.value is interruption
    assert not owner.closed
    assert owner.pending_sources
    assert failed >= 0
    assert real_fstat(failed)

    monkeypatch.setattr(source_fingerprint_module.os, "fstat", real_fstat)
    monkeypatch.setattr(source_fingerprint_module.os, "close", real_close)
    owner.close()
    assert owner.closed


def test_capture_scan_eio_keeps_primary_and_explicit_partial_fd_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "source.py").write_bytes(b"VALUE = 1\n")
    interruption = SystemExit("injected POSIX scan failure")
    real_close = os.close
    scan_descriptor = -1

    def interrupt_scan(descriptor: int):
        nonlocal scan_descriptor
        scan_descriptor = descriptor
        raise interruption

    def persistent_close(descriptor: int) -> None:
        if descriptor == scan_descriptor:
            raise OSError(errno.EIO, "injected POSIX scan close EIO")
        real_close(descriptor)

    monkeypatch.setattr(source_fingerprint_module.os, "scandir", interrupt_scan)
    monkeypatch.setattr(
        source_fingerprint_module.os,
        "supports_fd",
        {*os.supports_fd, interrupt_scan},
    )
    monkeypatch.setattr(source_fingerprint_module.os, "close", persistent_close)

    with pytest.raises(SystemExit) as caught:
        capture_repository_source(tmp_path)

    assert caught.value is interruption
    cleanup_owner = caught.value.source_cleanup_owner
    assert not cleanup_owner.closed
    assert scan_descriptor in cleanup_owner.descriptors
    assert os.fstat(scan_descriptor)

    monkeypatch.setattr(source_fingerprint_module.os, "close", real_close)
    cleanup_owner.close()
    assert cleanup_owner.closed


def test_posix_resolution_construction_eio_keeps_primary_and_retry_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "source.py"
    source.write_bytes(b"VALUE = 1\n")
    authority = source_fingerprint_module._open_pinned_repository_root(repo)
    expected = contained_source_module._version_identity(source.lstat())
    interruption = KeyboardInterrupt("injected POSIX resolution failure")
    real_open = os.open
    real_fstat = os.fstat
    real_close = os.close
    failed = -1

    def record_open(path, flags, *args, **kwargs):
        nonlocal failed
        descriptor = real_open(path, flags, *args, **kwargs)
        if path == "source.py":
            failed = descriptor
        return descriptor

    def interrupt_source_metadata(descriptor: int):
        if descriptor == failed:
            raise interruption
        return real_fstat(descriptor)

    def persistent_close(descriptor: int) -> None:
        if descriptor == failed:
            raise OSError(errno.EIO, "injected POSIX resolution close EIO")
        real_close(descriptor)

    monkeypatch.setattr(contained_source_module.os, "open", record_open)
    monkeypatch.setattr(contained_source_module.os, "fstat", interrupt_source_metadata)
    monkeypatch.setattr(contained_source_module.os, "close", persistent_close)

    with pytest.raises(KeyboardInterrupt) as caught:
        with contained_source_module._resolved_repository_file_at(
            repo,
            authority.descriptor,
            "source.py",
            expected_root_identity=authority.root_identity,
            expected_final_identity=expected,
        ):
            pytest.fail("resolution construction must not yield")

    assert caught.value is interruption
    cleanup_owner = caught.value.source_cleanup_owner
    assert not cleanup_owner.closed
    assert failed >= 0
    assert real_fstat(failed)

    monkeypatch.setattr(contained_source_module.os, "fstat", real_fstat)
    monkeypatch.setattr(contained_source_module.os, "close", real_close)
    cleanup_owner.close()
    authority.close()
    assert cleanup_owner.closed


def _legacy_link_only_digest(path: bytes, target: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(
        (
            "codenib-source-fingerprint:1:" f"{REPOSITORY_FILTER_POLICY_VERSION}\0"
        ).encode("ascii")
    )
    digest.update(b"source-selection-digest\0")
    digest.update(DEFAULT_REPOSITORY_SOURCE_SELECTION.digest.encode("ascii"))
    digest.update(b"\0")
    digest.update(b"L\0" + path + b"\0" + target + b"\0")
    return f"sha256:{digest.hexdigest()}"


class _FakeWindowsSourceApi:
    """HANDLE-only Windows source model with exact 128-bit identities."""

    def __init__(self) -> None:
        self.nodes: dict[bytes, dict[str, object]] = {}
        self.handles: dict[int, bytes] = {}
        self.offsets: dict[int, int] = {}
        self.next_id = 1
        self.next_handle = 100
        self.open_relative_hook = None
        self.create_root_hook = None
        self.query_reparse_hook = None
        self.created_paths: list[str] = []
        self.volume_id = self.add_directory()
        self.root_id = self.add_directory(self.volume_id, "repo")

    def _file_id(self) -> bytes:
        value = self.next_id.to_bytes(16, "little")
        self.next_id += 1
        return value

    def add_directory(
        self,
        parent: bytes | None = None,
        name: str = "",
    ) -> bytes:
        file_id = self._file_id()
        self.nodes[file_id] = {
            "kind": "directory",
            "children": {},
            "data": b"",
            "version": 1,
            "file_id": file_id,
            "reparse": None,
        }
        if parent is not None:
            self._children(parent)[name] = file_id
        return file_id

    def add_file(self, parent: bytes, name: str, data: bytes) -> bytes:
        file_id = self._file_id()
        self.nodes[file_id] = {
            "kind": "file",
            "children": {},
            "data": data,
            "version": 1,
            "file_id": file_id,
            "reparse": None,
        }
        self._children(parent)[name] = file_id
        return file_id

    def add_symlink(
        self,
        parent: bytes,
        name: str,
        target: str,
        *,
        directory: bool = False,
        absolute: bool = False,
        substitute: str | None = None,
    ) -> bytes:
        file_id = self._file_id()
        point = windows_fs_module.WindowsReparsePoint(
            tag=windows_fs_module.IO_REPARSE_TAG_SYMLINK,
            substitute_name=substitute or target,
            print_name=target,
            flags=0 if absolute else windows_fs_module.SYMLINK_FLAG_RELATIVE,
        )
        self.nodes[file_id] = {
            "kind": "directory-link" if directory else "link",
            "children": {},
            "data": b"",
            "version": 1,
            "file_id": file_id,
            "reparse": point,
        }
        self._children(parent)[name] = file_id
        return file_id

    def add_unknown_reparse(self, parent: bytes, name: str) -> bytes:
        file_id = self._file_id()
        self.nodes[file_id] = {
            "kind": "link",
            "children": {},
            "data": b"",
            "version": 1,
            "file_id": file_id,
            "reparse": windows_fs_module.WindowsReparsePoint(
                tag=windows_fs_module.IO_REPARSE_TAG_MOUNT_POINT,
                substitute_name=None,
                print_name=None,
            ),
        }
        self._children(parent)[name] = file_id
        return file_id

    def _children(self, file_id: bytes) -> dict[str, bytes]:
        children = self.nodes[file_id]["children"]
        assert isinstance(children, dict)
        return children

    def _new_handle(self, file_id: bytes) -> int:
        handle = self.next_handle
        self.next_handle += 1
        self.handles[handle] = file_id
        self.offsets[handle] = 0
        return handle

    def create_directory_handle(self, _path: Path) -> int:
        self.created_paths.append(str(_path))
        file_id = self.volume_id
        if self.create_root_hook is not None:
            file_id = self.create_root_hook(file_id)
        return self._new_handle(file_id)

    def duplicate_handle(self, handle: int) -> int:
        return self._new_handle(self.handles[handle])

    def close(self, handle: int) -> None:
        self.handles.pop(handle, None)
        self.offsets.pop(handle, None)

    def metadata(self, handle: int) -> windows_fs_module.WindowsHandleMetadata:
        file_id = self.handles[handle]
        node = self.nodes[file_id]
        kind = str(node["kind"])
        directory = kind in {"directory", "directory-link"}
        reparse = node["reparse"]
        attributes = windows_fs_module.FILE_ATTRIBUTE_DIRECTORY if directory else 0
        reparse_tag = 0
        if isinstance(reparse, windows_fs_module.WindowsReparsePoint):
            attributes |= windows_fs_module.FILE_ATTRIBUTE_REPARSE_POINT
            reparse_tag = reparse.tag
        data = node["data"]
        assert isinstance(data, bytes)
        version = int(node["version"])
        return windows_fs_module.WindowsHandleMetadata(
            st_dev=7,
            st_ino=int.from_bytes(file_id[:8], "little"),
            st_mode=windows_fs_module.windows_mode_from_attributes(attributes),
            st_size=len(data),
            st_mtime_ns=version,
            st_ctime_ns=version,
            st_nlink=1,
            st_file_attributes=attributes,
            file_id_128=file_id,
            reparse_tag=reparse_tag,
            delete_pending=False,
        )

    def iter_directory(self, handle: int):
        parent_id = self.handles[handle]
        for name, file_id in list(self._children(parent_id).items()):
            child_handle = self._new_handle(file_id)
            try:
                metadata = self.metadata(child_handle)
            finally:
                self.close(child_handle)
            yield windows_fs_module.WindowsDirectoryEntry(
                name=name,
                file_id=int.from_bytes(file_id[:8], "little"),
                attributes=metadata.st_file_attributes,
                file_id_128=file_id,
                reparse_tag=metadata.reparse_tag,
            )

    def enumerate_directory(
        self,
        handle: int,
    ) -> tuple[windows_fs_module.WindowsDirectoryEntry, ...]:
        return tuple(self.iter_directory(handle))

    def open_relative(
        self,
        parent_handle: int,
        name: str,
        *,
        desired_access: int,
        is_directory: bool,
        allow_reparse: bool,
    ) -> int:
        del desired_access
        parent_id = self.handles[parent_handle]
        file_id = self._children(parent_id)[name]
        if self.open_relative_hook is not None:
            file_id = self.open_relative_hook(parent_id, name, file_id)
        node = self.nodes[file_id]
        directory = str(node["kind"]) in {"directory", "directory-link"}
        assert directory is is_directory
        if node["reparse"] is not None and not allow_reparse:
            raise ValueError("fake no-follow open rejected a reparse point")
        assert allow_reparse is (node["reparse"] is not None)
        return self._new_handle(file_id)

    def query_reparse_point(
        self,
        handle: int,
    ) -> windows_fs_module.WindowsReparsePoint:
        point = self.nodes[self.handles[handle]]["reparse"]
        assert isinstance(point, windows_fs_module.WindowsReparsePoint)
        if self.query_reparse_hook is not None:
            return self.query_reparse_hook(point)
        return point

    def read(self, handle: int, size: int) -> bytes:
        node = self.nodes[self.handles[handle]]
        data = node["data"]
        assert isinstance(data, bytes)
        offset = self.offsets[handle]
        block = data[offset : offset + size]
        self.offsets[handle] += len(block)
        return block


def test_windows_inventory_cancellation_does_not_resume_poisoned_tail() -> None:
    api = _FakeWindowsSourceApi()
    api.add_file(api.root_id, "a.py", b"A\n")
    api.add_file(api.root_id, "b.py", b"B\n")
    (
        selected,
        authority,
        _root_identity,
    ) = contained_source_module._open_windows_pinned_repository_root(
        Path(r"C:\repo"),
        api=api,
    )
    retained_handles = dict(api.handles)
    real_iter_directory = api.iter_directory
    stop = RuntimeError("injected Windows inventory stop")
    armed = False
    yielded: list[str] = []

    def poison_tail(handle: int):
        nonlocal armed
        for entry in real_iter_directory(handle):
            if yielded:
                raise AssertionError(
                    "cancelled Windows inventory resumed its poisoned tail"
                )
            yielded.append(entry.name)
            armed = True
            yield entry

    def check_cancelled() -> None:
        if armed:
            raise stop

    api.iter_directory = poison_tail  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError) as caught:
            source_fingerprint_module._scan_pinned_windows_repository(
                selected,
                authority.handle,
                excluded=(),
                collect_entries=False,
                check_cancelled=check_cancelled,
            )

        assert caught.value is stop
        assert yielded == ["a.py"]
        assert api.handles == retained_handles
    finally:
        authority.close()
    assert api.handles == {}


def test_windows_link_resolution_cancellation_does_not_poison_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsSourceApi()
    hidden = api.add_directory(api.root_id, ".codenib-cache")
    api.add_file(hidden, "target.py", b"VALUE = 1\n")
    api.add_symlink(
        api.root_id,
        "visible.py",
        r".codenib-cache\target.py",
    )
    binding = source_fingerprint_module._fingerprint_windows_repository(
        r"C:\repo",
        exclude_roots=(),
        version=SOURCE_FINGERPRINT_VERSION,
        api=api,
        retain_binding=True,
    )
    assert isinstance(binding, RepositorySourceBinding)
    retained_handles = dict(api.handles)
    real_open = contained_source_module._open_windows_resolution_at
    stop = RuntimeError("injected Windows link-resolution stop")
    inside_resolution = False
    active = True
    resolution_polls = 0

    def observe_open(*args, **kwargs):
        nonlocal inside_resolution
        inside_resolution = True
        try:
            return real_open(*args, **kwargs)
        finally:
            inside_resolution = False

    def check_cancelled() -> None:
        nonlocal resolution_polls
        if active and inside_resolution:
            resolution_polls += 1
            if resolution_polls == 2:
                raise stop

    monkeypatch.setattr(
        contained_source_module,
        "_open_windows_resolution_at",
        observe_open,
    )
    with pytest.raises(RuntimeError) as caught:
        binding.verify_snapshot(check_cancelled=check_cancelled)

    assert caught.value is stop
    assert resolution_polls == 2
    assert binding.usable
    assert api.handles == retained_handles
    active = False
    binding.verify_snapshot()
    binding.close()
    assert api.handles == {}


def _open_fake_windows_resolution(
    api: _FakeWindowsSourceApi,
    relative: str,
):
    (
        selected,
        authority,
        root_identity,
    ) = contained_source_module._open_windows_pinned_repository_root(
        Path(r"C:\repo"),
        api=api,
    )
    scan = source_fingerprint_module._scan_pinned_windows_repository(
        selected,
        authority.handle,
        excluded=(),
        collect_entries=True,
    )
    record = next(entry for entry in scan.entries if entry.relative == relative)
    binding = contained_source_module._open_windows_resolution_at(
        Path(r"C:\repo"),
        authority,
        tuple(relative.split("/")),
        expected_root_identity=root_identity,
        expected_final_identity=source_fingerprint_module._entry_version_identity(
            record.metadata
        ),
        allow_stable_unresolved=True,
        api=selected,
    )
    return authority, binding


def _fake_windows_resolution_handoff_case(
    case: str,
) -> tuple[
    _FakeWindowsSourceApi,
    object,
    tuple[object, ...],
    tuple[object, ...],
]:
    api = _FakeWindowsSourceApi()
    if case == "regular":
        api.add_file(api.root_id, "current.py", b"VALUE = 1\n")
    elif case == "link-loop":
        api.add_symlink(api.root_id, "current.py", "current.py")
    elif case == "directory":
        api.add_symlink(api.root_id, "current.py", ".", directory=True)
    elif case == "missing":
        api.add_symlink(api.root_id, "current.py", "missing.py")
    else:  # pragma: no cover - test parameter invariant
        raise AssertionError(case)
    (
        selected,
        authority,
        root_identity,
    ) = contained_source_module._open_windows_pinned_repository_root(
        Path(r"C:\repo"),
        api=api,
    )
    scan = source_fingerprint_module._scan_pinned_windows_repository(
        selected,
        authority.handle,
        excluded=(),
        collect_entries=True,
    )
    record = scan.entries[0]
    if case == "missing":
        del api._children(api.root_id)["current.py"]
    return (
        api,
        authority,
        root_identity,
        source_fingerprint_module._entry_version_identity(record.metadata),
    )


def test_fingerprint_changes_with_source_content_and_path(tmp_path):
    source = tmp_path / "module.py"
    source.write_text("VALUE = 1\n")
    initial = fingerprint_repository(tmp_path)

    source.write_text("VALUE = 2\n")
    changed_content = fingerprint_repository(tmp_path)
    assert changed_content.value != initial.value
    assert changed_content.file_count == 1

    source.rename(tmp_path / "renamed.py")
    changed_path = fingerprint_repository(tmp_path)
    assert changed_path.value != changed_content.value


def test_v2_fingerprint_is_canonical_and_deterministic(tmp_path: Path) -> None:
    (tmp_path / "module.py").write_bytes(b"VALUE = 1\n")

    first = fingerprint_repository(tmp_path)
    second = fingerprint_repository(tmp_path)

    assert SOURCE_FINGERPRINT_VERSION == 2
    assert first == second
    assert first.value.startswith("sha256-v2:")
    assert is_secure_source_fingerprint_v2(first.value)
    assert source_fingerprint_version(first.value) == 2


def test_selection_identity_changes_v1_and_v2_without_matching_paths(
    tmp_path: Path,
) -> None:
    (tmp_path / "module.py").write_bytes(b"VALUE = 1\n")
    empty = RepositorySourceSelection()
    latent = RepositorySourceSelection(("future/generated",))

    empty_v2 = fingerprint_repository(tmp_path, selection=empty)
    latent_v2 = fingerprint_repository(tmp_path, selection=latent)
    empty_v1 = fingerprint_repository_v1_for_diagnostics(
        tmp_path,
        selection=empty,
    )
    latent_v1 = fingerprint_repository_v1_for_diagnostics(
        tmp_path,
        selection=latent,
    )

    assert empty_v2.file_count == latent_v2.file_count == 1
    assert empty_v1.file_count == latent_v1.file_count == 1
    assert empty_v2.value != latent_v2.value
    assert empty_v1.value != latent_v1.value


def test_selection_excludes_subtree_before_stat_open_or_content_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    generated = repo / "generated"
    generated.mkdir(parents=True)
    (repo / "included.py").write_bytes(b"INCLUDED = 1\n")
    excluded_file = generated / "excluded.py"
    excluded_file.write_bytes(b"EXCLUDED = 1\n")
    selection = RepositorySourceSelection(("generated",))
    real_stat = source_fingerprint_module.os.stat
    real_open = source_fingerprint_module.os.open
    real_resolve = source_fingerprint_module._resolved_repository_file_at

    def reject_excluded_stat(path, *args, **kwargs):
        if path == "generated" and kwargs.get("dir_fd") is not None:
            raise AssertionError("excluded subtree was statted")
        return real_stat(path, *args, **kwargs)

    def reject_excluded_open(path, flags, *args, **kwargs):
        if path == "generated" and kwargs.get("dir_fd") is not None:
            raise AssertionError("excluded subtree was opened")
        return real_open(path, flags, *args, **kwargs)

    @contextmanager
    def reject_excluded_read(root, descriptor, relative, **kwargs):
        if relative == "generated" or relative.startswith("generated/"):
            raise AssertionError("excluded subtree content was read")
        with real_resolve(root, descriptor, relative, **kwargs) as binding:
            yield binding

    monkeypatch.setattr(source_fingerprint_module.os, "stat", reject_excluded_stat)
    monkeypatch.setattr(source_fingerprint_module.os, "open", reject_excluded_open)
    monkeypatch.setattr(
        source_fingerprint_module,
        "_resolved_repository_file_at",
        reject_excluded_read,
    )

    initial = fingerprint_repository(repo, selection=selection)
    excluded_file.write_bytes(b"EXCLUDED = 2\n")
    excluded_changed = fingerprint_repository(repo, selection=selection)
    (repo / "included.py").write_bytes(b"INCLUDED = 2\n")
    included_changed = fingerprint_repository(repo, selection=selection)

    assert initial.file_count == 1
    assert excluded_changed == initial
    assert included_changed.value != initial.value


def test_selection_and_internal_exclude_roots_remain_independent(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    generated = repo / "generated"
    internal = repo / "runtime-artifact"
    generated.mkdir(parents=True)
    internal.mkdir()
    (repo / "included.py").write_bytes(b"INCLUDED = 1\n")
    generated_file = generated / "excluded.py"
    generated_file.write_bytes(b"GENERATED = 1\n")
    internal_file = internal / "state.bin"
    internal_file.write_bytes(b"STATE = 1\n")
    selection = RepositorySourceSelection(("generated",))

    initial = fingerprint_repository(
        repo,
        exclude_roots=(internal,),
        selection=selection,
    )
    generated_file.write_bytes(b"GENERATED = 2\n")
    internal_file.write_bytes(b"STATE = 2\n")
    excluded_changed = fingerprint_repository(
        repo,
        exclude_roots=(internal,),
        selection=selection,
    )
    internal_included = fingerprint_repository(repo, selection=selection)

    assert initial == excluded_changed
    assert initial.file_count == 1
    assert internal_included.file_count == 2
    assert internal_included.value != initial.value


def test_selection_uses_same_exact_policy_for_initial_and_final_posix_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    generated = repo / "generated"
    generated.mkdir(parents=True)
    (repo / "included.py").write_bytes(b"INCLUDED = 1\n")
    excluded_file = generated / "excluded.py"
    excluded_file.write_bytes(b"EXCLUDED = 1\n")
    selection = RepositorySourceSelection(("generated",))
    real_scan = source_fingerprint_module._scan_pinned_repository
    observed: list[tuple[RepositorySourceSelection, bool]] = []

    def mutate_after_initial(
        descriptor,
        *,
        excluded,
        selection,
        collect_entries,
    ):
        scan = real_scan(
            descriptor,
            excluded=excluded,
            selection=selection,
            collect_entries=collect_entries,
        )
        observed.append((selection, collect_entries))
        if collect_entries:
            excluded_file.write_bytes(b"EXCLUDED = 2\n")
        return scan

    monkeypatch.setattr(
        source_fingerprint_module,
        "_scan_pinned_repository",
        mutate_after_initial,
    )

    fingerprint = fingerprint_repository(repo, selection=selection)

    assert fingerprint.file_count == 1
    assert observed == [(selection, True), (selection, False)]


def test_selection_walk_and_authenticated_records_have_path_parity(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "generated").mkdir(parents=True)
    (repo / "generated-extra").mkdir()
    (repo / "src" / "cache").mkdir(parents=True)
    (repo / "generated" / "excluded.py").write_bytes(b"EXCLUDED = 1\n")
    (repo / "generated-extra" / "included.py").write_bytes(b"INCLUDED = 1\n")
    (repo / "src" / "cache" / "excluded.py").write_bytes(b"EXCLUDED = 2\n")
    (repo / "src" / "included.py").write_bytes(b"INCLUDED = 2\n")
    selection = RepositorySourceSelection(("generated", "src/cache"))

    walked = {
        path.relative_to(repo).as_posix()
        for path in walk_repository_files(
            repo,
            selection=selection,
        )
    }
    with capture_repository_source(repo, selection=selection) as binding:
        captured = {record.path for record in binding.file_records}

    assert walked == captured == {"generated-extra/included.py", "src/included.py"}


def test_retained_binding_detaches_and_reuses_exact_selection(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    generated = repo / "generated"
    generated.mkdir(parents=True)
    included = repo / "included.py"
    included.write_bytes(b"INCLUDED = 1\n")
    excluded_file = generated / "excluded.py"
    excluded_file.write_bytes(b"EXCLUDED = 1\n")
    caller_selection = RepositorySourceSelection(("generated",))
    binding = capture_repository_source(repo, selection=caller_selection)

    object.__setattr__(caller_selection, "exclude_subtrees", ())
    first = binding.authenticated_identity_snapshot()
    assert first.source_selection == RepositorySourceSelection(("generated",))
    assert first.source_selection is not binding._selection
    assert first.source_selection is not None
    object.__setattr__(first.source_selection, "exclude_subtrees", ("forged",))
    second = binding.authenticated_identity_snapshot()
    assert second.source_selection == RepositorySourceSelection(("generated",))

    excluded_file.write_bytes(b"EXCLUDED = 2\n")
    binding.verify_snapshot()
    assert binding.read_bytes("included.py", max_bytes=64) == b"INCLUDED = 1\n"

    included.write_bytes(b"INCLUDED = changed\n")
    with pytest.raises(RepositoryChangedError, match="inventory changed"):
        binding.verify_snapshot()
    assert not binding.usable
    binding.close()


def test_legacy_manifest_v11_compat_identity_is_exact_and_explicit(
    tmp_path: Path,
) -> None:
    (tmp_path / "module.py").write_bytes(b"VALUE = 1\n")

    legacy = source_fingerprint_module._fingerprint_repository_legacy_manifest_v11(
        tmp_path
    )
    current = fingerprint_repository(tmp_path)

    assert legacy.value == (
        "sha256-v2:8756a1746b8447cee00a0d9e89bcb7c2" "8df91376f2dd3d457b8050be76283d17"
    )
    assert legacy.file_count == current.file_count == 1
    assert legacy.value != current.value

    expected = pin_repository_source_root(tmp_path)
    binding = source_fingerprint_module._capture_repository_source_legacy_manifest_v11(
        tmp_path,
        expected_root_authority=expected,
    )
    try:
        snapshot = binding.authenticated_identity_snapshot()
        assert snapshot.fingerprint == legacy.value
        assert snapshot.source_selection is None
        expected.close()
        assert binding.read_bytes("module.py", max_bytes=64) == b"VALUE = 1\n"
    finally:
        binding.close()
        if not expected.closed:
            expected.close()


def test_fingerprint_rejects_non_selection_policy(tmp_path: Path) -> None:
    (tmp_path / "module.py").write_bytes(b"VALUE = 1\n")

    with pytest.raises(TypeError, match="RepositorySourceSelection"):
        fingerprint_repository(tmp_path, selection=None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="RepositorySourceSelection"):
        capture_repository_source(tmp_path, selection=None)  # type: ignore[arg-type]


def test_windows_expected_root_authority_uses_binding_only_casefolded_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsSourceApi()
    api.add_file(api.root_id, "source.py", b"VALUE = 1\n")
    real_open_relative = api.open_relative

    def casefold_open_relative(
        parent_handle: int,
        name: str,
        **kwargs: object,
    ) -> int:
        parent_id = api.handles[parent_handle]
        matches = [
            candidate
            for candidate in api._children(parent_id)
            if candidate.casefold() == name.casefold()
        ]
        assert len(matches) == 1
        return real_open_relative(parent_handle, matches[0], **kwargs)

    def open_fake_root(
        root: Path,
        *,
        api: object | None = None,
        cleanup_slot: object | None = None,
    ):
        selected = api if api is not None else fake_api
        return contained_source_module._open_windows_pinned_repository_root(
            root,
            api=selected,
            cleanup_slot=cleanup_slot,
        )

    fake_api = api
    monkeypatch.setattr(api, "open_relative", casefold_open_relative)
    monkeypatch.setattr(
        source_fingerprint_module,
        "_open_windows_pinned_repository_root",
        open_fake_root,
    )
    monkeypatch.setattr(
        source_fingerprint_module,
        "sys",
        SimpleNamespace(platform="win32", exc_info=sys.exc_info),
    )

    expected = pin_repository_source_root(Path(r"C:\REPO"))
    api.nodes[api.root_id]["version"] = 2
    binding = capture_repository_source(
        Path(r"c:\repo"),
        expected_root_authority=expected,
    )
    try:
        assert binding.read_bytes("source.py", max_bytes=1024) == b"VALUE = 1\n"
    finally:
        binding.close()

    replacement = api.add_directory()
    api.add_file(replacement, "source.py", b"VALUE = 1\n")
    api._children(api.volume_id)["repo"] = replacement
    try:
        with pytest.raises(RepositoryChangedError, match="authority changed"):
            capture_repository_source(
                Path(r"c:\repo"),
                expected_root_authority=expected,
            )
    finally:
        expected.close()


def test_windows_fake_regular_tree_matches_posix_v1_and_v2(
    tmp_path: Path,
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (tmp_path / "module.py").write_bytes(b"VALUE = 1\n")
    (package / "nested.py").write_bytes(b"NESTED = 1\n")
    expected_v1 = fingerprint_repository_v1_for_diagnostics(tmp_path)
    expected_v2 = fingerprint_repository(tmp_path)

    api = _FakeWindowsSourceApi()
    api.add_file(api.root_id, "module.py", b"VALUE = 1\n")
    fake_package = api.add_directory(api.root_id, "package")
    api.add_file(fake_package, "nested.py", b"NESTED = 1\n")

    observed_v1 = source_fingerprint_module._fingerprint_windows_repository(
        r"C:\repo",
        exclude_roots=(),
        version=1,
        api=api,
    )
    observed_v2 = source_fingerprint_module._fingerprint_windows_repository(
        r"C:\repo",
        exclude_roots=(),
        version=SOURCE_FINGERPRINT_VERSION,
        api=api,
    )

    assert observed_v1 == expected_v1
    assert observed_v2 == expected_v2
    assert api.handles == {}
    assert set(api.created_paths) == {"C:\\"}


def test_windows_fake_selection_matches_posix_initial_and_final_scans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated = tmp_path / "generated"
    generated.mkdir()
    generated_extra = tmp_path / "generated-extra"
    generated_extra.mkdir()
    (tmp_path / "module.py").write_bytes(b"VALUE = 1\n")
    (generated / "excluded.py").write_bytes(b"EXCLUDED = 1\n")
    (generated_extra / "included.py").write_bytes(b"INCLUDED = 1\n")
    selection = RepositorySourceSelection(("generated",))
    expected_v1 = fingerprint_repository_v1_for_diagnostics(
        tmp_path,
        selection=selection,
    )
    expected_v2 = fingerprint_repository(tmp_path, selection=selection)

    api = _FakeWindowsSourceApi()
    api.add_file(api.root_id, "module.py", b"VALUE = 1\n")
    fake_generated = api.add_directory(api.root_id, "generated")
    api.add_file(fake_generated, "excluded.py", b"EXCLUDED = 1\n")
    fake_generated_extra = api.add_directory(api.root_id, "generated-extra")
    api.add_file(fake_generated_extra, "included.py", b"INCLUDED = 1\n")

    def reject_excluded_open(parent_id, name, file_id):
        if parent_id == api.root_id and name == "generated":
            raise AssertionError("excluded Windows subtree was opened")
        return file_id

    api.open_relative_hook = reject_excluded_open
    real_scan = source_fingerprint_module._scan_pinned_windows_repository
    observed: list[tuple[RepositorySourceSelection, bool]] = []

    def observe_scan(
        selected_api,
        root_handle,
        *,
        excluded,
        selection,
        collect_entries,
    ):
        scan = real_scan(
            selected_api,
            root_handle,
            excluded=excluded,
            selection=selection,
            collect_entries=collect_entries,
        )
        observed.append((selection, collect_entries))
        return scan

    monkeypatch.setattr(
        source_fingerprint_module,
        "_scan_pinned_windows_repository",
        observe_scan,
    )

    observed_v1 = source_fingerprint_module._fingerprint_windows_repository(
        r"C:\repo",
        exclude_roots=(),
        selection=selection,
        version=1,
        api=api,
    )
    observed_v2 = source_fingerprint_module._fingerprint_windows_repository(
        r"C:\repo",
        exclude_roots=(),
        selection=selection,
        version=SOURCE_FINGERPRINT_VERSION,
        api=api,
    )

    assert observed_v1 == expected_v1
    assert observed_v2 == expected_v2
    assert observed == [
        (selection, True),
        (selection, False),
        (selection, True),
        (selection, False),
    ]
    assert api.handles == {}


def test_windows_reparse_parser_is_bounded_and_exact() -> None:
    substitute = r"target.py".encode("utf-16-le")
    printable = r"target.py".encode("utf-16-le")
    path_buffer = substitute + printable
    data_length = 12 + len(path_buffer)
    payload = b"".join(
        (
            windows_fs_module.IO_REPARSE_TAG_SYMLINK.to_bytes(4, "little"),
            data_length.to_bytes(2, "little"),
            b"\0\0",
            (0).to_bytes(2, "little"),
            len(substitute).to_bytes(2, "little"),
            len(substitute).to_bytes(2, "little"),
            len(printable).to_bytes(2, "little"),
            windows_fs_module.SYMLINK_FLAG_RELATIVE.to_bytes(4, "little"),
            path_buffer,
        )
    )

    assert windows_fs_module.parse_windows_reparse_data(payload) == (
        windows_fs_module.WindowsReparsePoint(
            tag=windows_fs_module.IO_REPARSE_TAG_SYMLINK,
            substitute_name="target.py",
            print_name="target.py",
            flags=windows_fs_module.SYMLINK_FLAG_RELATIVE,
        )
    )
    with pytest.raises(RuntimeError, match="out of bounds"):
        windows_fs_module.parse_windows_reparse_data(
            payload[:10] + (0xFFFE).to_bytes(2, "little") + payload[12:]
        )


def test_windows_fake_public_contained_read_and_hash_close_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsSourceApi()
    api.add_file(api.root_id, "target.py", b"VALUE = 1\n")
    api.add_symlink(api.root_id, "current.py", "target.py")
    monkeypatch.setattr(contained_source_module.sys, "platform", "win32")
    monkeypatch.setattr(
        contained_source_module,
        "_windows_kernel_api",
        lambda: api,
    )
    monkeypatch.setattr(
        contained_source_module,
        "_shared_windows_require_source_api",
        lambda _api: None,
    )

    contained_source_module.validate_repository_file(
        r"C:\repo",
        "current.py",
    )
    assert (
        contained_source_module.read_repository_file(
            r"C:\repo",
            "current.py",
            max_bytes=64,
        )
        == b"VALUE = 1\n"
    )
    digest = hashlib.sha256()
    contained_source_module.update_repository_file_hash(
        r"C:\repo",
        "current.py",
        digest,
    )

    assert digest.hexdigest() == hashlib.sha256(b"VALUE = 1\n").hexdigest()
    assert api.handles == {}


def test_windows_fake_unbound_read_rejects_absolute_symlink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsSourceApi()
    api.add_file(api.root_id, "target.py", b"VALUE = 1\n")
    api.add_symlink(
        api.root_id,
        "current.py",
        r"C:\repo\target.py",
        absolute=True,
        substitute=r"\??\C:\repo\target.py",
    )
    monkeypatch.setattr(contained_source_module.sys, "platform", "win32")
    monkeypatch.setattr(contained_source_module, "_windows_kernel_api", lambda: api)
    monkeypatch.setattr(
        contained_source_module,
        "_shared_windows_require_source_api",
        lambda _api: None,
    )

    with pytest.raises(ValueError, match="target must be relative"):
        contained_source_module.read_repository_file(
            r"C:\repo",
            "current.py",
            max_bytes=64,
        )

    assert api.handles == {}


def test_windows_fake_relative_symlink_matches_posix_v1_and_v2(
    tmp_path: Path,
) -> None:
    (tmp_path / "target.py").write_bytes(b"VALUE = 1\n")
    try:
        (tmp_path / "current.py").symlink_to("target.py")
    except OSError as exc:  # pragma: no cover - platform capability
        pytest.skip(f"symlink parity fixture is unavailable: {exc}")
    expected_v1 = fingerprint_repository_v1_for_diagnostics(tmp_path)
    expected_v2 = fingerprint_repository(tmp_path)

    api = _FakeWindowsSourceApi()
    api.add_file(api.root_id, "target.py", b"VALUE = 1\n")
    api.add_symlink(api.root_id, "current.py", "target.py")

    assert (
        source_fingerprint_module._fingerprint_windows_repository(
            r"C:\repo",
            exclude_roots=(),
            version=1,
            api=api,
        )
        == expected_v1
    )
    assert (
        source_fingerprint_module._fingerprint_windows_repository(
            r"C:\repo",
            exclude_roots=(),
            version=SOURCE_FINGERPRINT_VERSION,
            api=api,
        )
        == expected_v2
    )
    assert api.handles == {}


def test_windows_fake_excludes_contained_directory_and_root_links() -> None:
    api = _FakeWindowsSourceApi()
    package = api.add_directory(api.root_id, "package")
    api.add_file(package, "module.py", b"VALUE = 1\n")
    baseline_v1 = source_fingerprint_module._fingerprint_windows_repository(
        r"C:\repo",
        exclude_roots=(),
        version=1,
        api=api,
    )
    baseline_v2 = source_fingerprint_module._fingerprint_windows_repository(
        r"C:\repo",
        exclude_roots=(),
        version=SOURCE_FINGERPRINT_VERSION,
        api=api,
    )
    api.add_symlink(
        api.root_id,
        "package-link",
        "package",
        directory=True,
    )
    api.add_symlink(api.root_id, "root-link", ".", directory=True)

    assert (
        source_fingerprint_module._fingerprint_windows_repository(
            r"C:\repo",
            exclude_roots=(),
            version=1,
            api=api,
        )
        == baseline_v1
    )
    assert (
        source_fingerprint_module._fingerprint_windows_repository(
            r"C:\repo",
            exclude_roots=(),
            version=SOURCE_FINGERPRINT_VERSION,
            api=api,
        )
        == baseline_v2
    )
    assert api.handles == {}


@pytest.mark.parametrize(
    ("root", "target", "substitute"),
    [
        (r"C:\repo", r"C:\repo\target.py", r"C:\repo\target.py"),
        (r"C:\repo", r"C:\repo\target.py", r"\??\C:\repo\target.py"),
        (r"C:\repo", r"C:\repo\target.py", r"\\?\C:\repo\target.py"),
        (r"C:\repo", r"C:\repo\target.py", r"\??\c:\REPO\target.py"),
        (r"\\?\C:\repo", r"C:\repo\target.py", r"\??\C:\repo\target.py"),
        (
            r"\\server\share\repo",
            r"\\server\share\repo\target.py",
            r"\\server\share\repo\target.py",
        ),
        (
            r"\\server\share\repo",
            r"\\server\share\repo\target.py",
            r"\??\UNC\server\share\repo\target.py",
        ),
        (
            r"\\server\share\repo",
            r"\\server\share\repo\target.py",
            r"\\?\UNC\server\share\repo\target.py",
        ),
        (
            r"\\?\UNC\server\share\repo",
            r"\\server\share\repo\target.py",
            r"\??\UNC\server\share\repo\target.py",
        ),
    ],
)
def test_windows_fake_accepts_absolute_contained_symlink(
    root: str,
    target: str,
    substitute: str,
) -> None:
    api = _FakeWindowsSourceApi()
    api.add_file(api.root_id, "target.py", b"VALUE = 1\n")
    api.add_symlink(
        api.root_id,
        "current.py",
        target,
        absolute=True,
        substitute=substitute,
    )

    observed = source_fingerprint_module._fingerprint_windows_repository(
        root,
        exclude_roots=(),
        version=SOURCE_FINGERPRINT_VERSION,
        api=api,
    )

    assert observed.file_count == 2
    assert observed.value.startswith("sha256-v2:")
    assert api.handles == {}


def test_windows_fake_absolute_symlink_retained_reads() -> None:
    api = _FakeWindowsSourceApi()
    api.add_file(api.root_id, "target.py", b"FIRST = 1\r\nSECOND = 2\r\n")
    api.add_symlink(
        api.root_id,
        "current.py",
        r"C:\repo\target.py",
        absolute=True,
        substitute=r"\??\C:\repo\target.py",
    )
    binding = source_fingerprint_module._fingerprint_windows_repository(
        r"C:\repo",
        exclude_roots=(),
        version=SOURCE_FINGERPRINT_VERSION,
        api=api,
        retain_binding=True,
    )
    assert isinstance(binding, RepositorySourceBinding)

    assert binding.read_bytes("current.py", max_bytes=1024).startswith(b"FIRST")
    assert binding.read_prefix("current.py", max_bytes=5) == b"FIRST"
    assert (
        binding.read_line_range(
            "current.py",
            start_line=2,
            end_line=2,
            max_bytes=1024,
        )
        == b"SECOND = 2\r\n"
    )
    binding.close()
    assert api.handles == {}


def test_windows_fake_binds_initial_symlink_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsSourceApi()
    api.add_file(api.root_id, "first.py", b"SAME\n")
    api.add_file(api.root_id, "second.py", b"SAME\n")
    link_id = api.add_symlink(api.root_id, "current.py", "first.py")
    original = api.nodes[link_id]["reparse"]
    replacement = windows_fs_module.WindowsReparsePoint(
        tag=windows_fs_module.IO_REPARSE_TAG_SYMLINK,
        substitute_name="second.py",
        print_name="second.py",
        flags=windows_fs_module.SYMLINK_FLAG_RELATIVE,
    )
    real_resolve = source_fingerprint_module._resolved_windows_repository_file_at
    swapped = False

    @contextmanager
    def swap_target(root, authority, relative, **kwargs):
        nonlocal swapped
        if swapped or relative != "current.py":
            with real_resolve(root, authority, relative, **kwargs) as binding:
                yield binding
            return
        swapped = True
        api.nodes[link_id]["reparse"] = replacement
        try:
            with real_resolve(root, authority, relative, **kwargs) as binding:
                yield binding
        finally:
            api.nodes[link_id]["reparse"] = original

    monkeypatch.setattr(
        source_fingerprint_module,
        "_resolved_windows_repository_file_at",
        swap_target,
    )

    with pytest.raises(RepositoryChangedError, match="read consistently"):
        source_fingerprint_module._fingerprint_windows_repository(
            r"C:\repo",
            exclude_roots=(),
            version=SOURCE_FINGERPRINT_VERSION,
            api=api,
        )

    assert swapped
    assert api.handles == {}


def test_windows_fake_binds_initial_symlink_substitute_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsSourceApi()
    api.add_file(api.root_id, "first.py", b"SAME\n")
    api.add_file(api.root_id, "second.py", b"SAME\n")
    link_id = api.add_symlink(
        api.root_id,
        "current.py",
        r"C:\repo\first.py",
        absolute=True,
        substitute=r"\??\C:\repo\first.py",
    )
    original = api.nodes[link_id]["reparse"]
    replacement = windows_fs_module.WindowsReparsePoint(
        tag=windows_fs_module.IO_REPARSE_TAG_SYMLINK,
        substitute_name=r"\??\C:\repo\second.py",
        print_name=r"C:\repo\first.py",
        flags=0,
    )
    real_resolve = source_fingerprint_module._resolved_windows_repository_file_at
    swapped = False

    @contextmanager
    def swap_substitute(root, authority, relative, **kwargs):
        nonlocal swapped
        if swapped or relative != "current.py":
            with real_resolve(root, authority, relative, **kwargs) as binding:
                yield binding
            return
        swapped = True
        api.nodes[link_id]["reparse"] = replacement
        try:
            with real_resolve(root, authority, relative, **kwargs) as binding:
                yield binding
        finally:
            api.nodes[link_id]["reparse"] = original

    monkeypatch.setattr(
        source_fingerprint_module,
        "_resolved_windows_repository_file_at",
        swap_substitute,
    )

    with pytest.raises(RepositoryChangedError, match="read consistently"):
        source_fingerprint_module._fingerprint_windows_repository(
            r"C:\repo",
            exclude_roots=(),
            version=SOURCE_FINGERPRINT_VERSION,
            api=api,
        )

    assert swapped
    assert api.handles == {}


def test_windows_fake_binding_revalidates_unresolved_excluded_target() -> None:
    api = _FakeWindowsSourceApi()
    hidden = api.add_directory(api.root_id, ".codenib-cache")
    api.add_symlink(api.root_id, "visible.py", r".codenib-cache\later.py")
    binding = source_fingerprint_module._fingerprint_windows_repository(
        r"C:\repo",
        exclude_roots=(),
        version=SOURCE_FINGERPRINT_VERSION,
        api=api,
        retain_binding=True,
    )
    assert isinstance(binding, RepositorySourceBinding)

    api.add_file(hidden, "later.py", b"VALUE = 1\n")

    with pytest.raises(RepositoryChangedError):
        binding.verify_snapshot()
    binding.close()
    assert api.handles == {}


@pytest.mark.parametrize(
    "target,absolute,substitute",
    [
        (r"..\outside.py", False, None),
        (r"C:\outside.py", True, r"\??\C:\outside.py"),
        (r"C:\repo\target.py", True, r"\??\D:\repo\target.py"),
        (r"C:\repo\target.py", True, r"\??\C:\outside.py"),
        (r"C:\repo\target.py", True, r"\??\C:\repository\target.py"),
        (r"C:\repo\target.py", True, r"\??\C:\repo\target.py:stream"),
        (
            r"C:\repo\target.py",
            True,
            r"\??\C:\repo\hidden:stream\..\target.py",
        ),
        (
            r"C:\repo\target.py",
            True,
            r"\??\C:\repo\..\repo\target.py",
        ),
        (r"C:\repo\target.py", True, r"\??\\\server\share\repo\target.py"),
        (r"C:\repo\target.py", True, r"\??\GLOBALROOT\Device\target.py"),
        (r"C:\repo\target.py", True, r"\\?\GLOBALROOT\Device\target.py"),
        (r"C:\repo\target.py", True, r"\??\Volume{abc}\repo\target.py"),
        (r"C:\repo\target.py", True, r"\??\UNCevil\repo\target.py"),
        (r"C:\repo\target.py", True, r"\??\C:/repo/target.py"),
        (r"C:\repo\target.py", True, "\\??\\Å:\\repo\\target.py"),
        (r"C:\repo\target.py", True, r"\\.\C:\repo\target.py"),
    ],
)
def test_windows_fake_rejects_outside_symlink_without_handle_leak(
    target: str,
    absolute: bool,
    substitute: str | None,
) -> None:
    api = _FakeWindowsSourceApi()
    api.add_symlink(
        api.root_id,
        "current.py",
        target,
        absolute=absolute,
        substitute=substitute,
    )

    with pytest.raises(RepositoryChangedError, match="read consistently"):
        source_fingerprint_module._fingerprint_windows_repository(
            r"C:\repo",
            exclude_roots=(),
            version=SOURCE_FINGERPRINT_VERSION,
            api=api,
        )

    assert api.handles == {}


@pytest.mark.parametrize(
    "substitute",
    [
        r"\??\UNC\server\share\repository\target.py",
        r"\??\UNC\server\other\repo\target.py",
        r"\??\UNC\other\share\repo\target.py",
    ],
)
def test_windows_fake_rejects_absolute_unc_component_collision(
    substitute: str,
) -> None:
    api = _FakeWindowsSourceApi()
    api.add_symlink(
        api.root_id,
        "current.py",
        r"\\server\share\repo\target.py",
        absolute=True,
        substitute=substitute,
    )

    with pytest.raises(RepositoryChangedError, match="read consistently"):
        source_fingerprint_module._fingerprint_windows_repository(
            r"\\server\share\repo",
            exclude_roots=(),
            version=SOURCE_FINGERPRINT_VERSION,
            api=api,
        )

    assert api.handles == {}


def test_windows_fake_broken_link_preserves_v1_link_only_semantics() -> None:
    api = _FakeWindowsSourceApi()
    api.add_symlink(api.root_id, "link.py", "missing.py")

    legacy = source_fingerprint_module._fingerprint_windows_repository(
        r"C:\repo",
        exclude_roots=(),
        version=1,
        api=api,
    )
    current = source_fingerprint_module._fingerprint_windows_repository(
        r"C:\repo",
        exclude_roots=(),
        version=SOURCE_FINGERPRINT_VERSION,
        api=api,
    )

    assert legacy.value == _legacy_link_only_digest(b"link.py", b"missing.py")
    assert legacy.file_count == current.file_count == 1
    assert current.value != legacy.value
    assert api.handles == {}


def test_windows_fake_rejects_junction_or_unknown_reparse() -> None:
    api = _FakeWindowsSourceApi()
    api.add_unknown_reparse(api.root_id, "unsafe")

    with pytest.raises(RepositoryChangedError, match="repository changed"):
        source_fingerprint_module._fingerprint_windows_repository(
            r"C:\repo",
            exclude_roots=(),
            version=SOURCE_FINGERPRINT_VERSION,
            api=api,
        )

    assert api.handles == {}


def test_windows_fake_rejects_zero_128_bit_entry_identity() -> None:
    api = _FakeWindowsSourceApi()
    api.add_file(api.root_id, "source.py", b"VALUE = 1\n")
    iter_directory = api.iter_directory

    def zero_identity(handle: int):
        for entry in iter_directory(handle):
            yield windows_fs_module.WindowsDirectoryEntry(
                name=entry.name,
                file_id=0,
                attributes=entry.attributes,
                file_id_128=b"\0" * 16,
                reparse_tag=entry.reparse_tag,
            )

    api.iter_directory = zero_identity  # type: ignore[method-assign]

    with pytest.raises(RepositoryChangedError, match="repository changed"):
        source_fingerprint_module._fingerprint_windows_repository(
            r"C:\repo",
            exclude_roots=(),
            version=SOURCE_FINGERPRINT_VERSION,
            api=api,
        )

    assert api.handles == {}


def test_windows_fake_rejects_root_a_to_b_to_a_rebind() -> None:
    api = _FakeWindowsSourceApi()
    api.add_file(api.root_id, "source.py", b"VALUE = 'owned'\n")
    foreign = api.add_directory()
    api.add_file(foreign, "source.py", b"VALUE = 'foreign'\n")
    injected = False

    def reversible_swap(parent: bytes, name: str, original: bytes) -> bytes:
        nonlocal injected
        if parent == api.volume_id and name == "repo" and not injected:
            injected = True
            return foreign
        return original

    api.open_relative_hook = reversible_swap

    with pytest.raises(RepositoryChangedError, match="repository changed"):
        source_fingerprint_module._fingerprint_windows_repository(
            r"C:\repo",
            exclude_roots=(),
            version=SOURCE_FINGERPRINT_VERSION,
            api=api,
        )

    assert injected
    assert api.handles == {}


def test_windows_fake_authority_rejects_late_root_a_to_b_to_a_rebind() -> None:
    api = _FakeWindowsSourceApi()
    foreign = api.add_directory()
    authority = windows_fs_module.open_lexical_directory_authority(
        r"C:\repo",
        api=api,
    )
    iter_directory = api.iter_directory
    injected = False

    def reversible_rebind(handle: int):
        nonlocal injected
        if api.handles[handle] == api.volume_id and not injected:
            injected = True
            yield windows_fs_module.WindowsDirectoryEntry(
                name="repo",
                file_id=int.from_bytes(foreign[:8], "little"),
                attributes=windows_fs_module.FILE_ATTRIBUTE_DIRECTORY,
                file_id_128=foreign,
            )
            return
        yield from iter_directory(handle)

    api.iter_directory = reversible_rebind  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="binding changed"):
            authority.verify()
    finally:
        authority.close()

    assert injected
    assert api.handles == {}


def test_windows_fake_rejects_walker_to_open_foreign_replacement() -> None:
    api = _FakeWindowsSourceApi()
    api.add_file(api.root_id, "source.py", b"VALUE = 'owned'\n")
    foreign = api.add_file(api.root_id, ".foreign", b"VALUE = 'foreign'\n")
    injected = False

    def substitute_open(parent: bytes, name: str, original: bytes) -> bytes:
        nonlocal injected
        if parent == api.root_id and name == "source.py" and not injected:
            injected = True
            return foreign
        return original

    api.open_relative_hook = substitute_open

    with pytest.raises(RepositoryChangedError, match="repository changed"):
        source_fingerprint_module._fingerprint_windows_repository(
            r"C:\repo",
            exclude_roots=(),
            version=SOURCE_FINGERPRINT_VERSION,
            api=api,
        )

    assert injected
    assert api.handles == {}


def test_windows_fake_lexical_root_rejects_intermediate_reparse() -> None:
    api = _FakeWindowsSourceApi()
    api.add_symlink(
        api.volume_id,
        "alias",
        "repo",
        directory=True,
    )

    with pytest.raises(RepositoryChangedError, match="repository changed"):
        source_fingerprint_module._fingerprint_windows_repository(
            r"C:\alias\repo",
            exclude_roots=(),
            version=SOURCE_FINGERPRINT_VERSION,
            api=api,
        )

    assert api.handles == {}


def test_windows_fake_baseexception_during_reparse_query_closes_handles() -> None:
    api = _FakeWindowsSourceApi()
    api.add_symlink(api.root_id, "current.py", "missing.py")

    def interrupt(_point):
        raise KeyboardInterrupt("injected reparse interruption")

    api.query_reparse_hook = interrupt

    with pytest.raises(KeyboardInterrupt, match="injected"):
        source_fingerprint_module._fingerprint_windows_repository(
            r"C:\repo",
            exclude_roots=(),
            version=SOURCE_FINGERPRINT_VERSION,
            api=api,
        )

    assert api.handles == {}


def test_windows_fake_scan_eio_keeps_primary_and_partial_handle_owner() -> None:
    api = _FakeWindowsSourceApi()
    api.add_symlink(api.root_id, "current.py", "missing.py")
    interruption = SystemExit("injected Windows scan failure")
    real_query = api.query_reparse_point
    real_close = api.close
    failed = 0

    def interrupt_query(handle: int):
        nonlocal failed
        failed = handle
        raise interruption

    def persistent_close(handle: int) -> None:
        if handle == failed:
            raise OSError(errno.EIO, "injected Windows scan close EIO")
        real_close(handle)

    api.query_reparse_point = interrupt_query  # type: ignore[method-assign]
    api.close = persistent_close  # type: ignore[method-assign]

    with pytest.raises(SystemExit) as caught:
        source_fingerprint_module._fingerprint_windows_repository(
            r"C:\repo",
            exclude_roots=(),
            version=SOURCE_FINGERPRINT_VERSION,
            api=api,
        )

    assert caught.value is interruption
    cleanup_owner = caught.value.source_cleanup_owner
    assert not cleanup_owner.closed
    assert failed in api.handles

    api.query_reparse_point = real_query  # type: ignore[method-assign]
    api.close = real_close  # type: ignore[method-assign]
    cleanup_owner.close()
    assert cleanup_owner.closed
    assert api.handles == {}


def test_windows_resolution_construction_eio_keeps_primary_and_retry_owner() -> None:
    api = _FakeWindowsSourceApi()
    api.add_symlink(api.root_id, "current.py", "missing.py")
    (
        selected,
        authority,
        root_identity,
    ) = contained_source_module._open_windows_pinned_repository_root(
        Path(r"C:\repo"),
        api=api,
    )
    scan = source_fingerprint_module._scan_pinned_windows_repository(
        selected,
        authority.handle,
        excluded=(),
        collect_entries=True,
    )
    record = scan.entries[0]
    real_query = api.query_reparse_point
    real_close = api.close
    interruption = SystemExit("injected Windows resolution failure")
    failed = 0

    def interrupt_query(handle: int):
        nonlocal failed
        failed = handle
        raise interruption

    def persistent_close(handle: int) -> None:
        if handle == failed:
            raise OSError(errno.EIO, "injected Windows resolution close EIO")
        real_close(handle)

    api.query_reparse_point = interrupt_query  # type: ignore[method-assign]
    api.close = persistent_close  # type: ignore[method-assign]

    with pytest.raises(SystemExit) as caught:
        contained_source_module._open_windows_resolution_at(
            Path(r"C:\repo"),
            authority,
            ("current.py",),
            expected_root_identity=root_identity,
            expected_final_identity=(
                source_fingerprint_module._entry_version_identity(record.metadata)
            ),
            allow_stable_unresolved=True,
            api=selected,
        )

    assert caught.value is interruption
    cleanup_owner = caught.value.source_cleanup_owner
    assert not cleanup_owner.closed
    assert failed in api.handles

    api.query_reparse_point = real_query  # type: ignore[method-assign]
    api.close = real_close  # type: ignore[method-assign]
    cleanup_owner.close()
    authority.close()
    assert cleanup_owner.closed
    assert api.handles == {}


@pytest.mark.parametrize(
    ("timing", "interruption"),
    [
        pytest.param(
            "before",
            KeyboardInterrupt("injected before resolution HANDLE close"),
            id="before-close",
        ),
        pytest.param(
            "after",
            SystemExit("injected after resolution HANDLE close"),
            id="after-close",
        ),
    ],
)
def test_windows_fake_resolution_close_finishes_chain_before_cancellation(
    timing: str,
    interruption: BaseException,
) -> None:
    api = _FakeWindowsSourceApi()
    api.add_file(api.root_id, "source.py", b"VALUE = 1\n")
    authority, binding = _open_fake_windows_resolution(api, "source.py")
    targets = set(binding.handles)
    real_close = api.close
    injected = False
    seen: set[int] = set()

    def interrupt_one_close(handle: int) -> None:
        nonlocal injected
        if handle in targets:
            seen.add(handle)
            if not injected:
                injected = True
                if timing == "before":
                    raise interruption
                real_close(handle)
                raise interruption
        real_close(handle)

    api.close = interrupt_one_close  # type: ignore[method-assign]
    with pytest.raises(type(interruption)) as caught:
        binding.close()

    assert caught.value is interruption
    assert injected
    assert binding.closed
    assert binding.handles == []
    assert targets <= seen
    assert targets.isdisjoint(api.handles)

    api.close = real_close  # type: ignore[method-assign]
    authority.close()
    assert api.handles == {}


def test_windows_fake_resolution_close_persistent_eio_retains_retry_owner() -> None:
    api = _FakeWindowsSourceApi()
    api.add_file(api.root_id, "source.py", b"VALUE = 1\n")
    authority, binding = _open_fake_windows_resolution(api, "source.py")
    targets = tuple(binding.handles)
    failed = targets[-1]
    real_close = api.close

    def persistent_eio(handle: int) -> None:
        if handle == failed:
            raise OSError(errno.EIO, "injected resolution HANDLE EIO")
        real_close(handle)

    api.close = persistent_eio  # type: ignore[method-assign]
    with pytest.raises(OSError, match="resolution HANDLE EIO"):
        binding.close()

    assert not binding.closed
    assert binding.handles == [failed]
    assert set(api.handles) == {failed, *authority.handles}
    with pytest.raises(ValueError, match="closed"):
        binding.verify()

    api.close = real_close  # type: ignore[method-assign]
    binding.close()
    assert binding.closed
    authority.close()
    assert api.handles == {}


@pytest.mark.parametrize("case", ["regular", "link-loop", "directory", "missing"])
@pytest.mark.parametrize("timing", ["before-store", "after-store"])
def test_windows_resolution_binding_slot_store_cancellation_closes_every_branch(
    case: str,
    timing: str,
) -> None:
    (
        api,
        authority,
        root_identity,
        expected_identity,
    ) = _fake_windows_resolution_handoff_case(case)
    interruption = KeyboardInterrupt(f"injected {case} {timing}")

    class InterruptingSlot(contained_source_module._SourceCleanupSlot):
        def own(self, resource: object) -> None:
            is_binding = isinstance(
                resource,
                contained_source_module._WindowsResolutionBinding,
            )
            if is_binding and timing == "before-store":
                raise interruption
            super().own(resource)
            if is_binding and timing == "after-store":
                raise interruption

    slot = InterruptingSlot()
    with pytest.raises(KeyboardInterrupt) as caught:
        contained_source_module._open_windows_resolution_at(
            Path(r"C:\repo"),
            authority,
            ("current.py",),
            expected_root_identity=root_identity,
            expected_final_identity=expected_identity,
            allow_stable_unresolved=True,
            api=api,
            cleanup_slot=slot,
        )

    assert caught.value is interruption
    assert slot.closed
    assert set(api.handles) == set(authority.handles)
    authority.close()
    assert api.handles == {}


def test_windows_resolution_completed_binding_cancellation_closes_slot_owner() -> None:
    (
        api,
        authority,
        root_identity,
        expected_identity,
    ) = _fake_windows_resolution_handoff_case("regular")
    interruption = SystemExit("injected after completed resolution handoff")

    class InterruptingSlot(contained_source_module._SourceCleanupSlot):
        def own(self, resource: object) -> None:
            super().own(resource)
            if isinstance(resource, contained_source_module._WindowsResolutionBinding):
                raise interruption

    slot = InterruptingSlot()
    with pytest.raises(SystemExit) as caught:
        contained_source_module._open_windows_resolution_at(
            Path(r"C:\repo"),
            authority,
            ("current.py",),
            expected_root_identity=root_identity,
            expected_final_identity=expected_identity,
            allow_stable_unresolved=True,
            api=api,
            cleanup_slot=slot,
        )

    assert caught.value is interruption
    assert slot.closed
    assert set(api.handles) == set(authority.handles)
    authority.close()
    assert api.handles == {}


def test_windows_resolution_handoff_eio_retains_owner_without_double_close() -> None:
    (
        api,
        authority,
        root_identity,
        expected_identity,
    ) = _fake_windows_resolution_handoff_case("regular")
    interruption = KeyboardInterrupt("injected resolution handoff cancellation")
    real_close = api.close
    failed = 0
    close_calls: dict[int, int] = {}
    armed = False

    class InterruptingSlot(contained_source_module._SourceCleanupSlot):
        def own(self, resource: object) -> None:
            nonlocal armed
            super().own(resource)
            if isinstance(resource, contained_source_module._WindowsResolutionBinding):
                armed = True
                raise interruption

    slot = InterruptingSlot()

    def persistent_close(handle: int) -> None:
        nonlocal failed
        if not armed:
            real_close(handle)
            return
        close_calls[handle] = close_calls.get(handle, 0) + 1
        if handle not in authority.handles and not failed:
            failed = handle
        if handle == failed:
            raise OSError(errno.EIO, "injected resolution return close EIO")
        real_close(handle)

    api.close = persistent_close  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt) as caught:
        contained_source_module._open_windows_resolution_at(
            Path(r"C:\repo"),
            authority,
            ("current.py",),
            expected_root_identity=root_identity,
            expected_final_identity=expected_identity,
            allow_stable_unresolved=True,
            api=api,
            cleanup_slot=slot,
        )

    assert caught.value is interruption
    assert caught.value.source_cleanup_owner is slot
    assert failed in api.handles
    closed_once = {
        handle
        for handle, calls in close_calls.items()
        if handle != failed and calls == 1
    }
    assert closed_once

    api.close = real_close  # type: ignore[method-assign]
    slot.close()
    assert slot.closed
    assert set(api.handles) == set(authority.handles)
    assert all(close_calls[handle] == 1 for handle in closed_once)
    authority.close()
    assert api.handles == {}


@pytest.mark.parametrize(
    "interruption",
    [
        pytest.param(KeyboardInterrupt("after final resolution close"), id="keyboard"),
        pytest.param(SystemExit("after final resolution close"), id="system-exit"),
    ],
)
def test_windows_resolution_close_retry_publishes_closed_after_empty_handoff(
    monkeypatch: pytest.MonkeyPatch,
    interruption: BaseException,
) -> None:
    api = _FakeWindowsSourceApi()
    api.add_file(api.root_id, "source.py", b"VALUE = 1\n")
    authority, binding = _open_fake_windows_resolution(api, "source.py")
    real_close_handles = contained_source_module._close_windows_handles
    injected = False

    def close_then_interrupt(*args, **kwargs):
        nonlocal injected
        failure = real_close_handles(*args, **kwargs)
        if not injected:
            injected = True
            raise interruption
        return failure

    monkeypatch.setattr(
        contained_source_module,
        "_close_windows_handles",
        close_then_interrupt,
    )
    with pytest.raises(type(interruption)) as caught:
        binding.close()

    assert caught.value is interruption
    assert binding.handles == []
    assert not binding.closed

    binding.close()
    assert binding.closed
    authority.close()
    assert api.handles == {}


def test_windows_fake_resolution_close_uses_stable_handle_cookie() -> None:
    api = _FakeWindowsSourceApi()
    file_id = api.add_file(api.root_id, "source.py", b"VALUE = 1\n")
    authority, binding = _open_fake_windows_resolution(api, "source.py")
    api.nodes[file_id]["version"] = 2

    binding.close()

    assert binding.closed
    authority.close()
    assert api.handles == {}


@pytest.mark.parametrize(
    ("timing", "interruption"),
    [
        pytest.param(
            "before",
            KeyboardInterrupt("injected before HANDLE close"),
            id="before-close",
        ),
        pytest.param(
            "after",
            SystemExit("injected after HANDLE close"),
            id="after-close",
        ),
    ],
)
def test_windows_fake_binding_close_finishes_handle_chain_before_cancellation(
    timing: str,
    interruption: BaseException,
) -> None:
    api = _FakeWindowsSourceApi()
    api.add_file(api.root_id, "source.py", b"VALUE = 1\n")
    binding = source_fingerprint_module._fingerprint_windows_repository(
        r"C:\repo",
        exclude_roots=(),
        version=SOURCE_FINGERPRINT_VERSION,
        api=api,
        retain_binding=True,
    )
    assert isinstance(binding, RepositorySourceBinding)
    targets = set(api.handles)
    real_close = api.close
    calls = 0
    injected = False
    seen: set[int] = set()

    def interrupt_one_close(handle: int) -> None:
        nonlocal calls, injected
        if handle in targets:
            seen.add(handle)
            calls += 1
            if calls == 2 and not injected:
                injected = True
                if timing == "before":
                    raise interruption
                real_close(handle)
                raise interruption
        real_close(handle)

    api.close = interrupt_one_close  # type: ignore[method-assign]
    with pytest.raises(type(interruption)) as caught:
        binding.close()

    assert caught.value is interruption
    assert injected
    assert binding.closed
    assert targets <= seen
    assert api.handles == {}


def test_windows_fake_binding_close_commits_interrupted_owner_pop() -> None:
    api = _FakeWindowsSourceApi()
    api.add_file(api.root_id, "source.py", b"VALUE = 1\n")
    binding = source_fingerprint_module._fingerprint_windows_repository(
        r"C:\repo",
        exclude_roots=(),
        version=SOURCE_FINGERPRINT_VERSION,
        api=api,
        retain_binding=True,
    )
    assert isinstance(binding, RepositorySourceBinding)
    authority = binding._windows_authority
    assert authority is not None
    interruption = SystemExit("injected after HANDLE owner pop")
    authority.handles = _InterruptAfterPopList(
        authority.handles,
        interruption,
    )

    with pytest.raises(SystemExit) as caught:
        binding.close()

    assert caught.value is interruption
    assert binding.closed
    assert api.handles == {}


@pytest.mark.parametrize("timing", ["before-binding-store", "after-binding-store"])
def test_windows_repository_binding_handoff_cancellation_closes_slot_owner(
    timing: str,
) -> None:
    api = _FakeWindowsSourceApi()
    api.add_file(api.root_id, "source.py", b"VALUE = 1\n")
    interruption = SystemExit(f"injected repository {timing} cancellation")

    class InterruptingSlot(contained_source_module._SourceCleanupSlot):
        def own(self, resource: object) -> None:
            is_binding = isinstance(resource, RepositorySourceBinding)
            if is_binding and timing == "before-binding-store":
                raise interruption
            super().own(resource)
            if is_binding and timing == "after-binding-store":
                raise interruption

    slot = InterruptingSlot()
    with pytest.raises(SystemExit) as caught:
        source_fingerprint_module._fingerprint_windows_repository(
            r"C:\repo",
            exclude_roots=(),
            version=SOURCE_FINGERPRINT_VERSION,
            api=api,
            retain_binding=True,
            cleanup_slot=slot,
        )

    assert caught.value is interruption
    assert slot.pending_sources == ()
    slot.close()
    assert slot.closed
    assert api.handles == {}


def test_windows_repository_return_eio_attaches_retry_owner_without_double_close() -> (
    None
):
    api = _FakeWindowsSourceApi()
    api.add_file(api.root_id, "source.py", b"VALUE = 1\n")
    interruption = KeyboardInterrupt("injected repository binding cancellation")
    real_close = api.close
    failed = 0
    close_calls: dict[int, int] = {}
    armed = False

    class InterruptingSlot(contained_source_module._SourceCleanupSlot):
        def own(self, resource: object) -> None:
            nonlocal armed
            super().own(resource)
            if isinstance(resource, RepositorySourceBinding):
                armed = True
                raise interruption

    slot = InterruptingSlot()

    def persistent_close(handle: int) -> None:
        nonlocal failed
        if not armed:
            real_close(handle)
            return
        close_calls[handle] = close_calls.get(handle, 0) + 1
        if not failed:
            failed = handle
        if handle == failed:
            raise OSError(errno.EIO, "injected repository return close EIO")
        real_close(handle)

    api.close = persistent_close  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt) as caught:
        source_fingerprint_module._fingerprint_windows_repository(
            r"C:\repo",
            exclude_roots=(),
            version=SOURCE_FINGERPRINT_VERSION,
            api=api,
            retain_binding=True,
            cleanup_slot=slot,
        )

    assert caught.value is interruption
    retry_owner = caught.value.source_cleanup_owner
    assert not retry_owner.closed
    assert failed in api.handles
    closed_once = {
        handle
        for handle, calls in close_calls.items()
        if handle != failed and calls == 1
    }
    assert closed_once

    api.close = real_close  # type: ignore[method-assign]
    slot.close()
    assert slot.closed
    assert api.handles == {}
    assert all(close_calls[handle] == 1 for handle in closed_once)


@pytest.mark.parametrize(
    "interruption",
    [
        pytest.param(KeyboardInterrupt("after final authority close"), id="keyboard"),
        pytest.param(SystemExit("after final authority close"), id="system-exit"),
    ],
)
def test_windows_binding_close_retry_converges_after_empty_authority_handoff(
    monkeypatch: pytest.MonkeyPatch,
    interruption: BaseException,
) -> None:
    api = _FakeWindowsSourceApi()
    api.add_file(api.root_id, "source.py", b"VALUE = 1\n")
    binding = source_fingerprint_module._fingerprint_windows_repository(
        r"C:\repo",
        exclude_roots=(),
        version=SOURCE_FINGERPRINT_VERSION,
        api=api,
        retain_binding=True,
    )
    assert isinstance(binding, RepositorySourceBinding)
    authority = binding._windows_authority
    assert authority is not None
    real_close_handles = windows_fs_module._close_windows_handles
    injected = False

    def close_then_interrupt(*args, **kwargs):
        nonlocal injected
        failure = real_close_handles(*args, **kwargs)
        if not injected:
            injected = True
            raise interruption
        return failure

    monkeypatch.setattr(
        windows_fs_module,
        "_close_windows_handles",
        close_then_interrupt,
    )
    with pytest.raises(type(interruption)) as caught:
        binding.close()

    assert caught.value is interruption
    assert authority.handles == []
    assert not authority.closed
    assert not binding.closed

    binding.close()
    assert authority.closed
    assert binding.closed
    assert api.handles == {}


def test_windows_fake_binding_close_persistent_eio_keeps_owner_and_closes_rest() -> (
    None
):
    api = _FakeWindowsSourceApi()
    api.add_file(api.root_id, "source.py", b"VALUE = 1\n")
    binding = source_fingerprint_module._fingerprint_windows_repository(
        r"C:\repo",
        exclude_roots=(),
        version=SOURCE_FINGERPRINT_VERSION,
        api=api,
        retain_binding=True,
    )
    assert isinstance(binding, RepositorySourceBinding)
    authority = binding._windows_authority
    assert authority is not None
    targets = tuple(authority.handles)
    failed = targets[-1]
    real_close = api.close

    def fail_one_handle(handle: int) -> None:
        if handle == failed:
            raise OSError(errno.EIO, "injected persistent HANDLE EIO")
        real_close(handle)

    api.close = fail_one_handle  # type: ignore[method-assign]
    with pytest.raises(OSError, match="persistent HANDLE EIO"):
        binding.close()

    assert not binding.closed
    assert not binding.usable
    assert authority.handles == [failed]
    assert set(api.handles) == {failed}

    api.close = real_close  # type: ignore[method-assign]
    binding.close()
    assert binding.closed
    assert api.handles == {}


def test_windows_fake_binding_close_preflight_eio_keeps_owner_and_closes_rest() -> None:
    api = _FakeWindowsSourceApi()
    api.add_file(api.root_id, "source.py", b"VALUE = 1\n")
    binding = source_fingerprint_module._fingerprint_windows_repository(
        r"C:\repo",
        exclude_roots=(),
        version=SOURCE_FINGERPRINT_VERSION,
        api=api,
        retain_binding=True,
    )
    assert isinstance(binding, RepositorySourceBinding)
    authority = binding._windows_authority
    assert authority is not None
    targets = tuple(authority.handles)
    failed = targets[-1]
    real_metadata = api.metadata

    def fail_one_handle(handle: int):
        if handle == failed:
            raise OSError(errno.EIO, "injected persistent HANDLE metadata EIO")
        return real_metadata(handle)

    api.metadata = fail_one_handle  # type: ignore[method-assign]
    with pytest.raises(OSError, match="persistent HANDLE metadata EIO"):
        binding.close()

    assert not binding.closed
    assert not binding.usable
    assert authority.handles == [failed]
    assert set(api.handles) == {failed}

    api.metadata = real_metadata  # type: ignore[method-assign]
    binding.close()
    assert binding.closed
    assert api.handles == {}


def test_windows_fake_binding_close_uses_stable_handle_identity() -> None:
    api = _FakeWindowsSourceApi()
    api.add_file(api.root_id, "source.py", b"VALUE = 1\n")
    binding = source_fingerprint_module._fingerprint_windows_repository(
        r"C:\repo",
        exclude_roots=(),
        version=SOURCE_FINGERPRINT_VERSION,
        api=api,
        retain_binding=True,
    )
    assert isinstance(binding, RepositorySourceBinding)
    api.nodes[api.root_id]["version"] = 2

    binding.close()

    assert binding.closed
    assert api.handles == {}


def test_windows_fake_binding_close_does_not_close_reused_handle() -> None:
    api = _FakeWindowsSourceApi()
    api.add_file(api.root_id, "source.py", b"VALUE = 1\n")
    binding = source_fingerprint_module._fingerprint_windows_repository(
        r"C:\repo",
        exclude_roots=(),
        version=SOURCE_FINGERPRINT_VERSION,
        api=api,
        retain_binding=True,
    )
    assert isinstance(binding, RepositorySourceBinding)
    authority = binding._windows_authority
    assert authority is not None
    reused = authority.handles[-1]
    replacement_id = api.add_directory()
    api.close(reused)
    api.handles[reused] = replacement_id
    api.offsets[reused] = 0

    with pytest.raises(RuntimeError, match="ownership changed"):
        binding.close()

    assert binding.closed
    assert api.handles == {reused: replacement_id}
    api.close(reused)


def test_windows_fake_binding_close_preserves_observable_reuse_after_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsSourceApi()
    api.add_file(api.root_id, "source.py", b"VALUE = 1\n")
    binding = source_fingerprint_module._fingerprint_windows_repository(
        r"C:\repo",
        exclude_roots=(),
        version=SOURCE_FINGERPRINT_VERSION,
        api=api,
        retain_binding=True,
    )
    assert isinstance(binding, RepositorySourceBinding)
    authority = binding._windows_authority
    assert authority is not None
    target = authority.handles[-1]
    replacement_id = api.add_directory()
    real_close = api.close
    interruption = KeyboardInterrupt("injected after observable HANDLE reuse")
    replaced = False

    def close_then_reuse(handle: int) -> None:
        nonlocal replaced
        real_close(handle)
        if handle == target and not replaced:
            api.handles[target] = replacement_id
            api.offsets[target] = 0
            replaced = True
            raise interruption

    monkeypatch.setattr(api, "close", close_then_reuse)
    with pytest.raises(KeyboardInterrupt) as caught:
        binding.close()

    assert caught.value is interruption
    assert binding.closed
    assert api.handles == {target: replacement_id}
    binding.close()
    assert api.handles == {target: replacement_id}
    monkeypatch.setattr(api, "close", real_close)
    real_close(target)


def test_windows_kernel_close_checks_closehandle_result() -> None:
    class CloseFailureApi(windows_fs_module.WindowsKernelApi):
        def _raise_last_error(self, context: str) -> None:
            raise OSError(errno.EIO, context)

    api = object.__new__(CloseFailureApi)
    api.kernel32 = SimpleNamespace(CloseHandle=lambda _handle: 0)
    api.invalid_handle = -1

    with pytest.raises(OSError, match="could not close"):
        api.close(41)


@pytest.mark.parametrize(
    "interruption",
    [
        pytest.param(KeyboardInterrupt("Windows root interrupted"), id="keyboard"),
        pytest.param(SystemExit("Windows root exited"), id="system-exit"),
    ],
)
def test_windows_fake_root_open_failure_closes_owned_handles(
    interruption: BaseException,
) -> None:
    api = _FakeWindowsSourceApi()
    real_metadata = api.metadata
    calls = 0

    def interrupt_after_child_open(handle: int):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise interruption
        return real_metadata(handle)

    api.metadata = interrupt_after_child_open  # type: ignore[method-assign]
    with pytest.raises(type(interruption)) as caught:
        windows_fs_module.open_lexical_directory_authority(
            r"C:\repo",
            api=api,
        )

    assert caught.value is interruption
    assert api.handles == {}


@pytest.mark.parametrize(
    "interruption",
    [
        pytest.param(KeyboardInterrupt("after direct authority close"), id="keyboard"),
        pytest.param(SystemExit("after direct authority close"), id="system-exit"),
    ],
)
def test_windows_authority_close_retry_converges_after_last_handle_removed(
    monkeypatch: pytest.MonkeyPatch,
    interruption: BaseException,
) -> None:
    api = _FakeWindowsSourceApi()
    authority = windows_fs_module.open_lexical_directory_authority(
        r"C:\repo",
        api=api,
    )
    real_close_handles = windows_fs_module._close_windows_handles
    injected = False

    def close_then_interrupt(*args, **kwargs):
        nonlocal injected
        failure = real_close_handles(*args, **kwargs)
        if not injected:
            injected = True
            raise interruption
        return failure

    monkeypatch.setattr(
        windows_fs_module,
        "_close_windows_handles",
        close_then_interrupt,
    )
    with pytest.raises(type(interruption)) as caught:
        authority.close()

    assert caught.value is interruption
    assert authority.handles == []
    assert not authority.closed

    authority.close()
    assert authority.closed
    assert api.handles == {}


def test_windows_root_construction_eio_keeps_preinstalled_retry_owner() -> None:
    api = _FakeWindowsSourceApi()
    owner = SourceBindingCleanupOwner()
    real_metadata = api.metadata
    real_close = api.close
    interruption = KeyboardInterrupt("injected Windows root construction failure")
    metadata_calls = 0
    failed = 0

    def interrupt_metadata(handle: int):
        nonlocal metadata_calls
        metadata_calls += 1
        if metadata_calls == 3:
            raise interruption
        return real_metadata(handle)

    def persistent_close(handle: int) -> None:
        nonlocal failed
        if not failed:
            failed = handle
        if handle == failed:
            raise OSError(errno.EIO, "injected Windows construction close EIO")
        real_close(handle)

    api.metadata = interrupt_metadata  # type: ignore[method-assign]
    api.close = persistent_close  # type: ignore[method-assign]

    with pytest.raises(KeyboardInterrupt) as caught:
        source_fingerprint_module._fingerprint_windows_repository(
            r"C:\repo",
            exclude_roots=(),
            version=SOURCE_FINGERPRINT_VERSION,
            api=api,
            retain_binding=True,
            source_owner=owner.retain,
        )

    assert caught.value is interruption
    assert not owner.closed
    assert failed in api.handles

    api.metadata = real_metadata  # type: ignore[method-assign]
    api.close = real_close  # type: ignore[method-assign]
    owner.close()
    assert owner.closed
    assert api.handles == {}


def test_fingerprint_ignores_generated_and_explicit_artifact_roots(tmp_path):
    (tmp_path / "module.py").write_text("VALUE = 1\n")
    initial = fingerprint_repository(tmp_path)

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "wheel.whl").write_bytes(b"generated")
    cache = tmp_path / "custom-cache"
    cache.mkdir()
    (cache / "index.bin").write_bytes(b"artifact")

    assert (
        fingerprint_repository(tmp_path, exclude_roots=(cache,)).value == initial.value
    )


def test_fingerprint_ignores_git_worktree_pointer_file(tmp_path):
    (tmp_path / "module.py").write_text("VALUE = 1\n")
    initial = fingerprint_repository(tmp_path)

    (tmp_path / ".git").write_text("gitdir: /shared/repository/.git/worktrees/test\n")

    observed = fingerprint_repository(tmp_path)
    assert observed == initial


def test_fingerprint_includes_symlink_target_and_content(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "target.py"
    target.write_text("VALUE = 1\n")
    link = repo / "current.py"
    link.symlink_to("target.py")
    initial = fingerprint_repository(repo)

    target.write_text("VALUE = 2\n")
    changed_content = fingerprint_repository(repo)
    assert changed_content.value != initial.value

    link.unlink()
    (repo / "other.py").write_text("VALUE = 3\n")
    link.symlink_to("other.py")
    assert fingerprint_repository(repo).value != changed_content.value


@pytest.mark.parametrize("terminal", ["missing", "fifo", "loop"])
def test_fingerprint_preserves_v1_link_only_terminal_digest(
    tmp_path: Path,
    terminal: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    link = repo / "link.py"
    if terminal == "missing":
        target = "missing.py"
    elif terminal == "fifo":
        target = ".codenib-hidden-pipe"
        os.mkfifo(repo / target)
    else:
        target = "link.py"
    link.symlink_to(target)

    observed = fingerprint_repository_v1_for_diagnostics(repo)
    current = fingerprint_repository(repo)

    assert observed.file_count == 1
    assert observed.value == _legacy_link_only_digest(b"link.py", os.fsencode(target))
    assert is_legacy_source_fingerprint_v1(observed.value)
    assert source_fingerprint_version(observed.value) == 1
    assert current.value != observed.value


def test_v2_framing_separates_a_v1_structural_collision(tmp_path: Path) -> None:
    state_a = tmp_path / "state-a"
    state_b = tmp_path / "state-b"
    state_a.mkdir()
    state_b.mkdir()
    (state_a / "a").write_bytes(b"")
    (state_a / "b").write_bytes(b"X\0F\0c\0Y")
    (state_b / "a").write_bytes(b"\0F\0b\0X")
    (state_b / "c").write_bytes(b"Y")

    legacy_a = fingerprint_repository_v1_for_diagnostics(state_a)
    legacy_b = fingerprint_repository_v1_for_diagnostics(state_b)
    current_a = fingerprint_repository(state_a)
    current_b = fingerprint_repository(state_b)

    assert legacy_a.file_count == legacy_b.file_count == 2
    assert legacy_a.value == legacy_b.value
    assert current_a.file_count == current_b.file_count == 2
    assert current_a.value != current_b.value


def test_fingerprint_rejects_reversible_root_swap_after_enumeration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    replacement = tmp_path / "replacement"
    parked = tmp_path / "parked"
    repo.mkdir()
    replacement.mkdir()
    (repo / "source.py").write_text("VALUE = 'owned'\n", encoding="utf-8")
    (replacement / "source.py").write_text("VALUE = 'foreign'\n", encoding="utf-8")
    real_resolve = source_fingerprint_module._resolved_repository_file_at
    swapped = False

    @contextmanager
    def swap_root(root, descriptor, relative, **kwargs):
        nonlocal swapped
        if swapped:
            with real_resolve(root, descriptor, relative, **kwargs) as binding:
                yield binding
            return
        swapped = True
        repo.rename(parked)
        replacement.rename(repo)
        try:
            with real_resolve(root, descriptor, relative, **kwargs) as binding:
                yield binding
        finally:
            repo.rename(replacement)
            parked.rename(repo)

    monkeypatch.setattr(
        source_fingerprint_module,
        "_resolved_repository_file_at",
        swap_root,
    )

    with pytest.raises(RepositoryChangedError, match="read consistently"):
        fingerprint_repository(repo)
    assert (repo / "source.py").read_text(encoding="utf-8") == "VALUE = 'owned'\n"
    assert (replacement / "source.py").read_text(encoding="utf-8") == (
        "VALUE = 'foreign'\n"
    )


def test_fingerprint_never_resolves_replaceable_root_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    replacement = tmp_path / "replacement"
    parked = tmp_path / "parked"
    repo.mkdir()
    replacement.mkdir()
    (repo / "source.py").write_text("VALUE = 'owned'\n", encoding="utf-8")
    (replacement / "source.py").write_text("VALUE = 'foreign'\n", encoding="utf-8")
    expected = fingerprint_repository(repo)
    real_resolve = Path.resolve
    resolve_calls = 0

    def redirect_during_resolve(path, *args, **kwargs):
        nonlocal resolve_calls
        resolve_calls += 1
        repo.rename(parked)
        replacement.rename(repo)
        try:
            return real_resolve(replacement, *args, **kwargs)
        finally:
            repo.rename(replacement)
            parked.rename(repo)

    monkeypatch.setattr(Path, "resolve", redirect_during_resolve)

    assert fingerprint_repository(repo) == expected
    assert resolve_calls == 0


def test_fingerprint_rejects_symlink_repository_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    alias = tmp_path / "alias"
    repo.mkdir()
    (repo / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    try:
        alias.symlink_to(repo, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - platform capability
        pytest.skip(f"source fingerprint root test needs symlink support: {exc}")

    with pytest.raises(RepositoryChangedError, match="repository changed"):
        fingerprint_repository(alias)


def test_scan_charges_wide_directory_before_sorting_all_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = 0

    class WideDirectory:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return None

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal observed
            observed += 1
            if observed > 4:  # pragma: no cover - proves eager scan regression
                raise AssertionError("scanner consumed entries beyond its budget")
            return SimpleNamespace(name=f"source-{observed}.py")

    descriptor = os.open(tmp_path, source_fingerprint_module._directory_flags())
    try:
        monkeypatch.setattr(source_fingerprint_module, "_MAX_SOURCE_ENTRIES", 3)
        monkeypatch.setattr(
            source_fingerprint_module.os,
            "scandir",
            lambda _descriptor: WideDirectory(),
        )

        with pytest.raises(ValueError, match="entry limit"):
            source_fingerprint_module._scan_pinned_repository(
                descriptor,
                excluded=(),
                collect_entries=True,
            )
    finally:
        os.close(descriptor)

    assert observed == 4


def test_fingerprint_scan_failure_closes_pinned_root_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor_root = Path("/proc/self/fd")
    if not descriptor_root.is_dir():  # pragma: no cover - non-Linux host
        pytest.skip("descriptor accounting needs /proc/self/fd")
    (tmp_path / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    before = len(tuple(descriptor_root.iterdir()))

    def fail_scan(*_args, **_kwargs):
        raise ValueError("injected scan failure")

    monkeypatch.setattr(
        source_fingerprint_module,
        "_scan_pinned_repository",
        fail_scan,
    )
    for _ in range(32):
        with pytest.raises(RepositoryChangedError, match="repository changed"):
            fingerprint_repository(tmp_path)

    after = len(tuple(descriptor_root.iterdir()))
    assert after <= before + 1


def test_fingerprint_rejects_reversible_intermediate_swap_after_enumeration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    package = repo / "package"
    replacement = tmp_path / "replacement"
    parked = tmp_path / "parked"
    package.mkdir(parents=True)
    replacement.mkdir()
    (package / "source.py").write_text("VALUE = 'owned'\n", encoding="utf-8")
    (replacement / "source.py").write_text("VALUE = 'foreign'\n", encoding="utf-8")
    real_resolve = source_fingerprint_module._resolved_repository_file_at
    swapped = False

    @contextmanager
    def swap_intermediate(root, descriptor, relative, **kwargs):
        nonlocal swapped
        if swapped or relative != "package/source.py":
            with real_resolve(root, descriptor, relative, **kwargs) as binding:
                yield binding
            return
        swapped = True
        package.rename(parked)
        replacement.rename(package)
        try:
            with real_resolve(root, descriptor, relative, **kwargs) as binding:
                yield binding
        finally:
            package.rename(replacement)
            parked.rename(package)

    monkeypatch.setattr(
        source_fingerprint_module,
        "_resolved_repository_file_at",
        swap_intermediate,
    )

    with pytest.raises(RepositoryChangedError, match="read consistently"):
        fingerprint_repository(repo)
    assert (package / "source.py").read_text(encoding="utf-8") == ("VALUE = 'owned'\n")
    assert (replacement / "source.py").read_text(encoding="utf-8") == (
        "VALUE = 'foreign'\n"
    )


def test_fingerprint_rejects_file_swap_in_walker_to_open_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.py"
    foreign = tmp_path / ".foreign"
    parked = tmp_path / ".parked"
    source.write_text("VALUE = 'owned'\n", encoding="utf-8")
    foreign.write_text("VALUE = 'foreign'\n", encoding="utf-8")
    real_resolve = source_fingerprint_module._resolved_repository_file_at
    swapped = False

    @contextmanager
    def swap_file(root, descriptor, relative, **kwargs):
        nonlocal swapped
        if swapped or relative != "source.py":
            with real_resolve(root, descriptor, relative, **kwargs) as binding:
                yield binding
            return
        swapped = True
        source.rename(parked)
        foreign.rename(source)
        try:
            with real_resolve(root, descriptor, relative, **kwargs) as binding:
                yield binding
        finally:
            source.rename(foreign)
            parked.rename(source)

    monkeypatch.setattr(
        source_fingerprint_module,
        "_resolved_repository_file_at",
        swap_file,
    )

    with pytest.raises(RepositoryChangedError, match="read consistently"):
        fingerprint_repository(tmp_path)
    assert source.read_text(encoding="utf-8") == "VALUE = 'owned'\n"
    assert foreign.read_text(encoding="utf-8") == "VALUE = 'foreign'\n"


def test_fingerprint_accepts_absolute_contained_symlink_and_retained_reads(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "target.py"
    target.write_bytes(b"FIRST = 1\nSECOND = 2\n")
    (repo / "link.py").symlink_to(target)

    observed = fingerprint_repository(repo)

    with capture_repository_source(repo) as binding:
        assert observed.file_count == binding.file_count == 2
        assert observed.value == binding.fingerprint
        assert binding.read_bytes("link.py", max_bytes=1024) == target.read_bytes()
        assert binding.read_prefix("link.py", max_bytes=5) == b"FIRST"
        assert (
            binding.read_line_range(
                "link.py",
                start_line=2,
                end_line=2,
                max_bytes=1024,
            )
            == b"SECOND = 2\n"
        )
        binding.verify_snapshot()


def test_absolute_symlink_retained_reads_use_authenticated_root(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "target.py"
    target.write_bytes(b"FIRST = 1\nSECOND = 2\n")
    (repo / "link.py").symlink_to(target)
    foreign = tmp_path / "foreign"
    foreign.mkdir()

    with capture_repository_source(repo) as binding:
        with binding.read_session():
            public_root = binding.root
            object.__setattr__(binding, "root", foreign)
            try:
                assert binding.read_prefix("link.py", max_bytes=5) == b"FIRST"
                assert (
                    binding.read_line_range(
                        "link.py",
                        start_line=2,
                        end_line=2,
                        max_bytes=1024,
                    )
                    == b"SECOND = 2\n"
                )
            finally:
                object.__setattr__(binding, "root", public_root)


@pytest.mark.parametrize("operation", ["bytes", "prefix", "line-range"])
def test_absolute_symlink_retained_reads_reject_retarget(
    tmp_path: Path,
    operation: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    first = repo / "first.py"
    second = repo / "second.py"
    first.write_bytes(b"SAME\n")
    second.write_bytes(b"SAME\n")
    link = repo / "link.py"
    link.symlink_to(first)
    binding = capture_repository_source(repo)
    link.unlink()
    link.symlink_to(second)

    with pytest.raises(RepositoryChangedError):
        if operation == "bytes":
            binding.read_bytes("link.py", max_bytes=1024)
        elif operation == "prefix":
            binding.read_prefix("link.py", max_bytes=4)
        else:
            binding.read_line_range(
                "link.py",
                start_line=1,
                end_line=1,
                max_bytes=1024,
            )

    assert not binding.usable
    binding.close()


def test_fingerprint_accepts_mixed_contained_symlink_chain(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    nested = repo / "nested"
    nested.mkdir(parents=True)
    (repo / "target.py").write_bytes(b"VALUE = 1\n")
    (nested / "relative.py").symlink_to("../target.py")
    (repo / "absolute.py").symlink_to(nested / "relative.py")

    with capture_repository_source(repo) as binding:
        assert binding.file_count == 3
        assert binding.read_bytes("absolute.py", max_bytes=1024) == b"VALUE = 1\n"


def test_fingerprint_preserves_contained_absolute_broken_link(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "missing.py"
    (repo / "link.py").symlink_to(target)

    observed = fingerprint_repository(repo)
    legacy = fingerprint_repository_v1_for_diagnostics(repo)

    assert observed.file_count == legacy.file_count == 1
    assert legacy.value == _legacy_link_only_digest(
        b"link.py",
        os.fsencode(target),
    )


def test_fingerprint_rejects_absolute_target_that_leaves_and_reenters_root(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "target.py").write_bytes(b"VALUE = 1\n")
    raw_target = repo / ".." / repo.name / "target.py"
    (repo / "link.py").symlink_to(raw_target)

    with pytest.raises(RepositoryChangedError, match="read consistently"):
        fingerprint_repository(repo)


@pytest.mark.parametrize("absolute", [False, True])
def test_fingerprint_excludes_contained_directory_symlink_in_v1_and_v2(
    tmp_path: Path,
    absolute: bool,
) -> None:
    repo = tmp_path / "repo"
    package = repo / "package"
    package.mkdir(parents=True)
    (package / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    link = repo / "package-link"
    link.symlink_to(package if absolute else "package", target_is_directory=True)

    current_with_link = fingerprint_repository(repo)
    legacy_with_link = fingerprint_repository_v1_for_diagnostics(repo)
    link.unlink()

    assert fingerprint_repository(repo) == current_with_link
    assert fingerprint_repository_v1_for_diagnostics(repo) == legacy_with_link
    assert current_with_link.file_count == legacy_with_link.file_count == 1


def test_fingerprint_accepts_expo_pods_absolute_directory_link(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    dependency = repo / "node_modules" / ".pnpm" / "dependency" / "package"
    dependency.mkdir(parents=True)
    (dependency / "index.ts").write_bytes(b"export const value = 1;\n")
    pod = repo / "ios" / "Pods" / "Dependency"
    pod.parent.mkdir(parents=True)
    pod.symlink_to(dependency, target_is_directory=True)

    observed = fingerprint_repository(repo)
    with capture_repository_source(repo) as binding:
        binding.verify_snapshot()

    assert observed.file_count == binding.file_count == 0


@pytest.mark.parametrize("absolute", [False, True])
def test_fingerprint_excludes_repository_root_directory_symlink_in_v1_and_v2(
    tmp_path: Path,
    absolute: bool,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    link = repo / "root-link"
    link.symlink_to(repo if absolute else ".", target_is_directory=True)

    current_with_link = fingerprint_repository(repo)
    legacy_with_link = fingerprint_repository_v1_for_diagnostics(repo)
    link.unlink()

    assert fingerprint_repository(repo) == current_with_link
    assert fingerprint_repository_v1_for_diagnostics(repo) == legacy_with_link
    assert current_with_link.file_count == legacy_with_link.file_count == 1


@pytest.mark.parametrize("absolute", [False, True])
def test_fingerprint_rejects_source_symlink_outside_repository(
    tmp_path: Path,
    absolute: bool,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("SECRET = 1\n")
    (repo / "current.py").symlink_to(outside if absolute else "../outside.py")

    with pytest.raises(RepositoryChangedError, match="could not be read consistently"):
        fingerprint_repository(repo)


@pytest.mark.parametrize("absolute", [False, True])
def test_fingerprint_rejects_directory_symlink_outside_repository(
    tmp_path: Path,
    absolute: bool,
) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    (repo / "outside-link").symlink_to(
        outside if absolute else "../outside",
        target_is_directory=True,
    )

    with pytest.raises(RepositoryChangedError, match="could not be read consistently"):
        fingerprint_repository(repo)


def test_inventory_scan_never_follows_source_symlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("SECRET = 1\n", encoding="utf-8")
    (repo / "current.py").symlink_to(outside)
    descriptor = os.open(repo, source_fingerprint_module._directory_flags())
    real_stat = os.stat

    def reject_follow(path, *args, **kwargs):
        if kwargs.get("dir_fd") is not None and kwargs.get("follow_symlinks", True):
            raise AssertionError("inventory scan followed a source symlink")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(source_fingerprint_module.os, "stat", reject_follow)
    try:
        scan = source_fingerprint_module._scan_pinned_repository(
            descriptor,
            excluded=(),
            collect_entries=True,
        )
    finally:
        os.close(descriptor)

    assert [entry.relative for entry in scan.entries] == ["current.py"]


def test_fingerprint_rejects_link_to_directory_swap_before_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    directory = repo / "package"
    repo.mkdir()
    directory.mkdir()
    (directory / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    (repo / "target.py").write_text("VALUE = 1\n", encoding="utf-8")
    link = repo / "current.py"
    link.symlink_to("target.py")
    real_resolve = source_fingerprint_module._resolved_repository_file_at
    swapped = False

    @contextmanager
    def swap_to_directory(root, descriptor, relative, **kwargs):
        nonlocal swapped
        if swapped or relative != "current.py":
            with real_resolve(root, descriptor, relative, **kwargs) as binding:
                yield binding
            return
        swapped = True
        link.unlink()
        link.symlink_to("package", target_is_directory=True)
        try:
            with real_resolve(root, descriptor, relative, **kwargs) as binding:
                yield binding
        finally:
            link.unlink()
            link.symlink_to("target.py")

    monkeypatch.setattr(
        source_fingerprint_module,
        "_resolved_repository_file_at",
        swap_to_directory,
    )

    with pytest.raises(RepositoryChangedError, match="read consistently"):
        fingerprint_repository(repo)


def test_fingerprint_rejects_reversible_source_symlink_swap(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "target.py").write_text("VALUE = 1\n")
    (tmp_path / "outside.py").write_text("SECRET = 1\n")
    link = repo / "current.py"
    link.symlink_to("target.py")
    real_verify = contained_source_module._BoundRepositoryFile.verify
    calls = 0

    def swap_then_restore(binding) -> None:
        nonlocal calls
        calls += 1
        if calls != 2:
            return real_verify(binding)
        link.unlink()
        link.symlink_to("../outside.py")
        try:
            return real_verify(binding)
        finally:
            link.unlink()
            link.symlink_to("target.py")

    monkeypatch.setattr(
        contained_source_module._BoundRepositoryFile,
        "verify",
        swap_then_restore,
    )

    with pytest.raises(RepositoryChangedError, match="could not be read consistently"):
        fingerprint_repository(repo)


def test_dirty_check_ignores_generated_dirs_but_detects_source_changes(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
        check=True,
    )
    source = tmp_path / "module.py"
    source.write_text("VALUE = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "module.py"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-qm", "initial"],
        check=True,
    )

    assert repository_source_is_dirty(tmp_path) is False

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "generated.js").write_text("output\n")
    assert repository_source_is_dirty(tmp_path) is False

    source.write_text("VALUE = 2\n")
    assert repository_source_is_dirty(tmp_path) is True


def test_dirty_check_ignores_changes_in_selected_exclusions(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
        check=True,
    )
    generated = tmp_path / "generated" / "output.py"
    generated.parent.mkdir()
    generated.write_text("VALUE = 1\n")
    (tmp_path / "module.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-qm", "initial"],
        check=True,
    )
    selection = RepositorySourceSelection(("generated",))

    generated.write_text("VALUE = 2\n")
    assert repository_source_is_dirty(tmp_path, selection=selection) is False

    (tmp_path / "module.py").write_text("VALUE = 2\n")
    assert repository_source_is_dirty(tmp_path, selection=selection) is True


def test_dirty_check_never_resolves_replaceable_root_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    observed_command: list[str] = []

    def reject_resolve(*_args, **_kwargs):
        raise AssertionError("dirty check must keep the lexical repository root")

    def clean_status(command, **_kwargs):
        observed_command.extend(command)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(Path, "resolve", reject_resolve)
    monkeypatch.setattr(source_fingerprint_module.subprocess, "run", clean_status)

    assert repository_source_is_dirty(repo) is False
    assert observed_command[0:3] == ["git", "-C", str(repo)]


@pytest.mark.skipif(sys.platform != "win32", reason="requires real Windows HANDLEs")
def test_windows_real_source_authority_abi_layout_node(tmp_path: Path) -> None:
    import ctypes

    api = windows_fs_module.windows_kernel_api()
    assert ctypes.sizeof(api.FILE_ID_128) == 16
    assert ctypes.sizeof(api.FILE_ID_INFO) == 24
    assert api.FILE_ID_EXTD_DIR_INFO.FileId.offset == 72
    assert api.FILE_ID_EXTD_DIR_INFO.FileName.offset == 88

    handle = api.create_directory_handle(tmp_path)
    reopened = 0
    try:
        metadata = api.metadata(handle)
        assert windows_fs_module.windows_file_id_is_reliable(metadata.file_id_128)
        reopened = api.open_by_extended_id(
            handle,
            metadata.file_id_128,
            desired_access=(
                windows_fs_module.FILE_LIST_DIRECTORY
                | windows_fs_module.FILE_READ_ATTRIBUTES
                | windows_fs_module.SYNCHRONIZE
            ),
            is_directory=True,
        )
        assert api.metadata(reopened).file_identity == metadata.file_identity
    finally:
        if reopened:
            api.close(reopened)
        api.close(handle)


@pytest.mark.skipif(sys.platform != "win32", reason="requires real Windows HANDLEs")
def test_windows_real_source_v2_regular_and_contained_symlink_node(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "target.py"
    target.write_bytes(b"VALUE = 1\n")
    try:
        (repo / "relative.py").symlink_to("target.py")
        (repo / "absolute.py").symlink_to(target)
    except OSError as exc:  # pragma: no cover - Windows policy dependent
        pytest.skip(f"Windows symlink creation is unavailable: {exc}")

    first = fingerprint_repository(repo)
    second = fingerprint_repository(repo)
    legacy = fingerprint_repository_v1_for_diagnostics(repo)

    assert first == second
    assert first.file_count == legacy.file_count == 3
    assert first.value.startswith("sha256-v2:")
    assert legacy.value.startswith("sha256:")


@pytest.mark.skipif(sys.platform != "win32", reason="requires real Windows HANDLEs")
def test_windows_real_source_v2_rejects_outside_symlink_node(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"SECRET = 1\n")
    try:
        (repo / "current.py").symlink_to(outside)
    except OSError as exc:  # pragma: no cover - Windows policy dependent
        pytest.skip(f"Windows symlink creation is unavailable: {exc}")

    with pytest.raises(RepositoryChangedError, match="read consistently"):
        fingerprint_repository(repo)


@pytest.mark.skipif(sys.platform != "win32", reason="requires real Windows HANDLEs")
def test_windows_real_source_root_handle_blocks_reversible_swap_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "source.py").write_bytes(b"VALUE = 1\n")
    parked = tmp_path / "parked"
    scan = source_fingerprint_module._scan_pinned_windows_repository
    attempted = False

    def attempt_swap(*args, **kwargs):
        nonlocal attempted
        if not attempted:
            attempted = True
            with pytest.raises(OSError):
                repo.rename(parked)
        return scan(*args, **kwargs)

    monkeypatch.setattr(
        source_fingerprint_module,
        "_scan_pinned_windows_repository",
        attempt_swap,
    )

    assert fingerprint_repository(repo).file_count == 1
    assert attempted
    assert repo.is_dir()
    assert not parked.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="requires real Windows HANDLEs")
def test_windows_real_source_failure_closes_all_handles_node(
    tmp_path: Path,
) -> None:
    import ctypes
    import ctypes.wintypes as wintypes

    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"SECRET = 1\n")
    try:
        (repo / "current.py").symlink_to(outside)
    except OSError as exc:  # pragma: no cover - Windows policy dependent
        pytest.skip(f"Windows symlink creation is unavailable: {exc}")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = ()
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.GetProcessHandleCount.restype = wintypes.BOOL

    def handle_count() -> int:
        count = wintypes.DWORD()
        if not kernel32.GetProcessHandleCount(
            kernel32.GetCurrentProcess(),
            ctypes.byref(count),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(count.value)

    before = handle_count()
    for _ in range(32):
        with pytest.raises(RepositoryChangedError):
            fingerprint_repository(repo)
    after = handle_count()

    assert after <= before + 2


@pytest.mark.skipif(sys.platform == "win32", reason="exercises POSIX directory fds")
def test_expected_root_authority_handoff_is_independent_and_binding_only(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent"
    repo = parent / "repo"
    repo.mkdir(parents=True)
    (repo / "source.py").write_bytes(b"VALUE = 1\n")
    expected = pin_repository_source_root(repo)

    parent_before = parent.stat()
    root_before = repo.stat()
    os.utime(
        parent,
        ns=(parent_before.st_atime_ns, parent_before.st_mtime_ns + 1_000_000),
    )
    os.utime(
        repo,
        ns=(root_before.st_atime_ns, root_before.st_mtime_ns + 1_000_000),
    )

    binding = capture_repository_source(
        repo,
        expected_root_authority=expected,
    )
    try:
        assert type(expected) is RepositorySourceRootAuthority
        assert binding._posix_authority is not expected._posix_authority
        expected.close()
        assert binding.read_bytes("source.py", max_bytes=1024) == b"VALUE = 1\n"
    finally:
        if not expected.closed:
            expected.close()
        binding.close()


@pytest.mark.skipif(sys.platform == "win32", reason="exercises POSIX directory fds")
@pytest.mark.parametrize("replacement", ["root", "ancestor"])
def test_expected_root_authority_rejects_identical_replacement(
    tmp_path: Path,
    replacement: str,
) -> None:
    parent = tmp_path / "parent"
    repo = parent / "repo"
    repo.mkdir(parents=True)
    payload = b"VALUE = 1\n"
    (repo / "source.py").write_bytes(payload)
    expected = pin_repository_source_root(repo)

    if replacement == "root":
        parked = parent / "parked-repo"
        repo.rename(parked)
        repo.mkdir()
    else:
        parked = tmp_path / "parked-parent"
        parent.rename(parked)
        repo.mkdir(parents=True)
    (repo / "source.py").write_bytes(payload)

    try:
        with pytest.raises(RepositoryChangedError, match="authority changed"):
            capture_repository_source(
                repo,
                expected_root_authority=expected,
            )
    finally:
        expected.close()


@pytest.mark.skipif(sys.platform == "win32", reason="exercises POSIX directory fds")
def test_expected_root_authority_validates_type_root_and_closed_state(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "source.py").write_bytes(b"FIRST\n")
    (second / "source.py").write_bytes(b"SECOND\n")

    with pytest.raises(TypeError, match="RepositorySourceRootAuthority"):
        capture_repository_source(
            first,
            expected_root_authority=object(),  # type: ignore[arg-type]
        )

    foreign = pin_repository_source_root(second)
    try:
        with pytest.raises(ValueError, match="differs from root"):
            capture_repository_source(first, expected_root_authority=foreign)
    finally:
        foreign.close()

    closed = pin_repository_source_root(first)
    closed.close()
    with pytest.raises(RepositoryChangedError, match="authority changed"):
        capture_repository_source(first, expected_root_authority=closed)


@pytest.mark.skipif(sys.platform == "win32", reason="exercises POSIX directory fds")
def test_pin_root_installs_cleanup_slot_before_open_and_preserves_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    owner = SourceBindingCleanupOwner()
    interruption = KeyboardInterrupt("injected root pin cancellation")

    def interrupt_open(*_args: object, **_kwargs: object) -> int:
        assert not owner.closed
        assert len(owner._sources) == 1
        raise interruption

    monkeypatch.setattr(source_fingerprint_module.os, "open", interrupt_open)
    with pytest.raises(KeyboardInterrupt) as caught:
        pin_repository_source_root(repo, _source_owner=owner.retain)

    assert caught.value is interruption
    assert not owner.closed
    assert len(owner._sources) == 1
    owner.close()
    assert owner.closed


@pytest.mark.skipif(sys.platform == "win32", reason="exercises POSIX directory fds")
def test_pin_root_cancellation_retains_acquired_authority_for_cleanup_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    owner = SourceBindingCleanupOwner()
    interruption = KeyboardInterrupt("injected completed root pin")
    cleanup_failure = OSError(errno.EIO, "injected root authority close failure")
    real_close = RepositorySourceRootAuthority.close

    def interrupt_verify(_authority: RepositorySourceRootAuthority) -> None:
        raise interruption

    def fail_close(_authority: RepositorySourceRootAuthority) -> None:
        raise cleanup_failure

    monkeypatch.setattr(RepositorySourceRootAuthority, "verify", interrupt_verify)
    monkeypatch.setattr(RepositorySourceRootAuthority, "close", fail_close)
    with pytest.raises(KeyboardInterrupt) as caught:
        pin_repository_source_root(repo, _source_owner=owner.retain)

    assert caught.value is interruption
    assert caught.value.__cause__ is cleanup_failure
    assert not owner.closed
    assert len(owner.pending_sources) == 1
    assert type(owner.pending_sources[0]) is RepositorySourceRootAuthority
    assert caught.value.source_cleanup_owner is owner._sources[0]

    monkeypatch.setattr(RepositorySourceRootAuthority, "close", real_close)
    owner.close()
    assert owner.closed


@pytest.mark.skipif(sys.platform == "win32", reason="exercises POSIX directory fds")
def test_expected_root_capture_lease_serializes_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "source.py").write_bytes(b"VALUE = 1\n")
    expected = pin_repository_source_root(repo)
    scan_entered = threading.Event()
    allow_scan = threading.Event()
    close_finished = threading.Event()
    real_scan = source_fingerprint_module._scan_pinned_repository
    bindings: list[RepositorySourceBinding] = []
    failures: list[BaseException] = []
    scan_calls = 0

    def blocking_scan(*args: object, **kwargs: object):
        nonlocal scan_calls
        scan_calls += 1
        if scan_calls == 1:
            scan_entered.set()
            if not allow_scan.wait(timeout=5):
                raise AssertionError("timed out waiting to finish repository scan")
        return real_scan(*args, **kwargs)

    def capture() -> None:
        try:
            bindings.append(
                capture_repository_source(
                    repo,
                    expected_root_authority=expected,
                )
            )
        except BaseException as exc:  # noqa: B036 - report thread failure
            failures.append(exc)

    def close() -> None:
        try:
            expected.close()
        except BaseException as exc:  # noqa: B036 - report thread failure
            failures.append(exc)
        finally:
            close_finished.set()

    monkeypatch.setattr(
        source_fingerprint_module,
        "_scan_pinned_repository",
        blocking_scan,
    )
    capture_thread = threading.Thread(target=capture)
    close_thread = threading.Thread(target=close)
    capture_thread.start()
    assert scan_entered.wait(timeout=2)
    close_thread.start()
    assert not close_finished.wait(timeout=0.1)
    allow_scan.set()
    capture_thread.join(timeout=5)
    close_thread.join(timeout=5)

    try:
        assert not capture_thread.is_alive()
        assert not close_thread.is_alive()
        assert failures == []
        assert len(bindings) == 1
        assert expected.closed
        assert bindings[0].read_bytes("source.py", max_bytes=1024) == b"VALUE = 1\n"
    finally:
        allow_scan.set()
        if capture_thread.is_alive():
            capture_thread.join(timeout=5)
        if close_thread.is_alive():
            close_thread.join(timeout=5)
        if not expected.closed:
            expected.close()
        for binding in bindings:
            binding.close()


@pytest.mark.skipif(
    not hasattr(os, "fork") or sys.platform == "win32",
    reason="requires POSIX fork and directory fds",
)
def test_expected_root_child_close_revokes_without_inherited_lock(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "source.py").write_bytes(b"VALUE = 1\n")
    expected = pin_repository_source_root(repo)
    held = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        expected._lock.acquire()
        held.set()
        release.wait(timeout=10)
        expected._lock.release()

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert held.wait(timeout=2)
    read_descriptor, write_descriptor = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no cover - child reports through the pipe
        try:
            os.close(read_descriptor)
            try:
                expected.close()
            except RuntimeError as exc:
                if "cannot cross processes" not in str(exc):
                    raise
            resources = expected._posix_authority._resources
            os.write(
                write_descriptor,
                b"1" if expected.closed and not resources else b"0",
            )
        except BaseException:  # noqa: B036 - report child failure through pipe
            os.write(write_descriptor, b"E")
        finally:
            os.close(write_descriptor)
            os._exit(0)

    os.close(write_descriptor)
    try:
        ready, _, _ = select.select([read_descriptor], [], [], 3)
        if not ready:
            os.kill(child, signal.SIGKILL)
            pytest.fail("fork child deadlocked on inherited root authority lock")
        assert os.read(read_descriptor, 1) == b"1"
    finally:
        os.close(read_descriptor)
        release.set()
        thread.join(timeout=2)
        os.waitpid(child, 0)
        expected.verify()
        expected.close()
