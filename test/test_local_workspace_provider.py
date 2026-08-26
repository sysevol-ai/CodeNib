# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import errno
import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import codenib
import codenib._local_workspace_provider as local_provider_module
import codenib._workspace_owner as workspace_owner
import codenib._workspace_provider as workspace_provider_module
from codenib._atomic_directory import publication_parent_identity
from codenib._captured_directory import (
    PublishedWorkspaceDestinationBinding,
    PublishedWorkspaceReceiptOwner,
    UnsupportedWorkspaceCreation,
    WorkspaceDirectory,
    WorkspaceFile,
    WorkspacePlan,
)
from codenib._local_workspace_provider import LocalWorkspaceProvider
from codenib._workspace_provider import (
    StrictWorkspaceRequest,
    StrictWorkspaceSession,
    run_strict_workspace,
)


def _require_native_provider(root: Path) -> LocalWorkspaceProvider:
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    provider = LocalWorkspaceProvider(root)
    try:
        provider.require_support()
    except UnsupportedWorkspaceCreation as error:
        pytest.skip(f"native local workspace provider is unavailable: {error}")
    return provider


def _plan() -> WorkspacePlan:
    return WorkspacePlan(
        subject_digest=hashlib.sha256(b"local-workspace-provider").hexdigest(),
        directories=(WorkspaceDirectory(Path("data")),),
        files=(
            WorkspaceFile(
                Path("data/result.json"),
                mode=0o600,
                max_bytes=1 << 20,
            ),
        ),
    )


def _request(
    root: Path,
    *,
    destination_binding: PublishedWorkspaceDestinationBinding | None = None,
) -> StrictWorkspaceRequest:
    return StrictWorkspaceRequest(
        purpose="test-local-workspace-provider",
        destination=root / "published",
        plan=_plan(),
        destination_binding=destination_binding,
    )


def _write_and_publish(session: StrictWorkspaceSession, payload: bytes) -> object:
    record = session.write_file("data/result.json", (payload,))
    return session.publish_validated(
        lambda reader: (record, reader.capture_ownership(allow_empty_root=True))
    )


def test_local_provider_and_receipt_owner_are_public_lazy_exports() -> None:
    assert codenib.LocalWorkspaceProvider is LocalWorkspaceProvider
    assert codenib.PublishedWorkspaceReceiptOwner is PublishedWorkspaceReceiptOwner


def test_local_provider_publishes_and_transfers_one_receipt(tmp_path: Path) -> None:
    root = tmp_path / "authority"
    provider = _require_native_provider(root)
    request = _request(root)
    receipt_owner = PublishedWorkspaceReceiptOwner()
    payload = b'{"result":"ok"}'

    record, ownership = run_strict_workspace(
        provider,
        request,
        receipt_owner=receipt_owner,
        operation=lambda session: _write_and_publish(session, payload),
    )

    assert record.sha256 == hashlib.sha256(payload).hexdigest()
    assert ownership.file_records == (record,)
    assert receipt_owner.active
    assert (request.destination / "data/result.json").read_bytes() == payload

    receipt_owner.close()

    assert receipt_owner.closed
    assert (request.destination / "data/result.json").read_bytes() == payload


def test_local_provider_aborts_an_exact_prepublish_error(tmp_path: Path) -> None:
    root = tmp_path / "authority"
    provider = _require_native_provider(root)
    request = _request(root)
    receipt_owner = PublishedWorkspaceReceiptOwner()
    error = RuntimeError("injected prepublish failure")

    def fail(session: StrictWorkspaceSession) -> None:
        session.write_file("data/result.json", (b"partial",))
        raise error

    with pytest.raises(RuntimeError) as caught:
        run_strict_workspace(
            provider,
            request,
            receipt_owner=receipt_owner,
            operation=fail,
        )

    assert caught.value is error
    assert receipt_owner.state == "empty"
    assert not request.destination.exists()
    assert not tuple(root.glob(".codenib-workspace-stage-*"))
    assert tuple(root.glob(".codenib-workspace-orphan-*"))


def test_local_provider_retains_owner_when_native_cleanup_cannot_authenticate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "authority"
    provider = _require_native_provider(root)
    request = _request(root)
    receipt_owner = PublishedWorkspaceReceiptOwner()
    primary_error = RuntimeError("injected operation failure")
    cleanup_error = PermissionError(errno.EPERM, "injected OFD comparison denial")
    real_abort = workspace_owner.abort_owner

    def reject_abort(_owner: object) -> None:
        raise cleanup_error

    monkeypatch.setattr(
        local_provider_module._workspace_owner,
        "abort_owner",
        reject_abort,
    )

    def fail(_session: StrictWorkspaceSession) -> None:
        raise primary_error

    with pytest.raises(RuntimeError) as caught:
        run_strict_workspace(
            provider,
            request,
            receipt_owner=receipt_owner,
            operation=fail,
        )

    assert caught.value is primary_error
    cleanup_owners = getattr(primary_error, "publication_cleanup_owners", ())
    assert len(cleanup_owners) == 1
    cleanup_owner = cleanup_owners[0]
    assert type(cleanup_owner).__name__ == "_ProviderWorkspaceCleanupOwner"
    assert not cleanup_owner.closed
    monkeypatch.setattr(
        local_provider_module._workspace_owner,
        "abort_owner",
        real_abort,
    )
    cleanup_owner.close()
    assert cleanup_owner.closed
    assert receipt_owner.state == "empty"
    assert not request.destination.exists()
    assert not tuple(root.glob(".codenib-workspace-stage-*"))
    assert tuple(root.glob(".codenib-workspace-orphan-*"))


