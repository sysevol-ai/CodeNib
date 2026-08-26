# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Schema-v6 SQLite execution-control tests."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator, Mapping

import pytest

from codenib.storage import sqlite_catalog as sqlite_catalog_module
from codenib.storage.models import (
    INDEX_JOB_REQUEST_CONTRACT,
    MAX_INDEX_JOB_EVENTS_PER_ATTEMPT,
    IndexJobCompletion,
    IndexJobEffectiveMode,
    IndexJobRunnableCursor,
    IndexJobRunnableCycle,
    IndexJobStatus,
    IndexJobViewOutcome,
    StorageIntegrityError,
    StorageValidationError,
)
from codenib.storage.sqlite_catalog import (
    LATEST_SCHEMA_VERSION,
    CatalogConflictError,
    CatalogError,
    CatalogNotFoundError,
    CatalogValidationError,
    SQLiteCatalog,
)


def _setup_repository(catalog: SQLiteCatalog) -> tuple[str, str, str]:
    repository_id = catalog.create_repository("owner/repo")
    source_revision_id = catalog.create_source_revision(
        repository_id,
        commit_sha="a" * 40,
        tree_sha="b" * 64,
    )
    profile_id = catalog.create_view_profile("bm25", {"tokenizer": "unicode61"})
    return repository_id, source_revision_id, profile_id


def _request(profile_id: str) -> dict[str, object]:
    return {
        "contract": INDEX_JOB_REQUEST_CONTRACT,
        "views": {
            "bm25": {
                "profile_id": profile_id,
                "requested_mode": "full",
                "required": True,
            }
        },
    }


def _create_job(
    catalog: SQLiteCatalog,
    *,
    idempotency_key: str = "request",
    ref_name: str = "main",
    max_attempts: int = 3,
):
    repository_id, source_revision_id, profile_id = _setup_repository(catalog)
    return catalog.create_job(
        repository_id,
        source_revision_id,
        idempotency_key,
        _request(profile_id),
        ref_name=ref_name,
        max_attempts=max_attempts,
    )


def _install_clock(catalog: SQLiteCatalog, clock: dict[str, int]) -> None:
    catalog._connection.create_function(
        "julianday",
        1,
        lambda _value: 2440587.5 + clock["ms"] / 86_400_000,
    )


def _install_stepping_clock(catalog: SQLiteCatalog, *samples_ms: int) -> None:
    assert samples_ms
    calls = 0

    def julianday(_value: str) -> float:
        nonlocal calls
        sample = samples_ms[min(calls, len(samples_ms) - 1)]
        calls += 1
        return 2440587.5 + sample / 86_400_000

    catalog._connection.create_function("julianday", 1, julianday)


def _patch_connection_clock(
    monkeypatch: pytest.MonkeyPatch,
    clock: dict[str, int],
) -> None:
    connect = sqlite_catalog_module.sqlite3.connect

    def connect_with_clock(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.create_function(
            "julianday",
            1,
            lambda _value: 2440587.5 + clock["ms"] / 86_400_000,
        )
        return connection

    monkeypatch.setattr(sqlite_catalog_module.sqlite3, "connect", connect_with_clock)


_EXECUTION_STATE_TABLES = (
    "index_jobs",
    "index_job_views",
    "index_job_attempt_baselines",
    "index_job_execution_clock",
    "index_job_cancellation_requests",
    "index_job_attempts",
    "index_job_attempt_completions",
    "index_job_events",
    "index_job_attempt_closure_frontiers",
    "ref_job_leases",
    "sqlite_sequence",
)


def _execution_state(
    catalog: SQLiteCatalog,
) -> dict[str, tuple[tuple[object, ...], ...]]:
    return {
        table: tuple(
            tuple(row)
            for row in catalog._connection.execute(
                f"SELECT * FROM {table} ORDER BY rowid"  # noqa: S608
            ).fetchall()
        )
        for table in _EXECUTION_STATE_TABLES
    }


def _remove_execution_clock_row(catalog: SQLiteCatalog) -> None:
    row = catalog._connection.execute(
        """
        SELECT sql FROM sqlite_master
        WHERE type = 'trigger'
            AND name = 'index_job_execution_clock_cannot_be_deleted'
        """
    ).fetchone()
    assert row is not None and type(row["sql"]) is str
    catalog._connection.execute(
        "DROP TRIGGER index_job_execution_clock_cannot_be_deleted"
    )
    catalog._connection.execute("DELETE FROM index_job_execution_clock")
    catalog._connection.execute(row["sql"])
    assert (
        catalog._connection.execute(
            "SELECT COUNT(*) FROM index_job_execution_clock"
        ).fetchone()[0]
        == 0
    )


def _drop_and_restore_triggers(
    connection: sqlite3.Connection,
    names: tuple[str, ...],
    mutation: str,
    parameters: tuple[object, ...],
) -> None:
    definitions = []
    for name in names:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            (name,),
        ).fetchone()
        assert row is not None and type(row[0]) is str
        definitions.append(row[0])
        connection.execute(f"DROP TRIGGER {name!r}")
    connection.execute(mutation, parameters)
    for definition in definitions:
        connection.execute(definition)


def _nested_payload_json(depth: int) -> str:
    value: dict[str, object] = {}
    for _ in range(depth):
        value = {"child": value}
    return json.dumps(value)


