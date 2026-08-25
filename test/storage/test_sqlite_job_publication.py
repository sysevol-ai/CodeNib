# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Permanent SQLite v5 tests for atomic fenced job publication."""

from __future__ import annotations

import dis
import hashlib
import json
import sqlite3
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Event
from types import FrameType
from typing import Any

import pytest

from codenib.storage import sqlite_catalog as sqlite_catalog_module
from codenib.storage.models import (
    INDEX_JOB_PUBLICATION_CONTRACT,
    INDEX_JOB_REQUEST_CONTRACT,
    MAX_VIEW_GENERATION_MEMBERS,
    IndexJobRecord,
    IndexJobStatus,
    IndexJobViewOutput,
    ObjectRecord,
    StorageIntegrityError,
    StorageValidationError,
    canonical_json,
)
from codenib.storage.sqlite_catalog import (
    LATEST_SCHEMA_VERSION,
    CatalogConflictError,
    CatalogValidationError,
    SQLiteCatalog,
)


@dataclass(frozen=True)
class _JobFixture:
    job: IndexJobRecord
    owner_id: str
    fencing_token: int
    profiles: dict[str, str]


def _warm_transaction_opcode_tracing(catalog: SQLiteCatalog) -> None:
    """Warm traced generator/settlement paths before a one-shot injection."""

    def trace(frame: FrameType, event: str, _arg: object):
        if event == "call":
            frame.f_trace_opcodes = True
        return trace

    previous = sys.gettrace()
    sys.settrace(trace)
    try:
        with catalog._transaction(immediate=False):
            catalog._connection.execute("SELECT 1").fetchone()
    finally:
        sys.settrace(previous)


@contextmanager
def _inject_commit_successor(
    catalog: SQLiteCatalog,
    error: BaseException,
) -> Iterator[None]:
    _warm_transaction_opcode_tracing(catalog)
    function = sqlite_catalog_module._sqlite_transaction_pass
    instructions = tuple(dis.get_instructions(function))
    load_index = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname in {"LOAD_ATTR", "LOAD_METHOD"}
        and instruction.argval == "commit"
    )
    call_index = next(
        index
        for index in range(load_index + 1, len(instructions))
        if instructions[index].opname.startswith("CALL")
    )
    successor = instructions[call_index + 1].offset
    fired = False

    def trace(frame: FrameType, event: str, _arg: object):
        nonlocal fired
        if event == "call" and frame.f_code is function.__code__:
            frame.f_trace_opcodes = True
            return trace
        if (
            event == "opcode"
            and frame.f_code is function.__code__
            and frame.f_lasti == successor
            and not fired
        ):
            fired = True
            raise error
        return trace

    previous = sys.gettrace()
    sys.settrace(trace)
    try:
        yield
    finally:
        sys.settrace(previous)
        assert fired


def _object(
    value: int, *, media_type: str = "application/x-test-index"
) -> ObjectRecord:
    digest = f"{value:064x}"
    return ObjectRecord(
        digest=digest,
        byte_size=value + 1,
        storage_key=f"sha256/{digest[:2]}/{digest[2:]}",
        media_type=media_type,
    )


def _output(
    view_type: str,
    profile_id: str,
    value: int,
    *,
    members: tuple[ObjectRecord, ...] = (),
) -> IndexJobViewOutput:
    return IndexJobViewOutput.create(
        view_type,
        profile_id,
        _object(value),
        schema_version=f"{view_type}.v1",
        metadata={"builder": "deterministic", "value": value},
        member_object_records=members,
    )


def _request(
    profiles: dict[str, str],
    *,
    required: frozenset[str] = frozenset({"bm25"}),
) -> dict[str, object]:
    return {
        "contract": INDEX_JOB_REQUEST_CONTRACT,
        "views": {
            view_type: {
                "profile_id": profile_id,
                "requested_mode": "auto",
                "required": view_type in required,
            }
            for view_type, profile_id in profiles.items()
        },
    }


def _repository(catalog: SQLiteCatalog) -> tuple[str, str]:
    repository_id = catalog.create_repository("owner/job-publication")
    source_revision_id = catalog.create_source_revision(
        repository_id,
        commit_sha="a" * 40,
        tree_sha="b" * 64,
    )
    return repository_id, source_revision_id


def _profiles(
    catalog: SQLiteCatalog,
    view_types: tuple[str, ...] = ("bm25", "vector"),
) -> dict[str, str]:
    return {
        view_type: catalog.create_view_profile(
            view_type,
            {"view": view_type, "version": 1},
        )
        for view_type in view_types
    }


def _create_running_job(
    catalog: SQLiteCatalog,
    *,
    repository_id: str | None = None,
    source_revision_id: str | None = None,
    profiles: dict[str, str] | None = None,
    required: frozenset[str] = frozenset({"bm25"}),
    expected_ref_generation: int = 0,
    idempotency_key: str = "request-1",
    owner_id: str = "worker-1",
) -> _JobFixture:
    if repository_id is None or source_revision_id is None:
        repository_id, source_revision_id = _repository(catalog)
    if profiles is None:
        profiles = _profiles(catalog)
    job = catalog.create_job(
        repository_id,
        source_revision_id,
        idempotency_key,
        _request(profiles, required=required),
        expected_ref_generation=expected_ref_generation,
    )
    lease = catalog.acquire_job_lease(
        job.job_id,
        owner_id=owner_id,
        lease_duration_ms=60_000,
    )
    return _JobFixture(job, owner_id, lease.fencing_token, profiles)


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


def _direct_publish_output(
    catalog: SQLiteCatalog,
    job: IndexJobRecord,
    output: IndexJobViewOutput,
    *,
    expected_generation: int,
) -> dict[str, Any]:
    for record in (output.object_record, *output.member_object_records):
        catalog.register_object(
            record.digest,
            storage_key=record.storage_key,
            byte_size=record.byte_size,
            media_type=record.media_type,
        )
    generation_id = catalog.stage_view_generation(
        job.repository_id,
        job.source_revision_id,
        output.profile_id,
        output.view_type,
        output.object_record.digest,
        schema_version=output.schema_version,
        metadata=output.metadata,
        member_object_digests=tuple(
            member.digest for member in output.member_object_records
        ),
    )
    return catalog.publish_snapshot(
        job.repository_id,
        job.source_revision_id,
        (generation_id,),
        ref_name=job.ref_name,
        expected_generation=expected_generation,
    )


_PUBLICATION_TABLES = (
    "objects",
    "view_generations",
    "view_generation_objects",
    "snapshots",
    "snapshot_views",
    "refs",
    "index_job_publications",
    "index_job_attempt_closure_frontiers",
    "index_job_execution_clock",
    "index_jobs",
    "ref_job_leases",
)


