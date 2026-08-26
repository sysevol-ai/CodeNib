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

import hashlib
import json
import os
import sqlite3
import stat
import sys
import tempfile
import time
import weakref
from collections import deque
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from functools import partial, wraps
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .._owned_file_publication import _CancellationSafeRLock
from .models import (
    DEFAULT_NAMESPACE_ID,
    DEFAULT_NAMESPACE_NAME,
    INDEX_JOB_EVENT_PAYLOAD_MAX_TEXT_CHARS,
    INDEX_JOB_PUBLICATION_CONTRACT,
    MAX_INDEX_JOB_EVENTS_PER_ATTEMPT,
    MAX_VIEW_GENERATION_MEMBERS,
    VIEW_GENERATION_MEMBERS_METADATA_KEY,
    IndexJobAttemptCompletionRecord,
    IndexJobAttemptHeartbeat,
    IndexJobAttemptRecord,
    IndexJobCompletion,
    IndexJobEffectiveMode,
    IndexJobEventKind,
    IndexJobEventRecord,
    IndexJobRecord,
    IndexJobRequest,
    IndexJobRunnableCursor,
    IndexJobRunnableCycle,
    IndexJobRunnablePage,
    IndexJobStatus,
    IndexJobViewOutcome,
    IndexJobViewOutput,
    IndexJobViewRecord,
    NamespaceIdentity,
    ObjectRecord,
    PublishConflict,
    RefJobLease,
    RepositoryIdentity,
    SourceRevision,
    StorageError,
    StorageIntegrityError,
    StorageNotFound,
    StorageValidationError,
    canonical_json,
    canonical_utc_timestamp,
    content_id,
    normalize_digest,
    normalize_view_generation_metadata,
    snapshot_index_job_event_payload,
    view_generation_member_digests,
)
from .protocols import (
    RETAINED_IMPORT_CATALOG_CONTRACT,
    RETAINED_IMPORT_RESPONSE_MAX_TEXT_CHARS,
    snapshot_retained_import_response,
)

LATEST_SCHEMA_VERSION = 7

CatalogError = StorageError
CatalogConflictError = PublishConflict
CatalogNotFoundError = StorageNotFound
CatalogValidationError = StorageValidationError


class _CatalogValidationNamespaceChanged(CatalogError):
    """A validated catalog namespace changed during its private copy."""


class _CatalogPathCoordination:
    """Process-local serialization for one resolved SQLite catalog path."""

    __slots__ = ("lock", "__weakref__")

    def __init__(self) -> None:
        self.lock = _CancellationSafeRLock()


_CatalogPathCoordinationState = tuple[
    _CancellationSafeRLock,
    weakref.WeakValueDictionary[str, _CatalogPathCoordination],
]


def _new_catalog_path_coordination_state() -> _CatalogPathCoordinationState:
    return _CancellationSafeRLock(), weakref.WeakValueDictionary()


_CATALOG_PATH_COORDINATION_STATES: dict[int, _CatalogPathCoordinationState] = {
    os.getpid(): _new_catalog_path_coordination_state()
}


def _reset_catalog_path_coordination_after_fork() -> None:
    """Discard every inherited path lock before a child can use the registry."""

    global _CATALOG_PATH_COORDINATION_STATES
    owner_pid = os.getpid()
    _CATALOG_PATH_COORDINATION_STATES = {
        owner_pid: _new_catalog_path_coordination_state()
    }


if hasattr(os, "register_at_fork"):  # pragma: no branch - POSIX runtime gate
    os.register_at_fork(after_in_child=_reset_catalog_path_coordination_after_fork)


def _catalog_path_coordination(path: Path | None) -> _CatalogPathCoordination:
    """Return a fork-safe, process-local coordinator for one resolved path."""

    if path is None:
        return _CatalogPathCoordination()
    owner_pid = os.getpid()
    # CPython publishes one process-local state atomically even if a fork child
    # starts several threads before its first catalog lookup. Never acquire an
    # inherited guard: its owning parent thread may not exist in this process.
    guard, coordinations = _CATALOG_PATH_COORDINATION_STATES.setdefault(
        owner_pid,
        _new_catalog_path_coordination_state(),
    )
    key = os.path.normcase(str(path))

    def registered_coordination() -> _CatalogPathCoordination:
        coordination = coordinations.get(key)
        if coordination is None:
            coordination = _CatalogPathCoordination()
            coordinations[key] = coordination
        return coordination

    return guard.run(registered_coordination)


def _coordinated_catalog_method(method: Any) -> Any:
    """Run one complete catalog method under its cancellation-safe path lock."""

    @wraps(method)
    def coordinated(self: Any, *args: Any, **kwargs: Any) -> Any:
        self._require_owner_pid()
        callback = partial(method, self, *args, **kwargs)
        return self._path_coordination.lock.run(callback)

    return coordinated


_DB_NOW_MS_SQL = "CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)"
_INDEX_JOB_EXECUTION_WITNESS_SQL = """
SELECT initial_created_at_ms AS evidence_at_ms
FROM index_job_attempt_baselines
UNION ALL
SELECT legacy_content_high_water_ms FROM index_job_attempt_baselines
UNION ALL
SELECT legacy_started_at_ms FROM index_job_attempt_baselines
WHERE legacy_started_at_ms IS NOT NULL
UNION ALL
SELECT started_at_ms FROM index_job_attempts
UNION ALL
SELECT requested_at_ms FROM index_job_cancellation_requests
UNION ALL
SELECT created_at_ms FROM index_job_events
UNION ALL
SELECT completed_at_ms FROM index_job_attempt_completions
UNION ALL
SELECT completed_at_ms FROM index_job_publications
"""
_SQLITE_INT64_MAX = 9_223_372_036_854_775_807
_MAX_JOB_PUBLICATION_OUTPUTS = 64
_MAX_RUNNABLE_JOB_SCAN_LIMIT = 256
_MAX_JOB_EVENT_PAGE_LIMIT = 256
_SQLITE_LEGACY_AUTOCOMMIT = getattr(
    sqlite3,
    "LEGACY_TRANSACTION_CONTROL",
    None,
)
_SQLITE_CONNECT_OPTIONS = (
    {"autocommit": _SQLITE_LEGACY_AUTOCOMMIT}
    if _SQLITE_LEGACY_AUTOCOMMIT is not None
    else {}
)
_MAX_VALIDATION_NAMESPACE_BYTES = 1_073_741_824
_MAX_VALIDATION_SHM_BYTES = 16_777_216
_VALIDATION_COPY_CHUNK_BYTES = 1_048_576
_VALIDATION_NAMESPACE_MAX_ATTEMPTS = 16
_VALIDATION_RETRY_INITIAL_SECONDS = 0.001
_VALIDATION_RETRY_MAX_SECONDS = 0.025
_MAX_VALIDATION_MOUNTINFO_ENTRIES = 100_000
_MAX_VALIDATION_MOUNTINFO_LINE_BYTES = 65_536
_WINDOWS_REPARSE_POINT_ATTRIBUTE = 0x400
_SCHEMA_MIGRATIONS_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
)
"""


_TRANSACTION_NEW = "new"
_TRANSACTION_BEGINNING = "beginning"
_TRANSACTION_ACTIVE = "active"
_TRANSACTION_COMMIT_PENDING = "commit-pending"
_TRANSACTION_COMMITTING = "committing"
_TRANSACTION_ROLLING_BACK = "rolling-back"
_TRANSACTION_CLOSING = "closing"
_TRANSACTION_COMMITTED = "committed"
_TRANSACTION_ROLLED_BACK = "rolled-back"
_TRANSACTION_CLOSED = "closed"


class _SQLiteTransactionOwner:
    """Drive one explicit transaction until completion is observable.

    Cancellation may arrive immediately after any C-level BEGIN, COMMIT,
    ROLLBACK, or close call. The owner therefore records intent separately and
    repeatedly observes ``in_transaction`` until the transaction is absent or
    the connection has been closed.
    """

    __slots__ = (
        "ambient_error",
        "body_succeeded",
        "commit_attempted",
        "connection",
        "connection_closed",
        "force_close",
        "outcome",
        "owner_pid",
        "phase",
        "primary_error",
        "rollback_attempted",
        "settled",
        "settlement_runner",
    )

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.owner_pid = os.getpid()
        self.connection = connection
        self.ambient_error = sys.exc_info()[1]
        self.phase = _TRANSACTION_NEW
        self.body_succeeded = False
        self.commit_attempted = False
        self.rollback_attempted = False
        self.force_close = False
        self.connection_closed = False
        self.settled = False
        self.outcome: str | None = None
        self.primary_error: BaseException | None = None
        # Construct the constant-stack C iterator before BEGIN can acquire a
        # database lock.
        self.settlement_runner = partial(_sqlite_transaction_outer_pass, self)

    def require_owner_pid(self) -> None:
        if os.getpid() != self.owner_pid:
            raise CatalogError("SQLite transaction owner crossed a PID boundary")

    def begin(self, *, immediate: bool) -> None:
        self.require_owner_pid()
        if not _sqlite_uses_legacy_autocommit(self.connection):
            raise CatalogError(
                "SQLite connection autocommit mode changed before transaction BEGIN"
            )
        self.phase = _TRANSACTION_BEGINNING
        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        self.phase = _TRANSACTION_ACTIVE

    def mark_body_succeeded(self) -> None:
        self.require_owner_pid()
        self.body_succeeded = True
        self.phase = _TRANSACTION_COMMIT_PENDING

    def retain(self, error: BaseException, *, label: str) -> None:
        self.require_owner_pid()
        candidate = _first_transaction_error(
            error,
            ambient_error=self.ambient_error,
        )
        if self.primary_error is None:
            self.primary_error = candidate
            if candidate is not error:
                _annotate_transaction_error(candidate, label, error)
            return
        _annotate_transaction_error(self.primary_error, label, error)


def _first_transaction_error(
    error: BaseException,
    *,
    ambient_error: BaseException | None,
) -> BaseException:
    """Recover an earlier ordinary error replaced by cleanup cancellation."""

    candidate = error
    seen: set[int] = set()
    while not isinstance(candidate, Exception):
        marker = id(candidate)
        if marker in seen:
            break
        seen.add(marker)
        try:
            if BaseException.__getattribute__(candidate, "__suppress_context__"):
                break
            context = BaseException.__getattribute__(candidate, "__context__")
        except BaseException:  # noqa: B036 - retaining the top error is safe
            break
        if not isinstance(context, BaseException):
            break
        # A transaction may be entered while an unrelated exception is being
        # handled.  That ambient context is not part of this transaction's
        # failure chain and must never replace its own KeyboardInterrupt or
        # SystemExit primary.
        if context is ambient_error:
            break
        candidate = context
    return candidate


def _annotate_transaction_error(
    primary: BaseException,
    label: str,
    secondary: BaseException,
) -> None:
    if primary is secondary:
        return
    try:
        add_note = getattr(BaseException, "add_note", None)
        if add_note is not None:
            add_note(primary, f"{label}: {secondary!r}")
    except BaseException:  # noqa: B036 - diagnostics are secondary
        return


def _sqlite_transaction_pass(owner: _SQLiteTransactionOwner) -> bool:
    """Advance one restartable commit, rollback, or close transition."""

    owner.require_owner_pid()
    if owner.connection_closed:
        owner.phase = _TRANSACTION_CLOSED
        owner.outcome = _TRANSACTION_CLOSED
        owner.settled = True
        return True

    if owner.force_close:
        owner.phase = _TRANSACTION_CLOSING
        owner.connection.close()
        owner.connection_closed = True
        # Observe the completion marker on a new pass, including when
        # cancellation lands immediately after close returns.
        return False

    try:
        legacy_autocommit = _sqlite_uses_legacy_autocommit(owner.connection)
    except sqlite3.ProgrammingError:
        owner.connection_closed = True
        raise
    except BaseException as error:  # noqa: B036 - retry cancellation, close errors
        if isinstance(error, Exception):
            owner.force_close = True
        raise
    if not legacy_autocommit:
        owner.force_close = True
        raise CatalogError(
            "SQLite connection autocommit mode changed during transaction"
        )

    try:
        active = owner.connection.in_transaction
    except sqlite3.ProgrammingError as error:
        if owner.phase == _TRANSACTION_CLOSING:
            owner.connection_closed = True
            return False
        owner.force_close = True
        raise error
    except BaseException as error:  # noqa: B036 - close unusable connections
        if isinstance(error, Exception):
            owner.force_close = True
        raise

    if not active:
        if owner.commit_attempted and not owner.rollback_attempted:
            owner.phase = _TRANSACTION_COMMITTED
            owner.outcome = _TRANSACTION_COMMITTED
        else:
            owner.phase = _TRANSACTION_ROLLED_BACK
            owner.outcome = _TRANSACTION_ROLLED_BACK
        owner.settled = True
        return True

    if owner.body_succeeded and owner.primary_error is None:
        owner.phase = _TRANSACTION_COMMITTING
        owner.commit_attempted = True
        owner.connection.commit()
        # A return value is not proof of completion; the next pass observes it.
        return False

    owner.phase = _TRANSACTION_ROLLING_BACK
    owner.rollback_attempted = True
    try:
        owner.connection.rollback()
    except BaseException as error:  # noqa: B036 - retry cancellation
        if isinstance(error, Exception):
            owner.force_close = True
        raise
    return False


def _sqlite_transaction_inner_pass(owner: _SQLiteTransactionOwner) -> bool:
    try:
        return _sqlite_transaction_pass(owner)
    except BaseException as error:  # noqa: B036 - settle before propagation
        owner.retain(error, label="SQLite transaction settlement also failed")
        return owner.settled


def _sqlite_transaction_outer_pass(owner: _SQLiteTransactionOwner) -> bool:
    """Contain cancellation at the inner runner's Python call boundary."""

    owner.require_owner_pid()
    try:
        active_error = sys.exc_info()[1]
        if active_error is not None and active_error is not owner.ambient_error:
            owner.retain(
                active_error,
                label="SQLite transaction exception dispatch was interrupted",
            )
        return _sqlite_transaction_inner_pass(owner)
    except BaseException as error:  # noqa: B036 - C iterator retries
        try:
            owner.retain(
                error,
                label="SQLite transaction settlement boundary also failed",
            )
        except BaseException:  # noqa: B036 - retain again on the next pass
            pass
        return owner.settled


def _settle_sqlite_transaction(owner: _SQLiteTransactionOwner) -> None:
    """Drive an owner to a no-active-transaction state in constant stack."""

    owner.require_owner_pid()
    deque(iter(owner.settlement_runner, True), maxlen=0)


def _sqlite_uses_legacy_autocommit(connection: sqlite3.Connection) -> bool:
    legacy = _SQLITE_LEGACY_AUTOCOMMIT
    if legacy is None:
        return True
    return connection.autocommit == legacy


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


def _exact_positive_integer(value: int, field: str) -> int:
    if type(value) is not int or value < 1:
        raise CatalogValidationError(f"{field} must be an exact positive integer")
    return value


def _positive_int64(value: int, field: str) -> int:
    normalized = _exact_positive_integer(value, field)
    if normalized > _SQLITE_INT64_MAX:
        raise CatalogValidationError(f"{field} exceeds catalog int64 range")
    return normalized


