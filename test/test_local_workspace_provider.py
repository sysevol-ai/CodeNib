# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import errno
import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import codenib
import codenib._local_workspace_provider as local_provider_module
import codenib._workspace_owner as workspace_owner
from codenib._atomic_directory import publication_parent_identity
from codenib._captured_directory import (
    PublishedWorkspaceReceiptOwner,
    UnsupportedWorkspaceCreation,
    WorkspaceDirectory,
    WorkspaceFile,
    WorkspacePlan,
)
from codenib._local_workspace_provider import LocalWorkspaceProvider
from codenib._workspace_provider import (
    DestinationExpectation,
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
    expectation: DestinationExpectation = "missing",
) -> StrictWorkspaceRequest:
    return StrictWorkspaceRequest(
        purpose="test-local-workspace-provider",
        destination=root / "published",
        plan=_plan(),
        destination_expectation=expectation,
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


def test_local_provider_rejects_bound_exact_expectation_before_mutation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "authority"
    provider = _require_native_provider(root)
    request = _request(root, expectation="provider-bound-exact")
    receipt_owner = PublishedWorkspaceReceiptOwner()

    with pytest.raises(UnsupportedWorkspaceCreation, match="missing destination"):
        run_strict_workspace(
            provider,
            request,
            receipt_owner=receipt_owner,
            operation=lambda session: _write_and_publish(session, b"forbidden"),
        )

    assert tuple(root.iterdir()) == ()
    assert receipt_owner.state == "empty"


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
