# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import ctypes.util
import errno
import multiprocessing
import os
import queue
import stat
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from codenib.compiler import _directory_lease as lease_module
from codenib.compiler._directory_lease import (
    DirectoryLeaseMode,
    PrivateDirectoryLeaseOwner,
    PrivateDirectoryLeaseRoute,
    acquire_private_directory_lease,
    require_private_directory_lease_support,
)

try:  # pragma: no cover - selected by module support tests
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or fcntl is None,
    reason="requires Linux flock",
)


def _identity_from_metadata(
    metadata: os.stat_result,
) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        getattr(metadata, "st_file_attributes", 0),
    )


def _identity(path: Path) -> tuple[int, int, int, int]:
    return _identity_from_metadata(path.lstat())


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _route(path: Path) -> PrivateDirectoryLeaseRoute:
    return PrivateDirectoryLeaseRoute(path, _identity(path), os.getpid())


def _acquire(
    route: PrivateDirectoryLeaseRoute,
    mode: DirectoryLeaseMode,
    *,
    blocking: bool = True,
):
    installed: list[PrivateDirectoryLeaseOwner] = []
    owner = acquire_private_directory_lease(
        route,
        mode=mode,
        blocking=blocking,
        _construction_owner=installed.append,
    )
    assert installed == [owner]
    return owner


def _exception_link_graph_is_acyclic(error: BaseException) -> bool:
    pending: list[tuple[BaseException, bool]] = [(error, False)]
    visiting: set[int] = set()
    complete: set[int] = set()
    while pending:
        current, leaving = pending.pop()
        identity = id(current)
        if leaving:
            visiting.discard(identity)
            complete.add(identity)
            continue
        if identity in visiting:
            return False
        if identity in complete:
            continue
        if len(visiting) + len(complete) >= 64:
            return False
        visiting.add(identity)
        pending.append((current, True))
        for attribute in ("__context__", "__cause__"):
            linked = vars(BaseException)[attribute].__get__(current, type(current))
            if isinstance(linked, BaseException):
                pending.append((linked, False))
    return True


def _bounded_exception_recovery_graph(error: BaseException) -> tuple[object, ...]:
    pending: list[object] = [error]
    seen: set[int] = set()
    recovered: list[object] = []
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        assert len(seen) < 128
        seen.add(identity)
        recovered.append(current)
        if isinstance(current, BaseException):
            for attribute in ("__cause__", "__context__"):
                linked = vars(BaseException)[attribute].__get__(current, type(current))
                if isinstance(linked, BaseException):
                    pending.append(linked)
            if type(current) is RuntimeError:
                args = BaseException.__getattribute__(current, "args")
                assert type(args) is tuple and len(args) <= 64
                pending.append(args)
        elif type(current) is tuple:
            assert len(current) <= 64
            pending.extend(current)
    return tuple(recovered)


def _spawn_hold_lease(
    path_text: str,
    identity: tuple[int, int, int, int],
    mode_value: str,
    ready,
    release,
) -> None:
    path = Path(path_text)
    route = PrivateDirectoryLeaseRoute(path, identity, os.getpid())
    mode = DirectoryLeaseMode(mode_value)
    installed: list[PrivateDirectoryLeaseOwner] = []
    owner = acquire_private_directory_lease(
        route,
        mode=mode,
        blocking=True,
        _construction_owner=installed.append,
    )
    ready.set()
    try:
        if not release.wait(timeout=20):
            raise TimeoutError("lease release was not signaled")
    finally:
        owner.close()


def _spawn_wait_for_lease(
    path_text: str,
    identity: tuple[int, int, int, int],
    mode_value: str,
    ready,
    acquired,
) -> None:
    path = Path(path_text)
    route = PrivateDirectoryLeaseRoute(path, identity, os.getpid())
    ready.set()
    installed: list[PrivateDirectoryLeaseOwner] = []
    owner = acquire_private_directory_lease(
        route,
        mode=DirectoryLeaseMode(mode_value),
        blocking=True,
        _construction_owner=installed.append,
    )
    try:
        acquired.set()
    finally:
        owner.close()


def test_support_probe_accepts_current_platform() -> None:
    require_private_directory_lease_support()


def test_support_probe_rejects_unsupported_platform_before_touching_a_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lease_module.os, "name", "nt")
    with pytest.raises(RuntimeError, match="Linux directory-inode flock"):
        require_private_directory_lease_support()


def test_owner_retains_exact_diagnostics_and_closes(tmp_path: Path) -> None:
    route = _route(_private_directory(tmp_path / "shard"))
    owner = _acquire(route, DirectoryLeaseMode.SHARED)

    assert type(owner) is PrivateDirectoryLeaseOwner
    assert owner.path is route.path
    assert owner.identity is route.identity
    assert owner.mode is DirectoryLeaseMode.SHARED
    assert not owner.closed

    owner.close()
    owner.close()
    assert owner.closed


def test_owner_context_manager_unlocks_after_body_failure(tmp_path: Path) -> None:
    route = _route(_private_directory(tmp_path / "shard"))
    failure = RuntimeError("lease body failed")
    installed: list[PrivateDirectoryLeaseOwner] = []

    with pytest.raises(RuntimeError) as caught:
        with acquire_private_directory_lease(
            route,
            mode=DirectoryLeaseMode.EXCLUSIVE,
            blocking=True,
            _construction_owner=installed.append,
        ) as owner:
            assert owner.closed is False
            raise failure

    assert caught.value is failure
    assert owner.closed
    replacement = acquire_private_directory_lease(
        route,
        mode=DirectoryLeaseMode.EXCLUSIVE,
        blocking=False,
        _construction_owner=installed.append,
    )
    replacement.close()
    assert replacement.closed


