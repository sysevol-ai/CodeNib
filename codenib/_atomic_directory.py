# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Rollback-safe publication of fully staged directory trees."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_EXPECTED_DESTINATION_UNSET = object()
_EXPECTED_OWNERSHIP_UNSET = object()
_MAX_SAFE_REMOVAL_DEPTH = 256
_MAX_OWNERSHIP_ENTRIES = 100_000
_MAX_OWNERSHIP_BYTES = 64 << 30
_MAX_OWNERSHIP_METADATA_BYTES = 16 << 20
_MAX_OWNERSHIP_PATH_BYTES = 4_096
_MAX_OWNERSHIP_COMPONENT_BYTES = 255
_OWNERSHIP_COPY_BYTES = 1 << 20
_SAFE_REMOVAL_DIRECTORY_FDS = (
    hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
    and os.scandir in os.supports_fd
    and os.unlink in os.supports_dir_fd
    and os.rmdir in os.supports_dir_fd
)
_SAFE_OWNERSHIP_DIRECTORY_FDS = (
    hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
    and os.scandir in os.supports_fd
)


@dataclass(slots=True)
class _RemovalState:
    destructive_started: bool = False


@dataclass(slots=True)
class _OwnershipBudget:
    entries: int = 0
    byte_count: int = 0
    metadata_bytes: int = 0


@dataclass(frozen=True, slots=True)
class _TreeOwnership:
    root_identity: tuple[int, ...]
    digest: str
    entries: int
    byte_count: int
    metadata_bytes: int
    inventory: tuple[tuple[str, str], ...]


class _PreviousOutputIdentityLost(RuntimeError):
    """The moved previous tree can no longer be trusted for rollback."""


def lexical_directory_path(path: Path) -> Path:
    """Resolve the parent without ever following the final path component."""

    candidate = path.expanduser()
    if candidate.name in {"", ".", ".."}:
        raise ValueError(f"directory path has no safe final component: {path}")
    return candidate.parent.resolve() / candidate.name


