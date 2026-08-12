# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Callback-scoped strict workspace provisioning contracts.

This module does not provision directories.  A provider is a trusted authority
boundary which receives an exact immutable plan, supplies an already-adopted
``OwnedWorkspaceAuthority`` to the operation, and keeps every provisioning
resource reachable until the callback finishes.  Path-only ``mkdir`` followed
by ``open`` is not a provider implementation.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, TypeVar

from ._atomic_directory import (
    PublicationDirectoryReader,
    TreeFileRecord,
    _annotate_secondary_error,
)
from ._captured_directory import (
    OwnedWorkspaceAuthority,
    PublishedWorkspaceReceiptOwner,
    WorkspacePlan,
    require_owned_workspace_publication_support,
)
from ._owned_file_publication import _CancellationSafeRLock

_OperationResult = TypeVar("_OperationResult")
_ValidationResult = TypeVar("_ValidationResult")
DestinationExpectation = Literal["missing", "provider-bound-exact"]
_SESSION_RECOVERY_LIMIT = 64
_UNSET_RESULT = object()
_CONSUMED_SESSION_PROVENANCE = object()


def _create_session_provenance_registry() -> tuple[
    Callable[..., object],
    Callable[[object, object, object, object], object],
    Callable[[object], None],
]:
    bindings: dict[int, tuple[object, object, object, object]] = {}

    def bind(
        gate: object,
        request: object,
        session: object,
        provenance: object,
    ) -> None:
        key = id(gate)
        current = bindings.get(key)
        if current is not None and current[0] is gate:
            raise RuntimeError("strict workspace operation is already bound")
        bindings[key] = (gate, request, session, provenance)

    def consume(
        gate: object,
        request: object,
        session: object,
        provenance: object,
    ) -> object:
        key = id(gate)
        current = bindings.get(key)
        if (
            current is None
            or current[0] is not gate
            or current[1] is not request
            or current[2] is not session
            or current[3] is not provenance
        ):
            raise TypeError("strict workspace session provenance is unbound")
        del bindings[key]
        return provenance

    def discard(gate: object) -> None:
        key = id(gate)
        current = bindings.get(key)
        if current is not None and current[0] is gate:
            del bindings[key]

    def run_adopted_workspace_operation(
        request: StrictWorkspaceRequest,
        *,
        workspace: OwnedWorkspaceAuthority,
        receipt_owner: PublishedWorkspaceReceiptOwner,
        operation: Callable[[StrictWorkspaceSession], _OperationResult],
    ) -> _OperationResult:
        """Run one bound operation against an already-adopted authority."""

        return _run_adopted_workspace_operation(
            bind,
            request,
            workspace=workspace,
            receipt_owner=receipt_owner,
            operation=operation,
        )

    return run_adopted_workspace_operation, consume, discard


(
    run_adopted_workspace_operation,
    _CONSUME_SESSION_PROVENANCE,
    _DISCARD_SESSION_PROVENANCE,
) = _create_session_provenance_registry()


@dataclass(frozen=True, slots=True)
class StrictWorkspaceRequest:
    """One exact strict publication requested from an authority provider."""

    purpose: str
    destination: Path
    plan: WorkspacePlan
    destination_expectation: DestinationExpectation = "missing"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.purpose, str)
            or not self.purpose
            or len(self.purpose.encode("utf-8", errors="strict")) > 128
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in self.purpose
            )
        ):
            raise ValueError("strict workspace purpose must be bounded printable text")
        if not isinstance(self.destination, Path):
            raise TypeError("strict workspace destination must be a Path")
        requested_destination = self.destination.expanduser()
        if requested_destination.name in {"", ".", ".."}:
            raise ValueError("strict workspace destination must name one directory")
        destination = Path(os.path.abspath(os.fspath(requested_destination)))
        if not isinstance(self.plan, WorkspacePlan):
            raise TypeError("strict workspace request plan must be WorkspacePlan")
        if self.destination_expectation not in {"missing", "provider-bound-exact"}:
            raise ValueError("strict workspace destination expectation is invalid")
        object.__setattr__(self, "destination", destination)


