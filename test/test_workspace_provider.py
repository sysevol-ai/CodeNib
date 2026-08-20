# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import os
import signal
import threading
from pathlib import Path
from typing import Callable

import pytest

import codenib._workspace_provider as workspace_provider
from codenib._atomic_directory import capture_directory_ownership
from codenib._captured_directory import (
    OwnedWorkspaceAuthority,
    PublishedWorkspaceReceiptOwner,
    UnsupportedWorkspaceCreation,
    WorkspaceFile,
    WorkspacePlan,
)

_DEFAULT_EXPECTATION = object()
from codenib._workspace_provider import (
    StrictWorkspaceRequest,
    StrictWorkspaceSession,
    run_adopted_workspace_operation,
    run_strict_workspace,
)


def _plan() -> WorkspacePlan:
    return WorkspacePlan(
        subject_digest=hashlib.sha256(b"workspace-provider-test").hexdigest(),
        files=(WorkspaceFile(Path("payload.bin"), max_bytes=32),),
    )


def _interrupt_before_store_attr(
    callback: Callable[[], object],
    *,
    target_type: type[object],
    attribute: str,
    error: BaseException,
    injected: list[bool],
) -> None:
    inherited_setattr = target_type.__setattr__
    had_local_setattr = "__setattr__" in target_type.__dict__
    local_setattr = target_type.__dict__.get("__setattr__")
    was_injected = False

    def interrupt_store(instance, name, value):
        nonlocal was_injected
        if name == attribute and not was_injected and hasattr(instance, attribute):
            was_injected = True
            raise error
        inherited_setattr(instance, name, value)

    target_type.__setattr__ = interrupt_store  # type: ignore[method-assign]
    try:
        callback()
    finally:
        if had_local_setattr:
            target_type.__setattr__ = local_setattr  # type: ignore[method-assign]
        else:
            del target_type.__setattr__
        injected.append(was_injected)


class _TestProvider:
    def __init__(
        self,
        *,
        adopted_destination: Path | None = None,
        expected_destination: object = _DEFAULT_EXPECTATION,
    ) -> None:
        self.support_checks = 0
        self.runs = 0
        self.last_workspace: OwnedWorkspaceAuthority | None = None
        self.adopted_destination = adopted_destination
        self.expected_destination = expected_destination

    def require_support(self) -> None:
        self.support_checks += 1

    def run_workspace(
        self,
        request: StrictWorkspaceRequest,
        *,
        receipt_owner: PublishedWorkspaceReceiptOwner,
        operation: Callable[[StrictWorkspaceSession], object],
    ) -> object:
        self.runs += 1
        destination = self.adopted_destination or request.destination
        parent = destination.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        stage = parent / ".provider-stage"
        stage.mkdir(mode=request.plan.root_mode)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        parent_descriptor = os.open(parent, flags)
        root_descriptor = os.open(stage, flags)
        workspace = OwnedWorkspaceAuthority()
        self.last_workspace = workspace
        expected_destination = self.expected_destination
        if expected_destination is _DEFAULT_EXPECTATION:
            expected_destination = (
                capture_directory_ownership(
                    destination,
                    allow_empty_root=True,
                )
                if request.destination_expectation == "provider-bound-exact"
                else None
            )
        try:
            workspace.adopt(
                destination=destination,
                stage_name=stage.name,
                parent_descriptor=parent_descriptor,
                root_descriptor=root_descriptor,
                directory_descriptors={},
                plan=request.plan,
                expected_destination=expected_destination,
            )
            return run_adopted_workspace_operation(
                request,
                workspace=workspace,
                receipt_owner=receipt_owner,
                operation=operation,
            )
        finally:
            os.close(root_descriptor)
            os.close(parent_descriptor)
            if receipt_owner.state == "empty":
                workspace.close()


def _run_thread(
    callback: Callable[[], object],
    *,
    values: list[object],
    errors: list[BaseException],
    finished: threading.Event,
) -> None:
    try:
        values.append(callback())
    except BaseException as exc:  # noqa: B036 - assert exact thread failures
        errors.append(exc)
    finally:
        finished.set()


def _notes(error: BaseException) -> tuple[str, ...]:
    return tuple(getattr(error, "__notes__", ())) + tuple(
        getattr(error, "_codenib_cleanup_notes", ())
    )


def _fork_expect_pid_boundary(
    callback: Callable[[], object],
    *,
    expected_message: str,
) -> tuple[int, str]:
    read_descriptor, write_descriptor = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        os.close(read_descriptor)

        def abort_blocked_child(*_args: object) -> None:
            os._exit(90)

        signal.signal(signal.SIGALRM, abort_blocked_child)
        signal.alarm(3)
        exit_code = 0
        try:
            callback()
        except BaseException as error:  # noqa: B036 - report exact child failure
            if isinstance(error, RuntimeError) and expected_message in str(error):
                payload = b"expected PID boundary"
            else:
                payload = f"{type(error).__name__}: {error}".encode(
                    "utf-8",
                    errors="replace",
                )
                exit_code = 91
        else:
            payload = b"forked entry returned"
            exit_code = 92
        signal.alarm(0)
        os.write(write_descriptor, payload)
        os.close(write_descriptor)
        os._exit(exit_code)

    os.close(write_descriptor)
    try:
        payload = os.read(read_descriptor, 4096).decode("utf-8", errors="replace")
    finally:
        os.close(read_descriptor)
    waited_pid, status = os.waitpid(child_pid, 0)
    assert waited_pid == child_pid
    return os.waitstatus_to_exitcode(status), payload


