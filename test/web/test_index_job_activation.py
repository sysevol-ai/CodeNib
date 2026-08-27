# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from contextlib import contextmanager

import pytest

from codenib.storage import (
    INDEX_JOB_REQUEST_CONTRACT,
    IndexJobCurrentResult,
    IndexJobRecord,
    IndexJobRequest,
    IndexJobStatus,
    IndexJobWorkerDisposition,
    IndexJobWorkerRunResult,
)
from codenib.web.index_job_activation import (
    CatalogIndexJobRuntimeReconciler,
    IndexJobActivationError,
)
from codenib.web.index_jobs import IndexJobRepoBinding

_FIRST_REF_TIME = "2026-08-27T00:00:01+00:00"
_SECOND_REF_TIME = "2026-08-27T00:00:02+00:00"


def _successful_job(
    binding: IndexJobRepoBinding,
    *,
    idempotency_key: str,
    snapshot_value: str,
    finished_at_ms: int,
    expected_ref_generation: int = 0,
    view_type: str = "bm25",
) -> IndexJobRecord:
    request = IndexJobRequest.create(
        binding.repository_id,
        "src_" + "b" * 64,
        idempotency_key,
        {
            "contract": INDEX_JOB_REQUEST_CONTRACT,
            "views": {
                view_type: {
                    "profile_id": "profile_" + "c" * 64,
                    "requested_mode": "full",
                    "required": True,
                }
            },
        },
        ref_name=binding.ref_name,
        expected_ref_generation=expected_ref_generation,
    )
    return IndexJobRecord(
        job_id=request.job_id,
        repository_id=request.repository_id,
        source_revision_id=request.source_revision_id,
        ref_name=request.ref_name,
        idempotency_key=request.idempotency_key,
        expected_ref_generation=request.expected_ref_generation,
        max_attempts=request.max_attempts,
        request_json=request.request_json,
        request_digest=request.request_digest,
        status=IndexJobStatus.SUCCEEDED,
        cancel_requested=False,
        attempt_count=1,
        result_snapshot_id="snapshot_" + snapshot_value * 64,
        error_code=None,
        error_message=None,
        created_at_ms=1,
        updated_at_ms=finished_at_ms,
        started_at_ms=2,
        finished_at_ms=finished_at_ms,
    )


def _current(
    job: IndexJobRecord,
    *,
    generation: int,
    updated_at: str,
) -> IndexJobCurrentResult:
    return IndexJobCurrentResult(
        job=job,
        ref_generation=generation,
        ref_updated_at=updated_at,
    )


class _ResultCatalog:
    def __init__(self, jobs, current) -> None:
        self.jobs = jobs
        self.current = current

    def get_job(self, job_id):
        return self.jobs[job_id]

    def find_current_successful_job(self, repository_id, ref_name="main"):
        return self.current.get((repository_id, ref_name))

    def run_current_successful_job_guarded(self, expected, transfer):
        key = (expected.job.repository_id, expected.job.ref_name)
        if self.current.get(key) != expected:
            raise RuntimeError("current result changed before guarded transfer")
        result = transfer()
        if result is not None:
            raise RuntimeError("guarded transfer returned a value")


class _Publisher:
    def __init__(self) -> None:
        self.calls = []

    def publish(self, binding, activation, *, transfer_if_current):
        transfer_if_current(lambda: self.calls.append((binding, activation)))


def _reconciler(binding, catalog, publisher):
    @contextmanager
    def factory():
        yield catalog

    return CatalogIndexJobRuntimeReconciler(factory, (binding,), publisher)


def test_reconcile_all_replays_current_result_once_per_process() -> None:
    binding = IndexJobRepoBinding("demo", "repo_" + "a" * 64)
    job = _successful_job(
        binding,
        idempotency_key="request-1",
        snapshot_value="d",
        finished_at_ms=3,
    )
    current = _current(job, generation=1, updated_at=_FIRST_REF_TIME)
    catalog = _ResultCatalog(
        {job.job_id: job},
        {(binding.repository_id, binding.ref_name): current},
    )
    publisher = _Publisher()
    reconciler = _reconciler(binding, catalog, publisher)

    first = reconciler.reconcile_all()

    assert len(first) == 1
    assert first[0].job_id == job.job_id
    assert first[0].snapshot_id == job.result_snapshot_id
    assert first[0].ref_generation == 1
    assert first[0].ref_updated_at == _FIRST_REF_TIME
    assert reconciler.reconcile_all() == ()
    assert len(publisher.calls) == 1

    restarted = _reconciler(binding, catalog, publisher)
    assert restarted.reconcile("demo") is not None
    assert len(publisher.calls) == 2


