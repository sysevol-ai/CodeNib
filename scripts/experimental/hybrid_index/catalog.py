# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Four-table SQLite WAL control plane for the H1 experiment."""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from functools import lru_cache
from pathlib import Path

from .contracts import (
    Generation,
    PublishConflict,
    RefHead,
    ResolvedSnapshot,
    Snapshot,
    StorageIntegrityError,
    StorageNotFound,
    StorageValidationError,
    required_text,
)

_APPLICATION_ID = 0x434E4958  # ``CNIX``
_SCHEMA_VERSION = 1
_SQLITE_BUSY = 5
_SQLITE_LOCKED = 6

_SCHEMA = (
    """
    CREATE TABLE generations (
        generation_id TEXT PRIMARY KEY,
        repository TEXT NOT NULL,
        commit_sha TEXT NOT NULL,
        source_fingerprint TEXT NOT NULL,
        view_type TEXT NOT NULL,
        metadata_digest TEXT NOT NULL CHECK (length(metadata_digest) = 64),
        archive_digest TEXT NOT NULL CHECK (length(archive_digest) = 64),
        archive_size INTEGER NOT NULL CHECK (archive_size >= 0),
        file_count INTEGER NOT NULL CHECK (file_count >= 0),
        byte_count INTEGER NOT NULL CHECK (byte_count >= 0),
        created_at REAL NOT NULL,
        UNIQUE (generation_id, view_type)
    )
    """,
    """
    CREATE TABLE snapshots (
        snapshot_id TEXT PRIMARY KEY,
        repository TEXT NOT NULL,
        commit_sha TEXT NOT NULL,
        source_fingerprint TEXT NOT NULL,
        created_at REAL NOT NULL,
        UNIQUE (snapshot_id, repository)
    )
    """,
    """
    CREATE TABLE snapshot_generations (
        snapshot_id TEXT NOT NULL,
        view_type TEXT NOT NULL,
        generation_id TEXT NOT NULL,
        PRIMARY KEY (snapshot_id, view_type),
        UNIQUE (snapshot_id, generation_id),
        FOREIGN KEY (snapshot_id) REFERENCES snapshots(snapshot_id)
            ON DELETE RESTRICT,
        FOREIGN KEY (generation_id, view_type)
            REFERENCES generations(generation_id, view_type)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE refs (
        repository TEXT NOT NULL,
        ref_name TEXT NOT NULL,
        snapshot_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision > 0),
        updated_at REAL NOT NULL,
        PRIMARY KEY (repository, ref_name),
        FOREIGN KEY (snapshot_id, repository)
            REFERENCES snapshots(snapshot_id, repository)
            ON DELETE RESTRICT
    )
    """,
)


def _normalize_sql(value: object) -> str | None:
    if value is None:
        return None
    return " ".join(str(value).split())


def _schema_objects(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            row["type"],
            row["name"],
            row["tbl_name"],
            _normalize_sql(row["sql"]),
        )
        for row in connection.execute("""
            SELECT type, name, tbl_name, sql
              FROM sqlite_schema
             WHERE name NOT LIKE 'sqlite_%'
             ORDER BY type, name
            """)
    )


def _create_schema(connection: sqlite3.Connection) -> None:
    for statement in _SCHEMA:
        connection.execute(statement)


@lru_cache(maxsize=1)
def _expected_schema() -> tuple[tuple[object, ...], ...]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _create_schema(connection)
        return _schema_objects(connection)
    finally:
        connection.close()


def _database_identity(path: Path) -> tuple[int, int, int]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise StorageIntegrityError("catalog database path is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or not metadata.st_dev or not metadata.st_ino:
        raise StorageIntegrityError("catalog database is not a stable regular file")
    return (metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode))


def _generation_fields(generation: Generation) -> tuple[object, ...]:
    return (
        generation.repository,
        generation.commit,
        generation.source_fingerprint,
        generation.view_type,
        generation.metadata_digest,
        generation.archive_digest,
        generation.archive_size,
        generation.file_count,
        generation.byte_count,
    )


def _snapshot_fields(snapshot: Snapshot) -> tuple[object, ...]:
    return (
        snapshot.repository,
        snapshot.commit,
        snapshot.source_fingerprint,
    )


