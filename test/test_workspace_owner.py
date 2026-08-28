# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import ctypes.util
import errno
import fcntl
import gc
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

import codenib._atomic_directory as atomic_module
import codenib._captured_directory as captured_module
import codenib._workspace_owner as workspace_owner
from codenib._captured_directory import (
    OwnedWorkspaceAuthority,
    PublishedWorkspaceReceiptOwner,
    WorkspaceDirectory,
    WorkspaceFile,
    WorkspacePlan,
)


def _plan() -> WorkspacePlan:
    return WorkspacePlan(
        subject_digest=hashlib.sha256(b"workspace-owner-native").hexdigest(),
        directories=(
            WorkspaceDirectory(Path("views")),
            WorkspaceDirectory(Path("views/bm25")),
        ),
    )


def _file_plan() -> WorkspacePlan:
    return WorkspacePlan(
        subject_digest=hashlib.sha256(b"workspace-owner-native-file").hexdigest(),
        directories=(
            WorkspaceDirectory(Path("views")),
            WorkspaceDirectory(Path("views/bm25")),
        ),
        files=(
            WorkspaceFile(
                Path("views/bm25/documents.json"),
                mode=0o600,
                max_bytes=1 << 20,
            ),
        ),
    )


def _require_native_owner() -> object:
    if not workspace_owner._workspace_owner_protocol_available:
        pytest.skip("optional workspace-owner extension is not built")
    try:
        workspace_owner.require_support()
    except RuntimeError as error:
        pytest.skip(f"native workspace ownership is unavailable: {error}")
    return workspace_owner.create_owner()


def _provision(
    root: Path,
    *,
    stage: bytes = b".stage",
    plan: WorkspacePlan | None = None,
) -> tuple[object, WorkspacePlan, object]:
    selected_plan = _plan() if plan is None else plan
    owner = _require_native_owner()
    publication_permit = workspace_owner.claim_owner_publish_permit(owner)
    assert (
        workspace_owner.provision_owner(
            owner,
            os.fsencode(root),
            b"published",
            stage,
            selected_plan.digest.encode("ascii"),
            selected_plan.root_mode,
            tuple(
                (os.fsencode(item.path.as_posix()), item.mode)
                for item in selected_plan.directories
            ),
            time.monotonic_ns() + 10_000_000_000,
        )
        is None
    )
    return owner, selected_plan, publication_permit


def _capture_existing_destination(root: Path, destination: bytes) -> object:
    owner = _require_native_owner()
    assert (
        workspace_owner.capture_owner_destination(
            owner,
            os.fsencode(root),
            destination,
            time.monotonic_ns() + 10_000_000_000,
        )
        is None
    )
    return owner


def _provision_replacement(
    root: Path,
    *,
    destination: bytes = b"published",
    slot: bytes = b".replacement",
    plan: WorkspacePlan | None = None,
) -> tuple[object, WorkspacePlan, object, int]:
    selected_plan = _file_plan() if plan is None else plan
    owner = _capture_existing_destination(root, destination)
    workspace_owner.acquire_owner_replacement_lease(
        owner,
        time.monotonic_ns() + 10_000_000_000,
    )
    incumbent_descriptor = workspace_owner.borrow_owner_destination_descriptor(owner)
    replacement_permit = workspace_owner.claim_owner_replacement_permit(owner)
    workspace_owner.provision_owner_replacement(
        owner,
        slot,
        selected_plan.digest.encode("ascii"),
        selected_plan.root_mode,
        tuple(
            (os.fsencode(item.path.as_posix()), item.mode)
            for item in selected_plan.directories
        ),
        time.monotonic_ns() + 10_000_000_000,
    )
    return owner, selected_plan, replacement_permit, incumbent_descriptor


def _adopt_and_fill_replacement(
    root: Path,
    owner: object,
    plan: WorkspacePlan,
    *,
    destination: bytes = b"published",
    slot: bytes = b".replacement",
    payload: bytes = b"replacement",
) -> None:
    workspace_owner.verify_owner_adoption_binding(
        owner,
        os.fsencode(root / os.fsdecode(destination)),
        slot,
        plan.digest.encode("ascii"),
    )
    workspace_owner.mark_owner_adopted(owner)
    if plan.files:
        file_spec = plan.files[0]
        workspace_owner.begin_owner_file(
            owner,
            os.fsencode(file_spec.path.parent.as_posix()),
            os.fsencode(file_spec.path.name),
            file_spec.mode,
        )
        workspace_owner.write_owner_file(owner, payload)
        workspace_owner.finish_owner_file(owner, file_spec.mode)
    workspace_owner.seal_owner_directories(owner)


def _tree_fingerprint(root: Path) -> tuple[tuple[object, ...], ...]:
    entries: list[tuple[object, ...]] = []
    for path in (root, *sorted(root.rglob("*"))):
        metadata = path.lstat()
        payload: bytes | str | None = None
        if stat.S_ISREG(metadata.st_mode):
            payload = path.read_bytes()
        elif stat.S_ISLNK(metadata.st_mode):
            payload = os.readlink(path)
        entries.append(
            (
                os.fspath(path.relative_to(root.parent)),
                stat.S_IFMT(metadata.st_mode),
                stat.S_IMODE(metadata.st_mode),
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_nlink,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
                payload,
            )
        )
    return tuple(entries)


@pytest.mark.parametrize("replacement", (False, True), ids=("missing", "exact"))
def test_native_provisioning_preserves_exact_mid_plan_cancellation(
    tmp_path: Path,
    replacement: bool,
) -> None:
    root = tmp_path / "authority"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    destination = root / "published"
    stage_name = b".replacement" if replacement else b".stage"
    stage = root / os.fsdecode(stage_name)
    directories = tuple(
        (f"directory-{index:02d}".encode("ascii"), 0o711) for index in range(8)
    )
    if replacement:
        destination.mkdir(mode=0o700)
        (destination / "incumbent.txt").write_bytes(b"incumbent")
        owner = _capture_existing_destination(root, b"published")
        workspace_owner.acquire_owner_replacement_lease(
            owner,
            time.monotonic_ns() + 10_000_000_000,
        )
        permit = workspace_owner.claim_owner_replacement_permit(owner)
    else:
        owner = _require_native_owner()
        permit = workspace_owner.claim_owner_publish_permit(owner)
    cancellation = KeyboardInterrupt("cancel during native provisioning")
    observed_partial_counts: list[int] = []

    def check_cancelled() -> None:
        if not stage.is_dir():
            return
        created = tuple(
            (stage.joinpath(os.fsdecode(path)), mode)
            for path, mode in directories
            if stage.joinpath(os.fsdecode(path)).is_dir()
        )
        if 0 < len(created) < len(directories):
            assert all(
                stat.S_IMODE(path.lstat().st_mode) == mode for path, mode in created
            )
            observed_partial_counts.append(len(created))
            raise cancellation

    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            if replacement:
                workspace_owner.provision_owner_replacement(
                    owner,
                    stage_name,
                    b"0" * 64,
                    0o700,
                    directories,
                    time.monotonic_ns() + 10_000_000_000,
                    check_cancelled=check_cancelled,
                )
            else:
                workspace_owner.provision_owner(
                    owner,
                    os.fsencode(root),
                    b"published",
                    stage_name,
                    b"0" * 64,
                    0o700,
                    directories,
                    time.monotonic_ns() + 10_000_000_000,
                    check_cancelled=check_cancelled,
                )

        assert caught.value is cancellation
        assert len(observed_partial_counts) == 1
        assert 0 < observed_partial_counts[0] < len(directories)
        assert stage.is_dir()
        if replacement:
            assert workspace_owner.owner_state(owner) == "replacement-provisioning"
            assert destination.is_dir()
            assert (destination / "incumbent.txt").read_bytes() == b"incumbent"
        else:
            assert workspace_owner.owner_state(owner) == "provisioning"
            assert not destination.exists()
    finally:
        if not workspace_owner.owner_closed(owner):
            workspace_owner.abort_owner(owner)
        del permit

    assert workspace_owner.owner_closed(owner)
    if replacement:
        assert stage.is_dir()
        assert (
            sum(path.is_dir() for path in stage.iterdir()) == observed_partial_counts[0]
        )
        assert (destination / "incumbent.txt").read_bytes() == b"incumbent"
        assert set(root.iterdir()) == {destination, stage}
    else:
        assert not stage.exists()
        assert not destination.exists()
        quarantined = tuple(root.glob(".codenib-workspace-orphan-*"))
        assert len(quarantined) == 1
        assert (
            sum(path.is_dir() for path in quarantined[0].iterdir())
            == observed_partial_counts[0]
        )


def test_native_directory_seal_preserves_exact_mid_loop_cancellation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    plan = WorkspacePlan(
        subject_digest=hashlib.sha256(b"interruptible-directory-seal").hexdigest(),
        directories=tuple(
            WorkspaceDirectory(Path(f"directory-{index:02d}")) for index in range(4)
        ),
    )
    owner, plan, permit = _provision(root, plan=plan)
    workspace_owner.verify_owner_adoption_binding(
        owner,
        os.fsencode(root / "published"),
        b".stage",
        plan.digest.encode("ascii"),
    )
    workspace_owner.mark_owner_adopted(owner)
    cancellation = KeyboardInterrupt("cancel during native directory seal")
    callback_calls = 0

    def check_cancelled() -> None:
        nonlocal callback_calls
        callback_calls += 1
        if callback_calls == 2:
            raise cancellation

    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            workspace_owner.seal_owner_directories(
                owner,
                check_cancelled=check_cancelled,
            )

        assert caught.value is cancellation
        assert callback_calls == 2
        assert workspace_owner.owner_state(owner) == "adopted"
        assert workspace_owner.verify_owner_authority(owner) is None
        assert workspace_owner.seal_owner_directories(owner) is None
    finally:
        if not workspace_owner.owner_closed(owner):
            workspace_owner.abort_owner(owner)
        del permit

    assert workspace_owner.owner_closed(owner)
    assert not (root / ".stage").exists()
    assert not (root / "published").exists()
    quarantined = tuple(root.glob(".codenib-workspace-orphan-*"))
    assert len(quarantined) == 1
    assert {path.name for path in quarantined[0].iterdir()} == {
        f"directory-{index:02d}" for index in range(4)
    }


def _compile_exchange_fault_shim(tmp_path: Path) -> Path:
    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("a C compiler is required for the exchange fault shim")
    source = tmp_path / "exchange_faults.c"
    library = tmp_path / "exchange_faults.so"
    source.write_text(
        textwrap.dedent(
            r"""
            #define _GNU_SOURCE
            #include <dlfcn.h>
            #include <errno.h>
            #include <fcntl.h>
            #include <stdarg.h>
            #include <stdlib.h>
            #include <string.h>
            #include <sys/file.h>
            #include <sys/stat.h>
            #include <sys/syscall.h>
            #include <unistd.h>

            #ifndef RENAME_EXCHANGE
            #define RENAME_EXCHANGE (1U << 1)
            #endif

            typedef long (*syscall_function)(long, ...);
            typedef int (*fsync_function)(int);
            typedef int (*flock_function)(int, int);
            typedef int (*fcntl_function)(int, int, ...);

            static syscall_function real_syscall;
            static fsync_function real_fsync;
            static flock_function real_flock;
            static fcntl_function real_fcntl;
            static fcntl_function real_fcntl64;
            static int exchange_calls;
            static int exchange_live;
            static int fsync_failed;
            static int unlock_failed;
            static int slot_stats;
            static int post_exchange_fsyncs;
            static int lock_rebind_injected;
            static int lock_delay_injected;
            static int exclusive_lock_calls;
            static int lease_unlocked;
            static int close_kcmp_failed;
            static int close_fcntl_failed;
            static int replacement_mkdir_calls;
            static int settlement_slot_stat_failures;

            static const char *fault_mode(void) {
              const char *mode = getenv("CODENIB_EXCHANGE_FAULT");
              return mode == NULL ? "" : mode;
            }

            static void resolve_symbols(void) {
              if (real_syscall == NULL) {
                real_syscall = (syscall_function)dlsym(RTLD_NEXT, "syscall");
              }
              if (real_fsync == NULL) {
                real_fsync = (fsync_function)dlsym(RTLD_NEXT, "fsync");
              }
              if (real_flock == NULL) {
                real_flock = (flock_function)dlsym(RTLD_NEXT, "flock");
              }
              if (real_fcntl == NULL) {
                real_fcntl = (fcntl_function)dlsym(RTLD_NEXT, "fcntl");
              }
              if (real_fcntl64 == NULL) {
                real_fcntl64 = (fcntl_function)dlsym(RTLD_NEXT, "fcntl64");
              }
            }

            long syscall(long number, ...) {
              va_list arguments;
              long first;
              long second;
              long third;
              long fourth;
              long fifth;
              long sixth;
              long result;
              const char *mode;

              resolve_symbols();
              if (real_syscall == NULL) {
                errno = ENOSYS;
                return -1;
              }
              mode = fault_mode();
              va_start(arguments, number);
              first = va_arg(arguments, long);
              if (number == SYS_close) {
                va_end(arguments);
                return real_syscall(number, first);
              }
              second = va_arg(arguments, long);
              if (number == SYS_fstat) {
                va_end(arguments);
                return real_syscall(number, first, second);
              }
              third = va_arg(arguments, long);
              fourth = va_arg(arguments, long);
              if (number == SYS_newfstatat) {
                va_end(arguments);
                if (second != 0 &&
                    strcmp((const char *)second, ".replacement") == 0 &&
                    (strcmp(mode, "provision-identity-fail") == 0 ||
                     strcmp(mode, "mkdir-fail-stat-ambiguous") == 0)) {
                  ++slot_stats;
                  if (slot_stats == 2) {
                    if (strcmp(mode, "mkdir-fail-stat-ambiguous") == 0) {
                      ++settlement_slot_stat_failures;
                    }
                    errno = EIO;
                    return -1;
                  }
                }
                return real_syscall(number, first, second, third, fourth);
              }
              fifth = va_arg(arguments, long);
              if (number == SYS_kcmp) {
                va_end(arguments);
                if (lease_unlocked && !close_kcmp_failed &&
                    strcmp(mode, "close-auth-error-once") == 0) {
                  close_kcmp_failed = 1;
                  errno = EPERM;
                  return -1;
                }
                return real_syscall(number, first, second, third, fourth, fifth);
              }
              if (number == SYS_renameat2) {
                va_end(arguments);
                if ((unsigned int)fifth != RENAME_EXCHANGE) {
                  return real_syscall(number, first, second, third, fourth, fifth);
                }
                ++exchange_calls;
                if (strcmp(mode, "unsupported") == 0) {
                  errno = EOPNOTSUPP;
                  return -1;
                }
                if (strcmp(mode, "success-without-swap") == 0) {
                  return 0;
                }
                if (strcmp(mode, "reverse-error-once") == 0 &&
                    exchange_calls == 2) {
                  errno = EIO;
                  return -1;
                }
                if (strcmp(mode, "reverse-success-without-restore") == 0 &&
                    exchange_calls == 2) {
                  return 0;
                }
                result = real_syscall(number, first, second, third, fourth, fifth);
                if (result == 0) {
                  exchange_live = !exchange_live;
                }
                if (strcmp(mode, "error-after-swap") == 0 &&
                    exchange_calls == 1 && result == 0) {
                  errno = EIO;
                  return -1;
                }
                if (strcmp(mode, "reverse-error-after-restore") == 0 &&
                    exchange_calls == 2 && result == 0) {
                  errno = EIO;
                  return -1;
                }
                return result;
              }
              sixth = va_arg(arguments, long);
              va_end(arguments);
              return real_syscall(
                  number, first, second, third, fourth, fifth, sixth
              );
            }

            int fsync(int descriptor) {
              const char *mode;
              resolve_symbols();
              if (real_fsync == NULL) {
                errno = ENOSYS;
                return -1;
              }
              mode = fault_mode();
              if (exchange_calls > 0) {
                ++post_exchange_fsyncs;
              }
              if (!fsync_failed && exchange_live &&
                  strcmp(mode, "forward-fsync-once") == 0) {
                fsync_failed = 1;
                errno = EIO;
                return -1;
              }
              if (!fsync_failed && !exchange_live && exchange_calls >= 2 &&
                  strcmp(mode, "reverse-fsync-once") == 0) {
                fsync_failed = 1;
                errno = EIO;
                return -1;
              }
              return real_fsync(descriptor);
            }

            int mkdirat(int descriptor, const char *path, mode_t mode) {
              int result;
              const char *fault;

              resolve_symbols();
              if (real_syscall == NULL) {
                errno = ENOSYS;
                return -1;
              }
              fault = fault_mode();
              if (path != NULL && strcmp(path, ".replacement") == 0 &&
                  (strcmp(fault, "mkdir-fail-before-create") == 0 ||
                   strcmp(fault, "mkdir-fail-stat-ambiguous") == 0 ||
                   strcmp(fault, "mkdir-error-after-create") == 0)) {
                ++replacement_mkdir_calls;
                if (strcmp(fault, "mkdir-error-after-create") != 0) {
                  errno = EIO;
                  return -1;
                }
                result = (int)real_syscall(SYS_mkdirat, descriptor, path, mode);
                if (result == 0) {
                  errno = EIO;
                  return -1;
                }
                return result;
              }
              return (int)real_syscall(SYS_mkdirat, descriptor, path, mode);
            }

            int flock(int descriptor, int operation) {
              int result;
              resolve_symbols();
              if (real_flock == NULL) {
                errno = ENOSYS;
                return -1;
              }
              if (operation == (LOCK_EX | LOCK_NB)) {
                ++exclusive_lock_calls;
                if (exclusive_lock_calls > 1 &&
                    strcmp(fault_mode(), "second-lock-fail") == 0) {
                  errno = EBUSY;
                  return -1;
                }
              }
              if (!unlock_failed && exchange_live && operation == LOCK_UN &&
                  strcmp(fault_mode(), "unlock-fail-once") == 0) {
                unlock_failed = 1;
                errno = EIO;
                return -1;
              }
              if (!unlock_failed && operation == LOCK_UN &&
                  (strcmp(fault_mode(),
                          "deadline-after-lock-unlock-fail") == 0 ||
                   strcmp(fault_mode(),
                          "destination-rebind-after-lock-unlock-fail") == 0)) {
                unlock_failed = 1;
                errno = EIO;
                return -1;
              }
              result = real_flock(descriptor, operation);
              if (result == 0 && operation == LOCK_UN &&
                  strcmp(fault_mode(), "close-auth-error-once") == 0) {
                lease_unlocked = 1;
              }
              if (result == 0 && !lock_delay_injected &&
                  operation == (LOCK_EX | LOCK_NB) &&
                  strcmp(fault_mode(),
                         "deadline-after-lock-unlock-fail") == 0) {
                lock_delay_injected = 1;
                usleep(100000);
              }
              if (result == 0 && !lock_rebind_injected &&
                  operation == (LOCK_EX | LOCK_NB) &&
                  (strcmp(fault_mode(), "destination-rebind-after-lock") == 0 ||
                   strcmp(fault_mode(),
                          "destination-rebind-after-lock-unlock-fail") == 0)) {
                if (real_syscall(SYS_renameat2, descriptor, "published",
                                 descriptor, ".foreign", RENAME_EXCHANGE) != 0) {
                  int exchange_error = errno;
                  real_flock(descriptor, LOCK_UN);
                  errno = exchange_error;
                  return -1;
                }
                lock_rebind_injected = 1;
              }
              return result;
            }

            int fcntl(int descriptor, int command, ...) {
              va_list arguments;
              int integer_argument;
              void *pointer_argument;

              resolve_symbols();
              if (real_fcntl == NULL) {
                errno = ENOSYS;
                return -1;
              }
              if (command == F_GETFL && lease_unlocked && close_kcmp_failed &&
                  !close_fcntl_failed &&
                  strcmp(fault_mode(), "close-auth-error-once") == 0) {
                close_fcntl_failed = 1;
                errno = EIO;
                return -1;
              }
              if (command == F_GETFD || command == F_GETFL ||
                  command == F_GETOWN || command == F_GETSIG) {
                return real_fcntl(descriptor, command);
              }
              va_start(arguments, command);
              if (command == F_DUPFD || command == F_DUPFD_CLOEXEC ||
                  command == F_SETFD || command == F_SETFL ||
                  command == F_SETOWN || command == F_SETSIG) {
                integer_argument = va_arg(arguments, int);
                va_end(arguments);
                return real_fcntl(descriptor, command, integer_argument);
              }
              pointer_argument = va_arg(arguments, void *);
              va_end(arguments);
              return real_fcntl(descriptor, command, pointer_argument);
            }

            int fcntl64(int descriptor, int command, ...) {
              va_list arguments;
              int integer_argument;
              void *pointer_argument;

              resolve_symbols();
              if (real_fcntl64 == NULL) {
                errno = ENOSYS;
                return -1;
              }
              if (command == F_GETFL && lease_unlocked && close_kcmp_failed &&
                  !close_fcntl_failed &&
                  strcmp(fault_mode(), "close-auth-error-once") == 0) {
                close_fcntl_failed = 1;
                errno = EIO;
                return -1;
              }
              if (command == F_GETFD || command == F_GETFL ||
                  command == F_GETOWN || command == F_GETSIG) {
                return real_fcntl64(descriptor, command);
              }
              va_start(arguments, command);
              if (command == F_DUPFD || command == F_DUPFD_CLOEXEC ||
                  command == F_SETFD || command == F_SETFL ||
                  command == F_SETOWN || command == F_SETSIG) {
                integer_argument = va_arg(arguments, int);
                va_end(arguments);
                return real_fcntl64(descriptor, command, integer_argument);
              }
              pointer_argument = va_arg(arguments, void *);
              va_end(arguments);
              return real_fcntl64(descriptor, command, pointer_argument);
            }

            int codenib_exchange_fault_exchange_calls(void) {
              return exchange_calls;
            }

            int codenib_exchange_fault_fsync_failed(void) {
              return fsync_failed;
            }

            int codenib_exchange_fault_unlock_failed(void) {
              return unlock_failed;
            }

            int codenib_exchange_fault_slot_stats(void) {
              return slot_stats;
            }

            int codenib_exchange_fault_post_exchange_fsyncs(void) {
              return post_exchange_fsyncs;
            }

            int codenib_exchange_fault_lock_rebind_injected(void) {
              return lock_rebind_injected;
            }

            int codenib_exchange_fault_lock_delay_injected(void) {
              return lock_delay_injected;
            }

            int codenib_exchange_fault_exclusive_lock_calls(void) {
              return exclusive_lock_calls;
            }

            int codenib_exchange_fault_close_kcmp_failed(void) {
              return close_kcmp_failed;
            }

            int codenib_exchange_fault_close_fcntl_failed(void) {
              return close_fcntl_failed;
            }

            int codenib_exchange_fault_replacement_mkdir_calls(void) {
              return replacement_mkdir_calls;
            }

            int codenib_exchange_fault_settlement_slot_stat_failures(void) {
              return settlement_slot_stat_failures;
            }
            """
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [compiler, "-shared", "-fPIC", "-O2", "-Wall", "-o", library, source, "-ldl"],
        check=True,
        capture_output=True,
        text=True,
    )
    return library


def _run_exchange_fault_script(
    root: Path,
    library: Path,
    mode: str,
    script: str,
) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    existing_preload = environment.get("LD_PRELOAD")
    environment["LD_PRELOAD"] = os.pathsep.join(
        value for value in (os.fspath(library), existing_preload) if value
    )
    environment["CODENIB_EXCHANGE_FAULT"] = mode
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(repository_root), environment.get("PYTHONPATH", ""))
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, os.fspath(root), os.fspath(library)],
        cwd=repository_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_workspace_owner_facade_rejects_an_incomplete_abi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        workspace_owner,
        "_workspace_owner_protocol_available",
        False,
    )
    monkeypatch.setattr(workspace_owner, "_require_support_exact", None)

    with pytest.raises(RuntimeError, match="workspace-owner extension"):
        workspace_owner.require_support()