def test_reconcile_deduplicates_job_replays_by_snapshot_and_generation() -> None:
    binding = IndexJobRepoBinding("demo", "repo_" + "a" * 64)
    first = _successful_job(
        binding,
        idempotency_key="request-1",
        snapshot_value="d",
        finished_at_ms=3,
    )
    replay = _successful_job(
        binding,
        idempotency_key="request-2",
        snapshot_value="d",
        finished_at_ms=4,
        expected_ref_generation=1,
    )
    advanced = _successful_job(
        binding,
        idempotency_key="request-3",
        snapshot_value="e",
        finished_at_ms=5,
        expected_ref_generation=1,
    )
    catalog = _ResultCatalog(
        {job.job_id: job for job in (first, replay, advanced)},
        {
            (binding.repository_id, binding.ref_name): _current(
                first,
                generation=1,
                updated_at=_FIRST_REF_TIME,
            )
        },
    )
    publisher = _Publisher()
    reconciler = _reconciler(binding, catalog, publisher)

    assert reconciler.reconcile("demo") is not None
    catalog.current[(binding.repository_id, binding.ref_name)] = _current(
        replay,
        generation=1,
        updated_at=_FIRST_REF_TIME,
    )
    assert reconciler.reconcile("demo") is None
    assert len(publisher.calls) == 1

    catalog.current[(binding.repository_id, binding.ref_name)] = _current(
        advanced,
        generation=2,
        updated_at=_SECOND_REF_TIME,
    )
    activated = reconciler.reconcile("demo")
    assert activated is not None
    assert activated.job_id == advanced.job_id
    assert activated.publication_fence == (advanced.result_snapshot_id, 2)
    assert len(publisher.calls) == 2

    catalog.current[(binding.repository_id, binding.ref_name)] = _current(
        first,
        generation=1,
        updated_at=_FIRST_REF_TIME,
    )
    with pytest.raises(IndexJobActivationError, match="regressed"):
        reconciler.reconcile("demo")
    assert len(publisher.calls) == 2


def test_worker_callback_attests_attempt_then_reconciles_current_result() -> None:
    binding = IndexJobRepoBinding("demo", "repo_" + "a" * 64)
    first = _successful_job(
        binding,
        idempotency_key="request-1",
        snapshot_value="d",
        finished_at_ms=3,
    )
    current_job = _successful_job(
        binding,
        idempotency_key="request-2",
        snapshot_value="e",
        finished_at_ms=4,
        expected_ref_generation=1,
    )
    catalog = _ResultCatalog(
        {first.job_id: first, current_job.job_id: current_job},
        {
            (binding.repository_id, binding.ref_name): _current(
                current_job,
                generation=2,
                updated_at=_SECOND_REF_TIME,
            )
        },
    )
    publisher = _Publisher()
    reconciler = _reconciler(binding, catalog, publisher)

    ignored = reconciler.on_worker_result(
        IndexJobWorkerRunResult(
            IndexJobWorkerDisposition.REQUEUED,
            first.job_id,
            first.attempt_count,
        )
    )
    activated = reconciler.on_worker_result(
        IndexJobWorkerRunResult(
            IndexJobWorkerDisposition.SUCCEEDED,
            first.job_id,
            first.attempt_count,
        )
    )

    assert ignored is None
    assert activated is not None
    assert activated.job_id == current_job.job_id
    assert publisher.calls[0][1] == activated


