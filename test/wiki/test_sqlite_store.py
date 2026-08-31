# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""SQLite-specific integration tests for the Wiki store contract."""

from __future__ import annotations

import multiprocessing
import os
import queue
import sqlite3
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from codenib.wiki.sqlite_store import (
    _APPLICATION_ID,
    _SCHEMA_VERSION,
    SQLiteWikiStore,
    _record_digest,
    _sqlite_error,
)
from codenib.wiki.store import (
    WikiStoreCorruptionError,
    WikiStoreError,
    WikiStoreSchemaError,
)

from .test_store_contract import WikiStoreContract


def _probe_generation_guard_process(
    database_path: str,
    entry_id: str,
    result_queue,
    retry_after_release,
) -> None:
    from filelock import Timeout

    import codenib.wiki.sqlite_store as sqlite_store_module

    store = sqlite_store_module.SQLiteWikiStore(database_path)
    original_file_lock = sqlite_store_module.FileLock

    class NonBlockingFileLock(original_file_lock):
        def acquire(self, *args, **kwargs):
            kwargs["timeout"] = 0
            return super().acquire(*args, **kwargs)

    sqlite_store_module.FileLock = NonBlockingFileLock
    try:
        try:
            with store.generation_guard(entry_id):
                result_queue.put("entered-while-held")
        except sqlite_store_module.WikiStoreError as exc:
            if not isinstance(exc.__cause__, Timeout):
                raise
            result_queue.put("blocked")
    finally:
        sqlite_store_module.FileLock = original_file_lock

    if not retry_after_release.wait(timeout=10):
        raise RuntimeError("parent did not release the generation guard")
    with store.generation_guard(entry_id):
        result_queue.put("entered-after-release")


def _exercise_store_operation(store: SQLiteWikiStore, operation: str) -> object:
    if operation == "read":
        return store.read("page:repo-a:overview")
    if operation == "scan":
        return store.scan()
    if operation == "publish":
        return store.publish(
            entry_id="page:repo-a:overview",
            repository_id="repo-a",
            envelope={"data": {"body": "replacement"}},
        )
    raise AssertionError(f"unexpected test operation: {operation}")


class TestSQLiteWikiStoreContract(WikiStoreContract):
    @pytest.fixture
    def store(self, tmp_path: Path) -> SQLiteWikiStore:
        return SQLiteWikiStore(tmp_path / "wiki.sqlite3")


def test_reopen_preserves_entries_and_uses_wal(tmp_path: Path) -> None:
    path = tmp_path / "wiki.sqlite3"
    store = SQLiteWikiStore(path)
    expected = store.publish(
        entry_id="page:repo-a:overview",
        repository_id="repo-a",
        envelope={"data": {"body": "persisted"}},
    )

    reopened = SQLiteWikiStore(path)

    assert reopened.read(expected.entry_id) == expected
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA application_id").fetchone()[0] != 0
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_existing_only_uri_preserves_reserved_path_characters(tmp_path: Path) -> None:
    path = tmp_path / "wiki cache#1.sqlite3"
    store = SQLiteWikiStore(path)
    expected = store.publish(
        entry_id="page:repo-a:overview",
        repository_id="repo-a",
        envelope={"data": {"body": "persisted"}},
    )

    assert store.read(expected.entry_id) == expected


def test_new_database_and_wal_sidecars_are_private_under_common_umask(
    tmp_path: Path,
) -> None:
    path = tmp_path / "wiki.sqlite3"
    previous_umask = os.umask(0o022)
    try:
        store = SQLiteWikiStore(path)
        entry = store.publish(
            entry_id="page:repo-a:overview",
            repository_id="repo-a",
            envelope={"data": {"body": "private"}},
        )

        with sqlite3.connect(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE wiki_entries SET repository_id = ? WHERE entry_id = ?",
                ("repo-a", entry.entry_id),
            )
            private_files = (
                path,
                Path(f"{path}-wal"),
                Path(f"{path}-shm"),
            )
            assert all(candidate.exists() for candidate in private_files)
            assert all(
                stat.S_IMODE(candidate.stat().st_mode) == 0o600
                for candidate in private_files
            )
            assert stat.S_IMODE(Path(f"{path}.locks").stat().st_mode) == 0o700
            connection.rollback()
    finally:
        os.umask(previous_umask)


