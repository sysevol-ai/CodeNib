# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Least-authority durable index-job creation for the Web control plane."""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

from ..storage import (
    INDEX_JOB_REQUEST_CONTRACT,
    IndexJobRecord,
    IndexJobRequest,
    IndexJobStatus,
    JobCreationCatalog,
    PublishConflict,
    StorageError,
)
from .index_jobs import (
    IndexJobNotFoundError,
    IndexJobRepoBinding,
    _canonical_text,
    _safe_error_code,
    _safe_error_message,
)
from .schemas import IndexJobStatusResponse, IndexJobSurface

_SUPPORTED_WRITE_INDEXES = frozenset({"bm25", "vector"})
_MAX_CATALOG_INT64 = 2**63 - 1


class IndexJobRequestError(ValueError):
    """A Web request asks for behavior the configured worker cannot provide."""


class IndexJobConflictError(RuntimeError):
    """Another active job or an incompatible idempotent request won the slot."""


class IndexJobWriteError(RuntimeError):
    """A configured durable job could not be planned, created, or attested."""


@dataclass(frozen=True, slots=True)
class IndexJobCreatePlan:
    """Trusted, pre-registered storage identities for one FULL job view."""

    source_revision_id: str
    profile_id: str
    expected_ref_generation: int
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if type(self) is not IndexJobCreatePlan:
            raise TypeError("index job creation plan must use the exact type")
        object.__setattr__(
            self,
            "source_revision_id",
            _canonical_text(
                self.source_revision_id,
                label="source revision ID",
                max_length=96,
            ),
        )
        object.__setattr__(
            self,
            "profile_id",
            _canonical_text(
                self.profile_id,
                label="profile ID",
                max_length=96,
            ),
        )
        if (
            type(self.expected_ref_generation) is not int
            or not 0 <= self.expected_ref_generation <= _MAX_CATALOG_INT64
        ):
            raise ValueError("index job expected ref generation is invalid")
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 1_000:
            raise ValueError("index job maximum attempts are invalid")


@runtime_checkable
class IndexJobCreatePlanner(Protocol):
    """Resolve trusted source/profile identities without granting them to Web.

    A planner must keep one idempotency key bound to the same plan so response
    loss cannot retarget a retry after repository state advances.
    """

    def plan(
        self,
        binding: IndexJobRepoBinding,
        index_type: str,
        *,
        idempotency_key: str,
    ) -> IndexJobCreatePlan: ...


@runtime_checkable
class IndexJobWriter(Protocol):
    """Small Web-facing write contract suitable for explicit injection."""

    def create(
        self,
        repo_id: str,
        *,
        indexes: tuple[str, ...],
        mode: str,
        force: bool,
        idempotency_key: str,
    ) -> IndexJobStatusResponse: ...


def _request_view(index_type: str, profile_id: str) -> dict[str, object]:
    return {
        "contract": INDEX_JOB_REQUEST_CONTRACT,
        "views": {
            index_type: {
                "profile_id": profile_id,
                "requested_mode": "full",
                "required": True,
            }
        },
    }


def _created_job_response(
    binding: IndexJobRepoBinding,
    expected: IndexJobRequest,
    value: object,
    *,
    index_type: str,
) -> IndexJobStatusResponse:
    if type(value) is not IndexJobRecord:
        raise IndexJobWriteError("catalog returned an invalid created index job")
    job = value
    try:
        observed = IndexJobRequest(
            repository_id=job.repository_id,
            source_revision_id=job.source_revision_id,
            ref_name=job.ref_name,
            idempotency_key=job.idempotency_key,
            expected_ref_generation=job.expected_ref_generation,
            max_attempts=job.max_attempts,
            request_json=job.request_json,
        )
        if (
            observed != expected
            or observed.job_id != job.job_id
            or observed.request_digest != job.request_digest
            or type(job.status) is not IndexJobStatus
        ):
            raise IndexJobWriteError("catalog returned a different created index job")
        views = observed.view_requests
        if (
            len(views) != 1
            or views[0].view_type != index_type
            or views[0].requested_mode.value != "full"
            or views[0].required is not True
        ):
            raise IndexJobWriteError("created index job request is inconsistent")
        return IndexJobStatusResponse(
            job_id=job.job_id,
            repo_id=binding.repo_id,
            status=job.status.value,
            cancel_requested=job.cancel_requested,
            attempt_count=job.attempt_count,
            max_attempts=job.max_attempts,
            indexes=[
                IndexJobSurface(
                    index_type=index_type,
                    requested_mode="full",
                    required=True,
                )
            ],
            result_snapshot_id=job.result_snapshot_id,
            error_code=_safe_error_code(job),
            error_message=_safe_error_message(job),
            created_at_ms=job.created_at_ms,
            updated_at_ms=job.updated_at_ms,
            started_at_ms=job.started_at_ms,
            finished_at_ms=job.finished_at_ms,
            events=[],
            next_event_sequence=0,
        )
    except IndexJobWriteError:
        raise
    except (StorageError, TypeError, ValueError) as exc:
        raise IndexJobWriteError(
            "catalog created index job could not be safely projected"
        ) from exc


