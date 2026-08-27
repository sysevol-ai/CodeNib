# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Restart-safe current-job reconciliation for Web runtime publishers."""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from dataclasses import dataclass
from threading import Lock, RLock
from typing import Callable, Protocol, runtime_checkable

from ..storage import (
    IndexJobCurrentResult,
    IndexJobRecord,
    IndexJobRequest,
    IndexJobRequestedMode,
    IndexJobStatus,
    IndexJobWorkerDisposition,
    IndexJobWorkerRunResult,
    JobResultActivationCatalog,
    StorageError,
)
from ..storage.models import canonical_utc_timestamp
from .index_jobs import IndexJobRepoBinding, _canonical_text

_CATALOG_INT64_MAX = 2**63 - 1


class IndexJobActivationError(RuntimeError):
    """A durable current result could not be safely reconciled."""


@dataclass(frozen=True, slots=True)
class IndexJobRuntimeActivation:
    """Detached identity of one exact current BM25 publication."""

    repo_id: str
    repository_id: str
    ref_name: str
    job_id: str
    attempt_count: int
    snapshot_id: str
    ref_generation: int
    ref_updated_at: str
    finished_at_ms: int

    def __post_init__(self) -> None:
        if type(self) is not IndexJobRuntimeActivation:
            raise TypeError("runtime activation must use the exact type")
        for field, label, maximum in (
            ("repo_id", "Web repository ID", 512),
            ("repository_id", "repository ID", 96),
            ("ref_name", "ref name", 512),
            ("job_id", "job ID", 80),
            ("snapshot_id", "snapshot ID", 96),
        ):
            object.__setattr__(
                self,
                field,
                _canonical_text(getattr(self, field), label=label, max_length=maximum),
            )
        if type(self.attempt_count) is not int or not 1 <= self.attempt_count <= 1_000:
            raise ValueError("runtime activation attempt count is invalid")
        if (
            type(self.ref_generation) is not int
            or not 1 <= self.ref_generation <= _CATALOG_INT64_MAX
        ):
            raise ValueError("runtime activation ref generation is invalid")
        if type(self.finished_at_ms) is not int or not 0 <= self.finished_at_ms:
            raise ValueError("runtime activation completion time is invalid")
        object.__setattr__(
            self,
            "ref_updated_at",
            canonical_utc_timestamp(
                self.ref_updated_at,
                "runtime activation ref updated_at",
            ),
        )

    @property
    def publication_fence(self) -> tuple[str, int]:
        """Return the ref identity used for in-process activation deduplication."""

        return self.snapshot_id, self.ref_generation


@runtime_checkable
class IndexJobRuntimePublisher(Protocol):
    """Publish one exact snapshot with idempotent, monotonic retry semantics.

    Implementations own materialization and runtime-generation transfer. A
    successful return must mean the complete generation is published. They
    must treat the same snapshot/generation fence idempotently and must not
    replace a newer incumbent generation with an older one. Raising must leave
    the incumbent usable and retain any retryable cleanup authority. After
    materialization, implementations must synchronously invoke
    ``transfer_if_current`` exactly once with the complete atomic runtime
    transfer. The guard revalidates the durable result while excluding ref
    writers and does not release that fence until the transfer returns.
    Implementations must not transfer outside or retain either callback.
    """

    def publish(
        self,
        binding: IndexJobRepoBinding,
        activation: IndexJobRuntimeActivation,
        *,
        transfer_if_current: Callable[[Callable[[], None]], None],
    ) -> None: ...