def test_v5_to_v6_backfills_only_the_current_active_attempt(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "catalog.sqlite3"
    monkeypatch.setattr(sqlite_catalog_module, "LATEST_SCHEMA_VERSION", 5)
    with SQLiteCatalog(path) as catalog:
        running = _create_job(catalog, idempotency_key="running")
        queued = catalog.create_job(
            running.repository_id,
            running.source_revision_id,
            "queued",
            running.request,
            ref_name="other",
        )
        now_ms = catalog._db_now_ms()
        catalog._connection.execute(
            """
            INSERT INTO ref_job_leases(
                repository_id, ref_name, job_id, owner_id, fencing_token,
                acquired_at_ms, heartbeat_at_ms, lease_expires_at_ms,
                updated_at_ms
            ) VALUES (?, ?, ?, 'legacy-worker', 1, ?, ?, ?, ?)
            """,
            (
                running.repository_id,
                running.ref_name,
                running.job_id,
                now_ms,
                now_ms,
                now_ms + 60_000,
                now_ms,
            ),
        )
        catalog._connection.execute(
            """
            UPDATE index_jobs
            SET status = 'running', attempt_count = 1,
                started_at_ms = ?, updated_at_ms = ?
            WHERE job_id = ?
            """,
            (now_ms, now_ms, running.job_id),
        )
        assert catalog.schema_version == 5

    monkeypatch.setattr(
        sqlite_catalog_module,
        "LATEST_SCHEMA_VERSION",
        LATEST_SCHEMA_VERSION,
    )
    with SQLiteCatalog(path, create=False) as catalog:
        assert catalog.schema_version == 6
        rows = catalog._connection.execute(
            """
            SELECT job_id, attempt_count, owner_id, fencing_token, started_at_ms
            FROM index_job_attempts
            """
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            (running.job_id, 1, "legacy-worker", 1, now_ms)
        ]
        assert catalog.get_job(queued.job_id).status is IndexJobStatus.QUEUED


def test_v5_first_active_attempt_start_migrates_exactly_and_reopens(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "legacy-first-active.sqlite3"
    clock = {"ms": 1_000}
    monkeypatch.setattr(sqlite_catalog_module, "LATEST_SCHEMA_VERSION", 5)
    with SQLiteCatalog(path) as catalog:
        _install_clock(catalog, clock)
        job = _create_job(catalog)
        clock["ms"] = 2_000
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="legacy-worker",
            lease_duration_ms=60_000,
        )

    monkeypatch.setattr(
        sqlite_catalog_module,
        "LATEST_SCHEMA_VERSION",
        LATEST_SCHEMA_VERSION,
    )
    with SQLiteCatalog(path, create=False) as catalog:
        baseline = catalog._connection.execute(
            """
            SELECT legacy_attempt_count, legacy_started_at_ms
            FROM index_job_attempt_baselines WHERE job_id = ?
            """,
            (job.job_id,),
        ).fetchone()
        attempt = catalog.get_job_attempt(job.job_id, 1)
        assert tuple(baseline) == (0, lease.acquired_at_ms)
        assert attempt.started_at_ms == lease.acquired_at_ms
        assert catalog.get_job(job.job_id).started_at_ms == lease.acquired_at_ms

    with SQLiteCatalog(path, create=False) as catalog:
        assert catalog.get_job_attempt(job.job_id, 1).started_at_ms == (
            lease.acquired_at_ms
        )


def test_v5_first_active_attempt_start_mismatch_fails_migration_atomically(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "legacy-first-active-mismatch.sqlite3"
    clock = {"ms": 1_000}
    monkeypatch.setattr(sqlite_catalog_module, "LATEST_SCHEMA_VERSION", 5)
    with SQLiteCatalog(path) as catalog:
        _install_clock(catalog, clock)
        job = _create_job(catalog)
        clock["ms"] = 2_000
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="legacy-worker",
            lease_duration_ms=60_000,
        )
        forged_started_at_ms = (job.created_at_ms + lease.acquired_at_ms) // 2
        _drop_and_restore_triggers(
            catalog._connection,
            ("index_job_status_transitions_are_valid",),
            "UPDATE index_jobs SET started_at_ms = ? WHERE job_id = ?",
            (forged_started_at_ms, job.job_id),
        )

    monkeypatch.setattr(
        sqlite_catalog_module,
        "LATEST_SCHEMA_VERSION",
        LATEST_SCHEMA_VERSION,
    )
    with pytest.raises(
        CatalogConflictError,
        match="first index job attempt|start witness",
    ):
        SQLiteCatalog(path, create=False)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert (
            connection.execute(
                """
                SELECT COUNT(*) FROM sqlite_master
                WHERE name IN (
                    'index_job_attempt_baselines',
                    'index_job_execution_clock',
                    'index_job_attempts'
                )
                """
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


@pytest.mark.parametrize("status", ("failed", "cancelled"))
def test_legacy_terminal_finish_time_cannot_drift_within_content_high_water(
    tmp_path,
    monkeypatch,
    status: str,
) -> None:
    path = tmp_path / f"legacy-terminal-finish-{status}.sqlite3"
    clock = {"ms": 1_000}
    monkeypatch.setattr(sqlite_catalog_module, "LATEST_SCHEMA_VERSION", 5)
    with SQLiteCatalog(path) as catalog:
        _install_clock(catalog, clock)
        job = _create_job(catalog)
        clock["ms"] = 2_000
        catalog.acquire_job_lease(
            job.job_id,
            owner_id="legacy-worker",
            lease_duration_ms=60_000,
        )
        clock["ms"] = 3_000
        completed_at_ms = catalog._db_now_ms()
        catalog._connection.execute(
            """
            UPDATE index_jobs
            SET status = ?, cancel_requested = ?, error_code = 'legacy',
                finished_at_ms = ?, updated_at_ms = ?
            WHERE job_id = ?
            """,
            (
                status,
                int(status == "cancelled"),
                completed_at_ms,
                completed_at_ms,
                job.job_id,
            ),
        )
        catalog._connection.execute(
            """
            UPDATE ref_job_leases
            SET job_id = NULL, owner_id = NULL, acquired_at_ms = NULL,
                heartbeat_at_ms = NULL, lease_expires_at_ms = NULL,
                updated_at_ms = ?
            WHERE job_id = ?
            """,
            (completed_at_ms, job.job_id),
        )

    monkeypatch.setattr(
        sqlite_catalog_module,
        "LATEST_SCHEMA_VERSION",
        LATEST_SCHEMA_VERSION,
    )
    with SQLiteCatalog(path, create=False) as catalog:
        persisted = catalog.get_job(job.job_id)
        assert persisted.finished_at_ms == persisted.updated_at_ms
        _drop_and_restore_triggers(
            catalog._connection,
            (
                "terminal_index_jobs_are_immutable",
                "index_job_status_transitions_are_valid",
            ),
            "UPDATE index_jobs SET finished_at_ms = started_at_ms WHERE job_id = ?",
            (job.job_id,),
        )

    with pytest.raises(
        CatalogConflictError,
        match="legacy terminal job time conflicts",
    ):
        SQLiteCatalog(path, create=False)


def test_v5_to_v6_baselines_hide_legacy_history_but_keep_active_attempt(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    clock = {"ms": 1_000}
    monkeypatch.setattr(sqlite_catalog_module, "LATEST_SCHEMA_VERSION", 5)
    with SQLiteCatalog(path) as catalog:
        _install_clock(catalog, clock)
        never = _create_job(catalog, idempotency_key="never", ref_name="never")
        jobs = {
            name: catalog.create_job(
                never.repository_id,
                never.source_revision_id,
                name,
                never.request,
                ref_name=name,
                max_attempts=4,
            )
            for name in ("history", "active", "failed", "cancelled")
        }

        def close_legacy_attempt(name: str, status: str) -> None:
            job = jobs[name]
            clock["ms"] += 100
            catalog.acquire_job_lease(
                job.job_id,
                owner_id=f"legacy-{name}",
                lease_duration_ms=60_000,
            )
            clock["ms"] += 100
            completed_at_ms = catalog._db_now_ms()
            catalog._connection.execute(
                """
                UPDATE index_jobs
                SET status = ?, cancel_requested = ?, error_code = 'legacy',
                    finished_at_ms = CASE WHEN ? = 'queued' THEN NULL ELSE ? END,
                    updated_at_ms = ?
                WHERE job_id = ?
                """,
                (
                    status,
                    int(status == "cancelled"),
                    status,
                    completed_at_ms,
                    completed_at_ms,
                    job.job_id,
                ),
            )
            catalog._connection.execute(
                """
                UPDATE ref_job_leases
                SET job_id = NULL, owner_id = NULL, acquired_at_ms = NULL,
                    heartbeat_at_ms = NULL, lease_expires_at_ms = NULL,
                    updated_at_ms = ?
                WHERE job_id = ?
                """,
                (completed_at_ms, job.job_id),
            )

        for _ in range(2):
            close_legacy_attempt("history", "queued")
            close_legacy_attempt("active", "queued")
        clock["ms"] += 100
        active_lease = catalog.acquire_job_lease(
            jobs["active"].job_id,
            owner_id="legacy-active-current",
            lease_duration_ms=60_000,
        )
        close_legacy_attempt("failed", "failed")
        close_legacy_attempt("cancelled", "cancelled")
        legacy_jobs = {
            job_id: catalog.get_job(job_id)
            for job_id in (never.job_id, *(job.job_id for job in jobs.values()))
        }

    monkeypatch.setattr(
        sqlite_catalog_module,
        "LATEST_SCHEMA_VERSION",
        LATEST_SCHEMA_VERSION,
    )
    with SQLiteCatalog(path, create=False) as catalog:
        baseline_rows = {
            row["job_id"]: tuple(row)[1:]
            for row in catalog._connection.execute(
                """
                SELECT job_id, legacy_attempt_count, initial_created_at_ms,
                    legacy_content_high_water_ms, legacy_started_at_ms
                FROM index_job_attempt_baselines
                """
            ).fetchall()
        }
        expected_attempt_counts = {
            never.job_id: 0,
            jobs["history"].job_id: 2,
            jobs["active"].job_id: 2,
            jobs["failed"].job_id: 1,
            jobs["cancelled"].job_id: 1,
        }
        expected_baselines = {
            job_id: (
                expected_attempt_counts[job_id],
                legacy_job.created_at_ms,
                legacy_job.updated_at_ms,
                legacy_job.started_at_ms,
            )
            for job_id, legacy_job in legacy_jobs.items()
        }
        assert baseline_rows == expected_baselines
        assert catalog.list_job_attempts(jobs["history"].job_id) == ()
        assert catalog.list_job_attempts(jobs["failed"].job_id) == ()
        assert catalog.list_job_attempts(jobs["cancelled"].job_id) == ()
        active_attempts = catalog.list_job_attempts(jobs["active"].job_id)
        assert len(active_attempts) == 1
        assert active_attempts[0].attempt_count == 3
        assert active_attempts[0].fencing_token == active_lease.fencing_token
        expected_high_water_ms = max(
            value
            for baseline in expected_baselines.values()
            for value in baseline[1:]
            if value is not None
        )
        expected_high_water_ms = max(
            expected_high_water_ms,
            active_attempts[0].started_at_ms,
        )
        assert (
            catalog._connection.execute(
                "SELECT high_water_ms FROM index_job_execution_clock"
            ).fetchone()[0]
            == expected_high_water_ms
        )

    with SQLiteCatalog(path, create=False) as catalog:
        assert (
            {
                row["job_id"]: tuple(row)[1:]
                for row in catalog._connection.execute(
                    """
                SELECT job_id, legacy_attempt_count, initial_created_at_ms,
                    legacy_content_high_water_ms, legacy_started_at_ms
                FROM index_job_attempt_baselines
                """
                ).fetchall()
            }
            == expected_baselines
        )
        assert (
            catalog._connection.execute(
                "SELECT high_water_ms FROM index_job_execution_clock"
            ).fetchone()[0]
            == expected_high_water_ms
        )


@pytest.mark.parametrize("raw_status", ["queued", "failed", "cancelled"])
def test_v5_half_state_migration_materializes_exact_closure_and_reopens(
    tmp_path,
    monkeypatch,
    raw_status: str,
) -> None:
    path = tmp_path / f"legacy-{raw_status}.sqlite3"
    monkeypatch.setattr(sqlite_catalog_module, "LATEST_SCHEMA_VERSION", 5)
    with SQLiteCatalog(path) as catalog:
        job = _create_job(catalog)
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="legacy-worker",
            lease_duration_ms=60_000,
        )
        completed_at_ms = catalog._db_now_ms()
        if raw_status == "queued":
            catalog._connection.execute(
                """
                UPDATE index_jobs
                SET status = 'queued', error_code = 'legacy_half_state',
                    updated_at_ms = ?
                WHERE job_id = ?
                """,
                (completed_at_ms, job.job_id),
            )
        else:
            catalog._connection.execute(
                """
                UPDATE index_jobs
                SET status = ?, cancel_requested = ?,
                    error_code = 'legacy_half_state', finished_at_ms = ?,
                    updated_at_ms = ?
                WHERE job_id = ?
                """,
                (
                    raw_status,
                    int(raw_status == "cancelled"),
                    completed_at_ms,
                    completed_at_ms,
                    job.job_id,
                ),
            )

    monkeypatch.setattr(
        sqlite_catalog_module,
        "LATEST_SCHEMA_VERSION",
        LATEST_SCHEMA_VERSION,
    )
    with SQLiteCatalog(path, create=False) as catalog:
        baseline = catalog._connection.execute(
            """
            SELECT legacy_attempt_count FROM index_job_attempt_baselines
            WHERE job_id = ?
            """,
            (job.job_id,),
        ).fetchone()[0]
        closure = catalog._connection.execute(
            """
            SELECT owner_id, fencing_token, outcome, error_code, completed_at_ms
            FROM index_job_attempt_completions
            WHERE job_id = ? AND attempt_count = 1
            """,
            (job.job_id,),
        ).fetchone()
        assert baseline == 0
        assert tuple(closure) == (
            "legacy-worker",
            lease.fencing_token,
            "requeue" if raw_status == "queued" else raw_status,
            "legacy_half_state",
            completed_at_ms,
        )
        frontier = catalog._connection.execute(
            """
            SELECT owner_id, fencing_token, event_count, max_event_sequence,
                max_event_created_at_ms
            FROM index_job_attempt_closure_frontiers
            WHERE job_id = ? AND attempt_count = 1
            """,
            (job.job_id,),
        ).fetchone()
        assert tuple(frontier) == (
            "legacy-worker",
            lease.fencing_token,
            0,
            0,
            0,
        )
        assert (
            catalog._connection.execute(
                "SELECT job_id FROM ref_job_leases WHERE repository_id = ?",
                (job.repository_id,),
            ).fetchone()[0]
            is None
        )

    with SQLiteCatalog(path, create=False) as catalog:
        assert catalog.get_job(job.job_id).status.value == raw_status


def test_v5_running_cancel_after_later_heartbeat_migrates_without_invented_evidence(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "legacy-cancel-heartbeat.sqlite3"
    clock = {"ms": 1_000}
    monkeypatch.setattr(sqlite_catalog_module, "LATEST_SCHEMA_VERSION", 5)
    with SQLiteCatalog(path) as catalog:
        _install_clock(catalog, clock)
        job = _create_job(catalog)
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="legacy-worker",
            lease_duration_ms=10_000,
        )
        clock["ms"] = 2_000
        requested = catalog.request_job_cancel(job.job_id)
        assert requested.cancel_requested
        clock["ms"] = 3_000
        renewed = catalog.renew_job_lease(
            job.job_id,
            owner_id="legacy-worker",
            fencing_token=lease.fencing_token,
            lease_duration_ms=10_000,
        )
        assert renewed.heartbeat_at_ms > requested.updated_at_ms

    monkeypatch.setattr(
        sqlite_catalog_module,
        "LATEST_SCHEMA_VERSION",
        LATEST_SCHEMA_VERSION,
    )
    with SQLiteCatalog(path, create=False) as catalog:
        marker = catalog._connection.execute(
            """
            SELECT request_kind, attempt_count, owner_id, fencing_token,
                observed_heartbeat_at_ms
            FROM index_job_cancellation_requests WHERE job_id = ?
            """,
            (job.job_id,),
        ).fetchone()
        assert tuple(marker) == ("legacy_v5", None, None, None, None)
        assert catalog.get_job(job.job_id).status is IndexJobStatus.RUNNING
        assert catalog.get_job(job.job_id).cancel_requested
        assert catalog.list_job_attempts(job.job_id)[0].owner_id == "legacy-worker"

    with SQLiteCatalog(path, create=False) as catalog:
        assert catalog.get_job(job.job_id).cancel_requested


def test_v5_running_cancel_from_prior_attempt_fails_migration_atomically(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "legacy-prior-attempt-cancel.sqlite3"
    clock = {"ms": 1_000}
    monkeypatch.setattr(sqlite_catalog_module, "LATEST_SCHEMA_VERSION", 5)
    with SQLiteCatalog(path) as catalog:
        _install_clock(catalog, clock)
        job = _create_job(catalog)
        clock["ms"] = 2_000
        catalog.acquire_job_lease(
            job.job_id,
            owner_id="legacy-worker-1",
            lease_duration_ms=60_000,
        )
        clock["ms"] = 2_500
        prior_attempt_time = catalog._db_now_ms()
        catalog._connection.execute(
            """
            UPDATE index_jobs
            SET status = 'queued', error_code = 'legacy_retry',
                updated_at_ms = ? WHERE job_id = ?
            """,
            (prior_attempt_time, job.job_id),
        )
        catalog._connection.execute(
            """
            UPDATE ref_job_leases
            SET job_id = NULL, owner_id = NULL, acquired_at_ms = NULL,
                heartbeat_at_ms = NULL, lease_expires_at_ms = NULL,
                updated_at_ms = ? WHERE repository_id = ? AND ref_name = ?
            """,
            (prior_attempt_time, job.repository_id, job.ref_name),
        )
        clock["ms"] = 3_000
        current = catalog.acquire_job_lease(
            job.job_id,
            owner_id="legacy-worker-2",
            lease_duration_ms=60_000,
        )
        assert prior_attempt_time < current.acquired_at_ms
        catalog._connection.execute(
            """
            UPDATE index_jobs
            SET cancel_requested = 1, updated_at_ms = ? WHERE job_id = ?
            """,
            (prior_attempt_time, job.job_id),
        )

    monkeypatch.setattr(
        sqlite_catalog_module,
        "LATEST_SCHEMA_VERSION",
        LATEST_SCHEMA_VERSION,
    )
    with pytest.raises(
        CatalogConflictError,
        match="legacy cancellation predates its current attempt",
    ):
        SQLiteCatalog(path, create=False)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert (
            connection.execute(
                """
                SELECT COUNT(*) FROM sqlite_master
                WHERE name IN (
                    'index_job_attempt_baselines',
                    'index_job_execution_clock',
                    'index_job_cancellation_requests',
                    'index_job_attempts'
                )
                """
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_v5_retry_attempt_authority_migrates_and_reopens_exactly(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "legacy-retry-authority.sqlite3"
    clock = {"ms": 1_000}
    monkeypatch.setattr(sqlite_catalog_module, "LATEST_SCHEMA_VERSION", 5)
    with SQLiteCatalog(path) as catalog:
        _install_clock(catalog, clock)
        job = _create_job(catalog)
        clock["ms"] = 2_000
        first = catalog.acquire_job_lease(
            job.job_id,
            owner_id="legacy-worker-1",
            lease_duration_ms=60_000,
        )
        clock["ms"] = 2_500
        retry_at_ms = catalog._db_now_ms()
        catalog._connection.execute(
            """
            UPDATE index_jobs
            SET status = 'queued', error_code = 'legacy_retry',
                updated_at_ms = ? WHERE job_id = ?
            """,
            (retry_at_ms, job.job_id),
        )
        catalog._connection.execute(
            """
            UPDATE ref_job_leases
            SET job_id = NULL, owner_id = NULL, acquired_at_ms = NULL,
                heartbeat_at_ms = NULL, lease_expires_at_ms = NULL,
                updated_at_ms = ? WHERE repository_id = ? AND ref_name = ?
            """,
            (retry_at_ms, job.repository_id, job.ref_name),
        )
        clock["ms"] = 3_000
        second = catalog.acquire_job_lease(
            job.job_id,
            owner_id="legacy-worker-2",
            lease_duration_ms=60_000,
        )
        clock["ms"] = 4_000
        cancelled = catalog.request_job_cancel(job.job_id)
        assert first.fencing_token == 1
        assert second.fencing_token == 2
        assert cancelled.started_at_ms == first.acquired_at_ms
        assert cancelled.updated_at_ms >= second.acquired_at_ms

    monkeypatch.setattr(
        sqlite_catalog_module,
        "LATEST_SCHEMA_VERSION",
        LATEST_SCHEMA_VERSION,
    )
    with SQLiteCatalog(path, create=False) as catalog:
        baseline = catalog._connection.execute(
            """
            SELECT legacy_attempt_count, legacy_started_at_ms
            FROM index_job_attempt_baselines WHERE job_id = ?
            """,
            (job.job_id,),
        ).fetchone()
        modeled = catalog.get_job_attempt(job.job_id, 2)
        assert tuple(baseline) == (1, first.acquired_at_ms)
        assert modeled.started_at_ms == second.acquired_at_ms
        assert modeled.fencing_token == second.fencing_token

    with SQLiteCatalog(path, create=False) as catalog:
        assert catalog.get_job_attempt(job.job_id, 2).fencing_token == 2


@pytest.mark.parametrize(
    "corruption",
    ("start_after_current", "low_fence", "inflated_attempt_count"),
)
def test_v5_retry_attempt_corruption_fails_migration_atomically(
    tmp_path,
    monkeypatch,
    corruption: str,
) -> None:
    path = tmp_path / f"legacy-retry-{corruption}.sqlite3"
    clock = {"ms": 1_000}
    monkeypatch.setattr(sqlite_catalog_module, "LATEST_SCHEMA_VERSION", 5)
    with SQLiteCatalog(path) as catalog:
        _install_clock(catalog, clock)
        job = _create_job(catalog)
        clock["ms"] = 2_000
        catalog.acquire_job_lease(
            job.job_id,
            owner_id="legacy-worker-1",
            lease_duration_ms=60_000,
        )
        clock["ms"] = 2_500
        retry_at_ms = catalog._db_now_ms()
        catalog._connection.execute(
            """
            UPDATE index_jobs
            SET status = 'queued', error_code = 'legacy_retry',
                updated_at_ms = ? WHERE job_id = ?
            """,
            (retry_at_ms, job.job_id),
        )
        catalog._connection.execute(
            """
            UPDATE ref_job_leases
            SET job_id = NULL, owner_id = NULL, acquired_at_ms = NULL,
                heartbeat_at_ms = NULL, lease_expires_at_ms = NULL,
                updated_at_ms = ? WHERE repository_id = ? AND ref_name = ?
            """,
            (retry_at_ms, job.repository_id, job.ref_name),
        )
        clock["ms"] = 3_000
        current = catalog.acquire_job_lease(
            job.job_id,
            owner_id="legacy-worker-2",
            lease_duration_ms=60_000,
        )
        if corruption == "start_after_current":
            clock["ms"] = 4_000
            cancelled = catalog.request_job_cancel(job.job_id)
            forged_started_at_ms = (
                current.acquired_at_ms + cancelled.updated_at_ms
            ) // 2
            _drop_and_restore_triggers(
                catalog._connection,
                ("index_job_status_transitions_are_valid",),
                "UPDATE index_jobs SET started_at_ms = ? WHERE job_id = ?",
                (forged_started_at_ms, job.job_id),
            )
        elif corruption == "low_fence":
            _drop_and_restore_triggers(
                catalog._connection,
                ("ref_job_lease_updates_are_fenced",),
                "UPDATE ref_job_leases SET fencing_token = 1 WHERE job_id = ?",
                (job.job_id,),
            )
        else:
            _drop_and_restore_triggers(
                catalog._connection,
                ("index_job_status_transitions_are_valid",),
                "UPDATE index_jobs SET attempt_count = 3 WHERE job_id = ?",
                (job.job_id,),
            )

    monkeypatch.setattr(
        sqlite_catalog_module,
        "LATEST_SCHEMA_VERSION",
        LATEST_SCHEMA_VERSION,
    )
    with pytest.raises(CatalogConflictError):
        SQLiteCatalog(path, create=False)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert (
            connection.execute(
                """
                SELECT COUNT(*) FROM sqlite_master
                WHERE name IN (
                    'index_job_attempt_baselines',
                    'index_job_execution_clock',
                    'index_job_attempts'
                )
                """
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_v6_attempt_insert_rejects_fence_below_attempt_count(tmp_path) -> None:
    clock = {"ms": 1_000}
    with SQLiteCatalog(tmp_path / "low-attempt-fence.sqlite3") as catalog:
        _install_clock(catalog, clock)
        job = _create_job(catalog)
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker-1",
            lease_duration_ms=60_000,
        )
        clock["ms"] = 2_000
        catalog.complete_job_attempt(
            job.job_id,
            attempt_count=1,
            owner_id="worker-1",
            fencing_token=lease.fencing_token,
            outcome=IndexJobCompletion.REQUEUE,
            error_code="retry",
        )
        high_water_ms = catalog._connection.execute(
            "SELECT high_water_ms FROM index_job_execution_clock"
        ).fetchone()[0]
        _drop_and_restore_triggers(
            catalog._connection,
            ("ref_job_lease_updates_are_fenced",),
            """
            UPDATE ref_job_leases
            SET job_id = ?, owner_id = 'raw-worker', fencing_token = 1,
                acquired_at_ms = ?, heartbeat_at_ms = ?,
                lease_expires_at_ms = ?, updated_at_ms = ?
            WHERE repository_id = ? AND ref_name = ? AND job_id IS NULL
            """,
            (
                job.job_id,
                high_water_ms,
                high_water_ms,
                high_water_ms + 60_000,
                high_water_ms,
                job.repository_id,
                job.ref_name,
            ),
        )

        with pytest.raises(sqlite3.IntegrityError, match="attempt start is invalid"):
            catalog._connection.execute(
                """
                INSERT INTO index_job_attempts(
                    job_id, attempt_count, repository_id, ref_name,
                    request_digest, owner_id, fencing_token, started_at_ms
                ) VALUES (?, 2, ?, ?, ?, 'raw-worker', 1, ?)
                """,
                (
                    job.job_id,
                    job.repository_id,
                    job.ref_name,
                    job.request_digest,
                    high_water_ms,
                ),
            )
        assert [
            attempt.attempt_count for attempt in catalog.list_job_attempts(job.job_id)
        ] == [1]


@pytest.mark.parametrize("corruption", ["queued_cancel", "queued_exhausted", "failed0"])
def test_v5_stranded_lifecycle_states_fail_migration_atomically(
    tmp_path,
    monkeypatch,
    corruption: str,
) -> None:
    path = tmp_path / f"legacy-{corruption}.sqlite3"
    monkeypatch.setattr(sqlite_catalog_module, "LATEST_SCHEMA_VERSION", 5)
    with SQLiteCatalog(path) as catalog:
        job = _create_job(catalog)
        now_ms = catalog._db_now_ms()
        if corruption == "queued_cancel":
            catalog._connection.execute(
                """
                UPDATE index_jobs SET cancel_requested = 1, updated_at_ms = ?
                WHERE job_id = ?
                """,
                (now_ms, job.job_id),
            )
        elif corruption == "queued_exhausted":
            catalog._connection.execute(
                """
                UPDATE index_jobs SET attempt_count = max_attempts
                WHERE job_id = ?
                """,
                (job.job_id,),
            )
        else:
            _drop_and_restore_triggers(
                catalog._connection,
                ("index_job_status_transitions_are_valid",),
                """
                UPDATE index_jobs
                SET status = 'failed', error_code = 'raw', finished_at_ms = ?,
                    updated_at_ms = ? WHERE job_id = ?
                """,
                (now_ms, now_ms, job.job_id),
            )

    monkeypatch.setattr(
        sqlite_catalog_module,
        "LATEST_SCHEMA_VERSION",
        LATEST_SCHEMA_VERSION,
    )
    with pytest.raises(StorageValidationError):
        SQLiteCatalog(path, create=False)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert (
            connection.execute(
                """
                SELECT COUNT(*) FROM sqlite_master
                WHERE name IN (
                    'index_job_attempt_baselines',
                    'index_job_attempts',
                    'index_job_attempt_closure_frontiers'
                )
                """
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "corruption",
    (
        "queued_started",
        "queued_error",
        "cancelled_started",
        "cancelled_error_message",
    ),
)
def test_v5_zero_attempt_state_corruption_fails_migration_atomically(
    tmp_path,
    monkeypatch,
    corruption: str,
) -> None:
    path = tmp_path / f"legacy-zero-attempt-{corruption}.sqlite3"
    monkeypatch.setattr(sqlite_catalog_module, "LATEST_SCHEMA_VERSION", 5)
    with SQLiteCatalog(path) as catalog:
        job = _create_job(catalog)
        cancelled = corruption.startswith("cancelled_")
        if cancelled:
            job = catalog.request_job_cancel(job.job_id)
            assert job.status is IndexJobStatus.CANCELLED
        triggers = (
            (
                "terminal_index_jobs_are_immutable",
                "index_job_status_transitions_are_valid",
            )
            if cancelled
            else ("index_job_status_transitions_are_valid",)
        )
        if corruption.endswith("started"):
            mutation = """
                UPDATE index_jobs SET started_at_ms = created_at_ms
                WHERE job_id = ?
            """
        elif corruption == "queued_error":
            mutation = """
                UPDATE index_jobs SET error_code = 'raw_initial'
                WHERE job_id = ?
            """
        else:
            mutation = """
                UPDATE index_jobs SET error_message = 'raw_cancelled'
                WHERE job_id = ?
            """
        _drop_and_restore_triggers(
            catalog._connection,
            triggers,
            mutation,
            (job.job_id,),
        )

    monkeypatch.setattr(
        sqlite_catalog_module,
        "LATEST_SCHEMA_VERSION",
        LATEST_SCHEMA_VERSION,
    )
    with pytest.raises(
        (StorageValidationError, StorageIntegrityError, CatalogConflictError)
    ):
        SQLiteCatalog(path, create=False)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert (
            connection.execute(
                """
                SELECT COUNT(*) FROM sqlite_master
                WHERE name IN (
                    'index_job_attempt_baselines',
                    'index_job_execution_clock',
                    'index_job_attempts',
                    'index_job_attempt_closure_frontiers'
                )
                """
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


@pytest.mark.parametrize("status", ("queued", "failed", "cancelled"))
def test_v5_hidden_attempt_without_start_time_fails_migration_atomically(
    tmp_path,
    monkeypatch,
    status: str,
) -> None:
    path = tmp_path / f"legacy-hidden-start-{status}.sqlite3"
    monkeypatch.setattr(sqlite_catalog_module, "LATEST_SCHEMA_VERSION", 5)
    with SQLiteCatalog(path) as catalog:
        job = _create_job(catalog)
        catalog.acquire_job_lease(
            job.job_id,
            owner_id="legacy-worker",
            lease_duration_ms=60_000,
        )
        completed_at_ms = catalog._db_now_ms()
        catalog._connection.execute(
            """
            UPDATE index_jobs
            SET status = ?, cancel_requested = ?, error_code = 'legacy',
                finished_at_ms = CASE WHEN ? = 'queued' THEN NULL ELSE ? END,
                updated_at_ms = ?
            WHERE job_id = ?
            """,
            (
                status,
                int(status == "cancelled"),
                status,
                completed_at_ms,
                completed_at_ms,
                job.job_id,
            ),
        )
        catalog._connection.execute(
            """
            UPDATE ref_job_leases
            SET job_id = NULL, owner_id = NULL, acquired_at_ms = NULL,
                heartbeat_at_ms = NULL, lease_expires_at_ms = NULL,
                updated_at_ms = ?
            WHERE job_id = ?
            """,
            (completed_at_ms, job.job_id),
        )
        triggers = (
            ("index_job_status_transitions_are_valid",)
            if status == "queued"
            else (
                "terminal_index_jobs_are_immutable",
                "index_job_status_transitions_are_valid",
            )
        )
        _drop_and_restore_triggers(
            catalog._connection,
            triggers,
            "UPDATE index_jobs SET started_at_ms = NULL WHERE job_id = ?",
            (job.job_id,),
        )

    monkeypatch.setattr(
        sqlite_catalog_module,
        "LATEST_SCHEMA_VERSION",
        LATEST_SCHEMA_VERSION,
    )
    with pytest.raises(
        (StorageValidationError, StorageIntegrityError, CatalogConflictError)
    ):
        SQLiteCatalog(path, create=False)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert (
            connection.execute(
                """
                SELECT COUNT(*) FROM sqlite_master
                WHERE name IN (
                    'index_job_attempt_baselines',
                    'index_job_execution_clock',
                    'index_job_attempts'
                )
                """
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


@pytest.mark.parametrize("corruption", ("started", "error"))
def test_v6_bypassed_initial_state_corruption_fails_reopen(
    tmp_path,
    corruption: str,
) -> None:
    path = tmp_path / f"v6-initial-{corruption}.sqlite3"
    with SQLiteCatalog(path) as catalog:
        job = _create_job(catalog)
        mutation = (
            "UPDATE index_jobs SET started_at_ms = created_at_ms WHERE job_id = ?"
            if corruption == "started"
            else "UPDATE index_jobs SET error_code = 'raw' WHERE job_id = ?"
        )
        _drop_and_restore_triggers(
            catalog._connection,
            ("index_job_status_transitions_are_valid",),
            mutation,
            (job.job_id,),
        )
        with pytest.raises(
            CatalogConflictError,
            match="initial index job state|start time conflicts",
        ):
            catalog._validate_job_execution_aggregates()

    with pytest.raises(
        CatalogConflictError,
        match="initial index job state|start time conflicts",
    ):
        SQLiteCatalog(path, create=False)


def test_v5_failed_cancelled_active_half_state_fails_migration_atomically(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "legacy-failed-cancel-active.sqlite3"
    monkeypatch.setattr(sqlite_catalog_module, "LATEST_SCHEMA_VERSION", 5)
    with SQLiteCatalog(path) as catalog:
        job = _create_job(catalog)
        catalog.acquire_job_lease(
            job.job_id,
            owner_id="legacy-worker",
            lease_duration_ms=60_000,
        )
        requested = catalog.request_job_cancel(job.job_id)
        completed_at_ms = catalog._db_now_ms()
        catalog._connection.execute(
            """
            UPDATE index_jobs
            SET status = 'failed', error_code = 'legacy_failed',
                finished_at_ms = ?, updated_at_ms = ?
            WHERE job_id = ? AND cancel_requested = 1
            """,
            (completed_at_ms, completed_at_ms, requested.job_id),
        )

    monkeypatch.setattr(
        sqlite_catalog_module,
        "LATEST_SCHEMA_VERSION",
        LATEST_SCHEMA_VERSION,
    )
    with pytest.raises(CatalogConflictError):
        SQLiteCatalog(path, create=False)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert (
            connection.execute(
                """
                SELECT COUNT(*) FROM sqlite_master
                WHERE name = 'index_job_attempt_closure_frontiers'
                """
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_v5_inexact_active_lease_clock_fails_migration_atomically(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "legacy-inexact-lease-clock.sqlite3"
    clock = {"ms": 1_000}
    monkeypatch.setattr(sqlite_catalog_module, "LATEST_SCHEMA_VERSION", 5)
    with SQLiteCatalog(path) as catalog:
        _install_clock(catalog, clock)
        job = _create_job(catalog)
        catalog.acquire_job_lease(
            job.job_id,
            owner_id="legacy-worker",
            lease_duration_ms=60_000,
        )
        catalog._connection.execute(
            """
            UPDATE ref_job_leases
            SET updated_at_ms = 70000, lease_expires_at_ms = 80000
            WHERE job_id = ?
            """,
            (job.job_id,),
        )

    monkeypatch.setattr(
        sqlite_catalog_module,
        "LATEST_SCHEMA_VERSION",
        LATEST_SCHEMA_VERSION,
    )
    with pytest.raises(CatalogConflictError, match="lease slot"):
        SQLiteCatalog(path, create=False)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert (
            connection.execute(
                """
                SELECT COUNT(*) FROM sqlite_master
                WHERE name IN (
                    'index_job_attempt_baselines',
                    'index_job_attempts',
                    'index_job_attempt_closure_frontiers'
                )
                """
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_failed_v6_migration_rolls_back_all_execution_tables(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "catalog.sqlite3"
    monkeypatch.setattr(sqlite_catalog_module, "LATEST_SCHEMA_VERSION", 5)
    with SQLiteCatalog(path):
        pass

    broken = dict(sqlite_catalog_module._MIGRATIONS)
    broken[6] = (
        *sqlite_catalog_module._SCHEMA_V6[:5],
        "CREATE TABLE migration_v6_must_rollback(value TEXT)",
        "THIS IS NOT VALID SQL",
    )
    monkeypatch.setattr(sqlite_catalog_module, "_MIGRATIONS", broken)
    monkeypatch.setattr(sqlite_catalog_module, "LATEST_SCHEMA_VERSION", 6)
    with pytest.raises(sqlite3.OperationalError):
        SQLiteCatalog(path)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert (
            connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
            == 5
        )
        assert (
            connection.execute(
                """
                SELECT COUNT(*) FROM sqlite_master
                WHERE name IN (
                    'migration_v6_must_rollback',
                    'index_job_attempt_baselines',
                    'index_job_execution_clock',
                    'index_job_cancellation_requests',
                    'index_job_attempts'
                )
                """
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "mutation",
    (
        "INSERT INTO index_job_execution_clock VALUES (1, 1000)",
        "INSERT OR REPLACE INTO index_job_execution_clock VALUES (1, 1000)",
        "DELETE FROM index_job_execution_clock",
        "UPDATE index_job_execution_clock SET singleton_id = 2",
        "UPDATE index_job_execution_clock SET high_water_ms = high_water_ms + 1",
    ),
)
def test_execution_clock_singleton_rejects_raw_mutation_with_recursive_off(
    tmp_path,
    mutation: str,
) -> None:
    clock = {"ms": 1_000}
    with SQLiteCatalog(tmp_path / "clock-guards.sqlite3") as catalog:
        _install_clock(catalog, clock)
        _create_job(catalog)
        before = tuple(
            catalog._connection.execute(
                "SELECT singleton_id, high_water_ms FROM index_job_execution_clock"
            ).fetchone()
        )
        catalog._connection.execute("PRAGMA recursive_triggers = OFF")
        with pytest.raises(sqlite3.IntegrityError):
            catalog._connection.execute(mutation)
        assert (
            tuple(
                catalog._connection.execute(
                    """
                    SELECT singleton_id, high_water_ms
                    FROM index_job_execution_clock
                    """
                ).fetchone()
            )
            == before
        )


@pytest.mark.parametrize(
    "corruption",
    ("missing", "multiple", "text", "one_less", "one_more"),
)
def test_execution_clock_corruption_fails_existing_only_reopen(
    tmp_path,
    corruption: str,
) -> None:
    path = tmp_path / f"clock-corruption-{corruption}.sqlite3"
    clock = {"ms": 1_000}
    with SQLiteCatalog(path) as catalog:
        _install_clock(catalog, clock)
        _create_job(catalog)
        high_water_ms = catalog._connection.execute(
            "SELECT high_water_ms FROM index_job_execution_clock"
        ).fetchone()[0]
        trigger_rows = catalog._connection.execute(
            """
            SELECT name, sql FROM sqlite_master
            WHERE type = 'trigger' AND tbl_name = 'index_job_execution_clock'
            ORDER BY name
            """
        ).fetchall()
        for row in trigger_rows:
            catalog._connection.execute(f"DROP TRIGGER {row['name']!r}")
        if corruption == "missing":
            catalog._connection.execute("DELETE FROM index_job_execution_clock")
        elif corruption == "multiple":
            catalog._connection.execute("DROP TABLE index_job_execution_clock")
            catalog._connection.execute(
                """
                CREATE TABLE index_job_execution_clock(
                    singleton_id INTEGER,
                    high_water_ms INTEGER
                )
                """
            )
            catalog._connection.executemany(
                "INSERT INTO index_job_execution_clock VALUES (?, ?)",
                ((1, high_water_ms), (2, high_water_ms)),
            )
        elif corruption == "text":
            catalog._connection.execute("PRAGMA ignore_check_constraints = ON")
            catalog._connection.execute(
                "UPDATE index_job_execution_clock SET high_water_ms = 'raw'"
            )
            catalog._connection.execute("PRAGMA ignore_check_constraints = OFF")
        else:
            delta = -1 if corruption == "one_less" else 1
            catalog._connection.execute(
                "UPDATE index_job_execution_clock SET high_water_ms = ?",
                (high_water_ms + delta,),
            )
        for row in trigger_rows:
            catalog._connection.execute(row["sql"])

    with pytest.raises((CatalogError, CatalogConflictError, StorageIntegrityError)):
        SQLiteCatalog(path, create=False)


@pytest.mark.parametrize(
    "action",
    (
        "initial_job",
        "initial_lease",
        "released_claim",
        "expired_claim",
        "attempt",
        "status_start",
        "status_cancel",
        "cancellation_marker",
        "event",
        "completion",
    ),
)
def test_missing_execution_clock_fails_closed_in_content_triggers(
    tmp_path,
    action: str,
) -> None:
    clock = {"ms": 1_000}
    with SQLiteCatalog(tmp_path / f"missing-clock-{action}.sqlite3") as catalog:
        _install_clock(catalog, clock)
        job = _create_job(catalog)
        lease = None
        target = job

        if action in {"attempt", "status_start"}:
            high_water_ms = catalog._connection.execute(
                "SELECT high_water_ms FROM index_job_execution_clock"
            ).fetchone()[0]
            catalog._connection.execute(
                """
                INSERT INTO ref_job_leases(
                    repository_id, ref_name, job_id, owner_id, fencing_token,
                    acquired_at_ms, heartbeat_at_ms, lease_expires_at_ms,
                    updated_at_ms
                ) VALUES (?, ?, ?, 'raw-worker', 1, ?, ?, ?, ?)
                """,
                (
                    job.repository_id,
                    job.ref_name,
                    job.job_id,
                    high_water_ms,
                    high_water_ms,
                    high_water_ms + 60_000,
                    high_water_ms,
                ),
            )
            if action == "status_start":
                catalog._connection.execute(
                    """
                    INSERT INTO index_job_attempts(
                        job_id, attempt_count, repository_id, ref_name,
                        request_digest, owner_id, fencing_token, started_at_ms
                    ) VALUES (?, 1, ?, ?, ?, 'raw-worker', 1, ?)
                    """,
                    (
                        job.job_id,
                        job.repository_id,
                        job.ref_name,
                        job.request_digest,
                        high_water_ms,
                    ),
                )
        elif action in {
            "released_claim",
            "expired_claim",
            "cancellation_marker",
            "event",
            "completion",
        }:
            lease = catalog.acquire_job_lease(
                job.job_id,
                owner_id="worker-1",
                lease_duration_ms=100 if action == "expired_claim" else 60_000,
            )
            if action == "released_claim":
                clock["ms"] = 2_000
                catalog.complete_job_attempt(
                    job.job_id,
                    attempt_count=1,
                    owner_id="worker-1",
                    fencing_token=lease.fencing_token,
                    outcome=IndexJobCompletion.REQUEUE,
                    error_code="retry",
                )
            elif action == "expired_claim":
                clock["ms"] = 2_000
                target = catalog.create_job(
                    job.repository_id,
                    job.source_revision_id,
                    "takeover-target",
                    job.request,
                )

        high_water_ms = catalog._connection.execute(
            "SELECT high_water_ms FROM index_job_execution_clock"
        ).fetchone()[0]
        if action == "expired_claim":
            _drop_and_restore_triggers(
                catalog._connection,
                ("index_job_status_transitions_are_valid",),
                """
                UPDATE index_jobs
                SET status = 'queued', error_code = 'lease_expired',
                    error_message = NULL, finished_at_ms = NULL,
                    updated_at_ms = ?
                WHERE job_id = ?
                """,
                (high_water_ms, job.job_id),
            )
        if action == "cancellation_marker":
            assert lease is not None
            marker_trigger = catalog._connection.execute(
                """
                SELECT sql FROM sqlite_master
                WHERE type = 'trigger'
                    AND name = 'index_job_cancellation_is_recorded'
                """
            ).fetchone()
            assert marker_trigger is not None
            catalog._connection.execute(
                "DROP TRIGGER index_job_cancellation_is_recorded"
            )
            catalog._connection.execute(
                """
                UPDATE index_jobs
                SET cancel_requested = 1, updated_at_ms = ?
                WHERE job_id = ?
                """,
                (high_water_ms, job.job_id),
            )
            catalog._connection.execute(marker_trigger["sql"])

        _remove_execution_clock_row(catalog)
        before = _execution_state(catalog)
        catalog._connection.execute("PRAGMA recursive_triggers = OFF")
        expected_message = {
            "initial_job": "initial v6 index job state is invalid",
            "initial_lease": "initial ref job lease state is invalid",
            "released_claim": "ref job lease fencing transition is invalid",
            "expired_claim": "ref job lease fencing transition is invalid",
            "attempt": "index job attempt start is invalid",
            "status_start": "invalid index job status transition",
            "status_cancel": "invalid index job status transition",
            "cancellation_marker": "index job cancellation request is invalid",
            "event": "index job event is invalid",
            "completion": "index job attempt completion is invalid",
        }[action]
        with pytest.raises(sqlite3.IntegrityError, match=re.escape(expected_message)):
            if action == "initial_job":
                catalog._connection.execute(
                    """
                    INSERT INTO index_jobs(
                        job_id, repository_id, source_revision_id, ref_name,
                        idempotency_key, expected_ref_generation, max_attempts,
                        request_contract, request_json, request_digest, status,
                        cancel_requested, attempt_count, result_snapshot_id,
                        error_code, error_message, created_at_ms, updated_at_ms,
                        started_at_ms, finished_at_ms
                    )
                    SELECT ?, repository_id, source_revision_id, ref_name,
                        'raw-missing-clock', expected_ref_generation, max_attempts,
                        request_contract, request_json, request_digest, 'queued',
                        0, 0, NULL, NULL, NULL, ?, ?, NULL, NULL
                    FROM index_jobs WHERE job_id = ?
                    """,
                    ("job_" + "c" * 64, high_water_ms, high_water_ms, job.job_id),
                )
            elif action == "initial_lease":
                catalog._connection.execute(
                    """
                    INSERT INTO ref_job_leases(
                        repository_id, ref_name, job_id, owner_id, fencing_token,
                        acquired_at_ms, heartbeat_at_ms, lease_expires_at_ms,
                        updated_at_ms
                    ) VALUES (?, ?, ?, 'raw-worker', 1, ?, ?, ?, ?)
                    """,
                    (
                        job.repository_id,
                        job.ref_name,
                        job.job_id,
                        high_water_ms,
                        high_water_ms,
                        high_water_ms + 60_000,
                        high_water_ms,
                    ),
                )
            elif action in {"released_claim", "expired_claim"}:
                assert lease is not None
                catalog._connection.execute(
                    """
                    UPDATE ref_job_leases
                    SET job_id = ?, owner_id = 'worker-2',
                        fencing_token = fencing_token + 1,
                        acquired_at_ms = ?, heartbeat_at_ms = ?,
                        lease_expires_at_ms = ?, updated_at_ms = ?
                    WHERE repository_id = ? AND ref_name = ?
                    """,
                    (
                        target.job_id,
                        high_water_ms,
                        high_water_ms,
                        high_water_ms + 60_000,
                        high_water_ms,
                        job.repository_id,
                        job.ref_name,
                    ),
                )
            elif action == "attempt":
                catalog._connection.execute(
                    """
                    INSERT INTO index_job_attempts(
                        job_id, attempt_count, repository_id, ref_name,
                        request_digest, owner_id, fencing_token, started_at_ms
                    ) VALUES (?, 1, ?, ?, ?, 'raw-worker', 1, ?)
                    """,
                    (
                        job.job_id,
                        job.repository_id,
                        job.ref_name,
                        job.request_digest,
                        high_water_ms,
                    ),
                )
            elif action == "status_start":
                catalog._connection.execute(
                    """
                    UPDATE index_jobs
                    SET status = 'running', attempt_count = 1,
                        started_at_ms = ?, updated_at_ms = ?
                    WHERE job_id = ? AND status = 'queued'
                    """,
                    (high_water_ms, high_water_ms, job.job_id),
                )
            elif action == "status_cancel":
                catalog._connection.execute(
                    """
                    UPDATE index_jobs
                    SET status = 'cancelled', cancel_requested = 1,
                        error_code = 'cancelled', error_message = NULL,
                        finished_at_ms = ?, updated_at_ms = ?
                    WHERE job_id = ? AND status = 'queued'
                    """,
                    (high_water_ms, high_water_ms, job.job_id),
                )
            elif action == "cancellation_marker":
                assert lease is not None
                catalog._connection.execute(
                    """
                    INSERT INTO index_job_cancellation_requests(
                        job_id, requested_at_ms, request_kind, attempt_count,
                        owner_id, fencing_token, observed_heartbeat_at_ms
                    ) VALUES (?, ?, 'running_v6', 1, 'worker-1', ?, ?)
                    """,
                    (
                        job.job_id,
                        high_water_ms,
                        lease.fencing_token,
                        lease.heartbeat_at_ms,
                    ),
                )
            elif action == "event":
                assert lease is not None
                catalog._connection.execute(
                    """
                    INSERT INTO index_job_events(
                        job_id, attempt_count, event_key, kind, owner_id,
                        fencing_token, view_type, effective_mode, outcome,
                        payload_json, created_at_ms
                    ) VALUES (?, 1, 'missing-clock', 'progress', 'worker-1',
                        ?, NULL, NULL, NULL, '{}', ?)
                    """,
                    (job.job_id, lease.fencing_token, high_water_ms),
                )
            else:
                assert lease is not None
                catalog._connection.execute(
                    """
                    INSERT INTO index_job_attempt_completions(
                        job_id, attempt_count, owner_id, fencing_token, outcome,
                        error_code, error_message, completed_at_ms
                    ) VALUES (?, 1, 'worker-1', ?, 'failed',
                        'missing_clock', NULL, ?)
                    """,
                    (job.job_id, lease.fencing_token, high_water_ms),
                )
        assert _execution_state(catalog) == before


def test_v6_execution_triggers_reject_raw_half_states_and_cancel_erasure(
    tmp_path,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        job = _create_job(catalog)
        catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker",
            lease_duration_ms=60_000,
        )
        cancelled = catalog.request_job_cancel(job.job_id)
        assert cancelled.cancel_requested

    connection = sqlite3.connect(path, isolation_level=None)
    try:
        assert connection.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        now_ms = connection.execute(
            """
            SELECT CAST(
                (julianday('now') - 2440587.5) * 86400000 AS INTEGER
            )
            """
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="status transition"):
            connection.execute(
                """
                UPDATE index_jobs
                SET status = 'failed', error_code = 'raw_failure',
                    finished_at_ms = ?, updated_at_ms = ?
                WHERE job_id = ?
                """,
                (now_ms, now_ms, job.job_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot be cleared"):
            connection.execute(
                "UPDATE index_jobs SET cancel_requested = 0 WHERE job_id = ?",
                (job.job_id,),
            )
    finally:
        connection.close()

    with SQLiteCatalog(path, create=False) as catalog:
        assert catalog.get_job(job.job_id).cancel_requested


@pytest.mark.parametrize(
    ("created_delta", "updated_delta"),
    [(0, 1), (1, 1), (None, None)],
    ids=["advanced-update", "future-initial", "int64-max"],
)
def test_v6_initial_job_insert_is_canonical_and_rolls_back_its_baseline(
    tmp_path,
    created_delta: int | None,
    updated_delta: int | None,
) -> None:
    path = tmp_path / f"initial-{created_delta}-{updated_delta}.sqlite3"
    clock = {"ms": 1_000}
    with SQLiteCatalog(path) as catalog:
        _install_clock(catalog, clock)
        job = _create_job(catalog)
        now_ms = catalog._db_now_ms()
        created_at_ms = 2**63 - 1 if created_delta is None else now_ms + created_delta
        updated_at_ms = 2**63 - 1 if updated_delta is None else now_ms + updated_delta
        raw_job_id = "job_" + "f" * 64
        catalog._connection.execute("PRAGMA recursive_triggers = OFF")
        with pytest.raises(sqlite3.IntegrityError, match="initial v6 index job"):
            catalog._connection.execute(
                """
                INSERT INTO index_jobs(
                    job_id, repository_id, source_revision_id, ref_name,
                    idempotency_key, expected_ref_generation, max_attempts,
                    request_contract, request_json, request_digest, status,
                    cancel_requested, attempt_count, result_snapshot_id,
                    error_code, error_message, created_at_ms, updated_at_ms,
                    started_at_ms, finished_at_ms
                )
                SELECT ?, repository_id, source_revision_id, 'raw-ref',
                    'raw-initial', expected_ref_generation, max_attempts,
                    request_contract, request_json, request_digest, 'queued',
                    0, 0, NULL, NULL, NULL, ?, ?, NULL, NULL
                FROM index_jobs WHERE job_id = ?
                """,
                (raw_job_id, created_at_ms, updated_at_ms, job.job_id),
            )
        assert (
            catalog._connection.execute(
                "SELECT COUNT(*) FROM index_jobs WHERE job_id = ?",
                (raw_job_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            catalog._connection.execute(
                "SELECT COUNT(*) FROM index_job_attempt_baselines WHERE job_id = ?",
                (raw_job_id,),
            ).fetchone()[0]
            == 0
        )

        catalog._connection.execute("PRAGMA recursive_triggers = ON")
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker",
            lease_duration_ms=60_000,
        )
        assert lease.fencing_token == 1

    with SQLiteCatalog(path, create=False) as catalog:
        assert catalog.get_job(job.job_id).status is IndexJobStatus.RUNNING


def test_scan_hides_and_reopen_rejects_bypassed_future_initial_job(tmp_path) -> None:
    path = tmp_path / "bypassed-future-initial.sqlite3"
    with SQLiteCatalog(path) as catalog:
        job = _create_job(catalog)
        _drop_and_restore_triggers(
            catalog._connection,
            (
                "index_job_request_is_immutable",
                "index_job_status_transitions_are_valid",
            ),
            """
            UPDATE index_jobs
            SET created_at_ms = 9223372036854775807,
                updated_at_ms = 9223372036854775807
            WHERE job_id = ?
            """,
            (job.job_id,),
        )
        assert catalog.scan_runnable_jobs().jobs == ()

    with pytest.raises(
        CatalogConflictError,
        match="attempt baseline|initial index job time",
    ):
        SQLiteCatalog(path, create=False)


def test_v6_lease_creation_and_reactivation_require_exact_database_time(
    tmp_path,
) -> None:
    path = tmp_path / "initial-lease-causality.sqlite3"
    clock = {"ms": 1_000}
    with SQLiteCatalog(path) as catalog:
        _install_clock(catalog, clock)
        job = _create_job(catalog)
        now_ms = catalog._db_now_ms()
        catalog._connection.execute("PRAGMA recursive_triggers = OFF")
        with pytest.raises(sqlite3.IntegrityError, match="initial ref job lease"):
            catalog._connection.execute(
                """
                INSERT INTO ref_job_leases(
                    repository_id, ref_name, job_id, owner_id, fencing_token,
                    acquired_at_ms, heartbeat_at_ms, lease_expires_at_ms,
                    updated_at_ms
                ) VALUES (?, ?, NULL, NULL, 0, NULL, NULL, NULL, ?)
                """,
                (job.repository_id, job.ref_name, 2**63 - 1),
            )
        with pytest.raises(sqlite3.IntegrityError, match="initial ref job lease"):
            catalog._connection.execute(
                """
                INSERT INTO ref_job_leases(
                    repository_id, ref_name, job_id, owner_id, fencing_token,
                    acquired_at_ms, heartbeat_at_ms, lease_expires_at_ms,
                    updated_at_ms
                ) VALUES (?, ?, ?, 'raw-worker', 1, ?, 5000, 6000, 5000)
                """,
                (job.repository_id, job.ref_name, job.job_id, now_ms),
            )
        assert (
            catalog._connection.execute(
                "SELECT COUNT(*) FROM ref_job_leases"
            ).fetchone()[0]
            == 0
        )

        catalog._connection.execute("PRAGMA recursive_triggers = ON")
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker",
            lease_duration_ms=60_000,
        )
        clock["ms"] = 2_000
        catalog.complete_job_attempt(
            job.job_id,
            attempt_count=1,
            owner_id="worker",
            fencing_token=lease.fencing_token,
            outcome=IndexJobCompletion.REQUEUE,
            error_code="retry",
        )
        before = catalog._connection.execute("SELECT * FROM ref_job_leases").fetchone()
        clock["ms"] = 3_000
        with pytest.raises(sqlite3.IntegrityError, match="fencing transition"):
            catalog._connection.execute(
                """
                UPDATE ref_job_leases
                SET job_id = ?, owner_id = 'raw-worker', fencing_token = 2,
                    acquired_at_ms = 5000, heartbeat_at_ms = 5000,
                    lease_expires_at_ms = 6000, updated_at_ms = 5000
                WHERE repository_id = ? AND ref_name = ? AND job_id IS NULL
                """,
                (job.job_id, job.repository_id, job.ref_name),
            )
        after = catalog._connection.execute("SELECT * FROM ref_job_leases").fetchone()
        assert tuple(after) == tuple(before)

    with SQLiteCatalog(path, create=False) as catalog:
        reacquired = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker-2",
            lease_duration_ms=60_000,
        )
        assert reacquired.fencing_token == 2


def test_initial_lease_claim_cannot_predate_target_job_high_water(tmp_path) -> None:
    path = tmp_path / "initial-lease-target-floor.sqlite3"
    clock = {"ms": 1_000}
    with SQLiteCatalog(path) as catalog:
        _install_clock(catalog, clock)
        job = _create_job(catalog)
        _drop_and_restore_triggers(
            catalog._connection,
            ("index_job_status_transitions_are_valid",),
            "UPDATE index_jobs SET updated_at_ms = 5000 WHERE job_id = ?",
            (job.job_id,),
        )
        clock["ms"] = 3_000
        claim_at_ms = catalog._db_now_ms()
        catalog._connection.execute("PRAGMA recursive_triggers = OFF")
        with pytest.raises(sqlite3.IntegrityError, match="initial ref job lease"):
            catalog._connection.execute(
                """
                INSERT INTO ref_job_leases(
                    repository_id, ref_name, job_id, owner_id, fencing_token,
                    acquired_at_ms, heartbeat_at_ms, lease_expires_at_ms,
                    updated_at_ms
                ) VALUES (?, ?, ?, 'raw-worker', 1, ?, ?, ?, ?)
                """,
                (
                    job.repository_id,
                    job.ref_name,
                    job.job_id,
                    claim_at_ms,
                    claim_at_ms,
                    claim_at_ms + 1_000,
                    claim_at_ms,
                ),
            )
        assert (
            catalog._connection.execute(
                "SELECT COUNT(*) FROM ref_job_leases"
            ).fetchone()[0]
            == 0
        )
        assert catalog.list_job_attempts(job.job_id) == ()

        _drop_and_restore_triggers(
            catalog._connection,
            ("index_job_status_transitions_are_valid",),
            """
            UPDATE index_jobs SET updated_at_ms = created_at_ms WHERE job_id = ?
            """,
            (job.job_id,),
        )
        catalog._connection.execute("PRAGMA recursive_triggers = ON")
        assert (
            catalog.acquire_job_lease(
                job.job_id,
                owner_id="worker",
                lease_duration_ms=60_000,
            ).fencing_token
            == 1
        )

    with SQLiteCatalog(path, create=False) as catalog:
        assert catalog.get_job(job.job_id).status is IndexJobStatus.RUNNING


def test_released_slot_cannot_exceed_the_durable_content_clock(tmp_path) -> None:
    path = tmp_path / "released-slot-content-ceiling.sqlite3"
    clock = {"ms": 1_000}
    with SQLiteCatalog(path) as catalog:
        _install_clock(catalog, clock)
        job = _create_job(catalog)
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker-1",
            lease_duration_ms=60_000,
        )
        clock["ms"] = 2_000
        catalog.complete_job_attempt(
            job.job_id,
            attempt_count=1,
            owner_id="worker-1",
            fencing_token=lease.fencing_token,
            outcome=IndexJobCompletion.REQUEUE,
            error_code="retry",
        )
        high_water_ms = catalog._connection.execute(
            "SELECT high_water_ms FROM index_job_execution_clock"
        ).fetchone()[0]
        _drop_and_restore_triggers(
            catalog._connection,
            ("ref_job_lease_updates_are_fenced",),
            """
            UPDATE ref_job_leases SET updated_at_ms = ?
            WHERE repository_id = ? AND ref_name = ? AND job_id IS NULL
            """,
            (high_water_ms + 1, job.repository_id, job.ref_name),
        )
        before = _execution_state(catalog)
        with pytest.raises(
            CatalogConflictError,
            match="released job lease slot exceeds its durable content clock",
        ):
            catalog.acquire_job_lease(
                job.job_id,
                owner_id="worker-2",
                lease_duration_ms=60_000,
            )
        assert _execution_state(catalog) == before

    with pytest.raises(
        CatalogConflictError,
        match="released job lease slot exceeds its durable content clock",
    ):
        SQLiteCatalog(path, create=False)


def test_released_slot_claim_cannot_predate_target_job_high_water(tmp_path) -> None:
    path = tmp_path / "released-lease-target-floor.sqlite3"
    clock = {"ms": 1_000}
    with SQLiteCatalog(path) as catalog:
        _install_clock(catalog, clock)
        job = _create_job(catalog)
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker-1",
            lease_duration_ms=60_000,
        )
        clock["ms"] = 2_000
        completed = catalog.complete_job_attempt(
            job.job_id,
            attempt_count=1,
            owner_id="worker-1",
            fencing_token=lease.fencing_token,
            outcome=IndexJobCompletion.REQUEUE,
            error_code="retry",
        )
        _drop_and_restore_triggers(
            catalog._connection,
            ("index_job_status_transitions_are_valid",),
            "UPDATE index_jobs SET updated_at_ms = 5000 WHERE job_id = ?",
            (job.job_id,),
        )
        clock["ms"] = 3_000
        claim_at_ms = catalog._db_now_ms()
        before = tuple(
            catalog._connection.execute("SELECT * FROM ref_job_leases").fetchone()
        )
        catalog._connection.execute("PRAGMA recursive_triggers = OFF")
        with pytest.raises(sqlite3.IntegrityError, match="fencing transition"):
            catalog._connection.execute(
                """
                UPDATE ref_job_leases
                SET job_id = ?, owner_id = 'raw-worker', fencing_token = 2,
                    acquired_at_ms = ?, heartbeat_at_ms = ?,
                    lease_expires_at_ms = ?, updated_at_ms = ?
                WHERE repository_id = ? AND ref_name = ? AND job_id IS NULL
                """,
                (
                    job.job_id,
                    claim_at_ms,
                    claim_at_ms,
                    claim_at_ms + 1_000,
                    claim_at_ms,
                    job.repository_id,
                    job.ref_name,
                ),
            )
        assert (
            tuple(
                catalog._connection.execute("SELECT * FROM ref_job_leases").fetchone()
            )
            == before
        )
        assert len(catalog.list_job_attempts(job.job_id)) == 1

        _drop_and_restore_triggers(
            catalog._connection,
            ("index_job_status_transitions_are_valid",),
            "UPDATE index_jobs SET updated_at_ms = ? WHERE job_id = ?",
            (completed.updated_at_ms, job.job_id),
        )
        catalog._connection.execute("PRAGMA recursive_triggers = ON")
        clock["ms"] = 6_000
        assert (
            catalog.acquire_job_lease(
                job.job_id,
                owner_id="worker-2",
                lease_duration_ms=60_000,
            ).fencing_token
            == 2
        )

    with SQLiteCatalog(path, create=False) as catalog:
        assert len(catalog.list_job_attempts(job.job_id)) == 2


def test_expired_takeover_cannot_predate_target_job_high_water(tmp_path) -> None:
    path = tmp_path / "takeover-target-floor.sqlite3"
    clock = {"ms": 1_000}
    with SQLiteCatalog(path) as catalog:
        _install_clock(catalog, clock)
        first = _create_job(catalog, idempotency_key="first")
        second = catalog.create_job(
            first.repository_id,
            first.source_revision_id,
            "second",
            first.request,
        )
        first_lease = catalog.acquire_job_lease(
            first.job_id,
            owner_id="worker-1",
            lease_duration_ms=60_000,
        )
        completed = catalog.complete_job_attempt(
            first.job_id,
            attempt_count=1,
            owner_id="worker-1",
            fencing_token=first_lease.fencing_token,
            outcome=IndexJobCompletion.REQUEUE,
            error_code="retry",
        )

        clock["ms"] = 2_000
        intermediate = catalog.acquire_job_lease(
            first.job_id,
            owner_id="raw-intermediate",
            lease_duration_ms=100,
        )
        assert intermediate.fencing_token == 2
        _drop_and_restore_triggers(
            catalog._connection,
            ("index_job_status_transitions_are_valid",),
            "UPDATE index_jobs SET status = 'queued' WHERE job_id = ?",
            (first.job_id,),
        )
        _drop_and_restore_triggers(
            catalog._connection,
            ("index_job_status_transitions_are_valid",),
            "UPDATE index_jobs SET updated_at_ms = 5000 WHERE job_id = ?",
            (second.job_id,),
        )
        clock["ms"] = 3_000
        takeover_at_ms = catalog._db_now_ms()
        before = tuple(
            catalog._connection.execute("SELECT * FROM ref_job_leases").fetchone()
        )
        catalog._connection.execute("PRAGMA recursive_triggers = OFF")
        with pytest.raises(sqlite3.IntegrityError, match="fencing transition"):
            catalog._connection.execute(
                """
                UPDATE ref_job_leases
                SET job_id = ?, owner_id = 'raw-takeover', fencing_token = 3,
                    acquired_at_ms = ?, heartbeat_at_ms = ?,
                    lease_expires_at_ms = ?, updated_at_ms = ?
                WHERE repository_id = ? AND ref_name = ?
                    AND job_id = ? AND fencing_token = 2
                """,
                (
                    second.job_id,
                    takeover_at_ms,
                    takeover_at_ms,
                    takeover_at_ms + 1_000,
                    takeover_at_ms,
                    second.repository_id,
                    second.ref_name,
                    first.job_id,
                ),
            )
        assert (
            tuple(
                catalog._connection.execute("SELECT * FROM ref_job_leases").fetchone()
            )
            == before
        )
        assert catalog.list_job_attempts(second.job_id) == ()

        _drop_and_restore_triggers(
            catalog._connection,
            ("index_job_status_transitions_are_valid",),
            "UPDATE index_jobs SET updated_at_ms = created_at_ms WHERE job_id = ?",
            (second.job_id,),
        )
        _drop_and_restore_triggers(
            catalog._connection,
            ("index_job_status_transitions_are_valid",),
            "UPDATE index_jobs SET status = 'running' WHERE job_id = ?",
            (first.job_id,),
        )
        catalog._connection.execute("PRAGMA recursive_triggers = ON")
        clock["ms"] = 4_000
        assert (
            catalog.acquire_job_lease(
                second.job_id,
                owner_id="worker-2",
                lease_duration_ms=60_000,
            ).fencing_token
            == 3
        )
        assert completed.status is IndexJobStatus.QUEUED

    with SQLiteCatalog(path, create=False) as catalog:
        assert catalog.get_job(second.job_id).status is IndexJobStatus.RUNNING


def test_reopen_rejects_released_zero_fence_but_v5_release_migrates(
    tmp_path,
    monkeypatch,
) -> None:
    corrupt_path = tmp_path / "released-zero-fence.sqlite3"
    with SQLiteCatalog(corrupt_path) as catalog:
        job = _create_job(catalog)
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker",
            lease_duration_ms=60_000,
        )
        catalog.complete_job_attempt(
            job.job_id,
            attempt_count=1,
            owner_id="worker",
            fencing_token=lease.fencing_token,
            outcome=IndexJobCompletion.REQUEUE,
            error_code="retry",
        )
        _drop_and_restore_triggers(
            catalog._connection,
            ("ref_job_lease_updates_are_fenced",),
            "UPDATE ref_job_leases SET fencing_token = 0",
            (),
        )
    with pytest.raises(CatalogConflictError, match="impossible causal history"):
        SQLiteCatalog(corrupt_path, create=False)

    legacy_path = tmp_path / "v5-released.sqlite3"
    monkeypatch.setattr(sqlite_catalog_module, "LATEST_SCHEMA_VERSION", 5)
    with SQLiteCatalog(legacy_path) as catalog:
        legacy_job = _create_job(catalog)
        legacy_lease = catalog.acquire_job_lease(
            legacy_job.job_id,
            owner_id="legacy-worker",
            lease_duration_ms=60_000,
        )
        assert legacy_lease.fencing_token == 1
        completed_at_ms = catalog._db_now_ms()
        catalog._connection.execute(
            """
            UPDATE index_jobs
            SET status = 'queued', error_code = 'retry', updated_at_ms = ?
            WHERE job_id = ?
            """,
            (completed_at_ms, legacy_job.job_id),
        )
        catalog._connection.execute(
            """
            UPDATE ref_job_leases
            SET job_id = NULL, owner_id = NULL, acquired_at_ms = NULL,
                heartbeat_at_ms = NULL, lease_expires_at_ms = NULL,
                updated_at_ms = ?
            WHERE job_id = ?
            """,
            (completed_at_ms, legacy_job.job_id),
        )

    monkeypatch.setattr(
        sqlite_catalog_module,
        "LATEST_SCHEMA_VERSION",
        LATEST_SCHEMA_VERSION,
    )
    with SQLiteCatalog(legacy_path, create=False) as catalog:
        slot = catalog._connection.execute("SELECT * FROM ref_job_leases").fetchone()
        assert slot["job_id"] is None
        assert slot["fencing_token"] == 1
        assert (
            catalog.acquire_job_lease(
                legacy_job.job_id,
                owner_id="v6-worker",
                lease_duration_ms=60_000,
            ).fencing_token
            == 2
        )


def test_requeued_job_acquire_and_queued_cancel_reject_clock_rollback_cleanly(
    tmp_path,
) -> None:
    clock = {"ms": 1_000}
    with SQLiteCatalog(tmp_path / "queued-clock-rollback.sqlite3") as catalog:
        _install_clock(catalog, clock)
        job = _create_job(catalog)
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker-1",
            lease_duration_ms=60_000,
        )
        clock["ms"] = 3_000
        requeued = catalog.complete_job_attempt(
            job.job_id,
            attempt_count=1,
            owner_id="worker-1",
            fencing_token=lease.fencing_token,
            outcome=IndexJobCompletion.REQUEUE,
            error_code="retry",
        )
        assert requeued.status is IndexJobStatus.QUEUED
        expected_slot = tuple(
            catalog._connection.execute("SELECT * FROM ref_job_leases").fetchone()
        )
        clock["ms"] = 2_500

        with pytest.raises(
            CatalogConflictError,
            match="clock moved backwards|impossible causal history",
        ):
            catalog.acquire_job_lease(
                job.job_id,
                owner_id="worker-2",
                lease_duration_ms=60_000,
            )
        with pytest.raises(
            CatalogConflictError,
            match="clock moved backwards|impossible causal history",
        ):
            catalog.request_job_cancel(job.job_id)

        persisted = catalog.get_job(job.job_id)
        assert persisted.status is IndexJobStatus.QUEUED
        assert not persisted.cancel_requested
        assert len(catalog.list_job_attempts(job.job_id)) == 1
        assert len(catalog.list_job_attempt_completions(job.job_id)) == 1
        assert (
            tuple(
                catalog._connection.execute("SELECT * FROM ref_job_leases").fetchone()
            )
            == expected_slot
        )
        assert (
            catalog._connection.execute(
                "SELECT COUNT(*) FROM index_job_cancellation_requests"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "action",
    (
        "create",
        "claim_initial",
        "claim_released",
        "claim_expired",
        "cancel_queued",
        "cancel_running",
        "event",
        "complete_requeue",
        "complete_failed",
        "complete_cancelled",
    ),
)
def test_fresh_execution_mutations_freeze_one_clock_tick_or_roll_back_cleanly(
    tmp_path,
    action: str,
) -> None:
    clock = {"ms": 1_000}
    with SQLiteCatalog(tmp_path / f"moving-clock-{action}.sqlite3") as catalog:
        _install_clock(catalog, clock)
        job = None
        lease = None
        second = None
        repository_id = source_revision_id = profile_id = None
        if action == "create":
            repository_id, source_revision_id, profile_id = _setup_repository(catalog)
            catalog.create_job(
                repository_id,
                source_revision_id,
                "seed",
                _request(profile_id),
            )
        else:
            job = _create_job(catalog)
            if action in {
                "claim_released",
                "claim_expired",
                "cancel_running",
                "event",
                "complete_requeue",
                "complete_failed",
                "complete_cancelled",
            }:
                lease = catalog.acquire_job_lease(
                    job.job_id,
                    owner_id="worker-1",
                    lease_duration_ms=(100 if action == "claim_expired" else 60_000),
                )
            if action == "claim_released":
                assert lease is not None
                catalog.complete_job_attempt(
                    job.job_id,
                    attempt_count=1,
                    owner_id="worker-1",
                    fencing_token=lease.fencing_token,
                    outcome=IndexJobCompletion.REQUEUE,
                    error_code="retry",
                )
            elif action == "claim_expired":
                second = catalog.create_job(
                    job.repository_id,
                    job.source_revision_id,
                    "second",
                    job.request,
                )
            elif action == "complete_cancelled":
                catalog.request_job_cancel(job.job_id)

        before = _execution_state(catalog)
        _install_stepping_clock(catalog, 3_000, 500)
        with pytest.raises(CatalogConflictError):
            if action == "create":
                assert repository_id is not None
                assert source_revision_id is not None
                assert profile_id is not None
                catalog.create_job(
                    repository_id,
                    source_revision_id,
                    "moving-create",
                    _request(profile_id),
                )
            elif action.startswith("claim_"):
                target = second if second is not None else job
                assert target is not None
                catalog.acquire_job_lease(
                    target.job_id,
                    owner_id="worker-2",
                    lease_duration_ms=60_000,
                )
            elif action.startswith("cancel_"):
                assert job is not None
                catalog.request_job_cancel(job.job_id)
            elif action == "event":
                assert job is not None and lease is not None
                catalog.append_job_event(
                    job.job_id,
                    attempt_count=1,
                    owner_id="worker-1",
                    fencing_token=lease.fencing_token,
                    event_key="moving-event",
                )
            else:
                assert job is not None and lease is not None
                outcome = {
                    "complete_requeue": IndexJobCompletion.REQUEUE,
                    "complete_failed": IndexJobCompletion.FAILED,
                    "complete_cancelled": IndexJobCompletion.CANCELLED,
                }[action]
                catalog.complete_job_attempt(
                    job.job_id,
                    attempt_count=1,
                    owner_id="worker-1",
                    fencing_token=lease.fencing_token,
                    outcome=outcome,
                    error_code=None if outcome is IndexJobCompletion.CANCELLED else "x",
                )
        assert _execution_state(catalog) == before


def test_expired_takeover_retires_and_starts_at_one_content_tick(tmp_path) -> None:
    clock = {"ms": 1_000}
    with SQLiteCatalog(tmp_path / "one-tick-takeover.sqlite3") as catalog:
        _install_clock(catalog, clock)
        first = _create_job(catalog, idempotency_key="first")
        second = catalog.create_job(
            first.repository_id,
            first.source_revision_id,
            "second",
            first.request,
        )
        catalog.acquire_job_lease(
            first.job_id,
            owner_id="worker-1",
            lease_duration_ms=100,
        )

        clock["ms"] = 3_000
        expected_tick = catalog._db_now_ms()
        second_lease = catalog.acquire_job_lease(
            second.job_id,
            owner_id="worker-2",
            lease_duration_ms=60_000,
        )
        first_completion = catalog.get_job_attempt_completion(first.job_id, 1)
        second_attempt = catalog.get_job_attempt(second.job_id, 1)
        high_water_ms = catalog._connection.execute(
            "SELECT high_water_ms FROM index_job_execution_clock"
        ).fetchone()[0]

        assert first_completion.completed_at_ms == expected_tick
        assert catalog.get_job(first.job_id).updated_at_ms == expected_tick
        assert second_lease.acquired_at_ms == expected_tick
        assert second_attempt.started_at_ms == expected_tick
        assert catalog.get_job(second.job_id).updated_at_ms == expected_tick
        assert high_water_ms == expected_tick


def test_initial_job_reopens_during_clock_rollback_and_waits_to_run(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "restart-initial-job.sqlite3"
    clock = {"ms": 2_999}
    with SQLiteCatalog(path) as catalog:
        _install_clock(catalog, clock)
        job = _create_job(catalog)
        assert job.created_at_ms == 2_999

    clock["ms"] = 2_998
    _patch_connection_clock(monkeypatch, clock)
    with SQLiteCatalog(path, create=False) as catalog:
        assert catalog.get_job(job.job_id) == job
        assert catalog.scan_runnable_jobs().jobs == ()
        before = _execution_state(catalog)
        with pytest.raises(CatalogConflictError, match="clock moved backwards"):
            catalog.acquire_job_lease(
                job.job_id,
                owner_id="worker",
                lease_duration_ms=60_000,
            )
        with pytest.raises(CatalogConflictError, match="clock moved backwards"):
            catalog.request_job_cancel(job.job_id)
        assert _execution_state(catalog) == before

        clock["ms"] = 3_001
        assert [item.job_id for item in catalog.scan_runnable_jobs().jobs] == [
            job.job_id
        ]
        expected_tick = catalog._db_now_ms()
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker",
            lease_duration_ms=60_000,
        )
        assert lease.acquired_at_ms == expected_tick


@pytest.mark.parametrize(
    "outcome",
    (
        IndexJobCompletion.REQUEUE,
        IndexJobCompletion.FAILED,
        IndexJobCompletion.CANCELLED,
    ),
)
def test_canonical_attempt_closure_reopens_after_wall_clock_rollback(
    tmp_path,
    monkeypatch,
    outcome: IndexJobCompletion,
) -> None:
    path = tmp_path / f"restart-{outcome.value}.sqlite3"
    clock = {"ms": 1_000}
    with SQLiteCatalog(path) as catalog:
        _install_clock(catalog, clock)
        job = _create_job(catalog)
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker",
            lease_duration_ms=60_000,
        )
        if outcome is IndexJobCompletion.CANCELLED:
            clock["ms"] = 2_000
            catalog.request_job_cancel(job.job_id)
        clock["ms"] = 2_999
        completed = catalog.complete_job_attempt(
            job.job_id,
            attempt_count=1,
            owner_id="worker",
            fencing_token=lease.fencing_token,
            outcome=outcome,
            error_code=None if outcome is IndexJobCompletion.CANCELLED else "closed",
        )

    clock["ms"] = 2_998
    _patch_connection_clock(monkeypatch, clock)
    with SQLiteCatalog(path, create=False) as catalog:
        assert catalog.get_job(job.job_id) == completed
        assert (
            catalog._connection.execute(
                "SELECT high_water_ms FROM index_job_execution_clock"
            ).fetchone()[0]
            == 2_999
        )
        if outcome is IndexJobCompletion.REQUEUE:
            before = _execution_state(catalog)
            with pytest.raises(CatalogConflictError, match="clock moved backwards"):
                catalog.acquire_job_lease(
                    job.job_id,
                    owner_id="worker-2",
                    lease_duration_ms=60_000,
                )
            with pytest.raises(CatalogConflictError, match="clock moved backwards"):
                catalog.request_job_cancel(job.job_id)
            assert _execution_state(catalog) == before
            clock["ms"] = 3_000
            assert (
                catalog.acquire_job_lease(
                    job.job_id,
                    owner_id="worker-2",
                    lease_duration_ms=60_000,
                ).fencing_token
                == 2
            )


def test_exact_execution_replays_ignore_content_clock_rollback(tmp_path) -> None:
    clock = {"ms": 1_000}
    with SQLiteCatalog(tmp_path / "exact-replay-clock.sqlite3") as catalog:
        _install_clock(catalog, clock)
        closed_job = _create_job(catalog, idempotency_key="closed")
        closed_lease = catalog.acquire_job_lease(
            closed_job.job_id,
            owner_id="closed-worker",
            lease_duration_ms=60_000,
        )
        clock["ms"] = 2_000
        event = catalog.append_job_event(
            closed_job.job_id,
            attempt_count=1,
            owner_id="closed-worker",
            fencing_token=closed_lease.fencing_token,
            event_key="durable-progress",
            payload={"phase": "done"},
        )
        clock["ms"] = 2_500
        catalog.request_job_cancel(closed_job.job_id)
        clock["ms"] = 3_000
        completed = catalog.complete_job_attempt(
            closed_job.job_id,
            attempt_count=1,
            owner_id="closed-worker",
            fencing_token=closed_lease.fencing_token,
            outcome=IndexJobCompletion.CANCELLED,
        )
        active_job = catalog.create_job(
            closed_job.repository_id,
            closed_job.source_revision_id,
            "active",
            closed_job.request,
            ref_name="active",
        )
        active_lease = catalog.acquire_job_lease(
            active_job.job_id,
            owner_id="active-worker",
            lease_duration_ms=60_000,
        )
        before = _execution_state(catalog)

        clock["ms"] = 500
        assert (
            catalog.create_job(
                closed_job.repository_id,
                closed_job.source_revision_id,
                "closed",
                closed_job.request,
            )
            == completed
        )
        assert (
            catalog.acquire_job_lease(
                active_job.job_id,
                owner_id="active-worker",
                lease_duration_ms=60_000,
            )
            == active_lease
        )
        assert catalog.request_job_cancel(closed_job.job_id) == completed
        assert (
            catalog.append_job_event(
                closed_job.job_id,
                attempt_count=1,
                owner_id="closed-worker",
                fencing_token=closed_lease.fencing_token,
                event_key="durable-progress",
                payload={"phase": "done"},
            )
            == event
        )
        assert (
            catalog.complete_job_attempt(
                closed_job.job_id,
                attempt_count=1,
                owner_id="closed-worker",
                fencing_token=closed_lease.fencing_token,
                outcome=IndexJobCompletion.CANCELLED,
            )
            == completed
        )
        assert _execution_state(catalog) == before


def test_heartbeat_clock_is_independent_but_fresh_content_observes_it(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "heartbeat-content-clock.sqlite3"
    clock = {"ms": 1_000}
    with SQLiteCatalog(path) as catalog:
        _install_clock(catalog, clock)
        job = _create_job(catalog)
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker",
            lease_duration_ms=60_000,
        )
        content_high_water = catalog._connection.execute(
            "SELECT high_water_ms FROM index_job_execution_clock"
        ).fetchone()[0]
        clock["ms"] = 2_000
        heartbeat = catalog.heartbeat_job_attempt(
            job.job_id,
            attempt_count=1,
            owner_id="worker",
            fencing_token=lease.fencing_token,
            lease_duration_ms=60_000,
        )
        assert heartbeat.lease.heartbeat_at_ms > content_high_water
        assert (
            catalog._connection.execute(
                "SELECT high_water_ms FROM index_job_execution_clock"
            ).fetchone()[0]
            == content_high_water
        )

    clock["ms"] = 1_500
    _patch_connection_clock(monkeypatch, clock)
    with SQLiteCatalog(path, create=False) as catalog:
        before = _execution_state(catalog)
        assert (
            catalog.acquire_job_lease(
                job.job_id,
                owner_id="worker",
                lease_duration_ms=60_000,
            )
            == heartbeat.lease
        )
        with pytest.raises(CatalogConflictError, match="clock moved backwards"):
            catalog.append_job_event(
                job.job_id,
                attempt_count=1,
                owner_id="worker",
                fencing_token=lease.fencing_token,
                event_key="too-early",
            )
        assert _execution_state(catalog) == before

        clock["ms"] = heartbeat.lease.heartbeat_at_ms + 10
        event = catalog.append_job_event(
            job.job_id,
            attempt_count=1,
            owner_id="worker",
            fencing_token=lease.fencing_token,
            event_key="caught-up",
        )
        assert event.created_at_ms >= heartbeat.lease.heartbeat_at_ms


def test_expired_cancelled_holder_retirement_rejects_clock_rollback_cleanly(
    tmp_path,
) -> None:
    clock = {"ms": 1_000}
    with SQLiteCatalog(tmp_path / "retirement-clock-rollback.sqlite3") as catalog:
        _install_clock(catalog, clock)
        first = _create_job(catalog, idempotency_key="first")
        second = catalog.create_job(
            first.repository_id,
            first.source_revision_id,
            "second",
            first.request,
        )
        lease = catalog.acquire_job_lease(
            first.job_id,
            owner_id="worker-1",
            lease_duration_ms=100,
        )
        clock["ms"] = 3_000
        assert catalog.request_job_cancel(first.job_id).cancel_requested
        expected_slot = tuple(
            catalog._connection.execute("SELECT * FROM ref_job_leases").fetchone()
        )
        clock["ms"] = 2_500

        with pytest.raises(CatalogConflictError, match="clock moved backwards"):
            catalog.acquire_job_lease(
                second.job_id,
                owner_id="worker-2",
                lease_duration_ms=60_000,
            )

        first_after = catalog.get_job(first.job_id)
        assert first_after.status is IndexJobStatus.RUNNING
        assert first_after.cancel_requested
        assert catalog.get_job(second.job_id).status is IndexJobStatus.QUEUED
        assert catalog.list_job_attempt_completions(first.job_id) == ()
        assert (
            catalog._connection.execute(
                "SELECT COUNT(*) FROM index_job_attempt_closure_frontiers"
            ).fetchone()[0]
            == 0
        )
        assert (
            tuple(
                catalog._connection.execute("SELECT * FROM ref_job_leases").fetchone()
            )
            == expected_slot
        )
        assert lease.lease_expires_at_ms < first_after.updated_at_ms


def test_raw_future_job_update_is_rejected_and_expired_takeover_stays_usable(
    tmp_path,
) -> None:
    path = tmp_path / "future-job-time.sqlite3"
    clock = {"ms": 1_000}
    with SQLiteCatalog(path) as catalog:
        _install_clock(catalog, clock)
        first = _create_job(catalog, idempotency_key="first")
        second = catalog.create_job(
            first.repository_id,
            first.source_revision_id,
            "second",
            first.request,
        )
        lease = catalog.acquire_job_lease(
            first.job_id,
            owner_id="worker-1",
            lease_duration_ms=10,
        )
        before = catalog._connection.execute(
            "SELECT * FROM index_jobs WHERE job_id = ?",
            (first.job_id,),
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="status transition"):
            catalog._connection.execute(
                "UPDATE index_jobs SET updated_at_ms = ? WHERE job_id = ?",
                (2**63 - 1, first.job_id),
            )
        after = catalog._connection.execute(
            "SELECT * FROM index_jobs WHERE job_id = ?",
            (first.job_id,),
        ).fetchone()
        assert tuple(after) == tuple(before)

    with SQLiteCatalog(path, create=False) as catalog:
        acquired = catalog.acquire_job_lease(
            second.job_id,
            owner_id="worker-2",
            lease_duration_ms=60_000,
        )
        assert acquired.fencing_token == lease.fencing_token + 1
        assert catalog.get_job(first.job_id).status is IndexJobStatus.QUEUED
        assert catalog.get_job(second.job_id).status is IndexJobStatus.RUNNING

    with SQLiteCatalog(path, create=False) as catalog:
        assert catalog.get_job(second.job_id).status is IndexJobStatus.RUNNING


def test_reopen_rejects_bypassed_future_running_job_update(tmp_path) -> None:
    path = tmp_path / "bypassed-future-job-time.sqlite3"
    with SQLiteCatalog(path) as catalog:
        job = _create_job(catalog)
        catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker",
            lease_duration_ms=60_000,
        )
        _drop_and_restore_triggers(
            catalog._connection,
            ("index_job_status_transitions_are_valid",),
            "UPDATE index_jobs SET updated_at_ms = ? WHERE job_id = ?",
            (2**63 - 1, job.job_id),
        )

    with pytest.raises(CatalogConflictError, match="update time"):
        SQLiteCatalog(path, create=False)


def test_raw_future_lease_renewal_is_rejected_before_reopen_and_real_renewal(
    tmp_path,
) -> None:
    path = tmp_path / "future-lease-time.sqlite3"
    clock = {"ms": 1_000}
    with SQLiteCatalog(path) as catalog:
        _install_clock(catalog, clock)
        job = _create_job(catalog)
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker",
            lease_duration_ms=60_000,
        )
        before = catalog._connection.execute(
            "SELECT * FROM ref_job_leases WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="fencing transition"):
            catalog._connection.execute(
                """
                UPDATE ref_job_leases
                SET heartbeat_at_ms = 9223372036854775806,
                    updated_at_ms = 9223372036854775806,
                    lease_expires_at_ms = 9223372036854775807
                WHERE job_id = ?
                """,
                (job.job_id,),
            )
        after = catalog._connection.execute(
            "SELECT * FROM ref_job_leases WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()
        assert tuple(after) == tuple(before)

    with SQLiteCatalog(path, create=False) as catalog:
        clock["ms"] = 2_000
        _install_clock(catalog, clock)
        renewed = catalog.renew_job_lease(
            job.job_id,
            owner_id="worker",
            fencing_token=lease.fencing_token,
            lease_duration_ms=60_000,
        )
        assert renewed.heartbeat_at_ms > lease.heartbeat_at_ms
        clock["ms"] = 3_000
        assert catalog.request_job_cancel(job.job_id).cancel_requested


def test_cancellation_markers_distinguish_queued_and_exact_running_authority(
    tmp_path,
) -> None:
    path = tmp_path / "cancellation-authority.sqlite3"
    with SQLiteCatalog(path) as catalog:
        queued = _create_job(catalog, idempotency_key="queued", ref_name="queued")
        cancelled = catalog.request_job_cancel(queued.job_id)
        assert cancelled.status is IndexJobStatus.CANCELLED
        queued_marker = catalog._connection.execute(
            "SELECT * FROM index_job_cancellation_requests WHERE job_id = ?",
            (queued.job_id,),
        ).fetchone()
        assert tuple(queued_marker)[2:] == ("queued_v6", None, None, None, None)

        main = catalog.create_job(
            queued.repository_id,
            queued.source_revision_id,
            "main-running",
            queued.request,
            ref_name="main",
        )
        feature = catalog.create_job(
            queued.repository_id,
            queued.source_revision_id,
            "feature-running",
            queued.request,
            ref_name="feature",
        )
        catalog.acquire_job_lease(
            main.job_id,
            owner_id="main-worker",
            lease_duration_ms=60_000,
        )
        feature_lease = catalog.acquire_job_lease(
            feature.job_id,
            owner_id="feature-worker",
            lease_duration_ms=60_000,
        )
        requested = catalog.request_job_cancel(feature.job_id)
        marker = catalog._connection.execute(
            """
            SELECT request_kind, attempt_count, owner_id, fencing_token,
                observed_heartbeat_at_ms
            FROM index_job_cancellation_requests WHERE job_id = ?
            """,
            (feature.job_id,),
        ).fetchone()
        assert tuple(marker) == (
            "running_v6",
            1,
            "feature-worker",
            feature_lease.fencing_token,
            feature_lease.heartbeat_at_ms,
        )
        assert requested.status is IndexJobStatus.RUNNING

    with SQLiteCatalog(path, create=False) as catalog:
        assert catalog.get_job(feature.job_id).cancel_requested


def test_raw_queued_or_future_running_cancellation_cannot_strand_a_job(
    tmp_path,
) -> None:
    path = tmp_path / "raw-cancellation.sqlite3"
    clock = {"ms": 1_000}
    with SQLiteCatalog(path) as catalog:
        _install_clock(catalog, clock)
        queued = _create_job(catalog, idempotency_key="queued", ref_name="queued")
        with pytest.raises(sqlite3.IntegrityError, match="status transition"):
            catalog._connection.execute(
                """
                UPDATE index_jobs
                SET cancel_requested = 1, updated_at_ms = ? WHERE job_id = ?
                """,
                (catalog._db_now_ms(), queued.job_id),
            )

        running = catalog.create_job(
            queued.repository_id,
            queued.source_revision_id,
            "running",
            queued.request,
            ref_name="running",
        )
        catalog.acquire_job_lease(
            running.job_id,
            owner_id="worker",
            lease_duration_ms=60_000,
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="cancellation request|status transition",
        ):
            catalog._connection.execute(
                """
                UPDATE index_jobs
                SET cancel_requested = 1, updated_at_ms = ? WHERE job_id = ?
                """,
                (5_000, running.job_id),
            )
        assert not catalog.get_job(queued.job_id).cancel_requested
        assert not catalog.get_job(running.job_id).cancel_requested
        assert (
            catalog._connection.execute(
                "SELECT COUNT(*) FROM index_job_cancellation_requests"
            ).fetchone()[0]
            == 0
        )


def test_reopen_rejects_restored_schema_with_stranded_queued_cancellation(
    tmp_path,
) -> None:
    path = tmp_path / "stranded-queued-cancel.sqlite3"
    with SQLiteCatalog(path) as catalog:
        job = _create_job(catalog)
        now_ms = catalog._db_now_ms()
        _drop_and_restore_triggers(
            catalog._connection,
            (
                "index_job_status_transitions_are_valid",
                "index_job_cancellation_requests_validate_insert",
            ),
            """
            UPDATE index_jobs
            SET cancel_requested = 1, updated_at_ms = ? WHERE job_id = ?
            """,
            (now_ms, job.job_id),
        )

    with pytest.raises(
        (StorageValidationError, StorageIntegrityError, CatalogConflictError),
    ):
        SQLiteCatalog(path, create=False)


def test_reopen_rejects_active_lease_with_distinct_update_and_heartbeat_times(
    tmp_path,
) -> None:
    path = tmp_path / "inexact-active-lease.sqlite3"
    clock = {"ms": 1_000}
    with SQLiteCatalog(path) as catalog:
        _install_clock(catalog, clock)
        job = _create_job(catalog)
        catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker",
            lease_duration_ms=60_000,
        )
        _drop_and_restore_triggers(
            catalog._connection,
            ("ref_job_lease_updates_are_fenced",),
            """
            UPDATE ref_job_leases
            SET updated_at_ms = 70000, lease_expires_at_ms = 80000
            WHERE job_id = ?
            """,
            (job.job_id,),
        )

    with pytest.raises(CatalogConflictError, match="lease slot"):
        SQLiteCatalog(path, create=False)


def test_reopen_rejects_restored_schema_after_flag_or_attempt_history_erasure(
    tmp_path,
) -> None:
    cancel_path = tmp_path / "cancel.sqlite3"
    with SQLiteCatalog(cancel_path) as catalog:
        cancelled_job = _create_job(catalog)
        catalog.acquire_job_lease(
            cancelled_job.job_id,
            owner_id="worker",
            lease_duration_ms=60_000,
        )
        catalog.request_job_cancel(cancelled_job.job_id)
        _drop_and_restore_triggers(
            catalog._connection,
            (
                "index_job_cancellation_is_monotonic",
                "index_job_status_transitions_are_valid",
            ),
            "UPDATE index_jobs SET cancel_requested = 0 WHERE job_id = ?",
            (cancelled_job.job_id,),
        )
    with pytest.raises(CatalogConflictError, match="cancellation"):
        SQLiteCatalog(cancel_path, create=False)

    attempt_path = tmp_path / "attempt.sqlite3"
    with SQLiteCatalog(attempt_path) as catalog:
        queued_job = _create_job(catalog)
        _drop_and_restore_triggers(
            catalog._connection,
            ("index_job_status_transitions_are_valid",),
            "UPDATE index_jobs SET attempt_count = max_attempts WHERE job_id = ?",
            (queued_job.job_id,),
        )
    with pytest.raises(
        (StorageValidationError, StorageIntegrityError, CatalogConflictError),
    ):
        SQLiteCatalog(attempt_path, create=False)


@pytest.mark.parametrize("corruption", ["missing", "completion_time"])
def test_reopen_rejects_missing_or_inexact_attempt_closure(
    tmp_path,
    corruption: str,
) -> None:
    path = tmp_path / f"{corruption}.sqlite3"
    with SQLiteCatalog(path) as catalog:
        job = _create_job(catalog)
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker",
            lease_duration_ms=60_000,
        )
        catalog.complete_job_attempt(
            job.job_id,
            attempt_count=1,
            owner_id="worker",
            fencing_token=lease.fencing_token,
            outcome=IndexJobCompletion.REQUEUE,
            error_code="retry",
        )
        if corruption == "missing":
            trigger = "index_job_attempt_completions_cannot_be_deleted"
            mutation = """
                DELETE FROM index_job_attempt_completions
                WHERE job_id = ? AND attempt_count = 1
            """
        else:
            trigger = "index_job_attempt_completions_are_immutable"
            mutation = """
                UPDATE index_job_attempt_completions
                SET completed_at_ms = completed_at_ms + 1
                WHERE job_id = ? AND attempt_count = 1
            """
        _drop_and_restore_triggers(
            catalog._connection,
            (trigger,),
            mutation,
            (job.job_id,),
        )

    with pytest.raises(CatalogConflictError, match="closure|job state"):
        SQLiteCatalog(path, create=False)


@pytest.mark.parametrize(
    "payload_json",
    [
        '{"api_token":"hidden"}',
        '{ "safe": 1 }',
        _nested_payload_json(17),
        json.dumps({"items": [None] * 1_024}),
        json.dumps({"x" * 129: 1}),
    ],
)
def test_reopen_revalidates_raw_event_payloads_fail_closed(
    tmp_path,
    payload_json: str,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        job = _create_job(catalog)
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker",
            lease_duration_ms=60_000,
        )
        catalog.append_job_event(
            job.job_id,
            attempt_count=1,
            owner_id="worker",
            fencing_token=lease.fencing_token,
            event_key="event",
        )
        _drop_and_restore_triggers(
            catalog._connection,
            ("index_job_events_are_immutable",),
            """
            UPDATE index_job_events SET payload_json = ?
            WHERE job_id = ? AND event_key = 'event'
            """,
            (payload_json, job.job_id),
        )

    with pytest.raises(
        (StorageValidationError, StorageIntegrityError, CatalogConflictError),
    ):
        SQLiteCatalog(path, create=False)


def test_acquire_and_non_success_completion_have_immutable_exact_closures(
    tmp_path,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        job = _create_job(catalog)
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker-1",
            lease_duration_ms=60_000,
        )
        assert (
            catalog.acquire_job_lease(
                job.job_id,
                owner_id="worker-1",
                lease_duration_ms=1,
            )
            == lease
        )
        attempts = catalog._connection.execute(
            "SELECT * FROM index_job_attempts WHERE job_id = ?",
            (job.job_id,),
        ).fetchall()
        assert len(attempts) == 1
        assert attempts[0]["attempt_count"] == 1
        assert attempts[0]["owner_id"] == "worker-1"
        assert attempts[0]["fencing_token"] == lease.fencing_token

        requeued = catalog.complete_job_attempt(
            job.job_id,
            attempt_count=1,
            owner_id="worker-1",
            fencing_token=lease.fencing_token,
            outcome=IndexJobCompletion.REQUEUE,
            error_code="transient_io",
        )
        replayed = catalog.complete_job_attempt(
            job.job_id,
            attempt_count=1,
            owner_id="worker-1",
            fencing_token=lease.fencing_token,
            outcome=IndexJobCompletion.REQUEUE,
            error_code="transient_io",
        )
        assert requeued == replayed
        assert replayed.status is IndexJobStatus.QUEUED
        second_lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker-2",
            lease_duration_ms=60_000,
        )
        historical_replay = catalog.complete_job_attempt(
            job.job_id,
            attempt_count=1,
            owner_id="worker-1",
            fencing_token=lease.fencing_token,
            outcome=IndexJobCompletion.REQUEUE,
            error_code="transient_io",
        )
        assert historical_replay == requeued
        assert historical_replay.attempt_count == 1
        assert catalog.get_job(job.job_id).attempt_count == 2
        assert catalog.get_job(job.job_id).status is IndexJobStatus.RUNNING
        assert second_lease.owner_id == "worker-2"
        with pytest.raises(CatalogConflictError, match="replay conflicts"):
            catalog.complete_job_attempt(
                job.job_id,
                attempt_count=1,
                owner_id="worker-1",
                fencing_token=lease.fencing_token,
                outcome=IndexJobCompletion.REQUEUE,
                error_code="different",
            )

        with pytest.raises(sqlite3.IntegrityError, match="attempts are immutable"):
            catalog._connection.execute(
                """
                UPDATE index_job_attempts SET owner_id = 'replacement'
                WHERE job_id = ? AND attempt_count = 1
                """,
                (job.job_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="completions are immutable"):
            catalog._connection.execute(
                """
                DELETE FROM index_job_attempt_completions
                WHERE job_id = ? AND attempt_count = 1
                """,
                (job.job_id,),
            )


def test_event_cancel_and_completion_reject_database_clock_rollback(
    tmp_path,
) -> None:
    path = tmp_path / "causal-clock.sqlite3"
    clock = {"ms": 1_000}
    with SQLiteCatalog(path) as catalog:
        _install_clock(catalog, clock)
        job = _create_job(catalog)
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker",
            lease_duration_ms=60_000,
        )
        clock["ms"] = 2_000
        heartbeat = catalog.heartbeat_job_attempt(
            job.job_id,
            attempt_count=1,
            owner_id="worker",
            fencing_token=lease.fencing_token,
            lease_duration_ms=60_000,
        )
        clock["ms"] = 3_000
        event = catalog.append_job_event(
            job.job_id,
            attempt_count=1,
            owner_id="worker",
            fencing_token=lease.fencing_token,
            event_key="progress",
        )

        clock["ms"] = 2_500
        with pytest.raises(CatalogConflictError, match="clock moved backwards"):
            catalog.append_job_event(
                job.job_id,
                attempt_count=1,
                owner_id="worker",
                fencing_token=lease.fencing_token,
                event_key="rolled-back-event",
            )
        with pytest.raises(CatalogConflictError, match="clock moved backwards"):
            catalog.request_job_cancel(job.job_id)
        assert not catalog.get_job(job.job_id).cancel_requested

        clock["ms"] = 4_000
        requested = catalog.request_job_cancel(job.job_id)
        assert requested.cancel_requested
        marker = catalog._connection.execute(
            """
            SELECT observed_heartbeat_at_ms
            FROM index_job_cancellation_requests WHERE job_id = ?
            """,
            (job.job_id,),
        ).fetchone()[0]
        assert marker == heartbeat.lease.heartbeat_at_ms

        clock["ms"] = 3_500
        with pytest.raises(CatalogConflictError, match="clock moved backwards"):
            catalog.complete_job_attempt(
                job.job_id,
                attempt_count=1,
                owner_id="worker",
                fencing_token=lease.fencing_token,
                outcome=IndexJobCompletion.CANCELLED,
            )
        assert catalog.get_job(job.job_id).status is IndexJobStatus.RUNNING

        clock["ms"] = 5_000
        completed = catalog.complete_job_attempt(
            job.job_id,
            attempt_count=1,
            owner_id="worker",
            fencing_token=lease.fencing_token,
            outcome=IndexJobCompletion.CANCELLED,
        )
        assert completed.status is IndexJobStatus.CANCELLED
        frontier = catalog._connection.execute(
            """
            SELECT event_count, max_event_sequence, max_event_created_at_ms
            FROM index_job_attempt_closure_frontiers
            WHERE job_id = ? AND attempt_count = 1
            """,
            (job.job_id,),
        ).fetchone()
        assert tuple(frontier) == (1, event.sequence, event.created_at_ms)

    with SQLiteCatalog(path, create=False) as catalog:
        assert catalog.get_job(job.job_id).status is IndexJobStatus.CANCELLED


def test_cancelled_attempt_can_only_commit_a_cancelled_closure(tmp_path) -> None:
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        job = _create_job(catalog)
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker",
            lease_duration_ms=60_000,
        )
        catalog.request_job_cancel(job.job_id)
        completed_at_ms = catalog._db_now_ms()
        with pytest.raises(sqlite3.IntegrityError, match="completion is invalid"):
            catalog._connection.execute(
                """
                INSERT INTO index_job_attempt_completions(
                    job_id, attempt_count, owner_id, fencing_token, outcome,
                    error_code, error_message, completed_at_ms
                ) VALUES (?, 1, 'worker', ?, 'failed', 'raw', NULL, ?)
                """,
                (job.job_id, lease.fencing_token, completed_at_ms),
            )
        for outcome in (IndexJobCompletion.REQUEUE, IndexJobCompletion.FAILED):
            with pytest.raises(CatalogConflictError, match="cancelled"):
                catalog.complete_job_attempt(
                    job.job_id,
                    attempt_count=1,
                    owner_id="worker",
                    fencing_token=lease.fencing_token,
                    outcome=outcome,
                    error_code="raw",
                )
        completed = catalog.complete_job_attempt(
            job.job_id,
            attempt_count=1,
            owner_id="worker",
            fencing_token=lease.fencing_token,
            outcome=IndexJobCompletion.CANCELLED,
        )
        assert completed.status is IndexJobStatus.CANCELLED


@pytest.mark.parametrize(
    ("table", "event", "when"),
    [
        ("index_job_attempt_closure_frontiers", "INSERT", "1"),
        ("index_jobs", "UPDATE", "NEW.status = 'failed'"),
        ("ref_job_leases", "UPDATE", "NEW.job_id IS NULL"),
    ],
)
def test_failure_at_each_completion_stage_rolls_back_the_whole_closure(
    tmp_path,
    table: str,
    event: str,
    when: str,
) -> None:
    with SQLiteCatalog(tmp_path / f"{table}.sqlite3") as catalog:
        job = _create_job(catalog)
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker",
            lease_duration_ms=60_000,
        )
        catalog.append_job_event(
            job.job_id,
            attempt_count=1,
            owner_id="worker",
            fencing_token=lease.fencing_token,
            event_key="progress",
        )
        before_job = tuple(
            catalog._connection.execute(
                "SELECT * FROM index_jobs WHERE job_id = ?",
                (job.job_id,),
            ).fetchone()
        )
        before_lease = tuple(
            catalog._connection.execute(
                "SELECT * FROM ref_job_leases WHERE job_id = ?",
                (job.job_id,),
            ).fetchone()
        )
        catalog._connection.execute(
            f"""
            CREATE TRIGGER injected_completion_failure
            BEFORE {event} ON {table}
            WHEN {when}
            BEGIN
                SELECT RAISE(ABORT, 'injected completion failure');
            END
            """
        )  # noqa: S608 - values come from the fixed parametrization

        with pytest.raises(sqlite3.IntegrityError, match="injected completion"):
            catalog.complete_job_attempt(
                job.job_id,
                attempt_count=1,
                owner_id="worker",
                fencing_token=lease.fencing_token,
                outcome=IndexJobCompletion.FAILED,
                error_code="failed",
            )
        assert (
            tuple(
                catalog._connection.execute(
                    "SELECT * FROM index_jobs WHERE job_id = ?",
                    (job.job_id,),
                ).fetchone()
            )
            == before_job
        )
        assert (
            tuple(
                catalog._connection.execute(
                    "SELECT * FROM ref_job_leases WHERE job_id = ?",
                    (job.job_id,),
                ).fetchone()
            )
            == before_lease
        )
        assert (
            catalog._connection.execute(
                "SELECT COUNT(*) FROM index_job_attempt_completions"
            ).fetchone()[0]
            == 0
        )
        assert (
            catalog._connection.execute(
                "SELECT COUNT(*) FROM index_job_attempt_closure_frontiers"
            ).fetchone()[0]
            == 0
        )


def test_cancel_and_event_trigger_failures_roll_back_their_whole_statement(
    tmp_path,
) -> None:
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        job = _create_job(catalog)
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker",
            lease_duration_ms=60_000,
        )
        catalog._connection.execute(
            """
            CREATE TRIGGER injected_event_failure
            AFTER INSERT ON index_job_events
            BEGIN
                SELECT RAISE(ABORT, 'injected event failure');
            END
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected event"):
            catalog.append_job_event(
                job.job_id,
                attempt_count=1,
                owner_id="worker",
                fencing_token=lease.fencing_token,
                event_key="rolled-back",
            )
        assert catalog.list_job_events(job.job_id) == ()
        catalog._connection.execute("DROP TRIGGER injected_event_failure")

        catalog._connection.execute(
            """
            CREATE TRIGGER injected_cancellation_failure
            AFTER INSERT ON index_job_cancellation_requests
            BEGIN
                SELECT RAISE(ABORT, 'injected cancellation failure');
            END
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected cancellation"):
            catalog.request_job_cancel(job.job_id)
        assert not catalog.get_job(job.job_id).cancel_requested
        assert (
            catalog._connection.execute(
                "SELECT COUNT(*) FROM index_job_cancellation_requests"
            ).fetchone()[0]
            == 0
        )


def test_exact_completion_replay_authenticates_its_event_frontier(tmp_path) -> None:
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        job = _create_job(catalog)
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker",
            lease_duration_ms=60_000,
        )
        catalog.append_job_event(
            job.job_id,
            attempt_count=1,
            owner_id="worker",
            fencing_token=lease.fencing_token,
            event_key="progress",
        )
        catalog.complete_job_attempt(
            job.job_id,
            attempt_count=1,
            owner_id="worker",
            fencing_token=lease.fencing_token,
            outcome=IndexJobCompletion.REQUEUE,
            error_code="retry",
        )
        _drop_and_restore_triggers(
            catalog._connection,
            ("index_job_attempt_frontiers_are_immutable",),
            """
            UPDATE index_job_attempt_closure_frontiers
            SET event_count = 0, max_event_sequence = 0,
                max_event_created_at_ms = 0
            WHERE job_id = ? AND attempt_count = 1
            """,
            (job.job_id,),
        )
        with pytest.raises(CatalogConflictError, match="frontier"):
            catalog.complete_job_attempt(
                job.job_id,
                attempt_count=1,
                owner_id="worker",
                fencing_token=lease.fencing_token,
                outcome=IndexJobCompletion.REQUEUE,
                error_code="retry",
            )


def test_equal_time_event_after_closure_is_detected_by_the_exact_frontier(
    tmp_path,
) -> None:
    path = tmp_path / "post-close-event.sqlite3"
    clock = {"ms": 1_000}
    with SQLiteCatalog(path) as catalog:
        _install_clock(catalog, clock)
        job = _create_job(catalog)
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker",
            lease_duration_ms=60_000,
        )
        event = catalog.append_job_event(
            job.job_id,
            attempt_count=1,
            owner_id="worker",
            fencing_token=lease.fencing_token,
            event_key="before-close",
        )
        completed = catalog.complete_job_attempt(
            job.job_id,
            attempt_count=1,
            owner_id="worker",
            fencing_token=lease.fencing_token,
            outcome=IndexJobCompletion.FAILED,
            error_code="failed",
        )
        assert completed.finished_at_ms is not None
        _drop_and_restore_triggers(
            catalog._connection,
            ("index_job_events_validate_insert",),
            """
            INSERT INTO index_job_events(
                job_id, attempt_count, event_key, kind, owner_id,
                fencing_token, view_type, effective_mode, outcome,
                payload_json, created_at_ms
            ) VALUES (?, 1, 'after-close', 'progress', 'worker', ?, NULL,
                NULL, NULL, '{}', ?)
            """,
            (job.job_id, lease.fencing_token, completed.finished_at_ms),
        )
        assert len(catalog.list_job_events(job.job_id)) == 2
        assert event.created_at_ms == completed.finished_at_ms

    with pytest.raises(CatalogConflictError, match="frontier"):
        SQLiteCatalog(path, create=False)


@pytest.mark.parametrize("event_sequence", [-1, 0, 1, 2**63 - 1])
def test_raw_event_sequence_must_be_allocator_assigned_and_positive(
    tmp_path,
    event_sequence: int,
) -> None:
    with SQLiteCatalog(tmp_path / f"event-{event_sequence}.sqlite3") as catalog:
        job = _create_job(catalog)
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker",
            lease_duration_ms=60_000,
        )
        catalog._connection.execute("PRAGMA recursive_triggers = OFF")
        assert (
            catalog._connection.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="CHECK constraint|index job event is invalid",
        ):
            catalog._connection.execute(
                """
                INSERT INTO index_job_events(
                    event_sequence, job_id, attempt_count, event_key, kind,
                    owner_id, fencing_token, view_type, effective_mode, outcome,
                    payload_json, created_at_ms
                ) VALUES (?, ?, 1, 'raw', 'progress', 'worker', ?, NULL,
                    NULL, NULL, '{}', ?)
                """,
                (
                    event_sequence,
                    job.job_id,
                    lease.fencing_token,
                    catalog._db_now_ms(),
                ),
            )
        assert catalog.list_job_events(job.job_id) == ()


def test_event_sequence_exhaustion_is_stable_and_exact_replay_still_wins(
    tmp_path,
) -> None:
    with SQLiteCatalog(tmp_path / "event-sequence-exhaustion.sqlite3") as catalog:
        job = _create_job(catalog)
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker",
            lease_duration_ms=60_000,
        )
        first = catalog.append_job_event(
            job.job_id,
            attempt_count=1,
            owner_id="worker",
            fencing_token=lease.fencing_token,
            event_key="first",
        )
        assert first.sequence == 1
        catalog._connection.execute(
            "UPDATE sqlite_sequence SET seq = ? WHERE name = 'index_job_events'",
            (2**63 - 2,),
        )
        final = catalog.append_job_event(
            job.job_id,
            attempt_count=1,
            owner_id="worker",
            fencing_token=lease.fencing_token,
            event_key="final",
            payload={"step": "final"},
        )
        assert final.sequence == 2**63 - 1
        assert (
            catalog.append_job_event(
                job.job_id,
                attempt_count=1,
                owner_id="worker",
                fencing_token=lease.fencing_token,
                event_key="final",
                payload={"step": "final"},
            )
            == final
        )

        before = catalog.list_job_events(job.job_id)
        with pytest.raises(CatalogConflictError, match="sequence is exhausted"):
            catalog.append_job_event(
                job.job_id,
                attempt_count=1,
                owner_id="worker",
                fencing_token=lease.fencing_token,
                event_key="after-max",
            )
        assert catalog.list_job_events(job.job_id) == before

        catalog._connection.execute(
            "UPDATE sqlite_sequence SET seq = 1 WHERE name = 'index_job_events'"
        )
        with pytest.raises(CatalogConflictError, match="sequence is exhausted"):
            catalog.append_job_event(
                job.job_id,
                attempt_count=1,
                owner_id="worker",
                fencing_token=lease.fencing_token,
                event_key="table-high-water",
            )
        assert catalog.list_job_events(job.job_id) == before


def test_attempt_and_completion_history_has_backend_neutral_read_api(
    tmp_path,
) -> None:
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        job = _create_job(catalog)
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker",
            lease_duration_ms=60_000,
        )
        attempt = catalog.get_job_attempt(job.job_id, 1)
        assert catalog.list_job_attempts(job.job_id) == (attempt,)
        assert catalog.list_job_attempt_completions(job.job_id) == ()
        with pytest.raises(CatalogNotFoundError, match="completion not found"):
            catalog.get_job_attempt_completion(job.job_id, 1)

        catalog.complete_job_attempt(
            job.job_id,
            attempt_count=1,
            owner_id="worker",
            fencing_token=lease.fencing_token,
            outcome=IndexJobCompletion.REQUEUE,
            error_code="retry",
        )
        completion = catalog.get_job_attempt_completion(job.job_id, 1)
        assert catalog.list_job_attempt_completions(job.job_id) == (completion,)


def test_sql_execution_boundaries_reject_int64_overflow_before_sql(tmp_path) -> None:
    class IntSubclass(int):
        pass

    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        job = _create_job(catalog)
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker",
            lease_duration_ms=60_000,
        )
        queued = catalog.create_job(
            job.repository_id,
            job.source_revision_id,
            "duration-boundary",
            job.request,
            ref_name="duration-boundary",
        )
        too_large = 2**63
        with pytest.raises(CatalogValidationError, match="int64"):
            catalog.heartbeat_job_attempt(
                job.job_id,
                attempt_count=1,
                owner_id="worker",
                fencing_token=too_large,
                lease_duration_ms=10,
            )
        with pytest.raises(CatalogValidationError, match="int64"):
            catalog.append_job_event(
                job.job_id,
                attempt_count=1,
                owner_id="worker",
                fencing_token=too_large,
                event_key="overflow",
            )
        with pytest.raises(CatalogValidationError, match="int64"):
            catalog.list_job_events(job.job_id, after_sequence=too_large)
        with pytest.raises(CatalogValidationError, match="too large"):
            catalog.get_job_attempt(job.job_id, too_large)
        for invalid in (True, 1.0, IntSubclass(1), -(2**63)):
            with pytest.raises(CatalogValidationError, match="exact"):
                catalog.heartbeat_job_attempt(
                    job.job_id,
                    attempt_count=invalid,  # type: ignore[arg-type]
                    owner_id="worker",
                    fencing_token=lease.fencing_token,
                    lease_duration_ms=10,
                )
            with pytest.raises(CatalogValidationError, match="exact"):
                catalog.append_job_event(
                    job.job_id,
                    attempt_count=1,
                    owner_id="worker",
                    fencing_token=invalid,  # type: ignore[arg-type]
                    event_key=f"invalid-{type(invalid).__name__}",
                )
            with pytest.raises(CatalogValidationError, match="exact"):
                catalog.list_job_events(
                    job.job_id,
                    after_sequence=invalid,  # type: ignore[arg-type]
                )
            with pytest.raises(CatalogValidationError, match="exact"):
                catalog.get_job_attempt(
                    job.job_id,
                    invalid,  # type: ignore[arg-type]
                )
            with pytest.raises(CatalogValidationError, match="exact"):
                catalog.scan_runnable_jobs(limit=invalid)  # type: ignore[arg-type]
            with pytest.raises(CatalogValidationError, match="exact"):
                catalog.list_job_events(
                    job.job_id,
                    limit=invalid,  # type: ignore[arg-type]
                )
            with pytest.raises(CatalogValidationError, match="exact"):
                catalog.heartbeat_job_attempt(
                    job.job_id,
                    attempt_count=1,
                    owner_id="worker",
                    fencing_token=lease.fencing_token,
                    lease_duration_ms=invalid,  # type: ignore[arg-type]
                )
            with pytest.raises(CatalogValidationError, match="exact"):
                catalog.acquire_job_lease(
                    queued.job_id,
                    owner_id="queued-worker",
                    lease_duration_ms=invalid,  # type: ignore[arg-type]
                )
            with pytest.raises(CatalogValidationError, match="exact"):
                catalog.renew_job_lease(
                    job.job_id,
                    owner_id="worker",
                    fencing_token=lease.fencing_token,
                    lease_duration_ms=invalid,  # type: ignore[arg-type]
                )
        assert catalog.get_job(job.job_id).status is IndexJobStatus.RUNNING
        assert catalog.get_job(queued.job_id).status is IndexJobStatus.QUEUED
        assert catalog.list_job_attempts(queued.job_id) == ()
        assert lease.fencing_token == 1


def test_fencing_token_exhaustion_fails_without_starting_an_attempt(tmp_path) -> None:
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        first = _create_job(catalog, idempotency_key="first")
        second = catalog.create_job(
            first.repository_id,
            first.source_revision_id,
            "second",
            first.request,
        )
        lease = catalog.acquire_job_lease(
            first.job_id,
            owner_id="worker-1",
            lease_duration_ms=60_000,
        )
        catalog.complete_job_attempt(
            first.job_id,
            attempt_count=1,
            owner_id="worker-1",
            fencing_token=lease.fencing_token,
            outcome=IndexJobCompletion.FAILED,
            error_code="done",
        )
        _drop_and_restore_triggers(
            catalog._connection,
            ("ref_job_lease_updates_are_fenced",),
            """
            UPDATE ref_job_leases SET fencing_token = 9223372036854775807
            WHERE repository_id = ? AND ref_name = ?
            """,
            (second.repository_id, second.ref_name),
        )

        with pytest.raises(CatalogConflictError, match="token is exhausted"):
            catalog.acquire_job_lease(
                second.job_id,
                owner_id="worker-2",
                lease_duration_ms=60_000,
            )
        assert catalog.get_job(second.job_id).status is IndexJobStatus.QUEUED
        assert catalog.list_job_attempts(second.job_id) == ()


def test_expiry_takeover_records_requeue_before_new_attempt(tmp_path) -> None:
    path = tmp_path / "catalog.sqlite3"
    clock = {"ms": 100}
    with SQLiteCatalog(path) as catalog:
        _install_clock(catalog, clock)
        first = _create_job(catalog, idempotency_key="first")
        second = catalog.create_job(
            first.repository_id,
            first.source_revision_id,
            "second",
            first.request,
        )
        stale = catalog.acquire_job_lease(
            first.job_id,
            owner_id="worker-1",
            lease_duration_ms=10,
        )
        assert catalog.scan_runnable_jobs().jobs == ()

        clock["ms"] = stale.lease_expires_at_ms + 1_000
        retired_at_ms = catalog._db_now_ms()
        runnable = catalog.scan_runnable_jobs().jobs
        assert {job.job_id for job in runnable} == {first.job_id, second.job_id}
        current = catalog.acquire_job_lease(
            second.job_id,
            owner_id="worker-2",
            lease_duration_ms=30,
        )
        assert current.fencing_token == stale.fencing_token + 1
        assert catalog.get_job(first.job_id).status is IndexJobStatus.QUEUED
        completion = catalog._connection.execute(
            """
            SELECT outcome, error_code, completed_at_ms
            FROM index_job_attempt_completions
            WHERE job_id = ? AND attempt_count = 1
            """,
            (first.job_id,),
        ).fetchone()
        assert tuple(completion) == ("requeue", "lease_expired", retired_at_ms)
        assert (
            catalog._connection.execute(
                "SELECT COUNT(*) FROM index_job_attempts WHERE job_id = ?",
                (second.job_id,),
            ).fetchone()[0]
            == 1
        )


def test_runnable_scan_uses_stable_keyset_pages_and_is_advisory(tmp_path) -> None:
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        first = _create_job(catalog, idempotency_key="first")
        jobs = [first]
        for index in range(1, 4):
            jobs.append(
                catalog.create_job(
                    first.repository_id,
                    first.source_revision_id,
                    f"request-{index}",
                    first.request,
                    ref_name=f"ref-{index}",
                )
            )
        expected = tuple(sorted(jobs, key=lambda job: (job.created_at_ms, job.job_id)))
        cycle = catalog.begin_runnable_job_cycle()
        assert type(cycle) is IndexJobRunnableCycle
        first_page = catalog.scan_runnable_jobs(cycle=cycle, limit=2)
        assert first_page.jobs == expected[:2]
        assert first_page.next_cursor == IndexJobRunnableCursor(
            expected[1].created_at_ms,
            expected[1].job_id,
        )
        late = catalog.create_job(
            first.repository_id,
            first.source_revision_id,
            "request-late",
            first.request,
            ref_name="ref-late",
        )
        second_page = catalog.scan_runnable_jobs(
            cursor=first_page.next_cursor,
            cycle=cycle,
            limit=2,
        )
        assert second_page.jobs == expected[2:]
        assert second_page.next_cursor is None
        fresh_cycle = catalog.begin_runnable_job_cycle()
        assert (
            late
            in catalog.scan_runnable_jobs(
                cycle=fresh_cycle,
                limit=64,
            ).jobs
        )
        assert all(job.status is IndexJobStatus.QUEUED for job in jobs)
        with pytest.raises(CatalogValidationError, match="cannot exceed"):
            catalog.scan_runnable_jobs(limit=257)


def test_job_events_are_bounded_secret_free_replayable_and_paginated(
    tmp_path,
) -> None:
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        job = _create_job(catalog)
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker",
            lease_duration_ms=60_000,
        )

        class FalseyPayload(dict[str, object]):
            def __bool__(self) -> bool:
                return False

        payload = {"phase": ["capture"]}
        progress = catalog.append_job_event(
            job.job_id,
            attempt_count=1,
            owner_id="worker",
            fencing_token=lease.fencing_token,
            event_key="start",
            view_type="bm25",
            payload=payload,
        )
        payload["phase"].append("mutated")
        replay = catalog.append_job_event(
            job.job_id,
            attempt_count=1,
            owner_id="worker",
            fencing_token=lease.fencing_token,
            event_key="start",
            view_type="bm25",
            payload={"phase": ["capture"]},
        )
        assert replay == progress
        falsey = catalog.append_job_event(
            job.job_id,
            attempt_count=1,
            owner_id="worker",
            fencing_token=lease.fencing_token,
            event_key="falsey",
            payload=FalseyPayload(preserved=0),
        )
        assert falsey.payload == {"preserved": 0}
        result = catalog.record_job_view_result(
            job.job_id,
            attempt_count=1,
            owner_id="worker",
            fencing_token=lease.fencing_token,
            event_key="bm25-result",
            view_type="bm25",
            effective_mode=IndexJobEffectiveMode.FULL,
            outcome=IndexJobViewOutcome.SUCCEEDED,
            payload={"documents": 3},
        )
        assert result.sequence == falsey.sequence + 1
        with pytest.raises(CatalogConflictError, match="already has a result"):
            catalog.record_job_view_result(
                job.job_id,
                attempt_count=1,
                owner_id="worker",
                fencing_token=lease.fencing_token,
                event_key="different-result",
                view_type="bm25",
                effective_mode=IndexJobEffectiveMode.INCREMENTAL,
                outcome=IndexJobViewOutcome.FAILED,
            )
        with pytest.raises(StorageValidationError, match="secret field"):
            catalog.append_job_event(
                job.job_id,
                attempt_count=1,
                owner_id="worker",
                fencing_token=lease.fencing_token,
                event_key="secret",
                payload={"api_token": "hidden"},
            )

        for index in range(3, MAX_INDEX_JOB_EVENTS_PER_ATTEMPT):
            catalog.append_job_event(
                job.job_id,
                attempt_count=1,
                owner_id="worker",
                fencing_token=lease.fencing_token,
                event_key=f"progress-{index}",
                payload={"step": index},
            )
        assert len(catalog.list_job_events(job.job_id, limit=256)) == 256
        with pytest.raises(CatalogConflictError, match="capacity is exhausted"):
            catalog.append_job_event(
                job.job_id,
                attempt_count=1,
                owner_id="worker",
                fencing_token=lease.fencing_token,
                event_key="too-many",
            )
        page = catalog.list_job_events(
            job.job_id,
            after_sequence=progress.sequence,
            limit=1,
        )
        assert page == (falsey,)
        with pytest.raises(CatalogValidationError, match="cannot exceed"):
            catalog.list_job_events(job.job_id, limit=257)


def test_event_payload_callback_exception_is_wrapped_before_sql_mutation(
    tmp_path,
) -> None:
    class BrokenPayload(Mapping[str, object]):
        def __len__(self) -> int:
            return 1

        def __iter__(self) -> Iterator[str]:
            yield "value"

        def __getitem__(self, key: str) -> object:
            raise RuntimeError(key)

    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        job = _create_job(catalog)
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker",
            lease_duration_ms=60_000,
        )
        with pytest.raises(StorageValidationError, match="snapshotted"):
            catalog.append_job_event(
                job.job_id,
                attempt_count=1,
                owner_id="worker",
                fencing_token=lease.fencing_token,
                event_key="broken",
                payload=BrokenPayload(),
            )
        assert catalog.list_job_events(job.job_id) == ()


def test_plain_sql_cannot_replace_or_mutate_execution_history_rows(tmp_path) -> None:
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        job = _create_job(catalog)
        lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker",
            lease_duration_ms=60_000,
        )
        event = catalog.append_job_event(
            job.job_id,
            attempt_count=1,
            owner_id="worker",
            fencing_token=lease.fencing_token,
            event_key="start",
        )
        catalog.request_job_cancel(job.job_id)
        catalog.complete_job_attempt(
            job.job_id,
            attempt_count=1,
            owner_id="worker",
            fencing_token=lease.fencing_token,
            outcome=IndexJobCompletion.CANCELLED,
        )
        completion = tuple(
            catalog._connection.execute(
                """
                SELECT * FROM index_job_attempt_completions
                WHERE job_id = ? AND attempt_count = 1
                """,
                (job.job_id,),
            ).fetchone()
        )
        frontier = tuple(
            catalog._connection.execute(
                """
                SELECT * FROM index_job_attempt_closure_frontiers
                WHERE job_id = ? AND attempt_count = 1
                """,
                (job.job_id,),
            ).fetchone()
        )
        marker = tuple(
            catalog._connection.execute(
                "SELECT * FROM index_job_cancellation_requests WHERE job_id = ?",
                (job.job_id,),
            ).fetchone()
        )

    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("PRAGMA recursive_triggers = OFF")
        with pytest.raises(sqlite3.IntegrityError, match="index job attempt"):
            connection.execute(
                """
                INSERT OR REPLACE INTO index_job_attempts
                SELECT * FROM index_job_attempts
                WHERE job_id = ? AND attempt_count = 1
                """,
                (job.job_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT OR REPLACE INTO index_job_events(
                    event_sequence, job_id, attempt_count, event_key, kind,
                    owner_id, fencing_token, view_type, effective_mode, outcome,
                    payload_json, created_at_ms
                ) VALUES (?, ?, 1, 'start', 'progress', 'worker', ?, NULL,
                    NULL, NULL, '{}', ?)
                """,
                (
                    event.sequence + 100,
                    job.job_id,
                    lease.fencing_token,
                    event.created_at_ms,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT OR REPLACE INTO index_job_attempt_completions
                SELECT * FROM index_job_attempt_completions
                WHERE job_id = ? AND attempt_count = 1
                """,
                (job.job_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT OR REPLACE INTO index_job_attempt_closure_frontiers
                SELECT * FROM index_job_attempt_closure_frontiers
                WHERE job_id = ? AND attempt_count = 1
                """,
                (job.job_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT OR REPLACE INTO index_job_cancellation_requests
                SELECT * FROM index_job_cancellation_requests WHERE job_id = ?
                """,
                (job.job_id,),
            )
        for table, field in (
            ("index_job_attempt_completions", "error_code"),
            ("index_job_attempt_closure_frontiers", "event_count"),
            ("index_job_cancellation_requests", "requested_at_ms"),
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(
                    f"UPDATE {table} SET {field} = {field} WHERE job_id = ?",  # noqa: S608
                    (job.job_id,),
                )
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(
                    f"DELETE FROM {table} WHERE job_id = ?",  # noqa: S608
                    (job.job_id,),
                )
    finally:
        connection.close()

    with SQLiteCatalog(path, create=False) as catalog:
        assert (
            tuple(
                catalog._connection.execute(
                    """
                    SELECT * FROM index_job_attempt_completions
                    WHERE job_id = ? AND attempt_count = 1
                    """,
                    (job.job_id,),
                ).fetchone()
            )
            == completion
        )
        assert (
            tuple(
                catalog._connection.execute(
                    """
                    SELECT * FROM index_job_attempt_closure_frontiers
                    WHERE job_id = ? AND attempt_count = 1
                    """,
                    (job.job_id,),
                ).fetchone()
            )
            == frontier
        )
        assert (
            tuple(
                catalog._connection.execute(
                    "SELECT * FROM index_job_cancellation_requests WHERE job_id = ?",
                    (job.job_id,),
                ).fetchone()
            )
            == marker
        )
