# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Linux provider for missing and receipt-bound exact local workspaces.

The configured root is an authority boundary, not a sandbox.  It must be a
private, quiescent directory controlled by the current process owner.  The
native aggregate pins every namespace component and owns all mutating file
descriptors; Python only borrows those handles through the existing strict
workspace lifecycle.  This is not an in-process Python sandbox: callbacks share
the interpreter and must not reflectively mutate CodeNib's private internals.
If a process later blocks both native OFD-comparison mechanisms, cleanup fails
closed and the raised exception retains the explicit cleanup owner for retry.
"""

from __future__ import annotations

import os
import secrets
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TypeVar

from . import _workspace_owner
from ._atomic_directory import (
    _OrderedAction,
    _run_context_with_cleanup_actions,
    publication_parent_identity,
)
from ._captured_directory import (
    OwnedWorkspaceAuthority,
    PublishedWorkspaceReceiptOwner,
    UnsupportedWorkspaceCreation,
    WorkspacePlan,
    _snapshot_workspace_plan,
    require_owned_workspace_publication_support,
)
from ._workspace_provider import (
    StrictWorkspaceRequest,
    StrictWorkspaceSession,
    _ReplacementSourceGate,
    run_adopted_workspace_operation,
)

_OperationResult = TypeVar("_OperationResult")
_DEFAULT_PROVISION_TIMEOUT_NS = 30_000_000_000
_MAX_PROVISION_TIMEOUT_NS = 300_000_000_000
_STAGE_PREFIX = ".codenib-workspace-stage-"
_PROVISION_BOUND_REPLACEMENT_EXACT = OwnedWorkspaceAuthority.provision_bound_replacement


def _provisioning_directories(
    plan: WorkspacePlan,
    check_cancelled: Callable[[], None] | None,
) -> tuple[tuple[bytes, int], ...]:
    records: list[tuple[bytes, int]] = []
    for index, item in enumerate(plan.directories):
        records.append((os.fsencode(item.path.as_posix()), item.mode))
        if check_cancelled is not None and index + 1 < len(plan.directories):
            check_cancelled()
    return tuple(records)


@dataclass(slots=True)
class _ProviderWorkspaceCleanupOwner:
    workspace: OwnedWorkspaceAuthority
    native_owner: object

    @property
    def closed(self) -> bool:
        return self.workspace._provider_owner_settled(self.native_owner)

    def close(self) -> None:
        self.workspace._settle_provider_owner(self.native_owner)


@dataclass(frozen=True, slots=True)
class LocalWorkspaceProvider:
    """Provision strict workspaces below one private Linux authority root."""

    allowed_root: Path
    provision_timeout_ns: int = _DEFAULT_PROVISION_TIMEOUT_NS
    _owner_pid: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self) is not LocalWorkspaceProvider:
            raise TypeError("LocalWorkspaceProvider cannot be subclassed")
        if not isinstance(self.allowed_root, Path):
            raise TypeError("local workspace allowed_root must be a Path")
        root = Path(os.path.abspath(os.fspath(self.allowed_root.expanduser())))
        if root == root.parent:
            raise ValueError("local workspace allowed_root cannot be a filesystem root")
        timeout = self.provision_timeout_ns
        if type(timeout) is not int:
            raise TypeError("local workspace provision timeout must be an integer")
        if timeout <= 0 or timeout > _MAX_PROVISION_TIMEOUT_NS:
            raise ValueError("local workspace provision timeout is out of bounds")
        object.__setattr__(self, "allowed_root", root)
        object.__setattr__(self, "_owner_pid", os.getpid())

    def _require_owner_pid(self) -> None:
        if os.getpid() != self._owner_pid:
            raise RuntimeError("local workspace provider cannot cross a PID boundary")

    def _require_private_root(self) -> None:
        try:
            metadata = self.allowed_root.lstat()
        except OSError as error:
            raise UnsupportedWorkspaceCreation(
                "local workspace allowed_root is unavailable"
            ) from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or stat.S_IMODE(metadata.st_mode) & 0o700 != 0o700
        ):
            raise UnsupportedWorkspaceCreation(
                "local workspace allowed_root must be a private owner-only directory"
            )

    def require_support(self) -> None:
        self._require_owner_pid()
        require_owned_workspace_publication_support()
        try:
            _workspace_owner.require_support()
        except (OSError, RuntimeError) as error:
            raise UnsupportedWorkspaceCreation(
                "native local workspace ownership is unavailable"
            ) from error
        self._require_private_root()

    def _relative_destination(self, destination: Path) -> bytes:
        try:
            relative = destination.relative_to(self.allowed_root)
        except ValueError as error:
            raise ValueError(
                "strict workspace destination is outside the provider root"
            ) from error
        if not relative.parts:
            raise ValueError("strict workspace destination must be below provider root")
        return os.fsencode(relative.as_posix())

    def run_workspace(
        self,
        request: StrictWorkspaceRequest,
        *,
        receipt_owner: PublishedWorkspaceReceiptOwner,
        operation: Callable[[StrictWorkspaceSession], _OperationResult],
        check_cancelled: Callable[[], None] | None = None,
        _expected_parent_identity: tuple[int, ...] | None = None,
        _replacement_source: _ReplacementSourceGate | None = None,
    ) -> _OperationResult:
        self._require_owner_pid()
        if type(request) is not StrictWorkspaceRequest:
            raise TypeError("strict workspace request has an invalid type")
        if type(receipt_owner) is not PublishedWorkspaceReceiptOwner:
            raise TypeError("strict workspace receipt owner has an invalid type")
        if check_cancelled is not None and not callable(check_cancelled):
            raise TypeError("local workspace cancellation check must be callable")
        replacement = request.destination_binding is not None
        if replacement:
            if type(_replacement_source) is not _ReplacementSourceGate:
                raise TypeError(
                    "exact local workspace requires replacement source provenance"
                )
        elif _replacement_source is not None:
            raise ValueError(
                "missing local workspace cannot receive replacement source provenance"
            )
        if _expected_parent_identity is not None and (
            type(_expected_parent_identity) is not tuple
            or len(_expected_parent_identity) < 2
            or any(type(value) is not int for value in _expected_parent_identity)
        ):
            raise TypeError("expected workspace parent identity is invalid")
        if check_cancelled is None:
            detached_plan = _snapshot_workspace_plan(request.plan)
        else:
            detached_plan = _snapshot_workspace_plan(
                request.plan,
                check_cancelled=check_cancelled,
            )
        relative_destination = self._relative_destination(request.destination)
        self.require_support()
        if check_cancelled is not None:
            check_cancelled()
        stage_name = _STAGE_PREFIX + secrets.token_hex(16)
        deadline_ns = time.monotonic_ns() + self.provision_timeout_ns
        directories = _provisioning_directories(detached_plan, check_cancelled)
        if replacement:
            assert _replacement_source is not None
            return self._run_replacement_workspace(
                request,
                receipt_owner=receipt_owner,
                operation=operation,
                replacement_source=_replacement_source,
                detached_plan=detached_plan,
                relative_destination=relative_destination,
                stage_name=stage_name,
                deadline_ns=deadline_ns,
                check_cancelled=check_cancelled,
                expected_parent_identity=_expected_parent_identity,
            )

        workspace = OwnedWorkspaceAuthority()
        native_owner = _workspace_owner.create_owner()
        publication_permit = _workspace_owner.claim_owner_publish_permit(native_owner)
        cleanup_owner = _ProviderWorkspaceCleanupOwner(workspace, native_owner)
        cleanup_actions = (
            _OrderedAction(
                label="local workspace provider cleanup also failed",
                action=cleanup_owner.close,
                complete=lambda: cleanup_owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=cleanup_owner,
            ),
        )
        with _run_context_with_cleanup_actions(cleanup_actions):
            # Recheck mutable external policy immediately before the native
            # call that can perform the first namespace mutation.
            self._require_private_root()
            provision_arguments = (
                native_owner,
                os.fsencode(self.allowed_root),
                relative_destination,
                os.fsencode(stage_name),
                detached_plan.digest.encode("ascii"),
                detached_plan.root_mode,
                directories,
                deadline_ns,
            )
            if check_cancelled is None:
                _workspace_owner.provision_owner(*provision_arguments)
            else:
                _workspace_owner.provision_owner(
                    *provision_arguments,
                    check_cancelled=check_cancelled,
                )
            if _expected_parent_identity is not None:
                parent_descriptor = _workspace_owner.borrow_owner_parent_descriptor(
                    native_owner
                )
                if (
                    publication_parent_identity(parent_descriptor)
                    != _expected_parent_identity
                ):
                    raise RuntimeError(
                        "native workspace parent differs from retained authority"
                    )
            adoption_arguments = {
                "destination": request.destination,
                "stage_name": stage_name,
                "provisioned_owner": native_owner,
                "publication_permit": publication_permit,
                "plan": detached_plan,
                "destination_binding": request.destination_binding,
            }
            if check_cancelled is None:
                workspace.adopt_provisioned(**adoption_arguments)
            else:
                workspace.adopt_provisioned(
                    **adoption_arguments,
                    check_cancelled=check_cancelled,
                )
            return run_adopted_workspace_operation(
                request,
                workspace=workspace,
                receipt_owner=receipt_owner,
                operation=operation,
            )

    def _run_replacement_workspace(
        self,
        request: StrictWorkspaceRequest,
        *,
        receipt_owner: PublishedWorkspaceReceiptOwner,
        operation: Callable[[StrictWorkspaceSession], _OperationResult],
        replacement_source: _ReplacementSourceGate,
        detached_plan: WorkspacePlan,
        relative_destination: bytes,
        stage_name: str,
        deadline_ns: int,
        check_cancelled: Callable[[], None] | None,
        expected_parent_identity: tuple[int, ...] | None,
    ) -> _OperationResult:
        """Bind before candidate mutation and delegate the handed-off owner."""

        workspace = OwnedWorkspaceAuthority()
        native_owner = _workspace_owner.create_owner()
        cleanup_owner = _ProviderWorkspaceCleanupOwner(workspace, native_owner)
        cleanup_actions = (
            _OrderedAction(
                label="local workspace replacement cleanup also failed",
                action=cleanup_owner.close,
                complete=lambda: cleanup_owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=cleanup_owner,
            ),
        )
        with _run_context_with_cleanup_actions(cleanup_actions):
            # Capture and lease are pre-handoff native reads/coordination. The
            # cleanup owner is already reachable if either transition becomes
            # uncertain or retains the cooperative lease for retry.
            self._require_private_root()
            _workspace_owner.capture_owner_destination(
                native_owner,
                os.fsencode(self.allowed_root),
                relative_destination,
                deadline_ns,
            )
            _workspace_owner.acquire_owner_replacement_lease(
                native_owner,
                deadline_ns,
            )
            if expected_parent_identity is not None:
                parent_descriptor = _workspace_owner.borrow_owner_parent_descriptor(
                    native_owner
                )
                if (
                    publication_parent_identity(parent_descriptor)
                    != expected_parent_identity
                ):
                    raise RuntimeError(
                        "native workspace parent differs from retained authority"
                    )

            # This call synchronously consumes the active source owner and is
            # the sole native-owner handoff. Candidate mutation is impossible
            # before it returns successfully.
            replacement_source.bind(
                workspace,
                native_owner,
                stage_name,
                detached_plan,
            )
            self._require_private_root()
            provision_deadline_ns = time.monotonic_ns() + self.provision_timeout_ns
            if check_cancelled is None:
                _PROVISION_BOUND_REPLACEMENT_EXACT(
                    workspace,
                    deadline_ns=provision_deadline_ns,
                )
            else:
                _PROVISION_BOUND_REPLACEMENT_EXACT(
                    workspace,
                    deadline_ns=provision_deadline_ns,
                    check_cancelled=check_cancelled,
                )
            return run_adopted_workspace_operation(
                request,
                workspace=workspace,
                receipt_owner=receipt_owner,
                operation=operation,
                _replacement_timeout_ns=self.provision_timeout_ns,
            )


__all__ = ["LocalWorkspaceProvider"]