class StrictWorkspaceSession(Protocol):
    """Borrowed session valid only during a provider operation callback."""

    @property
    def request(self) -> StrictWorkspaceRequest: ...

    def write_file(
        self,
        relative: str | Path | PurePosixPath,
        chunks: Iterable[bytes],
    ) -> TreeFileRecord: ...

    def publish_validated(
        self,
        validator: Callable[
            [PublicationDirectoryReader],
            _ValidationResult,
        ],
    ) -> _ValidationResult: ...


class StrictWorkspaceProvider(Protocol):
    """Trusted callback-scoped provider of pre-opened workspace authority.

    Implementations provision and adopt the authority, then enter ``operation``
    only through :func:`run_adopted_workspace_operation`.  Hand-built session
    facades are rejected because they cannot prove the requested destination
    expectation or share the revocation gate.
    """

    def require_support(self) -> None: ...

    def run_workspace(
        self,
        request: StrictWorkspaceRequest,
        *,
        receipt_owner: PublishedWorkspaceReceiptOwner,
        operation: Callable[[StrictWorkspaceSession], _OperationResult],
    ) -> _OperationResult: ...


class _AdoptedWorkspaceSession:
    __slots__ = (
        "_active",
        "_gate",
        "_operation_provenance",
        "_owner_pid",
        "_published",
        "_receipt_owner",
        "_request",
        "_workspace",
    )

    def __init__(
        self,
        request: StrictWorkspaceRequest,
        workspace: OwnedWorkspaceAuthority,
        receipt_owner: PublishedWorkspaceReceiptOwner,
        *,
        operation_provenance: object,
    ) -> None:
        self._request = request
        self._workspace = workspace
        self._receipt_owner = receipt_owner
        self._active = True
        self._operation_provenance = operation_provenance
        self._owner_pid = os.getpid()
        self._gate = _CancellationSafeRLock()
        self._published = False

    def _require_owner_pid(self) -> None:
        if os.getpid() != self._owner_pid:
            raise RuntimeError("strict workspace session cannot cross a PID boundary")

    def _require_active(self) -> None:
        if not self._active:
            raise RuntimeError("strict workspace session is no longer active")

    @property
    def request(self) -> StrictWorkspaceRequest:
        self._require_owner_pid()

        def read() -> StrictWorkspaceRequest:
            self._require_active()
            return self._request

        return self._gate.run(read)

    def _require_request_binding(self) -> None:
        self._require_owner_pid()
        if self._workspace.plan != self._request.plan:
            raise ValueError("adopted workspace plan differs from its request")
        if self._workspace.destination != self._request.destination:
            raise ValueError("adopted workspace destination differs from its request")
        expected = (
            "missing"
            if self._workspace.expected_destination_ownership is None
            else "provider-bound-exact"
        )
        if expected != self._request.destination_expectation:
            raise ValueError(
                "adopted workspace destination expectation differs from its request"
            )

    def _require_operation_request(self, request: StrictWorkspaceRequest) -> None:
        self._require_owner_pid()

        def require() -> None:
            self._require_active()
            if self._request is not request:
                raise ValueError("strict workspace session belongs to another request")
            self._require_request_binding()

        self._gate.run(require)

    def _require_operation_provenance(
        self,
        request: StrictWorkspaceRequest,
        provenance: object,
        *,
        consume: bool,
    ) -> None:
        self._require_owner_pid()

        def require() -> None:
            self._require_active()
            if self._request is not request:
                raise ValueError("strict workspace session belongs to another request")
            if self._operation_provenance is not provenance:
                raise TypeError("strict workspace session provenance is invalid")
            self._require_request_binding()
            if consume:
                self._operation_provenance = _CONSUMED_SESSION_PROVENANCE

        self._gate.run(require)

    def write_file(
        self,
        relative: str | Path | PurePosixPath,
        chunks: Iterable[bytes],
    ) -> TreeFileRecord:
        self._require_owner_pid()

        def write() -> TreeFileRecord:
            self._require_active()
            if self._published:
                raise RuntimeError("strict workspace session was already published")
            self._require_request_binding()
            return self._workspace.write_file(relative, chunks)

        return self._gate.run(write)

    def publish_validated(
        self,
        validator: Callable[
            [PublicationDirectoryReader],
            _ValidationResult,
        ],
    ) -> _ValidationResult:
        self._require_owner_pid()

        def publish() -> _ValidationResult:
            self._require_active()
            if not callable(validator):
                raise TypeError("strict workspace validator must be callable")
            if self._published:
                raise RuntimeError("strict workspace session was already published")
            self._require_request_binding()
            self._workspace.seal()
            published: list[_ValidationResult] = []

            def validate_staged(reader: PublicationDirectoryReader) -> None:
                validator(reader)
                # This callback runs after the staged tree is validated but
                # before the atomic rename.  Recheck the provider's adopted
                # destination contract inside that exact publication window.
                self._require_request_binding()

            def validate_published(reader: PublicationDirectoryReader) -> None:
                result = validator(reader)
                self._require_request_binding()
                published.append(result)

            # Sealing is a separate authority transition. Recheck again
            # immediately before entering the authority's rename operation.
            self._require_request_binding()
            self._workspace.publish_into(
                self._receipt_owner,
                validate_staged_directory=validate_staged,
                validate_published_destination=validate_published,
            )
            if len(published) != 1 or not self._receipt_owner.active:
                raise RuntimeError(
                    "strict workspace publication did not install one active receipt"
                )
            receipt = self._receipt_owner.receipt
            if (
                receipt.path != self._request.destination
                or receipt.plan != self._request.plan
            ):
                raise RuntimeError("strict workspace receipt differs from its request")
            self._published = True
            return published[0]

        return self._gate.run(publish)

    def _deactivate(self) -> None:
        self._require_owner_pid()
        self._active = False

    def _invalidate(self) -> None:
        self._require_owner_pid()
        deferred: BaseException | None = None
        for _attempt in range(_SESSION_RECOVERY_LIMIT):
            try:
                self._gate.run(self._deactivate)
            except BaseException as interruption:  # noqa: B036 - reconcile state
                if deferred is None:
                    deferred = interruption
                else:
                    _annotate_secondary_error(
                        deferred,
                        "strict workspace session revocation was interrupted again",
                        interruption,
                    )
                continue
            try:
                inactive = self._gate.run(lambda: not self._active)
            except BaseException as interruption:  # noqa: B036
                if deferred is None:
                    deferred = interruption
                else:
                    _annotate_secondary_error(
                        deferred,
                        "strict workspace session revocation check also failed",
                        interruption,
                    )
                continue
            if inactive:
                if deferred is not None:
                    raise deferred.with_traceback(deferred.__traceback__)
                return
        recovery_error = RuntimeError(
            "strict workspace session revocation did not converge"
        )
        if deferred is not None:
            raise recovery_error from deferred
        raise recovery_error


