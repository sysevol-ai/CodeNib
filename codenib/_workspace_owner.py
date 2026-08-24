# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Fail-closed facade for the optional strict-workspace native owner.

The owner ABI is deliberately independent from every optional cleanup or
retrieval extension.  Source installs may omit the extension, but a production
local workspace provider must call :func:`require_support` before its first
filesystem mutation and must never select a path-based fallback.
"""

from __future__ import annotations

from typing import Any, Callable

_EXPECTED_WORKSPACE_OWNER_PROTOCOL_VERSION = 3
_IMPORT_ERROR: BaseException | None = None

try:
    from . import _workspace_owner_impl as _implementation
except (ImportError, OSError) as error:
    _implementation = None
    _IMPORT_ERROR = error


def _implementation_attribute(name: str) -> Any:
    if _implementation is None:
        return None
    return getattr(_implementation, name, None)


_protocol_version = _implementation_attribute("workspace_owner_protocol_version")
_require_support_exact = _implementation_attribute("require_support")
_create_owner_exact = _implementation_attribute("create_owner_exact")
_claim_owner_publish_permit_exact = _implementation_attribute(
    "claim_owner_publish_permit_exact"
)
_require_owner_exact = _implementation_attribute("require_owner_exact")
_close_owner_exact = _implementation_attribute("close_owner_exact")
_provision_owner_exact = _implementation_attribute("provision_owner_exact")
_capture_owner_destination_exact = _implementation_attribute(
    "capture_owner_destination_exact"
)
_verify_owner_authority_exact = _implementation_attribute(
    "verify_owner_authority_exact"
)
_verify_owner_adoption_binding_exact = _implementation_attribute(
    "verify_owner_adoption_binding_exact"
)
_verify_owner_destination_binding_exact = _implementation_attribute(
    "verify_owner_destination_binding_exact"
)
_borrow_owner_parent_descriptor_exact = _implementation_attribute(
    "borrow_owner_parent_descriptor_exact"
)
_borrow_owner_root_descriptor_exact = _implementation_attribute(
    "borrow_owner_root_descriptor_exact"
)
_borrow_owner_destination_descriptor_exact = _implementation_attribute(
    "borrow_owner_destination_descriptor_exact"
)
_borrow_owner_directory_descriptor_exact = _implementation_attribute(
    "borrow_owner_directory_descriptor_exact"
)
_begin_owner_file_exact = _implementation_attribute("begin_owner_file_exact")
_write_owner_file_exact = _implementation_attribute("write_owner_file_exact")
_finish_owner_file_exact = _implementation_attribute("finish_owner_file_exact")
_abort_owner_file_exact = _implementation_attribute("abort_owner_file_exact")
_seal_owner_directories_exact = _implementation_attribute(
    "seal_owner_directories_exact"
)
_sync_owner_parent_exact = _implementation_attribute("sync_owner_parent_exact")
_mark_owner_adopted_exact = _implementation_attribute("mark_owner_adopted_exact")
_rename_owner_child_noreplace_exact = _implementation_attribute(
    "rename_owner_child_noreplace_exact"
)
_commit_owner_receipt_exact = _implementation_attribute("commit_owner_receipt_exact")
_abort_owner_exact = _implementation_attribute("abort_owner_exact")
_quarantine_owner_exact = _implementation_attribute("quarantine_owner_exact")
_owner_state_exact = _implementation_attribute("owner_state_exact")
_owner_closed_exact = _implementation_attribute("owner_closed_exact")

_workspace_owner_protocol_available = (
    type(_protocol_version) is int
    and _protocol_version == _EXPECTED_WORKSPACE_OWNER_PROTOCOL_VERSION
    and all(
        callable(symbol)
        for symbol in (
            _require_support_exact,
            _create_owner_exact,
            _claim_owner_publish_permit_exact,
            _require_owner_exact,
            _close_owner_exact,
            _provision_owner_exact,
            _capture_owner_destination_exact,
            _verify_owner_authority_exact,
            _verify_owner_adoption_binding_exact,
            _verify_owner_destination_binding_exact,
            _borrow_owner_parent_descriptor_exact,
            _borrow_owner_root_descriptor_exact,
            _borrow_owner_destination_descriptor_exact,
            _borrow_owner_directory_descriptor_exact,
            _begin_owner_file_exact,
            _write_owner_file_exact,
            _finish_owner_file_exact,
            _abort_owner_file_exact,
            _seal_owner_directories_exact,
            _sync_owner_parent_exact,
            _mark_owner_adopted_exact,
            _rename_owner_child_noreplace_exact,
            _commit_owner_receipt_exact,
            _abort_owner_exact,
            _quarantine_owner_exact,
            _owner_state_exact,
            _owner_closed_exact,
        )
    )
)

if not _workspace_owner_protocol_available:
    _require_support_exact = None
    _create_owner_exact = None
    _claim_owner_publish_permit_exact = None
    _require_owner_exact = None
    _close_owner_exact = None
    _provision_owner_exact = None
    _capture_owner_destination_exact = None
    _verify_owner_authority_exact = None
    _verify_owner_adoption_binding_exact = None
    _verify_owner_destination_binding_exact = None
    _borrow_owner_parent_descriptor_exact = None
    _borrow_owner_root_descriptor_exact = None
    _borrow_owner_destination_descriptor_exact = None
    _borrow_owner_directory_descriptor_exact = None
    _begin_owner_file_exact = None
    _write_owner_file_exact = None
    _finish_owner_file_exact = None
    _abort_owner_file_exact = None
    _seal_owner_directories_exact = None
    _sync_owner_parent_exact = None
    _mark_owner_adopted_exact = None
    _rename_owner_child_noreplace_exact = None
    _commit_owner_receipt_exact = None
    _abort_owner_exact = None
    _quarantine_owner_exact = None
    _owner_state_exact = None
    _owner_closed_exact = None


def require_support() -> None:
    """Require the complete owner ABI without choosing a fallback."""

    _require_protocol()
    assert _require_support_exact is not None
    result = _require_support_exact()
    if result is not None:
        raise RuntimeError("native workspace support result changed")


def _require_protocol() -> None:
    """Require only the immutable ABI, without a fallible support probe."""

    if not _workspace_owner_protocol_available:
        raise RuntimeError(
            "strict local workspaces require the CodeNib workspace-owner " "extension"
        ) from _IMPORT_ERROR


def create_owner() -> Any:
    """Create one exact empty native aggregate owner."""

    require_support()
    assert _create_owner_exact is not None
    return _create_owner_exact()


def claim_owner_publish_permit(owner: object) -> object:
    """Claim the owner's one private, one-shot publication capability."""

    _require_protocol()
    assert _claim_owner_publish_permit_exact is not None
    return _claim_owner_publish_permit_exact(owner)