def _directory_or_missing(path: Path, *, label: str) -> os.stat_result | None:
    """Return directory metadata, rejecting links and non-directories."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    is_reparse = bool(
        getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )
    if (
        stat.S_ISLNK(metadata.st_mode)
        or is_reparse
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise ValueError(f"{label} is not a directory or is a link: {path}")
    return metadata


def _directory_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        getattr(metadata, "st_file_attributes", 0),
    )


def _directory_inode_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        getattr(metadata, "st_file_attributes", 0),
    )


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _mountinfo_path(value: str) -> str:
    """Decode the octal escapes used for paths in Linux mountinfo."""

    return (
        value.replace(r"\040", " ")
        .replace(r"\011", "\t")
        .replace(r"\012", "\n")
        .replace(r"\134", "\\")
    )


def _linux_mount_points() -> frozenset[str]:
    if os.name != "posix" or not Path("/proc/self/mountinfo").is_file():
        return frozenset()
    points: set[str] = set()
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as mountinfo:
            for line in mountinfo:
                fields = line.split()
                if len(fields) >= 5:
                    points.add(os.path.normpath(_mountinfo_path(fields[4])))
    except OSError:
        # os.path.ismount and device checks remain the portable baseline.
        return frozenset()
    return frozenset(points)


def _path_is_mount_point(
    path: Path,
    *,
    mount_points: frozenset[str] | None = None,
) -> bool:
    lexical = os.path.normpath(os.path.abspath(path))
    observed_mount_points = (
        _linux_mount_points() if mount_points is None else mount_points
    )
    return os.path.ismount(path) or lexical in observed_mount_points


def _ownership_binding_identity(metadata: os.stat_result) -> tuple[int, ...]:
    """Bind path and descriptor observations without cache-sensitive times."""

    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size if stat.S_ISREG(metadata.st_mode) else 0,
        getattr(metadata, "st_file_attributes", 0),
    )


def _ownership_version_identity(metadata: os.stat_result) -> tuple[int, ...]:
    """Detect mutation between observations made through one open descriptor."""

    return (
        *_ownership_binding_identity(metadata),
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
    )


def _ownership_hash_field(hasher, value: bytes) -> None:
    hasher.update(len(value).to_bytes(8, "big"))
    hasher.update(value)


def _ownership_relative_path(parts: tuple[bytes, ...]) -> bytes:
    if len(parts) > _MAX_SAFE_REMOVAL_DEPTH:
        raise RuntimeError("directory ownership path exceeds its depth limit")
    relative = b"/".join(parts)
    if len(relative) > _MAX_OWNERSHIP_PATH_BYTES:
        raise RuntimeError("directory ownership path exceeds its byte limit")
    return relative


def _reserve_ownership_record(
    budget: _OwnershipBudget,
    *,
    relative: bytes,
) -> None:
    budget.entries += 1
    if budget.entries > _MAX_OWNERSHIP_ENTRIES:
        raise RuntimeError("directory ownership scan exceeds its entry limit")
    # Charge the largest record before descending so nested wide directories
    # cannot accumulate more work than the global budget before unwinding.
    budget.metadata_bytes += 8 + len(relative) + 1 + 4 + 8 + 32
    if budget.metadata_bytes > _MAX_OWNERSHIP_METADATA_BYTES:
        raise RuntimeError("directory ownership scan exceeds its metadata limit")


def _hash_owned_regular_file(
    parent_descriptor: int,
    name: str,
    path: Path,
    metadata: os.stat_result,
    *,
    root_device: int,
    budget: _OwnershipBudget,
) -> tuple[int, str]:
    flags = os.O_RDONLY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != root_device
            or _ownership_binding_identity(opened)
            != _ownership_binding_identity(metadata)
        ):
            raise RuntimeError(f"directory ownership file changed: {path}")
        size = opened.st_size
        if size < 0 or budget.byte_count + size > _MAX_OWNERSHIP_BYTES:
            raise RuntimeError("directory ownership scan exceeds its byte limit")
        digest = hashlib.sha256()
        remaining = size
        while remaining:
            chunk = os.read(descriptor, min(remaining, _OWNERSHIP_COPY_BYTES))
            if not chunk:
                raise RuntimeError(f"directory ownership file was truncated: {path}")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise RuntimeError(f"directory ownership file grew while read: {path}")
        after = os.fstat(descriptor)
        if _ownership_version_identity(after) != _ownership_version_identity(opened):
            raise RuntimeError(f"directory ownership file changed: {path}")
        path_after = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if _ownership_binding_identity(path_after) != _ownership_binding_identity(
            opened
        ):
            raise RuntimeError(f"directory ownership file changed: {path}")
    finally:
        os.close(descriptor)
    budget.byte_count += size
    return size, digest.hexdigest()


def _scan_owned_directory(
    descriptor: int,
    path: Path,
    parts: tuple[bytes, ...],
    *,
    root_device: int,
    mount_points: frozenset[str],
    budget: _OwnershipBudget,
    inventory: list[tuple[str, str]],
    depth: int,
    required_root_file: bytes | None = None,
    allow_empty_root: bool = False,
) -> bytes:
    if depth > _MAX_SAFE_REMOVAL_DEPTH:
        raise RuntimeError("directory ownership scan exceeds its depth limit")
    before = os.fstat(descriptor)
    if not stat.S_ISDIR(before.st_mode) or before.st_dev != root_device:
        raise RuntimeError(f"directory ownership root changed: {path}")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    scan_descriptor = os.open(".", flags, dir_fd=descriptor)
    names: list[tuple[bytes, str]] = []
    try:
        with os.scandir(scan_descriptor) as entries:
            for entry in entries:
                raw_name = os.fsencode(entry.name)
                if not raw_name or len(raw_name) > _MAX_OWNERSHIP_COMPONENT_BYTES:
                    raise RuntimeError(
                        "directory ownership component exceeds its byte limit"
                    )
                relative = _ownership_relative_path(parts + (raw_name,))
                _reserve_ownership_record(budget, relative=relative)
                names.append((raw_name, entry.name))
    finally:
        os.close(scan_descriptor)
    names.sort(key=lambda item: item[0])
    if (
        required_root_file is not None
        and not (allow_empty_root and not names)
        and not any(raw_name == required_root_file for raw_name, _name in names)
    ):
        raise RuntimeError("directory ownership root is missing its required marker")

    hasher = hashlib.sha256()
    hasher.update(b"codenib.atomic-directory.v1\x00")
    hasher.update(stat.S_IMODE(before.st_mode).to_bytes(4, "big"))
    for raw_name, name in names:
        child_parts = parts + (raw_name,)
        relative = _ownership_relative_path(child_parts)
        child_path = path / name
        metadata = os.stat(
            name,
            dir_fd=descriptor,
            follow_symlinks=False,
        )
        if metadata.st_dev != root_device or _path_is_mount_point(
            child_path,
            mount_points=mount_points,
        ):
            raise RuntimeError(
                f"directory ownership scan refuses mounted content: {child_path}"
            )
        if _is_link_or_reparse(metadata):
            raise RuntimeError(
                f"directory ownership scan refuses linked content: {child_path}"
            )

        entry_hasher = hashlib.sha256()
        if stat.S_ISDIR(metadata.st_mode):
            child_descriptor = os.open(name, flags, dir_fd=descriptor)
            try:
                opened = os.fstat(child_descriptor)
                if _ownership_binding_identity(opened) != _ownership_binding_identity(
                    metadata
                ):
                    raise RuntimeError(
                        f"directory ownership directory changed: {child_path}"
                    )
                child_digest = _scan_owned_directory(
                    child_descriptor,
                    child_path,
                    child_parts,
                    root_device=root_device,
                    mount_points=mount_points,
                    budget=budget,
                    inventory=inventory,
                    depth=depth + 1,
                )
                after = os.fstat(child_descriptor)
                if _ownership_version_identity(after) != _ownership_version_identity(
                    opened
                ):
                    raise RuntimeError(
                        f"directory ownership directory changed: {child_path}"
                    )
            finally:
                os.close(child_descriptor)
            path_after = os.stat(
                name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            if _ownership_binding_identity(path_after) != _ownership_binding_identity(
                metadata
            ):
                raise RuntimeError(
                    f"directory ownership directory changed: {child_path}"
                )
            entry_hasher.update(b"D")
            _ownership_hash_field(entry_hasher, relative)
            entry_hasher.update(stat.S_IMODE(metadata.st_mode).to_bytes(4, "big"))
            entry_hasher.update(child_digest)
            inventory.append((os.fsdecode(relative), "directory"))
        elif stat.S_ISREG(metadata.st_mode):
            size, digest = _hash_owned_regular_file(
                descriptor,
                name,
                child_path,
                metadata,
                root_device=root_device,
                budget=budget,
            )
            digest_bytes = bytes.fromhex(digest)
            entry_hasher.update(b"F")
            _ownership_hash_field(entry_hasher, relative)
            entry_hasher.update(stat.S_IMODE(metadata.st_mode).to_bytes(4, "big"))
            entry_hasher.update(size.to_bytes(8, "big"))
            entry_hasher.update(digest_bytes)
            inventory.append((os.fsdecode(relative), "file"))
        else:
            raise RuntimeError(
                f"directory ownership scan refuses special content: {child_path}"
            )
        if raw_name == required_root_file and not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("directory ownership root marker is not a regular file")
        hasher.update(entry_hasher.digest())

    after = os.fstat(descriptor)
    if _ownership_version_identity(after) != _ownership_version_identity(before):
        raise RuntimeError(f"directory ownership root changed: {path}")
    return hasher.digest()


def capture_directory_ownership(
    path: Path,
    *,
    required_root_file: str | None = None,
    allow_empty_root: bool = False,
) -> _TreeOwnership:
    """Return a bounded content token for one stable, real directory tree."""

    required_root_file_bytes: bytes | None = None
    if required_root_file is not None:
        if (
            not required_root_file
            or required_root_file in {".", ".."}
            or "/" in required_root_file
            or "\\" in required_root_file
        ):
            raise ValueError("directory ownership marker must be one file name")
        required_root_file_bytes = os.fsencode(required_root_file)
        if len(required_root_file_bytes) > _MAX_OWNERSHIP_COMPONENT_BYTES:
            raise ValueError("directory ownership marker exceeds its byte limit")

    metadata = _directory_or_missing(path, label="directory ownership root")
    if metadata is None:
        raise RuntimeError(f"directory ownership root disappeared: {path}")
    if _path_is_mount_point(path):
        raise RuntimeError(f"directory ownership root is mounted: {path}")
    if not _SAFE_OWNERSHIP_DIRECTORY_FDS:
        # A path-only fallback cannot safely traverse a non-empty tree.  It can
        # still bind the empty sentinel used for a first publication: a raced
        # entry is checked again after the sentinel is moved and cleanup uses
        # rmdir rather than recursive deletion on the same platforms.
        with os.scandir(path) as entries:
            if next(iter(entries), None) is not None:
                raise RuntimeError(
                    "safe directory ownership requires no-follow directory-fd "
                    "support for non-empty trees"
                )
        after = _directory_or_missing(path, label="directory ownership root")
        if after is None or _ownership_binding_identity(
            after
        ) != _ownership_binding_identity(metadata):
            raise RuntimeError(f"directory ownership root changed: {path}")
        digest = hashlib.sha256()
        digest.update(b"codenib.atomic-directory.v1\x00")
        digest.update(stat.S_IMODE(metadata.st_mode).to_bytes(4, "big"))
        return _TreeOwnership(
            root_identity=_directory_inode_identity(metadata),
            digest=digest.hexdigest(),
            entries=0,
            byte_count=0,
            metadata_bytes=0,
            inventory=(),
        )
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if _ownership_binding_identity(opened) != _ownership_binding_identity(metadata):
            raise RuntimeError(f"directory ownership root changed: {path}")
        budget = _OwnershipBudget()
        inventory: list[tuple[str, str]] = []
        digest = _scan_owned_directory(
            descriptor,
            path,
            (),
            root_device=opened.st_dev,
            mount_points=_linux_mount_points(),
            budget=budget,
            inventory=inventory,
            depth=0,
            required_root_file=required_root_file_bytes,
            allow_empty_root=allow_empty_root,
        )
        after = os.fstat(descriptor)
        if _ownership_version_identity(after) != _ownership_version_identity(opened):
            raise RuntimeError(f"directory ownership root changed: {path}")
    finally:
        os.close(descriptor)
    path_after = _directory_or_missing(path, label="directory ownership root")
    if path_after is None or _ownership_binding_identity(
        path_after
    ) != _ownership_binding_identity(metadata):
        raise RuntimeError(f"directory ownership root changed: {path}")
    return _TreeOwnership(
        root_identity=_directory_inode_identity(opened),
        digest=digest.hex(),
        entries=budget.entries,
        byte_count=budget.byte_count,
        metadata_bytes=budget.metadata_bytes,
        inventory=tuple(sorted(inventory)),
    )


def directory_ownership_inventory(
    ownership: _TreeOwnership,
) -> tuple[tuple[str, str], ...]:
    """Return the bounded no-follow inventory captured with an ownership token."""

    if not isinstance(ownership, _TreeOwnership):
        raise TypeError("ownership must be a captured directory ownership token")
    return ownership.inventory


def directory_ownership_root_identity(
    ownership: _TreeOwnership,
) -> tuple[int, ...]:
    """Return the no-follow root identity bound by an ownership token."""

    if not isinstance(ownership, _TreeOwnership):
        raise TypeError("ownership must be a captured directory ownership token")
    return ownership.root_identity


def _require_tree_ownership(
    path: Path,
    expected: _TreeOwnership,
    *,
    label: str,
) -> None:
    try:
        observed = capture_directory_ownership(path)
    except (OSError, ValueError, RuntimeError) as exc:
        raise RuntimeError(f"{label} changed during directory publication") from exc
    if observed != expected:
        raise RuntimeError(f"{label} changed during directory publication")


def discard_owned_directory(path: Path, ownership: _TreeOwnership) -> None:
    """Best-effort cleanup that never follows a substituted root path."""

    try:
        metadata = _directory_or_missing(path, label="owned temporary directory")
    except (OSError, ValueError):
        return
    if (
        metadata is None
        or _directory_inode_identity(metadata) != ownership.root_identity
    ):
        return
    try:
        _remove_owned_directory(path, ownership.root_identity)
    except (OSError, RuntimeError, ValueError):
        # Temporary cleanup must not reacquire or remove a replacement path.
        # A partially removed owned tree is left for explicit orphan GC.
        return


def _require_safe_tree(
    path: Path,
    *,
    root_device: int,
    mount_points: frozenset[str],
    depth: int = 0,
) -> None:
    """Preflight cleanup without following links or crossing mount boundaries."""

    if depth > _MAX_SAFE_REMOVAL_DEPTH:
        raise RuntimeError(f"directory cleanup exceeds its depth limit: {path}")
    metadata = path.lstat()
    if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(f"directory cleanup target changed: {path}")
    if metadata.st_dev != root_device or _path_is_mount_point(
        path,
        mount_points=mount_points,
    ):
        raise RuntimeError(f"refusing to remove a mounted directory tree: {path}")
    with os.scandir(path) as entries:
        for entry in entries:
            child = path / entry.name
            child_metadata = entry.stat(follow_symlinks=False)
            if child_metadata.st_dev != root_device:
                raise RuntimeError(
                    f"refusing to cross a device during directory cleanup: {child}"
                )
            if _path_is_mount_point(child, mount_points=mount_points):
                raise RuntimeError(
                    f"refusing to remove a mounted filesystem entry: {child}"
                )
            if _is_link_or_reparse(child_metadata):
                if stat.S_ISDIR(child_metadata.st_mode) or bool(
                    getattr(child_metadata, "st_file_attributes", 0)
                    & _FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    raise RuntimeError(
                        f"refusing to follow a linked directory during cleanup: {child}"
                    )
                continue
            if stat.S_ISDIR(child_metadata.st_mode):
                _require_safe_tree(
                    child,
                    root_device=root_device,
                    mount_points=mount_points,
                    depth=depth + 1,
                )


def _remove_tree_at(
    parent_descriptor: int,
    name: str,
    path: Path,
    *,
    root_device: int,
    mount_points: frozenset[str],
    removal_state: _RemovalState,
    depth: int,
) -> None:
    if depth > _MAX_SAFE_REMOVAL_DEPTH:
        raise RuntimeError(f"directory cleanup exceeds its depth limit: {path}")
    metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if (
        _is_link_or_reparse(metadata)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_dev != root_device
        or _path_is_mount_point(path, mount_points=mount_points)
    ):
        raise RuntimeError(f"directory cleanup target changed: {path}")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    try:
        opened = os.fstat(descriptor)
        if _directory_inode_identity(opened) != _directory_inode_identity(metadata):
            raise RuntimeError(f"directory cleanup target changed: {path}")
        if opened.st_dev != root_device or _path_is_mount_point(
            path,
            mount_points=mount_points,
        ):
            raise RuntimeError(f"refusing to remove a mounted directory tree: {path}")
        while True:
            # A fresh open file description resets the directory stream without
            # materializing an attacker-sized list of names.  ``dup`` is not
            # sufficient because it shares the directory offset.
            scan_descriptor = os.open(".", flags, dir_fd=descriptor)
            try:
                if _directory_inode_identity(
                    os.fstat(scan_descriptor)
                ) != _directory_inode_identity(opened):
                    raise RuntimeError(f"directory cleanup target changed: {path}")
                with os.scandir(scan_descriptor) as entries:
                    entry = next(iter(entries), None)
            finally:
                os.close(scan_descriptor)
            if entry is None:
                break
            child_metadata = os.stat(
                entry.name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            child_path = path / entry.name
            if child_metadata.st_dev != root_device:
                raise RuntimeError(
                    f"refusing to cross a device during directory cleanup: {child_path}"
                )
            if _path_is_mount_point(child_path, mount_points=mount_points):
                raise RuntimeError(
                    f"refusing to remove a mounted filesystem entry: {child_path}"
                )
            if stat.S_ISDIR(child_metadata.st_mode) and not _is_link_or_reparse(
                child_metadata
            ):
                _remove_tree_at(
                    descriptor,
                    entry.name,
                    child_path,
                    root_device=root_device,
                    mount_points=mount_points,
                    removal_state=removal_state,
                    depth=depth + 1,
                )
            else:
                if bool(
                    getattr(child_metadata, "st_file_attributes", 0)
                    & _FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    raise RuntimeError(
                        f"refusing to remove a reparse point during cleanup: {child_path}"
                    )
                removal_state.destructive_started = True
                os.unlink(entry.name, dir_fd=descriptor)
        if _directory_inode_identity(os.fstat(descriptor)) != _directory_inode_identity(
            opened
        ):
            raise RuntimeError(f"directory cleanup target changed: {path}")
    finally:
        os.close(descriptor)
    removal_state.destructive_started = True
    os.rmdir(name, dir_fd=parent_descriptor)


def _remove_owned_directory(
    path: Path,
    expected_identity: tuple[int, ...],
    *,
    removal_state: _RemovalState | None = None,
) -> None:
    """Remove an owned tree without following links or crossing mounts."""

    if removal_state is None:
        removal_state = _RemovalState()
    metadata = _directory_or_missing(path, label="directory cleanup target")
    if metadata is None or _directory_inode_identity(metadata) != expected_identity:
        raise RuntimeError(f"directory cleanup target changed: {path}")
    root_device = metadata.st_dev
    if not _SAFE_REMOVAL_DIRECTORY_FDS:
        with os.scandir(path) as entries:
            if next(iter(entries), None) is not None:
                raise RuntimeError(
                    "safe directory cleanup requires no-follow directory-fd support"
                )
        final_metadata = _directory_or_missing(
            path,
            label="empty directory cleanup target",
        )
        if (
            final_metadata is None
            or _directory_inode_identity(final_metadata) != expected_identity
        ):
            raise RuntimeError(f"directory cleanup target changed: {path}")
        removal_state.destructive_started = True
        path.rmdir()
        return
    _require_safe_tree(
        path,
        root_device=root_device,
        mount_points=_linux_mount_points(),
    )
    parent_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_descriptor = os.open(path.parent, parent_flags)
    try:
        _remove_tree_at(
            parent_descriptor,
            path.name,
            path,
            root_device=root_device,
            mount_points=_linux_mount_points(),
            removal_state=removal_state,
            depth=0,
        )
    finally:
        os.close(parent_descriptor)


def _restore_previous_directory(
    backup: Path,
    destination: Path,
    *,
    destination_was_missing: bool,
) -> None:
    os.replace(backup, destination)
    if destination_was_missing:
        destination.rmdir()


def _quarantine_destination(destination: Path) -> Path | None:
    """Move an untrusted post-publication object out of the caller's stage path."""

    try:
        destination.lstat()
    except FileNotFoundError:
        return None
    quarantine = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.quarantine-",
            dir=str(destination.parent),
        )
    )
    quarantine.rmdir()
    try:
        os.replace(destination, quarantine)
    except BaseException:
        try:
            quarantine.rmdir()
        except OSError:
            pass
        raise
    return quarantine