def test_empty_sqlite_v0_database_is_initialized(tmp_path: Path) -> None:
    path = tmp_path / "wiki.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("VACUUM")
    assert path.stat().st_size > 0

    store = SQLiteWikiStore(path)

    assert store.scan() == ()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA application_id").fetchone()[0] != 0
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_concurrent_first_open_serializes_database_initialization(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "wiki.sqlite3"
    original_initialize = SQLiteWikiStore._initialize
    call_guard = threading.Lock()
    first_entered = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    second_entered = threading.Event()
    call_count = 0

    def observed_initialize(store: SQLiteWikiStore) -> None:
        nonlocal call_count
        with call_guard:
            call_count += 1
            ordinal = call_count
        if ordinal == 1:
            first_entered.set()
            assert release_first.wait(timeout=5)
        else:
            second_entered.set()
        original_initialize(store)

    def open_second() -> SQLiteWikiStore:
        second_started.set()
        return SQLiteWikiStore(path)

    monkeypatch.setattr(SQLiteWikiStore, "_initialize", observed_initialize)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_result = executor.submit(SQLiteWikiStore, path)
        try:
            assert first_entered.wait(timeout=5)
            second_result = executor.submit(open_second)
            assert second_started.wait(timeout=5)
            assert not second_entered.wait(timeout=0.2)
        finally:
            release_first.set()
        first_store = first_result.result(timeout=5)
        second_store = second_result.result(timeout=5)

    assert second_entered.is_set()
    assert first_store.path == second_store.path == path
    assert second_store.scan() == ()


def test_future_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "wiki.sqlite3"
    SQLiteWikiStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 2")

    with pytest.raises(WikiStoreSchemaError, match="newer"):
        SQLiteWikiStore(path)


def test_wrong_application_id_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "wiki.sqlite3"
    SQLiteWikiStore(path)
    with sqlite3.connect(path) as connection:
        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
        connection.execute(f"PRAGMA application_id = {application_id + 1}")

    with pytest.raises(WikiStoreSchemaError, match="not an initialized"):
        SQLiteWikiStore(path)


def test_unidentified_foreign_database_fails_before_wal_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "foreign.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")

    with pytest.raises(WikiStoreSchemaError, match="not an initialized"):
        SQLiteWikiStore(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA application_id").fetchone()[0] == 0
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchone()[0]
            == "unrelated"
        )


def test_unidentified_view_only_database_is_not_treated_as_empty(
    tmp_path: Path,
) -> None:
    path = tmp_path / "foreign.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE VIEW unrelated AS SELECT 1 AS value")

    with pytest.raises(WikiStoreSchemaError, match="not an initialized"):
        SQLiteWikiStore(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA application_id").fetchone()[0] == 0
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert (
            connection.execute(
                "SELECT type FROM sqlite_master WHERE name = 'unrelated'"
            ).fetchone()[0]
            == "view"
        )
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'wiki_entries'"
            ).fetchone()
            is None
        )


@pytest.mark.parametrize(
    "schema_sql",
    (
        "CREATE VIEW unexpected_view AS SELECT 1 AS value",
        "CREATE INDEX unexpected_index ON wiki_entries(entry_id)",
        """
        CREATE TRIGGER unexpected_trigger
        AFTER INSERT ON wiki_entries
        BEGIN
            DELETE FROM wiki_entries WHERE entry_id = NEW.entry_id;
        END
        """,
    ),
)
def test_identified_database_rejects_unexpected_schema_objects(
    tmp_path: Path,
    schema_sql: str,
) -> None:
    path = tmp_path / "wiki.sqlite3"
    live_store = SQLiteWikiStore(path)
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == "delete"
        )
        connection.execute(schema_sql)

    with pytest.raises(WikiStoreSchemaError, match="schema object"):
        SQLiteWikiStore(path)
    with pytest.raises(WikiStoreSchemaError, match="schema object"):
        live_store.scan()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_identified_database_rejects_misbound_expected_index(tmp_path: Path) -> None:
    path = tmp_path / "wiki.sqlite3"
    SQLiteWikiStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX wiki_entries_repository_entry_idx")
        connection.execute(
            "CREATE INDEX wiki_entries_repository_entry_idx "
            "ON wiki_entries(entry_id)"
        )

    with pytest.raises(WikiStoreSchemaError, match="schema object"):
        SQLiteWikiStore(path)


