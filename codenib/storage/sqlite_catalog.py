# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""SQLite control-plane catalog for immutable index generations.

The catalog deliberately stores metadata, not index payloads.  Payloads live in
an artifact store and are registered here by their SHA-256 digest.  Published
snapshots and ready view generations are immutable; the only mutable serving
pointer is a named ref, advanced with compare-and-swap semantics.

This module uses only primitive Python values so higher-level storage protocols
can adapt it without coupling the SQLite implementation to application models.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .models import (
    IndexJobCompletion,
    IndexJobRecord,
    IndexJobRequest,
    IndexJobStatus,
    IndexJobViewRecord,
    ObjectRecord,
    PublishConflict,
    RefJobLease,
    SourceRevision,
    StorageError,
    StorageIntegrityError,
    StorageNotFound,
    StorageValidationError,
    canonical_json,
    content_id,
    normalize_digest,
)

LATEST_SCHEMA_VERSION = 2
DEFAULT_NAMESPACE_ID = "ns_default"
DEFAULT_NAMESPACE_NAME = "default"

CatalogError = StorageError
CatalogConflictError = PublishConflict
CatalogNotFoundError = StorageNotFound
CatalogValidationError = StorageValidationError

_DB_NOW_MS_SQL = "CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalogValidationError(f"{field} must not be empty")
    return value.strip()


def _bounded_text(value: str, field: str, *, max_length: int) -> str:
    normalized = _required_text(value, field)
    if "\x00" in normalized:
        raise CatalogValidationError(f"{field} must not contain NUL")
    if len(normalized) > max_length:
        raise CatalogValidationError(f"{field} must not exceed {max_length} characters")
    return normalized


def _optional_bounded_text(
    value: str | None, field: str, *, max_length: int
) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field, max_length=max_length)