def _recover_invalid_publication(
    backup: Path,
    destination: Path,
    *,
    destination_was_missing: bool,
    expected_backup_identity: tuple[int, ...],
    published_output_trusted: bool,
) -> Path | None:
    def require_expected_backup(*, message: str) -> None:
        try:
            backup_metadata = _directory_or_missing(
                backup,
                label="previous destination",
            )
        except (OSError, ValueError) as exc:
            raise _PreviousOutputIdentityLost(message) from exc
        if (
            backup_metadata is None
            or _directory_inode_identity(backup_metadata) != expected_backup_identity
        ):
            raise _PreviousOutputIdentityLost(message)

    if published_output_trusted:
        require_expected_backup(
            message=(
                "publication committed; cleanup incomplete; previous output "
                "identity lost"
            )
        )
    try:
        quarantine = _quarantine_destination(destination)
    except Exception as quarantine_error:
        raise RuntimeError(
            "published directory identity failed validation; the suspect "
            f"output remains at {destination} and the previous output remains "
            f"at {backup}"
        ) from quarantine_error
    if not published_output_trusted:
        suspect_disposition = (
            f"suspect output was quarantined at {quarantine}"
            if quarantine is not None
            else "suspect output vanished before quarantine"
        )
        require_expected_backup(
            message=(
                "published directory identity failed validation; "
                f"{suspect_disposition}; previous output identity lost; active "
                "destination is absent"
            )
        )
    try:
        _restore_previous_directory(
            backup,
            destination,
            destination_was_missing=destination_was_missing,
        )
    except OSError as restore_error:
        quarantine_message = (
            f"; suspect output is quarantined at {quarantine}"
            if quarantine is not None
            else ""
        )
        raise RuntimeError(
            "published directory identity failed validation and the previous "
            f"output could not be restored; it remains at {backup}"
            f"{quarantine_message}"
        ) from restore_error
    return quarantine


