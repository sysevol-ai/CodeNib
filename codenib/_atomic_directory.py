# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Rollback-safe publication of fully staged directory trees."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Callable

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_EXPECTED_DESTINATION_UNSET = object()


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
) -> Path | None:
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
    validate_moved_destination: Callable[[Path], None] | None = None,
) -> None:
    """Publish *stage*, restoring an existing destination on rename failure.

    Both paths must share a parent filesystem so each rename is atomic. A
    successful call consumes *stage*. If the final rename fails, the previous
    destination is restored and *stage* remains available to caller cleanup.

    The destination-to-backup rename is the ownership linearization point.
    ``validate_moved_destination`` runs against that exact moved directory, so
    every mutation completed before the rename must be reflected in validation.
    After the callback succeeds, the moved tree is owned by this publication.
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
        if _directory_identity(published_metadata) != _directory_identity(
            stage_metadata
        ):
            raise RuntimeError("staged directory changed at the publication boundary")
    except Exception as boundary_error:
        quarantine = _recover_invalid_publication(
            backup,
            destination,
            destination_was_missing=destination_was_missing,
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
        )
        raise
    else:
        shutil.rmtree(backup, ignore_errors=True)


__all__ = ["publish_staged_directory"]