def _nonnegative_integer(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CatalogValidationError(f"{field} must be a non-negative integer")
    return value


def _nonnegative_int64(value: int, field: str) -> int:
    if type(value) is not int or value < 0:
        raise CatalogValidationError(f"{field} must be an exact non-negative integer")
    normalized = value
    if normalized > _SQLITE_INT64_MAX:
        raise CatalogValidationError(f"{field} exceeds catalog int64 range")
    return normalized


def _persisted_positive_int64(value: object, field: str) -> int:
    """Reject SQLite numeric coercions when reading an identity counter."""
    if type(value) is not int or value < 1 or value > _SQLITE_INT64_MAX:
        raise CatalogConflictError(f"{field} must be a positive 64-bit integer")
    return value


def _persisted_nonnegative_int64(value: object, field: str) -> int:
    """Reject SQLite numeric coercions for a persisted zero-based counter."""

    if type(value) is not int or value < 0 or value > _SQLITE_INT64_MAX:
        raise CatalogConflictError(f"{field} must be a non-negative 64-bit integer")
    return value


def _persisted_utc_timestamp(value: object, field: str) -> str:
    """Require the exact UTC ISO-8601 representation emitted by ``_now``."""
    try:
        return canonical_utc_timestamp(value, field)
    except StorageValidationError as exc:
        raise CatalogConflictError(str(exc)) from exc


def _freeze_job_publication_outputs(
    outputs: tuple[IndexJobViewOutput, ...],
) -> tuple[IndexJobViewOutput, ...]:
    """Detach one bounded, deterministic catalog publication closure."""

    if type(outputs) is not tuple:
        raise CatalogValidationError(
            "index job publication outputs must be an exact tuple"
        )
    if not outputs:
        raise CatalogValidationError("index job publication requires an output")
    if len(outputs) > _MAX_JOB_PUBLICATION_OUTPUTS:
        raise CatalogValidationError(
            f"index job publication cannot exceed {_MAX_JOB_PUBLICATION_OUTPUTS} outputs"
        )
    detached: list[IndexJobViewOutput] = []
    total_members = 0
    for output in outputs:
        if type(output) is not IndexJobViewOutput:
            raise CatalogValidationError(
                "index job publication outputs must be exact IndexJobViewOutput values"
            )
        frozen = IndexJobViewOutput(
            view_type=output.view_type,
            profile_id=output.profile_id,
            object_record=output.object_record,
            schema_version=output.schema_version,
            metadata_json=output.metadata_json,
            member_object_records=output.member_object_records,
        )
        total_members += len(frozen.member_object_records)
        if total_members > MAX_VIEW_GENERATION_MEMBERS:
            raise CatalogValidationError(
                "index job publication has too many aggregate member objects"
            )
        detached.append(frozen)
    ordered = tuple(sorted(detached, key=lambda output: output.view_type))
    view_types = tuple(output.view_type for output in ordered)
    if len(view_types) != len(set(view_types)):
        raise CatalogValidationError("index job publication has duplicate view type")
    bounded = snapshot_retained_import_response(
        {"outputs": [output.identity for output in ordered]},
        label="index job publication closure",
    )
    if len(canonical_json(bounded)) > RETAINED_IMPORT_RESPONSE_MAX_TEXT_CHARS:
        raise CatalogValidationError("index job publication closure is too large")
    return ordered


def _job_publication_output_identity(
    job: IndexJobRecord,
    output: IndexJobViewOutput,
) -> dict[str, Any]:
    identity = output.identity
    identity["view_generation_id"] = content_id(
        "view",
        {
            "repository_id": job.repository_id,
            "source_revision_id": job.source_revision_id,
            "profile_id": output.profile_id,
            "view_type": output.view_type,
            "object_digest": output.object_record.digest,
            "schema_version": output.schema_version,
            "metadata": output.generation_metadata,
        },
    )
    return identity


def _job_publication_snapshot_id(
    job: IndexJobRecord,
    output_identities: Sequence[Mapping[str, Any]],
) -> str:
    return content_id(
        "snapshot",
        {
            "repository_id": job.repository_id,
            "source_revision_id": job.source_revision_id,
            "views": [
                [identity["view_type"], identity["view_generation_id"]]
                for identity in output_identities
            ],
        },
    )


def _canonical_job_publication_closure(
    job: IndexJobRecord,
    *,
    owner_id: str,
    fencing_token: int,
    snapshot_id: str,
    ref_generation: int,
    ref_changed: bool,
    ref_updated_at: str,
    output_identities: Sequence[Mapping[str, Any]],
) -> tuple[str, str]:
    closure = {
        "contract": INDEX_JOB_PUBLICATION_CONTRACT,
        "job_id": job.job_id,
        "repository_id": job.repository_id,
        "source_revision_id": job.source_revision_id,
        "ref_name": job.ref_name,
        "request_digest": job.request_digest,
        "expected_ref_generation": job.expected_ref_generation,
        "owner_id": owner_id,
        "fencing_token": fencing_token,
        "snapshot_id": snapshot_id,
        "ref_generation": ref_generation,
        "ref_changed": ref_changed,
        "ref_updated_at": ref_updated_at,
        "outputs": list(output_identities),
    }
    bounded = snapshot_retained_import_response(
        closure,
        label="index job publication closure",
    )
    closure_json = canonical_json(bounded)
    if len(closure_json) > RETAINED_IMPORT_RESPONSE_MAX_TEXT_CHARS:
        raise CatalogValidationError("index job publication closure is too large")
    return closure_json, hashlib.sha256(closure_json.encode("utf-8")).hexdigest()


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

_SCHEMA_V3 = (
    """
    CREATE TRIGGER objects_reject_duplicate_inserts
    BEFORE INSERT ON objects
    WHEN EXISTS (
        SELECT 1 FROM objects AS existing
        WHERE existing.digest = NEW.digest
            OR existing.storage_key = NEW.storage_key
    )
    BEGIN
        SELECT RAISE(ABORT, 'duplicate object insert is forbidden');
    END
    """,
    """
    CREATE TRIGGER referenced_objects_cannot_be_deleted
    BEFORE DELETE ON objects
    WHEN EXISTS (
        SELECT 1 FROM view_generations AS generation
        WHERE generation.object_digest = OLD.digest
    )
    BEGIN
        SELECT RAISE(ABORT, 'referenced objects cannot be deleted');
    END
    """,
    """
    CREATE TRIGGER view_generations_reject_duplicate_inserts
    BEFORE INSERT ON view_generations
    WHEN EXISTS (
        SELECT 1 FROM view_generations AS existing
        WHERE existing.view_generation_id = NEW.view_generation_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'duplicate view generation insert is forbidden');
    END
    """,
    """
    CREATE TRIGGER referenced_view_generations_cannot_be_deleted
    BEFORE DELETE ON view_generations
    WHEN EXISTS (
        SELECT 1 FROM snapshot_views AS member
        WHERE member.view_generation_id = OLD.view_generation_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'referenced view generations cannot be deleted');
    END
    """,
    """
    CREATE TRIGGER snapshots_reject_duplicate_inserts
    BEFORE INSERT ON snapshots
    WHEN EXISTS (
        SELECT 1 FROM snapshots AS existing
        WHERE existing.snapshot_id = NEW.snapshot_id
            OR (
                existing.repository_id = NEW.repository_id
                AND existing.content_digest = NEW.content_digest
            )
    )
    BEGIN
        SELECT RAISE(ABORT, 'duplicate snapshot insert is forbidden');
    END
    """,
    """
    CREATE TRIGGER referenced_snapshots_cannot_be_deleted
    BEFORE DELETE ON snapshots
    WHEN EXISTS (
        SELECT 1 FROM refs AS ref
        WHERE ref.snapshot_id = OLD.snapshot_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'referenced snapshots cannot be deleted');
    END
    """,
    """
    CREATE TRIGGER refs_reject_duplicate_inserts
    BEFORE INSERT ON refs
    WHEN EXISTS (
        SELECT 1 FROM refs AS existing
        WHERE existing.repository_id = NEW.repository_id
            AND existing.ref_name = NEW.ref_name
    )
    BEGIN
        SELECT RAISE(ABORT, 'duplicate ref insert is forbidden');
    END
    """,
    """
    CREATE TRIGGER refs_cannot_be_deleted
    BEFORE DELETE ON refs
    BEGIN
        SELECT RAISE(ABORT, 'refs cannot be deleted');
    END
    """,
    """
    CREATE TRIGGER ref_identity_is_immutable
    BEFORE UPDATE ON refs
    WHEN
        NEW.repository_id IS NOT OLD.repository_id
        OR NEW.ref_name IS NOT OLD.ref_name
    BEGIN
        SELECT RAISE(ABORT, 'ref identity is immutable');
    END
    """,
    "DROP TRIGGER refs_require_ready_snapshot_on_insert",
    "DROP TRIGGER refs_require_ready_snapshot_on_update",
    """
    CREATE TRIGGER refs_require_ready_snapshot_on_insert
    BEFORE INSERT ON refs
    WHEN NOT EXISTS (
        SELECT 1 FROM snapshots AS snapshot
        WHERE snapshot.snapshot_id = NEW.snapshot_id
            AND snapshot.repository_id = NEW.repository_id
            AND snapshot.status = 'ready'
    )
    BEGIN
        SELECT RAISE(ABORT, 'refs may only target ready snapshots in their repository');
    END
    """,
    """
    CREATE TRIGGER refs_require_ready_snapshot_on_update
    BEFORE UPDATE ON refs
    WHEN NOT EXISTS (
        SELECT 1 FROM snapshots AS snapshot
        WHERE snapshot.snapshot_id = NEW.snapshot_id
            AND snapshot.repository_id = NEW.repository_id
            AND snapshot.status = 'ready'
    )
    BEGIN
        SELECT RAISE(ABORT, 'refs may only target ready snapshots in their repository');
    END
    """,
)

_SCHEMA_V4 = (
    """
    CREATE TABLE view_generation_objects (
        view_generation_id TEXT NOT NULL,
        object_digest TEXT NOT NULL,
        PRIMARY KEY (view_generation_id, object_digest),
        FOREIGN KEY (view_generation_id) REFERENCES view_generations(view_generation_id)
            ON DELETE CASCADE,
        FOREIGN KEY (object_digest) REFERENCES objects(digest)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX view_generation_objects_digest_idx
        ON view_generation_objects(object_digest)
    """,
    """
    CREATE TRIGGER view_generation_objects_reject_duplicate_inserts
    BEFORE INSERT ON view_generation_objects
    WHEN EXISTS (
        SELECT 1 FROM view_generation_objects AS existing
        WHERE existing.view_generation_id = NEW.view_generation_id
            AND existing.object_digest = NEW.object_digest
    )
    BEGIN
        SELECT RAISE(ABORT, 'duplicate view generation object is forbidden');
    END
    """,
    """
    CREATE TRIGGER view_generation_objects_are_immutable
    BEFORE UPDATE ON view_generation_objects
    BEGIN
        SELECT RAISE(ABORT, 'view generation objects are immutable');
    END
    """,
    """
    CREATE TRIGGER ready_view_generation_objects_cannot_be_inserted
    BEFORE INSERT ON view_generation_objects
    WHEN EXISTS (
        SELECT 1 FROM view_generations AS generation
        WHERE generation.view_generation_id = NEW.view_generation_id
            AND generation.status = 'ready'
    )
    BEGIN
        SELECT RAISE(ABORT, 'ready view generation objects are immutable');
    END
    """,
    """
    CREATE TRIGGER ready_view_generation_objects_cannot_be_deleted
    BEFORE DELETE ON view_generation_objects
    WHEN EXISTS (
        SELECT 1 FROM view_generations AS generation
        WHERE generation.view_generation_id = OLD.view_generation_id
            AND generation.status = 'ready'
    )
    BEGIN
        SELECT RAISE(ABORT, 'ready view generation objects are immutable');
    END
    """,
    """
    CREATE TRIGGER member_objects_cannot_be_deleted
    BEFORE DELETE ON objects
    WHEN EXISTS (
        SELECT 1 FROM view_generation_objects AS member
        WHERE member.object_digest = OLD.digest
    )
    BEGIN
        SELECT RAISE(ABORT, 'member objects cannot be deleted');
    END
    """,
)

_SCHEMA_V5 = (
    "DROP TRIGGER m1_index_jobs_cannot_update_succeeded",
    f"""
    CREATE TABLE index_job_publications (
        job_id TEXT PRIMARY KEY CHECK (typeof(job_id) = 'text'),
        repository_id TEXT NOT NULL CHECK (typeof(repository_id) = 'text'),
        source_revision_id TEXT NOT NULL CHECK (
            typeof(source_revision_id) = 'text'
        ),
        ref_name TEXT NOT NULL CHECK (
            typeof(ref_name) = 'text'
            AND
            length(ref_name) BETWEEN 1 AND 512 AND instr(ref_name, char(0)) = 0
        ),
        request_digest TEXT NOT NULL CHECK (typeof(request_digest) = 'text'),
        owner_id TEXT NOT NULL CHECK (
            typeof(owner_id) = 'text'
            AND
            length(owner_id) BETWEEN 1 AND 256 AND instr(owner_id, char(0)) = 0
        ),
        fencing_token INTEGER NOT NULL CHECK (
            typeof(fencing_token) = 'integer'
            AND fencing_token BETWEEN 1 AND 9223372036854775807
        ),
        expected_ref_generation INTEGER NOT NULL CHECK (
            typeof(expected_ref_generation) = 'integer'
            AND expected_ref_generation BETWEEN 0 AND 9223372036854775807
        ),
        snapshot_id TEXT NOT NULL CHECK (typeof(snapshot_id) = 'text'),
        ref_generation INTEGER NOT NULL CHECK (
            typeof(ref_generation) = 'integer'
            AND ref_generation BETWEEN 1 AND 9223372036854775807
        ),
        ref_changed INTEGER NOT NULL CHECK (
            typeof(ref_changed) = 'integer' AND ref_changed IN (0, 1)
        ),
        ref_updated_at TEXT NOT NULL CHECK (typeof(ref_updated_at) = 'text'),
        closure_digest TEXT NOT NULL UNIQUE CHECK (
            typeof(closure_digest) = 'text'
            AND length(closure_digest) = 64
            AND closure_digest NOT GLOB '*[^0-9a-f]*'
        ),
        closure_json TEXT NOT NULL CHECK (
            typeof(closure_json) = 'text'
            AND length(closure_json) BETWEEN 1 AND {RETAINED_IMPORT_RESPONSE_MAX_TEXT_CHARS}
            AND instr(closure_json, char(0)) = 0
            AND json_valid(closure_json)
            AND json_type(closure_json) IS 'object'
        ),
        completed_at_ms INTEGER NOT NULL CHECK (
            typeof(completed_at_ms) = 'integer' AND completed_at_ms >= 0
        ),
        FOREIGN KEY (job_id, repository_id, ref_name)
            REFERENCES index_jobs(job_id, repository_id, ref_name)
            ON DELETE RESTRICT,
        FOREIGN KEY (snapshot_id, source_revision_id, repository_id)
            REFERENCES snapshots(snapshot_id, source_revision_id, repository_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX index_job_publications_snapshot_idx
        ON index_job_publications(snapshot_id)
    """,
    """
    CREATE TRIGGER index_job_publications_reject_duplicate_inserts
    BEFORE INSERT ON index_job_publications
    WHEN EXISTS (
        SELECT 1 FROM index_job_publications AS existing
        WHERE existing.job_id = NEW.job_id
            OR existing.closure_digest = NEW.closure_digest
    )
    BEGIN
        SELECT RAISE(ABORT, 'duplicate index job publication is forbidden');
    END
    """,
    f"""
    CREATE TRIGGER index_job_publications_validate_insert
    BEFORE INSERT ON index_job_publications
    WHEN NOT (
        EXISTS (
            SELECT 1
            FROM index_jobs AS job
            JOIN ref_job_leases AS lease
                ON lease.repository_id = job.repository_id
                AND lease.ref_name = job.ref_name
                AND lease.job_id = job.job_id
            JOIN refs AS ref
                ON ref.repository_id = job.repository_id
                AND ref.ref_name = job.ref_name
            JOIN snapshots AS snapshot
                ON snapshot.snapshot_id = NEW.snapshot_id
                AND snapshot.repository_id = job.repository_id
                AND snapshot.source_revision_id = job.source_revision_id
            WHERE job.job_id = NEW.job_id
                AND job.repository_id = NEW.repository_id
                AND job.source_revision_id = NEW.source_revision_id
                AND job.ref_name = NEW.ref_name
                AND job.request_digest = NEW.request_digest
                AND job.expected_ref_generation = NEW.expected_ref_generation
                AND job.status = 'running'
                AND job.cancel_requested = 0
                AND NEW.completed_at_ms >= job.updated_at_ms
                AND NEW.completed_at_ms >= lease.heartbeat_at_ms
                AND NEW.completed_at_ms <= {_DB_NOW_MS_SQL}
                AND lease.owner_id = NEW.owner_id
                AND lease.fencing_token = NEW.fencing_token
                AND lease.lease_expires_at_ms > {_DB_NOW_MS_SQL}
                AND snapshot.status = 'ready'
                AND ref.snapshot_id = NEW.snapshot_id
                AND ref.generation = NEW.ref_generation
                AND ref.updated_at = NEW.ref_updated_at
                AND (
                    (NEW.ref_changed = 1
                        AND NEW.expected_ref_generation < 9223372036854775807
                        AND NEW.ref_generation = NEW.expected_ref_generation + 1)
                    OR
                    (NEW.ref_changed = 0
                        AND NEW.expected_ref_generation > 0
                        AND NEW.ref_generation = NEW.expected_ref_generation)
                )
        )
        AND (SELECT COUNT(*) FROM json_each(NEW.closure_json)) = 14
        AND json_type(NEW.closure_json, '$.contract') IS 'text'
        AND json_extract(
            NEW.closure_json, '$.contract'
        ) = {INDEX_JOB_PUBLICATION_CONTRACT!r}
        AND json_type(NEW.closure_json, '$.job_id') IS 'text'
        AND json_extract(NEW.closure_json, '$.job_id') = NEW.job_id
        AND json_type(NEW.closure_json, '$.repository_id') IS 'text'
        AND json_extract(
            NEW.closure_json, '$.repository_id'
        ) = NEW.repository_id
        AND json_type(NEW.closure_json, '$.source_revision_id') IS 'text'
        AND json_extract(
            NEW.closure_json, '$.source_revision_id'
        ) = NEW.source_revision_id
        AND json_type(NEW.closure_json, '$.ref_name') IS 'text'
        AND json_extract(NEW.closure_json, '$.ref_name') = NEW.ref_name
        AND json_type(NEW.closure_json, '$.request_digest') IS 'text'
        AND json_extract(
            NEW.closure_json, '$.request_digest'
        ) = NEW.request_digest
        AND json_type(
            NEW.closure_json, '$.expected_ref_generation'
        ) IS 'integer'
        AND json_extract(
            NEW.closure_json, '$.expected_ref_generation'
        ) = NEW.expected_ref_generation
        AND json_type(NEW.closure_json, '$.owner_id') IS 'text'
        AND json_extract(NEW.closure_json, '$.owner_id') = NEW.owner_id
        AND json_type(NEW.closure_json, '$.fencing_token') IS 'integer'
        AND json_extract(
            NEW.closure_json, '$.fencing_token'
        ) = NEW.fencing_token
        AND json_type(NEW.closure_json, '$.snapshot_id') IS 'text'
        AND json_extract(NEW.closure_json, '$.snapshot_id') = NEW.snapshot_id
        AND json_type(NEW.closure_json, '$.ref_generation') IS 'integer'
        AND json_extract(
            NEW.closure_json, '$.ref_generation'
        ) = NEW.ref_generation
        AND json_type(NEW.closure_json, '$.ref_changed') IS CASE NEW.ref_changed
            WHEN 1 THEN 'true' ELSE 'false'
        END
        AND json_extract(
            NEW.closure_json, '$.ref_changed'
        ) = NEW.ref_changed
        AND json_type(NEW.closure_json, '$.ref_updated_at') IS 'text'
        AND json_extract(
            NEW.closure_json, '$.ref_updated_at'
        ) = NEW.ref_updated_at
        AND json_type(NEW.closure_json, '$.outputs') IS 'array'
        AND json_array_length(NEW.closure_json, '$.outputs') BETWEEN 1 AND 64
        AND json_array_length(NEW.closure_json, '$.outputs') = (
            SELECT COUNT(*) FROM snapshot_views
            WHERE snapshot_id = NEW.snapshot_id
        )
        AND json_array_length(NEW.closure_json, '$.outputs') = (
            SELECT COUNT(DISTINCT json_extract(output.value, '$.view_type'))
            FROM json_each(NEW.closure_json, '$.outputs') AS output
        )
        AND (
            SELECT COALESCE(SUM(json_array_length(
                output.value, '$.member_objects'
            )), 0)
            FROM json_each(NEW.closure_json, '$.outputs') AS output
        ) <= {MAX_VIEW_GENERATION_MEMBERS}
        AND NOT EXISTS (
            SELECT 1
            FROM (
                SELECT
                    json_extract(output.value, '$.view_type') AS view_type,
                    LAG(json_extract(
                        output.value, '$.view_type'
                    )) OVER (
                        ORDER BY CAST(output.key AS INTEGER)
                    ) AS previous_view_type
                FROM json_each(
                    NEW.closure_json, '$.outputs'
                ) AS output
            ) AS ordered_outputs
            WHERE previous_view_type IS NOT NULL
                AND previous_view_type >= view_type
        )
        AND NOT EXISTS (
            SELECT 1
            FROM index_job_views AS requested
            WHERE requested.job_id = NEW.job_id
                AND requested.required = 1
                AND NOT EXISTS (
                    SELECT 1
                    FROM json_each(
                        NEW.closure_json, '$.outputs'
                    ) AS offered
                    WHERE json_extract(
                        offered.value, '$.view_type'
                    ) = requested.view_type
                )
        )
        AND NOT EXISTS (
            SELECT 1
            FROM json_each(NEW.closure_json, '$.outputs') AS output
            LEFT JOIN index_job_views AS requested
                ON requested.job_id = NEW.job_id
                AND requested.view_type = json_extract(
                    output.value, '$.view_type'
                )
            LEFT JOIN snapshot_views AS selected
                ON selected.snapshot_id = NEW.snapshot_id
                AND selected.view_type = json_extract(
                    output.value, '$.view_type'
                )
                AND selected.view_generation_id = json_extract(
                    output.value, '$.view_generation_id'
                )
            LEFT JOIN view_generations AS generation
                ON generation.view_generation_id = selected.view_generation_id
            LEFT JOIN objects AS primary_object
                ON primary_object.digest = generation.object_digest
            WHERE json_type(output.value) IS NOT 'object'
                OR (SELECT COUNT(*) FROM json_each(output.value)) != 7
                OR json_type(output.value, '$.view_type') IS NOT 'text'
                OR json_type(output.value, '$.profile_id') IS NOT 'text'
                OR json_type(
                    output.value, '$.view_generation_id'
                ) IS NOT 'text'
                OR json_type(output.value, '$.schema_version') IS NOT 'text'
                OR json_type(output.value, '$.metadata') IS NOT 'object'
                OR json_type(output.value, '$.object') IS NOT 'object'
                OR json_type(output.value, '$.member_objects') IS NOT 'array'
                OR requested.job_id IS NULL
                OR requested.profile_id != json_extract(
                    output.value, '$.profile_id'
                )
                OR selected.snapshot_id IS NULL
                OR generation.view_generation_id IS NULL
                OR generation.repository_id != NEW.repository_id
                OR generation.source_revision_id != NEW.source_revision_id
                OR generation.profile_id != json_extract(
                    output.value, '$.profile_id'
                )
                OR generation.view_type != json_extract(
                    output.value, '$.view_type'
                )
                OR generation.schema_version != json_extract(
                    output.value, '$.schema_version'
                )
                OR generation.metadata_json != json_extract(
                    output.value, '$.metadata'
                )
                OR generation.status != 'ready'
                OR primary_object.digest IS NULL
                OR (SELECT COUNT(*) FROM json_each(
                    output.value, '$.object'
                )) != 4
                OR json_type(output.value, '$.object.digest') IS NOT 'text'
                OR json_type(
                    output.value, '$.object.storage_key'
                ) IS NOT 'text'
                OR json_type(
                    output.value, '$.object.byte_size'
                ) IS NOT 'integer'
                OR json_type(
                    output.value, '$.object.media_type'
                ) IS NOT 'text'
                OR primary_object.digest != json_extract(
                    output.value, '$.object.digest'
                )
                OR primary_object.storage_key != json_extract(
                    output.value, '$.object.storage_key'
                )
                OR primary_object.byte_size != json_extract(
                    output.value, '$.object.byte_size'
                )
                OR primary_object.media_type != json_extract(
                    output.value, '$.object.media_type'
                )
                OR json_array_length(
                    output.value, '$.member_objects'
                ) > {MAX_VIEW_GENERATION_MEMBERS}
                OR json_array_length(output.value, '$.member_objects') != (
                    SELECT COUNT(*) FROM view_generation_objects AS member
                    WHERE member.view_generation_id = generation.view_generation_id
                )
                OR json_array_length(output.value, '$.member_objects') != (
                    SELECT COUNT(DISTINCT json_extract(
                        member.value, '$.digest'
                    ))
                    FROM json_each(
                        output.value, '$.member_objects'
                    ) AS member
                )
                OR EXISTS (
                    SELECT 1
                    FROM json_each(
                        output.value, '$.member_objects'
                    ) AS member
                    LEFT JOIN view_generation_objects AS membership
                        ON membership.view_generation_id = generation.view_generation_id
                        AND membership.object_digest = json_extract(
                            member.value, '$.digest'
                        )
                    LEFT JOIN objects AS member_object
                        ON member_object.digest = membership.object_digest
                    WHERE json_type(member.value) IS NOT 'object'
                        OR (SELECT COUNT(*) FROM json_each(member.value)) != 4
                        OR json_type(member.value, '$.digest') IS NOT 'text'
                        OR json_type(
                            member.value, '$.storage_key'
                        ) IS NOT 'text'
                        OR json_type(
                            member.value, '$.byte_size'
                        ) IS NOT 'integer'
                        OR json_type(
                            member.value, '$.media_type'
                        ) IS NOT 'text'
                        OR membership.object_digest IS NULL
                        OR member_object.storage_key != json_extract(
                            member.value, '$.storage_key'
                        )
                        OR member_object.byte_size != json_extract(
                            member.value, '$.byte_size'
                        )
                        OR member_object.media_type != json_extract(
                            member.value, '$.media_type'
                        )
                        OR member_object.digest = primary_object.digest
                )
                OR EXISTS (
                    SELECT 1
                    FROM (
                        SELECT
                            CAST(member.key AS INTEGER) AS member_index,
                            json_extract(
                                member.value, '$.digest'
                            ) AS member_digest,
                            LAG(CAST(member.key AS INTEGER)) OVER (
                                ORDER BY CAST(member.key AS INTEGER)
                            ) AS previous_index,
                            LAG(json_extract(
                                member.value, '$.digest'
                            )) OVER (
                                ORDER BY CAST(member.key AS INTEGER)
                            ) AS previous_digest
                        FROM json_each(
                            output.value, '$.member_objects'
                        ) AS member
                    ) AS ordered_members
                    WHERE previous_index IS NOT NULL
                        AND (
                            member_index != previous_index + 1
                            OR previous_digest >= member_digest
                        )
                )
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'index job publication closure is invalid');
    END
    """,
    """
    CREATE TRIGGER m1_index_jobs_cannot_update_succeeded
    BEFORE UPDATE ON index_jobs
    WHEN NEW.status = 'succeeded' AND NOT EXISTS (
        SELECT 1 FROM index_job_publications AS publication
        WHERE publication.job_id = NEW.job_id
            AND publication.repository_id = NEW.repository_id
            AND publication.source_revision_id = NEW.source_revision_id
            AND publication.ref_name = NEW.ref_name
            AND publication.request_digest = NEW.request_digest
            AND publication.expected_ref_generation = NEW.expected_ref_generation
            AND publication.snapshot_id = NEW.result_snapshot_id
            AND publication.completed_at_ms = NEW.finished_at_ms
    )
    BEGIN
        SELECT RAISE(
            ABORT,
            'M1 cannot persist successful index jobs without publication closure'
        );
    END
    """,
    """
    CREATE TRIGGER index_job_publication_completes_job
    AFTER INSERT ON index_job_publications
    BEGIN
        UPDATE index_jobs
        SET status = 'succeeded', result_snapshot_id = NEW.snapshot_id,
            error_code = NULL, error_message = NULL,
            finished_at_ms = NEW.completed_at_ms,
            updated_at_ms = NEW.completed_at_ms
        WHERE job_id = NEW.job_id AND status = 'running'
            AND cancel_requested = 0;
        SELECT CASE changes()
            WHEN 1 THEN 1
            ELSE RAISE(ABORT, 'index job publication did not complete its job')
        END;
        UPDATE ref_job_leases
        SET job_id = NULL, owner_id = NULL,
            acquired_at_ms = NULL, heartbeat_at_ms = NULL,
            lease_expires_at_ms = NULL, updated_at_ms = NEW.completed_at_ms
        WHERE repository_id = NEW.repository_id
            AND ref_name = NEW.ref_name
            AND job_id = NEW.job_id
            AND owner_id = NEW.owner_id
            AND fencing_token = NEW.fencing_token;
        SELECT CASE changes()
            WHEN 1 THEN 1
            ELSE RAISE(ABORT, 'index job publication did not release its lease')
        END;
    END
    """,
    """
    CREATE TRIGGER index_job_publications_are_immutable
    BEFORE UPDATE ON index_job_publications
    BEGIN
        SELECT RAISE(ABORT, 'index job publications are immutable');
    END
    """,
    """
    CREATE TRIGGER index_job_publications_cannot_be_deleted
    BEFORE DELETE ON index_job_publications
    BEGIN
        SELECT RAISE(ABORT, 'index job publications are immutable');
    END
    """,
    """
    CREATE TRIGGER published_index_jobs_cannot_be_deleted
    BEFORE DELETE ON index_jobs
    WHEN EXISTS (
        SELECT 1 FROM index_job_publications
        WHERE job_id = OLD.job_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'published index jobs are immutable');
    END
    """,
    """
    CREATE TRIGGER published_snapshots_cannot_be_deleted
    BEFORE DELETE ON snapshots
    WHEN EXISTS (
        SELECT 1 FROM index_job_publications
        WHERE snapshot_id = OLD.snapshot_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'published job snapshots are immutable');
    END
    """,
)


_V6_INDEX_JOB_PUBLICATIONS_VALIDATE_INSERT = (
    next(
        statement
        for statement in _SCHEMA_V5
        if "CREATE TRIGGER index_job_publications_validate_insert" in statement
    )
    .replace(
        f"AND NEW.completed_at_ms <= {_DB_NOW_MS_SQL}",
        """AND NEW.completed_at_ms = (
                    SELECT high_water_ms FROM index_job_execution_clock
                    WHERE singleton_id = 1
                )
                AND (SELECT COUNT(*) FROM index_job_execution_clock) = 1""",
    )
    .replace(
        f"AND lease.lease_expires_at_ms > {_DB_NOW_MS_SQL}",
        "AND lease.lease_expires_at_ms > NEW.completed_at_ms",
    )
)


_SCHEMA_V6 = (
    """
    CREATE TABLE index_job_attempt_baselines (
        job_id TEXT PRIMARY KEY CHECK (
            typeof(job_id) = 'text'
            AND length(job_id) BETWEEN 1 AND 80
            AND instr(job_id, char(0)) = 0
        ),
        legacy_attempt_count INTEGER NOT NULL CHECK (
            typeof(legacy_attempt_count) = 'integer'
            AND legacy_attempt_count BETWEEN 0 AND 1000
        ),
        initial_created_at_ms INTEGER NOT NULL CHECK (
            typeof(initial_created_at_ms) = 'integer'
            AND initial_created_at_ms BETWEEN 0 AND 9223372036854775807
        ),
        legacy_content_high_water_ms INTEGER NOT NULL CHECK (
            typeof(legacy_content_high_water_ms) = 'integer'
            AND legacy_content_high_water_ms BETWEEN 0 AND 9223372036854775807
        ),
        legacy_started_at_ms INTEGER CHECK (
            legacy_started_at_ms IS NULL OR (
                typeof(legacy_started_at_ms) = 'integer'
                AND legacy_started_at_ms BETWEEN 0 AND 9223372036854775807
            )
        ),
        CHECK (legacy_content_high_water_ms >= initial_created_at_ms),
        CHECK (
            legacy_started_at_ms IS NULL
            OR legacy_content_high_water_ms >= legacy_started_at_ms
        ),
        FOREIGN KEY (job_id) REFERENCES index_jobs(job_id) ON DELETE CASCADE
    )
    """,
    """
    INSERT INTO index_job_attempt_baselines(
        job_id, legacy_attempt_count, initial_created_at_ms,
        legacy_content_high_water_ms, legacy_started_at_ms
    )
    SELECT
        job.job_id,
        CASE
            WHEN job.attempt_count >= 1 AND EXISTS (
                SELECT 1 FROM ref_job_leases AS lease
                WHERE lease.repository_id = job.repository_id
                    AND lease.ref_name = job.ref_name
                    AND lease.job_id = job.job_id
            ) THEN job.attempt_count - 1
            ELSE job.attempt_count
        END,
        job.created_at_ms,
        job.updated_at_ms,
        job.started_at_ms
    FROM index_jobs AS job
    """,
    """
    CREATE TABLE index_job_execution_clock (
        singleton_id INTEGER PRIMARY KEY CHECK (
            typeof(singleton_id) = 'integer' AND singleton_id = 1
        ),
        high_water_ms INTEGER NOT NULL CHECK (
            typeof(high_water_ms) = 'integer'
            AND high_water_ms BETWEEN 0 AND 9223372036854775807
        )
    )
    """,
    """
    CREATE TABLE index_job_cancellation_requests (
        job_id TEXT PRIMARY KEY CHECK (
            typeof(job_id) = 'text'
            AND length(job_id) BETWEEN 1 AND 80
            AND instr(job_id, char(0)) = 0
        ),
        requested_at_ms INTEGER NOT NULL CHECK (
            typeof(requested_at_ms) = 'integer' AND requested_at_ms >= 0
        ),
        request_kind TEXT NOT NULL CHECK (
            typeof(request_kind) = 'text'
            AND request_kind IN ('legacy_v5', 'queued_v6', 'running_v6')
        ),
        attempt_count INTEGER CHECK (
            attempt_count IS NULL OR (
                typeof(attempt_count) = 'integer'
                AND attempt_count BETWEEN 1 AND 1000
            )
        ),
        owner_id TEXT CHECK (
            owner_id IS NULL OR (
                typeof(owner_id) = 'text'
                AND length(owner_id) BETWEEN 1 AND 256
                AND instr(owner_id, char(0)) = 0
            )
        ),
        fencing_token INTEGER CHECK (
            fencing_token IS NULL OR (
                typeof(fencing_token) = 'integer'
                AND fencing_token BETWEEN 1 AND 9223372036854775807
            )
        ),
        observed_heartbeat_at_ms INTEGER CHECK (
            observed_heartbeat_at_ms IS NULL OR (
                typeof(observed_heartbeat_at_ms) = 'integer'
                AND observed_heartbeat_at_ms >= 0
            )
        ),
        FOREIGN KEY (job_id) REFERENCES index_jobs(job_id) ON DELETE CASCADE,
        FOREIGN KEY (job_id, attempt_count)
            REFERENCES index_job_attempts(job_id, attempt_count)
            ON DELETE RESTRICT,
        CHECK (
            (request_kind IN ('legacy_v5', 'queued_v6')
                AND attempt_count IS NULL AND owner_id IS NULL
                AND fencing_token IS NULL
                AND observed_heartbeat_at_ms IS NULL)
            OR
            (request_kind = 'running_v6'
                AND attempt_count IS NOT NULL AND owner_id IS NOT NULL
                AND fencing_token IS NOT NULL
                AND observed_heartbeat_at_ms IS NOT NULL)
        )
    )
    """,
    """
    CREATE TABLE index_job_attempts (
        job_id TEXT NOT NULL CHECK (
            typeof(job_id) = 'text'
            AND length(job_id) BETWEEN 1 AND 80
            AND instr(job_id, char(0)) = 0
        ),
        attempt_count INTEGER NOT NULL CHECK (
            typeof(attempt_count) = 'integer'
            AND attempt_count BETWEEN 1 AND 1000
        ),
        repository_id TEXT NOT NULL CHECK (
            typeof(repository_id) = 'text'
            AND length(repository_id) BETWEEN 1 AND 96
            AND instr(repository_id, char(0)) = 0
        ),
        ref_name TEXT NOT NULL CHECK (
            typeof(ref_name) = 'text'
            AND length(ref_name) BETWEEN 1 AND 512
            AND instr(ref_name, char(0)) = 0
        ),
        request_digest TEXT NOT NULL CHECK (
            typeof(request_digest) = 'text'
            AND length(request_digest) BETWEEN 1 AND 96
            AND instr(request_digest, char(0)) = 0
        ),
        owner_id TEXT NOT NULL CHECK (
            typeof(owner_id) = 'text'
            AND length(owner_id) BETWEEN 1 AND 256
            AND instr(owner_id, char(0)) = 0
        ),
        fencing_token INTEGER NOT NULL CHECK (
            typeof(fencing_token) = 'integer'
            AND fencing_token BETWEEN 1 AND 9223372036854775807
        ),
        started_at_ms INTEGER NOT NULL CHECK (
            typeof(started_at_ms) = 'integer' AND started_at_ms >= 0
        ),
        PRIMARY KEY (job_id, attempt_count),
        UNIQUE (repository_id, ref_name, fencing_token),
        FOREIGN KEY (job_id, repository_id, ref_name)
            REFERENCES index_jobs(job_id, repository_id, ref_name)
            ON DELETE RESTRICT
    )
    """,
    """
    INSERT INTO index_job_attempts(
        job_id, attempt_count, repository_id, ref_name, request_digest,
        owner_id, fencing_token, started_at_ms
    )
    SELECT
        job.job_id, job.attempt_count, job.repository_id, job.ref_name,
        job.request_digest, lease.owner_id, lease.fencing_token,
        lease.acquired_at_ms
    FROM index_jobs AS job
    JOIN ref_job_leases AS lease
        ON lease.repository_id = job.repository_id
        AND lease.ref_name = job.ref_name
        AND lease.job_id = job.job_id
    WHERE job.attempt_count BETWEEN 1 AND job.max_attempts
        AND lease.owner_id IS NOT NULL
        AND lease.acquired_at_ms IS NOT NULL
    """,
    """
    INSERT INTO index_job_cancellation_requests(
        job_id, requested_at_ms, request_kind, attempt_count, owner_id,
        fencing_token, observed_heartbeat_at_ms
    )
    SELECT job.job_id, job.updated_at_ms, 'legacy_v5', NULL, NULL, NULL, NULL
    FROM index_jobs AS job
    WHERE job.cancel_requested = 1
    """,
    """
    CREATE TABLE index_job_attempt_completions (
        job_id TEXT NOT NULL CHECK (
            typeof(job_id) = 'text'
            AND length(job_id) BETWEEN 1 AND 80
            AND instr(job_id, char(0)) = 0
        ),
        attempt_count INTEGER NOT NULL CHECK (
            typeof(attempt_count) = 'integer'
            AND attempt_count BETWEEN 1 AND 1000
        ),
        owner_id TEXT NOT NULL CHECK (
            typeof(owner_id) = 'text'
            AND length(owner_id) BETWEEN 1 AND 256
            AND instr(owner_id, char(0)) = 0
        ),
        fencing_token INTEGER NOT NULL CHECK (
            typeof(fencing_token) = 'integer'
            AND fencing_token BETWEEN 1 AND 9223372036854775807
        ),
        outcome TEXT NOT NULL CHECK (
            typeof(outcome) = 'text'
            AND outcome IN ('requeue', 'failed', 'cancelled')
        ),
        error_code TEXT NOT NULL CHECK (
            typeof(error_code) = 'text'
            AND length(error_code) BETWEEN 1 AND 128
            AND instr(error_code, char(0)) = 0
        ),
        error_message TEXT CHECK (
            error_message IS NULL OR (
                typeof(error_message) = 'text'
                AND length(error_message) BETWEEN 1 AND 4096
                AND instr(error_message, char(0)) = 0
            )
        ),
        completed_at_ms INTEGER NOT NULL CHECK (
            typeof(completed_at_ms) = 'integer' AND completed_at_ms >= 0
        ),
        PRIMARY KEY (job_id, attempt_count),
        FOREIGN KEY (job_id, attempt_count)
            REFERENCES index_job_attempts(job_id, attempt_count)
            ON DELETE RESTRICT
    )
    """,
    """
    INSERT INTO index_job_attempt_completions(
        job_id, attempt_count, owner_id, fencing_token, outcome,
        error_code, error_message, completed_at_ms
    )
    SELECT
        job.job_id,
        job.attempt_count,
        lease.owner_id,
        lease.fencing_token,
        CASE job.status
            WHEN 'queued' THEN 'requeue'
            WHEN 'failed' THEN 'failed'
            ELSE 'cancelled'
        END,
        job.error_code,
        job.error_message,
        job.updated_at_ms
    FROM index_jobs AS job
    JOIN ref_job_leases AS lease
        ON lease.repository_id = job.repository_id
        AND lease.ref_name = job.ref_name
        AND lease.job_id = job.job_id
    WHERE job.status IN ('queued', 'failed', 'cancelled')
        AND job.attempt_count BETWEEN 1 AND job.max_attempts
        AND job.error_code IS NOT NULL
        AND job.result_snapshot_id IS NULL
        AND job.started_at_ms IS NOT NULL
        AND job.updated_at_ms >= lease.updated_at_ms
        AND job.updated_at_ms >= lease.heartbeat_at_ms
        AND (
            (job.status = 'queued'
                AND job.cancel_requested = 0
                AND job.finished_at_ms IS NULL
                AND job.attempt_count < job.max_attempts)
            OR
            (job.status = 'failed'
                AND job.cancel_requested = 0
                AND job.finished_at_ms = job.updated_at_ms)
            OR
            (job.status = 'cancelled'
                AND job.cancel_requested = 1
                AND job.finished_at_ms = job.updated_at_ms)
        )
    """,
    """
    UPDATE ref_job_leases
    SET job_id = NULL,
        owner_id = NULL,
        acquired_at_ms = NULL,
        heartbeat_at_ms = NULL,
        lease_expires_at_ms = NULL,
        updated_at_ms = (
            SELECT completion.completed_at_ms
            FROM index_job_attempt_completions AS completion
            WHERE completion.job_id = ref_job_leases.job_id
        )
    WHERE job_id IN (
        SELECT completion.job_id
        FROM index_job_attempt_completions AS completion
    )
    """,
    """
    UPDATE ref_job_leases
    SET job_id = NULL,
        owner_id = NULL,
        acquired_at_ms = NULL,
        heartbeat_at_ms = NULL,
        lease_expires_at_ms = NULL,
        updated_at_ms = (
            SELECT publication.completed_at_ms
            FROM index_job_publications AS publication
            WHERE publication.job_id = ref_job_leases.job_id
        )
    WHERE EXISTS (
        SELECT 1
        FROM index_job_publications AS publication
        WHERE publication.job_id = ref_job_leases.job_id
            AND publication.owner_id = ref_job_leases.owner_id
            AND publication.fencing_token = ref_job_leases.fencing_token
            AND publication.completed_at_ms >= ref_job_leases.updated_at_ms
    )
    """,
    f"""
    CREATE TABLE index_job_events (
        event_sequence INTEGER PRIMARY KEY AUTOINCREMENT CHECK (
            typeof(event_sequence) = 'integer'
            AND event_sequence BETWEEN 1 AND 9223372036854775807
        ),
        job_id TEXT NOT NULL CHECK (
            typeof(job_id) = 'text'
            AND length(job_id) BETWEEN 1 AND 80
            AND instr(job_id, char(0)) = 0
        ),
        attempt_count INTEGER NOT NULL CHECK (
            typeof(attempt_count) = 'integer'
            AND attempt_count BETWEEN 1 AND 1000
        ),
        event_key TEXT NOT NULL CHECK (
            typeof(event_key) = 'text'
            AND length(event_key) BETWEEN 1 AND 128
            AND instr(event_key, char(0)) = 0
        ),
        kind TEXT NOT NULL CHECK (
            typeof(kind) = 'text' AND kind IN ('progress', 'view_result')
        ),
        owner_id TEXT NOT NULL CHECK (
            typeof(owner_id) = 'text'
            AND length(owner_id) BETWEEN 1 AND 256
            AND instr(owner_id, char(0)) = 0
        ),
        fencing_token INTEGER NOT NULL CHECK (
            typeof(fencing_token) = 'integer'
            AND fencing_token BETWEEN 1 AND 9223372036854775807
        ),
        view_type TEXT CHECK (
            view_type IS NULL OR (
                typeof(view_type) = 'text'
                AND length(view_type) BETWEEN 1 AND 128
                AND instr(view_type, char(0)) = 0
            )
        ),
        effective_mode TEXT CHECK (
            effective_mode IS NULL OR (
                typeof(effective_mode) = 'text'
                AND effective_mode IN (
                    'full', 'incremental', 'rebuild_fallback', 'unavailable'
                )
            )
        ),
        outcome TEXT CHECK (
            outcome IS NULL OR (
                typeof(outcome) = 'text'
                AND outcome IN ('succeeded', 'failed', 'skipped')
            )
        ),
        payload_json TEXT NOT NULL CHECK (
            typeof(payload_json) = 'text'
            AND length(payload_json) BETWEEN 2 AND {INDEX_JOB_EVENT_PAYLOAD_MAX_TEXT_CHARS}
            AND instr(payload_json, char(0)) = 0
            AND json_valid(payload_json)
            AND json_type(payload_json) = 'object'
        ),
        created_at_ms INTEGER NOT NULL CHECK (
            typeof(created_at_ms) = 'integer' AND created_at_ms >= 0
        ),
        UNIQUE (job_id, attempt_count, event_key),
        FOREIGN KEY (job_id, attempt_count)
            REFERENCES index_job_attempts(job_id, attempt_count)
            ON DELETE RESTRICT,
        CHECK (
            (kind = 'progress' AND effective_mode IS NULL AND outcome IS NULL)
            OR
            (kind = 'view_result' AND view_type IS NOT NULL
                AND effective_mode IS NOT NULL AND outcome IS NOT NULL)
        )
    )
    """,
    f"""
    CREATE TABLE index_job_attempt_closure_frontiers (
        job_id TEXT NOT NULL CHECK (
            typeof(job_id) = 'text'
            AND length(job_id) BETWEEN 1 AND 80
            AND instr(job_id, char(0)) = 0
        ),
        attempt_count INTEGER NOT NULL CHECK (
            typeof(attempt_count) = 'integer'
            AND attempt_count BETWEEN 1 AND 1000
        ),
        owner_id TEXT NOT NULL CHECK (
            typeof(owner_id) = 'text'
            AND length(owner_id) BETWEEN 1 AND 256
            AND instr(owner_id, char(0)) = 0
        ),
        fencing_token INTEGER NOT NULL CHECK (
            typeof(fencing_token) = 'integer'
            AND fencing_token BETWEEN 1 AND 9223372036854775807
        ),
        event_count INTEGER NOT NULL CHECK (
            typeof(event_count) = 'integer'
            AND event_count BETWEEN 0 AND {MAX_INDEX_JOB_EVENTS_PER_ATTEMPT}
        ),
        max_event_sequence INTEGER NOT NULL CHECK (
            typeof(max_event_sequence) = 'integer'
            AND max_event_sequence BETWEEN 0 AND 9223372036854775807
        ),
        max_event_created_at_ms INTEGER NOT NULL CHECK (
            typeof(max_event_created_at_ms) = 'integer'
            AND max_event_created_at_ms >= 0
        ),
        PRIMARY KEY (job_id, attempt_count),
        FOREIGN KEY (job_id, attempt_count)
            REFERENCES index_job_attempts(job_id, attempt_count)
            ON DELETE RESTRICT,
        CHECK (
            (event_count = 0 AND max_event_sequence = 0
                AND max_event_created_at_ms = 0)
            OR
            (event_count BETWEEN 1 AND {MAX_INDEX_JOB_EVENTS_PER_ATTEMPT}
                AND max_event_sequence >= 1)
        )
    )
    """,
    """
    INSERT INTO index_job_attempt_closure_frontiers(
        job_id, attempt_count, owner_id, fencing_token, event_count,
        max_event_sequence, max_event_created_at_ms
    )
    SELECT
        attempt.job_id, attempt.attempt_count, attempt.owner_id,
        attempt.fencing_token, 0, 0, 0
    FROM index_job_attempts AS attempt
    WHERE EXISTS (
        SELECT 1 FROM index_job_attempt_completions AS completion
        WHERE completion.job_id = attempt.job_id
            AND completion.attempt_count = attempt.attempt_count
            AND completion.owner_id = attempt.owner_id
            AND completion.fencing_token = attempt.fencing_token
    ) OR EXISTS (
        SELECT 1 FROM index_job_publications AS publication
        WHERE publication.job_id = attempt.job_id
            AND publication.owner_id = attempt.owner_id
            AND publication.fencing_token = attempt.fencing_token
    )
    """,
    f"""
    INSERT INTO index_job_execution_clock(singleton_id, high_water_ms)
    SELECT 1, COALESCE(MAX(evidence_at_ms), 0)
    FROM ({_INDEX_JOB_EXECUTION_WITNESS_SQL})
    """,
    """
    CREATE INDEX index_jobs_runnable_idx
        ON index_jobs(status, created_at_ms, job_id)
        WHERE status IN ('queued', 'running')
    """,
    """
    CREATE INDEX index_job_events_job_sequence_idx
        ON index_job_events(job_id, event_sequence)
    """,
    """
    CREATE UNIQUE INDEX index_job_events_view_result_idx
        ON index_job_events(job_id, attempt_count, view_type)
        WHERE kind = 'view_result'
    """,
    f"""
    CREATE TRIGGER index_jobs_v6_initial_state_is_canonical
    BEFORE INSERT ON index_jobs
    WHEN NOT (
        NEW.status = 'queued'
        AND NEW.cancel_requested = 0
        AND NEW.attempt_count = 0
        AND NEW.result_snapshot_id IS NULL
        AND NEW.error_code IS NULL
        AND NEW.error_message IS NULL
        AND NEW.started_at_ms IS NULL
        AND NEW.finished_at_ms IS NULL
        AND NEW.created_at_ms = NEW.updated_at_ms
        AND (SELECT COUNT(*) FROM index_job_execution_clock) = 1
        AND NEW.created_at_ms = (
            SELECT high_water_ms FROM index_job_execution_clock
            WHERE singleton_id = 1
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'initial v6 index job state is invalid');
    END
    """,
    """
    CREATE TRIGGER index_job_execution_clock_rejects_inserts
    BEFORE INSERT ON index_job_execution_clock
    BEGIN
        SELECT RAISE(ABORT, 'index job execution clock is a singleton');
    END
    """,
    f"""
    CREATE TRIGGER index_job_execution_clock_advances_canonically
    BEFORE UPDATE ON index_job_execution_clock
    WHEN NOT (
        typeof(OLD.singleton_id) = 'integer'
        AND typeof(NEW.singleton_id) = 'integer'
        AND typeof(OLD.high_water_ms) = 'integer'
        AND typeof(NEW.high_water_ms) = 'integer'
        AND OLD.singleton_id = 1
        AND NEW.singleton_id = OLD.singleton_id
        AND NEW.high_water_ms >= OLD.high_water_ms
        AND NEW.high_water_ms = {_DB_NOW_MS_SQL}
    )
    BEGIN
        SELECT RAISE(ABORT, 'index job execution clock update is invalid');
    END
    """,
    """
    CREATE TRIGGER index_job_execution_clock_cannot_be_deleted
    BEFORE DELETE ON index_job_execution_clock
    BEGIN
        SELECT RAISE(ABORT, 'index job execution clock is a singleton');
    END
    """,
    """
    CREATE TRIGGER index_job_attempt_baselines_reject_duplicate_inserts
    BEFORE INSERT ON index_job_attempt_baselines
    WHEN EXISTS (
        SELECT 1 FROM index_job_attempt_baselines AS existing
        WHERE existing.job_id = NEW.job_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'duplicate index job attempt baseline is forbidden');
    END
    """,
    """
    CREATE TRIGGER index_job_attempt_baselines_validate_insert
    BEFORE INSERT ON index_job_attempt_baselines
    WHEN NOT EXISTS (
        SELECT 1 FROM index_jobs AS job
        WHERE job.job_id = NEW.job_id
            AND job.status = 'queued'
            AND job.attempt_count = 0
            AND NEW.legacy_attempt_count = 0
            AND NEW.initial_created_at_ms = job.created_at_ms
            AND NEW.legacy_content_high_water_ms = job.created_at_ms
            AND NEW.legacy_started_at_ms IS NULL
    )
    BEGIN
        SELECT RAISE(ABORT, 'index job attempt baseline is invalid');
    END
    """,
    """
    CREATE TRIGGER index_job_attempt_baselines_are_immutable
    BEFORE UPDATE ON index_job_attempt_baselines
    BEGIN
        SELECT RAISE(ABORT, 'index job attempt baselines are immutable');
    END
    """,
    """
    CREATE TRIGGER index_job_attempt_baselines_cannot_be_deleted
    BEFORE DELETE ON index_job_attempt_baselines
    BEGIN
        SELECT RAISE(ABORT, 'index job attempt baselines are immutable');
    END
    """,
    """
    CREATE TRIGGER index_jobs_with_execution_history_cannot_be_deleted
    BEFORE DELETE ON index_jobs
    WHEN NOT EXISTS (
        SELECT 1 FROM index_job_publications AS publication
        WHERE publication.job_id = OLD.job_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'index job view requests are immutable');
    END
    """,
    """
    CREATE TRIGGER index_jobs_create_attempt_baseline
    AFTER INSERT ON index_jobs
    BEGIN
        INSERT INTO index_job_attempt_baselines(
            job_id, legacy_attempt_count, initial_created_at_ms,
            legacy_content_high_water_ms, legacy_started_at_ms
        ) VALUES (
            NEW.job_id, 0, NEW.created_at_ms, NEW.created_at_ms, NULL
        );
    END
    """,
    """
    CREATE TRIGGER index_job_attempt_frontiers_reject_duplicate_inserts
    BEFORE INSERT ON index_job_attempt_closure_frontiers
    WHEN EXISTS (
        SELECT 1 FROM index_job_attempt_closure_frontiers AS existing
        WHERE existing.job_id = NEW.job_id
            AND existing.attempt_count = NEW.attempt_count
    )
    BEGIN
        SELECT RAISE(ABORT, 'duplicate job attempt closure frontier is forbidden');
    END
    """,
    """
    CREATE TRIGGER index_job_attempt_frontiers_validate_insert
    BEFORE INSERT ON index_job_attempt_closure_frontiers
    WHEN NOT EXISTS (
        SELECT 1
        FROM index_job_attempts AS attempt
        WHERE attempt.job_id = NEW.job_id
            AND attempt.attempt_count = NEW.attempt_count
            AND attempt.owner_id = NEW.owner_id
            AND attempt.fencing_token = NEW.fencing_token
            AND NEW.event_count = (
                SELECT COUNT(*) FROM index_job_events AS event
                WHERE event.job_id = attempt.job_id
                    AND event.attempt_count = attempt.attempt_count
            )
            AND NEW.max_event_sequence = COALESCE((
                SELECT MAX(event.event_sequence) FROM index_job_events AS event
                WHERE event.job_id = attempt.job_id
                    AND event.attempt_count = attempt.attempt_count
            ), 0)
            AND NEW.max_event_created_at_ms = COALESCE((
                SELECT MAX(event.created_at_ms) FROM index_job_events AS event
                WHERE event.job_id = attempt.job_id
                    AND event.attempt_count = attempt.attempt_count
            ), 0)
            AND (
                EXISTS (
                    SELECT 1 FROM index_job_attempt_completions AS completion
                    WHERE completion.job_id = attempt.job_id
                        AND completion.attempt_count = attempt.attempt_count
                        AND completion.owner_id = attempt.owner_id
                        AND completion.fencing_token = attempt.fencing_token
                )
                OR EXISTS (
                    SELECT 1 FROM index_job_publications AS publication
                    WHERE publication.job_id = attempt.job_id
                        AND publication.owner_id = attempt.owner_id
                        AND publication.fencing_token = attempt.fencing_token
                )
            )
    )
    BEGIN
        SELECT RAISE(ABORT, 'index job attempt closure frontier is invalid');
    END
    """,
    """
    CREATE TRIGGER index_job_attempt_frontiers_are_immutable
    BEFORE UPDATE ON index_job_attempt_closure_frontiers
    BEGIN
        SELECT RAISE(ABORT, 'index job attempt closure frontiers are immutable');
    END
    """,
    """
    CREATE TRIGGER index_job_attempt_frontiers_cannot_be_deleted
    BEFORE DELETE ON index_job_attempt_closure_frontiers
    BEGIN
        SELECT RAISE(ABORT, 'index job attempt closure frontiers are immutable');
    END
    """,
    """
    CREATE TRIGGER index_job_cancellation_requests_reject_duplicate_inserts
    BEFORE INSERT ON index_job_cancellation_requests
    WHEN EXISTS (
        SELECT 1 FROM index_job_cancellation_requests AS existing
        WHERE existing.job_id = NEW.job_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'duplicate index job cancellation request is forbidden');
    END
    """,
    f"""
    CREATE TRIGGER index_job_cancellation_requests_validate_insert
    BEFORE INSERT ON index_job_cancellation_requests
    WHEN NOT EXISTS (
        SELECT 1 FROM index_jobs AS job
        WHERE job.job_id = NEW.job_id
            AND job.cancel_requested = 1
            AND NEW.requested_at_ms = job.updated_at_ms
            AND (SELECT COUNT(*) FROM index_job_execution_clock) = 1
            AND NEW.requested_at_ms = (
                SELECT high_water_ms FROM index_job_execution_clock
                WHERE singleton_id = 1
            )
            AND (
                (
                    NEW.request_kind = 'queued_v6'
                    AND job.status = 'cancelled'
                    AND NEW.attempt_count IS NULL
                    AND NEW.owner_id IS NULL
                    AND NEW.fencing_token IS NULL
                    AND NEW.observed_heartbeat_at_ms IS NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM ref_job_leases AS lease
                        WHERE lease.job_id = job.job_id
                    )
                )
                OR EXISTS (
                    SELECT 1
                    FROM index_job_attempts AS attempt
                    JOIN ref_job_leases AS lease
                        ON lease.repository_id = attempt.repository_id
                        AND lease.ref_name = attempt.ref_name
                        AND lease.job_id = attempt.job_id
                    WHERE NEW.request_kind = 'running_v6'
                        AND job.status = 'running'
                        AND attempt.job_id = job.job_id
                        AND attempt.attempt_count = job.attempt_count
                        AND NEW.attempt_count = attempt.attempt_count
                        AND NEW.owner_id = attempt.owner_id
                        AND NEW.fencing_token = attempt.fencing_token
                        AND lease.owner_id = attempt.owner_id
                        AND lease.fencing_token = attempt.fencing_token
                        AND NEW.observed_heartbeat_at_ms = lease.heartbeat_at_ms
                        AND NEW.requested_at_ms >= lease.heartbeat_at_ms
                        AND NEW.requested_at_ms >= COALESCE((
                            SELECT MAX(event.created_at_ms)
                            FROM index_job_events AS event
                            WHERE event.job_id = attempt.job_id
                                AND event.attempt_count = attempt.attempt_count
                        ), attempt.started_at_ms)
                )
            )
    )
    BEGIN
        SELECT RAISE(ABORT, 'index job cancellation request is invalid');
    END
    """,
    """
    CREATE TRIGGER index_job_cancellation_requests_are_immutable
    BEFORE UPDATE ON index_job_cancellation_requests
    BEGIN
        SELECT RAISE(ABORT, 'index job cancellation requests are immutable');
    END
    """,
    """
    CREATE TRIGGER index_job_cancellation_requests_cannot_be_deleted
    BEFORE DELETE ON index_job_cancellation_requests
    BEGIN
        SELECT RAISE(ABORT, 'index job cancellation requests are immutable');
    END
    """,
    """
    CREATE TRIGGER index_job_cancellation_is_recorded
    AFTER UPDATE OF cancel_requested ON index_jobs
    WHEN OLD.cancel_requested = 0 AND NEW.cancel_requested = 1
    BEGIN
        INSERT INTO index_job_cancellation_requests(
            job_id, requested_at_ms, request_kind, attempt_count, owner_id,
            fencing_token, observed_heartbeat_at_ms
        ) VALUES (
            NEW.job_id,
            NEW.updated_at_ms,
            CASE
                WHEN NEW.status = 'running' THEN 'running_v6'
                ELSE 'queued_v6'
            END,
            CASE WHEN NEW.status = 'running' THEN NEW.attempt_count END,
            CASE WHEN NEW.status = 'running' THEN (
                SELECT attempt.owner_id FROM index_job_attempts AS attempt
                WHERE attempt.job_id = NEW.job_id
                    AND attempt.attempt_count = NEW.attempt_count
            ) END,
            CASE WHEN NEW.status = 'running' THEN (
                SELECT attempt.fencing_token FROM index_job_attempts AS attempt
                WHERE attempt.job_id = NEW.job_id
                    AND attempt.attempt_count = NEW.attempt_count
            ) END,
            CASE WHEN NEW.status = 'running' THEN (
                SELECT lease.heartbeat_at_ms
                FROM index_job_attempts AS attempt
                JOIN ref_job_leases AS lease
                    ON lease.repository_id = attempt.repository_id
                    AND lease.ref_name = attempt.ref_name
                    AND lease.job_id = attempt.job_id
                    AND lease.owner_id = attempt.owner_id
                    AND lease.fencing_token = attempt.fencing_token
                WHERE attempt.job_id = NEW.job_id
                    AND attempt.attempt_count = NEW.attempt_count
            ) END
        );
    END
    """,
    """
    DROP TRIGGER ref_job_lease_insert_fencing
    """,
    f"""
    CREATE TRIGGER ref_job_lease_insert_fencing
    BEFORE INSERT ON ref_job_leases
    WHEN NOT (
        NEW.job_id IS NOT NULL
        AND NEW.owner_id IS NOT NULL
        AND NEW.fencing_token = 1
        AND (SELECT COUNT(*) FROM index_job_execution_clock) = 1
        AND NEW.acquired_at_ms = (
            SELECT high_water_ms FROM index_job_execution_clock
            WHERE singleton_id = 1
        )
        AND NEW.heartbeat_at_ms = NEW.acquired_at_ms
        AND NEW.updated_at_ms = NEW.acquired_at_ms
        AND NEW.lease_expires_at_ms > NEW.heartbeat_at_ms
        AND NEW.lease_expires_at_ms <= CASE
            WHEN NEW.acquired_at_ms > 9223372034707292160
                THEN 9223372036854775807
            ELSE NEW.acquired_at_ms + 2147483647
        END
        AND EXISTS (
            SELECT 1 FROM index_jobs AS job
            WHERE job.job_id = NEW.job_id
                AND job.repository_id = NEW.repository_id
                AND job.ref_name = NEW.ref_name
                AND job.status = 'queued'
                AND job.cancel_requested = 0
                AND NEW.acquired_at_ms >= job.updated_at_ms
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'initial ref job lease state is invalid');
    END
    """,
    """
    DROP TRIGGER ref_job_lease_updates_are_fenced
    """,
    f"""
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
                AND OLD.updated_at_ms = OLD.heartbeat_at_ms
                AND OLD.lease_expires_at_ms < 9223372036854775807
                AND OLD.lease_expires_at_ms > {_DB_NOW_MS_SQL}
                AND NEW.heartbeat_at_ms = MAX(
                    OLD.heartbeat_at_ms, {_DB_NOW_MS_SQL}
                )
                AND NEW.updated_at_ms = MAX(
                    OLD.updated_at_ms, {_DB_NOW_MS_SQL}
                )
                AND NEW.updated_at_ms = NEW.heartbeat_at_ms
                AND NEW.lease_expires_at_ms > OLD.lease_expires_at_ms
                AND NEW.lease_expires_at_ms > NEW.heartbeat_at_ms
                AND NEW.lease_expires_at_ms <= CASE
                    WHEN {_DB_NOW_MS_SQL} > 9223372034707292160
                        THEN 9223372036854775807
                    ELSE MAX(
                        OLD.lease_expires_at_ms + 1,
                        {_DB_NOW_MS_SQL} + 2147483647
                    )
                END
                AND (
                    SELECT status FROM index_jobs WHERE job_id = NEW.job_id
                ) = 'running'
            )
            OR
            (
                OLD.job_id IS NOT NULL AND NEW.job_id IS NULL
                AND NEW.fencing_token = OLD.fencing_token
                AND (SELECT COUNT(*) FROM index_job_execution_clock) = 1
                AND NEW.updated_at_ms = (
                    SELECT high_water_ms FROM index_job_execution_clock
                    WHERE singleton_id = 1
                )
                AND (
                    SELECT status FROM index_jobs WHERE job_id = OLD.job_id
                ) != 'running'
            )
            OR
            (
                OLD.job_id IS NULL AND NEW.job_id IS NOT NULL
                AND NEW.fencing_token = OLD.fencing_token + 1
                AND OLD.fencing_token >= 1
                AND (SELECT COUNT(*) FROM index_job_execution_clock) = 1
                AND OLD.updated_at_ms <= (
                    SELECT high_water_ms FROM index_job_execution_clock
                    WHERE singleton_id = 1
                )
                AND NEW.acquired_at_ms = (
                    SELECT high_water_ms FROM index_job_execution_clock
                    WHERE singleton_id = 1
                )
                AND NEW.heartbeat_at_ms = NEW.acquired_at_ms
                AND NEW.updated_at_ms = NEW.acquired_at_ms
                AND NEW.lease_expires_at_ms > NEW.heartbeat_at_ms
                AND NEW.lease_expires_at_ms <= CASE
                    WHEN NEW.acquired_at_ms > 9223372034707292160
                        THEN 9223372036854775807
                    ELSE NEW.acquired_at_ms + 2147483647
                END
                AND (
                    SELECT status FROM index_jobs WHERE job_id = NEW.job_id
                ) = 'queued'
                AND (
                    SELECT cancel_requested FROM index_jobs
                    WHERE job_id = NEW.job_id
                ) = 0
                AND NEW.acquired_at_ms >= (
                    SELECT updated_at_ms FROM index_jobs
                    WHERE job_id = NEW.job_id
                )
            )
            OR
            (
                OLD.job_id IS NOT NULL AND NEW.job_id IS NOT NULL
                AND NEW.fencing_token = OLD.fencing_token + 1
                AND (SELECT COUNT(*) FROM index_job_execution_clock) = 1
                AND OLD.lease_expires_at_ms <= (
                    SELECT high_water_ms FROM index_job_execution_clock
                    WHERE singleton_id = 1
                )
                AND NEW.acquired_at_ms = (
                    SELECT high_water_ms FROM index_job_execution_clock
                    WHERE singleton_id = 1
                )
                AND NEW.heartbeat_at_ms = NEW.acquired_at_ms
                AND NEW.updated_at_ms = NEW.acquired_at_ms
                AND NEW.lease_expires_at_ms > NEW.heartbeat_at_ms
                AND NEW.lease_expires_at_ms <= CASE
                    WHEN NEW.acquired_at_ms > 9223372034707292160
                        THEN 9223372036854775807
                    ELSE NEW.acquired_at_ms + 2147483647
                END
                AND (
                    SELECT status FROM index_jobs WHERE job_id = OLD.job_id
                ) != 'running'
                AND (
                    SELECT status FROM index_jobs WHERE job_id = NEW.job_id
                ) = 'queued'
                AND (
                    SELECT cancel_requested FROM index_jobs
                    WHERE job_id = NEW.job_id
                ) = 0
                AND NEW.acquired_at_ms >= (
                    SELECT updated_at_ms FROM index_jobs
                    WHERE job_id = NEW.job_id
                )
            )
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'ref job lease fencing transition is invalid');
    END
    """,
    """
    DROP TRIGGER index_job_status_transitions_are_valid
    """,
    f"""
    CREATE TRIGGER index_job_status_transitions_are_valid
    BEFORE UPDATE ON index_jobs
    WHEN NOT (
        (
            OLD.status = NEW.status
            AND NEW.attempt_count = OLD.attempt_count
            AND (
                (
                    NEW.cancel_requested = OLD.cancel_requested
                    AND NEW.updated_at_ms = OLD.updated_at_ms
                )
                OR (
                    OLD.status = 'running'
                    AND OLD.cancel_requested = 0
                    AND NEW.cancel_requested = 1
                    AND (SELECT COUNT(*) FROM index_job_execution_clock) = 1
                    AND NEW.updated_at_ms = (
                        SELECT high_water_ms FROM index_job_execution_clock
                        WHERE singleton_id = 1
                    )
                )
            )
            AND NEW.result_snapshot_id IS OLD.result_snapshot_id
            AND NEW.error_code IS OLD.error_code
            AND NEW.error_message IS OLD.error_message
            AND NEW.started_at_ms IS OLD.started_at_ms
            AND NEW.finished_at_ms IS OLD.finished_at_ms
        )
        OR
        (
            OLD.status = 'queued'
            AND NEW.status = 'running'
            AND OLD.cancel_requested = 0
            AND NEW.cancel_requested = 0
            AND NEW.attempt_count = OLD.attempt_count + 1
            AND NEW.result_snapshot_id IS NULL
            AND NEW.error_code IS NULL
            AND NEW.error_message IS NULL
            AND NEW.finished_at_ms IS NULL
            AND NEW.updated_at_ms >= OLD.updated_at_ms
            AND EXISTS (
                SELECT 1
                FROM index_job_attempts AS attempt
                JOIN ref_job_leases AS lease
                    ON lease.repository_id = attempt.repository_id
                    AND lease.ref_name = attempt.ref_name
                    AND lease.job_id = attempt.job_id
                    AND lease.owner_id = attempt.owner_id
                    AND lease.fencing_token = attempt.fencing_token
                WHERE attempt.job_id = NEW.job_id
                    AND attempt.attempt_count = NEW.attempt_count
                    AND attempt.repository_id = NEW.repository_id
                    AND attempt.ref_name = NEW.ref_name
                    AND attempt.request_digest = NEW.request_digest
                    AND attempt.started_at_ms = lease.acquired_at_ms
                    AND NEW.started_at_ms = COALESCE(
                        OLD.started_at_ms, attempt.started_at_ms
                    )
                    AND NEW.updated_at_ms = attempt.started_at_ms
                    AND (SELECT COUNT(*) FROM index_job_execution_clock) = 1
                    AND attempt.started_at_ms = (
                        SELECT high_water_ms FROM index_job_execution_clock
                        WHERE singleton_id = 1
                    )
            )
        )
        OR
        (
            OLD.status = 'queued'
            AND NEW.status = 'cancelled'
            AND NEW.attempt_count = OLD.attempt_count
            AND NEW.cancel_requested = 1
            AND NEW.result_snapshot_id IS NULL
            AND NEW.error_code = 'cancelled'
            AND NEW.error_message IS NULL
            AND NEW.started_at_ms IS OLD.started_at_ms
            AND NEW.finished_at_ms = NEW.updated_at_ms
            AND NEW.updated_at_ms >= OLD.updated_at_ms
            AND (SELECT COUNT(*) FROM index_job_execution_clock) = 1
            AND NEW.updated_at_ms = (
                SELECT high_water_ms FROM index_job_execution_clock
                WHERE singleton_id = 1
            )
            AND NOT EXISTS (
                SELECT 1 FROM ref_job_leases AS lease
                WHERE lease.job_id = NEW.job_id
            )
        )
        OR
        (
            OLD.status = 'running'
            AND NEW.status IN ('queued', 'failed', 'cancelled')
            AND NEW.attempt_count = OLD.attempt_count
            AND NEW.cancel_requested = OLD.cancel_requested
            AND NEW.result_snapshot_id IS NULL
            AND NEW.started_at_ms IS OLD.started_at_ms
            AND EXISTS (
                SELECT 1
                FROM index_job_attempt_completions AS completion
                WHERE completion.job_id = NEW.job_id
                    AND completion.attempt_count = NEW.attempt_count
                    AND completion.outcome = CASE NEW.status
                        WHEN 'queued' THEN 'requeue'
                        WHEN 'failed' THEN 'failed'
                        ELSE 'cancelled'
                    END
                    AND completion.error_code = NEW.error_code
                    AND completion.error_message IS NEW.error_message
                    AND completion.completed_at_ms = NEW.updated_at_ms
                    AND NEW.finished_at_ms IS CASE NEW.status
                        WHEN 'queued' THEN NULL
                        ELSE completion.completed_at_ms
                    END
            )
        )
        OR
        (
            OLD.status = 'running'
            AND NEW.status = 'succeeded'
            AND OLD.cancel_requested = 0
            AND NEW.cancel_requested = 0
            AND NEW.attempt_count = OLD.attempt_count
            AND NEW.error_code IS NULL
            AND NEW.error_message IS NULL
            AND NEW.started_at_ms IS OLD.started_at_ms
            AND NEW.finished_at_ms = NEW.updated_at_ms
            AND EXISTS (
                SELECT 1 FROM index_job_publications AS publication
                WHERE publication.job_id = NEW.job_id
                    AND publication.snapshot_id = NEW.result_snapshot_id
                    AND publication.completed_at_ms = NEW.finished_at_ms
            )
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid index job status transition');
    END
    """,
    """
    CREATE TRIGGER index_job_cancellation_is_monotonic
    BEFORE UPDATE ON index_jobs
    WHEN OLD.cancel_requested = 1 AND NEW.cancel_requested = 0
    BEGIN
        SELECT RAISE(ABORT, 'index job cancellation cannot be cleared');
    END
    """,
    """
    CREATE TRIGGER index_job_attempts_reject_duplicate_inserts
    BEFORE INSERT ON index_job_attempts
    WHEN EXISTS (
        SELECT 1 FROM index_job_attempts AS existing
        WHERE (existing.job_id = NEW.job_id
                AND existing.attempt_count = NEW.attempt_count)
            OR (
                existing.repository_id = NEW.repository_id
                AND existing.ref_name = NEW.ref_name
                AND existing.fencing_token = NEW.fencing_token
            )
    )
    BEGIN
        SELECT RAISE(ABORT, 'duplicate index job attempt insert is forbidden');
    END
    """,
    f"""
    CREATE TRIGGER index_job_attempts_validate_insert
    BEFORE INSERT ON index_job_attempts
    WHEN NOT EXISTS (
        SELECT 1
        FROM index_jobs AS job
        JOIN ref_job_leases AS lease
            ON lease.repository_id = job.repository_id
            AND lease.ref_name = job.ref_name
            AND lease.job_id = job.job_id
        WHERE job.job_id = NEW.job_id
            AND job.repository_id = NEW.repository_id
            AND job.ref_name = NEW.ref_name
            AND job.request_digest = NEW.request_digest
            AND job.status = 'queued'
            AND job.cancel_requested = 0
            AND job.attempt_count + 1 = NEW.attempt_count
            AND NEW.attempt_count BETWEEN 1 AND job.max_attempts
            AND lease.owner_id = NEW.owner_id
            AND lease.fencing_token = NEW.fencing_token
            AND NEW.fencing_token >= NEW.attempt_count
            AND lease.acquired_at_ms = NEW.started_at_ms
            AND lease.heartbeat_at_ms >= NEW.started_at_ms
            AND lease.lease_expires_at_ms > lease.heartbeat_at_ms
            AND (SELECT COUNT(*) FROM index_job_execution_clock) = 1
            AND NEW.started_at_ms = (
                SELECT high_water_ms FROM index_job_execution_clock
                WHERE singleton_id = 1
            )
            AND EXISTS (
                SELECT 1 FROM index_job_attempt_baselines AS baseline
                WHERE baseline.job_id = job.job_id
                    AND baseline.legacy_attempt_count <= job.attempt_count
            )
    )
    BEGIN
        SELECT RAISE(ABORT, 'index job attempt start is invalid');
    END
    """,
    """
    CREATE TRIGGER index_job_attempts_are_immutable
    BEFORE UPDATE ON index_job_attempts
    BEGIN
        SELECT RAISE(ABORT, 'index job attempts are immutable');
    END
    """,
    """
    CREATE TRIGGER index_job_attempts_cannot_be_deleted
    BEFORE DELETE ON index_job_attempts
    BEGIN
        SELECT RAISE(ABORT, 'index job attempts are immutable');
    END
    """,
    """
    CREATE TRIGGER index_job_attempt_completions_reject_duplicate_inserts
    BEFORE INSERT ON index_job_attempt_completions
    WHEN EXISTS (
        SELECT 1 FROM index_job_attempt_completions AS existing
        WHERE existing.job_id = NEW.job_id
            AND existing.attempt_count = NEW.attempt_count
    )
    BEGIN
        SELECT RAISE(ABORT, 'duplicate job attempt completion is forbidden');
    END
    """,
    f"""
    CREATE TRIGGER index_job_attempt_completions_validate_insert
    BEFORE INSERT ON index_job_attempt_completions
    WHEN NOT EXISTS (
        SELECT 1
        FROM index_job_attempts AS attempt
        JOIN index_jobs AS job ON job.job_id = attempt.job_id
        JOIN ref_job_leases AS lease
            ON lease.repository_id = attempt.repository_id
            AND lease.ref_name = attempt.ref_name
            AND lease.job_id = attempt.job_id
        WHERE attempt.job_id = NEW.job_id
            AND attempt.attempt_count = NEW.attempt_count
            AND attempt.owner_id = NEW.owner_id
            AND attempt.fencing_token = NEW.fencing_token
            AND job.status = 'running'
            AND job.attempt_count = NEW.attempt_count
            AND lease.owner_id = NEW.owner_id
            AND lease.fencing_token = NEW.fencing_token
            AND NEW.completed_at_ms >= attempt.started_at_ms
            AND NEW.completed_at_ms >= job.updated_at_ms
            AND NEW.completed_at_ms >= lease.heartbeat_at_ms
            AND NEW.completed_at_ms >= COALESCE((
                SELECT MAX(event.created_at_ms)
                FROM index_job_events AS event
                WHERE event.job_id = attempt.job_id
                    AND event.attempt_count = attempt.attempt_count
            ), attempt.started_at_ms)
            AND (SELECT COUNT(*) FROM index_job_execution_clock) = 1
            AND NEW.completed_at_ms = (
                SELECT high_water_ms FROM index_job_execution_clock
                WHERE singleton_id = 1
            )
            AND (
                (job.cancel_requested = 0 AND (
                    (NEW.outcome = 'requeue'
                        AND NEW.attempt_count < job.max_attempts)
                    OR NEW.outcome = 'failed'
                ))
                OR (NEW.outcome = 'cancelled' AND job.cancel_requested = 1)
            )
    )
    BEGIN
        SELECT RAISE(ABORT, 'index job attempt completion is invalid');
    END
    """,
    """
    CREATE TRIGGER index_job_attempt_completion_closes_job
    AFTER INSERT ON index_job_attempt_completions
    BEGIN
        INSERT INTO index_job_attempt_closure_frontiers(
            job_id, attempt_count, owner_id, fencing_token, event_count,
            max_event_sequence, max_event_created_at_ms
        ) SELECT
            NEW.job_id,
            NEW.attempt_count,
            NEW.owner_id,
            NEW.fencing_token,
            COUNT(event.event_sequence),
            COALESCE(MAX(event.event_sequence), 0),
            COALESCE(MAX(event.created_at_ms), 0)
        FROM index_job_events AS event
        WHERE event.job_id = NEW.job_id
            AND event.attempt_count = NEW.attempt_count;
        UPDATE index_jobs
        SET status = CASE NEW.outcome
                WHEN 'requeue' THEN 'queued'
                WHEN 'failed' THEN 'failed'
                ELSE 'cancelled'
            END,
            error_code = NEW.error_code,
            error_message = NEW.error_message,
            finished_at_ms = CASE NEW.outcome
                WHEN 'requeue' THEN NULL
                ELSE NEW.completed_at_ms
            END,
            updated_at_ms = NEW.completed_at_ms
        WHERE job_id = NEW.job_id
            AND status = 'running'
            AND attempt_count = NEW.attempt_count;
        SELECT CASE changes()
            WHEN 1 THEN 1
            ELSE RAISE(ABORT, 'attempt completion did not close its job')
        END;
        UPDATE ref_job_leases
        SET job_id = NULL, owner_id = NULL,
            acquired_at_ms = NULL, heartbeat_at_ms = NULL,
            lease_expires_at_ms = NULL, updated_at_ms = NEW.completed_at_ms
        WHERE job_id = NEW.job_id
            AND owner_id = NEW.owner_id
            AND fencing_token = NEW.fencing_token;
        SELECT CASE changes()
            WHEN 1 THEN 1
            ELSE RAISE(ABORT, 'attempt completion did not release its lease')
        END;
    END
    """,
    """
    CREATE TRIGGER index_job_attempt_completions_are_immutable
    BEFORE UPDATE ON index_job_attempt_completions
    BEGIN
        SELECT RAISE(ABORT, 'index job attempt completions are immutable');
    END
    """,
    """
    CREATE TRIGGER index_job_attempt_completions_cannot_be_deleted
    BEFORE DELETE ON index_job_attempt_completions
    BEGIN
        SELECT RAISE(ABORT, 'index job attempt completions are immutable');
    END
    """,
    """
    CREATE TRIGGER index_job_events_reject_duplicate_inserts
    BEFORE INSERT ON index_job_events
    WHEN EXISTS (
        SELECT 1 FROM index_job_events AS existing
        WHERE existing.event_sequence = NEW.event_sequence
            OR (
                existing.job_id = NEW.job_id
                AND existing.attempt_count = NEW.attempt_count
                AND (
                    existing.event_key = NEW.event_key
                    OR (
                        NEW.kind = 'view_result'
                        AND existing.kind = 'view_result'
                        AND existing.view_type = NEW.view_type
                    )
                )
            )
    )
    BEGIN
        SELECT RAISE(ABORT, 'duplicate index job event is forbidden');
    END
    """,
    f"""
    CREATE TRIGGER index_job_events_validate_insert
    BEFORE INSERT ON index_job_events
    WHEN NOT (
        NEW.event_sequence = -1
        AND (SELECT COUNT(*) FROM index_job_events
            WHERE job_id = NEW.job_id
                AND attempt_count = NEW.attempt_count
        ) < {MAX_INDEX_JOB_EVENTS_PER_ATTEMPT}
        AND NOT EXISTS (
            SELECT 1 FROM index_job_attempt_closure_frontiers AS frontier
            WHERE frontier.job_id = NEW.job_id
                AND frontier.attempt_count = NEW.attempt_count
        )
        AND EXISTS (
            SELECT 1
            FROM index_job_attempts AS attempt
            JOIN index_jobs AS job ON job.job_id = attempt.job_id
            JOIN ref_job_leases AS lease
                ON lease.repository_id = attempt.repository_id
                AND lease.ref_name = attempt.ref_name
                AND lease.job_id = attempt.job_id
            WHERE attempt.job_id = NEW.job_id
                AND attempt.attempt_count = NEW.attempt_count
                AND attempt.owner_id = NEW.owner_id
                AND attempt.fencing_token = NEW.fencing_token
                AND job.status = 'running'
                AND job.attempt_count = NEW.attempt_count
                AND lease.owner_id = NEW.owner_id
                AND lease.fencing_token = NEW.fencing_token
                AND lease.lease_expires_at_ms > NEW.created_at_ms
                AND NEW.created_at_ms >= attempt.started_at_ms
                AND NEW.created_at_ms >= job.updated_at_ms
                AND NEW.created_at_ms >= lease.heartbeat_at_ms
                AND NEW.created_at_ms >= COALESCE((
                    SELECT MAX(existing.created_at_ms)
                    FROM index_job_events AS existing
                    WHERE existing.job_id = attempt.job_id
                        AND existing.attempt_count = attempt.attempt_count
                ), attempt.started_at_ms)
                AND (SELECT COUNT(*) FROM index_job_execution_clock) = 1
                AND NEW.created_at_ms = (
                    SELECT high_water_ms FROM index_job_execution_clock
                    WHERE singleton_id = 1
                )
        )
        AND (
            NEW.view_type IS NULL
            OR EXISTS (
                SELECT 1 FROM index_job_views AS requested
                WHERE requested.job_id = NEW.job_id
                    AND requested.view_type = NEW.view_type
            )
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'index job event is invalid');
    END
    """,
    """
    CREATE TRIGGER index_job_events_are_immutable
    BEFORE UPDATE ON index_job_events
    BEGIN
        SELECT RAISE(ABORT, 'index job events are immutable');
    END
    """,
    """
    CREATE TRIGGER index_job_events_cannot_be_deleted
    BEFORE DELETE ON index_job_events
    BEGIN
        SELECT RAISE(ABORT, 'index job events are immutable');
    END
    """,
    """
    DROP TRIGGER index_job_publications_validate_insert
    """,
    _V6_INDEX_JOB_PUBLICATIONS_VALIDATE_INSERT,
    """
    DROP TRIGGER index_job_publication_completes_job
    """,
    """
    CREATE TRIGGER index_job_publication_completes_job
    AFTER INSERT ON index_job_publications
    BEGIN
        INSERT INTO index_job_attempt_closure_frontiers(
            job_id, attempt_count, owner_id, fencing_token, event_count,
            max_event_sequence, max_event_created_at_ms
        ) SELECT
            NEW.job_id,
            job.attempt_count,
            NEW.owner_id,
            NEW.fencing_token,
            COUNT(event.event_sequence),
            COALESCE(MAX(event.event_sequence), 0),
            COALESCE(MAX(event.created_at_ms), 0)
        FROM index_jobs AS job
        LEFT JOIN index_job_events AS event
            ON event.job_id = job.job_id
            AND event.attempt_count = job.attempt_count
        WHERE job.job_id = NEW.job_id;
        UPDATE index_jobs
        SET status = 'succeeded', result_snapshot_id = NEW.snapshot_id,
            error_code = NULL, error_message = NULL,
            finished_at_ms = NEW.completed_at_ms,
            updated_at_ms = NEW.completed_at_ms
        WHERE job_id = NEW.job_id AND status = 'running'
            AND cancel_requested = 0;
        SELECT CASE changes()
            WHEN 1 THEN 1
            ELSE RAISE(ABORT, 'index job publication did not complete its job')
        END;
        UPDATE ref_job_leases
        SET job_id = NULL, owner_id = NULL,
            acquired_at_ms = NULL, heartbeat_at_ms = NULL,
            lease_expires_at_ms = NULL, updated_at_ms = NEW.completed_at_ms
        WHERE repository_id = NEW.repository_id
            AND ref_name = NEW.ref_name
            AND job_id = NEW.job_id
            AND owner_id = NEW.owner_id
            AND fencing_token = NEW.fencing_token;
        SELECT CASE changes()
            WHEN 1 THEN 1
            ELSE RAISE(ABORT, 'index job publication did not release its lease')
        END;
    END
    """,
    """
    CREATE TRIGGER index_job_publications_require_attempt_start
    BEFORE INSERT ON index_job_publications
    WHEN NOT EXISTS (
        SELECT 1
        FROM index_jobs AS job
        JOIN index_job_attempts AS attempt
            ON attempt.job_id = job.job_id
            AND attempt.attempt_count = job.attempt_count
        WHERE job.job_id = NEW.job_id
            AND attempt.repository_id = NEW.repository_id
            AND attempt.ref_name = NEW.ref_name
            AND attempt.request_digest = NEW.request_digest
            AND attempt.owner_id = NEW.owner_id
            AND attempt.fencing_token = NEW.fencing_token
            AND attempt.started_at_ms <= NEW.completed_at_ms
            AND NEW.completed_at_ms >= job.updated_at_ms
            AND NEW.completed_at_ms >= COALESCE((
                SELECT MAX(event.created_at_ms)
                FROM index_job_events AS event
                WHERE event.job_id = attempt.job_id
                    AND event.attempt_count = attempt.attempt_count
            ), attempt.started_at_ms)
    )
    BEGIN
        SELECT RAISE(ABORT, 'index job publication lacks its attempt start');
    END
    """,
)


_SCHEMA_V7 = (
    """
    CREATE TABLE index_job_insertion_sequences (
        job_sequence INTEGER PRIMARY KEY AUTOINCREMENT CHECK (
            typeof(job_sequence) = 'integer'
            AND job_sequence BETWEEN 1 AND 9223372036854775807
        ),
        job_id TEXT NOT NULL UNIQUE CHECK (
            typeof(job_id) = 'text'
            AND length(job_id) BETWEEN 1 AND 80
            AND instr(job_id, char(0)) = 0
        ),
        FOREIGN KEY (job_id) REFERENCES index_jobs(job_id) ON DELETE RESTRICT
    )
    """,
    """
    INSERT INTO index_job_insertion_sequences(job_id)
    SELECT job_id FROM index_jobs ORDER BY created_at_ms, job_id
    """,
    """
    CREATE TRIGGER index_jobs_allocate_insertion_sequence
    AFTER INSERT ON index_jobs
    BEGIN
        INSERT INTO index_job_insertion_sequences(job_id) VALUES (NEW.job_id);
    END
    """,
    """
    CREATE TRIGGER index_job_insertion_sequences_are_immutable
    BEFORE UPDATE ON index_job_insertion_sequences
    BEGIN
        SELECT RAISE(ABORT, 'index job insertion sequences are immutable');
    END
    """,
    """
    CREATE TRIGGER index_job_insertion_sequences_cannot_be_deleted
    BEFORE DELETE ON index_job_insertion_sequences
    BEGIN
        SELECT RAISE(ABORT, 'index job insertion sequences are immutable');
    END
    """,
)

_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: _SCHEMA_V1,
    2: _SCHEMA_V2,
    3: _SCHEMA_V3,
    4: _SCHEMA_V4,
    5: _SCHEMA_V5,
    6: _SCHEMA_V6,
    7: _SCHEMA_V7,
}