def test_owner_context_manager_retains_body_and_owner_on_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _route(_private_directory(tmp_path / "shard"))
    body_failure = RuntimeError("lease body failed")
    prior = LookupError("lease body prior")
    cleanup_failure = OSError(errno.EIO, "lease descriptor close failed")
    installed: list[PrivateDirectoryLeaseOwner] = []
    real_close = lease_module._close_descriptor_for_cleanup

    def fail_close(_owner: PrivateDirectoryLeaseOwner) -> None:
        raise cleanup_failure

    monkeypatch.setattr(lease_module, "_close_descriptor_for_cleanup", fail_close)
    with pytest.raises(RuntimeError) as caught:
        with acquire_private_directory_lease(
            route,
            mode=DirectoryLeaseMode.EXCLUSIVE,
            blocking=True,
            _construction_owner=installed.append,
        ) as owner:
            raise body_failure from prior

    assert caught.value is body_failure
    assert _exception_link_graph_is_acyclic(body_failure)
    recovered = _bounded_exception_recovery_graph(body_failure)
    assert owner in recovered
    assert cleanup_failure in recovered
    assert prior in recovered
    assert not owner.closed
    monkeypatch.setattr(lease_module, "_close_descriptor_for_cleanup", real_close)
    with pytest.raises(RuntimeError, match="already held by this thread"):
        _acquire(route, DirectoryLeaseMode.EXCLUSIVE, blocking=False)

    owner.close()
    replacement = _acquire(route, DirectoryLeaseMode.EXCLUSIVE, blocking=False)
    replacement.close()


def test_construction_owner_is_installed_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _route(_private_directory(tmp_path / "shard"))
    installed: list[PrivateDirectoryLeaseOwner] = []
    real_open = lease_module._native_workspace_owner._open_directory_fd

    def observed_open(native_owner: object, path: bytes) -> None:
        assert len(installed) == 1
        assert type(installed[0]) is PrivateDirectoryLeaseOwner
        assert installed[0] in lease_module._ACTIVE_DIRECTORY_LEASE_OWNERS
        assert native_owner is installed[0]._native_descriptor_owner
        assert path == os.fsencode(route.path)
        with pytest.raises(RuntimeError, match="not open"):
            lease_module._native_workspace_owner._borrow_directory_fd(native_owner)
        real_open(native_owner, path)

    monkeypatch.setattr(
        lease_module._native_workspace_owner,
        "_open_directory_fd",
        observed_open,
    )
    owner = acquire_private_directory_lease(
        route,
        mode=DirectoryLeaseMode.SHARED,
        blocking=True,
        _construction_owner=installed.append,
    )
    try:
        assert installed == [owner]
        assert not owner.closed
    finally:
        owner.close()


def test_construction_owner_failure_retains_a_closed_owner(tmp_path: Path) -> None:
    route = _route(_private_directory(tmp_path / "shard"))
    installed: list[PrivateDirectoryLeaseOwner] = []
    primary = RuntimeError("construction handoff failed")

    def install(owner: PrivateDirectoryLeaseOwner) -> None:
        installed.append(owner)
        raise primary

    with pytest.raises(RuntimeError) as raised:
        acquire_private_directory_lease(
            route,
            mode=DirectoryLeaseMode.SHARED,
            blocking=True,
            _construction_owner=install,
        )
    assert raised.value is primary
    assert len(installed) == 1
    assert installed[0].closed


@pytest.mark.parametrize("close_in_thread", (False, True))
def test_construction_owner_close_prevents_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    close_in_thread: bool,
) -> None:
    route = _route(_private_directory(tmp_path / "shard"))
    installed: list[PrivateDirectoryLeaseOwner] = []
    close_errors: list[BaseException] = []

    def close_owner(owner: PrivateDirectoryLeaseOwner) -> None:
        try:
            owner.close()
        except BaseException as error:  # noqa: B036 - report thread failure
            close_errors.append(error)

    def install(owner: PrivateDirectoryLeaseOwner) -> None:
        installed.append(owner)
        if close_in_thread:
            closer = threading.Thread(target=close_owner, args=(owner,))
            closer.start()
            closer.join()
        else:
            close_owner(owner)
        assert not close_errors
        assert owner.closed

    def reject_open(_owner: object, _path: bytes) -> None:
        raise AssertionError("closed construction owner reached native open")

    monkeypatch.setattr(
        lease_module._native_workspace_owner,
        "_open_directory_fd",
        reject_open,
    )
    with pytest.raises(RuntimeError, match="closed before acquisition"):
        acquire_private_directory_lease(
            route,
            mode=DirectoryLeaseMode.SHARED,
            blocking=True,
            _construction_owner=install,
        )

    assert len(installed) == 1
    assert installed[0].closed
    assert not lease_module._ACTIVE_DIRECTORY_LEASE_OWNERS


def test_acquire_requires_preexisting_construction_owner(tmp_path: Path) -> None:
    route = _route(_private_directory(tmp_path / "shard"))

    with pytest.raises(TypeError, match="requires a construction owner"):
        acquire_private_directory_lease(
            route,
            mode=DirectoryLeaseMode.SHARED,
            blocking=True,
        )