def test_workspace_owner_facade_rejects_symbol_complete_protocol_v2() -> None:
    required_symbols = (
        "require_support",
        "create_owner_exact",
        "claim_owner_publish_permit_exact",
        "require_owner_exact",
        "close_owner_exact",
        "provision_owner_exact",
        "verify_owner_authority_exact",
        "verify_owner_adoption_binding_exact",
        "borrow_owner_parent_descriptor_exact",
        "borrow_owner_root_descriptor_exact",
        "borrow_owner_directory_descriptor_exact",
        "begin_owner_file_exact",
        "write_owner_file_exact",
        "finish_owner_file_exact",
        "abort_owner_file_exact",
        "seal_owner_directories_exact",
        "sync_owner_parent_exact",
        "mark_owner_adopted_exact",
        "rename_owner_child_noreplace_exact",
        "commit_owner_receipt_exact",
        "abort_owner_exact",
        "quarantine_owner_exact",
        "owner_state_exact",
        "owner_closed_exact",
    )
    script = textwrap.dedent(
        f"""
        import sys
        import types

        implementation = types.ModuleType("codenib._workspace_owner_impl")
        implementation.workspace_owner_protocol_version = 2
        for name in {required_symbols!r}:
            setattr(implementation, name, lambda *args, **kwargs: None)
        sys.modules[implementation.__name__] = implementation

        import codenib._workspace_owner as facade

        assert not facade._workspace_owner_protocol_available
        try:
            facade.require_support()
        except RuntimeError as error:
            assert "workspace-owner extension" in str(error)
        else:
            raise AssertionError("protocol-v2 implementation was accepted")
        """
    )
    repository_root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(repository_root), environment.get("PYTHONPATH", ""))
    )

    subprocess.run(
        [sys.executable, "-c", script],
        cwd=repository_root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


def test_workspace_owner_facade_rejects_symbol_complete_protocol_v4() -> None:
    required_symbols = (
        "require_support",
        "create_owner_exact",
        "claim_owner_publish_permit_exact",
        "claim_owner_replacement_permit_exact",
        "require_owner_exact",
        "close_owner_exact",
        "provision_owner_exact",
        "capture_owner_destination_exact",
        "acquire_owner_replacement_lease_exact",
        "provision_owner_replacement_exact",
        "verify_owner_authority_exact",
        "verify_owner_adoption_binding_exact",
        "verify_owner_destination_binding_exact",
        "verify_owner_replacement_binding_exact",
        "borrow_owner_parent_descriptor_exact",
        "borrow_owner_root_descriptor_exact",
        "borrow_owner_destination_descriptor_exact",
        "borrow_owner_directory_descriptor_exact",
        "begin_owner_file_exact",
        "write_owner_file_exact",
        "finish_owner_file_exact",
        "abort_owner_file_exact",
        "seal_owner_directories_exact",
        "sync_owner_parent_exact",
        "mark_owner_adopted_exact",
        "rename_owner_child_noreplace_exact",
        "exchange_owner_replacement_exact",
        "commit_owner_receipt_exact",
        "abort_owner_exact",
        "quarantine_owner_exact",
        "owner_state_exact",
        "owner_closed_exact",
    )
    script = textwrap.dedent(
        f"""
        import sys
        import types

        implementation = types.ModuleType("codenib._workspace_owner_impl")
        implementation.workspace_owner_protocol_version = 4
        for name in {required_symbols!r}:
            setattr(implementation, name, lambda *args, **kwargs: None)
        sys.modules[implementation.__name__] = implementation

        import codenib._workspace_owner as facade

        assert not facade._workspace_owner_protocol_available
        try:
            facade.require_support()
        except RuntimeError as error:
            assert "workspace-owner extension" in str(error)
        else:
            raise AssertionError("protocol-v4 implementation was accepted")
        """
    )
    repository_root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(repository_root), environment.get("PYTHONPATH", ""))
    )

    subprocess.run(
        [sys.executable, "-c", script],
        cwd=repository_root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


def test_workspace_owner_facade_rejects_each_incomplete_protocol_v6_abi() -> None:
    required_symbols = (
        "require_support",
        "create_owner_exact",
        "claim_owner_publish_permit_exact",
        "claim_owner_replacement_permit_exact",
        "require_owner_exact",
        "close_owner_exact",
        "provision_owner_exact",
        "provision_owner_interruptibly_exact",
        "capture_owner_destination_exact",
        "acquire_owner_replacement_lease_exact",
        "provision_owner_replacement_exact",
        "provision_owner_replacement_interruptibly_exact",
        "verify_owner_authority_exact",
        "verify_owner_adoption_binding_exact",
        "verify_owner_destination_binding_exact",
        "verify_owner_replacement_binding_exact",
        "borrow_owner_parent_descriptor_exact",
        "borrow_owner_root_descriptor_exact",
        "borrow_owner_destination_descriptor_exact",
        "borrow_owner_directory_descriptor_exact",
        "begin_owner_file_exact",
        "write_owner_file_exact",
        "finish_owner_file_exact",
        "abort_owner_file_exact",
        "seal_owner_directories_exact",
        "seal_owner_directories_interruptibly_exact",
        "sync_owner_parent_exact",
        "mark_owner_adopted_exact",
        "rename_owner_child_noreplace_exact",
        "exchange_owner_replacement_exact",
        "commit_owner_receipt_exact",
        "abort_owner_exact",
        "quarantine_owner_exact",
        "owner_state_exact",
        "owner_closed_exact",
    )
    script = textwrap.dedent(
        f"""
        import importlib
        import sys
        import types

        import codenib

        required = {required_symbols!r}
        cached = tuple(
            "_require_support_exact" if name == "require_support" else f"_{{name}}"
            for name in required
        )
        for missing in required:
            implementation = types.ModuleType("codenib._workspace_owner_impl")
            implementation.workspace_owner_protocol_version = 6
            for name in required:
                if name != missing:
                    setattr(implementation, name, lambda *args, **kwargs: None)
            sys.modules[implementation.__name__] = implementation
            setattr(codenib, "_workspace_owner_impl", implementation)
            sys.modules.pop("codenib._workspace_owner", None)
            if hasattr(codenib, "_workspace_owner"):
                delattr(codenib, "_workspace_owner")

            facade = importlib.import_module("codenib._workspace_owner")
            assert not facade._workspace_owner_protocol_available, missing
            for cached_name in cached:
                assert getattr(facade, cached_name) is None, (missing, cached_name)
            try:
                facade.require_support()
            except RuntimeError as error:
                assert "workspace-owner extension" in str(error)
            else:
                raise AssertionError(f"incomplete protocol-v6 ABI accepted: {{missing}}")
        """
    )
    repository_root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(repository_root), environment.get("PYTHONPATH", ""))
    )

    subprocess.run(
        [sys.executable, "-c", script],
        cwd=repository_root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


def test_directory_fd_owner_protocol_is_additive_to_workspace_protocol() -> None:
    workspace_symbols = (
        "require_support",
        "create_owner_exact",
        "claim_owner_publish_permit_exact",
        "claim_owner_replacement_permit_exact",
        "require_owner_exact",
        "close_owner_exact",
        "provision_owner_exact",
        "provision_owner_interruptibly_exact",
        "capture_owner_destination_exact",
        "acquire_owner_replacement_lease_exact",
        "provision_owner_replacement_exact",
        "provision_owner_replacement_interruptibly_exact",
        "verify_owner_authority_exact",
        "verify_owner_adoption_binding_exact",
        "verify_owner_destination_binding_exact",
        "verify_owner_replacement_binding_exact",
        "borrow_owner_parent_descriptor_exact",
        "borrow_owner_root_descriptor_exact",
        "borrow_owner_destination_descriptor_exact",
        "borrow_owner_directory_descriptor_exact",
        "begin_owner_file_exact",
        "write_owner_file_exact",
        "finish_owner_file_exact",
        "abort_owner_file_exact",
        "seal_owner_directories_exact",
        "seal_owner_directories_interruptibly_exact",
        "sync_owner_parent_exact",
        "mark_owner_adopted_exact",
        "rename_owner_child_noreplace_exact",
        "exchange_owner_replacement_exact",
        "commit_owner_receipt_exact",
        "abort_owner_exact",
        "quarantine_owner_exact",
        "owner_state_exact",
        "owner_closed_exact",
    )
    script = textwrap.dedent(
        f"""
        import sys
        import types

        implementation = types.ModuleType("codenib._workspace_owner_impl")
        implementation.workspace_owner_protocol_version = 6
        for name in {workspace_symbols!r}:
            setattr(implementation, name, lambda *args, **kwargs: None)
        sys.modules[implementation.__name__] = implementation

        import codenib._workspace_owner as facade

        assert facade._workspace_owner_protocol_available
        assert not facade._directory_fd_owner_protocol_available
        assert facade.require_support() is None
        try:
            facade._require_directory_fd_owner_support()
        except RuntimeError as error:
            assert "directory-fd-owner extension ABI" in str(error)
        else:
            raise AssertionError("missing additive fd-owner ABI was accepted")
        """
    )
    repository_root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(repository_root), environment.get("PYTHONPATH", ""))
    )

    subprocess.run(
        [sys.executable, "-c", script],
        cwd=repository_root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


def test_directory_fd_owner_support_rejects_missing_native_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        workspace_owner,
        "_directory_fd_owner_protocol_available",
        True,
    )
    monkeypatch.setattr(workspace_owner, "_directory_fd_owner_supported", 0)

    with pytest.raises(RuntimeError, match="native directory open flags"):
        workspace_owner._require_directory_fd_owner_support()


def test_workspace_owner_cleanup_does_not_repeat_the_support_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = object()
    closed: list[object] = []

    def reject_support() -> None:
        raise PermissionError("support probe was blocked after acquisition")

    def require_owner(value: object) -> object:
        return value

    def close_owner(value: object) -> None:
        closed.append(value)

    monkeypatch.setattr(
        workspace_owner,
        "_workspace_owner_protocol_available",
        True,
    )
    monkeypatch.setattr(workspace_owner, "_require_support_exact", reject_support)
    monkeypatch.setattr(workspace_owner, "_require_owner_exact", require_owner)
    monkeypatch.setattr(workspace_owner, "_close_owner_exact", close_owner)

    assert workspace_owner.require_exact_owner(candidate) is candidate
    assert workspace_owner.close_owner_exact(candidate) is None
    assert closed == [candidate]


