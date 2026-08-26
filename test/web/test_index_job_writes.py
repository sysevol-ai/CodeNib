# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

import codenib.web.app as web_app
from codenib.storage import (
    INDEX_JOB_REQUEST_CONTRACT,
    IndexJobRecord,
    IndexJobRequest,
    IndexJobStatus,
    JobCatalog,
    JobCreationCatalog,
    JobCreationReplayCatalog,
    SQLiteCatalog,
)
from codenib.web.index_job_writes import (
    CatalogIndexJobWriter,
    IndexJobConflictError,
    IndexJobCreatePlan,
    IndexJobRequestError,
    IndexJobWriteError,
)
from codenib.web.index_jobs import IndexJobNotFoundError, IndexJobRepoBinding
from codenib.web.schemas import IndexJobStatusResponse, IndexJobSurface


class _Planner:
    def __init__(self, plan: IndexJobCreatePlan) -> None:
        self.result = plan
        self.calls: list[tuple[IndexJobRepoBinding, str, str]] = []

    def plan(
        self,
        binding: IndexJobRepoBinding,
        index_type: str,
        *,
        idempotency_key: str,
    ) -> IndexJobCreatePlan:
        self.calls.append((binding, index_type, idempotency_key))
        return self.result


def _sqlite_writer(tmp_path, index_type: str):
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        repository_id = catalog.create_repository("owner/repo")
        source_revision_id = catalog.create_source_revision(
            repository_id,
            commit_sha="a" * 40,
            tree_sha="b" * 64,
        )
        profile_id = catalog.create_view_profile(index_type, {})
    binding = IndexJobRepoBinding("demo", repository_id)
    planner = _Planner(
        IndexJobCreatePlan(
            source_revision_id=source_revision_id,
            profile_id=profile_id,
            expected_ref_generation=0,
        )
    )

    @contextmanager
    def factory():
        with SQLiteCatalog(path, create=False) as catalog:
            yield catalog

    return CatalogIndexJobWriter(factory, (binding,), planner), path, planner, binding


def _queued_job(
    repository_id: str,
    source_revision_id: str,
    idempotency_key: str,
    request,
    *,
    ref_name: str,
    expected_ref_generation: int,
    max_attempts: int,
) -> IndexJobRecord:
    planned = IndexJobRequest.create(
        repository_id,
        source_revision_id,
        idempotency_key,
        request,
        ref_name=ref_name,
        expected_ref_generation=expected_ref_generation,
        max_attempts=max_attempts,
    )
    return IndexJobRecord(
        job_id=planned.job_id,
        repository_id=planned.repository_id,
        source_revision_id=planned.source_revision_id,
        ref_name=planned.ref_name,
        idempotency_key=planned.idempotency_key,
        expected_ref_generation=planned.expected_ref_generation,
        max_attempts=planned.max_attempts,
        request_json=planned.request_json,
        request_digest=planned.request_digest,
        status=IndexJobStatus.QUEUED,
        cancel_requested=False,
        attempt_count=0,
        result_snapshot_id=None,
        error_code=None,
        error_message=None,
        created_at_ms=1,
        updated_at_ms=1,
        started_at_ms=None,
        finished_at_ms=None,
    )


def _response() -> IndexJobStatusResponse:
    return IndexJobStatusResponse(
        job_id="job_" + "a" * 64,
        repo_id="demo",
        status="queued",
        cancel_requested=False,
        attempt_count=0,
        max_attempts=3,
        indexes=[
            IndexJobSurface(
                index_type="bm25",
                requested_mode="full",
                required=True,
            )
        ],
        created_at_ms=1,
        updated_at_ms=1,
        next_event_sequence=0,
    )


@pytest.mark.parametrize("index_type", ("bm25", "vector"))
def test_catalog_writer_creates_and_attests_one_full_job(
    tmp_path,
    index_type,
) -> None:
    writer, path, planner, binding = _sqlite_writer(tmp_path, index_type)

    created = writer.create(
        "demo",
        indexes=(index_type,),
        mode="full",
        force=False,
        idempotency_key="browser-request-1",
    )
    initial_plan = planner.result
    planner.result = IndexJobCreatePlan(
        source_revision_id="src_" + "d" * 64,
        profile_id="profile_" + "e" * 64,
        expected_ref_generation=99,
    )
    replayed = writer.create(
        "demo",
        indexes=(index_type,),
        mode="full",
        force=False,
        idempotency_key="browser-request-1",
    )

    assert created == replayed
    assert created.status == "queued"
    assert created.repo_id == "demo"
    assert created.indexes[0].index_type == index_type
    assert created.indexes[0].requested_mode == "full"
    serialized = created.model_dump_json()
    assert initial_plan.source_revision_id not in serialized
    assert initial_plan.profile_id not in serialized
    assert planner.calls == [(binding, index_type, "browser-request-1")]
    with SQLiteCatalog(path, create=False) as catalog:
        job = catalog.get_job(created.job_id)
        views = catalog.get_job_views(created.job_id)
    assert job.repository_id == binding.repository_id
    assert job.ref_name == binding.ref_name
    assert len(views) == 1
    assert views[0].view_type == index_type
    assert views[0].required is True

    with pytest.raises(IndexJobConflictError):
        writer.create(
            "demo",
            indexes=(index_type,),
            mode="full",
            force=False,
            idempotency_key="browser-request-2",
        )