@pytest.mark.parametrize(
    ("field", "value", "error"),
    (
        ("path", "not-a-path", TypeError),
        ("identity", (1, 2, stat.S_IFDIR), TypeError),
        ("identity", (1, 2, stat.S_IFDIR, False), TypeError),
        ("identity", (1, 2, stat.S_IFREG, 0), ValueError),
        ("owner_pid", True, TypeError),
        ("owner_pid", -1, ValueError),
    ),
)
def test_route_rejects_inexact_fields(
    tmp_path: Path,
    field: str,
    value: object,
    error: type[BaseException],
) -> None:
    path = _private_directory(tmp_path / "shard")
    values = {"path": path, "identity": _identity(path), "owner_pid": os.getpid()}
    values[field] = value
    with pytest.raises(error):
        PrivateDirectoryLeaseRoute(**values)  # type: ignore[arg-type]


def test_route_rejects_path_subclass_and_relative_path(tmp_path: Path) -> None:
    path = _private_directory(tmp_path / "shard")
    concrete_path_type = type(Path())

    class DerivedPath(concrete_path_type):
        pass

    with pytest.raises(TypeError, match="exact Path"):
        PrivateDirectoryLeaseRoute(
            DerivedPath(path),
            _identity(path),
            os.getpid(),
        )
    with pytest.raises(ValueError, match="absolute"):
        PrivateDirectoryLeaseRoute(
            Path("relative-shard"),
            _identity(path),
            os.getpid(),
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    (
        ({"mode": "shared", "blocking": True}, "mode"),
        ({"mode": DirectoryLeaseMode.SHARED, "blocking": 1}, "blocking"),
        (
            {
                "mode": DirectoryLeaseMode.SHARED,
                "blocking": True,
                "check_cancelled": object(),
            },
            "cancellation",
        ),
        (
            {
                "mode": DirectoryLeaseMode.SHARED,
                "blocking": True,
                "_construction_owner": object(),
            },
            "construction owner",
        ),
    ),
)
def test_acquire_rejects_inexact_policy_before_constructing_owner(
    tmp_path: Path,
    kwargs: dict[str, object],
    message: str,
) -> None:
    route = _route(_private_directory(tmp_path / "shard"))
    with pytest.raises(TypeError, match=message):
        acquire_private_directory_lease(route, **kwargs)  # type: ignore[arg-type]


def test_rejects_wrong_retained_identity_without_opening_lock(
    tmp_path: Path,
) -> None:
    path = _private_directory(tmp_path / "shard")
    identity = _identity(path)
    wrong = (identity[0], identity[1] + 1, identity[2], identity[3])
    route = PrivateDirectoryLeaseRoute(path, wrong, os.getpid())
    with pytest.raises(RuntimeError, match="changed"):
        _acquire(route, DirectoryLeaseMode.SHARED)


def test_rejects_non_private_directory_mode(tmp_path: Path) -> None:
    path = _private_directory(tmp_path / "shard")
    route = _route(path)
    path.chmod(0o750)
    with pytest.raises(RuntimeError, match="mode 0700"):
        _acquire(route, DirectoryLeaseMode.SHARED)


def test_rejects_directory_owned_by_another_euid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _private_directory(tmp_path / "shard")
    route = _route(path)
    monkeypatch.setattr(lease_module.os, "geteuid", lambda: path.stat().st_uid + 1)
    with pytest.raises(RuntimeError, match="another owner"):
        _acquire(route, DirectoryLeaseMode.SHARED)


def test_rejects_symlink_and_regular_file_routes(tmp_path: Path) -> None:
    target = _private_directory(tmp_path / "target")
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    link_route = PrivateDirectoryLeaseRoute(link, _identity(target), os.getpid())
    with pytest.raises(RuntimeError, match="not real"):
        _acquire(link_route, DirectoryLeaseMode.SHARED)

    regular = tmp_path / "regular"
    regular.write_bytes(b"")
    regular.chmod(0o700)
    target_identity = _identity(target)
    file_route = PrivateDirectoryLeaseRoute(
        regular,
        (
            regular.stat().st_dev,
            regular.stat().st_ino,
            stat.S_IFDIR,
            target_identity[3],
        ),
        os.getpid(),
    )
    with pytest.raises(RuntimeError, match="not real"):
        _acquire(file_route, DirectoryLeaseMode.EXCLUSIVE)


def test_same_thread_reentry_fails_fast_for_same_inode(tmp_path: Path) -> None:
    route = _route(_private_directory(tmp_path / "shard"))
    owner = _acquire(route, DirectoryLeaseMode.SHARED)
    try:
        with pytest.raises(RuntimeError, match="already held by this thread"):
            _acquire(route, DirectoryLeaseMode.SHARED)
    finally:
        owner.close()

    replacement = _acquire(route, DirectoryLeaseMode.EXCLUSIVE)
    replacement.close()


def test_owner_may_close_on_another_thread_without_stranding_claim(
    tmp_path: Path,
) -> None:
    route = _route(_private_directory(tmp_path / "shard"))
    owner = _acquire(route, DirectoryLeaseMode.SHARED)
    failures: list[BaseException] = []

    def close() -> None:
        try:
            owner.close()
        except BaseException as error:  # noqa: B036 - asserted below
            failures.append(error)

    closer = threading.Thread(target=close)
    closer.start()
    closer.join(timeout=10)
    assert not closer.is_alive()
    assert not failures
    assert owner.closed

    reacquired = _acquire(route, DirectoryLeaseMode.EXCLUSIVE)
    reacquired.close()


