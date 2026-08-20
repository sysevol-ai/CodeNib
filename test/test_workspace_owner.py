# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import ctypes.util
import gc
import hashlib
import os
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


def test_workspace_owner_facade_rejects_symbol_complete_protocol_v1() -> None:
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
        implementation.workspace_owner_protocol_version = 1
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
            raise AssertionError("protocol-v1 implementation was accepted")
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
