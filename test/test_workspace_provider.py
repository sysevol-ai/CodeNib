# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import dis
import hashlib
import os
import sys
from pathlib import Path
from typing import Callable

import pytest

import codenib._workspace_provider as workspace_provider
from codenib._captured_directory import (
    OwnedWorkspaceAuthority,
    PublishedWorkspaceReceiptOwner,
    UnsupportedWorkspaceCreation,
    WorkspaceFile,
    WorkspacePlan,
)
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
    target_code: object,
    attribute: str,
    error: BaseException,
) -> None:
    instructions = {
        instruction.offset: instruction
        for instruction in dis.get_instructions(target_code)
    }
    previous_trace = sys.gettrace()

    def trace(frame, event, _arg):
        if event == "call" and frame.f_code is target_code:
            frame.f_trace_opcodes = True
            return trace
        if event == "opcode" and frame.f_code is target_code:
            instruction = instructions.get(frame.f_lasti)
            if (
                instruction is not None
                and instruction.opname == "STORE_ATTR"
                and instruction.argval == attribute
            ):
                sys.settrace(None)
                raise error
        return trace

    sys.settrace(trace)
    try:
        callback()
    finally:
        sys.settrace(previous_trace)


class _TestProvider:
    def __init__(self) -> None:
        self.support_checks = 0
        self.runs = 0
        self.last_workspace: OwnedWorkspaceAuthority | None = None

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
        parent = request.destination.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        stage = parent / ".provider-stage"
        stage.mkdir(mode=request.plan.root_mode)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        parent_descriptor = os.open(parent, flags)
        root_descriptor = os.open(stage, flags)
        workspace = OwnedWorkspaceAuthority()
        self.last_workspace = workspace
        try:
            workspace.adopt(
                destination=request.destination,
                stage_name=stage.name,
                parent_descriptor=parent_descriptor,
                root_descriptor=root_descriptor,
                directory_descriptors={},
                plan=request.plan,
                expected_destination=None,
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

    with pytest.raises(KeyboardInterrupt, match="session revoke"):
        _interrupt_before_store_attr(
            run,
            target_code=workspace_provider._AdoptedWorkspaceSession._invalidate.__code__,
            attribute="_active",
            error=KeyboardInterrupt("injected session revoke"),
        )

    assert owner.state == "empty"
    assert provider.last_workspace is not None
    assert provider.last_workspace.state == "closed"
    with pytest.raises(RuntimeError, match="no longer active"):
        _ = escaped[0].request


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


def test_wrong_active_receipt_is_rejected_without_losing_owner(
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

    def operation(session: StrictWorkspaceSession) -> None:
        session.write_file("payload.bin", (b"owned",))
        session.publish_validated(lambda _reader: None)

    try:
        with pytest.raises(RuntimeError, match="wrong request"):
            run_strict_workspace(
                WrongProvider(),
                request,
                receipt_owner=owner,
                operation=operation,
            )
        assert owner.active
        assert owner.receipt.path == wrong_request.destination
    finally:
        owner.close()


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