def test_shared_leases_coexist_across_threads(tmp_path: Path) -> None:
    route = _route(_private_directory(tmp_path / "shard"))
    first = _acquire(route, DirectoryLeaseMode.SHARED)
    result: queue.Queue[object] = queue.Queue()

    def acquire_second() -> None:
        try:
            second = _acquire(route, DirectoryLeaseMode.SHARED, blocking=False)
            result.put(second)
        except BaseException as error:  # noqa: B036 - asserted below
            result.put(error)

    thread = threading.Thread(target=acquire_second)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    observed = result.get_nowait()
    try:
        assert type(observed) is PrivateDirectoryLeaseOwner
    finally:
        if type(observed) is PrivateDirectoryLeaseOwner:
            observed.close()
        first.close()


def test_shared_excludes_exclusive_across_processes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    path = _private_directory(tmp_path / "shard")
    identity = _identity(path)
    ready = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_spawn_hold_lease,
        args=(
            str(path),
            identity,
            DirectoryLeaseMode.SHARED.value,
            ready,
            release,
        ),
    )
    holder.start()
    try:
        assert ready.wait(timeout=10)
        route = PrivateDirectoryLeaseRoute(path, identity, os.getpid())
        with pytest.raises(BlockingIOError):
            _acquire(route, DirectoryLeaseMode.EXCLUSIVE, blocking=False)
    finally:
        release.set()
        holder.join(timeout=10)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=10)
    assert holder.exitcode == 0