def test_native_directory_fd_owner_has_exact_lifecycle(tmp_path: Path) -> None:
    if not workspace_owner._directory_fd_owner_protocol_available:
        pytest.skip("optional workspace-owner extension is not built")
    directory = tmp_path / "leased"
    directory.mkdir()
    owner = workspace_owner._create_directory_fd_owner()

    assert not workspace_owner._directory_fd_owner_closed(owner)
    with pytest.raises(RuntimeError, match="not open"):
        workspace_owner._borrow_directory_fd(owner)

    assert workspace_owner._open_directory_fd(owner, os.fsencode(directory)) is None
    descriptor = workspace_owner._borrow_directory_fd(owner)
    metadata = os.fstat(descriptor)
    assert stat.S_ISDIR(metadata.st_mode)
    assert fcntl.fcntl(descriptor, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
    assert fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
    checks: list[bool] = []

    def check_before_mkdir() -> None:
        checks.append(True)

    assert workspace_owner._mkdir_directory_fd_child(
        owner,
        b"child",
        check_before_mkdir,
    )
    assert not workspace_owner._mkdir_directory_fd_child(
        owner,
        b"child",
        check_before_mkdir,
    )
    assert checks == [True, True]
    assert (directory / "child").is_dir()
    with pytest.raises(RuntimeError, match="cannot be reopened"):
        workspace_owner._open_directory_fd(owner, os.fsencode(directory))
    assert workspace_owner._close_directory_fd_owner(owner) is None
    assert workspace_owner._close_directory_fd_owner(owner) is None
    assert workspace_owner._directory_fd_owner_closed(owner)
    with pytest.raises(OSError) as caught:
        os.fstat(descriptor)
    assert caught.value.errno == errno.EBADF
    with pytest.raises(RuntimeError, match="not open"):
        workspace_owner._borrow_directory_fd(owner)
    with pytest.raises(RuntimeError, match="cannot be reopened"):
        workspace_owner._open_directory_fd(owner, os.fsencode(directory))


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_native_directory_fd_owner_failstops_fork_child(
    tmp_path: Path,
) -> None:
    if not workspace_owner._directory_fd_owner_protocol_available:
        pytest.skip("optional workspace-owner extension is not built")

    directory = tmp_path / "leased"
    directory.mkdir()
    owner = workspace_owner._create_directory_fd_owner()
    workspace_owner._open_directory_fd(owner, os.fsencode(directory))
    descriptor = workspace_owner._borrow_directory_fd(owner)
    child_pid = os.fork()
    if child_pid == 0:  # pragma: no cover - native callback must exit first
        os._exit(5)

    try:
        waited_pid, status = os.waitpid(child_pid, 0)
        assert waited_pid == child_pid
        assert os.waitstatus_to_exitcode(status) == 70
        assert os.fstat(descriptor).st_ino == directory.stat().st_ino
    finally:
        workspace_owner._close_directory_fd_owner(owner)


def test_native_directory_fd_owner_fork_guard_rejects_parent_call(
    tmp_path: Path,
) -> None:
    if not workspace_owner._directory_fd_owner_protocol_available:
        pytest.skip("optional workspace-owner extension is not built")

    directory = tmp_path / "leased"
    directory.mkdir()
    owner = workspace_owner._create_directory_fd_owner()
    workspace_owner._open_directory_fd(owner, os.fsencode(directory))
    descriptor = workspace_owner._borrow_directory_fd(owner)
    try:
        with pytest.raises(RuntimeError, match="requires a fork-child process"):
            workspace_owner._fail_fork_child_if_directory_fd_owner_active()
        assert os.fstat(descriptor).st_ino == directory.stat().st_ino
        assert not workspace_owner._directory_fd_owner_closed(owner)
    finally:
        workspace_owner._close_directory_fd_owner(owner)

    assert workspace_owner._fail_fork_child_if_directory_fd_owner_active() is None


def test_native_directory_fd_owner_does_not_close_reused_foreign_fd(
    tmp_path: Path,
) -> None:
    if not workspace_owner._directory_fd_owner_protocol_available:
        pytest.skip("optional workspace-owner extension is not built")
    leased = tmp_path / "leased"
    foreign = tmp_path / "foreign"
    leased.mkdir()
    foreign.mkdir()
    owner = workspace_owner._create_directory_fd_owner()
    workspace_owner._open_directory_fd(owner, os.fsencode(leased))
    descriptor = workspace_owner._borrow_directory_fd(owner)
    os.close(descriptor)
    foreign_source = os.open(
        foreign,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    foreign_descriptor = foreign_source
    if foreign_source != descriptor:
        os.dup2(foreign_source, descriptor, inheritable=False)
        foreign_descriptor = descriptor
    try:
        expected = os.fstat(foreign_descriptor)
        with pytest.raises(OSError) as caught:
            workspace_owner._close_directory_fd_owner(owner)

        assert caught.value.errno == errno.ESTALE
        assert workspace_owner._directory_fd_owner_closed(owner)
        observed = os.fstat(foreign_descriptor)
        assert (observed.st_dev, observed.st_ino) == (
            expected.st_dev,
            expected.st_ino,
        )
        assert workspace_owner._close_directory_fd_owner(owner) is None
    finally:
        os.close(foreign_descriptor)
        if foreign_source != foreign_descriptor:
            os.close(foreign_source)


def test_native_directory_fd_owner_terminalizes_two_closed_slots(
    tmp_path: Path,
) -> None:
    if not workspace_owner._directory_fd_owner_protocol_available:
        pytest.skip("optional workspace-owner extension is not built")
    directory = tmp_path / "leased"
    directory.mkdir()
    owner = workspace_owner._create_directory_fd_owner()
    workspace_owner._open_directory_fd(owner, os.fsencode(directory))
    descriptor = workspace_owner._borrow_directory_fd(owner)
    expected = directory.stat()
    owned_descriptors = []
    for raw_descriptor in os.listdir("/proc/self/fd"):
        try:
            candidate = int(raw_descriptor)
            observed = os.fstat(candidate)
        except (OSError, ValueError):
            continue
        if (observed.st_dev, observed.st_ino) == (
            expected.st_dev,
            expected.st_ino,
        ):
            owned_descriptors.append(candidate)
    assert descriptor in owned_descriptors
    assert len(owned_descriptors) == 2
    for candidate in owned_descriptors:
        os.close(candidate)

    with pytest.raises(OSError) as caught:
        workspace_owner._close_directory_fd_owner(owner)

    assert caught.value.errno == errno.ESTALE
    assert workspace_owner._directory_fd_owner_closed(owner)
    assert workspace_owner._fail_fork_child_if_directory_fd_owner_active() is None


def test_native_directory_fd_owner_retains_same_inode_with_unsafe_flags(
    tmp_path: Path,
) -> None:
    if not workspace_owner._directory_fd_owner_protocol_available:
        pytest.skip("optional workspace-owner extension is not built")
    leased = tmp_path / "leased"
    foreign = tmp_path / "foreign"
    leased.mkdir()
    foreign.mkdir()
    owner = workspace_owner._create_directory_fd_owner()
    workspace_owner._open_directory_fd(owner, os.fsencode(leased))
    descriptor = workspace_owner._borrow_directory_fd(owner)
    expected = leased.stat()
    owner_slots = []
    for raw_descriptor in os.listdir("/proc/self/fd"):
        try:
            candidate = int(raw_descriptor)
            observed = os.fstat(candidate)
        except (OSError, ValueError):
            continue
        if (observed.st_dev, observed.st_ino) == (
            expected.st_dev,
            expected.st_ino,
        ):
            owner_slots.append(candidate)
    assert len(owner_slots) == 2
    guard = next(candidate for candidate in owner_slots if candidate != descriptor)
    os.set_inheritable(descriptor, True)
    os.close(guard)
    foreign_source = os.open(
        foreign,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    if foreign_source != guard:
        os.dup2(foreign_source, guard, inheritable=False)
    try:
        with pytest.raises(OSError) as caught:
            workspace_owner._close_directory_fd_owner(owner)

        assert caught.value.errno in (errno.EPERM, errno.ESTALE)
        assert not workspace_owner._directory_fd_owner_closed(owner)
        assert os.fstat(descriptor).st_ino == expected.st_ino
    finally:
        os.close(guard)
        os.dup2(descriptor, guard, inheritable=False)
        os.set_inheritable(descriptor, False)
        if foreign_source != guard:
            os.close(foreign_source)
        workspace_owner._close_directory_fd_owner(owner)

    assert workspace_owner._directory_fd_owner_closed(owner)


def test_native_directory_fd_owner_retains_independently_reopened_guard(
    tmp_path: Path,
) -> None:
    if not workspace_owner._directory_fd_owner_protocol_available:
        pytest.skip("optional workspace-owner extension is not built")
    leased = tmp_path / "leased"
    leased.mkdir()
    owner = workspace_owner._create_directory_fd_owner()
    workspace_owner._open_directory_fd(owner, os.fsencode(leased))
    descriptor = workspace_owner._borrow_directory_fd(owner)
    expected = leased.stat()
    owner_slots = []
    for raw_descriptor in os.listdir("/proc/self/fd"):
        try:
            candidate = int(raw_descriptor)
            observed = os.fstat(candidate)
        except (OSError, ValueError):
            continue
        if (observed.st_dev, observed.st_ino) == (
            expected.st_dev,
            expected.st_ino,
        ):
            owner_slots.append(candidate)
    assert len(owner_slots) == 2
    guard = next(candidate for candidate in owner_slots if candidate != descriptor)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.close(guard)
    replacement_source = os.open(
        leased,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    if replacement_source != guard:
        os.dup2(replacement_source, guard, inheritable=False)
    try:
        with pytest.raises(OSError) as caught:
            workspace_owner._close_directory_fd_owner(owner)

        assert caught.value.errno == errno.ESTALE
        assert not workspace_owner._directory_fd_owner_closed(owner)
        assert os.fstat(descriptor).st_ino == expected.st_ino
        assert os.fstat(guard).st_ino == expected.st_ino
        with pytest.raises(RuntimeError, match="fork-child process"):
            workspace_owner._fail_fork_child_if_directory_fd_owner_active()
    finally:
        os.close(guard)
        os.dup2(descriptor, guard, inheritable=False)
        if replacement_source != guard:
            os.close(replacement_source)
        workspace_owner._close_directory_fd_owner(owner)

    assert workspace_owner._directory_fd_owner_closed(owner)


def test_directory_fd_owner_facade_keeps_exact_functions_pinned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not workspace_owner._directory_fd_owner_protocol_available:
        pytest.skip("optional workspace-owner extension is not built")
    implementation = workspace_owner._implementation
    assert implementation is not None

    def reject_mutable_dispatch(*_args: object) -> object:
        raise AssertionError("mutable implementation dispatch was consulted")

    for name in (
        "create_directory_fd_owner_exact",
        "open_directory_fd_exact",
        "borrow_directory_fd_exact",
        "mkdir_directory_fd_child_exact",
        "close_directory_fd_owner_exact",
        "directory_fd_owner_closed_exact",
        "fail_fork_child_if_directory_fd_owner_active_exact",
    ):
        monkeypatch.setattr(implementation, name, reject_mutable_dispatch)

    owner = workspace_owner._create_directory_fd_owner()
    workspace_owner._open_directory_fd(owner, os.fsencode(tmp_path))
    assert workspace_owner._borrow_directory_fd(owner) >= 0
    assert workspace_owner._mkdir_directory_fd_child(
        owner,
        b"pinned-child",
        lambda: None,
    )
    assert not workspace_owner._directory_fd_owner_closed(owner)
    workspace_owner._close_directory_fd_owner(owner)
    assert workspace_owner._directory_fd_owner_closed(owner)


def test_native_directory_fd_owner_rejects_invalid_paths(tmp_path: Path) -> None:
    if not workspace_owner._directory_fd_owner_protocol_available:
        pytest.skip("optional workspace-owner extension is not built")

    for invalid in ("not-bytes", bytearray(b"not-exact")):
        owner = workspace_owner._create_directory_fd_owner()
        with pytest.raises(TypeError, match="exact bytes"):
            workspace_owner._open_directory_fd(owner, invalid)  # type: ignore[arg-type]
        workspace_owner._close_directory_fd_owner(owner)

    for invalid in (b"", b"embedded\0nul", b"x" * 4097):
        owner = workspace_owner._create_directory_fd_owner()
        with pytest.raises(ValueError, match="empty, unbounded, or contains NUL"):
            workspace_owner._open_directory_fd(owner, invalid)
        workspace_owner._close_directory_fd_owner(owner)

    target = tmp_path / "target"
    target.mkdir()
    symlink = tmp_path / "symlink"
    symlink.symlink_to(target, target_is_directory=True)
    owner = workspace_owner._create_directory_fd_owner()
    with pytest.raises(OSError):
        workspace_owner._open_directory_fd(owner, os.fsencode(symlink))
    workspace_owner._close_directory_fd_owner(owner)


def test_native_directory_fd_owner_is_exact_and_destructor_closes(
    tmp_path: Path,
) -> None:
    if not workspace_owner._directory_fd_owner_protocol_available:
        pytest.skip("optional workspace-owner extension is not built")
    owner = workspace_owner._create_directory_fd_owner()
    owner_type = type(owner)
    with pytest.raises(TypeError, match="cannot be constructed"):
        owner_type()
    with pytest.raises(AttributeError):
        owner.fd = -1
    with pytest.raises(TypeError, match="invalid native type"):
        workspace_owner._borrow_directory_fd(object())

    workspace_owner._open_directory_fd(owner, os.fsencode(tmp_path))
    descriptor = workspace_owner._borrow_directory_fd(owner)
    del owner
    gc.collect()

    with pytest.raises(OSError) as caught:
        os.fstat(descriptor)
    assert caught.value.errno == errno.EBADF


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or ctypes.util.find_library("seccomp") is None,
    reason="requires Linux libseccomp",
)
def test_native_directory_fd_owner_retains_close_denied_by_seccomp(
    tmp_path: Path,
) -> None:
    if not workspace_owner._directory_fd_owner_protocol_available:
        pytest.skip("optional workspace-owner extension is not built")
    directory = tmp_path / "leased"
    directory.mkdir()
    script = textwrap.dedent(
        """
        import ctypes
        import ctypes.util
        import errno
        import fcntl
        import os
        import sys

        import codenib._workspace_owner as workspace_owner

        owner = workspace_owner._create_directory_fd_owner()
        workspace_owner._open_directory_fd(owner, os.fsencode(sys.argv[1]))
        descriptor = workspace_owner._borrow_directory_fd(owner)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        expected = os.fstat(descriptor)

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
        library.seccomp_load.argtypes = [ctypes.c_void_p]
        library.seccomp_load.restype = ctypes.c_int
        library.seccomp_release.argtypes = [ctypes.c_void_p]

        allow = 0x7FFF0000
        deny = 0x00050000 | errno.EPERM
        context = library.seccomp_init(allow)
        if not context:
            os._exit(77)
        close_number = library.seccomp_syscall_resolve_name(b"close")
        if close_number < 0:
            os._exit(77)
        if library.seccomp_rule_add(context, deny, close_number, 0) != 0:
            os._exit(77)
        if library.seccomp_load(context) != 0:
            os._exit(77)
        library.seccomp_release(context)

        for _ in range(2):
            try:
                workspace_owner._close_directory_fd_owner(owner)
            except PermissionError as error:
                assert error.errno == errno.EPERM
            else:
                os._exit(3)
            assert not workspace_owner._directory_fd_owner_closed(owner)
            observed = os.fstat(descriptor)
            assert (observed.st_dev, observed.st_ino) == (
                expected.st_dev,
                expected.st_ino,
            )
        os._exit(0)
        """
    )
    repository_root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(repository_root), environment.get("PYTHONPATH", ""))
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, os.fspath(directory)],
        cwd=repository_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode == 77:
        pytest.skip("libseccomp filter could not be installed")
    assert completed.returncode == 0, (completed.stdout, completed.stderr)


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or ctypes.util.find_library("seccomp") is None,
    reason="requires Linux libseccomp",
)
def test_native_directory_open_failure_retains_partial_owner_and_fork_guard(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "leased"
    directory.mkdir()
    script = textwrap.dedent(
        """
        import ctypes
        import ctypes.util
        import errno
        import fcntl
        import os
        import sys

        import codenib._workspace_owner as workspace_owner

        class ArgCmp(ctypes.Structure):
            _fields_ = [
                ("arg", ctypes.c_uint),
                ("op", ctypes.c_int),
                ("datum_a", ctypes.c_uint64),
                ("datum_b", ctypes.c_uint64),
            ]

        owner = workspace_owner._create_directory_fd_owner()
        expected = os.stat(sys.argv[1])
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
            ctypes.POINTER(ArgCmp),
        ]
        library.seccomp_rule_add_array.restype = ctypes.c_int
        library.seccomp_load.argtypes = [ctypes.c_void_p]
        library.seccomp_load.restype = ctypes.c_int
        library.seccomp_release.argtypes = [ctypes.c_void_p]

        allow = 0x7FFF0000
        deny = 0x00050000 | errno.EPERM
        equal = 4
        context = library.seccomp_init(allow)
        if not context:
            raise SystemExit(77)
        fcntl_number = library.seccomp_syscall_resolve_name(b"fcntl")
        close_number = library.seccomp_syscall_resolve_name(b"close")
        comparison = ArgCmp(1, equal, fcntl.F_DUPFD_CLOEXEC, 0)
        if (
            fcntl_number < 0
            or close_number < 0
            or library.seccomp_rule_add_array(
                context,
                deny,
                fcntl_number,
                1,
                ctypes.byref(comparison),
            )
            != 0
            or library.seccomp_rule_add(context, deny, close_number, 0) != 0
            or library.seccomp_load(context) != 0
        ):
            library.seccomp_release(context)
            raise SystemExit(77)
        library.seccomp_release(context)

        try:
            workspace_owner._open_directory_fd(owner, os.fsencode(sys.argv[1]))
        except OSError as error:
            assert error.errno == errno.EPERM
        else:
            raise AssertionError("guard-duplication denial was accepted")
        assert not workspace_owner._directory_fd_owner_closed(owner)
        matching = []
        for raw_descriptor in os.listdir("/proc/self/fd"):
            try:
                descriptor = int(raw_descriptor)
                observed = os.fstat(descriptor)
            except (OSError, ValueError):
                continue
            if (observed.st_dev, observed.st_ino) == (
                expected.st_dev,
                expected.st_ino,
            ):
                matching.append(descriptor)
        assert len(matching) == 1
        try:
            workspace_owner._close_directory_fd_owner(owner)
        except OSError as error:
            assert error.errno == errno.EPERM
        else:
            raise AssertionError("close denial was reported as terminal")
        assert not workspace_owner._directory_fd_owner_closed(owner)

        child = os.fork()
        if child == 0:
            os._exit(3)
        _waited, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 70
        """
    )
    repository_root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(repository_root), environment.get("PYTHONPATH", ""))
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, os.fspath(directory)],
        cwd=repository_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode == 77:
        pytest.skip("libseccomp filter could not be installed")
    assert completed.returncode == 0, (completed.stdout, completed.stderr)


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or ctypes.util.find_library("seccomp") is None,
    reason="requires Linux libseccomp",
)
@pytest.mark.parametrize("deny_f_setfl", (False, True))
def test_native_cleanup_remains_safe_after_seccomp_tightening(
    tmp_path: Path,
    deny_f_setfl: bool,
) -> None:
    probe_owner = _require_native_owner()
    workspace_owner.close_owner_exact(probe_owner)
    root = tmp_path / ("deny-both" if deny_f_setfl else "deny-kcmp")
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    script = textwrap.dedent(
        f"""
        import ctypes
        import ctypes.util
        import errno
        import fcntl
        import os
        import sys
        import time

        import codenib._workspace_owner as workspace_owner

        root = os.fsencode(sys.argv[1])
        owner = workspace_owner.create_owner()
        publication_permit = workspace_owner.claim_owner_publish_permit(owner)
        workspace_owner.provision_owner(
            owner,
            root,
            b"published",
            b".stage",
            b"0" * 64,
            0o700,
            (),
            time.monotonic_ns() + 10_000_000_000,
        )
        workspace_owner.borrow_owner_root_descriptor(owner)

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
        library.seccomp_load.argtypes = [ctypes.c_void_p]
        library.seccomp_load.restype = ctypes.c_int
        library.seccomp_release.argtypes = [ctypes.c_void_p]

        allow = 0x7FFF0000
        deny = 0x00050000 | errno.EPERM
        context = library.seccomp_init(allow)
        if not context:
            raise SystemExit(77)

        def deny_syscall(name):
            number = library.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number < 0:
                raise SystemExit(77)
            if library.seccomp_rule_add(context, deny, number, 0) != 0:
                raise SystemExit(77)

        deny_syscall("kcmp")
        if {deny_f_setfl!r}:
            class ArgCmp(ctypes.Structure):
                _fields_ = [
                    ("arg", ctypes.c_uint),
                    ("op", ctypes.c_int),
                    ("datum_a", ctypes.c_uint64),
                    ("datum_b", ctypes.c_uint64),
                ]

            library.seccomp_rule_add_array.argtypes = [
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_int,
                ctypes.c_uint,
                ctypes.POINTER(ArgCmp),
            ]
            library.seccomp_rule_add_array.restype = ctypes.c_int
            fcntl_number = library.seccomp_syscall_resolve_name(b"fcntl")
            comparison = ArgCmp(1, 4, fcntl.F_SETFL, 0)
            if fcntl_number < 0 or library.seccomp_rule_add_array(
                context,
                deny,
                fcntl_number,
                1,
                ctypes.byref(comparison),
            ) != 0:
                raise SystemExit(77)

        if library.seccomp_load(context) != 0:
            raise SystemExit(77)
        library.seccomp_release(context)

        def owned_targets():
            observed = []
            for name in os.listdir("/proc/self/fd"):
                try:
                    target = os.readlink(f"/proc/self/fd/{{name}}")
                except OSError:
                    continue
                if os.fsdecode(root) in target:
                    observed.append(target)
            return tuple(sorted(observed))

        if {deny_f_setfl!r}:
            before = owned_targets()
            assert before
            after_first = None
            for attempt in range(2):
                try:
                    workspace_owner.abort_owner(owner)
                except PermissionError as error:
                    assert error.errno == errno.EPERM
                else:
                    raise AssertionError("cleanup unexpectedly bypassed denied OFD checks")
                observed = owned_targets()
                assert len(observed) >= 2
                if attempt == 0:
                    after_first = observed
                else:
                    assert observed == after_first
            assert workspace_owner.owner_state(owner) == "quarantined"
            assert not workspace_owner.owner_closed(owner)
        else:
            workspace_owner.abort_owner(owner)
            assert workspace_owner.owner_closed(owner)

        del publication_permit
        """
    )
    repository_root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(repository_root), environment.get("PYTHONPATH", ""))
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, os.fspath(root)],
        cwd=repository_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 77:
        pytest.skip("libseccomp filter could not be installed")
    assert completed.returncode == 0, completed.stderr


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux")
def test_native_low_available_fd_failure_is_cleanup_atomic(tmp_path: Path) -> None:
    probe_owner = _require_native_owner()
    workspace_owner.close_owner_exact(probe_owner)
    root = tmp_path / "low-fd"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    script = textwrap.dedent(
        """
        import errno
        import gc
        import os
        import resource
        import sys
        import time

        import codenib._workspace_owner as workspace_owner

        root = os.fsencode(sys.argv[1])
        owner = workspace_owner.create_owner()
        publication_permit = workspace_owner.claim_owner_publish_permit(owner)
        soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
        selected_limit = (
            128 if hard_limit == resource.RLIM_INFINITY else min(hard_limit, 128)
        )
        if selected_limit < 64:
            raise SystemExit(77)
        resource.setrlimit(resource.RLIMIT_NOFILE, (selected_limit, hard_limit))
        pressure = []
        try:
            while True:
                pressure.append(
                    os.open("/dev/null", os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
                )
        except OSError as error:
            assert error.errno == errno.EMFILE
        for descriptor in pressure[-11:]:
            os.close(descriptor)
        del pressure[-11:]

        try:
            workspace_owner.provision_owner(
                owner,
                root,
                b"published",
                b".stage",
                b"0" * 64,
                0o700,
                (),
                time.monotonic_ns() + 10_000_000_000,
            )
        except OSError as error:
            assert error.errno == errno.EMFILE
        else:
            raise AssertionError("low-descriptor provision unexpectedly succeeded")

        workspace_owner.abort_owner(owner)
        assert workspace_owner.owner_closed(owner)
        assert os.listdir(os.fsdecode(root)) == []
        del publication_permit
        del owner
        gc.collect()
        targets = []
        for name in os.listdir("/proc/self/fd"):
            try:
                target = os.readlink(f"/proc/self/fd/{name}")
            except OSError:
                continue
            if os.fsdecode(root) in target:
                targets.append(target)
        assert targets == []
        for descriptor in pressure:
            os.close(descriptor)
        """
    )
    repository_root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(repository_root), environment.get("PYTHONPATH", ""))
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, os.fspath(root)],
        cwd=repository_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 77:
        pytest.skip("RLIMIT_NOFILE is too low for the probe")
    assert completed.returncode == 0, completed.stderr


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux")
def test_native_capture_budgets_hidden_replacement_lease_pair(tmp_path: Path) -> None:
    probe_owner = _require_native_owner()
    workspace_owner.close_owner_exact(probe_owner)
    root = tmp_path / "capture-low-fd"
    (root / "published").mkdir(parents=True)
    root.chmod(0o700)
    script = textwrap.dedent(
        """
        import errno
        import gc
        import os
        import resource
        import sys
        import time
        from pathlib import Path

        import codenib._workspace_owner as workspace_owner

        root_path = Path(sys.argv[1])
        root = os.fsencode(root_path)
        owner = workspace_owner.create_owner()
        _soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
        selected_limit = (
            256 if hard_limit == resource.RLIM_INFINITY else min(hard_limit, 256)
        )
        absolute_records = len(root_path.parts)
        owner_pairs_without_hidden_lease = absolute_records + 1 + 1
        prior_required = owner_pairs_without_hidden_lease * 2 + 2 + 64
        if selected_limit <= prior_required + 1:
            raise SystemExit(77)
        resource.setrlimit(resource.RLIMIT_NOFILE, (selected_limit, hard_limit))
        pressure = []
        try:
            while True:
                pressure.append(
                    os.open("/dev/null", os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
                )
        except OSError as error:
            assert error.errno == errno.EMFILE
        for descriptor in pressure[-(prior_required + 1) :]:
            os.close(descriptor)
        del pressure[-(prior_required + 1) :]

        try:
            workspace_owner.capture_owner_destination(
                owner,
                root,
                b"published",
                time.monotonic_ns() + 10_000_000_000,
            )
        except OSError as error:
            assert error.errno == errno.EMFILE
        else:
            raise AssertionError("capture omitted the hidden lease-pair budget")

        assert workspace_owner.owner_state(owner) == "empty"
        workspace_owner.abort_owner(owner)
        assert workspace_owner.owner_closed(owner)
        del owner
        gc.collect()
        retained_targets = []
        for name in os.listdir("/proc/self/fd"):
            try:
                target = os.readlink(f"/proc/self/fd/{name}")
            except OSError:
                continue
            if os.fspath(root_path) in target:
                retained_targets.append(target)
        assert retained_targets == []
        assert (root_path / "published").is_dir()
        for descriptor in pressure:
            os.close(descriptor)
        """
    )
    repository_root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(repository_root), environment.get("PYTHONPATH", ""))
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, os.fspath(root)],
        cwd=repository_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 77:
        pytest.skip("RLIMIT_NOFILE is too low for the capture lease-pair probe")
    assert completed.returncode == 0, completed.stderr


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux")
def test_native_replacement_additional_fd_budget_fails_before_mkdir(
    tmp_path: Path,
) -> None:
    probe_owner = _require_native_owner()
    workspace_owner.close_owner_exact(probe_owner)
    root = tmp_path / "replacement-low-fd"
    (root / "published").mkdir(parents=True)
    root.chmod(0o700)
    script = textwrap.dedent(
        """
        import errno
        import gc
        import os
        import resource
        import sys
        import time

        import codenib._workspace_owner as workspace_owner

        root = os.fsencode(sys.argv[1])
        owner = workspace_owner.create_owner()
        workspace_owner.capture_owner_destination(
            owner,
            root,
            b"published",
            time.monotonic_ns() + 10_000_000_000,
        )
        workspace_owner.acquire_owner_replacement_lease(
            owner,
            time.monotonic_ns() + 10_000_000_000,
        )
        permit = workspace_owner.claim_owner_replacement_permit(owner)
        soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
        selected_limit = (
            128 if hard_limit == resource.RLIM_INFINITY else min(hard_limit, 128)
        )
        if selected_limit < 96:
            raise SystemExit(77)
        resource.setrlimit(resource.RLIMIT_NOFILE, (selected_limit, hard_limit))
        pressure = []
        try:
            while True:
                pressure.append(
                    os.open("/dev/null", os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
                )
        except OSError as error:
            assert error.errno == errno.EMFILE
        for descriptor in pressure[-11:]:
            os.close(descriptor)
        del pressure[-11:]

        try:
            workspace_owner.provision_owner_replacement(
                owner,
                b".replacement",
                b"0" * 64,
                0o700,
                (),
                time.monotonic_ns() + 10_000_000_000,
            )
        except OSError as error:
            assert error.errno == errno.EMFILE
        else:
            raise AssertionError("replacement descriptor preflight succeeded")
        assert workspace_owner.owner_state(owner) == "destination-leased"
        assert not os.path.exists(os.path.join(os.fsdecode(root), ".replacement"))

        workspace_owner.abort_owner(owner)
        assert workspace_owner.owner_closed(owner)
        del permit
        del owner
        gc.collect()
        targets = []
        for name in os.listdir("/proc/self/fd"):
            try:
                target = os.readlink(f"/proc/self/fd/{name}")
            except OSError:
                continue
            if os.fsdecode(root) in target:
                targets.append(target)
        assert targets == []
        for descriptor in pressure:
            os.close(descriptor)
        """
    )
    repository_root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(repository_root), environment.get("PYTHONPATH", ""))
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, os.fspath(root)],
        cwd=repository_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 77:
        pytest.skip("RLIMIT_NOFILE is too low for the replacement probe")
    assert completed.returncode == 0, completed.stderr


def test_native_owner_provisions_exact_skeleton_and_quarantines(
    tmp_path: Path,
) -> None:
    owner, plan, _publication_permit = _provision(tmp_path)
    stage = tmp_path / ".stage"

    assert workspace_owner.owner_state(owner) == "provisioned"
    assert stat.S_IMODE(stage.stat().st_mode) == plan.root_mode
    assert tuple(
        path.relative_to(stage).as_posix() for path in sorted(stage.rglob("*"))
    ) == ("views", "views/bm25")
    assert workspace_owner.require_exact_owner(owner) is owner

    orphan_name = workspace_owner.quarantine_owner(owner)
    assert workspace_owner.quarantine_owner(owner) == orphan_name
    assert orphan_name is not None
    assert not stage.exists()
    assert (tmp_path / orphan_name).is_dir()
    workspace_owner.close_owner_exact(owner)
    workspace_owner.close_owner_exact(owner)
    assert workspace_owner.owner_closed(owner)


def test_native_owner_writes_without_exposing_the_file_descriptor(
    tmp_path: Path,
) -> None:
    owner, _plan_value, _publication_permit = _provision(tmp_path)
    workspace_owner.mark_owner_adopted(owner)

    assert (
        workspace_owner.begin_owner_file(
            owner,
            b"views/bm25",
            b"documents.json",
            0o600,
        )
        is None
    )
    assert workspace_owner.write_owner_file(owner, b'{"documents":') is None
    assert workspace_owner.write_owner_file(owner, b"[]}") is None
    metadata = workspace_owner.finish_owner_file(owner, 0o600)

    assert type(metadata) is tuple
    assert len(metadata) == 8
    assert metadata[2] & 0o170000 == stat.S_IFREG
    assert metadata[3] == len(b'{"documents":[]}')
    assert (tmp_path / ".stage/views/bm25/documents.json").read_bytes() == (
        b'{"documents":[]}'
    )
    workspace_owner.abort_owner(owner)
    assert workspace_owner.owner_closed(owner)


def test_native_publication_facade_borrows_without_closing_the_owner(
    tmp_path: Path,
) -> None:
    owner, _plan_value, publication_permit = _provision(tmp_path)
    authority_owner = atomic_module._PublicationAuthorityOwner()
    authority = atomic_module._adopt_native_posix_publication_authority(
        tmp_path,
        native_owner=owner,
        publication_permit=publication_permit,
        authority_owner=authority_owner,
    )

    ownership = authority.capture_child(
        ".stage",
        path=tmp_path / ".stage",
        label="native workspace stage",
        allow_empty_root=True,
    )
    assert ownership.inventory == (("views", "directory"), ("views/bm25", "directory"))
    authority.sync_parent()
    authority_owner.close()

    assert authority._closed
    assert not workspace_owner.owner_closed(owner)
    workspace_owner.abort_owner(owner)
    assert workspace_owner.owner_closed(owner)


def test_native_owner_publishes_through_the_caller_receipt(
    tmp_path: Path,
) -> None:
    plan = _file_plan()
    owner, _plan_value, publication_permit = _provision(tmp_path, plan=plan)
    workspace = OwnedWorkspaceAuthority()
    receipt_owner = PublishedWorkspaceReceiptOwner()
    payload = b'{"documents":[]}'

    workspace.adopt_provisioned(
        destination=tmp_path / "published",
        stage_name=".stage",
        provisioned_owner=owner,
        publication_permit=publication_permit,
        plan=plan,
        destination_binding=None,
    )
    record = workspace.write_file(
        "views/bm25/documents.json",
        (payload[:5], payload[5:]),
    )
    assert record.sha256 == hashlib.sha256(payload).hexdigest()
    workspace.seal()
    workspace.publish_into(receipt_owner)

    assert receipt_owner.active
    assert workspace_owner.owner_state(owner) == "receipted"
    assert (tmp_path / "published/views/bm25/documents.json").read_bytes() == payload

    receipt_owner.close()

    assert receipt_owner.closed
    assert workspace_owner.owner_closed(owner)
    assert (tmp_path / "published/views/bm25/documents.json").read_bytes() == payload