def test_catalog_writer_requires_only_creation_and_exact_replay_authority() -> None:
    binding = IndexJobRepoBinding("demo", "repo_" + "a" * 64)
    plan = IndexJobCreatePlan(
        source_revision_id="src_" + "b" * 64,
        profile_id="profile_" + "c" * 64,
        expected_ref_generation=4,
    )

    class CreationOnlyCatalog:
        def find_job_by_idempotency(self, repository_id, idempotency_key):
            return None

        def create_job_if_idle(
            self,
            repository_id,
            source_revision_id,
            idempotency_key,
            request,
            *,
            ref_name="main",
            expected_ref_generation=0,
            max_attempts=3,
        ):
            return _queued_job(
                repository_id,
                source_revision_id,
                idempotency_key,
                request,
                ref_name=ref_name,
                expected_ref_generation=expected_ref_generation,
                max_attempts=max_attempts,
            )

    catalog = CreationOnlyCatalog()
    assert isinstance(catalog, JobCreationCatalog)
    assert isinstance(catalog, JobCreationReplayCatalog)
    assert not isinstance(catalog, JobCatalog)

    @contextmanager
    def factory():
        yield catalog

    writer = CatalogIndexJobWriter(factory, (binding,), _Planner(plan))

    assert (
        writer.create(
            "demo",
            indexes=("vector",),
            mode="full",
            force=False,
            idempotency_key="request",
        ).status
        == "queued"
    )


def test_catalog_writer_rejects_changed_public_request_without_replanning(
    tmp_path,
) -> None:
    writer, _path, planner, _binding = _sqlite_writer(tmp_path, "bm25")
    writer.create(
        "demo",
        indexes=("bm25",),
        mode="full",
        force=False,
        idempotency_key="browser-request",
    )

    with pytest.raises(IndexJobConflictError, match="another index-job request"):
        writer.create(
            "demo",
            indexes=("vector",),
            mode="full",
            force=False,
            idempotency_key="browser-request",
        )

    assert len(planner.calls) == 1


def test_catalog_writer_attests_exact_replay_candidate_without_replanning() -> None:
    binding = IndexJobRepoBinding("demo", "repo_" + "a" * 64)
    plan = IndexJobCreatePlan(
        source_revision_id="src_" + "b" * 64,
        profile_id="profile_" + "c" * 64,
        expected_ref_generation=0,
    )
    returned = _queued_job(
        binding.repository_id,
        plan.source_revision_id,
        "another-request",
        {
            "contract": INDEX_JOB_REQUEST_CONTRACT,
            "views": {
                "bm25": {
                    "profile_id": plan.profile_id,
                    "requested_mode": "full",
                    "required": True,
                }
            },
        },
        ref_name=binding.ref_name,
        expected_ref_generation=plan.expected_ref_generation,
        max_attempts=plan.max_attempts,
    )

    class WrongReplayCatalog:
        def find_job_by_idempotency(self, repository_id, idempotency_key):
            assert (repository_id, idempotency_key) == (
                binding.repository_id,
                "request",
            )
            return returned

        def create_job_if_idle(self, *args, **kwargs):
            raise AssertionError("an existing replay candidate must not create a job")

    @contextmanager
    def factory():
        yield WrongReplayCatalog()

    planner = _Planner(plan)
    writer = CatalogIndexJobWriter(factory, (binding,), planner)

    with pytest.raises(IndexJobWriteError, match="different replay candidate"):
        writer.create(
            "demo",
            indexes=("bm25",),
            mode="full",
            force=False,
            idempotency_key="request",
        )

    assert planner.calls == []


@pytest.mark.parametrize(
    ("indexes", "mode", "force", "message"),
    (
        (("bm25", "vector"), "full", False, "exactly one"),
        (("symbol_graph",), "full", False, "symbol graph"),
        (("unknown",), "full", False, "surfaces"),
        (("bm25",), "auto", False, "mode"),
        (("vector",), "incremental", False, "incremental"),
        (("bm25",), "full", True, "forced"),
    ),
)
def test_catalog_writer_rejects_unimplemented_worker_capabilities(
    indexes,
    mode,
    force,
    message,
) -> None:
    binding = IndexJobRepoBinding("demo", "repo_" + "a" * 64)
    planner = _Planner(
        IndexJobCreatePlan(
            source_revision_id="src_" + "b" * 64,
            profile_id="profile_" + "c" * 64,
            expected_ref_generation=0,
        )
    )

    @contextmanager
    def unused_factory():
        raise AssertionError("unsupported requests must not open the catalog")
        yield

    writer = CatalogIndexJobWriter(unused_factory, (binding,), planner)

    with pytest.raises(IndexJobRequestError, match=message):
        writer.create(
            "demo",
            indexes=indexes,
            mode=mode,
            force=force,
            idempotency_key="request",
        )
    assert planner.calls == []