def test_worker_callback_skips_a_success_superseded_by_non_job_publication() -> None:
    binding = IndexJobRepoBinding("demo", "repo_" + "a" * 64)
    job = _successful_job(
        binding,
        idempotency_key="request-1",
        snapshot_value="d",
        finished_at_ms=3,
    )
    catalog = _ResultCatalog({job.job_id: job}, {})
    publisher = _Publisher()
    reconciler = _reconciler(binding, catalog, publisher)

    result = reconciler.on_worker_result(
        IndexJobWorkerRunResult(
            IndexJobWorkerDisposition.SUCCEEDED,
            job.job_id,
            job.attempt_count,
        )
    )

    assert result is None
    assert publisher.calls == []


def test_worker_callback_rejects_wrong_attempt_and_unsupported_result() -> None:
    binding = IndexJobRepoBinding("demo", "repo_" + "a" * 64)
    bm25 = _successful_job(
        binding,
        idempotency_key="request-1",
        snapshot_value="d",
        finished_at_ms=3,
    )
    vector = _successful_job(
        binding,
        idempotency_key="request-2",
        snapshot_value="e",
        finished_at_ms=4,
        view_type="vector",
    )
    catalog = _ResultCatalog(
        {bm25.job_id: bm25, vector.job_id: vector},
        {
            (binding.repository_id, binding.ref_name): _current(
                vector,
                generation=1,
                updated_at=_FIRST_REF_TIME,
            )
        },
    )
    reconciler = _reconciler(binding, catalog, _Publisher())

    with pytest.raises(IndexJobActivationError, match="another attempt"):
        reconciler.on_worker_result(
            IndexJobWorkerRunResult(
                IndexJobWorkerDisposition.SUCCEEDED,
                bm25.job_id,
                2,
            )
        )
    with pytest.raises(IndexJobActivationError, match="successful BM25"):
        reconciler.reconcile("demo")


def test_failed_runtime_publication_remains_retryable() -> None:
    binding = IndexJobRepoBinding("demo", "repo_" + "a" * 64)
    job = _successful_job(
        binding,
        idempotency_key="request-1",
        snapshot_value="d",
        finished_at_ms=3,
    )
    catalog = _ResultCatalog(
        {job.job_id: job},
        {
            (binding.repository_id, binding.ref_name): _current(
                job,
                generation=1,
                updated_at=_FIRST_REF_TIME,
            )
        },
    )

    class FlakyPublisher(_Publisher):
        def publish(self, binding, activation, *, transfer_if_current):
            super().publish(
                binding,
                activation,
                transfer_if_current=transfer_if_current,
            )
            if len(self.calls) == 1:
                raise RuntimeError("private materialization failure")

    publisher = FlakyPublisher()
    reconciler = _reconciler(binding, catalog, publisher)

    with pytest.raises(IndexJobActivationError, match="publication failed") as error:
        reconciler.reconcile("demo")
    assert "private" not in str(error.value)
    assert reconciler.reconcile("demo") is not None
    assert len(publisher.calls) == 2


def test_reconcile_all_does_not_starve_later_repositories_after_failure() -> None:
    first_binding = IndexJobRepoBinding("alpha", "repo_" + "a" * 64)
    second_binding = IndexJobRepoBinding("beta", "repo_" + "b" * 64)
    first_job = _successful_job(
        first_binding,
        idempotency_key="request-1",
        snapshot_value="d",
        finished_at_ms=3,
    )
    second_job = _successful_job(
        second_binding,
        idempotency_key="request-2",
        snapshot_value="e",
        finished_at_ms=4,
    )
    catalog = _ResultCatalog(
        {first_job.job_id: first_job, second_job.job_id: second_job},
        {
            (first_binding.repository_id, first_binding.ref_name): _current(
                first_job,
                generation=1,
                updated_at=_FIRST_REF_TIME,
            ),
            (second_binding.repository_id, second_binding.ref_name): _current(
                second_job,
                generation=1,
                updated_at=_FIRST_REF_TIME,
            ),
        },
    )

    class FirstTargetFails(_Publisher):
        def publish(self, binding, activation, *, transfer_if_current):
            super().publish(
                binding,
                activation,
                transfer_if_current=transfer_if_current,
            )
            if binding is first_binding:
                raise RuntimeError("private first-target failure")

    publisher = FirstTargetFails()

    @contextmanager
    def factory():
        yield catalog

    reconciler = CatalogIndexJobRuntimeReconciler(
        factory,
        (second_binding, first_binding),
        publisher,
    )

    with pytest.raises(IndexJobActivationError, match="publication failed") as error:
        reconciler.reconcile_all()

    assert "private" not in str(error.value)
    assert [binding.repo_id for binding, _activation in publisher.calls] == [
        "alpha",
        "beta",
    ]

    with pytest.raises(IndexJobActivationError, match="publication failed"):
        reconciler.reconcile_all()
    assert [binding.repo_id for binding, _activation in publisher.calls] == [
        "alpha",
        "beta",
        "alpha",
    ]


