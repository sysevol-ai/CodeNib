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
import time
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
    PublishedWorkspaceDestinationBinding,
    PublishedWorkspaceReceiptOwner,
    WorkspacePlan,
    _snapshot_workspace_plan,
    require_owned_workspace_publication_support,
)
from ._owned_file_publication import _CancellationSafeRLock

_OperationResult = TypeVar("_OperationResult")
_ValidationResult = TypeVar("_ValidationResult")
DestinationExpectation = Literal["missing", "provider-bound-exact"]
_PUBLISHED_WORKSPACE_DESTINATION_BINDING_TYPE = PublishedWorkspaceDestinationBinding
_PUBLISHED_WORKSPACE_RECEIPT_OWNER_TYPE = PublishedWorkspaceReceiptOwner
_OWNED_WORKSPACE_AUTHORITY_TYPE = OwnedWorkspaceAuthority
_BIND_REPLACEMENT_SOURCE_EXACT = OwnedWorkspaceAuthority.bind_replacement_source
_PUBLISH_REPLACEMENT_EXACT = OwnedWorkspaceAuthority.publish_replacement_into
_SOURCE_DESTINATION_BINDING_EXACT = (
    PublishedWorkspaceReceiptOwner.destination_binding.fget
)
_MONOTONIC_NS_EXACT = time.monotonic_ns
_MAX_REPLACEMENT_TIMEOUT_NS = 300_000_000_000
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
        _replacement_timeout_ns: int | None = None,
    ) -> _OperationResult:
        """Run one bound operation against an already-adopted authority."""

        return _run_adopted_workspace_operation(
            bind,
            request,
            workspace=workspace,
            receipt_owner=receipt_owner,
            operation=operation,
            replacement_timeout_ns=_replacement_timeout_ns,
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
    destination_binding: PublishedWorkspaceDestinationBinding | None = None

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
        plan = _snapshot_workspace_plan(self.plan)
        binding = self.destination_binding
        if (
            binding is not None
            and type(binding) is not _PUBLISHED_WORKSPACE_DESTINATION_BINDING_TYPE
        ):
            raise TypeError("strict workspace destination binding is invalid")
        if binding is not None and binding.destination != destination:
            raise ValueError(
                "strict workspace destination binding differs from its destination"
            )
        object.__setattr__(self, "destination", destination)
        object.__setattr__(self, "plan", plan)

    @property
    def destination_expectation(self) -> DestinationExpectation:
        """Describe the destination mode derived from its exact binding."""

        return "missing" if self.destination_binding is None else "provider-bound-exact"


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
        *,
        validate_published_destination: (
            Callable[[PublicationDirectoryReader], _ValidationResult] | None
        ) = None,
    ) -> _ValidationResult: ...


class StrictWorkspaceProvider(Protocol):
    """Trusted callback-scoped provider of pre-opened workspace authority.

    Implementations provision and adopt the authority, then enter ``operation``
    only through :func:`run_adopted_workspace_operation`.  Hand-built session
    facades are rejected because they cannot prove the requested destination
    expectation or share the revocation gate.
    """

    def require_support(self) -> None:
        """Repeatably probe support without provisioning or consuming authority."""
        ...

    def run_workspace(
        self,
        request: StrictWorkspaceRequest,
        *,
        receipt_owner: PublishedWorkspaceReceiptOwner,
        operation: Callable[[StrictWorkspaceSession], _OperationResult],
        check_cancelled: Callable[[], None] | None = None,
        _replacement_source: _ReplacementSourceGate | None = None,
    ) -> _OperationResult: ...


