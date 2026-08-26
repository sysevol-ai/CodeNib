# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Callable

import pytest

import codenib._workspace_provider as workspace_provider
from codenib._atomic_directory import capture_directory_ownership
from codenib._captured_directory import (
    OwnedWorkspaceAuthority,
    PublishedWorkspaceDestinationBinding,
    PublishedWorkspaceReceiptOwner,
    UnsupportedWorkspaceCreation,
    WorkspaceFile,
    WorkspacePlan,
)

_DEFAULT_BINDING = object()
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


def _active_replacement_source_gate(
    request: StrictWorkspaceRequest,
    source_owner: PublishedWorkspaceReceiptOwner,
) -> workspace_provider._ReplacementSourceGate:
    lifecycle = [False, False, False, True]
    gate = workspace_provider._ReplacementSourceGate(
        request,
        source_owner,
        lifecycle=lifecycle,
    )
    lifecycle[3] = False
    lifecycle[0] = True
    lifecycle[2] = True
    return gate


def _active_provider_operation_gate(
    request: StrictWorkspaceRequest,
    operation: Callable[[StrictWorkspaceSession], object],
) -> workspace_provider._ProviderOperationGate:
    lifecycle = [False, False, True]
    gate = workspace_provider._ProviderOperationGate(
        request,
        operation,
        lifecycle=lifecycle,
    )
    lifecycle[2] = False
    lifecycle[0] = True
    return gate


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
        destination_binding: object = _DEFAULT_BINDING,
    ) -> None:
        self.support_checks = 0
        self.runs = 0
        self.last_workspace: OwnedWorkspaceAuthority | None = None
        self.last_replacement_source: object | None = None
        self.adopted_destination = adopted_destination
        self.destination_binding = destination_binding

    def require_support(self) -> None:
        self.support_checks += 1

    def run_workspace(
        self,
        request: StrictWorkspaceRequest,
        *,
        receipt_owner: PublishedWorkspaceReceiptOwner,
        operation: Callable[[StrictWorkspaceSession], object],
        _replacement_source: object | None = None,
    ) -> object:
        self.runs += 1
        self.last_replacement_source = _replacement_source
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
        destination_binding = self.destination_binding
        if destination_binding is _DEFAULT_BINDING:
            destination_binding = request.destination_binding
        try:
            workspace.adopt(
                destination=destination,
                stage_name=stage.name,
                parent_descriptor=parent_descriptor,
                root_descriptor=root_descriptor,
                directory_descriptors={},
                plan=request.plan,
                destination_binding=destination_binding,  # type: ignore[arg-type]
            )
            return run_adopted_workspace_operation(
                request,
                workspace=workspace,
                receipt_owner=receipt_owner,
                operation=operation,
                _replacement_timeout_ns=(
                    1_000_000_000 if request.destination_binding is not None else None
                ),
            )
        finally:
            os.close(root_descriptor)
            os.close(parent_descriptor)
            if receipt_owner.state == "empty":
                workspace.close()