def test_exclusive_blocks_shared_process_until_close(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    path = _private_directory(tmp_path / "shard")
    identity = _identity(path)
    route = PrivateDirectoryLeaseRoute(path, identity, os.getpid())
    owner = _acquire(route, DirectoryLeaseMode.EXCLUSIVE)
    ready = context.Event()
    acquired = context.Event()
    waiter = context.Process(
        target=_spawn_wait_for_lease,
        args=(
            str(path),
            identity,
            DirectoryLeaseMode.SHARED.value,
            ready,
            acquired,
        ),
    )
    waiter.start()
    try:
        assert ready.wait(timeout=10)
        assert not acquired.wait(timeout=0.25)
        owner.close()
        assert acquired.wait(timeout=10)
    finally:
        if not owner.closed:
            owner.close()
        waiter.join(timeout=10)
        if waiter.is_alive():
            waiter.terminate()
            waiter.join(timeout=10)
    assert waiter.exitcode == 0


def test_distinct_directory_inodes_do_not_contend(tmp_path: Path) -> None:
    first_route = _route(_private_directory(tmp_path / "first"))
    second_route = _route(_private_directory(tmp_path / "second"))
    first = _acquire(first_route, DirectoryLeaseMode.EXCLUSIVE)
    try:
        second = _acquire(
            second_route,
            DirectoryLeaseMode.EXCLUSIVE,
            blocking=False,
        )
        second.close()
    finally:
        first.close()


def test_path_replacement_rejects_stale_route(tmp_path: Path) -> None:
    path = _private_directory(tmp_path / "shard")
    route = _route(path)
    path.rename(tmp_path / "old-shard")
    _private_directory(path)

    with pytest.raises(RuntimeError, match="changed"):
        _acquire(route, DirectoryLeaseMode.SHARED)


def test_replacement_during_acquisition_releases_old_inode_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _private_directory(tmp_path / "shard")
    old_path = tmp_path / "old-shard"
    route = _route(path)
    real_acquire_flock = lease_module._acquire_flock
    replaced = False

    def acquire_then_replace(owner, **kwargs) -> None:
        nonlocal replaced
        real_acquire_flock(owner, **kwargs)
        if not replaced:
            replaced = True
            path.rename(old_path)
            _private_directory(path)

    monkeypatch.setattr(lease_module, "_acquire_flock", acquire_then_replace)
    with pytest.raises(RuntimeError, match="changed"):
        _acquire(route, DirectoryLeaseMode.EXCLUSIVE)

    descriptor = os.open(
        old_path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        assert fcntl is not None
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def test_interruptible_wait_cancels_and_releases_all_resources(
    tmp_path: Path,
) -> None:
    class Cancelled(BaseException):
        pass

    route = _route(_private_directory(tmp_path / "shard"))
    holder = _acquire(route, DirectoryLeaseMode.EXCLUSIVE)
    failures: list[BaseException] = []
    checks = 0

    def wait_for_shared() -> None:
        def check_cancelled() -> None:
            nonlocal checks
            checks += 1
            if checks >= 2:
                raise Cancelled

        try:
            installed: list[PrivateDirectoryLeaseOwner] = []
            acquire_private_directory_lease(
                route,
                mode=DirectoryLeaseMode.SHARED,
                blocking=True,
                check_cancelled=check_cancelled,
                _construction_owner=installed.append,
            )
        except BaseException as error:  # noqa: B036 - exact cancellation asserted
            failures.append(error)

    waiter = threading.Thread(target=wait_for_shared)
    waiter.start()
    waiter.join(timeout=10)
    try:
        assert not waiter.is_alive()
        assert len(failures) == 1
        assert type(failures[0]) is Cancelled
        assert checks == 2
    finally:
        holder.close()

    replacement = _acquire(route, DirectoryLeaseMode.EXCLUSIVE)
    replacement.close()


def test_cancellation_after_successful_flock_releases_lease(tmp_path: Path) -> None:
    class Cancelled(BaseException):
        pass

    route = _route(_private_directory(tmp_path / "shard"))
    checks = 0

    def check_cancelled() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise Cancelled

    installed: list[PrivateDirectoryLeaseOwner] = []
    with pytest.raises(Cancelled):
        acquire_private_directory_lease(
            route,
            mode=DirectoryLeaseMode.EXCLUSIVE,
            blocking=True,
            check_cancelled=check_cancelled,
            _construction_owner=installed.append,
        )
    assert checks == 2

    owner = _acquire(route, DirectoryLeaseMode.EXCLUSIVE, blocking=False)
    owner.close()


def test_native_open_return_failure_closes_preinstalled_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Interrupted(BaseException):
        pass

    route = _route(_private_directory(tmp_path / "shard"))
    interruption = Interrupted("native directory open return interrupted")
    installed: list[PrivateDirectoryLeaseOwner] = []
    real_open = lease_module._native_workspace_owner._open_directory_fd

    def open_then_interrupt(native_owner: object, path: bytes) -> None:
        real_open(native_owner, path)
        raise interruption

    monkeypatch.setattr(
        lease_module._native_workspace_owner,
        "_open_directory_fd",
        open_then_interrupt,
    )
    with pytest.raises(Interrupted) as caught:
        acquire_private_directory_lease(
            route,
            mode=DirectoryLeaseMode.EXCLUSIVE,
            blocking=True,
            _construction_owner=installed.append,
        )

    assert caught.value is interruption
    assert len(installed) == 1
    assert installed[0].closed
    assert not lease_module._ACTIVE_DIRECTORY_LEASE_OWNERS
    monkeypatch.setattr(
        lease_module._native_workspace_owner,
        "_open_directory_fd",
        real_open,
    )
    replacement = _acquire(route, DirectoryLeaseMode.EXCLUSIVE, blocking=False)
    replacement.close()


def test_descriptor_close_failure_retains_same_thread_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _route(_private_directory(tmp_path / "shard"))
    owner = _acquire(route, DirectoryLeaseMode.SHARED)
    close_failure = OSError(errno.EIO, "lease descriptor close failed")
    real_close = lease_module._close_descriptor_for_cleanup

    def fail_close(_owner: PrivateDirectoryLeaseOwner) -> None:
        raise close_failure

    monkeypatch.setattr(
        lease_module,
        "_close_descriptor_for_cleanup",
        fail_close,
    )
    with pytest.raises(OSError) as caught:
        owner.close()

    assert caught.value is close_failure
    assert owner._descriptor_owner
    assert owner._claim_owner
    monkeypatch.setattr(lease_module, "_close_descriptor_for_cleanup", real_close)
    with pytest.raises(RuntimeError, match="already held by this thread"):
        _acquire(route, DirectoryLeaseMode.EXCLUSIVE, blocking=False)

    owner.close()
    replacement = _acquire(route, DirectoryLeaseMode.EXCLUSIVE, blocking=False)
    replacement.close()


def test_native_close_failure_retains_flock_until_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _route(_private_directory(tmp_path / "shard"))
    owner = _acquire(route, DirectoryLeaseMode.EXCLUSIVE)
    close_failure = OSError(errno.EPERM, "lease descriptor close denied")
    real_close = lease_module._native_workspace_owner._close_directory_fd_owner
    retained_native_owner = owner._native_descriptor_owner

    def fail_before_close(candidate: object) -> None:
        if candidate is retained_native_owner:
            raise close_failure
        real_close(candidate)

    monkeypatch.setattr(
        lease_module._native_workspace_owner,
        "_close_directory_fd_owner",
        fail_before_close,
    )
    with pytest.raises(OSError) as caught:
        owner.close()

    assert caught.value is close_failure
    assert not owner.closed
    result: queue.Queue[object] = queue.Queue()

    def try_shared() -> None:
        try:
            result.put(_acquire(route, DirectoryLeaseMode.SHARED, blocking=False))
        except BaseException as error:  # noqa: B036 - asserted below
            result.put(error)

    contender = threading.Thread(target=try_shared)
    contender.start()
    contender.join(timeout=10)
    assert not contender.is_alive()
    observed = result.get_nowait()
    try:
        assert type(observed) is BlockingIOError
    finally:
        if type(observed) is PrivateDirectoryLeaseOwner:
            observed.close()

    monkeypatch.setattr(
        lease_module._native_workspace_owner,
        "_close_directory_fd_owner",
        real_close,
    )
    owner.close()
    replacement = _acquire(route, DirectoryLeaseMode.SHARED, blocking=False)
    replacement.close()
    assert not lease_module._ACTIVE_DIRECTORY_LEASE_OWNERS


@pytest.mark.skipif(
    not Path("/proc/self/fd").is_dir(),
    reason="requires procfs descriptor accounting",
)
def test_success_failure_and_cancellation_do_not_leak_descriptors(
    tmp_path: Path,
) -> None:
    path = _private_directory(tmp_path / "shard")
    route = _route(path)

    def matching_descriptors() -> tuple[int, ...]:
        matches: list[int] = []
        for raw_descriptor in os.listdir("/proc/self/fd"):
            try:
                descriptor = int(raw_descriptor)
                metadata = os.fstat(descriptor)
            except (OSError, ValueError):
                continue
            if _identity_from_metadata(metadata) == route.identity:
                matches.append(descriptor)
        return tuple(matches)

    assert matching_descriptors() == ()

    for _ in range(20):
        owner = _acquire(route, DirectoryLeaseMode.SHARED)
        owner.close()
    assert matching_descriptors() == ()
    assert not lease_module._ACTIVE_DIRECTORY_LEASE_OWNERS

    wrong_identity = (
        route.identity[0],
        route.identity[1] + 1,
        route.identity[2],
        route.identity[3],
    )
    wrong_route = PrivateDirectoryLeaseRoute(path, wrong_identity, os.getpid())
    with pytest.raises(RuntimeError):
        _acquire(wrong_route, DirectoryLeaseMode.SHARED)
    assert matching_descriptors() == ()
    assert not lease_module._ACTIVE_DIRECTORY_LEASE_OWNERS


@pytest.mark.skipif(
    not hasattr(os, "fork") or not hasattr(os, "register_at_fork"),
    reason="requires POSIX fork callbacks",
)
def test_fork_child_fails_closed_while_parent_retains_lock(
    tmp_path: Path,
) -> None:
    path = _private_directory(tmp_path / "shard")
    route = _route(path)
    owner = _acquire(route, DirectoryLeaseMode.EXCLUSIVE)
    descriptor = owner._descriptor_owner[-1]
    child_pid = os.fork()
    if child_pid == 0:  # pragma: no cover - native callback must exit first
        os._exit(5)

    try:
        waited_pid, status = os.waitpid(child_pid, 0)
        assert waited_pid == child_pid
        assert os.waitstatus_to_exitcode(status) == (
            lease_module._FORK_CLEANUP_FAILURE_EXIT_CODE
        )
        assert not owner.closed
        assert os.fstat(descriptor).st_ino == route.identity[1]
    finally:
        owner.close()


@pytest.mark.skipif(
    not hasattr(os, "fork") or not hasattr(os, "register_at_fork"),
    reason="requires POSIX fork callbacks",
)
def test_fork_during_post_open_validation_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _route(_private_directory(tmp_path / "shard"))
    real_validate = lease_module._validate_identity_sandwich
    forked = False
    child_pid: int | None = None

    def validate_and_fork(descriptor: int, observed_route) -> None:
        nonlocal child_pid, forked
        real_validate(descriptor, observed_route)
        if forked:
            return
        forked = True
        child_pid = os.fork()
        if child_pid == 0:  # pragma: no cover - native callback must exit first
            os._exit(5)
        waited_pid, status = os.waitpid(child_pid, 0)
        child_pid = None
        assert waited_pid > 0
        assert os.waitstatus_to_exitcode(status) == (
            lease_module._FORK_CLEANUP_FAILURE_EXIT_CODE
        )

    monkeypatch.setattr(
        lease_module,
        "_validate_identity_sandwich",
        validate_and_fork,
    )
    try:
        owner = _acquire(route, DirectoryLeaseMode.EXCLUSIVE)
        owner.close()
    finally:
        if child_pid not in (None, 0):
            os.kill(child_pid, 9)
            os.waitpid(child_pid, 0)
    assert forked


@pytest.mark.skipif(
    not hasattr(os, "fork") or not hasattr(os, "register_at_fork"),
    reason="requires POSIX fork callbacks",
)
def test_fork_after_directory_owner_close_can_continue(tmp_path: Path) -> None:
    path = _private_directory(tmp_path / "shard")
    owner = _acquire(_route(path), DirectoryLeaseMode.EXCLUSIVE)
    owner.close()

    child_pid = os.fork()
    if child_pid == 0:  # pragma: no cover - disposable fork child
        os._exit(0)
    waited_pid, status = os.waitpid(child_pid, 0)
    assert waited_pid == child_pid
    assert os.waitstatus_to_exitcode(status) == 0


@pytest.mark.skipif(
    not hasattr(os, "fork") or ctypes.util.find_library("seccomp") is None,
    reason="requires Linux fork and libseccomp",
)
def test_fork_child_fails_closed_when_ofd_comparison_is_permanently_denied(
    tmp_path: Path,
) -> None:
    path = _private_directory(tmp_path / "shard")
    script = textwrap.dedent(
        """
        import ctypes
        import ctypes.util
        import errno
        import fcntl
        import os
        import stat
        import sys
        from pathlib import Path

        from codenib.compiler import _directory_lease as lease_module
        from codenib.compiler._directory_lease import (
            DirectoryLeaseMode,
            PrivateDirectoryLeaseRoute,
            acquire_private_directory_lease,
        )

        path = Path(sys.argv[1])
        metadata = path.lstat()
        route = PrivateDirectoryLeaseRoute(
            path,
            (
                metadata.st_dev,
                metadata.st_ino,
                stat.S_IFMT(metadata.st_mode),
                getattr(metadata, "st_file_attributes", 0),
            ),
            os.getpid(),
        )
        installed = []
        owner = acquire_private_directory_lease(
            route,
            mode=DirectoryLeaseMode.EXCLUSIVE,
            blocking=True,
            _construction_owner=installed.append,
        )
        descriptor = owner._descriptor_owner[-1]

        library_name = ctypes.util.find_library("seccomp") or "libseccomp.so.2"
        library = ctypes.CDLL(library_name, use_errno=True)
        library.seccomp_init.argtypes = [ctypes.c_uint32]
        library.seccomp_init.restype = ctypes.c_void_p
        library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
        library.seccomp_syscall_resolve_name.restype = ctypes.c_int
        library.seccomp_rule_add.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint,
        ]
        library.seccomp_rule_add.restype = ctypes.c_int
        library.seccomp_rule_add_array.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_void_p,
        ]
        library.seccomp_rule_add_array.restype = ctypes.c_int
        library.seccomp_load.argtypes = [ctypes.c_void_p]
        library.seccomp_load.restype = ctypes.c_int
        library.seccomp_release.argtypes = [ctypes.c_void_p]

        class ArgCmp(ctypes.Structure):
            _fields_ = [
                ("arg", ctypes.c_uint),
                ("op", ctypes.c_int),
                ("datum_a", ctypes.c_uint64),
                ("datum_b", ctypes.c_uint64),
            ]

        allow = 0x7FFF0000
        deny = 0x00050000 | errno.EPERM
        context = library.seccomp_init(allow)
        if not context:
            os._exit(77)
        kcmp_number = library.seccomp_syscall_resolve_name(b"kcmp")
        fcntl_number = library.seccomp_syscall_resolve_name(b"fcntl")
        if kcmp_number < 0 or fcntl_number < 0:
            os._exit(77)
        if library.seccomp_rule_add(context, deny, kcmp_number, 0) != 0:
            os._exit(77)
        comparison = ArgCmp(1, 4, fcntl.F_SETFL, 0)
        if library.seccomp_rule_add_array(
            context,
            deny,
            fcntl_number,
            1,
            ctypes.byref(comparison),
        ) != 0:
            os._exit(77)
        if library.seccomp_load(context) != 0:
            os._exit(77)
        library.seccomp_release(context)

        child_pid = os.fork()
        if child_pid == 0:
            os._exit(5)
        waited_pid, status = os.waitpid(child_pid, 0)
        child_status = os.waitstatus_to_exitcode(status)
        expected = lease_module._FORK_CLEANUP_FAILURE_EXIT_CODE
        os._exit(0 if waited_pid == child_pid and child_status == expected else 4)
        """
    )
    repository_root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(repository_root), environment.get("PYTHONPATH", ""))
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script, os.fspath(path)],
            cwd=repository_root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"fork child hung in inherited directory cleanup: {error}")
    if completed.returncode == 77:
        pytest.skip("libseccomp filter could not be installed")
    assert completed.returncode == 0, (completed.stdout, completed.stderr)