class _ReplacementSourceGate:
    """One-shot active-source handoff for one exact replacement operation."""

    __slots__ = (
        "_bind_replacement_source",
        "_binding",
        "_bound_native_owner",
        "_bound_workspace",
        "_lifecycle",
        "_lock",
        "_owner_pid",
        "_request",
        "_snapshot_plan",
        "_source_destination_binding",
        "_source_owner",
    )

    def __init__(
        self,
        request: StrictWorkspaceRequest,
        source_owner: PublishedWorkspaceReceiptOwner,
        *,
        lifecycle: list[bool],
    ) -> None:
        if type(lifecycle) is not list or lifecycle != [False, False, False, True]:
            raise TypeError("strict workspace replacement lifecycle is invalid")
        # The enclosing strict call owns this mutable lifecycle before invoking
        # the constructor.  Keep it inactive here: even an interruption at the
        # constructor return boundary can then retain no usable bind authority.
        self._lifecycle = lifecycle
        self._owner_pid = os.getpid()
        self._lock = _CancellationSafeRLock()
        self._bound_workspace: OwnedWorkspaceAuthority | None = None
        self._bound_native_owner: object | None = None
        try:
            if type(request) is not StrictWorkspaceRequest:
                raise TypeError("strict workspace request has an invalid type")
            if type(source_owner) is not _PUBLISHED_WORKSPACE_RECEIPT_OWNER_TYPE:
                raise TypeError(
                    "strict workspace replacement source must be an exact "
                    "receipt owner"
                )
            binding = request.destination_binding
            if binding is None:
                raise ValueError(
                    "strict workspace replacement source requires an exact binding"
                )
            source_destination_binding = _SOURCE_DESTINATION_BINDING_EXACT
            assert source_destination_binding is not None
            if source_destination_binding(source_owner) is not binding:
                raise ValueError(
                    "strict workspace replacement source differs from its request"
                )
            self._request = request
            self._source_owner = source_owner
            self._binding = binding
            # Freeze exact binding callbacks before provider code can run.
            self._bind_replacement_source = _BIND_REPLACEMENT_SOURCE_EXACT
            self._snapshot_plan = _snapshot_workspace_plan
            self._source_destination_binding = source_destination_binding
        except BaseException:  # noqa: B036 - partial gates stay fail-closed
            self._lifecycle[0] = False
            self._lifecycle[2] = False
            self._lifecycle[3] = False
            raise

    def _require_owner_pid(self) -> None:
        if os.getpid() != self._owner_pid:
            raise RuntimeError(
                "strict workspace replacement source cannot cross a PID boundary"
            )

    def bind(
        self,
        workspace: OwnedWorkspaceAuthority,
        native_owner: object,
        stage_name: str,
        plan: WorkspacePlan,
        *,
        check_cancelled: Callable[[], None] | None = None,
    ) -> None:
        """Consume and bind the active source without returning its authority."""

        self._require_owner_pid()
        if check_cancelled is not None and not callable(check_cancelled):
            raise TypeError(
                "strict workspace replacement cancellation check must be callable"
            )
        if self._lock.held_by_current_thread():
            raise RuntimeError("strict workspace replacement source bind is reentrant")

        def bind_once() -> None:
            if not self._lifecycle[0] or not self._lifecycle[2]:
                raise RuntimeError(
                    "strict workspace replacement source is no longer active"
                )
            if self._lifecycle[1]:
                raise RuntimeError("strict workspace replacement source is one-shot")
            self._lifecycle[1] = True
            if type(workspace) is not _OWNED_WORKSPACE_AUTHORITY_TYPE:
                raise TypeError("strict workspace replacement authority is invalid")
            if self._request.destination_binding is not self._binding:
                raise RuntimeError(
                    "strict workspace replacement request binding changed"
                )
            if self._request.plan != self._snapshot_plan(plan):
                raise ValueError(
                    "strict workspace replacement plan differs from request"
                )
            if (
                self._source_destination_binding(self._source_owner)
                is not self._binding
            ):
                raise RuntimeError(
                    "strict workspace replacement source binding changed"
                )
            try:
                if check_cancelled is None:
                    result = self._bind_replacement_source(
                        workspace,
                        self._source_owner,
                        destination_binding=self._binding,
                        native_owner=native_owner,
                        stage_name=stage_name,
                        plan=plan,
                    )
                else:
                    result = self._bind_replacement_source(
                        workspace,
                        self._source_owner,
                        destination_binding=self._binding,
                        native_owner=native_owner,
                        stage_name=stage_name,
                        plan=plan,
                        check_cancelled=check_cancelled,
                    )
                if result is not None:
                    raise RuntimeError(
                        "strict workspace replacement source bind result changed"
                    )
                self._bound_workspace = workspace
                self._bound_native_owner = native_owner
            finally:
                # An attempted handoff is never replayable, including when the
                # native/workspace settlement path raises or is interrupted.
                self._lifecycle[0] = False

        self._lock.run(bind_once)

    def _require_bound_workspace(
        self,
        request: StrictWorkspaceRequest,
        workspace: OwnedWorkspaceAuthority,
    ) -> None:
        self._require_owner_pid()

        def require() -> None:
            if not self._lifecycle[2]:
                raise RuntimeError(
                    "strict workspace replacement source is no longer active"
                )
            if request is not self._request:
                raise ValueError(
                    "strict workspace replacement source belongs to another request"
                )
            if request.destination_binding is not self._binding:
                raise RuntimeError(
                    "strict workspace replacement request binding changed"
                )
            if not self._lifecycle[1] or self._bound_workspace is not workspace:
                raise RuntimeError(
                    "strict workspace replacement source was not bound to "
                    "this authority"
                )
            if (
                self._bound_native_owner is None
                or workspace._native_owner is not self._bound_native_owner
            ):
                raise RuntimeError("strict workspace replacement native owner changed")

        self._lock.run(require)

    def revoke(self) -> None:
        self._require_owner_pid()
        deferred: BaseException | None = None

        def deactivate() -> None:
            self._lifecycle[0] = False
            self._lifecycle[2] = False
            self._lifecycle[3] = False

        for _attempt in range(_SESSION_RECOVERY_LIMIT):
            try:
                self._lock.run(deactivate)
            except BaseException as interruption:  # noqa: B036
                if deferred is None:
                    deferred = interruption
                else:
                    _annotate_secondary_error(
                        deferred,
                        "strict workspace replacement source revocation "
                        "was interrupted again",
                        interruption,
                    )
                continue
            try:
                inactive = self._lock.run(
                    lambda: (
                        not self._lifecycle[0]
                        and not self._lifecycle[2]
                        and not self._lifecycle[3]
                    )
                )
            except BaseException as interruption:  # noqa: B036
                if deferred is None:
                    deferred = interruption
                else:
                    _annotate_secondary_error(
                        deferred,
                        "strict workspace replacement source revocation "
                        "check also failed",
                        interruption,
                    )
                continue
            if inactive:
                if deferred is not None:
                    raise deferred.with_traceback(deferred.__traceback__)
                return
        recovery_error = RuntimeError(
            "strict workspace replacement source revocation did not converge"
        )
        if deferred is not None:
            raise recovery_error from deferred
        raise recovery_error