_CatalogSchemaObject = tuple[str, str, str, str | None]
_CatalogSchemaSignature = tuple[_CatalogSchemaObject, ...]

_SQLITE_ANALYZE_SCHEMA_OBJECTS = frozenset(
    {
        (
            "table",
            "sqlite_stat1",
            "sqlite_stat1",
            "CREATE TABLE sqlite_stat1(tbl,idx,stat)",
        ),
        (
            "table",
            "sqlite_stat2",
            "sqlite_stat2",
            "CREATE TABLE sqlite_stat2(tbl,idx,sampleno,sample)",
        ),
        (
            "table",
            "sqlite_stat3",
            "sqlite_stat3",
            "CREATE TABLE sqlite_stat3(tbl,idx,neq,nlt,ndlt,sample)",
        ),
        (
            "table",
            "sqlite_stat4",
            "sqlite_stat4",
            "CREATE TABLE sqlite_stat4(tbl,idx,neq,nlt,ndlt,sample)",
        ),
    }
)


def _normalized_schema_sql(sql: str | None) -> str | None:
    if sql is None:
        return None
    return "\n".join(line.strip() for line in sql.strip().splitlines() if line.strip())


def _catalog_schema_signature(
    connection: sqlite3.Connection,
) -> _CatalogSchemaSignature:
    """Return every application schema object in a formatting-stable form."""

    signature: list[_CatalogSchemaObject] = []
    schema_rows = connection.execute(
        "SELECT type, name, tbl_name, sql "
        "FROM sqlite_schema ORDER BY type, name, tbl_name"
    )
    for row in schema_rows:
        schema_object = (
            row[0],
            row[1],
            row[2],
            _normalized_schema_sql(row[3]),
        )
        # ANALYZE can add one of SQLite's exact internal statistics-table
        # schemas after routine maintenance.  Ignore only those known exact
        # objects; a forged or malformed ``sqlite_*`` entry remains part of
        # the authenticated signature and is rejected.
        if schema_object in _SQLITE_ANALYZE_SCHEMA_OBJECTS:
            continue
        signature.append(schema_object)
    return tuple(signature)