def test_catalog_writer_hides_unknown_bindings_and_invalid_plans() -> None:
    binding = IndexJobRepoBinding("demo", "repo_" + "a" * 64)

    @contextmanager
    def unused_factory():
        raise AssertionError("invalid plans must not mutate the catalog")
        yield

    valid = IndexJobCreatePlan(
        source_revision_id="src_" + "b" * 64,
        profile_id="profile_" + "c" * 64,
        expected_ref_generation=0,
    )
    writer = CatalogIndexJobWriter(unused_factory, (binding,), _Planner(valid))
    with pytest.raises(IndexJobNotFoundError):
        writer.create(
            "unknown",
            indexes=("bm25",),
            mode="full",
            force=False,
            idempotency_key="request",
        )

    class InvalidPlanner:
        def plan(self, binding, index_type, *, idempotency_key):
            return object()

    class ReplayOnlyCatalog:
        def find_job_by_idempotency(self, repository_id, idempotency_key):
            return None

        def create_job_if_idle(self, *args, **kwargs):
            raise AssertionError("invalid plans must not create a job")

    @contextmanager
    def replay_factory():
        yield ReplayOnlyCatalog()

    writer = CatalogIndexJobWriter(replay_factory, (binding,), InvalidPlanner())
    with pytest.raises(IndexJobWriteError, match="invalid creation plan"):
        writer.create(
            "demo",
            indexes=("bm25",),
            mode="full",
            force=False,
            idempotency_key="request",
        )


def test_create_endpoint_requires_header_and_uses_injected_writer(monkeypatch) -> None:
    expected = _response()
    calls = []

    class Writer:
        def create(self, repo_id, **kwargs):
            calls.append((repo_id, kwargs))
            return expected

    monkeypatch.setattr(web_app.app.state, "index_job_writer", Writer(), raising=False)
    client = TestClient(web_app.app)

    missing = client.post(
        "/api/repos/demo/index-jobs",
        json={"indexes": ["bm25"], "mode": "full"},
    )
    duplicate = client.post(
        "/api/repos/demo/index-jobs",
        headers={"Idempotency-Key": "request"},
        json={"indexes": ["bm25", "bm25"], "mode": "full"},
    )
    coerced_force = client.post(
        "/api/repos/demo/index-jobs",
        headers={"Idempotency-Key": "request"},
        json={"indexes": ["bm25"], "mode": "full", "force": "false"},
    )
    unknown_field = client.post(
        "/api/repos/demo/index-jobs",
        headers={"Idempotency-Key": "request"},
        json={"indexes": ["bm25"], "mode": "full", "unknown": True},
    )
    created = client.post(
        "/api/repos/demo/index-jobs",
        headers={"Idempotency-Key": "request"},
        json={"indexes": ["bm25"], "mode": "full", "force": False},
    )

    assert missing.status_code == 422
    assert duplicate.status_code == 422
    assert coerced_force.status_code == 422
    assert unknown_field.status_code == 422
    assert created.status_code == 202
    assert created.json()["job_id"] == expected.job_id
    assert calls == [
        (
            "demo",
            {
                "indexes": ("bm25",),
                "mode": "full",
                "force": False,
                "idempotency_key": "request",
            },
        )
    ]


def test_create_endpoint_is_unavailable_without_writer(monkeypatch) -> None:
    monkeypatch.delattr(web_app.app.state, "index_job_writer", raising=False)
    client = TestClient(web_app.app)

    response = client.post(
        "/api/repos/demo/index-jobs",
        headers={"Idempotency-Key": "request"},
        json={"indexes": ["bm25"], "mode": "full"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Durable index job creation is not configured"}


@pytest.mark.parametrize(
    ("error", "status_code", "detail"),
    (
        (
            IndexJobNotFoundError("private binding"),
            404,
            "Repository is not configured for index updates",
        ),
        (
            IndexJobRequestError("incremental updates are unavailable"),
            422,
            "incremental updates are unavailable",
        ),
        (
            IndexJobConflictError("private conflict"),
            409,
            "An index update is already active or the idempotency key conflicts",
        ),
        (
            IndexJobWriteError("private storage failure"),
            503,
            "Durable index job creation is unavailable",
        ),
    ),
)
def test_create_endpoint_maps_writer_errors_without_private_details(
    monkeypatch,
    error,
    status_code,
    detail,
) -> None:
    class Writer:
        def create(self, repo_id, **kwargs):
            raise error

    monkeypatch.setattr(web_app.app.state, "index_job_writer", Writer(), raising=False)

    response = TestClient(web_app.app).post(
        "/api/repos/demo/index-jobs",
        headers={"Idempotency-Key": "request"},
        json={"indexes": ["bm25"], "mode": "full"},
    )

    assert response.status_code == status_code
    assert response.json() == {"detail": detail}
    assert "private" not in response.text
