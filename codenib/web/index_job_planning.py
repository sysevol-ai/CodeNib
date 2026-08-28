# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Trusted local source planning for explicit Web index-job writers."""

from __future__ import annotations

from contextlib import AbstractContextManager
from types import MappingProxyType
from typing import Callable, Mapping

from ..artifacts.runtime import SourceBindingCleanupOwner, _attach_source_cleanup_owner
from ..compiler.job_resources import LocalBM25SourceJobTarget
from ..compiler.retained_manifest_contract import repo_manifest_projection_profile
from ..storage import IndexJobPlanningCatalog, SourceRevision
from .index_job_writes import (
    IndexJobCreatePlan,
    IndexJobRequestError,
    IndexJobWriteError,
)
from .index_jobs import IndexJobRepoBinding, _canonical_text

_MAX_LOCAL_TARGETS = 4_096


class LocalBM25SourceJobPlanner:
    """Register one exact retained-source BM25 plan for a trusted target.

    Targets are explicit configuration shared with the production source-job
    worker. Planning captures and finally revalidates the target's frozen
    source-selection policy, registers only content-addressed identities, and
    reads only the ref generation needed by fenced publication.
    """

    def __init__(
        self,
        catalog_factory: Callable[[], AbstractContextManager[IndexJobPlanningCatalog]],
        targets: tuple[LocalBM25SourceJobTarget, ...],
        *,
        max_attempts: int = 3,
    ) -> None:
        if not callable(catalog_factory):
            raise TypeError("index job planning catalog factory must be callable")
        if type(targets) is not tuple or not (1 <= len(targets) <= _MAX_LOCAL_TARGETS):
            raise ValueError("BM25 source planner requires bounded local targets")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 1_000:
            raise ValueError("BM25 source planner maximum attempts are invalid")
        targets_by_repository_id: dict[str, LocalBM25SourceJobTarget] = {}
        for target in targets:
            if type(target) is not LocalBM25SourceJobTarget:
                raise TypeError("BM25 source planner target is invalid")
            if target.repository_id in targets_by_repository_id:
                raise ValueError("BM25 source planner has duplicate repository targets")
            targets_by_repository_id[target.repository_id] = target
        self._catalog_factory = catalog_factory
        self._targets_by_repository_id: Mapping[str, LocalBM25SourceJobTarget] = (
            MappingProxyType(targets_by_repository_id)
        )
        self._max_attempts = max_attempts

    @staticmethod
    def _require_catalog(value: object) -> IndexJobPlanningCatalog:
        if not isinstance(value, IndexJobPlanningCatalog):
            raise IndexJobWriteError(
                "catalog does not implement least-authority index job planning"
            )
        return value

    def plan(
        self,
        binding: IndexJobRepoBinding,
        index_type: str,
        *,
        idempotency_key: str,
    ) -> IndexJobCreatePlan:
        if type(binding) is not IndexJobRepoBinding:
            raise TypeError("BM25 source planner binding must use the exact type")
        _canonical_text(
            idempotency_key,
            label="idempotency key",
            max_length=256,
        )
        if type(index_type) is not str or index_type != "bm25":
            raise IndexJobRequestError(
                "retained-source updates are currently available only for BM25"
            )
        target = self._targets_by_repository_id.get(binding.repository_id)
        if target is None:
            raise IndexJobRequestError(
                "repository has no retained-source BM25 worker target"
            )

        cleanup_owner = SourceBindingCleanupOwner()
        result: IndexJobCreatePlan | None = None
        try:
            source = target.capture_source(source_owner=cleanup_owner)
            cleanup_owner.retain(source)
            identity = source.authenticated_identity_snapshot()
            if (
                identity.root != target.repository_root
                or identity.source_selection != target.source_selection
            ):
                raise IndexJobWriteError(
                    "captured BM25 source differs from its trusted target"
                )
            revision = SourceRevision.dirty(
                target.repository_id,
                source_fingerprint=identity.fingerprint,
                commit_sha=None,
            )
            profile = target.profile
            supporting_profile = repo_manifest_projection_profile()
            with self._catalog_factory() as value:
                catalog = self._require_catalog(value)
                source_revision_id = catalog.create_source_revision(
                    target.repository_id,
                    commit_sha=None,
                    dirty=True,
                    source_fingerprint=identity.fingerprint,
                )
                profile_id = catalog.create_view_profile(
                    profile.view_type,
                    profile.config,
                    name=profile.name,
                )
                supporting_profile_id = catalog.create_view_profile(
                    supporting_profile.view_type,
                    supporting_profile.config,
                    name=supporting_profile.name,
                )
                expected_ref_generation = catalog.read_ref_generation(
                    target.repository_id,
                    binding.ref_name,
                )
            if source_revision_id != revision.source_revision_id:
                raise IndexJobWriteError(
                    "catalog registered a different BM25 source revision"
                )
            if profile_id != profile.profile_id:
                raise IndexJobWriteError(
                    "catalog registered a different BM25 view profile"
                )
            if supporting_profile_id != supporting_profile.profile_id:
                raise IndexJobWriteError(
                    "catalog registered a different snapshot-support profile"
                )
            result = IndexJobCreatePlan(
                source_revision_id=source_revision_id,
                profile_id=profile_id,
                expected_ref_generation=expected_ref_generation,
                max_attempts=self._max_attempts,
            )
            source.verify_snapshot()
        except BaseException as primary:  # noqa: B036 - preserve planning fault
            try:
                cleanup_owner.close()
            except BaseException:  # noqa: B036 - expose retryable cleanup owner
                _attach_source_cleanup_owner(primary, cleanup_owner)
            raise
        try:
            cleanup_owner.close()
        except BaseException as cleanup_failure:  # noqa: B036 - retryable owner
            _attach_source_cleanup_owner(cleanup_failure, cleanup_owner)
            raise
        if result is None:  # pragma: no cover - successful path sets a result
            raise AssertionError("BM25 source planner produced no creation plan")
        return result


__all__ = ["LocalBM25SourceJobPlanner"]