def _publish_generation(
    destination: Path,
    *,
    payload: bytes = b"same bytes",
) -> PublishedWorkspaceReceiptOwner:
    owner = PublishedWorkspaceReceiptOwner()
    request = StrictWorkspaceRequest("binding-source", destination, _plan())

    def publish(session: StrictWorkspaceSession) -> None:
        session.write_file("payload.bin", (payload,))
        session.publish_validated(lambda _reader: None)

    try:
        run_strict_workspace(
            _TestProvider(),
            request,
            receipt_owner=owner,
            operation=publish,
        )
    except BaseException:
        owner.close()
        raise
    return owner


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="strict workspace publication currently requires Linux directory fsync",
)
def test_run_strict_workspace_preserves_exact_staged_cancellation(
    tmp_path: Path,
) -> None:
    request = StrictWorkspaceRequest("test", tmp_path / "published", _plan())
    provider = _TestProvider()
    owner = PublishedWorkspaceReceiptOwner()
    cancellation = KeyboardInterrupt("exact staged cancellation")
    armed = False

    def check_cancelled() -> None:
        if armed:
            raise cancellation

    def operation(session: StrictWorkspaceSession) -> None:
        nonlocal armed
        session.write_file("payload.bin", (b"owned",))

        def validate_staged(_reader) -> None:
            nonlocal armed
            armed = True

        def forbidden_published(_reader) -> None:
            raise AssertionError("cancelled stage reached published validation")

        session.publish_validated(
            validate_staged,
            validate_published_destination=forbidden_published,
        )

    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            run_strict_workspace(
                provider,
                request,
                receipt_owner=owner,
                operation=operation,
                check_cancelled=check_cancelled,
            )

        assert caught.value is cancellation
        assert owner.state == "cleanup"
        assert not request.destination.exists()
        assert provider.last_workspace is not None
        assert provider.last_workspace.state == "failed"
        owner.close()
        assert owner.closed
        assert provider.last_workspace.state == "closed"
    finally:
        if not owner.closed:
            owner.close()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="strict workspace publication currently requires Linux directory fsync",
)
def test_run_strict_workspace_defers_latched_published_stop_until_receipted(
    tmp_path: Path,
) -> None:
    request = StrictWorkspaceRequest("test", tmp_path / "published", _plan())
    owner = PublishedWorkspaceReceiptOwner()
    cancellation = KeyboardInterrupt("latched during published validation")
    published = False

    def check_cancelled() -> None:
        if published:
            raise cancellation

    def operation(session: StrictWorkspaceSession) -> bytes:
        session.write_file("payload.bin", (b"owned",))

        def validate_staged(_reader) -> bytes:
            return b"staged"

        def validate_published(_reader) -> bytes:
            nonlocal published
            published = True
            return b"published"

        return session.publish_validated(
            validate_staged,
            validate_published_destination=validate_published,
        )

    try:
        result = run_strict_workspace(
            _TestProvider(),
            request,
            receipt_owner=owner,
            operation=operation,
            check_cancelled=check_cancelled,
        )

        assert result == b"published"
        assert published
        assert owner.active
        receipt = owner.receipt
        assert receipt.path == request.destination
        assert receipt.plan == request.plan
        assert request.destination.joinpath("payload.bin").read_bytes() == b"owned"
        with pytest.raises(KeyboardInterrupt) as caught:
            check_cancelled()
        assert caught.value is cancellation
    finally:
        owner.close()


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
    assert request.destination_binding is None
    assert request.destination_expectation == "missing"
    with pytest.raises(ValueError, match="purpose"):
        StrictWorkspaceRequest("bad\nname", destination, _plan())
    with pytest.raises(TypeError, match="unexpected keyword"):
        StrictWorkspaceRequest(
            "test",
            destination,
            _plan(),
            destination_expectation="provider-bound-exact",  # type: ignore[call-arg]
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


def test_request_accepts_only_exact_binding_for_its_lexical_destination(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "parent" / "published"
    source_owner = _publish_generation(destination)
    try:
        binding = source_owner.destination_binding
        request = StrictWorkspaceRequest(
            "replace",
            destination,
            _plan(),
            destination_binding=binding,
        )
        assert request.destination_binding is binding
        assert request.destination_expectation == "provider-bound-exact"

        with pytest.raises(ValueError, match="binding differs from its destination"):
            StrictWorkspaceRequest(
                "replace",
                tmp_path / "wrong" / "published",
                _plan(),
                destination_binding=binding,
            )
        with pytest.raises(TypeError, match="destination binding is invalid"):
            StrictWorkspaceRequest(
                "replace",
                destination,
                _plan(),
                destination_binding=capture_directory_ownership(destination),  # type: ignore[arg-type]
            )

        class DerivedBinding(PublishedWorkspaceDestinationBinding):
            pass

        forged = object.__new__(DerivedBinding)
        object.__setattr__(forged, "destination", binding.destination)
        object.__setattr__(forged, "parent_identity", binding.parent_identity)
        object.__setattr__(forged, "ownership", binding.ownership)
        with pytest.raises(TypeError, match="destination binding is invalid"):
            StrictWorkspaceRequest(
                "replace",
                destination,
                _plan(),
                destination_binding=forged,
            )
    finally:
        source_owner.close()


def test_exact_request_requires_its_separate_active_source_before_provider(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "published"
    source_owner = _publish_generation(destination)
    request = StrictWorkspaceRequest(
        "replace",
        destination,
        _plan(),
        destination_binding=source_owner.destination_binding,
    )
    provider = _TestProvider()
    output_owner = PublishedWorkspaceReceiptOwner()
    try:
        with pytest.raises(TypeError, match="exact receipt owner"):
            run_strict_workspace(
                provider,
                request,
                receipt_owner=output_owner,
                operation=lambda _session: None,
            )
        assert provider.support_checks == provider.runs == 0
        assert source_owner.active
        assert output_owner.state == "empty"
        assert destination.joinpath("payload.bin").read_bytes() == b"same bytes"
    finally:
        output_owner.close()
        source_owner.close()


def test_exact_request_rejects_another_active_source_by_binding_identity(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "published"
    source_owner = _publish_generation(destination)
    other_owner = _publish_generation(tmp_path / "other")
    request = StrictWorkspaceRequest(
        "replace",
        destination,
        _plan(),
        destination_binding=source_owner.destination_binding,
    )
    provider = _TestProvider()
    output_owner = PublishedWorkspaceReceiptOwner()
    try:
        with pytest.raises(ValueError, match="differs from its request"):
            run_strict_workspace(
                provider,
                request,
                receipt_owner=output_owner,
                operation=lambda _session: None,
                source_owner=other_owner,
            )
        assert provider.support_checks == provider.runs == 0
        assert source_owner.active and other_owner.active
        assert output_owner.state == "empty"
    finally:
        output_owner.close()
        other_owner.close()
        source_owner.close()


def test_missing_request_rejects_replacement_source_before_provider(
    tmp_path: Path,
) -> None:
    source_owner = _publish_generation(tmp_path / "source")
    request = StrictWorkspaceRequest("missing", tmp_path / "output", _plan())
    provider = _TestProvider()
    output_owner = PublishedWorkspaceReceiptOwner()
    try:
        with pytest.raises(ValueError, match="missing strict workspace"):
            run_strict_workspace(
                provider,
                request,
                receipt_owner=output_owner,
                operation=lambda _session: None,
                source_owner=source_owner,
            )
        assert provider.support_checks == provider.runs == 0
        assert output_owner.state == "empty"
        assert not request.destination.exists()
    finally:
        output_owner.close()
        source_owner.close()


def test_exact_source_constructor_return_interruption_retains_inactive_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    source_owner = _publish_generation(destination)
    request = StrictWorkspaceRequest(
        "replace",
        destination,
        _plan(),
        destination_binding=source_owner.destination_binding,
    )
    output_owner = PublishedWorkspaceReceiptOwner()
    provider = _TestProvider()
    failure = KeyboardInterrupt("replacement constructor return interruption")
    retained: list[workspace_provider._ReplacementSourceGate] = []
    gate_type = workspace_provider._ReplacementSourceGate
    initialize = gate_type.__init__

    def interrupted_init(self, *args, **kwargs) -> None:
        initialize(self, *args, **kwargs)
        retained.append(self)
        raise failure

    monkeypatch.setattr(gate_type, "__init__", interrupted_init)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            run_strict_workspace(
                provider,
                request,
                receipt_owner=output_owner,
                operation=lambda _session: None,
                source_owner=source_owner,
            )

        assert caught.value is failure
        assert len(retained) == 1
        gate = retained[0]
        assert gate._lifecycle == [False, False, False, False]
        with pytest.raises(RuntimeError, match="no longer active"):
            gate.bind(  # type: ignore[arg-type]
                object(),
                object(),
                ".late-constructor-stage",
                request.plan,
            )
        assert provider.support_checks == provider.runs == 0
        assert output_owner.state == "empty"
        assert (
            source_owner.consume(
                lambda _receipt, reader: reader.read_bytes(
                    "payload.bin",
                    max_bytes=32,
                )
            )
            == b"same bytes"
        )
    finally:
        output_owner.close()
        source_owner.close()


@pytest.mark.parametrize("exact", [False, True], ids=["missing", "exact"])
def test_operation_constructor_return_interruption_retains_inactive_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exact: bool,
) -> None:
    destination = tmp_path / "published"
    source_owner = _publish_generation(destination) if exact else None
    request = StrictWorkspaceRequest(
        "replace" if exact else "missing",
        destination,
        _plan(),
        destination_binding=(
            source_owner.destination_binding if source_owner is not None else None
        ),
    )
    output_owner = PublishedWorkspaceReceiptOwner()
    provider = _TestProvider()
    failure = KeyboardInterrupt("operation constructor return interruption")
    retained: list[workspace_provider._ProviderOperationGate] = []
    gate_type = workspace_provider._ProviderOperationGate
    initialize = gate_type.__init__

    def interrupted_init(self, *args, **kwargs) -> None:
        initialize(self, *args, **kwargs)
        retained.append(self)
        raise failure

    monkeypatch.setattr(gate_type, "__init__", interrupted_init)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            run_strict_workspace(
                provider,
                request,
                receipt_owner=output_owner,
                operation=lambda _session: None,
                source_owner=source_owner,
            )

        assert caught.value is failure
        assert len(retained) == 1
        gate = retained[0]
        assert gate._lifecycle == [False, False, False]
        with pytest.raises(RuntimeError, match="no longer active"):
            gate(object())  # type: ignore[arg-type]
        if exact:
            replacement_source = gate._replacement_source
            assert replacement_source is not None
            assert replacement_source._lifecycle == [False, False, False, False]
            with pytest.raises(RuntimeError, match="no longer active"):
                replacement_source.bind(  # type: ignore[arg-type]
                    object(),
                    object(),
                    ".late-operation-constructor-stage",
                    request.plan,
                )
            assert provider.support_checks == 0
        else:
            assert provider.support_checks == 1
        assert provider.runs == 0
        assert output_owner.state == "empty"
        if source_owner is None:
            assert not destination.exists()
        else:
            assert (
                source_owner.consume(
                    lambda _receipt, reader: reader.read_bytes(
                        "payload.bin",
                        max_bytes=32,
                    )
                )
                == b"same bytes"
            )
    finally:
        output_owner.close()
        if source_owner is not None:
            source_owner.close()


@pytest.mark.parametrize("exact", [False, True], ids=["missing", "exact"])
def test_activation_interruption_revokes_outer_owned_gate_cells(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exact: bool,
) -> None:
    destination = tmp_path / "published"
    source_owner = _publish_generation(destination) if exact else None
    request = StrictWorkspaceRequest(
        "replace" if exact else "missing",
        destination,
        _plan(),
        destination_binding=(
            source_owner.destination_binding if source_owner is not None else None
        ),
    )
    output_owner = PublishedWorkspaceReceiptOwner()
    provider = _TestProvider()
    failure = KeyboardInterrupt("gate activation interruption")
    operation_gates: list[workspace_provider._ProviderOperationGate] = []
    source_gates: list[workspace_provider._ReplacementSourceGate] = []
    operation_init = workspace_provider._ProviderOperationGate.__init__
    source_init = workspace_provider._ReplacementSourceGate.__init__

    def capture_operation_gate(self, *args, **kwargs) -> None:
        operation_init(self, *args, **kwargs)
        operation_gates.append(self)

    def capture_source_gate(self, *args, **kwargs) -> None:
        source_init(self, *args, **kwargs)
        source_gates.append(self)

    monkeypatch.setattr(
        workspace_provider._ProviderOperationGate,
        "__init__",
        capture_operation_gate,
    )
    monkeypatch.setattr(
        workspace_provider._ReplacementSourceGate,
        "__init__",
        capture_source_gate,
    )
    injected: list[bool] = []
    target_code = run_strict_workspace.__code__
    previous_trace = sys.gettrace()

    def interrupt_active_cells(frame, event, _argument):
        if not injected and event == "line" and frame.f_code is target_code:
            operation_lifecycle = frame.f_locals.get("operation_lifecycle")
            replacement_lifecycle = frame.f_locals.get("replacement_lifecycle")
            operation_active = (
                type(operation_lifecycle) is list and operation_lifecycle[0]
            )
            replacement_active = (
                type(replacement_lifecycle) is list
                and replacement_lifecycle[0]
                and replacement_lifecycle[2]
            )
            if operation_active and (not exact or replacement_active):
                injected.append(True)
                raise failure
        return interrupt_active_cells

    try:
        sys.settrace(interrupt_active_cells)
        with pytest.raises(KeyboardInterrupt) as caught:
            run_strict_workspace(
                provider,
                request,
                receipt_owner=output_owner,
                operation=lambda _session: None,
                source_owner=source_owner,
            )
    finally:
        sys.settrace(previous_trace)

    try:
        assert caught.value is failure
        assert injected == [True]
        assert len(operation_gates) == 1
        operation_gate = operation_gates[0]
        assert operation_gate._lifecycle == [False, False, False]
        with pytest.raises(RuntimeError, match="no longer active"):
            operation_gate(object())  # type: ignore[arg-type]
        if exact:
            assert len(source_gates) == 1
            source_gate = source_gates[0]
            assert source_gate._lifecycle == [False, False, False, False]
            with pytest.raises(RuntimeError, match="no longer active"):
                source_gate.bind(  # type: ignore[arg-type]
                    object(),
                    object(),
                    ".late-activation-stage",
                    request.plan,
                )
        else:
            assert source_gates == []
        assert provider.support_checks == 1
        assert provider.runs == 0
        assert output_owner.state == "empty"
        if source_owner is None:
            assert not destination.exists()
        else:
            assert (
                source_owner.consume(
                    lambda _receipt, reader: reader.read_bytes(
                        "payload.bin",
                        max_bytes=32,
                    )
                )
                == b"same bytes"
            )
    finally:
        output_owner.close()
        if source_owner is not None:
            source_owner.close()


@pytest.mark.parametrize("exact", [False, True], ids=["missing", "exact"])
def test_cleanup_entry_interruption_retries_both_gate_settlements(
    tmp_path: Path,
    exact: bool,
) -> None:
    destination = tmp_path / "published"
    source_owner = _publish_generation(destination) if exact else None
    request = StrictWorkspaceRequest(
        "replace" if exact else "missing",
        destination,
        _plan(),
        destination_binding=(
            source_owner.destination_binding if source_owner is not None else None
        ),
    )
    output_owner = PublishedWorkspaceReceiptOwner()
    returned = object()
    failure = KeyboardInterrupt("cleanup entry interruption")

    class ReturningProvider:
        def __init__(self) -> None:
            self.support_checks = 0
            self.runs = 0
            self.operation_gate = None
            self.source_gate = None

        def require_support(self) -> None:
            self.support_checks += 1

        def run_workspace(
            self,
            _request,
            *,
            receipt_owner,
            operation,
            _replacement_source=None,
        ) -> object:
            del receipt_owner
            self.runs += 1
            self.operation_gate = operation
            self.source_gate = _replacement_source
            return returned

    provider = ReturningProvider()
    injected: list[int] = []
    target_code = workspace_provider._invoke_strict_workspace_provider.__code__
    previous_trace = sys.gettrace()

    def interrupt_first_cleanup_line(frame, event, _argument):
        if (
            not injected
            and event == "line"
            and frame.f_code is target_code
            and frame.f_locals.get("result") is returned
        ):
            operation_lifecycle = frame.f_locals.get("operation_lifecycle")
            replacement_lifecycle = frame.f_locals.get("replacement_lifecycle")
            operation_active = (
                type(operation_lifecycle) is list and operation_lifecycle[0]
            )
            replacement_active = (
                type(replacement_lifecycle) is list
                and replacement_lifecycle[0]
                and replacement_lifecycle[2]
            )
            if operation_active and (not exact or replacement_active):
                injected.append(frame.f_lineno)
                raise failure
        return interrupt_first_cleanup_line

    try:
        sys.settrace(interrupt_first_cleanup_line)
        with pytest.raises(KeyboardInterrupt) as caught:
            run_strict_workspace(
                provider,
                request,
                receipt_owner=output_owner,
                operation=lambda _session: None,
                source_owner=source_owner,
            )
    finally:
        sys.settrace(previous_trace)

    try:
        assert caught.value is failure
        assert len(injected) == 1
        assert provider.support_checks == provider.runs == 1
        operation_gate = provider.operation_gate
        assert type(operation_gate) is workspace_provider._ProviderOperationGate
        assert operation_gate._lifecycle == [False, False, False], injected
        with pytest.raises(RuntimeError, match="no longer active"):
            operation_gate(object())
        if exact:
            source_gate = provider.source_gate
            assert type(source_gate) is workspace_provider._ReplacementSourceGate
            assert source_gate._lifecycle == [False, False, False, False]
            with pytest.raises(RuntimeError, match="no longer active"):
                source_gate.bind(  # type: ignore[arg-type]
                    object(),
                    object(),
                    ".late-cleanup-stage",
                    request.plan,
                )
        else:
            assert provider.source_gate is None
        assert output_owner.state == "empty"
        if source_owner is None:
            assert not destination.exists()
        else:
            assert (
                source_owner.consume(
                    lambda _receipt, reader: reader.read_bytes(
                        "payload.bin",
                        max_bytes=32,
                    )
                )
                == b"same bytes"
            )
    finally:
        output_owner.close()
        if source_owner is not None:
            source_owner.close()


@pytest.mark.parametrize("exact", [False, True], ids=["missing", "exact"])
@pytest.mark.parametrize("seam", ["commit-entry", "cleanup-entry"])
def test_cleanup_entry_interruption_preserves_provider_primary(
    tmp_path: Path,
    exact: bool,
    seam: str,
) -> None:
    destination = tmp_path / "published"
    source_owner = _publish_generation(destination) if exact else None
    request = StrictWorkspaceRequest(
        "replace" if exact else "missing",
        destination,
        _plan(),
        destination_binding=(
            source_owner.destination_binding if source_owner is not None else None
        ),
    )
    output_owner = PublishedWorkspaceReceiptOwner()
    primary = RuntimeError("provider-primary")
    secondary = KeyboardInterrupt("cleanup-entry-secondary")

    class FailingProvider:
        def __init__(self) -> None:
            self.support_checks = 0
            self.runs = 0
            self.operation_gate = None
            self.source_gate = None

        def require_support(self) -> None:
            self.support_checks += 1

        def run_workspace(
            self,
            _request,
            *,
            receipt_owner,
            operation,
            _replacement_source=None,
        ) -> object:
            del receipt_owner
            self.runs += 1
            self.operation_gate = operation
            self.source_gate = _replacement_source
            raise primary

    provider = FailingProvider()
    injected: list[int] = []
    target_code = workspace_provider._invoke_strict_workspace_provider.__code__
    previous_trace = sys.gettrace()

    def interrupt_first_cleanup_line(frame, event, _argument):
        provider_primary = frame.f_locals.get("provider_primary")
        at_commit_entry = (
            seam == "commit-entry"
            and frame.f_locals.get("exc") is primary
            and type(provider_primary) is list
            and provider_primary[0] is None
        )
        at_cleanup_entry = (
            seam == "cleanup-entry" and frame.f_locals.get("primary_error") is primary
        )
        if (
            not injected
            and event == "line"
            and frame.f_code is target_code
            and (at_commit_entry or at_cleanup_entry)
        ):
            operation_lifecycle = frame.f_locals.get("operation_lifecycle")
            replacement_lifecycle = frame.f_locals.get("replacement_lifecycle")
            operation_active = (
                type(operation_lifecycle) is list and operation_lifecycle[0]
            )
            replacement_active = (
                type(replacement_lifecycle) is list
                and replacement_lifecycle[0]
                and replacement_lifecycle[2]
            )
            if operation_active and (not exact or replacement_active):
                injected.append(frame.f_lineno)
                raise secondary
        return interrupt_first_cleanup_line

    try:
        sys.settrace(interrupt_first_cleanup_line)
        with pytest.raises(RuntimeError) as caught:
            run_strict_workspace(
                provider,
                request,
                receipt_owner=output_owner,
                operation=lambda _session: None,
                source_owner=source_owner,
            )
    finally:
        sys.settrace(previous_trace)

    try:
        assert caught.value is primary
        assert len(injected) == 1
        trace_names: list[str] = []
        traceback = caught.value.__traceback__
        while traceback is not None:
            trace_names.append(traceback.tb_frame.f_code.co_name)
            traceback = traceback.tb_next
        assert "run_workspace" in trace_names
        assert any(
            "provider cleanup boundary also failed" in note
            and "cleanup-entry-secondary" in note
            for note in _notes(primary)
        )
        assert provider.support_checks == provider.runs == 1
        operation_gate = provider.operation_gate
        assert type(operation_gate) is workspace_provider._ProviderOperationGate
        assert operation_gate._lifecycle == [False, False, False]
        with pytest.raises(RuntimeError, match="no longer active"):
            operation_gate(object())
        if exact:
            source_gate = provider.source_gate
            assert type(source_gate) is workspace_provider._ReplacementSourceGate
            assert source_gate._lifecycle == [False, False, False, False]
            with pytest.raises(RuntimeError, match="no longer active"):
                source_gate.bind(  # type: ignore[arg-type]
                    object(),
                    object(),
                    ".late-primary-cleanup-stage",
                    request.plan,
                )
        else:
            assert provider.source_gate is None
        assert output_owner.state == "empty"
        if source_owner is None:
            assert not destination.exists()
        else:
            assert (
                source_owner.consume(
                    lambda _receipt, reader: reader.read_bytes(
                        "payload.bin",
                        max_bytes=32,
                    )
                )
                == b"same bytes"
            )
    finally:
        output_owner.close()
        if source_owner is not None:
            source_owner.close()


def test_stashed_replacement_source_gate_is_revoked_without_instance_store(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "published"
    source_owner = _publish_generation(destination)
    request = StrictWorkspaceRequest(
        "replace",
        destination,
        _plan(),
        destination_binding=source_owner.destination_binding,
    )
    output_owner = PublishedWorkspaceReceiptOwner()
    stashed: list[object] = []
    attempted_stores: list[str] = []
    gate_type = workspace_provider._ReplacementSourceGate
    inherited_setattr = gate_type.__setattr__

    def forbid_instance_store(instance, name, value) -> None:
        attempted_stores.append(name)
        raise KeyboardInterrupt("permanent replacement-gate store interruption")

    class StashingProvider:
        def require_support(self) -> None:
            return None

        def run_workspace(
            self,
            _request,
            *,
            receipt_owner,
            operation,
            _replacement_source,
        ) -> object:
            del receipt_owner, operation
            stashed.append(_replacement_source)
            gate_type.__setattr__ = forbid_instance_store
            return object()

    try:
        with pytest.raises(RuntimeError, match="did not invoke its operation"):
            run_strict_workspace(
                StashingProvider(),
                request,
                receipt_owner=output_owner,
                operation=lambda _session: None,
                source_owner=source_owner,
            )
    finally:
        gate_type.__setattr__ = inherited_setattr

    assert attempted_stores == []
    assert len(stashed) == 1
    gate = stashed[0]
    workspace = OwnedWorkspaceAuthority()
    try:
        with pytest.raises(RuntimeError, match="no longer active"):
            gate.bind(  # type: ignore[attr-defined]
                workspace,
                object(),
                ".late-stage",
                request.plan,
            )
    finally:
        workspace.close()
        output_owner.close()
        source_owner.close()


def test_exact_support_baseexception_revokes_gate_retained_by_traceback(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "published"
    source_owner = _publish_generation(destination)
    request = StrictWorkspaceRequest(
        "replace",
        destination,
        _plan(),
        destination_binding=source_owner.destination_binding,
    )
    output_owner = PublishedWorkspaceReceiptOwner()
    failure = KeyboardInterrupt("injected exact support failure")
    support_calls = 0
    run_called = False

    class FailingSupportProvider:
        def require_support(self) -> None:
            nonlocal support_calls
            support_calls += 1
            raise failure

        def run_workspace(self, *_args: object, **_kwargs: object) -> object:
            nonlocal run_called
            run_called = True
            raise AssertionError("exact support failure reached provider execution")

    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            run_strict_workspace(
                FailingSupportProvider(),
                request,
                receipt_owner=output_owner,
                operation=lambda _session: None,
                source_owner=source_owner,
            )

        assert caught.value is failure
        retained_gates: list[object] = []
        traceback = caught.value.__traceback__
        while traceback is not None:
            candidate = traceback.tb_frame.f_locals.get("replacement_source")
            if type(candidate) is workspace_provider._ReplacementSourceGate:
                retained_gates.append(candidate)
            traceback = traceback.tb_next
        assert retained_gates
        gate = retained_gates[0]
        assert all(candidate is gate for candidate in retained_gates)
        assert gate._lifecycle == [  # type: ignore[attr-defined]
            False,
            False,
            False,
            False,
        ]
        workspace = OwnedWorkspaceAuthority()
        try:
            with pytest.raises(RuntimeError, match="no longer active"):
                gate.bind(  # type: ignore[attr-defined]
                    workspace,
                    object(),
                    ".late-support-stage",
                    request.plan,
                )
        finally:
            workspace.close()
        assert support_calls == 1
        assert not run_called
        assert output_owner.state == "empty"
        assert source_owner.active
        assert destination.joinpath("payload.bin").read_bytes() == b"same bytes"
        assert (
            source_owner.consume(
                lambda _receipt, reader: reader.read_bytes(
                    "payload.bin",
                    max_bytes=32,
                )
            )
            == b"same bytes"
        )
    finally:
        output_owner.close()
        source_owner.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_replacement_source_gate_pid_boundary_precedes_argument_inspection(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "published"
    source_owner = _publish_generation(destination)
    request = StrictWorkspaceRequest(
        "replace",
        destination,
        _plan(),
        destination_binding=source_owner.destination_binding,
    )
    gate = _active_replacement_source_gate(request, source_owner)
    try:
        exit_code, payload = _fork_expect_pid_boundary(
            lambda: gate.bind(  # type: ignore[arg-type]
                object(),
                object(),
                ".child-stage",
                request.plan,
            ),
            expected_message=(
                "strict workspace replacement source cannot cross a PID boundary"
            ),
        )
        assert exit_code == 0, payload
        assert payload == "expected PID boundary"
        assert gate._lifecycle == [True, False, True, False]
        assert source_owner.active
    finally:
        gate.revoke()
        source_owner.close()


def test_replacement_source_gate_reentrant_bind_preserves_parent_lifecycle(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "published"
    source_owner = _publish_generation(destination)
    request = StrictWorkspaceRequest(
        "replace",
        destination,
        _plan(),
        destination_binding=source_owner.destination_binding,
    )
    gate = _active_replacement_source_gate(request, source_owner)
    try:
        with pytest.raises(RuntimeError, match="bind is reentrant"):
            gate._lock.run(
                lambda: gate.bind(  # type: ignore[arg-type]
                    object(),
                    object(),
                    ".reentrant-stage",
                    request.plan,
                )
            )
        assert gate._lifecycle == [True, False, True, False]
        assert source_owner.active
    finally:
        gate.revoke()
        source_owner.close()


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


def test_missing_destination_request_never_selects_the_replacement_seam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = StrictWorkspaceRequest("missing", tmp_path / "published", _plan())
    provider = _TestProvider()
    owner = PublishedWorkspaceReceiptOwner()
    replacement_calls: list[str] = []

    def forbidden_replacement(
        _workspace: OwnedWorkspaceAuthority,
        *_args: object,
        **_kwargs: object,
    ) -> None:
        replacement_calls.append("replacement")
        raise AssertionError("missing destination selected replacement authority")

    for method in (
        "bind_replacement_source",
        "provision_bound_replacement",
        "publish_replacement_into",
    ):
        monkeypatch.setattr(
            OwnedWorkspaceAuthority,
            method,
            forbidden_replacement,
        )

    def publish(session: StrictWorkspaceSession) -> bytes:
        session.write_file("payload.bin", (b"missing",))
        return session.publish_validated(
            lambda reader: reader.read_bytes("payload.bin", max_bytes=32)
        )

    try:
        assert (
            run_strict_workspace(
                provider,
                request,
                receipt_owner=owner,
                operation=publish,
            )
            == b"missing"
        )
        assert replacement_calls == []
        assert owner.active
        assert request.destination.joinpath("payload.bin").read_bytes() == b"missing"
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
    source_owner = _publish_generation(destination)
    request = StrictWorkspaceRequest("test", destination, _plan())
    provider = _TestProvider(
        destination_binding=source_owner.destination_binding,
    )
    owner = PublishedWorkspaceReceiptOwner()
    called = False

    def operation(_session: StrictWorkspaceSession) -> None:
        nonlocal called
        called = True

    try:
        with pytest.raises(ValueError, match="binding differs"):
            run_strict_workspace(
                provider,
                request,
                receipt_owner=owner,
                operation=operation,
            )

        assert called is False
        assert owner.state == "empty"
        assert destination.joinpath("payload.bin").read_bytes() == b"same bytes"
    finally:
        owner.close()
        source_owner.close()


def test_adopted_workspace_rejects_missing_binding_for_exact_request(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "published"
    source_owner = _publish_generation(destination)
    binding = source_owner.destination_binding
    destination.rename(tmp_path / "parked-generation")
    request = StrictWorkspaceRequest(
        "test",
        destination,
        _plan(),
        destination_binding=binding,
    )
    provider = _TestProvider(destination_binding=None)
    owner = PublishedWorkspaceReceiptOwner()
    called = False

    def operation(_session: StrictWorkspaceSession) -> None:
        nonlocal called
        called = True

    try:
        with pytest.raises(ValueError, match="binding differs"):
            run_strict_workspace(
                provider,
                request,
                receipt_owner=owner,
                operation=operation,
                source_owner=source_owner,
            )

        assert called is False
        assert owner.state == "empty"
        assert not destination.exists()
    finally:
        owner.close()
        source_owner.close()


def test_provider_bound_exact_request_replaces_only_captured_destination(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "published"
    source_owner = _publish_generation(destination, payload=b"old")
    binding = source_owner.destination_binding
    request = StrictWorkspaceRequest(
        "test",
        destination,
        _plan(),
        destination_binding=binding,
    )
    provider = _TestProvider()
    owner = PublishedWorkspaceReceiptOwner()

    def operation(session: StrictWorkspaceSession) -> bytes:
        session.write_file("payload.bin", (b"owned",))
        return session.publish_validated(
            lambda reader: reader.read_bytes("payload.bin", max_bytes=32)
        )

    try:
        with pytest.raises(RuntimeError, match="was not bound to this authority"):
            run_strict_workspace(
                provider,
                request,
                receipt_owner=owner,
                operation=operation,
                source_owner=source_owner,
            )
        assert owner.state == "empty"
        assert source_owner.active
        assert destination.joinpath("payload.bin").read_bytes() == b"old"
    finally:
        owner.close()
        source_owner.close()


def test_session_compares_entire_genuine_destination_binding(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "published"
    first_owner = _publish_generation(destination)
    first_binding = first_owner.destination_binding
    destination.rename(tmp_path / "first-generation")
    second_owner = _publish_generation(destination)
    second_binding = second_owner.destination_binding
    assert first_binding.destination == second_binding.destination
    assert first_binding.parent_identity == second_binding.parent_identity
    assert first_binding.ownership != second_binding.ownership

    request = StrictWorkspaceRequest(
        "replace",
        destination,
        _plan(),
        destination_binding=first_binding,
    )
    provider = _TestProvider(destination_binding=second_binding)
    output_owner = PublishedWorkspaceReceiptOwner()
    called = False

    def operation(_session: StrictWorkspaceSession) -> None:
        nonlocal called
        called = True

    try:
        with pytest.raises(ValueError, match="binding differs"):
            run_strict_workspace(
                provider,
                request,
                receipt_owner=output_owner,
                operation=operation,
                source_owner=first_owner,
            )
        assert called is False
        assert output_owner.state == "empty"
        assert destination.joinpath("payload.bin").read_bytes() == b"same bytes"
    finally:
        output_owner.close()
        second_owner.close()
        first_owner.close()


def test_provider_cannot_launder_independently_captured_tree_token(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "published"
    source_owner = _publish_generation(destination)
    request = StrictWorkspaceRequest(
        "replace",
        destination,
        _plan(),
        destination_binding=source_owner.destination_binding,
    )
    provider = _TestProvider(
        destination_binding=capture_directory_ownership(destination),
    )
    output_owner = PublishedWorkspaceReceiptOwner()
    called = False

    def operation(_session: StrictWorkspaceSession) -> None:
        nonlocal called
        called = True

    try:
        with pytest.raises(TypeError, match="destination binding is invalid"):
            run_strict_workspace(
                provider,
                request,
                receipt_owner=output_owner,
                operation=operation,
                source_owner=source_owner,
            )
        assert called is False
        assert output_owner.state == "empty"
        assert source_owner.active
    finally:
        output_owner.close()
        source_owner.close()


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


def test_adopted_workspace_rechecks_binding_after_staged_validator(
    tmp_path: Path,
) -> None:
    request = StrictWorkspaceRequest("test", tmp_path / "published", _plan())
    provider = _TestProvider()
    owner = PublishedWorkspaceReceiptOwner()

    def operation(session: StrictWorkspaceSession) -> None:
        session.write_file("payload.bin", (b"owned",))

        def validate(_reader) -> None:
            assert provider.last_workspace is not None
            provider.last_workspace._destination_binding = object()  # type: ignore[attr-defined]  # noqa: SLF001

        session.publish_validated(validate)

    try:
        with pytest.raises(ValueError, match="binding differs"):
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
    monkeypatch: pytest.MonkeyPatch,
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
    deactivate = workspace_provider._ProviderOperationGate._deactivate

    def interrupt_deactivate(self) -> None:
        if not injected:
            injected.append(True)
            raise RuntimeError("provider-revocation-secondary")
        deactivate(self)

    monkeypatch.setattr(
        workspace_provider._ProviderOperationGate,
        "_deactivate",
        interrupt_deactivate,
    )
    with pytest.raises(primary_type, match="provider-primary") as caught:
        run()

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

        operation_gate = _active_provider_operation_gate(
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