def test_local_provider_propagates_exact_cancellation_into_provisioning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "authority"
    provider = _require_native_provider(root)
    request = _request(root)
    receipt_owner = PublishedWorkspaceReceiptOwner()
    cancellation = KeyboardInterrupt("cancel during local provisioning")
    armed = False
    forwarded: list[object] = []
    operation_calls = 0

    def check_cancelled() -> None:
        if armed:
            raise cancellation

    def stop_during_provision(*_args: object, **kwargs: object) -> None:
        nonlocal armed
        callback = kwargs.pop("check_cancelled")
        assert kwargs == {}
        assert callable(callback)
        forwarded.append(callback)
        armed = True
        callback()

    def forbidden_operation(_session: StrictWorkspaceSession) -> None:
        nonlocal operation_calls
        operation_calls += 1

    monkeypatch.setattr(
        workspace_owner,
        "provision_owner",
        stop_during_provision,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        run_strict_workspace(
            provider,
            request,
            receipt_owner=receipt_owner,
            operation=forbidden_operation,
            check_cancelled=check_cancelled,
        )

    assert caught.value is cancellation
    assert forwarded == [check_cancelled]
    assert operation_calls == 0
    assert receipt_owner.state == "empty"
    assert tuple(root.iterdir()) == ()
    receipt_owner.close()
    assert receipt_owner.closed


def test_local_provider_propagates_cancellation_into_native_adoption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "authority"
    provider = _require_native_provider(root)
    request = _request(root)
    receipt_owner = PublishedWorkspaceReceiptOwner()
    cancellation = KeyboardInterrupt("cancel during local adoption")
    real_provision = workspace_owner.provision_owner
    armed = False
    operation_calls = 0

    def check_cancelled() -> None:
        if armed:
            raise cancellation

    def provision_then_stop(*args: object, **kwargs: object) -> None:
        nonlocal armed
        assert kwargs.get("check_cancelled") is check_cancelled
        real_provision(*args, **kwargs)
        armed = True

    def forbidden_operation(_session: StrictWorkspaceSession) -> None:
        nonlocal operation_calls
        operation_calls += 1

    monkeypatch.setattr(
        workspace_owner,
        "provision_owner",
        provision_then_stop,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        run_strict_workspace(
            provider,
            request,
            receipt_owner=receipt_owner,
            operation=forbidden_operation,
            check_cancelled=check_cancelled,
        )

    assert caught.value is cancellation
    assert operation_calls == 0
    assert receipt_owner.state == "empty"
    assert len(tuple(root.glob(".codenib-workspace-orphan-*"))) == 1
    receipt_owner.close()
    assert receipt_owner.closed


def test_local_adoption_settles_native_owner_after_plan_poll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "authority"
    provider = _require_native_provider(root)
    base_plan = _plan()
    plan = WorkspacePlan(
        subject_digest=base_plan.subject_digest,
        directories=base_plan.directories,
        files=(
            *base_plan.files,
            WorkspaceFile("data/second.json", max_bytes=1),
        ),
    )
    request = StrictWorkspaceRequest(
        purpose="test-local-workspace-provider",
        destination=root / "published",
        plan=plan,
    )
    receipt_owner = PublishedWorkspaceReceiptOwner()
    cancellation = RuntimeError("cancel after native ownership handoff")
    workspaces: list[object] = []
    real_adopt = local_provider_module.OwnedWorkspaceAuthority._adopt_locked

    def observe_adopt(workspace, *args: object, **kwargs: object) -> None:
        workspaces.append(workspace)
        real_adopt(workspace, *args, **kwargs)

    def check_cancelled() -> None:
        if workspaces and workspaces[-1].state == "adopting":
            raise cancellation

    monkeypatch.setattr(
        local_provider_module.OwnedWorkspaceAuthority,
        "_adopt_locked",
        observe_adopt,
    )

    with pytest.raises(RuntimeError) as caught:
        run_strict_workspace(
            provider,
            request,
            receipt_owner=receipt_owner,
            operation=lambda _session: None,
            check_cancelled=check_cancelled,
        )

    assert caught.value is cancellation
    assert len(workspaces) == 1
    assert workspaces[0].state == "closed"
    assert receipt_owner.state == "empty"
    assert not request.destination.exists()
    assert len(tuple(root.glob(".codenib-workspace-orphan-*"))) == 1
    receipt_owner.close()


def test_local_provider_keeps_a_postpublish_receipt_active(tmp_path: Path) -> None:
    root = tmp_path / "authority"
    provider = _require_native_provider(root)
    request = _request(root)
    receipt_owner = PublishedWorkspaceReceiptOwner()
    error = KeyboardInterrupt("injected postpublish cancellation")

    def publish_then_fail(session: StrictWorkspaceSession) -> None:
        _write_and_publish(session, b"committed")
        raise error

    with pytest.raises(KeyboardInterrupt) as caught:
        run_strict_workspace(
            provider,
            request,
            receipt_owner=receipt_owner,
            operation=publish_then_fail,
        )

    assert caught.value is error
    assert receipt_owner.active
    assert (request.destination / "data/result.json").read_bytes() == b"committed"
    receipt_owner.close()
    assert receipt_owner.closed


def test_local_provider_rejects_outside_root_before_mutation(tmp_path: Path) -> None:
    root = tmp_path / "authority"
    provider = _require_native_provider(root)
    request = StrictWorkspaceRequest(
        purpose="test-local-workspace-provider",
        destination=tmp_path / "outside",
        plan=_plan(),
    )
    receipt_owner = PublishedWorkspaceReceiptOwner()

    with pytest.raises(ValueError, match="outside the provider root"):
        run_strict_workspace(
            provider,
            request,
            receipt_owner=receipt_owner,
            operation=lambda session: _write_and_publish(session, b"forbidden"),
        )

    assert tuple(root.iterdir()) == ()
    assert not request.destination.exists()
    assert receipt_owner.state == "empty"


def test_local_provider_native_policy_recheck_rejects_late_root_widening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "authority"
    provider = _require_native_provider(root)
    request = _request(root)
    receipt_owner = PublishedWorkspaceReceiptOwner()
    real_provision = workspace_owner.provision_owner

    def widen_root_then_provision(*args: object, **kwargs: object) -> None:
        root.chmod(0o755)
        real_provision(*args, **kwargs)

    monkeypatch.setattr(
        local_provider_module._workspace_owner,
        "provision_owner",
        widen_root_then_provision,
    )
    try:
        with pytest.raises(PermissionError):
            run_strict_workspace(
                provider,
                request,
                receipt_owner=receipt_owner,
                operation=lambda session: _write_and_publish(session, b"forbidden"),
            )
    finally:
        root.chmod(0o700)

    assert receipt_owner.state == "empty"
    assert tuple(root.iterdir()) == ()
    assert not request.destination.exists()


def test_local_provider_binds_native_parent_to_expected_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    provider = _require_native_provider(root)
    request = _request(root)
    receipt_owner = PublishedWorkspaceReceiptOwner()
    operation_called = False
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        expected = publication_parent_identity(descriptor)
    finally:
        os.close(descriptor)
    wrong = expected[:1] + (expected[1] + 1,) + expected[2:]

    def operation(session: StrictWorkspaceSession) -> object:
        nonlocal operation_called
        operation_called = True
        return _write_and_publish(session, b"forbidden")

    class BoundProvider:
        def require_support(self) -> None:
            provider.require_support()

        def run_workspace(
            self,
            bound_request: StrictWorkspaceRequest,
            *,
            receipt_owner: PublishedWorkspaceReceiptOwner,
            operation,
        ) -> object:
            return provider.run_workspace(
                bound_request,
                receipt_owner=receipt_owner,
                operation=operation,
                _expected_parent_identity=wrong,
            )

    with pytest.raises(
        RuntimeError,
        match="native workspace parent differs from retained authority",
    ):
        run_strict_workspace(
            BoundProvider(),
            request,
            receipt_owner=receipt_owner,
            operation=operation,
        )

    assert not operation_called
    assert receipt_owner.state == "empty"
    assert not request.destination.exists()
    assert not tuple(root.glob(".codenib-workspace-stage-*"))


def test_local_provider_accepts_matching_expected_parent_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    provider = _require_native_provider(root)
    request = _request(root)
    receipt_owner = PublishedWorkspaceReceiptOwner()
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        expected = publication_parent_identity(descriptor)
    finally:
        os.close(descriptor)

    class BoundProvider:
        def require_support(self) -> None:
            provider.require_support()

        def run_workspace(
            self,
            bound_request: StrictWorkspaceRequest,
            *,
            receipt_owner: PublishedWorkspaceReceiptOwner,
            operation,
        ) -> object:
            return provider.run_workspace(
                bound_request,
                receipt_owner=receipt_owner,
                operation=operation,
                _expected_parent_identity=expected,
            )

    run_strict_workspace(
        BoundProvider(),
        request,
        receipt_owner=receipt_owner,
        operation=lambda session: _write_and_publish(session, b"bound"),
    )

    assert (request.destination / "data/result.json").read_bytes() == b"bound"
    receipt_owner.close()


def test_local_provider_preserves_an_existing_destination(tmp_path: Path) -> None:
    root = tmp_path / "authority"
    provider = _require_native_provider(root)
    request = _request(root)
    request.destination.mkdir()
    marker = request.destination / "old.txt"
    marker.write_text("old", encoding="utf-8")
    receipt_owner = PublishedWorkspaceReceiptOwner()

    with pytest.raises(FileExistsError):
        run_strict_workspace(
            provider,
            request,
            receipt_owner=receipt_owner,
            operation=lambda session: _write_and_publish(session, b"new"),
        )

    assert marker.read_text(encoding="utf-8") == "old"
    assert not tuple(root.glob(".codenib-workspace-stage-*"))
    assert receipt_owner.state == "empty"


def test_local_provider_replaces_receipt_bound_exact_via_two_phase_seam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "authority"
    provider = _require_native_provider(root)
    source_owner = PublishedWorkspaceReceiptOwner()
    source_request = _request(root)
    run_strict_workspace(
        provider,
        source_request,
        receipt_owner=source_owner,
        operation=lambda session: _write_and_publish(session, b"old"),
    )
    binding = source_owner.destination_binding
    request = _request(root, destination_binding=binding)
    receipt_owner = PublishedWorkspaceReceiptOwner()
    events: list[str] = []
    real_capture = workspace_owner.capture_owner_destination
    real_lease = workspace_owner.acquire_owner_replacement_lease
    real_bind = local_provider_module._ReplacementSourceGate.bind
    real_provision = local_provider_module._PROVISION_BOUND_REPLACEMENT_EXACT
    replay_errors: list[BaseException] = []
    bound_gates: list[object] = []

    def capture(*args: object, **kwargs: object) -> None:
        events.append("capture")
        real_capture(*args, **kwargs)

    def lease(*args: object, **kwargs: object) -> None:
        events.append("lease")
        real_lease(*args, **kwargs)

    def bind(*args: object, **kwargs: object) -> None:
        events.append("bind")
        bound_gates.append(args[0])
        real_bind(*args, **kwargs)
        try:
            real_bind(*args, **kwargs)
        except BaseException as error:  # noqa: B036 - assert exact replay denial
            replay_errors.append(error)
        else:
            raise AssertionError("replacement source gate allowed a second bind")

    def provision(*args: object, **kwargs: object) -> None:
        events.append("provision")
        real_provision(*args, **kwargs)

    def forbid_missing_provision(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("exact replacement reached missing-only provisioning")

    monkeypatch.setattr(workspace_owner, "capture_owner_destination", capture)
    monkeypatch.setattr(workspace_owner, "acquire_owner_replacement_lease", lease)
    monkeypatch.setattr(local_provider_module._ReplacementSourceGate, "bind", bind)
    monkeypatch.setattr(
        local_provider_module,
        "_PROVISION_BOUND_REPLACEMENT_EXACT",
        provision,
    )
    monkeypatch.setattr(workspace_owner, "provision_owner", forbid_missing_provision)

    try:
        record, _ownership = run_strict_workspace(
            provider,
            request,
            receipt_owner=receipt_owner,
            operation=lambda session: _write_and_publish(session, b"new"),
            source_owner=source_owner,
        )

        assert events == ["capture", "lease", "bind", "provision"]
        assert len(bound_gates) == 1
        assert len(replay_errors) == 1
        assert isinstance(replay_errors[0], RuntimeError)
        assert "no longer active" in str(replay_errors[0])
        assert record.sha256 == hashlib.sha256(b"new").hexdigest()
        assert request.destination.joinpath("data/result.json").read_bytes() == b"new"
        assert source_owner.active
        assert receipt_owner.active
        bound_gate = bound_gates[0]
        bound_workspace = bound_gate._bound_workspace  # type: ignore[attr-defined]
        assert bound_workspace is not None
        with pytest.raises(RuntimeError, match="no longer active"):
            bound_gate._require_bound_workspace(  # type: ignore[attr-defined]
                request,
                bound_workspace,
            )
        orphan = receipt_owner.receipt.orphan
        assert orphan is not None
        assert orphan.locator.backend_tag == "linux-renameat2"
        assert (
            orphan.reopen(
                lambda reader: reader.read_bytes(
                    "data/result.json",
                    max_bytes=16,
                )
            )
            == b"old"
        )
    finally:
        receipt_owner.close()
        source_owner.close()


def test_local_exact_mints_fresh_provision_and_publication_deadlines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "authority"
    source_provider = _require_native_provider(root)
    source_owner = PublishedWorkspaceReceiptOwner()
    source_request = _request(root)
    run_strict_workspace(
        source_provider,
        source_request,
        receipt_owner=source_owner,
        operation=lambda session: _write_and_publish(session, b"old"),
    )
    timeout_ns = 10_000_000_000
    provider = LocalWorkspaceProvider(root, provision_timeout_ns=timeout_ns)
    request = _request(root, destination_binding=source_owner.destination_binding)
    output_owner = PublishedWorkspaceReceiptOwner()
    base_ns = local_provider_module.time.monotonic_ns()
    capture_deadlines: list[int] = []
    provision_deadlines: list[int] = []
    publication_deadlines: list[int] = []
    real_capture = workspace_owner.capture_owner_destination
    real_bind = local_provider_module._ReplacementSourceGate.bind
    real_provision = local_provider_module._PROVISION_BOUND_REPLACEMENT_EXACT
    real_publish = workspace_provider_module._PUBLISH_REPLACEMENT_EXACT

    def capture(
        owner: object,
        allowed_root: bytes,
        destination: bytes,
        deadline_ns: int,
    ) -> None:
        capture_deadlines.append(deadline_ns)
        real_capture(owner, allowed_root, destination, deadline_ns)

    def bind(*args: object, **kwargs: object) -> None:
        real_bind(*args, **kwargs)
        monkeypatch.setattr(
            local_provider_module.time,
            "monotonic_ns",
            lambda: base_ns + 2 * timeout_ns,
        )

    def provision(workspace, *, deadline_ns: int) -> None:
        provision_deadlines.append(deadline_ns)
        real_provision(workspace, deadline_ns=deadline_ns)

    def publish(workspace, receipt_owner, *, deadline_ns: int, **kwargs) -> None:
        publication_deadlines.append(deadline_ns)
        real_publish(
            workspace,
            receipt_owner,
            deadline_ns=deadline_ns,
            **kwargs,
        )

    monkeypatch.setattr(workspace_owner, "capture_owner_destination", capture)
    monkeypatch.setattr(local_provider_module._ReplacementSourceGate, "bind", bind)
    monkeypatch.setattr(
        local_provider_module,
        "_PROVISION_BOUND_REPLACEMENT_EXACT",
        provision,
    )
    monkeypatch.setattr(
        workspace_provider_module,
        "_MONOTONIC_NS_EXACT",
        lambda: base_ns + 4 * timeout_ns,
    )
    monkeypatch.setattr(
        workspace_provider_module,
        "_PUBLISH_REPLACEMENT_EXACT",
        publish,
    )

    def operation(session: StrictWorkspaceSession) -> object:
        # The operation gate already froze its clock. This later replacement
        # must not collapse the publication deadline back to one timeout.
        monkeypatch.setattr(
            workspace_provider_module,
            "_MONOTONIC_NS_EXACT",
            lambda: 1,
        )
        return _write_and_publish(session, b"new")

    try:
        run_strict_workspace(
            provider,
            request,
            receipt_owner=output_owner,
            operation=operation,
            source_owner=source_owner,
        )
        assert len(capture_deadlines) == 1
        assert capture_deadlines[0] < base_ns + 2 * timeout_ns
        assert provision_deadlines == [base_ns + 3 * timeout_ns]
        assert publication_deadlines == [base_ns + 5 * timeout_ns]
        assert output_owner.active
    finally:
        output_owner.close()
        source_owner.close()


def test_local_exact_never_uses_generic_workspace_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "authority"
    provider = _require_native_provider(root)
    source_owner = PublishedWorkspaceReceiptOwner()
    source_request = _request(root)
    run_strict_workspace(
        provider,
        source_request,
        receipt_owner=source_owner,
        operation=lambda session: _write_and_publish(session, b"old"),
    )
    request = _request(root, destination_binding=source_owner.destination_binding)
    output_owner = PublishedWorkspaceReceiptOwner()

    def forbid_generic(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("exact Local publication reached the generic helper")

    monkeypatch.setattr(
        local_provider_module.OwnedWorkspaceAuthority,
        "publish_into",
        forbid_generic,
    )
    try:
        run_strict_workspace(
            provider,
            request,
            receipt_owner=output_owner,
            operation=lambda session: _write_and_publish(session, b"new"),
            source_owner=source_owner,
        )
        assert output_owner.active
        assert request.destination.joinpath("data/result.json").read_bytes() == b"new"
    finally:
        output_owner.close()
        source_owner.close()


def test_local_exact_freezes_gate_callbacks_before_provider_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "authority"
    delegate = _require_native_provider(root)
    source_owner = PublishedWorkspaceReceiptOwner()
    source_request = _request(root)
    run_strict_workspace(
        delegate,
        source_request,
        receipt_owner=source_owner,
        operation=lambda session: _write_and_publish(session, b"old"),
    )
    request = _request(root, destination_binding=source_owner.destination_binding)
    output_owner = PublishedWorkspaceReceiptOwner()
    intercepted: list[str] = []

    def forbid_frozen_callback(*_args: object, **_kwargs: object) -> None:
        intercepted.append("intercepted")
        raise AssertionError("provider intercepted an exact callback")

    class MutatingProvider:
        def require_support(self) -> None:
            for name in (
                "_BIND_REPLACEMENT_SOURCE_EXACT",
                "_MONOTONIC_NS_EXACT",
                "_PUBLISH_REPLACEMENT_EXACT",
                "_SOURCE_DESTINATION_BINDING_EXACT",
                "_commit_provider_primary",
                "_invoke_strict_workspace_provider",
                "_settle_provider_operation_gate",
                "_settle_replacement_source_gate",
                "_snapshot_workspace_plan",
            ):
                monkeypatch.setattr(
                    workspace_provider_module,
                    name,
                    forbid_frozen_callback,
                )
            delegate.require_support()

        def run_workspace(
            self,
            bound_request: StrictWorkspaceRequest,
            *,
            receipt_owner: PublishedWorkspaceReceiptOwner,
            operation,
            _replacement_source,
        ) -> object:
            return delegate.run_workspace(
                bound_request,
                receipt_owner=receipt_owner,
                operation=operation,
                _replacement_source=_replacement_source,
            )

    try:
        run_strict_workspace(
            MutatingProvider(),
            request,
            receipt_owner=output_owner,
            operation=lambda session: _write_and_publish(session, b"new"),
            source_owner=source_owner,
        )
        assert intercepted == []
        assert output_owner.active
        assert request.destination.joinpath("data/result.json").read_bytes() == b"new"
    finally:
        output_owner.close()
        source_owner.close()


def test_local_exact_posthandoff_failure_delegates_cleanup_to_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "authority"
    provider = _require_native_provider(root)
    source_owner = PublishedWorkspaceReceiptOwner()
    source_request = _request(root)
    run_strict_workspace(
        provider,
        source_request,
        receipt_owner=source_owner,
        operation=lambda session: _write_and_publish(session, b"old"),
    )
    request = _request(root, destination_binding=source_owner.destination_binding)
    output_owner = PublishedWorkspaceReceiptOwner()
    failure = RuntimeError("injected posthandoff failure")

    def fail_provision(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(
        local_provider_module,
        "_PROVISION_BOUND_REPLACEMENT_EXACT",
        fail_provision,
    )
    try:
        with pytest.raises(RuntimeError) as caught:
            run_strict_workspace(
                provider,
                request,
                receipt_owner=output_owner,
                operation=lambda session: _write_and_publish(session, b"new"),
                source_owner=source_owner,
            )
        assert caught.value is failure
        assert output_owner.state == "empty"
        assert source_owner.active
        assert request.destination.joinpath("data/result.json").read_bytes() == b"old"
        assert not tuple(root.glob(".codenib-workspace-stage-*"))
    finally:
        output_owner.close()
        source_owner.close()


def test_local_exact_gate_bind_failure_releases_lease_before_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "authority"
    provider = _require_native_provider(root)
    source_owner = PublishedWorkspaceReceiptOwner()
    source_request = _request(root)
    run_strict_workspace(
        provider,
        source_request,
        receipt_owner=source_owner,
        operation=lambda session: _write_and_publish(session, b"old"),
    )
    request = _request(root, destination_binding=source_owner.destination_binding)
    failed_output_owner = PublishedWorkspaceReceiptOwner()
    successful_output_owner = PublishedWorkspaceReceiptOwner()
    failure = KeyboardInterrupt("injected gate bind failure")
    real_bind = local_provider_module._ReplacementSourceGate.bind

    def fail_bind(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(
        local_provider_module._ReplacementSourceGate,
        "bind",
        fail_bind,
    )
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            run_strict_workspace(
                provider,
                request,
                receipt_owner=failed_output_owner,
                operation=lambda session: _write_and_publish(session, b"forbidden"),
                source_owner=source_owner,
            )

        assert caught.value is failure
        assert failed_output_owner.state == "empty"
        assert source_owner.active
        assert (
            source_owner.consume(
                lambda _receipt, reader: reader.read_bytes(
                    "data/result.json",
                    max_bytes=16,
                )
            )
            == b"old"
        )
        assert not tuple(root.glob(".codenib-workspace-stage-*"))

        # A second exact replacement must acquire the released native lease.
        monkeypatch.setattr(
            local_provider_module._ReplacementSourceGate,
            "bind",
            real_bind,
        )
        run_strict_workspace(
            provider,
            request,
            receipt_owner=successful_output_owner,
            operation=lambda session: _write_and_publish(session, b"new"),
            source_owner=source_owner,
        )
        assert successful_output_owner.active
        assert request.destination.joinpath("data/result.json").read_bytes() == b"new"
    finally:
        failed_output_owner.close()
        successful_output_owner.close()
        source_owner.close()


def test_local_exact_user_cancellation_quarantines_provisioned_candidate(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    provider = _require_native_provider(root)
    source_owner = PublishedWorkspaceReceiptOwner()
    source_request = _request(root)
    run_strict_workspace(
        provider,
        source_request,
        receipt_owner=source_owner,
        operation=lambda session: _write_and_publish(session, b"old"),
    )
    request = _request(root, destination_binding=source_owner.destination_binding)
    output_owner = PublishedWorkspaceReceiptOwner()
    cancellation = KeyboardInterrupt("injected prepublication cancellation")

    def cancel_before_publication(session: StrictWorkspaceSession) -> None:
        session.write_file("data/result.json", (b"new",))
        raise cancellation

    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            run_strict_workspace(
                provider,
                request,
                receipt_owner=output_owner,
                operation=cancel_before_publication,
                source_owner=source_owner,
            )

        assert caught.value is cancellation
        trace_names: list[str] = []
        traceback = caught.value.__traceback__
        while traceback is not None:
            trace_names.append(traceback.tb_frame.f_code.co_name)
            traceback = traceback.tb_next
        assert "cancel_before_publication" in trace_names
        assert output_owner.state == "empty"
        assert source_owner.active
        assert (
            source_owner.consume(
                lambda _receipt, reader: reader.read_bytes(
                    "data/result.json",
                    max_bytes=16,
                )
            )
            == b"old"
        )
        quarantined = tuple(root.glob(".codenib-workspace-stage-*"))
        assert len(quarantined) == 1
        assert quarantined[0].joinpath("data/result.json").read_bytes() == b"new"
    finally:
        output_owner.close()
        source_owner.close()


def test_local_exact_staged_validation_expiry_restores_incumbent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "authority"
    source_provider = _require_native_provider(root)
    source_owner = PublishedWorkspaceReceiptOwner()
    source_request = _request(root)
    run_strict_workspace(
        source_provider,
        source_request,
        receipt_owner=source_owner,
        operation=lambda session: _write_and_publish(session, b"old"),
    )
    timeout_ns = 1_000_000_000
    provider = LocalWorkspaceProvider(root, provision_timeout_ns=timeout_ns)
    request = _request(root, destination_binding=source_owner.destination_binding)
    output_owner = PublishedWorkspaceReceiptOwner()
    real_monotonic_ns = time.monotonic_ns
    staged_calls = 0
    monkeypatch.setattr(
        workspace_provider_module,
        "_MONOTONIC_NS_EXACT",
        lambda: real_monotonic_ns() - timeout_ns + 20_000_000,
    )

    def operation(session: StrictWorkspaceSession) -> None:
        session.write_file("data/result.json", (b"new",))

        def expire_before_exchange(_reader) -> None:
            nonlocal staged_calls
            staged_calls += 1
            time.sleep(0.05)

        session.publish_validated(expire_before_exchange)

    try:
        with pytest.raises(TimeoutError, match="deadline expired"):
            run_strict_workspace(
                provider,
                request,
                receipt_owner=output_owner,
                operation=operation,
                source_owner=source_owner,
            )
        assert staged_calls == 1
        # Publication reserved the caller slot before validation, so the slot
        # retains cleanup authority but never exposes an active receipt.
        assert output_owner.state == "cleanup"
        assert not output_owner.active
        assert source_owner.active
        assert request.destination.joinpath("data/result.json").read_bytes() == b"old"
        assert (
            source_owner.consume(
                lambda _receipt, reader: reader.read_bytes(
                    "data/result.json",
                    max_bytes=16,
                )
            )
            == b"old"
        )
        quarantined = tuple(root.glob(".codenib-workspace-stage-*"))
        assert len(quarantined) == 1
        assert quarantined[0].joinpath("data/result.json").read_bytes() == b"new"
        output_owner.close()
        assert output_owner.closed
        assert tuple(root.glob(".codenib-workspace-stage-*")) == quarantined
    finally:
        output_owner.close()
        source_owner.close()


def test_local_provider_fails_closed_without_the_complete_native_abi(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "authority"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    provider = LocalWorkspaceProvider(root)
    request = _request(root)
    receipt_owner = PublishedWorkspaceReceiptOwner()
    monkeypatch.setattr(
        workspace_owner,
        "_workspace_owner_protocol_available",
        False,
    )

    with pytest.raises(UnsupportedWorkspaceCreation, match="ownership is unavailable"):
        run_strict_workspace(
            provider,
            request,
            receipt_owner=receipt_owner,
            operation=lambda session: _write_and_publish(session, b"forbidden"),
        )

    assert tuple(root.iterdir()) == ()
    assert receipt_owner.state == "empty"


@pytest.mark.parametrize("invalid_plan", ("subclass", "digest"))
def test_local_provider_rejects_nonexact_plan_before_native_mutation(
    tmp_path: Path,
    invalid_plan: str,
) -> None:
    root = tmp_path / "authority"
    provider = _require_native_provider(root)
    plan = _plan()
    if invalid_plan == "subclass":

        class DerivedWorkspacePlan(WorkspacePlan):
            pass

        plan = DerivedWorkspacePlan(
            subject_digest=plan.subject_digest,
            directories=plan.directories,
            files=plan.files,
            root_mode=plan.root_mode,
        )
        expected_error = TypeError
    else:
        object.__setattr__(plan, "digest", "0" * 64)
        expected_error = ValueError
    request = _request(root)
    object.__setattr__(request, "plan", plan)
    receipt_owner = PublishedWorkspaceReceiptOwner()

    with pytest.raises(expected_error):
        run_strict_workspace(
            provider,
            request,
            receipt_owner=receipt_owner,
            operation=lambda session: _write_and_publish(session, b"forbidden"),
        )

    assert tuple(root.iterdir()) == ()
    assert receipt_owner.state == "empty"


def test_local_provider_requires_a_private_owner_only_root(tmp_path: Path) -> None:
    root = tmp_path / "authority"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    provider = LocalWorkspaceProvider(root)

    with pytest.raises(UnsupportedWorkspaceCreation, match="private owner-only"):
        provider.require_support()

    assert tuple(root.iterdir()) == ()
    assert os.stat(root).st_mode & 0o777 == 0o755


def test_local_provider_timeout_precedes_namespace_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "authority"
    provider = _require_native_provider(root)
    request = _request(root)
    receipt_owner = PublishedWorkspaceReceiptOwner()
    monkeypatch.setattr(local_provider_module.time, "monotonic_ns", lambda: 0)

    with pytest.raises(TimeoutError, match="deadline expired"):
        run_strict_workspace(
            provider,
            request,
            receipt_owner=receipt_owner,
            operation=lambda session: _write_and_publish(session, b"too late"),
        )

    assert tuple(root.iterdir()) == ()
    assert receipt_owner.state == "empty"


def test_local_provider_aborts_when_operation_returns_without_publish(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    provider = _require_native_provider(root)
    request = _request(root)
    receipt_owner = PublishedWorkspaceReceiptOwner()

    with pytest.raises(RuntimeError, match="without publishing"):
        run_strict_workspace(
            provider,
            request,
            receipt_owner=receipt_owner,
            operation=lambda _session: "unpublished",
        )

    assert receipt_owner.state == "empty"
    assert not request.destination.exists()
    assert not tuple(root.glob(".codenib-workspace-stage-*"))
    assert tuple(root.glob(".codenib-workspace-orphan-*"))


def test_local_provider_same_destination_has_one_noreplace_winner(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    provider = _require_native_provider(root)
    barrier = threading.Barrier(2)
    owners = (PublishedWorkspaceReceiptOwner(), PublishedWorkspaceReceiptOwner())
    payloads = (b"first", b"second")

    def publish(index: int) -> tuple[str, object]:
        request = _request(root)

        def operation(session: StrictWorkspaceSession) -> object:
            barrier.wait(timeout=10)
            return _write_and_publish(session, payloads[index])

        try:
            return (
                "published",
                run_strict_workspace(
                    provider,
                    request,
                    receipt_owner=owners[index],
                    operation=operation,
                ),
            )
        except BaseException as error:  # noqa: B036 - assert the exact loser
            return "failed", error

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(publish, range(2)))

    winner_indexes = tuple(
        index for index, outcome in enumerate(outcomes) if outcome[0] == "published"
    )
    loser_indexes = tuple(
        index for index, outcome in enumerate(outcomes) if outcome[0] == "failed"
    )
    assert len(winner_indexes) == 1
    assert len(loser_indexes) == 1
    winner_index = winner_indexes[0]
    loser_index = loser_indexes[0]
    assert isinstance(outcomes[loser_index][1], FileExistsError)
    assert owners[winner_index].active
    assert owners[loser_index].state == "cleanup"
    assert (root / "published/data/result.json").read_bytes() == payloads[winner_index]
    assert not tuple(root.glob(".codenib-workspace-stage-*"))
    assert len(tuple(root.glob(".codenib-workspace-orphan-*"))) == 1

    owners[winner_index].close()
    owners[loser_index].close()
    assert all(owner.closed for owner in owners)


def test_local_provider_runs_distinct_destinations_concurrently(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    provider = _require_native_provider(root)
    barrier = threading.Barrier(2)
    owners = (PublishedWorkspaceReceiptOwner(), PublishedWorkspaceReceiptOwner())
    destinations = (root / "first", root / "second")
    payloads = (b"one", b"two")

    def publish(index: int) -> object:
        request = StrictWorkspaceRequest(
            purpose="test-local-workspace-provider",
            destination=destinations[index],
            plan=_plan(),
        )

        def operation(session: StrictWorkspaceSession) -> object:
            barrier.wait(timeout=10)
            return _write_and_publish(session, payloads[index])

        return run_strict_workspace(
            provider,
            request,
            receipt_owner=owners[index],
            operation=operation,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        tuple(executor.map(publish, range(2)))

    assert all(owner.active for owner in owners)
    for destination, payload in zip(destinations, payloads, strict=True):
        assert (destination / "data/result.json").read_bytes() == payload
    assert not tuple(root.glob(".codenib-workspace-stage-*"))
    assert not tuple(root.glob(".codenib-workspace-orphan-*"))
    for owner in owners:
        owner.close()


@pytest.mark.parametrize("incumbent_kind", ("directory", "file", "symlink"))
def test_local_provider_never_overwrites_a_late_destination(
    tmp_path: Path,
    incumbent_kind: str,
) -> None:
    root = tmp_path / "authority"
    provider = _require_native_provider(root)
    request = _request(root)
    receipt_owner = PublishedWorkspaceReceiptOwner()
    incumbent_target = root / "incumbent-target"

    def install_incumbent(session: StrictWorkspaceSession) -> object:
        if incumbent_kind == "directory":
            request.destination.mkdir()
            (request.destination / "marker").write_bytes(b"incumbent-directory")
        elif incumbent_kind == "file":
            request.destination.write_bytes(b"incumbent-file")
        else:
            incumbent_target.mkdir()
            request.destination.symlink_to(incumbent_target, target_is_directory=True)
        return _write_and_publish(session, b"must not replace")

    with pytest.raises((FileExistsError, RuntimeError, ValueError)):
        run_strict_workspace(
            provider,
            request,
            receipt_owner=receipt_owner,
            operation=install_incumbent,
        )

    if incumbent_kind == "directory":
        assert (request.destination / "marker").read_bytes() == b"incumbent-directory"
    elif incumbent_kind == "file":
        assert request.destination.read_bytes() == b"incumbent-file"
    else:
        assert request.destination.is_symlink()
        assert request.destination.readlink() == incumbent_target
    assert receipt_owner.state == "cleanup"
    assert not tuple(root.glob(".codenib-workspace-stage-*"))
    assert len(tuple(root.glob(".codenib-workspace-orphan-*"))) == 1
    receipt_owner.close()
    assert receipt_owner.closed


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_inherited_local_provider_rejects_child_before_mutation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    provider = _require_native_provider(root)
    request = _request(root)
    read_descriptor, write_descriptor = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        os.close(read_descriptor)
        child_owner = PublishedWorkspaceReceiptOwner()
        try:
            run_strict_workspace(
                provider,
                request,
                receipt_owner=child_owner,
                operation=lambda session: _write_and_publish(session, b"child"),
            )
        except RuntimeError as error:
            report = repr((str(error), child_owner.state, tuple(root.iterdir())))
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
    assert (
        report
        == "('local workspace provider cannot cross a PID boundary', 'empty', ())"
    )
    assert tuple(root.iterdir()) == ()
    provider.require_support()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_local_provider_child_escape_closes_only_inherited_native_descriptors(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    provider = _require_native_provider(root)
    request = _request(root)
    receipt_owner = PublishedWorkspaceReceiptOwner()
    read_descriptor, write_descriptor = os.pipe()
    original_pid = os.getpid()
    child_pids: list[int] = []

    def operation(session: StrictWorkspaceSession) -> object:
        child_pid = os.fork()
        if child_pid == 0:
            os.close(read_descriptor)
            raise RuntimeError("child escaped the provider callback")
        child_pids.append(child_pid)
        os.close(write_descriptor)
        return _write_and_publish(session, b"parent")

    try:
        run_strict_workspace(
            provider,
            request,
            receipt_owner=receipt_owner,
            operation=operation,
        )
    except RuntimeError as error:
        if os.getpid() != original_pid:
            try:
                descriptor_targets = []
                for name in os.listdir("/proc/self/fd"):
                    try:
                        target = os.readlink(f"/proc/self/fd/{name}")
                    except OSError:
                        continue
                    if str(root) in target:
                        descriptor_targets.append(target)
                cleanup_owners = getattr(error, "publication_cleanup_owners", ())
                report = repr((str(error), descriptor_targets, cleanup_owners))
                os.write(write_descriptor, report.encode("utf-8"))
            finally:
                os.close(write_descriptor)
                os._exit(0)
        raise

    child_pid = child_pids[0]
    report = os.read(read_descriptor, 1 << 20).decode("utf-8")
    os.close(read_descriptor)
    waited_pid, status = os.waitpid(child_pid, 0)

    assert waited_pid == child_pid
    assert os.waitstatus_to_exitcode(status) == 0
    assert report == "('child escaped the provider callback', [], ())"
    assert receipt_owner.active
    assert (request.destination / "data/result.json").read_bytes() == b"parent"
    receipt_owner.close()
