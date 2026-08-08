# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for durable SQLite index jobs and fenced ref leases."""

from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from codenib.storage import sqlite_catalog as sqlite_catalog_module
from codenib.storage.models import (
    INDEX_JOB_REQUEST_CONTRACT,
    IndexJobCompletion,
    IndexJobRecord,
    IndexJobRequest,
    IndexJobRequestedMode,
    IndexJobStatus,
    StorageIntegrityError,
    StorageValidationError,
)
from codenib.storage.sqlite_catalog import (
    LATEST_SCHEMA_VERSION,
    CatalogConflictError,
    CatalogNotFoundError,
    SQLiteCatalog,
)


def _request(
    profile_id: str,
    *,
    view_type: str = "bm25",
    mode: str = "auto",
    required: bool = True,
) -> dict[str, object]:
    return {
        "contract": INDEX_JOB_REQUEST_CONTRACT,
        "views": {
            view_type: {
                "profile_id": profile_id,
                "requested_mode": mode,
                "required": required,
            }
        },
    }


def _repository(catalog: SQLiteCatalog, key: str = "owner/repo") -> tuple[str, str]:
    repository_id = catalog.create_repository(key)
    source_revision_id = catalog.create_source_revision(
        repository_id,
        commit_sha="a" * 40,
        tree_sha="b" * 64,
    )
    return repository_id, source_revision_id


def _job_setup(
    catalog: SQLiteCatalog,
    *,
    key: str = "owner/repo",
    idempotency_key: str = "request-1",
    max_attempts: int = 3,
) -> IndexJobRecord:
    repository_id, source_revision_id = _repository(catalog, key)
    profile_id = catalog.create_view_profile("bm25", {"tokenizer": "unicode61"})
    return catalog.create_job(
        repository_id,
        source_revision_id,
        idempotency_key,
        _request(profile_id),
        expected_ref_generation=0,
        max_attempts=max_attempts,
    )


def _publish_snapshot(
    catalog: SQLiteCatalog,
    job: IndexJobRecord,
    *,
    source_revision_id: str | None = None,
    suffix: str = "d",
    ref_name: str = "published",
) -> str:
    source = source_revision_id or job.source_revision_id
    profile_id = job.request["views"]["bm25"]["profile_id"]
    digest = catalog.register_object(
        suffix * 64,
        storage_key=f"sha256/{suffix * 2}/{suffix * 62}",
        byte_size=1,
    )
    view_id = catalog.stage_view_generation(
        job.repository_id,
        source,
        profile_id,
        "bm25",
        digest,
        schema_version="1",
    )
    return catalog.publish_snapshot(
        job.repository_id,
        source,
        [view_id],
        ref_name=ref_name,
        expected_generation=0,
    )["snapshot_id"]


def test_job_request_identity_is_canonical_and_idempotency_scoped_to_repo() -> None:
    repository_id = "repo_" + "a" * 64
    source_revision_id = "src_" + "b" * 64
    profile_id = "profile_" + "c" * 64
    first = IndexJobRequest.create(
        repository_id,
        source_revision_id,
        "client-request",
        _request(profile_id),
        ref_name="main",
        expected_ref_generation=4,
        max_attempts=5,
    )
    reordered = IndexJobRequest.create(
        repository_id,
        source_revision_id,
        "client-request",
        {
            "views": {
                "bm25": {
                    "required": True,
                    "requested_mode": "auto",
                    "profile_id": profile_id,
                }
            },
            "contract": INDEX_JOB_REQUEST_CONTRACT,
        },
        ref_name="main",
        expected_ref_generation=4,
        max_attempts=5,
    )
    other_ref = IndexJobRequest.create(
        repository_id,
        source_revision_id,
        "client-request",
        _request(profile_id),
        ref_name="release",
        expected_ref_generation=4,
        max_attempts=5,
    )

    assert first.job_id == reordered.job_id == other_ref.job_id
    assert first.request_digest == reordered.request_digest
    assert first.request_digest != other_ref.request_digest
    assert first.view_requests[0].requested_mode is IndexJobRequestedMode.AUTO


def test_job_request_rejects_secrets_unbounded_text_and_invalid_shape() -> None:
    base = {
        "repository_id": "repo_" + "a" * 64,
        "source_revision_id": "src_" + "b" * 64,
        "idempotency_key": "request",
    }
    profile_id = "profile_" + "c" * 64
    secret_request = _request(profile_id)
    secret_request["views"]["bm25"]["api_key"] = "must-not-persist"  # type: ignore[index]

    with pytest.raises(StorageValidationError, match="secret field"):
        IndexJobRequest.create(**base, request=secret_request)
    with pytest.raises(StorageValidationError, match="NUL"):
        IndexJobRequest.create(
            repository_id=base["repository_id"],
            source_revision_id=base["source_revision_id"],
            idempotency_key="bad\x00key",
            request=_request(profile_id),
        )
    with pytest.raises(StorageValidationError, match="maximum attempts"):
        IndexJobRequest.create(**base, request=_request(profile_id), max_attempts=0)
    with pytest.raises(StorageValidationError, match="int64"):
        IndexJobRequest.create(
            **base,
            request=_request(profile_id),
            expected_ref_generation=9_223_372_036_854_775_808,
        )
    with pytest.raises(StorageValidationError, match="must be canonical"):
        IndexJobRequest.create(**base, request=_request(f" {profile_id} "))
    with pytest.raises(StorageValidationError, match="must be canonical"):
        IndexJobRequest.create(
            **base,
            request=_request(profile_id, view_type=" bm25 "),
        )
    with pytest.raises(StorageValidationError, match="contain exactly"):
        IndexJobRequest.create(
            **base,
            request={"contract": INDEX_JOB_REQUEST_CONTRACT, "views": {}, "extra": 1},
        )


