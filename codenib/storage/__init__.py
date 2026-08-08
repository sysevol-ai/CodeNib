# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Transactional catalog and immutable artifact storage contracts."""

from .cas import BlobInfo, LocalCAS
from .models import (
    INDEX_JOB_REQUEST_CONTRACT,
    ArtifactMember,
    IndexJobCompletion,
    IndexJobRecord,
    IndexJobRequest,
    IndexJobRequestedMode,
    IndexJobStatus,
    IndexJobViewRecord,
    ObjectRecord,
    PublishConflict,
    PublishedSnapshot,
    RefJobLease,
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
from .protocols import IndexCatalog, JobCatalog, ObjectStore
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
    "INDEX_JOB_REQUEST_CONTRACT",
    "IndexCatalog",
    "IndexJobCompletion",
    "IndexJobRecord",
    "IndexJobRequest",
    "IndexJobRequestedMode",
    "IndexJobStatus",
    "IndexJobViewRecord",
    "JobCatalog",
    "LocalCAS",
    "ObjectRecord",
    "ObjectStore",
    "PublishConflict",
    "PublishedSnapshot",
    "RefState",
    "RefJobLease",
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