class _AdoptedWorkspaceSession:
    __slots__ = (
        "_active",
        "_check_cancelled",
        "_gate",
        "_monotonic_ns",
        "_operation_provenance",
        "_owner_pid",
        "_published",
        "_publish_replacement",
        "_receipt_owner",
        "_replacement_timeout_ns",
        "_request",
        "_workspace",
    )

    def __init__(
        self,
        request: StrictWorkspaceRequest,
        workspace: OwnedWorkspaceAuthority,
        receipt_owner: PublishedWorkspaceReceiptOwner,
        *,
        check_cancelled: Callable[[], None] | None = None,
        operation_provenance: object,
        monotonic_ns: Callable[[], int] = _MONOTONIC_NS_EXACT,
        publish_replacement: Callable[..., None] = _PUBLISH_REPLACEMENT_EXACT,
        replacement_timeout_ns: int | None = None,
    ) -> None:
        self._request = request
        self._workspace = workspace
        self._receipt_owner = receipt_owner
        self._active = True
        self._check_cancelled = check_cancelled
        self._monotonic_ns = monotonic_ns
        self._operation_provenance = operation_provenance
        self._replacement_timeout_ns = replacement_timeout_ns
        self._owner_pid = os.getpid()
        self._gate = _CancellationSafeRLock()
        self._published = False
        self._publish_replacement = publish_replacement

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
        if (
            self._workspace.expected_destination_binding
            != self._request.destination_binding
        ):
            raise ValueError(
                "adopted workspace destination binding differs from its request"
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
            if self._check_cancelled is None:
                return self._workspace.write_file(relative, chunks)
            return self._workspace.write_file(
                relative,
                chunks,
                check_cancelled=self._check_cancelled,
            )

        return self._gate.run(write)

    def publish_validated(
        self,
        validator: Callable[
            [PublicationDirectoryReader],
            _ValidationResult,
        ],
        *,
        validate_published_destination: (
            Callable[[PublicationDirectoryReader], _ValidationResult] | None
        ) = None,
    ) -> _ValidationResult:
        self._require_owner_pid()

        def publish() -> _ValidationResult:
            self._require_active()
            if not callable(validator):
                raise TypeError("strict workspace validator must be callable")
            if validate_published_destination is not None and not callable(
                validate_published_destination
            ):
                raise TypeError("strict workspace published validator must be callable")
            if self._published:
                raise RuntimeError("strict workspace session was already published")
            self._require_request_binding()
            if self._check_cancelled is None:
                self._workspace.seal()
            else:
                self._workspace.seal(check_cancelled=self._check_cancelled)
            published: list[_ValidationResult] = []
            published_validator = (
                validator
                if validate_published_destination is None
                else validate_published_destination
            )

            def validate_staged(reader: PublicationDirectoryReader) -> None:
                validator(reader)
                # This callback runs after the staged tree is validated but
                # before the atomic rename.  Recheck the provider's adopted
                # destination contract inside that exact publication window.
                self._require_request_binding()

            def validate_published(reader: PublicationDirectoryReader) -> None:
                result = published_validator(reader)
                self._require_request_binding()
                published.append(result)

            # Sealing is a separate authority transition. Recheck again
            # immediately before entering the authority's rename operation.
            self._require_request_binding()
            if self._request.destination_binding is None:
                if self._replacement_timeout_ns is not None:
                    raise RuntimeError(
                        "missing workspace received a replacement timeout"
                    )
                publish_kwargs = {
                    "validate_staged_directory": validate_staged,
                    "validate_published_destination": validate_published,
                }
                if self._check_cancelled is None:
                    self._workspace.publish_into(
                        self._receipt_owner,
                        **publish_kwargs,
                    )
                else:
                    self._workspace.publish_into(
                        self._receipt_owner,
                        check_cancelled=self._check_cancelled,
                        **publish_kwargs,
                    )
            else:
                timeout_ns = self._replacement_timeout_ns
                if type(timeout_ns) is not int or not (
                    0 < timeout_ns <= _MAX_REPLACEMENT_TIMEOUT_NS
                ):
                    raise RuntimeError("exact workspace replacement timeout is missing")
                deadline_ns = self._monotonic_ns() + timeout_ns
                replacement_kwargs = {
                    "deadline_ns": deadline_ns,
                    "validate_staged_directory": validate_staged,
                    "validate_published_destination": validate_published,
                }
                if self._check_cancelled is None:
                    self._publish_replacement(
                        self._workspace,
                        self._receipt_owner,
                        **replacement_kwargs,
                    )
                else:
                    self._publish_replacement(
                        self._workspace,
                        self._receipt_owner,
                        check_cancelled=self._check_cancelled,
                        **replacement_kwargs,
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
        "_check_cancelled",
        "_lifecycle",
        "_lock",
        "_monotonic_ns",
        "_operation",
        "_operation_outcome",
        "_owner_pid",
        "_publish_replacement",
        "_replacement_source",
        "_request",
    )

    def __init__(
        self,
        request: StrictWorkspaceRequest,
        operation: Callable[[StrictWorkspaceSession], _OperationResult],
        *,
        check_cancelled: Callable[[], None] | None = None,
        lifecycle: list[bool],
        replacement_source: _ReplacementSourceGate | None = None,
    ) -> None:
        if type(lifecycle) is not list or lifecycle != [False, False, True]:
            raise TypeError("strict workspace operation lifecycle is invalid")
        if check_cancelled is not None and not callable(check_cancelled):
            raise TypeError("strict workspace cancellation check must be callable")
        # As for the source gate, the enclosing strict call owns this inactive
        # cell before construction and is the only code that may activate it.
        self._lifecycle = lifecycle
        self._check_cancelled = check_cancelled
        self._lock = _CancellationSafeRLock()
        # Exact callers construct this gate before provider support; missing
        # callers construct it before provisioning. Freeze the publication
        # callback and clock once for either lifecycle.
        self._monotonic_ns = _MONOTONIC_NS_EXACT
        self._publish_replacement = _PUBLISH_REPLACEMENT_EXACT
        self._operation = operation
        self._operation_outcome: tuple[object, BaseException | None] | object = (
            _UNSET_RESULT
        )
        self._owner_pid = os.getpid()
        self._replacement_source = replacement_source
        self._request = request

    def _require_owner_pid(self) -> None:
        if os.getpid() != self._owner_pid:
            raise RuntimeError(
                "strict workspace provider operation cannot cross a PID boundary"
            )

    def __call__(self, session: StrictWorkspaceSession) -> _OperationResult:
        self._require_owner_pid()

        def invoke() -> _OperationResult:
            if not self._lifecycle[0]:
                raise RuntimeError("strict workspace operation is no longer active")
            if self._lifecycle[1]:
                raise RuntimeError("strict workspace operation is one-shot")
            if type(session) is not _AdoptedWorkspaceSession:
                raise TypeError(
                    "strict workspace provider must supply an adopted session"
                )
            replacement_source = self._replacement_source
            if replacement_source is None:
                if self._request.destination_binding is not None:
                    raise RuntimeError(
                        "exact workspace operation lacks replacement source provenance"
                    )
            else:
                replacement_source._require_bound_workspace(
                    self._request,
                    session._workspace,
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
            self._lifecycle[1] = True
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
            if not self._lifecycle[0]:
                raise RuntimeError("strict workspace operation is no longer active")
            if self._lifecycle[1]:
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
        return self._lock.run(lambda: self._lifecycle[1])

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
        self._lifecycle[0] = False
        self._lifecycle[2] = False
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
                inactive = self._lock.run(
                    lambda: not self._lifecycle[0] and not self._lifecycle[2]
                )
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


def _settle_replacement_source_gate(
    gate: _ReplacementSourceGate | None,
    lifecycle: list[bool] | None,
) -> BaseException | None:
    """Deactivate even when cancellation lands before the first gate call."""

    deferred: BaseException | None = None
    for _attempt in range(_SESSION_RECOVERY_LIMIT):
        try:
            if gate is None:
                if lifecycle is not None:
                    lifecycle[0] = False
                    lifecycle[2] = False
                    lifecycle[3] = False
            else:
                gate.revoke()
        except BaseException as interruption:  # noqa: B036
            if deferred is None:
                deferred = interruption
            else:
                try:
                    _annotate_secondary_error(
                        deferred,
                        "strict workspace replacement source outer settlement "
                        "was interrupted again",
                        interruption,
                    )
                except BaseException:  # noqa: B036 - diagnostic is best-effort
                    pass
            continue
        try:
            if gate is None:
                inactive = lifecycle is None or (
                    not lifecycle[0] and not lifecycle[2] and not lifecycle[3]
                )
            else:
                inactive = gate._lock.run(
                    lambda: (
                        not gate._lifecycle[0]
                        and not gate._lifecycle[2]
                        and not gate._lifecycle[3]
                    )
                )
        except BaseException as interruption:  # noqa: B036
            if deferred is None:
                deferred = interruption
            else:
                try:
                    _annotate_secondary_error(
                        deferred,
                        "strict workspace replacement source outer settlement "
                        "check also failed",
                        interruption,
                    )
                except BaseException:  # noqa: B036 - diagnostic is best-effort
                    pass
            continue
        if inactive:
            return deferred
    recovery_error = RuntimeError(
        "strict workspace replacement source outer settlement did not converge"
    )
    if deferred is not None:
        try:
            _annotate_secondary_error(
                recovery_error,
                "strict workspace replacement source outer settlement failed",
                deferred,
            )
        except BaseException:  # noqa: B036 - diagnostic is best-effort
            pass
    return recovery_error


def _settle_provider_operation_gate(
    gate: _ProviderOperationGate | None,
    lifecycle: list[bool] | None,
) -> BaseException | None:
    """Deactivate a callback cell across constructor and call boundaries."""

    deferred: BaseException | None = None
    for _attempt in range(_SESSION_RECOVERY_LIMIT):
        try:
            if gate is None:
                if lifecycle is not None:
                    lifecycle[0] = False
                    lifecycle[2] = False
            else:
                gate.revoke()
        except BaseException as interruption:  # noqa: B036
            if deferred is None:
                deferred = interruption
            else:
                try:
                    _annotate_secondary_error(
                        deferred,
                        "strict workspace provider callback outer settlement "
                        "was interrupted again",
                        interruption,
                    )
                except BaseException:  # noqa: B036 - diagnostic is best-effort
                    pass
            continue
        try:
            if gate is None:
                inactive = lifecycle is None or (not lifecycle[0] and not lifecycle[2])
            else:
                inactive = gate._lock.run(
                    lambda: not gate._lifecycle[0] and not gate._lifecycle[2]
                )
        except BaseException as interruption:  # noqa: B036
            if deferred is None:
                deferred = interruption
            else:
                try:
                    _annotate_secondary_error(
                        deferred,
                        "strict workspace provider callback outer settlement "
                        "check also failed",
                        interruption,
                    )
                except BaseException:  # noqa: B036 - diagnostic is best-effort
                    pass
            continue
        if inactive:
            return deferred
    recovery_error = RuntimeError(
        "strict workspace provider callback outer settlement did not converge"
    )
    if deferred is not None:
        try:
            _annotate_secondary_error(
                recovery_error,
                "strict workspace provider callback outer settlement failed",
                deferred,
            )
        except BaseException:  # noqa: B036 - diagnostic is best-effort
            pass
    return recovery_error


def _commit_provider_primary(
    primary_cell: list[BaseException | None],
    primary_error: BaseException,
) -> None:
    """Commit the provider primary before child-frame settlement can run."""

    for _attempt in range(_SESSION_RECOVERY_LIMIT):
        try:
            primary_cell[0] = primary_error
            if primary_cell[0] is not primary_error:
                raise RuntimeError("strict workspace provider primary changed")
        except BaseException as transition_error:  # noqa: B036
            if transition_error is not primary_error:
                try:
                    _annotate_secondary_error(
                        primary_error,
                        "strict workspace provider primary commit also failed",
                        transition_error,
                    )
                except BaseException:  # noqa: B036 - diagnostic is best-effort
                    pass
            continue
        return
    recovery_error = RuntimeError(
        "strict workspace provider primary commit did not converge"
    )
    try:
        _annotate_secondary_error(
            primary_error,
            "strict workspace provider primary commit recovery also failed",
            recovery_error,
        )
    except BaseException:  # noqa: B036 - diagnostic is best-effort
        pass
    raise primary_error.with_traceback(primary_error.__traceback__)


def _invoke_strict_workspace_provider(
    run_workspace: Callable[..., object],
    request: StrictWorkspaceRequest,
    receipt_owner: PublishedWorkspaceReceiptOwner,
    operation_gate: _ProviderOperationGate,
    check_cancelled: Callable[[], None] | None,
    replacement_source: _ReplacementSourceGate | None,
    replacement_lifecycle: list[bool] | None,
    operation_lifecycle: list[bool],
    provider_primary: list[BaseException | None],
    provider_started: list[bool],
    provider_returned: list[bool],
    commit_provider_primary: Callable[
        [list[BaseException | None], BaseException],
        None,
    ],
    settle_replacement_source: Callable[
        [_ReplacementSourceGate | None, list[bool] | None],
        BaseException | None,
    ],
    settle_provider_operation: Callable[
        [_ProviderOperationGate | None, list[bool] | None],
        BaseException | None,
    ],
) -> object:
    """Invoke once, with a first settlement pass owned by a child frame."""

    primary_error: BaseException | None = None
    result: object = _UNSET_RESULT
    try:
        try:
            provider_started[0] = True
            if replacement_source is None:
                if check_cancelled is None:
                    result = run_workspace(
                        request,
                        receipt_owner=receipt_owner,
                        operation=operation_gate,
                    )
                else:
                    result = run_workspace(
                        request,
                        receipt_owner=receipt_owner,
                        operation=operation_gate,
                        check_cancelled=check_cancelled,
                    )
            else:
                if check_cancelled is None:
                    result = run_workspace(
                        request,
                        receipt_owner=receipt_owner,
                        operation=operation_gate,
                        _replacement_source=replacement_source,
                    )
                else:
                    result = run_workspace(
                        request,
                        receipt_owner=receipt_owner,
                        operation=operation_gate,
                        check_cancelled=check_cancelled,
                        _replacement_source=replacement_source,
                    )
            provider_returned[0] = True
        except BaseException as exc:  # noqa: B036 - settle before propagation
            commit_provider_primary(provider_primary, exc)
            primary_error = exc
    finally:
        try:
            replacement_cleanup_error = settle_replacement_source(
                replacement_source,
                replacement_lifecycle,
            )
            if replacement_cleanup_error is not None:
                if primary_error is None:
                    primary_error = replacement_cleanup_error
                else:
                    _annotate_secondary_error(
                        primary_error,
                        "strict workspace replacement source revocation also failed",
                        replacement_cleanup_error,
                    )
        finally:
            operation_cleanup_error = settle_provider_operation(
                operation_gate,
                operation_lifecycle,
            )
            if operation_cleanup_error is not None:
                if primary_error is None:
                    primary_error = operation_cleanup_error
                else:
                    _annotate_secondary_error(
                        primary_error,
                        "strict workspace provider callback revocation also failed",
                        operation_cleanup_error,
                    )
    if primary_error is not None:
        raise primary_error.with_traceback(primary_error.__traceback__)
    if result is _UNSET_RESULT:
        raise RuntimeError("strict workspace provider returned no result")
    return result


def _run_adopted_workspace_operation(
    bind_provenance: Callable[[object, object, object, object], None],
    request: StrictWorkspaceRequest,
    *,
    workspace: OwnedWorkspaceAuthority,
    receipt_owner: PublishedWorkspaceReceiptOwner,
    operation: Callable[[StrictWorkspaceSession], _OperationResult],
    replacement_timeout_ns: int | None,
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
    if request.destination_binding is None:
        if replacement_timeout_ns is not None:
            raise ValueError("missing workspace cannot use a replacement timeout")
    elif type(replacement_timeout_ns) is not int or not (
        0 < replacement_timeout_ns <= _MAX_REPLACEMENT_TIMEOUT_NS
    ):
        raise ValueError("exact workspace requires a bounded replacement timeout")

    operation_provenance = object()
    session = _AdoptedWorkspaceSession(
        request,
        workspace,
        receipt_owner,
        check_cancelled=operation._check_cancelled,
        operation_provenance=operation_provenance,
        monotonic_ns=operation._monotonic_ns,
        publish_replacement=operation._publish_replacement,
        replacement_timeout_ns=replacement_timeout_ns,
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
    source_owner: PublishedWorkspaceReceiptOwner | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> _OperationResult:
    """Preflight and invoke one trusted provider without an ambient fallback."""

    if type(request) is not StrictWorkspaceRequest:
        raise TypeError("strict workspace request has an invalid type")
    if type(receipt_owner) is not PublishedWorkspaceReceiptOwner:
        raise TypeError("strict workspace receipt owner has an invalid type")
    if not callable(operation):
        raise TypeError("strict workspace operation must be callable")
    if check_cancelled is not None and not callable(check_cancelled):
        raise TypeError("strict workspace cancellation check must be callable")
    if receipt_owner.state != "empty":
        raise RuntimeError("strict workspace receipt owner must be empty")
    binding = request.destination_binding
    replacement_lifecycle: list[bool] | None = None
    operation_lifecycle: list[bool] | None = None
    replacement_source: _ReplacementSourceGate | None = None
    operation_gate: _ProviderOperationGate | None = None
    run_workspace: Callable[..., object] | None = None
    primary_error: BaseException | None = None
    result: object = _UNSET_RESULT
    provider_primary: list[BaseException | None] = [None]
    provider_started = [False]
    provider_returned = [False]
    invoke_strict_provider = _invoke_strict_workspace_provider
    commit_provider_primary = _commit_provider_primary
    settle_replacement_source = _settle_replacement_source_gate
    settle_provider_operation = _settle_provider_operation_gate
    if binding is None and source_owner is not None:
        raise ValueError("missing strict workspace cannot receive a replacement source")
    if binding is not None and source_owner is receipt_owner:
        raise ValueError(
            "strict workspace source and output receipt owners must differ"
        )

    try:
        try:
            if binding is None:
                # Preserve the missing-destination ordering exactly: shared
                # support and provider support precede callback-gate construction.
                require_owned_workspace_publication_support()
                require_support = getattr(provider, "require_support", None)
                provider_run_workspace = getattr(provider, "run_workspace", None)
                if not callable(require_support) or not callable(
                    provider_run_workspace
                ):
                    raise TypeError("strict workspace provider has an invalid contract")
                require_support()
                operation_lifecycle = [False, False, True]
                operation_gate = _ProviderOperationGate(
                    request,
                    operation,
                    check_cancelled=check_cancelled,
                    lifecycle=operation_lifecycle,
                )
                run_workspace = provider_run_workspace
                # Spend the outer-only activation authority before making the
                # callback live. Both stores and provider entry remain inside
                # the same settlement boundary.
                operation_lifecycle[2] = False
                operation_lifecycle[0] = True
                if check_cancelled is not None:
                    check_cancelled()
                result = invoke_strict_provider(
                    run_workspace,
                    request,
                    receipt_owner,
                    operation_gate,
                    check_cancelled,
                    None,
                    None,
                    operation_lifecycle,
                    provider_primary,
                    provider_started,
                    provider_returned,
                    commit_provider_primary,
                    settle_replacement_source,
                    settle_provider_operation,
                )
            else:
                replacement_lifecycle = [False, False, False, True]
                replacement_source = _ReplacementSourceGate(
                    request,
                    source_owner,  # type: ignore[arg-type]
                    lifecycle=replacement_lifecycle,
                )
                operation_lifecycle = [False, False, True]
                operation_gate = _ProviderOperationGate(
                    request,
                    operation,
                    check_cancelled=check_cancelled,
                    lifecycle=operation_lifecycle,
                    replacement_source=replacement_source,
                )
                # Exact gates must be revoked on every later exit, including
                # shared support, hostile provider descriptors, and provider
                # support. Both gates remain inactive during those callbacks.
                require_owned_workspace_publication_support()
                require_support = getattr(provider, "require_support", None)
                provider_run_workspace = getattr(provider, "run_workspace", None)
                if not callable(require_support) or not callable(
                    provider_run_workspace
                ):
                    raise TypeError("strict workspace provider has an invalid contract")
                require_support()
                run_workspace = provider_run_workspace
                # No activation callable is retained by either gate. Spend both
                # outer-only activation bits before enabling either capability,
                # then enter the provider without leaving this guarded region.
                replacement_lifecycle[3] = False
                operation_lifecycle[2] = False
                replacement_lifecycle[0] = True
                replacement_lifecycle[2] = True
                operation_lifecycle[0] = True
                if check_cancelled is not None:
                    check_cancelled()
                result = invoke_strict_provider(
                    run_workspace,
                    request,
                    receipt_owner,
                    operation_gate,
                    check_cancelled,
                    replacement_source,
                    replacement_lifecycle,
                    operation_lifecycle,
                    provider_primary,
                    provider_started,
                    provider_returned,
                    commit_provider_primary,
                    settle_replacement_source,
                    settle_provider_operation,
                )
        except BaseException as exc:  # noqa: B036 - settle before propagation
            if (
                provider_primary[0] is None
                and provider_started[0]
                and not provider_returned[0]
                and exc.__context__ is not None
            ):
                commit_provider_primary(provider_primary, exc.__context__)
            primary_error = exc
        # Settle once while still inside the protected outer try. If
        # cancellation lands on the first cleanup line, control transfers to
        # the outer handler/finally and the second pass still owns every cell.
        try:
            replacement_cleanup_error = settle_replacement_source(
                replacement_source,
                replacement_lifecycle,
            )
            if replacement_cleanup_error is not None:
                if primary_error is None:
                    primary_error = replacement_cleanup_error
                else:
                    _annotate_secondary_error(
                        primary_error,
                        "strict workspace replacement source revocation also failed",
                        replacement_cleanup_error,
                    )
        finally:
            operation_cleanup_error = settle_provider_operation(
                operation_gate,
                operation_lifecycle,
            )
            if operation_cleanup_error is not None:
                if primary_error is None:
                    primary_error = operation_cleanup_error
                else:
                    _annotate_secondary_error(
                        primary_error,
                        "strict workspace provider callback revocation also failed",
                        operation_cleanup_error,
                    )
    except BaseException as settlement_boundary_error:  # noqa: B036
        if primary_error is None:
            primary_error = settlement_boundary_error
        elif settlement_boundary_error is not primary_error:
            try:
                _annotate_secondary_error(
                    primary_error,
                    "strict workspace gate settlement boundary also failed",
                    settlement_boundary_error,
                )
            except BaseException:  # noqa: B036 - diagnostic is best-effort
                pass
    finally:
        try:
            try:
                replacement_cleanup_error = settle_replacement_source(
                    replacement_source,
                    replacement_lifecycle,
                )
            except BaseException as interruption:  # noqa: B036
                # Cancellation can land on the helper-call boundary before its
                # own retry loop begins. Retain that exact interruption and
                # retry the whole settlement until inactivity is observed.
                replacement_cleanup_error = interruption
                for _attempt in range(_SESSION_RECOVERY_LIMIT):
                    try:
                        retry_error = settle_replacement_source(
                            replacement_source,
                            replacement_lifecycle,
                        )
                    except BaseException as retry_interruption:  # noqa: B036
                        try:
                            _annotate_secondary_error(
                                replacement_cleanup_error,
                                "strict workspace replacement source settlement "
                                "boundary was interrupted again",
                                retry_interruption,
                            )
                        except BaseException:  # noqa: B036 - best-effort note
                            pass
                        continue
                    if retry_error is not None:
                        try:
                            _annotate_secondary_error(
                                replacement_cleanup_error,
                                "strict workspace replacement source settlement "
                                "also failed",
                                retry_error,
                            )
                        except BaseException:  # noqa: B036 - best-effort note
                            pass
                    break
                else:
                    recovery_error = RuntimeError(
                        "strict workspace replacement source settlement boundary "
                        "did not converge"
                    )
                    try:
                        _annotate_secondary_error(
                            replacement_cleanup_error,
                            "strict workspace replacement source settlement "
                            "recovery also failed",
                            recovery_error,
                        )
                    except BaseException:  # noqa: B036 - best-effort note
                        pass
            if replacement_cleanup_error is not None:
                if primary_error is None:
                    primary_error = replacement_cleanup_error
                else:
                    _annotate_secondary_error(
                        primary_error,
                        "strict workspace replacement source revocation also failed",
                        replacement_cleanup_error,
                    )
        finally:
            try:
                operation_cleanup_error = settle_provider_operation(
                    operation_gate,
                    operation_lifecycle,
                )
            except BaseException as interruption:  # noqa: B036
                operation_cleanup_error = interruption
                for _attempt in range(_SESSION_RECOVERY_LIMIT):
                    try:
                        retry_error = settle_provider_operation(
                            operation_gate,
                            operation_lifecycle,
                        )
                    except BaseException as retry_interruption:  # noqa: B036
                        try:
                            _annotate_secondary_error(
                                operation_cleanup_error,
                                "strict workspace provider callback settlement "
                                "boundary was interrupted again",
                                retry_interruption,
                            )
                        except BaseException:  # noqa: B036 - best-effort note
                            pass
                        continue
                    if retry_error is not None:
                        try:
                            _annotate_secondary_error(
                                operation_cleanup_error,
                                "strict workspace provider callback settlement "
                                "also failed",
                                retry_error,
                            )
                        except BaseException:  # noqa: B036 - best-effort note
                            pass
                    break
                else:
                    recovery_error = RuntimeError(
                        "strict workspace provider callback settlement boundary "
                        "did not converge"
                    )
                    try:
                        _annotate_secondary_error(
                            operation_cleanup_error,
                            "strict workspace provider callback settlement "
                            "recovery also failed",
                            recovery_error,
                        )
                    except BaseException:  # noqa: B036 - best-effort note
                        pass
            if operation_cleanup_error is not None:
                if primary_error is None:
                    primary_error = operation_cleanup_error
                else:
                    _annotate_secondary_error(
                        primary_error,
                        "strict workspace provider callback revocation also failed",
                        operation_cleanup_error,
                    )

    committed_provider_primary = provider_primary[0]
    if (
        committed_provider_primary is not None
        and primary_error is not committed_provider_primary
    ):
        if primary_error is not None:
            try:
                _annotate_secondary_error(
                    committed_provider_primary,
                    "strict workspace provider cleanup boundary also failed",
                    primary_error,
                )
            except BaseException:  # noqa: B036 - diagnostic is best-effort
                pass
        primary_error = committed_provider_primary

    if operation_gate is None:
        if primary_error is None:
            raise RuntimeError("strict workspace operation gate was not constructed")
        raise primary_error.with_traceback(primary_error.__traceback__)
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
