# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Rollback-safe publication of fully staged directory trees."""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_EXPECTED_DESTINATION_UNSET = object()
_MAX_SAFE_REMOVAL_DEPTH = 256
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


@dataclass(slots=True)
class _RemovalState:
    destructive_started: bool = False


class _PreviousOutputIdentityLost(RuntimeError):
    """The moved previous tree can no longer be trusted for rollback."""


def _lexical_child_of_resolved_parent(path: Path) -> Path:
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
        if _directory_identity(opened) != _directory_identity(metadata):
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
    if metadata is None or _directory_identity(metadata) != expected_identity:
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
            or _directory_identity(final_metadata) != expected_identity
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
    expected_backup_identity: tuple[int, ...] | None = None,
) -> Path | None:
    if expected_backup_identity is not None:
        try:
            backup_metadata = _directory_or_missing(
                backup,
                label="previous destination",
            )
        except (OSError, ValueError) as exc:
            raise _PreviousOutputIdentityLost(
                "publication committed; cleanup incomplete; previous output "
                "identity lost"
            ) from exc
        if (
            backup_metadata is None
            or _directory_identity(backup_metadata) != expected_backup_identity
        ):
            raise _PreviousOutputIdentityLost(
                "publication committed; cleanup incomplete; previous output "
                "identity lost"
            )
    try:
        quarantine = _quarantine_destination(destination)
    except Exception as quarantine_error:
        raise RuntimeError(
            "published directory identity failed validation; the suspect "
            f"output remains at {destination} and the previous output remains "
            f"at {backup}"
        ) from quarantine_error
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
    validate_moved_destination: Callable[[Path], None] | None = None,
    validate_published_destination: Callable[[Path], None] | None = None,
) -> None:
    """Publish *stage*, restoring an existing destination on rename failure.

    Both paths must share a parent filesystem so each rename is atomic. A
    successful call consumes *stage*. If the final rename fails, the previous
    destination is restored and *stage* remains available to caller cleanup.

    The destination-to-backup rename is the old tree's ownership linearization
    point. ``validate_moved_destination`` runs against that exact moved tree
    both before and after stage publication. ``validate_published_destination``
    binds the new tree's complete ownership token before cleanup. All cleanup
    preflight runs before deletion. Until the first unlink/rmdir, failure can
    quarantine the new tree and restore the old one only while the moved old
    root still has its captured identity. If that identity is lost, or once
    destructive cleanup starts, publication is committed: failures retain the
    new destination and preserve whatever remains at the backup path.
    """

    # Only parents are resolved.  Resolving the final destination component
    # would turn ``destination -> victim`` into ``victim`` and let a raced
    # symlink redirect the replacement into an unrelated directory.
    stage = _lexical_child_of_resolved_parent(stage)
    destination = _lexical_child_of_resolved_parent(destination)
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
    required_stage_identity = expected_stage_identity or observed_stage_identity
    destination_metadata = _directory_or_missing(
        destination,
        label="destination",
    )
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
        or _directory_identity(stage_before_destination_rename)
        != required_stage_identity
    ):
        if destination_was_missing:
            destination.rmdir()
        raise RuntimeError("staged directory changed before destination publication")

    try:
        os.replace(destination, backup)
    except BaseException:
        if destination_was_missing:
            destination.rmdir()
        raise
    try:
        moved_metadata = _directory_or_missing(
            backup,
            label="moved destination",
        )
    except (OSError, ValueError) as exc:
        os.replace(backup, destination)
        raise RuntimeError("destination changed at the publication boundary") from exc
    if moved_metadata is None:
        raise RuntimeError("destination disappeared at the publication boundary")
    if _directory_identity(moved_metadata) != _directory_identity(destination_metadata):
        _restore_previous_directory(
            backup,
            destination,
            destination_was_missing=destination_was_missing,
        )
        raise RuntimeError("destination changed at the publication boundary")

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
    if (
        stage_before_publish is None
        or _directory_identity(stage_before_publish) != required_stage_identity
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
        if _directory_identity(published_metadata) != required_stage_identity:
            raise RuntimeError("staged directory changed at the publication boundary")
        if validate_published_destination is not None:
            validate_published_destination(destination)
    except Exception as boundary_error:
        quarantine = _recover_invalid_publication(
            backup,
            destination,
            destination_was_missing=destination_was_missing,
            expected_backup_identity=_directory_identity(moved_metadata),
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
            expected_backup_identity=_directory_identity(moved_metadata),
        )
        raise
    if validate_moved_destination is not None and not destination_was_missing:
        try:
            validate_moved_destination(backup)
        except Exception as cleanup_error:
            try:
                quarantine = _recover_invalid_publication(
                    backup,
                    destination,
                    destination_was_missing=destination_was_missing,
                    expected_backup_identity=_directory_identity(moved_metadata),
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
            _directory_identity(moved_metadata),
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
            expected_backup_identity=_directory_identity(moved_metadata),
        )
        quarantine_message = (
            f" at {quarantine}" if quarantine is not None else " because it vanished"
        )
        raise RuntimeError(
            "previous destination failed safe cleanup validation; newly "
            f"published output was quarantined{quarantine_message}"
        ) from cleanup_error


__all__ = ["publish_staged_directory"]