class _ProviderOperationGate:
    """One-shot callback lease revoked before a provider call can escape."""

    __slots__ = (
        "_active",
        "_called",
        "_lock",
        "_operation",
        "_operation_outcome",
        "_owner_pid",
        "_request",
    )

    def __init__(
        self,
        request: StrictWorkspaceRequest,
        operation: Callable[[StrictWorkspaceSession], _OperationResult],
    ) -> None:
        self._active = True
        self._called = False
        self._lock = _CancellationSafeRLock()
        self._operation = operation
        self._operation_outcome: tuple[object, BaseException | None] | object = (
            _UNSET_RESULT
        )
        self._owner_pid = os.getpid()
        self._request = request

    def _require_owner_pid(self) -> None:
        if os.getpid() != self._owner_pid:
            raise RuntimeError(
                "strict workspace provider operation cannot cross a PID boundary"
            )

    def __call__(self, session: StrictWorkspaceSession) -> _OperationResult:
        self._require_owner_pid()

        def invoke() -> _OperationResult:
            if not self._active:
                raise RuntimeError("strict workspace operation is no longer active")
            if self._called:
                raise RuntimeError("strict workspace operation is one-shot")
            if type(session) is not _AdoptedWorkspaceSession:
                raise TypeError(
                    "strict workspace provider must supply an adopted session"
                )
            provenance = _CONSUME_SESSION_PROVENANCE(
                self,
                self._request,
                session,
                session._operation_provenance,
            )
            session._require_operation_provenance(
                self._request,
                provenance,
                consume=True,
            )
            self._called = True
            try:
                result = self._operation(session)
                self._record_operation_outcome(result, None)
                return result
            except BaseException as operation_error:  # noqa: B036
                self._record_operation_outcome(_UNSET_RESULT, operation_error)
                raise

        return self._lock.run(invoke)

    def _bind_adopted_session(
        self,
        bind_provenance: Callable[[object, object, object, object], None],
        request: StrictWorkspaceRequest,
        session: _AdoptedWorkspaceSession,
        provenance: object,
    ) -> None:
        self._require_owner_pid()

        def bind() -> None:
            if not self._active:
                raise RuntimeError("strict workspace operation is no longer active")
            if self._called:
                raise RuntimeError("strict workspace operation is one-shot")
            if request is not self._request:
                raise ValueError("strict workspace session belongs to another request")
            if type(session) is not _AdoptedWorkspaceSession:
                raise TypeError(
                    "strict workspace provider must supply an adopted session"
                )
            session._require_operation_provenance(
                request,
                provenance,
                consume=False,
            )
            bind_provenance(
                self,
                request,
                session,
                provenance,
            )

        self._lock.run(bind)

    @property
    def called(self) -> bool:
        self._require_owner_pid()
        return self._lock.run(lambda: self._called)

    def _record_operation_outcome(
        self,
        result: object,
        operation_error: BaseException | None,
    ) -> None:
        """Commit one callback outcome before allowing it to escape the gate."""

        primary_error = operation_error
        outcome = (result, operation_error)
        for _attempt in range(_SESSION_RECOVERY_LIMIT):
            try:
                self._operation_outcome = outcome
                if self._operation_outcome is not outcome:
                    raise RuntimeError(
                        "strict workspace callback outcome changed during commit"
                    )
            except BaseException as transition_error:  # noqa: B036
                if primary_error is None:
                    primary_error = transition_error
                    outcome = (_UNSET_RESULT, primary_error)
                elif transition_error is not primary_error:
                    try:
                        _annotate_secondary_error(
                            primary_error,
                            "strict workspace callback outcome commit also failed",
                            transition_error,
                        )
                    except BaseException:  # noqa: B036 - diagnostic is best-effort
                        pass
                continue
            if primary_error is not None:
                raise primary_error.with_traceback(primary_error.__traceback__)
            return

        recovery_error = RuntimeError(
            "strict workspace callback outcome commit did not converge"
        )
        if primary_error is not None:
            try:
                _annotate_secondary_error(
                    primary_error,
                    "strict workspace callback outcome recovery also failed",
                    recovery_error,
                )
            except BaseException:  # noqa: B036 - diagnostic is best-effort
                pass
            raise primary_error.with_traceback(primary_error.__traceback__)
        raise recovery_error

    @property
    def operation_outcome(self) -> tuple[bool, object, BaseException | None]:
        """Return the exact user-callback outcome recorded by this gate."""

        self._require_owner_pid()

        def read() -> tuple[bool, object, BaseException | None]:
            outcome = self._operation_outcome
            if outcome is _UNSET_RESULT:
                return False, _UNSET_RESULT, None
            result, operation_error = outcome  # type: ignore[misc]
            return True, result, operation_error

        return self._lock.run(read)

    def _deactivate(self) -> None:
        self._require_owner_pid()
        self._active = False
        _DISCARD_SESSION_PROVENANCE(self)

    def revoke(self) -> None:
        self._require_owner_pid()
        deferred: BaseException | None = None
        for _attempt in range(_SESSION_RECOVERY_LIMIT):
            try:
                self._lock.run(self._deactivate)
            except BaseException as interruption:  # noqa: B036
                if deferred is None:
                    deferred = interruption
                else:
                    _annotate_secondary_error(
                        deferred,
                        "strict workspace operation revocation was interrupted again",
                        interruption,
                    )
                continue
            try:
                inactive = self._lock.run(lambda: not self._active)
            except BaseException as interruption:  # noqa: B036
                if deferred is None:
                    deferred = interruption
                else:
                    _annotate_secondary_error(
                        deferred,
                        "strict workspace operation revocation check also failed",
                        interruption,
                    )
                continue
            if inactive:
                if deferred is not None:
                    raise deferred.with_traceback(deferred.__traceback__)
                return
        recovery_error = RuntimeError(
            "strict workspace operation revocation did not converge"
        )
        if deferred is not None:
            raise recovery_error from deferred
        raise recovery_error


