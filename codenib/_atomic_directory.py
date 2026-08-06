# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Rollback-safe publication of fully staged directory trees."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


def publish_staged_directory(stage: Path, destination: Path) -> None:
    """Publish *stage*, restoring an existing destination on rename failure.

    Both paths must share a parent filesystem so each rename is atomic. A
    successful call consumes *stage*. If the final rename fails, the previous
    destination is restored and *stage* remains available to caller cleanup.
    """

    stage = stage.resolve()
    destination = destination.resolve()
    if stage == destination:
        raise ValueError("staged and destination directories must differ")
    if stage.parent != destination.parent:
        raise ValueError("staged and destination directories must share a parent")
    if not stage.is_dir():
        raise ValueError(f"staged directory does not exist: {stage}")
    if destination.exists() and not destination.is_dir():
        raise ValueError(f"destination is not a directory: {destination}")

    backup: Path | None = None
    if destination.exists():
        backup = Path(
            tempfile.mkdtemp(
                prefix=f".{destination.name}.previous-",
                dir=str(destination.parent),
            )
        )
        backup.rmdir()
        os.replace(destination, backup)

    try:
        os.replace(stage, destination)
    except BaseException:
        if backup is not None:
            try:
                os.replace(backup, destination)
            except OSError as restore_error:
                raise RuntimeError(
                    "directory publication failed and the previous output could "
                    f"not be restored; it remains at {backup}"
                ) from restore_error
        raise
    else:
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)


__all__ = ["publish_staged_directory"]
