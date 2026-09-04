# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from scripts.experimental.hybrid_index.catalog import SQLiteCatalog
from scripts.experimental.hybrid_index.contracts import (
    Generation,
    PublishConflict,
    Snapshot,
    StorageIntegrityError,
)

_TABLES = (
    "generations",
    "refs",
    "snapshot_generations",
    "snapshots",
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _generation(
    seed: str,
    *,
    repository: str = "owner/repository",
) -> Generation:
    payload = f"archive:{seed}".encode("utf-8")
    return Generation.create(
        repository=repository,
        commit=_digest(f"commit:{seed}")[:40],
        source_fingerprint=f"source-v2:{_digest(f'source:{seed}')}",
        metadata_digest=_digest(f"metadata:{seed}"),
        archive_digest=hashlib.sha256(payload).hexdigest(),
        archive_size=len(payload),
        file_count=3,
        byte_count=len(payload) + 128,
    )


def _snapshot(
    seed: str,
    *,
    repository: str = "owner/repository",
) -> Snapshot:
    return Snapshot.create(_generation(seed, repository=repository))


def _table_counts(path: Path) -> dict[str, int]:
    connection = sqlite3.connect(path)
    try:
        return {
            table: int(
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            )
            for table in _TABLES
        }
    finally:
        connection.close()


def test_catalog_creates_exact_four_table_cnix_wal_schema(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    catalog = SQLiteCatalog(path)

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        objects = tuple(tuple(row) for row in connection.execute("""
                SELECT type, name, tbl_name
                  FROM sqlite_schema
                 WHERE name NOT LIKE 'sqlite_%'
                 ORDER BY type, name
                """))
        columns = {
            table: tuple(
                str(row["name"])
                for row in connection.execute(f"PRAGMA table_info({table})")
            )
            for table in _TABLES
        }
        snapshot_foreign_keys = {
            (row["table"], row["from"], row["to"], row["on_delete"])
            for row in connection.execute(
                "PRAGMA foreign_key_list(snapshot_generations)"
            )
        }
        ref_foreign_keys = {
            (row["table"], row["from"], row["to"], row["on_delete"])
            for row in connection.execute("PRAGMA foreign_key_list(refs)")
        }

        assert int(connection.execute("PRAGMA application_id").fetchone()[0]) == (
            0x434E4958
        )
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 1
        assert str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() == (
            "wal"
        )
    finally:
        connection.close()

    assert catalog.path == path.resolve()
    assert catalog.journal_mode == "wal"
    assert objects == tuple(("table", table, table) for table in _TABLES)
    assert columns == {
        "generations": (
            "generation_id",
            "repository",
            "commit_sha",
            "source_fingerprint",
            "view_type",
            "metadata_digest",
            "archive_digest",
            "archive_size",
            "file_count",
            "byte_count",
            "created_at",
        ),
        "refs": (
            "repository",
            "ref_name",
            "snapshot_id",
            "revision",
            "updated_at",
        ),
        "snapshot_generations": (
            "snapshot_id",
            "view_type",
            "generation_id",
        ),
        "snapshots": (
            "snapshot_id",
            "repository",
            "commit_sha",
            "source_fingerprint",
            "created_at",
        ),
    }
    assert snapshot_foreign_keys == {
        ("generations", "generation_id", "generation_id", "RESTRICT"),
        ("generations", "view_type", "view_type", "RESTRICT"),
        ("snapshots", "snapshot_id", "snapshot_id", "RESTRICT"),
    }
    assert ref_foreign_keys == {
        ("snapshots", "repository", "repository", "RESTRICT"),
        ("snapshots", "snapshot_id", "snapshot_id", "RESTRICT"),
    }


def test_concurrent_first_open_initializes_one_catalog(tmp_path: Path) -> None:
    worker_count = 16

    def open_concurrently(path: Path) -> tuple[SQLiteCatalog, ...]:
        start = threading.Barrier(worker_count)

        def open_catalog() -> SQLiteCatalog:
            start.wait(timeout=10)
            return SQLiteCatalog(path, timeout=10)

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            return tuple(
                executor.map(lambda _index: open_catalog(), range(worker_count))
            )

    for iteration in range(25):
        path = tmp_path / f"catalog-{iteration}.sqlite3"
        catalogs = open_concurrently(path)
        assert {catalog.path for catalog in catalogs} == {path.resolve()}
        assert {catalog.journal_mode for catalog in catalogs} == {"wal"}
        assert _table_counts(path) == {table: 0 for table in _TABLES}


def test_unrelated_sqlite_database_fails_closed_without_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unrelated.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE unrelated(value TEXT NOT NULL)")
        connection.execute("INSERT INTO unrelated(value) VALUES ('keep')")
        connection.commit()
    finally:
        connection.close()
    before = path.read_bytes()

    with pytest.raises(StorageIntegrityError, match="not a CodeNib index"):
        SQLiteCatalog(path)

    assert path.read_bytes() == before
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT value FROM unrelated").fetchone() == ("keep",)
        assert connection.execute("PRAGMA application_id").fetchone() == (0,)
        assert connection.execute("PRAGMA user_version").fetchone() == (0,)
        assert str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() == (
            "delete"
        )
    finally:
        connection.close()


@pytest.mark.parametrize("tamper", ["application_id", "schema", "user_version"])
def test_live_catalog_rejects_identity_or_schema_tampering(
    tmp_path: Path,
    tamper: str,
) -> None:
    path = tmp_path / f"{tamper}.sqlite3"
    catalog = SQLiteCatalog(path)
    connection = sqlite3.connect(path)
    try:
        if tamper == "application_id":
            connection.execute("PRAGMA application_id = 305419896")
        elif tamper == "schema":
            connection.execute("CREATE TABLE injected(value TEXT)")
        else:
            connection.execute("PRAGMA user_version = 2")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(StorageIntegrityError):
        catalog.resolve_ref("owner/repository")


def test_live_catalog_rejects_database_path_replacement(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    stale_catalog = SQLiteCatalog(path)
    displaced = tmp_path / "displaced.sqlite3"
    os.replace(path, displaced)
    replacement_catalog = SQLiteCatalog(path)

    assert replacement_catalog.journal_mode == "wal"
    with pytest.raises(StorageIntegrityError, match="path identity changed"):
        stale_catalog.resolve_ref("owner/repository")


def test_publish_is_idempotent_and_keeps_historical_snapshots(
    tmp_path: Path,
) -> None:
    catalog = SQLiteCatalog(tmp_path / "catalog.sqlite3")
    first = _snapshot("first")
    second = _snapshot("second")

    first_head = catalog.publish_snapshot(first, expected_revision=0)
    retry_from_original_precondition = catalog.publish_snapshot(
        first,
        expected_revision=0,
    )
    retry_from_current_precondition = catalog.publish_snapshot(
        first,
        expected_revision=1,
    )
    second_head = catalog.publish_snapshot(second, expected_revision=1)

    assert first_head.revision == 1
    assert retry_from_original_precondition == first_head
    assert retry_from_current_precondition == first_head
    assert second_head.revision == 2
    assert catalog.resolve_ref(first.repository).snapshot == second
    assert catalog.resolve_ref(first.repository).ref == second_head
    assert catalog.get_snapshot(first.snapshot_id).snapshot == first
    assert catalog.get_snapshot(second.snapshot_id).snapshot == second
    assert _table_counts(catalog.path) == {
        "generations": 2,
        "refs": 1,
        "snapshot_generations": 2,
        "snapshots": 2,
    }


def test_concurrent_compare_and_swap_advances_ref_once(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    catalog = SQLiteCatalog(path)
    initial = _snapshot("initial")
    candidates = (_snapshot("candidate-a"), _snapshot("candidate-b"))
    catalog.publish_snapshot(initial, expected_revision=0)

    first_connection = SQLiteCatalog(path, timeout=10)
    second_connection = SQLiteCatalog(path, timeout=10)
    start = threading.Barrier(2)

    def publish(candidate: Snapshot, publisher: SQLiteCatalog) -> object:
        start.wait(timeout=10)
        try:
            return publisher.publish_snapshot(candidate, expected_revision=1)
        except PublishConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(
                lambda item: publish(*item),
                zip(
                    candidates,
                    (first_connection, second_connection),
                    strict=True,
                ),
            )
        )

    winners = [result for result in results if not isinstance(result, PublishConflict)]
    conflicts = [result for result in results if isinstance(result, PublishConflict)]
    assert len(winners) == 1
    assert len(conflicts) == 1
    winner = winners[0]
    assert winner.revision == 2
    assert winner.snapshot_id in {candidate.snapshot_id for candidate in candidates}
    resolved = catalog.resolve_ref(initial.repository)
    assert resolved.ref == winner
    assert resolved.snapshot.snapshot_id == winner.snapshot_id
    assert catalog.get_snapshot(initial.snapshot_id).snapshot == initial
    assert _table_counts(path) == {
        "generations": 2,
        "refs": 1,
        "snapshot_generations": 2,
        "snapshots": 2,
    }


def test_reader_overlapping_publication_observes_complete_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    reader = SQLiteCatalog(path)
    publisher = SQLiteCatalog(path)
    initial = _snapshot("initial")
    candidate = _snapshot("candidate")
    initial_head = publisher.publish_snapshot(initial, expected_revision=0)
    before_ref = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def pause_before_ref_update() -> None:
        before_ref.set()
        if not release.wait(timeout=10):
            raise TimeoutError("reader did not release publisher")

    monkeypatch.setattr(publisher, "_before_ref_update", pause_before_ref_update)

    def publish() -> None:
        try:
            publisher.publish_snapshot(candidate, expected_revision=1)
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread = threading.Thread(target=publish)
    thread.start()
    assert before_ref.wait(timeout=10)
    overlapping = reader.resolve_ref(initial.repository)
    release.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert errors == []
    assert overlapping.ref == initial_head
    assert overlapping.snapshot == initial
    published = reader.resolve_ref(initial.repository)
    assert published.ref is not None
    assert published.ref.revision == 2
    assert published.snapshot == candidate


def test_failure_before_ref_update_rolls_back_all_catalog_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = SQLiteCatalog(tmp_path / "catalog.sqlite3")
    current = _snapshot("current")
    candidate = _snapshot("candidate")
    current_head = catalog.publish_snapshot(current, expected_revision=0)
    before = _table_counts(catalog.path)

    def fail_before_ref_update() -> None:
        raise RuntimeError("injected transaction failure")

    monkeypatch.setattr(catalog, "_before_ref_update", fail_before_ref_update)

    with pytest.raises(RuntimeError, match="injected transaction failure"):
        catalog.publish_snapshot(candidate, expected_revision=1)

    assert _table_counts(catalog.path) == before
    resolved = catalog.resolve_ref(current.repository)
    assert resolved.ref == current_head
    assert resolved.snapshot == current


def test_generation_identity_collision_is_rejected_without_ref_change(
    tmp_path: Path,
) -> None:
    catalog = SQLiteCatalog(tmp_path / "catalog.sqlite3")
    original = _generation("original")
    first = Snapshot.create(original)
    first_head = catalog.publish_snapshot(first, expected_revision=0)
    conflicting_archive = b"different immutable archive"
    conflicting = Generation(
        generation_id=original.generation_id,
        repository=original.repository,
        commit=original.commit,
        source_fingerprint=original.source_fingerprint,
        view_type=original.view_type,
        metadata_digest=original.metadata_digest,
        archive_digest=hashlib.sha256(conflicting_archive).hexdigest(),
        archive_size=len(conflicting_archive),
        file_count=original.file_count,
        byte_count=original.byte_count,
    )
    collision = Snapshot.create(conflicting)

    with pytest.raises(StorageIntegrityError, match="generation .* is not immutable"):
        catalog.publish_snapshot(collision, expected_revision=1)

    assert catalog.resolve_ref(original.repository).ref == first_head
    assert catalog.resolve_ref(original.repository).snapshot == first
    assert _table_counts(catalog.path) == {
        "generations": 1,
        "refs": 1,
        "snapshot_generations": 1,
        "snapshots": 1,
    }


def test_snapshot_identity_collision_rolls_back_new_generation_and_ref(
    tmp_path: Path,
) -> None:
    catalog = SQLiteCatalog(tmp_path / "catalog.sqlite3")
    candidate = _snapshot("candidate")
    connection = sqlite3.connect(catalog.path)
    try:
        connection.execute(
            """
            INSERT INTO snapshots(
                snapshot_id, repository, commit_sha,
                source_fingerprint, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                candidate.snapshot_id,
                "different/repository",
                candidate.commit,
                candidate.source_fingerprint,
                0.0,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(StorageIntegrityError, match="snapshot .* is not immutable"):
        catalog.publish_snapshot(candidate, expected_revision=0)

    assert _table_counts(catalog.path) == {
        "generations": 0,
        "refs": 0,
        "snapshot_generations": 0,
        "snapshots": 1,
    }