def test_request_is_lexical_and_rejects_invalid_contract(tmp_path: Path) -> None:
    destination = tmp_path / "parent" / ".." / "parent" / "published"
    request = StrictWorkspaceRequest("bm25-normalize", destination, _plan())

    assert request.destination == Path(os.path.abspath(destination))
    with pytest.raises(ValueError, match="purpose"):
        StrictWorkspaceRequest("bad\nname", destination, _plan())
    with pytest.raises(ValueError, match="expectation"):
        StrictWorkspaceRequest(
            "test",
            destination,
            _plan(),
            destination_expectation="ambient",  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="name one directory"):
        StrictWorkspaceRequest("test", tmp_path / "parent" / "..", _plan())

    class DerivedWorkspacePlan(WorkspacePlan):
        pass

    original = _plan()
    with pytest.raises(TypeError, match="exact WorkspacePlan"):
        StrictWorkspaceRequest(
            "test",
            destination,
            DerivedWorkspacePlan(
                subject_digest=original.subject_digest,
                directories=original.directories,
                files=original.files,
                root_mode=original.root_mode,
            ),
        )
    object.__setattr__(original, "digest", "0" * 64)
    with pytest.raises(ValueError, match="digest is inconsistent"):
        StrictWorkspaceRequest("test", destination, original)


def test_provider_runs_one_callback_scoped_validated_publication(
    tmp_path: Path,
) -> None:
    request = StrictWorkspaceRequest("test", tmp_path / "published", _plan())
    provider = _TestProvider()
    owner = PublishedWorkspaceReceiptOwner()
    escaped: list[StrictWorkspaceSession] = []
    validations: list[bytes] = []

    def operation(session: StrictWorkspaceSession) -> bytes:
        escaped.append(session)
        record = session.write_file("payload.bin", (b"owned",))
        assert (record.path, record.mode, record.size) == (
            "payload.bin",
            0o600,
            5,
        )

        def validate(reader):
            payload = reader.read_bytes("payload.bin", max_bytes=32)
            validations.append(payload)
            return payload

        return session.publish_validated(validate)

    try:
        assert (
            run_strict_workspace(
                provider,
                request,
                receipt_owner=owner,
                operation=operation,
            )
            == b"owned"
        )
        assert validations == [b"owned", b"owned"]
        assert provider.support_checks == provider.runs == 1
        assert owner.active
        assert owner.receipt.path == request.destination
        assert owner.receipt.plan == request.plan
        assert (
            owner.consume(
                lambda _receipt, reader: reader.read_bytes(
                    "payload.bin",
                    max_bytes=32,
                )
            )
            == b"owned"
        )
        with pytest.raises(RuntimeError, match="no longer active"):
            _ = escaped[0].request
        with pytest.raises(RuntimeError, match="no longer active"):
            escaped[0].write_file("payload.bin", (b"late",))
        with pytest.raises(RuntimeError, match="no longer active"):
            escaped[0].publish_validated(lambda _reader: None)
    finally:
        owner.close()


def test_provider_cannot_substitute_the_callback_result(tmp_path: Path) -> None:
    request = StrictWorkspaceRequest("test", tmp_path / "published", _plan())
    delegate = _TestProvider()
    owner = PublishedWorkspaceReceiptOwner()

    class SubstitutingProvider:
        def require_support(self) -> None:
            return None

        def run_workspace(self, request, *, receipt_owner, operation):
            delegate.run_workspace(
                request,
                receipt_owner=receipt_owner,
                operation=operation,
            )
            return b"provider-substitution"

    def operation(session: StrictWorkspaceSession) -> bytes:
        session.write_file("payload.bin", (b"owned",))
        session.publish_validated(lambda _reader: None)
        return b"callback-result"

    try:
        assert (
            run_strict_workspace(
                SubstitutingProvider(),
                request,
                receipt_owner=owner,
                operation=operation,
            )
            == b"callback-result"
        )
        assert owner.active
    finally:
        if owner.active:
            owner.close()


@pytest.mark.parametrize(
    "primary_type",
    [KeyboardInterrupt, SystemExit, GeneratorExit],
)
def test_provider_cannot_swallow_the_callback_primary(
    tmp_path: Path,
    primary_type: type[BaseException],
) -> None:
    request = StrictWorkspaceRequest("test", tmp_path / "published", _plan())
    delegate = _TestProvider()
    owner = PublishedWorkspaceReceiptOwner()
    primary = primary_type("callback-primary")

    class SwallowingProvider:
        def require_support(self) -> None:
            return None

        def run_workspace(self, request, *, receipt_owner, operation):
            try:
                delegate.run_workspace(
                    request,
                    receipt_owner=receipt_owner,
                    operation=operation,
                )
            except BaseException:  # noqa: B036 - exercise a broken provider
                return b"provider-swallowed-callback"
            raise AssertionError("callback primary was not raised")

    def operation(session: StrictWorkspaceSession) -> None:
        session.write_file("payload.bin", (b"owned",))
        session.publish_validated(lambda _reader: None)
        raise primary

    try:
        with pytest.raises(primary_type, match="callback-primary") as caught:
            run_strict_workspace(
                SwallowingProvider(),
                request,
                receipt_owner=owner,
                operation=operation,
            )

        assert caught.value is primary
        assert owner.active
        assert request.destination.joinpath("payload.bin").read_bytes() == b"owned"
    finally:
        if owner.active:
            owner.close()