def test_job_record_canonicalizes_identity_fields_and_orders_timestamps() -> None:
    request = IndexJobRequest.create(
        "repo_" + "a" * 64,
        "src_" + "b" * 64,
        "request",
        _request("profile_" + "c" * 64),
    )
    record = IndexJobRecord(
        job_id=request.job_id,
        repository_id=f" {request.repository_id} ",
        source_revision_id=f" {request.source_revision_id} ",
        ref_name=" main ",
        idempotency_key=" request ",
        expected_ref_generation=0,
        max_attempts=3,
        request_json=request.request_json,
        request_digest=request.request_digest,
        status=IndexJobStatus.QUEUED,
        cancel_requested=False,
        attempt_count=0,
        result_snapshot_id=None,
        error_code=None,
        error_message=None,
        created_at_ms=10,
        updated_at_ms=10,
        started_at_ms=None,
        finished_at_ms=None,
    )

    assert record.repository_id == request.repository_id
    assert record.ref_name == "main"
    assert record.idempotency_key == "request"
    with pytest.raises(StorageValidationError, match="start time"):
        IndexJobRecord(
            **{
                field: getattr(record, field)
                for field in record.__dataclass_fields__
                if field
                not in {"status", "attempt_count", "updated_at_ms", "started_at_ms"}
            },
            status=IndexJobStatus.RUNNING,
            attempt_count=1,
            updated_at_ms=11,
            started_at_ms=9,
        )


