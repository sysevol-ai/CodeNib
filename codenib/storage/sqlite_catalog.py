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
    ObjectRecord,
    PublishConflict,
    SourceRevision,
    StorageError,
    StorageNotFound,
    StorageValidationError,
    canonical_json,
    content_id,
    normalize_digest,
)

LATEST_SCHEMA_VERSION = 1
DEFAULT_NAMESPACE_ID = "ns_default"
DEFAULT_NAMESPACE_NAME = "default"

CatalogError = StorageError
CatalogConflictError = PublishConflict
CatalogNotFoundError = StorageNotFound
CatalogValidationError = StorageValidationError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalogValidationError(f"{field} must not be empty")
    return value.strip()


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

_MIGRATIONS: dict[int, tuple[str, ...]] = {1: _SCHEMA_V1}


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
        """Atomically publish views and advance a ref with compare-and-swap.

        A missing ref has generation zero.  On any validation or CAS failure,
        the snapshot insertion and every staged-to-ready transition roll back.
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

            current_ref = self._connection.execute(
                """
                SELECT generation FROM refs
                WHERE repository_id = ? AND ref_name = ?
                """,
                (repository, normalized_ref),
            ).fetchone()
            current_generation = (
                int(current_ref["generation"]) if current_ref is not None else 0
            )
            if current_generation != expected_generation:
                raise CatalogConflictError(
                    f"ref {normalized_ref!r} generation is {current_generation}; "
                    f"expected {expected_generation}"
                )

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
            published_at = _now()
            existing_snapshot = self._connection.execute(
                "SELECT * FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
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
                expected_snapshot = (
                    repository,
                    source,
                    content_digest,
                    "ready",
                )
                actual_snapshot = tuple(
                    existing_snapshot[key]
                    for key in (
                        "repository_id",
                        "source_revision_id",
                        "content_digest",
                        "status",
                    )
                )
                if actual_snapshot != expected_snapshot:
                    raise CatalogConflictError(
                        "existing snapshot identity or seal state conflicts"
                    )
                existing_members = self._connection.execute(
                    """
                    SELECT view_type, view_generation_id FROM snapshot_views
                    WHERE snapshot_id = ? ORDER BY view_type
                    """,
                    (snapshot_id,),
                ).fetchall()
                actual_members = [
                    (row["view_type"], row["view_generation_id"])
                    for row in existing_members
                ]
                if actual_members != members or any(
                    row["status"] != "ready" for row in rows
                ):
                    raise CatalogConflictError(
                        "existing ready snapshot membership conflicts"
                    )

            next_generation = expected_generation + 1
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
        }

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