def _attest_successful_bm25_job(
    binding: IndexJobRepoBinding,
    value: object,
    *,
    expected_job_id: str | None = None,
    expected_attempt_count: int | None = None,
) -> IndexJobRecord:
    if type(value) is not IndexJobRecord:
        raise IndexJobActivationError("catalog returned an invalid successful job")
    job = value
    try:
        request = IndexJobRequest(
            repository_id=job.repository_id,
            source_revision_id=job.source_revision_id,
            ref_name=job.ref_name,
            idempotency_key=job.idempotency_key,
            expected_ref_generation=job.expected_ref_generation,
            max_attempts=job.max_attempts,
            request_json=job.request_json,
        )
        views = request.view_requests
        if (
            request.job_id != job.job_id
            or request.request_digest != job.request_digest
            or request.repository_id != binding.repository_id
            or request.ref_name != binding.ref_name
            or job.status is not IndexJobStatus.SUCCEEDED
            or job.cancel_requested
            or job.result_snapshot_id is None
            or job.error_code is not None
            or job.error_message is not None
            or job.started_at_ms is None
            or job.finished_at_ms is None
            or job.updated_at_ms != job.finished_at_ms
            or len(views) != 1
            or views[0].view_type != "bm25"
            or views[0].requested_mode is not IndexJobRequestedMode.FULL
            or views[0].required is not True
        ):
            raise IndexJobActivationError(
                "durable job is not an exact successful BM25 result"
            )
        if expected_job_id is not None and job.job_id != expected_job_id:
            raise IndexJobActivationError("worker result identifies another job")
        if (
            expected_attempt_count is not None
            and job.attempt_count != expected_attempt_count
        ):
            raise IndexJobActivationError("worker result identifies another attempt")
        return job
    except IndexJobActivationError:
        raise
    except (StorageError, TypeError, ValueError) as exc:
        raise IndexJobActivationError(
            "durable successful job could not be safely attested"
        ) from exc


def _attest_current_result(
    binding: IndexJobRepoBinding,
    value: object,
) -> IndexJobRuntimeActivation:
    if type(value) is not IndexJobCurrentResult:
        raise IndexJobActivationError("catalog returned an invalid current job result")
    try:
        current = IndexJobCurrentResult(
            job=value.job,
            ref_generation=value.ref_generation,
            ref_updated_at=value.ref_updated_at,
        )
        job = _attest_successful_bm25_job(binding, current.job)
        if job.result_snapshot_id != current.snapshot_id:
            raise IndexJobActivationError(
                "current job result changed its snapshot identity"
            )
        finished_at_ms = job.finished_at_ms
        if finished_at_ms is None:  # pragma: no cover - job attestation proves success
            raise IndexJobActivationError("current job result has no completion time")
        return IndexJobRuntimeActivation(
            repo_id=binding.repo_id,
            repository_id=job.repository_id,
            ref_name=job.ref_name,
            job_id=job.job_id,
            attempt_count=job.attempt_count,
            snapshot_id=current.snapshot_id,
            ref_generation=current.ref_generation,
            ref_updated_at=current.ref_updated_at,
            finished_at_ms=finished_at_ms,
        )
    except IndexJobActivationError:
        raise
    except (StorageError, TypeError, ValueError) as exc:
        raise IndexJobActivationError(
            "durable current job result could not be safely attested"
        ) from exc