def _is_sqlite_busy_or_locked(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if type(code) is int:
        return code & 0xFF in {_SQLITE_BUSY, _SQLITE_LOCKED}
    message = str(exc).casefold()
    return message.startswith(
        ("database is locked", "database table is locked", "database schema is locked")
    )


class SQLiteCatalog:
    """Short-connection catalog whose only mutable rows are named refs.

    Object bytes are made durable before :meth:`publish_snapshot` starts.
    The ref INSERT/UPDATE inside ``BEGIN IMMEDIATE`` decides the winning value;
    ``COMMIT`` is the publication linearization point visible to readers. A
    failed transaction may leave an unreachable CAS object, but cannot expose
    an incomplete snapshot.
    """

    def __init__(self, path: str | os.PathLike[str], *, timeout: float = 30.0) -> None:
        if type(timeout) not in {int, float} or timeout <= 0:
            raise StorageValidationError("catalog timeout must be positive")
        if str(path) == ":memory:":
            raise StorageValidationError("H1 catalog requires a filesystem path")
        try:
            candidate = Path(path).expanduser().resolve(strict=False)
            candidate.parent.mkdir(parents=True, exist_ok=True)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise StorageValidationError("invalid catalog database path") from exc
        self.path = candidate
        self.timeout = float(timeout)
        self._identity: tuple[int, int, int] | None = None
        self._initialize()
        self._identity = _database_identity(self.path)

    def _open_raw(self, *, create: bool) -> sqlite3.Connection:
        if not create and self._identity is not None:
            if _database_identity(self.path) != self._identity:
                raise StorageIntegrityError("catalog database path identity changed")
        target = self.path
        if create:
            connection = sqlite3.connect(
                target,
                timeout=self.timeout,
                isolation_level=None,
            )
        else:
            connection = sqlite3.connect(
                f"{target.as_uri()}?mode=rw",
                timeout=self.timeout,
                isolation_level=None,
                uri=True,
            )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
        if not create and self._identity is not None:
            if _database_identity(self.path) != self._identity:
                connection.close()
                raise StorageIntegrityError("catalog database path identity changed")
        return connection

    @staticmethod
    def _header(connection: sqlite3.Connection) -> tuple[int, int]:
        return (
            int(connection.execute("PRAGMA application_id").fetchone()[0]),
            int(connection.execute("PRAGMA user_version").fetchone()[0]),
        )

    @classmethod
    def _require_schema(cls, connection: sqlite3.Connection) -> None:
        application_id, user_version = cls._header(connection)
        if application_id != _APPLICATION_ID:
            raise StorageIntegrityError(
                "SQLite file is not a CodeNib index experiment catalog"
            )
        if user_version != _SCHEMA_VERSION:
            raise StorageIntegrityError(
                f"unsupported index experiment schema: {user_version}"
            )
        if _schema_objects(connection) != _expected_schema():
            raise StorageIntegrityError("index experiment schema is not canonical")

    def _initialize(self) -> None:
        connection = self._open_raw(create=True)
        try:
            if _schema_objects(connection):
                self._require_schema(connection)
            else:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    if _schema_objects(connection):
                        self._require_schema(connection)
                    else:
                        application_id, user_version = self._header(connection)
                        if application_id != 0 or user_version != 0:
                            raise StorageIntegrityError(
                                "refusing to initialize an identified SQLite file"
                            )
                        _create_schema(connection)
                        connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
                        connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                        self._require_schema(connection)
                    connection.commit()
                except BaseException:
                    if connection.in_transaction:
                        connection.rollback()
                    raise
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    mode = str(
                        connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                    ).lower()
                    break
                except sqlite3.OperationalError as exc:
                    remaining = deadline - time.monotonic()
                    if not _is_sqlite_busy_or_locked(exc) or remaining <= 0:
                        raise
                    time.sleep(min(0.01, remaining))
            if mode != "wal":
                raise StorageIntegrityError("index experiment catalog requires WAL")
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = self._open_raw(create=False)
        try:
            self._require_schema(connection)
            mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            if mode != "wal":
                raise StorageIntegrityError("index experiment catalog left WAL mode")
            return connection
        except BaseException:
            connection.close()
            raise

    @property
    def journal_mode(self) -> str:
        connection = self._connect()
        try:
            return str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        finally:
            connection.close()

    @staticmethod
    def _insert_generation(
        connection: sqlite3.Connection,
        generation: Generation,
        *,
        now: float,
    ) -> None:
        row = connection.execute(
            """
            SELECT repository, commit_sha, source_fingerprint, view_type,
                   metadata_digest, archive_digest, archive_size,
                   file_count, byte_count
              FROM generations
             WHERE generation_id = ?
            """,
            (generation.generation_id,),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO generations(
                    generation_id, repository, commit_sha, source_fingerprint,
                    view_type, metadata_digest, archive_digest, archive_size,
                    file_count, byte_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (generation.generation_id, *_generation_fields(generation), now),
            )
        elif tuple(row) != _generation_fields(generation):
            raise StorageIntegrityError(
                f"generation {generation.generation_id} is not immutable"
            )

    @staticmethod
    def _insert_snapshot(
        connection: sqlite3.Connection,
        snapshot: Snapshot,
        *,
        now: float,
    ) -> None:
        row = connection.execute(
            """
            SELECT repository, commit_sha, source_fingerprint
              FROM snapshots
             WHERE snapshot_id = ?
            """,
            (snapshot.snapshot_id,),
        ).fetchone()
        expected_members = tuple(
            (generation.view_type, generation.generation_id)
            for generation in snapshot.generations
        )
        if row is None:
            connection.execute(
                """
                INSERT INTO snapshots(
                    snapshot_id, repository, commit_sha,
                    source_fingerprint, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (snapshot.snapshot_id, *_snapshot_fields(snapshot), now),
            )
            connection.executemany(
                """
                INSERT INTO snapshot_generations(
                    snapshot_id, view_type, generation_id
                ) VALUES (?, ?, ?)
                """,
                (
                    (snapshot.snapshot_id, view_type, generation_id)
                    for view_type, generation_id in expected_members
                ),
            )
            return
        observed_members = tuple(
            tuple(member)
            for member in connection.execute(
                """
                SELECT view_type, generation_id
                  FROM snapshot_generations
                 WHERE snapshot_id = ?
                 ORDER BY view_type
                """,
                (snapshot.snapshot_id,),
            )
        )
        if tuple(row) != _snapshot_fields(snapshot) or observed_members != (
            expected_members
        ):
            raise StorageIntegrityError(
                f"snapshot {snapshot.snapshot_id} is not immutable"
            )

    def _before_ref_update(self) -> None:
        """Fault-injection seam immediately before the mutable operation."""

    @staticmethod
    def _advance_ref(
        connection: sqlite3.Connection,
        snapshot: Snapshot,
        *,
        ref_name: str,
        expected_revision: int,
        now: float,
    ) -> RefHead:
        row = connection.execute(
            """
            SELECT snapshot_id, revision
              FROM refs
             WHERE repository = ? AND ref_name = ?
            """,
            (snapshot.repository, ref_name),
        ).fetchone()
        if row is None:
            if expected_revision != 0:
                raise PublishConflict(
                    f"ref {snapshot.repository}:{ref_name} does not exist"
                )
            revision = 1
            connection.execute(
                """
                INSERT INTO refs(
                    repository, ref_name, snapshot_id, revision, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    snapshot.repository,
                    ref_name,
                    snapshot.snapshot_id,
                    revision,
                    now,
                ),
            )
        else:
            current_snapshot = str(row["snapshot_id"])
            current_revision = int(row["revision"])
            if current_snapshot == snapshot.snapshot_id and expected_revision in {
                current_revision,
                current_revision - 1,
            }:
                return RefHead(
                    repository=snapshot.repository,
                    ref_name=ref_name,
                    snapshot_id=current_snapshot,
                    revision=current_revision,
                )
            if current_revision != expected_revision:
                raise PublishConflict(
                    f"ref {snapshot.repository}:{ref_name} is at revision "
                    f"{current_revision}, not {expected_revision}"
                )
            revision = current_revision + 1
            connection.execute(
                """
                UPDATE refs
                   SET snapshot_id = ?, revision = ?, updated_at = ?
                 WHERE repository = ? AND ref_name = ? AND revision = ?
                """,
                (
                    snapshot.snapshot_id,
                    revision,
                    now,
                    snapshot.repository,
                    ref_name,
                    expected_revision,
                ),
            )
            if int(connection.execute("SELECT changes()").fetchone()[0]) != 1:
                raise PublishConflict(
                    f"ref {snapshot.repository}:{ref_name} changed concurrently"
                )
        return RefHead(
            repository=snapshot.repository,
            ref_name=ref_name,
            snapshot_id=snapshot.snapshot_id,
            revision=revision,
        )

    def publish_snapshot(
        self,
        snapshot: Snapshot,
        *,
        ref_name: str = "main",
        expected_revision: int = 0,
    ) -> RefHead:
        if type(snapshot) is not Snapshot:
            raise TypeError("catalog publication requires an exact Snapshot")
        ref_name = required_text(ref_name, label="ref name")
        if type(expected_revision) is not int or expected_revision < 0:
            raise StorageValidationError(
                "expected ref revision must be a non-negative integer"
            )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = time.time()
            for generation in snapshot.generations:
                self._insert_generation(connection, generation, now=now)
            self._insert_snapshot(connection, snapshot, now=now)
            self._before_ref_update()
            head = self._advance_ref(
                connection,
                snapshot,
                ref_name=ref_name,
                expected_revision=expected_revision,
                now=now,
            )
            connection.commit()
            return head
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _load_snapshot(
        connection: sqlite3.Connection,
        snapshot_id: str,
    ) -> Snapshot:
        row = connection.execute(
            """
            SELECT repository, commit_sha, source_fingerprint
              FROM snapshots
             WHERE snapshot_id = ?
            """,
            (snapshot_id,),
        ).fetchone()
        if row is None:
            raise StorageNotFound(f"snapshot does not exist: {snapshot_id}")
        generation_rows = tuple(
            connection.execute(
                """
                SELECT g.generation_id, g.repository, g.commit_sha,
                       g.source_fingerprint, g.view_type, g.metadata_digest,
                       g.archive_digest, g.archive_size, g.file_count, g.byte_count
                  FROM snapshot_generations AS sg
                  JOIN generations AS g
                    ON g.generation_id = sg.generation_id
                   AND g.view_type = sg.view_type
                 WHERE sg.snapshot_id = ?
                 ORDER BY sg.view_type
                """,
                (snapshot_id,),
            )
        )
        try:
            generations = tuple(
                Generation(
                    generation_id=generation["generation_id"],
                    repository=generation["repository"],
                    commit=generation["commit_sha"],
                    source_fingerprint=generation["source_fingerprint"],
                    view_type=generation["view_type"],
                    metadata_digest=generation["metadata_digest"],
                    archive_digest=generation["archive_digest"],
                    archive_size=generation["archive_size"],
                    file_count=generation["file_count"],
                    byte_count=generation["byte_count"],
                )
                for generation in generation_rows
            )
            return Snapshot(
                snapshot_id=snapshot_id,
                repository=row["repository"],
                commit=row["commit_sha"],
                source_fingerprint=row["source_fingerprint"],
                generations=generations,
            )
        except (TypeError, ValueError) as exc:
            raise StorageIntegrityError(
                f"snapshot {snapshot_id} contains invalid metadata"
            ) from exc

    def get_snapshot(self, snapshot_id: str) -> ResolvedSnapshot:
        snapshot_id = required_text(snapshot_id, label="snapshot ID")
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            snapshot = self._load_snapshot(connection, snapshot_id)
            connection.commit()
            return ResolvedSnapshot(snapshot=snapshot)
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def resolve_ref(
        self,
        repository: str,
        ref_name: str = "main",
    ) -> ResolvedSnapshot:
        repository = required_text(repository, label="repository")
        ref_name = required_text(ref_name, label="ref name")
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            row = connection.execute(
                """
                SELECT snapshot_id, revision
                  FROM refs
                 WHERE repository = ? AND ref_name = ?
                """,
                (repository, ref_name),
            ).fetchone()
            if row is None:
                raise StorageNotFound(f"ref does not exist: {repository}:{ref_name}")
            head = RefHead(
                repository=repository,
                ref_name=ref_name,
                snapshot_id=row["snapshot_id"],
                revision=row["revision"],
            )
            snapshot = self._load_snapshot(connection, head.snapshot_id)
            if snapshot.repository != repository:
                raise StorageIntegrityError("ref crosses repository identity")
            connection.commit()
            return ResolvedSnapshot(snapshot=snapshot, ref=head)
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()


__all__ = ["SQLiteCatalog"]