def test_native_publication_binds_exact_rename_before_validators(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _file_plan()
    owner, _plan_value, publication_permit = _provision(tmp_path, plan=plan)
    workspace = OwnedWorkspaceAuthority()
    receipt_owner = PublishedWorkspaceReceiptOwner()
    workspace.adopt_provisioned(
        destination=tmp_path / "published",
        stage_name=".stage",
        provisioned_owner=owner,
        publication_permit=publication_permit,
        plan=plan,
        destination_binding=None,
    )
    workspace.write_file("views/bm25/documents.json", (b"{}",))
    workspace.seal()
    intercepted_tokens: list[object] = []
    original_rename = workspace_owner.rename_owner_child_noreplace

    def intercept_rename(
        permit: object,
        source: bytes,
        destination: bytes,
    ) -> object | None:
        token = original_rename(permit, source, destination)
        if token is not None:
            intercepted_tokens.append(token)
        return token

    monkeypatch.setattr(
        workspace_owner,
        "rename_owner_child_noreplace",
        intercept_rename,
    )

    workspace.publish_into(
        receipt_owner,
        validate_staged_directory=lambda _reader: None,
        validate_published_destination=lambda _reader: None,
    )

    assert intercepted_tokens == []
    assert receipt_owner.active
    assert workspace_owner.owner_state(owner) == "receipted"
    receipt_owner.close()
    assert receipt_owner.closed


def test_native_publication_binds_receipt_installation_before_validators(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _file_plan()
    owner, _plan_value, publication_permit = _provision(tmp_path, plan=plan)
    workspace = OwnedWorkspaceAuthority()
    receipt_owner = PublishedWorkspaceReceiptOwner()
    workspace.adopt_provisioned(
        destination=tmp_path / "published",
        stage_name=".stage",
        provisioned_owner=owner,
        publication_permit=publication_permit,
        plan=plan,
        destination_binding=None,
    )
    workspace.write_file("views/bm25/documents.json", (b"{}",))
    workspace.seal()
    receipt_type = captured_module.PublishedWorkspaceReceipt
    intercepted: list[str] = []

    def intercept_receipt(*_args: object, **_kwargs: object) -> object:
        intercepted.append("receipt")
        raise AssertionError("validator intercepted the native receipt token")

    def intercept_install(
        _candidate: PublishedWorkspaceReceiptOwner,
        _reservation: object,
        _receipt: object,
    ) -> None:
        intercepted.append("install")
        raise AssertionError("validator intercepted receipt installation")

    def mutate_public_receipt_names(_reader: object) -> None:
        monkeypatch.setattr(
            captured_module,
            "PublishedWorkspaceReceipt",
            intercept_receipt,
        )
        monkeypatch.setattr(
            PublishedWorkspaceReceiptOwner,
            "_install",
            intercept_install,
        )

    workspace.publish_into(
        receipt_owner,
        validate_published_destination=mutate_public_receipt_names,
    )

    assert intercepted == []
    assert receipt_owner.active
    assert type(receipt_owner.receipt) is receipt_type
    assert workspace_owner.owner_state(owner) == "receipted"
    receipt_owner.close()
    assert receipt_owner.closed


def test_native_workspace_close_aborts_before_receipt_publication(
    tmp_path: Path,
) -> None:
    plan = _file_plan()
    owner, _plan_value, publication_permit = _provision(tmp_path, plan=plan)
    workspace = OwnedWorkspaceAuthority()
    workspace.adopt_provisioned(
        destination=tmp_path / "published",
        stage_name=".stage",
        provisioned_owner=owner,
        publication_permit=publication_permit,
        plan=plan,
        destination_binding=None,
    )

    workspace.close()

    assert workspace.state == "closed"
    assert workspace_owner.owner_closed(owner)
    assert not (tmp_path / ".stage").exists()
    assert not (tmp_path / "published").exists()
    assert tuple(tmp_path.glob(".codenib-workspace-orphan-*"))


def test_native_post_rename_validation_failure_aborts_the_candidate(
    tmp_path: Path,
) -> None:
    plan = _file_plan()
    owner, _plan_value, publication_permit = _provision(tmp_path, plan=plan)
    workspace = OwnedWorkspaceAuthority()
    receipt_owner = PublishedWorkspaceReceiptOwner()
    workspace.adopt_provisioned(
        destination=tmp_path / "published",
        stage_name=".stage",
        provisioned_owner=owner,
        publication_permit=publication_permit,
        plan=plan,
        destination_binding=None,
    )
    workspace.write_file("views/bm25/documents.json", (b"{}",))
    workspace.seal()

    def reject(_reader: object) -> None:
        raise RuntimeError("injected post-rename validation failure")

    with pytest.raises(RuntimeError):
        workspace.publish_into(
            receipt_owner,
            validate_published_destination=reject,
        )

    assert receipt_owner.state == "cleanup"
    receipt_owner.close()
    assert receipt_owner.closed
    assert workspace_owner.owner_closed(owner)
    assert not (tmp_path / "published").exists()
    assert tuple(tmp_path.glob(".codenib-workspace-orphan-*"))


def test_staged_validator_cannot_mint_native_publication_authority(
    tmp_path: Path,
) -> None:
    plan = _file_plan()
    owner, _plan_value, publication_permit = _provision(tmp_path, plan=plan)
    workspace = OwnedWorkspaceAuthority()
    receipt_owner = PublishedWorkspaceReceiptOwner()
    workspace.adopt_provisioned(
        destination=tmp_path / "published",
        stage_name=".stage",
        provisioned_owner=owner,
        publication_permit=publication_permit,
        plan=plan,
        destination_binding=None,
    )
    workspace.write_file("views/bm25/documents.json", (b"{}",))
    workspace.seal()
    error = RuntimeError("injected hostile staged validator")

    def reject(_reader: object) -> None:
        with pytest.raises(RuntimeError):
            workspace_owner.claim_owner_publish_permit(owner)
        with pytest.raises(TypeError):
            workspace_owner.rename_owner_child_noreplace(
                owner,
                b".stage",
                b"published",
            )
        with pytest.raises(TypeError):
            workspace_owner.commit_owner_receipt(owner)
        assert workspace_owner.owner_state(owner) == "adopted"
        raise error

    with pytest.raises(RuntimeError) as caught:
        workspace.publish_into(
            receipt_owner,
            validate_staged_directory=reject,
        )

    assert caught.value is error
    assert receipt_owner.state == "cleanup"
    receipt_owner.close()
    assert receipt_owner.closed
    assert workspace_owner.owner_closed(owner)
    assert not (tmp_path / "published").exists()
    assert tuple(tmp_path.glob(".codenib-workspace-orphan-*"))


def test_published_validator_cannot_forge_native_receipt_commit(
    tmp_path: Path,
) -> None:
    plan = _file_plan()
    owner, _plan_value, publication_permit = _provision(tmp_path, plan=plan)
    workspace = OwnedWorkspaceAuthority()
    receipt_owner = PublishedWorkspaceReceiptOwner()
    workspace.adopt_provisioned(
        destination=tmp_path / "published",
        stage_name=".stage",
        provisioned_owner=owner,
        publication_permit=publication_permit,
        plan=plan,
        destination_binding=None,
    )
    workspace.write_file("views/bm25/documents.json", (b"{}",))
    workspace.seal()
    error = RuntimeError("injected hostile published validator")

    def reject(_reader: object) -> None:
        with pytest.raises(RuntimeError):
            workspace_owner.claim_owner_publish_permit(owner)
        with pytest.raises(TypeError):
            workspace_owner.rename_owner_child_noreplace(
                owner,
                b".stage",
                b"published",
            )
        with pytest.raises(TypeError):
            workspace_owner.commit_owner_receipt(owner)
        assert workspace_owner.owner_state(owner) == "published-unreceipted"
        raise error

    with pytest.raises(RuntimeError) as caught:
        workspace.publish_into(
            receipt_owner,
            validate_published_destination=reject,
        )

    assert caught.value is error
    assert receipt_owner.state == "cleanup"
    receipt_owner.close()
    assert receipt_owner.closed
    assert workspace_owner.owner_closed(owner)
    assert not (tmp_path / "published").exists()
    assert tuple(tmp_path.glob(".codenib-workspace-orphan-*"))


def test_native_owner_aborts_an_unreceipted_publication(
    tmp_path: Path,
) -> None:
    owner, _plan_value, publication_permit = _provision(tmp_path)
    workspace_owner.mark_owner_adopted(owner)
    receipt_token = workspace_owner.rename_owner_child_noreplace(
        publication_permit,
        b".stage",
        b"published",
    )
    assert receipt_token is not None

    assert workspace_owner.owner_state(owner) == "published-unreceipted"
    workspace_owner.abort_owner(owner)

    assert workspace_owner.owner_closed(owner)
    assert not (tmp_path / "published").exists()
    assert tuple(tmp_path.glob(".codenib-workspace-orphan-*"))


def test_native_owner_preserves_only_an_explicitly_receipted_publication(
    tmp_path: Path,
) -> None:
    owner, _plan_value, publication_permit = _provision(tmp_path)
    workspace_owner.mark_owner_adopted(owner)
    receipt_token = workspace_owner.rename_owner_child_noreplace(
        publication_permit,
        b".stage",
        b"published",
    )
    assert receipt_token is not None
    assert workspace_owner.commit_owner_receipt(receipt_token) is None
    assert workspace_owner.commit_owner_receipt(receipt_token) is None
    assert workspace_owner.owner_state(owner) == "receipted"

    workspace_owner.close_owner_exact(owner)

    assert workspace_owner.owner_closed(owner)
    assert (tmp_path / "published").is_dir()
    assert not tuple(tmp_path.glob(".codenib-workspace-orphan-*"))


def test_native_owner_deallocation_aborts_a_candidate_publication(
    tmp_path: Path,
) -> None:
    owner, _plan_value, publication_permit = _provision(tmp_path)
    workspace_owner.mark_owner_adopted(owner)
    workspace_owner.rename_owner_child_noreplace(
        publication_permit,
        b".stage",
        b"published",
    )
    del publication_permit
    del owner
    gc.collect()

    assert not (tmp_path / "published").exists()
    assert tuple(tmp_path.glob(".codenib-workspace-orphan-*"))


@pytest.mark.parametrize("interrupt_after_store", (False, True))
def test_native_receipt_slot_store_is_the_publication_authority_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt_after_store: bool,
) -> None:
    plan = _file_plan()
    owner, _plan_value, publication_permit = _provision(tmp_path, plan=plan)
    workspace = OwnedWorkspaceAuthority()
    receipt_owner = PublishedWorkspaceReceiptOwner()
    workspace.adopt_provisioned(
        destination=tmp_path / "published",
        stage_name=".stage",
        provisioned_owner=owner,
        publication_permit=publication_permit,
        plan=plan,
        destination_binding=None,
    )
    workspace.write_file("views/bm25/documents.json", (b"{}",))
    workspace.seal()
    error = KeyboardInterrupt("injected receipt installation interruption")
    original_install = PublishedWorkspaceReceiptOwner._install

    def interrupt_install(
        candidate: PublishedWorkspaceReceiptOwner,
        reservation: object,
        receipt: object,
    ) -> None:
        if interrupt_after_store:
            original_install(candidate, reservation, receipt)  # type: ignore[arg-type]
        raise error

    monkeypatch.setattr(PublishedWorkspaceReceiptOwner, "_install", interrupt_install)

    with pytest.raises(KeyboardInterrupt) as caught:
        workspace.publish_into(receipt_owner)

    assert caught.value is error
    if interrupt_after_store:
        assert receipt_owner.active
        assert workspace_owner.owner_state(owner) == "receipted"
        assert (tmp_path / "published/views/bm25/documents.json").is_file()
    else:
        assert receipt_owner.state == "cleanup"
        assert workspace_owner.owner_closed(owner)
        assert not (tmp_path / "published").exists()
        assert tuple(tmp_path.glob(".codenib-workspace-orphan-*"))
    receipt_owner.close()
    assert receipt_owner.closed


def test_native_receipt_commit_return_interruption_keeps_active_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _file_plan()
    owner, _plan_value, publication_permit = _provision(tmp_path, plan=plan)
    workspace = OwnedWorkspaceAuthority()
    receipt_owner = PublishedWorkspaceReceiptOwner()
    workspace.adopt_provisioned(
        destination=tmp_path / "published",
        stage_name=".stage",
        provisioned_owner=owner,
        publication_permit=publication_permit,
        plan=plan,
        destination_binding=None,
    )
    workspace.write_file("views/bm25/documents.json", (b"{}",))
    workspace.seal()
    error = KeyboardInterrupt("injected native receipt commit return interruption")
    original_commit = workspace_owner.commit_owner_receipt

    def commit_then_interrupt(candidate: object) -> None:
        original_commit(candidate)
        raise error

    monkeypatch.setattr(workspace_owner, "commit_owner_receipt", commit_then_interrupt)

    with pytest.raises(KeyboardInterrupt) as caught:
        workspace.publish_into(receipt_owner)

    assert caught.value is error
    assert receipt_owner.active
    assert workspace_owner.owner_state(owner) == "receipted"
    assert (tmp_path / "published/views/bm25/documents.json").is_file()
    receipt_owner.close()
    assert receipt_owner.closed


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
@pytest.mark.parametrize("receipted", (False, True))
def test_native_owner_child_close_does_not_change_parent_namespace(
    tmp_path: Path,
    receipted: bool,
) -> None:
    owner, _plan_value, publication_permit = _provision(tmp_path)
    expected_state = "provisioned"
    expected_path = tmp_path / ".stage"
    if receipted:
        workspace_owner.mark_owner_adopted(owner)
        receipt_token = workspace_owner.rename_owner_child_noreplace(
            publication_permit,
            b".stage",
            b"published",
        )
        assert receipt_token is not None
        workspace_owner.commit_owner_receipt(receipt_token)
        expected_state = "receipted"
        expected_path = tmp_path / "published"
    read_descriptor, write_descriptor = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        os.close(read_descriptor)
        try:
            try:
                workspace_owner.close_owner_exact(owner)
            except RuntimeError as error:
                descriptor_targets = []
                for name in os.listdir("/proc/self/fd"):
                    try:
                        target = os.readlink(f"/proc/self/fd/{name}")
                    except OSError:
                        continue
                    if str(tmp_path) in target:
                        descriptor_targets.append(target)
                report = repr((str(error), descriptor_targets))
                os.write(write_descriptor, report.encode("utf-8"))
        finally:
            os.close(write_descriptor)
            os._exit(0)

    os.close(write_descriptor)
    report = os.read(read_descriptor, 1 << 20).decode("utf-8")
    os.close(read_descriptor)
    waited_pid, status = os.waitpid(child_pid, 0)

    assert waited_pid == child_pid
    assert os.waitstatus_to_exitcode(status) == 0
    assert report == "('native workspace owner cannot cross a PID boundary', [])"
    assert workspace_owner.owner_state(owner) == expected_state
    assert expected_path.is_dir()
    if receipted:
        workspace_owner.close_owner_exact(owner)
        assert expected_path.is_dir()
    else:
        workspace_owner.abort_owner(owner)
        assert not expected_path.exists()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_native_owner_child_cleanup_does_not_close_reused_foreign_fds(
    tmp_path: Path,
) -> None:
    owner, _plan_value, _publication_permit = _provision(tmp_path)
    borrowed = (
        workspace_owner.borrow_owner_parent_descriptor(owner),
        workspace_owner.borrow_owner_root_descriptor(owner),
        workspace_owner.borrow_owner_directory_descriptor(owner, b"views"),
    )
    read_descriptor, write_descriptor = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        os.close(read_descriptor)
        try:
            foreign_source = os.open(
                "/dev/null", os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            )
            for descriptor in borrowed:
                os.close(descriptor)
                os.dup2(foreign_source, descriptor, inheritable=False)
            sentinels = borrowed
            with pytest.raises(RuntimeError, match="PID boundary"):
                workspace_owner.close_owner_exact(owner)
            for descriptor in sentinels:
                os.fstat(descriptor)
            owned_targets = []
            for name in os.listdir("/proc/self/fd"):
                try:
                    target = os.readlink(f"/proc/self/fd/{name}")
                except OSError:
                    continue
                if str(tmp_path) in target:
                    owned_targets.append(target)
            assert owned_targets == []
            os.write(write_descriptor, b"ok")
        except BaseException as error:  # noqa: B036 - report child failure
            os.write(write_descriptor, repr(error).encode("utf-8"))
        finally:
            os.close(write_descriptor)
            os._exit(0)

    os.close(write_descriptor)
    report = os.read(read_descriptor, 4096)
    os.close(read_descriptor)
    waited_pid, status = os.waitpid(child_pid, 0)
    assert waited_pid == child_pid
    assert os.waitstatus_to_exitcode(status) == 0
    assert report == b"ok"
    assert workspace_owner.owner_state(owner) == "provisioned"
    assert (tmp_path / ".stage").is_dir()
    workspace_owner.abort_owner(owner)
    assert workspace_owner.owner_closed(owner)


@pytest.mark.parametrize("cleanup", ("close", "abort"))
def test_native_owner_captures_existing_destination_without_mutation(
    tmp_path: Path,
    cleanup: str,
) -> None:
    root = tmp_path / "authority"
    destination = root / "nested" / "published"
    destination.mkdir(mode=0o750, parents=True)
    root.chmod(0o700)
    (destination / "payload.txt").write_bytes(b"retained")
    before = _tree_fingerprint(root)
    expected = destination.stat()
    expected_parent = destination.parent.stat()

    owner = _capture_existing_destination(root, b"nested/published")

    assert workspace_owner.owner_state(owner) == "destination-captured"
    assert not workspace_owner.owner_closed(owner)
    assert workspace_owner.require_exact_owner(owner) is owner
    assert workspace_owner.verify_owner_authority(owner) is None
    assert workspace_owner.verify_owner_destination_binding(owner) is None
    parent_descriptor = workspace_owner.borrow_owner_parent_descriptor(owner)
    assert workspace_owner.borrow_owner_parent_descriptor(owner) == parent_descriptor
    observed_parent = os.fstat(parent_descriptor)
    assert (observed_parent.st_dev, observed_parent.st_ino) == (
        expected_parent.st_dev,
        expected_parent.st_ino,
    )
    assert fcntl.fcntl(parent_descriptor, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
    descriptor = workspace_owner.borrow_owner_destination_descriptor(owner)
    assert workspace_owner.borrow_owner_destination_descriptor(owner) == descriptor
    observed = os.fstat(descriptor)
    assert stat.S_ISDIR(observed.st_mode)
    assert (observed.st_dev, observed.st_ino) == (
        expected.st_dev,
        expected.st_ino,
    )
    assert fcntl.fcntl(descriptor, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
    assert _tree_fingerprint(root) == before

    if cleanup == "close":
        assert workspace_owner.close_owner_exact(owner) is None
        assert workspace_owner.close_owner_exact(owner) is None
    else:
        assert workspace_owner.abort_owner(owner) is None
        assert workspace_owner.abort_owner(owner) is None
    assert workspace_owner.owner_closed(owner)
    assert _tree_fingerprint(root) == before


def test_native_destination_capture_return_interruption_preserves_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "authority"
    destination = root / "published"
    destination.mkdir(parents=True)
    root.chmod(0o700)
    (destination / "payload.txt").write_bytes(b"preserved")
    before = _tree_fingerprint(root)
    owner = _require_native_owner()
    interruption = KeyboardInterrupt("after native destination capture")

    def interrupt_after_capture(result: object, label: str) -> None:
        assert result is None
        assert label == "destination-capture"
        assert workspace_owner.owner_state(owner) == "destination-captured"
        raise interruption

    with monkeypatch.context() as context:
        context.setattr(
            workspace_owner,
            "_require_none_result",
            interrupt_after_capture,
        )
        with pytest.raises(KeyboardInterrupt) as captured:
            workspace_owner.capture_owner_destination(
                owner,
                os.fsencode(root),
                b"published",
                time.monotonic_ns() + 10_000_000_000,
            )

    assert captured.value is interruption
    assert workspace_owner.verify_owner_destination_binding(owner) is None
    assert _tree_fingerprint(root) == before
    workspace_owner.abort_owner(owner)
    assert workspace_owner.owner_closed(owner)
    assert _tree_fingerprint(root) == before


def test_native_destination_capture_symbols_require_the_exact_owner_type(
    tmp_path: Path,
) -> None:
    probe = _require_native_owner()
    workspace_owner.close_owner_exact(probe)
    candidate = object()

    with pytest.raises(TypeError, match="invalid native type"):
        workspace_owner.capture_owner_destination(
            candidate,
            os.fsencode(tmp_path),
            b"published",
            time.monotonic_ns() + 10_000_000_000,
        )
    with pytest.raises(TypeError, match="invalid native type"):
        workspace_owner.acquire_owner_replacement_lease(
            candidate,
            time.monotonic_ns() + 10_000_000_000,
        )
    with pytest.raises(TypeError, match="invalid native type"):
        workspace_owner.verify_owner_destination_binding(candidate)
    with pytest.raises(TypeError, match="invalid native type"):
        workspace_owner.borrow_owner_destination_descriptor(candidate)


def test_native_replacement_lease_requires_exact_deadline_and_captured_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    (root / "published").mkdir(parents=True)
    root.chmod(0o700)
    owner = _capture_existing_destination(root, b"published")

    with pytest.raises(RuntimeError, match="not claimable"):
        workspace_owner.claim_owner_replacement_permit(owner)
    with pytest.raises(RuntimeError, match="not replacement-ready"):
        workspace_owner.provision_owner_replacement(
            owner,
            b".replacement",
            b"0" * 64,
            0o700,
            (),
            time.monotonic_ns() + 10_000_000_000,
        )
    for deadline, error_type in (
        (True, TypeError),
        (1.0, TypeError),
        (0, ValueError),
        (-1, ValueError),
        (time.monotonic_ns() - 1, TimeoutError),
    ):
        with pytest.raises(error_type):
            workspace_owner.acquire_owner_replacement_lease(
                owner,
                deadline,  # type: ignore[arg-type]
            )
        assert workspace_owner.owner_state(owner) == "destination-captured"
        assert not (root / ".replacement").exists()

    workspace_owner.acquire_owner_replacement_lease(
        owner,
        time.monotonic_ns() + 10_000_000_000,
    )
    assert workspace_owner.owner_state(owner) == "destination-leased"
    assert workspace_owner.verify_owner_destination_binding(owner) is None
    assert (
        workspace_owner.acquire_owner_replacement_lease(
            owner,
            time.monotonic_ns() + 10_000_000_000,
        )
        is None
    )
    permit = workspace_owner.claim_owner_replacement_permit(owner)
    with pytest.raises(RuntimeError, match="not replacement-lease-ready"):
        workspace_owner.acquire_owner_replacement_lease(
            owner,
            time.monotonic_ns() + 10_000_000_000,
        )
    del permit
    workspace_owner.abort_owner(owner)
    assert workspace_owner.owner_closed(owner)


def test_native_replacement_lease_return_interruption_retains_settlement(
    tmp_path: Path,
) -> None:
    library = _compile_exchange_fault_shim(tmp_path)
    root = tmp_path / "authority"
    script = textwrap.dedent(
        """
        import ctypes
        import errno
        import fcntl
        import os
        import sys
        import time
        from pathlib import Path

        import codenib._workspace_owner as workspace_owner

        root = Path(sys.argv[1])
        faults = ctypes.CDLL(sys.argv[2])
        faults.codenib_exchange_fault_exclusive_lock_calls.restype = ctypes.c_int

        def assert_parent_lease_held():
            child = os.fork()
            if child == 0:
                os.environ.pop("CODENIB_EXCHANGE_FAULT", None)
                descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    os.close(descriptor)
                    os._exit(
                        0 if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK) else 2
                    )
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
                os._exit(3)
            waited, status = os.waitpid(child, 0)
            assert waited == child
            assert os.waitstatus_to_exitcode(status) == 0

        destination = root / "published"
        destination.mkdir(parents=True)
        root.chmod(0o700)
        owner = workspace_owner.create_owner()
        workspace_owner.capture_owner_destination(
            owner,
            os.fsencode(root),
            b"published",
            time.monotonic_ns() + 10_000_000_000,
        )
        interruption = KeyboardInterrupt("after native replacement lease")

        def interrupt_after_lease(result, label):
            assert result is None
            assert label == "replacement-lease"
            assert workspace_owner.owner_state(owner) == "destination-leased"
            raise interruption

        original = workspace_owner._require_none_result
        workspace_owner._require_none_result = interrupt_after_lease
        try:
            try:
                workspace_owner.acquire_owner_replacement_lease(
                    owner,
                    time.monotonic_ns() + 10_000_000_000,
                )
            except KeyboardInterrupt as error:
                assert error is interruption
            else:
                raise AssertionError("replacement lease interruption was hidden")
        finally:
            workspace_owner._require_none_result = original

        assert workspace_owner.owner_state(owner) == "destination-leased"
        assert faults.codenib_exchange_fault_exclusive_lock_calls() == 1
        assert_parent_lease_held()

        assert workspace_owner.acquire_owner_replacement_lease(
            owner,
            time.monotonic_ns() + 10_000_000_000,
        ) is None
        assert faults.codenib_exchange_fault_exclusive_lock_calls() == 1

        try:
            workspace_owner.acquire_owner_replacement_lease(
                owner,
                time.monotonic_ns() - 1,
            )
        except TimeoutError:
            pass
        else:
            raise AssertionError("expired replacement lease retry succeeded")
        assert workspace_owner.owner_state(owner) == "destination-leased"
        assert faults.codenib_exchange_fault_exclusive_lock_calls() == 1
        assert_parent_lease_held()

        moved = root / ".stale"
        destination.rename(moved)
        try:
            workspace_owner.acquire_owner_replacement_lease(
                owner,
                time.monotonic_ns() + 10_000_000_000,
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("stale replacement lease retry succeeded")
        assert workspace_owner.owner_state(owner) == "destination-leased"
        assert faults.codenib_exchange_fault_exclusive_lock_calls() == 1
        assert_parent_lease_held()
        assert not (root / ".replacement").exists()

        moved.rename(destination)
        workspace_owner.abort_owner(owner)
        assert workspace_owner.owner_closed(owner)
        os.environ.pop("CODENIB_EXCHANGE_FAULT", None)
        independent_parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        fcntl.flock(independent_parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(independent_parent, fcntl.LOCK_UN)
        os.close(independent_parent)
        """
    )
    _run_exchange_fault_script(root, library, "second-lock-fail", script)


def test_native_replacement_lease_contention_is_clean_before_candidate_mutation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    (root / "first").mkdir(parents=True)
    (root / "second").mkdir()
    root.chmod(0o700)
    first_owner = _capture_existing_destination(root, b"first")
    second_owner = _capture_existing_destination(root, b"second")

    workspace_owner.acquire_owner_replacement_lease(
        first_owner,
        time.monotonic_ns() + 10_000_000_000,
    )
    with pytest.raises(BlockingIOError):
        workspace_owner.acquire_owner_replacement_lease(
            second_owner,
            time.monotonic_ns() + 10_000_000_000,
        )

    assert workspace_owner.owner_state(first_owner) == "destination-leased"
    assert workspace_owner.owner_state(second_owner) == "destination-captured"
    assert not (root / ".first-replacement").exists()
    assert not (root / ".second-replacement").exists()
    workspace_owner.abort_owner(second_owner)
    workspace_owner.abort_owner(first_owner)
    assert workspace_owner.owner_closed(first_owner)
    assert workspace_owner.owner_closed(second_owner)


def test_native_borrowed_parent_unlock_cannot_release_replacement_lease(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    (root / "published").mkdir(parents=True)
    root.chmod(0o700)
    gc.collect()
    baseline_fd_count = len(os.listdir("/proc/self/fd"))
    owner = _capture_existing_destination(root, b"published")
    borrowed_parent = workspace_owner.borrow_owner_parent_descriptor(owner)
    workspace_owner.acquire_owner_replacement_lease(
        owner,
        time.monotonic_ns() + 10_000_000_000,
    )
    contender = os.open(root, os.O_RDONLY | os.O_DIRECTORY)

    fcntl.flock(borrowed_parent, fcntl.LOCK_UN)
    assert workspace_owner.owner_state(owner) == "destination-leased"
    assert workspace_owner.verify_owner_destination_binding(owner) is None
    with pytest.raises(BlockingIOError):
        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)

    workspace_owner.abort_owner(owner)
    assert workspace_owner.owner_closed(owner)
    assert len(os.listdir("/proc/self/fd")) == baseline_fd_count + 1
    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    fcntl.flock(contender, fcntl.LOCK_UN)
    os.close(contender)
    del owner
    gc.collect()
    assert len(os.listdir("/proc/self/fd")) == baseline_fd_count


def test_native_replacement_lease_rejects_reused_borrowed_parent_before_flock(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    (root / "published").mkdir(parents=True)
    root.chmod(0o700)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    owner = _capture_existing_destination(root, b"published")
    borrowed_parent = workspace_owner.borrow_owner_parent_descriptor(owner)
    os.close(borrowed_parent)
    foreign_source = os.open(
        foreign,
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    if foreign_source != borrowed_parent:
        os.dup2(foreign_source, borrowed_parent, inheritable=False)
        os.close(foreign_source)
    fcntl.flock(borrowed_parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
    independent_foreign = os.open(foreign, os.O_RDONLY | os.O_DIRECTORY)
    independent_parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(RuntimeError, match="not replacement-lease-ready"):
            workspace_owner.acquire_owner_replacement_lease(
                owner,
                time.monotonic_ns() + 10_000_000_000,
            )
        assert workspace_owner.owner_state(owner) == "destination-captured"
        assert not (root / ".replacement").exists()
        with pytest.raises(BlockingIOError):
            fcntl.flock(
                independent_foreign,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        fcntl.flock(independent_parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(independent_parent, fcntl.LOCK_UN)

        with pytest.raises(OSError):
            workspace_owner.abort_owner(owner)
        assert workspace_owner.owner_closed(owner)
        os.fstat(borrowed_parent)
        with pytest.raises(BlockingIOError):
            fcntl.flock(
                independent_foreign,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
    finally:
        fcntl.flock(borrowed_parent, fcntl.LOCK_UN)
        os.close(borrowed_parent)
        os.close(independent_foreign)
        os.close(independent_parent)


@pytest.mark.parametrize("descriptor_kind", ("parent", "destination"))
def test_native_replacement_lease_rejects_reused_borrowed_fd_before_flock(
    tmp_path: Path,
    descriptor_kind: str,
) -> None:
    library = _compile_exchange_fault_shim(tmp_path)
    root = tmp_path / "authority"
    script = textwrap.dedent(
        f"""
        import ctypes
        import os
        import sys
        import time
        from pathlib import Path

        import codenib._workspace_owner as workspace_owner

        root = Path(sys.argv[1])
        faults = ctypes.CDLL(sys.argv[2])
        faults.codenib_exchange_fault_exclusive_lock_calls.restype = ctypes.c_int
        destination = root / "published"
        destination.mkdir(parents=True)
        root.chmod(0o700)
        descriptor_kind = {descriptor_kind!r}
        foreign = root.parent / ("foreign-" + descriptor_kind)
        foreign.mkdir()

        owner = workspace_owner.create_owner()
        workspace_owner.capture_owner_destination(
            owner,
            os.fsencode(root),
            b"published",
            time.monotonic_ns() + 10_000_000_000,
        )
        borrower = (
            workspace_owner.borrow_owner_parent_descriptor
            if descriptor_kind == "parent"
            else workspace_owner.borrow_owner_destination_descriptor
        )
        borrowed = borrower(owner)
        os.close(borrowed)
        foreign_source = os.open(
            foreign,
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        if foreign_source != borrowed:
            os.dup2(foreign_source, borrowed, inheritable=False)
            os.close(foreign_source)

        try:
            workspace_owner.acquire_owner_replacement_lease(
                owner,
                time.monotonic_ns() + 10_000_000_000,
            )
        except RuntimeError as error:
            assert "not replacement-lease-ready" in str(error)
        else:
            raise AssertionError("reused captured descriptor was accepted")
        assert faults.codenib_exchange_fault_exclusive_lock_calls() == 0
        assert workspace_owner.owner_state(owner) == "destination-captured"
        assert not (root / ".replacement").exists()

        try:
            workspace_owner.abort_owner(owner)
        except OSError:
            pass
        else:
            raise AssertionError("reused captured descriptor cleanup was accepted")
        assert workspace_owner.owner_closed(owner)
        assert faults.codenib_exchange_fault_exclusive_lock_calls() == 0
        assert os.fstat(borrowed).st_ino == foreign.stat().st_ino
        os.close(borrowed)
        """
    )
    _run_exchange_fault_script(root, library, "second-lock-fail", script)


def test_native_no_candidate_settlement_is_retryable_after_close_auth_failure(
    tmp_path: Path,
) -> None:
    library = _compile_exchange_fault_shim(tmp_path)
    root = tmp_path / "authority"
    script = textwrap.dedent(
        """
        import ctypes
        import errno
        import fcntl
        import os
        import sys
        import time
        from pathlib import Path

        import codenib._workspace_owner as workspace_owner

        root = Path(sys.argv[1])
        faults = ctypes.CDLL(sys.argv[2])
        faults.codenib_exchange_fault_close_kcmp_failed.restype = ctypes.c_int
        faults.codenib_exchange_fault_close_fcntl_failed.restype = ctypes.c_int

        destination = root / "published"
        destination.mkdir(parents=True)
        root.chmod(0o700)
        foreign = root.parent / "foreign-lock"
        foreign.mkdir()
        foreign_holder = os.open(foreign, os.O_RDONLY | os.O_DIRECTORY)
        foreign_contender = os.open(foreign, os.O_RDONLY | os.O_DIRECTORY)
        fcntl.flock(foreign_holder, fcntl.LOCK_EX | fcntl.LOCK_NB)

        owner = workspace_owner.create_owner()
        workspace_owner.capture_owner_destination(
            owner,
            os.fsencode(root),
            b"published",
            time.monotonic_ns() + 10_000_000_000,
        )
        workspace_owner.acquire_owner_replacement_lease(
            owner,
            time.monotonic_ns() + 10_000_000_000,
        )
        permit = workspace_owner.claim_owner_replacement_permit(owner)
        try:
            workspace_owner.provision_owner_replacement(
                owner,
                b"not-hidden",
                b"0" * 64,
                0o700,
                (),
                time.monotonic_ns() + 10_000_000_000,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("invalid replacement slot was accepted")
        assert workspace_owner.owner_state(owner) == "destination-leased"
        assert not (root / ".replacement").exists()

        borrowed_parent = workspace_owner.borrow_owner_parent_descriptor(owner)
        assert os.fstat(borrowed_parent).st_ino == root.stat().st_ino
        independent_parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            workspace_owner.abort_owner(owner)
        except OSError as error:
            assert error.errno == errno.EIO
        else:
            raise AssertionError("one-shot close authentication error was hidden")
        assert faults.codenib_exchange_fault_close_kcmp_failed() == 1
        assert faults.codenib_exchange_fault_close_fcntl_failed() == 1
        assert workspace_owner.owner_state(owner) == "destination-captured"
        assert not workspace_owner.owner_closed(owner)
        assert not (root / ".replacement").exists()
        os.fstat(borrowed_parent)

        fcntl.flock(independent_parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(independent_parent, fcntl.LOCK_UN)
        try:
            fcntl.flock(foreign_contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise AssertionError("unrelated foreign lock was released")

        workspace_owner.close_owner_exact(owner)
        assert workspace_owner.owner_closed(owner)
        try:
            workspace_owner.exchange_owner_replacement(
                permit,
                b".replacement",
                b"published",
                time.monotonic_ns() + 10_000_000_000,
            )
        except RuntimeError as error:
            assert "unavailable" in str(error)
        else:
            raise AssertionError("stale replacement permit remained usable")

        os.close(independent_parent)
        fcntl.flock(foreign_holder, fcntl.LOCK_UN)
        os.close(foreign_contender)
        os.close(foreign_holder)
        retained_targets = []
        for name in os.listdir("/proc/self/fd"):
            try:
                target = os.readlink(f"/proc/self/fd/{name}")
            except OSError:
                continue
            if os.fspath(root) in target:
                retained_targets.append(target)
        assert retained_targets == []
        """
    )
    _run_exchange_fault_script(root, library, "close-auth-error-once", script)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux")
def test_native_failed_replacement_mkdir_settles_without_lock_or_fd_leak(
    tmp_path: Path,
) -> None:
    library = _compile_exchange_fault_shim(tmp_path)
    root = tmp_path / "authority"
    destination = root / "published"
    destination.mkdir(parents=True)
    root.chmod(0o700)
    (destination / "incumbent.txt").write_bytes(b"incumbent")
    script = textwrap.dedent(
        """
        import ctypes
        import errno
        import fcntl
        import gc
        import os
        import sys
        import time
        from pathlib import Path

        import codenib._workspace_owner as workspace_owner

        root = Path(sys.argv[1])
        faults = ctypes.CDLL(sys.argv[2])
        counter = faults.codenib_exchange_fault_replacement_mkdir_calls
        counter.restype = ctypes.c_int
        gc.collect()
        baseline_fd_count = len(os.listdir("/proc/self/fd"))

        for iteration in range(6):
            owner = workspace_owner.create_owner()
            workspace_owner.capture_owner_destination(
                owner,
                os.fsencode(root),
                b"published",
                time.monotonic_ns() + 10_000_000_000,
            )
            workspace_owner.acquire_owner_replacement_lease(
                owner,
                time.monotonic_ns() + 10_000_000_000,
            )
            permit = workspace_owner.claim_owner_replacement_permit(owner)
            try:
                workspace_owner.provision_owner_replacement(
                    owner,
                    b".replacement",
                    b"0" * 64,
                    0o700,
                    (),
                    time.monotonic_ns() + 10_000_000_000,
                )
            except OSError as error:
                assert error.errno == errno.EIO
            else:
                raise AssertionError("pre-creation mkdir failure was hidden")

            assert workspace_owner.owner_state(owner) == "replacement-provisioning"
            assert not (root / ".replacement").exists()
            contender = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise AssertionError("failed provision released its lease early")

            if iteration % 2 == 0:
                workspace_owner.abort_owner(owner)
                assert workspace_owner.owner_closed(owner)
                try:
                    workspace_owner.exchange_owner_replacement(
                        permit,
                        b".replacement",
                        b"published",
                        time.monotonic_ns() + 10_000_000_000,
                    )
                except RuntimeError as error:
                    assert "unavailable" in str(error)
                else:
                    raise AssertionError("settled replacement permit stayed active")
                del permit
                del owner
                gc.collect()
            else:
                sentinel = KeyboardInterrupt(f"sentinel-{iteration}")
                try:
                    raise sentinel
                except BaseException:  # noqa: B036 - preserve active exception
                    del permit
                    del owner
                    gc.collect()
                    assert sys.exc_info()[1] is sentinel

            assert len(os.listdir("/proc/self/fd")) == baseline_fd_count + 1
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(contender, fcntl.LOCK_UN)
            os.close(contender)
            assert len(os.listdir("/proc/self/fd")) == baseline_fd_count
            assert not (root / ".replacement").exists()

        assert counter() == 6
        retained_targets = []
        for name in os.listdir("/proc/self/fd"):
            try:
                target = os.readlink(f"/proc/self/fd/{name}")
            except OSError:
                continue
            if os.fspath(root) in target:
                retained_targets.append(target)
        assert retained_targets == []
        assert (root / "published" / "incumbent.txt").read_bytes() == b"incumbent"
        """
    )
    _run_exchange_fault_script(root, library, "mkdir-fail-before-create", script)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux")
def test_native_apparent_replacement_mkdir_keeps_recovery_lease_and_config(
    tmp_path: Path,
) -> None:
    library = _compile_exchange_fault_shim(tmp_path)
    root = tmp_path / "authority"
    destination = root / "published"
    destination.mkdir(parents=True)
    root.chmod(0o700)
    (destination / "incumbent.txt").write_bytes(b"incumbent")
    script = textwrap.dedent(
        """
        import ctypes
        import errno
        import fcntl
        import gc
        import os
        import sys
        import time
        from pathlib import Path

        import codenib._workspace_owner as workspace_owner

        root = Path(sys.argv[1])
        faults = ctypes.CDLL(sys.argv[2])
        counter = faults.codenib_exchange_fault_replacement_mkdir_calls
        counter.restype = ctypes.c_int
        gc.collect()
        baseline_fd_count = len(os.listdir("/proc/self/fd"))
        owner = workspace_owner.create_owner()
        workspace_owner.capture_owner_destination(
            owner,
            os.fsencode(root),
            b"published",
            time.monotonic_ns() + 10_000_000_000,
        )
        workspace_owner.acquire_owner_replacement_lease(
            owner,
            time.monotonic_ns() + 10_000_000_000,
        )
        permit = workspace_owner.claim_owner_replacement_permit(owner)
        try:
            workspace_owner.provision_owner_replacement(
                owner,
                b".replacement",
                b"0" * 64,
                0o700,
                (),
                time.monotonic_ns() + 10_000_000_000,
            )
        except OSError as error:
            assert error.errno == errno.EIO
        else:
            raise AssertionError("post-create mkdir error was hidden")

        slot = root / ".replacement"
        assert counter() == 1
        assert slot.is_dir()
        assert workspace_owner.owner_state(owner) == "replacement-provisioning"
        contender = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            workspace_owner.abort_owner(owner)
        except OSError as error:
            assert error.errno == errno.ESTALE
        else:
            raise AssertionError("ambiguous created slot was settled")
        assert workspace_owner.owner_state(owner) == (
            "replacement-recovery-required"
        )
        assert not workspace_owner.owner_closed(owner)
        assert slot.is_dir()
        try:
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise AssertionError("ambiguous slot released the recovery lease")
        try:
            workspace_owner.exchange_owner_replacement(
                permit,
                b".replacement",
                b"published",
                time.monotonic_ns() + 10_000_000_000,
            )
        except RuntimeError as error:
            assert "not exchange-ready" in str(error)
        else:
            raise AssertionError("ambiguous replacement became exchange-ready")

        slot.rmdir()
        workspace_owner.abort_owner(owner)
        assert workspace_owner.owner_closed(owner)
        assert len(os.listdir("/proc/self/fd")) == baseline_fd_count + 1
        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(contender, fcntl.LOCK_UN)
        os.close(contender)
        try:
            workspace_owner.exchange_owner_replacement(
                permit,
                b".replacement",
                b"published",
                time.monotonic_ns() + 10_000_000_000,
            )
        except RuntimeError as error:
            assert "unavailable" in str(error)
        else:
            raise AssertionError("recovered replacement permit stayed active")
        del permit
        del owner
        gc.collect()
        assert len(os.listdir("/proc/self/fd")) == baseline_fd_count
        assert not slot.exists()
        assert (root / "published" / "incumbent.txt").read_bytes() == b"incumbent"
        """
    )
    _run_exchange_fault_script(root, library, "mkdir-error-after-create", script)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux")
def test_native_replacement_slot_stat_ambiguity_retains_recovery_lease(
    tmp_path: Path,
) -> None:
    library = _compile_exchange_fault_shim(tmp_path)
    root = tmp_path / "authority"
    destination = root / "published"
    destination.mkdir(parents=True)
    root.chmod(0o700)
    (destination / "incumbent.txt").write_bytes(b"incumbent")
    script = textwrap.dedent(
        """
        import ctypes
        import errno
        import fcntl
        import gc
        import os
        import sys
        import time
        from pathlib import Path

        import codenib._workspace_owner as workspace_owner

        root = Path(sys.argv[1])
        faults = ctypes.CDLL(sys.argv[2])
        mkdir_calls = faults.codenib_exchange_fault_replacement_mkdir_calls
        mkdir_calls.restype = ctypes.c_int
        slot_stats = faults.codenib_exchange_fault_slot_stats
        slot_stats.restype = ctypes.c_int
        stat_failures = faults.codenib_exchange_fault_settlement_slot_stat_failures
        stat_failures.restype = ctypes.c_int
        gc.collect()
        baseline_fd_count = len(os.listdir("/proc/self/fd"))
        owner = workspace_owner.create_owner()
        workspace_owner.capture_owner_destination(
            owner,
            os.fsencode(root),
            b"published",
            time.monotonic_ns() + 10_000_000_000,
        )
        workspace_owner.acquire_owner_replacement_lease(
            owner,
            time.monotonic_ns() + 10_000_000_000,
        )
        permit = workspace_owner.claim_owner_replacement_permit(owner)
        try:
            workspace_owner.provision_owner_replacement(
                owner,
                b".replacement",
                b"0" * 64,
                0o700,
                (),
                time.monotonic_ns() + 10_000_000_000,
            )
        except OSError as error:
            assert error.errno == errno.EIO
        else:
            raise AssertionError("pre-creation mkdir failure was hidden")

        assert mkdir_calls() == 1
        assert slot_stats() == 1
        assert not (root / ".replacement").exists()
        contender = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            workspace_owner.abort_owner(owner)
        except OSError as error:
            assert error.errno == errno.ESTALE
        else:
            raise AssertionError("ambiguous slot stat was treated as ENOENT")
        assert slot_stats() == 2
        assert stat_failures() == 1
        assert workspace_owner.owner_state(owner) == (
            "replacement-recovery-required"
        )
        assert not workspace_owner.owner_closed(owner)
        try:
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise AssertionError("ambiguous slot stat released the recovery lease")

        workspace_owner.abort_owner(owner)
        assert workspace_owner.owner_closed(owner)
        assert slot_stats() >= 4
        assert stat_failures() == 1
        assert len(os.listdir("/proc/self/fd")) == baseline_fd_count + 1
        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(contender, fcntl.LOCK_UN)
        os.close(contender)
        try:
            workspace_owner.exchange_owner_replacement(
                permit,
                b".replacement",
                b"published",
                time.monotonic_ns() + 10_000_000_000,
            )
        except RuntimeError as error:
            assert "unavailable" in str(error)
        else:
            raise AssertionError("settled replacement permit stayed active")
        del permit
        del owner
        gc.collect()
        assert len(os.listdir("/proc/self/fd")) == baseline_fd_count
        assert not (root / ".replacement").exists()
        assert (root / "published" / "incumbent.txt").read_bytes() == b"incumbent"
        """
    )
    _run_exchange_fault_script(root, library, "mkdir-fail-stat-ambiguous", script)


def test_native_captured_destination_rejects_every_legacy_owner_operation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    destination = root / "published"
    destination.mkdir(parents=True)
    root.chmod(0o700)
    (destination / "payload.txt").write_bytes(b"immutable")
    before = _tree_fingerprint(root)
    owner = _capture_existing_destination(root, b"published")

    rejected = (
        lambda: workspace_owner.claim_owner_publish_permit(owner),
        lambda: workspace_owner.provision_owner(
            owner,
            os.fsencode(root),
            b"other",
            b".stage",
            b"0" * 64,
            0o700,
            (),
            time.monotonic_ns() + 10_000_000_000,
        ),
        lambda: workspace_owner.capture_owner_destination(
            owner,
            os.fsencode(root),
            b"published",
            time.monotonic_ns() + 10_000_000_000,
        ),
        lambda: workspace_owner.verify_owner_adoption_binding(
            owner,
            os.fsencode(destination),
            b".stage",
            b"0" * 64,
        ),
        lambda: workspace_owner.borrow_owner_root_descriptor(owner),
        lambda: workspace_owner.borrow_owner_directory_descriptor(owner, b"views"),
        lambda: workspace_owner.begin_owner_file(owner, b"", b"new", 0o600),
        lambda: workspace_owner.write_owner_file(owner, b"new"),
        lambda: workspace_owner.finish_owner_file(owner, 0o600),
        lambda: workspace_owner.abort_owner_file(owner),
        lambda: workspace_owner.seal_owner_directories(owner),
        lambda: workspace_owner.sync_owner_parent(owner),
        lambda: workspace_owner.mark_owner_adopted(owner),
        lambda: workspace_owner.rename_owner_child_noreplace(
            owner,
            b"published",
            b"other",
        ),
        lambda: workspace_owner.commit_owner_receipt(owner),
        lambda: workspace_owner.quarantine_owner(owner),
    )
    for operation in rejected:
        with pytest.raises((RuntimeError, TypeError)):
            operation()
        assert workspace_owner.owner_state(owner) == "destination-captured"
        assert workspace_owner.verify_owner_destination_binding(owner) is None
        assert _tree_fingerprint(root) == before

    workspace_owner.abort_owner(owner)
    assert workspace_owner.owner_closed(owner)
    assert _tree_fingerprint(root) == before


def test_native_destination_capture_requires_exact_bounded_arguments(
    tmp_path: Path,
) -> None:
    class BytesSubclass(bytes):
        pass

    root = tmp_path / "authority"
    (root / "published").mkdir(parents=True)
    root.chmod(0o700)
    valid_root = os.fsencode(root)
    valid_destination = b"published"
    valid_deadline = time.monotonic_ns() + 10_000_000_000
    invalid_arguments = (
        (os.fspath(root), valid_destination, valid_deadline, TypeError),
        (BytesSubclass(valid_root), valid_destination, valid_deadline, TypeError),
        (valid_root, "published", valid_deadline, TypeError),
        (valid_root, BytesSubclass(valid_destination), valid_deadline, TypeError),
        (valid_root, valid_destination, True, TypeError),
        (valid_root, valid_destination, 1.0, TypeError),
        (valid_root, valid_destination, 0, ValueError),
        (valid_root, valid_destination, -1, ValueError),
        (b"relative", valid_destination, valid_deadline, ValueError),
        (valid_root, b"", valid_deadline, ValueError),
        (valid_root, b"/published", valid_deadline, ValueError),
        (valid_root, b"./published", valid_deadline, ValueError),
        (valid_root, b"nested/../published", valid_deadline, ValueError),
        (valid_root, b"published/", valid_deadline, ValueError),
        (valid_root, b"published\x00other", valid_deadline, ValueError),
    )

    for allowed_root, destination, deadline, error_type in invalid_arguments:
        owner = _require_native_owner()
        with pytest.raises(error_type):
            workspace_owner.capture_owner_destination(
                owner,
                allowed_root,  # type: ignore[arg-type]
                destination,  # type: ignore[arg-type]
                deadline,  # type: ignore[arg-type]
            )
        assert workspace_owner.owner_state(owner) == "empty"
        workspace_owner.close_owner_exact(owner)
        assert workspace_owner.owner_closed(owner)


def test_native_expired_capture_deadline_is_retryable_before_acquisition(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    (root / "published").mkdir(parents=True)
    root.chmod(0o700)
    owner = _require_native_owner()

    with pytest.raises(TimeoutError):
        workspace_owner.capture_owner_destination(
            owner,
            os.fsencode(root),
            b"published",
            time.monotonic_ns() - 1,
        )

    assert workspace_owner.owner_state(owner) == "empty"
    assert (
        workspace_owner.capture_owner_destination(
            owner,
            os.fsencode(root),
            b"published",
            time.monotonic_ns() + 10_000_000_000,
        )
        is None
    )
    workspace_owner.abort_owner(owner)
    assert (root / "published").is_dir()


@pytest.mark.parametrize(
    "destination_kind",
    ("missing", "file", "symlink", "symlink-parent"),
)
def test_native_capture_rejects_non_directory_or_unpinned_destinations_and_poison_closes(
    tmp_path: Path,
    destination_kind: str,
) -> None:
    root = tmp_path / "authority"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    relative = b"published"
    if destination_kind == "file":
        (root / "published").write_bytes(b"not-a-directory")
    elif destination_kind == "symlink":
        (root / "real").mkdir()
        (root / "published").symlink_to("real", target_is_directory=True)
    elif destination_kind == "symlink-parent":
        (root / "real-parent" / "published").mkdir(parents=True)
        (root / "alias").symlink_to("real-parent", target_is_directory=True)
        relative = b"alias/published"
    before = _tree_fingerprint(root)
    owner = _require_native_owner()

    with pytest.raises((OSError, ValueError)):
        workspace_owner.capture_owner_destination(
            owner,
            os.fsencode(root),
            relative,
            time.monotonic_ns() + 10_000_000_000,
        )

    assert workspace_owner.owner_state(owner) == "empty"
    for operation in (
        lambda: workspace_owner.claim_owner_publish_permit(owner),
        lambda: workspace_owner.capture_owner_destination(
            owner,
            os.fsencode(root),
            relative,
            time.monotonic_ns() + 10_000_000_000,
        ),
        lambda: workspace_owner.borrow_owner_parent_descriptor(owner),
        lambda: workspace_owner.abort_owner_file(owner),
        lambda: workspace_owner.sync_owner_parent(owner),
        lambda: workspace_owner.quarantine_owner(owner),
    ):
        with pytest.raises(RuntimeError):
            operation()
    assert _tree_fingerprint(root) == before
    workspace_owner.abort_owner(owner)
    assert workspace_owner.owner_closed(owner)
    assert _tree_fingerprint(root) == before


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux")
def test_native_capture_rejects_destination_exchanged_between_stat_and_open(
    tmp_path: Path,
) -> None:
    probe = _require_native_owner()
    workspace_owner.close_owner_exact(probe)
    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("a C compiler is required for the deterministic race shim")
    source = tmp_path / "capture_race.c"
    library = tmp_path / "capture_race.so"
    source.write_text(
        textwrap.dedent(
            r"""
            #define _GNU_SOURCE
            #include <dlfcn.h>
            #include <errno.h>
            #include <fcntl.h>
            #include <stdarg.h>
            #include <stdlib.h>
            #include <string.h>
            #include <sys/syscall.h>
            #include <sys/types.h>
            #include <unistd.h>

            #ifndef RENAME_EXCHANGE
            #define RENAME_EXCHANGE (1U << 1)
            #endif

            typedef int (*openat64_function)(int, const char *, int, ...);

            int openat64(int parent, const char *name, int flags, ...) {
              static openat64_function real_openat64 = NULL;
              static int injected = 0;
              mode_t mode = 0;
              va_list arguments;

              if (real_openat64 == NULL) {
                real_openat64 =
                    (openat64_function)dlsym(RTLD_NEXT, "openat64");
                if (real_openat64 == NULL) {
                  errno = ENOSYS;
                  return -1;
                }
              }
              if (!injected && name != NULL &&
                  strcmp(name, "published") == 0 &&
                  (flags & O_DIRECTORY) != 0 &&
                  getenv("CODENIB_CAPTURE_RACE") != NULL) {
                injected = 1;
                if (syscall(SYS_renameat2, parent, "published", parent,
                            "alternate", RENAME_EXCHANGE) != 0) {
                  return -1;
                }
              }
              if ((flags & O_CREAT) != 0) {
                va_start(arguments, flags);
                mode = va_arg(arguments, mode_t);
                va_end(arguments);
                return real_openat64(parent, name, flags, mode);
              }
              return real_openat64(parent, name, flags);
            }
            """
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [compiler, "-shared", "-fPIC", "-O2", "-Wall", "-o", library, source, "-ldl"],
        check=True,
        capture_output=True,
        text=True,
    )
    root = tmp_path / "authority"
    published = root / "published"
    alternate = root / "alternate"
    published.mkdir(parents=True)
    alternate.mkdir()
    root.chmod(0o700)
    (published / "identity.txt").write_bytes(b"published-before-race")
    (alternate / "identity.txt").write_bytes(b"alternate-before-race")
    script = textwrap.dedent(
        """
        import os
        import sys
        import time
        from pathlib import Path

        import codenib._workspace_owner as workspace_owner

        root = Path(sys.argv[1])
        owner = workspace_owner.create_owner()
        try:
            workspace_owner.capture_owner_destination(
                owner,
                os.fsencode(root),
                b"published",
                time.monotonic_ns() + 10_000_000_000,
            )
        except OSError:
            pass
        else:
            raise AssertionError("capture accepted a stat/open destination exchange")
        assert workspace_owner.owner_state(owner) == "empty"
        assert (root / "published" / "identity.txt").read_bytes() == (
            b"alternate-before-race"
        )
        assert (root / "alternate" / "identity.txt").read_bytes() == (
            b"published-before-race"
        )
        after_failure = tuple(
            sorted(
                (path.relative_to(root).as_posix(), path.read_bytes())
                for path in root.glob("*/identity.txt")
            )
        )
        for operation in (
            lambda: workspace_owner.borrow_owner_parent_descriptor(owner),
            lambda: workspace_owner.verify_owner_destination_binding(owner),
            lambda: workspace_owner.quarantine_owner(owner),
        ):
            try:
                operation()
            except RuntimeError:
                pass
            else:
                raise AssertionError("poisoned capture owner remained usable")
        workspace_owner.abort_owner(owner)
        assert workspace_owner.owner_closed(owner)
        after_cleanup = tuple(
            sorted(
                (path.relative_to(root).as_posix(), path.read_bytes())
                for path in root.glob("*/identity.txt")
            )
        )
        assert after_cleanup == after_failure
        """
    )
    repository_root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    existing_preload = environment.get("LD_PRELOAD")
    environment["LD_PRELOAD"] = os.pathsep.join(
        value for value in (os.fspath(library), existing_preload) if value
    )
    environment["CODENIB_CAPTURE_RACE"] = "1"
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(repository_root), environment.get("PYTHONPATH", ""))
    )

    completed = subprocess.run(
        [sys.executable, "-c", script, os.fspath(root)],
        cwd=repository_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("rebind", ("destination", "parent", "allowed-root"))
def test_native_captured_destination_detects_every_lexical_rebind(
    tmp_path: Path,
    rebind: str,
) -> None:
    root = tmp_path / "authority"
    destination = root / "nested" / "published"
    destination.mkdir(parents=True)
    root.chmod(0o700)
    (destination / "payload.txt").write_bytes(b"original")
    owner = _capture_existing_destination(root, b"nested/published")

    if rebind == "destination":
        destination.rename(root / "nested" / "captured-original")
        destination.mkdir()
    elif rebind == "parent":
        (root / "nested").rename(root / "captured-parent")
        destination.mkdir(parents=True)
    else:
        root.rename(tmp_path / "captured-root")
        destination.mkdir(parents=True)
        root.chmod(0o700)
    before_cleanup = _tree_fingerprint(tmp_path)

    with pytest.raises(RuntimeError, match="changed"):
        workspace_owner.verify_owner_destination_binding(owner)
    with pytest.raises(RuntimeError, match="changed"):
        workspace_owner.verify_owner_authority(owner)
    with pytest.raises(RuntimeError, match="unavailable"):
        workspace_owner.borrow_owner_destination_descriptor(owner)

    workspace_owner.abort_owner(owner)
    assert workspace_owner.owner_closed(owner)
    assert _tree_fingerprint(tmp_path) == before_cleanup


def test_native_captured_destination_cleanup_does_not_close_reused_foreign_fd(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    destination = root / "published"
    destination.mkdir(parents=True)
    root.chmod(0o700)
    before = _tree_fingerprint(root)
    owner = _capture_existing_destination(root, b"published")
    descriptor = workspace_owner.borrow_owner_destination_descriptor(owner)
    os.close(descriptor)
    foreign_source = os.open(
        destination,
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    if foreign_source != descriptor:
        os.dup2(foreign_source, descriptor, inheritable=False)

    try:
        with pytest.raises(RuntimeError, match="changed"):
            workspace_owner.verify_owner_destination_binding(owner)
        with pytest.raises(OSError):
            workspace_owner.abort_owner(owner)
        assert workspace_owner.owner_closed(owner)
        replacement = os.fstat(descriptor)
        expected = destination.stat()
        assert stat.S_ISDIR(replacement.st_mode)
        assert (replacement.st_dev, replacement.st_ino) == (
            expected.st_dev,
            expected.st_ino,
        )
        assert _tree_fingerprint(root) == before
        assert workspace_owner.abort_owner(owner) is None
    finally:
        os.close(descriptor)
        if foreign_source != descriptor:
            os.close(foreign_source)


def test_native_captured_destination_deallocation_only_closes_descriptors(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    destination = root / "published"
    destination.mkdir(parents=True)
    root.chmod(0o700)
    (destination / "payload.txt").write_bytes(b"preserved")
    before = _tree_fingerprint(root)
    owner = _capture_existing_destination(root, b"published")
    workspace_owner.borrow_owner_destination_descriptor(owner)

    del owner
    gc.collect()

    owned_targets = []
    for name in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{name}")
        except OSError:
            continue
        if os.fspath(root) in target:
            owned_targets.append(target)
    assert owned_targets == []
    assert _tree_fingerprint(root) == before


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_native_captured_destination_child_revokes_inherited_fds_without_mutation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    destination = root / "published"
    destination.mkdir(parents=True)
    root.chmod(0o700)
    (destination / "payload.txt").write_bytes(b"preserved")
    before = _tree_fingerprint(root)
    owner = _capture_existing_destination(root, b"published")
    borrowed = workspace_owner.borrow_owner_destination_descriptor(owner)
    read_descriptor, write_descriptor = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        os.close(read_descriptor)
        foreign_source = -1
        try:
            for operation in (
                lambda: workspace_owner.verify_owner_destination_binding(owner),
                lambda: workspace_owner.borrow_owner_destination_descriptor(owner),
                lambda: workspace_owner.abort_owner(owner),
            ):
                try:
                    operation()
                except RuntimeError as error:
                    assert "PID boundary" in str(error)
                else:
                    raise AssertionError("cross-PID owner operation succeeded")
            os.close(borrowed)
            foreign_source = os.open(
                "/dev/null",
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
            )
            if foreign_source != borrowed:
                os.dup2(foreign_source, borrowed, inheritable=False)
            try:
                workspace_owner.close_owner_exact(owner)
            except RuntimeError as error:
                assert "PID boundary" in str(error)
            else:
                raise AssertionError("cross-PID close did not report its boundary")
            assert stat.S_ISCHR(os.fstat(borrowed).st_mode)
            owned_targets = []
            for name in os.listdir("/proc/self/fd"):
                try:
                    target = os.readlink(f"/proc/self/fd/{name}")
                except OSError:
                    continue
                if os.fspath(root) in target:
                    owned_targets.append(target)
            assert owned_targets == []
            os.write(write_descriptor, b"ok")
        except BaseException as error:  # noqa: B036 - report child failure
            os.write(write_descriptor, repr(error).encode("utf-8"))
        finally:
            try:
                os.close(borrowed)
            except OSError:
                pass
            if foreign_source >= 0 and foreign_source != borrowed:
                os.close(foreign_source)
            os.close(write_descriptor)
            os._exit(0)

    os.close(write_descriptor)
    report = os.read(read_descriptor, 4096)
    os.close(read_descriptor)
    waited_pid, status = os.waitpid(child_pid, 0)
    assert waited_pid == child_pid
    assert os.waitstatus_to_exitcode(status) == 0
    assert report == b"ok"
    assert workspace_owner.verify_owner_destination_binding(owner) is None
    assert _tree_fingerprint(root) == before
    workspace_owner.abort_owner(owner)
    assert workspace_owner.owner_closed(owner)
    assert _tree_fingerprint(root) == before


def test_native_owner_atomically_exchanges_exact_replacement_and_commits_receipt(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    destination = root / "published"
    destination.mkdir(parents=True)
    root.chmod(0o700)
    (destination / "incumbent.txt").write_bytes(b"incumbent")
    incumbent_identity = destination.stat().st_dev, destination.stat().st_ino
    owner, plan, permit, incumbent_descriptor = _provision_replacement(root)

    assert workspace_owner.owner_state(owner) == "replacement-provisioned"
    candidate_descriptor = workspace_owner.borrow_owner_root_descriptor(owner)
    parent_descriptor = workspace_owner.borrow_owner_parent_descriptor(owner)
    assert os.fstat(incumbent_descriptor).st_ino == incumbent_identity[1]
    _adopt_and_fill_replacement(root, owner, plan)
    assert workspace_owner.owner_state(owner) == "replacement-adopted"
    assert workspace_owner.verify_owner_replacement_binding(owner) is None
    assert workspace_owner.borrow_owner_parent_descriptor(owner) == parent_descriptor

    receipt_token = workspace_owner.exchange_owner_replacement(
        permit,
        b".replacement",
        b"published",
        time.monotonic_ns() + 10_000_000_000,
    )

    assert workspace_owner.owner_state(owner) == ("replacement-exchanged-unreceipted")
    assert workspace_owner.verify_owner_replacement_binding(owner) is None
    with pytest.raises(RuntimeError, match="unavailable"):
        workspace_owner.borrow_owner_parent_descriptor(owner)
    assert (destination / "views/bm25/documents.json").read_bytes() == b"replacement"
    assert (root / ".replacement" / "incumbent.txt").read_bytes() == b"incumbent"
    assert (
        os.fstat(candidate_descriptor).st_dev,
        os.fstat(candidate_descriptor).st_ino,
    ) == (
        destination.stat().st_dev,
        destination.stat().st_ino,
    )
    assert (
        os.fstat(incumbent_descriptor).st_dev,
        os.fstat(incumbent_descriptor).st_ino,
    ) == (
        (root / ".replacement").stat().st_dev,
        (root / ".replacement").stat().st_ino,
    )

    independent_parent = os.open(
        root,
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(independent_parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
        workspace_owner.commit_owner_receipt(receipt_token)
        workspace_owner.commit_owner_receipt(receipt_token)
        assert workspace_owner.owner_state(owner) == "replacement-receipted"
        with pytest.raises(RuntimeError, match="binding changed"):
            workspace_owner.verify_owner_replacement_binding(owner)
        with pytest.raises(RuntimeError, match="unavailable"):
            workspace_owner.borrow_owner_parent_descriptor(owner)
        fcntl.flock(independent_parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(independent_parent, fcntl.LOCK_UN)
    finally:
        os.close(independent_parent)

    assert os.fstat(parent_descriptor).st_ino == root.stat().st_ino
    workspace_owner.close_owner_exact(owner)
    assert workspace_owner.owner_closed(owner)
    assert (destination / "views/bm25/documents.json").read_bytes() == b"replacement"
    assert (root / ".replacement" / "incumbent.txt").read_bytes() == b"incumbent"


def test_native_owner_rolls_back_unreceipted_replacement_and_quarantines_candidate(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    destination = root / "published"
    destination.mkdir(parents=True)
    root.chmod(0o700)
    (destination / "incumbent.txt").write_bytes(b"incumbent")
    owner, plan, permit, _incumbent_descriptor = _provision_replacement(root)
    _adopt_and_fill_replacement(root, owner, plan, payload=b"candidate")
    workspace_owner.exchange_owner_replacement(
        permit,
        b".replacement",
        b"published",
        time.monotonic_ns() + 10_000_000_000,
    )

    assert workspace_owner.abort_owner(owner) is None

    assert workspace_owner.owner_closed(owner)
    assert (destination / "incumbent.txt").read_bytes() == b"incumbent"
    assert (root / ".replacement" / "views/bm25/documents.json").read_bytes() == (
        b"candidate"
    )


def test_native_owner_baseexception_after_exchange_return_rolls_back_exactly(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    destination = root / "published"
    destination.mkdir(parents=True)
    root.chmod(0o700)
    (destination / "incumbent.txt").write_bytes(b"incumbent")
    owner, plan, permit, _incumbent_descriptor = _provision_replacement(root)
    _adopt_and_fill_replacement(root, owner, plan, payload=b"candidate")
    error = KeyboardInterrupt("after native exchange return")

    with pytest.raises(KeyboardInterrupt) as raised:
        try:
            workspace_owner.exchange_owner_replacement(
                permit,
                b".replacement",
                b"published",
                time.monotonic_ns() + 10_000_000_000,
            )
            raise error
        finally:
            workspace_owner.abort_owner(owner)

    assert raised.value is error
    assert workspace_owner.owner_closed(owner)
    assert (destination / "incumbent.txt").read_bytes() == b"incumbent"
    assert (root / ".replacement" / "views/bm25/documents.json").read_bytes() == (
        b"candidate"
    )


def test_native_replacement_commit_is_idempotent_after_return_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "authority"
    destination = root / "published"
    destination.mkdir(parents=True)
    root.chmod(0o700)
    (destination / "incumbent.txt").write_bytes(b"incumbent")
    owner, plan, permit, _incumbent_descriptor = _provision_replacement(root)
    _adopt_and_fill_replacement(root, owner, plan, payload=b"candidate")
    receipt_token = workspace_owner.exchange_owner_replacement(
        permit,
        b".replacement",
        b"published",
        time.monotonic_ns() + 10_000_000_000,
    )
    exact_commit = workspace_owner._commit_owner_receipt_exact
    assert exact_commit is not None
    interruption = KeyboardInterrupt("after replacement commit return")

    def commit_then_interrupt(token: object) -> None:
        assert exact_commit(token) is None
        raise interruption

    monkeypatch.setattr(
        workspace_owner,
        "_commit_owner_receipt_exact",
        commit_then_interrupt,
    )
    with pytest.raises(KeyboardInterrupt) as raised:
        workspace_owner.commit_owner_receipt(receipt_token)
    assert raised.value is interruption
    assert workspace_owner.owner_state(owner) == "replacement-receipted"

    monkeypatch.setattr(
        workspace_owner,
        "_commit_owner_receipt_exact",
        exact_commit,
    )
    workspace_owner.commit_owner_receipt(receipt_token)
    workspace_owner.close_owner_exact(owner)
    assert (destination / "views/bm25/documents.json").read_bytes() == b"candidate"
    assert (root / ".replacement" / "incumbent.txt").read_bytes() == b"incumbent"


def test_native_replacement_requires_exact_bounded_arguments(tmp_path: Path) -> None:
    class BytesSubclass(bytes):
        pass

    root = tmp_path / "authority"
    (root / "published").mkdir(parents=True)
    root.chmod(0o700)
    digest = _plan().digest.encode("ascii")
    deadline = time.monotonic_ns() + 10_000_000_000
    invalid_arguments = (
        (".replacement", digest, 0o700, (), deadline, TypeError),
        (BytesSubclass(b".replacement"), digest, 0o700, (), deadline, TypeError),
        (b"", digest, 0o700, (), deadline, ValueError),
        (b"replacement", digest, 0o700, (), deadline, ValueError),
        (b"nested/slot", digest, 0o700, (), deadline, ValueError),
        (b"published", digest, 0o700, (), deadline, ValueError),
        (b".replacement", digest.decode(), 0o700, (), deadline, TypeError),
        (b".replacement", BytesSubclass(digest), 0o700, (), deadline, TypeError),
        (b".replacement", b"0" * 63, 0o700, (), deadline, ValueError),
        (b".replacement", b"A" * 64, 0o700, (), deadline, ValueError),
        (b".replacement", digest, True, (), deadline, TypeError),
        (b".replacement", digest, -1, (), deadline, ValueError),
        (b".replacement", digest, 0o1000, (), deadline, ValueError),
        (b".replacement", digest, 0o700, [], deadline, TypeError),
        (b".replacement", digest, 0o700, (("views", 0o700),), deadline, TypeError),
        (b".replacement", digest, 0o700, ((b"views", True),), deadline, TypeError),
        (b".replacement", digest, 0o700, (), True, TypeError),
        (b".replacement", digest, 0o700, (), 0, ValueError),
        (
            b".replacement",
            digest,
            0o700,
            (),
            time.monotonic_ns() - 1,
            TimeoutError,
        ),
    )

    for (
        slot,
        plan_digest,
        mode,
        directories,
        provision_deadline,
        error_type,
    ) in invalid_arguments:
        owner = _capture_existing_destination(root, b"published")
        workspace_owner.acquire_owner_replacement_lease(
            owner,
            time.monotonic_ns() + 10_000_000_000,
        )
        replacement_permit = workspace_owner.claim_owner_replacement_permit(owner)
        with pytest.raises(error_type):
            workspace_owner.provision_owner_replacement(
                owner,
                slot,  # type: ignore[arg-type]
                plan_digest,  # type: ignore[arg-type]
                mode,
                directories,  # type: ignore[arg-type]
                provision_deadline,
            )
        assert workspace_owner.owner_state(owner) == "destination-leased"
        assert not (root / ".replacement").exists()
        del replacement_permit
        workspace_owner.abort_owner(owner)
        assert workspace_owner.owner_closed(owner)


def test_native_replacement_permit_is_exact_owner_bound_and_one_shot(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    destination = root / "published"
    destination.mkdir(parents=True)
    root.chmod(0o700)
    owner, plan, permit, _incumbent_descriptor = _provision_replacement(root)
    _adopt_and_fill_replacement(root, owner, plan)

    with pytest.raises(TypeError, match="cannot be constructed"):
        type(permit)()
    with pytest.raises(TypeError, match="invalid native type"):
        workspace_owner.exchange_owner_replacement(
            owner,
            b".replacement",
            b"published",
            time.monotonic_ns() + 10_000_000_000,
        )
    with pytest.raises(TypeError, match="invalid native type"):
        workspace_owner.claim_owner_replacement_permit(object())
    with pytest.raises(TypeError, match="invalid native type"):
        workspace_owner.provision_owner_replacement(
            object(),
            b".replacement",
            b"0" * 64,
            0o700,
            (),
            time.monotonic_ns() + 10_000_000_000,
        )
    with pytest.raises(TypeError, match="invalid native type"):
        workspace_owner.verify_owner_replacement_binding(object())
    for slot, destination_name, deadline, error_type in (
        (".replacement", b"published", time.monotonic_ns() + 10_000_000_000, TypeError),
        (b".replacement", "published", time.monotonic_ns() + 10_000_000_000, TypeError),
        (b"wrong", b"published", time.monotonic_ns() + 10_000_000_000, ValueError),
        (b".replacement", b"wrong", time.monotonic_ns() + 10_000_000_000, ValueError),
        (b".replacement", b"published", True, TypeError),
        (b".replacement", b"published", 0, ValueError),
    ):
        with pytest.raises(error_type):
            workspace_owner.exchange_owner_replacement(
                permit,
                slot,  # type: ignore[arg-type]
                destination_name,  # type: ignore[arg-type]
                deadline,
            )
        assert workspace_owner.owner_state(owner) == "replacement-adopted"

    receipt_token = workspace_owner.exchange_owner_replacement(
        permit,
        b".replacement",
        b"published",
        time.monotonic_ns() + 10_000_000_000,
    )
    with pytest.raises(RuntimeError, match="unavailable"):
        workspace_owner.exchange_owner_replacement(
            permit,
            b".replacement",
            b"published",
            time.monotonic_ns() + 10_000_000_000,
        )
    workspace_owner.commit_owner_receipt(receipt_token)
    workspace_owner.close_owner_exact(owner)


def test_native_replacement_permit_drop_and_cross_owner_use_fail_closed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    (root / "first-parent" / "first").mkdir(parents=True)
    (root / "second-parent" / "second").mkdir(parents=True)
    root.chmod(0o700)
    first_owner = _capture_existing_destination(root, b"first-parent/first")
    second_owner = _capture_existing_destination(root, b"second-parent/second")
    workspace_owner.acquire_owner_replacement_lease(
        first_owner,
        time.monotonic_ns() + 10_000_000_000,
    )
    workspace_owner.acquire_owner_replacement_lease(
        second_owner,
        time.monotonic_ns() + 10_000_000_000,
    )
    first_permit = workspace_owner.claim_owner_replacement_permit(first_owner)
    second_permit = workspace_owner.claim_owner_replacement_permit(second_owner)
    workspace_owner.provision_owner_replacement(
        second_owner,
        b".second-replacement",
        b"0" * 64,
        0o700,
        (),
        time.monotonic_ns() + 10_000_000_000,
    )
    workspace_owner.verify_owner_adoption_binding(
        second_owner,
        os.fsencode(root / "second-parent" / "second"),
        b".second-replacement",
        b"0" * 64,
    )
    workspace_owner.mark_owner_adopted(second_owner)
    workspace_owner.seal_owner_directories(second_owner)

    with pytest.raises(ValueError, match="names differ"):
        workspace_owner.exchange_owner_replacement(
            first_permit,
            b".second-replacement",
            b"second",
            time.monotonic_ns() + 10_000_000_000,
        )
    assert workspace_owner.owner_state(second_owner) == "replacement-adopted"

    del first_permit
    gc.collect()
    with pytest.raises(RuntimeError, match="not replacement-ready"):
        workspace_owner.provision_owner_replacement(
            first_owner,
            b".replacement",
            b"0" * 64,
            0o700,
            (),
            time.monotonic_ns() + 10_000_000_000,
        )
    assert workspace_owner.owner_state(first_owner) == "destination-leased"
    assert not (root / "first-parent" / ".replacement").exists()
    workspace_owner.abort_owner(first_owner)
    workspace_owner.abort_owner(second_owner)
    del second_permit


def test_native_exchanged_replacement_rejects_every_legacy_owner_operation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    destination = root / "published"
    destination.mkdir(parents=True)
    root.chmod(0o700)
    owner, plan, permit, _incumbent_descriptor = _provision_replacement(root)
    _adopt_and_fill_replacement(root, owner, plan)
    receipt_token = workspace_owner.exchange_owner_replacement(
        permit,
        b".replacement",
        b"published",
        time.monotonic_ns() + 10_000_000_000,
    )
    before = _tree_fingerprint(root)

    rejected = (
        lambda: workspace_owner.claim_owner_publish_permit(owner),
        lambda: workspace_owner.claim_owner_replacement_permit(owner),
        lambda: workspace_owner.capture_owner_destination(
            owner,
            os.fsencode(root),
            b"published",
            time.monotonic_ns() + 10_000_000_000,
        ),
        lambda: workspace_owner.provision_owner(
            owner,
            os.fsencode(root),
            b"other",
            b".stage",
            b"0" * 64,
            0o700,
            (),
            time.monotonic_ns() + 10_000_000_000,
        ),
        lambda: workspace_owner.borrow_owner_destination_descriptor(owner),
        lambda: workspace_owner.borrow_owner_parent_descriptor(owner),
        lambda: workspace_owner.borrow_owner_root_descriptor(owner),
        lambda: workspace_owner.borrow_owner_directory_descriptor(owner, b"views"),
        lambda: workspace_owner.begin_owner_file(owner, b"", b"late", 0o600),
        lambda: workspace_owner.write_owner_file(owner, b"late"),
        lambda: workspace_owner.finish_owner_file(owner, 0o600),
        lambda: workspace_owner.abort_owner_file(owner),
        lambda: workspace_owner.seal_owner_directories(owner),
        lambda: workspace_owner.sync_owner_parent(owner),
        lambda: workspace_owner.mark_owner_adopted(owner),
        lambda: workspace_owner.quarantine_owner(owner),
    )
    for operation in rejected:
        with pytest.raises((RuntimeError, TypeError)):
            operation()
        assert workspace_owner.owner_state(owner) == (
            "replacement-exchanged-unreceipted"
        )
        assert _tree_fingerprint(root) == before

    workspace_owner.commit_owner_receipt(receipt_token)
    workspace_owner.close_owner_exact(owner)


@pytest.mark.parametrize("rebind", ("destination", "replacement-slot"))
@pytest.mark.parametrize("phase", ("provisioned", "adopted"))
def test_native_replacement_sync_rejects_every_rebound_root(
    tmp_path: Path,
    rebind: str,
    phase: str,
) -> None:
    root = tmp_path / "authority"
    destination = root / "published"
    destination.mkdir(parents=True)
    root.chmod(0o700)
    (destination / "incumbent.txt").write_bytes(b"incumbent")
    owner, plan, _permit, _incumbent_descriptor = _provision_replacement(root)
    if phase == "adopted":
        _adopt_and_fill_replacement(root, owner, plan, payload=b"candidate")
    assert workspace_owner.owner_state(owner) == f"replacement-{phase}"
    slot = root / ".replacement"
    rebound = destination if rebind == "destination" else slot
    retained = root / f".retained-sync-{phase}-{rebind}"
    foreign = root / f".foreign-sync-{phase}-{rebind}"
    rebound.rename(retained)
    rebound.mkdir()
    (rebound / "foreign.txt").write_bytes(b"foreign")
    before = _tree_fingerprint(root)

    with pytest.raises(RuntimeError, match="binding changed"):
        workspace_owner.sync_owner_parent(owner)
    assert workspace_owner.owner_state(owner) == f"replacement-{phase}"
    assert _tree_fingerprint(root) == before

    rebound.rename(foreign)
    retained.rename(rebound)
    workspace_owner.abort_owner(owner)
    assert workspace_owner.owner_closed(owner)
    assert (foreign / "foreign.txt").read_bytes() == b"foreign"


@pytest.mark.parametrize("rebind", ("destination", "replacement-slot"))
def test_native_preexchange_settlement_retains_lease_until_rebind_is_restored(
    tmp_path: Path,
    rebind: str,
) -> None:
    root = tmp_path / "authority"
    destination = root / "published"
    destination.mkdir(parents=True)
    root.chmod(0o700)
    (destination / "incumbent.txt").write_bytes(b"incumbent")
    owner, plan, permit, _incumbent_descriptor = _provision_replacement(root)
    _adopt_and_fill_replacement(root, owner, plan, payload=b"candidate")
    slot = root / ".replacement"
    rebound = destination if rebind == "destination" else slot
    retained = root / f".retained-{rebind}"
    foreign = root / f".foreign-{rebind}"
    rebound.rename(retained)
    rebound.mkdir()
    (rebound / "foreign.txt").write_bytes(b"foreign")

    with pytest.raises(RuntimeError, match="changed"):
        workspace_owner.verify_owner_replacement_binding(owner)
    with pytest.raises(RuntimeError, match="unavailable"):
        workspace_owner.borrow_owner_parent_descriptor(owner)
    with pytest.raises(RuntimeError, match="not exchange-ready"):
        workspace_owner.exchange_owner_replacement(
            permit,
            b".replacement",
            b"published",
            time.monotonic_ns() + 10_000_000_000,
        )
    with pytest.raises(OSError):
        workspace_owner.abort_owner(owner)
    assert workspace_owner.owner_state(owner) == "replacement-recovery-required"
    with pytest.raises(RuntimeError, match="unavailable"):
        workspace_owner.borrow_owner_parent_descriptor(owner)
    with pytest.raises(OSError):
        workspace_owner.close_owner_exact(owner)

    independent_parent = os.open(
        root,
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(independent_parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(independent_parent)

    rebound.rename(foreign)
    retained.rename(rebound)
    workspace_owner.abort_owner(owner)
    assert workspace_owner.owner_closed(owner)
    assert (foreign / "foreign.txt").read_bytes() == b"foreign"
    assert (destination / "incumbent.txt").read_bytes() == b"incumbent"
    assert (slot / "views/bm25/documents.json").read_bytes() == b"candidate"


@pytest.mark.parametrize("rebind", ("destination", "replacement-slot"))
def test_native_postexchange_rebind_never_blindly_rolls_back_foreign_name(
    tmp_path: Path,
    rebind: str,
) -> None:
    root = tmp_path / "authority"
    destination = root / "published"
    destination.mkdir(parents=True)
    root.chmod(0o700)
    (destination / "incumbent.txt").write_bytes(b"incumbent")
    owner, plan, permit, _incumbent_descriptor = _provision_replacement(root)
    _adopt_and_fill_replacement(root, owner, plan, payload=b"candidate")
    workspace_owner.exchange_owner_replacement(
        permit,
        b".replacement",
        b"published",
        time.monotonic_ns() + 10_000_000_000,
    )
    slot = root / ".replacement"
    rebound = destination if rebind == "destination" else slot
    retained = root / f".retained-post-{rebind}"
    foreign = root / f".foreign-post-{rebind}"
    rebound.rename(retained)
    rebound.mkdir()
    (rebound / "foreign.txt").write_bytes(b"foreign")

    with pytest.raises(OSError):
        workspace_owner.abort_owner(owner)
    assert workspace_owner.owner_state(owner) == "replacement-recovery-required"
    assert (rebound / "foreign.txt").read_bytes() == b"foreign"

    rebound.rename(foreign)
    retained.rename(rebound)
    workspace_owner.abort_owner(owner)
    assert workspace_owner.owner_closed(owner)
    assert (foreign / "foreign.txt").read_bytes() == b"foreign"
    assert (destination / "incumbent.txt").read_bytes() == b"incumbent"
    assert (slot / "views/bm25/documents.json").read_bytes() == b"candidate"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux")
def test_native_abort_reverses_an_exact_exchange_observed_in_preexchange_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    destination = root / "published"
    destination.mkdir(parents=True)
    root.chmod(0o700)
    (destination / "incumbent.txt").write_bytes(b"incumbent")
    owner, plan, _permit, _incumbent_descriptor = _provision_replacement(root)
    _adopt_and_fill_replacement(root, owner, plan, payload=b"candidate")
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        workspace_owner.abort_owner(owner)
        pytest.skip("libc does not expose renameat2")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(root / ".replacement"),
        -100,
        os.fsencode(destination),
        1 << 1,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        workspace_owner.abort_owner(owner)
        pytest.skip(f"RENAME_EXCHANGE unavailable: errno {error_number}")

    assert (destination / "views/bm25/documents.json").read_bytes() == b"candidate"
    workspace_owner.abort_owner(owner)

    assert workspace_owner.owner_closed(owner)
    assert (destination / "incumbent.txt").read_bytes() == b"incumbent"
    assert (root / ".replacement" / "views/bm25/documents.json").read_bytes() == (
        b"candidate"
    )


def test_native_parent_commit_lease_serializes_cooperative_sibling_replacements(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    first_destination = root / "first"
    second_destination = root / "second"
    first_destination.mkdir(parents=True)
    second_destination.mkdir()
    root.chmod(0o700)
    (first_destination / "old.txt").write_bytes(b"first")
    (second_destination / "old.txt").write_bytes(b"second")
    first_owner = _capture_existing_destination(root, b"first")
    second_owner = _capture_existing_destination(root, b"second")
    workspace_owner.acquire_owner_replacement_lease(
        first_owner,
        time.monotonic_ns() + 10_000_000_000,
    )
    first_plan = _file_plan()
    first_permit = workspace_owner.claim_owner_replacement_permit(first_owner)
    workspace_owner.provision_owner_replacement(
        first_owner,
        b".first-replacement",
        first_plan.digest.encode("ascii"),
        first_plan.root_mode,
        tuple(
            (os.fsencode(item.path.as_posix()), item.mode)
            for item in first_plan.directories
        ),
        time.monotonic_ns() + 10_000_000_000,
    )
    _adopt_and_fill_replacement(
        root,
        first_owner,
        first_plan,
        destination=b"first",
        slot=b".first-replacement",
        payload=b"first-new",
    )

    with pytest.raises(BlockingIOError):
        workspace_owner.acquire_owner_replacement_lease(
            second_owner,
            time.monotonic_ns() + 10_000_000_000,
        )
    assert workspace_owner.owner_state(second_owner) == "destination-captured"
    assert not (root / ".second-replacement").exists()

    first_token = workspace_owner.exchange_owner_replacement(
        first_permit,
        b".first-replacement",
        b"first",
        time.monotonic_ns() + 10_000_000_000,
    )
    workspace_owner.commit_owner_receipt(first_token)
    workspace_owner.close_owner_exact(first_owner)

    workspace_owner.acquire_owner_replacement_lease(
        second_owner,
        time.monotonic_ns() + 10_000_000_000,
    )
    assert workspace_owner.owner_state(second_owner) == "destination-leased"
    workspace_owner.abort_owner(second_owner)
    assert workspace_owner.owner_closed(second_owner)
    assert (first_destination / "views/bm25/documents.json").read_bytes() == (
        b"first-new"
    )
    assert (second_destination / "old.txt").read_bytes() == b"second"


def test_native_preexchange_contention_dealloc_closes_only_the_losing_owner(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    first_destination = root / "first"
    second_destination = root / "second"
    first_destination.mkdir(parents=True)
    second_destination.mkdir()
    root.chmod(0o700)
    (first_destination / "old.txt").write_bytes(b"first")
    (second_destination / "old.txt").write_bytes(b"second")
    first_owner, first_plan, first_permit, _ = _provision_replacement(
        root,
        destination=b"first",
        slot=b".first-replacement",
    )
    _adopt_and_fill_replacement(
        root,
        first_owner,
        first_plan,
        destination=b"first",
        slot=b".first-replacement",
        payload=b"first-new",
    )
    first_token = workspace_owner.exchange_owner_replacement(
        first_permit,
        b".first-replacement",
        b"first",
        time.monotonic_ns() + 10_000_000_000,
    )
    gc.collect()
    baseline_fd_count = len(os.listdir("/proc/self/fd"))

    second_owner = _capture_existing_destination(root, b"second")
    before_cleanup = _tree_fingerprint(root)
    with pytest.raises(BlockingIOError):
        workspace_owner.acquire_owner_replacement_lease(
            second_owner,
            time.monotonic_ns() + 10_000_000_000,
        )
    assert not workspace_owner.owner_closed(second_owner)
    assert workspace_owner.owner_state(second_owner) == "destination-captured"
    assert _tree_fingerprint(root) == before_cleanup

    sentinel = KeyboardInterrupt("sentinel")
    try:
        raise sentinel
    except BaseException:  # noqa: B036 - assert dealloc preserves the exception
        del second_owner
        gc.collect()
        assert sys.exc_info()[1] is sentinel

    assert len(os.listdir("/proc/self/fd")) == baseline_fd_count
    assert _tree_fingerprint(root) == before_cleanup
    independent_parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(independent_parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(independent_parent)

    workspace_owner.commit_owner_receipt(first_token)
    workspace_owner.close_owner_exact(first_owner)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux")
def test_native_overlapping_same_destination_capture_fails_closed_after_commit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    destination = root / "published"
    destination.mkdir(parents=True)
    root.chmod(0o700)
    (destination / "incumbent.txt").write_bytes(b"incumbent")
    first_owner = _capture_existing_destination(root, b"published")
    second_owner = _capture_existing_destination(root, b"published")
    workspace_owner.acquire_owner_replacement_lease(
        first_owner,
        time.monotonic_ns() + 10_000_000_000,
    )
    first_plan = _file_plan()
    first_permit = workspace_owner.claim_owner_replacement_permit(first_owner)
    workspace_owner.provision_owner_replacement(
        first_owner,
        b".first-replacement",
        first_plan.digest.encode("ascii"),
        first_plan.root_mode,
        tuple(
            (os.fsencode(item.path.as_posix()), item.mode)
            for item in first_plan.directories
        ),
        time.monotonic_ns() + 10_000_000_000,
    )
    _adopt_and_fill_replacement(
        root,
        first_owner,
        first_plan,
        slot=b".first-replacement",
        payload=b"first-candidate",
    )
    first_token = workspace_owner.exchange_owner_replacement(
        first_permit,
        b".first-replacement",
        b"published",
        time.monotonic_ns() + 10_000_000_000,
    )
    workspace_owner.commit_owner_receipt(first_token)

    with pytest.raises(RuntimeError, match="changed before replacement lease"):
        workspace_owner.acquire_owner_replacement_lease(
            second_owner,
            time.monotonic_ns() + 10_000_000_000,
        )
    assert workspace_owner.owner_state(second_owner) == "destination-captured"
    assert not (root / ".second-replacement").exists()
    assert (destination / "views/bm25/documents.json").read_bytes() == (
        b"first-candidate"
    )
    independent_parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(independent_parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(independent_parent, fcntl.LOCK_UN)
    finally:
        os.close(independent_parent)

    workspace_owner.abort_owner(second_owner)
    workspace_owner.close_owner_exact(first_owner)


def test_native_overlapping_capture_can_lease_after_prior_owner_aborts_exchange(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    destination = root / "published"
    destination.mkdir(parents=True)
    root.chmod(0o700)
    (destination / "incumbent.txt").write_bytes(b"incumbent")
    first_owner = _capture_existing_destination(root, b"published")
    second_owner = _capture_existing_destination(root, b"published")
    workspace_owner.acquire_owner_replacement_lease(
        first_owner,
        time.monotonic_ns() + 10_000_000_000,
    )
    first_plan = _file_plan()
    first_permit = workspace_owner.claim_owner_replacement_permit(first_owner)
    workspace_owner.provision_owner_replacement(
        first_owner,
        b".first-replacement",
        first_plan.digest.encode("ascii"),
        first_plan.root_mode,
        tuple(
            (os.fsencode(item.path.as_posix()), item.mode)
            for item in first_plan.directories
        ),
        time.monotonic_ns() + 10_000_000_000,
    )
    _adopt_and_fill_replacement(
        root,
        first_owner,
        first_plan,
        slot=b".first-replacement",
        payload=b"candidate",
    )
    workspace_owner.exchange_owner_replacement(
        first_permit,
        b".first-replacement",
        b"published",
        time.monotonic_ns() + 10_000_000_000,
    )
    workspace_owner.abort_owner(first_owner)

    assert workspace_owner.owner_closed(first_owner)
    assert (destination / "incumbent.txt").read_bytes() == b"incumbent"
    assert workspace_owner.owner_state(second_owner) == "destination-captured"
    assert not (root / ".second-replacement").exists()
    workspace_owner.acquire_owner_replacement_lease(
        second_owner,
        time.monotonic_ns() + 10_000_000_000,
    )
    assert workspace_owner.owner_state(second_owner) == "destination-leased"
    assert workspace_owner.verify_owner_destination_binding(second_owner) is None
    assert not (root / ".second-replacement").exists()
    workspace_owner.abort_owner(second_owner)
    assert workspace_owner.owner_closed(second_owner)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_native_cross_process_stale_capture_releases_lease_without_candidate(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    destination = root / "published"
    destination.mkdir(parents=True)
    root.chmod(0o700)
    (destination / "incumbent.txt").write_bytes(b"incumbent")
    ready_read, ready_write = os.pipe()
    proceed_read, proceed_write = os.pipe()
    report_read, report_write = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        os.close(ready_read)
        os.close(proceed_write)
        os.close(report_read)
        try:
            owner = _capture_existing_destination(root, b"published")
            os.write(ready_write, b"ready")
            os.close(ready_write)
            assert os.read(proceed_read, 1) == b"x"
            os.close(proceed_read)
            try:
                workspace_owner.acquire_owner_replacement_lease(
                    owner,
                    time.monotonic_ns() + 10_000_000_000,
                )
            except RuntimeError as error:
                assert "changed before replacement lease" in str(error)
            else:
                raise AssertionError("stale captured destination was leased")
            assert workspace_owner.owner_state(owner) == "destination-captured"
            assert not (root / ".second-replacement").exists()
            independent_parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                fcntl.flock(
                    independent_parent,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
                fcntl.flock(independent_parent, fcntl.LOCK_UN)
            finally:
                os.close(independent_parent)
            workspace_owner.abort_owner(owner)
            assert workspace_owner.owner_closed(owner)
            report = b"ok"
        except BaseException as error:  # noqa: B036 - report child failure
            report = repr(error).encode("utf-8")
        os.write(report_write, report)
        os.close(report_write)
        os._exit(0)

    os.close(ready_write)
    os.close(proceed_read)
    os.close(report_write)
    assert os.read(ready_read, 5) == b"ready"
    os.close(ready_read)
    first_owner = _capture_existing_destination(root, b"published")
    workspace_owner.acquire_owner_replacement_lease(
        first_owner,
        time.monotonic_ns() + 10_000_000_000,
    )
    first_plan = _file_plan()
    first_permit = workspace_owner.claim_owner_replacement_permit(first_owner)
    workspace_owner.provision_owner_replacement(
        first_owner,
        b".first-replacement",
        first_plan.digest.encode("ascii"),
        first_plan.root_mode,
        tuple(
            (os.fsencode(item.path.as_posix()), item.mode)
            for item in first_plan.directories
        ),
        time.monotonic_ns() + 10_000_000_000,
    )
    _adopt_and_fill_replacement(
        root,
        first_owner,
        first_plan,
        slot=b".first-replacement",
        payload=b"first-candidate",
    )
    first_token = workspace_owner.exchange_owner_replacement(
        first_permit,
        b".first-replacement",
        b"published",
        time.monotonic_ns() + 10_000_000_000,
    )
    workspace_owner.commit_owner_receipt(first_token)
    workspace_owner.close_owner_exact(first_owner)
    os.write(proceed_write, b"x")
    os.close(proceed_write)

    report = os.read(report_read, 4096)
    os.close(report_read)
    waited_pid, status = os.waitpid(child_pid, 0)
    assert waited_pid == child_pid
    assert os.waitstatus_to_exitcode(status) == 0
    assert report == b"ok"
    assert (destination / "views/bm25/documents.json").read_bytes() == (
        b"first-candidate"
    )
    assert not (root / ".second-replacement").exists()


def test_native_replacement_fd_reuse_and_dealloc_preserve_primary_baseexception(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    destination = root / "published"
    destination.mkdir(parents=True)
    root.chmod(0o700)
    script = textwrap.dedent(
        """
        import fcntl
        import gc
        import os
        import sys
        import time
        from pathlib import Path

        import codenib._workspace_owner as workspace_owner

        root = Path(sys.argv[1])
        owner = workspace_owner.create_owner()
        workspace_owner.capture_owner_destination(
            owner,
            os.fsencode(root),
            b"published",
            time.monotonic_ns() + 10_000_000_000,
        )
        workspace_owner.borrow_owner_destination_descriptor(owner)
        workspace_owner.acquire_owner_replacement_lease(
            owner,
            time.monotonic_ns() + 10_000_000_000,
        )
        permit = workspace_owner.claim_owner_replacement_permit(owner)
        workspace_owner.provision_owner_replacement(
            owner,
            b".replacement",
            b"0" * 64,
            0o700,
            (),
            time.monotonic_ns() + 10_000_000_000,
        )
        descriptor = workspace_owner.borrow_owner_root_descriptor(owner)
        os.close(descriptor)
        independent = os.open(
            root / ".replacement",
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        if independent != descriptor:
            os.dup2(independent, descriptor, inheritable=False)
        try:
            try:
                workspace_owner.verify_owner_replacement_binding(owner)
            except RuntimeError:
                pass
            else:
                raise AssertionError("independent same-inode OFD was accepted")
            try:
                workspace_owner.abort_owner(owner)
            except OSError:
                pass
            else:
                raise AssertionError("reused candidate FD cleanup succeeded")
            assert workspace_owner.owner_state(owner) == (
                "replacement-recovery-required"
            )
            replacement = os.fstat(descriptor)
            expected = (root / ".replacement").stat()
            assert (replacement.st_dev, replacement.st_ino) == (
                expected.st_dev,
                expected.st_ino,
            )
            parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                try:
                    fcntl.flock(parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    raise AssertionError("recovery owner released its lease")
            finally:
                os.close(parent)

            sentinel = KeyboardInterrupt("sentinel")
            retained_fd_count = len(os.listdir("/proc/self/fd"))
            try:
                raise sentinel
            except BaseException:
                del permit
                del owner
                gc.collect()
                assert sys.exc_info()[1] is sentinel
            assert len(os.listdir("/proc/self/fd")) == retained_fd_count
            retained_parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                try:
                    fcntl.flock(retained_parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    raise AssertionError("deallocated recovery owner released its lease")
            finally:
                os.close(retained_parent)
        finally:
            os.close(descriptor)
            if independent != descriptor:
                os.close(independent)
        """
    )
    repository_root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(repository_root), environment.get("PYTHONPATH", ""))
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, os.fspath(root)],
        cwd=repository_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_native_replacement_permit_cannot_cross_a_pid_boundary(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    destination = root / "published"
    destination.mkdir(parents=True)
    root.chmod(0o700)
    (destination / "incumbent.txt").write_bytes(b"incumbent")
    owner, plan, permit, _incumbent_descriptor = _provision_replacement(root)
    _adopt_and_fill_replacement(root, owner, plan, payload=b"candidate")
    read_descriptor, write_descriptor = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        os.close(read_descriptor)
        try:
            workspace_owner.acquire_owner_replacement_lease(
                owner,
                time.monotonic_ns() + 10_000_000_000,
            )
        except RuntimeError as error:
            if "PID boundary" not in str(error):
                os.write(write_descriptor, repr(error).encode())
                os.close(write_descriptor)
                os._exit(0)
        else:
            os.write(write_descriptor, b"cross-PID replacement lease succeeded")
            os.close(write_descriptor)
            os._exit(0)
        try:
            workspace_owner.exchange_owner_replacement(
                permit,
                b".replacement",
                b"published",
                time.monotonic_ns() + 10_000_000_000,
            )
        except RuntimeError as error:
            report = b"ok" if "PID boundary" in str(error) else repr(error).encode()
        else:
            report = b"cross-PID replacement permit succeeded"
        os.write(write_descriptor, report)
        os.close(write_descriptor)
        os._exit(0)

    os.close(write_descriptor)
    report = os.read(read_descriptor, 4096)
    os.close(read_descriptor)
    waited_pid, status = os.waitpid(child_pid, 0)
    assert waited_pid == child_pid
    assert os.waitstatus_to_exitcode(status) == 0
    assert report == b"ok"

    receipt_token = workspace_owner.exchange_owner_replacement(
        permit,
        b".replacement",
        b"published",
        time.monotonic_ns() + 10_000_000_000,
    )
    workspace_owner.commit_owner_receipt(receipt_token)
    workspace_owner.close_owner_exact(owner)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_native_replacement_child_closes_fds_without_unlocking_or_mutating(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    destination = root / "published"
    destination.mkdir(parents=True)
    root.chmod(0o700)
    (destination / "incumbent.txt").write_bytes(b"incumbent")
    owner, plan, permit, incumbent_descriptor = _provision_replacement(root)
    candidate_descriptor = workspace_owner.borrow_owner_root_descriptor(owner)
    _adopt_and_fill_replacement(root, owner, plan, payload=b"candidate")
    receipt_token = workspace_owner.exchange_owner_replacement(
        permit,
        b".replacement",
        b"published",
        time.monotonic_ns() + 10_000_000_000,
    )
    before = _tree_fingerprint(root)
    read_descriptor, write_descriptor = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        os.close(read_descriptor)
        try:
            for operation in (
                lambda: workspace_owner.verify_owner_replacement_binding(owner),
                lambda: workspace_owner.commit_owner_receipt(receipt_token),
                lambda: workspace_owner.abort_owner(owner),
            ):
                try:
                    operation()
                except RuntimeError as error:
                    assert "PID boundary" in str(error)
                else:
                    raise AssertionError("cross-PID replacement operation succeeded")
            try:
                workspace_owner.close_owner_exact(owner)
            except RuntimeError as error:
                assert "PID boundary" in str(error)
            else:
                raise AssertionError("cross-PID close did not report its boundary")
            for descriptor in (incumbent_descriptor, candidate_descriptor):
                try:
                    os.fstat(descriptor)
                except OSError:
                    pass
                else:
                    raise AssertionError("child retained an inherited owner FD")
            os.write(write_descriptor, b"ok")
        except BaseException as error:  # noqa: B036 - report child failure
            os.write(write_descriptor, repr(error).encode("utf-8"))
        finally:
            os.close(write_descriptor)
            os._exit(0)

    os.close(write_descriptor)
    report = os.read(read_descriptor, 4096)
    os.close(read_descriptor)
    waited_pid, status = os.waitpid(child_pid, 0)
    assert waited_pid == child_pid
    assert os.waitstatus_to_exitcode(status) == 0
    assert report == b"ok"
    assert _tree_fingerprint(root) == before

    independent_parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(independent_parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(independent_parent)
    workspace_owner.commit_owner_receipt(receipt_token)
    workspace_owner.close_owner_exact(owner)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux")
@pytest.mark.parametrize(
    "fault_mode",
    (
        "deadline-after-lock-unlock-fail",
        "destination-rebind-after-lock",
        "destination-rebind-after-lock-unlock-fail",
    ),
)
def test_native_replacement_lease_faults_are_settled_exactly(
    tmp_path: Path,
    fault_mode: str,
) -> None:
    library = _compile_exchange_fault_shim(tmp_path)
    root = tmp_path / "authority"
    script = textwrap.dedent(
        """
        import ctypes
        import fcntl
        import os
        import sys
        import time
        from pathlib import Path

        import codenib._workspace_owner as workspace_owner

        root = Path(sys.argv[1])
        faults = ctypes.CDLL(sys.argv[2])
        mode = os.environ["CODENIB_EXCHANGE_FAULT"]
        for counter_name in (
            "codenib_exchange_fault_exchange_calls",
            "codenib_exchange_fault_unlock_failed",
            "codenib_exchange_fault_lock_rebind_injected",
            "codenib_exchange_fault_lock_delay_injected",
        ):
            getattr(faults, counter_name).restype = ctypes.c_int

        def counter(name):
            return getattr(faults, name)()

        def parent_lease_is_held():
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                return False
            finally:
                os.close(descriptor)

        destination = root / "published"
        destination.mkdir(parents=True)
        root.chmod(0o700)
        (destination / "incumbent.txt").write_bytes(b"incumbent")
        if mode in (
            "destination-rebind-after-lock",
            "destination-rebind-after-lock-unlock-fail",
        ):
            foreign = root / ".foreign"
            foreign.mkdir()
            (foreign / "foreign.txt").write_bytes(b"foreign")
        owner = workspace_owner.create_owner()
        workspace_owner.capture_owner_destination(
            owner,
            os.fsencode(root),
            b"published",
            time.monotonic_ns() + 10_000_000_000,
        )
        deadline = (
            time.monotonic_ns() + 20_000_000
            if mode == "deadline-after-lock-unlock-fail"
            else time.monotonic_ns() + 10_000_000_000
        )

        if mode == "deadline-after-lock-unlock-fail":
            try:
                workspace_owner.acquire_owner_replacement_lease(owner, deadline)
            except TimeoutError:
                pass
            else:
                raise AssertionError("post-lock lease deadline expiry was hidden")
            assert counter("codenib_exchange_fault_lock_delay_injected") == 1
            assert counter("codenib_exchange_fault_unlock_failed") == 1
            assert workspace_owner.owner_state(owner) == (
                "replacement-recovery-required"
            )
            assert parent_lease_is_held()
            assert not (root / ".replacement").exists()
            workspace_owner.abort_owner(owner)
        else:
            try:
                workspace_owner.acquire_owner_replacement_lease(owner, deadline)
            except RuntimeError as error:
                assert "changed before replacement lease" in str(error)
            else:
                raise AssertionError("under-lease destination rebind was accepted")
            assert counter("codenib_exchange_fault_lock_rebind_injected") == 1
            assert not (root / ".replacement").exists()
            if mode == "destination-rebind-after-lock-unlock-fail":
                assert counter("codenib_exchange_fault_unlock_failed") == 1
                assert workspace_owner.owner_state(owner) == (
                    "replacement-recovery-required"
                )
                assert parent_lease_is_held()
                workspace_owner.abort_owner(owner)
                assert not parent_lease_is_held()
                assert (destination / "foreign.txt").read_bytes() == b"foreign"
                assert (root / ".foreign" / "incumbent.txt").read_bytes() == (
                    b"incumbent"
                )
            else:
                assert workspace_owner.owner_state(owner) == "destination-captured"
                assert not parent_lease_is_held()
                retained_foreign = root / ".retained-foreign"
                (root / "published").rename(retained_foreign)
                (root / ".foreign").rename(root / "published")
                retained_foreign.rename(root / ".foreign")
                workspace_owner.abort_owner(owner)
                assert (root / ".foreign" / "foreign.txt").read_bytes() == (
                    b"foreign"
                )

        assert counter("codenib_exchange_fault_exchange_calls") == 0
        assert workspace_owner.owner_closed(owner)
        if mode != "destination-rebind-after-lock-unlock-fail":
            assert (destination / "incumbent.txt").read_bytes() == b"incumbent"
        """
    )
    _run_exchange_fault_script(root, library, fault_mode, script)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux")
@pytest.mark.parametrize(
    "fault_mode",
    (
        "success-without-swap",
        "error-after-swap",
        "forward-fsync-once",
        "reverse-error-after-restore",
        "reverse-error-once",
        "reverse-fsync-once",
        "reverse-success-without-restore",
        "second-lock-fail",
        "unlock-fail-once",
    ),
)
def test_native_exchange_faults_are_classified_and_settled_exactly(
    tmp_path: Path,
    fault_mode: str,
) -> None:
    library = _compile_exchange_fault_shim(tmp_path)
    root = tmp_path / "authority"
    script = textwrap.dedent(
        """
        import ctypes
        import errno
        import fcntl
        import os
        import sys
        import time
        from pathlib import Path

        import codenib._workspace_owner as workspace_owner

        root = Path(sys.argv[1])
        faults = ctypes.CDLL(sys.argv[2])
        mode = os.environ["CODENIB_EXCHANGE_FAULT"]
        for counter_name in (
            "codenib_exchange_fault_exchange_calls",
            "codenib_exchange_fault_fsync_failed",
            "codenib_exchange_fault_unlock_failed",
            "codenib_exchange_fault_post_exchange_fsyncs",
            "codenib_exchange_fault_lock_rebind_injected",
            "codenib_exchange_fault_lock_delay_injected",
            "codenib_exchange_fault_exclusive_lock_calls",
        ):
            getattr(faults, counter_name).restype = ctypes.c_int

        def counter(name):
            return getattr(faults, name)()

        def parent_lease_is_held():
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                return False
            finally:
                os.close(descriptor)

        destination = root / "published"
        destination.mkdir(parents=True)
        root.chmod(0o700)
        (destination / "incumbent.txt").write_bytes(b"incumbent")
        digest = b"0" * 64
        owner = workspace_owner.create_owner()
        workspace_owner.capture_owner_destination(
            owner,
            os.fsencode(root),
            b"published",
            time.monotonic_ns() + 10_000_000_000,
        )
        workspace_owner.acquire_owner_replacement_lease(
            owner,
            time.monotonic_ns() + 10_000_000_000,
        )
        permit = workspace_owner.claim_owner_replacement_permit(owner)
        workspace_owner.provision_owner_replacement(
            owner,
            b".replacement",
            digest,
            0o700,
            ((b"views", 0o700), (b"views/bm25", 0o700)),
            time.monotonic_ns() + 10_000_000_000,
        )
        workspace_owner.verify_owner_adoption_binding(
            owner,
            os.fsencode(destination),
            b".replacement",
            digest,
        )
        workspace_owner.mark_owner_adopted(owner)
        workspace_owner.begin_owner_file(
            owner, b"views/bm25", b"documents.json", 0o600
        )
        workspace_owner.write_owner_file(owner, b"candidate")
        workspace_owner.finish_owner_file(owner, 0o600)
        workspace_owner.seal_owner_directories(owner)
        deadline = time.monotonic_ns() + 10_000_000_000

        if mode == "success-without-swap":
            try:
                workspace_owner.exchange_owner_replacement(
                    permit, b".replacement", b"published", deadline
                )
            except RuntimeError as error:
                assert "success without swapping" in str(error)
            else:
                raise AssertionError("false exchange success was accepted")
            assert counter("codenib_exchange_fault_exchange_calls") == 1
            assert counter("codenib_exchange_fault_post_exchange_fsyncs") >= 1
            assert workspace_owner.owner_state(owner) == "replacement-adopted"
            assert parent_lease_is_held()
            workspace_owner.abort_owner(owner)
        elif mode == "error-after-swap":
            token = workspace_owner.exchange_owner_replacement(
                permit, b".replacement", b"published", deadline
            )
            assert counter("codenib_exchange_fault_exchange_calls") == 1
            assert workspace_owner.owner_state(owner) == (
                "replacement-exchanged-unreceipted"
            )
            workspace_owner.commit_owner_receipt(token)
            workspace_owner.close_owner_exact(owner)
        elif mode == "forward-fsync-once":
            try:
                workspace_owner.exchange_owner_replacement(
                    permit, b".replacement", b"published", deadline
                )
            except OSError as error:
                assert error.errno == errno.EIO
            else:
                raise AssertionError("forward fsync failure was hidden")
            assert counter("codenib_exchange_fault_exchange_calls") == 1
            assert counter("codenib_exchange_fault_fsync_failed") == 1
            assert workspace_owner.owner_state(owner) == (
                "replacement-recovery-required"
            )
            assert parent_lease_is_held()
            try:
                workspace_owner.verify_owner_replacement_binding(owner)
            except RuntimeError:
                pass
            else:
                raise AssertionError("recovery owner exposed ordinary verification")
            workspace_owner.abort_owner(owner)
        elif mode == "reverse-error-once":
            workspace_owner.exchange_owner_replacement(
                permit, b".replacement", b"published", deadline
            )
            try:
                workspace_owner.abort_owner(owner)
            except OSError as error:
                assert error.errno == errno.EIO
            else:
                raise AssertionError("reverse exchange error was hidden")
            assert counter("codenib_exchange_fault_exchange_calls") == 2
            assert workspace_owner.owner_state(owner) == (
                "replacement-recovery-required"
            )
            assert parent_lease_is_held()
            workspace_owner.abort_owner(owner)
            assert counter("codenib_exchange_fault_exchange_calls") == 3
        elif mode == "reverse-error-after-restore":
            workspace_owner.exchange_owner_replacement(
                permit, b".replacement", b"published", deadline
            )
            workspace_owner.abort_owner(owner)
            assert counter("codenib_exchange_fault_exchange_calls") == 2
            assert counter("codenib_exchange_fault_post_exchange_fsyncs") >= 2
        elif mode == "reverse-fsync-once":
            workspace_owner.exchange_owner_replacement(
                permit, b".replacement", b"published", deadline
            )
            try:
                workspace_owner.abort_owner(owner)
            except OSError as error:
                assert error.errno == errno.EIO
            else:
                raise AssertionError("reverse fsync error was hidden")
            assert counter("codenib_exchange_fault_exchange_calls") == 2
            assert counter("codenib_exchange_fault_fsync_failed") == 1
            assert workspace_owner.owner_state(owner) == (
                "replacement-recovery-required"
            )
            assert parent_lease_is_held()
            workspace_owner.abort_owner(owner)
            assert counter("codenib_exchange_fault_exchange_calls") == 2
        elif mode == "reverse-success-without-restore":
            workspace_owner.exchange_owner_replacement(
                permit, b".replacement", b"published", deadline
            )
            try:
                workspace_owner.abort_owner(owner)
            except OSError:
                pass
            else:
                raise AssertionError("false reverse success was accepted")
            assert counter("codenib_exchange_fault_exchange_calls") == 2
            assert workspace_owner.owner_state(owner) == (
                "replacement-recovery-required"
            )
            assert parent_lease_is_held()
            syncs_before_retry = counter(
                "codenib_exchange_fault_post_exchange_fsyncs"
            )
            temporary = root / ".restore-candidate"
            (root / "published").rename(temporary)
            (root / ".replacement").rename(root / "published")
            temporary.rename(root / ".replacement")
            workspace_owner.abort_owner(owner)
            assert counter("codenib_exchange_fault_exchange_calls") == 2
            assert (
                counter("codenib_exchange_fault_post_exchange_fsyncs")
                > syncs_before_retry
            )
        elif mode == "second-lock-fail":
            token = workspace_owner.exchange_owner_replacement(
                permit, b".replacement", b"published", deadline
            )
            assert counter("codenib_exchange_fault_exclusive_lock_calls") == 1
            workspace_owner.commit_owner_receipt(token)
            workspace_owner.close_owner_exact(owner)
        elif mode == "unlock-fail-once":
            token = workspace_owner.exchange_owner_replacement(
                permit, b".replacement", b"published", deadline
            )
            try:
                workspace_owner.commit_owner_receipt(token)
            except OSError as error:
                assert error.errno == errno.EIO
            else:
                raise AssertionError("receipt unlock failure was hidden")
            assert counter("codenib_exchange_fault_unlock_failed") == 1
            assert workspace_owner.owner_state(owner) == (
                "replacement-recovery-required"
            )
            assert parent_lease_is_held()
            syncs_before_retry = counter(
                "codenib_exchange_fault_post_exchange_fsyncs"
            )
            workspace_owner.commit_owner_receipt(token)
            assert counter("codenib_exchange_fault_unlock_failed") == 1
            assert (
                counter("codenib_exchange_fault_post_exchange_fsyncs")
                > syncs_before_retry
            )
            assert workspace_owner.owner_state(owner) == "replacement-receipted"
            assert not parent_lease_is_held()
            workspace_owner.close_owner_exact(owner)
        else:
            raise AssertionError(f"unexpected fault mode: {mode}")

        assert workspace_owner.owner_closed(owner)
        if mode in ("error-after-swap", "second-lock-fail", "unlock-fail-once"):
            assert not (destination / "incumbent.txt").exists()
            assert (
                destination / "views/bm25/documents.json"
            ).read_bytes() == b"candidate"
            assert (
                root / ".replacement" / "incumbent.txt"
            ).read_bytes() == b"incumbent"
        else:
            assert (destination / "incumbent.txt").read_bytes() == b"incumbent"
            assert (
                root / ".replacement" / "views/bm25/documents.json"
            ).read_bytes() == b"candidate"
        """
    )
    _run_exchange_fault_script(root, library, fault_mode, script)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux")
def test_native_unsupported_exchange_preserves_error_during_last_owner_dealloc(
    tmp_path: Path,
) -> None:
    library = _compile_exchange_fault_shim(tmp_path)
    root = tmp_path / "authority"
    script = textwrap.dedent(
        """
        import ctypes
        import errno
        import os
        import sys
        import time
        from pathlib import Path

        import codenib._workspace_owner as workspace_owner

        root = Path(sys.argv[1])
        faults = ctypes.CDLL(sys.argv[2])
        faults.codenib_exchange_fault_exchange_calls.restype = ctypes.c_int
        destination = root / "published"
        destination.mkdir(parents=True)
        root.chmod(0o700)
        (destination / "incumbent.txt").write_bytes(b"incumbent")
        owner = workspace_owner.create_owner()
        workspace_owner.capture_owner_destination(
            owner,
            os.fsencode(root),
            b"published",
            time.monotonic_ns() + 10_000_000_000,
        )
        workspace_owner.acquire_owner_replacement_lease(
            owner,
            time.monotonic_ns() + 10_000_000_000,
        )
        permit = workspace_owner.claim_owner_replacement_permit(owner)
        workspace_owner.provision_owner_replacement(
            owner,
            b".replacement",
            b"0" * 64,
            0o700,
            (),
            time.monotonic_ns() + 10_000_000_000,
        )
        workspace_owner.verify_owner_adoption_binding(
            owner,
            os.fsencode(destination),
            b".replacement",
            b"0" * 64,
        )
        workspace_owner.mark_owner_adopted(owner)
        workspace_owner.seal_owner_directories(owner)
        holder = [permit]
        del permit
        del owner
        exact_exchange = workspace_owner._exchange_owner_replacement_exact
        assert exact_exchange is not None

        try:
            exact_exchange(
                holder.pop(),
                b".replacement",
                b"published",
                time.monotonic_ns() + 10_000_000_000,
            )
        except OSError as error:
            assert error.errno == errno.EOPNOTSUPP
        else:
            raise AssertionError("unsupported exchange did not raise OSError")

        assert faults.codenib_exchange_fault_exchange_calls() == 1
        assert not holder
        assert (destination / "incumbent.txt").read_bytes() == b"incumbent"
        assert (root / ".replacement").is_dir()
        """
    )
    _run_exchange_fault_script(root, library, "unsupported", script)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux")
def test_native_partial_replacement_identity_failure_never_adopts_rebound_slot(
    tmp_path: Path,
) -> None:
    library = _compile_exchange_fault_shim(tmp_path)
    root = tmp_path / "authority"
    script = textwrap.dedent(
        """
        import ctypes
        import fcntl
        import gc
        import os
        import sys
        import time
        from pathlib import Path

        import codenib._workspace_owner as workspace_owner

        root = Path(sys.argv[1])
        faults = ctypes.CDLL(sys.argv[2])
        faults.codenib_exchange_fault_slot_stats.restype = ctypes.c_int
        destination = root / "published"
        destination.mkdir(parents=True)
        root.chmod(0o700)
        (destination / "incumbent.txt").write_bytes(b"incumbent")
        owner = workspace_owner.create_owner()
        workspace_owner.capture_owner_destination(
            owner,
            os.fsencode(root),
            b"published",
            time.monotonic_ns() + 10_000_000_000,
        )
        workspace_owner.acquire_owner_replacement_lease(
            owner,
            time.monotonic_ns() + 10_000_000_000,
        )
        permit = workspace_owner.claim_owner_replacement_permit(owner)
        try:
            workspace_owner.provision_owner_replacement(
                owner,
                b".replacement",
                b"0" * 64,
                0o700,
                (),
                time.monotonic_ns() + 10_000_000_000,
            )
        except OSError:
            pass
        else:
            raise AssertionError("candidate identity fault was hidden")
        assert faults.codenib_exchange_fault_slot_stats() == 2
        assert workspace_owner.owner_state(owner) == "replacement-provisioning"

        for operation in (
            lambda: workspace_owner.verify_owner_authority(owner),
            lambda: workspace_owner.verify_owner_replacement_binding(owner),
            lambda: workspace_owner.borrow_owner_parent_descriptor(owner),
            lambda: workspace_owner.borrow_owner_root_descriptor(owner),
            lambda: workspace_owner.borrow_owner_destination_descriptor(owner),
            lambda: workspace_owner.borrow_owner_directory_descriptor(
                owner, b"views"
            ),
            lambda: workspace_owner.begin_owner_file(
                owner, b"", b"unexpected", 0o600
            ),
            lambda: workspace_owner.abort_owner_file(owner),
            lambda: workspace_owner.seal_owner_directories(owner),
            lambda: workspace_owner.sync_owner_parent(owner),
            lambda: workspace_owner.mark_owner_adopted(owner),
            lambda: workspace_owner.quarantine_owner(owner),
        ):
            try:
                operation()
            except (RuntimeError, KeyError):
                pass
            else:
                raise AssertionError("partial replacement exposed an operation")
            assert workspace_owner.owner_state(owner) == (
                "replacement-provisioning"
            )

        slot = root / ".replacement"
        retained = root / ".retained-candidate"
        slot.rename(retained)
        slot.mkdir()
        (slot / "foreign.txt").write_bytes(b"foreign")
        for operation in (
            lambda: workspace_owner.abort_owner(owner),
            lambda: workspace_owner.close_owner_exact(owner),
        ):
            try:
                operation()
            except OSError:
                pass
            else:
                raise AssertionError("unknown candidate identity was settled")
            assert workspace_owner.owner_state(owner) == (
                "replacement-recovery-required"
            )
            assert (slot / "foreign.txt").read_bytes() == b"foreign"
            assert retained.is_dir()

        independent = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            try:
                fcntl.flock(independent, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise AssertionError("unknown candidate released its lease")
        finally:
            os.close(independent)

        holder = [permit]
        del permit
        del owner
        gc.collect()
        try:
            workspace_owner._exchange_owner_replacement_exact(
                holder.pop(),
                b".replacement",
                b"published",
                time.monotonic_ns() + 10_000_000_000,
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("recovery permit unexpectedly exchanged")
        assert not holder
        assert (slot / "foreign.txt").read_bytes() == b"foreign"
        assert retained.is_dir()
        """
    )
    _run_exchange_fault_script(root, library, "provision-identity-fail", script)
    assert (root / ".replacement" / "foreign.txt").read_bytes() == b"foreign"
    assert (root / ".retained-candidate").is_dir()
    assert (root / "published" / "incumbent.txt").read_bytes() == b"incumbent"


@pytest.mark.parametrize("rebind", ("destination", "replacement-slot"))
def test_native_replacement_receipt_recovery_retries_only_exact_exchanged_mapping(
    tmp_path: Path,
    rebind: str,
) -> None:
    root = tmp_path / "authority"
    destination = root / "published"
    destination.mkdir(parents=True)
    root.chmod(0o700)
    (destination / "incumbent.txt").write_bytes(b"incumbent")
    owner, plan, permit, _incumbent_descriptor = _provision_replacement(root)
    _adopt_and_fill_replacement(root, owner, plan, payload=b"candidate")
    receipt_token = workspace_owner.exchange_owner_replacement(
        permit,
        b".replacement",
        b"published",
        time.monotonic_ns() + 10_000_000_000,
    )
    slot = root / ".replacement"
    rebound = destination if rebind == "destination" else slot
    retained = root / f".retained-receipt-{rebind}"
    foreign = root / f".foreign-receipt-{rebind}"
    rebound.rename(retained)
    rebound.mkdir()
    (rebound / "foreign.txt").write_bytes(b"foreign")

    with pytest.raises(RuntimeError, match="binding changed"):
        workspace_owner.commit_owner_receipt(receipt_token)
    assert workspace_owner.owner_state(owner) == "replacement-recovery-required"
    independent_parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(independent_parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(independent_parent)

    rebound.rename(foreign)
    retained.rename(rebound)
    workspace_owner.commit_owner_receipt(receipt_token)
    assert workspace_owner.owner_state(owner) == "replacement-receipted"
    workspace_owner.close_owner_exact(owner)
    assert (foreign / "foreign.txt").read_bytes() == b"foreign"
    assert (destination / "views/bm25/documents.json").read_bytes() == b"candidate"
    assert (slot / "incumbent.txt").read_bytes() == b"incumbent"