@pytest.mark.skipif(
    not hasattr(os, "fork") or not hasattr(os, "register_at_fork"),
    reason="requires POSIX fork callbacks",
)
@pytest.mark.parametrize(
    "replacement_target",
    ("public", "guard", "both", "both-same-ofd"),
)
def test_fork_child_fails_closed_after_earlier_callback_reuses_fd_pair(
    tmp_path: Path,
    replacement_target: str,
) -> None:
    path = _private_directory(tmp_path / "shard")
    foreign = _private_directory(tmp_path / "foreign")
    script = textwrap.dedent(
        """
        import os
        import stat
        import sys
        from pathlib import Path

        callback_state = {}

        def replace_one(descriptor):
            os.close(descriptor)
            source = os.open(
                callback_state["foreign"],
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            if source != descriptor:
                os.dup2(source, descriptor, inheritable=False)

        def replace_inherited_descriptor():
            target = callback_state["target"]
            if target == "both-same-ofd":
                public = callback_state["public"]
                guard = callback_state["guard"]
                os.close(public)
                os.close(guard)
                source = os.open(
                    callback_state["path"],
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                )
                if source != public:
                    os.dup2(source, public, inheritable=False)
                if source != guard:
                    os.dup2(source, guard, inheritable=False)
                return
            if target in {"public", "both"}:
                replace_one(callback_state["public"])
            if target in {"guard", "both"}:
                replace_one(callback_state["guard"])

        os.register_at_fork(after_in_child=replace_inherited_descriptor)

        from codenib.compiler import _directory_lease as lease_module
        from codenib.compiler._directory_lease import (
            DirectoryLeaseMode,
            PrivateDirectoryLeaseRoute,
            acquire_private_directory_lease,
        )

        path = Path(sys.argv[1])
        foreign = Path(sys.argv[2])
        metadata = path.lstat()
        route = PrivateDirectoryLeaseRoute(
            path,
            (
                metadata.st_dev,
                metadata.st_ino,
                stat.S_IFMT(metadata.st_mode),
                getattr(metadata, "st_file_attributes", 0),
            ),
            os.getpid(),
        )
        installed = []
        owner = acquire_private_directory_lease(
            route,
            mode=DirectoryLeaseMode.EXCLUSIVE,
            blocking=True,
            _construction_owner=installed.append,
        )
        public = owner._descriptor_owner[-1]
        matching = []
        for raw_descriptor in os.listdir("/proc/self/fd"):
            try:
                descriptor = int(raw_descriptor)
                observed = os.fstat(descriptor)
            except (OSError, ValueError):
                continue
            if observed.st_dev == metadata.st_dev and observed.st_ino == metadata.st_ino:
                matching.append(descriptor)
        guard = next(descriptor for descriptor in matching if descriptor != public)
        callback_state.update(
            public=public,
            guard=guard,
            foreign=os.fspath(foreign),
            path=os.fspath(path),
            target=sys.argv[3],
        )

        child_pid = os.fork()
        if child_pid == 0:
            os._exit(5)
        waited_pid, status = os.waitpid(child_pid, 0)
        child_status = os.waitstatus_to_exitcode(status)
        owner.close()
        expected = lease_module._FORK_CLEANUP_FAILURE_EXIT_CODE
        os._exit(0 if waited_pid == child_pid and child_status == expected else 4)
        """
    )
    repository_root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(repository_root), environment.get("PYTHONPATH", ""))
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            os.fspath(path),
            os.fspath(foreign),
            replacement_target,
        ],
        cwd=repository_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)