def _canonical_catalog_schemas() -> dict[int, _CatalogSchemaSignature]:
    """Materialize the exact schema produced after each supported migration."""

    connection = sqlite3.connect(
        ":memory:",
        isolation_level=None,
        **_SQLITE_CONNECT_OPTIONS,
    )
    try:
        connection.execute(_SCHEMA_MIGRATIONS_SQL)
        schemas: dict[int, _CatalogSchemaSignature] = {}
        for version in range(1, LATEST_SCHEMA_VERSION + 1):
            for statement in _MIGRATIONS[version]:
                connection.execute(statement)
            schemas[version] = _catalog_schema_signature(connection)
        return schemas
    finally:
        connection.close()


_CANONICAL_CATALOG_SCHEMAS = _canonical_catalog_schemas()


def _validation_source_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
        int(metadata.st_nlink),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
        int(getattr(metadata, "st_file_attributes", 0)),
    )


def _validation_source_structural_identity(
    metadata: os.stat_result,
) -> tuple[int, ...]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
        int(metadata.st_nlink),
        int(getattr(metadata, "st_file_attributes", 0)),
    )


def _validation_linux_mount_points() -> frozenset[str]:
    """Read a bounded Linux mount table without a fail-open fallback."""

    if not sys.platform.startswith("linux"):
        return frozenset()
    from .._atomic_directory import _mountinfo_path

    points: set[str] = set()
    try:
        with open("/proc/self/mountinfo", "rb") as mountinfo:
            for index, line in enumerate(mountinfo):
                if index >= _MAX_VALIDATION_MOUNTINFO_ENTRIES:
                    raise CatalogError("Linux mount table exceeds its safe entry limit")
                if len(line) > _MAX_VALIDATION_MOUNTINFO_LINE_BYTES:
                    raise CatalogError("Linux mount table contains an oversized entry")
                fields = line.split()
                try:
                    separator = fields.index(b"-")
                except ValueError as exc:
                    raise CatalogError(
                        "Linux mount table contains a malformed entry"
                    ) from exc
                if separator < 6 or len(fields) < separator + 4:
                    raise CatalogError("Linux mount table contains a malformed entry")
                point = os.path.normpath(_mountinfo_path(os.fsdecode(fields[4])))
                if not os.path.isabs(point):
                    raise CatalogError("Linux mount table contains a relative path")
                points.add(point)
    except CatalogError:
        raise
    except (OSError, UnicodeError) as exc:
        raise CatalogError("Linux mount table could not be inspected safely") from exc
    if not points:
        raise CatalogError("Linux mount table is empty")
    return frozenset(points)


def _require_validation_source_not_mount(path: Path, *, label: str) -> None:
    """Reject a catalog leaf whose bytes are supplied by a mount alias."""

    try:
        normalized = os.path.normpath(os.path.realpath(path))
        is_mount = os.path.ismount(path) or os.path.ismount(normalized)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CatalogError(
            f"existing SQLite catalog {label} mount identity could not be inspected"
        ) from exc
    if sys.platform.startswith("linux"):
        is_mount = is_mount or normalized in _validation_linux_mount_points()
    if is_mount:
        raise CatalogError(
            f"existing SQLite catalog {label} must not be a file mount point"
        )


def _open_validation_source(
    path: Path,
    *,
    label: str,
    required: bool,
) -> tuple[int, os.stat_result] | None:
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        if not required:
            return None
        raise CatalogError(f"existing SQLite catalog {label} is missing") from exc
    except OSError as exc:
        raise CatalogError(
            f"existing SQLite catalog {label} could not be inspected"
        ) from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_dev < 1
        or before.st_ino < 1
        or before.st_nlink != 1
        or before.st_size < 0
        or getattr(before, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT_ATTRIBUTE
    ):
        raise CatalogError(
            f"existing SQLite catalog {label} must be a single-linked regular file"
        )
    _require_validation_source_not_mount(path, label=label)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CatalogError(
            f"existing SQLite catalog {label} could not be opened safely"
        ) from exc
    after = before
    try:
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_dev < 1
            or after.st_ino < 1
            or after.st_nlink != 1
            or getattr(after, "st_file_attributes", 0)
            & _WINDOWS_REPARSE_POINT_ATTRIBUTE
        ):
            raise CatalogError(
                f"existing SQLite catalog {label} must be a single-linked regular file"
            )
        if _validation_source_structural_identity(
            before
        ) != _validation_source_structural_identity(after):
            raise CatalogError(
                f"existing SQLite catalog {label} identity changed before copy"
            )
        if _validation_source_identity(before) != _validation_source_identity(after):
            raise _CatalogValidationNamespaceChanged(
                f"existing SQLite catalog {label} changed before copy"
            )
        _require_validation_source_not_mount(path, label=label)
    except BaseException as primary_error:  # noqa: B036 - retain failed cleanup
        _close_validation_descriptor(
            descriptor,
            after,
            primary_error=primary_error,
            label=label,
        )
        raise
    return descriptor, after


def _close_validation_descriptor(
    descriptor: int,
    expected: os.stat_result,
    *,
    primary_error: BaseException | None,
    label: str,
) -> None:
    """Close one file descriptor while retaining any provably open owner."""

    from .._contained_source import _PosixDescriptorCleanup

    cleanup = _PosixDescriptorCleanup()
    cleanup.retain(descriptor, expected)
    _finish_validation_cleanup(
        cleanup,
        primary_error=primary_error,
        label=label,
    )


def _finish_validation_cleanup(
    cleanup: object,
    *,
    primary_error: BaseException | None,
    label: str,
) -> None:
    """Finish one retryable descriptor owner without replacing a primary."""

    from .._atomic_directory import (
        _annotate_secondary_error,
        _attach_publication_cleanup_owner,
    )

    try:
        cleanup.close()  # type: ignore[attr-defined]
    except BaseException as close_error:  # noqa: B036 - preserve primary
        target = close_error if primary_error is None else primary_error
        if primary_error is not None:
            _annotate_secondary_error(
                primary_error,
                f"SQLite validation {label} cleanup also failed",
                close_error,
            )
        _attach_publication_cleanup_owner(target, cleanup)
        if primary_error is None:
            raise
    else:
        if primary_error is not None:
            _attach_publication_cleanup_owner(primary_error, cleanup)


def _copy_validation_source(
    descriptor: int,
    expected: os.stat_result,
    destination: Path,
    *,
    label: str,
) -> None:
    from .._contained_source import _PosixDescriptorCleanup

    destination_cleanup = _PosixDescriptorCleanup()
    destination_descriptor = -1
    try:
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        destination_metadata = os.fstat(destination_descriptor)
        destination_cleanup.retain(
            destination_descriptor,
            destination_metadata,
        )
    except OSError as exc:
        primary_error = CatalogError(
            f"private SQLite validation {label} could not be created"
        )
        if destination_descriptor >= 0:
            if destination_cleanup.closed:
                try:
                    destination_cleanup.retain(destination_descriptor)
                except BaseException as retention_error:  # noqa: B036
                    from .._atomic_directory import _annotate_secondary_error

                    _annotate_secondary_error(
                        primary_error,
                        "private SQLite validation cleanup ownership also failed",
                        retention_error,
                    )
            _finish_validation_cleanup(
                destination_cleanup,
                primary_error=primary_error,
                label=f"private {label}",
            )
        raise primary_error from exc
    except BaseException as primary_error:  # noqa: B036 - retain acquisition
        if destination_descriptor >= 0:
            if destination_cleanup.closed:
                try:
                    destination_cleanup.retain(destination_descriptor)
                except BaseException as retention_error:  # noqa: B036
                    from .._atomic_directory import _annotate_secondary_error

                    _annotate_secondary_error(
                        primary_error,
                        "private SQLite validation cleanup ownership also failed",
                        retention_error,
                    )
            _finish_validation_cleanup(
                destination_cleanup,
                primary_error=primary_error,
                label=f"private {label}",
            )
        raise
    primary_error: BaseException | None = None
    try:
        content_changed = False
        remaining = int(expected.st_size)
        while remaining:
            chunk = os.read(
                descriptor,
                min(remaining, _VALIDATION_COPY_CHUNK_BYTES),
            )
            if not chunk:
                content_changed = True
                break
            view = memoryview(chunk)
            while view:
                written = os.write(destination_descriptor, view)
                if written < 1:
                    raise CatalogError(
                        f"private SQLite validation {label} copy stalled"
                    )
                view = view[written:]
            remaining -= len(chunk)
        if not content_changed and os.read(descriptor, 1):
            content_changed = True
        try:
            observed = os.fstat(descriptor)
        except OSError as exc:
            raise CatalogError(
                f"existing SQLite catalog {label} could not be rechecked after copy"
            ) from exc
        if _validation_source_structural_identity(
            observed
        ) != _validation_source_structural_identity(expected):
            raise CatalogError(
                f"existing SQLite catalog {label} identity changed during copy"
            )
        if content_changed or _validation_source_identity(
            observed
        ) != _validation_source_identity(expected):
            raise _CatalogValidationNamespaceChanged(
                f"existing SQLite catalog {label} changed during copy"
            )
    except OSError as exc:
        primary_error = CatalogError(
            f"existing SQLite catalog {label} could not be copied safely"
        )
        raise primary_error from exc
    except BaseException as exc:  # noqa: B036 - preserve primary across cleanup
        primary_error = exc
        raise
    finally:
        _finish_validation_cleanup(
            destination_cleanup,
            primary_error=primary_error,
            label=f"private {label}",
        )


def _require_no_rollback_journal(path: Path) -> None:
    journal = Path(f"{path}-journal")
    try:
        journal.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CatalogError(
            "existing SQLite catalog rollback journal could not be inspected"
        ) from exc
    raise CatalogError("existing SQLite catalog rollback journal is not allowed")


def _require_safe_shared_memory(path: Path) -> None:
    source = _open_validation_source(
        Path(f"{path}-shm"),
        label="SHM sidecar",
        required=False,
    )
    if source is None:
        return
    descriptor, expected = source
    primary_error: BaseException | None = None
    try:
        if expected.st_size > _MAX_VALIDATION_SHM_BYTES:
            raise CatalogError(
                "existing SQLite catalog SHM sidecar exceeds "
                f"{_MAX_VALIDATION_SHM_BYTES} bytes"
            )
        try:
            observed = os.fstat(descriptor)
        except OSError as exc:
            raise CatalogError(
                "existing SQLite catalog SHM sidecar could not be rechecked"
            ) from exc
        if _validation_source_structural_identity(
            observed
        ) != _validation_source_structural_identity(expected):
            raise CatalogError("existing SQLite catalog SHM sidecar identity changed")
        if _validation_source_identity(observed) != _validation_source_identity(
            expected
        ):
            raise _CatalogValidationNamespaceChanged(
                "existing SQLite catalog SHM sidecar changed"
            )
    except BaseException as exc:  # noqa: B036 - preserve primary across cleanup
        primary_error = exc
        raise
    finally:
        _close_validation_descriptor(
            descriptor,
            expected,
            primary_error=primary_error,
            label="SHM sidecar",
        )


@contextmanager
def _catalog_validation_snapshot(path: Path) -> Iterator[Path]:
    """Copy the SQLite recovery namespace without mutating the source files."""

    from .._contained_source import _PosixDescriptorCleanup

    with tempfile.TemporaryDirectory(prefix="codenib-sqlite-validation-") as root:
        snapshot = Path(root) / "catalog.sqlite3"
        _require_no_rollback_journal(path)
        sources: list[tuple[int, os.stat_result, Path, str]] = []
        source_cleanup = _PosixDescriptorCleanup()
        primary_error: BaseException | None = None
        try:
            main_source = _open_validation_source(
                path,
                label="main file",
                required=True,
            )
            assert main_source is not None
            source_cleanup.retain(*main_source)
            sources.append((*main_source, snapshot, "main file"))
            wal_source = _open_validation_source(
                Path(f"{path}-wal"),
                label="WAL sidecar",
                required=False,
            )
            if wal_source is not None:
                source_cleanup.retain(*wal_source)
                sources.append((*wal_source, Path(f"{snapshot}-wal"), "WAL sidecar"))
            total_bytes = sum(source[1].st_size for source in sources)
            if total_bytes > _MAX_VALIDATION_NAMESPACE_BYTES:
                raise CatalogError(
                    "existing SQLite catalog validation namespace exceeds "
                    f"{_MAX_VALIDATION_NAMESPACE_BYTES} bytes"
                )
            for descriptor, metadata, destination, label in sources:
                _copy_validation_source(
                    descriptor,
                    metadata,
                    destination,
                    label=label,
                )
            _require_no_rollback_journal(path)
        except BaseException as exc:  # noqa: B036 - preserve primary across cleanup
            primary_error = exc
            raise
        finally:
            _finish_validation_cleanup(
                source_cleanup,
                primary_error=primary_error,
                label="source namespace",
            )
        yield snapshot


