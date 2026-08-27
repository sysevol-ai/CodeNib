# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Existing-only local storage ownership for the Web index service."""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .._atomic_directory import _OrderedAction, _run_context_with_cleanup_actions
from ..storage import SQLiteCatalog

_MAX_BUSY_TIMEOUT_MS = 86_400_000
_MISSING_CATALOG = object()


class LocalIndexServiceError(RuntimeError):
    """The explicitly configured local index service is unavailable."""


def _canonical_catalog_path(value: Path) -> Path:
    if type(value) is not type(Path()):
        raise TypeError("local index catalog path must be an exact Path")
    if (
        not value.is_absolute()
        or value == value.parent
        or Path(os.path.abspath(os.fspath(value))) != value
    ):
        raise ValueError(
            "local index catalog path must be canonical, absolute, and non-root"
        )
    return value


def _observe_catalog_identity(path: Path) -> tuple[int, int, int]:
    """Attest one non-aliased, single-linked existing catalog file."""

    try:
        resolved_before = path.resolve(strict=True)
        metadata = path.lstat()
        resolved_after = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise LocalIndexServiceError(
            "local index catalog cannot be inspected safely"
        ) from exc
    identity = (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_nlink),
    )
    if (
        resolved_before != path
        or resolved_after != path
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or identity[0] < 1
        or identity[1] < 1
        or identity[2] != 1
    ):
        raise LocalIndexServiceError(
            "local index catalog must be one real single-linked file"
        )
    return identity


class _CatalogSessionOwner:
    """Retain an opened catalog until cancellation-safe cleanup completes."""

    __slots__ = ("_catalog",)

    def __init__(self) -> None:
        self._catalog: object = _MISSING_CATALOG

    @property
    def closed(self) -> bool:
        return self._catalog is _MISSING_CATALOG

    def acquire(self, factory) -> SQLiteCatalog:
        if self._catalog is not _MISSING_CATALOG:
            raise RuntimeError("local index catalog session is already open")
        catalog = factory()
        self._catalog = catalog
        return catalog

    def close(self) -> None:
        catalog = self._catalog
        if catalog is _MISSING_CATALOG:
            return
        catalog.close()  # type: ignore[attr-defined]
        self._catalog = _MISSING_CATALOG


@dataclass(frozen=True, slots=True)
class ExistingLocalIndexCatalogFactory:
    """Open short existing-only sessions bound to one catalog inode."""

    catalog_path: Path
    busy_timeout_ms: int = 5_000
    _catalog_identity: tuple[int, int, int] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if type(self) is not ExistingLocalIndexCatalogFactory:
            raise TypeError("local index catalog factory must use the exact type")
        path = _canonical_catalog_path(self.catalog_path)
        timeout = self.busy_timeout_ms
        if type(timeout) is not int or not 0 <= timeout <= _MAX_BUSY_TIMEOUT_MS:
            raise ValueError("local index catalog busy timeout is invalid")
        object.__setattr__(self, "catalog_path", path)
        object.__setattr__(self, "_catalog_identity", _observe_catalog_identity(path))

    @property
    def catalog_identity(self) -> tuple[int, int, int]:
        """Return the immutable existing-file identity for topology checks."""

        return self._catalog_identity

    def verify(self) -> None:
        """Fail if the configured catalog path no longer names its inode."""

        if _observe_catalog_identity(self.catalog_path) != self._catalog_identity:
            raise LocalIndexServiceError("local index catalog binding changed")

    @contextmanager
    def __call__(self) -> Iterator[SQLiteCatalog]:
        """Open one thread-confined catalog and revalidate its exit binding."""

        owner = _CatalogSessionOwner()
        cleanup_actions = (
            (
                "local index catalog exit binding validation also failed",
                self.verify,
            ),
            _OrderedAction(
                label="local index catalog session cleanup also failed",
                action=owner.close,
                complete=lambda: owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=owner,
            ),
        )
        with _run_context_with_cleanup_actions(cleanup_actions):
            self.verify()
            catalog = owner.acquire(
                lambda: SQLiteCatalog(
                    self.catalog_path,
                    create=False,
                    expected_file_identity=self._catalog_identity,
                    busy_timeout_ms=self.busy_timeout_ms,
                )
            )
            self.verify()
            yield catalog


__all__ = [
    "ExistingLocalIndexCatalogFactory",
    "LocalIndexServiceError",
]
