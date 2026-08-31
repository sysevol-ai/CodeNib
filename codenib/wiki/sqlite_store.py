# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""SQLite WAL implementation of the narrow Wiki store contract.

This is a trusted local, regenerable cache. Bounds and digests catch malformed
or accidentally damaged entries; they are not a security boundary against a
process that can rewrite both the database payload and its digest.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
from collections.abc import Collection, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from filelock import FileLock

from .._bounded_json import (
    _bounded_parse_float,
    canonical_json_value_chunks,
    validate_bounded_json_stream,
    validate_json_complexity,
)
from .store import (
    WIKI_ENVELOPE_MAX_BYTES,
    WikiStoreCorruptionError,
    WikiStoredEntry,
    WikiStoreError,
    WikiStoreSchemaError,
    WikiStoreValidationError,
)

_APPLICATION_ID = 0x434E574B  # ``CNWK``
_SCHEMA_VERSION = 1
_MAX_IDENTIFIER_BYTES = 4_096
# Stable SQLite primary result codes. Python 3.10 does not expose the matching
# ``sqlite3.SQLITE_*`` module constants even though exceptions carry the code.
_SQLITE_CORRUPT = 11
_SQLITE_SCHEMA = 17
_SQLITE_NOTADB = 26
_SQLITE_CORRUPTION_MESSAGES = frozenset(
    ("database disk image is malformed", "file is not a database")
)

_CREATE_SCHEMA_SQL = """
CREATE TABLE wiki_entries (
    entry_id TEXT PRIMARY KEY NOT NULL,
    repository_id TEXT NOT NULL,
    envelope_json BLOB NOT NULL,
    envelope_sha256 TEXT NOT NULL CHECK (length(envelope_sha256) = 64)
);
CREATE INDEX wiki_entries_repository_entry_idx
    ON wiki_entries(repository_id, entry_id);
"""

_EXPECTED_COLUMNS = (
    ("entry_id", "TEXT", 1, 1),
    ("repository_id", "TEXT", 1, 0),
    ("envelope_json", "BLOB", 1, 0),
    ("envelope_sha256", "TEXT", 1, 0),
)


def _reject_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def _reject_nonfinite_number(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _sqlite_error(operation: str, exc: sqlite3.Error) -> WikiStoreError:
    error_code = getattr(exc, "sqlite_errorcode", None)
    primary_code = error_code & 0xFF if isinstance(error_code, int) else None
    message = str(exc).strip().casefold()
    if primary_code in {_SQLITE_CORRUPT, _SQLITE_NOTADB} or (
        primary_code is None
        and type(exc) is sqlite3.DatabaseError
        and message in _SQLITE_CORRUPTION_MESSAGES
    ):
        return WikiStoreCorruptionError(f"Wiki database {operation} found corruption")
    if primary_code == _SQLITE_SCHEMA or (
        primary_code is None
        and not isinstance(exc, sqlite3.IntegrityError)
        and message == "database schema has changed"
    ):
        return WikiStoreSchemaError(f"Wiki database schema changed during {operation}")
    if isinstance(exc, sqlite3.IntegrityError):
        return WikiStoreValidationError(
            f"Wiki database rejected an entry during {operation}"
        )
    return WikiStoreError(f"Wiki database {operation} failed")


def _validate_identifier(value: object, *, field: str) -> str:
    if type(value) is not str or not value:
        raise WikiStoreValidationError(f"{field} must be a non-empty string")
    if "\x00" in value:
        raise WikiStoreValidationError(f"{field} must not contain NUL bytes")
    if len(value.encode("utf-8")) > _MAX_IDENTIFIER_BYTES:
        raise WikiStoreValidationError(
            f"{field} exceeds its {_MAX_IDENTIFIER_BYTES}-byte limit"
        )
    return value


def _canonical_envelope(envelope: Mapping[str, Any]) -> bytes:
    if not isinstance(envelope, Mapping):
        raise WikiStoreValidationError("envelope must be a JSON object")
    try:
        value = dict(envelope)
        validate_json_complexity(value, label="Wiki envelope")
        payload = bytearray()
        for chunk in canonical_json_value_chunks(value):
            if len(payload) + len(chunk) > WIKI_ENVELOPE_MAX_BYTES:
                raise WikiStoreValidationError(
                    "Wiki envelope exceeds its " f"{WIKI_ENVELOPE_MAX_BYTES}-byte limit"
                )
            payload.extend(chunk)
    except WikiStoreValidationError:
        raise
    except MemoryError:
        raise
    except Exception as exc:
        raise WikiStoreValidationError(
            "envelope must contain only bounded JSON values"
        ) from exc
    return bytes(payload)


def _decode_envelope(payload: object, digest: object) -> dict[str, Any]:
    if isinstance(payload, memoryview):
        payload = payload.tobytes()
    if type(payload) is not bytes:
        raise WikiStoreCorruptionError("Wiki entry payload is not a byte string")
    if len(payload) > WIKI_ENVELOPE_MAX_BYTES:
        raise WikiStoreCorruptionError("Wiki entry payload exceeds its size limit")
    if type(digest) is not str:
        raise WikiStoreCorruptionError("Wiki entry has an invalid SHA-256 digest")
    observed_digest = hashlib.sha256(payload).hexdigest()
    if observed_digest != digest:
        raise WikiStoreCorruptionError("Wiki entry failed its SHA-256 integrity check")
    try:
        validate_bounded_json_stream(
            io.BytesIO(payload),
            label="persisted Wiki envelope",
            max_bytes=WIKI_ENVELOPE_MAX_BYTES,
        )
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_object,
            parse_constant=_reject_nonfinite_number,
            parse_float=_bounded_parse_float,
        )
        if not isinstance(value, dict):
            raise ValueError("Wiki envelope is not a JSON object")
        validate_json_complexity(value, label="persisted Wiki envelope")
    except (ValueError, WikiStoreValidationError) as exc:
        raise WikiStoreCorruptionError("Wiki entry contains invalid JSON") from exc
    return value