def test_forward_migrates_v1_catalog_without_losing_existing_rows(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "catalog.sqlite3"
    monkeypatch.setattr(sqlite_catalog_module, "LATEST_SCHEMA_VERSION", 1)
    with SQLiteCatalog(path) as catalog:
        repository_id, _ = _repository(catalog)
        assert catalog.schema_version == 1

    monkeypatch.setattr(
        sqlite_catalog_module, "LATEST_SCHEMA_VERSION", LATEST_SCHEMA_VERSION
    )
    with SQLiteCatalog(path) as catalog:
        assert catalog.schema_version == 2
        assert catalog.create_repository("owner/repo") == repository_id
        assert (
            catalog._connection.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()[0]
            == 2
        )
        tables = {
            row[0]
            for row in catalog._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"index_jobs", "index_job_views", "ref_job_leases"} <= tables


def test_failed_v2_migration_rolls_back_as_one_transaction(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "catalog.sqlite3"
    monkeypatch.setattr(sqlite_catalog_module, "LATEST_SCHEMA_VERSION", 1)
    with SQLiteCatalog(path):
        pass

    original_migrations = sqlite_catalog_module._MIGRATIONS
    broken = dict(original_migrations)
    broken[2] = (
        "CREATE TABLE migration_must_rollback(value TEXT)",
        "THIS IS NOT VALID SQL",
    )
    monkeypatch.setattr(sqlite_catalog_module, "_MIGRATIONS", broken)
    monkeypatch.setattr(sqlite_catalog_module, "LATEST_SCHEMA_VERSION", 2)
    with pytest.raises(sqlite3.OperationalError):
        SQLiteCatalog(path)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = 'migration_must_rollback'"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_create_job_is_idempotent_and_conflicts_on_any_request_change(tmp_path) -> None:
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_revision_id = _repository(catalog)
        profile_id = catalog.create_view_profile("bm25", {})
        request = _request(profile_id)
        first = catalog.create_job(
            repository_id,
            source_revision_id,
            "request-1",
            request,
            ref_name="main",
            expected_ref_generation=2,
            max_attempts=4,
        )
        same = catalog.create_job(
            repository_id,
            source_revision_id,
            "request-1",
            {
                "views": request["views"],
                "contract": INDEX_JOB_REQUEST_CONTRACT,
            },
            ref_name="main",
            expected_ref_generation=2,
            max_attempts=4,
        )

        assert same == first
        assert catalog.get_job_views(first.job_id)[0].profile_id == profile_id
        with pytest.raises(StorageValidationError, match="must be canonical"):
            catalog.create_job(
                repository_id,
                source_revision_id,
                "request-1",
                _request(f" {profile_id} "),
                ref_name="main",
                expected_ref_generation=2,
                max_attempts=4,
            )
        for changed in (
            {"ref_name": "other"},
            {"expected_ref_generation": 3},
            {"max_attempts": 5},
            {"request": _request(profile_id, mode="full")},
        ):
            kwargs = {
                "ref_name": "main",
                "expected_ref_generation": 2,
                "max_attempts": 4,
                "request": request,
            }
            kwargs.update(changed)
            with pytest.raises(CatalogConflictError, match="idempotency key"):
                catalog.create_job(
                    repository_id,
                    source_revision_id,
                    "request-1",
                    **kwargs,
                )

        other_repository, other_source = _repository(catalog, "owner/other")
        other = catalog.create_job(
            other_repository,
            other_source,
            "request-1",
            request,
        )
        assert other.job_id != first.job_id
        int64_job = catalog.create_job(
            repository_id,
            source_revision_id,
            "int64-maximum",
            request,
            expected_ref_generation=9_223_372_036_854_775_807,
        )
        assert int64_job.expected_ref_generation == 9_223_372_036_854_775_807
        with pytest.raises(StorageValidationError, match="int64"):
            catalog.create_job(
                repository_id,
                source_revision_id,
                "int64-overflow",
                request,
                expected_ref_generation=9_223_372_036_854_775_808,
            )


def test_job_views_and_contract_are_revalidated_against_canonical_request(
    tmp_path,
) -> None:
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        job = _job_setup(catalog)
        with pytest.raises(sqlite3.IntegrityError, match="view requests are immutable"):
            catalog._connection.execute(
                "UPDATE index_job_views SET required = 0 WHERE job_id = ?",
                (job.job_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="view requests are immutable"):
            catalog._connection.execute(
                "DELETE FROM index_job_views WHERE job_id = ?", (job.job_id,)
            )
        with pytest.raises(sqlite3.IntegrityError, match="duplicate index job view"):
            catalog._connection.execute(
                """
                INSERT OR REPLACE INTO index_job_views(
                    job_id, view_type, profile_id, requested_mode, required
                )
                SELECT job_id, view_type, profile_id, requested_mode, required
                FROM index_job_views WHERE job_id = ?
                """,
                (job.job_id,),
            )
        vector_profile = catalog.create_view_profile("vector", {})
        with pytest.raises(sqlite3.IntegrityError, match="canonical request"):
            catalog._connection.execute(
                """
                INSERT INTO index_job_views(
                    job_id, view_type, profile_id, requested_mode, required
                ) VALUES (?, 'vector', ?, 'full', 1)
                """,
                (job.job_id, vector_profile),
            )
        with pytest.raises(sqlite3.IntegrityError, match="view requests are immutable"):
            catalog._connection.execute(
                "DELETE FROM index_jobs WHERE job_id = ?", (job.job_id,)
            )

        catalog._connection.execute("DROP TRIGGER index_job_views_are_immutable")
        catalog._connection.execute("DROP TRIGGER index_job_views_cannot_be_deleted")
        catalog._connection.execute(
            "DELETE FROM index_job_views WHERE job_id = ?", (job.job_id,)
        )
        with pytest.raises(StorageIntegrityError, match="canonical request"):
            catalog.get_job(job.job_id)

    with SQLiteCatalog(tmp_path / "contract.sqlite3") as catalog:
        job = _job_setup(catalog)
        catalog._connection.execute("DROP TRIGGER index_job_request_is_immutable")
        catalog._connection.execute(
            "UPDATE index_jobs SET request_contract = 'corrupt.v1' WHERE job_id = ?",
            (job.job_id,),
        )
        with pytest.raises(StorageIntegrityError, match="contract column"):
            catalog.get_job(job.job_id)


def test_two_connections_compete_for_one_ref_slot(tmp_path) -> None:
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        first = _job_setup(catalog, idempotency_key="first")
        second = catalog.create_job(
            first.repository_id,
            first.source_revision_id,
            "second",
            first.request,
        )

    barrier = threading.Barrier(2)

    def acquire(job_id: str, owner_id: str) -> tuple[str, object]:
        with SQLiteCatalog(path) as catalog:
            barrier.wait(timeout=5)
            try:
                return (
                    "acquired",
                    catalog.acquire_job_lease(
                        job_id,
                        owner_id=owner_id,
                        lease_duration_ms=60_000,
                    ),
                )
            except CatalogConflictError as exc:
                return "conflict", str(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda args: acquire(*args),
                ((first.job_id, "worker-1"), (second.job_id, "worker-2")),
            )
        )

    assert sorted(result[0] for result in results) == ["acquired", "conflict"]
    with SQLiteCatalog(path) as catalog:
        running = catalog._connection.execute(
            "SELECT COUNT(*) FROM index_jobs WHERE status = 'running'"
        ).fetchone()[0]
        assert running == 1


def test_acquire_retry_is_idempotent_but_other_owners_are_fenced(tmp_path) -> None:
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        job = _job_setup(catalog)
        queued = catalog.create_job(
            job.repository_id,
            job.source_revision_id,
            "queued-replacement",
            job.request,
        )
        first = catalog.acquire_job_lease(
            job.job_id, owner_id="worker-1", lease_duration_ms=60_000
        )
        retried = catalog.acquire_job_lease(
            job.job_id, owner_id="worker-1", lease_duration_ms=1
        )

        assert retried == first
        assert catalog.get_job(job.job_id).attempt_count == 1
        with pytest.raises(sqlite3.IntegrityError, match="duplicate ref job lease"):
            catalog._connection.execute(
                """
                INSERT OR REPLACE INTO ref_job_leases(
                    repository_id, ref_name, job_id, owner_id, fencing_token,
                    acquired_at_ms, heartbeat_at_ms, lease_expires_at_ms,
                    updated_at_ms
                )
                SELECT repository_id, ref_name, ?, 'replacement-owner', fencing_token,
                    acquired_at_ms, heartbeat_at_ms, lease_expires_at_ms,
                    updated_at_ms
                FROM ref_job_leases
                WHERE repository_id = ? AND ref_name = ?
                """,
                (queued.job_id, job.repository_id, job.ref_name),
            )
        unchanged = catalog._connection.execute(
            """
            SELECT owner_id, fencing_token FROM ref_job_leases
            WHERE repository_id = ? AND ref_name = ?
            """,
            (job.repository_id, job.ref_name),
        ).fetchone()
        assert (unchanged["owner_id"], unchanged["fencing_token"]) == (
            "worker-1",
            first.fencing_token,
        )
        with pytest.raises(CatalogConflictError, match="active"):
            catalog.acquire_job_lease(
                job.job_id,
                owner_id="worker-2",
                lease_duration_ms=60_000,
            )


@pytest.mark.parametrize(
    "enable_foreign_keys",
    [False, True],
    ids=["foreign-keys-off", "foreign-keys-on"],
)
def test_duplicate_insert_guards_block_replace_from_plain_connections(
    tmp_path,
    enable_foreign_keys: bool,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        job = _job_setup(catalog)
        first_lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker",
            lease_duration_ms=30_000,
        )
        running = catalog.get_job(job.job_id)
        requested_view = catalog.get_job_views(job.job_id)[0]

    def open_plain_connection() -> sqlite3.Connection:
        connection = sqlite3.connect(path, isolation_level=None)
        assert connection.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        if enable_foreign_keys:
            connection.execute("PRAGMA foreign_keys = ON")
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        return connection

    connection = open_plain_connection()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="duplicate index job"):
            connection.execute(
                """
                INSERT OR REPLACE INTO index_jobs(
                    job_id, repository_id, source_revision_id, ref_name,
                    idempotency_key, expected_ref_generation, max_attempts,
                    request_contract, request_json, request_digest, status,
                    cancel_requested, attempt_count, result_snapshot_id,
                    error_code, error_message, created_at_ms, updated_at_ms,
                    started_at_ms, finished_at_ms
                )
                SELECT
                    job_id, repository_id, source_revision_id, ref_name,
                    idempotency_key, expected_ref_generation, max_attempts,
                    request_contract, '{"contract":"tampered.v1","views":{}}',
                    'tampered-request-digest', 'queued', 0, 0, NULL,
                    NULL, NULL, created_at_ms, updated_at_ms, NULL, NULL
                FROM index_jobs WHERE job_id = ?
                """,
                (job.job_id,),
            )
        alternate_job_id = "job_" + "f" * 64
        assert alternate_job_id != job.job_id
        with pytest.raises(sqlite3.IntegrityError, match="duplicate index job"):
            connection.execute(
                """
                INSERT OR REPLACE INTO index_jobs(
                    job_id, repository_id, source_revision_id, ref_name,
                    idempotency_key, expected_ref_generation, max_attempts,
                    request_contract, request_json, request_digest, status,
                    cancel_requested, attempt_count, result_snapshot_id,
                    error_code, error_message, created_at_ms, updated_at_ms,
                    started_at_ms, finished_at_ms
                )
                SELECT
                    ?, repository_id, source_revision_id, ref_name,
                    idempotency_key, expected_ref_generation, max_attempts,
                    request_contract, request_json, request_digest, 'queued',
                    0, 0, NULL, NULL, NULL, created_at_ms, updated_at_ms,
                    NULL, NULL
                FROM index_jobs WHERE job_id = ?
                """,
                (alternate_job_id, job.job_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="duplicate index job view"):
            connection.execute(
                """
                INSERT OR REPLACE INTO index_job_views(
                    job_id, view_type, profile_id, requested_mode, required
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    requested_view.job_id,
                    requested_view.view_type,
                    requested_view.profile_id,
                    requested_view.requested_mode.value,
                    int(requested_view.required),
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT OR REPLACE INTO index_job_views(
                    job_id, view_type, profile_id, requested_mode, required
                ) VALUES (?, ?, ?, 'full', 0)
                """,
                (
                    requested_view.job_id,
                    requested_view.view_type,
                    requested_view.profile_id,
                ),
            )

        persisted_job = connection.execute(
            """
            SELECT status, attempt_count, request_json, request_digest
            FROM index_jobs WHERE job_id = ?
            """,
            (job.job_id,),
        ).fetchone()
        assert persisted_job == (
            "running",
            1,
            running.request_json,
            running.request_digest,
        )
        persisted_view = connection.execute(
            """
            SELECT profile_id, requested_mode, required
            FROM index_job_views WHERE job_id = ? AND view_type = ?
            """,
            (requested_view.job_id, requested_view.view_type),
        ).fetchone()
        assert persisted_view == (
            requested_view.profile_id,
            requested_view.requested_mode.value,
            int(requested_view.required),
        )
    finally:
        connection.close()

    with SQLiteCatalog(path) as catalog:
        requeued = catalog.finish_job_attempt(
            job.job_id,
            owner_id="worker",
            fencing_token=first_lease.fencing_token,
            outcome=IndexJobCompletion.REQUEUE,
            error_code="retry",
        )
        assert requeued.status is IndexJobStatus.QUEUED

    connection = open_plain_connection()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="duplicate ref job lease"):
            connection.execute(
                """
                INSERT OR REPLACE INTO ref_job_leases(
                    repository_id, ref_name, job_id, owner_id, fencing_token,
                    acquired_at_ms, heartbeat_at_ms, lease_expires_at_ms,
                    updated_at_ms
                ) VALUES (?, ?, NULL, NULL, 0, NULL, NULL, NULL, 0)
                """,
                (job.repository_id, job.ref_name),
            )
        persisted_slot = connection.execute(
            """
            SELECT job_id, owner_id, fencing_token FROM ref_job_leases
            WHERE repository_id = ? AND ref_name = ?
            """,
            (job.repository_id, job.ref_name),
        ).fetchone()
        assert persisted_slot == (None, None, first_lease.fencing_token)
    finally:
        connection.close()

    with SQLiteCatalog(path) as catalog:
        reacquired = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker",
            lease_duration_ms=30_000,
        )
        assert reacquired.fencing_token == first_lease.fencing_token + 1
        with pytest.raises(CatalogConflictError, match="fenced lease"):
            catalog.finish_job_attempt(
                job.job_id,
                owner_id="worker",
                fencing_token=first_lease.fencing_token,
                outcome=IndexJobCompletion.FAILED,
                error_code="stale_worker",
            )
        assert catalog.get_job(job.job_id).status is IndexJobStatus.RUNNING


