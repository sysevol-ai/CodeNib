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


def test_workspace_owner_facade_rejects_each_incomplete_protocol_v3_abi() -> None:
    required_symbols = (
        "require_support",
        "create_owner_exact",
        "claim_owner_publish_permit_exact",
        "require_owner_exact",
        "close_owner_exact",
        "provision_owner_exact",
        "capture_owner_destination_exact",
        "verify_owner_authority_exact",
        "verify_owner_adoption_binding_exact",
        "verify_owner_destination_binding_exact",
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
            implementation.workspace_owner_protocol_version = 3
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
                raise AssertionError(f"incomplete protocol-v3 ABI accepted: {{missing}}")
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