@pytest.mark.skipif(
    not hasattr(os, "fork") or not hasattr(os, "register_at_fork"),
    reason="requires POSIX fork callbacks",
)
def test_fork_after_native_open_authenticates_reused_unborrowed_fd(
    tmp_path: Path,
) -> None:
    path = _private_directory(tmp_path / "shard")
    foreign = _private_directory(tmp_path / "foreign")
    script = textwrap.dedent(
        """
        import os
        import stat
        import sys
        from pathlib import Path

        callback_state = {"armed": False, "reused": -1}

        def replace_unborrowed_descriptor():
            if not callback_state["armed"]:
                return
            matches = []
            for raw_descriptor in os.listdir("/proc/self/fd"):
                try:
                    descriptor = int(raw_descriptor)
                    metadata = os.fstat(descriptor)
                except (OSError, ValueError):
                    continue
                if (
                    metadata.st_dev == callback_state["device"]
                    and metadata.st_ino == callback_state["inode"]
                    and descriptor not in callback_state["baseline"]
                ):
                    matches.append(descriptor)
            descriptor = min(matches)
            os.close(descriptor)
            source = os.open(
                callback_state["foreign"],
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            if source != descriptor:
                os.dup2(source, descriptor, inheritable=False)
            callback_state["reused"] = descriptor

        os.register_at_fork(after_in_child=replace_unborrowed_descriptor)

        from codenib.compiler import _directory_lease as lease_module
        from codenib.compiler._directory_lease import (
            DirectoryLeaseMode,
            PrivateDirectoryLeaseRoute,
            acquire_private_directory_lease,
        )

        path = Path(sys.argv[1])
        foreign = Path(sys.argv[2])
        metadata = path.lstat()
        route = PrivateDirectoryLeaseRoute(
            path,
            (
                metadata.st_dev,
                metadata.st_ino,
                stat.S_IFMT(metadata.st_mode),
                getattr(metadata, "st_file_attributes", 0),
            ),
            os.getpid(),
        )
        baseline = set()
        for raw_descriptor in os.listdir("/proc/self/fd"):
            try:
                descriptor = int(raw_descriptor)
                os.fstat(descriptor)
            except (OSError, ValueError):
                continue
            baseline.add(descriptor)
        callback_state.update(
            device=metadata.st_dev,
            inode=metadata.st_ino,
            foreign=os.fspath(foreign),
            baseline=baseline,
        )
        installed = []
        real_open = lease_module._native_workspace_owner._open_directory_fd
        forked = False

        def open_and_fork(native_owner, path_bytes):
            global forked
            real_open(native_owner, path_bytes)
            if forked:
                return
            forked = True
            callback_state["armed"] = True
            child_pid = os.fork()
            if child_pid == 0:
                os._exit(5)
            waited_pid, status = os.waitpid(child_pid, 0)
            child_status = os.waitstatus_to_exitcode(status)
            expected = lease_module._FORK_CLEANUP_FAILURE_EXIT_CODE
            if waited_pid != child_pid or child_status != expected:
                raise AssertionError(
                    "fork-child unborrowed fd cleanup did not fail closed: "
                    f"status={child_status}"
                )
            callback_state["armed"] = False

        lease_module._native_workspace_owner._open_directory_fd = open_and_fork
        owner = acquire_private_directory_lease(
            route,
            mode=DirectoryLeaseMode.EXCLUSIVE,
            blocking=True,
            _construction_owner=installed.append,
        )
        owner.close()
        os._exit(0 if forked else 4)
        """
    )
    repository_root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(repository_root), environment.get("PYTHONPATH", ""))
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, os.fspath(path), os.fspath(foreign)],
        cwd=repository_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)


@pytest.mark.skipif(
    not hasattr(os, "fork") or not hasattr(os, "register_at_fork"),
    reason="requires POSIX fork callbacks",
)
def test_fork_child_fails_closed_with_inherited_private_descriptor_owner(
    tmp_path: Path,
) -> None:
    route = _route(_private_directory(tmp_path / "shard"))
    owner = lease_module._create_private_directory_descriptor_owner(route)
    owner._open()
    child_pid = os.fork()
    if child_pid == 0:  # pragma: no cover - native callback must exit first
        os._exit(5)

    try:
        waited_pid, status = os.waitpid(child_pid, 0)
        assert waited_pid == child_pid
        assert os.waitstatus_to_exitcode(status) == (
            lease_module._FORK_CLEANUP_FAILURE_EXIT_CODE
        )
        assert not owner.closed
    finally:
        owner.close()
