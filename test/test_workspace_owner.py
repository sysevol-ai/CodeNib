# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import ctypes.util
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
            #include <sys/syscall.h>
            #include <unistd.h>

            #ifndef RENAME_EXCHANGE
            #define RENAME_EXCHANGE (1U << 1)
            #endif

            typedef long (*syscall_function)(long, ...);
            typedef int (*fsync_function)(int);
            typedef int (*flock_function)(int, int);

            static syscall_function real_syscall;
            static fsync_function real_fsync;
            static flock_function real_flock;
            static int exchange_calls;
            static int exchange_live;
            static int fsync_failed;
            static int unlock_failed;
            static int slot_stats;
            static int post_exchange_fsyncs;
            static int lock_rebind_injected;
            static int lock_delay_injected;

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
                if (strcmp(mode, "provision-identity-fail") == 0 &&
                    second != 0 &&
                    strcmp((const char *)second, ".replacement") == 0 &&
                    ++slot_stats == 2) {
                  errno = EIO;
                  return -1;
                }
                return real_syscall(number, first, second, third, fourth);
              }
              fifth = va_arg(arguments, long);
              if (number == SYS_kcmp) {
                va_end(arguments);
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

            int flock(int descriptor, int operation) {
              int result;
              resolve_symbols();
              if (real_flock == NULL) {
                errno = ENOSYS;
                return -1;
              }
              if (!unlock_failed && exchange_live && operation == LOCK_UN &&
                  strcmp(fault_mode(), "unlock-fail-once") == 0) {
                unlock_failed = 1;
                errno = EIO;
                return -1;
              }
              if (!unlock_failed && operation == LOCK_UN &&
                  strcmp(fault_mode(),
                         "deadline-after-lock-unlock-fail") == 0) {
                unlock_failed = 1;
                errno = EIO;
                return -1;
              }
              result = real_flock(descriptor, operation);
              if (result == 0 && !lock_delay_injected &&
                  operation == (LOCK_EX | LOCK_NB) &&
                  strcmp(fault_mode(),
                         "deadline-after-lock-unlock-fail") == 0) {
                lock_delay_injected = 1;
                usleep(100000);
              }
              if (result == 0 && !lock_rebind_injected &&
                  operation == (LOCK_EX | LOCK_NB) &&
                  strcmp(fault_mode(), "slot-rebind-after-lock") == 0) {
                if (real_syscall(SYS_renameat2, descriptor, ".replacement",
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


def test_workspace_owner_facade_rejects_symbol_complete_protocol_v3() -> None:
    required_symbols = (
        "require_support",
        "create_owner_exact",
        "claim_owner_publish_permit_exact",
        "claim_owner_replacement_permit_exact",
        "require_owner_exact",
        "close_owner_exact",
        "provision_owner_exact",
        "capture_owner_destination_exact",
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
        implementation.workspace_owner_protocol_version = 3
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
            raise AssertionError("protocol-v3 implementation was accepted")
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


def test_workspace_owner_facade_rejects_each_incomplete_protocol_v4_abi() -> None:
    required_symbols = (
        "require_support",
        "create_owner_exact",
        "claim_owner_publish_permit_exact",
        "claim_owner_replacement_permit_exact",
        "require_owner_exact",
        "close_owner_exact",
        "provision_owner_exact",
        "capture_owner_destination_exact",
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
            implementation.workspace_owner_protocol_version = 4
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
                raise AssertionError(f"incomplete protocol-v4 ABI accepted: {{missing}}")
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
        assert workspace_owner.owner_state(owner) == "destination-captured"
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
        expected_destination=None,
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
        expected_destination=None,
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
        expected_destination=None,
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
        expected_destination=None,
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
        expected_destination=None,
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
        expected_destination=None,
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
        expected_destination=None,
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
        expected_destination=None,
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
        expected_destination=None,
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

    owner = _capture_existing_destination(root, b"nested/published")

    assert workspace_owner.owner_state(owner) == "destination-captured"
    assert not workspace_owner.owner_closed(owner)
    assert workspace_owner.require_exact_owner(owner) is owner
    assert workspace_owner.verify_owner_authority(owner) is None
    assert workspace_owner.verify_owner_destination_binding(owner) is None
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
        workspace_owner.verify_owner_destination_binding(candidate)
    with pytest.raises(TypeError, match="invalid native type"):
        workspace_owner.borrow_owner_destination_descriptor(candidate)


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
        lambda: workspace_owner.borrow_owner_parent_descriptor(owner),
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

    receipt_token = workspace_owner.exchange_owner_replacement(
        permit,
        b".replacement",
        b"published",
        time.monotonic_ns() + 10_000_000_000,
    )

    assert workspace_owner.owner_state(owner) == ("replacement-exchanged-unreceipted")
    assert workspace_owner.verify_owner_replacement_binding(owner) is None
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
        assert workspace_owner.owner_state(owner) == "destination-captured"
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
    (root / "first").mkdir(parents=True)
    (root / "second").mkdir()
    root.chmod(0o700)
    first_owner = _capture_existing_destination(root, b"first")
    second_owner = _capture_existing_destination(root, b"second")
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
        os.fsencode(root / "second"),
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
    assert workspace_owner.owner_state(first_owner) == "destination-captured"
    assert not (root / ".replacement").exists()
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
    first_owner, first_plan, first_permit, _ = _provision_replacement(
        root,
        destination=b"first",
        slot=b".first-replacement",
    )
    second_owner, second_plan, second_permit, _ = _provision_replacement(
        root,
        destination=b"second",
        slot=b".second-replacement",
    )
    _adopt_and_fill_replacement(
        root,
        first_owner,
        first_plan,
        destination=b"first",
        slot=b".first-replacement",
        payload=b"first-new",
    )
    _adopt_and_fill_replacement(
        root,
        second_owner,
        second_plan,
        destination=b"second",
        slot=b".second-replacement",
        payload=b"second-new",
    )
    first_token = workspace_owner.exchange_owner_replacement(
        first_permit,
        b".first-replacement",
        b"first",
        time.monotonic_ns() + 10_000_000_000,
    )

    with pytest.raises(BlockingIOError):
        workspace_owner.exchange_owner_replacement(
            second_permit,
            b".second-replacement",
            b"second",
            time.monotonic_ns() + 10_000_000_000,
        )
    assert workspace_owner.owner_state(second_owner) == "replacement-adopted"
    with pytest.raises(BlockingIOError):
        workspace_owner.abort_owner(second_owner)

    workspace_owner.commit_owner_receipt(first_token)
    workspace_owner.close_owner_exact(first_owner)
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

    second_owner, second_plan, second_permit, _ = _provision_replacement(
        root,
        destination=b"second",
        slot=b".second-replacement",
    )
    _adopt_and_fill_replacement(
        root,
        second_owner,
        second_plan,
        destination=b"second",
        slot=b".second-replacement",
        payload=b"second-new",
    )
    before_cleanup = _tree_fingerprint(root)
    with pytest.raises(BlockingIOError):
        workspace_owner.exchange_owner_replacement(
            second_permit,
            b".second-replacement",
            b"second",
            time.monotonic_ns() + 10_000_000_000,
        )
    with pytest.raises(BlockingIOError):
        workspace_owner.abort_owner(second_owner)
    with pytest.raises(BlockingIOError):
        workspace_owner.close_owner_exact(second_owner)
    assert not workspace_owner.owner_closed(second_owner)
    assert workspace_owner.owner_state(second_owner) == "replacement-adopted"
    assert _tree_fingerprint(root) == before_cleanup

    sentinel = KeyboardInterrupt("sentinel")
    try:
        raise sentinel
    except BaseException:  # noqa: B036 - assert dealloc preserves the exception
        del second_permit
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
    first_owner, first_plan, first_permit, _ = _provision_replacement(
        root,
        slot=b".first-replacement",
    )
    second_owner, second_plan, second_permit, _ = _provision_replacement(
        root,
        slot=b".second-replacement",
    )
    _adopt_and_fill_replacement(
        root,
        first_owner,
        first_plan,
        slot=b".first-replacement",
        payload=b"first-candidate",
    )
    _adopt_and_fill_replacement(
        root,
        second_owner,
        second_plan,
        slot=b".second-replacement",
        payload=b"second-candidate",
    )
    first_token = workspace_owner.exchange_owner_replacement(
        first_permit,
        b".first-replacement",
        b"published",
        time.monotonic_ns() + 10_000_000_000,
    )
    workspace_owner.commit_owner_receipt(first_token)

    with pytest.raises(RuntimeError, match="not exchange-ready"):
        workspace_owner.exchange_owner_replacement(
            second_permit,
            b".second-replacement",
            b"published",
            time.monotonic_ns() + 10_000_000_000,
        )
    assert workspace_owner.owner_state(second_owner) == "replacement-adopted"
    with pytest.raises(OSError):
        workspace_owner.abort_owner(second_owner)
    assert workspace_owner.owner_state(second_owner) == (
        "replacement-recovery-required"
    )
    assert (destination / "views/bm25/documents.json").read_bytes() == (
        b"first-candidate"
    )
    assert (
        root / ".second-replacement" / "views/bm25/documents.json"
    ).read_bytes() == b"second-candidate"
    independent_parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(independent_parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(independent_parent)

    temporary = root / ".restore-first-candidate"
    destination.rename(temporary)
    (root / ".first-replacement").rename(destination)
    temporary.rename(root / ".first-replacement")
    workspace_owner.abort_owner(second_owner)
    workspace_owner.close_owner_exact(first_owner)


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
        "success-without-swap",
        "error-after-swap",
        "forward-fsync-once",
        "reverse-error-after-restore",
        "reverse-error-once",
        "reverse-fsync-once",
        "reverse-success-without-restore",
        "slot-rebind-after-lock",
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
        if mode == "slot-rebind-after-lock":
            foreign = root / ".foreign"
            foreign.mkdir()
            (foreign / "foreign.txt").write_bytes(b"foreign")
        if mode == "deadline-after-lock-unlock-fail":
            deadline = time.monotonic_ns() + 20_000_000
        else:
            deadline = time.monotonic_ns() + 10_000_000_000

        if mode == "deadline-after-lock-unlock-fail":
            try:
                workspace_owner.exchange_owner_replacement(
                    permit, b".replacement", b"published", deadline
                )
            except TimeoutError:
                pass
            else:
                raise AssertionError("post-lock deadline expiry was hidden")
            assert counter("codenib_exchange_fault_lock_delay_injected") == 1
            assert counter("codenib_exchange_fault_unlock_failed") == 1
            assert counter("codenib_exchange_fault_exchange_calls") == 0
            assert workspace_owner.owner_state(owner) == (
                "replacement-recovery-required"
            )
            assert parent_lease_is_held()
            workspace_owner.abort_owner(owner)
        elif mode == "success-without-swap":
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
            assert not parent_lease_is_held()
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
        elif mode == "slot-rebind-after-lock":
            try:
                workspace_owner.exchange_owner_replacement(
                    permit, b".replacement", b"published", deadline
                )
            except RuntimeError as error:
                assert "binding changed under" in str(error)
            else:
                raise AssertionError("under-lease slot rebind was accepted")
            assert counter("codenib_exchange_fault_lock_rebind_injected") == 1
            assert counter("codenib_exchange_fault_exchange_calls") == 0
            assert workspace_owner.owner_state(owner) == (
                "replacement-recovery-required"
            )
            assert parent_lease_is_held()
            retained_foreign = root / ".retained-foreign"
            (root / ".replacement").rename(retained_foreign)
            (root / ".foreign").rename(root / ".replacement")
            retained_foreign.rename(root / ".foreign")
            workspace_owner.abort_owner(owner)
            assert (root / ".foreign" / "foreign.txt").read_bytes() == b"foreign"
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
        if mode in ("error-after-swap", "unlock-fail-once"):
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