def test_identified_database_rejects_primary_key_collation_change(
    tmp_path: Path,
) -> None:
    path = tmp_path / "wiki.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            f"""
            CREATE TABLE wiki_entries (
                entry_id TEXT COLLATE NOCASE PRIMARY KEY NOT NULL,
                repository_id TEXT NOT NULL,
                envelope_json BLOB NOT NULL,
                record_sha256 TEXT NOT NULL CHECK (length(record_sha256) = 64)
            );
            CREATE INDEX wiki_entries_repository_entry_idx
                ON wiki_entries(repository_id, entry_id);
            PRAGMA application_id = {_APPLICATION_ID};
            PRAGMA user_version = {_SCHEMA_VERSION};
            """
        )

    with pytest.raises(WikiStoreSchemaError, match="schema object"):
        SQLiteWikiStore(path)


@pytest.mark.parametrize("operation", ("read", "publish", "scan"))
def test_operations_do_not_recreate_a_removed_database(
    tmp_path: Path,
    operation: str,
) -> None:
    path = tmp_path / "wiki.sqlite3"
    store = SQLiteWikiStore(path)
    path.unlink()

    with pytest.raises(WikiStoreError, match="operation"):
        _exercise_store_operation(store, operation)

    assert not path.exists()


@pytest.mark.parametrize("operation", ("read", "publish", "scan"))
def test_operations_reject_a_foreign_replacement_database(
    tmp_path: Path,
    operation: str,
) -> None:
    path = tmp_path / "wiki.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    store = SQLiteWikiStore(path)
    with sqlite3.connect(replacement) as connection:
        connection.executescript(
            """
            CREATE TABLE wiki_entries (
                entry_id TEXT PRIMARY KEY NOT NULL,
                repository_id TEXT NOT NULL,
                envelope_json BLOB NOT NULL,
                record_sha256 TEXT NOT NULL CHECK (length(record_sha256) = 64)
            );
            CREATE INDEX wiki_entries_repository_entry_idx
                ON wiki_entries(repository_id, entry_id);
            """
        )
    os.replace(replacement, path)

    with pytest.raises(WikiStoreSchemaError, match="not an initialized"):
        _exercise_store_operation(store, operation)

    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM wiki_entries").fetchone()[0] == 0
        )


def test_error_mapping_supports_python_without_sqlite_result_constants(
    tmp_path: Path,
    monkeypatch,
) -> None:
    for name in ("SQLITE_CORRUPT", "SQLITE_NOTADB", "SQLITE_SCHEMA"):
        monkeypatch.delattr(sqlite3, name, raising=False)
    database_directory = tmp_path / "wiki.sqlite3"
    database_directory.mkdir()

    with pytest.raises(WikiStoreError, match="initialization"):
        SQLiteWikiStore(database_directory)


@pytest.mark.parametrize(
    "message",
    (
        "file is not a database",
        "database disk image is malformed",
        "malformed database schema (broken) - incomplete input",
    ),
)
def test_error_mapping_without_result_attributes_identifies_corruption(
    message: str,
) -> None:
    error = sqlite3.DatabaseError(message)
    assert not hasattr(error, "sqlite_errorcode")

    mapped = _sqlite_error("read", error)

    assert isinstance(mapped, WikiStoreCorruptionError)


def test_payload_digest_corruption_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "wiki.sqlite3"
    store = SQLiteWikiStore(path)
    store.publish(
        entry_id="outline:repo-a",
        repository_id="repo-a",
        envelope={"data": {"pages": []}},
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE wiki_entries SET envelope_json = ? WHERE entry_id = ?",
            (b"{}", "outline:repo-a"),
        )

    with pytest.raises(WikiStoreCorruptionError, match="SHA-256"):
        store.read("outline:repo-a")


@pytest.mark.parametrize(
    ("column", "replacement", "lookup"),
    (
        ("entry_id", "page:repo-b:overview", "page:repo-b:overview"),
        ("repository_id", "repo-b", "page:repo-a:overview"),
    ),
)
def test_identity_metadata_corruption_is_reported(
    tmp_path: Path,
    column: str,
    replacement: str,
    lookup: str,
) -> None:
    path = tmp_path / "wiki.sqlite3"
    store = SQLiteWikiStore(path)
    store.publish(
        entry_id="page:repo-a:overview",
        repository_id="repo-a",
        envelope={"data": {"body": "persisted"}},
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"UPDATE wiki_entries SET {column} = ? WHERE entry_id = ?",
            (replacement, "page:repo-a:overview"),
        )

    with pytest.raises(WikiStoreCorruptionError, match="SHA-256"):
        store.read(lookup)
    if column == "repository_id":
        with pytest.raises(WikiStoreCorruptionError, match="SHA-256"):
            store.publish(
                entry_id="page:repo-a:overview",
                repository_id="repo-a",
                envelope={"data": {"body": "replacement"}},
            )


def test_overflowed_json_number_is_reported_as_corruption(tmp_path: Path) -> None:
    path = tmp_path / "wiki.sqlite3"
    store = SQLiteWikiStore(path)
    payload = b'{"data":{"score":1e9999}}'
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO wiki_entries(
                entry_id,
                repository_id,
                envelope_json,
                record_sha256
            ) VALUES (?, ?, ?, ?)
            """,
            (
                "page:repo-a:overview",
                "repo-a",
                payload,
                _record_digest("page:repo-a:overview", "repo-a", payload),
            ),
        )

    with pytest.raises(WikiStoreCorruptionError, match="invalid JSON"):
        store.read("page:repo-a:overview")