class CatalogIndexJobRuntimeReconciler:
    """Reconcile current durable BM25 results through an injected publisher.

    Worker callbacks trigger the fast path. ``reconcile_all`` is the durable
    startup/retry path and reads only publications that still equal the current
    ref instead of relying on callback memory or latest-completion ordering.
    """

    def __init__(
        self,
        catalog_factory: Callable[
            [],
            AbstractContextManager[JobResultActivationCatalog],
        ],
        bindings: tuple[IndexJobRepoBinding, ...],
        publisher: IndexJobRuntimePublisher,
    ) -> None:
        if not callable(catalog_factory):
            raise TypeError("job result catalog factory must be callable")
        if type(bindings) is not tuple or not bindings:
            raise ValueError("runtime reconciler requires repository bindings")
        if any(type(binding) is not IndexJobRepoBinding for binding in bindings):
            raise TypeError("runtime reconciler bindings must use exact values")
        if not isinstance(publisher, IndexJobRuntimePublisher):
            raise TypeError("runtime reconciler requires a snapshot publisher")

        by_repo: dict[str, IndexJobRepoBinding] = {}
        by_storage: dict[tuple[str, str], IndexJobRepoBinding] = {}
        for binding in bindings:
            storage_key = (binding.repository_id, binding.ref_name)
            if binding.repo_id in by_repo or storage_key in by_storage:
                raise ValueError("runtime reconciler bindings must be unique")
            by_repo[binding.repo_id] = binding
            by_storage[storage_key] = binding
        self._catalog_factory = catalog_factory
        self._by_repo = by_repo
        self._by_storage = by_storage
        self._publisher = publisher
        self._published: dict[tuple[str, str], tuple[str, int]] = {}
        self._lock = RLock()

    @staticmethod
    def _require_catalog(value: object) -> JobResultActivationCatalog:
        if not isinstance(value, JobResultActivationCatalog):
            raise IndexJobActivationError(
                "catalog does not implement guarded current-result activation"
            )
        return value

    def _read_job(self, job_id: str) -> IndexJobRecord:
        try:
            with self._catalog_factory() as value:
                return self._require_catalog(value).get_job(job_id)
        except IndexJobActivationError:
            raise
        except (OSError, sqlite3.Error, StorageError) as exc:
            raise IndexJobActivationError(
                "durable successful-job lookup is unavailable"
            ) from exc
        except Exception as exc:
            raise IndexJobActivationError(
                "durable successful-job lookup failed"
            ) from exc

    def _read_current(
        self,
        binding: IndexJobRepoBinding,
    ) -> IndexJobCurrentResult | None:
        try:
            with self._catalog_factory() as value:
                return self._require_catalog(value).find_current_successful_job(
                    binding.repository_id,
                    binding.ref_name,
                )
        except IndexJobActivationError:
            raise
        except (OSError, sqlite3.Error, StorageError) as exc:
            raise IndexJobActivationError(
                "durable current-result reconciliation is unavailable"
            ) from exc
        except Exception as exc:
            raise IndexJobActivationError(
                "durable current-result reconciliation failed"
            ) from exc

    def _reconcile_binding(
        self,
        binding: IndexJobRepoBinding,
    ) -> IndexJobRuntimeActivation | None:
        value = self._read_current(binding)
        if value is None:
            return None
        activation = _attest_current_result(binding, value)
        storage_key = (binding.repository_id, binding.ref_name)
        published = self._published.get(storage_key)
        already_published = published == activation.publication_fence
        if published is not None and (
            activation.ref_generation < published[1]
            or (
                activation.ref_generation == published[1]
                and activation.snapshot_id != published[0]
            )
        ):
            raise IndexJobActivationError(
                "durable current result regressed its runtime publication fence"
            )

        publication_lock = Lock()
        publication_state = "open"

        def transfer_if_current(transfer: Callable[[], None]) -> None:
            nonlocal publication_state
            with publication_lock:
                if publication_state != "open" or not callable(transfer):
                    if publication_state != "closed":
                        publication_state = "failed"
                    raise IndexJobActivationError(
                        "runtime snapshot publisher used an invalid guarded transfer"
                    )
                publication_state = "guarding"

            def guarded_transfer() -> None:
                nonlocal publication_state
                with publication_lock:
                    if publication_state != "guarding":
                        if publication_state != "closed":
                            publication_state = "failed"
                        raise IndexJobActivationError(
                            "current-result catalog used an invalid runtime transfer"
                        )
                    publication_state = "transferring"
                try:
                    transfer_result = transfer()
                except BaseException:
                    with publication_lock:
                        if publication_state == "transferring":
                            publication_state = "failed"
                    raise
                with publication_lock:
                    if publication_state != "transferring":
                        if publication_state != "closed":
                            publication_state = "failed"
                        raise IndexJobActivationError(
                            "current-result catalog used an invalid runtime transfer"
                        )
                    if transfer_result is not None:
                        publication_state = "failed"
                        raise IndexJobActivationError(
                            "runtime snapshot transfer returned an invalid result"
                        )
                    publication_state = "transferred"

            try:
                with self._catalog_factory() as candidate:
                    guard_result = self._require_catalog(
                        candidate
                    ).run_current_successful_job_guarded(
                        value,
                        guarded_transfer,
                    )
                if guard_result is not None:
                    raise IndexJobActivationError(
                        "current-result activation guard returned an invalid result"
                    )
            except BaseException:
                with publication_lock:
                    if publication_state != "closed":
                        publication_state = "failed"
                raise
            with publication_lock:
                if publication_state != "transferred":
                    if publication_state != "closed":
                        publication_state = "failed"
                    raise IndexJobActivationError(
                        "current-result activation guard skipped runtime transfer"
                    )
                publication_state = "published"

        try:
            try:
                result = self._publisher.publish(
                    binding,
                    activation,
                    transfer_if_current=transfer_if_current,
                )
            finally:
                with publication_lock:
                    guarded = publication_state == "published"
                    publication_state = "closed"
        except Exception as exc:
            raise IndexJobActivationError(
                "durable runtime snapshot publication failed"
            ) from exc
        if result is not None:
            raise IndexJobActivationError(
                "runtime snapshot publisher returned an invalid result"
            )
        if not guarded:
            raise IndexJobActivationError(
                "runtime snapshot publisher skipped guarded runtime transfer"
            )
        self._published[storage_key] = activation.publication_fence
        return None if already_published else activation

    def reconcile(self, repo_id: str) -> IndexJobRuntimeActivation | None:
        """Publish the current durable result for one configured repository."""

        try:
            normalized = _canonical_text(
                repo_id,
                label="Web repository ID",
                max_length=512,
            )
        except (TypeError, ValueError) as exc:
            raise IndexJobActivationError(
                "Web repository has no runtime activation binding"
            ) from exc
        binding = self._by_repo.get(normalized)
        if binding is None:
            raise IndexJobActivationError(
                "Web repository has no runtime activation binding"
            )
        with self._lock:
            return self._reconcile_binding(binding)

    def reconcile_all(self) -> tuple[IndexJobRuntimeActivation, ...]:
        """Replay every configured repository's current durable result."""

        reconciled: list[IndexJobRuntimeActivation] = []
        first_failure: IndexJobActivationError | None = None
        with self._lock:
            for repo_id in sorted(self._by_repo):
                try:
                    value = self._reconcile_binding(self._by_repo[repo_id])
                except IndexJobActivationError as failure:
                    # One persistently broken target must not starve every
                    # later repository. Successful publications remain
                    # deduplicated even though the first sanitized failure is
                    # reported after the complete pass.
                    if first_failure is None:
                        first_failure = failure
                    continue
                if value is not None:
                    reconciled.append(value)
        if first_failure is not None:
            raise first_failure
        return tuple(reconciled)

    def on_worker_result(
        self,
        result: IndexJobWorkerRunResult,
    ) -> IndexJobRuntimeActivation | None:
        """Attest one worker success, then reconcile the current ref result."""

        if type(result) is not IndexJobWorkerRunResult:
            raise IndexJobActivationError("worker returned an invalid job result")
        if result.disposition is not IndexJobWorkerDisposition.SUCCEEDED:
            return None
        if result.job_id is None or result.attempt_count is None:
            raise IndexJobActivationError("successful worker result lacks identity")

        with self._lock:
            job = self._read_job(result.job_id)
            if type(job) is not IndexJobRecord:
                raise IndexJobActivationError(
                    "catalog returned an invalid worker result job"
                )
            binding = self._by_storage.get((job.repository_id, job.ref_name))
            if binding is None:
                raise IndexJobActivationError(
                    "successful worker result has no runtime activation binding"
                )
            _attest_successful_bm25_job(
                binding,
                job,
                expected_job_id=result.job_id,
                expected_attempt_count=result.attempt_count,
            )
            return self._reconcile_binding(binding)


__all__ = [
    "CatalogIndexJobRuntimeReconciler",
    "IndexJobActivationError",
    "IndexJobRuntimeActivation",
    "IndexJobRuntimePublisher",
]
