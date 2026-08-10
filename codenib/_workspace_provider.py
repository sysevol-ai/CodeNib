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
    """Trusted callback-scoped provider of pre-opened workspace authority."""

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
    ) -> None:
        self._request = request
        self._workspace = workspace
        self._receipt_owner = receipt_owner
        self._active = True
        self._published = False

    def _require_active(self) -> None:
        if not self._active:
            raise RuntimeError("strict workspace session is no longer active")

    @property
    def request(self) -> StrictWorkspaceRequest:
        self._require_active()
        return self._request

    def write_file(
        self,
        relative: str | Path | PurePosixPath,
        chunks: Iterable[bytes],
    ) -> TreeFileRecord:
        self._require_active()
        if self._published:
            raise RuntimeError("strict workspace session was already published")
        return self._workspace.write_file(relative, chunks)

    def publish_validated(
        self,
        validator: Callable[
            [PublicationDirectoryReader],
            _ValidationResult,
        ],
    ) -> _ValidationResult:
        self._require_active()
        if not callable(validator):
            raise TypeError("strict workspace validator must be callable")
        if self._published:
            raise RuntimeError("strict workspace session was already published")
        self._workspace.seal()
        published: list[_ValidationResult] = []

        def validate_staged(reader: PublicationDirectoryReader) -> None:
            validator(reader)

        def validate_published(reader: PublicationDirectoryReader) -> None:
            published.append(validator(reader))

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

    def _invalidate(self) -> None:
        deferred: BaseException | None = None
        for _attempt in range(_SESSION_RECOVERY_LIMIT):
            try:
                self._active = False
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
                inactive = not self._active
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

    __slots__ = ("_active", "_called", "_lock", "_operation")

    def __init__(
        self,
        operation: Callable[[StrictWorkspaceSession], _OperationResult],
    ) -> None:
        self._active = True
        self._called = False
        self._lock = _CancellationSafeRLock()
        self._operation = operation

    def __call__(self, session: StrictWorkspaceSession) -> _OperationResult:
        def invoke() -> _OperationResult:
            if not self._active:
                raise RuntimeError("strict workspace operation is no longer active")
            if self._called:
                raise RuntimeError("strict workspace operation is one-shot")
            self._called = True
            return self._operation(session)

        return self._lock.run(invoke)

    @property
    def called(self) -> bool:
        return self._lock.run(lambda: self._called)

    def _deactivate(self) -> None:
        self._active = False

    def revoke(self) -> None:
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


def run_adopted_workspace_operation(
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

    if not isinstance(request, StrictWorkspaceRequest):
        raise TypeError("strict workspace request has an invalid type")
    if not isinstance(workspace, OwnedWorkspaceAuthority):
        raise TypeError("strict workspace authority has an invalid type")
    if not isinstance(receipt_owner, PublishedWorkspaceReceiptOwner):
        raise TypeError("strict workspace receipt owner has an invalid type")
    if not callable(operation):
        raise TypeError("strict workspace operation must be callable")
    if receipt_owner.state != "empty":
        raise RuntimeError("strict workspace receipt owner must be empty")
    if workspace.plan != request.plan:
        raise ValueError("adopted workspace plan differs from its request")
    if workspace.state != "adopted":
        raise RuntimeError("strict workspace authority must be freshly adopted")

    session = _AdoptedWorkspaceSession(request, workspace, receipt_owner)
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

    if not isinstance(request, StrictWorkspaceRequest):
        raise TypeError("strict workspace request has an invalid type")
    if not isinstance(receipt_owner, PublishedWorkspaceReceiptOwner):
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
    operation_gate = _ProviderOperationGate(operation)
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
    if primary_error is not None:
        raise primary_error.with_traceback(primary_error.__traceback__)
    if result is _UNSET_RESULT:
        raise RuntimeError("strict workspace provider returned no result")
    if not operation_gate.called:
        raise RuntimeError("strict workspace provider did not invoke its operation")
    if not receipt_owner.active:
        raise RuntimeError("strict workspace provider returned without a receipt")
    receipt = receipt_owner.receipt
    if receipt.path != request.destination or receipt.plan != request.plan:
        raise RuntimeError("strict workspace provider published the wrong request")
    return result  # type: ignore[return-value]


__all__ = [
    "DestinationExpectation",
    "StrictWorkspaceProvider",
    "StrictWorkspaceRequest",
    "StrictWorkspaceSession",
    "run_adopted_workspace_operation",
    "run_strict_workspace",
]