def test_failed_publish_rolls_back_the_previous_entry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "wiki.sqlite3"
    store = SQLiteWikiStore(path)
    original = store.publish(
        entry_id="page:repo-a:overview",
        repository_id="repo-a",
        envelope={"data": {"body": "original"}},
    )
    original_connection = store._connection

    @contextmanager
    def connection_with_rejected_update():
        with original_connection() as connection:
            connection.execute(
                """
                CREATE TEMP TRIGGER reject_wiki_update
                BEFORE UPDATE ON wiki_entries
                BEGIN
                    SELECT RAISE(ABORT, 'injected update failure');
                END
                """
            )
            yield connection

    monkeypatch.setattr(store, "_connection", connection_with_rejected_update)

    with pytest.raises(WikiStoreError):
        store.publish(
            entry_id=original.entry_id,
            repository_id=original.repository_id,
            envelope={"data": {"body": "replacement"}},
        )

    assert store.read(original.entry_id) == original


def test_concurrent_if_absent_publishers_observe_one_winner(tmp_path: Path) -> None:
    store = SQLiteWikiStore(tmp_path / "wiki.sqlite3")
    workers = 8
    start = threading.Barrier(workers)

    def publish(candidate: int) -> object:
        start.wait()
        return store.publish(
            entry_id="page:repo-a:overview",
            repository_id="repo-a",
            envelope={"data": {"candidate": candidate}},
            if_absent=True,
        ).envelope

    with ThreadPoolExecutor(max_workers=workers) as executor:
        observed = tuple(executor.map(publish, range(workers)))

    assert len({repr(envelope) for envelope in observed}) == 1
    persisted = store.read("page:repo-a:overview")
    assert persisted is not None
    assert all(envelope == persisted.envelope for envelope in observed)


def test_generation_guard_serializes_the_same_entry(tmp_path: Path) -> None:
    store = SQLiteWikiStore(tmp_path / "wiki.sqlite3")
    first_entered = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    second_entered = threading.Event()

    def first() -> None:
        with store.generation_guard("page:repo-a:overview"):
            first_entered.set()
            assert release_first.wait(timeout=5)

    def second() -> None:
        assert first_entered.wait(timeout=5)
        second_started.set()
        with store.generation_guard("page:repo-a:overview"):
            second_entered.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_result = executor.submit(first)
        second_result = executor.submit(second)
        assert first_entered.wait(timeout=5)
        assert second_started.wait(timeout=5)
        assert not second_entered.wait(timeout=0.2)
        release_first.set()
        first_result.result(timeout=5)
        second_result.result(timeout=5)

    assert second_entered.is_set()


def test_generation_guard_serializes_across_processes(tmp_path: Path) -> None:
    path = tmp_path / "wiki.sqlite3"
    store = SQLiteWikiStore(path)
    entry_id = "page:repo-a:overview"
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    retry_after_release = context.Event()
    process = context.Process(
        target=_probe_generation_guard_process,
        args=(str(path), entry_id, result_queue, retry_after_release),
    )

    try:
        with store.generation_guard(entry_id):
            process.start()
            assert result_queue.get(timeout=10) == "blocked"
            retry_after_release.set()
            with pytest.raises(queue.Empty):
                result_queue.get(timeout=0.2)

        assert result_queue.get(timeout=10) == "entered-after-release"
        process.join(timeout=10)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        result_queue.close()
