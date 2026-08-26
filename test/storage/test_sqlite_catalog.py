# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the SQLite immutable-generation catalog."""

from __future__ import annotations

import io
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

import codenib.storage.protocols as storage_protocols
import codenib.storage.sqlite_catalog as sqlite_catalog_module
from codenib.storage.cas import LocalCAS
from codenib.storage.models import (
    ObjectRecord,
    PublishedSnapshot,
    RepositoryIdentity,
    SnapshotView,
    SourceRevision,
    ViewGeneration,
    ViewProfile,
)
from codenib.storage.sqlite_catalog import (
    DEFAULT_NAMESPACE_ID,
    LATEST_SCHEMA_VERSION,
    CatalogConflictError,
    CatalogError,
    CatalogNotFoundError,
    CatalogValidationError,
    SQLiteCatalog,
)


def _source(catalog: SQLiteCatalog, repository_id: str, suffix: str = "a") -> str:
    return catalog.create_source_revision(
        repository_id,
        commit_sha=suffix * 40,
        tree_sha=suffix * 64,
    )


def _object(
    catalog: SQLiteCatalog,
    suffix: str,
    *,
    byte_size: int = 12,
) -> str:
    return catalog.register_object(
        suffix * 64,
        storage_key=f"sha256/{suffix * 2}/{suffix * 62}",
        byte_size=byte_size,
    )


def _stage(
    catalog: SQLiteCatalog,
    repository_id: str,
    source_revision_id: str,
    profile_id: str,
    object_digest: str,
    *,
    view_type: str = "bm25",
) -> str:
    return catalog.stage_view_generation(
        repository_id,
        source_revision_id,
        profile_id,
        view_type,
        object_digest,
        schema_version="1",
        metadata={"document_count": 4},
    )


def _setup_staged_view(catalog: SQLiteCatalog, suffix: str = "a"):
    repository_id = catalog.create_repository("owner/repo")
    source_revision_id = _source(catalog, repository_id)
    profile_id = catalog.create_view_profile("bm25", {})
    object_digest = _object(catalog, suffix)
    view_id = _stage(
        catalog,
        repository_id,
        source_revision_id,
        profile_id,
        object_digest,
    )
    return repository_id, source_revision_id, profile_id, object_digest, view_id


def _catalog_file_identity(path: Path) -> tuple[int, int, int]:
    metadata = path.lstat()
    return int(metadata.st_dev), int(metadata.st_ino), int(metadata.st_nlink)


def _prepare_delete_journal_catalog(path: Path) -> bytes:
    with SQLiteCatalog(path):
        pass
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == (
            "delete"
        )
    finally:
        connection.close()
    return path.read_bytes()


def _prepare_supported_schema_version(path: Path, version: int) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(sqlite_catalog_module._SCHEMA_MIGRATIONS_SQL)
        for migration_version in range(1, version + 1):
            for statement in sqlite_catalog_module._MIGRATIONS[migration_version]:
                connection.execute(statement)
            if migration_version == 1:
                connection.execute(
                    """
                    INSERT INTO namespaces(namespace_id, name, created_at)
                    VALUES (?, ?, '2026-08-20T00:00:00+00:00')
                    """,
                    (
                        sqlite_catalog_module.DEFAULT_NAMESPACE_ID,
                        sqlite_catalog_module.DEFAULT_NAMESPACE_NAME,
                    ),
                )
            connection.execute(
                """
                INSERT INTO schema_migrations(version, applied_at)
                VALUES (?, '2026-08-20T00:00:00+00:00')
                """,
                (migration_version,),
            )
            connection.execute(f"PRAGMA user_version = {migration_version}")
        connection.commit()
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        connection.close()


def _catalog_namespace_bytes(path: Path) -> dict[str, bytes | None]:
    return {
        suffix: (
            sidecar.read_bytes()
            if (sidecar := Path(f"{path}{suffix}")).exists()
            else None
        )
        for suffix in ("", "-wal", "-shm", "-journal")
    }


def _rewrite_schema_sql(
    connection: sqlite3.Connection,
    *,
    object_type: str,
    name: str,
    old: str,
    new: str,
) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type = ? AND name = ?",
        (object_type, name),
    ).fetchone()
    assert row is not None
    sql = row[0]
    assert old in sql
    schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
    connection.execute("PRAGMA writable_schema = ON")
    try:
        connection.execute(
            "UPDATE sqlite_schema SET sql = ? WHERE type = ? AND name = ?",
            (sql.replace(old, new, 1), object_type, name),
        )
        connection.execute(f"PRAGMA schema_version = {schema_version + 1}")
    finally:
        connection.execute("PRAGMA writable_schema = OFF")


def _corrupt_catalog_schema(path: Path, corruption: str) -> None:
    connection = sqlite3.connect(path)
    try:
        if corruption == "table":
            connection.execute("DROP TABLE namespaces")
        elif corruption == "column":
            _rewrite_schema_sql(
                connection,
                object_type="table",
                name="objects",
                old="created_at TEXT NOT NULL",
                new="created_at BLOB NOT NULL",
            )
        elif corruption == "constraint":
            _rewrite_schema_sql(
                connection,
                object_type="table",
                name="objects",
                old="byte_size INTEGER NOT NULL CHECK (byte_size >= 0)",
                new="byte_size INTEGER NOT NULL",
            )
        elif corruption == "index":
            connection.execute("DROP INDEX source_revisions_repository_idx")
            connection.execute(
                """
                CREATE INDEX source_revisions_repository_idx
                    ON source_revisions(source_revision_id)
                """
            )
        elif corruption == "foreign-key":
            _rewrite_schema_sql(
                connection,
                object_type="table",
                name="repositories",
                old=(
                    "FOREIGN KEY (namespace_id) "
                    "REFERENCES namespaces(namespace_id)\n"
                    "            ON DELETE RESTRICT"
                ),
                new="CHECK (namespace_id IS NOT NULL)",
            )
        elif corruption == "trigger":
            connection.execute("DROP TRIGGER objects_are_immutable")
            connection.execute(
                """
                CREATE TRIGGER objects_are_immutable
                BEFORE UPDATE ON objects
                BEGIN
                    SELECT 1;
                END
                """
            )
        elif corruption == "extra-object":
            connection.execute("CREATE TABLE counterfeit(value TEXT)")
        else:  # pragma: no cover - the parametrization is closed
            raise AssertionError(f"unexpected corruption: {corruption}")
        connection.commit()
    finally:
        connection.close()


def test_initializes_pragmas_default_namespace_and_idempotent_reopen(tmp_path):
    path = tmp_path / "catalog.sqlite3"

    with SQLiteCatalog(path, busy_timeout_ms=1_234) as catalog:
        assert catalog.schema_version == LATEST_SCHEMA_VERSION
        assert (
            catalog._connection.execute("PRAGMA user_version").fetchone()[0]
            == LATEST_SCHEMA_VERSION
        )
        assert catalog._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert (
            catalog._connection.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
        )
        assert catalog._connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert catalog._connection.execute("PRAGMA busy_timeout").fetchone()[0] == 1_234
        assert catalog._connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        repository_id = catalog.create_repository("sysevol-ai/CodeNib")

    with SQLiteCatalog(path) as reopened:
        assert reopened.schema_version == LATEST_SCHEMA_VERSION
        assert reopened.create_namespace("default") == DEFAULT_NAMESPACE_ID
        assert reopened.create_repository("sysevol-ai/CodeNib") == repository_id
        migration_count = reopened._connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]
        assert migration_count == LATEST_SCHEMA_VERSION


def test_existing_only_mode_never_creates_a_missing_catalog(tmp_path):
    path = tmp_path / "catalog.sqlite3"

    with pytest.raises(CatalogError, match="existing SQLite catalog"):
        SQLiteCatalog(path, create=False)

    assert not path.exists()

    nested = tmp_path / "missing" / "catalog.sqlite3"
    with pytest.raises(CatalogError, match="existing SQLite catalog"):
        SQLiteCatalog(nested, create=False)
    assert not nested.parent.exists()

    with SQLiteCatalog(path) as created:
        assert created.schema_version == LATEST_SCHEMA_VERSION
    with SQLiteCatalog(path, create=False) as reopened:
        assert reopened.schema_version == LATEST_SCHEMA_VERSION

    escaped = tmp_path / "catalog #%25.sqlite3"
    with SQLiteCatalog(escaped):
        pass
    with SQLiteCatalog(escaped, create=False) as reopened:
        assert reopened.schema_version == LATEST_SCHEMA_VERSION


@pytest.mark.parametrize("version", range(1, LATEST_SCHEMA_VERSION + 1))
def test_existing_only_accepts_every_canonical_supported_schema(tmp_path, version):
    path = tmp_path / f"catalog-v{version}.sqlite3"
    _prepare_supported_schema_version(path, version)

    with SQLiteCatalog(path, create=False) as catalog:
        assert catalog.schema_version == LATEST_SCHEMA_VERSION
        assert (
            catalog._connection.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()[0]
            == LATEST_SCHEMA_VERSION
        )


def test_existing_only_accepts_sqlite_analyze_statistics(tmp_path):
    path = tmp_path / "analyzed-catalog.sqlite3"
    _prepare_supported_schema_version(path, LATEST_SCHEMA_VERSION)
    connection = sqlite3.connect(path)
    try:
        connection.execute("ANALYZE")
        connection.commit()
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name = 'sqlite_stat1'"
        ).fetchone() == (1,)
    finally:
        connection.close()

    with SQLiteCatalog(path, create=False) as catalog:
        assert catalog.schema_version == LATEST_SCHEMA_VERSION


def test_existing_only_rejects_malformed_sqlite_statistics_schema(tmp_path):
    path = tmp_path / "malformed-statistics.sqlite3"
    _prepare_supported_schema_version(path, LATEST_SCHEMA_VERSION)
    connection = sqlite3.connect(path)
    try:
        connection.execute("ANALYZE")
        _rewrite_schema_sql(
            connection,
            object_type="table",
            name="sqlite_stat1",
            old="CREATE TABLE sqlite_stat1(tbl,idx,stat)",
            new="CREATE TABLE sqlite_stat1(tbl,idx,stat,extra)",
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(CatalogError, match="schema is not canonical"):
        SQLiteCatalog(path, create=False)


def test_existing_only_accepts_schema_and_data_from_uncheckpointed_wal(tmp_path):
    path = tmp_path / "live-wal.sqlite3"

    with SQLiteCatalog(path) as writer:
        writer._connection.execute("PRAGMA wal_autocheckpoint = 0")
        repository_id = writer.create_repository("owner/live-wal")
        assert Path(f"{path}-wal").stat().st_size > 0
        assert Path(f"{path}-shm").exists()

        main_only = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro&immutable=1",
            uri=True,
        )
        try:
            assert main_only.execute("PRAGMA user_version").fetchone()[0] == 0
            assert (
                main_only.execute(
                    "SELECT name FROM sqlite_schema WHERE name = 'schema_migrations'"
                ).fetchone()
                is None
            )
        finally:
            main_only.close()

        with SQLiteCatalog(
            path,
            create=False,
            expected_file_identity=_catalog_file_identity(path),
        ) as reader:
            assert reader.schema_version == LATEST_SCHEMA_VERSION
            assert (
                reader._connection.execute(
                    "SELECT repository_id FROM repositories WHERE repository_key = ?",
                    ("owner/live-wal",),
                ).fetchone()[0]
                == repository_id
            )