def _run_adopted_workspace_operation(
    bind_provenance: Callable[[object, object, object, object], None],
    request: StrictWorkspaceRequest,
    *,
    workspace: OwnedWorkspaceAuthority,
    receipt_owner: PublishedWorkspaceReceiptOwner,
    operation: Callable[[StrictWorkspaceSession], _OperationResult],
) -> _OperationResult:
    """Run one operation against an already-adopted borrowed authority.

    The provider remains responsible for closing ``workspace`` after a
    pre-publication failure.  Once a receipt is installed, only
    ``receipt_owner`` may close the transferred aggregate authority.
    """

    if type(request) is not StrictWorkspaceRequest:
        raise TypeError("strict workspace request has an invalid type")
    if type(workspace) is not OwnedWorkspaceAuthority:
        raise TypeError("strict workspace authority has an invalid type")
    if type(receipt_owner) is not PublishedWorkspaceReceiptOwner:
        raise TypeError("strict workspace receipt owner has an invalid type")
    if type(operation) is not _ProviderOperationGate:
        raise TypeError("strict workspace operation has invalid provenance")
    if receipt_owner.state != "empty":
        raise RuntimeError("strict workspace receipt owner must be empty")
    if workspace.state != "adopted":
        raise RuntimeError("strict workspace authority must be freshly adopted")

    operation_provenance = object()
    session = _AdoptedWorkspaceSession(
        request,
        workspace,
        receipt_owner,
        operation_provenance=operation_provenance,
    )
    session._require_operation_request(request)
    operation._bind_adopted_session(
        bind_provenance,
        request,
        session,
        operation_provenance,
    )
    primary_error: BaseException | None = None
    result: object = _UNSET_RESULT
    try:
        result = operation(session)
    except BaseException as exc:  # noqa: B036 - revoke before propagation
        primary_error = exc
    try:
        session._invalidate()
    except BaseException as revoke_error:  # noqa: B036 - preserve operation fault
        if primary_error is None:
            primary_error = revoke_error
        else:
            _annotate_secondary_error(
                primary_error,
                "strict workspace session revocation also failed",
                revoke_error,
            )
    if primary_error is not None:
        raise primary_error.with_traceback(primary_error.__traceback__)
    if result is _UNSET_RESULT:
        raise RuntimeError("strict workspace operation returned no result")
    if not session._published or not receipt_owner.active:
        raise RuntimeError("strict workspace operation returned without publishing")
    receipt = receipt_owner.receipt
    if receipt.path != request.destination or receipt.plan != request.plan:
        raise RuntimeError("strict workspace provider published the wrong request")
    return result  # type: ignore[return-value]