def _positive_integer(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CatalogValidationError(f"{field} must be a positive integer")
    return value


_SCHEMA_V1 = (
    """
    CREATE TABLE namespaces (
        namespace_id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE repositories (
        repository_id TEXT PRIMARY KEY,
        namespace_id TEXT NOT NULL,
        repository_key TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (namespace_id, repository_key),
        FOREIGN KEY (namespace_id) REFERENCES namespaces(namespace_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE source_revisions (
        source_revision_id TEXT PRIMARY KEY,
        repository_id TEXT NOT NULL,
        source_kind TEXT NOT NULL CHECK (source_kind IN ('clean', 'dirty')),
        commit_sha TEXT,
        tree_sha TEXT,
        source_fingerprint TEXT NOT NULL,
        identity_digest TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (repository_id, identity_digest),
        UNIQUE (source_revision_id, repository_id),
        CHECK (
            (source_kind = 'clean'
                AND commit_sha IS NOT NULL AND length(commit_sha) > 0
                AND tree_sha IS NOT NULL AND length(tree_sha) > 0)
            OR
            (source_kind = 'dirty' AND length(source_fingerprint) > 0)
        ),
        FOREIGN KEY (repository_id) REFERENCES repositories(repository_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE view_profiles (
        profile_id TEXT PRIMARY KEY,
        view_type TEXT NOT NULL,
        name TEXT NOT NULL,
        config_json TEXT NOT NULL,
        profile_digest TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        UNIQUE (profile_id, view_type)
    )
    """,
    """
    CREATE TABLE objects (
        digest TEXT PRIMARY KEY,
        storage_key TEXT NOT NULL,
        byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
        media_type TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (storage_key)
    )
    """,
    """
    CREATE TABLE view_generations (
        view_generation_id TEXT PRIMARY KEY,
        repository_id TEXT NOT NULL,
        source_revision_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        view_type TEXT NOT NULL,
        object_digest TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        metadata_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('staged', 'ready')),
        created_at TEXT NOT NULL,
        ready_at TEXT,
        UNIQUE (view_generation_id, view_type),
        FOREIGN KEY (repository_id) REFERENCES repositories(repository_id)
            ON DELETE RESTRICT,
        FOREIGN KEY (source_revision_id, repository_id)
            REFERENCES source_revisions(source_revision_id, repository_id)
            ON DELETE RESTRICT,
        FOREIGN KEY (profile_id, view_type)
            REFERENCES view_profiles(profile_id, view_type) ON DELETE RESTRICT,
        FOREIGN KEY (object_digest) REFERENCES objects(digest)
            ON DELETE RESTRICT,
        CHECK (
            (status = 'staged' AND ready_at IS NULL)
            OR (status = 'ready' AND ready_at IS NOT NULL)
        )
    )
    """,
    """
    CREATE TABLE snapshots (
        snapshot_id TEXT PRIMARY KEY,
        repository_id TEXT NOT NULL,
        source_revision_id TEXT NOT NULL,
        content_digest TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('building', 'ready')),
        published_at TEXT,
        UNIQUE (repository_id, content_digest),
        UNIQUE (snapshot_id, repository_id),
        FOREIGN KEY (repository_id) REFERENCES repositories(repository_id)
            ON DELETE RESTRICT,
        FOREIGN KEY (source_revision_id, repository_id)
            REFERENCES source_revisions(source_revision_id, repository_id)
            ON DELETE RESTRICT,
        CHECK (
            (status = 'building' AND published_at IS NULL)
            OR (status = 'ready' AND published_at IS NOT NULL)
        )
    )
    """,
    """
    CREATE TABLE snapshot_views (
        snapshot_id TEXT NOT NULL,
        view_type TEXT NOT NULL,
        view_generation_id TEXT NOT NULL,
        PRIMARY KEY (snapshot_id, view_type),
        UNIQUE (snapshot_id, view_generation_id),
        FOREIGN KEY (snapshot_id) REFERENCES snapshots(snapshot_id)
            ON DELETE CASCADE,
        FOREIGN KEY (view_generation_id, view_type)
            REFERENCES view_generations(view_generation_id, view_type)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE refs (
        repository_id TEXT NOT NULL,
        ref_name TEXT NOT NULL,
        snapshot_id TEXT NOT NULL,
        generation INTEGER NOT NULL CHECK (generation > 0),
        updated_at TEXT NOT NULL,
        PRIMARY KEY (repository_id, ref_name),
        FOREIGN KEY (repository_id) REFERENCES repositories(repository_id)
            ON DELETE RESTRICT,
        FOREIGN KEY (snapshot_id, repository_id)
            REFERENCES snapshots(snapshot_id, repository_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX source_revisions_repository_idx
        ON source_revisions(repository_id)
    """,
    """
    CREATE INDEX view_generations_source_idx
        ON view_generations(repository_id, source_revision_id, profile_id)
    """,
    """
    CREATE INDEX refs_snapshot_idx ON refs(snapshot_id)
    """,
    """
    CREATE TRIGGER objects_are_immutable
    BEFORE UPDATE ON objects
    BEGIN
        SELECT RAISE(ABORT, 'registered objects are immutable');
    END
    """,
    """
    CREATE TRIGGER namespaces_are_immutable
    BEFORE UPDATE ON namespaces
    BEGIN
        SELECT RAISE(ABORT, 'namespace identities are immutable');
    END
    """,
    """
    CREATE TRIGGER repositories_are_immutable
    BEFORE UPDATE ON repositories
    BEGIN
        SELECT RAISE(ABORT, 'repository identities are immutable');
    END
    """,
    """
    CREATE TRIGGER source_revisions_are_immutable
    BEFORE UPDATE ON source_revisions
    BEGIN
        SELECT RAISE(ABORT, 'source revisions are immutable');
    END
    """,
    """
    CREATE TRIGGER view_profiles_are_immutable
    BEFORE UPDATE ON view_profiles
    BEGIN
        SELECT RAISE(ABORT, 'view profiles are immutable');
    END
    """,
    """
    CREATE TRIGGER staged_view_generation_identity_is_immutable
    BEFORE UPDATE ON view_generations
    WHEN
        NEW.view_generation_id IS NOT OLD.view_generation_id
        OR NEW.repository_id IS NOT OLD.repository_id
        OR NEW.source_revision_id IS NOT OLD.source_revision_id
        OR NEW.profile_id IS NOT OLD.profile_id
        OR NEW.view_type IS NOT OLD.view_type
        OR NEW.object_digest IS NOT OLD.object_digest
        OR NEW.schema_version IS NOT OLD.schema_version
        OR NEW.metadata_json IS NOT OLD.metadata_json
        OR NEW.created_at IS NOT OLD.created_at
    BEGIN
        SELECT RAISE(ABORT, 'view generation identity is immutable');
    END
    """,
    """
    CREATE TRIGGER ready_view_generations_are_immutable
    BEFORE UPDATE ON view_generations
    WHEN OLD.status = 'ready'
    BEGIN
        SELECT RAISE(ABORT, 'ready view generations are immutable');
    END
    """,
    """
    CREATE TRIGGER snapshot_seal_is_the_only_update
    BEFORE UPDATE ON snapshots
    WHEN NOT (
        OLD.status = 'building'
        AND OLD.published_at IS NULL
        AND NEW.status = 'ready'
        AND NEW.published_at IS NOT NULL
        AND NEW.snapshot_id IS OLD.snapshot_id
        AND NEW.repository_id IS OLD.repository_id
        AND NEW.source_revision_id IS OLD.source_revision_id
        AND NEW.content_digest IS OLD.content_digest
        AND EXISTS (
            SELECT 1 FROM snapshot_views
            WHERE snapshot_id = OLD.snapshot_id
        )
        AND NOT EXISTS (
            SELECT 1
            FROM snapshot_views AS sv
            JOIN view_generations AS vg
                ON vg.view_generation_id = sv.view_generation_id
            WHERE sv.snapshot_id = OLD.snapshot_id
                AND (
                    vg.status != 'ready'
                    OR vg.repository_id != OLD.repository_id
                    OR vg.source_revision_id != OLD.source_revision_id
                )
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'only an immutable snapshot seal is allowed');
    END
    """,
    """
    CREATE TRIGGER snapshot_views_are_immutable
    BEFORE UPDATE ON snapshot_views
    BEGIN
        SELECT RAISE(ABORT, 'published snapshot views are immutable');
    END
    """,
    """
    CREATE TRIGGER ready_snapshot_views_cannot_be_inserted
    BEFORE INSERT ON snapshot_views
    WHEN (SELECT status FROM snapshots WHERE snapshot_id = NEW.snapshot_id) = 'ready'
    BEGIN
        SELECT RAISE(ABORT, 'ready snapshot views are immutable');
    END
    """,
    """
    CREATE TRIGGER ready_snapshot_views_cannot_be_deleted
    BEFORE DELETE ON snapshot_views
    WHEN (SELECT status FROM snapshots WHERE snapshot_id = OLD.snapshot_id) = 'ready'
    BEGIN
        SELECT RAISE(ABORT, 'ready snapshot views are immutable');
    END
    """,
    """
    CREATE TRIGGER refs_require_ready_snapshot_on_insert
    BEFORE INSERT ON refs
    WHEN (
        SELECT status FROM snapshots WHERE snapshot_id = NEW.snapshot_id
    ) IS NOT 'ready'
    BEGIN
        SELECT RAISE(ABORT, 'refs may only target ready snapshots');
    END
    """,
    """
    CREATE TRIGGER refs_require_ready_snapshot_on_update
    BEFORE UPDATE ON refs
    WHEN (
        SELECT status FROM snapshots WHERE snapshot_id = NEW.snapshot_id
    ) IS NOT 'ready'
    BEGIN
        SELECT RAISE(ABORT, 'refs may only target ready snapshots');
    END
    """,
)

_SCHEMA_V2 = (
    """
    CREATE UNIQUE INDEX snapshots_source_identity_idx
        ON snapshots(snapshot_id, source_revision_id, repository_id)
    """,
    """
    CREATE TABLE index_jobs (
        job_id TEXT PRIMARY KEY,
        repository_id TEXT NOT NULL,
        source_revision_id TEXT NOT NULL,
        ref_name TEXT NOT NULL CHECK (
            length(ref_name) BETWEEN 1 AND 512 AND instr(ref_name, char(0)) = 0
        ),
        idempotency_key TEXT NOT NULL CHECK (
            length(idempotency_key) BETWEEN 1 AND 256
            AND instr(idempotency_key, char(0)) = 0
        ),
        expected_ref_generation INTEGER NOT NULL CHECK (
            expected_ref_generation BETWEEN 0 AND 9223372036854775807
        ),
        max_attempts INTEGER NOT NULL CHECK (max_attempts BETWEEN 1 AND 1000),
        request_contract TEXT NOT NULL CHECK (
            length(request_contract) BETWEEN 1 AND 128
            AND instr(request_contract, char(0)) = 0
        ),
        request_json TEXT NOT NULL CHECK (
            length(request_json) BETWEEN 1 AND 65536
            AND instr(request_json, char(0)) = 0
            AND json_valid(request_json)
        ),
        request_digest TEXT NOT NULL,
        status TEXT NOT NULL CHECK (
            status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')
        ),
        cancel_requested INTEGER NOT NULL DEFAULT 0
            CHECK (cancel_requested IN (0, 1)),
        attempt_count INTEGER NOT NULL DEFAULT 0
            CHECK (attempt_count BETWEEN 0 AND max_attempts),
        result_snapshot_id TEXT,
        error_code TEXT CHECK (
            error_code IS NULL OR (
                length(error_code) BETWEEN 1 AND 128
                AND instr(error_code, char(0)) = 0
            )
        ),
        error_message TEXT CHECK (
            error_message IS NULL OR (
                length(error_message) BETWEEN 1 AND 4096
                AND instr(error_message, char(0)) = 0
            )
        ),
        created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
        updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
        started_at_ms INTEGER CHECK (started_at_ms IS NULL OR started_at_ms >= 0),
        finished_at_ms INTEGER CHECK (finished_at_ms IS NULL OR finished_at_ms >= 0),
        UNIQUE (repository_id, idempotency_key),
        UNIQUE (job_id, repository_id, ref_name),
        FOREIGN KEY (repository_id) REFERENCES repositories(repository_id)
            ON DELETE RESTRICT,
        FOREIGN KEY (source_revision_id, repository_id)
            REFERENCES source_revisions(source_revision_id, repository_id)
            ON DELETE RESTRICT,
        FOREIGN KEY (result_snapshot_id, source_revision_id, repository_id)
            REFERENCES snapshots(snapshot_id, source_revision_id, repository_id)
            ON DELETE RESTRICT,
        CHECK (error_message IS NULL OR error_code IS NOT NULL),
        CHECK (status != 'running' OR started_at_ms IS NOT NULL),
        CHECK (status != 'cancelled' OR cancel_requested = 1),
        CHECK (started_at_ms IS NULL OR started_at_ms >= created_at_ms),
        CHECK (
            finished_at_ms IS NULL
            OR finished_at_ms >= COALESCE(started_at_ms, created_at_ms)
        ),
        CHECK (started_at_ms IS NULL OR updated_at_ms >= started_at_ms),
        CHECK (finished_at_ms IS NULL OR updated_at_ms >= finished_at_ms),
        CHECK (
            (status IN ('queued', 'running')
                AND finished_at_ms IS NULL AND result_snapshot_id IS NULL)
            OR
            (status = 'succeeded'
                AND finished_at_ms IS NOT NULL
                AND result_snapshot_id IS NOT NULL
                AND error_code IS NULL AND error_message IS NULL)
            OR
            (status = 'failed'
                AND finished_at_ms IS NOT NULL
                AND result_snapshot_id IS NULL AND error_code IS NOT NULL)
            OR
            (status = 'cancelled'
                AND finished_at_ms IS NOT NULL AND result_snapshot_id IS NULL)
        )
    )
    """,
    """
    CREATE TABLE index_job_views (
        job_id TEXT NOT NULL,
        view_type TEXT NOT NULL CHECK (
            length(view_type) BETWEEN 1 AND 128
            AND instr(view_type, char(0)) = 0
        ),
        profile_id TEXT NOT NULL,
        requested_mode TEXT NOT NULL
            CHECK (requested_mode IN ('auto', 'full', 'incremental')),
        required INTEGER NOT NULL CHECK (required IN (0, 1)),
        PRIMARY KEY (job_id, view_type),
        FOREIGN KEY (job_id) REFERENCES index_jobs(job_id) ON DELETE CASCADE,
        FOREIGN KEY (profile_id, view_type)
            REFERENCES view_profiles(profile_id, view_type) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE ref_job_leases (
        repository_id TEXT NOT NULL,
        ref_name TEXT NOT NULL CHECK (
            length(ref_name) BETWEEN 1 AND 512 AND instr(ref_name, char(0)) = 0
        ),
        job_id TEXT,
        owner_id TEXT CHECK (
            owner_id IS NULL OR (
                length(owner_id) BETWEEN 1 AND 256
                AND instr(owner_id, char(0)) = 0
            )
        ),
        fencing_token INTEGER NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
        acquired_at_ms INTEGER CHECK (
            acquired_at_ms IS NULL OR acquired_at_ms >= 0
        ),
        heartbeat_at_ms INTEGER CHECK (
            heartbeat_at_ms IS NULL OR heartbeat_at_ms >= 0
        ),
        lease_expires_at_ms INTEGER CHECK (
            lease_expires_at_ms IS NULL OR lease_expires_at_ms >= 0
        ),
        updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0),
        PRIMARY KEY (repository_id, ref_name),
        UNIQUE (job_id),
        FOREIGN KEY (repository_id) REFERENCES repositories(repository_id)
            ON DELETE RESTRICT,
        FOREIGN KEY (job_id, repository_id, ref_name)
            REFERENCES index_jobs(job_id, repository_id, ref_name)
            ON DELETE RESTRICT,
        CHECK (
            (job_id IS NULL AND owner_id IS NULL
                AND acquired_at_ms IS NULL AND heartbeat_at_ms IS NULL
                AND lease_expires_at_ms IS NULL)
            OR
            (job_id IS NOT NULL AND owner_id IS NOT NULL
                AND acquired_at_ms IS NOT NULL AND heartbeat_at_ms IS NOT NULL
                AND lease_expires_at_ms IS NOT NULL
                AND acquired_at_ms <= heartbeat_at_ms
                AND heartbeat_at_ms < lease_expires_at_ms)
        )
    )
    """,
    """
    CREATE INDEX index_jobs_queue_idx
        ON index_jobs(status, repository_id, ref_name, created_at_ms)
    """,
    """
    CREATE INDEX ref_job_leases_expiry_idx
        ON ref_job_leases(lease_expires_at_ms)
        WHERE job_id IS NOT NULL
    """,
    """
    CREATE TRIGGER index_jobs_reject_duplicate_inserts
    BEFORE INSERT ON index_jobs
    WHEN EXISTS (
        SELECT 1 FROM index_jobs AS existing
        WHERE existing.job_id = NEW.job_id
            OR (
                existing.repository_id = NEW.repository_id
                AND existing.idempotency_key = NEW.idempotency_key
            )
    )
    BEGIN
        SELECT RAISE(ABORT, 'duplicate index job insert is forbidden');
    END
    """,
    """
    CREATE TRIGGER index_job_request_is_immutable
    BEFORE UPDATE ON index_jobs
    WHEN
        NEW.job_id IS NOT OLD.job_id
        OR NEW.repository_id IS NOT OLD.repository_id
        OR NEW.source_revision_id IS NOT OLD.source_revision_id
        OR NEW.ref_name IS NOT OLD.ref_name
        OR NEW.idempotency_key IS NOT OLD.idempotency_key
        OR NEW.expected_ref_generation IS NOT OLD.expected_ref_generation
        OR NEW.max_attempts IS NOT OLD.max_attempts
        OR NEW.request_contract IS NOT OLD.request_contract
        OR NEW.request_json IS NOT OLD.request_json
        OR NEW.request_digest IS NOT OLD.request_digest
        OR NEW.created_at_ms IS NOT OLD.created_at_ms
    BEGIN
        SELECT RAISE(ABORT, 'index job request is immutable');
    END
    """,
    """
    CREATE TRIGGER terminal_index_jobs_are_immutable
    BEFORE UPDATE ON index_jobs
    WHEN OLD.status IN ('succeeded', 'failed', 'cancelled')
    BEGIN
        SELECT RAISE(ABORT, 'terminal index jobs are immutable');
    END
    """,
    """
    CREATE TRIGGER index_job_status_transitions_are_valid
    BEFORE UPDATE ON index_jobs
    WHEN NOT (
        (OLD.status = 'queued' AND NEW.status IN ('queued', 'cancelled'))
        OR
        (
            OLD.status = 'queued' AND NEW.status = 'running'
            AND EXISTS (
                SELECT 1 FROM ref_job_leases AS lease
                WHERE lease.repository_id = NEW.repository_id
                    AND lease.ref_name = NEW.ref_name
                    AND lease.job_id = NEW.job_id
                    AND lease.owner_id IS NOT NULL
            )
        )
        OR
        (OLD.status = 'running'
            AND NEW.status IN (
                'running', 'queued', 'succeeded', 'failed', 'cancelled'
            ))
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid index job status transition');
    END
    """,
    """
    CREATE TRIGGER m1_index_jobs_cannot_insert_succeeded
    BEFORE INSERT ON index_jobs
    WHEN NEW.status = 'succeeded'
    BEGIN
        SELECT RAISE(ABORT, 'M1 cannot persist successful index jobs');
    END
    """,
    """
    CREATE TRIGGER m1_index_jobs_cannot_update_succeeded
    BEFORE UPDATE ON index_jobs
    WHEN NEW.status = 'succeeded'
    BEGIN
        SELECT RAISE(ABORT, 'M1 cannot persist successful index jobs');
    END
    """,
    """
    CREATE TRIGGER m1_index_jobs_must_start_queued
    BEFORE INSERT ON index_jobs
    WHEN NEW.status != 'queued'
    BEGIN
        SELECT RAISE(ABORT, 'M1 index jobs must start queued');
    END
    """,
    """
    CREATE TRIGGER index_job_views_are_immutable
    BEFORE UPDATE ON index_job_views
    BEGIN
        SELECT RAISE(ABORT, 'index job view requests are immutable');
    END
    """,
    """
    CREATE TRIGGER index_job_views_cannot_be_deleted
    BEFORE DELETE ON index_job_views
    BEGIN
        SELECT RAISE(ABORT, 'index job view requests are immutable');
    END
    """,
    """
    CREATE TRIGGER index_job_views_reject_duplicate_inserts
    BEFORE INSERT ON index_job_views
    WHEN EXISTS (
        SELECT 1 FROM index_job_views AS existing
        WHERE existing.job_id = NEW.job_id
            AND existing.view_type = NEW.view_type
    )
    BEGIN
        SELECT RAISE(ABORT, 'duplicate index job view insert is forbidden');
    END
    """,
    """
    CREATE TRIGGER index_job_view_request_must_match
    BEFORE INSERT ON index_job_views
    WHEN NOT EXISTS (
        SELECT 1
        FROM index_jobs AS job,
             json_each(job.request_json, '$.views') AS requested
        WHERE job.job_id = NEW.job_id
            AND requested.key = NEW.view_type
            AND json_type(requested.value) = 'object'
            AND (
                SELECT COUNT(*) FROM json_each(requested.value)
            ) = 3
            AND json_type(requested.value, '$.profile_id') = 'text'
            AND json_extract(requested.value, '$.profile_id') = NEW.profile_id
            AND json_type(requested.value, '$.requested_mode') = 'text'
            AND json_extract(
                requested.value, '$.requested_mode'
            ) = NEW.requested_mode
            AND json_type(requested.value, '$.required') = CASE NEW.required
                WHEN 1 THEN 'true'
                ELSE 'false'
            END
    )
    BEGIN
        SELECT RAISE(ABORT, 'index job view does not match canonical request');
    END
    """,
    """
    CREATE TRIGGER ref_job_leases_reject_duplicate_inserts
    BEFORE INSERT ON ref_job_leases
    WHEN EXISTS (
        SELECT 1 FROM ref_job_leases AS existing
        WHERE (
                existing.repository_id = NEW.repository_id
                AND existing.ref_name = NEW.ref_name
            )
            OR (
                NEW.job_id IS NOT NULL
                AND existing.job_id = NEW.job_id
            )
    )
    BEGIN
        SELECT RAISE(ABORT, 'duplicate ref job lease insert is forbidden');
    END
    """,
    """
    CREATE TRIGGER ref_job_lease_insert_fencing
    BEFORE INSERT ON ref_job_leases
    WHEN
        (NEW.job_id IS NOT NULL AND NEW.fencing_token != 1)
        OR (NEW.job_id IS NULL AND NEW.fencing_token != 0)
        OR (
            NEW.job_id IS NOT NULL
            AND (SELECT status FROM index_jobs WHERE job_id = NEW.job_id) != 'queued'
        )
    BEGIN
        SELECT RAISE(ABORT, 'initial ref job lease fencing token is invalid');
    END
    """,
    """
    CREATE TRIGGER ref_job_lease_updates_are_fenced
    BEFORE UPDATE ON ref_job_leases
    WHEN NOT (
        NEW.repository_id IS OLD.repository_id
        AND NEW.ref_name IS OLD.ref_name
        AND NEW.updated_at_ms >= OLD.updated_at_ms
        AND (
            (
                OLD.job_id IS NOT NULL AND NEW.job_id IS NOT NULL
                AND NEW.fencing_token = OLD.fencing_token
                AND NEW.job_id IS OLD.job_id
                AND NEW.owner_id IS OLD.owner_id
                AND NEW.acquired_at_ms IS OLD.acquired_at_ms
                AND NEW.heartbeat_at_ms >= OLD.heartbeat_at_ms
                AND NEW.lease_expires_at_ms > OLD.lease_expires_at_ms
                AND OLD.lease_expires_at_ms > CAST(
                    (julianday('now') - 2440587.5) * 86400000 AS INTEGER
                )
                AND (
                    SELECT status FROM index_jobs WHERE job_id = NEW.job_id
                ) = 'running'
            )
            OR
            (
                OLD.job_id IS NOT NULL AND NEW.job_id IS NULL
                AND NEW.fencing_token = OLD.fencing_token
                AND (
                    SELECT status FROM index_jobs WHERE job_id = OLD.job_id
                ) != 'running'
            )
            OR
            (
                OLD.job_id IS NULL AND NEW.job_id IS NOT NULL
                AND NEW.fencing_token = OLD.fencing_token + 1
                AND (
                    SELECT status FROM index_jobs WHERE job_id = NEW.job_id
                ) = 'queued'
            )
            OR
            (
                OLD.job_id IS NOT NULL AND NEW.job_id IS NOT NULL
                AND NEW.fencing_token = OLD.fencing_token + 1
                AND OLD.lease_expires_at_ms <= CAST(
                    (julianday('now') - 2440587.5) * 86400000 AS INTEGER
                )
                AND (
                    SELECT status FROM index_jobs WHERE job_id = OLD.job_id
                ) != 'running'
                AND (
                    SELECT status FROM index_jobs WHERE job_id = NEW.job_id
                ) = 'queued'
            )
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'ref job lease fencing transition is invalid');
    END
    """,
    """
    CREATE TRIGGER ref_job_lease_slots_are_persistent
    BEFORE DELETE ON ref_job_leases
    BEGIN
        SELECT RAISE(ABORT, 'ref job lease slots are persistent');
    END
    """,
)

_MIGRATIONS: dict[int, tuple[str, ...]] = {1: _SCHEMA_V1, 2: _SCHEMA_V2}


class SQLiteCatalog:
    """Transactional SQLite catalog for immutable index snapshots."""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        if (
            isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or busy_timeout_ms < 0
        ):
            raise ValueError("busy_timeout_ms must be a non-negative integer")

        raw_path = str(path)
        if raw_path != ":memory:":
            resolved = Path(path).expanduser().resolve()
            resolved.parent.mkdir(parents=True, exist_ok=True)
            raw_path = str(resolved)
        self.path = raw_path
        self._connection = sqlite3.connect(
            raw_path,
            timeout=busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA recursive_triggers = ON")
            if self._connection.execute("PRAGMA recursive_triggers").fetchone()[0] != 1:
                raise CatalogError("SQLite recursive triggers could not be enabled")
            try:
                json1_available = self._connection.execute(
                    "SELECT json_valid('{}')"
                ).fetchone()[0]
            except sqlite3.DatabaseError as exc:
                raise CatalogError("SQLite JSON1 support is required") from exc
            if json1_available != 1:
                raise CatalogError("SQLite JSON1 support is required")
            self._connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms:d}")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._migrate()
        except BaseException:
            self._connection.close()
            raise

    def close(self) -> None:
        """Close the underlying database connection."""
        self._connection.close()

    def __enter__(self) -> SQLiteCatalog:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        """Return the latest successfully applied schema migration."""
        row = self._connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
        ).fetchone()
        return int(row["version"])

    @contextmanager
    def _transaction(self, *, immediate: bool = True) -> Iterator[None]:
        if self._connection.in_transaction:
            raise CatalogError("nested catalog transactions are not supported")
        self._connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield
            self._connection.commit()
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    def _migrate(self) -> None:
        with self._transaction():
            self._connection.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """)
            rows = self._connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
            applied = [int(row["version"]) for row in rows]
            if applied != list(range(1, len(applied) + 1)):
                raise CatalogError(f"non-contiguous schema migrations: {applied!r}")
            current_version = applied[-1] if applied else 0
            user_version = int(
                self._connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if current_version > LATEST_SCHEMA_VERSION:
                raise CatalogError(
                    "catalog schema is newer than this CodeNib version: "
                    f"{current_version} > {LATEST_SCHEMA_VERSION}"
                )
            if user_version > LATEST_SCHEMA_VERSION:
                raise CatalogError(
                    "catalog user_version is newer than this CodeNib version: "
                    f"{user_version} > {LATEST_SCHEMA_VERSION}"
                )
            if user_version != current_version:
                raise CatalogError(
                    "catalog user_version does not match schema_migrations: "
                    f"{user_version} != {current_version}"
                )

            for version in range(current_version + 1, LATEST_SCHEMA_VERSION + 1):
                statements = _MIGRATIONS.get(version)
                if statements is None:
                    raise CatalogError(f"missing catalog migration {version}")
                for statement in statements:
                    self._connection.execute(statement)
                if version == 1:
                    self._connection.execute(
                        """
                        INSERT INTO namespaces(namespace_id, name, created_at)
                        VALUES (?, ?, ?)
                        """,
                        (DEFAULT_NAMESPACE_ID, DEFAULT_NAMESPACE_NAME, _now()),
                    )
                self._connection.execute(
                    """
                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (?, ?)
                    """,
                    (version, _now()),
                )
                self._connection.execute(f"PRAGMA user_version = {version:d}")

    def _require_record(self, table: str, key: str, value: str) -> sqlite3.Row:
        allowed = {
            "namespaces": "namespace_id",
            "repositories": "repository_id",
            "source_revisions": "source_revision_id",
            "view_profiles": "profile_id",
            "objects": "digest",
            "view_generations": "view_generation_id",
            "snapshots": "snapshot_id",
            "index_jobs": "job_id",
        }
        if allowed.get(table) != key:
            raise AssertionError("unsafe catalog lookup")
        row = self._connection.execute(
            f"SELECT * FROM {table} WHERE {key} = ?", (value,)
        ).fetchone()
        if row is None:
            label = table.replace("_", " ").rstrip("s")
            raise CatalogNotFoundError(f"{label} not found: {value}")
        return row

    def _db_now_ms(self) -> int:
        row = self._connection.execute(f"SELECT {_DB_NOW_MS_SQL} AS now_ms").fetchone()
        return int(row["now_ms"])

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> IndexJobRecord:
        record = IndexJobRecord(
            job_id=row["job_id"],
            repository_id=row["repository_id"],
            source_revision_id=row["source_revision_id"],
            ref_name=row["ref_name"],
            idempotency_key=row["idempotency_key"],
            expected_ref_generation=int(row["expected_ref_generation"]),
            max_attempts=int(row["max_attempts"]),
            request_json=row["request_json"],
            request_digest=row["request_digest"],
            status=IndexJobStatus(row["status"]),
            cancel_requested=bool(row["cancel_requested"]),
            attempt_count=int(row["attempt_count"]),
            result_snapshot_id=row["result_snapshot_id"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            created_at_ms=int(row["created_at_ms"]),
            updated_at_ms=int(row["updated_at_ms"]),
            started_at_ms=(
                int(row["started_at_ms"]) if row["started_at_ms"] is not None else None
            ),
            finished_at_ms=(
                int(row["finished_at_ms"])
                if row["finished_at_ms"] is not None
                else None
            ),
        )
        if row["request_contract"] != record.request["contract"]:
            raise StorageIntegrityError(
                f"job request contract column is inconsistent: {record.job_id}"
            )
        return record

    @staticmethod
    def _job_view_from_row(row: sqlite3.Row) -> IndexJobViewRecord:
        return IndexJobViewRecord(
            job_id=row["job_id"],
            view_type=row["view_type"],
            profile_id=row["profile_id"],
            requested_mode=row["requested_mode"],
            required=bool(row["required"]),
        )

    @staticmethod
    def _lease_from_row(row: sqlite3.Row) -> RefJobLease:
        return RefJobLease(
            repository_id=row["repository_id"],
            ref_name=row["ref_name"],
            job_id=row["job_id"],
            owner_id=row["owner_id"],
            fencing_token=int(row["fencing_token"]),
            acquired_at_ms=int(row["acquired_at_ms"]),
            heartbeat_at_ms=int(row["heartbeat_at_ms"]),
            lease_expires_at_ms=int(row["lease_expires_at_ms"]),
        )

    def _job_views(self, job: IndexJobRecord) -> tuple[IndexJobViewRecord, ...]:
        rows = self._connection.execute(
            """
            SELECT job_id, view_type, profile_id, requested_mode, required
            FROM index_job_views WHERE job_id = ? ORDER BY view_type
            """,
            (job.job_id,),
        ).fetchall()
        actual = tuple(self._job_view_from_row(row) for row in rows)
        expected = IndexJobRequest(
            repository_id=job.repository_id,
            source_revision_id=job.source_revision_id,
            ref_name=job.ref_name,
            idempotency_key=job.idempotency_key,
            expected_ref_generation=job.expected_ref_generation,
            max_attempts=job.max_attempts,
            request_json=job.request_json,
        ).view_requests
        if actual != expected:
            raise StorageIntegrityError(
                f"job view rows do not match canonical request: {job.job_id}"
            )
        return actual

    def create_job(
        self,
        repository_id: str,
        source_revision_id: str,
        idempotency_key: str,
        request: Mapping[str, Any],
        *,
        ref_name: str = "main",
        expected_ref_generation: int = 0,
        max_attempts: int = 3,
    ) -> IndexJobRecord:
        """Create or return one canonical idempotent index-job request."""
        job_request = IndexJobRequest.create(
            repository_id,
            source_revision_id,
            idempotency_key,
            request,
            ref_name=ref_name,
            expected_ref_generation=expected_ref_generation,
            max_attempts=max_attempts,
        )
        with self._transaction():
            self._require_record(
                "repositories", "repository_id", job_request.repository_id
            )
            row = self._connection.execute(
                """
                SELECT * FROM index_jobs
                WHERE repository_id = ? AND idempotency_key = ?
                """,
                (job_request.repository_id, job_request.idempotency_key),
            ).fetchone()
            if row is not None:
                job = self._job_from_row(row)
                if (
                    job.job_id != job_request.job_id
                    or job.request_digest != job_request.request_digest
                ):
                    raise CatalogConflictError(
                        "idempotency key is already bound to another "
                        "index-job request"
                    )
                self._job_views(job)
                return job

            source = self._require_record(
                "source_revisions",
                "source_revision_id",
                job_request.source_revision_id,
            )
            if source["repository_id"] != job_request.repository_id:
                raise CatalogValidationError(
                    "index job source revision belongs to another repository"
                )
            for view in job_request.view_requests:
                profile = self._require_record(
                    "view_profiles", "profile_id", view.profile_id
                )
                if profile["view_type"] != view.view_type:
                    raise CatalogValidationError(
                        f"job view does not match its profile: {view.view_type}"
                    )

            now_ms = self._db_now_ms()
            self._connection.execute(
                """
                    INSERT INTO index_jobs(
                        job_id, repository_id, source_revision_id, ref_name,
                        idempotency_key, expected_ref_generation, max_attempts,
                        request_contract, request_json, request_digest, status,
                        cancel_requested, attempt_count, result_snapshot_id,
                        error_code, error_message, created_at_ms, updated_at_ms,
                        started_at_ms, finished_at_ms
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, 0,
                        NULL, NULL, NULL, ?, ?, NULL, NULL
                    )
                    """,
                (
                    job_request.job_id,
                    job_request.repository_id,
                    job_request.source_revision_id,
                    job_request.ref_name,
                    job_request.idempotency_key,
                    job_request.expected_ref_generation,
                    job_request.max_attempts,
                    job_request.contract,
                    job_request.request_json,
                    job_request.request_digest,
                    now_ms,
                    now_ms,
                ),
            )
            self._connection.executemany(
                """
                    INSERT INTO index_job_views(
                        job_id, view_type, profile_id, requested_mode, required
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                [
                    (
                        view.job_id,
                        view.view_type,
                        view.profile_id,
                        view.requested_mode.value,
                        int(view.required),
                    )
                    for view in job_request.view_requests
                ],
            )
            row = self._require_record("index_jobs", "job_id", job_request.job_id)
            job = self._job_from_row(row)
            self._job_views(job)
            return job

    def get_job(self, job_id: str) -> IndexJobRecord:
        """Return one persisted index job after validating its canonical request."""
        normalized = _bounded_text(job_id, "job ID", max_length=80)
        with self._transaction(immediate=False):
            job = self._job_from_row(
                self._require_record("index_jobs", "job_id", normalized)
            )
            self._job_views(job)
            return job

    def get_job_views(self, job_id: str) -> tuple[IndexJobViewRecord, ...]:
        """Return the immutable requested view mapping for one index job."""
        normalized = _bounded_text(job_id, "job ID", max_length=80)
        with self._transaction(immediate=False):
            job = self._job_from_row(
                self._require_record("index_jobs", "job_id", normalized)
            )
            return self._job_views(job)

    def _retire_expired_holder(self, job_id: str, now_ms: int) -> IndexJobStatus:
        job = self._job_from_row(self._require_record("index_jobs", "job_id", job_id))
        self._job_views(job)
        if job.status is not IndexJobStatus.RUNNING:
            return job.status
        if job.cancel_requested:
            status = IndexJobStatus.CANCELLED
            error_code = "cancelled"
            error_message = "lease expired after cancellation was requested"
            finished_at_ms: int | None = now_ms
        elif job.attempt_count >= job.max_attempts:
            status = IndexJobStatus.FAILED
            error_code = "attempts_exhausted"
            error_message = "lease expired on the final permitted attempt"
            finished_at_ms = now_ms
        else:
            status = IndexJobStatus.QUEUED
            error_code = "lease_expired"
            error_message = "previous worker lease expired; job was requeued"
            finished_at_ms = None
        cursor = self._connection.execute(
            """
            UPDATE index_jobs
            SET status = ?, error_code = ?, error_message = ?,
                finished_at_ms = ?, updated_at_ms = ?
            WHERE job_id = ? AND status = 'running'
            """,
            (
                status.value,
                error_code,
                error_message,
                finished_at_ms,
                now_ms,
                job.job_id,
            ),
        )
        if cursor.rowcount != 1:
            raise CatalogConflictError("expired index job changed during takeover")
        return status

    def _release_job_slot(
        self,
        job: IndexJobRecord,
        *,
        fencing_token: int,
        now_ms: int,
    ) -> None:
        cursor = self._connection.execute(
            """
            UPDATE ref_job_leases
            SET job_id = NULL, owner_id = NULL,
                acquired_at_ms = NULL, heartbeat_at_ms = NULL,
                lease_expires_at_ms = NULL, updated_at_ms = ?
            WHERE repository_id = ? AND ref_name = ?
                AND job_id = ? AND fencing_token = ?
            """,
            (
                now_ms,
                job.repository_id,
                job.ref_name,
                job.job_id,
                fencing_token,
            ),
        )
        if cursor.rowcount != 1:
            raise CatalogConflictError("index job lease changed before release")

    def acquire_job_lease(
        self,
        job_id: str,
        *,
        owner_id: str,
        lease_duration_ms: int,
    ) -> RefJobLease:
        """Acquire or take over the single fenced publisher slot for a ref."""
        normalized_job = _bounded_text(job_id, "job ID", max_length=80)
        owner = _bounded_text(owner_id, "owner ID", max_length=256)
        duration = _positive_integer(lease_duration_ms, "lease duration")
        if duration > 2_147_483_647:
            raise CatalogValidationError("lease duration is too large")

        lease: RefJobLease | None = None
        blocked_reason: str | None = None
        with self._transaction():
            job = self._job_from_row(
                self._require_record("index_jobs", "job_id", normalized_job)
            )
            self._job_views(job)
            if job.status not in {IndexJobStatus.QUEUED, IndexJobStatus.RUNNING}:
                raise CatalogConflictError(
                    f"terminal index job cannot be acquired: {job.status.value}"
                )
            if job.status is IndexJobStatus.QUEUED and job.cancel_requested:
                raise StorageIntegrityError("queued index job has a cancellation flag")
            if job.status is IndexJobStatus.QUEUED and (
                job.attempt_count >= job.max_attempts
            ):
                raise CatalogConflictError("index job has exhausted its attempts")

            now_ms = self._db_now_ms()
            expires_at_ms = now_ms + duration
            slot = self._connection.execute(
                """
                SELECT * FROM ref_job_leases
                WHERE repository_id = ? AND ref_name = ?
                """,
                (job.repository_id, job.ref_name),
            ).fetchone()
            if (
                slot is not None
                and slot["job_id"] is not None
                and int(slot["lease_expires_at_ms"]) > now_ms
            ):
                if (
                    slot["job_id"] == job.job_id
                    and slot["owner_id"] == owner
                    and job.status is IndexJobStatus.RUNNING
                ):
                    return self._lease_from_row(slot)
                raise CatalogConflictError(
                    "repository ref already has an active index-job lease"
                )

            if slot is None:
                token = 1
                cursor = self._connection.execute(
                    """
                    INSERT INTO ref_job_leases(
                        repository_id, ref_name, job_id, owner_id, fencing_token,
                        acquired_at_ms, heartbeat_at_ms, lease_expires_at_ms,
                        updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job.repository_id,
                        job.ref_name,
                        job.job_id,
                        owner,
                        token,
                        now_ms,
                        now_ms,
                        expires_at_ms,
                        now_ms,
                    ),
                )
                if cursor.rowcount != 1:
                    raise CatalogConflictError("ref job lease slot was not created")
            elif slot["job_id"] is None:
                token = int(slot["fencing_token"]) + 1
                cursor = self._connection.execute(
                    """
                    UPDATE ref_job_leases
                    SET job_id = ?, owner_id = ?, fencing_token = ?,
                        acquired_at_ms = ?, heartbeat_at_ms = ?,
                        lease_expires_at_ms = ?, updated_at_ms = ?
                    WHERE repository_id = ? AND ref_name = ? AND job_id IS NULL
                    """,
                    (
                        job.job_id,
                        owner,
                        token,
                        now_ms,
                        now_ms,
                        expires_at_ms,
                        now_ms,
                        job.repository_id,
                        job.ref_name,
                    ),
                )
                if cursor.rowcount != 1:
                    raise CatalogConflictError("empty ref job slot changed")
            else:
                old_job_id = str(slot["job_id"])
                retired_status = self._retire_expired_holder(old_job_id, now_ms)
                token = int(slot["fencing_token"]) + 1
                if old_job_id == job.job_id and retired_status in {
                    IndexJobStatus.FAILED,
                    IndexJobStatus.CANCELLED,
                }:
                    current = self._job_from_row(
                        self._require_record("index_jobs", "job_id", job.job_id)
                    )
                    self._release_job_slot(
                        current,
                        fencing_token=int(slot["fencing_token"]),
                        now_ms=now_ms,
                    )
                    blocked_reason = (
                        "expired lease made the index job terminal: "
                        f"{retired_status.value}"
                    )
                else:
                    cursor = self._connection.execute(
                        """
                        UPDATE ref_job_leases
                        SET job_id = ?, owner_id = ?, fencing_token = ?,
                            acquired_at_ms = ?, heartbeat_at_ms = ?,
                            lease_expires_at_ms = ?, updated_at_ms = ?
                        WHERE repository_id = ? AND ref_name = ?
                            AND job_id = ? AND fencing_token = ?
                            AND lease_expires_at_ms <= ?
                        """,
                        (
                            job.job_id,
                            owner,
                            token,
                            now_ms,
                            now_ms,
                            expires_at_ms,
                            now_ms,
                            job.repository_id,
                            job.ref_name,
                            old_job_id,
                            int(slot["fencing_token"]),
                            now_ms,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise CatalogConflictError(
                            "expired ref job slot changed during takeover"
                        )

            if blocked_reason is None:
                job = self._job_from_row(
                    self._require_record("index_jobs", "job_id", job.job_id)
                )
                if job.status is not IndexJobStatus.QUEUED:
                    raise StorageIntegrityError(
                        "an acquirable index job must be queued before its attempt"
                    )
                cursor = self._connection.execute(
                    """
                    UPDATE index_jobs
                    SET status = 'running', attempt_count = attempt_count + 1,
                        started_at_ms = COALESCE(started_at_ms, ?),
                        updated_at_ms = ?, error_code = NULL, error_message = NULL
                    WHERE job_id = ? AND status = 'queued'
                        AND attempt_count < max_attempts
                    """,
                    (now_ms, now_ms, job.job_id),
                )
                if cursor.rowcount != 1:
                    raise CatalogConflictError(
                        "index job could not start a new attempt"
                    )
                lease_row = self._connection.execute(
                    """
                    SELECT * FROM ref_job_leases
                    WHERE repository_id = ? AND ref_name = ?
                    """,
                    (job.repository_id, job.ref_name),
                ).fetchone()
                if lease_row is None:
                    raise StorageIntegrityError("acquired index job lease disappeared")
                lease = self._lease_from_row(lease_row)

        if blocked_reason is not None:
            raise CatalogConflictError(blocked_reason)
        if lease is None:
            raise AssertionError("successful lease acquisition produced no lease")
        return lease

    def renew_job_lease(
        self,
        job_id: str,
        *,
        owner_id: str,
        fencing_token: int,
        lease_duration_ms: int,
    ) -> RefJobLease:
        """Extend an unexpired lease without changing its fencing token."""
        normalized_job = _bounded_text(job_id, "job ID", max_length=80)
        owner = _bounded_text(owner_id, "owner ID", max_length=256)
        token = _positive_integer(fencing_token, "fencing token")
        duration = _positive_integer(lease_duration_ms, "lease duration")
        if duration > 2_147_483_647:
            raise CatalogValidationError("lease duration is too large")

        with self._transaction():
            job = self._job_from_row(
                self._require_record("index_jobs", "job_id", normalized_job)
            )
            self._job_views(job)
            if job.status is not IndexJobStatus.RUNNING:
                raise CatalogConflictError(
                    "index job lease can only be renewed while running"
                )
            cursor = self._connection.execute(
                f"""
                UPDATE ref_job_leases
                SET heartbeat_at_ms = MAX(heartbeat_at_ms, {_DB_NOW_MS_SQL}),
                    lease_expires_at_ms = MAX(
                        lease_expires_at_ms + 1, {_DB_NOW_MS_SQL} + ?
                    ),
                    updated_at_ms = MAX(updated_at_ms, {_DB_NOW_MS_SQL})
                WHERE repository_id = ? AND ref_name = ? AND job_id = ?
                    AND owner_id = ? AND fencing_token = ?
                    AND lease_expires_at_ms > {_DB_NOW_MS_SQL}
                """,
                (
                    duration,
                    job.repository_id,
                    job.ref_name,
                    job.job_id,
                    owner,
                    token,
                ),
            )
            if cursor.rowcount != 1:
                raise CatalogConflictError("index job lease expired before renewal")
            renewed = self._connection.execute(
                """
                SELECT * FROM ref_job_leases
                WHERE repository_id = ? AND ref_name = ? AND job_id = ?
                    AND owner_id = ? AND fencing_token = ?
                """,
                (job.repository_id, job.ref_name, job.job_id, owner, token),
            ).fetchone()
            if renewed is None:
                raise StorageIntegrityError("renewed index job lease disappeared")
            return self._lease_from_row(renewed)

    def request_job_cancel(self, job_id: str) -> IndexJobRecord:
        """Cancel a queued job or request cooperative cancellation while running."""
        normalized = _bounded_text(job_id, "job ID", max_length=80)
        with self._transaction():
            job = self._job_from_row(
                self._require_record("index_jobs", "job_id", normalized)
            )
            self._job_views(job)
            if job.status in {
                IndexJobStatus.SUCCEEDED,
                IndexJobStatus.FAILED,
                IndexJobStatus.CANCELLED,
            }:
                return job
            if job.cancel_requested:
                return job
            now_ms = self._db_now_ms()
            if job.status is IndexJobStatus.QUEUED:
                active_slot = self._connection.execute(
                    "SELECT 1 FROM ref_job_leases WHERE job_id = ?",
                    (job.job_id,),
                ).fetchone()
                if active_slot is not None:
                    raise StorageIntegrityError(
                        "queued index job holds an active lease"
                    )
                self._connection.execute(
                    """
                    UPDATE index_jobs
                    SET status = 'cancelled', cancel_requested = 1,
                        error_code = 'cancelled', error_message = NULL,
                        finished_at_ms = ?, updated_at_ms = ?
                    WHERE job_id = ? AND status = 'queued'
                    """,
                    (now_ms, now_ms, job.job_id),
                )
            else:
                self._connection.execute(
                    """
                    UPDATE index_jobs
                    SET cancel_requested = 1, updated_at_ms = ?
                    WHERE job_id = ? AND status = 'running'
                    """,
                    (now_ms, job.job_id),
                )
            return self._job_from_row(
                self._require_record("index_jobs", "job_id", job.job_id)
            )

    def finish_job_attempt(
        self,
        job_id: str,
        *,
        owner_id: str,
        fencing_token: int,
        outcome: IndexJobCompletion,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> IndexJobRecord:
        """Finish a fenced M1 attempt as failed, cancelled, or requeued."""
        normalized_job = _bounded_text(job_id, "job ID", max_length=80)
        owner = _bounded_text(owner_id, "owner ID", max_length=256)
        token = _positive_integer(fencing_token, "fencing token")
        try:
            normalized_outcome = IndexJobCompletion(outcome)
        except ValueError as exc:
            raise CatalogValidationError(
                f"unsupported M1 job completion: {outcome}"
            ) from exc
        code = _optional_bounded_text(error_code, "job error code", max_length=128)
        message = _optional_bounded_text(
            error_message, "job error message", max_length=4_096
        )
        if message is not None and code is None:
            raise CatalogValidationError("job error message requires an error code")
        if (
            normalized_outcome
            in {
                IndexJobCompletion.REQUEUE,
                IndexJobCompletion.FAILED,
            }
            and code is None
        ):
            raise CatalogValidationError(
                f"{normalized_outcome.value} completion requires an error code"
            )
        if normalized_outcome is IndexJobCompletion.CANCELLED and code is None:
            code = "cancelled"

        with self._transaction():
            job = self._job_from_row(
                self._require_record("index_jobs", "job_id", normalized_job)
            )
            self._job_views(job)
            if job.status is not IndexJobStatus.RUNNING:
                raise CatalogConflictError(
                    "index job mutation requires its current unexpired fenced lease"
                )
            if (
                normalized_outcome is IndexJobCompletion.CANCELLED
                and not job.cancel_requested
            ):
                raise CatalogConflictError(
                    "running index job must receive cancellation before acknowledging it"
                )
            if (
                normalized_outcome is IndexJobCompletion.REQUEUE
                and job.cancel_requested
            ):
                raise CatalogConflictError(
                    "cancelled index job attempt cannot be requeued"
                )
            if (
                normalized_outcome is IndexJobCompletion.REQUEUE
                and job.attempt_count >= job.max_attempts
            ):
                raise CatalogConflictError(
                    "final index job attempt must fail instead of requeueing"
                )

            if normalized_outcome is IndexJobCompletion.REQUEUE:
                status = IndexJobStatus.QUEUED
                is_terminal = 0
            elif normalized_outcome is IndexJobCompletion.FAILED:
                status = IndexJobStatus.FAILED
                is_terminal = 1
            else:
                status = IndexJobStatus.CANCELLED
                is_terminal = 1
            cursor = self._connection.execute(
                f"""
                UPDATE index_jobs
                SET status = ?, error_code = ?, error_message = ?,
                    finished_at_ms = CASE ?
                        WHEN 1 THEN {_DB_NOW_MS_SQL}
                        ELSE NULL
                    END,
                    updated_at_ms = MAX(updated_at_ms, {_DB_NOW_MS_SQL})
                WHERE job_id = ? AND status = 'running'
                    AND EXISTS (
                        SELECT 1 FROM ref_job_leases AS lease
                        WHERE lease.repository_id = index_jobs.repository_id
                            AND lease.ref_name = index_jobs.ref_name
                            AND lease.job_id = index_jobs.job_id
                            AND lease.owner_id = ?
                            AND lease.fencing_token = ?
                            AND lease.lease_expires_at_ms > {_DB_NOW_MS_SQL}
                    )
                """,
                (
                    status.value,
                    code,
                    message,
                    is_terminal,
                    job.job_id,
                    owner,
                    token,
                ),
            )
            if cursor.rowcount != 1:
                raise CatalogConflictError(
                    "index job mutation requires its current unexpired fenced lease"
                )
            self._release_job_slot(
                job,
                fencing_token=token,
                now_ms=self._db_now_ms(),
            )
            return self._job_from_row(
                self._require_record("index_jobs", "job_id", job.job_id)
            )

    def create_namespace(self, name: str) -> str:
        """Create an idempotent logical namespace and return its stable ID."""
        normalized = _required_text(name, "namespace name")
        if normalized == DEFAULT_NAMESPACE_NAME:
            namespace_id = DEFAULT_NAMESPACE_ID
        else:
            namespace_id = content_id("ns", {"name": normalized})
        with self._transaction():
            self._connection.execute(
                """
                INSERT OR IGNORE INTO namespaces(namespace_id, name, created_at)
                VALUES (?, ?, ?)
                """,
                (namespace_id, normalized, _now()),
            )
            row = self._connection.execute(
                "SELECT namespace_id FROM namespaces WHERE name = ?", (normalized,)
            ).fetchone()
            if row is None or row["namespace_id"] != namespace_id:
                raise CatalogConflictError(
                    f"namespace identity conflicts with existing name: {normalized}"
                )
        return namespace_id

    def create_repository(
        self,
        repository_key: str,
        *,
        namespace_id: str = DEFAULT_NAMESPACE_ID,
    ) -> str:
        """Create an idempotent repository identity in a non-null namespace."""
        key = _required_text(repository_key, "repository key")
        namespace = _required_text(namespace_id, "namespace ID")
        repository_id = content_id(
            "repo", {"namespace_id": namespace, "repository_key": key}
        )
        with self._transaction():
            self._require_record("namespaces", "namespace_id", namespace)
            self._connection.execute(
                """
                INSERT OR IGNORE INTO repositories(
                    repository_id, namespace_id, repository_key, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (repository_id, namespace, key, _now()),
            )
            row = self._connection.execute(
                """
                SELECT repository_id FROM repositories
                WHERE namespace_id = ? AND repository_key = ?
                """,
                (namespace, key),
            ).fetchone()
            if row is None or row["repository_id"] != repository_id:
                raise CatalogConflictError(
                    f"repository identity conflicts with existing key: {key}"
                )
        return repository_id

    def create_source_revision(
        self,
        repository_id: str,
        *,
        commit_sha: str | None = None,
        tree_sha: str | None = None,
        dirty: bool = False,
        source_fingerprint: str | None = None,
    ) -> str:
        """Create a stable clean or dirty source identity.

        Clean identities require both a commit and tree and use the tree as the
        canonical source fingerprint.  Dirty identities require an explicit
        content fingerprint, may bind a base commit, and never include a base
        tree as a second identity for the same dirty contents.
        """
        repository = _required_text(repository_id, "repository ID")
        if dirty:
            revision = SourceRevision(
                repository_id=repository,
                source_kind="dirty",
                commit_sha=commit_sha,
                tree_sha=tree_sha,
                source_fingerprint=source_fingerprint or "",
            )
        else:
            revision = SourceRevision.clean(
                repository,
                commit_sha=commit_sha or "",
                tree_sha=tree_sha or "",
            )

        identity = {
            "repository_id": revision.repository_id,
            "source_kind": revision.source_kind,
            "commit_sha": revision.commit_sha,
            "tree_sha": revision.tree_sha,
            "source_fingerprint": revision.source_fingerprint,
        }
        source_revision_id = revision.source_revision_id
        if source_revision_id != content_id("src", identity):
            raise AssertionError("source revision identity implementations diverged")
        identity_digest = source_revision_id.removeprefix("src_")
        with self._transaction():
            self._require_record("repositories", "repository_id", repository)
            self._connection.execute(
                """
                INSERT OR IGNORE INTO source_revisions(
                    source_revision_id, repository_id, source_kind, commit_sha,
                    tree_sha, source_fingerprint, identity_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_revision_id,
                    repository,
                    revision.source_kind,
                    revision.commit_sha,
                    revision.tree_sha,
                    revision.source_fingerprint,
                    identity_digest,
                    _now(),
                ),
            )
            row = self._connection.execute(
                """
                SELECT source_revision_id FROM source_revisions
                WHERE repository_id = ? AND identity_digest = ?
                """,
                (repository, identity_digest),
            ).fetchone()
            if row is None or row["source_revision_id"] != source_revision_id:
                raise CatalogConflictError("source revision identity conflict")
        return source_revision_id

    def create_view_profile(
        self,
        view_type: str,
        config: Mapping[str, Any] | None = None,
        *,
        name: str = "default",
    ) -> str:
        """Create an idempotent, view-specific canonical JSON profile."""
        normalized_view_type = _required_text(view_type, "view type")
        normalized_name = _required_text(name, "profile name")
        config_json = canonical_json(config or {})
        identity = {
            "view_type": normalized_view_type,
            "name": normalized_name,
            "config": json.loads(config_json),
        }
        profile_id = content_id("profile", identity)
        profile_digest = profile_id.removeprefix("profile_")
        with self._transaction():
            self._connection.execute(
                """
                INSERT OR IGNORE INTO view_profiles(
                    profile_id, view_type, name, config_json, profile_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    profile_id,
                    normalized_view_type,
                    normalized_name,
                    config_json,
                    profile_digest,
                    _now(),
                ),
            )
            row = self._connection.execute(
                "SELECT profile_id FROM view_profiles WHERE profile_digest = ?",
                (profile_digest,),
            ).fetchone()
            if row is None or row["profile_id"] != profile_id:
                raise CatalogConflictError("view profile identity conflict")
        return profile_id

    def register_object(
        self,
        digest: str,
        *,
        storage_key: str,
        byte_size: int,
        media_type: str = "application/octet-stream",
    ) -> str:
        """Register metadata for an already-durable, independently verified object.

        This low-level catalog has no object-store handle.  Publication
        coordinators must call ``ObjectStore.verify`` and match its digest,
        size, and storage key before registering or publishing the receipt.
        """
        record = ObjectRecord(
            digest=digest,
            storage_key=storage_key,
            byte_size=byte_size,
            media_type=media_type,
        )
        normalized_digest = record.digest
        normalized_storage_key = record.storage_key
        normalized_media_type = record.media_type

        with self._transaction():
            row = self._connection.execute(
                "SELECT * FROM objects WHERE digest = ?", (normalized_digest,)
            ).fetchone()
            if row is None:
                self._connection.execute(
                    """
                    INSERT INTO objects(
                        digest, storage_key, byte_size, media_type, created_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_digest,
                        normalized_storage_key,
                        record.byte_size,
                        normalized_media_type,
                        _now(),
                    ),
                )
            elif (
                row["storage_key"] != normalized_storage_key
                or row["byte_size"] != record.byte_size
                or row["media_type"] != normalized_media_type
            ):
                raise CatalogConflictError(
                    f"object metadata is immutable: {normalized_digest}"
                )
        return normalized_digest

    def stage_view_generation(
        self,
        repository_id: str,
        source_revision_id: str,
        profile_id: str,
        view_type: str,
        object_digest: str,
        *,
        schema_version: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        """Stage an immutable view generation backed by a registered object."""
        repository = _required_text(repository_id, "repository ID")
        source = _required_text(source_revision_id, "source revision ID")
        profile = _required_text(profile_id, "profile ID")
        normalized_view_type = _required_text(view_type, "view type")
        digest = normalize_digest(object_digest)
        normalized_schema_version = _required_text(
            schema_version, "view schema version"
        )
        metadata_json = canonical_json(metadata or {})
        identity = {
            "repository_id": repository,
            "source_revision_id": source,
            "profile_id": profile,
            "view_type": normalized_view_type,
            "object_digest": digest,
            "schema_version": normalized_schema_version,
            "metadata": json.loads(metadata_json),
        }
        view_generation_id = content_id("view", identity)

        with self._transaction():
            self._require_record("repositories", "repository_id", repository)
            source_row = self._require_record(
                "source_revisions", "source_revision_id", source
            )
            if source_row["repository_id"] != repository:
                raise CatalogValidationError(
                    "view source revision belongs to another repository"
                )
            profile_row = self._require_record("view_profiles", "profile_id", profile)
            if profile_row["view_type"] != normalized_view_type:
                raise CatalogValidationError(
                    "view type does not match the view profile"
                )
            self._require_record("objects", "digest", digest)
            self._connection.execute(
                """
                INSERT OR IGNORE INTO view_generations(
                    view_generation_id, repository_id, source_revision_id,
                    profile_id, view_type, object_digest, schema_version,
                    metadata_json, status, created_at, ready_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'staged', ?, NULL)
                """,
                (
                    view_generation_id,
                    repository,
                    source,
                    profile,
                    normalized_view_type,
                    digest,
                    normalized_schema_version,
                    metadata_json,
                    _now(),
                ),
            )
            row = self._require_record(
                "view_generations", "view_generation_id", view_generation_id
            )
            expected = (
                repository,
                source,
                profile,
                normalized_view_type,
                digest,
                normalized_schema_version,
                metadata_json,
            )
            actual = tuple(
                row[key]
                for key in (
                    "repository_id",
                    "source_revision_id",
                    "profile_id",
                    "view_type",
                    "object_digest",
                    "schema_version",
                    "metadata_json",
                )
            )
            if actual != expected:
                raise CatalogConflictError("view generation identity conflict")
        return view_generation_id

    def publish_snapshot(
        self,
        repository_id: str,
        source_revision_id: str,
        view_generation_ids: Sequence[str],
        *,
        ref_name: str = "main",
        expected_generation: int = 0,
    ) -> dict[str, Any]:
        """Publish the desired snapshot and advance a ref when necessary.

        A missing ref has generation zero.  A retry whose ref already targets
        the fully validated desired snapshot is idempotent even when it carries
        the generation expected before the first publication.  On any
        validation or CAS failure, the snapshot insertion and every
        staged-to-ready transition roll back.
        """
        repository = _required_text(repository_id, "repository ID")
        source = _required_text(source_revision_id, "source revision ID")
        normalized_ref = _required_text(ref_name, "ref name")
        if isinstance(expected_generation, bool) or not isinstance(
            expected_generation, int
        ):
            raise CatalogValidationError("expected generation must be an integer")
        if expected_generation < 0:
            raise CatalogValidationError("expected generation must not be negative")
        if isinstance(view_generation_ids, (str, bytes)):
            raise CatalogValidationError("view generation IDs must be a sequence")
        view_ids = [
            _required_text(value, "view generation ID") for value in view_generation_ids
        ]
        if not view_ids:
            raise CatalogValidationError("a snapshot requires at least one view")
        if len(set(view_ids)) != len(view_ids):
            raise CatalogValidationError("duplicate view generation IDs")

        with self._transaction():
            self._require_record("repositories", "repository_id", repository)
            source_row = self._require_record(
                "source_revisions", "source_revision_id", source
            )
            if source_row["repository_id"] != repository:
                raise CatalogValidationError(
                    "snapshot source revision belongs to another repository"
                )

            placeholders = ",".join("?" for _ in view_ids)
            rows = self._connection.execute(
                f"""
                SELECT * FROM view_generations
                WHERE view_generation_id IN ({placeholders})
                """,
                view_ids,
            ).fetchall()
            if len(rows) != len(view_ids):
                found = {row["view_generation_id"] for row in rows}
                missing = sorted(set(view_ids) - found)
                raise CatalogNotFoundError(
                    f"view generation not found: {', '.join(missing)}"
                )

            view_types: set[str] = set()
            for row in rows:
                if row["repository_id"] != repository:
                    raise CatalogValidationError(
                        "snapshot view belongs to another repository"
                    )
                if row["source_revision_id"] != source:
                    raise CatalogValidationError(
                        "all snapshot views must share the snapshot source identity"
                    )
                if row["view_type"] in view_types:
                    raise CatalogValidationError(
                        f"snapshot has duplicate view type: {row['view_type']}"
                    )
                view_types.add(row["view_type"])

                self._validate_view_generation_input(row)

            members = sorted(
                (row["view_type"], row["view_generation_id"]) for row in rows
            )
            snapshot_identity = {
                "repository_id": repository,
                "source_revision_id": source,
                "views": members,
            }
            snapshot_id = content_id("snapshot", snapshot_identity)
            content_digest = snapshot_id.removeprefix("snapshot_")

            current_ref = self._connection.execute(
                """
                SELECT snapshot_id, generation, updated_at FROM refs
                WHERE repository_id = ? AND ref_name = ?
                """,
                (repository, normalized_ref),
            ).fetchone()
            current_generation = (
                int(current_ref["generation"]) if current_ref is not None else 0
            )
            if current_ref is not None and (
                current_generation < 1
                or not isinstance(current_ref["updated_at"], str)
                or not current_ref["updated_at"].strip()
            ):
                raise CatalogConflictError(
                    f"ref {normalized_ref!r} has invalid publication metadata"
                )

            existing_snapshot = self._connection.execute(
                "SELECT * FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
            if current_ref is not None and current_ref["snapshot_id"] == snapshot_id:
                self._validate_ready_snapshot(
                    existing_snapshot,
                    repository_id=repository,
                    source_revision_id=source,
                    content_digest=content_digest,
                    members=members,
                    view_rows=rows,
                )
                result = {
                    "snapshot_id": snapshot_id,
                    "repository_id": repository,
                    "ref_name": normalized_ref,
                    "generation": current_generation,
                    "updated_at": current_ref["updated_at"],
                    "changed": False,
                }
                return result

            if current_generation != expected_generation:
                raise CatalogConflictError(
                    f"ref {normalized_ref!r} generation is {current_generation}; "
                    f"expected {expected_generation}"
                )

            published_at = _now()
            if existing_snapshot is None:
                self._connection.execute(
                    """
                    INSERT INTO snapshots(
                        snapshot_id, repository_id, source_revision_id,
                        content_digest, status, published_at
                    ) VALUES (?, ?, ?, ?, 'building', NULL)
                    """,
                    (snapshot_id, repository, source, content_digest),
                )
                for view_type, view_generation_id in members:
                    self._connection.execute(
                        """
                        INSERT INTO snapshot_views(
                            snapshot_id, view_type, view_generation_id
                        ) VALUES (?, ?, ?)
                        """,
                        (snapshot_id, view_type, view_generation_id),
                    )
                    self._connection.execute(
                        """
                        UPDATE view_generations
                        SET status = 'ready', ready_at = ?
                        WHERE view_generation_id = ? AND status = 'staged'
                        """,
                        (published_at, view_generation_id),
                    )
                seal = self._connection.execute(
                    """
                    UPDATE snapshots
                    SET status = 'ready', published_at = ?
                    WHERE snapshot_id = ? AND status = 'building'
                    """,
                    (published_at, snapshot_id),
                )
                if seal.rowcount != 1:
                    raise CatalogConflictError("snapshot could not be sealed")
            else:
                self._validate_ready_snapshot(
                    existing_snapshot,
                    repository_id=repository,
                    source_revision_id=source,
                    content_digest=content_digest,
                    members=members,
                    view_rows=rows,
                )

            next_generation = current_generation + 1
            if current_ref is None:
                self._connection.execute(
                    """
                    INSERT INTO refs(
                        repository_id, ref_name, snapshot_id, generation, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        repository,
                        normalized_ref,
                        snapshot_id,
                        next_generation,
                        published_at,
                    ),
                )
            else:
                cursor = self._connection.execute(
                    """
                    UPDATE refs
                    SET snapshot_id = ?, generation = ?, updated_at = ?
                    WHERE repository_id = ? AND ref_name = ? AND generation = ?
                    """,
                    (
                        snapshot_id,
                        next_generation,
                        published_at,
                        repository,
                        normalized_ref,
                        expected_generation,
                    ),
                )
                if cursor.rowcount != 1:
                    raise CatalogConflictError(
                        f"ref {normalized_ref!r} changed during publication"
                    )

        return {
            "snapshot_id": snapshot_id,
            "repository_id": repository,
            "ref_name": normalized_ref,
            "generation": next_generation,
            "updated_at": published_at,
            "changed": True,
        }

    def _validate_view_generation_input(self, row: sqlite3.Row) -> None:
        """Validate the immutable profile, object, and generation identities."""
        profile_row = self._require_record(
            "view_profiles", "profile_id", row["profile_id"]
        )
        if profile_row["view_type"] != row["view_type"]:
            raise CatalogValidationError("view type does not match the view profile")
        try:
            profile_config = json.loads(profile_row["config_json"])
            if not isinstance(profile_config, dict):
                raise CatalogValidationError(
                    "view profile config must be a JSON object"
                )
            canonical_profile_config = canonical_json(profile_config)
        except (TypeError, json.JSONDecodeError, CatalogValidationError) as exc:
            raise CatalogConflictError("view profile identity conflicts") from exc
        expected_profile_id = content_id(
            "profile",
            {
                "view_type": profile_row["view_type"],
                "name": profile_row["name"],
                "config": profile_config,
            },
        )
        if (
            profile_row["profile_id"] != expected_profile_id
            or profile_row["profile_digest"]
            != expected_profile_id.removeprefix("profile_")
            or profile_row["config_json"] != canonical_profile_config
        ):
            raise CatalogConflictError("view profile identity conflicts")

        object_row = self._require_record("objects", "digest", row["object_digest"])
        try:
            object_record = ObjectRecord(
                digest=object_row["digest"],
                storage_key=object_row["storage_key"],
                byte_size=object_row["byte_size"],
                media_type=object_row["media_type"],
            )
        except CatalogValidationError as exc:
            raise CatalogConflictError("registered object metadata conflicts") from exc
        if object_record.digest != row["object_digest"]:
            raise CatalogConflictError("registered object identity conflicts")

        try:
            metadata = json.loads(row["metadata_json"])
            if not isinstance(metadata, dict):
                raise CatalogValidationError(
                    "view generation metadata must be a JSON object"
                )
            canonical_metadata = canonical_json(metadata)
        except (TypeError, json.JSONDecodeError, CatalogValidationError) as exc:
            raise CatalogConflictError("view generation identity conflicts") from exc
        expected_view_generation_id = content_id(
            "view",
            {
                "repository_id": row["repository_id"],
                "source_revision_id": row["source_revision_id"],
                "profile_id": row["profile_id"],
                "view_type": row["view_type"],
                "object_digest": row["object_digest"],
                "schema_version": row["schema_version"],
                "metadata": metadata,
            },
        )
        if (
            row["view_generation_id"] != expected_view_generation_id
            or row["metadata_json"] != canonical_metadata
        ):
            raise CatalogConflictError("view generation identity conflicts")

    def _validate_ready_snapshot(
        self,
        snapshot: sqlite3.Row | None,
        *,
        repository_id: str,
        source_revision_id: str,
        content_digest: str,
        members: Sequence[tuple[str, str]],
        view_rows: Sequence[sqlite3.Row],
    ) -> None:
        """Fail closed unless a persisted snapshot exactly matches its identity."""
        expected_snapshot = (
            repository_id,
            source_revision_id,
            content_digest,
            "ready",
        )
        actual_snapshot = (
            tuple(
                snapshot[key]
                for key in (
                    "repository_id",
                    "source_revision_id",
                    "content_digest",
                    "status",
                )
            )
            if snapshot is not None
            else None
        )
        if (
            actual_snapshot != expected_snapshot
            or snapshot is None
            or not isinstance(snapshot["published_at"], str)
            or not snapshot["published_at"].strip()
        ):
            raise CatalogConflictError(
                "existing snapshot identity or seal state conflicts"
            )

        persisted_members = self._connection.execute(
            """
            SELECT view_type, view_generation_id FROM snapshot_views
            WHERE snapshot_id = ? ORDER BY view_type
            """,
            (snapshot["snapshot_id"],),
        ).fetchall()
        actual_members = [
            (row["view_type"], row["view_generation_id"]) for row in persisted_members
        ]
        if actual_members != list(members) or any(
            row["status"] != "ready"
            or not isinstance(row["ready_at"], str)
            or not row["ready_at"].strip()
            for row in view_rows
        ):
            raise CatalogConflictError("existing ready snapshot membership conflicts")

    def resolve_ref(self, repository_id: str, ref_name: str = "main") -> dict[str, Any]:
        """Resolve a named ref and return its pinned manifest summary."""
        repository = _required_text(repository_id, "repository ID")
        normalized_ref = _required_text(ref_name, "ref name")
        with self._transaction(immediate=False):
            row = self._connection.execute(
                """
                SELECT snapshot_id, generation, updated_at FROM refs
                WHERE repository_id = ? AND ref_name = ?
                """,
                (repository, normalized_ref),
            ).fetchone()
            if row is None:
                raise CatalogNotFoundError(
                    f"ref not found: {repository}:{normalized_ref}"
                )
            manifest = self._manifest_summary(row["snapshot_id"])
            return {
                "repository_id": repository,
                "ref_name": normalized_ref,
                "snapshot_id": row["snapshot_id"],
                "generation": int(row["generation"]),
                "updated_at": row["updated_at"],
                "manifest": manifest,
            }

    def get_manifest_summary(self, snapshot_id: str) -> dict[str, Any]:
        """Return the immutable summary for a published, ready snapshot."""
        normalized_snapshot = _required_text(snapshot_id, "snapshot ID")
        with self._transaction(immediate=False):
            return self._manifest_summary(normalized_snapshot)

    def _manifest_summary(self, snapshot_id: str) -> dict[str, Any]:
        snapshot = self._require_record("snapshots", "snapshot_id", snapshot_id)
        if snapshot["status"] != "ready":
            raise CatalogValidationError(
                f"snapshot is not ready for publication: {snapshot_id}"
            )
        source = self._require_record(
            "source_revisions", "source_revision_id", snapshot["source_revision_id"]
        )
        view_rows = self._connection.execute(
            """
            SELECT
                sv.view_type,
                vg.view_generation_id,
                vg.schema_version,
                vg.metadata_json,
                vg.object_digest,
                vp.profile_id,
                vp.name AS profile_name,
                vp.config_json AS profile_config_json,
                o.storage_key,
                o.byte_size,
                o.media_type
            FROM snapshot_views AS sv
            JOIN view_generations AS vg
                ON vg.view_generation_id = sv.view_generation_id
            JOIN view_profiles AS vp ON vp.profile_id = vg.profile_id
            JOIN objects AS o ON o.digest = vg.object_digest
            WHERE sv.snapshot_id = ?
            ORDER BY sv.view_type
            """,
            (snapshot_id,),
        ).fetchall()
        views = {
            row["view_type"]: {
                "view_generation_id": row["view_generation_id"],
                "schema_version": row["schema_version"],
                "metadata": json.loads(row["metadata_json"]),
                "profile": {
                    "profile_id": row["profile_id"],
                    "name": row["profile_name"],
                    "config": json.loads(row["profile_config_json"]),
                },
                "object": {
                    "digest": row["object_digest"],
                    "storage_key": row["storage_key"],
                    "byte_size": int(row["byte_size"]),
                    "media_type": row["media_type"],
                },
            }
            for row in view_rows
        }
        return {
            "snapshot_id": snapshot["snapshot_id"],
            "repository_id": snapshot["repository_id"],
            "status": snapshot["status"],
            "published_at": snapshot["published_at"],
            "source": {
                "source_revision_id": source["source_revision_id"],
                "kind": source["source_kind"],
                "commit_sha": source["commit_sha"],
                "tree_sha": source["tree_sha"],
                "source_fingerprint": source["source_fingerprint"],
            },
            "views": views,
        }


__all__ = [
    "CatalogConflictError",
    "CatalogError",
    "CatalogNotFoundError",
    "CatalogValidationError",
    "DEFAULT_NAMESPACE_ID",
    "DEFAULT_NAMESPACE_NAME",
    "LATEST_SCHEMA_VERSION",
    "SQLiteCatalog",
]
