# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Transactional catalog and immutable artifact storage contracts."""

from .cas import BlobInfo, LocalCAS
from .models import (
    ArtifactMember,
    ObjectRecord,
    PublishConflict,
    PublishedSnapshot,
    RefState,
    RepositoryIdentity,
    SnapshotView,
    SourceRevision,
    StorageError,
    StorageIntegrityError,
    StorageNotFound,
    StorageValidationError,
    ViewGeneration,
    ViewProfile,
)
from .protocols import IndexCatalog, ObjectStore
from .sqlite_catalog import (
    DEFAULT_NAMESPACE_ID,
    CatalogConflictError,
    CatalogError,
    CatalogNotFoundError,
    CatalogValidationError,
    SQLiteCatalog,
)

__all__ = [
    "ArtifactMember",
    "BlobInfo",
    "CatalogConflictError",
    "CatalogError",
    "CatalogNotFoundError",
    "CatalogValidationError",
    "DEFAULT_NAMESPACE_ID",
    "IndexCatalog",
    "LocalCAS",
    "ObjectRecord",
    "ObjectStore",
    "PublishConflict",
    "PublishedSnapshot",
    "RefState",
    "RepositoryIdentity",
    "SQLiteCatalog",
    "SnapshotView",
    "SourceRevision",
    "StorageError",
    "StorageIntegrityError",
    "StorageNotFound",
    "StorageValidationError",
    "ViewGeneration",
    "ViewProfile",
]