def publish_staged_directory(
    stage: Path,
    destination: Path,
    *,
    expected_destination_identity: tuple[int, ...] | None | object = (
        _EXPECTED_DESTINATION_UNSET
    ),
    expected_stage_identity: tuple[int, ...] | None = None,
    expected_stage_root_ownership: _TreeOwnership | None = None,
    expected_destination_ownership: _TreeOwnership | None | object = (
        _EXPECTED_OWNERSHIP_UNSET
    ),
    validate_staged_directory: Callable[[Path], None] | None = None,
    validate_moved_destination: Callable[[Path], None] | None = None,
    validate_published_destination: Callable[[Path], None] | None = None,
) -> None:
    """Publish *stage*, restoring an existing destination on rename failure.

    Both paths must share a parent filesystem so each rename is atomic. A
    successful call consumes *stage*. If the final rename fails, the previous
    destination is restored and *stage* remains available to caller cleanup.

    The destination-to-backup rename is the old tree's ownership linearization
    point. Callers that observed the destination before building a stage must
    pass that opaque ``expected_destination_ownership`` token; the helper
    rejects any completed pre-linearization change. ``validate_staged_directory``
    reruns the caller's artifact-specific verifier inside the publication
    boundary. ``validate_moved_destination`` runs against the exact moved old
    tree both before and after stage publication, while
    ``validate_published_destination`` verifies the new tree before cleanup.
    All cleanup preflight runs before deletion. An untrusted published tree is
    quarantined before the previous root is considered for restoration. During
    cleanup, the already-verified new tree stays active if the moved old root
    loses its captured identity. Once destructive cleanup starts, publication
    is also committed and failures preserve whatever remains at the backup
    path.
    """

    # Only parents are resolved.  Resolving the final destination component
    # would turn ``destination -> victim`` into ``victim`` and let a raced
    # symlink redirect the replacement into an unrelated directory.
    stage = lexical_directory_path(stage)
    destination = lexical_directory_path(destination)
    if stage == destination:
        raise ValueError("staged and destination directories must differ")
    if stage.parent != destination.parent:
        raise ValueError("staged and destination directories must share a parent")
    stage_metadata = _directory_or_missing(stage, label="staged directory")
    if stage_metadata is None:
        raise ValueError(f"staged directory does not exist: {stage}")
    observed_stage_identity = _directory_identity(stage_metadata)
    if (
        expected_stage_identity is not None
        and observed_stage_identity != expected_stage_identity
    ):
        raise RuntimeError("staged directory changed before directory publication")
    required_stage_inode_identity = _directory_inode_identity(stage_metadata)
    if (
        expected_stage_root_ownership is not None
        and required_stage_inode_identity != expected_stage_root_ownership.root_identity
    ):
        raise RuntimeError("staged directory root changed before publication")
    expected_stage_ownership = None
    if validate_staged_directory is not None:
        if _SAFE_OWNERSHIP_DIRECTORY_FDS:
            expected_stage_ownership = capture_directory_ownership(stage)
        validate_staged_directory(stage)
        if expected_stage_ownership is not None:
            _require_tree_ownership(
                stage,
                expected_stage_ownership,
                label="staged directory",
            )
    elif validate_published_destination is None:
        expected_stage_ownership = capture_directory_ownership(stage)
    destination_metadata = _directory_or_missing(
        destination,
        label="destination",
    )
    if expected_destination_ownership is not _EXPECTED_OWNERSHIP_UNSET:
        observed_ownership = (
            None
            if destination_metadata is None
            else capture_directory_ownership(destination)
        )
        if observed_ownership != expected_destination_ownership:
            raise RuntimeError("destination changed before directory publication")
    if expected_destination_identity is not _EXPECTED_DESTINATION_UNSET:
        observed_identity = (
            None
            if destination_metadata is None
            else _directory_identity(destination_metadata)
        )
        if observed_identity != expected_destination_identity:
            raise RuntimeError("destination changed before directory publication")

    destination_was_missing = destination_metadata is None
    if destination_was_missing:
        try:
            destination.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise ValueError(
                f"destination appeared during publication: {destination}"
            ) from exc
        destination_metadata = _directory_or_missing(
            destination,
            label="destination sentinel",
        )

    if destination_metadata is None:
        raise RuntimeError("destination sentinel disappeared during publication")
    required_destination_inode_identity = _directory_inode_identity(
        destination_metadata
    )
    try:
        if destination_was_missing:
            moved_destination_ownership = capture_directory_ownership(destination)
        elif expected_destination_ownership is not _EXPECTED_OWNERSHIP_UNSET:
            moved_destination_ownership = expected_destination_ownership
        elif validate_moved_destination is None:
            moved_destination_ownership = capture_directory_ownership(destination)
        else:
            moved_destination_ownership = None
    except BaseException:
        if destination_was_missing:
            try:
                destination.rmdir()
            except OSError:
                pass
        raise
    try:
        backup = Path(
            tempfile.mkdtemp(
                prefix=f".{destination.name}.previous-",
                dir=str(destination.parent),
            )
        )
        backup.rmdir()
    except BaseException:
        if destination_was_missing:
            destination.rmdir()
        raise

    if expected_stage_ownership is not None:
        try:
            _require_tree_ownership(
                stage,
                expected_stage_ownership,
                label="staged directory",
            )
        except Exception as exc:
            if destination_was_missing:
                destination.rmdir()
            raise RuntimeError(
                "staged directory changed before destination publication"
            ) from exc
        except BaseException:
            if destination_was_missing:
                try:
                    destination.rmdir()
                except OSError:
                    pass
            raise
    try:
        stage_before_destination_rename = _directory_or_missing(
            stage,
            label="staged directory",
        )
    except (OSError, ValueError) as exc:
        if destination_was_missing:
            destination.rmdir()
        raise RuntimeError(
            "staged directory changed before destination publication"
        ) from exc
    if (
        stage_before_destination_rename is None
        or _directory_inode_identity(stage_before_destination_rename)
        != required_stage_inode_identity
    ):
        if destination_was_missing:
            destination.rmdir()
        raise RuntimeError("staged directory changed before destination publication")

    try:
        os.replace(destination, backup)
    except BaseException:
        try:
            backup_after_error = _directory_or_missing(
                backup,
                label="moved destination after interrupted rename",
            )
            destination_after_error = _directory_or_missing(
                destination,
                label="destination after interrupted rename",
            )
        except (OSError, ValueError) as inspection_error:
            raise RuntimeError(
                "destination rename was interrupted and its publication state "
                f"could not be inspected; the previous output may remain at {backup}"
            ) from inspection_error
        rename_completed = (
            backup_after_error is not None
            and _directory_inode_identity(backup_after_error)
            == required_destination_inode_identity
            and destination_after_error is None
        )
        if rename_completed:
            try:
                _restore_previous_directory(
                    backup,
                    destination,
                    destination_was_missing=destination_was_missing,
                )
            except OSError as restore_error:
                raise RuntimeError(
                    "destination rename was interrupted after completion and the "
                    f"previous output could not be restored; inspect {backup} and "
                    f"{destination}"
                ) from restore_error
        elif destination_was_missing and (
            destination_after_error is not None
            and _directory_inode_identity(destination_after_error)
            == required_destination_inode_identity
        ):
            try:
                destination.rmdir()
            except OSError:
                # Preserve raced content at a destination that was initially
                # absent; it must never be recursively cleaned here.
                pass
        raise
    try:
        moved_metadata = _directory_or_missing(
            backup,
            label="moved destination",
        )
    except (OSError, ValueError) as exc:
        try:
            _restore_previous_directory(
                backup,
                destination,
                destination_was_missing=destination_was_missing,
            )
        except OSError as restore_error:
            raise RuntimeError(
                "destination changed at the publication boundary and the previous "
                f"output could not be restored; it remains at {backup}"
            ) from restore_error
        raise RuntimeError("destination changed at the publication boundary") from exc
    except BaseException:
        try:
            _restore_previous_directory(
                backup,
                destination,
                destination_was_missing=destination_was_missing,
            )
        except OSError as restore_error:
            raise RuntimeError(
                "destination boundary inspection was interrupted and the previous "
                f"output could not be restored; it remains at {backup}"
            ) from restore_error
        raise
    if moved_metadata is None:
        raise RuntimeError("destination disappeared at the publication boundary")
    if _directory_inode_identity(moved_metadata) != required_destination_inode_identity:
        _restore_previous_directory(
            backup,
            destination,
            destination_was_missing=destination_was_missing,
        )
        raise RuntimeError("destination changed at the publication boundary")

    if moved_destination_ownership is not None:
        try:
            _require_tree_ownership(
                backup,
                moved_destination_ownership,
                label="moved destination",
            )
        except Exception as ownership_error:
            try:
                _restore_previous_directory(
                    backup,
                    destination,
                    destination_was_missing=destination_was_missing,
                )
            except OSError as restore_error:
                raise RuntimeError(
                    "destination changed at the publication boundary; raced "
                    f"content remains at {destination}"
                ) from restore_error
            raise RuntimeError(
                "destination changed at the publication boundary"
            ) from ownership_error
        except BaseException:
            try:
                _restore_previous_directory(
                    backup,
                    destination,
                    destination_was_missing=destination_was_missing,
                )
            except OSError as restore_error:
                raise RuntimeError(
                    "destination ownership scan was interrupted and the previous "
                    f"output could not be restored; it remains at {backup}"
                ) from restore_error
            raise

    if validate_moved_destination is not None and not destination_was_missing:
        try:
            validate_moved_destination(backup)
        except BaseException:
            try:
                _restore_previous_directory(
                    backup,
                    destination,
                    destination_was_missing=False,
                )
            except OSError as restore_error:
                raise RuntimeError(
                    "moved destination validation failed and the previous output "
                    f"could not be restored; it remains at {backup}"
                ) from restore_error
            raise

    if expected_stage_ownership is not None:
        try:
            _require_tree_ownership(
                stage,
                expected_stage_ownership,
                label="staged directory",
            )
        except BaseException:
            _restore_previous_directory(
                backup,
                destination,
                destination_was_missing=destination_was_missing,
            )
            raise
    try:
        stage_before_publish = _directory_or_missing(
            stage,
            label="staged directory",
        )
    except (OSError, ValueError) as exc:
        _restore_previous_directory(
            backup,
            destination,
            destination_was_missing=destination_was_missing,
        )
        raise RuntimeError("staged directory changed before final publication") from exc
    except BaseException:
        try:
            _restore_previous_directory(
                backup,
                destination,
                destination_was_missing=destination_was_missing,
            )
        except OSError as restore_error:
            raise RuntimeError(
                "staged directory inspection was interrupted and the previous "
                f"output could not be restored; it remains at {backup}"
            ) from restore_error
        raise
    if (
        stage_before_publish is None
        or _directory_inode_identity(stage_before_publish)
        != required_stage_inode_identity
    ):
        _restore_previous_directory(
            backup,
            destination,
            destination_was_missing=destination_was_missing,
        )
        raise RuntimeError("staged directory changed before final publication")

    try:
        os.replace(stage, destination)
    except BaseException:
        try:
            _restore_previous_directory(
                backup,
                destination,
                destination_was_missing=destination_was_missing,
            )
        except OSError as restore_error:
            raise RuntimeError(
                "directory publication failed and the previous output could "
                f"not be restored; it remains at {backup}"
            ) from restore_error
        raise

    try:
        published_metadata = _directory_or_missing(
            destination,
            label="published staged directory",
        )
        if published_metadata is None:
            raise RuntimeError("staged directory disappeared during publication")
        if (
            _directory_inode_identity(published_metadata)
            != required_stage_inode_identity
        ):
            raise RuntimeError("staged directory changed at the publication boundary")
        if expected_stage_ownership is not None:
            _require_tree_ownership(
                destination,
                expected_stage_ownership,
                label="published staged directory",
            )
        if validate_published_destination is not None:
            validate_published_destination(destination)
        if expected_stage_ownership is not None:
            _require_tree_ownership(
                destination,
                expected_stage_ownership,
                label="published staged directory",
            )
    except Exception as boundary_error:
        quarantine = _recover_invalid_publication(
            backup,
            destination,
            destination_was_missing=destination_was_missing,
            expected_backup_identity=_directory_inode_identity(moved_metadata),
            published_output_trusted=False,
        )
        quarantine_message = (
            f" at {quarantine}" if quarantine is not None else " because it vanished"
        )
        raise RuntimeError(
            "published directory identity failed validation; suspect output was "
            f"quarantined{quarantine_message}"
        ) from boundary_error
    except BaseException:
        _recover_invalid_publication(
            backup,
            destination,
            destination_was_missing=destination_was_missing,
            expected_backup_identity=_directory_inode_identity(moved_metadata),
            published_output_trusted=False,
        )
        raise
    if moved_destination_ownership is not None:
        try:
            _require_tree_ownership(
                backup,
                moved_destination_ownership,
                label="previous destination",
            )
        except Exception as cleanup_error:
            try:
                quarantine = _recover_invalid_publication(
                    backup,
                    destination,
                    destination_was_missing=destination_was_missing,
                    expected_backup_identity=_directory_inode_identity(moved_metadata),
                    published_output_trusted=True,
                )
            except _PreviousOutputIdentityLost as recovery_error:
                raise recovery_error from cleanup_error
            quarantine_message = (
                f" at {quarantine}"
                if quarantine is not None
                else " because it vanished"
            )
            raise RuntimeError(
                "previous destination failed safe cleanup validation; newly "
                f"published output was quarantined{quarantine_message}"
            ) from cleanup_error
    if validate_moved_destination is not None and not destination_was_missing:
        try:
            validate_moved_destination(backup)
        except Exception as cleanup_error:
            try:
                quarantine = _recover_invalid_publication(
                    backup,
                    destination,
                    destination_was_missing=destination_was_missing,
                    expected_backup_identity=_directory_inode_identity(moved_metadata),
                    published_output_trusted=True,
                )
            except _PreviousOutputIdentityLost as recovery_error:
                raise recovery_error from cleanup_error
            quarantine_message = (
                f" at {quarantine}"
                if quarantine is not None
                else " because it vanished"
            )
            raise RuntimeError(
                "previous destination failed safe cleanup validation; newly "
                f"published output was quarantined{quarantine_message}"
            ) from cleanup_error

    removal_state = _RemovalState()
    try:
        _remove_owned_directory(
            backup,
            _directory_inode_identity(moved_metadata),
            removal_state=removal_state,
        )
    except Exception as cleanup_error:
        if removal_state.destructive_started:
            raise RuntimeError(
                "directory publication committed; cleanup incomplete; previous "
                f"output remains partially at {backup}"
            ) from cleanup_error
        quarantine = _recover_invalid_publication(
            backup,
            destination,
            destination_was_missing=destination_was_missing,
            expected_backup_identity=_directory_inode_identity(moved_metadata),
            published_output_trusted=True,
        )
        quarantine_message = (
            f" at {quarantine}" if quarantine is not None else " because it vanished"
        )
        raise RuntimeError(
            "previous destination failed safe cleanup validation; newly "
            f"published output was quarantined{quarantine_message}"
        ) from cleanup_error


__all__ = [
    "capture_directory_ownership",
    "directory_ownership_inventory",
    "directory_ownership_root_identity",
    "discard_owned_directory",
    "lexical_directory_path",
    "publish_staged_directory",
]