def run_strict_workspace(
    provider: StrictWorkspaceProvider,
    request: StrictWorkspaceRequest,
    *,
    receipt_owner: PublishedWorkspaceReceiptOwner,
    operation: Callable[[StrictWorkspaceSession], _OperationResult],
) -> _OperationResult:
    """Preflight and invoke one trusted provider without an ambient fallback."""

    if type(request) is not StrictWorkspaceRequest:
        raise TypeError("strict workspace request has an invalid type")
    if type(receipt_owner) is not PublishedWorkspaceReceiptOwner:
        raise TypeError("strict workspace receipt owner has an invalid type")
    if not callable(operation):
        raise TypeError("strict workspace operation must be callable")
    if receipt_owner.state != "empty":
        raise RuntimeError("strict workspace receipt owner must be empty")

    # The shared platform gate precedes even provider attribute lookup: a
    # descriptor/property proxy is not allowed to run on an unsupported host.
    require_owned_workspace_publication_support()
    require_support = getattr(provider, "require_support", None)
    run_workspace = getattr(provider, "run_workspace", None)
    if not callable(require_support) or not callable(run_workspace):
        raise TypeError("strict workspace provider has an invalid contract")
    # The provider-specific gate also precedes its provisioning entry point.
    require_support()
    operation_gate = _ProviderOperationGate(request, operation)
    primary_error: BaseException | None = None
    result: object = _UNSET_RESULT
    try:
        result = run_workspace(
            request,
            receipt_owner=receipt_owner,
            operation=operation_gate,
        )
    except BaseException as exc:  # noqa: B036 - revoke before propagation
        primary_error = exc
    try:
        operation_gate.revoke()
    except BaseException as revoke_error:  # noqa: B036
        if primary_error is None:
            primary_error = revoke_error
        else:
            _annotate_secondary_error(
                primary_error,
                "strict workspace provider callback revocation also failed",
                revoke_error,
            )
    outcome_read_error: BaseException | None = None
    for _attempt in range(_SESSION_RECOVERY_LIMIT):
        try:
            callback_completed, callback_result, callback_error = (
                operation_gate.operation_outcome
            )
        except BaseException as read_error:  # noqa: B036 - recover exact outcome
            if outcome_read_error is None:
                outcome_read_error = read_error
            elif read_error is not outcome_read_error:
                try:
                    _annotate_secondary_error(
                        outcome_read_error,
                        "strict workspace callback outcome read failed again",
                        read_error,
                    )
                except BaseException:  # noqa: B036 - diagnostic is best-effort
                    pass
            continue
        break
    else:
        recovery_error = RuntimeError(
            "strict workspace callback outcome read did not converge"
        )
        if primary_error is not None:
            try:
                _annotate_secondary_error(
                    primary_error,
                    "strict workspace callback outcome recovery also failed",
                    outcome_read_error or recovery_error,
                )
            except BaseException:  # noqa: B036 - diagnostic is best-effort
                pass
            raise primary_error.with_traceback(primary_error.__traceback__)
        if outcome_read_error is not None:
            try:
                _annotate_secondary_error(
                    outcome_read_error,
                    "strict workspace callback outcome recovery also failed",
                    recovery_error,
                )
            except BaseException:  # noqa: B036 - diagnostic is best-effort
                pass
            raise outcome_read_error.with_traceback(outcome_read_error.__traceback__)
        raise recovery_error

    if callback_error is not None:
        if primary_error is not None and primary_error is not callback_error:
            try:
                _annotate_secondary_error(
                    callback_error,
                    "strict workspace provider also failed after its callback",
                    primary_error,
                )
            except BaseException:  # noqa: B036 - diagnostic is best-effort
                pass
        if outcome_read_error is not None and outcome_read_error is not callback_error:
            try:
                _annotate_secondary_error(
                    callback_error,
                    "strict workspace callback outcome read also failed",
                    outcome_read_error,
                )
            except BaseException:  # noqa: B036 - diagnostic is best-effort
                pass
        raise callback_error.with_traceback(callback_error.__traceback__)
    if primary_error is not None:
        if outcome_read_error is not None and outcome_read_error is not primary_error:
            try:
                _annotate_secondary_error(
                    primary_error,
                    "strict workspace callback outcome read also failed",
                    outcome_read_error,
                )
            except BaseException:  # noqa: B036 - diagnostic is best-effort
                pass
        raise primary_error.with_traceback(primary_error.__traceback__)
    if outcome_read_error is not None:
        raise outcome_read_error.with_traceback(outcome_read_error.__traceback__)
    if not operation_gate.called:
        raise RuntimeError("strict workspace provider did not invoke its operation")
    if not callback_completed:
        raise RuntimeError("strict workspace provider callback returned no outcome")
    if result is _UNSET_RESULT:
        raise RuntimeError("strict workspace provider returned no result")
    if not receipt_owner.active:
        raise RuntimeError("strict workspace provider returned without a receipt")
    receipt = receipt_owner.receipt
    if receipt.path != request.destination or receipt.plan != request.plan:
        raise RuntimeError("strict workspace provider published the wrong request")
    return callback_result  # type: ignore[return-value]


__all__ = [
    "DestinationExpectation",
    "StrictWorkspaceProvider",
    "StrictWorkspaceRequest",
    "StrictWorkspaceSession",
    "run_adopted_workspace_operation",
    "run_strict_workspace",
]