def test_plain_connection_cannot_move_lease_via_job_unique_identity(
    tmp_path,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        job = _job_setup(catalog)
        first_lease = catalog.acquire_job_lease(
            job.job_id,
            owner_id="worker",
            lease_duration_ms=20,
        )

    connection = sqlite3.connect(path, isolation_level=None)
    try:
        assert connection.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        connection.execute(
            """
            UPDATE index_jobs
            SET status = 'queued', error_code = 'raw_half_state'
            WHERE job_id = ?
            """,
            (job.job_id,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="duplicate ref job lease"):
            connection.execute(
                """
                INSERT OR REPLACE INTO ref_job_leases(
                    repository_id, ref_name, job_id, owner_id, fencing_token,
                    acquired_at_ms, heartbeat_at_ms, lease_expires_at_ms,
                    updated_at_ms
                )
                SELECT repository_id, 'hijacked', job_id, 'hijacker',
                    fencing_token, acquired_at_ms, heartbeat_at_ms,
                    lease_expires_at_ms, updated_at_ms
                FROM ref_job_leases
                WHERE repository_id = ? AND ref_name = ?
                """,
                (job.repository_id, job.ref_name),
            )
        persisted_slot = connection.execute(
            """
            SELECT ref_name, job_id, owner_id, fencing_token
            FROM ref_job_leases WHERE repository_id = ?
            """,
            (job.repository_id,),
        ).fetchall()
        assert persisted_slot == [
            (
                job.ref_name,
                job.job_id,
                "worker",
                first_lease.fencing_token,
            )
        ]
    finally:
        connection.close()

    time.sleep(0.05)
    with SQLiteCatalog(path) as catalog:
        recovered = catalog.acquire_job_lease(
            job.job_id,
            owner_id="recovery-worker",
            lease_duration_ms=30_000,
        )
        assert recovered.ref_name == job.ref_name
        assert recovered.fencing_token == first_lease.fencing_token + 1
        assert catalog.get_job(job.job_id).attempt_count == 2
        assert (
            catalog._connection.execute(
                """
                SELECT COUNT(*) FROM ref_job_leases
                WHERE repository_id = ? AND ref_name = 'hijacked'
                """,
                (job.repository_id,),
            ).fetchone()[0]
            == 0
        )


def test_renew_requeue_restart_and_fencing_token_are_durable(tmp_path) -> None:
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        job = _job_setup(catalog)
        lease = catalog.acquire_job_lease(
            job.job_id, owner_id="worker-1", lease_duration_ms=30_000
        )
        renewed = catalog.renew_job_lease(
            job.job_id,
            owner_id="worker-1",
            fencing_token=lease.fencing_token,
            lease_duration_ms=60_000,
        )
        requeued = catalog.finish_job_attempt(
            job.job_id,
            owner_id="worker-1",
            fencing_token=lease.fencing_token,
            outcome=IndexJobCompletion.REQUEUE,
            error_code="transient_io",
            error_message="retry the immutable build",
        )

        assert renewed.fencing_token == lease.fencing_token
        assert renewed.acquired_at_ms == lease.acquired_at_ms
        assert renewed.heartbeat_at_ms >= lease.heartbeat_at_ms
        assert renewed.lease_expires_at_ms > lease.lease_expires_at_ms
        assert requeued.status is IndexJobStatus.QUEUED
        assert requeued.error_code == "transient_io"
        slot = catalog._connection.execute(
            "SELECT * FROM ref_job_leases WHERE repository_id = ? AND ref_name = 'main'",
            (job.repository_id,),
        ).fetchone()
        assert slot["job_id"] is None
        assert slot["fencing_token"] == lease.fencing_token

    with SQLiteCatalog(path) as catalog:
        next_lease = catalog.acquire_job_lease(
            job.job_id, owner_id="worker-2", lease_duration_ms=30_000
        )
        restarted = catalog.get_job(job.job_id)
        assert next_lease.fencing_token == lease.fencing_token + 1
        assert restarted.attempt_count == 2
        assert restarted.error_code is None


def test_one_millisecond_renewal_uses_strict_db_clock_boundary(tmp_path) -> None:
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        # Keep every lease check on SQLite's clock while making the exact
        # millisecond boundary deterministic.  A wall-clock loop can be
        # descheduled past a short lease before its first renewal under xdist.
        clock = {"raw_ms": 0}
        catalog._connection.create_function(
            "julianday",
            1,
            lambda _value: 2440587.5 + clock["raw_ms"] / 86_400_000,
        )
        assert catalog._db_now_ms() == 0

        job = _job_setup(catalog)
        lease = catalog.acquire_job_lease(
            job.job_id, owner_id="worker", lease_duration_ms=1
        )
        assert lease.heartbeat_at_ms == 0
        assert lease.lease_expires_at_ms == 1
        assert catalog._db_now_ms() == lease.lease_expires_at_ms - 1

        lease = catalog.renew_job_lease(
            job.job_id,
            owner_id="worker",
            fencing_token=lease.fencing_token,
            lease_duration_ms=1,
        )
        assert lease.heartbeat_at_ms == 0
        assert lease.lease_expires_at_ms == 2

        clock["raw_ms"] = 2
        assert catalog._db_now_ms() == lease.lease_expires_at_ms
        with pytest.raises(CatalogConflictError, match="expired"):
            catalog.renew_job_lease(
                job.job_id,
                owner_id="worker",
                fencing_token=lease.fencing_token,
                lease_duration_ms=1,
            )

    # The test clock is connection-local and cannot leak into a restarted
    # catalog or alter the persisted lease while the failed renewal rolls back.
    with SQLiteCatalog(path) as restarted:
        assert restarted._db_now_ms() > lease.lease_expires_at_ms
        persisted = restarted._connection.execute(
            "SELECT * FROM ref_job_leases WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()
        assert persisted is not None
        assert persisted["heartbeat_at_ms"] == lease.heartbeat_at_ms
        assert persisted["lease_expires_at_ms"] == lease.lease_expires_at_ms


def test_expired_lease_cannot_mutate_and_takeover_atomically_requeues(tmp_path) -> None:
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as first_catalog:
        first = _job_setup(first_catalog, idempotency_key="first")
        second = first_catalog.create_job(
            first.repository_id,
            first.source_revision_id,
            "second",
            first.request,
        )
        stale = first_catalog.acquire_job_lease(
            first.job_id, owner_id="worker-1", lease_duration_ms=1
        )

    time.sleep(0.02)
    with SQLiteCatalog(path) as catalog:
        with pytest.raises(sqlite3.IntegrityError, match="fencing transition"):
            catalog._connection.execute(
                """
                UPDATE ref_job_leases
                SET heartbeat_at_ms = heartbeat_at_ms,
                    lease_expires_at_ms = lease_expires_at_ms + 10000,
                    updated_at_ms = updated_at_ms
                WHERE job_id = ?
                """,
                (first.job_id,),
            )
        with pytest.raises(CatalogConflictError, match="expired"):
            catalog.renew_job_lease(
                first.job_id,
                owner_id="worker-1",
                fencing_token=stale.fencing_token,
                lease_duration_ms=10_000,
            )
        with pytest.raises(CatalogConflictError, match="unexpired"):
            catalog.finish_job_attempt(
                first.job_id,
                owner_id="worker-1",
                fencing_token=stale.fencing_token,
                outcome=IndexJobCompletion.FAILED,
                error_code="stale_worker",
            )

        current = catalog.acquire_job_lease(
            second.job_id, owner_id="worker-2", lease_duration_ms=30_000
        )
        retired = catalog.get_job(first.job_id)
        assert current.fencing_token == stale.fencing_token + 1
        assert retired.status is IndexJobStatus.QUEUED
        assert retired.error_code == "lease_expired"


@pytest.mark.parametrize("raw_status", ["queued", "failed", "cancelled"])
def test_expired_nonrunning_half_state_is_reclaimed_without_fencing_aba(
    tmp_path,
    raw_status: str,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        first = _job_setup(catalog, idempotency_key="first")
        second = catalog.create_job(
            first.repository_id,
            first.source_revision_id,
            "second",
            first.request,
        )
        stale = catalog.acquire_job_lease(
            first.job_id, owner_id="worker-1", lease_duration_ms=100
        )
        now_ms = catalog._db_now_ms()
        if raw_status == "queued":
            catalog._connection.execute(
                """
                UPDATE index_jobs
                SET status = 'queued', error_code = 'raw_half_state',
                    updated_at_ms = ?
                WHERE job_id = ?
                """,
                (now_ms, first.job_id),
            )
        else:
            catalog._connection.execute(
                """
                UPDATE index_jobs
                SET status = ?, cancel_requested = ?,
                    error_code = 'raw_half_state', finished_at_ms = ?,
                    updated_at_ms = ?
                WHERE job_id = ?
                """,
                (
                    raw_status,
                    int(raw_status == "cancelled"),
                    now_ms,
                    now_ms,
                    first.job_id,
                ),
            )
        with pytest.raises(CatalogConflictError, match="active"):
            catalog.acquire_job_lease(
                second.job_id,
                owner_id="worker-2",
                lease_duration_ms=30_000,
            )

    time.sleep(0.12)
    with SQLiteCatalog(path) as catalog:
        current = catalog.acquire_job_lease(
            second.job_id, owner_id="worker-2", lease_duration_ms=30_000
        )
        assert current.fencing_token == stale.fencing_token + 1
        assert catalog.get_job(first.job_id).status.value == raw_status
        with pytest.raises(CatalogConflictError, match="fenced lease"):
            catalog.finish_job_attempt(
                second.job_id,
                owner_id="worker-1",
                fencing_token=stale.fencing_token,
                outcome=IndexJobCompletion.FAILED,
                error_code="stale_worker",
            )
        assert catalog.get_job(second.job_id).status is IndexJobStatus.RUNNING


def test_queued_and_running_cancellation_have_distinct_semantics(tmp_path) -> None:
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        queued = _job_setup(catalog, idempotency_key="queued")
        cancelled = catalog.request_job_cancel(queued.job_id)
        assert cancelled.status is IndexJobStatus.CANCELLED
        assert cancelled.cancel_requested
        assert catalog.request_job_cancel(queued.job_id) == cancelled
        with pytest.raises(CatalogConflictError, match="terminal"):
            catalog.acquire_job_lease(
                queued.job_id, owner_id="worker", lease_duration_ms=30_000
            )

        running = catalog.create_job(
            queued.repository_id,
            queued.source_revision_id,
            "running",
            queued.request,
            ref_name="other",
        )
        lease = catalog.acquire_job_lease(
            running.job_id, owner_id="worker", lease_duration_ms=30_000
        )

    with SQLiteCatalog(path) as requester:
        requested = requester.request_job_cancel(running.job_id)
        assert requested.status is IndexJobStatus.RUNNING
        assert requested.cancel_requested

    with SQLiteCatalog(path) as catalog:
        with pytest.raises(CatalogConflictError, match="cannot be requeued"):
            catalog.finish_job_attempt(
                running.job_id,
                owner_id="worker",
                fencing_token=lease.fencing_token,
                outcome=IndexJobCompletion.REQUEUE,
                error_code="retry",
            )
        finished = catalog.finish_job_attempt(
            running.job_id,
            owner_id="worker",
            fencing_token=lease.fencing_token,
            outcome=IndexJobCompletion.CANCELLED,
        )
        assert finished.status is IndexJobStatus.CANCELLED
        assert finished.finished_at_ms is not None


def test_final_attempt_cannot_requeue_and_failed_jobs_are_terminal(tmp_path) -> None:
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        job = _job_setup(catalog, max_attempts=1)
        lease = catalog.acquire_job_lease(
            job.job_id, owner_id="worker", lease_duration_ms=30_000
        )
        with pytest.raises(CatalogConflictError, match="must fail"):
            catalog.finish_job_attempt(
                job.job_id,
                owner_id="worker",
                fencing_token=lease.fencing_token,
                outcome=IndexJobCompletion.REQUEUE,
                error_code="retry",
            )
        failed = catalog.finish_job_attempt(
            job.job_id,
            owner_id="worker",
            fencing_token=lease.fencing_token,
            outcome=IndexJobCompletion.FAILED,
            error_code="builder_failed",
            error_message="bounded diagnostic",
        )
        assert failed.status is IndexJobStatus.FAILED
        with pytest.raises(CatalogConflictError, match="terminal"):
            catalog.acquire_job_lease(
                job.job_id, owner_id="other", lease_duration_ms=30_000
            )
        with pytest.raises(
            sqlite3.IntegrityError, match="terminal.*immutable|status transition"
        ):
            catalog._connection.execute(
                "UPDATE index_jobs SET status = 'queued' WHERE job_id = ?",
                (job.job_id,),
            )


def test_m1_rejects_success_and_result_snapshot_binds_full_source_identity(
    tmp_path,
) -> None:
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        job = _job_setup(catalog)
        correct_snapshot = _publish_snapshot(catalog, job)
        catalog.acquire_job_lease(
            job.job_id, owner_id="worker", lease_duration_ms=30_000
        )
        now_ms = catalog._db_now_ms()

        with pytest.raises(sqlite3.IntegrityError, match="M1"):
            catalog._connection.execute(
                """
                UPDATE index_jobs
                SET status = 'succeeded', result_snapshot_id = ?,
                    finished_at_ms = ?, updated_at_ms = ?
                WHERE job_id = ?
                """,
                (correct_snapshot, now_ms, now_ms, job.job_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="M1"):
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
                SELECT
                    'job_inserted_success', repository_id, source_revision_id,
                    ref_name, 'inserted-success', expected_ref_generation,
                    max_attempts, request_contract, request_json, request_digest,
                    'succeeded', 0, attempt_count, ?, NULL, NULL,
                    created_at_ms, ?, started_at_ms, ?
                FROM index_jobs WHERE job_id = ?
                """,
                (correct_snapshot, now_ms, now_ms, job.job_id),
            )

        other_source = catalog.create_source_revision(
            job.repository_id,
            commit_sha="c" * 40,
            tree_sha="e" * 64,
        )
        wrong_snapshot = _publish_snapshot(
            catalog,
            job,
            source_revision_id=other_source,
            suffix="e",
            ref_name="other-published",
        )
        catalog._connection.execute(
            "DROP TRIGGER m1_index_jobs_cannot_update_succeeded"
        )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            catalog._connection.execute(
                """
                UPDATE index_jobs
                SET status = 'succeeded', result_snapshot_id = ?,
                    finished_at_ms = ?, updated_at_ms = ?
                WHERE job_id = ?
                """,
                (wrong_snapshot, now_ms, now_ms, job.job_id),
            )
        assert catalog.get_job(job.job_id).status is IndexJobStatus.RUNNING


def test_job_finish_and_slot_release_roll_back_together(tmp_path) -> None:
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        job = _job_setup(catalog)
        lease = catalog.acquire_job_lease(
            job.job_id, owner_id="worker", lease_duration_ms=30_000
        )
        catalog._connection.execute("""
            CREATE TRIGGER fail_job_slot_release
            BEFORE UPDATE ON ref_job_leases
            WHEN NEW.job_id IS NULL
            BEGIN
                SELECT RAISE(ABORT, 'simulated slot release failure');
            END
            """)

        with pytest.raises(sqlite3.IntegrityError, match="simulated slot"):
            catalog.finish_job_attempt(
                job.job_id,
                owner_id="worker",
                fencing_token=lease.fencing_token,
                outcome=IndexJobCompletion.FAILED,
                error_code="builder_failed",
            )

        still_running = catalog.get_job(job.job_id)
        assert still_running.status is IndexJobStatus.RUNNING
        assert still_running.error_code is None
        assert (
            catalog._connection.execute(
                "SELECT job_id FROM ref_job_leases WHERE repository_id = ?",
                (job.repository_id,),
            ).fetchone()["job_id"]
            == job.job_id
        )


def test_db_clock_drives_job_and_lease_times_despite_python_clock_skew(
    tmp_path, monkeypatch
) -> None:
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_revision_id = _repository(catalog)
        profile_id = catalog.create_view_profile("bm25", {})
        before_ms = catalog._db_now_ms()
        monkeypatch.setattr(
            sqlite_catalog_module,
            "_now",
            lambda: "1900-01-01T00:00:00+00:00",
        )
        job = catalog.create_job(
            repository_id,
            source_revision_id,
            "clock-skew",
            _request(profile_id),
        )
        lease = catalog.acquire_job_lease(
            job.job_id, owner_id="worker", lease_duration_ms=10_000
        )
        after_ms = catalog._db_now_ms()

        assert before_ms <= job.created_at_ms <= after_ms
        assert before_ms <= lease.acquired_at_ms <= after_ms
        assert lease.lease_expires_at_ms > lease.heartbeat_at_ms


def test_direct_sql_cannot_skip_job_or_lease_state_machine(tmp_path) -> None:
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        job = _job_setup(catalog)
        with pytest.raises(sqlite3.IntegrityError, match="status transition"):
            catalog._connection.execute(
                """
                UPDATE index_jobs
                SET status = 'running', attempt_count = 1,
                    started_at_ms = updated_at_ms
                WHERE job_id = ?
                """,
                (job.job_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="status transition"):
            catalog._connection.execute(
                """
                UPDATE index_jobs
                SET status = 'failed', error_code = 'unsafe',
                    finished_at_ms = updated_at_ms
                WHERE job_id = ?
                """,
                (job.job_id,),
            )
        lease = catalog.acquire_job_lease(
            job.job_id, owner_id="worker", lease_duration_ms=30_000
        )
        with pytest.raises(sqlite3.IntegrityError, match="fencing transition"):
            catalog._connection.execute(
                """
                UPDATE ref_job_leases
                SET owner_id = 'thief', fencing_token = fencing_token + 1,
                    acquired_at_ms = heartbeat_at_ms,
                    heartbeat_at_ms = heartbeat_at_ms,
                    lease_expires_at_ms = lease_expires_at_ms + 1
                WHERE job_id = ?
                """,
                (job.job_id,),
            )
        assert catalog.get_job(job.job_id).status is IndexJobStatus.RUNNING
        assert lease.fencing_token == 1


def test_missing_job_uses_catalog_not_found_error(tmp_path) -> None:
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        with pytest.raises(CatalogNotFoundError, match="index job"):
            catalog.get_job("job_" + "f" * 64)
