# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""SQLite-specific integration tests for the Wiki store contract."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from codenib.wiki.sqlite_store import SQLiteWikiStore
from codenib.wiki.store import (
    WikiStoreCorruptionError,
    WikiStoreError,
    WikiStoreSchemaError,
)

from .test_store_contract import WikiStoreContract


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


def test_failed_publish_rolls_back_the_previous_entry(tmp_path: Path) -> None:
    path = tmp_path / "wiki.sqlite3"
    store = SQLiteWikiStore(path)
    original = store.publish(
        entry_id="page:repo-a:overview",
        repository_id="repo-a",
        envelope={"data": {"body": "original"}},
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_wiki_update
            BEFORE UPDATE ON wiki_entries
            BEGIN
                SELECT RAISE(ABORT, 'injected update failure');
            END
            """
        )

    with pytest.raises(WikiStoreError):
        store.publish(
            entry_id=original.entry_id,
            repository_id=original.repository_id,
            envelope={"data": {"body": "replacement"}},
        )

    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER reject_wiki_update")
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
