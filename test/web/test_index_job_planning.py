# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from codenib import LocalWorkspaceProvider
from codenib.compiler.index_builders import BM25IndexBuilder
from codenib.compiler.job_resources import LocalBM25SourceJobTarget
from codenib.repository_source_selection import RepositorySourceSelection
from codenib.source_fingerprint import RepositoryChangedError
from codenib.storage import SourceRevision, SQLiteCatalog, ViewProfile
from codenib.web.index_job_planning import LocalBM25SourceJobPlanner
from codenib.web.index_job_writes import (
    CatalogIndexJobWriter,
    IndexJobRequestError,
    IndexJobWriteError,
)
from codenib.web.index_jobs import IndexJobRepoBinding

_REPOSITORY_KEY = "owner/repo"
_DISPLAY_COMMIT = "a" * 40


def _target(
    tmp_path: Path,
    *,
    builder: BM25IndexBuilder | None = None,
) -> tuple[LocalBM25SourceJobTarget, Path]:
    repository = tmp_path / "repository"
    repository.mkdir()
    source = repository / "sample.py"
    source.write_text("def sample():\n    return 1\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    target = LocalBM25SourceJobTarget(
        repository_root=repository,
        workspace_provider=LocalWorkspaceProvider(workspace),
        repository_key=_REPOSITORY_KEY,
        display_commit=_DISPLAY_COMMIT,
        builder=builder or BM25IndexBuilder(languages=["python"]),
        environ={},
    )
    return target, source


def _catalog_factory(path: Path):
    @contextmanager
    def factory():
        with SQLiteCatalog(path, create=False) as catalog:
            yield catalog

    return factory


def test_local_bm25_source_planner_creates_replay_safe_web_job(tmp_path: Path) -> None:
    builder = BM25IndexBuilder(
        languages=["python"],
        max_k=17,
        source_selection=RepositorySourceSelection(("generated",)),
    )
    target, source = _target(tmp_path, builder=builder)
    catalog_path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(catalog_path) as catalog:
        repository_id = catalog.create_repository(_REPOSITORY_KEY)
    assert repository_id == target.repository_id

    factory = _catalog_factory(catalog_path)
    binding = IndexJobRepoBinding("demo", repository_id)
    planner = LocalBM25SourceJobPlanner(factory, (target,), max_attempts=5)
    writer = CatalogIndexJobWriter(factory, (binding,), planner)

    created = writer.create(
        "demo",
        indexes=("bm25",),
        mode="full",
        force=False,
        idempotency_key="browser-request",
    )
    source.write_text("def sample():\n    return 2\n", encoding="utf-8")
    replayed = writer.create(
        "demo",
        indexes=("bm25",),
        mode="full",
        force=False,
        idempotency_key="browser-request",
    )

    assert replayed == created
    assert created.status == "queued"
    assert created.max_attempts == 5
    with SQLiteCatalog(catalog_path, create=False) as catalog:
        job = catalog.get_job(created.job_id)
        views = catalog.get_job_views(created.job_id)
        assert catalog.read_ref_generation(repository_id) == 0
    assert job.source_revision_id.startswith("src_")
    assert len(views) == 1
    assert views[0].profile_id == target.profile_id
    assert views[0].view_type == "bm25"


def test_local_bm25_source_planner_freezes_builder_policy_and_rejects_vector(
    tmp_path: Path,
) -> None:
    builder = BM25IndexBuilder(
        languages=["python"],
        source_selection=RepositorySourceSelection(("generated",)),
    )
    target, _source = _target(tmp_path, builder=builder)
    original_profile = target.profile
    original_selection = target.source_selection
    builder.languages[:] = ["javascript"]
    builder.source_selection = RepositorySourceSelection(("sample.py",))

    @contextmanager
    def unused_factory():
        raise AssertionError(
            "unsupported views must not capture source or open storage"
        )
        yield

    planner = LocalBM25SourceJobPlanner(unused_factory, (target,))
    binding = IndexJobRepoBinding("demo", target.repository_id)

    with pytest.raises(IndexJobRequestError, match="only for BM25"):
        planner.plan(binding, "vector", idempotency_key="request")
    assert target.profile == original_profile
    assert target.source_selection == original_selection


def test_local_bm25_source_planner_revalidates_source_after_catalog_calls(
    tmp_path: Path,
) -> None:
    target, source_path = _target(tmp_path)
    binding = IndexJobRepoBinding("demo", target.repository_id)

    class MutatingPlanningCatalog:
        def create_source_revision(
            self,
            repository_id,
            *,
            commit_sha=None,
            tree_sha=None,
            dirty=False,
            source_fingerprint=None,
        ):
            revision = SourceRevision.dirty(
                repository_id,
                source_fingerprint=source_fingerprint,
                commit_sha=commit_sha,
            )
            source_path.write_text(
                "def sample():\n    return 22222\n",
                encoding="utf-8",
            )
            return revision.source_revision_id

        def create_view_profile(self, view_type, config=None, *, name="default"):
            return ViewProfile.create(view_type, config or {}, name=name).profile_id

        def read_ref_generation(self, repository_id, ref_name="main"):
            return 0

    @contextmanager
    def factory():
        yield MutatingPlanningCatalog()

    planner = LocalBM25SourceJobPlanner(factory, (target,))

    with pytest.raises(RepositoryChangedError):
        planner.plan(binding, "bm25", idempotency_key="request")


def test_local_bm25_source_planner_requires_least_authority_catalog(
    tmp_path: Path,
) -> None:
    target, _source = _target(tmp_path)

    @contextmanager
    def factory():
        yield object()

    planner = LocalBM25SourceJobPlanner(factory, (target,))

    with pytest.raises(IndexJobWriteError, match="least-authority"):
        planner.plan(
            IndexJobRepoBinding("demo", target.repository_id),
            "bm25",
            idempotency_key="request",
        )