class SQLiteCatalog:
    """Transactional SQLite catalog for immutable index snapshots."""

    def retained_import_contract(self) -> str:
        """Declare support for exact retained-import response attestation."""

        return RETAINED_IMPORT_CATALOG_CONTRACT

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 5_000,
        create: bool = True,
        expected_file_identity: tuple[int, int, int] | None = None,
    ) -> None:
        """Open a catalog, optionally requiring an initialized existing file.

        ``create=False`` prevents path creation and rejects empty, foreign, or
        corrupt databases.  A recognized older CodeNib catalog is still opened
        read-write, switched to WAL, and forward-migrated transactionally.  An
        expected file identity binds that existing-only open to one resolved
        single-linked regular inode across ``sqlite3.connect``.  Catalogs in one
        interpreter coordinate validation, transactions, and close by resolved
        path; exact same-inode namespace drift is recaptured with bounded
        attempts whose retry starts and backoff honor the busy-timeout deadline.
        """

        self._owner_pid = os.getpid()
        self._transaction_owner: _SQLiteTransactionOwner | None = None
        if (
            isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or busy_timeout_ms < 0
        ):
            raise ValueError("busy_timeout_ms must be a non-negative integer")
        if type(create) is not bool:
            raise TypeError("create must be a boolean")
        if expected_file_identity is not None:
            if create:
                raise ValueError("expected_file_identity requires create=False")
            if (
                type(expected_file_identity) is not tuple
                or len(expected_file_identity) != 3
                or any(type(value) is not int for value in expected_file_identity)
            ):
                raise TypeError(
                    "expected_file_identity must be an exact 3-tuple of integers"
                )
            if any(value < 1 for value in expected_file_identity):
                raise ValueError(
                    "expected_file_identity must contain positive integers"
                )
            if expected_file_identity[2] != 1:
                raise ValueError("expected_file_identity link count must be 1")

        raw_path = str(path)
        connection_target = raw_path
        use_uri = False
        resolved: Path | None = None
        if raw_path != ":memory:":
            resolved = Path(path).expanduser().resolve()
            raw_path = str(resolved)
            if create:
                resolved.parent.mkdir(parents=True, exist_ok=True)
                connection_target = raw_path
            else:
                connection_target = f"{resolved.as_uri()}?mode=rw"
                use_uri = True
        elif not create:
            raise ValueError(
                "an in-memory SQLite catalog cannot be opened existing-only"
            )
        self.path = raw_path
        self._path_coordination = _catalog_path_coordination(resolved)
        self._path_coordination.lock.run(
            partial(
                self._initialize_connection,
                resolved=resolved,
                raw_path=raw_path,
                connection_target=connection_target,
                use_uri=use_uri,
                busy_timeout_ms=busy_timeout_ms,
                create=create,
                expected_file_identity=expected_file_identity,
            )
        )

    def _initialize_connection(
        self,
        *,
        resolved: Path | None,
        raw_path: str,
        connection_target: str,
        use_uri: bool,
        busy_timeout_ms: int,
        create: bool,
        expected_file_identity: tuple[int, int, int] | None,
    ) -> None:
        """Validate and open one connection while local writers are quiescent."""

        if expected_file_identity is not None:
            assert resolved is not None
            self._require_expected_file_identity(resolved, expected_file_identity)
        if not create:
            assert resolved is not None
            validation_deadline = time.monotonic() + busy_timeout_ms / 1_000
            validation_failures = 0
            while True:
                try:
                    with _catalog_validation_snapshot(resolved) as validation_path:
                        validation_target = f"{validation_path.as_uri()}?mode=rw"
                        try:
                            validation_connection = sqlite3.connect(
                                validation_target,
                                timeout=busy_timeout_ms / 1_000,
                                isolation_level=None,
                                uri=True,
                                **_SQLITE_CONNECT_OPTIONS,
                            )
                        except sqlite3.Error as exc:
                            raise CatalogError(
                                "existing SQLite catalog could not be opened: "
                                f"{raw_path}"
                            ) from exc
                        try:
                            if expected_file_identity is not None:
                                self._require_expected_file_identity(
                                    resolved,
                                    expected_file_identity,
                                )
                            validation_connection.row_factory = sqlite3.Row
                            self._connection = validation_connection
                            self._require_existing_catalog_identity()
                            if expected_file_identity is not None:
                                self._require_expected_file_identity(
                                    resolved,
                                    expected_file_identity,
                                )
                        except sqlite3.Error as exc:
                            raise CatalogError(
                                "existing SQLite catalog could not be initialized: "
                                f"{raw_path}"
                            ) from exc
                        finally:
                            validation_connection.close()
                    _require_no_rollback_journal(resolved)
                    # The SHM file is a derived WAL index, so the private copy
                    # rebuilds it. Authenticate the original before SQLite may
                    # map or update it.
                    _require_safe_shared_memory(resolved)
                    if expected_file_identity is not None:
                        self._require_expected_file_identity(
                            resolved,
                            expected_file_identity,
                        )
                except _CatalogValidationNamespaceChanged as exc:
                    validation_failures += 1
                    now = time.monotonic()
                    if (
                        getattr(exc, "publication_cleanup_owners", ())
                        or validation_failures >= _VALIDATION_NAMESPACE_MAX_ATTEMPTS
                        or now >= validation_deadline
                    ):
                        raise
                    retry_delay = min(
                        _VALIDATION_RETRY_INITIAL_SECONDS
                        * 2 ** min(validation_failures - 1, 5),
                        _VALIDATION_RETRY_MAX_SECONDS,
                    )
                    if retry_delay <= 0 or retry_delay >= validation_deadline - now:
                        raise
                    time.sleep(retry_delay)
                    if time.monotonic() >= validation_deadline:
                        raise
                    continue
                break
        try:
            connection = sqlite3.connect(
                connection_target,
                timeout=busy_timeout_ms / 1_000,
                isolation_level=None,
                uri=use_uri,
                **_SQLITE_CONNECT_OPTIONS,
            )
        except sqlite3.Error as exc:
            if not create:
                raise CatalogError(
                    f"existing SQLite catalog could not be opened: {raw_path}"
                ) from exc
            raise
        try:
            if expected_file_identity is not None:
                assert resolved is not None
                self._require_expected_file_identity(
                    resolved,
                    expected_file_identity,
                )
        except BaseException:
            connection.close()
            raise
        self._connection = connection
        self._connection.row_factory = sqlite3.Row
        try:
            if not create:
                self._require_existing_catalog_identity()
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
        except sqlite3.Error as exc:
            self._connection.close()
            if not create:
                raise CatalogError(
                    f"existing SQLite catalog could not be initialized: {raw_path}"
                ) from exc
            raise
        except BaseException:
            self._connection.close()
            raise

    @staticmethod
    def _require_expected_file_identity(
        path: Path,
        expected: tuple[int, int, int],
    ) -> None:
        try:
            metadata = path.lstat()
        except (OSError, RuntimeError, ValueError) as exc:
            raise CatalogError(
                "existing SQLite catalog file identity could not be verified"
            ) from exc
        observed = (
            int(metadata.st_dev),
            int(metadata.st_ino),
            int(metadata.st_nlink),
        )
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_dev < 1
            or metadata.st_ino < 1
            or metadata.st_nlink != 1
            or observed != expected
        ):
            raise CatalogError("existing SQLite catalog file identity changed")

    def close(self) -> None:
        """Close the underlying database connection."""
        self._require_owner_pid()
        self._path_coordination.lock.run(self._close_connection)

    def _close_connection(self) -> None:
        owner = self._transaction_owner
        if owner is not None and not owner.settled:
            raise CatalogError("cannot close a catalog with an active transaction")
        self._connection.close()

    def _require_owner_pid(self) -> None:
        if os.getpid() != self._owner_pid:
            raise CatalogError("SQLite catalog connection crossed a PID boundary")

    def _require_existing_catalog_identity(self) -> None:
        observed_schema = _catalog_schema_signature(self._connection)
        if not any(
            object_type == "table" and name == "schema_migrations"
            for object_type, name, _table_name, _sql in observed_schema
        ):
            raise CatalogError("existing file is not an initialized CodeNib catalog")
        try:
            migration_rows = self._connection.execute(
                "SELECT version, applied_at FROM schema_migrations ORDER BY version"
            ).fetchall()
        except sqlite3.Error as exc:
            raise CatalogError(
                "existing file is not an initialized CodeNib catalog"
            ) from exc
        versions: list[int] = []
        for row in migration_rows:
            version = row["version"]
            applied_at = row["applied_at"]
            if (
                type(version) is not int
                or version < 1
                or version > _SQLITE_INT64_MAX
                or type(applied_at) is not str
                or not applied_at
            ):
                raise CatalogError(
                    "existing file is not an initialized CodeNib catalog"
                )
            versions.append(version)
        if not versions or versions != list(range(1, len(versions) + 1)):
            raise CatalogError("existing file is not an initialized CodeNib catalog")
        user_version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if type(user_version) is not int or user_version != versions[-1]:
            raise CatalogError("existing file is not an initialized CodeNib catalog")
        if user_version > LATEST_SCHEMA_VERSION:
            raise CatalogError(
                "catalog schema is newer than this CodeNib version: "
                f"{user_version} > {LATEST_SCHEMA_VERSION}"
            )
        expected_schema = _CANONICAL_CATALOG_SCHEMAS.get(user_version)
        if expected_schema is None or observed_schema != expected_schema:
            raise CatalogError(
                "existing file is not an initialized CodeNib catalog: "
                f"schema is not canonical for version {user_version}"
            )

    def __enter__(self) -> SQLiteCatalog:
        self._require_owner_pid()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    @_coordinated_catalog_method
    def schema_version(self) -> int:
        """Return the latest successfully applied schema migration."""
        self._require_owner_pid()
        row = self._connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
        ).fetchone()
        return int(row["version"])

    @contextmanager
    def _transaction(self, *, immediate: bool = True) -> Iterator[None]:
        """Settle one transaction inside a coordinated catalog method."""

        self._require_owner_pid()
        existing_owner = self._transaction_owner
        if existing_owner is not None and existing_owner.settled:
            self._transaction_owner = None
            existing_owner = None
        if existing_owner is not None or self._connection.in_transaction:
            raise CatalogError("nested catalog transactions are not supported")
        owner = _SQLiteTransactionOwner(self._connection)
        try:
            try:
                self._transaction_owner = owner
                owner.begin(immediate=immediate)
                yield
                self._require_owner_pid()
                owner.mark_body_succeeded()
            except BaseException as error:  # noqa: B036 - preserve first failure
                self._require_owner_pid()
                owner.retain(error, label="SQLite transaction body also failed")
            _settle_sqlite_transaction(owner)
            # Keep cleanup in the protected path: cancellation can arrive on
            # the first Python opcode after the C settlement runner returns.
            if self._transaction_owner is owner:
                self._transaction_owner = None
        except BaseException as error:  # noqa: B036 - contain cleanup interruption
            self._require_owner_pid()
            owner.retain(
                error,
                label="SQLite transaction exception capture also failed",
            )
            _settle_sqlite_transaction(owner)
            if self._transaction_owner is owner:
                self._transaction_owner = None
        if owner.primary_error is not None:
            raise owner.primary_error

    @_coordinated_catalog_method
    def _migrate(self) -> None:
        with self._transaction():
            self._connection.execute(_SCHEMA_MIGRATIONS_SQL)
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
            if self.schema_version >= 5:
                self._validate_publication_aggregates()
            if self.schema_version >= 6:
                self._validate_job_execution_aggregates()

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
        return _persisted_nonnegative_int64(row["now_ms"], "SQLite clock")

    def _job_execution_high_water_ms(self) -> int:
        """Return the exact durable content-clock singleton."""

        rows = self._connection.execute(
            """
            SELECT singleton_id, high_water_ms
            FROM index_job_execution_clock
            """
        ).fetchall()
        if (
            len(rows) != 1
            or type(rows[0]["singleton_id"]) is not int
            or rows[0]["singleton_id"] != 1
        ):
            raise CatalogConflictError(
                "persisted index job execution clock is not a singleton"
            )
        return _persisted_nonnegative_int64(
            rows[0]["high_water_ms"],
            "index job execution clock high-water",
        )

    def _job_insertion_sequence_high_water(self) -> int:
        """Return one gap-free, durable job insertion high-water mark."""

        aggregate = self._connection.execute(
            """
            SELECT COUNT(*) AS sequence_count,
                COALESCE(MIN(job_sequence), 0) AS min_job_sequence,
                COALESCE(MAX(job_sequence), 0) AS max_job_sequence
            FROM index_job_insertion_sequences
            """
        ).fetchone()
        job_count = self._connection.execute(
            "SELECT COUNT(*) AS job_count FROM index_jobs"
        ).fetchone()["job_count"]
        sequence_count = aggregate["sequence_count"]
        minimum = aggregate["min_job_sequence"]
        maximum = aggregate["max_job_sequence"]
        if any(
            type(value) is not int
            for value in (job_count, sequence_count, minimum, maximum)
        ):
            raise CatalogConflictError(
                "persisted index job insertion sequence is not canonical"
            )
        allocator = self._connection.execute(
            """
            SELECT seq FROM sqlite_sequence
            WHERE name = 'index_job_insertion_sequences'
            """
        ).fetchone()
        allocated = 0
        if allocator is not None:
            allocated = allocator["seq"]
            if type(allocated) is not int:
                raise CatalogConflictError(
                    "persisted index job insertion allocator is not canonical"
                )
        missing = self._connection.execute(
            """
            SELECT 1
            FROM index_jobs AS job
            LEFT JOIN index_job_insertion_sequences AS insertion
                ON insertion.job_id = job.job_id
            WHERE insertion.job_id IS NULL
            UNION ALL
            SELECT 1
            FROM index_job_insertion_sequences AS insertion
            LEFT JOIN index_jobs AS job ON job.job_id = insertion.job_id
            WHERE job.job_id IS NULL
            LIMIT 1
            """
        ).fetchone()
        if (
            missing is not None
            or sequence_count != job_count
            or minimum != (0 if sequence_count == 0 else 1)
            or maximum != sequence_count
            or allocated != maximum
        ):
            raise CatalogConflictError(
                "persisted index job insertion sequence conflicts with its jobs"
            )
        return _persisted_nonnegative_int64(
            maximum,
            "index job insertion sequence high-water",
        )

    def _advance_job_execution_clock(
        self,
        *,
        causal_floor_ms: int,
        action: str,
    ) -> int:
        """Atomically freeze SQLite time for one fresh content mutation."""

        floor = _persisted_nonnegative_int64(
            causal_floor_ms,
            "index job execution causal floor",
        )
        previous = self._job_execution_high_water_ms()
        try:
            cursor = self._connection.execute(
                f"""
                UPDATE index_job_execution_clock
                SET high_water_ms = {_DB_NOW_MS_SQL}
                WHERE singleton_id = 1
                    AND high_water_ms <= {_DB_NOW_MS_SQL}
                    AND ? <= {_DB_NOW_MS_SQL}
                """,
                (floor,),
            )
        except sqlite3.IntegrityError as exc:
            if str(exc) != "index job execution clock update is invalid":
                raise
            raise CatalogConflictError(
                f"database clock moved backwards before {action}"
            ) from exc
        if cursor.rowcount != 1:
            raise CatalogConflictError(
                f"database clock moved backwards before {action}"
            )
        frozen = self._job_execution_high_water_ms()
        if frozen < max(floor, previous):
            raise CatalogConflictError(
                f"database clock moved backwards before {action}"
            )
        return frozen

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> IndexJobRecord:
        required_text_fields = (
            "job_id",
            "repository_id",
            "source_revision_id",
            "ref_name",
            "idempotency_key",
            "request_contract",
            "request_json",
            "request_digest",
            "status",
        )
        optional_text_fields = (
            "result_snapshot_id",
            "error_code",
            "error_message",
        )
        integer_fields = (
            "expected_ref_generation",
            "max_attempts",
            "cancel_requested",
            "attempt_count",
            "created_at_ms",
            "updated_at_ms",
        )
        optional_integer_fields = ("started_at_ms", "finished_at_ms")
        if (
            any(type(row[field]) is not str for field in required_text_fields)
            or any(
                row[field] is not None and type(row[field]) is not str
                for field in optional_text_fields
            )
            or any(type(row[field]) is not int for field in integer_fields)
            or any(
                row[field] is not None and type(row[field]) is not int
                for field in optional_integer_fields
            )
            or row["cancel_requested"] not in (0, 1)
        ):
            raise StorageIntegrityError(
                "persisted index job contains non-exact scalar values"
            )
        record = IndexJobRecord(
            job_id=row["job_id"],
            repository_id=row["repository_id"],
            source_revision_id=row["source_revision_id"],
            ref_name=row["ref_name"],
            idempotency_key=row["idempotency_key"],
            expected_ref_generation=row["expected_ref_generation"],
            max_attempts=row["max_attempts"],
            request_json=row["request_json"],
            request_digest=row["request_digest"],
            status=IndexJobStatus(row["status"]),
            cancel_requested=bool(row["cancel_requested"]),
            attempt_count=row["attempt_count"],
            result_snapshot_id=row["result_snapshot_id"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            created_at_ms=row["created_at_ms"],
            updated_at_ms=row["updated_at_ms"],
            started_at_ms=row["started_at_ms"],
            finished_at_ms=row["finished_at_ms"],
        )
        raw_values = (
            tuple(row[field] for field in required_text_fields)
            + tuple(row[field] for field in optional_text_fields + integer_fields)
            + tuple(row[field] for field in optional_integer_fields)
        )
        normalized_values = (
            record.job_id,
            record.repository_id,
            record.source_revision_id,
            record.ref_name,
            record.idempotency_key,
            record.request["contract"],
            record.request_json,
            record.request_digest,
            record.status.value,
            record.result_snapshot_id,
            record.error_code,
            record.error_message,
            record.expected_ref_generation,
            record.max_attempts,
            int(record.cancel_requested),
            record.attempt_count,
            record.created_at_ms,
            record.updated_at_ms,
            record.started_at_ms,
            record.finished_at_ms,
        )
        if row["request_contract"] != record.request["contract"]:
            raise StorageIntegrityError(
                f"job request contract column is inconsistent: {record.job_id}"
            )
        if raw_values != normalized_values:
            raise StorageIntegrityError(
                f"persisted index job is not canonical: {record.job_id}"
            )
        return record

    @staticmethod
    def _job_view_from_row(row: sqlite3.Row) -> IndexJobViewRecord:
        if (
            any(
                type(row[field]) is not str
                for field in ("job_id", "view_type", "profile_id", "requested_mode")
            )
            or type(row["required"]) is not int
            or row["required"] not in (0, 1)
        ):
            raise StorageIntegrityError(
                "persisted index job view contains non-exact scalar values"
            )
        record = IndexJobViewRecord(
            job_id=row["job_id"],
            view_type=row["view_type"],
            profile_id=row["profile_id"],
            requested_mode=row["requested_mode"],
            required=bool(row["required"]),
        )
        if (
            record.job_id,
            record.view_type,
            record.profile_id,
            record.requested_mode.value,
            int(record.required),
        ) != (
            row["job_id"],
            row["view_type"],
            row["profile_id"],
            row["requested_mode"],
            row["required"],
        ):
            raise StorageIntegrityError(
                "persisted index job view is not canonical: "
                f"{row['job_id']}/{row['view_type']}"
            )
        return record

    @staticmethod
    def _job_attempt_from_row(row: sqlite3.Row) -> IndexJobAttemptRecord:
        text_fields = (
            "job_id",
            "repository_id",
            "ref_name",
            "request_digest",
            "owner_id",
        )
        integer_fields = ("attempt_count", "fencing_token", "started_at_ms")
        if any(type(row[field]) is not str for field in text_fields) or any(
            type(row[field]) is not int for field in integer_fields
        ):
            raise StorageIntegrityError(
                "persisted job attempt contains non-exact scalar values"
            )
        record = IndexJobAttemptRecord(
            job_id=row["job_id"],
            attempt_count=row["attempt_count"],
            repository_id=row["repository_id"],
            ref_name=row["ref_name"],
            request_digest=row["request_digest"],
            owner_id=row["owner_id"],
            fencing_token=row["fencing_token"],
            started_at_ms=row["started_at_ms"],
        )
        if (
            record.job_id,
            record.repository_id,
            record.ref_name,
            record.request_digest,
            record.owner_id,
            record.attempt_count,
            record.fencing_token,
            record.started_at_ms,
        ) != tuple(row[field] for field in text_fields + integer_fields):
            raise StorageIntegrityError(
                f"persisted job attempt is not canonical: {record.job_id}/"
                f"{record.attempt_count}"
            )
        return record

    @staticmethod
    def _job_attempt_completion_from_row(
        row: sqlite3.Row,
    ) -> IndexJobAttemptCompletionRecord:
        text_fields = ("job_id", "owner_id", "outcome", "error_code")
        integer_fields = ("attempt_count", "fencing_token", "completed_at_ms")
        if (
            any(type(row[field]) is not str for field in text_fields)
            or any(type(row[field]) is not int for field in integer_fields)
            or (
                row["error_message"] is not None
                and type(row["error_message"]) is not str
            )
        ):
            raise StorageIntegrityError(
                "persisted job attempt completion contains non-exact scalar values"
            )
        record = IndexJobAttemptCompletionRecord(
            job_id=row["job_id"],
            attempt_count=row["attempt_count"],
            owner_id=row["owner_id"],
            fencing_token=row["fencing_token"],
            outcome=row["outcome"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            completed_at_ms=row["completed_at_ms"],
        )
        if (
            record.job_id,
            record.owner_id,
            record.outcome.value,
            record.error_code,
            record.attempt_count,
            record.fencing_token,
            record.completed_at_ms,
            record.error_message,
        ) != tuple(row[field] for field in text_fields + integer_fields) + (
            row["error_message"],
        ):
            raise StorageIntegrityError(
                "persisted job attempt completion is not canonical: "
                f"{record.job_id}/{record.attempt_count}"
            )
        return record

    @staticmethod
    def _job_event_from_row(row: sqlite3.Row) -> IndexJobEventRecord:
        required_text = (
            "job_id",
            "event_key",
            "kind",
            "owner_id",
            "payload_json",
        )
        optional_text = ("view_type", "effective_mode", "outcome")
        integer_fields = (
            "event_sequence",
            "attempt_count",
            "fencing_token",
            "created_at_ms",
        )
        if (
            any(type(row[field]) is not str for field in required_text)
            or any(
                row[field] is not None and type(row[field]) is not str
                for field in optional_text
            )
            or any(type(row[field]) is not int for field in integer_fields)
        ):
            raise StorageIntegrityError(
                "persisted index job event contains non-exact scalar values"
            )
        record = IndexJobEventRecord(
            sequence=row["event_sequence"],
            job_id=row["job_id"],
            attempt_count=row["attempt_count"],
            event_key=row["event_key"],
            kind=row["kind"],
            owner_id=row["owner_id"],
            fencing_token=row["fencing_token"],
            view_type=row["view_type"],
            effective_mode=row["effective_mode"],
            outcome=row["outcome"],
            payload_json=row["payload_json"],
            created_at_ms=row["created_at_ms"],
        )
        if (
            record.job_id,
            record.event_key,
            record.kind.value,
            record.owner_id,
            record.payload_json,
            record.view_type,
            None if record.effective_mode is None else record.effective_mode.value,
            None if record.outcome is None else record.outcome.value,
            record.sequence,
            record.attempt_count,
            record.fencing_token,
            record.created_at_ms,
        ) != tuple(
            row[field] for field in required_text + optional_text + integer_fields
        ):
            raise StorageIntegrityError(
                f"persisted index job event is not canonical: {record.sequence}"
            )
        return record

    def _job_attempt(self, job_id: str, attempt_count: int) -> IndexJobAttemptRecord:
        row = self._connection.execute(
            """
            SELECT * FROM index_job_attempts
            WHERE job_id = ? AND attempt_count = ?
            """,
            (job_id, attempt_count),
        ).fetchone()
        if row is None:
            raise CatalogNotFoundError(
                f"index job attempt not found: {job_id}/{attempt_count}"
            )
        return self._job_attempt_from_row(row)

    @staticmethod
    def _lease_from_row(row: sqlite3.Row) -> RefJobLease:
        text_fields = ("repository_id", "ref_name", "job_id", "owner_id")
        integer_fields = (
            "fencing_token",
            "acquired_at_ms",
            "heartbeat_at_ms",
            "lease_expires_at_ms",
            "updated_at_ms",
        )
        if any(type(row[field]) is not str for field in text_fields) or any(
            type(row[field]) is not int for field in integer_fields
        ):
            raise StorageIntegrityError(
                "persisted active job lease contains non-exact scalar values"
            )
        record = RefJobLease(
            repository_id=row["repository_id"],
            ref_name=row["ref_name"],
            job_id=row["job_id"],
            owner_id=row["owner_id"],
            fencing_token=row["fencing_token"],
            acquired_at_ms=row["acquired_at_ms"],
            heartbeat_at_ms=row["heartbeat_at_ms"],
            lease_expires_at_ms=row["lease_expires_at_ms"],
        )
        if (
            record.repository_id,
            record.ref_name,
            record.job_id,
            record.owner_id,
            record.fencing_token,
            record.acquired_at_ms,
            record.heartbeat_at_ms,
            record.lease_expires_at_ms,
        ) != tuple(row[field] for field in text_fields + integer_fields[:-1]):
            raise StorageIntegrityError("persisted active job lease is not canonical")
        if row["updated_at_ms"] < 0:
            raise StorageIntegrityError(
                "persisted active job lease update time is not canonical"
            )
        return record

    def _validate_persisted_lease_slot(
        self,
        row: sqlite3.Row,
    ) -> RefJobLease | None:
        """Validate one exact released or active durable lease aggregate."""

        repository_id = row["repository_id"]
        ref_name = row["ref_name"]
        if type(repository_id) is not str or type(ref_name) is not str:
            raise CatalogConflictError(
                "persisted job lease slot identity is not canonical text"
            )
        try:
            canonical_repository = _bounded_text(
                repository_id,
                "job lease repository ID",
                max_length=96,
            )
            canonical_ref = _bounded_text(
                ref_name,
                "job lease ref name",
                max_length=512,
            )
        except CatalogValidationError as exc:
            raise CatalogConflictError(
                "persisted job lease slot identity is not canonical text"
            ) from exc
        if canonical_repository != repository_id or canonical_ref != ref_name:
            raise CatalogConflictError(
                "persisted job lease slot identity is not canonical text"
            )
        fencing_token = _persisted_nonnegative_int64(
            row["fencing_token"],
            "job lease fencing token",
        )
        updated_at_ms = _persisted_nonnegative_int64(
            row["updated_at_ms"],
            "job lease updated time",
        )

        active_values = tuple(
            row[field]
            for field in (
                "job_id",
                "owner_id",
                "acquired_at_ms",
                "heartbeat_at_ms",
                "lease_expires_at_ms",
            )
        )
        if row["job_id"] is None:
            if active_values != (None, None, None, None, None):
                raise CatalogConflictError(
                    "released job lease slot retains active authority"
                )
            if fencing_token < 1:
                raise CatalogConflictError(
                    "released job lease slot has impossible causal history"
                )
            if (
                self.schema_version >= 6
                and updated_at_ms > self._job_execution_high_water_ms()
            ):
                raise CatalogConflictError(
                    "released job lease slot exceeds its durable content clock"
                )
            return None
        if any(value is None for value in active_values):
            raise CatalogConflictError("active job lease slot is incomplete")

        lease = self._lease_from_row(row)
        try:
            leased_job = self._job_from_row(
                self._require_record("index_jobs", "job_id", lease.job_id)
            )
        except CatalogNotFoundError as exc:
            raise CatalogConflictError(
                "active job lease slot points to a missing job identity"
            ) from exc
        if (
            leased_job.job_id != lease.job_id
            or leased_job.repository_id != lease.repository_id
            or leased_job.ref_name != lease.ref_name
            or (
                self.schema_version >= 6
                and leased_job.status is not IndexJobStatus.RUNNING
            )
            or leased_job.attempt_count < 1
            or leased_job.started_at_ms is None
            or row["updated_at_ms"] != lease.heartbeat_at_ms
            or row["updated_at_ms"] >= lease.lease_expires_at_ms
            or (
                self.schema_version >= 6
                and lease.acquired_at_ms > self._job_execution_high_water_ms()
            )
        ):
            raise CatalogConflictError(
                "active job lease slot points to a different job identity"
            )
        if self.schema_version >= 6:
            self._validate_current_job_attempt(leased_job, lease)
        return lease

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

    def _validate_current_job_attempt(
        self,
        job: IndexJobRecord,
        lease: RefJobLease,
        *,
        attempt_count: int | None = None,
    ) -> IndexJobAttemptRecord:
        expected_attempt = job.attempt_count if attempt_count is None else attempt_count
        try:
            attempt = self._job_attempt(job.job_id, expected_attempt)
        except CatalogNotFoundError as exc:
            raise CatalogConflictError(
                "current index job lease is missing its attempt start"
            ) from exc
        if (
            attempt.job_id != job.job_id
            or attempt.attempt_count != job.attempt_count
            or attempt.repository_id != job.repository_id
            or attempt.ref_name != job.ref_name
            or attempt.request_digest != job.request_digest
            or attempt.owner_id != lease.owner_id
            or attempt.fencing_token != lease.fencing_token
            or attempt.started_at_ms != lease.acquired_at_ms
        ):
            raise CatalogConflictError(
                "current index job attempt authority is inconsistent"
            )
        return attempt

    def _validate_job_attempt_closure_frontier(
        self,
        attempt: IndexJobAttemptRecord,
    ) -> tuple[int, int, int]:
        """Authenticate the exact immutable event prefix at attempt closure."""

        row = self._connection.execute(
            """
            SELECT * FROM index_job_attempt_closure_frontiers
            WHERE job_id = ? AND attempt_count = ?
            """,
            (attempt.job_id, attempt.attempt_count),
        ).fetchone()
        if row is None:
            raise CatalogConflictError(
                "index job attempt closure is missing its event frontier"
            )
        integer_fields = (
            "attempt_count",
            "fencing_token",
            "event_count",
            "max_event_sequence",
            "max_event_created_at_ms",
        )
        if (
            type(row["job_id"]) is not str
            or type(row["owner_id"]) is not str
            or any(type(row[field]) is not int for field in integer_fields)
            or row["job_id"] != attempt.job_id
            or row["attempt_count"] != attempt.attempt_count
            or row["owner_id"] != attempt.owner_id
            or row["fencing_token"] != attempt.fencing_token
        ):
            raise CatalogConflictError(
                "index job attempt closure frontier authority conflicts"
            )
        aggregate = self._connection.execute(
            """
            SELECT COUNT(*), COALESCE(MAX(event_sequence), 0),
                COALESCE(MAX(created_at_ms), 0)
            FROM index_job_events
            WHERE job_id = ? AND attempt_count = ?
            """,
            (attempt.job_id, attempt.attempt_count),
        ).fetchone()
        observed = tuple(aggregate)
        if any(type(value) is not int for value in observed) or observed != (
            row["event_count"],
            row["max_event_sequence"],
            row["max_event_created_at_ms"],
        ):
            raise CatalogConflictError(
                "index job attempt closure frontier conflicts with its events"
            )
        closure_rows = self._connection.execute(
            """
            SELECT completed_at_ms FROM index_job_attempt_completions
            WHERE job_id = ? AND attempt_count = ?
                AND owner_id = ? AND fencing_token = ?
            UNION ALL
            SELECT completed_at_ms FROM index_job_publications
            WHERE job_id = ? AND owner_id = ? AND fencing_token = ?
            """,
            (
                attempt.job_id,
                attempt.attempt_count,
                attempt.owner_id,
                attempt.fencing_token,
                attempt.job_id,
                attempt.owner_id,
                attempt.fencing_token,
            ),
        ).fetchall()
        if (
            len(closure_rows) != 1
            or type(closure_rows[0]["completed_at_ms"]) is not int
            or closure_rows[0]["completed_at_ms"] < observed[2]
        ):
            raise CatalogConflictError(
                "index job attempt has both success and non-success closures "
                "or its frontier conflicts with its exact closure"
            )
        return observed

    @staticmethod
    def _job_attempt_completion_response(
        job: IndexJobRecord,
        completion: IndexJobAttemptCompletionRecord,
    ) -> IndexJobRecord:
        """Reconstruct the exact job response committed with one closure."""

        status = {
            IndexJobCompletion.REQUEUE: IndexJobStatus.QUEUED,
            IndexJobCompletion.FAILED: IndexJobStatus.FAILED,
            IndexJobCompletion.CANCELLED: IndexJobStatus.CANCELLED,
        }[completion.outcome]
        return replace(
            job,
            status=status,
            cancel_requested=completion.outcome is IndexJobCompletion.CANCELLED,
            attempt_count=completion.attempt_count,
            result_snapshot_id=None,
            error_code=completion.error_code,
            error_message=completion.error_message,
            updated_at_ms=completion.completed_at_ms,
            finished_at_ms=(
                None
                if completion.outcome is IndexJobCompletion.REQUEUE
                else completion.completed_at_ms
            ),
        )

    def _job_publication_output_identities(
        self,
        job: IndexJobRecord,
        requested_views: Sequence[IndexJobViewRecord],
        outputs: tuple[IndexJobViewOutput, ...],
    ) -> tuple[dict[str, Any], ...]:
        requested = {view.view_type: view for view in requested_views}
        offered = {output.view_type: output for output in outputs}
        extras = sorted(set(offered) - set(requested))
        missing = sorted(
            view_type
            for view_type, view in requested.items()
            if view.required and view_type not in offered
        )
        mismatched = sorted(
            view_type
            for view_type, output in offered.items()
            if view_type in requested
            and output.profile_id != requested[view_type].profile_id
        )
        if extras:
            raise CatalogValidationError(
                "index job outputs include unrequested views: " + ", ".join(extras)
            )
        if missing:
            raise CatalogValidationError(
                "index job outputs are missing required views: " + ", ".join(missing)
            )
        if mismatched:
            raise CatalogValidationError(
                "index job output profile does not match its request: "
                + ", ".join(mismatched)
            )
        return tuple(
            _job_publication_output_identity(job, output) for output in outputs
        )

    def _register_job_publication_object(self, record: ObjectRecord) -> None:
        candidate = ObjectRecord(
            digest=record.digest,
            storage_key=record.storage_key,
            byte_size=record.byte_size,
            media_type=record.media_type,
        )
        if candidate != record:
            raise CatalogValidationError(
                "index job publication object metadata is not canonical"
            )
        by_digest = self._connection.execute(
            "SELECT * FROM objects WHERE digest = ?",
            (candidate.digest,),
        ).fetchone()
        by_key = self._connection.execute(
            "SELECT * FROM objects WHERE storage_key = ?",
            (candidate.storage_key,),
        ).fetchone()
        if by_digest is None and by_key is None:
            self._connection.execute(
                """
                INSERT INTO objects(
                    digest, storage_key, byte_size, media_type, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    candidate.digest,
                    candidate.storage_key,
                    candidate.byte_size,
                    candidate.media_type,
                    _now(),
                ),
            )
            by_digest = self._require_record("objects", "digest", candidate.digest)
        if (
            by_digest is None
            or by_key is not None
            and (by_key["digest"] != candidate.digest)
        ):
            raise CatalogConflictError(
                f"object storage key is already registered: {candidate.storage_key}"
            )
        actual = (
            by_digest["digest"],
            by_digest["storage_key"],
            by_digest["byte_size"],
            by_digest["media_type"],
        )
        expected = (
            candidate.digest,
            candidate.storage_key,
            candidate.byte_size,
            candidate.media_type,
        )
        if actual != expected:
            raise CatalogConflictError(
                f"object metadata is immutable: {candidate.digest}"
            )

    def _stage_job_publication_generation(
        self,
        job: IndexJobRecord,
        output: IndexJobViewOutput,
        identity: Mapping[str, Any],
    ) -> sqlite3.Row:
        profile = self._require_record("view_profiles", "profile_id", output.profile_id)
        if profile["view_type"] != output.view_type:
            raise CatalogValidationError(
                "index job output view type does not match its profile"
            )
        self._register_job_publication_object(output.object_record)
        for member in output.member_object_records:
            self._register_job_publication_object(member)

        generation_id = str(identity["view_generation_id"])
        metadata_json = canonical_json(output.generation_metadata)
        row = self._connection.execute(
            "SELECT * FROM view_generations WHERE view_generation_id = ?",
            (generation_id,),
        ).fetchone()
        if row is None:
            self._connection.execute(
                """
                INSERT INTO view_generations(
                    view_generation_id, repository_id, source_revision_id,
                    profile_id, view_type, object_digest, schema_version,
                    metadata_json, status, created_at, ready_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'staged', ?, NULL)
                """,
                (
                    generation_id,
                    job.repository_id,
                    job.source_revision_id,
                    output.profile_id,
                    output.view_type,
                    output.object_record.digest,
                    output.schema_version,
                    metadata_json,
                    _now(),
                ),
            )
            row = self._require_record(
                "view_generations", "view_generation_id", generation_id
            )
        expected = (
            job.repository_id,
            job.source_revision_id,
            output.profile_id,
            output.view_type,
            output.object_record.digest,
            output.schema_version,
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
        member_digests = [member.digest for member in output.member_object_records]
        persisted_members = [
            member["object_digest"]
            for member in self._connection.execute(
                """
                SELECT object_digest FROM view_generation_objects
                WHERE view_generation_id = ? ORDER BY object_digest
                """,
                (generation_id,),
            ).fetchall()
        ]
        if not persisted_members and member_digests:
            if row["status"] != "staged":
                raise CatalogConflictError(
                    "ready view generation member objects are missing"
                )
            self._connection.executemany(
                """
                INSERT INTO view_generation_objects(
                    view_generation_id, object_digest
                ) VALUES (?, ?)
                """,
                ((generation_id, digest) for digest in member_digests),
            )
        elif persisted_members != member_digests:
            raise CatalogConflictError(
                "view generation member object identity conflict"
            )
        row = self._require_record(
            "view_generations", "view_generation_id", generation_id
        )
        self._validate_view_generation_input(row)
        return row

    def _validate_persisted_job_publication_outputs(
        self,
        job: IndexJobRecord,
        outputs: tuple[IndexJobViewOutput, ...],
        output_identities: Sequence[Mapping[str, Any]],
        snapshot_id: str,
    ) -> tuple[list[tuple[str, str]], list[sqlite3.Row]]:
        members: list[tuple[str, str]] = []
        rows: list[sqlite3.Row] = []
        for output, identity in zip(outputs, output_identities, strict=True):
            generation_id = str(identity["view_generation_id"])
            row = self._require_record(
                "view_generations", "view_generation_id", generation_id
            )
            expected = (
                job.repository_id,
                job.source_revision_id,
                output.profile_id,
                output.view_type,
                output.object_record.digest,
                output.schema_version,
                canonical_json(output.generation_metadata),
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
                raise CatalogConflictError(
                    "persisted job publication generation conflicts"
                )
            self._validate_view_generation_input(row)
            primary = self._require_record(
                "objects", "digest", output.object_record.digest
            )
            if (
                primary["digest"],
                primary["storage_key"],
                primary["byte_size"],
                primary["media_type"],
            ) != (
                output.object_record.digest,
                output.object_record.storage_key,
                output.object_record.byte_size,
                output.object_record.media_type,
            ):
                raise CatalogConflictError("persisted job publication object conflicts")
            persisted_members = self._generation_member_objects(generation_id)
            if persisted_members != output.member_object_records:
                raise CatalogConflictError(
                    "persisted job publication member objects conflict"
                )
            members.append((output.view_type, generation_id))
            rows.append(row)

        snapshot = self._connection.execute(
            "SELECT * FROM snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        self._validate_ready_snapshot(
            snapshot,
            repository_id=job.repository_id,
            source_revision_id=job.source_revision_id,
            content_digest=snapshot_id.removeprefix("snapshot_"),
            members=members,
            view_rows=rows,
        )
        return members, rows

    def _validate_job_publication_replay(
        self,
        job: IndexJobRecord,
        *,
        owner_id: str,
        fencing_token: int,
        outputs: tuple[IndexJobViewOutput, ...],
        output_identities: Sequence[Mapping[str, Any]],
        snapshot_id: str,
    ) -> IndexJobRecord:
        publication = self._connection.execute(
            "SELECT * FROM index_job_publications WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()
        if publication is None:
            raise StorageIntegrityError(
                "successful index job has no publication closure"
            )
        text_fields = (
            "job_id",
            "repository_id",
            "source_revision_id",
            "ref_name",
            "request_digest",
            "snapshot_id",
            "ref_updated_at",
            "closure_digest",
            "closure_json",
        )
        if any(type(publication[field]) is not str for field in text_fields):
            raise CatalogConflictError(
                "job publication stored core contains non-text identity data"
            )
        persisted_expected_generation = _persisted_nonnegative_int64(
            publication["expected_ref_generation"],
            "job publication expected ref generation",
        )
        persisted_owner = publication["owner_id"]
        if type(persisted_owner) is not str:
            raise CatalogConflictError(
                "job publication stored owner is not canonical text"
            )
        try:
            canonical_owner = _bounded_text(
                persisted_owner,
                "job publication stored owner",
                max_length=256,
            )
        except CatalogValidationError as exc:
            raise CatalogConflictError(
                "job publication stored owner is not canonical text"
            ) from exc
        if canonical_owner != persisted_owner:
            raise CatalogConflictError(
                "job publication stored owner is not canonical text"
            )
        persisted_fencing_token = _persisted_positive_int64(
            publication["fencing_token"],
            "job publication fencing token",
        )
        if persisted_owner != owner_id or persisted_fencing_token != fencing_token:
            raise CatalogConflictError(
                "successful index job replay uses different fenced authority"
            )
        try:
            ref_generation = _persisted_positive_int64(
                publication["ref_generation"],
                "job publication ref generation",
            )
            ref_updated_at = _persisted_utc_timestamp(
                publication["ref_updated_at"],
                "job publication ref_updated_at",
            )
        except CatalogConflictError as exc:
            raise CatalogConflictError(
                "job publication historical ref outcome conflicts"
            ) from exc
        if type(publication["ref_changed"]) is not int or publication[
            "ref_changed"
        ] not in (0, 1):
            raise CatalogConflictError(
                "job publication historical ref outcome conflicts"
            )
        ref_changed = bool(publication["ref_changed"])
        expected_result_generation = job.expected_ref_generation + int(ref_changed)
        if (
            expected_result_generation > _SQLITE_INT64_MAX
            or ref_generation != expected_result_generation
            or not ref_changed
            and job.expected_ref_generation == 0
        ):
            raise CatalogConflictError(
                "job publication historical ref outcome conflicts"
            )
        closure_json, closure_digest = _canonical_job_publication_closure(
            job,
            owner_id=owner_id,
            fencing_token=fencing_token,
            snapshot_id=snapshot_id,
            ref_generation=ref_generation,
            ref_changed=ref_changed,
            ref_updated_at=ref_updated_at,
            output_identities=output_identities,
        )
        expected_core = (
            job.job_id,
            job.repository_id,
            job.source_revision_id,
            job.ref_name,
            job.request_digest,
            job.expected_ref_generation,
            snapshot_id,
            closure_digest,
            closure_json,
        )
        actual_core = tuple(
            publication[key]
            for key in (
                "job_id",
                "repository_id",
                "source_revision_id",
                "ref_name",
                "request_digest",
                "expected_ref_generation",
                "snapshot_id",
                "closure_digest",
                "closure_json",
            )
        )
        if (
            persisted_expected_generation != job.expected_ref_generation
            or actual_core != expected_core
        ):
            raise CatalogConflictError(
                "successful index job replay differs from its publication closure"
            )
        completed_at = publication["completed_at_ms"]
        if (
            type(completed_at) is not int
            or completed_at < 0
            or job.status is not IndexJobStatus.SUCCEEDED
            or job.result_snapshot_id != snapshot_id
            or job.finished_at_ms != completed_at
            or job.updated_at_ms != completed_at
            or job.cancel_requested
            or job.attempt_count < 1
            or job.started_at_ms is None
        ):
            raise CatalogConflictError(
                "successful index job and publication closure conflict"
            )

        if self.schema_version >= 6:
            attempt_row = self._connection.execute(
                """
                SELECT * FROM index_job_attempts
                WHERE job_id = ? AND attempt_count = ?
                """,
                (job.job_id, job.attempt_count),
            ).fetchone()
            if attempt_row is not None:
                attempt = self._job_attempt_from_row(attempt_row)
                frontier = self._validate_job_attempt_closure_frontier(attempt)
                if completed_at < frontier[2]:
                    raise CatalogConflictError(
                        "job publication precedes its exact event frontier"
                    )

        self._validate_persisted_job_publication_outputs(
            job,
            outputs,
            output_identities,
            snapshot_id,
        )
        manifest = self._manifest_summary(snapshot_id)
        self._validate_retained_ref_response_bounds(
            repository_id=job.repository_id,
            ref_name=job.ref_name,
            snapshot_id=snapshot_id,
            generation=ref_generation,
            updated_at=ref_updated_at,
            manifest=manifest,
        )
        current_ref = self._connection.execute(
            """
            SELECT snapshot_id, generation, updated_at FROM refs
            WHERE repository_id = ? AND ref_name = ?
            """,
            (job.repository_id, job.ref_name),
        ).fetchone()
        if current_ref is None:
            raise CatalogConflictError(
                "job publication historical ref outcome is missing"
            )
        current_generation = _persisted_positive_int64(
            current_ref["generation"],
            "ref generation",
        )
        _persisted_utc_timestamp(current_ref["updated_at"], "ref updated_at")
        if current_generation < ref_generation or (
            current_generation == ref_generation
            and (
                current_ref["snapshot_id"] != snapshot_id
                or current_ref["updated_at"] != ref_updated_at
            )
        ):
            raise CatalogConflictError(
                "job publication historical ref outcome conflicts"
            )
        lease = self._connection.execute(
            """
            SELECT * FROM ref_job_leases
            WHERE repository_id = ? AND ref_name = ?
            """,
            (job.repository_id, job.ref_name),
        ).fetchone()
        if lease is None:
            raise CatalogConflictError("job publication lease history conflicts")
        active_lease = self._validate_persisted_lease_slot(lease)
        lease_fencing_token = lease["fencing_token"]
        lease_updated_at = lease["updated_at_ms"]
        if lease_fencing_token < fencing_token or lease_updated_at < completed_at:
            raise CatalogConflictError("job publication lease history conflicts")
        if active_lease is None:
            if (
                lease_fencing_token == fencing_token
                and lease_updated_at != completed_at
            ):
                raise CatalogConflictError(
                    "released job lease history conflicts with publication"
                )
        elif (
            active_lease.job_id == job.job_id
            or active_lease.fencing_token <= fencing_token
            or active_lease.acquired_at_ms < completed_at
        ):
            raise CatalogConflictError(
                "active job lease history conflicts with publication"
            )
        return job

    @staticmethod
    def _publication_object_from_json(
        value: object,
        *,
        label: str,
    ) -> ObjectRecord:
        if type(value) is not dict or set(value) != {
            "digest",
            "storage_key",
            "byte_size",
            "media_type",
        }:
            raise CatalogConflictError(f"{label} has an invalid object closure")
        if tuple(
            type(value[key])
            for key in ("digest", "storage_key", "byte_size", "media_type")
        ) != (str, str, int, str):
            raise CatalogConflictError(f"{label} has an invalid object closure")
        try:
            return ObjectRecord(
                digest=value["digest"],
                storage_key=value["storage_key"],
                byte_size=value["byte_size"],
                media_type=value["media_type"],
            )
        except StorageValidationError as exc:
            raise CatalogConflictError(
                f"{label} has an invalid object closure"
            ) from exc

    def _outputs_from_publication_row(
        self,
        job: IndexJobRecord,
        publication: sqlite3.Row,
    ) -> tuple[tuple[IndexJobViewOutput, ...], tuple[dict[str, Any], ...]]:
        raw_closure = publication["closure_json"]
        if type(raw_closure) is not str:
            raise CatalogConflictError("job publication closure is not canonical")
        try:
            closure = json.loads(raw_closure)
        except (TypeError, json.JSONDecodeError) as exc:
            raise CatalogConflictError(
                "job publication closure is not canonical"
            ) from exc
        if type(closure) is not dict or set(closure) != {
            "contract",
            "job_id",
            "repository_id",
            "source_revision_id",
            "ref_name",
            "request_digest",
            "expected_ref_generation",
            "owner_id",
            "fencing_token",
            "snapshot_id",
            "ref_generation",
            "ref_changed",
            "ref_updated_at",
            "outputs",
        }:
            raise CatalogConflictError("job publication closure is not canonical")
        try:
            bounded = snapshot_retained_import_response(
                closure,
                label="index job publication closure",
            )
            canonical_closure = canonical_json(bounded)
        except (StorageIntegrityError, StorageValidationError) as exc:
            raise CatalogConflictError(
                "job publication closure is not canonical"
            ) from exc
        digest = hashlib.sha256(canonical_closure.encode("utf-8")).hexdigest()
        if (
            canonical_closure != raw_closure
            or publication["closure_digest"] != digest
            or closure["contract"] != INDEX_JOB_PUBLICATION_CONTRACT
        ):
            raise CatalogConflictError("job publication closure is not canonical")
        outputs_value = closure["outputs"]
        if (
            type(outputs_value) is not list
            or not outputs_value
            or len(outputs_value) > _MAX_JOB_PUBLICATION_OUTPUTS
        ):
            raise CatalogConflictError("job publication output closure is invalid")

        outputs: list[IndexJobViewOutput] = []
        output_identities: list[dict[str, Any]] = []
        total_members = 0
        for value in outputs_value:
            if type(value) is not dict or set(value) != {
                "view_type",
                "profile_id",
                "view_generation_id",
                "schema_version",
                "metadata",
                "object",
                "member_objects",
            }:
                raise CatalogConflictError("job publication output closure is invalid")
            if tuple(
                type(value[key])
                for key in (
                    "view_type",
                    "profile_id",
                    "view_generation_id",
                    "schema_version",
                )
            ) != (str, str, str, str):
                raise CatalogConflictError("job publication output closure is invalid")
            metadata = value["metadata"]
            members_value = value["member_objects"]
            if type(metadata) is not dict or type(members_value) is not list:
                raise CatalogConflictError("job publication output closure is invalid")
            if len(members_value) > MAX_VIEW_GENERATION_MEMBERS:
                raise CatalogConflictError(
                    "job publication output closure has too many members"
                )
            total_members += len(members_value)
            if total_members > MAX_VIEW_GENERATION_MEMBERS:
                raise CatalogConflictError(
                    "job publication closure has too many aggregate members"
                )
            primary = self._publication_object_from_json(
                value["object"],
                label="job publication primary",
            )
            members = tuple(
                self._publication_object_from_json(
                    member,
                    label="job publication member",
                )
                for member in members_value
            )
            base_metadata = dict(metadata)
            reserved_members = base_metadata.pop(
                VIEW_GENERATION_MEMBERS_METADATA_KEY,
                None,
            )
            expected_member_digests = [member.digest for member in members]
            if (members and reserved_members != expected_member_digests) or (
                not members and reserved_members is not None
            ):
                raise CatalogConflictError(
                    "job publication member metadata closure is invalid"
                )
            try:
                output = IndexJobViewOutput.create(
                    value["view_type"],
                    value["profile_id"],
                    primary,
                    schema_version=value["schema_version"],
                    metadata=base_metadata,
                    member_object_records=members,
                )
            except StorageValidationError as exc:
                raise CatalogConflictError(
                    "job publication output closure is invalid"
                ) from exc
            expected_identity = _job_publication_output_identity(job, output)
            if expected_identity != value:
                raise CatalogConflictError("job publication output identity conflicts")
            outputs.append(output)
            output_identities.append(expected_identity)
        frozen_outputs = _freeze_job_publication_outputs(tuple(outputs))
        if tuple(output.identity for output in frozen_outputs) != tuple(
            {
                key: identity[key]
                for key in (
                    "view_type",
                    "profile_id",
                    "schema_version",
                    "metadata",
                    "object",
                    "member_objects",
                )
            }
            for identity in output_identities
        ):
            raise CatalogConflictError(
                "job publication output ordering is not canonical"
            )
        return frozen_outputs, tuple(output_identities)

    def _validate_publication_aggregates(self) -> None:
        lease_slots = self._connection.execute(
            """
            SELECT * FROM ref_job_leases
            ORDER BY repository_id, ref_name
            """
        ).fetchall()
        for lease_slot in lease_slots:
            self._validate_persisted_lease_slot(lease_slot)

        missing = self._connection.execute(
            """
            SELECT job.job_id FROM index_jobs AS job
            LEFT JOIN index_job_publications AS publication
                ON publication.job_id = job.job_id
            WHERE job.status = 'succeeded' AND publication.job_id IS NULL
            LIMIT 1
            """
        ).fetchone()
        if missing is not None:
            raise CatalogConflictError(
                "successful index job is missing its publication closure: "
                f"{missing['job_id']}"
            )
        publications = self._connection.execute(
            "SELECT * FROM index_job_publications ORDER BY job_id"
        ).fetchall()
        for publication in publications:
            job = self._job_from_row(
                self._require_record(
                    "index_jobs",
                    "job_id",
                    publication["job_id"],
                )
            )
            requested = self._job_views(job)
            outputs, output_identities = self._outputs_from_publication_row(
                job,
                publication,
            )
            if (
                self._job_publication_output_identities(
                    job,
                    requested,
                    outputs,
                )
                != output_identities
            ):
                raise CatalogConflictError("job publication request closure conflicts")
            snapshot_id = _job_publication_snapshot_id(job, output_identities)
            self._validate_job_publication_replay(
                job,
                owner_id=publication["owner_id"],
                fencing_token=publication["fencing_token"],
                outputs=outputs,
                output_identities=output_identities,
                snapshot_id=snapshot_id,
            )

    def _validate_job_execution_aggregates(self) -> None:
        execution_high_water_ms = self._job_execution_high_water_ms()
        jobs: dict[str, IndexJobRecord] = {}
        requested_views: dict[str, frozenset[str]] = {}
        for row in self._connection.execute(
            "SELECT * FROM index_jobs ORDER BY job_id"
        ).fetchall():
            job = self._job_from_row(row)
            jobs[job.job_id] = job
            requested_views[job.job_id] = frozenset(
                view.view_type for view in self._job_views(job)
            )

        if self.schema_version >= 7:
            self._job_insertion_sequence_high_water()

        baselines: dict[str, int] = {}
        baseline_content_high_water: dict[str, int] = {}
        baseline_started_at: dict[str, int | None] = {}
        baseline_rows = self._connection.execute(
            """
            SELECT job_id, legacy_attempt_count, initial_created_at_ms,
                legacy_content_high_water_ms, legacy_started_at_ms
            FROM index_job_attempt_baselines ORDER BY job_id
            """
        ).fetchall()
        for row in baseline_rows:
            job_id = row["job_id"]
            baseline = row["legacy_attempt_count"]
            initial_created_at_ms = row["initial_created_at_ms"]
            legacy_content_high_water_ms = row["legacy_content_high_water_ms"]
            legacy_started_at_ms = row["legacy_started_at_ms"]
            if (
                type(job_id) is not str
                or type(baseline) is not int
                or type(initial_created_at_ms) is not int
                or type(legacy_content_high_water_ms) is not int
                or (
                    legacy_started_at_ms is not None
                    and type(legacy_started_at_ms) is not int
                )
                or job_id not in jobs
                or baseline < 0
                or baseline > jobs[job_id].attempt_count
                or initial_created_at_ms != jobs[job_id].created_at_ms
                or legacy_content_high_water_ms < initial_created_at_ms
                or legacy_content_high_water_ms > jobs[job_id].updated_at_ms
                or (
                    legacy_started_at_ms is not None
                    and (
                        legacy_started_at_ms > legacy_content_high_water_ms
                        or legacy_started_at_ms != jobs[job_id].started_at_ms
                    )
                )
            ):
                raise CatalogConflictError(
                    "persisted index job attempt baseline conflicts with its job"
                )
            baselines[job_id] = baseline
            baseline_content_high_water[job_id] = legacy_content_high_water_ms
            baseline_started_at[job_id] = legacy_started_at_ms
        if set(baselines) != set(jobs):
            raise CatalogConflictError(
                "persisted index jobs do not have exact attempt baselines"
            )

        cancellation_requests: dict[
            str, tuple[str, int, int | None, str | None, int | None, int | None]
        ] = {}
        cancellation_rows = self._connection.execute(
            """
            SELECT job_id, requested_at_ms, request_kind, attempt_count,
                owner_id, fencing_token, observed_heartbeat_at_ms
            FROM index_job_cancellation_requests ORDER BY job_id
            """
        ).fetchall()
        for row in cancellation_rows:
            job_id = row["job_id"]
            requested_at_ms = row["requested_at_ms"]
            request_kind = row["request_kind"]
            attempt_count = row["attempt_count"]
            owner_id = row["owner_id"]
            fencing_token = row["fencing_token"]
            observed_heartbeat_at_ms = row["observed_heartbeat_at_ms"]
            job = jobs.get(job_id) if type(job_id) is str else None
            null_authority = (
                attempt_count is None
                and owner_id is None
                and fencing_token is None
                and observed_heartbeat_at_ms is None
            )
            exact_running_authority = (
                type(attempt_count) is int
                and 1 <= attempt_count <= 1_000
                and type(owner_id) is str
                and 1 <= len(owner_id) <= 256
                and "\x00" not in owner_id
                and type(fencing_token) is int
                and 1 <= fencing_token <= _SQLITE_INT64_MAX
                and type(observed_heartbeat_at_ms) is int
                and observed_heartbeat_at_ms >= 0
            )
            if (
                job is None
                or type(requested_at_ms) is not int
                or request_kind not in {"legacy_v5", "queued_v6", "running_v6"}
                or not job.cancel_requested
                or requested_at_ms < job.created_at_ms
                or requested_at_ms > job.updated_at_ms
                or (request_kind in {"legacy_v5", "queued_v6"} and not null_authority)
                or (request_kind == "running_v6" and not exact_running_authority)
                or (
                    request_kind == "queued_v6"
                    and (
                        job.status is not IndexJobStatus.CANCELLED
                        or job.finished_at_ms != requested_at_ms
                    )
                )
                or (
                    request_kind == "running_v6"
                    and (
                        job.status
                        not in {IndexJobStatus.RUNNING, IndexJobStatus.CANCELLED}
                        or requested_at_ms < observed_heartbeat_at_ms
                    )
                )
            ):
                raise CatalogConflictError(
                    "persisted index job cancellation request conflicts"
                )
            cancellation_requests[job_id] = (
                request_kind,
                requested_at_ms,
                attempt_count,
                owner_id,
                fencing_token,
                observed_heartbeat_at_ms,
            )
        expected_cancellations = {
            job_id for job_id, job in jobs.items() if job.cancel_requested
        }
        if set(cancellation_requests) != expected_cancellations:
            raise CatalogConflictError(
                "persisted index job cancellation history is incomplete"
            )

        attempts: dict[tuple[str, int], IndexJobAttemptRecord] = {}
        attempts_by_job: dict[str, list[IndexJobAttemptRecord]] = {
            job_id: [] for job_id in jobs
        }
        attempt_rows = self._connection.execute(
            """
            SELECT * FROM index_job_attempts
            ORDER BY job_id, attempt_count
            """
        ).fetchall()
        for row in attempt_rows:
            attempt = self._job_attempt_from_row(row)
            job = jobs.get(attempt.job_id)
            if job is None or (
                attempt.repository_id != job.repository_id
                or attempt.ref_name != job.ref_name
                or attempt.request_digest != job.request_digest
                or attempt.attempt_count > job.max_attempts
                or attempt.fencing_token < attempt.attempt_count
                or attempt.started_at_ms < job.created_at_ms
            ):
                raise CatalogConflictError(
                    "persisted index job attempt conflicts with its job"
                )
            key = (attempt.job_id, attempt.attempt_count)
            if key in attempts:
                raise CatalogConflictError("persisted index job attempt is duplicated")
            attempts[key] = attempt
            attempts_by_job[attempt.job_id].append(attempt)

        completions: dict[tuple[str, int], IndexJobAttemptCompletionRecord] = {}
        completion_rows = self._connection.execute(
            """
            SELECT * FROM index_job_attempt_completions
            ORDER BY job_id, attempt_count
            """
        ).fetchall()
        for row in completion_rows:
            completion = self._job_attempt_completion_from_row(row)
            key = (completion.job_id, completion.attempt_count)
            attempt = attempts.get(key)
            if attempt is None or (
                completion.owner_id != attempt.owner_id
                or completion.fencing_token != attempt.fencing_token
                or completion.completed_at_ms < attempt.started_at_ms
            ):
                raise CatalogConflictError(
                    "persisted index job attempt completion conflicts"
                )
            if key in completions:
                raise CatalogConflictError(
                    "persisted index job attempt completion is duplicated"
                )
            completions[key] = completion

        publications = {
            row["job_id"]: row
            for row in self._connection.execute(
                "SELECT * FROM index_job_publications ORDER BY job_id"
            ).fetchall()
        }
        frontiers: dict[tuple[str, int], tuple[str, int, int, int, int]] = {}
        frontier_rows = self._connection.execute(
            """
            SELECT job_id, attempt_count, owner_id, fencing_token, event_count,
                max_event_sequence, max_event_created_at_ms
            FROM index_job_attempt_closure_frontiers
            ORDER BY job_id, attempt_count
            """
        ).fetchall()
        for row in frontier_rows:
            key = (row["job_id"], row["attempt_count"])
            attempt = attempts.get(key)
            owner_id = row["owner_id"]
            fencing_token = row["fencing_token"]
            event_count = row["event_count"]
            max_event_sequence = row["max_event_sequence"]
            max_event_created_at_ms = row["max_event_created_at_ms"]
            if (
                type(key[0]) is not str
                or type(key[1]) is not int
                or attempt is None
                or type(owner_id) is not str
                or owner_id != attempt.owner_id
                or type(fencing_token) is not int
                or fencing_token != attempt.fencing_token
                or type(event_count) is not int
                or not 0 <= event_count <= MAX_INDEX_JOB_EVENTS_PER_ATTEMPT
                or type(max_event_sequence) is not int
                or not 0 <= max_event_sequence <= _SQLITE_INT64_MAX
                or type(max_event_created_at_ms) is not int
                or max_event_created_at_ms < 0
                or (event_count == 0)
                != (max_event_sequence == 0 and max_event_created_at_ms == 0)
                or (event_count > 0 and max_event_sequence < 1)
                or key in frontiers
            ):
                raise CatalogConflictError(
                    "persisted index job attempt closure frontier conflicts"
                )
            frontiers[key] = (
                owner_id,
                fencing_token,
                event_count,
                max_event_sequence,
                max_event_created_at_ms,
            )

        active_leases: dict[str, RefJobLease] = {}
        active_lease_rows = self._connection.execute(
            """
            SELECT * FROM ref_job_leases
            WHERE job_id IS NOT NULL
            ORDER BY repository_id, ref_name
            """
        ).fetchall()
        for row in active_lease_rows:
            lease = self._validate_persisted_lease_slot(row)
            if lease is None or lease.job_id in active_leases:
                raise CatalogConflictError(
                    "persisted index job has non-unique active lease authority"
                )
            active_leases[lease.job_id] = lease

        for job_id, marker in cancellation_requests.items():
            (
                request_kind,
                requested_at_ms,
                marker_attempt_count,
                marker_owner_id,
                marker_fencing_token,
                observed_heartbeat_at_ms,
            ) = marker
            if request_kind == "legacy_v5":
                job = jobs[job_id]
                attempt = attempts.get((job_id, job.attempt_count))
                if (
                    job.status is IndexJobStatus.RUNNING
                    and attempt is not None
                    and requested_at_ms < attempt.started_at_ms
                ):
                    raise CatalogConflictError(
                        "persisted legacy cancellation predates its current attempt"
                    )
                continue
            if request_kind != "running_v6":
                continue
            assert marker_attempt_count is not None
            assert marker_owner_id is not None
            assert marker_fencing_token is not None
            assert observed_heartbeat_at_ms is not None
            attempt = attempts.get((job_id, marker_attempt_count))
            job = jobs[job_id]
            lease = active_leases.get(job_id)
            if (
                attempt is None
                or marker_attempt_count != job.attempt_count
                or marker_owner_id != attempt.owner_id
                or marker_fencing_token != attempt.fencing_token
                or observed_heartbeat_at_ms < attempt.started_at_ms
                or requested_at_ms < observed_heartbeat_at_ms
                or (
                    lease is not None
                    and (
                        lease.owner_id != marker_owner_id
                        or lease.fencing_token != marker_fencing_token
                        or lease.heartbeat_at_ms < observed_heartbeat_at_ms
                    )
                )
            ):
                raise CatalogConflictError(
                    "persisted running cancellation authority conflicts"
                )

        for job_id, job in jobs.items():
            baseline = baselines[job_id]
            marker = cancellation_requests.get(job_id)
            if (job.attempt_count == 0) != (job.started_at_ms is None):
                raise CatalogConflictError(
                    "persisted index job start time conflicts with its attempt count"
                )
            if job.attempt_count == 0 and job.status is IndexJobStatus.QUEUED:
                if (
                    job.cancel_requested
                    or job.result_snapshot_id is not None
                    or job.error_code is not None
                    or job.error_message is not None
                    or job.started_at_ms is not None
                    or job.finished_at_ms is not None
                    or job.updated_at_ms != job.created_at_ms
                    or baseline_content_high_water[job_id] != job.created_at_ms
                    or baseline_started_at[job_id] is not None
                    or marker is not None
                ):
                    raise CatalogConflictError(
                        "persisted initial index job state is not canonical"
                    )
            if job.attempt_count == 0 and job.status is IndexJobStatus.CANCELLED:
                marker_kind = None if marker is None else marker[0]
                expected_legacy_high_water = (
                    job.created_at_ms
                    if marker_kind == "queued_v6"
                    else job.updated_at_ms
                )
                if (
                    not job.cancel_requested
                    or job.result_snapshot_id is not None
                    or job.error_code != "cancelled"
                    or job.error_message is not None
                    or job.started_at_ms is not None
                    or job.finished_at_ms != job.updated_at_ms
                    or marker is None
                    or marker_kind not in {"legacy_v5", "queued_v6"}
                    or marker[1] != job.updated_at_ms
                    or baseline_content_high_water[job_id] != expected_legacy_high_water
                    or baseline_started_at[job_id] is not None
                ):
                    raise CatalogConflictError(
                        "persisted zero-attempt cancellation is not canonical"
                    )
            if job.status is IndexJobStatus.FAILED and job.attempt_count == 0:
                raise CatalogConflictError(
                    "failed index job has no durable attempt history"
                )
            expected_numbers = list(range(baseline + 1, job.attempt_count + 1))
            job_attempts = attempts_by_job[job_id]
            if [attempt.attempt_count for attempt in job_attempts] != expected_numbers:
                raise CatalogConflictError(
                    "persisted index job attempts do not cover post-v5 history"
                )
            legacy_started_at_ms = baseline_started_at[job_id]
            if baseline == 0 and job_attempts:
                first_attempt_started_at_ms = job_attempts[0].started_at_ms
                if (
                    job.started_at_ms != first_attempt_started_at_ms
                    or legacy_started_at_ms not in {None, first_attempt_started_at_ms}
                ):
                    raise CatalogConflictError(
                        "first index job attempt conflicts with its start witness"
                    )
            elif job_attempts and (
                job.started_at_ms is None
                or job.started_at_ms > job_attempts[0].started_at_ms
            ):
                raise CatalogConflictError(
                    "legacy index job start follows its modeled attempt"
                )
            if legacy_started_at_ms is None:
                expected_started_at_ms = (
                    None if not job_attempts else job_attempts[0].started_at_ms
                )
                if job.started_at_ms != expected_started_at_ms:
                    raise CatalogConflictError(
                        "persisted index job start time lacks exact authority"
                    )
            if baseline == job.attempt_count:
                expected_updated_at_ms = baseline_content_high_water[job_id]
                if marker is not None and marker[0] == "queued_v6":
                    expected_updated_at_ms = marker[1]
                if job.updated_at_ms != expected_updated_at_ms:
                    raise CatalogConflictError(
                        "legacy index job update time conflicts with its witness"
                    )
                if (
                    job.status
                    in {
                        IndexJobStatus.FAILED,
                        IndexJobStatus.CANCELLED,
                    }
                    and job.finished_at_ms != expected_updated_at_ms
                ):
                    raise CatalogConflictError(
                        "legacy terminal job time conflicts with its witness"
                    )
            lease = active_leases.get(job_id)
            if job.status is IndexJobStatus.RUNNING:
                if lease is None or not job_attempts:
                    raise CatalogConflictError(
                        "running index job is missing its active attempt authority"
                    )
                expected_updated_at_ms = (
                    marker[1]
                    if job.cancel_requested and marker is not None
                    else job_attempts[-1].started_at_ms
                )
                if job.updated_at_ms != expected_updated_at_ms:
                    raise CatalogConflictError(
                        "running index job update time conflicts with its authority"
                    )
            elif lease is not None:
                raise CatalogConflictError(
                    "non-running index job retains active lease authority"
                )

            previous_attempt: IndexJobAttemptRecord | None = None
            previous_completion: IndexJobAttemptCompletionRecord | None = None
            matched_publication = False
            for attempt in job_attempts:
                key = (job_id, attempt.attempt_count)
                completion = completions.get(key)
                frontier = frontiers.get(key)
                publication = publications.get(job_id)
                successful = publication is not None and (
                    publication["owner_id"] == attempt.owner_id
                    and publication["fencing_token"] == attempt.fencing_token
                )
                if completion is not None and successful:
                    raise CatalogConflictError(
                        "index job attempt has both success and non-success closures"
                    )
                if (frontier is not None) != (completion is not None or successful):
                    raise CatalogConflictError(
                        "index job attempt closure lacks its exact event frontier"
                    )
                if (
                    previous_attempt is not None
                    and previous_completion is not None
                    and (
                        attempt.started_at_ms < previous_completion.completed_at_ms
                        or attempt.fencing_token <= previous_attempt.fencing_token
                    )
                ):
                    raise CatalogConflictError(
                        "persisted index job attempt history is out of order"
                    )

                current = attempt.attempt_count == job.attempt_count
                if not current:
                    if (
                        completion is None
                        or completion.outcome is not IndexJobCompletion.REQUEUE
                        or successful
                    ):
                        raise CatalogConflictError(
                            "historical index job attempt lacks a requeue closure"
                        )
                elif job.status is IndexJobStatus.RUNNING:
                    if completion is not None or successful or lease is None:
                        raise CatalogConflictError(
                            "running index job attempt must remain open"
                        )
                    if (
                        lease.owner_id != attempt.owner_id
                        or lease.fencing_token != attempt.fencing_token
                        or lease.acquired_at_ms != attempt.started_at_ms
                    ):
                        raise CatalogConflictError(
                            "open index job attempt conflicts with its active lease"
                        )
                elif successful:
                    matched_publication = True
                    if (
                        job.status is not IndexJobStatus.SUCCEEDED
                        or publication["completed_at_ms"] < attempt.started_at_ms
                        or publication["completed_at_ms"] != job.finished_at_ms
                        or job.updated_at_ms != job.finished_at_ms
                    ):
                        raise CatalogConflictError(
                            "successful index job attempt closure conflicts"
                        )
                elif completion is None:
                    raise CatalogConflictError(
                        "closed index job attempt is missing its durable closure"
                    )
                else:
                    marker = cancellation_requests.get(job_id)
                    if (
                        completion.outcome is IndexJobCompletion.FAILED
                        and job.cancel_requested
                    ):
                        raise CatalogConflictError(
                            "cancelled index job attempt cannot fail"
                        )
                    expected_status = {
                        IndexJobCompletion.REQUEUE: IndexJobStatus.QUEUED,
                        IndexJobCompletion.FAILED: IndexJobStatus.FAILED,
                        IndexJobCompletion.CANCELLED: IndexJobStatus.CANCELLED,
                    }[completion.outcome]
                    cancelled_after_requeue = (
                        completion.outcome is IndexJobCompletion.REQUEUE
                        and job.status is IndexJobStatus.CANCELLED
                        and job.cancel_requested
                        and marker is not None
                        and marker[0] == "queued_v6"
                        and marker[1] >= completion.completed_at_ms
                        and job.error_code == "cancelled"
                        and job.error_message is None
                        and job.finished_at_ms is not None
                        and job.finished_at_ms == job.updated_at_ms
                        and job.finished_at_ms >= completion.completed_at_ms
                    )
                    exact_completion = (
                        job.status is expected_status
                        and job.error_code == completion.error_code
                        and job.error_message == completion.error_message
                        and job.updated_at_ms == completion.completed_at_ms
                        and job.finished_at_ms
                        == (
                            None
                            if completion.outcome is IndexJobCompletion.REQUEUE
                            else completion.completed_at_ms
                        )
                    )
                    if not exact_completion and not cancelled_after_requeue:
                        raise CatalogConflictError(
                            "latest job attempt completion conflicts with job state"
                        )
                    if (
                        completion.outcome is IndexJobCompletion.REQUEUE
                        and completion.attempt_count >= job.max_attempts
                    ):
                        raise CatalogConflictError(
                            "final index job attempt cannot have a requeue closure"
                        )

                previous_attempt = attempt
                previous_completion = completion

            publication = publications.get(job_id)
            if publication is not None and baseline < job.attempt_count:
                if not matched_publication:
                    raise CatalogConflictError(
                        "index job publication lacks its exact attempt authority"
                    )
            if job.status is IndexJobStatus.RUNNING and baseline == job.attempt_count:
                raise CatalogConflictError(
                    "running index job is hidden behind its legacy baseline"
                )

        events = self._connection.execute(
            "SELECT * FROM index_job_events ORDER BY event_sequence"
        ).fetchall()
        per_attempt: dict[tuple[str, int], int] = {}
        max_event_sequence: dict[tuple[str, int], int] = {}
        max_event_created_at_ms: dict[tuple[str, int], int] = {}
        for row in events:
            event = self._job_event_from_row(row)
            key = (event.job_id, event.attempt_count)
            attempt = attempts.get(key)
            if (
                attempt is None
                or event.owner_id != attempt.owner_id
                or event.fencing_token != attempt.fencing_token
                or event.created_at_ms < attempt.started_at_ms
                or event.created_at_ms < max_event_created_at_ms.get(key, 0)
            ):
                raise CatalogConflictError(
                    "persisted index job event conflicts with its attempt"
                )
            completion = completions.get(key)
            publication = publications.get(event.job_id)
            if (
                completion is not None
                and event.created_at_ms > completion.completed_at_ms
            ):
                raise CatalogConflictError(
                    "persisted index job event follows its attempt closure"
                )
            if (
                publication is not None
                and publication["owner_id"] == attempt.owner_id
                and publication["fencing_token"] == attempt.fencing_token
                and event.created_at_ms > publication["completed_at_ms"]
            ):
                raise CatalogConflictError(
                    "persisted index job event follows its publication closure"
                )
            if event.view_type is not None:
                if event.view_type not in requested_views[event.job_id]:
                    raise CatalogConflictError(
                        "persisted index job event names an unrequested view"
                    )
            per_attempt[key] = per_attempt.get(key, 0) + 1
            max_event_sequence[key] = event.sequence
            max_event_created_at_ms[key] = event.created_at_ms
            if per_attempt[key] > MAX_INDEX_JOB_EVENTS_PER_ATTEMPT:
                raise CatalogConflictError(
                    "persisted index job attempt has too many events"
                )

        for key, frontier in frontiers.items():
            if frontier[2:] != (
                per_attempt.get(key, 0),
                max_event_sequence.get(key, 0),
                max_event_created_at_ms.get(key, 0),
            ):
                raise CatalogConflictError(
                    "persisted closure frontier conflicts with its exact events"
                )

        evidence_rows = self._connection.execute(
            _INDEX_JOB_EXECUTION_WITNESS_SQL
        ).fetchall()
        modeled_high_water_ms = 0
        for row in evidence_rows:
            evidence_at_ms = _persisted_nonnegative_int64(
                row["evidence_at_ms"],
                "index job execution clock evidence",
            )
            modeled_high_water_ms = max(modeled_high_water_ms, evidence_at_ms)
        if execution_high_water_ms != modeled_high_water_ms:
            raise CatalogConflictError(
                "persisted index job execution clock conflicts with its content"
            )

        derived_time_rows = self._connection.execute(
            """
            SELECT created_at_ms AS derived_at_ms FROM index_jobs
            UNION ALL
            SELECT updated_at_ms FROM index_jobs
            UNION ALL
            SELECT started_at_ms FROM index_jobs WHERE started_at_ms IS NOT NULL
            UNION ALL
            SELECT finished_at_ms FROM index_jobs WHERE finished_at_ms IS NOT NULL
            UNION ALL
            SELECT max_event_created_at_ms
            FROM index_job_attempt_closure_frontiers
            UNION ALL
            SELECT updated_at_ms FROM ref_job_leases WHERE job_id IS NULL
            """
        ).fetchall()
        for row in derived_time_rows:
            derived_at_ms = _persisted_nonnegative_int64(
                row["derived_at_ms"],
                "derived index job execution time",
            )
            if derived_at_ms > execution_high_water_ms:
                raise CatalogConflictError(
                    "persisted index job time exceeds its durable content clock"
                )

    def _create_job_request(
        self,
        job_request: IndexJobRequest,
        *,
        require_idle_ref: bool,
    ) -> IndexJobRecord:
        """Create one validated request inside the caller's write transaction."""

        self._require_record("repositories", "repository_id", job_request.repository_id)
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
                    "idempotency key is already bound to another " "index-job request"
                )
            self._job_views(job)
            return job

        if require_idle_ref:
            active = self._connection.execute(
                """
                SELECT job_id FROM index_jobs
                WHERE repository_id = ? AND ref_name = ?
                  AND status IN ('queued', 'running')
                ORDER BY created_at_ms, job_id
                LIMIT 1
                """,
                (job_request.repository_id, job_request.ref_name),
            ).fetchone()
            if active is not None:
                raise CatalogConflictError(
                    "repository ref already has an active index job"
                )

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

        now_ms = (
            self._advance_job_execution_clock(
                causal_floor_ms=0,
                action="index job creation",
            )
            if self.schema_version >= 6
            else self._db_now_ms()
        )
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

    @_coordinated_catalog_method
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
            return self._create_job_request(
                job_request,
                require_idle_ref=False,
            )

    @_coordinated_catalog_method
    def create_job_if_idle(
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
        """Idempotently create a job only while its repository/ref is idle."""

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
            return self._create_job_request(
                job_request,
                require_idle_ref=True,
            )

    @_coordinated_catalog_method
    def get_job(self, job_id: str) -> IndexJobRecord:
        """Return one persisted index job after validating its canonical request."""
        normalized = _bounded_text(job_id, "job ID", max_length=80)
        with self._transaction(immediate=False):
            job = self._job_from_row(
                self._require_record("index_jobs", "job_id", normalized)
            )
            self._job_views(job)
            return job

    @_coordinated_catalog_method
    def get_job_views(self, job_id: str) -> tuple[IndexJobViewRecord, ...]:
        """Return the immutable requested view mapping for one index job."""
        normalized = _bounded_text(job_id, "job ID", max_length=80)
        with self._transaction(immediate=False):
            job = self._job_from_row(
                self._require_record("index_jobs", "job_id", normalized)
            )
            return self._job_views(job)

    @_coordinated_catalog_method
    def find_active_job(
        self,
        repository_id: str,
        ref_name: str = "main",
    ) -> IndexJobRecord | None:
        """Return the running or next queued job for one repository/ref."""

        repository = _bounded_text(
            repository_id,
            "repository ID",
            max_length=96,
        )
        ref = _bounded_text(ref_name, "ref name", max_length=512)
        with self._transaction(immediate=False):
            self._require_record("repositories", "repository_id", repository)
            running_rows = self._connection.execute(
                """
                SELECT * FROM index_jobs
                WHERE repository_id = ? AND ref_name = ? AND status = 'running'
                ORDER BY created_at_ms, job_id
                LIMIT 2
                """,
                (repository, ref),
            ).fetchall()
            if len(running_rows) > 1:
                raise CatalogConflictError(
                    "multiple running index jobs conflict for one repository ref"
                )
            row = running_rows[0] if running_rows else None
            if row is None:
                row = self._connection.execute(
                    """
                    SELECT * FROM index_jobs
                    WHERE repository_id = ? AND ref_name = ? AND status = 'queued'
                    ORDER BY created_at_ms, job_id
                    LIMIT 1
                    """,
                    (repository, ref),
                ).fetchone()
            if row is None:
                return None
            job = self._job_from_row(row)
            self._job_views(job)
            return job

    @_coordinated_catalog_method
    def get_job_attempt(
        self,
        job_id: str,
        attempt_count: int,
    ) -> IndexJobAttemptRecord:
        """Return one immutable post-v5 attempt start."""

        normalized_job = _bounded_text(job_id, "job ID", max_length=80)
        attempt_number = _exact_positive_integer(attempt_count, "job attempt count")
        if attempt_number > 1_000:
            raise CatalogValidationError("job attempt count is too large")
        with self._transaction(immediate=False):
            job = self._job_from_row(
                self._require_record("index_jobs", "job_id", normalized_job)
            )
            self._job_views(job)
            return self._job_attempt(job.job_id, attempt_number)

    @_coordinated_catalog_method
    def list_job_attempts(
        self,
        job_id: str,
    ) -> tuple[IndexJobAttemptRecord, ...]:
        """Return immutable post-v5 attempt starts in attempt order."""

        normalized_job = _bounded_text(job_id, "job ID", max_length=80)
        with self._transaction(immediate=False):
            job = self._job_from_row(
                self._require_record("index_jobs", "job_id", normalized_job)
            )
            self._job_views(job)
            rows = self._connection.execute(
                """
                SELECT * FROM index_job_attempts
                WHERE job_id = ? ORDER BY attempt_count
                """,
                (job.job_id,),
            ).fetchall()
            return tuple(self._job_attempt_from_row(row) for row in rows)

    @_coordinated_catalog_method
    def get_job_attempt_completion(
        self,
        job_id: str,
        attempt_count: int,
    ) -> IndexJobAttemptCompletionRecord:
        """Return one immutable post-v5 non-success attempt closure."""

        normalized_job = _bounded_text(job_id, "job ID", max_length=80)
        attempt_number = _exact_positive_integer(attempt_count, "job attempt count")
        if attempt_number > 1_000:
            raise CatalogValidationError("job attempt count is too large")
        with self._transaction(immediate=False):
            job = self._job_from_row(
                self._require_record("index_jobs", "job_id", normalized_job)
            )
            self._job_views(job)
            self._job_attempt(job.job_id, attempt_number)
            row = self._connection.execute(
                """
                SELECT * FROM index_job_attempt_completions
                WHERE job_id = ? AND attempt_count = ?
                """,
                (job.job_id, attempt_number),
            ).fetchone()
            if row is None:
                raise CatalogNotFoundError(
                    "index job attempt completion not found: "
                    f"{job.job_id}/{attempt_number}"
                )
            return self._job_attempt_completion_from_row(row)

    @_coordinated_catalog_method
    def list_job_attempt_completions(
        self,
        job_id: str,
    ) -> tuple[IndexJobAttemptCompletionRecord, ...]:
        """Return immutable non-success closures in attempt order."""

        normalized_job = _bounded_text(job_id, "job ID", max_length=80)
        with self._transaction(immediate=False):
            job = self._job_from_row(
                self._require_record("index_jobs", "job_id", normalized_job)
            )
            self._job_views(job)
            rows = self._connection.execute(
                """
                SELECT * FROM index_job_attempt_completions
                WHERE job_id = ? ORDER BY attempt_count
                """,
                (job.job_id,),
            ).fetchall()
            return tuple(self._job_attempt_completion_from_row(row) for row in rows)

    @_coordinated_catalog_method
    def begin_runnable_job_cycle(self) -> IndexJobRunnableCycle:
        """Freeze the current immutable job-insertion high-water sequence."""

        with self._transaction(immediate=False):
            return IndexJobRunnableCycle(self._job_insertion_sequence_high_water())

    @_coordinated_catalog_method
    def scan_runnable_jobs(
        self,
        *,
        cursor: IndexJobRunnableCursor | None = None,
        cycle: IndexJobRunnableCycle | None = None,
        limit: int = 64,
    ) -> IndexJobRunnablePage:
        """Return a deterministic advisory page using only SQLite's clock."""

        if cursor is not None and type(cursor) is not IndexJobRunnableCursor:
            raise CatalogValidationError("runnable job cursor must be exact")
        if cycle is not None and type(cycle) is not IndexJobRunnableCycle:
            raise CatalogValidationError("runnable job cycle must be exact")
        page_limit = _exact_positive_integer(limit, "runnable job page limit")
        if page_limit > _MAX_RUNNABLE_JOB_SCAN_LIMIT:
            raise CatalogValidationError(
                f"runnable job page limit cannot exceed {_MAX_RUNNABLE_JOB_SCAN_LIMIT}"
            )
        cursor_time = -1 if cursor is None else cursor.created_at_ms
        cursor_job = "" if cursor is None else cursor.job_id
        max_job_sequence = (
            _SQLITE_INT64_MAX if cycle is None else cycle.max_job_sequence
        )
        with self._transaction(immediate=False):
            rows = self._connection.execute(
                f"""
                SELECT job.*
                FROM index_jobs AS job
                JOIN index_job_insertion_sequences AS insertion
                    ON insertion.job_id = job.job_id
                WHERE (job.created_at_ms > ? OR (
                        job.created_at_ms = ? AND job.job_id > ?
                    ))
                    AND insertion.job_sequence <= ?
                    AND job.created_at_ms <= {_DB_NOW_MS_SQL}
                    AND job.updated_at_ms <= {_DB_NOW_MS_SQL}
                    AND (
                        (
                            job.status = 'queued'
                            AND job.cancel_requested = 0
                            AND job.attempt_count < job.max_attempts
                            AND NOT EXISTS (
                                SELECT 1 FROM ref_job_leases AS lease
                                WHERE lease.repository_id = job.repository_id
                                    AND lease.ref_name = job.ref_name
                                    AND lease.job_id IS NOT NULL
                                    AND lease.lease_expires_at_ms > {_DB_NOW_MS_SQL}
                            )
                        )
                        OR
                        (
                            job.status = 'running'
                            AND EXISTS (
                                SELECT 1 FROM ref_job_leases AS lease
                                WHERE lease.repository_id = job.repository_id
                                    AND lease.ref_name = job.ref_name
                                    AND lease.job_id = job.job_id
                                    AND lease.owner_id IS NOT NULL
                                    AND lease.fencing_token > 0
                                    AND lease.acquired_at_ms IS NOT NULL
                                    AND lease.heartbeat_at_ms IS NOT NULL
                                    AND lease.lease_expires_at_ms IS NOT NULL
                                    AND lease.lease_expires_at_ms <= {_DB_NOW_MS_SQL}
                            )
                        )
                    )
                ORDER BY job.created_at_ms, job.job_id
                LIMIT ?
                """,
                (
                    cursor_time,
                    cursor_time,
                    cursor_job,
                    max_job_sequence,
                    page_limit + 1,
                ),
            ).fetchall()
            jobs: list[IndexJobRecord] = []
            for row in rows[:page_limit]:
                job = self._job_from_row(row)
                self._job_views(job)
                if job.status is IndexJobStatus.RUNNING:
                    lease_row = self._connection.execute(
                        "SELECT * FROM ref_job_leases WHERE job_id = ?",
                        (job.job_id,),
                    ).fetchone()
                    if lease_row is None:
                        raise CatalogConflictError(
                            "runnable running job is missing its expired lease"
                        )
                    lease = self._validate_persisted_lease_slot(lease_row)
                    if lease is None:
                        raise CatalogConflictError(
                            "runnable running job lease is unexpectedly released"
                        )
                    self._validate_current_job_attempt(job, lease)
                jobs.append(job)
            next_cursor = None
            if len(rows) > page_limit:
                last = jobs[-1]
                next_cursor = IndexJobRunnableCursor(
                    created_at_ms=last.created_at_ms,
                    job_id=last.job_id,
                )
            return IndexJobRunnablePage(tuple(jobs), next_cursor)

    def _retire_expired_holder(self, job_id: str, now_ms: int) -> IndexJobStatus:
        job = self._job_from_row(self._require_record("index_jobs", "job_id", job_id))
        self._job_views(job)
        if job.status is not IndexJobStatus.RUNNING:
            if self.schema_version >= 6:
                raise CatalogConflictError(
                    "active execution lease belongs to a non-running job"
                )
            return job.status
        if now_ms < job.updated_at_ms:
            raise CatalogConflictError(
                "database clock moved backwards before expired lease retirement"
            )
        if self.schema_version < 6:
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
        lease_row = self._connection.execute(
            "SELECT * FROM ref_job_leases WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()
        if lease_row is None:
            raise CatalogConflictError("expired index job is missing its lease")
        lease = self._lease_from_row(lease_row)
        attempt = self._validate_current_job_attempt(job, lease)
        latest_event_at_ms = self._connection.execute(
            """
            SELECT MAX(created_at_ms) FROM index_job_events
            WHERE job_id = ? AND attempt_count = ?
            """,
            (job.job_id, attempt.attempt_count),
        ).fetchone()[0]
        if latest_event_at_ms is not None:
            latest_event_at_ms = _persisted_nonnegative_int64(
                latest_event_at_ms,
                "latest index job event time",
            )
        causal_floor = max(
            job.updated_at_ms,
            attempt.started_at_ms,
            lease.heartbeat_at_ms,
            0 if latest_event_at_ms is None else latest_event_at_ms,
        )
        if now_ms < causal_floor:
            raise CatalogConflictError(
                "database clock moved backwards before expired lease retirement"
            )
        if job.cancel_requested:
            outcome = IndexJobCompletion.CANCELLED
            error_code = "cancelled"
            error_message = "lease expired after cancellation was requested"
        elif job.attempt_count >= job.max_attempts:
            outcome = IndexJobCompletion.FAILED
            error_code = "attempts_exhausted"
            error_message = "lease expired on the final permitted attempt"
        else:
            outcome = IndexJobCompletion.REQUEUE
            error_code = "lease_expired"
            error_message = "previous worker lease expired; job was requeued"
        completion = IndexJobAttemptCompletionRecord(
            job_id=job.job_id,
            attempt_count=attempt.attempt_count,
            owner_id=lease.owner_id,
            fencing_token=lease.fencing_token,
            outcome=outcome,
            error_code=error_code,
            error_message=error_message,
            completed_at_ms=now_ms,
        )
        self._connection.execute(
            """
            INSERT INTO index_job_attempt_completions(
                job_id, attempt_count, owner_id, fencing_token, outcome,
                error_code, error_message, completed_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                completion.job_id,
                completion.attempt_count,
                completion.owner_id,
                completion.fencing_token,
                completion.outcome.value,
                completion.error_code,
                completion.error_message,
                completion.completed_at_ms,
            ),
        )
        return {
            IndexJobCompletion.REQUEUE: IndexJobStatus.QUEUED,
            IndexJobCompletion.FAILED: IndexJobStatus.FAILED,
            IndexJobCompletion.CANCELLED: IndexJobStatus.CANCELLED,
        }[outcome]

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

    @_coordinated_catalog_method
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
        duration = _exact_positive_integer(lease_duration_ms, "lease duration")
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

            observed_now_ms = self._db_now_ms()
            slot = self._connection.execute(
                """
                SELECT * FROM ref_job_leases
                WHERE repository_id = ? AND ref_name = ?
                """,
                (job.repository_id, job.ref_name),
            ).fetchone()
            slot_token = 0
            active_slot: RefJobLease | None = None
            if slot is not None:
                slot_token = _persisted_nonnegative_int64(
                    slot["fencing_token"], "job lease fencing token"
                )
                active_slot = self._validate_persisted_lease_slot(slot)
            if (
                active_slot is not None
                and active_slot.lease_expires_at_ms > observed_now_ms
            ):
                if (
                    active_slot.job_id == job.job_id
                    and active_slot.owner_id == owner
                    and job.status is IndexJobStatus.RUNNING
                ):
                    if self.schema_version >= 6:
                        self._validate_current_job_attempt(job, active_slot)
                    return active_slot
                raise CatalogConflictError(
                    "repository ref already has an active index-job lease"
                )

            if slot is not None and slot_token >= _SQLITE_INT64_MAX:
                raise CatalogConflictError("ref job fencing token is exhausted")

            acquisition_floor = job.updated_at_ms
            if active_slot is not None:
                acquisition_floor = max(
                    acquisition_floor,
                    active_slot.heartbeat_at_ms,
                )
            if self.schema_version >= 6:
                latest_completion_at_ms = self._connection.execute(
                    """
                    SELECT MAX(completed_at_ms)
                    FROM index_job_attempt_completions
                    WHERE job_id = ?
                    """,
                    (job.job_id,),
                ).fetchone()[0]
                if latest_completion_at_ms is not None:
                    acquisition_floor = max(
                        acquisition_floor,
                        _persisted_nonnegative_int64(
                            latest_completion_at_ms,
                            "latest index job completion time",
                        ),
                    )
            if self.schema_version >= 6:
                now_ms = self._advance_job_execution_clock(
                    causal_floor_ms=acquisition_floor,
                    action="job acquisition",
                )
                if active_slot is not None and active_slot.lease_expires_at_ms > now_ms:
                    raise CatalogConflictError(
                        "repository ref already has an active index-job lease"
                    )
            else:
                now_ms = observed_now_ms
                if now_ms < acquisition_floor:
                    raise CatalogConflictError(
                        "database clock moved backwards before job acquisition"
                    )
            if now_ms > _SQLITE_INT64_MAX - duration:
                raise CatalogConflictError(
                    "SQLite clock cannot represent the requested lease expiry"
                )

            if slot is None:
                token = 1
                if self.schema_version >= 6:
                    cursor = self._connection.execute(
                        """
                        INSERT INTO ref_job_leases(
                            repository_id, ref_name, job_id, owner_id,
                            fencing_token, acquired_at_ms, heartbeat_at_ms,
                            lease_expires_at_ms, updated_at_ms
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
                            now_ms + duration,
                            now_ms,
                        ),
                    )
                else:
                    cursor = self._connection.execute(
                        f"""
                        INSERT INTO ref_job_leases(
                            repository_id, ref_name, job_id, owner_id,
                            fencing_token, acquired_at_ms, heartbeat_at_ms,
                            lease_expires_at_ms, updated_at_ms
                        )
                        SELECT ?, ?, ?, ?, ?, {_DB_NOW_MS_SQL},
                            {_DB_NOW_MS_SQL}, {_DB_NOW_MS_SQL} + ?,
                            {_DB_NOW_MS_SQL}
                        WHERE {_DB_NOW_MS_SQL}
                            <= 9223372036854775807 - ?
                        """,
                        (
                            job.repository_id,
                            job.ref_name,
                            job.job_id,
                            owner,
                            token,
                            duration,
                            duration,
                        ),
                    )
                if cursor.rowcount != 1:
                    raise CatalogConflictError("ref job lease slot was not created")
            elif slot["job_id"] is None:
                token = slot_token + 1
                if self.schema_version >= 6:
                    cursor = self._connection.execute(
                        """
                        UPDATE ref_job_leases
                        SET job_id = ?, owner_id = ?, fencing_token = ?,
                            acquired_at_ms = ?, heartbeat_at_ms = ?,
                            lease_expires_at_ms = ?, updated_at_ms = ?
                        WHERE repository_id = ? AND ref_name = ?
                            AND job_id IS NULL AND fencing_token = ?
                        """,
                        (
                            job.job_id,
                            owner,
                            token,
                            now_ms,
                            now_ms,
                            now_ms + duration,
                            now_ms,
                            job.repository_id,
                            job.ref_name,
                            slot_token,
                        ),
                    )
                else:
                    cursor = self._connection.execute(
                        f"""
                        UPDATE ref_job_leases
                        SET job_id = ?, owner_id = ?, fencing_token = ?,
                            acquired_at_ms = {_DB_NOW_MS_SQL},
                            heartbeat_at_ms = {_DB_NOW_MS_SQL},
                            lease_expires_at_ms = {_DB_NOW_MS_SQL} + ?,
                            updated_at_ms = {_DB_NOW_MS_SQL}
                        WHERE repository_id = ? AND ref_name = ?
                            AND job_id IS NULL AND fencing_token = ?
                            AND updated_at_ms <= {_DB_NOW_MS_SQL}
                            AND {_DB_NOW_MS_SQL}
                                <= 9223372036854775807 - ?
                        """,
                        (
                            job.job_id,
                            owner,
                            token,
                            duration,
                            job.repository_id,
                            job.ref_name,
                            slot_token,
                            duration,
                        ),
                    )
                if cursor.rowcount != 1:
                    raise CatalogConflictError("empty ref job slot changed")
            else:
                old_job_id = slot["job_id"]
                retired_status = self._retire_expired_holder(old_job_id, now_ms)
                token = slot_token + 1
                current_slot = self._connection.execute(
                    """
                    SELECT * FROM ref_job_leases
                    WHERE repository_id = ? AND ref_name = ?
                    """,
                    (job.repository_id, job.ref_name),
                ).fetchone()
                if current_slot is None:
                    raise StorageIntegrityError(
                        "expired ref job lease slot disappeared during retirement"
                    )
                if old_job_id == job.job_id and retired_status in {
                    IndexJobStatus.FAILED,
                    IndexJobStatus.CANCELLED,
                }:
                    if current_slot["job_id"] is not None:
                        current = self._job_from_row(
                            self._require_record("index_jobs", "job_id", job.job_id)
                        )
                        self._release_job_slot(
                            current,
                            fencing_token=slot_token,
                            now_ms=now_ms,
                        )
                    blocked_reason = (
                        "expired lease made the index job terminal: "
                        f"{retired_status.value}"
                    )
                else:
                    if current_slot["job_id"] is None:
                        if self.schema_version >= 6:
                            cursor = self._connection.execute(
                                """
                                UPDATE ref_job_leases
                                SET job_id = ?, owner_id = ?, fencing_token = ?,
                                    acquired_at_ms = ?, heartbeat_at_ms = ?,
                                    lease_expires_at_ms = ?, updated_at_ms = ?
                                WHERE repository_id = ? AND ref_name = ?
                                    AND job_id IS NULL AND fencing_token = ?
                                """,
                                (
                                    job.job_id,
                                    owner,
                                    token,
                                    now_ms,
                                    now_ms,
                                    now_ms + duration,
                                    now_ms,
                                    job.repository_id,
                                    job.ref_name,
                                    slot_token,
                                ),
                            )
                        else:
                            cursor = self._connection.execute(
                                f"""
                                UPDATE ref_job_leases
                                SET job_id = ?, owner_id = ?, fencing_token = ?,
                                    acquired_at_ms = {_DB_NOW_MS_SQL},
                                    heartbeat_at_ms = {_DB_NOW_MS_SQL},
                                    lease_expires_at_ms = {_DB_NOW_MS_SQL} + ?,
                                    updated_at_ms = {_DB_NOW_MS_SQL}
                                WHERE repository_id = ? AND ref_name = ?
                                    AND job_id IS NULL AND fencing_token = ?
                                    AND updated_at_ms <= {_DB_NOW_MS_SQL}
                                    AND {_DB_NOW_MS_SQL}
                                        <= 9223372036854775807 - ?
                                """,
                                (
                                    job.job_id,
                                    owner,
                                    token,
                                    duration,
                                    job.repository_id,
                                    job.ref_name,
                                    slot_token,
                                    duration,
                                ),
                            )
                    else:
                        if self.schema_version >= 6:
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
                                    now_ms + duration,
                                    now_ms,
                                    job.repository_id,
                                    job.ref_name,
                                    old_job_id,
                                    slot_token,
                                    now_ms,
                                ),
                            )
                        else:
                            cursor = self._connection.execute(
                                f"""
                                UPDATE ref_job_leases
                                SET job_id = ?, owner_id = ?, fencing_token = ?,
                                    acquired_at_ms = {_DB_NOW_MS_SQL},
                                    heartbeat_at_ms = {_DB_NOW_MS_SQL},
                                    lease_expires_at_ms = {_DB_NOW_MS_SQL} + ?,
                                    updated_at_ms = {_DB_NOW_MS_SQL}
                                WHERE repository_id = ? AND ref_name = ?
                                    AND job_id = ? AND fencing_token = ?
                                    AND lease_expires_at_ms <= {_DB_NOW_MS_SQL}
                                    AND {_DB_NOW_MS_SQL}
                                        <= 9223372036854775807 - ?
                                """,
                                (
                                    job.job_id,
                                    owner,
                                    token,
                                    duration,
                                    job.repository_id,
                                    job.ref_name,
                                    old_job_id,
                                    slot_token,
                                    duration,
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
                if self.schema_version >= 6:
                    attempt = IndexJobAttemptRecord(
                        job_id=job.job_id,
                        attempt_count=job.attempt_count + 1,
                        repository_id=job.repository_id,
                        ref_name=job.ref_name,
                        request_digest=job.request_digest,
                        owner_id=lease.owner_id,
                        fencing_token=lease.fencing_token,
                        started_at_ms=lease.acquired_at_ms,
                    )
                    self._connection.execute(
                        """
                        INSERT INTO index_job_attempts(
                            job_id, attempt_count, repository_id, ref_name,
                            request_digest, owner_id, fencing_token, started_at_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            attempt.job_id,
                            attempt.attempt_count,
                            attempt.repository_id,
                            attempt.ref_name,
                            attempt.request_digest,
                            attempt.owner_id,
                            attempt.fencing_token,
                            attempt.started_at_ms,
                        ),
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
                    (lease.acquired_at_ms, lease.acquired_at_ms, job.job_id),
                )
                if cursor.rowcount != 1:
                    raise CatalogConflictError(
                        "index job could not start a new attempt"
                    )
                if self.schema_version >= 6:
                    running = self._job_from_row(
                        self._require_record("index_jobs", "job_id", job.job_id)
                    )
                    self._validate_current_job_attempt(running, lease)

        if blocked_reason is not None:
            raise CatalogConflictError(blocked_reason)
        if lease is None:
            raise AssertionError("successful lease acquisition produced no lease")
        return lease

    @_coordinated_catalog_method
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
        token = _positive_int64(fencing_token, "fencing token")
        duration = _exact_positive_integer(lease_duration_ms, "lease duration")
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
            now_ms = self._db_now_ms()
            if now_ms > _SQLITE_INT64_MAX - duration:
                raise CatalogConflictError(
                    "SQLite clock cannot represent the requested lease expiry"
                )
            cursor = self._connection.execute(
                f"""
                UPDATE ref_job_leases
                SET heartbeat_at_ms = MAX(heartbeat_at_ms, {_DB_NOW_MS_SQL}),
                    lease_expires_at_ms = CASE
                        WHEN lease_expires_at_ms = 9223372036854775807
                            THEN 9223372036854775807
                        ELSE MAX(
                            lease_expires_at_ms + 1,
                            {_DB_NOW_MS_SQL} + ?
                        )
                    END,
                    updated_at_ms = MAX(updated_at_ms, {_DB_NOW_MS_SQL})
                WHERE repository_id = ? AND ref_name = ? AND job_id = ?
                    AND owner_id = ? AND fencing_token = ?
                    AND lease_expires_at_ms < 9223372036854775807
                    AND lease_expires_at_ms > {_DB_NOW_MS_SQL}
                    AND {_DB_NOW_MS_SQL} <= 9223372036854775807 - ?
                """,
                (
                    duration,
                    job.repository_id,
                    job.ref_name,
                    job.job_id,
                    owner,
                    token,
                    duration,
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
            lease = self._lease_from_row(renewed)
            if self.schema_version >= 6:
                self._validate_current_job_attempt(job, lease)
            return lease

    @_coordinated_catalog_method
    def heartbeat_job_attempt(
        self,
        job_id: str,
        *,
        attempt_count: int,
        owner_id: str,
        fencing_token: int,
        lease_duration_ms: int,
    ) -> IndexJobAttemptHeartbeat:
        """Renew one exact attempt and read its cancellation flag atomically."""

        normalized_job = _bounded_text(job_id, "job ID", max_length=80)
        attempt_number = _exact_positive_integer(attempt_count, "job attempt count")
        owner = _bounded_text(owner_id, "owner ID", max_length=256)
        token = _positive_int64(fencing_token, "fencing token")
        duration = _exact_positive_integer(lease_duration_ms, "lease duration")
        if attempt_number > 1_000:
            raise CatalogValidationError("job attempt count is too large")
        if duration > 2_147_483_647:
            raise CatalogValidationError("lease duration is too large")

        with self._transaction():
            job = self._job_from_row(
                self._require_record("index_jobs", "job_id", normalized_job)
            )
            self._job_views(job)
            if (
                job.status is not IndexJobStatus.RUNNING
                or job.attempt_count != attempt_number
            ):
                raise CatalogConflictError(
                    "job heartbeat requires the current running attempt"
                )
            now_ms = self._db_now_ms()
            if now_ms > _SQLITE_INT64_MAX - duration:
                raise CatalogConflictError(
                    "SQLite clock cannot represent the requested lease expiry"
                )
            cursor = self._connection.execute(
                f"""
                UPDATE ref_job_leases
                SET heartbeat_at_ms = MAX(heartbeat_at_ms, {_DB_NOW_MS_SQL}),
                    lease_expires_at_ms = CASE
                        WHEN lease_expires_at_ms = 9223372036854775807
                            THEN 9223372036854775807
                        ELSE MAX(
                            lease_expires_at_ms + 1,
                            {_DB_NOW_MS_SQL} + ?
                        )
                    END,
                    updated_at_ms = MAX(updated_at_ms, {_DB_NOW_MS_SQL})
                WHERE repository_id = ? AND ref_name = ? AND job_id = ?
                    AND owner_id = ? AND fencing_token = ?
                    AND lease_expires_at_ms < 9223372036854775807
                    AND lease_expires_at_ms > {_DB_NOW_MS_SQL}
                    AND {_DB_NOW_MS_SQL} <= 9223372036854775807 - ?
                """,
                (
                    duration,
                    job.repository_id,
                    job.ref_name,
                    job.job_id,
                    owner,
                    token,
                    duration,
                ),
            )
            if cursor.rowcount != 1:
                raise CatalogConflictError(
                    "job heartbeat lost its current unexpired fenced lease"
                )
            lease_row = self._connection.execute(
                """
                SELECT * FROM ref_job_leases
                WHERE job_id = ? AND owner_id = ? AND fencing_token = ?
                """,
                (job.job_id, owner, token),
            ).fetchone()
            if lease_row is None:
                raise StorageIntegrityError("heartbeat job lease disappeared")
            lease = self._lease_from_row(lease_row)
            self._validate_current_job_attempt(
                job,
                lease,
                attempt_count=attempt_number,
            )
            return IndexJobAttemptHeartbeat(
                job_id=job.job_id,
                attempt_count=attempt_number,
                cancel_requested=job.cancel_requested,
                lease=lease,
            )

    @_coordinated_catalog_method
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
            if job.status is IndexJobStatus.QUEUED:
                slot_row = self._connection.execute(
                    """
                    SELECT * FROM ref_job_leases
                    WHERE repository_id = ? AND ref_name = ?
                    """,
                    (job.repository_id, job.ref_name),
                ).fetchone()
                cancellation_floor = job.updated_at_ms
                if slot_row is not None:
                    active_slot = self._validate_persisted_lease_slot(slot_row)
                    if active_slot is not None and active_slot.job_id == job.job_id:
                        raise StorageIntegrityError(
                            "queued index job holds an active lease"
                        )
                    if self.schema_version < 6:
                        cancellation_floor = max(
                            cancellation_floor,
                            _persisted_nonnegative_int64(
                                slot_row["updated_at_ms"],
                                "job lease updated time",
                            ),
                        )
                now_ms = (
                    self._advance_job_execution_clock(
                        causal_floor_ms=cancellation_floor,
                        action="cancellation",
                    )
                    if self.schema_version >= 6
                    else self._db_now_ms()
                )
                if self.schema_version < 6 and now_ms < cancellation_floor:
                    raise CatalogConflictError(
                        "database clock moved backwards before cancellation"
                    )
                cursor = self._connection.execute(
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
                lease_row = self._connection.execute(
                    "SELECT * FROM ref_job_leases WHERE job_id = ?",
                    (job.job_id,),
                ).fetchone()
                if lease_row is None:
                    raise StorageIntegrityError(
                        "running index job cancellation is missing its lease"
                    )
                lease = self._lease_from_row(lease_row)
                attempt_started_at_ms = lease.acquired_at_ms
                latest_event_at_ms = None
                if self.schema_version >= 6:
                    attempt = self._validate_current_job_attempt(job, lease)
                    attempt_started_at_ms = attempt.started_at_ms
                    latest_event_at_ms = self._connection.execute(
                        """
                        SELECT MAX(created_at_ms) FROM index_job_events
                        WHERE job_id = ? AND attempt_count = ?
                        """,
                        (job.job_id, job.attempt_count),
                    ).fetchone()[0]
                if (
                    latest_event_at_ms is not None
                    and type(latest_event_at_ms) is not int
                ):
                    raise StorageIntegrityError(
                        "latest index job event time is not canonical"
                    )
                causal_floor = max(
                    job.updated_at_ms,
                    attempt_started_at_ms,
                    lease.heartbeat_at_ms,
                    0 if latest_event_at_ms is None else latest_event_at_ms,
                )
                now_ms = (
                    self._advance_job_execution_clock(
                        causal_floor_ms=causal_floor,
                        action="cancellation",
                    )
                    if self.schema_version >= 6
                    else self._db_now_ms()
                )
                if self.schema_version < 6 and now_ms < causal_floor:
                    raise CatalogConflictError(
                        "database clock moved backwards before cancellation"
                    )
                cursor = self._connection.execute(
                    """
                    UPDATE index_jobs
                    SET cancel_requested = 1, updated_at_ms = ?
                    WHERE job_id = ? AND status = 'running'
                    """,
                    (now_ms, job.job_id),
                )
            if cursor.rowcount != 1:
                raise CatalogConflictError("index job cancellation state changed")
            return self._job_from_row(
                self._require_record("index_jobs", "job_id", job.job_id)
            )

    @_coordinated_catalog_method
    def _write_job_event(
        self,
        job_id: str,
        *,
        attempt_count: int,
        owner_id: str,
        fencing_token: int,
        event_key: str,
        kind: IndexJobEventKind,
        payload: Mapping[str, Any] | None,
        view_type: str | None,
        effective_mode: IndexJobEffectiveMode | None,
        outcome: IndexJobViewOutcome | None,
    ) -> IndexJobEventRecord:
        normalized_job = _bounded_text(job_id, "job ID", max_length=80)
        attempt_number = _exact_positive_integer(attempt_count, "job attempt count")
        owner = _bounded_text(owner_id, "owner ID", max_length=256)
        token = _positive_int64(fencing_token, "fencing token")
        key = _bounded_text(event_key, "job event key", max_length=128)
        if attempt_number > 1_000:
            raise CatalogValidationError("job attempt count is too large")
        try:
            event_kind = IndexJobEventKind(kind)
        except ValueError as exc:
            raise CatalogValidationError(f"invalid job event kind: {kind}") from exc
        try:
            mode = (
                None
                if effective_mode is None
                else IndexJobEffectiveMode(effective_mode)
            )
        except ValueError as exc:
            raise CatalogValidationError(
                f"invalid effective index mode: {effective_mode}"
            ) from exc
        try:
            view_outcome = None if outcome is None else IndexJobViewOutcome(outcome)
        except ValueError as exc:
            raise CatalogValidationError(
                f"invalid index job view outcome: {outcome}"
            ) from exc
        normalized_view = _optional_bounded_text(
            view_type,
            "job event view type",
            max_length=128,
        )
        if payload is not None and not isinstance(payload, Mapping):
            raise CatalogValidationError("index job event payload must be a mapping")
        payload_json = canonical_json(
            snapshot_index_job_event_payload({} if payload is None else payload)
        )
        # Validate the complete shape before any catalog lookup or mutation.
        normalized = IndexJobEventRecord(
            sequence=1,
            job_id=normalized_job,
            attempt_count=attempt_number,
            event_key=key,
            kind=event_kind,
            owner_id=owner,
            fencing_token=token,
            view_type=normalized_view,
            effective_mode=mode,
            outcome=view_outcome,
            payload_json=payload_json,
            created_at_ms=0,
        )

        with self._transaction():
            job = self._job_from_row(
                self._require_record("index_jobs", "job_id", normalized_job)
            )
            requested_views = self._job_views(job)
            existing_row = self._connection.execute(
                """
                SELECT * FROM index_job_events
                WHERE job_id = ? AND attempt_count = ? AND event_key = ?
                """,
                (normalized_job, attempt_number, key),
            ).fetchone()
            if existing_row is not None:
                existing = self._job_event_from_row(existing_row)
                if (
                    existing.kind is not normalized.kind
                    or existing.owner_id != owner
                    or existing.fencing_token != token
                    or existing.view_type != normalized.view_type
                    or existing.effective_mode is not normalized.effective_mode
                    or existing.outcome is not normalized.outcome
                    or existing.payload_json != payload_json
                ):
                    raise CatalogConflictError(
                        "index job event replay conflicts with its closure"
                    )
                return existing
            event_prefix = self._connection.execute(
                """
                SELECT COUNT(*) AS event_count,
                    COALESCE(MAX(event_sequence), 0) AS max_event_sequence
                FROM index_job_events
                WHERE job_id = ? AND attempt_count = ?
                """,
                (normalized_job, attempt_number),
            ).fetchone()
            event_count = _persisted_nonnegative_int64(
                event_prefix["event_count"],
                "index job attempt event count",
            )
            attempt_max_sequence = _persisted_nonnegative_int64(
                event_prefix["max_event_sequence"],
                "index job attempt event sequence",
            )
            if event_count >= MAX_INDEX_JOB_EVENTS_PER_ATTEMPT:
                raise CatalogConflictError(
                    "index job attempt event capacity is exhausted"
                )
            table_max_sequence = self._connection.execute(
                """
                SELECT COALESCE(MAX(event_sequence), 0)
                FROM index_job_events
                """
            ).fetchone()[0]
            table_max_sequence = _persisted_nonnegative_int64(
                table_max_sequence,
                "index job event sequence high-water",
            )
            sequence_row = self._connection.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = 'index_job_events'"
            ).fetchone()
            allocated_sequence = 0
            if sequence_row is not None:
                allocated_sequence = _persisted_nonnegative_int64(
                    sequence_row["seq"],
                    "index job event allocator high-water",
                )
            if (
                max(
                    attempt_max_sequence,
                    table_max_sequence,
                    allocated_sequence,
                )
                >= _SQLITE_INT64_MAX
            ):
                raise CatalogConflictError("index job event sequence is exhausted")
            if (
                job.status is not IndexJobStatus.RUNNING
                or job.attempt_count != attempt_number
            ):
                raise CatalogConflictError(
                    "index job events require the current running attempt"
                )
            if normalized.view_type is not None and normalized.view_type not in {
                view.view_type for view in requested_views
            }:
                raise CatalogValidationError(
                    "index job event view was not requested by the job"
                )
            duplicate_view = None
            if normalized.kind is IndexJobEventKind.VIEW_RESULT:
                duplicate_view = self._connection.execute(
                    """
                    SELECT * FROM index_job_events
                    WHERE job_id = ? AND attempt_count = ?
                        AND kind = 'view_result' AND view_type = ?
                    """,
                    (normalized_job, attempt_number, normalized.view_type),
                ).fetchone()
            if duplicate_view is not None:
                raise CatalogConflictError(
                    "index job attempt already has a result for this view"
                )
            lease_row = self._connection.execute(
                """
                SELECT * FROM ref_job_leases
                WHERE repository_id = ? AND ref_name = ? AND job_id = ?
                    AND owner_id = ? AND fencing_token = ?
                """,
                (
                    job.repository_id,
                    job.ref_name,
                    job.job_id,
                    owner,
                    token,
                ),
            ).fetchone()
            if lease_row is None:
                raise CatalogConflictError(
                    "index job event lost its current unexpired fenced lease"
                )
            lease = self._lease_from_row(lease_row)
            attempt = self._validate_current_job_attempt(
                job,
                lease,
                attempt_count=attempt_number,
            )
            latest_event_at_ms = self._connection.execute(
                """
                SELECT MAX(created_at_ms) FROM index_job_events
                WHERE job_id = ? AND attempt_count = ?
                """,
                (job.job_id, attempt_number),
            ).fetchone()[0]
            if latest_event_at_ms is not None and type(latest_event_at_ms) is not int:
                raise StorageIntegrityError(
                    "latest index job event time is not canonical"
                )
            causal_floor = max(
                job.updated_at_ms,
                attempt.started_at_ms,
                lease.heartbeat_at_ms,
                0 if latest_event_at_ms is None else latest_event_at_ms,
            )
            now_ms = self._advance_job_execution_clock(
                causal_floor_ms=causal_floor,
                action="the index job event",
            )
            if lease.lease_expires_at_ms <= now_ms:
                raise CatalogConflictError(
                    "index job event lost its current unexpired fenced lease"
                )
            insert = self._connection.execute(
                """
                INSERT INTO index_job_events(
                    job_id, attempt_count, event_key, kind, owner_id,
                    fencing_token, view_type, effective_mode, outcome,
                    payload_json, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized.job_id,
                    normalized.attempt_count,
                    normalized.event_key,
                    normalized.kind.value,
                    normalized.owner_id,
                    normalized.fencing_token,
                    normalized.view_type,
                    (
                        None
                        if normalized.effective_mode is None
                        else normalized.effective_mode.value
                    ),
                    None if normalized.outcome is None else normalized.outcome.value,
                    normalized.payload_json,
                    now_ms,
                ),
            )
            row = self._connection.execute(
                "SELECT * FROM index_job_events WHERE event_sequence = ?",
                (insert.lastrowid,),
            ).fetchone()
            if row is None:
                raise StorageIntegrityError("inserted index job event disappeared")
            return self._job_event_from_row(row)

    def append_job_event(
        self,
        job_id: str,
        *,
        attempt_count: int,
        owner_id: str,
        fencing_token: int,
        event_key: str,
        payload: Mapping[str, Any] | None = None,
        view_type: str | None = None,
    ) -> IndexJobEventRecord:
        """Append or exactly replay one bounded progress event."""

        return self._write_job_event(
            job_id,
            attempt_count=attempt_count,
            owner_id=owner_id,
            fencing_token=fencing_token,
            event_key=event_key,
            kind=IndexJobEventKind.PROGRESS,
            payload=payload,
            view_type=view_type,
            effective_mode=None,
            outcome=None,
        )

    def record_job_view_result(
        self,
        job_id: str,
        *,
        attempt_count: int,
        owner_id: str,
        fencing_token: int,
        event_key: str,
        view_type: str,
        effective_mode: IndexJobEffectiveMode,
        outcome: IndexJobViewOutcome,
        payload: Mapping[str, Any] | None = None,
    ) -> IndexJobEventRecord:
        """Append or exactly replay one terminal attempt-local view result."""

        return self._write_job_event(
            job_id,
            attempt_count=attempt_count,
            owner_id=owner_id,
            fencing_token=fencing_token,
            event_key=event_key,
            kind=IndexJobEventKind.VIEW_RESULT,
            payload=payload,
            view_type=view_type,
            effective_mode=effective_mode,
            outcome=outcome,
        )

    @_coordinated_catalog_method
    def list_job_events(
        self,
        job_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 128,
    ) -> tuple[IndexJobEventRecord, ...]:
        """Return one bounded, stable sequence page for a canonical job."""

        normalized_job = _bounded_text(job_id, "job ID", max_length=80)
        after = _nonnegative_int64(after_sequence, "job event sequence cursor")
        page_limit = _exact_positive_integer(limit, "job event page limit")
        if after > _SQLITE_INT64_MAX:
            raise CatalogValidationError("job event sequence cursor is too large")
        if page_limit > _MAX_JOB_EVENT_PAGE_LIMIT:
            raise CatalogValidationError(
                f"job event page limit cannot exceed {_MAX_JOB_EVENT_PAGE_LIMIT}"
            )
        with self._transaction(immediate=False):
            job = self._job_from_row(
                self._require_record("index_jobs", "job_id", normalized_job)
            )
            self._job_views(job)
            rows = self._connection.execute(
                """
                SELECT * FROM index_job_events
                WHERE job_id = ? AND event_sequence > ?
                ORDER BY event_sequence
                LIMIT ?
                """,
                (normalized_job, after, page_limit),
            ).fetchall()
            return tuple(self._job_event_from_row(row) for row in rows)

    @_coordinated_catalog_method
    def complete_job_attempt(
        self,
        job_id: str,
        *,
        attempt_count: int,
        owner_id: str,
        fencing_token: int,
        outcome: IndexJobCompletion,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> IndexJobRecord:
        """Persist or exactly replay one immutable non-success closure."""
        normalized_job = _bounded_text(job_id, "job ID", max_length=80)
        attempt_number = _exact_positive_integer(attempt_count, "job attempt count")
        owner = _bounded_text(owner_id, "owner ID", max_length=256)
        token = _positive_int64(fencing_token, "fencing token")
        if attempt_number > 1_000:
            raise CatalogValidationError("job attempt count is too large")
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
        assert code is not None

        with self._transaction():
            job = self._job_from_row(
                self._require_record("index_jobs", "job_id", normalized_job)
            )
            self._job_views(job)
            existing_row = self._connection.execute(
                """
                SELECT * FROM index_job_attempt_completions
                WHERE job_id = ? AND attempt_count = ?
                """,
                (job.job_id, attempt_number),
            ).fetchone()
            if existing_row is not None:
                existing = self._job_attempt_completion_from_row(existing_row)
                if (
                    existing.owner_id != owner
                    or existing.fencing_token != token
                    or existing.outcome is not normalized_outcome
                    or existing.error_code != code
                    or existing.error_message != message
                ):
                    raise CatalogConflictError(
                        "job attempt completion replay conflicts with its closure"
                    )
                attempt = self._job_attempt(job.job_id, attempt_number)
                frontier = self._validate_job_attempt_closure_frontier(attempt)
                if existing.completed_at_ms < frontier[2]:
                    raise CatalogConflictError(
                        "job attempt completion precedes its event frontier"
                    )
                return self._job_attempt_completion_response(job, existing)
            if (
                job.status is not IndexJobStatus.RUNNING
                or job.attempt_count != attempt_number
            ):
                raise CatalogConflictError(
                    "index job mutation requires its current unexpired fenced lease"
                )
            if normalized_outcome is IndexJobCompletion.CANCELLED and not (
                job.cancel_requested
            ):
                raise CatalogConflictError(
                    "running index job must receive cancellation before acknowledging it"
                )
            if (
                normalized_outcome
                in {IndexJobCompletion.REQUEUE, IndexJobCompletion.FAILED}
                and job.cancel_requested
            ):
                raise CatalogConflictError(
                    "cancelled index job attempt cannot be "
                    + (
                        "requeued"
                        if normalized_outcome is IndexJobCompletion.REQUEUE
                        else "failed"
                    )
                )
            if (
                normalized_outcome is IndexJobCompletion.REQUEUE
                and job.attempt_count >= job.max_attempts
            ):
                raise CatalogConflictError(
                    "final index job attempt must fail instead of requeueing"
                )
            lease_row = self._connection.execute(
                """
                SELECT * FROM ref_job_leases
                WHERE repository_id = ? AND ref_name = ? AND job_id = ?
                    AND owner_id = ? AND fencing_token = ?
                """,
                (
                    job.repository_id,
                    job.ref_name,
                    job.job_id,
                    owner,
                    token,
                ),
            ).fetchone()
            if lease_row is None:
                raise CatalogConflictError(
                    "index job mutation requires its current unexpired fenced lease"
                )
            lease = self._lease_from_row(lease_row)
            attempt = self._validate_current_job_attempt(
                job,
                lease,
                attempt_count=attempt_number,
            )
            latest_event_at_ms = self._connection.execute(
                """
                SELECT MAX(created_at_ms) FROM index_job_events
                WHERE job_id = ? AND attempt_count = ?
                """,
                (job.job_id, attempt_number),
            ).fetchone()[0]
            if latest_event_at_ms is not None and type(latest_event_at_ms) is not int:
                raise StorageIntegrityError(
                    "latest index job event time is not canonical"
                )
            causal_floor = max(
                job.updated_at_ms,
                attempt.started_at_ms,
                lease.heartbeat_at_ms,
                0 if latest_event_at_ms is None else latest_event_at_ms,
            )
            completed_at_ms = self._advance_job_execution_clock(
                causal_floor_ms=causal_floor,
                action="job completion",
            )
            if lease.lease_expires_at_ms <= completed_at_ms:
                raise CatalogConflictError(
                    "index job mutation requires its current unexpired fenced lease"
                )
            completion = IndexJobAttemptCompletionRecord(
                job_id=job.job_id,
                attempt_count=attempt_number,
                owner_id=owner,
                fencing_token=token,
                outcome=normalized_outcome,
                error_code=code,
                error_message=message,
                completed_at_ms=completed_at_ms,
            )
            self._connection.execute(
                """
                INSERT INTO index_job_attempt_completions(
                    job_id, attempt_count, owner_id, fencing_token, outcome,
                    error_code, error_message, completed_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    completion.job_id,
                    completion.attempt_count,
                    completion.owner_id,
                    completion.fencing_token,
                    completion.outcome.value,
                    completion.error_code,
                    completion.error_message,
                    completion.completed_at_ms,
                ),
            )
            self._validate_job_attempt_closure_frontier(attempt)
            return self._job_attempt_completion_response(job, completion)

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
        """Compatibility wrapper for the pre-v6 non-success API."""

        job = self.get_job(job_id)
        return self.complete_job_attempt(
            job.job_id,
            attempt_count=job.attempt_count,
            owner_id=owner_id,
            fencing_token=fencing_token,
            outcome=outcome,
            error_code=error_code,
            error_message=error_message,
        )

    @_coordinated_catalog_method
    def publish_job_outputs(
        self,
        job_id: str,
        *,
        owner_id: str,
        fencing_token: int,
        outputs: tuple[IndexJobViewOutput, ...],
    ) -> IndexJobRecord:
        """Atomically close one fenced job publication over immutable outputs."""

        if type(job_id) is not str or type(owner_id) is not str:
            raise CatalogValidationError(
                "job publication authority must use exact text values"
            )
        if type(fencing_token) is not int:
            raise CatalogValidationError(
                "job publication fencing token must be an exact integer"
            )
        normalized_job = _bounded_text(job_id, "job ID", max_length=80)
        owner = _bounded_text(owner_id, "owner ID", max_length=256)
        token = _positive_int64(fencing_token, "fencing token")
        if token > _SQLITE_INT64_MAX:
            raise CatalogValidationError("fencing token exceeds catalog int64 range")
        frozen_outputs = _freeze_job_publication_outputs(outputs)

        completed: IndexJobRecord | None = None
        with self._transaction():
            job_row = self._require_record("index_jobs", "job_id", normalized_job)
            job = self._job_from_row(job_row)
            requested_views = self._job_views(job)
            output_identities = self._job_publication_output_identities(
                job,
                requested_views,
                frozen_outputs,
            )
            snapshot_id = _job_publication_snapshot_id(job, output_identities)

            # A successful retry has no live lease: authenticate the immutable
            # historical closure before consulting the current lease slot.
            if job.status is IndexJobStatus.SUCCEEDED:
                completed = self._validate_job_publication_replay(
                    job,
                    owner_id=owner,
                    fencing_token=token,
                    outputs=frozen_outputs,
                    output_identities=output_identities,
                    snapshot_id=snapshot_id,
                )
            else:
                if job.status is not IndexJobStatus.RUNNING or job.cancel_requested:
                    raise CatalogConflictError(
                        "index job publication requires a running uncancelled job"
                    )
                repository = self._require_record(
                    "repositories", "repository_id", job.repository_id
                )
                source = self._require_record(
                    "source_revisions", "source_revision_id", job.source_revision_id
                )
                if source["repository_id"] != job.repository_id:
                    raise CatalogConflictError(
                        "index job source revision belongs to another repository"
                    )
                self._validate_repository_source_identity(repository, source)

                lease_row = self._connection.execute(
                    """
                    SELECT * FROM ref_job_leases
                    WHERE repository_id = ? AND ref_name = ?
                    """,
                    (job.repository_id, job.ref_name),
                ).fetchone()
                if (
                    lease_row is None
                    or lease_row["job_id"] != job.job_id
                    or lease_row["owner_id"] != owner
                    or lease_row["fencing_token"] != token
                    or type(lease_row["lease_expires_at_ms"]) is not int
                ):
                    raise CatalogConflictError(
                        "index job publication requires its current unexpired fenced lease"
                    )
                current_lease = self._lease_from_row(lease_row)
                if (
                    current_lease.repository_id != job.repository_id
                    or current_lease.ref_name != job.ref_name
                    or current_lease.job_id != job.job_id
                    or current_lease.owner_id != owner
                    or current_lease.fencing_token != token
                ):
                    raise CatalogConflictError(
                        "index job publication lease identity conflicts"
                    )

                current_ref = self._connection.execute(
                    """
                    SELECT snapshot_id, generation, updated_at FROM refs
                    WHERE repository_id = ? AND ref_name = ?
                    """,
                    (job.repository_id, job.ref_name),
                ).fetchone()
                if current_ref is None:
                    current_generation = 0
                else:
                    current_generation = _persisted_positive_int64(
                        current_ref["generation"], "ref generation"
                    )
                    _persisted_utc_timestamp(
                        current_ref["updated_at"], "ref updated_at"
                    )
                if current_generation != job.expected_ref_generation:
                    raise CatalogConflictError(
                        f"ref {job.ref_name!r} generation is {current_generation}; "
                        f"expected {job.expected_ref_generation}"
                    )

                generation_rows = [
                    self._stage_job_publication_generation(job, output, identity)
                    for output, identity in zip(
                        frozen_outputs,
                        output_identities,
                        strict=True,
                    )
                ]
                snapshot_members = [
                    (str(identity["view_type"]), str(identity["view_generation_id"]))
                    for identity in output_identities
                ]
                snapshot = self._connection.execute(
                    "SELECT * FROM snapshots WHERE snapshot_id = ?",
                    (snapshot_id,),
                ).fetchone()
                snapshot_published_at = _now()
                if snapshot is None:
                    self._connection.execute(
                        """
                        INSERT INTO snapshots(
                            snapshot_id, repository_id, source_revision_id,
                            content_digest, status, published_at
                        ) VALUES (?, ?, ?, ?, 'building', NULL)
                        """,
                        (
                            snapshot_id,
                            job.repository_id,
                            job.source_revision_id,
                            snapshot_id.removeprefix("snapshot_"),
                        ),
                    )
                    for view_type, generation_id in snapshot_members:
                        self._connection.execute(
                            """
                            INSERT INTO snapshot_views(
                                snapshot_id, view_type, view_generation_id
                            ) VALUES (?, ?, ?)
                            """,
                            (snapshot_id, view_type, generation_id),
                        )
                        self._connection.execute(
                            """
                            UPDATE view_generations
                            SET status = 'ready', ready_at = ?
                            WHERE view_generation_id = ? AND status = 'staged'
                            """,
                            (snapshot_published_at, generation_id),
                        )
                    seal = self._connection.execute(
                        """
                        UPDATE snapshots SET status = 'ready', published_at = ?
                        WHERE snapshot_id = ? AND status = 'building'
                        """,
                        (snapshot_published_at, snapshot_id),
                    )
                    if seal.rowcount != 1:
                        raise CatalogConflictError(
                            "job publication snapshot could not be sealed"
                        )
                    snapshot = self._connection.execute(
                        "SELECT * FROM snapshots WHERE snapshot_id = ?",
                        (snapshot_id,),
                    ).fetchone()
                    generation_rows = [
                        self._require_record(
                            "view_generations",
                            "view_generation_id",
                            generation_id,
                        )
                        for _view_type, generation_id in snapshot_members
                    ]
                self._validate_ready_snapshot(
                    snapshot,
                    repository_id=job.repository_id,
                    source_revision_id=job.source_revision_id,
                    content_digest=snapshot_id.removeprefix("snapshot_"),
                    members=snapshot_members,
                    view_rows=generation_rows,
                )
                self._validate_persisted_job_publication_outputs(
                    job,
                    frozen_outputs,
                    output_identities,
                    snapshot_id,
                )

                ref_changed = not (
                    current_ref is not None
                    and current_ref["snapshot_id"] == snapshot_id
                )
                if ref_changed:
                    if current_generation == _SQLITE_INT64_MAX:
                        raise CatalogConflictError(
                            "ref generation cannot be incremented"
                        )
                    result_generation = current_generation + 1
                    ref_updated_at = _now()
                else:
                    if current_generation < 1 or current_ref is None:
                        raise CatalogConflictError(
                            "an unchanged job publication requires an existing ref"
                        )
                    result_generation = current_generation
                    ref_updated_at = str(current_ref["updated_at"])

                manifest = self._manifest_summary(snapshot_id)
                self._validate_retained_ref_response_bounds(
                    repository_id=job.repository_id,
                    ref_name=job.ref_name,
                    snapshot_id=snapshot_id,
                    generation=result_generation,
                    updated_at=ref_updated_at,
                    manifest=manifest,
                )
                closure_json, closure_digest = _canonical_job_publication_closure(
                    job,
                    owner_id=owner,
                    fencing_token=token,
                    snapshot_id=snapshot_id,
                    ref_generation=result_generation,
                    ref_changed=ref_changed,
                    ref_updated_at=ref_updated_at,
                    output_identities=output_identities,
                )

                if ref_changed and current_ref is None:
                    self._connection.execute(
                        """
                        INSERT INTO refs(
                            repository_id, ref_name, snapshot_id,
                            generation, updated_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            job.repository_id,
                            job.ref_name,
                            snapshot_id,
                            result_generation,
                            ref_updated_at,
                        ),
                    )
                elif ref_changed:
                    moved = self._connection.execute(
                        """
                        UPDATE refs
                        SET snapshot_id = ?, generation = ?, updated_at = ?
                        WHERE repository_id = ? AND ref_name = ? AND generation = ?
                        """,
                        (
                            snapshot_id,
                            result_generation,
                            ref_updated_at,
                            job.repository_id,
                            job.ref_name,
                            current_generation,
                        ),
                    )
                    if moved.rowcount != 1:
                        raise CatalogConflictError(
                            f"ref {job.ref_name!r} changed during job publication"
                        )

                latest_event_at_ms = None
                if self.schema_version >= 6:
                    latest_event_at_ms = self._connection.execute(
                        """
                        SELECT MAX(created_at_ms) FROM index_job_events
                        WHERE job_id = ? AND attempt_count = ?
                        """,
                        (job.job_id, job.attempt_count),
                    ).fetchone()[0]
                if (
                    latest_event_at_ms is not None
                    and type(latest_event_at_ms) is not int
                ):
                    raise StorageIntegrityError(
                        "latest index job event time is not canonical"
                    )
                causal_floor = max(
                    job.updated_at_ms,
                    current_lease.heartbeat_at_ms,
                    0 if latest_event_at_ms is None else latest_event_at_ms,
                )
                completed_at_ms = (
                    self._advance_job_execution_clock(
                        causal_floor_ms=causal_floor,
                        action="job publication",
                    )
                    if self.schema_version >= 6
                    else self._db_now_ms()
                )
                if self.schema_version < 6 and completed_at_ms < causal_floor:
                    raise CatalogConflictError(
                        "database clock moved backwards during job publication"
                    )
                if current_lease.lease_expires_at_ms <= completed_at_ms:
                    raise CatalogConflictError(
                        "index job publication requires its current unexpired fenced lease"
                    )
                self._connection.execute(
                    """
                    INSERT INTO index_job_publications(
                        job_id, repository_id, source_revision_id, ref_name,
                        request_digest, owner_id, fencing_token,
                        expected_ref_generation, snapshot_id, ref_generation,
                        ref_changed, ref_updated_at, closure_digest, closure_json,
                        completed_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job.job_id,
                        job.repository_id,
                        job.source_revision_id,
                        job.ref_name,
                        job.request_digest,
                        owner,
                        token,
                        job.expected_ref_generation,
                        snapshot_id,
                        result_generation,
                        int(ref_changed),
                        ref_updated_at,
                        closure_digest,
                        closure_json,
                        completed_at_ms,
                    ),
                )
                succeeded = self._job_from_row(
                    self._require_record("index_jobs", "job_id", job.job_id)
                )
                completed = self._validate_job_publication_replay(
                    succeeded,
                    owner_id=owner,
                    fencing_token=token,
                    outputs=frozen_outputs,
                    output_identities=output_identities,
                    snapshot_id=snapshot_id,
                )

        if completed is None:
            raise AssertionError("job publication produced no completed job")
        return completed

    @_coordinated_catalog_method
    def create_namespace(self, name: str) -> str:
        """Create an idempotent logical namespace and return its stable ID."""
        namespace = NamespaceIdentity(name)
        normalized = namespace.name
        namespace_id = namespace.namespace_id
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

    @_coordinated_catalog_method
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

    @_coordinated_catalog_method
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

    @_coordinated_catalog_method
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

    @_coordinated_catalog_method
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

    @_coordinated_catalog_method
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
        member_object_digests: Sequence[str] = (),
    ) -> str:
        """Stage a view generation backed by one primary and optional members.

        Member objects are immutable reachability edges for compound views.
        Their canonical digest list is injected into generation metadata, so
        the list participates in the generation identity and cannot be changed
        independently from the primary manifest object.
        """
        repository = _required_text(repository_id, "repository ID")
        source = _required_text(source_revision_id, "source revision ID")
        profile = _required_text(profile_id, "profile ID")
        normalized_view_type = _required_text(view_type, "view type")
        digest = normalize_digest(object_digest)
        normalized_schema_version = _required_text(
            schema_version, "view schema version"
        )
        (
            normalized_metadata,
            normalized_member_tuple,
        ) = normalize_view_generation_metadata(
            digest,
            metadata,
            member_object_digests=member_object_digests,
        )
        normalized_members = list(normalized_member_tuple)
        metadata_json = canonical_json(normalized_metadata)
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
            for member_digest in normalized_members:
                self._require_record("objects", "digest", member_digest)
            row = self._connection.execute(
                "SELECT * FROM view_generations WHERE view_generation_id = ?",
                (view_generation_id,),
            ).fetchone()
            if row is None:
                self._connection.execute(
                    """
                    INSERT INTO view_generations(
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
            if self.schema_version < 4:
                if normalized_members:
                    raise CatalogValidationError(
                        "member objects require catalog schema version 4"
                    )
                member_rows = []
            else:
                member_rows = self._connection.execute(
                    """
                    SELECT object_digest FROM view_generation_objects
                    WHERE view_generation_id = ? ORDER BY object_digest
                    """,
                    (view_generation_id,),
                ).fetchall()
            persisted_members = [member["object_digest"] for member in member_rows]
            if not member_rows and normalized_members:
                if row["status"] != "staged":
                    raise CatalogConflictError(
                        "ready view generation member objects are missing"
                    )
                for member_digest in normalized_members:
                    self._connection.execute(
                        """
                        INSERT INTO view_generation_objects(
                            view_generation_id, object_digest
                        ) VALUES (?, ?)
                        """,
                        (view_generation_id, member_digest),
                    )
            elif persisted_members != normalized_members:
                raise CatalogConflictError(
                    "view generation member object identity conflict"
                )
        return view_generation_id

    @_coordinated_catalog_method
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
        if expected_generation > _SQLITE_INT64_MAX:
            raise CatalogValidationError(
                "expected generation must fit in a signed 64-bit integer"
            )
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
            repository_row = self._require_record(
                "repositories", "repository_id", repository
            )
            source_row = self._require_record(
                "source_revisions", "source_revision_id", source
            )
            if source_row["repository_id"] != repository:
                raise CatalogValidationError(
                    "snapshot source revision belongs to another repository"
                )
            self._validate_repository_source_identity(repository_row, source_row)

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
            if current_ref is None:
                current_generation = 0
            else:
                try:
                    current_generation = _persisted_positive_int64(
                        current_ref["generation"], "ref generation"
                    )
                    _persisted_utc_timestamp(
                        current_ref["updated_at"], "ref updated_at"
                    )
                except CatalogConflictError as exc:
                    raise CatalogConflictError(
                        f"ref {normalized_ref!r} has invalid publication metadata"
                    ) from exc

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
                summary = self._manifest_summary(snapshot_id)
                self._validate_retained_ref_response_bounds(
                    repository_id=repository,
                    ref_name=normalized_ref,
                    snapshot_id=snapshot_id,
                    generation=current_generation,
                    updated_at=current_ref["updated_at"],
                    manifest=summary,
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
                existing_snapshot = self._connection.execute(
                    "SELECT * FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
                ).fetchone()
                rows = self._connection.execute(
                    f"""
                    SELECT * FROM view_generations
                    WHERE view_generation_id IN ({placeholders})
                    """,
                    view_ids,
                ).fetchall()
                self._validate_ready_snapshot(
                    existing_snapshot,
                    repository_id=repository,
                    source_revision_id=source,
                    content_digest=content_digest,
                    members=members,
                    view_rows=rows,
                )
            else:
                self._validate_ready_snapshot(
                    existing_snapshot,
                    repository_id=repository,
                    source_revision_id=source,
                    content_digest=content_digest,
                    members=members,
                    view_rows=rows,
                )

            if current_generation == _SQLITE_INT64_MAX:
                raise CatalogConflictError("ref generation cannot be incremented")
            next_generation = current_generation + 1
            summary = self._manifest_summary(snapshot_id)
            self._validate_retained_ref_response_bounds(
                repository_id=repository,
                ref_name=normalized_ref,
                snapshot_id=snapshot_id,
                generation=next_generation,
                updated_at=published_at,
                manifest=summary,
            )
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

    @staticmethod
    def _validate_retained_ref_response_bounds(
        *,
        repository_id: str,
        ref_name: str,
        snapshot_id: str,
        generation: int,
        updated_at: str,
        manifest: dict[str, Any],
    ) -> None:
        try:
            snapshot_retained_import_response(
                {
                    "repository_id": repository_id,
                    "ref_name": ref_name,
                    "snapshot_id": snapshot_id,
                    "generation": generation,
                    "updated_at": updated_at,
                    "manifest": manifest,
                },
                label="retained ref resolution",
            )
        except StorageIntegrityError as exc:
            raise CatalogValidationError(
                "snapshot exceeds retained-import response bounds"
            ) from exc

    def _validate_repository_source_identity(
        self, repository_row: sqlite3.Row, source_row: sqlite3.Row
    ) -> None:
        """Recompute the content identities that bind a publication source."""
        namespace_row = self._require_record(
            "namespaces", "namespace_id", repository_row["namespace_id"]
        )
        try:
            namespace_name = _required_text(namespace_row["name"], "namespace name")
            expected_namespace_id = NamespaceIdentity(namespace_name).namespace_id
            repository_identity = RepositoryIdentity(
                namespace_id=repository_row["namespace_id"],
                repository_key=repository_row["repository_key"],
            )
            source_identity = SourceRevision(
                repository_id=source_row["repository_id"],
                source_kind=source_row["source_kind"],
                commit_sha=source_row["commit_sha"],
                tree_sha=source_row["tree_sha"],
                source_fingerprint=source_row["source_fingerprint"],
            )
        except (TypeError, CatalogValidationError) as exc:
            raise CatalogConflictError(
                "repository or source revision identity conflicts"
            ) from exc
        expected_source_id = source_identity.source_revision_id
        if (
            namespace_row["namespace_id"] != expected_namespace_id
            or namespace_row["name"] != namespace_name
            or repository_row["repository_id"] != repository_identity.repository_id
            or repository_row["namespace_id"] != repository_identity.namespace_id
            or repository_row["repository_key"] != repository_identity.repository_key
            or source_row["repository_id"] != repository_row["repository_id"]
            or source_row["source_revision_id"] != expected_source_id
            or source_row["identity_digest"] != expected_source_id.removeprefix("src_")
            or source_row["source_kind"] != source_identity.source_kind
            or source_row["commit_sha"] != source_identity.commit_sha
            or source_row["tree_sha"] != source_identity.tree_sha
            or source_row["source_fingerprint"] != source_identity.source_fingerprint
        ):
            raise CatalogConflictError(
                "repository or source revision identity conflicts"
            )

    def _validate_view_generation_input(self, row: sqlite3.Row) -> None:
        """Validate the immutable profile, object, and generation identities."""
        status = row["status"]
        ready_at = row["ready_at"]
        if status == "staged":
            if ready_at is not None:
                raise CatalogConflictError(
                    "staged view generation has invalid readiness metadata"
                )
        elif status == "ready":
            _persisted_utc_timestamp(ready_at, "view generation ready_at")
        else:
            raise CatalogConflictError("view generation has invalid readiness state")
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
            expected_member_digests = list(
                view_generation_member_digests(row["object_digest"], metadata)
            )
        except (TypeError, json.JSONDecodeError, CatalogValidationError) as exc:
            raise CatalogConflictError("view generation identity conflicts") from exc
        member_objects = self._generation_member_objects(row["view_generation_id"])
        if [member.digest for member in member_objects] != expected_member_digests:
            raise CatalogConflictError(
                "view generation member object identity conflicts"
            )
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

    def _generation_member_objects(
        self, view_generation_id: str
    ) -> tuple[ObjectRecord, ...]:
        if self.schema_version < 4:
            return ()
        raw_rows = self._connection.execute(
            """
            SELECT object_digest FROM view_generation_objects
            WHERE view_generation_id = ? ORDER BY object_digest
            """,
            (view_generation_id,),
        ).fetchall()
        joined_rows = self._connection.execute(
            """
            SELECT o.digest, o.storage_key, o.byte_size, o.media_type
            FROM view_generation_objects AS member
            JOIN objects AS o ON o.digest = member.object_digest
            WHERE member.view_generation_id = ?
            ORDER BY member.object_digest
            """,
            (view_generation_id,),
        ).fetchall()
        if len(raw_rows) != len(joined_rows):
            raise CatalogConflictError(
                "view generation member object dependencies are missing"
            )
        records: list[ObjectRecord] = []
        try:
            for raw, joined in zip(raw_rows, joined_rows, strict=True):
                if raw["object_digest"] != joined["digest"]:
                    raise CatalogValidationError(
                        "view generation member object join is inconsistent"
                    )
                record = ObjectRecord(
                    digest=joined["digest"],
                    storage_key=joined["storage_key"],
                    byte_size=joined["byte_size"],
                    media_type=joined["media_type"],
                )
                if (
                    record.digest,
                    record.storage_key,
                    record.byte_size,
                    record.media_type,
                ) != (
                    joined["digest"],
                    joined["storage_key"],
                    joined["byte_size"],
                    joined["media_type"],
                ):
                    raise CatalogValidationError(
                        "view generation member object metadata is not canonical"
                    )
                records.append(record)
        except CatalogValidationError as exc:
            raise CatalogConflictError(
                "view generation member object metadata conflicts"
            ) from exc
        return tuple(records)

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
        if actual_snapshot != expected_snapshot or snapshot is None:
            raise CatalogConflictError(
                "existing snapshot identity or seal state conflicts"
            )
        try:
            _persisted_utc_timestamp(snapshot["published_at"], "snapshot published_at")
        except CatalogConflictError as exc:
            raise CatalogConflictError(
                "existing snapshot identity or seal state conflicts"
            ) from exc

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
        if actual_members != list(members):
            raise CatalogConflictError("existing ready snapshot membership conflicts")
        for row in view_rows:
            if row["status"] != "ready":
                raise CatalogConflictError(
                    "existing ready snapshot membership conflicts"
                )
            try:
                _persisted_utc_timestamp(row["ready_at"], "view generation ready_at")
            except CatalogConflictError as exc:
                raise CatalogConflictError(
                    "existing ready snapshot membership conflicts"
                ) from exc

    @_coordinated_catalog_method
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
            try:
                generation = _persisted_positive_int64(
                    row["generation"], "ref generation"
                )
                updated_at = _persisted_utc_timestamp(
                    row["updated_at"], "ref updated_at"
                )
            except CatalogConflictError as exc:
                raise CatalogConflictError(
                    f"ref {normalized_ref!r} has invalid publication metadata"
                ) from exc
            manifest = self._manifest_summary(row["snapshot_id"])
            if manifest["repository_id"] != repository:
                raise CatalogConflictError(
                    f"ref {normalized_ref!r} targets another repository"
                )
            response = {
                "repository_id": repository,
                "ref_name": normalized_ref,
                "snapshot_id": row["snapshot_id"],
                "generation": generation,
                "updated_at": updated_at,
                "manifest": manifest,
            }
            try:
                return snapshot_retained_import_response(
                    response,
                    label="retained ref resolution",
                )
            except StorageIntegrityError as exc:
                raise CatalogConflictError(
                    "ref response exceeds retained-import bounds"
                ) from exc

    @_coordinated_catalog_method
    def get_manifest_summary(self, snapshot_id: str) -> dict[str, Any]:
        """Return the identity-closed summary for a published, ready snapshot."""
        normalized_snapshot = _required_text(snapshot_id, "snapshot ID")
        with self._transaction(immediate=False):
            summary = self._manifest_summary(normalized_snapshot)
            try:
                return snapshot_retained_import_response(
                    summary,
                    label="retained snapshot summary",
                )
            except StorageIntegrityError as exc:
                raise CatalogConflictError(
                    "snapshot response exceeds retained-import bounds"
                ) from exc

    def _manifest_summary(self, snapshot_id: str) -> dict[str, Any]:
        snapshot = self._require_record("snapshots", "snapshot_id", snapshot_id)
        if snapshot["status"] != "ready":
            raise CatalogValidationError(
                f"snapshot is not ready for publication: {snapshot_id}"
            )
        _persisted_utc_timestamp(snapshot["published_at"], "snapshot published_at")
        repository = self._require_record(
            "repositories", "repository_id", snapshot["repository_id"]
        )
        namespace = self._require_record(
            "namespaces", "namespace_id", repository["namespace_id"]
        )
        source = self._require_record(
            "source_revisions", "source_revision_id", snapshot["source_revision_id"]
        )
        self._validate_repository_source_identity(repository, source)
        raw_member_rows = self._connection.execute(
            """
            SELECT view_type, view_generation_id
            FROM snapshot_views
            WHERE snapshot_id = ?
            ORDER BY view_type
            """,
            (snapshot_id,),
        ).fetchall()
        raw_members: list[tuple[str, str]] = []
        for member in raw_member_rows:
            try:
                view_type = _required_text(member["view_type"], "snapshot view type")
                generation_id = _required_text(
                    member["view_generation_id"], "snapshot view generation ID"
                )
            except (TypeError, CatalogValidationError) as exc:
                raise CatalogConflictError(
                    "snapshot membership is not canonical"
                ) from exc
            if (
                member["view_type"] != view_type
                or member["view_generation_id"] != generation_id
            ):
                raise CatalogConflictError("snapshot membership is not canonical")
            raw_members.append((view_type, generation_id))
        if (
            not raw_members
            or len({view_type for view_type, _ in raw_members}) != len(raw_members)
            or len({generation_id for _, generation_id in raw_members})
            != len(raw_members)
        ):
            raise CatalogConflictError("snapshot membership is incomplete")

        expected_snapshot_id = content_id(
            "snapshot",
            {
                "repository_id": snapshot["repository_id"],
                "source_revision_id": snapshot["source_revision_id"],
                "views": raw_members,
            },
        )
        if snapshot["snapshot_id"] != expected_snapshot_id or snapshot[
            "content_digest"
        ] != expected_snapshot_id.removeprefix("snapshot_"):
            raise CatalogConflictError("snapshot content identity conflicts")

        view_rows = self._connection.execute(
            """
            SELECT
                sv.view_type AS snapshot_view_type,
                sv.view_generation_id AS selected_view_generation_id,
                vg.view_generation_id,
                vg.repository_id,
                vg.source_revision_id,
                vg.profile_id,
                vg.view_type,
                vg.schema_version,
                vg.metadata_json,
                vg.object_digest,
                vg.status,
                vg.ready_at,
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
        joined_members = [
            (row["snapshot_view_type"], row["selected_view_generation_id"])
            for row in view_rows
        ]
        if joined_members != raw_members:
            raise CatalogConflictError(
                "snapshot membership dependencies are missing or inconsistent"
            )
        for row in view_rows:
            if (
                row["selected_view_generation_id"] != row["view_generation_id"]
                or row["snapshot_view_type"] != row["view_type"]
                or row["repository_id"] != snapshot["repository_id"]
                or row["source_revision_id"] != snapshot["source_revision_id"]
            ):
                raise CatalogConflictError(
                    "snapshot membership dependencies are inconsistent"
                )
            self._validate_view_generation_input(row)
            if row["status"] != "ready":
                raise CatalogConflictError(
                    "ready snapshot contains a non-ready view generation"
                )
        views = {}
        for row in view_rows:
            member_objects = self._generation_member_objects(row["view_generation_id"])
            views[row["snapshot_view_type"]] = {
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
                "member_objects": [
                    {
                        "digest": member.digest,
                        "storage_key": member.storage_key,
                        "byte_size": member.byte_size,
                        "media_type": member.media_type,
                    }
                    for member in member_objects
                ],
            }
        return {
            "snapshot_id": snapshot["snapshot_id"],
            "repository_id": snapshot["repository_id"],
            "namespace": {
                "namespace_id": namespace["namespace_id"],
                "name": namespace["name"],
            },
            "repository": {
                "namespace_id": repository["namespace_id"],
                "repository_key": repository["repository_key"],
            },
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