def _bind_owner_publish_permit(
    publication_permit: object,
) -> Callable[[bytes, bytes], object | None]:
    """Bind one permit to the exact ABI callable before user callbacks run."""

    _require_protocol()
    exact_rename = _rename_owner_child_noreplace_exact
    assert exact_rename is not None

    def rename(source: bytes, destination: bytes) -> object | None:
        return exact_rename(publication_permit, source, destination)

    return rename


def require_exact_owner(candidate: object) -> Any:
    """Authenticate and return one exact native owner."""

    _require_protocol()
    assert _require_owner_exact is not None
    owner = _require_owner_exact(candidate)
    if owner is not candidate:
        raise RuntimeError("native workspace owner identity changed")
    return owner


def close_owner_exact(candidate: object) -> None:
    """Close an exact owner without consulting mutable instance dispatch."""

    _require_protocol()
    assert _close_owner_exact is not None
    result = _close_owner_exact(candidate)
    if result is not None:
        raise RuntimeError("native workspace close result changed")


def _require_none_result(result: object, label: str) -> None:
    if result is not None:
        raise RuntimeError(f"native workspace {label} result changed")


def provision_owner(
    owner: object,
    allowed_root: bytes,
    destination_name: bytes,
    stage_name: bytes,
    plan_digest: bytes,
    root_mode: int,
    directories: tuple[tuple[bytes, int], ...],
    deadline_ns: int,
) -> None:
    """Provision one exact missing-destination skeleton."""

    _require_protocol()
    assert _provision_owner_exact is not None
    _require_none_result(
        _provision_owner_exact(
            owner,
            allowed_root,
            destination_name,
            stage_name,
            plan_digest,
            root_mode,
            directories,
            deadline_ns,
        ),
        "provision",
    )


def capture_owner_destination(
    owner: object,
    allowed_root: bytes,
    destination: bytes,
    deadline_ns: int,
) -> None:
    """Capture one exact existing destination without mutating its namespace."""

    _require_protocol()
    assert _capture_owner_destination_exact is not None
    _require_none_result(
        _capture_owner_destination_exact(
            owner,
            allowed_root,
            destination,
            deadline_ns,
        ),
        "destination-capture",
    )


def verify_owner_authority(owner: object) -> None:
    _require_protocol()
    assert _verify_owner_authority_exact is not None
    _require_none_result(_verify_owner_authority_exact(owner), "verify")


def verify_owner_destination_binding(owner: object) -> None:
    _require_protocol()
    assert _verify_owner_destination_binding_exact is not None
    _require_none_result(
        _verify_owner_destination_binding_exact(owner),
        "destination-binding verification",
    )


def verify_owner_adoption_binding(
    owner: object,
    destination: bytes,
    stage_name: bytes,
    plan_digest: bytes,
) -> None:
    _require_protocol()
    assert _verify_owner_adoption_binding_exact is not None
    _require_none_result(
        _verify_owner_adoption_binding_exact(
            owner,
            destination,
            stage_name,
            plan_digest,
        ),
        "adoption-binding verification",
    )


def _require_descriptor(result: object, label: str) -> int:
    if type(result) is not int or result < 0:
        raise RuntimeError(f"native workspace {label} result changed")
    return result


