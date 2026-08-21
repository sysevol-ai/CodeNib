# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Linux missing-destination provider for strict local workspaces.

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
    _snapshot_workspace_plan,
    require_owned_workspace_publication_support,
)
from ._workspace_provider import (
    StrictWorkspaceRequest,
    StrictWorkspaceSession,
    run_adopted_workspace_operation,
)

_OperationResult = TypeVar("_OperationResult")
_DEFAULT_PROVISION_TIMEOUT_NS = 30_000_000_000
_MAX_PROVISION_TIMEOUT_NS = 300_000_000_000
_STAGE_PREFIX = ".codenib-workspace-stage-"


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
        _expected_parent_identity: tuple[int, ...] | None = None,
    ) -> _OperationResult:
        self._require_owner_pid()
        if type(request) is not StrictWorkspaceRequest:
            raise TypeError("strict workspace request has an invalid type")
        if type(receipt_owner) is not PublishedWorkspaceReceiptOwner:
            raise TypeError("strict workspace receipt owner has an invalid type")
        if request.destination_expectation != "missing":
            raise UnsupportedWorkspaceCreation(
                "local workspace provider currently requires a missing destination"
            )
        if _expected_parent_identity is not None and (
            type(_expected_parent_identity) is not tuple
            or len(_expected_parent_identity) < 2
            or any(type(value) is not int for value in _expected_parent_identity)
        ):
            raise TypeError("expected workspace parent identity is invalid")
        detached_plan = _snapshot_workspace_plan(request.plan)
        relative_destination = self._relative_destination(request.destination)
        self.require_support()
        stage_name = _STAGE_PREFIX + secrets.token_hex(16)
        deadline_ns = time.monotonic_ns() + self.provision_timeout_ns
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
            _workspace_owner.provision_owner(
                native_owner,
                os.fsencode(self.allowed_root),
                relative_destination,
                os.fsencode(stage_name),
                detached_plan.digest.encode("ascii"),
                detached_plan.root_mode,
                tuple(
                    (os.fsencode(item.path.as_posix()), item.mode)
                    for item in detached_plan.directories
                ),
                deadline_ns,
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
            workspace.adopt_provisioned(
                destination=request.destination,
                stage_name=stage_name,
                provisioned_owner=native_owner,
                publication_permit=publication_permit,
                plan=detached_plan,
                expected_destination=None,
            )
            return run_adopted_workspace_operation(
                request,
                workspace=workspace,
                receipt_owner=receipt_owner,
                operation=operation,
            )


__all__ = ["LocalWorkspaceProvider"]