def test_existing_only_retries_changed_wal_with_a_fresh_snapshot(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "retry-live-wal.sqlite3"

    with SQLiteCatalog(path) as writer:
        writer._connection.execute("PRAGMA wal_autocheckpoint = 0")
        repository_id = writer.create_repository("owner/retry-live-wal")
        assert Path(f"{path}-wal").stat().st_size > 0
        original_copy = sqlite_catalog_module._copy_validation_source
        wal_destinations: list[Path] = []

        def change_first_wal_copy(
            descriptor,
            expected,
            destination,
            *,
            label,
        ):
            if label == "WAL sidecar":
                wal_destinations.append(destination)
                original_copy(
                    descriptor,
                    expected,
                    destination,
                    label=label,
                )
                if len(wal_destinations) == 1:
                    raise sqlite_catalog_module._CatalogValidationNamespaceChanged(
                        "injected one-shot WAL change"
                    )
                return None
            return original_copy(
                descriptor,
                expected,
                destination,
                label=label,
            )

        monkeypatch.setattr(
            sqlite_catalog_module,
            "_copy_validation_source",
            change_first_wal_copy,
        )

        with SQLiteCatalog(path, create=False) as reader:
            observed = reader._connection.execute(
                "SELECT repository_id FROM repositories WHERE repository_key = ?",
                ("owner/retry-live-wal",),
            ).fetchone()

        assert observed[0] == repository_id
        assert len(wal_destinations) == 2
        assert wal_destinations[0].parent != wal_destinations[1].parent


def test_existing_only_does_not_retry_main_inode_replacement(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "identity-bound.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    _prepare_supported_schema_version(path, LATEST_SCHEMA_VERSION)
    _prepare_supported_schema_version(replacement, LATEST_SCHEMA_VERSION)
    original_open = os.open
    main_opens = 0
    swapped = False

    def replace_before_open(target, flags, mode=0o777, *, dir_fd=None):
        nonlocal main_opens, swapped
        if dir_fd is None and Path(target) == path:
            main_opens += 1
            if not swapped:
                swapped = True
                os.replace(replacement, path)
        if dir_fd is None:
            return original_open(target, flags, mode)
        return original_open(target, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", replace_before_open)

    with pytest.raises(
        CatalogError,
        match="main file identity changed before copy",
    ) as caught:
        SQLiteCatalog(path, create=False)

    assert type(caught.value) is CatalogError
    assert main_opens == 1
    assert swapped


def test_existing_only_bounds_repeated_validation_namespace_changes(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "repeated-live-wal.sqlite3"

    with SQLiteCatalog(path) as writer:
        writer._connection.execute("PRAGMA wal_autocheckpoint = 0")
        writer.create_repository("owner/repeated-live-wal")
        assert Path(f"{path}-wal").stat().st_size > 0
        attempts = 0
        clock = [10.0]
        retry_delays: list[float] = []

        def monotonic() -> float:
            return clock[0]

        def sleep(delay: float) -> None:
            retry_delays.append(delay)
            clock[0] += delay

        def change_every_wal_copy(
            _descriptor,
            _expected,
            _destination,
            *,
            label,
        ):
            nonlocal attempts
            assert label in {"main file", "WAL sidecar"}
            if label == "WAL sidecar":
                attempts += 1
                raise sqlite_catalog_module._CatalogValidationNamespaceChanged(
                    "injected sustained WAL change"
                )

        monkeypatch.setattr(
            sqlite_catalog_module,
            "_VALIDATION_NAMESPACE_MAX_ATTEMPTS",
            3,
        )
        monkeypatch.setattr(
            sqlite_catalog_module,
            "_VALIDATION_RETRY_INITIAL_SECONDS",
            0.000_001,
        )
        monkeypatch.setattr(
            sqlite_catalog_module,
            "_VALIDATION_RETRY_MAX_SECONDS",
            0.000_001,
        )
        monkeypatch.setattr(
            sqlite_catalog_module,
            "_copy_validation_source",
            change_every_wal_copy,
        )
        monkeypatch.setattr(sqlite_catalog_module.time, "monotonic", monotonic)
        monkeypatch.setattr(sqlite_catalog_module.time, "sleep", sleep)

        with pytest.raises(
            sqlite_catalog_module._CatalogValidationNamespaceChanged,
            match="injected sustained WAL change",
        ):
            SQLiteCatalog(path, create=False, busy_timeout_ms=1_000)

        assert attempts == 3
        assert retry_delays == [0.000_001, 0.000_001]


def test_existing_only_does_not_retry_after_backoff_crosses_deadline(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "validation-retry-deadline.sqlite3"

    with SQLiteCatalog(path) as writer:
        writer._connection.execute("PRAGMA wal_autocheckpoint = 0")
        writer.create_repository("owner/validation-retry-deadline")
        clock = [10.0]
        attempts = 0
        retry_delays: list[float] = []

        def monotonic() -> float:
            return clock[0]

        def oversleep(delay: float) -> None:
            retry_delays.append(delay)
            clock[0] += 0.020

        def change_every_copy(
            _descriptor,
            _expected,
            _destination,
            *,
            label,
        ):
            nonlocal attempts
            assert label in {"main file", "WAL sidecar"}
            attempts += 1
            raise sqlite_catalog_module._CatalogValidationNamespaceChanged(
                "injected validation drift"
            )

        monkeypatch.setattr(
            sqlite_catalog_module,
            "_copy_validation_source",
            change_every_copy,
        )
        monkeypatch.setattr(sqlite_catalog_module.time, "monotonic", monotonic)
        monkeypatch.setattr(sqlite_catalog_module.time, "sleep", oversleep)

        with pytest.raises(
            sqlite_catalog_module._CatalogValidationNamespaceChanged,
            match="injected validation drift",
        ):
            SQLiteCatalog(path, create=False, busy_timeout_ms=10)

        assert attempts == 1
        assert retry_delays == [0.001]


@pytest.mark.parametrize("retains_cleanup_owner", (False, True))
def test_existing_only_does_not_retry_hard_or_retained_cleanup_failures(
    tmp_path,
    monkeypatch,
    retains_cleanup_owner,
):
    path = tmp_path / f"hard-live-wal-{retains_cleanup_owner}.sqlite3"

    class CleanupOwner:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    with SQLiteCatalog(path) as writer:
        writer._connection.execute("PRAGMA wal_autocheckpoint = 0")
        writer.create_repository("owner/hard-live-wal")
        assert Path(f"{path}-wal").stat().st_size > 0
        attempts = 0
        cleanup_owner = CleanupOwner()
        if retains_cleanup_owner:
            failure = sqlite_catalog_module._CatalogValidationNamespaceChanged(
                "injected retained cleanup failure"
            )
            failure.publication_cleanup_owners = (cleanup_owner,)
        else:
            failure = CatalogError("injected hard validation failure")

        def fail_wal_copy(
            _descriptor,
            _expected,
            _destination,
            *,
            label,
        ):
            nonlocal attempts
            if label == "WAL sidecar":
                attempts += 1
                raise failure

        monkeypatch.setattr(
            sqlite_catalog_module,
            "_copy_validation_source",
            fail_wal_copy,
        )

        with pytest.raises(CatalogError) as caught:
            SQLiteCatalog(path, create=False)

        assert caught.value is failure
        assert attempts == 1
        assert not cleanup_owner.closed
        cleanup_owner.close()
        assert cleanup_owner.closed


def test_existing_only_serializes_slow_wal_copy_with_public_writer(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "coordinated-live-wal.sqlite3"
    with SQLiteCatalog(path):
        pass

    writer_ready = threading.Event()
    begin_writes = threading.Event()
    writer_lock_attempted = threading.Event()
    writes_completed = threading.Event()
    slow_copy_started = threading.Event()
    wal_destinations: list[Path] = []
    preexisting_repository: list[str] = []
    writer_coordinations: list[sqlite_catalog_module._CatalogPathCoordination] = []
    opener_thread = threading.get_ident()
    original_copy = sqlite_catalog_module._copy_validation_source
    original_lock_acquire = sqlite_catalog_module._CancellationSafeRLock._acquire

    def slow_wal_copy(
        descriptor,
        expected,
        destination,
        *,
        label,
    ):
        if (
            label == "WAL sidecar"
            and threading.get_ident() == opener_thread
            and not slow_copy_started.is_set()
        ):
            wal_destinations.append(destination)
            slow_copy_started.set()
            begin_writes.set()
            assert writer_lock_attempted.wait(timeout=5)
            assert not writes_completed.is_set()
        return original_copy(
            descriptor,
            expected,
            destination,
            label=label,
        )

    monkeypatch.setattr(
        sqlite_catalog_module,
        "_copy_validation_source",
        slow_wal_copy,
    )

    def write_repositories() -> tuple[str, ...]:
        with SQLiteCatalog(path, create=False) as writer:
            writer._connection.execute("PRAGMA wal_autocheckpoint = 0")
            before = writer.create_repository("owner/coordinated-before")
            preexisting_repository.append(before)
            writer_coordinations.append(writer._path_coordination)
            writer_ready.set()
            assert begin_writes.wait(timeout=5)
            during = tuple(
                writer.create_repository(f"owner/coordinated-during-{index}")
                for index in range(3)
            )
            writes_completed.set()
            return (before, *during)

    with ThreadPoolExecutor(max_workers=1) as pool:
        writer_future = pool.submit(write_repositories)
        assert writer_ready.wait(timeout=5)
        assert len(writer_coordinations) == 1
        coordination = writer_coordinations[0]

        def observe_writer_lock(lock) -> None:
            if lock is coordination.lock and threading.get_ident() != opener_thread:
                writer_lock_attempted.set()
            original_lock_acquire(lock)

        monkeypatch.setattr(
            sqlite_catalog_module._CancellationSafeRLock,
            "_acquire",
            observe_writer_lock,
        )
        assert Path(f"{path}-wal").stat().st_size > 0
        try:
            with SQLiteCatalog(path, create=False) as reader:
                assert reader.schema_version == LATEST_SCHEMA_VERSION
                observed = reader._connection.execute(
                    """
                    SELECT repository_id FROM repositories
                    WHERE repository_key = 'owner/coordinated-before'
                    """
                ).fetchone()
                assert observed[0] == preexisting_repository[0]
        finally:
            begin_writes.set()
        repository_ids = writer_future.result(timeout=5)

    assert slow_copy_started.is_set()
    assert writer_lock_attempted.is_set()
    assert writes_completed.is_set()
    assert len(wal_destinations) == 1
    assert len(repository_ids) == 4
    assert len(set(repository_ids)) == 4


def test_path_coordination_guard_recovers_interrupted_acquire(
    tmp_path,
    monkeypatch,
):
    guard = sqlite_catalog_module._CATALOG_PATH_COORDINATION_STATES[os.getpid()][0]
    original_acquire = sqlite_catalog_module._CancellationSafeRLock._acquire
    interruption = KeyboardInterrupt("after coordination guard acquire")
    injected = False

    def acquire_then_interrupt(lock) -> None:
        nonlocal injected
        original_acquire(lock)
        if lock is guard and not injected:
            injected = True
            raise interruption

    monkeypatch.setattr(
        sqlite_catalog_module._CancellationSafeRLock,
        "_acquire",
        acquire_then_interrupt,
    )
    with pytest.raises(KeyboardInterrupt) as caught:
        sqlite_catalog_module._catalog_path_coordination(
            tmp_path / "guard-interruption.sqlite3"
        )
    assert caught.value is interruption
    assert injected

    monkeypatch.setattr(
        sqlite_catalog_module._CancellationSafeRLock,
        "_acquire",
        original_acquire,
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        observed = pool.submit(
            sqlite_catalog_module._catalog_path_coordination,
            tmp_path / "guard-interruption.sqlite3",
        ).result(timeout=5)
    assert type(observed) is sqlite_catalog_module._CatalogPathCoordination


def test_concurrent_child_first_lookup_uses_one_process_coordination(
    tmp_path,
    monkeypatch,
):
    actual_pid = os.getpid()
    child_pid = actual_pid + 1_000_000
    states = sqlite_catalog_module._CATALOG_PATH_COORDINATION_STATES
    states.pop(child_pid, None)
    original_new_state = sqlite_catalog_module._new_catalog_path_coordination_state
    first_touch = threading.Barrier(2)
    created_states: list[sqlite_catalog_module._CatalogPathCoordinationState] = []

    def synchronized_new_state():
        state = original_new_state()
        created_states.append(state)
        first_touch.wait(timeout=5)
        return state

    monkeypatch.setattr(
        sqlite_catalog_module,
        "_new_catalog_path_coordination_state",
        synchronized_new_state,
    )
    monkeypatch.setattr(sqlite_catalog_module.os, "getpid", lambda: child_pid)
    path = tmp_path / "child-first-touch.sqlite3"
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = tuple(
                pool.submit(sqlite_catalog_module._catalog_path_coordination, path)
                for _index in range(2)
            )
            coordinations = tuple(future.result(timeout=5) for future in futures)

        assert len(created_states) == 2
        assert coordinations[0] is coordinations[1]
        assert states[child_pid] in created_states
    finally:
        states.pop(child_pid, None)


def test_fork_reset_discards_recycled_pid_coordination_state(
    tmp_path,
    monkeypatch,
):
    owner_pid = os.getpid()
    stale_state = sqlite_catalog_module._new_catalog_path_coordination_state()
    stale_guard = stale_state[0]
    inherited_state = sqlite_catalog_module._new_catalog_path_coordination_state()
    monkeypatch.setattr(
        sqlite_catalog_module,
        "_CATALOG_PATH_COORDINATION_STATES",
        {owner_pid - 1: inherited_state, owner_pid: stale_state},
    )
    guard_entered = threading.Event()
    release_guard = threading.Event()

    def hold_stale_guard() -> None:
        def wait_for_release() -> None:
            guard_entered.set()
            assert release_guard.wait(timeout=5)

        stale_guard.run(wait_for_release)

    holder = threading.Thread(target=hold_stale_guard)
    holder.start()
    assert guard_entered.wait(timeout=5)
    try:
        sqlite_catalog_module._reset_catalog_path_coordination_after_fork()
        states = sqlite_catalog_module._CATALOG_PATH_COORDINATION_STATES
        assert tuple(states) == (owner_pid,)
        assert states[owner_pid] is not stale_state

        with ThreadPoolExecutor(max_workers=1) as pool:
            observed = pool.submit(
                sqlite_catalog_module._catalog_path_coordination,
                tmp_path / "recycled-pid.sqlite3",
            ).result(timeout=5)
        assert type(observed) is sqlite_catalog_module._CatalogPathCoordination
    finally:
        release_guard.set()
        holder.join(timeout=5)
    assert not holder.is_alive()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_fork_child_discards_inherited_path_coordination_registry(tmp_path):
    path = tmp_path / "fork-reset-coordination.sqlite3"
    guard = sqlite_catalog_module._CATALOG_PATH_COORDINATION_STATES[os.getpid()][0]
    guard_entered = threading.Event()
    release_guard = threading.Event()

    def hold_registry_guard() -> None:
        def wait_for_release() -> None:
            guard_entered.set()
            assert release_guard.wait(timeout=5)

        guard.run(wait_for_release)

    holder = threading.Thread(target=hold_registry_guard)
    holder.start()
    assert guard_entered.wait(timeout=5)
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="This process .* is multi-threaded.*",
            category=DeprecationWarning,
        )
        child_pid = os.fork()
    if child_pid == 0:
        signal.alarm(2)
        try:
            coordination = sqlite_catalog_module._catalog_path_coordination(path)
        except BaseException:  # noqa: B036 - report any child failure to parent
            os._exit(2)
        os._exit(
            0
            if type(coordination) is sqlite_catalog_module._CatalogPathCoordination
            else 3
        )
    try:
        _waited_pid, status = os.waitpid(child_pid, 0)
        assert os.WIFEXITED(status)
        assert os.WEXITSTATUS(status) == 0
    finally:
        release_guard.set()
        holder.join(timeout=5)
    assert not holder.is_alive()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_inherited_catalog_rejects_before_acquiring_parent_path_lock(tmp_path):
    path = tmp_path / "inherited-catalog.sqlite3"
    catalog = SQLiteCatalog(path)
    lock_entered = threading.Event()
    release_lock = threading.Event()

    def hold_path_lock() -> None:
        def wait_for_release() -> None:
            lock_entered.set()
            assert release_lock.wait(timeout=5)

        catalog._path_coordination.lock.run(wait_for_release)

    holder = threading.Thread(target=hold_path_lock)
    holder.start()
    assert lock_entered.wait(timeout=5)
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="This process .* is multi-threaded.*",
            category=DeprecationWarning,
        )
        child_pid = os.fork()
    if child_pid == 0:
        signal.alarm(2)
        try:
            catalog.create_repository("owner/inherited-catalog")
        except CatalogError as error:
            os._exit(0 if "PID boundary" in str(error) else 2)
        except BaseException:  # noqa: B036 - report any child failure to parent
            os._exit(3)
        os._exit(4)
    try:
        _waited_pid, status = os.waitpid(child_pid, 0)
        assert os.WIFEXITED(status)
        assert os.WEXITSTATUS(status) == 0
    finally:
        release_lock.set()
        holder.join(timeout=5)
        catalog.close()
    assert not holder.is_alive()


def test_catalog_open_recovers_interrupted_path_lock_acquire(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "open-lock-interruption.sqlite3"
    with SQLiteCatalog(path) as keeper:
        coordination = keeper._path_coordination
        original_acquire = sqlite_catalog_module._CancellationSafeRLock._acquire
        interruption = KeyboardInterrupt("after catalog path lock acquire")
        injected = False

        def acquire_then_interrupt(lock) -> None:
            nonlocal injected
            original_acquire(lock)
            if lock is coordination.lock and not injected:
                injected = True
                raise interruption

        monkeypatch.setattr(
            sqlite_catalog_module._CancellationSafeRLock,
            "_acquire",
            acquire_then_interrupt,
        )
        with pytest.raises(KeyboardInterrupt) as caught:
            SQLiteCatalog(path, create=False)
        assert caught.value is interruption
        assert injected

        monkeypatch.setattr(
            sqlite_catalog_module._CancellationSafeRLock,
            "_acquire",
            original_acquire,
        )

        def reopen() -> int:
            with SQLiteCatalog(path, create=False) as catalog:
                return catalog.schema_version

        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(reopen).result(timeout=5) == LATEST_SCHEMA_VERSION


def test_catalog_transaction_recovers_interrupted_path_lock_acquire(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "transaction-lock-interruption.sqlite3"
    with SQLiteCatalog(path) as catalog:
        coordination = catalog._path_coordination
        original_acquire = sqlite_catalog_module._CancellationSafeRLock._acquire
        interruption = KeyboardInterrupt("after transaction path lock acquire")
        injected = False

        def acquire_then_interrupt(lock) -> None:
            nonlocal injected
            original_acquire(lock)
            if lock is coordination.lock and not injected:
                injected = True
                raise interruption

        monkeypatch.setattr(
            sqlite_catalog_module._CancellationSafeRLock,
            "_acquire",
            acquire_then_interrupt,
        )
        with pytest.raises(KeyboardInterrupt) as caught:
            catalog.create_repository("owner/interrupted-transaction-acquire")
        assert caught.value is interruption
        assert injected

        monkeypatch.setattr(
            sqlite_catalog_module._CancellationSafeRLock,
            "_acquire",
            original_acquire,
        )

        def create_after_interruption() -> str:
            with SQLiteCatalog(path, create=False) as observer:
                return observer.create_repository(
                    "owner/after-transaction-acquire-interruption"
                )

        with ThreadPoolExecutor(max_workers=1) as pool:
            repository_id = pool.submit(create_after_interruption).result(timeout=5)
        assert repository_id.startswith("repo_")


@pytest.mark.parametrize("phase", ("before", "after"))
def test_catalog_transaction_recovers_interrupted_path_lock_release(
    tmp_path,
    monkeypatch,
    phase,
):
    path = tmp_path / f"transaction-release-{phase}.sqlite3"
    with SQLiteCatalog(path) as catalog:
        coordination = catalog._path_coordination
        original_release = sqlite_catalog_module._CancellationSafeRLock._release
        interruption = SystemExit(f"{phase} transaction path lock release")
        injected = False

        def release_with_interruption(lock) -> None:
            nonlocal injected
            if lock is coordination.lock and not injected and phase == "before":
                injected = True
                raise interruption
            original_release(lock)
            if lock is coordination.lock and not injected and phase == "after":
                injected = True
                raise interruption

        monkeypatch.setattr(
            sqlite_catalog_module._CancellationSafeRLock,
            "_release",
            release_with_interruption,
        )
        with pytest.raises(SystemExit) as caught:
            catalog.create_repository(f"owner/interrupted-release-{phase}")
        assert caught.value is interruption
        assert injected

        monkeypatch.setattr(
            sqlite_catalog_module._CancellationSafeRLock,
            "_release",
            original_release,
        )

        def reopen() -> int:
            with SQLiteCatalog(path, create=False) as observer:
                return observer.schema_version

        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(reopen).result(timeout=5) == LATEST_SCHEMA_VERSION


def test_catalog_path_lock_release_preserves_transaction_body_primary(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "transaction-primary.sqlite3"
    with SQLiteCatalog(path) as catalog:
        coordination = catalog._path_coordination
        original_release = sqlite_catalog_module._CancellationSafeRLock._release
        primary = KeyboardInterrupt("transaction body primary")
        secondary = SystemExit("before transaction path lock release")
        injected = False

        def release_then_interrupt(lock) -> None:
            nonlocal injected
            if lock is coordination.lock and not injected:
                injected = True
                raise secondary
            original_release(lock)

        def fail_transaction() -> None:
            with catalog._transaction():
                raise primary

        monkeypatch.setattr(
            sqlite_catalog_module._CancellationSafeRLock,
            "_release",
            release_then_interrupt,
        )
        with pytest.raises(KeyboardInterrupt) as caught:
            coordination.lock.run(fail_transaction)
        assert caught.value is primary
        assert injected

        monkeypatch.setattr(
            sqlite_catalog_module._CancellationSafeRLock,
            "_release",
            original_release,
        )

        def reopen() -> int:
            with SQLiteCatalog(path, create=False) as observer:
                return observer.schema_version

        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(reopen).result(timeout=5) == LATEST_SCHEMA_VERSION


@pytest.mark.parametrize("phase", ("before", "after"))
def test_catalog_close_recovers_interrupted_path_lock_release(
    tmp_path,
    monkeypatch,
    phase,
):
    path = tmp_path / f"close-release-{phase}.sqlite3"
    catalog = SQLiteCatalog(path)
    coordination = catalog._path_coordination
    original_release = sqlite_catalog_module._CancellationSafeRLock._release
    interruption = SystemExit(f"{phase} close path lock release")
    injected = False

    def release_with_interruption(lock) -> None:
        nonlocal injected
        if lock is coordination.lock and not injected and phase == "before":
            injected = True
            raise interruption
        original_release(lock)
        if lock is coordination.lock and not injected and phase == "after":
            injected = True
            raise interruption

    monkeypatch.setattr(
        sqlite_catalog_module._CancellationSafeRLock,
        "_release",
        release_with_interruption,
    )
    with pytest.raises(SystemExit) as caught:
        catalog.close()
    assert caught.value is interruption
    assert injected

    monkeypatch.setattr(
        sqlite_catalog_module._CancellationSafeRLock,
        "_release",
        original_release,
    )

    def reopen() -> int:
        with SQLiteCatalog(path, create=False) as observer:
            return observer.schema_version

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(reopen).result(timeout=5) == LATEST_SCHEMA_VERSION


def test_existing_only_revalidates_original_before_wal_or_migration(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "catalog.sqlite3"
    _prepare_supported_schema_version(path, LATEST_SCHEMA_VERSION)
    real_require = SQLiteCatalog._require_existing_catalog_identity
    schema_checks = 0
    migration_calls = 0

    def corrupt_after_snapshot_check(catalog):
        nonlocal schema_checks
        schema_checks += 1
        real_require(catalog)
        if schema_checks == 1:
            _corrupt_catalog_schema(path, "index")

    def forbidden_migration(_catalog):
        nonlocal migration_calls
        migration_calls += 1
        pytest.fail("noncanonical original must be rejected before migration")

    monkeypatch.setattr(
        SQLiteCatalog,
        "_require_existing_catalog_identity",
        corrupt_after_snapshot_check,
    )
    monkeypatch.setattr(SQLiteCatalog, "_migrate", forbidden_migration)

    with pytest.raises(CatalogError, match="schema is not canonical"):
        SQLiteCatalog(path, create=False)

    assert schema_checks == 2
    assert migration_calls == 0
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        connection.close()


@pytest.mark.parametrize("version", range(1, LATEST_SCHEMA_VERSION + 1))
@pytest.mark.parametrize(
    "corruption",
    (
        "table",
        "column",
        "constraint",
        "index",
        "foreign-key",
        "trigger",
        "extra-object",
    ),
)
def test_existing_only_rejects_noncanonical_schema_without_mutating_namespace(
    tmp_path,
    version,
    corruption,
):
    path = tmp_path / f"catalog-v{version}-{corruption}.sqlite3"
    _prepare_supported_schema_version(path, version)
    _corrupt_catalog_schema(path, corruption)
    before = _catalog_namespace_bytes(path)
    assert before["-wal"] is None
    assert before["-shm"] is None

    with pytest.raises(CatalogError, match="schema is not canonical"):
        SQLiteCatalog(path, create=False)

    assert _catalog_namespace_bytes(path) == before


@pytest.mark.parametrize("version", range(1, LATEST_SCHEMA_VERSION + 1))
def test_existing_only_preserves_existing_sidecars_for_noncanonical_schema(
    tmp_path,
    version,
):
    path = tmp_path / f"catalog-v{version}.sqlite3"
    _prepare_supported_schema_version(path, version)
    _corrupt_catalog_schema(path, "index")
    Path(f"{path}-wal").write_bytes(b"untrusted WAL must remain byte-exact")
    Path(f"{path}-shm").write_bytes(b"untrusted SHM must remain byte-exact")
    before = _catalog_namespace_bytes(path)

    with pytest.raises(CatalogError, match="schema is not canonical"):
        SQLiteCatalog(path, create=False)

    assert _catalog_namespace_bytes(path) == before


def test_existing_only_rejects_symlink_wal_without_reading_its_target(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    _prepare_supported_schema_version(path, LATEST_SCHEMA_VERSION)
    protected = tmp_path / "protected"
    protected.write_bytes(b"must not be copied")
    wal = Path(f"{path}-wal")
    wal.symlink_to(protected)
    before_main = path.read_bytes()
    before_target = protected.read_bytes()

    with pytest.raises(CatalogError, match="single-linked regular file"):
        SQLiteCatalog(path, create=False)

    assert path.read_bytes() == before_main
    assert wal.is_symlink()
    assert wal.readlink() == protected
    assert protected.read_bytes() == before_target
    assert not Path(f"{path}-shm").exists()


def test_existing_only_rejects_special_wal_without_blocking_or_mutation(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    _prepare_supported_schema_version(path, LATEST_SCHEMA_VERSION)
    wal = Path(f"{path}-wal")
    os.mkfifo(wal, 0o600)
    before_main = path.read_bytes()

    with pytest.raises(CatalogError, match="single-linked regular file"):
        SQLiteCatalog(path, create=False)

    assert path.read_bytes() == before_main
    assert wal.is_fifo()
    assert not Path(f"{path}-shm").exists()


def test_existing_only_rejects_symlink_shm_without_writing_its_target(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    _prepare_supported_schema_version(path, LATEST_SCHEMA_VERSION)
    protected = tmp_path / "protected"
    protected.write_bytes(b"must not be mapped or changed")
    shm = Path(f"{path}-shm")
    shm.symlink_to(protected)
    before_main = path.read_bytes()
    before_target = protected.read_bytes()

    with pytest.raises(CatalogError, match="single-linked regular file"):
        SQLiteCatalog(path, create=False)

    assert path.read_bytes() == before_main
    assert shm.is_symlink()
    assert shm.readlink() == protected
    assert protected.read_bytes() == before_target
    assert not Path(f"{path}-wal").exists()


def test_existing_only_rejects_special_shm_without_blocking_or_mutation(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    _prepare_supported_schema_version(path, LATEST_SCHEMA_VERSION)
    shm = Path(f"{path}-shm")
    os.mkfifo(shm, 0o600)
    before_main = path.read_bytes()

    with pytest.raises(CatalogError, match="single-linked regular file"):
        SQLiteCatalog(path, create=False)

    assert path.read_bytes() == before_main
    assert shm.is_fifo()
    assert not Path(f"{path}-wal").exists()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="exact file-mount authentication uses Linux mountinfo",
)
@pytest.mark.parametrize(
    ("leaf", "label"),
    (
        ("main", "main file"),
        ("wal", "WAL sidecar"),
        ("shm", "SHM sidecar"),
    ),
)
def test_existing_only_rejects_mounted_namespace_leaf_before_opening_original(
    tmp_path,
    monkeypatch,
    leaf,
    label,
):
    path = tmp_path / "catalog.sqlite3"
    _prepare_supported_schema_version(path, LATEST_SCHEMA_VERSION)
    mounted = {
        "main": path,
        "wal": Path(f"{path}-wal"),
        "shm": Path(f"{path}-shm"),
    }[leaf]
    if mounted != path:
        mounted.write_bytes(f"protected {label}".encode())
    before = _catalog_namespace_bytes(path)
    monkeypatch.setattr(
        sqlite_catalog_module,
        "_validation_linux_mount_points",
        lambda: frozenset({os.path.normpath(os.path.realpath(mounted))}),
    )

    with pytest.raises(CatalogError, match=f"{label} must not be a file mount"):
        SQLiteCatalog(path, create=False)

    assert _catalog_namespace_bytes(path) == before


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="strict mount-table authentication is Linux-specific",
)
def test_existing_only_fails_closed_when_mount_table_cannot_be_read(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "catalog.sqlite3"
    _prepare_supported_schema_version(path, LATEST_SCHEMA_VERSION)
    before = _catalog_namespace_bytes(path)

    def unavailable():
        raise CatalogError("Linux mount table could not be inspected safely")

    monkeypatch.setattr(
        sqlite_catalog_module,
        "_validation_linux_mount_points",
        unavailable,
    )

    with pytest.raises(CatalogError, match="mount table could not be inspected"):
        SQLiteCatalog(path, create=False)

    assert _catalog_namespace_bytes(path) == before


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="strict mount-table authentication is Linux-specific",
)
@pytest.mark.parametrize(
    ("payload", "message"),
    (
        ("", "mount table is empty"),
        (
            "1 0 0:1 / / rw shared:1 ext4 /dev/root rw\n",
            "malformed entry",
        ),
    ),
)
def test_existing_only_fails_closed_on_untrusted_mount_table(
    tmp_path,
    monkeypatch,
    payload,
    message,
):
    path = tmp_path / "catalog.sqlite3"
    _prepare_supported_schema_version(path, LATEST_SCHEMA_VERSION)
    before = _catalog_namespace_bytes(path)
    monkeypatch.setattr(
        "builtins.open",
        lambda *_args, **_kwargs: io.BytesIO(payload.encode()),
    )

    with pytest.raises(CatalogError, match=message):
        SQLiteCatalog(path, create=False)

    assert _catalog_namespace_bytes(path) == before


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="strict mount-table authentication is Linux-specific",
)
def test_validation_mount_table_accepts_non_utf8_filesystem_paths(
    monkeypatch,
):
    mount_point = b"/mnt/non-utf8-\xff"
    payload = b"1 0 8:1 / " + mount_point + b" rw - ext4 /dev/root rw\n"
    monkeypatch.setattr(
        "builtins.open",
        lambda *_args, **_kwargs: io.BytesIO(payload),
    )

    points = sqlite_catalog_module._validation_linux_mount_points()

    assert {os.fsencode(point) for point in points} == {mount_point}


def test_validation_snapshot_closes_all_sources_after_first_close_failure(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "catalog.sqlite3"
    snapshot = tmp_path / "snapshot.sqlite3"
    path.touch()
    wal = tmp_path / "catalog.sqlite3-wal"
    wal.touch()
    descriptors = (os.open(path, os.O_RDONLY), os.open(wal, os.O_RDONLY))
    metadata = tuple(os.fstat(descriptor) for descriptor in descriptors)
    sources = iter(
        (
            (descriptors[0], metadata[0], snapshot, "main file"),
            (
                descriptors[1],
                metadata[1],
                Path(f"{snapshot}-wal"),
                "WAL sidecar",
            ),
        )
    )
    close_calls: list[int] = []
    original_close = os.close
    fail_close = True

    def open_source(*_args, **_kwargs):
        return next(sources)[:2]

    def close_source(descriptor):
        if descriptor not in descriptors:
            return original_close(descriptor)
        close_calls.append(descriptor)
        if descriptor == descriptors[1] and fail_close:
            raise OSError("first close failed")
        return original_close(descriptor)

    monkeypatch.setattr(sqlite_catalog_module, "_open_validation_source", open_source)
    monkeypatch.setattr(
        sqlite_catalog_module,
        "_copy_validation_source",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        sqlite_catalog_module,
        "_require_no_rollback_journal",
        lambda _path: None,
    )
    monkeypatch.setattr(os, "close", close_source)

    try:
        with pytest.raises(OSError, match="first close failed") as caught:
            with sqlite_catalog_module._catalog_validation_snapshot(path):
                pass

        assert close_calls[:2] == [descriptors[1], descriptors[0]]
        retained = caught.value.publication_cleanup_owners  # type: ignore[attr-defined]
        assert len(retained) == 1
        assert not retained[0].closed
        fail_close = False
        retained[0].close()
        assert retained[0].closed
    finally:
        for descriptor in descriptors:
            try:
                original_close(descriptor)
            except OSError:
                pass


def test_validation_source_retains_failed_post_open_cleanup(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "catalog.sqlite3"
    path.write_bytes(b"catalog")
    opened: list[int] = []
    mount_checks = 0
    fail_close = True
    original_open = os.open
    original_close = os.close

    def capture_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def reject_post_open(_path, *, label):
        nonlocal mount_checks
        mount_checks += 1
        if mount_checks == 2:
            raise CatalogError(f"{label} post-open validation failed")

    def interrupt_close(descriptor):
        if opened and descriptor == opened[0] and fail_close:
            raise OSError("injected descriptor close failure")
        return original_close(descriptor)

    monkeypatch.setattr(os, "open", capture_open)
    monkeypatch.setattr(os, "close", interrupt_close)
    monkeypatch.setattr(
        sqlite_catalog_module,
        "_require_validation_source_not_mount",
        reject_post_open,
    )

    with pytest.raises(CatalogError, match="post-open validation failed") as caught:
        sqlite_catalog_module._open_validation_source(
            path,
            label="main file",
            required=True,
        )

    retained = caught.value.publication_cleanup_owners  # type: ignore[attr-defined]
    assert len(retained) == 1
    assert not retained[0].closed
    fail_close = False
    retained[0].close()
    assert retained[0].closed


def test_shared_memory_retains_cleanup_after_validation_failure(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "catalog.sqlite3"
    shm = Path(f"{path}-shm")
    with shm.open("wb") as stream:
        stream.truncate(sqlite_catalog_module._MAX_VALIDATION_SHM_BYTES + 1)
    opened: list[int] = []
    fail_close = True
    original_open = os.open
    original_close = os.close

    def capture_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def interrupt_close(descriptor):
        if opened and descriptor == opened[0] and fail_close:
            raise OSError("injected SHM close failure")
        return original_close(descriptor)

    monkeypatch.setattr(os, "open", capture_open)
    monkeypatch.setattr(os, "close", interrupt_close)
    monkeypatch.setattr(
        sqlite_catalog_module,
        "_require_validation_source_not_mount",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(CatalogError, match="SHM sidecar exceeds") as caught:
        sqlite_catalog_module._require_safe_shared_memory(path)

    retained = caught.value.publication_cleanup_owners  # type: ignore[attr-defined]
    assert len(retained) == 1
    assert not retained[0].closed
    fail_close = False
    retained[0].close()
    assert retained[0].closed


def test_validation_copy_preserves_primary_and_retains_destination_cleanup(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.write_bytes(b"source")
    destination = tmp_path / "destination"
    source_descriptor = os.open(source, os.O_RDONLY)
    observed = os.fstat(source_descriptor)
    fields = list(observed)
    fields[6] = observed.st_size + 1
    expected = os.stat_result(fields)
    opened: list[int] = []
    fail_close = True
    original_open = os.open
    original_close = os.close

    def capture_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def interrupt_close(descriptor):
        if opened and descriptor == opened[0] and fail_close:
            raise OSError("injected destination close failure")
        return original_close(descriptor)

    monkeypatch.setattr(os, "open", capture_open)
    monkeypatch.setattr(os, "close", interrupt_close)
    try:
        with pytest.raises(CatalogError, match="changed during copy") as caught:
            sqlite_catalog_module._copy_validation_source(
                source_descriptor,
                expected,
                destination,
                label="main file",
            )

        assert type(caught.value) is (
            sqlite_catalog_module._CatalogValidationNamespaceChanged
        )
        retained = caught.value.publication_cleanup_owners  # type: ignore[attr-defined]
        assert len(retained) == 1
        assert not retained[0].closed
        fail_close = False
        retained[0].close()
        assert retained[0].closed
    finally:
        original_close(source_descriptor)


def test_validation_copy_prioritizes_structural_identity_change(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "structural-source"
    source.write_bytes(b"source")
    destination = tmp_path / "structural-destination"
    source_descriptor = os.open(source, os.O_RDONLY)
    expected_fields = list(os.fstat(source_descriptor))
    expected_fields[6] += 1
    expected = os.stat_result(expected_fields)
    original_fstat = os.fstat

    def changed_source_identity(descriptor):
        observed = original_fstat(descriptor)
        if descriptor != source_descriptor:
            return observed
        fields = list(observed)
        fields[1] += 1
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", changed_source_identity)
    try:
        with pytest.raises(
            CatalogError,
            match="main file identity changed during copy",
        ) as caught:
            sqlite_catalog_module._copy_validation_source(
                source_descriptor,
                expected,
                destination,
                label="main file",
            )

        assert type(caught.value) is CatalogError
    finally:
        os.close(source_descriptor)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="bind-mount regression requires Linux mount namespaces",
)
def test_existing_only_rejects_real_shm_file_bind_without_mutating_source(
    tmp_path,
):
    unshare = shutil.which("unshare")
    mount = shutil.which("mount")
    if unshare is None or mount is None:
        pytest.skip("Linux unshare/mount tools are unavailable")
    probe = subprocess.run(
        [
            unshare,
            "--user",
            "--map-root-user",
            "--mount",
            "--fork",
            "true",
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout).strip() or "permission denied"
        pytest.skip(f"Linux user/mount namespace is unavailable: {detail}")

    path = tmp_path / "catalog.sqlite3"
    _prepare_supported_schema_version(path, LATEST_SCHEMA_VERSION)
    shm = Path(f"{path}-shm")
    shm.write_bytes(b"destination".ljust(32_768, b"!"))
    protected = tmp_path / "protected-shm"
    protected.write_bytes(b"protected".ljust(32_768, b"?"))
    script = r"""
import os
import subprocess
import sys
from pathlib import Path

from codenib.storage.sqlite_catalog import CatalogError, SQLiteCatalog

mount, catalog, shm, protected = sys.argv[1:]
subprocess.run([mount, "--bind", protected, shm], check=True)
catalog_path = Path(catalog)
shm_path = Path(shm)
protected_path = Path(protected)
before = protected_path.read_bytes()
if (shm_path.stat().st_dev, shm_path.stat().st_ino) != (
    protected_path.stat().st_dev,
    protected_path.stat().st_ino,
):
    raise SystemExit("bind mount did not alias the protected file")
try:
    SQLiteCatalog(catalog_path, create=False)
except CatalogError as exc:
    if "SHM sidecar must not be a file mount point" not in str(exc):
        raise
else:
    raise SystemExit("mounted SHM sidecar was accepted")
if protected_path.read_bytes() != before:
    raise SystemExit("mounted SHM source bytes changed")
"""
    result = subprocess.run(
        [
            unshare,
            "--user",
            "--map-root-user",
            "--mount",
            "--fork",
            sys.executable,
            "-c",
            script,
            mount,
            os.fspath(path),
            os.fspath(shm),
            os.fspath(protected),
        ],
        capture_output=True,
        check=False,
        cwd=Path.cwd(),
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_existing_only_rejects_oversized_shm_without_mutation(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "catalog.sqlite3"
    _prepare_supported_schema_version(path, LATEST_SCHEMA_VERSION)
    Path(f"{path}-shm").write_bytes(b"oversized SHM")
    monkeypatch.setattr(sqlite_catalog_module, "_MAX_VALIDATION_SHM_BYTES", 3)
    before = _catalog_namespace_bytes(path)

    with pytest.raises(CatalogError, match="SHM sidecar exceeds"):
        SQLiteCatalog(path, create=False)

    assert _catalog_namespace_bytes(path) == before


@pytest.mark.parametrize("oversized", ("main", "wal"))
def test_existing_only_rejects_oversized_validation_namespace_without_mutation(
    tmp_path,
    monkeypatch,
    oversized,
):
    path = tmp_path / "catalog.sqlite3"
    _prepare_supported_schema_version(path, LATEST_SCHEMA_VERSION)
    if oversized == "wal":
        Path(f"{path}-wal").write_bytes(b"oversized WAL")
        limit = path.stat().st_size + len(b"oversized WAL") - 1
    else:
        limit = path.stat().st_size - 1
    monkeypatch.setattr(
        sqlite_catalog_module,
        "_MAX_VALIDATION_NAMESPACE_BYTES",
        limit,
    )
    before = _catalog_namespace_bytes(path)

    with pytest.raises(CatalogError, match="validation namespace exceeds"):
        SQLiteCatalog(path, create=False)

    assert _catalog_namespace_bytes(path) == before


def test_existing_only_rejects_rollback_journal_without_mutation(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    _prepare_supported_schema_version(path, LATEST_SCHEMA_VERSION)
    Path(f"{path}-journal").write_bytes(b"untrusted hot rollback journal")
    before = _catalog_namespace_bytes(path)

    with pytest.raises(CatalogError, match="rollback journal is not allowed"):
        SQLiteCatalog(path, create=False)

    assert _catalog_namespace_bytes(path) == before


@pytest.mark.parametrize(
    ("identity", "error", "message"),
    (
        ([1, 2, 1], TypeError, "exact 3-tuple"),
        ((1, 2), TypeError, "exact 3-tuple"),
        ((True, 2, 1), TypeError, "exact 3-tuple"),
        ((0, 2, 1), ValueError, "positive integers"),
        ((1, 0, 1), ValueError, "positive integers"),
        ((1, 2, 2), ValueError, "link count must be 1"),
    ),
)
def test_existing_only_expected_file_identity_is_an_exact_single_link_token(
    tmp_path,
    identity,
    error,
    message,
):
    path = tmp_path / "catalog.sqlite3"

    with pytest.raises(error, match=message):
        SQLiteCatalog(
            path,
            create=False,
            expected_file_identity=identity,
        )

    assert not path.exists()


def test_expected_file_identity_is_existing_only(tmp_path):
    path = tmp_path / "catalog.sqlite3"

    with pytest.raises(ValueError, match="requires create=False"):
        SQLiteCatalog(path, expected_file_identity=(1, 2, 1))

    assert not path.exists()


def test_existing_only_rejects_stable_replacement_before_connect(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "catalog.sqlite3"
    _prepare_delete_journal_catalog(path)
    expected_identity = _catalog_file_identity(path)
    replacement = tmp_path / "replacement.sqlite3"
    expected_bytes = _prepare_delete_journal_catalog(replacement)
    replacement.replace(path)
    real_connect = sqlite3.connect
    connect_calls = 0

    def forbidden_connect(*_args, **_kwargs):
        nonlocal connect_calls
        connect_calls += 1
        pytest.fail("a persistently replaced catalog must be rejected before connect")

    monkeypatch.setattr(sqlite_catalog_module.sqlite3, "connect", forbidden_connect)

    with pytest.raises(CatalogError, match="file identity changed"):
        SQLiteCatalog(
            path,
            create=False,
            expected_file_identity=expected_identity,
        )

    assert connect_calls == 0
    assert path.read_bytes() == expected_bytes
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()
    connection = real_connect(path)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == (
            LATEST_SCHEMA_VERSION
        )
    finally:
        connection.close()


def test_existing_only_rejects_persistent_replacement_during_connect(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "catalog.sqlite3"
    _prepare_delete_journal_catalog(path)
    expected_identity = _catalog_file_identity(path)
    replacement = tmp_path / "replacement.sqlite3"
    expected_bytes = _prepare_delete_journal_catalog(replacement)
    real_connect = sqlite3.connect
    opened_connections: list[sqlite3.Connection] = []
    schema_checks = 0
    migration_calls = 0

    def replacing_connect(database, *args, **kwargs):
        replacement.replace(path)
        connection = real_connect(database, *args, **kwargs)
        opened_connections.append(connection)
        return connection

    def forbidden_schema_check(_catalog):
        nonlocal schema_checks
        schema_checks += 1
        pytest.fail("replacement must be rejected before catalog schema inspection")

    def forbidden_migration(_catalog):
        nonlocal migration_calls
        migration_calls += 1
        pytest.fail("replacement must be rejected before migration")

    monkeypatch.setattr(sqlite_catalog_module.sqlite3, "connect", replacing_connect)
    monkeypatch.setattr(
        SQLiteCatalog,
        "_require_existing_catalog_identity",
        forbidden_schema_check,
    )
    monkeypatch.setattr(SQLiteCatalog, "_migrate", forbidden_migration)

    with pytest.raises(CatalogError, match="file identity changed"):
        SQLiteCatalog(
            path,
            create=False,
            expected_file_identity=expected_identity,
        )

    assert len(opened_connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened_connections[0].execute("SELECT 1")
    assert schema_checks == 0
    assert migration_calls == 0
    assert path.read_bytes() == expected_bytes
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()
    connection = real_connect(path)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == (
            LATEST_SCHEMA_VERSION
        )
    finally:
        connection.close()


def test_existing_only_mode_rejects_empty_or_foreign_databases(tmp_path):
    empty = tmp_path / "empty.sqlite3"
    empty.touch()

    with pytest.raises(CatalogError, match="initialized CodeNib catalog"):
        SQLiteCatalog(empty, create=False)

    assert empty.stat().st_size == 0
    assert not Path(f"{empty}-wal").exists()
    assert not Path(f"{empty}-shm").exists()

    foreign = tmp_path / "foreign.sqlite3"
    connection = sqlite3.connect(foreign)
    connection.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
    connection.execute("INSERT INTO sentinel(value) VALUES ('preserved')")
    connection.commit()
    connection.close()

    with pytest.raises(CatalogError, match="initialized CodeNib catalog"):
        SQLiteCatalog(foreign, create=False)

    connection = sqlite3.connect(foreign)
    try:
        tables = tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        )
        assert tables == ("sentinel",)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert connection.execute("SELECT value FROM sentinel").fetchone()[0] == (
            "preserved"
        )
    finally:
        connection.close()
    assert not Path(f"{foreign}-wal").exists()
    assert not Path(f"{foreign}-shm").exists()

    migration_db = tmp_path / "foreign-migrations.sqlite3"
    connection = sqlite3.connect(migration_db)
    connection.execute(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (1, 'foreign')"
    )
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()

    with pytest.raises(CatalogError, match="initialized CodeNib catalog"):
        SQLiteCatalog(migration_db, create=False)

    connection = sqlite3.connect(migration_db)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ) == ("schema_migrations",)
    finally:
        connection.close()
    assert not Path(f"{migration_db}-wal").exists()
    assert not Path(f"{migration_db}-shm").exists()


def test_existing_only_mode_maps_corrupt_database_errors(tmp_path):
    path = tmp_path / "corrupt.sqlite3"
    path.write_bytes(b"not a sqlite database")

    with pytest.raises(CatalogError, match="could not be initialized") as raised:
        SQLiteCatalog(path, create=False)

    assert isinstance(raised.value.__cause__, sqlite3.DatabaseError)
    assert path.read_bytes() == b"not a sqlite database"


def test_existing_only_mode_requires_an_exact_boolean(tmp_path):
    path = tmp_path / "catalog.sqlite3"

    with pytest.raises(TypeError, match="create must be a boolean"):
        SQLiteCatalog(path, create=1)  # type: ignore[arg-type]

    assert not path.exists()


def test_reopen_rejects_mismatched_schema_version_records(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path):
        pass
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 0")
    connection.commit()
    connection.close()

    with pytest.raises(CatalogError, match="does not match schema_migrations"):
        SQLiteCatalog(path)


def test_reopen_rejects_catalog_newer_than_implementation(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path):
        pass
    future_version = LATEST_SCHEMA_VERSION + 1
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 'future')",
        (future_version,),
    )
    connection.execute(f"PRAGMA user_version = {future_version}")
    connection.commit()
    connection.close()

    with pytest.raises(CatalogError, match="newer than this CodeNib version"):
        SQLiteCatalog(path)


def test_repository_requires_an_existing_non_null_namespace(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        namespace_id = catalog.create_namespace("team-one")
        repository_id = catalog.create_repository(
            "owner/repo", namespace_id=namespace_id
        )
        repository = catalog._connection.execute(
            "SELECT namespace_id FROM repositories WHERE repository_id = ?",
            (repository_id,),
        ).fetchone()

        assert repository["namespace_id"] == namespace_id
        with pytest.raises(CatalogNotFoundError, match="namespace"):
            catalog.create_repository("owner/missing", namespace_id="ns_missing")
        with pytest.raises(CatalogValidationError, match="namespace ID"):
            catalog.create_repository("owner/null", namespace_id="")


def test_namespace_and_repository_content_identities_are_immutable(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        namespace_id = catalog.create_namespace("team-one")
        repository_id = catalog.create_repository(
            "owner/repo", namespace_id=namespace_id
        )

        with pytest.raises(sqlite3.IntegrityError, match="namespace.*immutable"):
            catalog._connection.execute(
                "UPDATE namespaces SET name = 'renamed' WHERE namespace_id = ?",
                (namespace_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="repository.*immutable"):
            catalog._connection.execute(
                """
                UPDATE repositories SET repository_key = 'owner/renamed'
                WHERE repository_id = ?
                """,
                (repository_id,),
            )


def test_commit_failure_rolls_back_and_leaves_connection_reusable(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog._connection.execute("PRAGMA defer_foreign_keys = ON")

        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            with catalog._transaction():
                catalog._connection.execute(
                    """
                    INSERT INTO repositories(
                        repository_id, namespace_id, repository_key, created_at
                    ) VALUES ('invalid-repo', 'missing-namespace', 'invalid', 'now')
                    """
                )

        assert catalog._connection.in_transaction is False
        assert (
            catalog._connection.execute(
                "SELECT COUNT(*) FROM repositories WHERE repository_id = 'invalid-repo'"
            ).fetchone()[0]
            == 0
        )
        assert catalog.create_repository("owner/valid").startswith("repo_")


def test_clean_dirty_source_and_profile_identities_are_canonical(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id = catalog.create_repository("owner/repo")

        clean_one = catalog.create_source_revision(
            repository_id,
            commit_sha="A" * 40,
            tree_sha="B" * 64,
        )
        clean_two = catalog.create_source_revision(
            repository_id,
            commit_sha="a" * 40,
            tree_sha="b" * 64,
            source_fingerprint="ignored-for-clean-git-sources",
        )
        dirty_one = catalog.create_source_revision(
            repository_id,
            commit_sha="a" * 40,
            dirty=True,
            source_fingerprint="sha256:working-copy-one",
        )
        dirty_two = catalog.create_source_revision(
            repository_id,
            commit_sha="A" * 40,
            dirty=True,
            source_fingerprint="sha256:working-copy-one",
        )
        dirty_other = catalog.create_source_revision(
            repository_id,
            commit_sha="a" * 40,
            dirty=True,
            source_fingerprint="sha256:working-copy-two",
        )

        assert clean_one == clean_two
        assert dirty_one == dirty_two
        assert clean_one != dirty_one
        assert dirty_one != dirty_other
        with pytest.raises(CatalogValidationError, match="clean source tree"):
            catalog.create_source_revision(repository_id, commit_sha="a" * 40)
        with pytest.raises(CatalogValidationError, match="source fingerprint"):
            catalog.create_source_revision(repository_id, dirty=True)
        with pytest.raises(
            CatalogValidationError, match="must not include a base tree"
        ):
            catalog.create_source_revision(
                repository_id,
                commit_sha="a" * 40,
                tree_sha="b" * 64,
                dirty=True,
                source_fingerprint="sha256:working-copy-one",
            )

        profile_one = catalog.create_view_profile(
            "bm25",
            {"languages": ["python"], "options": {"b": 2, "a": 1}},
            name="default",
        )
        profile_two = catalog.create_view_profile(
            "bm25",
            {"options": {"a": 1, "b": 2}, "languages": ["python"]},
            name="default",
        )
        profile_other = catalog.create_view_profile(
            "vector",
            {"options": {"a": 1, "b": 2}, "languages": ["python"]},
            name="default",
        )

        assert profile_one == profile_two
        assert profile_one != profile_other
        with pytest.raises(CatalogValidationError, match="keys must be strings"):
            catalog.create_view_profile(
                "bm25",
                {"nested": [{1: "numeric key"}]},  # type: ignore[dict-item]
            )


def test_registered_objects_are_idempotent_but_immutable(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        digest = catalog.register_object(
            "A" * 64,
            storage_key="sha256/aa/object",
            byte_size=10,
            media_type="application/x-codenib-bm25",
        )

        assert digest == "a" * 64
        assert (
            catalog.register_object(
                f"sha256:{'a' * 64}",
                storage_key="sha256/aa/object",
                byte_size=10,
                media_type="application/x-codenib-bm25",
            )
            == digest
        )
        with pytest.raises(CatalogConflictError, match="immutable"):
            catalog.register_object(
                digest,
                storage_key="sha256/aa/replaced",
                byte_size=10,
                media_type="application/x-codenib-bm25",
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            catalog._connection.execute(
                "UPDATE objects SET storage_key = 'mutated' WHERE digest = ?",
                (digest,),
            )
        with pytest.raises(CatalogValidationError, match="relative and contained"):
            catalog.register_object(
                "b" * 64,
                storage_key="../../outside",
                byte_size=10,
            )
        with pytest.raises(CatalogValidationError, match="canonical"):
            catalog.register_object(
                "c" * 64,
                storage_key="sha256/./object",
                byte_size=10,
            )


def test_register_object_accepts_local_cas_blob_identity(tmp_path):
    blob = LocalCAS(tmp_path / "objects").put_bytes(b"compiled bm25 generation")

    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        digest = catalog.register_object(
            blob.digest,
            storage_key=blob.storage_key,
            byte_size=blob.byte_size,
        )
        stored = catalog._connection.execute(
            "SELECT digest, storage_key, byte_size FROM objects WHERE digest = ?",
            (digest,),
        ).fetchone()

        assert dict(stored) == {
            "digest": blob.digest,
            "storage_key": blob.storage_key,
            "byte_size": blob.byte_size,
        }


def test_stage_requires_registered_object_and_matching_repository(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_one = catalog.create_repository("owner/one")
        repository_two = catalog.create_repository("owner/two")
        source_one = _source(catalog, repository_one)
        profile_id = catalog.create_view_profile("bm25", {})

        with pytest.raises(CatalogNotFoundError, match="object"):
            _stage(
                catalog,
                repository_one,
                source_one,
                profile_id,
                "f" * 64,
            )

        digest = _object(catalog, "a")
        with pytest.raises(CatalogValidationError, match="another repository"):
            _stage(catalog, repository_two, source_one, profile_id, digest)
        with pytest.raises(CatalogValidationError, match="does not match"):
            _stage(
                catalog,
                repository_one,
                source_one,
                profile_id,
                digest,
                view_type="vector",
            )
        with pytest.raises(CatalogValidationError, match="keys must be strings"):
            catalog.stage_view_generation(
                repository_one,
                source_one,
                profile_id,
                "bm25",
                digest,
                schema_version="1",
                metadata={"nested": [{1: "numeric key"}]},  # type: ignore[dict-item]
            )


def test_compound_generation_members_are_identity_bound_and_immutable(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id = catalog.create_repository("owner/compound")
        source_revision_id = _source(catalog, repository_id)
        profile_id = catalog.create_view_profile("semantic_facts", {})
        primary = _object(catalog, "a")
        member_one = _object(catalog, "b")
        member_two = _object(catalog, "c")

        view_id = catalog.stage_view_generation(
            repository_id,
            source_revision_id,
            profile_id,
            "semantic_facts",
            primary,
            schema_version="1",
            metadata={"unit_count": 2},
            member_object_digests=(member_two, member_one),
        )
        source = SourceRevision.clean(
            repository_id,
            commit_sha="a" * 40,
            tree_sha="a" * 64,
        )
        profile = ViewProfile.create("semantic_facts", {})
        primary_record = ObjectRecord(
            digest=primary,
            byte_size=12,
            storage_key=f"sha256/{primary[:2]}/{primary[2:]}",
        )
        assert (
            ViewGeneration.create(
                source,
                profile,
                primary_record,
                schema_version="1",
                metadata={"unit_count": 2},
                member_object_digests=(member_two, member_one),
            ).view_generation_id
            == view_id
        )
        assert (
            catalog.stage_view_generation(
                repository_id,
                source_revision_id,
                profile_id,
                "semantic_facts",
                primary,
                schema_version="1",
                metadata={"unit_count": 2},
                member_object_digests=(member_one, member_two),
            )
            == view_id
        )
        with pytest.raises(CatalogValidationError, match="reserved"):
            catalog.stage_view_generation(
                repository_id,
                source_revision_id,
                profile_id,
                "semantic_facts",
                primary,
                schema_version="1",
                metadata={"_codenib_member_object_digests": []},
            )
        with pytest.raises(CatalogValidationError, match="primary object"):
            catalog.stage_view_generation(
                repository_id,
                source_revision_id,
                profile_id,
                "semantic_facts",
                primary,
                schema_version="1",
                member_object_digests=(primary,),
            )

        publication = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            (view_id,),
            expected_generation=0,
        )
        view = catalog.get_manifest_summary(publication["snapshot_id"])["views"][
            "semantic_facts"
        ]
        assert [member["digest"] for member in view["member_objects"]] == [
            member_one,
            member_two,
        ]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            catalog._connection.execute(
                """
                DELETE FROM view_generation_objects
                WHERE view_generation_id = ? AND object_digest = ?
                """,
                (view_id, member_one),
            )


def test_publish_rejects_aggregate_retained_response_over_budget_and_rolls_back(
    tmp_path,
    monkeypatch,
):
    def json_nodes(value):
        if type(value) is list:
            return 1 + sum(json_nodes(child) for child in value)
        if type(value) is dict:
            return 1 + sum(json_nodes(child) for child in value.values())
        return 1

    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id = catalog.create_repository("owner/aggregate-budget")
        source_revision_id = _source(catalog, repository_id)
        bm25_profile = catalog.create_view_profile("bm25", {})
        vector_profile = catalog.create_view_profile("vector", {})
        bm25 = _stage(
            catalog,
            repository_id,
            source_revision_id,
            bm25_profile,
            _object(catalog, "a"),
        )
        first = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            (bm25,),
            expected_generation=0,
        )
        first_summary = catalog.get_manifest_summary(first["snapshot_id"])
        vector = _stage(
            catalog,
            repository_id,
            source_revision_id,
            vector_profile,
            _object(catalog, "b"),
            view_type="vector",
        )

        original_limit = storage_protocols.RETAINED_IMPORT_RESPONSE_MAX_NODES
        monkeypatch.setattr(
            storage_protocols,
            "RETAINED_IMPORT_RESPONSE_MAX_NODES",
            json_nodes(first_summary),
        )
        with pytest.raises(CatalogValidationError, match="response bounds"):
            catalog.publish_snapshot(
                repository_id,
                source_revision_id,
                (bm25, vector),
                expected_generation=1,
            )
        monkeypatch.setattr(
            storage_protocols,
            "RETAINED_IMPORT_RESPONSE_MAX_NODES",
            original_limit,
        )

        ref = catalog.resolve_ref(repository_id)
        assert ref["snapshot_id"] == first["snapshot_id"]
        assert ref["generation"] == 1
        assert (
            catalog._connection.execute(
                "SELECT status FROM view_generations WHERE view_generation_id = ?",
                (vector,),
            ).fetchone()["status"]
            == "staged"
        )
        assert (
            catalog._connection.execute(
                "SELECT COUNT(*) FROM snapshots WHERE snapshot_id != ?",
                (first["snapshot_id"],),
            ).fetchone()[0]
            == 0
        )


def test_publish_budgets_complete_ref_response_before_moving_ref(
    tmp_path,
    monkeypatch,
):
    def json_nodes(value):
        if type(value) is list:
            return 1 + sum(json_nodes(child) for child in value)
        if type(value) is dict:
            return 1 + sum(json_nodes(child) for child in value.values())
        return 1

    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_revision_id, _, _, view_id = _setup_staged_view(catalog)
        first = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            (view_id,),
            expected_generation=0,
        )
        summary = catalog.get_manifest_summary(first["snapshot_id"])
        original_limit = storage_protocols.RETAINED_IMPORT_RESPONSE_MAX_NODES
        monkeypatch.setattr(
            storage_protocols,
            "RETAINED_IMPORT_RESPONSE_MAX_NODES",
            json_nodes(summary),
        )

        with pytest.raises(CatalogValidationError, match="response bounds"):
            catalog.publish_snapshot(
                repository_id,
                source_revision_id,
                (view_id,),
                ref_name="copy",
                expected_generation=0,
            )
        with pytest.raises(CatalogValidationError, match="response bounds"):
            catalog.publish_snapshot(
                repository_id,
                source_revision_id,
                (view_id,),
                expected_generation=0,
            )

        monkeypatch.setattr(
            storage_protocols,
            "RETAINED_IMPORT_RESPONSE_MAX_NODES",
            original_limit,
        )
        assert catalog.resolve_ref(repository_id)["snapshot_id"] == first["snapshot_id"]
        with pytest.raises(CatalogNotFoundError, match="ref not found"):
            catalog.resolve_ref(repository_id, "copy")


@pytest.mark.parametrize(
    ("column", "corrupt"),
    [
        ("digest", lambda value: value.upper()),
        ("storage_key", lambda value: f" {value} "),
        ("media_type", lambda value: f" {value} "),
    ],
)
def test_publish_rejects_noncanonical_compound_member_object_metadata(
    tmp_path,
    column,
    corrupt,
):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id = catalog.create_repository("owner/corrupt-compound")
        source_revision_id = _source(catalog, repository_id)
        profile_id = catalog.create_view_profile("semantic_facts", {})
        primary = _object(catalog, "a")
        member = _object(catalog, "b")
        view_id = catalog.stage_view_generation(
            repository_id,
            source_revision_id,
            profile_id,
            "semantic_facts",
            primary,
            schema_version="1",
            member_object_digests=(member,),
        )
        original = catalog._connection.execute(
            "SELECT * FROM objects WHERE digest = ?",
            (member,),
        ).fetchone()
        assert original is not None
        catalog._connection.execute("DROP TRIGGER objects_are_immutable")
        if column == "digest":
            catalog._connection.execute(
                "DROP TRIGGER view_generation_objects_are_immutable"
            )
            catalog._connection.execute(
                """
                INSERT INTO objects(
                    digest, storage_key, byte_size, media_type, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    corrupt(original["digest"]),
                    "corrupt/member-object",
                    original["byte_size"],
                    original["media_type"],
                    original["created_at"],
                ),
            )
            catalog._connection.execute(
                """
                UPDATE view_generation_objects SET object_digest = ?
                WHERE view_generation_id = ? AND object_digest = ?
                """,
                (corrupt(member), view_id, member),
            )
        else:
            catalog._connection.execute(
                f"UPDATE objects SET {column} = ? WHERE digest = ?",
                (corrupt(original[column]), member),
            )
        catalog._connection.commit()

        with pytest.raises(CatalogConflictError, match="member object metadata"):
            catalog.publish_snapshot(
                repository_id,
                source_revision_id,
                (view_id,),
                expected_generation=0,
            )


def test_publish_resolves_complete_pinned_manifest(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id = catalog.create_repository("owner/repo")
        source_revision_id = _source(catalog, repository_id)
        bm25_profile = catalog.create_view_profile(
            "bm25",
            {"languages": ["python"], "max_k": 128},
            name="python-sparse",
        )
        vector_profile = catalog.create_view_profile(
            "vector",
            {"languages": ["python"], "model": "test-embedding"},
            name="python-dense",
        )
        bm25 = _stage(
            catalog,
            repository_id,
            source_revision_id,
            bm25_profile,
            _object(catalog, "a", byte_size=20),
        )
        vector = _stage(
            catalog,
            repository_id,
            source_revision_id,
            vector_profile,
            _object(catalog, "b", byte_size=30),
            view_type="vector",
        )

        published = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [vector, bm25],
            expected_generation=0,
        )
        resolved = catalog.resolve_ref(repository_id)
        direct = catalog.get_manifest_summary(published["snapshot_id"])

        assert published["generation"] == 1
        assert published["changed"] is True
        assert resolved["snapshot_id"] == published["snapshot_id"]
        assert resolved["generation"] == 1
        assert published["updated_at"] == resolved["updated_at"]
        assert resolved["manifest"] == direct
        assert direct["status"] == "ready"
        assert direct["namespace"] == {
            "namespace_id": DEFAULT_NAMESPACE_ID,
            "name": "default",
        }
        assert direct["repository"] == {
            "namespace_id": DEFAULT_NAMESPACE_ID,
            "repository_key": "owner/repo",
        }
        repository_identity = RepositoryIdentity(**direct["repository"])
        assert repository_identity.repository_id == direct["repository_id"]
        source_identity = SourceRevision(
            repository_id=direct["repository_id"],
            source_kind=direct["source"]["kind"],
            commit_sha=direct["source"]["commit_sha"],
            tree_sha=direct["source"]["tree_sha"],
            source_fingerprint=direct["source"]["source_fingerprint"],
        )
        assert source_identity.source_revision_id == source_revision_id
        assert direct["source"]["source_revision_id"] == source_revision_id
        assert direct["source"]["kind"] == "clean"
        assert direct["views"]["bm25"]["profile"] == {
            "profile_id": bm25_profile,
            "name": "python-sparse",
            "config": {"languages": ["python"], "max_k": 128},
        }
        assert direct["views"]["vector"]["profile"] == {
            "profile_id": vector_profile,
            "name": "python-dense",
            "config": {"languages": ["python"], "model": "test-embedding"},
        }
        assert list(direct["views"]) == ["bm25", "vector"]
        assert direct["views"]["bm25"]["object"]["byte_size"] == 20
        assert direct["views"]["vector"]["object"]["byte_size"] == 30
        snapshot_views = []
        for view_type, raw in direct["views"].items():
            profile = ViewProfile.create(
                view_type,
                raw["profile"]["config"],
                name=raw["profile"]["name"],
            )
            assert profile.profile_id == raw["profile"]["profile_id"]
            generation = ViewGeneration.create(
                source_identity,
                profile,
                ObjectRecord(
                    digest=raw["object"]["digest"],
                    storage_key=raw["object"]["storage_key"],
                    byte_size=raw["object"]["byte_size"],
                    media_type=raw["object"]["media_type"],
                ),
                schema_version=raw["schema_version"],
                metadata=raw["metadata"],
            )
            assert generation.view_generation_id == raw["view_generation_id"]
            snapshot_views.append(SnapshotView(generation))
        assert (
            PublishedSnapshot(
                direct["repository_id"],
                source_revision_id,
                tuple(snapshot_views),
            ).snapshot_id
            == direct["snapshot_id"]
        )

        statuses = catalog._connection.execute(
            "SELECT DISTINCT status FROM view_generations"
        ).fetchall()
        assert [row["status"] for row in statuses] == ["ready"]


def test_manifest_summary_closes_custom_namespace_and_repository_identity(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        namespace_id = catalog.create_namespace("team-one")
        repository_id = catalog.create_repository(
            "owner/repo",
            namespace_id=namespace_id,
        )
        source_revision_id = _source(catalog, repository_id)
        profile_id = catalog.create_view_profile("bm25")
        generation_id = _stage(
            catalog,
            repository_id,
            source_revision_id,
            profile_id,
            _object(catalog, "c"),
        )
        published = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [generation_id],
            expected_generation=0,
        )

        summary = catalog.get_manifest_summary(published["snapshot_id"])

        assert summary["namespace"] == {
            "namespace_id": namespace_id,
            "name": "team-one",
        }
        assert summary["repository"] == {
            "namespace_id": namespace_id,
            "repository_key": "owner/repo",
        }
        assert (
            RepositoryIdentity(**summary["repository"]).repository_id == repository_id
        )


def test_publish_retry_for_current_snapshot_is_desired_state_idempotent(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_revision_id, _, _, view_id = _setup_staged_view(catalog)

        first = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [view_id],
            expected_generation=0,
        )
        retry_with_current_generation = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [view_id],
            expected_generation=1,
        )
        retry_with_original_generation = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [view_id],
            expected_generation=0,
        )

        assert first["changed"] is True
        for retry in (retry_with_current_generation, retry_with_original_generation):
            assert retry == {
                "snapshot_id": first["snapshot_id"],
                "repository_id": repository_id,
                "ref_name": "main",
                "generation": 1,
                "updated_at": first["updated_at"],
                "changed": False,
            }
        assert catalog.resolve_ref(repository_id)["generation"] == 1


@pytest.mark.parametrize(
    ("table", "key_column", "dependency"),
    (
        ("repositories", "repository_id", "repository"),
        ("source_revisions", "source_revision_id", "source"),
        ("view_profiles", "profile_id", "profile"),
        ("objects", "digest", "object"),
    ),
)
def test_idempotent_retry_revalidates_input_dependencies(
    tmp_path, table, key_column, dependency
):
    path = tmp_path / f"{dependency}.sqlite3"
    with SQLiteCatalog(path) as catalog:
        (
            repository_id,
            source_revision_id,
            profile_id,
            object_digest,
            view_id,
        ) = _setup_staged_view(catalog)
        catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [view_id],
            expected_generation=0,
        )

    identifiers = {
        "repositories": repository_id,
        "source_revisions": source_revision_id,
        "view_profiles": profile_id,
        "objects": object_digest,
    }
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    if table == "objects":
        connection.execute("DROP TRIGGER referenced_objects_cannot_be_deleted")
    connection.execute(
        f"DELETE FROM {table} WHERE {key_column} = ?", (identifiers[table],)
    )
    connection.commit()
    connection.close()

    with SQLiteCatalog(path) as catalog:
        with pytest.raises(CatalogNotFoundError, match="not found"):
            catalog.publish_snapshot(
                repository_id,
                source_revision_id,
                [view_id],
                expected_generation=0,
            )


@pytest.mark.parametrize("dependency", ("repository", "source", "source_digest"))
def test_publish_recomputes_repository_and_source_content_identities(
    tmp_path, dependency
):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_revision_id, _, _, view_id = _setup_staged_view(catalog)
        mutations = {
            "repository": (
                "repositories_are_immutable",
                "UPDATE repositories SET repository_key = 'owner/tampered' "
                "WHERE repository_id = ?",
                repository_id,
            ),
            "source": (
                "source_revisions_are_immutable",
                "UPDATE source_revisions SET commit_sha = ? "
                "WHERE source_revision_id = ?",
                ("b" * 40, source_revision_id),
            ),
            "source_digest": (
                "source_revisions_are_immutable",
                "UPDATE source_revisions SET identity_digest = ? "
                "WHERE source_revision_id = ?",
                ("b" * 64, source_revision_id),
            ),
        }
        trigger, statement, parameters = mutations[dependency]
        catalog._connection.execute(f"DROP TRIGGER {trigger}")
        if isinstance(parameters, tuple):
            catalog._connection.execute(statement, parameters)
        else:
            catalog._connection.execute(statement, (parameters,))

        with pytest.raises(
            CatalogConflictError, match="repository or source revision identity"
        ):
            catalog.publish_snapshot(
                repository_id,
                source_revision_id,
                [view_id],
                expected_generation=0,
            )


@pytest.mark.parametrize("dependency", ("repository", "source"))
def test_publish_rejects_raw_replace_of_repository_or_source_identity(
    tmp_path, dependency
):
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        repository_id, source_revision_id, _, _, view_id = _setup_staged_view(catalog)

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("PRAGMA recursive_triggers = OFF")
    if dependency == "repository":
        row = connection.execute(
            "SELECT namespace_id, created_at FROM repositories "
            "WHERE repository_id = ?",
            (repository_id,),
        ).fetchone()
        connection.execute(
            """
            INSERT OR REPLACE INTO repositories(
                repository_id, namespace_id, repository_key, created_at
            ) VALUES (?, ?, 'owner/tampered', ?)
            """,
            (repository_id, row[0], row[1]),
        )
    else:
        row = connection.execute(
            "SELECT * FROM source_revisions WHERE source_revision_id = ?",
            (source_revision_id,),
        ).fetchone()
        connection.execute(
            """
            INSERT OR REPLACE INTO source_revisions(
                source_revision_id, repository_id, source_kind, commit_sha,
                tree_sha, source_fingerprint, identity_digest, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row[0],
                row[1],
                row[2],
                "b" * 40,
                row[4],
                row[5],
                row[6],
                row[7],
            ),
        )
    connection.commit()
    connection.close()

    with SQLiteCatalog(path) as catalog:
        with pytest.raises(
            CatalogConflictError, match="repository or source revision identity"
        ):
            catalog.publish_snapshot(
                repository_id,
                source_revision_id,
                [view_id],
                expected_generation=0,
            )


def test_raw_connection_cannot_replace_or_delete_referenced_object(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        _, _, _, object_digest, _ = _setup_staged_view(catalog)

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("PRAGMA recursive_triggers = OFF")
    original = connection.execute(
        "SELECT digest, storage_key, byte_size, media_type FROM objects"
    ).fetchone()
    assert original is not None
    with pytest.raises(sqlite3.IntegrityError, match="duplicate object insert"):
        connection.execute(
            """
            INSERT OR REPLACE INTO objects(
                digest, storage_key, byte_size, media_type, created_at
            ) VALUES (?, 'sha256/changed/object', 99, 'application/evil', 'now')
            """,
            (object_digest,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="duplicate object insert"):
        connection.execute(
            """
            INSERT OR REPLACE INTO objects(
                digest, storage_key, byte_size, media_type, created_at
            ) VALUES (?, ?, 99, 'application/evil', 'now')
            """,
            ("b" * 64, original[1]),
        )
    with pytest.raises(sqlite3.IntegrityError, match="referenced objects"):
        connection.execute("DELETE FROM objects WHERE digest = ?", (object_digest,))
    connection.rollback()
    assert (
        connection.execute(
            "SELECT digest, storage_key, byte_size, media_type FROM objects"
        ).fetchone()
        == original
    )
    connection.close()


def test_raw_connection_can_delete_unreferenced_object_for_future_gc(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        digest = _object(catalog, "a")

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("PRAGMA recursive_triggers = OFF")
    connection.execute("DELETE FROM objects WHERE digest = ?", (digest,))
    connection.commit()
    assert connection.execute("SELECT COUNT(*) FROM objects").fetchone()[0] == 0
    connection.close()


def test_raw_connection_cannot_replace_or_delete_published_aggregate(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        repository_id, source_revision_id, _, _, view_id = _setup_staged_view(catalog)
        published = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [view_id],
            expected_generation=0,
        )

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("PRAGMA recursive_triggers = OFF")
    replacements = (
        (
            "duplicate view generation insert",
            """
            INSERT OR REPLACE INTO view_generations(
                view_generation_id, repository_id, source_revision_id,
                profile_id, view_type, object_digest, schema_version,
                metadata_json, status, created_at, ready_at
            ) SELECT
                view_generation_id, repository_id, source_revision_id,
                profile_id, view_type, object_digest, schema_version,
                metadata_json, status, created_at, ready_at
            FROM view_generations WHERE view_generation_id = ?
            """,
            (view_id,),
        ),
        (
            "duplicate snapshot insert",
            """
            INSERT OR REPLACE INTO snapshots(
                snapshot_id, repository_id, source_revision_id,
                content_digest, status, published_at
            ) SELECT
                snapshot_id, repository_id, source_revision_id,
                content_digest, status, published_at
            FROM snapshots WHERE snapshot_id = ?
            """,
            (published["snapshot_id"],),
        ),
        (
            "duplicate snapshot insert",
            """
            INSERT OR REPLACE INTO snapshots(
                snapshot_id, repository_id, source_revision_id,
                content_digest, status, published_at
            ) SELECT
                'snapshot_replacement', repository_id, source_revision_id,
                content_digest, status, published_at
            FROM snapshots WHERE snapshot_id = ?
            """,
            (published["snapshot_id"],),
        ),
        (
            "duplicate ref insert",
            """
            INSERT OR REPLACE INTO refs(
                repository_id, ref_name, snapshot_id, generation, updated_at
            ) SELECT
                repository_id, ref_name, snapshot_id, generation, updated_at
            FROM refs WHERE repository_id = ? AND ref_name = 'main'
            """,
            (repository_id,),
        ),
    )
    for message, statement, parameters in replacements:
        with pytest.raises(sqlite3.IntegrityError, match=message):
            connection.execute(statement, parameters)
    with pytest.raises(sqlite3.IntegrityError, match="ref identity is immutable"):
        connection.execute(
            "UPDATE refs SET ref_name = 'renamed' "
            "WHERE repository_id = ? AND ref_name = 'main'",
            (repository_id,),
        )

    deletions = (
        (
            "referenced view generations",
            "DELETE FROM view_generations WHERE view_generation_id = ?",
            view_id,
        ),
        (
            "referenced snapshots",
            "DELETE FROM snapshots WHERE snapshot_id = ?",
            published["snapshot_id"],
        ),
        (
            "refs cannot be deleted",
            "DELETE FROM refs WHERE repository_id = ? AND ref_name = 'main'",
            repository_id,
        ),
    )
    for message, statement, identifier in deletions:
        with pytest.raises(sqlite3.IntegrityError, match=message):
            connection.execute(statement, (identifier,))
    connection.rollback()
    assert (
        connection.execute("SELECT COUNT(*) FROM view_generations").fetchone()[0] == 1
    )
    assert connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM refs").fetchone()[0] == 1
    connection.close()


def test_concurrent_stage_calls_converge_without_replace(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        repository_id = catalog.create_repository("owner/repo")
        source_revision_id = _source(catalog, repository_id)
        profile_id = catalog.create_view_profile("bm25", {})
        object_digest = _object(catalog, "a")

    barrier = Barrier(2)

    def stage():
        with SQLiteCatalog(path) as catalog:
            barrier.wait()
            return _stage(
                catalog,
                repository_id,
                source_revision_id,
                profile_id,
                object_digest,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(stage) for _ in range(2)]
        results = [future.result() for future in futures]

    assert results[0] == results[1]
    with SQLiteCatalog(path) as catalog:
        assert (
            catalog._connection.execute(
                "SELECT COUNT(*) FROM view_generations"
            ).fetchone()[0]
            == 1
        )


def test_ref_database_gates_reject_cross_repository_targets(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        repository_one, source_one, _, _, view_one = _setup_staged_view(catalog, "a")
        snapshot_one = catalog.publish_snapshot(
            repository_one,
            source_one,
            [view_one],
            expected_generation=0,
        )
        repository_two = catalog.create_repository("owner/two")
        source_two = _source(catalog, repository_two, "b")
        profile_two = catalog.create_view_profile("vector", {})
        view_two = _stage(
            catalog,
            repository_two,
            source_two,
            profile_two,
            _object(catalog, "b"),
            view_type="vector",
        )
        catalog.publish_snapshot(
            repository_two,
            source_two,
            [view_two],
            expected_generation=0,
        )

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("PRAGMA recursive_triggers = OFF")
    updated_at = connection.execute(
        "SELECT updated_at FROM refs WHERE repository_id = ?",
        (repository_one,),
    ).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="their repository"):
        connection.execute(
            """
            INSERT INTO refs(
                repository_id, ref_name, snapshot_id, generation, updated_at
            ) VALUES (?, 'cross', ?, 1, ?)
            """,
            (repository_two, snapshot_one["snapshot_id"], updated_at),
        )
    with pytest.raises(sqlite3.IntegrityError, match="their repository"):
        connection.execute(
            "UPDATE refs SET snapshot_id = ? "
            "WHERE repository_id = ? AND ref_name = 'main'",
            (snapshot_one["snapshot_id"], repository_two),
        )
    connection.rollback()
    connection.close()


def test_resolve_ref_rejects_cross_repository_target_after_trigger_bypass(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        repository_one, source_one, _, _, view_one = _setup_staged_view(catalog, "a")
        snapshot_one = catalog.publish_snapshot(
            repository_one,
            source_one,
            [view_one],
            expected_generation=0,
        )
        repository_two = catalog.create_repository("owner/two")
        source_two = _source(catalog, repository_two, "b")
        profile_two = catalog.create_view_profile("vector", {})
        view_two = _stage(
            catalog,
            repository_two,
            source_two,
            profile_two,
            _object(catalog, "b"),
            view_type="vector",
        )
        catalog.publish_snapshot(
            repository_two,
            source_two,
            [view_two],
            expected_generation=0,
        )
        catalog._connection.execute(
            "DROP TRIGGER refs_require_ready_snapshot_on_update"
        )
        catalog._connection.execute("PRAGMA foreign_keys = OFF")
        catalog._connection.execute(
            "UPDATE refs SET snapshot_id = ? "
            "WHERE repository_id = ? AND ref_name = 'main'",
            (snapshot_one["snapshot_id"], repository_two),
        )
        catalog._connection.execute("PRAGMA foreign_keys = ON")

        with pytest.raises(CatalogConflictError, match="another repository"):
            catalog.resolve_ref(repository_two)


@pytest.mark.parametrize("column", ("commit_sha", "tree_sha"))
def test_publish_rejects_noncanonical_uppercase_source_fields(tmp_path, column):
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        repository_id, source_revision_id, _, _, view_id = _setup_staged_view(catalog)
        published = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [view_id],
            expected_generation=0,
        )

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("PRAGMA recursive_triggers = OFF")
    row = connection.execute(
        "SELECT * FROM source_revisions WHERE source_revision_id = ?",
        (source_revision_id,),
    ).fetchone()
    values = list(row)
    values[3 if column == "commit_sha" else 4] = values[
        3 if column == "commit_sha" else 4
    ].upper()
    connection.execute(
        """
        INSERT OR REPLACE INTO source_revisions(
            source_revision_id, repository_id, source_kind, commit_sha,
            tree_sha, source_fingerprint, identity_digest, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        values,
    )
    connection.commit()
    connection.close()

    with SQLiteCatalog(path) as catalog:
        with pytest.raises(
            CatalogConflictError, match="repository or source revision identity"
        ):
            catalog.publish_snapshot(
                repository_id,
                source_revision_id,
                [view_id],
                expected_generation=0,
            )
        with pytest.raises(
            CatalogConflictError, match="repository or source revision identity"
        ):
            catalog.get_manifest_summary(published["snapshot_id"])


@pytest.mark.parametrize("missing", ("profile", "generation", "object", "member"))
def test_manifest_summary_rejects_missing_membership_dependencies(tmp_path, missing):
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        (
            repository_id,
            source_revision_id,
            profile_id,
            object_digest,
            view_id,
        ) = _setup_staged_view(catalog)
        published = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [view_id],
            expected_generation=0,
        )

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("PRAGMA recursive_triggers = OFF")
    operations = {
        "profile": ("DELETE FROM view_profiles WHERE profile_id = ?", profile_id),
        "generation": (
            "DROP TRIGGER referenced_view_generations_cannot_be_deleted",
            None,
        ),
        "object": ("DROP TRIGGER referenced_objects_cannot_be_deleted", None),
        "member": ("DROP TRIGGER ready_snapshot_views_cannot_be_deleted", None),
    }
    statement, identifier = operations[missing]
    connection.execute(statement, () if identifier is None else (identifier,))
    if missing == "generation":
        connection.execute(
            "DELETE FROM view_generations WHERE view_generation_id = ?", (view_id,)
        )
    elif missing == "object":
        connection.execute("DELETE FROM objects WHERE digest = ?", (object_digest,))
    elif missing == "member":
        connection.execute(
            "DELETE FROM snapshot_views WHERE snapshot_id = ?",
            (published["snapshot_id"],),
        )
    connection.commit()
    connection.close()

    with SQLiteCatalog(path) as catalog:
        with pytest.raises(CatalogConflictError, match="membership"):
            catalog.get_manifest_summary(published["snapshot_id"])
        with pytest.raises(CatalogConflictError, match="membership"):
            catalog.resolve_ref(repository_id)


@pytest.mark.parametrize("dependency", ("profile", "generation"))
def test_manifest_summary_recomputes_joined_dependency_identities(tmp_path, dependency):
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        repository_id, source_revision_id, profile_id, _, view_id = _setup_staged_view(
            catalog
        )
        published = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [view_id],
            expected_generation=0,
        )

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("PRAGMA recursive_triggers = OFF")
    if dependency == "profile":
        row = connection.execute(
            "SELECT * FROM view_profiles WHERE profile_id = ?", (profile_id,)
        ).fetchone()
        connection.execute(
            """
            INSERT OR REPLACE INTO view_profiles(
                profile_id, view_type, name, config_json,
                profile_digest, created_at
            ) VALUES (?, ?, 'tampered', ?, ?, ?)
            """,
            (row[0], row[1], row[3], row[4], row[5]),
        )
        message = "profile identity"
    else:
        connection.execute("DROP TRIGGER view_generations_reject_duplicate_inserts")
        row = connection.execute(
            "SELECT * FROM view_generations WHERE view_generation_id = ?", (view_id,)
        ).fetchone()
        values = list(row)
        values[7] = '{"tampered":true}'
        connection.execute(
            """
            INSERT OR REPLACE INTO view_generations(
                view_generation_id, repository_id, source_revision_id,
                profile_id, view_type, object_digest, schema_version,
                metadata_json, status, created_at, ready_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        message = "generation identity"
    connection.commit()
    connection.close()

    with SQLiteCatalog(path) as catalog:
        with pytest.raises(CatalogConflictError, match=message):
            catalog.get_manifest_summary(published["snapshot_id"])


def test_manifest_summary_rejects_membership_and_snapshot_identity_drift(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_revision_id, _, _, view_id = _setup_staged_view(catalog)
        published = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [view_id],
            expected_generation=0,
        )
        catalog._connection.execute("DROP TRIGGER snapshot_views_are_immutable")
        catalog._connection.execute("PRAGMA foreign_keys = OFF")
        catalog._connection.execute(
            "UPDATE snapshot_views SET view_type = 'vector' " "WHERE snapshot_id = ?",
            (published["snapshot_id"],),
        )
        catalog._connection.execute("PRAGMA foreign_keys = ON")

        with pytest.raises(CatalogConflictError, match="snapshot content identity"):
            catalog.get_manifest_summary(published["snapshot_id"])


@pytest.mark.parametrize(
    "timestamp", ("now", "2026-01-01T00:00:00", "2026-01-01T01:00:00+01:00")
)
def test_idempotent_publish_rejects_noncanonical_publication_timestamps(
    tmp_path, timestamp
):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_revision_id, _, _, view_id = _setup_staged_view(catalog)
        published = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [view_id],
            expected_generation=0,
        )
        catalog._connection.execute(
            "UPDATE refs SET updated_at = ? WHERE repository_id = ?",
            (timestamp, repository_id),
        )

        with pytest.raises(CatalogConflictError, match="publication metadata"):
            catalog.publish_snapshot(
                repository_id,
                source_revision_id,
                [view_id],
                expected_generation=0,
            )
        with pytest.raises(CatalogConflictError, match="publication metadata"):
            catalog.resolve_ref(repository_id)
        assert published["changed"] is True


def test_publish_rejects_real_ref_generation_without_integer_coercion(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_revision_id, _, _, view_id = _setup_staged_view(catalog)
        catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [view_id],
            expected_generation=0,
        )
        catalog._connection.execute(
            "UPDATE refs SET generation = 1.5 WHERE repository_id = ?",
            (repository_id,),
        )

        with pytest.raises(CatalogConflictError, match="publication metadata"):
            catalog.publish_snapshot(
                repository_id,
                source_revision_id,
                [view_id],
                expected_generation=1,
            )
        with pytest.raises(CatalogConflictError, match="publication metadata"):
            catalog.resolve_ref(repository_id)


def test_new_publish_rejects_invalid_staged_readiness_and_rolls_back(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_revision_id, _, _, view_id = _setup_staged_view(catalog)
        catalog._connection.execute("PRAGMA ignore_check_constraints = ON")
        catalog._connection.execute(
            "UPDATE view_generations SET ready_at = 'now' "
            "WHERE view_generation_id = ?",
            (view_id,),
        )

        with pytest.raises(CatalogConflictError, match="readiness metadata"):
            catalog.publish_snapshot(
                repository_id,
                source_revision_id,
                [view_id],
                expected_generation=0,
            )

        assert (
            catalog._connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            == 0
        )
        assert (
            catalog._connection.execute(
                "SELECT status FROM view_generations WHERE view_generation_id = ?",
                (view_id,),
            ).fetchone()["status"]
            == "staged"
        )


@pytest.mark.parametrize("target", ("snapshot", "view"))
def test_publish_rejects_noncanonical_ready_timestamps_on_all_reuse_paths(
    tmp_path, target
):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_revision_id, _, _, view_id = _setup_staged_view(catalog)
        published = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [view_id],
            expected_generation=0,
        )
        if target == "snapshot":
            catalog._connection.execute("DROP TRIGGER snapshot_seal_is_the_only_update")
            catalog._connection.execute(
                "UPDATE snapshots SET published_at = 'now' WHERE snapshot_id = ?",
                (published["snapshot_id"],),
            )
            message = "seal state conflicts"
        else:
            catalog._connection.execute(
                "DROP TRIGGER ready_view_generations_are_immutable"
            )
            catalog._connection.execute(
                "UPDATE view_generations SET ready_at = 'now' "
                "WHERE view_generation_id = ?",
                (view_id,),
            )
            message = "ready_at"

        for ref_name in ("main", "stable"):
            with pytest.raises(CatalogConflictError, match=message):
                catalog.publish_snapshot(
                    repository_id,
                    source_revision_id,
                    [view_id],
                    ref_name=ref_name,
                    expected_generation=0,
                )


@pytest.mark.parametrize(
    ("trigger", "corruption", "target", "message"),
    (
        (
            "ready_snapshot_views_cannot_be_deleted",
            "DELETE FROM snapshot_views WHERE view_generation_id = ?",
            "view",
            "membership conflicts",
        ),
        (
            "snapshot_seal_is_the_only_update",
            """
            UPDATE snapshots SET status = 'building', published_at = NULL
            WHERE snapshot_id = ?
            """,
            "snapshot",
            "seal state conflicts",
        ),
        (
            "ready_view_generations_are_immutable",
            """
            UPDATE view_generations SET status = 'staged', ready_at = NULL
            WHERE view_generation_id = ?
            """,
            "view",
            "membership conflicts",
        ),
        (
            "ready_view_generations_are_immutable",
            """
            UPDATE view_generations SET ready_at = ''
            WHERE view_generation_id = ?
            """,
            "view",
            "ready_at",
        ),
    ),
)
def test_idempotent_retry_rejects_corrupt_ready_state(
    tmp_path, trigger, corruption, target, message
):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_revision_id, _, _, view_id = _setup_staged_view(catalog)
        published = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [view_id],
            expected_generation=0,
        )
        catalog._connection.execute(f"DROP TRIGGER {trigger}")
        target_id = published["snapshot_id"] if target == "snapshot" else view_id
        catalog._connection.execute(corruption, (target_id,))

        with pytest.raises(CatalogConflictError, match=message):
            catalog.publish_snapshot(
                repository_id,
                source_revision_id,
                [view_id],
                expected_generation=0,
            )


@pytest.mark.parametrize("identity_column", ("profile_id", "object_digest"))
def test_idempotent_retry_rejects_generation_dependency_mismatch(
    tmp_path, identity_column
):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_revision_id, _, _, view_id = _setup_staged_view(catalog)
        catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [view_id],
            expected_generation=0,
        )
        replacements = {
            "profile_id": catalog.create_view_profile(
                "bm25", {"variant": "tampered"}, name="tampered"
            ),
            "object_digest": _object(catalog, "b"),
        }
        catalog._connection.execute(
            "DROP TRIGGER staged_view_generation_identity_is_immutable"
        )
        catalog._connection.execute("DROP TRIGGER ready_view_generations_are_immutable")
        catalog._connection.execute(
            f"""
            UPDATE view_generations SET {identity_column} = ?
            WHERE view_generation_id = ?
            """,
            (replacements[identity_column], view_id),
        )

        with pytest.raises(CatalogConflictError, match="generation identity conflicts"):
            catalog.publish_snapshot(
                repository_id,
                source_revision_id,
                [view_id],
                expected_generation=0,
            )


def test_two_connections_converge_on_the_same_desired_snapshot(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        repository_id, source_revision_id, _, _, view_id = _setup_staged_view(catalog)

    barrier = Barrier(2)

    def publish():
        with SQLiteCatalog(path, busy_timeout_ms=5_000) as catalog:
            barrier.wait()
            return catalog.publish_snapshot(
                repository_id,
                source_revision_id,
                [view_id],
                expected_generation=0,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(publish) for _ in range(2)]
        results = [future.result() for future in futures]

    assert sorted(result["changed"] for result in results) == [False, True]
    assert {result["generation"] for result in results} == {1}
    assert {result["snapshot_id"] for result in results} == {results[0]["snapshot_id"]}
    assert {result["updated_at"] for result in results} == {results[0]["updated_at"]}
    with SQLiteCatalog(path) as catalog:
        assert catalog.resolve_ref(repository_id)["generation"] == 1


def test_two_connections_keep_cas_for_different_desired_snapshots(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        (
            repository_id,
            source_revision_id,
            profile_id,
            _,
            first_view,
        ) = _setup_staged_view(catalog)
        second_view = _stage(
            catalog,
            repository_id,
            source_revision_id,
            profile_id,
            _object(catalog, "b"),
        )
        views = (first_view, second_view)

    barrier = Barrier(2)

    def publish(view_id):
        with SQLiteCatalog(path, busy_timeout_ms=5_000) as catalog:
            barrier.wait()
            try:
                result = catalog.publish_snapshot(
                    repository_id,
                    source_revision_id,
                    [view_id],
                    expected_generation=0,
                )
            except CatalogConflictError:
                return view_id, None
            return view_id, result

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(publish, view_id) for view_id in views]
        outcomes = [future.result() for future in futures]

    successes = [
        (view_id, result) for view_id, result in outcomes if result is not None
    ]
    conflicts = [(view_id, result) for view_id, result in outcomes if result is None]
    assert len(successes) == 1
    assert len(conflicts) == 1
    winning_view, published = successes[0]
    losing_view, _ = conflicts[0]
    assert published["changed"] is True
    assert published["generation"] == 1

    with SQLiteCatalog(path) as catalog:
        resolved = catalog.resolve_ref(repository_id)
        statuses = {
            row["view_generation_id"]: row["status"]
            for row in catalog._connection.execute(
                """
                SELECT view_generation_id, status FROM view_generations
                WHERE view_generation_id IN (?, ?)
                """,
                views,
            ).fetchall()
        }
        assert resolved["snapshot_id"] == published["snapshot_id"]
        assert statuses == {winning_view: "ready", losing_view: "staged"}


def test_stale_cas_rolls_back_and_keeps_old_ref(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id = catalog.create_repository("owner/repo")
        source_revision_id = _source(catalog, repository_id)
        profile_id = catalog.create_view_profile("bm25", {})
        old_view = _stage(
            catalog,
            repository_id,
            source_revision_id,
            profile_id,
            _object(catalog, "a"),
        )
        first = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [old_view],
            expected_generation=0,
        )
        replacement_view = _stage(
            catalog,
            repository_id,
            source_revision_id,
            profile_id,
            _object(catalog, "b"),
        )

        with pytest.raises(CatalogConflictError, match="generation is 1"):
            catalog.publish_snapshot(
                repository_id,
                source_revision_id,
                [replacement_view],
                expected_generation=0,
            )

        resolved = catalog.resolve_ref(repository_id)
        replacement_status = catalog._connection.execute(
            """
            SELECT status FROM view_generations WHERE view_generation_id = ?
            """,
            (replacement_view,),
        ).fetchone()["status"]
        snapshot_count = catalog._connection.execute(
            "SELECT COUNT(*) FROM snapshots"
        ).fetchone()[0]
        building_count = catalog._connection.execute(
            "SELECT COUNT(*) FROM snapshots WHERE status = 'building'"
        ).fetchone()[0]
        assert resolved["snapshot_id"] == first["snapshot_id"]
        assert resolved["generation"] == 1
        assert replacement_status == "staged"
        assert snapshot_count == 1
        assert building_count == 0

        second = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [replacement_view],
            expected_generation=1,
        )
        assert second["generation"] == 2
        assert (
            catalog.resolve_ref(repository_id)["snapshot_id"] == second["snapshot_id"]
        )


def test_ready_generation_can_be_reused_in_a_later_snapshot(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id = catalog.create_repository("owner/repo")
        source_revision_id = _source(catalog, repository_id)
        bm25_profile = catalog.create_view_profile("bm25", {"max_k": 128})
        vector_profile = catalog.create_view_profile(
            "vector", {"model": "test-embedding"}
        )
        bm25 = _stage(
            catalog,
            repository_id,
            source_revision_id,
            bm25_profile,
            _object(catalog, "a"),
        )
        first = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [bm25],
            expected_generation=0,
        )
        alias = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [bm25],
            ref_name="stable",
            expected_generation=0,
        )
        assert alias["snapshot_id"] == first["snapshot_id"]
        assert alias["changed"] is True
        assert alias["generation"] == 1
        vector = _stage(
            catalog,
            repository_id,
            source_revision_id,
            vector_profile,
            _object(catalog, "b"),
            view_type="vector",
        )

        second = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [bm25, vector],
            expected_generation=1,
        )
        manifest = catalog.get_manifest_summary(second["snapshot_id"])

        assert second["generation"] == 2
        assert manifest["views"]["bm25"]["view_generation_id"] == bm25
        assert manifest["views"]["vector"]["view_generation_id"] == vector


def test_ready_snapshot_membership_stays_immutable_after_ref_moves(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id = catalog.create_repository("owner/repo")
        source_revision_id = _source(catalog, repository_id)
        bm25_profile = catalog.create_view_profile("bm25", {})
        old_view = _stage(
            catalog,
            repository_id,
            source_revision_id,
            bm25_profile,
            _object(catalog, "a"),
        )
        old_snapshot = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [old_view],
            expected_generation=0,
        )
        replacement_view = _stage(
            catalog,
            repository_id,
            source_revision_id,
            bm25_profile,
            _object(catalog, "b"),
        )
        catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [replacement_view],
            expected_generation=1,
        )
        vector_profile = catalog.create_view_profile("vector", {})
        vector_view = _stage(
            catalog,
            repository_id,
            source_revision_id,
            vector_profile,
            _object(catalog, "c"),
            view_type="vector",
        )

        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            catalog._connection.execute(
                """
                INSERT INTO snapshot_views(snapshot_id, view_type, view_generation_id)
                VALUES (?, 'vector', ?)
                """,
                (old_snapshot["snapshot_id"], vector_view),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            catalog._connection.execute(
                """
                DELETE FROM snapshot_views
                WHERE snapshot_id = ? AND view_generation_id = ?
                """,
                (old_snapshot["snapshot_id"], old_view),
            )

        catalog._connection.execute(
            "DELETE FROM snapshots WHERE snapshot_id = ?",
            (old_snapshot["snapshot_id"],),
        )
        assert (
            catalog._connection.execute(
                "SELECT COUNT(*) FROM snapshot_views WHERE snapshot_id = ?",
                (old_snapshot["snapshot_id"],),
            ).fetchone()[0]
            == 0
        )


def test_direct_sql_cannot_seal_invalid_or_expose_building_snapshots(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_one = catalog.create_repository("owner/one")
        source_one = _source(catalog, repository_one, "a")
        profile_id = catalog.create_view_profile("bm25", {})
        ready_view = _stage(
            catalog,
            repository_one,
            source_one,
            profile_id,
            _object(catalog, "a"),
        )
        catalog.publish_snapshot(
            repository_one,
            source_one,
            [ready_view],
            expected_generation=0,
        )

        repository_two = catalog.create_repository("owner/two")
        source_two = _source(catalog, repository_two, "b")
        staged_view = _stage(
            catalog,
            repository_two,
            source_two,
            profile_id,
            _object(catalog, "b"),
        )

        catalog._connection.execute(
            """
            INSERT INTO snapshots(
                snapshot_id, repository_id, source_revision_id,
                content_digest, status, published_at
            ) VALUES ('cross-source', ?, ?, 'cross', 'building', NULL)
            """,
            (repository_two, source_two),
        )
        catalog._connection.execute(
            """
            INSERT INTO snapshot_views(snapshot_id, view_type, view_generation_id)
            VALUES ('cross-source', 'bm25', ?)
            """,
            (ready_view,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="snapshot seal"):
            catalog._connection.execute(
                """
                UPDATE snapshots SET status = 'ready', published_at = 'now'
                WHERE snapshot_id = 'cross-source'
                """
            )

        catalog._connection.execute(
            """
            INSERT INTO snapshots(
                snapshot_id, repository_id, source_revision_id,
                content_digest, status, published_at
            ) VALUES ('staged-view', ?, ?, 'staged', 'building', NULL)
            """,
            (repository_two, source_two),
        )
        catalog._connection.execute(
            """
            INSERT INTO snapshot_views(snapshot_id, view_type, view_generation_id)
            VALUES ('staged-view', 'bm25', ?)
            """,
            (staged_view,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="snapshot seal"):
            catalog._connection.execute(
                """
                UPDATE snapshots SET status = 'ready', published_at = 'now'
                WHERE snapshot_id = 'staged-view'
                """
            )

        catalog._connection.execute(
            """
            INSERT INTO snapshots(
                snapshot_id, repository_id, source_revision_id,
                content_digest, status, published_at
            ) VALUES ('empty', ?, ?, 'empty', 'building', NULL)
            """,
            (repository_two, source_two),
        )
        with pytest.raises(sqlite3.IntegrityError, match="snapshot seal"):
            catalog._connection.execute(
                """
                UPDATE snapshots SET status = 'ready', published_at = 'now'
                WHERE snapshot_id = 'empty'
                """
            )
        with pytest.raises(CatalogValidationError, match="not ready"):
            catalog.get_manifest_summary("empty")

        with pytest.raises(sqlite3.IntegrityError, match="ready snapshots"):
            catalog._connection.execute(
                """
                INSERT INTO refs(
                    repository_id, ref_name, snapshot_id, generation, updated_at
                ) VALUES (?, 'unsafe', 'empty', 1, 'now')
                """,
                (repository_two,),
            )

        catalog._connection.execute(
            """
            INSERT INTO snapshots(
                snapshot_id, repository_id, source_revision_id,
                content_digest, status, published_at
            ) VALUES ('update-target', ?, ?, 'target', 'building', NULL)
            """,
            (repository_one, source_one),
        )
        with pytest.raises(CatalogValidationError, match="not ready"):
            catalog.get_manifest_summary("update-target")
        with pytest.raises(sqlite3.IntegrityError, match="ready snapshots"):
            catalog._connection.execute(
                """
                UPDATE refs SET snapshot_id = 'update-target', generation = 2
                WHERE repository_id = ? AND ref_name = 'main'
                """,
                (repository_one,),
            )


def test_late_ref_failure_rolls_back_building_snapshot_and_view_seal(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id = catalog.create_repository("owner/repo")
        source_revision_id = _source(catalog, repository_id)
        profile_id = catalog.create_view_profile("bm25", {})
        view_id = _stage(
            catalog,
            repository_id,
            source_revision_id,
            profile_id,
            _object(catalog, "a"),
        )
        catalog._connection.execute(
            """
            CREATE TRIGGER fail_ref_publication
            BEFORE INSERT ON refs
            BEGIN
                SELECT RAISE(ABORT, 'simulated ref failure');
            END
            """
        )

        with pytest.raises(sqlite3.IntegrityError, match="simulated ref failure"):
            catalog.publish_snapshot(
                repository_id,
                source_revision_id,
                [view_id],
                expected_generation=0,
            )

        assert (
            catalog._connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            == 0
        )
        assert (
            catalog._connection.execute(
                "SELECT status FROM view_generations WHERE view_generation_id = ?",
                (view_id,),
            ).fetchone()["status"]
            == "staged"
        )
        with pytest.raises(CatalogNotFoundError, match="ref not found"):
            catalog.resolve_ref(repository_id)


def test_cross_source_view_is_rejected_and_old_ref_is_unchanged(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id = catalog.create_repository("owner/repo")
        source_one = _source(catalog, repository_id, "a")
        source_two = _source(catalog, repository_id, "b")
        profile_id = catalog.create_view_profile("bm25", {})
        first_view = _stage(
            catalog,
            repository_id,
            source_one,
            profile_id,
            _object(catalog, "a"),
        )
        first = catalog.publish_snapshot(
            repository_id,
            source_one,
            [first_view],
            expected_generation=0,
        )
        other_source_view = _stage(
            catalog,
            repository_id,
            source_two,
            profile_id,
            _object(catalog, "b"),
        )

        with pytest.raises(CatalogValidationError, match="source identity"):
            catalog.publish_snapshot(
                repository_id,
                source_one,
                [other_source_view],
                expected_generation=1,
            )

        resolved = catalog.resolve_ref(repository_id)
        staged_status = catalog._connection.execute(
            """
            SELECT status FROM view_generations WHERE view_generation_id = ?
            """,
            (other_source_view,),
        ).fetchone()["status"]
        assert resolved["snapshot_id"] == first["snapshot_id"]
        assert resolved["generation"] == 1
        assert staged_status == "staged"


def test_ready_generation_cannot_be_mutated_or_deleted(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id = catalog.create_repository("owner/repo")
        source_revision_id = _source(catalog, repository_id)
        profile_id = catalog.create_view_profile("bm25", {})
        view_id = _stage(
            catalog,
            repository_id,
            source_revision_id,
            profile_id,
            _object(catalog, "a"),
        )
        catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [view_id],
            expected_generation=0,
        )

        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            catalog._connection.execute(
                """
                UPDATE view_generations SET metadata_json = '{}'
                WHERE view_generation_id = ?
                """,
                (view_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            catalog._connection.execute(
                "DELETE FROM view_generations WHERE view_generation_id = ?",
                (view_id,),
            )