def borrow_owner_parent_descriptor(owner: object) -> int:
    _require_protocol()
    assert _borrow_owner_parent_descriptor_exact is not None
    return _require_descriptor(
        _borrow_owner_parent_descriptor_exact(owner),
        "parent-descriptor",
    )


def borrow_owner_root_descriptor(owner: object) -> int:
    _require_protocol()
    assert _borrow_owner_root_descriptor_exact is not None
    return _require_descriptor(
        _borrow_owner_root_descriptor_exact(owner),
        "root-descriptor",
    )


def borrow_owner_destination_descriptor(owner: object) -> int:
    """Borrow the owner-held destination FD; callers must not close it."""

    _require_protocol()
    assert _borrow_owner_destination_descriptor_exact is not None
    return _require_descriptor(
        _borrow_owner_destination_descriptor_exact(owner),
        "destination-descriptor",
    )


def borrow_owner_directory_descriptor(owner: object, path: bytes) -> int:
    _require_protocol()
    assert _borrow_owner_directory_descriptor_exact is not None
    return _require_descriptor(
        _borrow_owner_directory_descriptor_exact(owner, path),
        "directory-descriptor",
    )


def begin_owner_file(
    owner: object,
    parent: bytes,
    name: bytes,
    create_mode: int,
) -> None:
    _require_protocol()
    assert _begin_owner_file_exact is not None
    _require_none_result(
        _begin_owner_file_exact(owner, parent, name, create_mode),
        "file-begin",
    )


def write_owner_file(owner: object, data: bytes) -> None:
    _require_protocol()
    assert _write_owner_file_exact is not None
    _require_none_result(_write_owner_file_exact(owner, data), "file-write")


def finish_owner_file(owner: object, final_mode: int) -> tuple[int, ...]:
    _require_protocol()
    assert _finish_owner_file_exact is not None
    result = _finish_owner_file_exact(owner, final_mode)
    if (
        type(result) is not tuple
        or len(result) != 8
        or any(type(value) is not int for value in result)
    ):
        raise RuntimeError("native workspace file-finish result changed")
    return result


def abort_owner_file(owner: object) -> None:
    _require_protocol()
    assert _abort_owner_file_exact is not None
    _require_none_result(_abort_owner_file_exact(owner), "file-abort")


def seal_owner_directories(owner: object) -> None:
    _require_protocol()
    assert _seal_owner_directories_exact is not None
    _require_none_result(_seal_owner_directories_exact(owner), "directory-seal")


def sync_owner_parent(owner: object) -> None:
    _require_protocol()
    assert _sync_owner_parent_exact is not None
    _require_none_result(_sync_owner_parent_exact(owner), "parent-sync")


def mark_owner_adopted(owner: object) -> None:
    _require_protocol()
    assert _mark_owner_adopted_exact is not None
    _require_none_result(_mark_owner_adopted_exact(owner), "adoption")


def rename_owner_child_noreplace(
    owner: object,
    source: bytes,
    destination: bytes,
) -> object | None:
    _require_protocol()
    assert _rename_owner_child_noreplace_exact is not None
    return _rename_owner_child_noreplace_exact(owner, source, destination)


def commit_owner_receipt(receipt_token: object) -> None:
    _require_protocol()
    assert _commit_owner_receipt_exact is not None
    _require_none_result(
        _commit_owner_receipt_exact(receipt_token),
        "receipt-commit",
    )


def abort_owner(owner: object) -> None:
    _require_protocol()
    assert _abort_owner_exact is not None
    _require_none_result(_abort_owner_exact(owner), "abort")


def quarantine_owner(owner: object) -> str | None:
    _require_protocol()
    assert _quarantine_owner_exact is not None
    result = _quarantine_owner_exact(owner)
    if result is not None and type(result) is not str:
        raise RuntimeError("native workspace quarantine result changed")
    return result


def owner_state(owner: object) -> str:
    _require_protocol()
    assert _owner_state_exact is not None
    result = _owner_state_exact(owner)
    if type(result) is not str:
        raise RuntimeError("native workspace state result changed")
    return result


def owner_closed(owner: object) -> bool:
    _require_protocol()
    assert _owner_closed_exact is not None
    result = _owner_closed_exact(owner)
    if type(result) is not bool:
        raise RuntimeError("native workspace closed result changed")
    return result


__all__ = [
    "abort_owner",
    "abort_owner_file",
    "begin_owner_file",
    "borrow_owner_destination_descriptor",
    "borrow_owner_directory_descriptor",
    "borrow_owner_parent_descriptor",
    "borrow_owner_root_descriptor",
    "claim_owner_publish_permit",
    "capture_owner_destination",
    "close_owner_exact",
    "commit_owner_receipt",
    "create_owner",
    "finish_owner_file",
    "mark_owner_adopted",
    "owner_closed",
    "owner_state",
    "provision_owner",
    "quarantine_owner",
    "rename_owner_child_noreplace",
    "require_exact_owner",
    "require_support",
    "seal_owner_directories",
    "sync_owner_parent",
    "verify_owner_adoption_binding",
    "verify_owner_authority",
    "verify_owner_destination_binding",
    "write_owner_file",
]