def test_callback_primary_survives_a_provider_secondary(tmp_path: Path) -> None:
    request = StrictWorkspaceRequest("test", tmp_path / "published", _plan())
    delegate = _TestProvider()
    owner = PublishedWorkspaceReceiptOwner()
    primary = KeyboardInterrupt("callback-primary")
    secondary = SystemExit("provider-secondary")

    class FailingProvider:
        def require_support(self) -> None:
            return None

        def run_workspace(self, request, *, receipt_owner, operation):
            try:
                delegate.run_workspace(
                    request,
                    receipt_owner=receipt_owner,
                    operation=operation,
                )
            except BaseException:  # noqa: B036 - exercise a broken provider
                raise secondary
            raise AssertionError("callback primary was not raised")

    def operation(session: StrictWorkspaceSession) -> None:
        session.write_file("payload.bin", (b"owned",))
        session.publish_validated(lambda _reader: None)
        raise primary

    try:
        with pytest.raises(KeyboardInterrupt, match="callback-primary") as caught:
            run_strict_workspace(
                FailingProvider(),
                request,
                receipt_owner=owner,
                operation=operation,
            )

        assert caught.value is primary
        assert any(
            "provider also failed after its callback" in note
            and "provider-secondary" in note
            for note in _notes(primary)
        )
        assert owner.active
    finally:
        if owner.active:
            owner.close()


def test_callback_outcome_commit_preserves_the_callback_primary(
    tmp_path: Path,
) -> None:
    request = StrictWorkspaceRequest("test", tmp_path / "published", _plan())
    delegate = _TestProvider()
    owner = PublishedWorkspaceReceiptOwner()
    primary = KeyboardInterrupt("callback-primary")
    transition = SystemExit("outcome-commit-interruption")

    class SwallowingProvider:
        def require_support(self) -> None:
            return None

        def run_workspace(self, request, *, receipt_owner, operation):
            try:
                delegate.run_workspace(
                    request,
                    receipt_owner=receipt_owner,
                    operation=operation,
                )
            except BaseException:  # noqa: B036 - exercise a broken provider
                return b"provider-swallowed-callback"
            raise AssertionError("callback primary was not raised")

    def operation(session: StrictWorkspaceSession) -> None:
        session.write_file("payload.bin", (b"owned",))
        session.publish_validated(lambda _reader: None)
        raise primary

    def run() -> object:
        return run_strict_workspace(
            SwallowingProvider(),
            request,
            receipt_owner=owner,
            operation=operation,
        )

    injected: list[bool] = []
    try:
        with pytest.raises(KeyboardInterrupt, match="callback-primary") as caught:
            _interrupt_before_store_attr(
                run,
                target_type=workspace_provider._ProviderOperationGate,
                attribute="_operation_outcome",
                error=transition,
                injected=injected,
            )

        assert caught.value is primary
        assert injected == [True]
        assert any(
            "outcome commit also failed" in note
            and "outcome-commit-interruption" in note
            for note in _notes(primary)
        )
        assert owner.active
    finally:
        if owner.active:
            owner.close()


def test_callback_outcome_read_preserves_the_callback_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = StrictWorkspaceRequest("test", tmp_path / "published", _plan())
    delegate = _TestProvider()
    owner = PublishedWorkspaceReceiptOwner()
    primary = KeyboardInterrupt("callback-primary")
    interruption = SystemExit("outcome-read-interruption")
    outcome_property = workspace_provider._ProviderOperationGate.operation_outcome
    outcome_getter = outcome_property.fget
    assert outcome_getter is not None
    reads = 0

    def interrupted_outcome(self):
        nonlocal reads
        reads += 1
        if reads == 1:
            raise interruption
        return outcome_getter(self)

    monkeypatch.setattr(
        workspace_provider._ProviderOperationGate,
        "operation_outcome",
        property(interrupted_outcome),
    )

    class SwallowingProvider:
        def require_support(self) -> None:
            return None

        def run_workspace(self, request, *, receipt_owner, operation):
            try:
                delegate.run_workspace(
                    request,
                    receipt_owner=receipt_owner,
                    operation=operation,
                )
            except BaseException:  # noqa: B036 - exercise a broken provider
                return b"provider-swallowed-callback"
            raise AssertionError("callback primary was not raised")

    def operation(session: StrictWorkspaceSession) -> None:
        session.write_file("payload.bin", (b"owned",))
        session.publish_validated(lambda _reader: None)
        raise primary

    try:
        with pytest.raises(KeyboardInterrupt, match="callback-primary") as caught:
            run_strict_workspace(
                SwallowingProvider(),
                request,
                receipt_owner=owner,
                operation=operation,
            )

        assert caught.value is primary
        assert reads == 2
        assert any(
            "outcome read also failed" in note and "outcome-read-interruption" in note
            for note in _notes(primary)
        )
        assert owner.active
    finally:
        if owner.active:
            owner.close()


def test_operation_return_without_publish_fails_and_provider_closes(
    tmp_path: Path,
) -> None:
    request = StrictWorkspaceRequest("test", tmp_path / "published", _plan())
    provider = _TestProvider()
    owner = PublishedWorkspaceReceiptOwner()

    with pytest.raises(RuntimeError, match="without publishing"):
        run_strict_workspace(
            provider,
            request,
            receipt_owner=owner,
            operation=lambda session: session.write_file("payload.bin", (b"x",)),
        )

    assert owner.state == "empty"
    assert provider.last_workspace is not None
    assert provider.last_workspace.state == "closed"