def test_runtime_transfer_is_guarded_by_the_exact_current_result() -> None:
    binding = IndexJobRepoBinding("demo", "repo_" + "a" * 64)
    job = _successful_job(
        binding,
        idempotency_key="request-1",
        snapshot_value="d",
        finished_at_ms=3,
    )
    storage_key = (binding.repository_id, binding.ref_name)
    current = _current(
        job,
        generation=1,
        updated_at=_FIRST_REF_TIME,
    )
    catalog = _ResultCatalog({job.job_id: job}, {storage_key: current})

    class SupersededPublisher(_Publisher):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def publish(self, binding, activation, *, transfer_if_current):
            self.attempts += 1
            if self.attempts == 1:
                catalog.current.pop(storage_key)
                transfer_if_current(lambda: self.calls.append((binding, activation)))
                raise AssertionError("a superseded snapshot reached runtime transfer")
            super().publish(
                binding,
                activation,
                transfer_if_current=transfer_if_current,
            )

    publisher = SupersededPublisher()
    reconciler = _reconciler(binding, catalog, publisher)

    with pytest.raises(IndexJobActivationError, match="publication failed") as error:
        reconciler.reconcile("demo")

    assert "changed during" not in str(error.value)
    assert publisher.calls == []

    catalog.current[storage_key] = current
    assert reconciler.reconcile("demo") is not None
    assert len(publisher.calls) == 1


def test_reconciler_rejects_invalid_catalog_and_publisher_results() -> None:
    binding = IndexJobRepoBinding("demo", "repo_" + "a" * 64)

    class InvalidCatalog:
        pass

    with pytest.raises(IndexJobActivationError, match="guarded current-result"):
        _reconciler(binding, InvalidCatalog(), _Publisher()).reconcile("demo")

    job = _successful_job(
        binding,
        idempotency_key="request-1",
        snapshot_value="d",
        finished_at_ms=3,
    )
    catalog = _ResultCatalog(
        {job.job_id: job},
        {
            (binding.repository_id, binding.ref_name): _current(
                job,
                generation=1,
                updated_at=_FIRST_REF_TIME,
            )
        },
    )

    class InvalidPublisher(_Publisher):
        def publish(self, binding, activation, *, transfer_if_current):
            super().publish(
                binding,
                activation,
                transfer_if_current=transfer_if_current,
            )
            return True

    publisher = InvalidPublisher()
    reconciler = _reconciler(binding, catalog, publisher)
    with pytest.raises(IndexJobActivationError, match="invalid result"):
        reconciler.reconcile("demo")
    with pytest.raises(IndexJobActivationError, match="invalid result"):
        reconciler.reconcile("demo")
    assert len(publisher.calls) == 2

    class UnguardedPublisher(_Publisher):
        def publish(self, binding, activation, *, transfer_if_current):
            self.calls.append((binding, activation))

    unguarded = UnguardedPublisher()
    reconciler = _reconciler(binding, catalog, unguarded)
    with pytest.raises(IndexJobActivationError, match="skipped guarded"):
        reconciler.reconcile("demo")
    assert len(unguarded.calls) == 1

    class RepeatedGuardPublisher(_Publisher):
        def publish(self, binding, activation, *, transfer_if_current):
            def transfer():
                self.calls.append((binding, activation))

            transfer_if_current(transfer)
            with pytest.raises(IndexJobActivationError, match="invalid guarded"):
                transfer_if_current(transfer)

    repeated = RepeatedGuardPublisher()
    reconciler = _reconciler(binding, catalog, repeated)
    with pytest.raises(IndexJobActivationError, match="skipped guarded"):
        reconciler.reconcile("demo")
    assert len(repeated.calls) == 1