class CatalogIndexJobWriter:
    """Create one honest FULL cache job through an atomic catalog slot.

    The current production worker can recapture exactly one already-current
    BM25 or vector compiler-cache view. This writer therefore rejects
    incremental, symbol-graph, multi-view, and force requests instead of
    claiming a capability the worker does not implement.
    """

    def __init__(
        self,
        catalog_factory: Callable[[], AbstractContextManager[JobCreationCatalog]],
        bindings: tuple[IndexJobRepoBinding, ...],
        planner: IndexJobCreatePlanner,
    ) -> None:
        if not callable(catalog_factory):
            raise TypeError("index job creation catalog factory must be callable")
        if type(bindings) is not tuple or not bindings:
            raise ValueError("index job writer requires repository bindings")
        if any(type(binding) is not IndexJobRepoBinding for binding in bindings):
            raise TypeError("index job writer bindings must use exact values")
        if not isinstance(planner, IndexJobCreatePlanner):
            raise TypeError("index job writer requires a creation planner")
        by_repo: dict[str, IndexJobRepoBinding] = {}
        by_storage: set[tuple[str, str]] = set()
        for binding in bindings:
            storage_key = (binding.repository_id, binding.ref_name)
            if binding.repo_id in by_repo or storage_key in by_storage:
                raise ValueError("index job writer bindings must be unique")
            by_repo[binding.repo_id] = binding
            by_storage.add(storage_key)
        self._catalog_factory = catalog_factory
        self._by_repo = by_repo
        self._planner = planner

    @staticmethod
    def _require_catalog(value: object) -> JobCreationCatalog:
        if not isinstance(value, JobCreationCatalog):
            raise IndexJobWriteError(
                "catalog does not implement atomic index job creation"
            )
        return value

    def create(
        self,
        repo_id: str,
        *,
        indexes: tuple[str, ...],
        mode: str,
        force: bool,
        idempotency_key: str,
    ) -> IndexJobStatusResponse:
        try:
            normalized_repo = _canonical_text(
                repo_id,
                label="Web repository ID",
                max_length=512,
            )
        except (TypeError, ValueError) as exc:
            raise IndexJobNotFoundError(
                "Web repository has no durable job binding"
            ) from exc
        binding = self._by_repo.get(normalized_repo)
        if binding is None:
            raise IndexJobNotFoundError("Web repository has no durable job binding")
        if type(indexes) is not tuple or any(type(item) is not str for item in indexes):
            raise IndexJobRequestError("index job surfaces are invalid")
        if len(indexes) != 1:
            raise IndexJobRequestError(
                "the configured worker requires exactly one index per job"
            )
        index_type = indexes[0]
        if index_type == "symbol_graph":
            raise IndexJobRequestError(
                "symbol graph updates are unavailable for the configured worker"
            )
        if index_type not in _SUPPORTED_WRITE_INDEXES:
            raise IndexJobRequestError("index job surfaces are invalid")
        if type(mode) is not str or mode not in {"full", "incremental"}:
            raise IndexJobRequestError("index job mode is invalid")
        if mode == "incremental":
            raise IndexJobRequestError(
                "incremental updates are unavailable for the configured worker"
            )
        if type(force) is not bool or force:
            raise IndexJobRequestError(
                "forced updates are unavailable for the configured worker"
            )
        try:
            normalized_key = _canonical_text(
                idempotency_key,
                label="idempotency key",
                max_length=256,
            )
        except (TypeError, ValueError) as exc:
            raise IndexJobRequestError("index job idempotency key is invalid") from exc

        try:
            plan = self._planner.plan(
                binding,
                index_type,
                idempotency_key=normalized_key,
            )
            if type(plan) is not IndexJobCreatePlan:
                raise IndexJobWriteError(
                    "index job planner returned an invalid creation plan"
                )
            expected = IndexJobRequest.create(
                binding.repository_id,
                plan.source_revision_id,
                normalized_key,
                _request_view(index_type, plan.profile_id),
                ref_name=binding.ref_name,
                expected_ref_generation=plan.expected_ref_generation,
                max_attempts=plan.max_attempts,
            )
            with self._catalog_factory() as value:
                catalog = self._require_catalog(value)
                job = catalog.create_job_if_idle(
                    expected.repository_id,
                    expected.source_revision_id,
                    expected.idempotency_key,
                    expected.request,
                    ref_name=expected.ref_name,
                    expected_ref_generation=expected.expected_ref_generation,
                    max_attempts=expected.max_attempts,
                )
            return _created_job_response(
                binding,
                expected,
                job,
                index_type=index_type,
            )
        except (IndexJobConflictError, IndexJobWriteError):
            raise
        except PublishConflict as exc:
            raise IndexJobConflictError(
                "index job conflicts with active or idempotent work"
            ) from exc
        except (OSError, sqlite3.Error, StorageError) as exc:
            raise IndexJobWriteError(
                "durable index job creation is unavailable"
            ) from exc
        except Exception as exc:
            raise IndexJobWriteError("index job planning failed") from exc


__all__ = [
    "CatalogIndexJobWriter",
    "IndexJobConflictError",
    "IndexJobCreatePlan",
    "IndexJobCreatePlanner",
    "IndexJobRequestError",
    "IndexJobWriteError",
    "IndexJobWriter",
]