def test_adopted_workspace_rejects_wrong_destination_before_operation(
    tmp_path: Path,
) -> None:
    request = StrictWorkspaceRequest("test", tmp_path / "published", _plan())
    wrong_destination = tmp_path / "wrong-published"
    provider = _TestProvider(adopted_destination=wrong_destination)
    owner = PublishedWorkspaceReceiptOwner()
    called = False

    def operation(_session: StrictWorkspaceSession) -> None:
        nonlocal called
        called = True

    with pytest.raises(ValueError, match="destination differs"):
        run_strict_workspace(
            provider,
            request,
            receipt_owner=owner,
            operation=operation,
        )

    assert called is False
    assert owner.state == "empty"
    assert not request.destination.exists()
    assert not wrong_destination.exists()


def test_adopted_workspace_rejects_exact_binding_for_missing_request(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "published"
    destination.mkdir()
    (destination / "old").write_bytes(b"old")
    exact = capture_directory_ownership(destination)
    request = StrictWorkspaceRequest("test", destination, _plan())
    provider = _TestProvider(expected_destination=exact)
    owner = PublishedWorkspaceReceiptOwner()
    called = False

    def operation(_session: StrictWorkspaceSession) -> None:
        nonlocal called
        called = True

    with pytest.raises(ValueError, match="expectation differs"):
        run_strict_workspace(
            provider,
            request,
            receipt_owner=owner,
            operation=operation,
        )

    assert called is False
    assert owner.state == "empty"
    assert (destination / "old").read_bytes() == b"old"


def test_adopted_workspace_rejects_missing_binding_for_exact_request(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "published"
    request = StrictWorkspaceRequest(
        "test",
        destination,
        _plan(),
        destination_expectation="provider-bound-exact",
    )
    provider = _TestProvider(expected_destination=None)
    owner = PublishedWorkspaceReceiptOwner()
    called = False

    def operation(_session: StrictWorkspaceSession) -> None:
        nonlocal called
        called = True

    with pytest.raises(ValueError, match="expectation differs"):
        run_strict_workspace(
            provider,
            request,
            receipt_owner=owner,
            operation=operation,
        )

    assert called is False
    assert owner.state == "empty"
    assert not destination.exists()


def test_provider_bound_exact_request_replaces_only_captured_destination(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "published"
    destination.mkdir()
    (destination / "old").write_bytes(b"old")
    request = StrictWorkspaceRequest(
        "test",
        destination,
        _plan(),
        destination_expectation="provider-bound-exact",
    )
    provider = _TestProvider()
    owner = PublishedWorkspaceReceiptOwner()

    def operation(session: StrictWorkspaceSession) -> bytes:
        session.write_file("payload.bin", (b"owned",))
        return session.publish_validated(
            lambda reader: reader.read_bytes("payload.bin", max_bytes=32)
        )

    try:
        assert (
            run_strict_workspace(
                provider,
                request,
                receipt_owner=owner,
                operation=operation,
            )
            == b"owned"
        )
        assert owner.active
        assert owner.receipt.orphan is not None
        assert destination.joinpath("payload.bin").read_bytes() == b"owned"
    finally:
        owner.close()


def test_adopted_workspace_rechecks_destination_before_write(tmp_path: Path) -> None:
    request = StrictWorkspaceRequest("test", tmp_path / "published", _plan())
    provider = _TestProvider()
    owner = PublishedWorkspaceReceiptOwner()

    def operation(session: StrictWorkspaceSession) -> None:
        assert provider.last_workspace is not None
        provider.last_workspace._destination = tmp_path / "changed"  # type: ignore[attr-defined]  # noqa: SLF001
        session.write_file("payload.bin", (b"must-not-write",))

    with pytest.raises(ValueError, match="destination differs"):
        run_strict_workspace(
            provider,
            request,
            receipt_owner=owner,
            operation=operation,
        )

    assert owner.state == "empty"
    assert not request.destination.exists()


def test_adopted_workspace_rechecks_expectation_after_staged_validator(
    tmp_path: Path,
) -> None:
    request = StrictWorkspaceRequest("test", tmp_path / "published", _plan())
    provider = _TestProvider()
    owner = PublishedWorkspaceReceiptOwner()

    def operation(session: StrictWorkspaceSession) -> None:
        session.write_file("payload.bin", (b"owned",))

        def validate(_reader) -> None:
            assert provider.last_workspace is not None
            provider.last_workspace._expected_destination = object()  # type: ignore[attr-defined]  # noqa: SLF001

        session.publish_validated(validate)

    try:
        with pytest.raises(ValueError, match="expectation differs"):
            run_strict_workspace(
                provider,
                request,
                receipt_owner=owner,
                operation=operation,
            )

        assert owner.state == "cleanup"
        assert not request.destination.exists()
    finally:
        owner.close()


@pytest.mark.parametrize(
    "interruption",
    [
        KeyboardInterrupt("injected after receipt install"),
        SystemExit("injected after receipt install"),
    ],
)
def test_post_publish_interruption_keeps_caller_receipt_active(
    tmp_path: Path,
    interruption: BaseException,
) -> None:
    request = StrictWorkspaceRequest("test", tmp_path / "published", _plan())
    provider = _TestProvider()
    owner = PublishedWorkspaceReceiptOwner()

    def operation(session: StrictWorkspaceSession) -> None:
        session.write_file("payload.bin", (b"owned",))
        session.publish_validated(lambda _reader: None)
        raise interruption

    try:
        with pytest.raises(type(interruption), match="after receipt install"):
            run_strict_workspace(
                provider,
                request,
                receipt_owner=owner,
                operation=operation,
            )
        assert owner.active
        assert request.destination.joinpath("payload.bin").read_bytes() == b"owned"
    finally:
        owner.close()


def test_shared_support_gate_runs_before_provider_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = StrictWorkspaceRequest("test", tmp_path / "published", _plan())
    provider = _TestProvider()
    owner = PublishedWorkspaceReceiptOwner()

    def unsupported() -> None:
        raise UnsupportedWorkspaceCreation("injected unsupported host")

    monkeypatch.setattr(
        workspace_provider,
        "require_owned_workspace_publication_support",
        unsupported,
    )
    with pytest.raises(UnsupportedWorkspaceCreation, match="unsupported host"):
        run_strict_workspace(
            provider,
            request,
            receipt_owner=owner,
            operation=lambda _session: None,
        )

    assert provider.support_checks == provider.runs == 0
    assert not request.destination.exists()
    assert not request.destination.parent.joinpath(".provider-stage").exists()


def test_shared_support_gate_precedes_provider_attribute_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class SideEffectProvider:
        @property
        def require_support(self):
            events.append("provider-getattr")
            return lambda: None

        @property
        def run_workspace(self):
            events.append("provider-run-getattr")
            return lambda *_args, **_kwargs: None

    def unsupported() -> None:
        events.append("shared-gate")
        raise UnsupportedWorkspaceCreation("unsupported")

    monkeypatch.setattr(
        workspace_provider,
        "require_owned_workspace_publication_support",
        unsupported,
    )
    with pytest.raises(UnsupportedWorkspaceCreation, match="unsupported"):
        run_strict_workspace(
            SideEffectProvider(),
            StrictWorkspaceRequest("test", tmp_path / "published", _plan()),
            receipt_owner=PublishedWorkspaceReceiptOwner(),
            operation=lambda _session: None,
        )

    assert events == ["shared-gate"]


def test_session_revocation_recovers_from_store_cancellation(
    tmp_path: Path,
) -> None:
    request = StrictWorkspaceRequest("test", tmp_path / "published", _plan())
    provider = _TestProvider()
    owner = PublishedWorkspaceReceiptOwner()
    escaped: list[StrictWorkspaceSession] = []

    def operation(session: StrictWorkspaceSession) -> object:
        escaped.append(session)
        return object()

    def run() -> object:
        return run_strict_workspace(
            provider,
            request,
            receipt_owner=owner,
            operation=operation,
        )

    injected: list[bool] = []
    with pytest.raises(KeyboardInterrupt, match="session revoke"):
        _interrupt_before_store_attr(
            run,
            target_type=workspace_provider._AdoptedWorkspaceSession,
            attribute="_active",
            error=KeyboardInterrupt("injected session revoke"),
            injected=injected,
        )

    assert injected == [True]
    assert owner.state == "empty"
    assert provider.last_workspace is not None
    assert provider.last_workspace.state == "closed"
    with pytest.raises(RuntimeError, match="no longer active"):
        _ = escaped[0].request


@pytest.mark.parametrize(
    "primary_type",
    [KeyboardInterrupt, SystemExit, GeneratorExit],
)
def test_operation_primary_survives_session_revocation_failure(
    tmp_path: Path,
    primary_type: type[BaseException],
) -> None:
    request = StrictWorkspaceRequest("test", tmp_path / "published", _plan())
    provider = _TestProvider()
    owner = PublishedWorkspaceReceiptOwner()
    primary = primary_type("operation-primary")

    def operation(_session: StrictWorkspaceSession) -> None:
        raise primary

    def run() -> object:
        return run_strict_workspace(
            provider,
            request,
            receipt_owner=owner,
            operation=operation,
        )

    injected: list[bool] = []
    with pytest.raises(primary_type, match="operation-primary") as caught:
        _interrupt_before_store_attr(
            run,
            target_type=workspace_provider._AdoptedWorkspaceSession,
            attribute="_active",
            error=RuntimeError("session-revocation-secondary"),
            injected=injected,
        )

    assert caught.value is primary
    assert injected == [True]
    assert any(
        "session revocation also failed" in note
        and "session-revocation-secondary" in note
        for note in _notes(primary)
    )
    assert owner.state == "empty"


@pytest.mark.parametrize(
    "primary_type",
    [KeyboardInterrupt, SystemExit, GeneratorExit],
)
def test_provider_primary_survives_callback_revocation_failure(
    tmp_path: Path,
    primary_type: type[BaseException],
) -> None:
    primary = primary_type("provider-primary")

    class FailingProvider:
        def require_support(self) -> None:
            return None

        def run_workspace(self, *_args, **_kwargs) -> object:
            raise primary

    def run() -> object:
        return run_strict_workspace(
            FailingProvider(),
            StrictWorkspaceRequest("test", tmp_path / "published", _plan()),
            receipt_owner=PublishedWorkspaceReceiptOwner(),
            operation=lambda _session: None,
        )

    injected: list[bool] = []
    with pytest.raises(primary_type, match="provider-primary") as caught:
        _interrupt_before_store_attr(
            run,
            target_type=workspace_provider._ProviderOperationGate,
            attribute="_active",
            error=RuntimeError("provider-revocation-secondary"),
            injected=injected,
        )

    assert caught.value is primary
    assert injected == [True]
    assert any(
        "provider callback revocation also failed" in note
        and "provider-revocation-secondary" in note
        for note in _notes(primary)
    )


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
@pytest.mark.parametrize(
    "entry",
    ["request", "write", "publish", "invalidate"],
)
def test_session_pid_boundary_precedes_inherited_gate(
    tmp_path: Path,
    entry: str,
) -> None:
    request = StrictWorkspaceRequest("test", tmp_path / "published", _plan())
    owner = PublishedWorkspaceReceiptOwner()

    def operation(session: StrictWorkspaceSession) -> None:
        assert isinstance(session, workspace_provider._AdoptedWorkspaceSession)
        gate_held = threading.Event()
        release_gate = threading.Event()
        holder_finished = threading.Event()
        holder_values: list[object] = []
        holder_errors: list[BaseException] = []

        def hold_gate() -> None:
            def block() -> None:
                gate_held.set()
                if not release_gate.wait(5):
                    raise TimeoutError("session gate holder was not released")

            session._gate.run(block)

        holder = threading.Thread(
            target=_run_thread,
            kwargs={
                "callback": hold_gate,
                "values": holder_values,
                "errors": holder_errors,
                "finished": holder_finished,
            },
            daemon=True,
        )
        holder.start()
        if not gate_held.wait(5):
            raise TimeoutError("session gate holder did not acquire the gate")

        def enter_from_child() -> object:
            if entry == "request":
                return session.request
            if entry == "write":
                return session.write_file("payload.bin", (b"child",))
            if entry == "publish":
                return session.publish_validated(lambda _reader: None)
            session._invalidate()
            return None

        try:
            exit_code, payload = _fork_expect_pid_boundary(
                enter_from_child,
                expected_message=(
                    "strict workspace session cannot cross a PID boundary"
                ),
            )
        finally:
            release_gate.set()
            assert holder_finished.wait(5)
            holder.join(timeout=1)

        assert not holder.is_alive()
        assert holder_values == [None]
        assert holder_errors == []
        assert exit_code == 0, payload or "forked session entry hit its alarm"
        assert payload == "expected PID boundary"

        session.write_file("payload.bin", (b"parent",))
        session.publish_validated(lambda _reader: None)

    try:
        run_strict_workspace(
            _TestProvider(),
            request,
            receipt_owner=owner,
            operation=operation,
        )
        assert owner.active
        assert request.destination.joinpath("payload.bin").read_bytes() == b"parent"
    finally:
        if owner.active:
            owner.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
@pytest.mark.parametrize("entry", ["callback", "called", "revoke"])
def test_provider_operation_pid_boundary_precedes_inherited_gate(
    tmp_path: Path,
    entry: str,
) -> None:
    request = StrictWorkspaceRequest("test", tmp_path / "published", _plan())
    owner = PublishedWorkspaceReceiptOwner()

    def operation(session: StrictWorkspaceSession) -> None:
        callback_started = threading.Event()
        release_callback = threading.Event()
        worker_finished = threading.Event()
        worker_values: list[object] = []
        worker_errors: list[BaseException] = []

        operation_gate = workspace_provider._ProviderOperationGate(
            request,
            lambda _session: None,
        )

        def hold_gate() -> None:
            def block() -> None:
                callback_started.set()
                if not release_callback.wait(5):
                    raise TimeoutError("provider operation gate was not released")

            operation_gate._lock.run(block)

        worker = threading.Thread(
            target=_run_thread,
            kwargs={
                "callback": hold_gate,
                "values": worker_values,
                "errors": worker_errors,
                "finished": worker_finished,
            },
            daemon=True,
        )
        worker.start()
        if not callback_started.wait(5):
            raise TimeoutError("provider operation did not acquire its gate")

        def enter_from_child() -> object:
            if entry == "callback":
                return operation_gate(session)
            if entry == "called":
                return operation_gate.called
            operation_gate.revoke()
            return None

        try:
            exit_code, payload = _fork_expect_pid_boundary(
                enter_from_child,
                expected_message=(
                    "strict workspace provider operation cannot cross a PID boundary"
                ),
            )
        finally:
            release_callback.set()
            assert worker_finished.wait(5)
            worker.join(timeout=1)

        assert not worker.is_alive()
        assert worker_values == [None]
        assert worker_errors == []
        assert exit_code == 0, payload or "forked provider entry hit its alarm"
        assert payload == "expected PID boundary"
        operation_gate.revoke()

        session.write_file("payload.bin", (b"parent",))
        session.publish_validated(lambda _reader: None)

    try:
        run_strict_workspace(
            _TestProvider(),
            request,
            receipt_owner=owner,
            operation=operation,
        )
        assert owner.active
        assert request.destination.joinpath("payload.bin").read_bytes() == b"parent"
    finally:
        if owner.active:
            owner.close()


def test_session_invalidation_waits_for_blocked_write_and_rejects_late_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = StrictWorkspaceRequest("test", tmp_path / "published", _plan())
    provider = _TestProvider()
    owner = PublishedWorkspaceReceiptOwner()
    write_started = threading.Event()
    release_write = threading.Event()
    allow_late_publish = threading.Event()
    operation_returned = threading.Event()
    invalidation_started = threading.Event()
    worker_finished = threading.Event()
    runner_finished = threading.Event()
    worker_values: list[object] = []
    worker_errors: list[BaseException] = []
    runner_values: list[object] = []
    runner_errors: list[BaseException] = []
    workers: list[threading.Thread] = []
    real_invalidate = workspace_provider._AdoptedWorkspaceSession._invalidate

    def track_invalidation(session) -> None:
        invalidation_started.set()
        real_invalidate(session)

    monkeypatch.setattr(
        workspace_provider._AdoptedWorkspaceSession,
        "_invalidate",
        track_invalidation,
    )

    def chunks():
        write_started.set()
        if not release_write.wait(5):
            raise TimeoutError("blocked write was not released")
        yield b"owned"

    def operation(session: StrictWorkspaceSession) -> str:
        def work() -> object:
            session.write_file("payload.bin", chunks())
            if not allow_late_publish.wait(5):
                raise TimeoutError("late publication was not released")
            return session.publish_validated(lambda _reader: None)

        worker = threading.Thread(
            target=_run_thread,
            kwargs={
                "callback": work,
                "values": worker_values,
                "errors": worker_errors,
                "finished": worker_finished,
            },
            daemon=True,
        )
        workers.append(worker)
        worker.start()
        if not write_started.wait(5):
            raise TimeoutError("worker did not enter its write")
        operation_returned.set()
        return "operation-returned"

    runner = threading.Thread(
        target=_run_thread,
        kwargs={
            "callback": lambda: run_strict_workspace(
                provider,
                request,
                receipt_owner=owner,
                operation=operation,
            ),
            "values": runner_values,
            "errors": runner_errors,
            "finished": runner_finished,
        },
        daemon=True,
    )
    try:
        runner.start()
        assert operation_returned.wait(5)
        assert invalidation_started.wait(5)
        assert not runner_finished.wait(0.1)
        release_write.set()
        assert runner_finished.wait(5)
        allow_late_publish.set()
        assert worker_finished.wait(5)
        runner.join(timeout=1)
        workers[0].join(timeout=1)
    finally:
        release_write.set()
        allow_late_publish.set()

    assert runner_values == []
    assert len(runner_errors) == 1
    assert isinstance(runner_errors[0], RuntimeError)
    assert "without publishing" in str(runner_errors[0])
    assert worker_values == []
    assert len(worker_errors) == 1
    assert isinstance(worker_errors[0], RuntimeError)
    assert "no longer active" in str(worker_errors[0])
    assert owner.state == "empty"
    assert not request.destination.exists()


def test_session_invalidation_waits_for_blocked_staged_validator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = StrictWorkspaceRequest("test", tmp_path / "published", _plan())
    provider = _TestProvider()
    owner = PublishedWorkspaceReceiptOwner()
    staged_validator_started = threading.Event()
    release_validator = threading.Event()
    operation_returned = threading.Event()
    invalidation_started = threading.Event()
    worker_finished = threading.Event()
    runner_finished = threading.Event()
    worker_values: list[object] = []
    worker_errors: list[BaseException] = []
    runner_values: list[object] = []
    runner_errors: list[BaseException] = []
    workers: list[threading.Thread] = []
    validations = 0
    real_invalidate = workspace_provider._AdoptedWorkspaceSession._invalidate

    def track_invalidation(session) -> None:
        invalidation_started.set()
        real_invalidate(session)

    monkeypatch.setattr(
        workspace_provider._AdoptedWorkspaceSession,
        "_invalidate",
        track_invalidation,
    )

    def operation(session: StrictWorkspaceSession) -> str:
        def work() -> object:
            session.write_file("payload.bin", (b"owned",))

            def validate(_reader) -> bytes:
                nonlocal validations
                validations += 1
                if validations == 1:
                    staged_validator_started.set()
                    if not release_validator.wait(5):
                        raise TimeoutError("staged validator was not released")
                return b"validated"

            return session.publish_validated(validate)

        worker = threading.Thread(
            target=_run_thread,
            kwargs={
                "callback": work,
                "values": worker_values,
                "errors": worker_errors,
                "finished": worker_finished,
            },
            daemon=True,
        )
        workers.append(worker)
        worker.start()
        if not staged_validator_started.wait(5):
            raise TimeoutError("worker did not enter the staged validator")
        operation_returned.set()
        return "operation-returned"

    runner = threading.Thread(
        target=_run_thread,
        kwargs={
            "callback": lambda: run_strict_workspace(
                provider,
                request,
                receipt_owner=owner,
                operation=operation,
            ),
            "values": runner_values,
            "errors": runner_errors,
            "finished": runner_finished,
        },
        daemon=True,
    )
    try:
        runner.start()
        assert operation_returned.wait(5)
        assert invalidation_started.wait(5)
        assert not runner_finished.wait(0.1)
        release_validator.set()
        assert runner_finished.wait(5)
        assert worker_finished.wait(5)
        runner.join(timeout=1)
        workers[0].join(timeout=1)
        assert runner_errors == []
        assert runner_values == ["operation-returned"]
        assert worker_errors == []
        assert worker_values == [b"validated"]
        assert validations == 2
        assert owner.active
        assert request.destination.joinpath("payload.bin").read_bytes() == b"owned"
    finally:
        release_validator.set()
        if owner.active:
            owner.close()


def test_provider_cannot_retain_operation_callback(tmp_path: Path) -> None:
    events: list[object] = []

    class EscapingProvider:
        escaped = None

        def require_support(self) -> None:
            return None

        def run_workspace(self, _request, *, receipt_owner, operation):
            del receipt_owner
            self.escaped = operation
            raise RuntimeError("provider returned early")

    provider = EscapingProvider()
    with pytest.raises(RuntimeError, match="returned early"):
        run_strict_workspace(
            provider,
            StrictWorkspaceRequest("test", tmp_path / "published", _plan()),
            receipt_owner=PublishedWorkspaceReceiptOwner(),
            operation=lambda session: events.append(session),
        )

    assert provider.escaped is not None
    with pytest.raises(RuntimeError, match="no longer active"):
        provider.escaped("late-session")
    assert events == []


def test_provider_cannot_invoke_operation_with_unbound_session(
    tmp_path: Path,
) -> None:
    called = False

    class UnboundProvider:
        def require_support(self) -> None:
            return None

        def run_workspace(self, _request, *, receipt_owner, operation):
            del receipt_owner
            return operation(object())

    def operation(_session: StrictWorkspaceSession) -> None:
        nonlocal called
        called = True

    with pytest.raises(TypeError, match="adopted session"):
        run_strict_workspace(
            UnboundProvider(),
            StrictWorkspaceRequest("test", tmp_path / "published", _plan()),
            receipt_owner=PublishedWorkspaceReceiptOwner(),
            operation=operation,
        )

    assert called is False


def test_provider_rejects_forged_session_subclass_before_user_callback(
    tmp_path: Path,
) -> None:
    request = StrictWorkspaceRequest("test", tmp_path / "published", _plan())
    called = 0

    class ForgedSession(workspace_provider._AdoptedWorkspaceSession):
        def _require_operation_provenance(self, *_args, **_kwargs) -> None:
            return None

    class ForgingProvider:
        def require_support(self) -> None:
            return None

        def run_workspace(self, _request, *, receipt_owner, operation):
            forged = ForgedSession(
                request,
                object(),  # type: ignore[arg-type]
                receipt_owner,
                operation_provenance=object(),
            )
            return operation(forged)

    def user_operation(_session: StrictWorkspaceSession) -> None:
        nonlocal called
        called += 1

    with pytest.raises(TypeError, match="adopted session"):
        run_strict_workspace(
            ForgingProvider(),
            request,
            receipt_owner=PublishedWorkspaceReceiptOwner(),
            operation=user_operation,
        )

    assert called == 0


def test_provider_rejects_exact_unprovenanced_session_before_user_callback(
    tmp_path: Path,
) -> None:
    request = StrictWorkspaceRequest("test", tmp_path / "published", _plan())
    called = 0
    field_forgery_rejected = False

    class FakeWorkspace:
        @property
        def plan(self) -> WorkspacePlan:
            raise AssertionError("forged workspace plan was inspected")

    class ForgingProvider:
        def require_support(self) -> None:
            return None

        def run_workspace(self, _request, *, receipt_owner, operation):
            nonlocal field_forgery_rejected
            forged_provenance = object()
            forged = workspace_provider._AdoptedWorkspaceSession(
                request,
                FakeWorkspace(),  # type: ignore[arg-type]
                receipt_owner,
                operation_provenance=forged_provenance,
            )
            try:
                operation._session_binding = (forged, forged_provenance)
            except AttributeError:
                field_forgery_rejected = True
            return operation(forged)

    def user_operation(_session: StrictWorkspaceSession) -> None:
        nonlocal called
        called += 1

    with pytest.raises(TypeError, match="provenance is unbound"):
        run_strict_workspace(
            ForgingProvider(),
            request,
            receipt_owner=PublishedWorkspaceReceiptOwner(),
            operation=user_operation,
        )

    assert called == 0
    assert field_forgery_rejected is True


def test_double_publish_fails_but_retains_active_receipt(tmp_path: Path) -> None:
    request = StrictWorkspaceRequest("test", tmp_path / "published", _plan())
    provider = _TestProvider()
    owner = PublishedWorkspaceReceiptOwner()

    def operation(session: StrictWorkspaceSession) -> None:
        session.write_file("payload.bin", (b"owned",))
        session.publish_validated(lambda _reader: None)
        session.publish_validated(lambda _reader: None)

    try:
        with pytest.raises(RuntimeError, match="already published"):
            run_strict_workspace(
                provider,
                request,
                receipt_owner=owner,
                operation=operation,
            )
        assert owner.active
    finally:
        owner.close()


def test_provider_wrong_request_is_rejected_before_operation(
    tmp_path: Path,
) -> None:
    request = StrictWorkspaceRequest("test", tmp_path / "published", _plan())
    wrong_request = StrictWorkspaceRequest(
        "test",
        tmp_path / "wrong-published",
        request.plan,
    )
    delegate = _TestProvider()

    class WrongProvider:
        def require_support(self) -> None:
            return None

        def run_workspace(self, _request, *, receipt_owner, operation):
            return delegate.run_workspace(
                wrong_request,
                receipt_owner=receipt_owner,
                operation=operation,
            )

    owner = PublishedWorkspaceReceiptOwner()
    called = False

    def operation(session: StrictWorkspaceSession) -> None:
        nonlocal called
        called = True
        session.write_file("payload.bin", (b"owned",))
        session.publish_validated(lambda _reader: None)

    with pytest.raises(ValueError, match="another request"):
        run_strict_workspace(
            WrongProvider(),
            request,
            receipt_owner=owner,
            operation=operation,
        )

    assert called is False
    assert owner.state == "empty"
    assert not wrong_request.destination.exists()


def test_provider_return_without_invoking_operation_is_rejected(tmp_path: Path) -> None:
    class BrokenProvider:
        def require_support(self) -> None:
            return None

        def run_workspace(self, *_args, **_kwargs) -> object:
            return object()

    request = StrictWorkspaceRequest("test", tmp_path / "published", _plan())
    with pytest.raises(RuntimeError, match="did not invoke its operation"):
        run_strict_workspace(
            BrokenProvider(),
            request,
            receipt_owner=PublishedWorkspaceReceiptOwner(),
            operation=lambda _session: None,
        )