def _publication_state(
    catalog: SQLiteCatalog,
) -> dict[str, tuple[tuple[Any, ...], ...]]:
    return {
        table: tuple(
            tuple(row)
            for row in catalog._connection.execute(
                f"SELECT * FROM {table} ORDER BY rowid"  # noqa: S608 - fixed allowlist
            ).fetchall()
        )
        for table in _PUBLICATION_TABLES
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


def _publication_row(catalog: SQLiteCatalog, job_id: str) -> sqlite3.Row:
    row = catalog._connection.execute(
        "SELECT * FROM index_job_publications WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert row is not None
    return row


@contextmanager
def _raw_table_corruption(
    catalog: SQLiteCatalog,
    table: str,
) -> Iterator[sqlite3.Connection]:
    """Temporarily bypass table guards, then restore the canonical schema."""

    triggers = tuple(
        catalog._connection.execute(
            """
            SELECT name, sql FROM sqlite_master
            WHERE type = 'trigger' AND tbl_name = ? ORDER BY name
            """,
            (table,),
        ).fetchall()
    )
    catalog._connection.execute("PRAGMA foreign_keys = OFF")
    catalog._connection.execute("PRAGMA ignore_check_constraints = ON")
    try:
        for trigger in triggers:
            catalog._connection.execute(f"DROP TRIGGER {trigger['name']!r}")
        yield catalog._connection
    finally:
        for trigger in triggers:
            catalog._connection.execute(trigger["sql"])
        catalog._connection.execute("PRAGMA ignore_check_constraints = OFF")
        catalog._connection.execute("PRAGMA foreign_keys = ON")


def test_required_output_publishes_one_atomic_identity_closed_snapshot(
    tmp_path,
) -> None:
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        fixture = _create_running_job(catalog)
        output = _output("bm25", fixture.profiles["bm25"], 100)

        completed = catalog.publish_job_outputs(
            fixture.job.job_id,
            owner_id=fixture.owner_id,
            fencing_token=fixture.fencing_token,
            outputs=(output,),
        )

        assert completed.status is IndexJobStatus.SUCCEEDED
        assert completed.result_snapshot_id is not None
        assert completed.finished_at_ms == completed.updated_at_ms
        resolved = catalog.resolve_ref(fixture.job.repository_id)
        assert resolved["generation"] == 1
        assert resolved["snapshot_id"] == completed.result_snapshot_id
        assert tuple(resolved["manifest"]["views"]) == ("bm25",)
        assert resolved["manifest"]["views"]["bm25"]["object"] == {
            "digest": output.object_record.digest,
            "storage_key": output.object_record.storage_key,
            "byte_size": output.object_record.byte_size,
            "media_type": output.object_record.media_type,
        }
        publication = _publication_row(catalog, fixture.job.job_id)
        closure = json.loads(publication["closure_json"])
        assert canonical_json(closure) == publication["closure_json"]
        assert hashlib.sha256(publication["closure_json"].encode()).hexdigest() == (
            publication["closure_digest"]
        )
        assert closure["job_id"] == fixture.job.job_id
        assert closure["contract"] == INDEX_JOB_PUBLICATION_CONTRACT
        assert closure["owner_id"] == fixture.owner_id
        assert closure["fencing_token"] == fixture.fencing_token
        assert closure["snapshot_id"] == completed.result_snapshot_id
        assert closure["ref_generation"] == 1
        assert closure["ref_changed"] is True
        assert closure["ref_updated_at"] == publication["ref_updated_at"]
        assert [item["view_type"] for item in closure["outputs"]] == ["bm25"]
        assert publication["completed_at_ms"] == completed.finished_at_ms
        lease = catalog._connection.execute(
            "SELECT * FROM ref_job_leases WHERE repository_id = ? AND ref_name = ?",
            (fixture.job.repository_id, fixture.job.ref_name),
        ).fetchone()
        assert lease["job_id"] is None
        assert lease["fencing_token"] == fixture.fencing_token


def test_publication_clock_rollback_preserves_the_open_attempt_and_outputs(
    tmp_path,
) -> None:
    path = tmp_path / "publication-clock.sqlite3"
    clock = {"ms": 1_000}
    with SQLiteCatalog(path) as catalog:
        _install_clock(catalog, clock)
        fixture = _create_running_job(catalog)
        output = _output("bm25", fixture.profiles["bm25"], 100)
        clock["ms"] = 3_000
        event = catalog.append_job_event(
            fixture.job.job_id,
            attempt_count=1,
            owner_id=fixture.owner_id,
            fencing_token=fixture.fencing_token,
            event_key="ready",
        )
        before = _publication_state(catalog)

        _install_stepping_clock(catalog, 4_000, 2_500)
        with pytest.raises(CatalogConflictError, match="clock moved backwards"):
            catalog.publish_job_outputs(
                fixture.job.job_id,
                owner_id=fixture.owner_id,
                fencing_token=fixture.fencing_token,
                outputs=(output,),
            )
        assert _publication_state(catalog) == before
        assert (
            catalog._connection.execute(
                "SELECT COUNT(*) FROM index_job_attempt_closure_frontiers"
            ).fetchone()[0]
            == 0
        )
        assert catalog.get_job(fixture.job.job_id).status is IndexJobStatus.RUNNING

        clock["ms"] = 4_000
        _install_clock(catalog, clock)
        completed = catalog.publish_job_outputs(
            fixture.job.job_id,
            owner_id=fixture.owner_id,
            fencing_token=fixture.fencing_token,
            outputs=(output,),
        )
        frontier = catalog._connection.execute(
            """
            SELECT event_count, max_event_sequence, max_event_created_at_ms
            FROM index_job_attempt_closure_frontiers
            WHERE job_id = ? AND attempt_count = 1
            """,
            (fixture.job.job_id,),
        ).fetchone()
        assert tuple(frontier) == (1, event.sequence, event.created_at_ms)
        assert completed.status is IndexJobStatus.SUCCEEDED

    with SQLiteCatalog(path, create=False) as catalog:
        assert catalog.get_job(fixture.job.job_id).status is IndexJobStatus.SUCCEEDED


def test_publication_reopens_and_replays_during_wall_clock_rollback(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "publication-restart-clock.sqlite3"
    clock = {"ms": 1_000}
    with SQLiteCatalog(path) as catalog:
        _install_clock(catalog, clock)
        fixture = _create_running_job(catalog)
        output = _output("bm25", fixture.profiles["bm25"], 100)
        clock["ms"] = 3_000
        completed = catalog.publish_job_outputs(
            fixture.job.job_id,
            owner_id=fixture.owner_id,
            fencing_token=fixture.fencing_token,
            outputs=(output,),
        )
        high_water_ms = catalog._connection.execute(
            "SELECT high_water_ms FROM index_job_execution_clock"
        ).fetchone()[0]

    clock["ms"] = 2_500
    _patch_connection_clock(monkeypatch, clock)
    with SQLiteCatalog(path, create=False) as catalog:
        before = _publication_state(catalog)
        assert (
            catalog.publish_job_outputs(
                fixture.job.job_id,
                owner_id=fixture.owner_id,
                fencing_token=fixture.fencing_token,
                outputs=(output,),
            )
            == completed
        )
        assert _publication_state(catalog) == before
        assert (
            catalog._connection.execute(
                "SELECT high_water_ms FROM index_job_execution_clock"
            ).fetchone()[0]
            == high_water_ms
        )


def test_equal_time_event_after_publication_is_detected_by_frontier(tmp_path) -> None:
    path = tmp_path / "post-publication-event.sqlite3"
    clock = {"ms": 1_000}
    with SQLiteCatalog(path) as catalog:
        _install_clock(catalog, clock)
        fixture = _create_running_job(catalog)
        output = _output("bm25", fixture.profiles["bm25"], 100)
        first = catalog.append_job_event(
            fixture.job.job_id,
            attempt_count=1,
            owner_id=fixture.owner_id,
            fencing_token=fixture.fencing_token,
            event_key="before-close",
        )
        completed = catalog.publish_job_outputs(
            fixture.job.job_id,
            owner_id=fixture.owner_id,
            fencing_token=fixture.fencing_token,
            outputs=(output,),
        )
        assert completed.finished_at_ms == first.created_at_ms
        with _raw_table_corruption(catalog, "index_job_events") as connection:
            connection.execute(
                """
                INSERT INTO index_job_events(
                    job_id, attempt_count, event_key, kind, owner_id,
                    fencing_token, view_type, effective_mode, outcome,
                    payload_json, created_at_ms
                ) VALUES (?, 1, 'after-close', 'progress', ?, ?, NULL,
                    NULL, NULL, '{}', ?)
                """,
                (
                    fixture.job.job_id,
                    fixture.owner_id,
                    fixture.fencing_token,
                    completed.finished_at_ms,
                ),
            )

    with pytest.raises(CatalogConflictError, match="frontier"):
        SQLiteCatalog(path, create=False)


def test_success_replay_authenticates_the_exact_event_frontier(tmp_path) -> None:
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        fixture = _create_running_job(catalog)
        output = _output("bm25", fixture.profiles["bm25"], 100)
        catalog.append_job_event(
            fixture.job.job_id,
            attempt_count=1,
            owner_id=fixture.owner_id,
            fencing_token=fixture.fencing_token,
            event_key="ready",
        )
        catalog.publish_job_outputs(
            fixture.job.job_id,
            owner_id=fixture.owner_id,
            fencing_token=fixture.fencing_token,
            outputs=(output,),
        )
        with _raw_table_corruption(
            catalog,
            "index_job_attempt_closure_frontiers",
        ) as connection:
            connection.execute(
                """
                UPDATE index_job_attempt_closure_frontiers
                SET event_count = 0, max_event_sequence = 0,
                    max_event_created_at_ms = 0
                WHERE job_id = ? AND attempt_count = 1
                """,
                (fixture.job.job_id,),
            )
        with pytest.raises(CatalogConflictError, match="frontier"):
            catalog.publish_job_outputs(
                fixture.job.job_id,
                owner_id=fixture.owner_id,
                fencing_token=fixture.fencing_token,
                outputs=(output,),
            )


def test_v5_publications_migrate_as_legacy_history_after_ref_advances(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    monkeypatch.setattr(sqlite_catalog_module, "LATEST_SCHEMA_VERSION", 5)
    with SQLiteCatalog(path) as catalog:
        first = _create_running_job(catalog, idempotency_key="first")
        first_output = _output("bm25", first.profiles["bm25"], 100)
        first_completed = catalog.publish_job_outputs(
            first.job.job_id,
            owner_id=first.owner_id,
            fencing_token=first.fencing_token,
            outputs=(first_output,),
        )
        second = _create_running_job(
            catalog,
            repository_id=first.job.repository_id,
            source_revision_id=first.job.source_revision_id,
            profiles=first.profiles,
            expected_ref_generation=1,
            idempotency_key="second",
            owner_id="worker-2",
        )
        second_output = _output("bm25", second.profiles["bm25"], 101)
        second_completed = catalog.publish_job_outputs(
            second.job.job_id,
            owner_id=second.owner_id,
            fencing_token=second.fencing_token,
            outputs=(second_output,),
        )
        assert catalog.resolve_ref(first.job.repository_id)["generation"] == 2

    monkeypatch.setattr(
        sqlite_catalog_module,
        "LATEST_SCHEMA_VERSION",
        LATEST_SCHEMA_VERSION,
    )
    with SQLiteCatalog(path, create=False) as catalog:
        assert (
            catalog._connection.execute(
                "SELECT COUNT(*) FROM index_job_attempts"
            ).fetchone()[0]
            == 0
        )
        assert (
            {
                tuple(row)
                for row in catalog._connection.execute(
                    """
                SELECT job_id, legacy_attempt_count
                FROM index_job_attempt_baselines
                WHERE job_id IN (?, ?)
                """,
                    (first.job.job_id, second.job.job_id),
                ).fetchall()
            }
            == {
                (first.job.job_id, 1),
                (second.job.job_id, 1),
            }
        )
        replay = catalog.publish_job_outputs(
            first.job.job_id,
            owner_id=first.owner_id,
            fencing_token=first.fencing_token,
            outputs=(first_output,),
        )
        assert replay == first_completed
        assert catalog.get_job(second.job.job_id) == second_completed
        assert catalog.resolve_ref(first.job.repository_id)["generation"] == 2

    with SQLiteCatalog(path, create=False) as catalog:
        assert catalog.get_job(first.job.job_id) == first_completed


def test_optional_requested_views_may_be_omitted_or_published_as_a_subset(
    tmp_path,
) -> None:
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_revision_id = _repository(catalog)
        profiles = _profiles(catalog, ("bm25", "vector", "semantic_facts"))
        fixture = _create_running_job(
            catalog,
            repository_id=repository_id,
            source_revision_id=source_revision_id,
            profiles=profiles,
            required=frozenset({"bm25"}),
        )
        outputs = (
            _output("semantic_facts", profiles["semantic_facts"], 102),
            _output("bm25", profiles["bm25"], 100),
        )

        completed = catalog.publish_job_outputs(
            fixture.job.job_id,
            owner_id=fixture.owner_id,
            fencing_token=fixture.fencing_token,
            outputs=outputs,
        )

        manifest = catalog.get_manifest_summary(completed.result_snapshot_id)
        assert tuple(manifest["views"]) == ("bm25", "semantic_facts")


@pytest.mark.parametrize(
    "case",
    ("empty", "missing_required", "extra", "duplicate", "profile_mismatch"),
)
def test_output_set_must_exactly_satisfy_the_persisted_request(
    tmp_path,
    case: str,
) -> None:
    with SQLiteCatalog(tmp_path / f"{case}.sqlite3") as catalog:
        fixture = _create_running_job(catalog)
        bm25 = _output("bm25", fixture.profiles["bm25"], 100)
        vector = _output("vector", fixture.profiles["vector"], 101)
        if case == "empty":
            outputs = ()
        elif case == "missing_required":
            outputs = (vector,)
        elif case == "extra":
            graph_profile = catalog.create_view_profile("graph", {"version": 1})
            outputs = (bm25, _output("graph", graph_profile, 102))
        elif case == "duplicate":
            outputs = (bm25, _output("bm25", fixture.profiles["bm25"], 102))
        else:
            other_profile = catalog.create_view_profile(
                "bm25", {"version": 2}, name="other"
            )
            outputs = (_output("bm25", other_profile, 100),)
        before = _publication_state(catalog)

        with pytest.raises((StorageValidationError, CatalogConflictError)):
            catalog.publish_job_outputs(
                fixture.job.job_id,
                owner_id=fixture.owner_id,
                fencing_token=fixture.fencing_token,
                outputs=outputs,
            )

        assert _publication_state(catalog) == before


@pytest.mark.parametrize("authority", ("owner", "fencing_token"))
def test_wrong_live_lease_authority_is_fenced_without_mutation(
    tmp_path,
    authority: str,
) -> None:
    with SQLiteCatalog(tmp_path / f"{authority}.sqlite3") as catalog:
        fixture = _create_running_job(catalog)
        output = _output("bm25", fixture.profiles["bm25"], 100)
        before = _publication_state(catalog)
        owner_id = "other-worker" if authority == "owner" else fixture.owner_id
        fencing_token = (
            fixture.fencing_token + 1
            if authority == "fencing_token"
            else fixture.fencing_token
        )

        with pytest.raises(CatalogConflictError, match="owner|fenc|lease"):
            catalog.publish_job_outputs(
                fixture.job.job_id,
                owner_id=owner_id,
                fencing_token=fencing_token,
                outputs=(output,),
            )

        assert _publication_state(catalog) == before


def test_expired_lease_and_requested_cancellation_both_block_publication(
    tmp_path,
) -> None:
    for case in ("expired", "cancelled"):
        with SQLiteCatalog(tmp_path / f"{case}.sqlite3") as catalog:
            clock = {"ms": 1_000}
            catalog._connection.create_function(
                "julianday",
                1,
                lambda _value, clock=clock: 2440587.5 + clock["ms"] / 86_400_000,
            )
            fixture = _create_running_job(catalog)
            output = _output("bm25", fixture.profiles["bm25"], 100)
            if case == "expired":
                lease = catalog._connection.execute(
                    "SELECT lease_expires_at_ms FROM ref_job_leases WHERE job_id = ?",
                    (fixture.job.job_id,),
                ).fetchone()
                clock["ms"] = lease["lease_expires_at_ms"] + 1_000
            else:
                catalog.request_job_cancel(fixture.job.job_id)
            before = _publication_state(catalog)

            with pytest.raises(CatalogConflictError, match="expired|cancel"):
                catalog.publish_job_outputs(
                    fixture.job.job_id,
                    owner_id=fixture.owner_id,
                    fencing_token=fixture.fencing_token,
                    outputs=(output,),
                )

            assert _publication_state(catalog) == before


def test_lease_expiring_after_manifest_validation_rolls_back_every_mutation(
    tmp_path,
    monkeypatch,
) -> None:
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        clock = {"ms": 1_000}
        catalog._connection.create_function(
            "julianday",
            1,
            lambda _value: 2440587.5 + clock["ms"] / 86_400_000,
        )
        fixture = _create_running_job(catalog)
        output = _output("bm25", fixture.profiles["bm25"], 100)
        lease_expires_at_ms = catalog._connection.execute(
            "SELECT lease_expires_at_ms FROM ref_job_leases WHERE job_id = ?",
            (fixture.job.job_id,),
        ).fetchone()[0]
        before = _publication_state(catalog)
        real_validate = SQLiteCatalog._validate_retained_ref_response_bounds
        validation_finished = False

        def validate_then_expire(**kwargs: Any) -> None:
            nonlocal validation_finished
            real_validate(**kwargs)
            validation_finished = True
            clock["ms"] = lease_expires_at_ms + 1

        monkeypatch.setattr(
            SQLiteCatalog,
            "_validate_retained_ref_response_bounds",
            staticmethod(validate_then_expire),
        )

        with pytest.raises(
            CatalogConflictError,
            match="current unexpired fenced lease",
        ):
            catalog.publish_job_outputs(
                fixture.job.job_id,
                owner_id=fixture.owner_id,
                fencing_token=fixture.fencing_token,
                outputs=(output,),
            )

        assert validation_finished
        assert _publication_state(catalog) == before


def test_stale_expected_ref_generation_rolls_back_every_publication_row(
    tmp_path,
) -> None:
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_revision_id = _repository(catalog)
        profiles = _profiles(catalog)
        seed = catalog.create_job(
            repository_id,
            source_revision_id,
            "seed-request",
            _request(profiles),
        )
        seed_output = _output("bm25", profiles["bm25"], 50)
        _direct_publish_output(catalog, seed, seed_output, expected_generation=0)
        fixture = _create_running_job(
            catalog,
            repository_id=repository_id,
            source_revision_id=source_revision_id,
            profiles=profiles,
            expected_ref_generation=0,
            idempotency_key="stale-request",
        )
        output = _output("bm25", profiles["bm25"], 100)
        before = _publication_state(catalog)

        with pytest.raises(CatalogConflictError, match="generation|compare|ref"):
            catalog.publish_job_outputs(
                fixture.job.job_id,
                owner_id=fixture.owner_id,
                fencing_token=fixture.fencing_token,
                outputs=(output,),
            )

        assert _publication_state(catalog) == before


def test_existing_desired_snapshot_completes_job_without_advancing_ref(
    tmp_path,
) -> None:
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_revision_id = _repository(catalog)
        profiles = _profiles(catalog)
        seed = catalog.create_job(
            repository_id,
            source_revision_id,
            "seed-request",
            _request(profiles),
        )
        output = _output("bm25", profiles["bm25"], 100)
        seeded = _direct_publish_output(catalog, seed, output, expected_generation=0)
        fixture = _create_running_job(
            catalog,
            repository_id=repository_id,
            source_revision_id=source_revision_id,
            profiles=profiles,
            expected_ref_generation=1,
            idempotency_key="job-request",
        )

        completed = catalog.publish_job_outputs(
            fixture.job.job_id,
            owner_id=fixture.owner_id,
            fencing_token=fixture.fencing_token,
            outputs=(output,),
        )

        publication = _publication_row(catalog, fixture.job.job_id)
        assert completed.result_snapshot_id == seeded["snapshot_id"]
        assert publication["ref_changed"] == 0
        assert publication["ref_generation"] == 1
        assert catalog.resolve_ref(repository_id)["generation"] == 1


def test_exact_committed_replay_returns_original_success_without_ref_movement(
    tmp_path,
) -> None:
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        fixture = _create_running_job(catalog)
        output = _output("bm25", fixture.profiles["bm25"], 100)
        first = catalog.publish_job_outputs(
            fixture.job.job_id,
            owner_id=fixture.owner_id,
            fencing_token=fixture.fencing_token,
            outputs=(output,),
        )
        committed = _publication_state(catalog)

        replay = catalog.publish_job_outputs(
            fixture.job.job_id,
            owner_id=fixture.owner_id,
            fencing_token=fixture.fencing_token,
            outputs=(output,),
        )

        assert replay == first
        assert _publication_state(catalog) == committed
        assert catalog.resolve_ref(fixture.job.repository_id)["generation"] == 1


def test_two_concurrent_exact_publishers_converge_on_one_publication(tmp_path) -> None:
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        fixture = _create_running_job(catalog)
        output = _output("bm25", fixture.profiles["bm25"], 100)

    ready = (Event(), Event())
    start = Event()

    def publish(index: int) -> IndexJobRecord:
        with SQLiteCatalog(path, create=False, busy_timeout_ms=10_000) as catalog:
            ready[index].set()
            assert start.wait(timeout=10)
            return catalog.publish_job_outputs(
                fixture.job.job_id,
                owner_id=fixture.owner_id,
                fencing_token=fixture.fencing_token,
                outputs=(output,),
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(publish, 0)
        try:
            assert ready[0].wait(timeout=15)
            second = executor.submit(publish, 1)
            assert ready[1].wait(timeout=15)
        finally:
            start.set()
        futures = (first, second)
        results = tuple(future.result(timeout=15) for future in futures)

    assert results[0] == results[1]
    assert results[0].status is IndexJobStatus.SUCCEEDED
    with SQLiteCatalog(path, create=False) as catalog:
        assert catalog.get_job(fixture.job.job_id) == results[0]
        assert catalog.resolve_ref(fixture.job.repository_id)["generation"] == 1
        assert (
            catalog._connection.execute(
                "SELECT COUNT(*) FROM index_job_publications WHERE job_id = ?",
                (fixture.job.job_id,),
            ).fetchone()[0]
            == 1
        )


def test_two_concurrent_different_closures_have_one_fenced_winner(tmp_path) -> None:
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        fixture = _create_running_job(catalog)
        outputs = (
            _output("bm25", fixture.profiles["bm25"], 100),
            _output("bm25", fixture.profiles["bm25"], 101),
        )

    ready = (Event(), Event())
    start = Event()

    def publish(index: int, output: IndexJobViewOutput) -> tuple[str, object]:
        with SQLiteCatalog(path, create=False, busy_timeout_ms=10_000) as catalog:
            ready[index].set()
            assert start.wait(timeout=10)
            try:
                return (
                    "succeeded",
                    catalog.publish_job_outputs(
                        fixture.job.job_id,
                        owner_id=fixture.owner_id,
                        fencing_token=fixture.fencing_token,
                        outputs=(output,),
                    ),
                )
            except CatalogConflictError as exc:
                return "conflict", exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(publish, 0, outputs[0])
        try:
            assert ready[0].wait(timeout=15)
            second = executor.submit(publish, 1, outputs[1])
            assert ready[1].wait(timeout=15)
        finally:
            start.set()
        futures = (first, second)
        outcomes = tuple(future.result(timeout=15) for future in futures)

    assert sorted(outcome[0] for outcome in outcomes) == ["conflict", "succeeded"]
    winner = next(value for status, value in outcomes if status == "succeeded")
    loser = next(value for status, value in outcomes if status == "conflict")
    assert isinstance(winner, IndexJobRecord)
    assert winner.status is IndexJobStatus.SUCCEEDED
    assert isinstance(loser, CatalogConflictError)
    with SQLiteCatalog(path, create=False) as catalog:
        assert catalog.get_job(fixture.job.job_id) == winner
        assert catalog.resolve_ref(fixture.job.repository_id)["generation"] == 1
        assert (
            catalog._connection.execute(
                "SELECT COUNT(*) FROM index_job_publications WHERE job_id = ?",
                (fixture.job.job_id,),
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("kind", (KeyboardInterrupt, SystemExit))
def test_commit_successor_response_loss_replays_the_full_v5_aggregate(
    tmp_path,
    kind: type[KeyboardInterrupt] | type[SystemExit],
) -> None:
    with SQLiteCatalog(tmp_path / f"commit-{kind.__name__}.sqlite3") as catalog:
        fixture = _create_running_job(catalog)
        member = _object(200, media_type="application/x-test-member")
        output = _output(
            "bm25",
            fixture.profiles["bm25"],
            100,
            members=(member,),
        )
        ambiguous = kind("publication commit completed before response loss")

        with pytest.raises(kind) as caught:
            with _inject_commit_successor(catalog, ambiguous):
                catalog.publish_job_outputs(
                    fixture.job.job_id,
                    owner_id=fixture.owner_id,
                    fencing_token=fixture.fencing_token,
                    outputs=(output,),
                )
        assert caught.value is ambiguous

        persisted = catalog.get_job(fixture.job.job_id)
        assert persisted.status is IndexJobStatus.SUCCEEDED
        assert persisted.result_snapshot_id is not None
        committed = _publication_state(catalog)
        assert catalog.resolve_ref(fixture.job.repository_id)["generation"] == 1

        replay = catalog.publish_job_outputs(
            fixture.job.job_id,
            owner_id=fixture.owner_id,
            fencing_token=fixture.fencing_token,
            outputs=(output,),
        )
        assert replay == persisted
        assert _publication_state(catalog) == committed

        differing_calls = (
            {
                "owner_id": "other-worker",
                "fencing_token": fixture.fencing_token,
                "outputs": (output,),
            },
            {
                "owner_id": fixture.owner_id,
                "fencing_token": fixture.fencing_token + 1,
                "outputs": (output,),
            },
            {
                "owner_id": fixture.owner_id,
                "fencing_token": fixture.fencing_token,
                "outputs": (_output("bm25", fixture.profiles["bm25"], 101),),
            },
        )
        for changed in differing_calls:
            with pytest.raises(CatalogConflictError):
                catalog.publish_job_outputs(
                    fixture.job.job_id,
                    **changed,
                )
            assert _publication_state(catalog) == committed
            assert catalog.resolve_ref(fixture.job.repository_id)["generation"] == 1


@pytest.mark.parametrize("difference", ("owner", "fencing_token", "closure"))
def test_committed_replay_rejects_any_authority_or_closure_difference(
    tmp_path,
    difference: str,
) -> None:
    with SQLiteCatalog(tmp_path / f"{difference}.sqlite3") as catalog:
        fixture = _create_running_job(catalog)
        output = _output("bm25", fixture.profiles["bm25"], 100)
        catalog.publish_job_outputs(
            fixture.job.job_id,
            owner_id=fixture.owner_id,
            fencing_token=fixture.fencing_token,
            outputs=(output,),
        )
        committed = _publication_state(catalog)
        owner_id = "other-worker" if difference == "owner" else fixture.owner_id
        fencing_token = (
            fixture.fencing_token + 1
            if difference == "fencing_token"
            else fixture.fencing_token
        )
        outputs = (
            (_output("bm25", fixture.profiles["bm25"], 101),)
            if difference == "closure"
            else (output,)
        )

        with pytest.raises(CatalogConflictError, match="replay|publication|authority"):
            catalog.publish_job_outputs(
                fixture.job.job_id,
                owner_id=owner_id,
                fencing_token=fencing_token,
                outputs=outputs,
            )

        assert _publication_state(catalog) == committed


def test_historical_exact_replay_survives_a_later_ref_publication(tmp_path) -> None:
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        fixture = _create_running_job(catalog)
        first_output = _output("bm25", fixture.profiles["bm25"], 100)
        first = catalog.publish_job_outputs(
            fixture.job.job_id,
            owner_id=fixture.owner_id,
            fencing_token=fixture.fencing_token,
            outputs=(first_output,),
        )
        later = _create_running_job(
            catalog,
            repository_id=fixture.job.repository_id,
            source_revision_id=fixture.job.source_revision_id,
            profiles=fixture.profiles,
            expected_ref_generation=1,
            idempotency_key="request-2",
            owner_id="worker-2",
        )
        second_output = _output("bm25", fixture.profiles["bm25"], 101)
        second = catalog.publish_job_outputs(
            later.job.job_id,
            owner_id=later.owner_id,
            fencing_token=later.fencing_token,
            outputs=(second_output,),
        )
        before_replay = _publication_state(catalog)

        replay = catalog.publish_job_outputs(
            fixture.job.job_id,
            owner_id=fixture.owner_id,
            fencing_token=fixture.fencing_token,
            outputs=(first_output,),
        )

        assert replay == first
        assert _publication_state(catalog) == before_replay
        current = catalog.resolve_ref(fixture.job.repository_id)
        assert current["generation"] == 2
        assert current["snapshot_id"] == second.result_snapshot_id


def test_manifest_response_budget_failure_rolls_back_the_full_publication(
    tmp_path,
) -> None:
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_revision_id = _repository(catalog)
        seed_profiles = _profiles(catalog, ("bm25",))
        seed_job = catalog.create_job(
            repository_id,
            source_revision_id,
            "seed-request",
            _request(seed_profiles),
        )
        seeded = _direct_publish_output(
            catalog,
            seed_job,
            _output("bm25", seed_profiles["bm25"], 50),
            expected_generation=0,
        )
        oversized_profile = catalog.create_view_profile(
            "bm25",
            {"manifest_nodes": [None] * 250_000},
            name="manifest-response-overflow",
        )
        fixture = _create_running_job(
            catalog,
            repository_id=repository_id,
            source_revision_id=source_revision_id,
            profiles={"bm25": oversized_profile},
            expected_ref_generation=1,
            idempotency_key="oversized-manifest-request",
        )
        output = _output("bm25", oversized_profile, 100)

        # The authority/output closure itself is small enough; only the full
        # authoritative manifest (which includes profile config) crosses the
        # retained-response node budget.
        assert sqlite_catalog_module._freeze_job_publication_outputs((output,)) == (
            output,
        )
        before = _publication_state(catalog)
        old_ref = catalog.resolve_ref(repository_id)
        running_job = catalog.get_job(fixture.job.job_id)
        lease = tuple(
            catalog._connection.execute(
                "SELECT * FROM ref_job_leases WHERE job_id = ?",
                (fixture.job.job_id,),
            ).fetchone()
        )

        with pytest.raises(CatalogValidationError, match="retained|bounds|snapshot"):
            catalog.publish_job_outputs(
                fixture.job.job_id,
                owner_id=fixture.owner_id,
                fencing_token=fixture.fencing_token,
                outputs=(output,),
            )

        assert _publication_state(catalog) == before
        assert catalog.resolve_ref(repository_id) == old_ref
        assert old_ref["generation"] == 1
        assert old_ref["snapshot_id"] == seeded["snapshot_id"]
        assert catalog.get_job(fixture.job.job_id) == running_job
        assert (
            tuple(
                catalog._connection.execute(
                    "SELECT * FROM ref_job_leases WHERE job_id = ?",
                    (fixture.job.job_id,),
                ).fetchone()
            )
            == lease
        )
        assert (
            catalog._connection.execute(
                "SELECT COUNT(*) FROM objects WHERE digest = ?",
                (output.object_record.digest,),
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    ("table", "event", "when"),
    [
        ("objects", "INSERT", "1"),
        ("view_generations", "INSERT", "1"),
        ("view_generation_objects", "INSERT", "1"),
        ("view_generations", "UPDATE", "NEW.status = 'ready'"),
        ("snapshots", "INSERT", "1"),
        ("snapshot_views", "INSERT", "1"),
        ("snapshots", "UPDATE", "NEW.status = 'ready'"),
        ("refs", "INSERT", "1"),
        ("index_job_publications", "INSERT", "1"),
        ("index_job_attempt_closure_frontiers", "INSERT", "1"),
        ("index_jobs", "UPDATE", "NEW.status = 'succeeded'"),
        ("ref_job_leases", "UPDATE", "NEW.job_id IS NULL"),
    ],
)
def test_failure_at_each_mutation_stage_rolls_back_the_whole_publication(
    tmp_path,
    table: str,
    event: str,
    when: str,
) -> None:
    path = tmp_path / f"{table}-{event}.sqlite3"
    with SQLiteCatalog(path) as catalog:
        fixture = _create_running_job(catalog)
        member = _object(200, media_type="application/x-test-member")
        output = _output(
            "bm25",
            fixture.profiles["bm25"],
            100,
            members=(member,),
        )
        before = _publication_state(catalog)
        catalog._connection.execute(
            f"""
            CREATE TRIGGER injected_publication_failure
            BEFORE {event} ON {table}
            WHEN {when}
            BEGIN
                SELECT RAISE(ABORT, 'injected publication failure');
            END
            """
        )  # noqa: S608 - values come from the fixed parametrization above

        with pytest.raises(sqlite3.IntegrityError, match="injected publication"):
            catalog.publish_job_outputs(
                fixture.job.job_id,
                owner_id=fixture.owner_id,
                fencing_token=fixture.fencing_token,
                outputs=(output,),
            )

        assert _publication_state(catalog) == before
        assert (
            catalog._connection.execute(
                "SELECT COUNT(*) FROM index_job_attempt_closure_frontiers"
            ).fetchone()[0]
            == 0
        )


def test_response_construction_failure_before_commit_rolls_back_publication(
    tmp_path,
    monkeypatch,
) -> None:
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        fixture = _create_running_job(catalog)
        output = _output("bm25", fixture.profiles["bm25"], 100)
        before = _publication_state(catalog)
        real_from_row = SQLiteCatalog._job_from_row

        def fail_successful_response(row: sqlite3.Row) -> IndexJobRecord:
            if row["status"] == "succeeded":
                raise RuntimeError("injected response construction failure")
            return real_from_row(row)

        monkeypatch.setattr(
            SQLiteCatalog,
            "_job_from_row",
            staticmethod(fail_successful_response),
        )

        with pytest.raises(RuntimeError, match="response construction"):
            catalog.publish_job_outputs(
                fixture.job.job_id,
                owner_id=fixture.owner_id,
                fencing_token=fixture.fencing_token,
                outputs=(output,),
            )

        assert _publication_state(catalog) == before


@pytest.mark.parametrize("initial_version", (1, 2, 3, 4))
def test_every_prior_catalog_version_forward_migrates_to_latest(
    tmp_path,
    monkeypatch,
    initial_version: int,
) -> None:
    path = tmp_path / f"v{initial_version}.sqlite3"
    monkeypatch.setattr(
        sqlite_catalog_module,
        "LATEST_SCHEMA_VERSION",
        initial_version,
    )
    with SQLiteCatalog(path) as catalog:
        repository_id, source_revision_id = _repository(catalog)
        profiles = _profiles(catalog, ("bm25",))
        job_id = None
        if initial_version >= 2:
            job_id = catalog.create_job(
                repository_id,
                source_revision_id,
                "pre-v5-job",
                _request(profiles),
            ).job_id
        record = _object(100 + initial_version)
        digest = catalog.register_object(
            record.digest,
            storage_key=record.storage_key,
            byte_size=record.byte_size,
            media_type=record.media_type,
        )
        member_digests: tuple[str, ...] = ()
        if initial_version >= 4:
            member = _object(200 + initial_version)
            catalog.register_object(
                member.digest,
                storage_key=member.storage_key,
                byte_size=member.byte_size,
                media_type=member.media_type,
            )
            member_digests = (member.digest,)
        generation_id = catalog.stage_view_generation(
            repository_id,
            source_revision_id,
            profiles["bm25"],
            "bm25",
            digest,
            schema_version="bm25.pre-v5",
            metadata={"initial_version": initial_version},
            member_object_digests=member_digests,
        )
        published = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            (generation_id,),
            expected_generation=0,
        )
        retained_ref = catalog.resolve_ref(repository_id)
        assert catalog.schema_version == initial_version

    monkeypatch.setattr(
        sqlite_catalog_module,
        "LATEST_SCHEMA_VERSION",
        LATEST_SCHEMA_VERSION,
    )
    with SQLiteCatalog(path, create=False) as catalog:
        assert catalog.schema_version == LATEST_SCHEMA_VERSION
        assert catalog.create_repository("owner/job-publication") == repository_id
        if job_id is not None:
            assert catalog.get_job(job_id).status is IndexJobStatus.QUEUED
        assert (
            catalog._connection.execute(
                "SELECT COUNT(*) FROM objects WHERE digest = ?", (digest,)
            ).fetchone()[0]
            == 1
        )
        assert catalog.resolve_ref(repository_id) == retained_ref
        assert retained_ref["snapshot_id"] == published["snapshot_id"]
        assert (
            catalog.get_manifest_summary(published["snapshot_id"])["views"]["bm25"][
                "view_generation_id"
            ]
            == generation_id
        )
        if member_digests:
            assert (
                catalog._connection.execute(
                    """
                SELECT object_digest FROM view_generation_objects
                WHERE view_generation_id = ?
                """,
                    (generation_id,),
                ).fetchone()[0]
                == member_digests[0]
            )
        tables = {
            row[0]
            for row in catalog._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        triggers = {
            row[0]
            for row in catalog._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        assert "index_job_publications" in tables
        assert {
            "index_job_publications_validate_insert",
            "index_job_publication_completes_job",
            "index_job_publications_reject_duplicate_inserts",
            "index_job_publications_are_immutable",
            "index_job_publications_cannot_be_deleted",
            "m1_index_jobs_cannot_update_succeeded",
            "published_index_jobs_cannot_be_deleted",
            "published_snapshots_cannot_be_deleted",
        } <= triggers
        assert "m1_index_jobs_cannot_insert_succeeded" in triggers


def test_failed_v5_migration_rolls_back_schema_and_trigger_changes(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    monkeypatch.setattr(sqlite_catalog_module, "LATEST_SCHEMA_VERSION", 4)
    with SQLiteCatalog(path) as catalog:
        fixture = _create_running_job(catalog)
        member = _object(200, media_type="application/x-test-member")
        output = _output(
            "bm25",
            fixture.profiles["bm25"],
            100,
            members=(member,),
        )
        seeded = _direct_publish_output(
            catalog,
            fixture.job,
            output,
            expected_generation=0,
        )
        retained_ref = catalog.resolve_ref(fixture.job.repository_id)
        retained_job = catalog.get_job(fixture.job.job_id)
        retained_lease = tuple(
            catalog._connection.execute(
                "SELECT * FROM ref_job_leases WHERE job_id = ?",
                (fixture.job.job_id,),
            ).fetchone()
        )

    original_migrations = sqlite_catalog_module._MIGRATIONS
    broken = dict(sqlite_catalog_module._MIGRATIONS)
    broken[5] = sqlite_catalog_module._MIGRATIONS[5] + ("THIS IS NOT VALID SQL",)
    monkeypatch.setattr(sqlite_catalog_module, "_MIGRATIONS", broken)
    monkeypatch.setattr(sqlite_catalog_module, "LATEST_SCHEMA_VERSION", 5)
    with pytest.raises(sqlite3.OperationalError):
        SQLiteCatalog(path)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert (
            connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[
                0
            ]
            == 4
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = 'index_job_publications'"
            ).fetchone()[0]
            == 0
        )
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        assert "m1_index_jobs_cannot_update_succeeded" in triggers
        assert "index_job_publication_completes_job" not in triggers
        assert connection.execute("SELECT COUNT(*) FROM objects").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM view_generation_objects"
            ).fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM refs").fetchone()[0] == 1
    finally:
        connection.close()

    monkeypatch.setattr(sqlite_catalog_module, "_MIGRATIONS", original_migrations)
    with SQLiteCatalog(path, create=False) as catalog:
        assert catalog.schema_version == 5
        assert catalog.resolve_ref(fixture.job.repository_id) == retained_ref
        assert retained_ref["snapshot_id"] == seeded["snapshot_id"]
        assert catalog.get_job(fixture.job.job_id) == retained_job
        assert (
            tuple(
                catalog._connection.execute(
                    "SELECT * FROM ref_job_leases WHERE job_id = ?",
                    (fixture.job.job_id,),
                ).fetchone()
            )
            == retained_lease
        )
        assert (
            catalog._connection.execute(
                """
            SELECT object_digest FROM view_generation_objects
            WHERE object_digest = ?
            """,
                (member.digest,),
            ).fetchone()[0]
            == member.digest
        )
        assert (
            catalog._connection.execute(
                "SELECT COUNT(*) FROM index_job_publications"
            ).fetchone()[0]
            == 0
        )


def test_raw_sql_cannot_replace_mutate_or_erase_a_publication_closure(
    tmp_path,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        fixture = _create_running_job(catalog)
        output = _output("bm25", fixture.profiles["bm25"], 100)
        completed = catalog.publish_job_outputs(
            fixture.job.job_id,
            owner_id=fixture.owner_id,
            fencing_token=fixture.fencing_token,
            outputs=(output,),
        )
        committed = _publication_state(catalog)

    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("PRAGMA recursive_triggers = OFF")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT OR REPLACE INTO index_job_publications
                SELECT * FROM index_job_publications WHERE job_id = ?
                """,
                (fixture.job.job_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE index_job_publications SET owner_id = 'attacker' WHERE job_id = ?",
                (fixture.job.job_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "DELETE FROM index_job_publications WHERE job_id = ?",
                (fixture.job.job_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="published index job"):
            connection.execute(
                "DELETE FROM index_jobs WHERE job_id = ?",
                (fixture.job.job_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="published job snapshot"):
            connection.execute(
                "DELETE FROM snapshots WHERE snapshot_id = ?",
                (completed.result_snapshot_id,),
            )
        connection.rollback()
    finally:
        connection.close()

    with SQLiteCatalog(path, create=False) as catalog:
        assert _publication_state(catalog) == committed
        replay = catalog.publish_job_outputs(
            fixture.job.job_id,
            owner_id=fixture.owner_id,
            fencing_token=fixture.fencing_token,
            outputs=(output,),
        )
        assert replay == completed


def test_reopen_rejects_success_and_non_success_double_attempt_closure(
    tmp_path,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        fixture = _create_running_job(catalog)
        output = _output("bm25", fixture.profiles["bm25"], 100)
        completed = catalog.publish_job_outputs(
            fixture.job.job_id,
            owner_id=fixture.owner_id,
            fencing_token=fixture.fencing_token,
            outputs=(output,),
        )
        assert completed.finished_at_ms is not None
        with _raw_table_corruption(
            catalog,
            table="index_job_attempt_completions",
        ) as connection:
            connection.execute(
                """
                INSERT INTO index_job_attempt_completions(
                    job_id, attempt_count, owner_id, fencing_token, outcome,
                    error_code, error_message, completed_at_ms
                ) VALUES (?, 1, ?, ?, 'failed', 'raw_double', NULL, ?)
                """,
                (
                    fixture.job.job_id,
                    fixture.owner_id,
                    fixture.fencing_token,
                    completed.finished_at_ms,
                ),
            )

    with pytest.raises(CatalogConflictError, match="both success and non-success"):
        SQLiteCatalog(path, create=False)


@pytest.mark.parametrize(
    "dependency",
    ("namespace", "repository", "source_revision", "view_profile"),
)
def test_raw_foundational_replace_is_detected_on_replay_and_existing_only_reopen(
    tmp_path,
    dependency: str,
) -> None:
    path = tmp_path / f"{dependency}.sqlite3"
    with SQLiteCatalog(path) as catalog:
        fixture = _create_running_job(catalog)
        output = _output("bm25", fixture.profiles["bm25"], 100)
        catalog.publish_job_outputs(
            fixture.job.job_id,
            owner_id=fixture.owner_id,
            fencing_token=fixture.fencing_token,
            outputs=(output,),
        )
        repository = catalog._require_record(
            "repositories", "repository_id", fixture.job.repository_id
        )

        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("PRAGMA recursive_triggers = OFF")
            if dependency == "namespace":
                connection.execute(
                    """
                    INSERT OR REPLACE INTO namespaces(
                        namespace_id, name, created_at
                    )
                    SELECT namespace_id, name || '-forged', created_at
                    FROM namespaces WHERE namespace_id = ?
                    """,
                    (repository["namespace_id"],),
                )
            elif dependency == "repository":
                connection.execute(
                    """
                    INSERT OR REPLACE INTO repositories(
                        repository_id, namespace_id, repository_key, created_at
                    )
                    SELECT repository_id, namespace_id,
                           repository_key || '-forged', created_at
                    FROM repositories WHERE repository_id = ?
                    """,
                    (fixture.job.repository_id,),
                )
            elif dependency == "source_revision":
                connection.execute(
                    """
                    INSERT OR REPLACE INTO source_revisions(
                        source_revision_id, repository_id, source_kind,
                        commit_sha, tree_sha, source_fingerprint,
                        identity_digest, created_at
                    )
                    SELECT source_revision_id, repository_id, source_kind,
                           commit_sha, ?, source_fingerprint,
                           identity_digest, created_at
                    FROM source_revisions WHERE source_revision_id = ?
                    """,
                    ("c" * 64, fixture.job.source_revision_id),
                )
            else:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO view_profiles(
                        profile_id, view_type, name, config_json,
                        profile_digest, created_at
                    )
                    SELECT profile_id, view_type, name, '{"forged":true}',
                           profile_digest, created_at
                    FROM view_profiles WHERE profile_id = ?
                    """,
                    (fixture.profiles["bm25"],),
                )
            connection.commit()
        finally:
            connection.close()

        with pytest.raises(CatalogConflictError):
            catalog.publish_job_outputs(
                fixture.job.job_id,
                owner_id=fixture.owner_id,
                fencing_token=fixture.fencing_token,
                outputs=(output,),
            )

    with pytest.raises(CatalogConflictError):
        SQLiteCatalog(path, create=False)


def test_existing_only_reopen_rejects_real_publication_fencing_token(tmp_path) -> None:
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        fixture = _create_running_job(catalog)
        output = _output("bm25", fixture.profiles["bm25"], 100)
        catalog.publish_job_outputs(
            fixture.job.job_id,
            owner_id=fixture.owner_id,
            fencing_token=fixture.fencing_token,
            outputs=(output,),
        )
        later = _create_running_job(
            catalog,
            repository_id=fixture.job.repository_id,
            source_revision_id=fixture.job.source_revision_id,
            profiles=fixture.profiles,
            expected_ref_generation=1,
            idempotency_key="later-lease",
            owner_id="worker-2",
        )
        assert later.fencing_token == fixture.fencing_token + 1

        immutable_trigger = catalog._connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'trigger'
                AND name = 'index_job_publications_are_immutable'
            """
        ).fetchone()[0]
        publication = _publication_row(catalog, fixture.job.job_id)
        closure = json.loads(publication["closure_json"])
        closure["fencing_token"] = 1.5
        closure_json = canonical_json(closure)
        catalog._connection.execute("DROP TRIGGER index_job_publications_are_immutable")
        catalog._connection.execute("PRAGMA ignore_check_constraints = ON")
        try:
            catalog._connection.execute(
                """
                UPDATE index_job_publications
                SET fencing_token = ?, closure_json = ?, closure_digest = ?
                WHERE job_id = ?
                """,
                (
                    1.5,
                    closure_json,
                    hashlib.sha256(closure_json.encode()).hexdigest(),
                    fixture.job.job_id,
                ),
            )
        finally:
            catalog._connection.execute("PRAGMA ignore_check_constraints = OFF")
        catalog._connection.execute(immutable_trigger)
        forged = _publication_row(catalog, fixture.job.job_id)
        assert type(forged["fencing_token"]) is float
        assert forged["fencing_token"] == 1.5

    with pytest.raises(CatalogConflictError):
        SQLiteCatalog(path, create=False)


def test_insert_trigger_rejects_output_missing_member_objects_key(tmp_path) -> None:
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        fixture = _create_running_job(catalog)
        output = _output("bm25", fixture.profiles["bm25"], 100)
        published = _direct_publish_output(
            catalog,
            fixture.job,
            output,
            expected_generation=0,
        )
        job = catalog.get_job(fixture.job.job_id)
        identities = catalog._job_publication_output_identities(
            job,
            catalog._job_views(job),
            (output,),
        )
        ref = catalog._connection.execute(
            """
            SELECT generation, updated_at FROM refs
            WHERE repository_id = ? AND ref_name = ?
            """,
            (job.repository_id, job.ref_name),
        ).fetchone()
        closure_json, _closure_digest = (
            sqlite_catalog_module._canonical_job_publication_closure(
                job,
                owner_id=fixture.owner_id,
                fencing_token=fixture.fencing_token,
                snapshot_id=published["snapshot_id"],
                ref_generation=ref["generation"],
                ref_changed=True,
                ref_updated_at=ref["updated_at"],
                output_identities=identities,
            )
        )
        closure = json.loads(closure_json)
        del closure["outputs"][0]["member_objects"]
        closure["outputs"][0]["extra"] = []
        forged_json = canonical_json(closure)
        completed_at_ms = catalog._db_now_ms()
        before = _publication_state(catalog)

        with pytest.raises(sqlite3.IntegrityError, match="publication closure"):
            catalog._connection.execute(
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
                    fixture.owner_id,
                    fixture.fencing_token,
                    job.expected_ref_generation,
                    published["snapshot_id"],
                    ref["generation"],
                    1,
                    ref["updated_at"],
                    hashlib.sha256(forged_json.encode()).hexdigest(),
                    forged_json,
                    completed_at_ms,
                ),
            )

        assert _publication_state(catalog) == before


def test_publication_trigger_fails_closed_when_execution_clock_is_missing(
    tmp_path,
) -> None:
    with SQLiteCatalog(tmp_path / "missing-publication-clock.sqlite3") as catalog:
        fixture = _create_running_job(catalog)
        output = _output("bm25", fixture.profiles["bm25"], 100)
        published = _direct_publish_output(
            catalog,
            fixture.job,
            output,
            expected_generation=0,
        )
        job = catalog.get_job(fixture.job.job_id)
        identities = catalog._job_publication_output_identities(
            job,
            catalog._job_views(job),
            (output,),
        )
        ref = catalog._connection.execute(
            """
            SELECT generation, updated_at FROM refs
            WHERE repository_id = ? AND ref_name = ?
            """,
            (job.repository_id, job.ref_name),
        ).fetchone()
        closure_json, closure_digest = (
            sqlite_catalog_module._canonical_job_publication_closure(
                job,
                owner_id=fixture.owner_id,
                fencing_token=fixture.fencing_token,
                snapshot_id=published["snapshot_id"],
                ref_generation=ref["generation"],
                ref_changed=True,
                ref_updated_at=ref["updated_at"],
                output_identities=identities,
            )
        )
        completed_at_ms = job.updated_at_ms
        _remove_execution_clock_row(catalog)
        before = _publication_state(catalog)
        catalog._connection.execute("PRAGMA recursive_triggers = OFF")

        with pytest.raises(sqlite3.IntegrityError, match="publication closure"):
            catalog._connection.execute(
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
                    fixture.owner_id,
                    fixture.fencing_token,
                    job.expected_ref_generation,
                    published["snapshot_id"],
                    ref["generation"],
                    1,
                    ref["updated_at"],
                    closure_digest,
                    closure_json,
                    completed_at_ms,
                ),
            )

        assert _publication_state(catalog) == before


@pytest.mark.parametrize(
    "corruption",
    (
        "expected_generation_real",
        "cancel_requested_non_boolean",
        "finished_at_real",
        "result_snapshot_padded",
        "repository_padded",
        "request_json_pretty",
        "view_profile_padded",
        "view_required_non_boolean",
        "succeeded_cancel_requested",
        "succeeded_attempt_zero",
        "succeeded_started_at_missing",
    ),
)
def test_existing_only_reopen_rejects_noncanonical_published_dependencies(
    tmp_path,
    corruption: str,
) -> None:
    path = tmp_path / f"{corruption}.sqlite3"
    with SQLiteCatalog(path) as catalog:
        fixture = _create_running_job(catalog)
        output = _output("bm25", fixture.profiles["bm25"], 100)
        catalog.publish_job_outputs(
            fixture.job.job_id,
            owner_id=fixture.owner_id,
            fencing_token=fixture.fencing_token,
            outputs=(output,),
        )
        table = "index_job_views" if corruption.startswith("view_") else "index_jobs"
        with _raw_table_corruption(catalog, table) as connection:
            if corruption == "expected_generation_real":
                assignment = "expected_ref_generation = 0.5"
            elif corruption == "cancel_requested_non_boolean":
                assignment = "cancel_requested = 2"
            elif corruption == "finished_at_real":
                assignment = "finished_at_ms = finished_at_ms + 0.5"
            elif corruption == "result_snapshot_padded":
                assignment = "result_snapshot_id = result_snapshot_id || ' '"
            elif corruption == "repository_padded":
                assignment = "repository_id = repository_id || ' '"
            elif corruption == "request_json_pretty":
                request_json = connection.execute(
                    "SELECT request_json FROM index_jobs WHERE job_id = ?",
                    (fixture.job.job_id,),
                ).fetchone()[0]
                connection.execute(
                    "UPDATE index_jobs SET request_json = ? WHERE job_id = ?",
                    (
                        json.dumps(json.loads(request_json), indent=2),
                        fixture.job.job_id,
                    ),
                )
            elif corruption == "view_profile_padded":
                assignment = "profile_id = profile_id || ' '"
            elif corruption == "view_required_non_boolean":
                assignment = "required = 2"
            elif corruption == "succeeded_cancel_requested":
                assignment = "cancel_requested = 1"
            elif corruption == "succeeded_attempt_zero":
                assignment = "attempt_count = 0"
            else:
                assignment = "started_at_ms = NULL"
            if corruption != "request_json_pretty":
                connection.execute(
                    f"UPDATE {table} SET {assignment} WHERE job_id = ?",  # noqa: S608
                    (fixture.job.job_id,),
                )

    with pytest.raises(
        (CatalogConflictError, StorageIntegrityError, StorageValidationError)
    ):
        SQLiteCatalog(path, create=False)


@pytest.mark.parametrize(
    "corruption",
    (
        "updated_at_real",
        "released_owner_present",
        "active_job_missing",
    ),
)
def test_existing_only_reopen_rejects_noncanonical_lease_slots(
    tmp_path,
    corruption: str,
) -> None:
    path = tmp_path / f"lease-{corruption}.sqlite3"
    with SQLiteCatalog(path) as catalog:
        fixture = _create_running_job(catalog)
        if corruption == "released_owner_present":
            output = _output("bm25", fixture.profiles["bm25"], 100)
            catalog.publish_job_outputs(
                fixture.job.job_id,
                owner_id=fixture.owner_id,
                fencing_token=fixture.fencing_token,
                outputs=(output,),
            )
        with _raw_table_corruption(catalog, "ref_job_leases") as connection:
            if corruption == "updated_at_real":
                assignment = "updated_at_ms = updated_at_ms + 0.5"
            elif corruption == "released_owner_present":
                assignment = "owner_id = 'ghost'"
            else:
                assignment = f"job_id = 'job_{'f' * 64}'"
            connection.execute(
                f"""
                UPDATE ref_job_leases SET {assignment}
                WHERE repository_id = ? AND ref_name = ?
                """,  # noqa: S608 - fixed adversarial assignments above
                (fixture.job.repository_id, fixture.job.ref_name),
            )

    with pytest.raises(
        (CatalogConflictError, StorageIntegrityError, StorageValidationError)
    ):
        SQLiteCatalog(path, create=False)


@pytest.mark.parametrize("history", ("released_current", "later_active"))
def test_existing_only_reopen_accepts_canonical_publication_lease_history(
    tmp_path,
    history: str,
) -> None:
    path = tmp_path / f"lease-history-{history}.sqlite3"
    with SQLiteCatalog(path) as catalog:
        fixture = _create_running_job(catalog)
        output = _output("bm25", fixture.profiles["bm25"], 100)
        completed = catalog.publish_job_outputs(
            fixture.job.job_id,
            owner_id=fixture.owner_id,
            fencing_token=fixture.fencing_token,
            outputs=(output,),
        )
        later = None
        if history == "later_active":
            later = _create_running_job(
                catalog,
                repository_id=fixture.job.repository_id,
                source_revision_id=fixture.job.source_revision_id,
                profiles=fixture.profiles,
                expected_ref_generation=1,
                idempotency_key="later-active",
                owner_id="worker-2",
            )
            assert later.fencing_token == fixture.fencing_token + 1

    with SQLiteCatalog(path, create=False) as catalog:
        assert catalog.get_job(fixture.job.job_id) == completed
        slot = catalog._connection.execute(
            """
            SELECT * FROM ref_job_leases
            WHERE repository_id = ? AND ref_name = ?
            """,
            (fixture.job.repository_id, fixture.job.ref_name),
        ).fetchone()
        if later is None:
            assert slot["job_id"] is None
            assert slot["fencing_token"] == fixture.fencing_token
        else:
            assert slot["job_id"] == later.job.job_id
            assert slot["fencing_token"] == later.fencing_token


@pytest.mark.parametrize(
    "corruption",
    ("fencing_token_not_advanced", "later_lease_predates_publication"),
)
def test_existing_only_reopen_rejects_impossible_publication_lease_history(
    tmp_path,
    corruption: str,
) -> None:
    path = tmp_path / f"lease-history-{corruption}.sqlite3"
    with SQLiteCatalog(path) as catalog:
        fixture = _create_running_job(catalog)
        output = _output("bm25", fixture.profiles["bm25"], 100)
        catalog.publish_job_outputs(
            fixture.job.job_id,
            owner_id=fixture.owner_id,
            fencing_token=fixture.fencing_token,
            outputs=(output,),
        )
        publication = _publication_row(catalog, fixture.job.job_id)
        later = _create_running_job(
            catalog,
            repository_id=fixture.job.repository_id,
            source_revision_id=fixture.job.source_revision_id,
            profiles=fixture.profiles,
            expected_ref_generation=1,
            idempotency_key=f"later-{corruption}",
            owner_id="worker-2",
        )
        assert later.fencing_token == fixture.fencing_token + 1
        with _raw_table_corruption(catalog, "ref_job_leases") as connection:
            if corruption == "fencing_token_not_advanced":
                connection.execute(
                    """
                    UPDATE ref_job_leases SET fencing_token = ?
                    WHERE repository_id = ? AND ref_name = ?
                    """,
                    (
                        fixture.fencing_token,
                        fixture.job.repository_id,
                        fixture.job.ref_name,
                    ),
                )
            else:
                before_publication = publication["completed_at_ms"] - 1
                connection.execute(
                    """
                    UPDATE ref_job_leases
                    SET acquired_at_ms = ?, heartbeat_at_ms = ?, updated_at_ms = ?
                    WHERE repository_id = ? AND ref_name = ?
                    """,
                    (
                        before_publication,
                        before_publication,
                        before_publication,
                        fixture.job.repository_id,
                        fixture.job.ref_name,
                    ),
                )

    with pytest.raises(
        (CatalogConflictError, StorageIntegrityError, StorageValidationError)
    ):
        SQLiteCatalog(path, create=False)


def test_partial_raw_publication_insert_is_rejected_declaratively(tmp_path) -> None:
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        fixture = _create_running_job(catalog)
        before = _publication_state(catalog)

        with pytest.raises(
            sqlite3.IntegrityError,
            match="closure|attempt start|NOT NULL",
        ):
            catalog._connection.execute(
                "INSERT INTO index_job_publications(job_id) VALUES (?)",
                (fixture.job.job_id,),
            )

        assert _publication_state(catalog) == before


@pytest.mark.parametrize("corruption", ("closure_digest", "closure_json"))
def test_replay_deep_validates_a_forged_persisted_publication(
    tmp_path,
    corruption: str,
) -> None:
    with SQLiteCatalog(tmp_path / f"{corruption}.sqlite3") as catalog:
        fixture = _create_running_job(catalog)
        output = _output("bm25", fixture.profiles["bm25"], 100)
        catalog.publish_job_outputs(
            fixture.job.job_id,
            owner_id=fixture.owner_id,
            fencing_token=fixture.fencing_token,
            outputs=(output,),
        )
        catalog._connection.execute("DROP TRIGGER index_job_publications_are_immutable")
        if corruption == "closure_digest":
            catalog._connection.execute(
                "UPDATE index_job_publications SET closure_digest = ? WHERE job_id = ?",
                ("f" * 64, fixture.job.job_id),
            )
        else:
            row = _publication_row(catalog, fixture.job.job_id)
            closure = json.loads(row["closure_json"])
            closure["owner_id"] = "forged-owner"
            forged_json = canonical_json(closure)
            catalog._connection.execute(
                """
                UPDATE index_job_publications
                SET closure_json = ?, closure_digest = ? WHERE job_id = ?
                """,
                (
                    forged_json,
                    hashlib.sha256(forged_json.encode()).hexdigest(),
                    fixture.job.job_id,
                ),
            )

        with pytest.raises((CatalogConflictError, StorageIntegrityError)):
            catalog.publish_job_outputs(
                fixture.job.job_id,
                owner_id=fixture.owner_id,
                fencing_token=fixture.fencing_token,
                outputs=(output,),
            )


def test_sqlite_publication_accepts_exactly_the_public_compound_member_cap(
    tmp_path,
) -> None:
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_revision_id = _repository(catalog)
        profiles = _profiles(catalog, ("semantic_facts",))
        fixture = _create_running_job(
            catalog,
            repository_id=repository_id,
            source_revision_id=source_revision_id,
            profiles=profiles,
            required=frozenset({"semantic_facts"}),
        )
        members = tuple(
            _object(value, media_type="application/x-fact-batch")
            for value in range(MAX_VIEW_GENERATION_MEMBERS)
        )
        output = _output(
            "semantic_facts",
            profiles["semantic_facts"],
            MAX_VIEW_GENERATION_MEMBERS + 100,
            members=members,
        )

        completed = catalog.publish_job_outputs(
            fixture.job.job_id,
            owner_id=fixture.owner_id,
            fencing_token=fixture.fencing_token,
            outputs=(output,),
        )

        assert completed.status is IndexJobStatus.SUCCEEDED
        manifest = catalog.get_manifest_summary(completed.result_snapshot_id)
        persisted = manifest["views"]["semantic_facts"]["member_objects"]
        assert len(persisted) == MAX_VIEW_GENERATION_MEMBERS
        assert persisted[0]["digest"] == members[0].digest
        assert persisted[-1]["digest"] == members[-1].digest