class SQLiteWikiStore:
    """A short-connection, transactional SQLite store for Wiki envelopes."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        if str(path) == ":memory:":
            raise WikiStoreValidationError(
                "short-connection Wiki stores require a filesystem path"
            )
        try:
            resolved = Path(path).expanduser().resolve()
            resolved.parent.mkdir(parents=True, exist_ok=True)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise WikiStoreValidationError("invalid Wiki database path") from exc
        self.path = resolved
        self._lock_directory = Path(f"{resolved}.locks")
        try:
            self._lock_directory.mkdir(parents=True, exist_ok=True)
            # Lock acquisition linearizes schema identity and the WAL transition
            # for this database. Initialization takes no entry lock, retains no
            # owner after return, and the OS releases the file lock on exit.
            with FileLock(str(self._lock_directory / ".initialize.lock")):
                self._initialize()
        except WikiStoreError:
            raise
        except OSError as exc:
            raise WikiStoreError("Wiki database initialization lock failed") from exc

    def _open_raw(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path),
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _identity(connection: sqlite3.Connection) -> tuple[int, int, frozenset[str]]:
        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = frozenset(
            row["name"]
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            )
        )
        return application_id, user_version, tables

    @staticmethod
    def _is_empty_database(connection: sqlite3.Connection) -> bool:
        return (
            connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is None
        )

    def _initialize(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._open_raw()
            application_id, user_version, _ = self._identity(connection)
            if (
                application_id == 0
                and user_version == 0
                and self._is_empty_database(connection)
            ):
                connection.execute("BEGIN IMMEDIATE")
                try:
                    application_id, user_version, _ = self._identity(connection)
                    if (
                        application_id == 0
                        and user_version == 0
                        and self._is_empty_database(connection)
                    ):
                        schema_statements = tuple(
                            statement.strip()
                            for statement in _CREATE_SCHEMA_SQL.split(";")
                            if statement.strip()
                        )
                        for statement in schema_statements:
                            connection.execute(statement)
                        connection.execute(
                            f"PRAGMA application_id = {_APPLICATION_ID:d}"
                        )
                        connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION:d}")
                    connection.commit()
                except BaseException:
                    if connection.in_transaction:
                        connection.rollback()
                    raise
            self._require_schema(connection)
            self._configure_database(connection)
            self._configure_connection(connection)
        except WikiStoreError:
            raise
        except sqlite3.Error as exc:
            raise _sqlite_error("initialization", exc) from exc
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _require_schema(connection: sqlite3.Connection) -> None:
        application_id, user_version, tables = SQLiteWikiStore._identity(connection)
        if application_id != _APPLICATION_ID:
            raise WikiStoreSchemaError(
                "file is not an initialized CodeNib Wiki database"
            )
        if user_version > _SCHEMA_VERSION:
            raise WikiStoreSchemaError(
                "Wiki database schema is newer than this CodeNib version: "
                f"{user_version} > {_SCHEMA_VERSION}"
            )
        if user_version != _SCHEMA_VERSION:
            raise WikiStoreSchemaError(
                f"unsupported Wiki database schema version: {user_version}"
            )
        if tables != frozenset(("wiki_entries",)):
            raise WikiStoreSchemaError("Wiki database has an unexpected table layout")
        columns = tuple(
            (row["name"], row["type"], row["notnull"], row["pk"])
            for row in connection.execute("PRAGMA table_info(wiki_entries)")
        )
        if columns != _EXPECTED_COLUMNS:
            raise WikiStoreSchemaError("Wiki database has an unexpected entry schema")

    @staticmethod
    def _configure_database(connection: sqlite3.Connection) -> None:
        journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(journal_mode).lower() != "wal":
            raise WikiStoreError("SQLite WAL mode could not be enabled")

    @staticmethod
    def _configure_connection(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA synchronous = NORMAL")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._open_raw()
            self._configure_connection(connection)
            yield connection
        except WikiStoreError:
            raise
        except sqlite3.Error as exc:
            raise _sqlite_error("operation", exc) from exc
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _entry_from_row(row: sqlite3.Row) -> WikiStoredEntry:
        try:
            entry_id = _validate_identifier(row["entry_id"], field="entry_id")
            repository_id = _validate_identifier(
                row["repository_id"], field="repository_id"
            )
        except WikiStoreValidationError as exc:
            raise WikiStoreCorruptionError(
                "Wiki entry contains invalid identity metadata"
            ) from exc
        envelope = _decode_envelope(row["envelope_json"], row["envelope_sha256"])
        return WikiStoredEntry(
            entry_id=entry_id,
            repository_id=repository_id,
            envelope=envelope,
        )

    def read(self, entry_id: str) -> WikiStoredEntry | None:
        entry_id = _validate_identifier(entry_id, field="entry_id")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM wiki_entries WHERE entry_id = ?", (entry_id,)
            ).fetchone()
        return None if row is None else self._entry_from_row(row)

    def publish(
        self,
        *,
        entry_id: str,
        repository_id: str,
        envelope: Mapping[str, Any],
        if_absent: bool = False,
    ) -> WikiStoredEntry:
        entry_id = _validate_identifier(entry_id, field="entry_id")
        repository_id = _validate_identifier(repository_id, field="repository_id")
        if type(if_absent) is not bool:
            raise WikiStoreValidationError("if_absent must be a boolean")
        payload = _canonical_envelope(envelope)
        digest = hashlib.sha256(payload).hexdigest()
        published = WikiStoredEntry(
            entry_id=entry_id,
            repository_id=repository_id,
            envelope=_decode_envelope(payload, digest),
        )

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM wiki_entries WHERE entry_id = ?", (entry_id,)
                ).fetchone()
                if row is not None:
                    try:
                        current_repository_id = _validate_identifier(
                            row["repository_id"], field="repository_id"
                        )
                    except WikiStoreValidationError as exc:
                        raise WikiStoreCorruptionError(
                            "Wiki entry contains invalid identity metadata"
                        ) from exc
                    if current_repository_id != repository_id:
                        raise WikiStoreValidationError(
                            "entry_id is already bound to different Wiki metadata"
                        )
                    if if_absent:
                        current = self._entry_from_row(row)
                        connection.commit()
                        return current
                    connection.execute(
                        """
                        UPDATE wiki_entries
                        SET envelope_json = ?, envelope_sha256 = ?
                        WHERE entry_id = ?
                        """,
                        (sqlite3.Binary(payload), digest, entry_id),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO wiki_entries(
                            entry_id,
                            repository_id,
                            envelope_json,
                            envelope_sha256
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            entry_id,
                            repository_id,
                            sqlite3.Binary(payload),
                            digest,
                        ),
                    )
                connection.commit()
                return published
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise

    def scan(
        self,
        *,
        repository_ids: Collection[str] | None = None,
    ) -> tuple[WikiStoredEntry, ...]:
        parameters: tuple[str, ...] = ()
        if repository_ids is not None:
            if isinstance(repository_ids, (str, bytes)) or not isinstance(
                repository_ids, Collection
            ):
                raise WikiStoreValidationError(
                    "repository_ids must be a collection of strings"
                )
            parameters = tuple(
                sorted(
                    {
                        _validate_identifier(value, field="repository_id")
                        for value in repository_ids
                    }
                )
            )
        with self._connection() as connection:
            if repository_ids is None:
                rows = connection.execute(
                    "SELECT * FROM wiki_entries ORDER BY entry_id"
                ).fetchall()
            elif not parameters:
                rows = []
            else:
                placeholders = ", ".join("?" for _ in parameters)
                rows = connection.execute(
                    "SELECT * FROM wiki_entries "
                    f"WHERE repository_id IN ({placeholders}) ORDER BY entry_id",
                    parameters,
                ).fetchall()
        return tuple(self._entry_from_row(row) for row in rows)

    @contextmanager
    def generation_guard(self, entry_id: str) -> Iterator[None]:
        entry_id = _validate_identifier(entry_id, field="entry_id")
        lock_name = hashlib.sha256(entry_id.encode("utf-8")).hexdigest() + ".lock"
        try:
            self._lock_directory.mkdir(parents=True, exist_ok=True)
            lock = FileLock(str(self._lock_directory / lock_name))
            lock.acquire()
        except OSError as exc:
            raise WikiStoreError("Wiki generation lock failed") from exc
        try:
            yield
        finally:
            try:
                lock.release()
            except OSError as exc:
                raise WikiStoreError("Wiki generation lock release failed") from exc


__all__ = ["SQLiteWikiStore"]
