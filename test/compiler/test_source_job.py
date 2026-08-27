# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import builtins
import inspect
import os
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

import codenib.compiler as compiler_module
import codenib.compiler.source_job as source_job_module
from codenib import LocalWorkspaceProvider
from codenib._captured_directory import PublishedWorkspaceReceiptOwner
from codenib.code_chunker import CodeChunker
from codenib.code_chunking.base import BaseCodeChunker
from codenib.compiler.index_builders import BM25IndexBuilder
from codenib.compiler.manifest import RepoManifest
from codenib.compiler.manifest_storage import BM25_PROFILE_AXES
from codenib.compiler.source_job import BM25SourceJobExecutor, bm25_source_job_profile
from codenib.repository_source_selection import RepositorySourceSelection
from codenib.source_fingerprint import (
    RepositorySourceBinding,
    capture_repository_source,
)
from codenib.storage import (
    INDEX_JOB_REQUEST_CONTRACT,
    VIEW_BUNDLE_SCHEMA,
    IndexJobExecutionContext,
    IndexJobExecutionResult,
    IndexJobStatus,
    IndexJobStopReason,
    LocalCAS,
    SourceRevision,
    SQLiteCatalog,
    StorageValidationError,
    ViewProfile,
)

_COMMIT = "a" * 40
_REPOSITORY_KEY = "owner/repo"


class _StopToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def reason(self) -> IndexJobStopReason | None:
        if self._event.is_set():
            return IndexJobStopReason.CANCEL_REQUESTED
        return None

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


@dataclass
class _Control:
    token: _StopToken

    @property
    def stop_token(self) -> _StopToken:
        return self.token

    def append_progress(self, *_args, **_kwargs):
        raise AssertionError("source-job executor unexpectedly appended progress")


@dataclass
class _SourceFixture:
    repository: Path
    source: RepositorySourceBinding
    workspace: Path
    provider: LocalWorkspaceProvider
    attempt_provider: LocalWorkspaceProvider
    owners: list[PublishedWorkspaceReceiptOwner]

    def owner(self) -> PublishedWorkspaceReceiptOwner:
        owner = PublishedWorkspaceReceiptOwner()
        self.owners.append(owner)
        return owner

    def close(self) -> None:
        for owner in reversed(self.owners):
            owner.close()
        self.source.close()


def _source_fixture(
    tmp_path: Path,
    *,
    selection: RepositorySourceSelection | None = None,
) -> _SourceFixture:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    (repository / "sample.py").write_text(
        "def answer():\n    return 42\n",
        encoding="utf-8",
    )
    selected = selection or RepositorySourceSelection()
    source = capture_repository_source(repository, selection=selected)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    return _SourceFixture(
        repository=repository,
        source=source,
        workspace=workspace,
        provider=LocalWorkspaceProvider(workspace),
        attempt_provider=LocalWorkspaceProvider(tmp_path),
        owners=[],
    )


def _execution_context(
    catalog: SQLiteCatalog,
    fixture: _SourceFixture,
    profile: ViewProfile,
    *,
    source_fingerprint: str | None = None,
    requested_mode: str = "full",
    required: bool = True,
    extra_view: bool = False,
) -> IndexJobExecutionContext:
    repository_id = catalog.create_repository(_REPOSITORY_KEY)
    source_revision_id = catalog.create_source_revision(
        repository_id,
        commit_sha=None,
        dirty=True,
        source_fingerprint=source_fingerprint or fixture.source.fingerprint,
    )
    profile_id = catalog.create_view_profile(
        "bm25",
        profile.config,
        name=profile.name,
    )
    views: dict[str, dict[str, object]] = {
        "bm25": {
            "profile_id": profile_id,
            "requested_mode": requested_mode,
            "required": required,
        }
    }
    if extra_view:
        vector_profile = ViewProfile.create("vector", {"fixture": True})
        vector_profile_id = catalog.create_view_profile(
            "vector",
            vector_profile.config,
            name=vector_profile.name,
        )
        views["vector"] = {
            "profile_id": vector_profile_id,
            "requested_mode": "full",
            "required": True,
        }
    queued = catalog.create_job(
        repository_id,
        source_revision_id,
        "bm25-retained-source",
        {"contract": INDEX_JOB_REQUEST_CONTRACT, "views": views},
        expected_ref_generation=0,
    )
    lease = catalog.acquire_job_lease(
        queued.job_id,
        owner_id="source-worker",
        lease_duration_ms=60_000,
    )
    running = catalog.get_job(queued.job_id)
    attempt = catalog.get_job_attempt(queued.job_id, running.attempt_count)
    return IndexJobExecutionContext(
        job=running,
        views=catalog.get_job_views(queued.job_id),
        attempt=attempt,
        lease=lease,
        control=_Control(_StopToken()),
    )


def _executor(
    fixture: _SourceFixture,
    cas: LocalCAS,
    builder: BM25IndexBuilder,
    *,
    attempt_generation: Path,
    view_owner: PublishedWorkspaceReceiptOwner,
    context_owner: PublishedWorkspaceReceiptOwner,
    attempt_owner: PublishedWorkspaceReceiptOwner | None = None,
    attempt_provider=None,
    forbidden_paths=(),
    environ=None,
) -> BM25SourceJobExecutor:
    return BM25SourceJobExecutor(
        attempt_generation=attempt_generation,
        display_commit=_COMMIT,
        builder=builder,
        attempt_output_owner=attempt_owner or fixture.owner(),
        attempt_workspace_provider=attempt_provider or fixture.attempt_provider,
        repository_source=fixture.source,
        view_output_owner=view_owner,
        context_output_owner=context_owner,
        view_destination=fixture.workspace / "published-bm25",
        context_destination=fixture.workspace / "published-context",
        workspace_provider=fixture.provider,
        repository_key=_REPOSITORY_KEY,
        object_store=cas,
        forbidden_paths=forbidden_paths,
        environ=environ,
    )


def _repository_path(value: object, repository: Path) -> bool:
    if isinstance(value, int):
        return False
    try:
        raw = os.fspath(value)
    except TypeError:
        return False
    if not isinstance(raw, str):
        return False
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate == repository or repository in candidate.parents


def _guard_lexical_repository_reads(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
) -> None:
    real_open = builtins.open
    real_path_open = Path.open
    real_walk = os.walk

    def guarded_open(path, *args, **kwargs):
        if _repository_path(path, repository):
            raise AssertionError(f"lexical repository open: {path}")
        return real_open(path, *args, **kwargs)

    def guarded_path_open(path: Path, *args, **kwargs):
        if _repository_path(path, repository):
            raise AssertionError(f"lexical repository Path.open: {path}")
        return real_path_open(path, *args, **kwargs)

    def guarded_walk(path, *args, **kwargs):
        if _repository_path(path, repository):
            raise AssertionError(f"lexical repository walk: {path}")
        return real_walk(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(Path, "open", guarded_path_open)
    monkeypatch.setattr(os, "walk", guarded_walk)
    monkeypatch.setattr(
        CodeChunker,
        "chunk_repository",
        lambda *_args, **_kwargs: pytest.fail("lexical chunk_repository was called"),
    )
    monkeypatch.setattr(
        BaseCodeChunker,
        "_read_source",
        lambda *_args, **_kwargs: pytest.fail("lexical chunk source read was called"),
    )


def test_bm25_source_executor_prepares_exact_artifact_without_lexical_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = BM25IndexBuilder(
        languages=["python"],
        max_k=17,
        max_lines_per_chunk=41,
        additional_ignore_dirs=["vendor"],
    )
    profile = bm25_source_job_profile(builder)
    fixture = _source_fixture(tmp_path)
    view_owner = PublishedWorkspaceReceiptOwner()
    context_owner = PublishedWorkspaceReceiptOwner()
    attempt = tmp_path / "attempt-generation"
    forbidden = tmp_path / "forbidden"
    environment = {"CODENIB_SOURCE_JOB_TEST": "before"}
    delegated: list[Path] = []
    try:
        with (
            LocalCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            context = _execution_context(catalog, fixture, profile)
            executor = _executor(
                fixture,
                cas,
                builder,
                attempt_generation=attempt,
                view_owner=view_owner,
                context_owner=context_owner,
                forbidden_paths=(path for path in (forbidden,)),
                environ=environment,
            )
            assert executor.forbidden_paths == (forbidden,)
            assert executor.environ == environment

            # The executor owns a detached builder/environment snapshot.
            builder.languages[:] = ["javascript"]
            builder.max_k = 99
            builder.max_lines_per_chunk = 3
            builder.additional_ignore_dirs[:] = ["changed"]
            builder.source_selection = RepositorySourceSelection(("sample.py",))
            environment["CODENIB_SOURCE_JOB_TEST"] = "after"

            real_prepare = (
                source_job_module.prepare_compiler_cache_job_view_from_generation
            )

            def delegated_prepare(cache_generation, **kwargs):
                assert cache_generation.active
                cache_dir = cache_generation.receipt.path
                delegated.append(cache_dir)
                manifest = RepoManifest.load(cache_dir / "repo_manifest.json")
                assert manifest.commit == _COMMIT
                assert manifest.indexes["bm25"].config["max_k"] == 17
                assert manifest.indexes["bm25"].config["languages"] == ["python"]
                assert kwargs["expected_manifest"].to_dict() == manifest.to_dict()
                assert kwargs["forbidden_paths"] == (forbidden,)
                assert kwargs["environ"]["CODENIB_SOURCE_JOB_TEST"] == "before"
                return real_prepare(cache_generation, **kwargs)

            with monkeypatch.context() as guard:
                guard.setattr(
                    source_job_module,
                    "prepare_compiler_cache_job_view_from_generation",
                    delegated_prepare,
                )
                _guard_lexical_repository_reads(guard, fixture.repository)
                result = executor.execute(context)

            assert type(result) is IndexJobExecutionResult
            assert result.publishable
            assert result.retryable is False
            assert result.views[0].payload == {
                "adapter": "bm25_source",
                "prepared": True,
            }
            artifact = result.views[0].artifact
            assert artifact is not None
            assert artifact.profile_id == profile.profile_id
            assert artifact.schema_version == VIEW_BUNDLE_SCHEMA
            assert len(artifact.member_artifacts) == 2
            assert cas.verify(artifact.object_artifact.receipt.digest) == (
                artifact.object_artifact.receipt
            )
            for member in artifact.member_artifacts:
                assert cas.verify(member.receipt.digest) == member.receipt
            assert delegated == [attempt]
            assert executor.attempt_output_owner.active
            assert attempt.is_dir()
            manifest = RepoManifest.load(attempt / "repo_manifest.json")
            assert manifest.repo_path == str(fixture.repository)
            assert manifest.source_fingerprint == fixture.source.fingerprint
            assert manifest.source_selection == RepositorySourceSelection()
            assert (
                context.job.source_revision_id
                == SourceRevision.dirty(
                    context.job.repository_id,
                    source_fingerprint=fixture.source.fingerprint,
                    commit_sha=None,
                ).source_revision_id
            )
            assert (
                context.job.source_revision_id
                != SourceRevision.dirty(
                    context.job.repository_id,
                    source_fingerprint=fixture.source.fingerprint,
                    commit_sha=_COMMIT,
                ).source_revision_id
            )
            assert catalog.get_job(context.job.job_id).status is IndexJobStatus.RUNNING
            assert catalog.get_job(context.job.job_id) == context.job
    finally:
        context_owner.close()
        view_owner.close()
        fixture.close()


def test_bm25_source_executor_refuses_replaced_generation_before_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = BM25IndexBuilder(languages=["python"])
    fixture = _source_fixture(tmp_path)
    view_owner = PublishedWorkspaceReceiptOwner()
    context_owner = PublishedWorkspaceReceiptOwner()
    attempt = tmp_path / "attempt-generation"
    retained_attempt = tmp_path / "retained-attempt-generation"
    real_prepare = source_job_module.prepare_compiler_cache_job_view_from_generation
    attacked = False

    def replace_generation(cache_generation, **kwargs):
        nonlocal attacked
        assert cache_generation.active
        assert cache_generation.receipt.path == attempt
        attacked = True
        attempt.rename(retained_attempt)
        attempt.mkdir(mode=0o700)
        (attempt / "decoy-marker").write_text("untrusted", encoding="utf-8")
        return real_prepare(cache_generation, **kwargs)

    try:
        with (
            LocalCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            context = _execution_context(
                catalog,
                fixture,
                bm25_source_job_profile(builder),
            )
            executor = _executor(
                fixture,
                cas,
                builder,
                attempt_generation=attempt,
                view_owner=view_owner,
                context_owner=context_owner,
            )
            monkeypatch.setattr(
                source_job_module,
                "prepare_compiler_cache_job_view_from_generation",
                replace_generation,
            )

            with pytest.raises(
                RuntimeError,
                match="published workspace root handle changed",
            ):
                executor.execute(context)

            assert attacked
            assert executor.attempt_output_owner.active
            assert retained_attempt.joinpath("repo_manifest.json").is_file()
            assert attempt.joinpath("decoy-marker").read_text(encoding="utf-8") == (
                "untrusted"
            )
            assert view_owner.state == "empty"
            assert context_owner.state == "empty"
            assert not (fixture.workspace / "published-bm25").exists()
            assert not (fixture.workspace / "published-context").exists()
            assert not any(path.is_file() for path in (tmp_path / "cas").rglob("*"))
    finally:
        context_owner.close()
        view_owner.close()
        fixture.close()


def test_bm25_source_executor_rejects_retargeted_local_attempt_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = BM25IndexBuilder(languages=["python"])
    fixture = _source_fixture(tmp_path)
    view_owner = PublishedWorkspaceReceiptOwner()
    context_owner = PublishedWorkspaceReceiptOwner()
    scratch = tmp_path / "attempt-workspace"
    scratch.mkdir(mode=0o700)
    retained_scratch = tmp_path / "retained-attempt-workspace"
    attempt = scratch / "attempt-generation"
    attempt_provider = LocalWorkspaceProvider(scratch)
    real_run = LocalWorkspaceProvider.run_workspace
    attacked = False

    def retarget_before_native_provision(
        self,
        request,
        *,
        receipt_owner,
        operation,
        check_cancelled=None,
        _expected_parent_identity=None,
        _replacement_source=None,
    ):
        nonlocal attacked
        if self is attempt_provider and not attacked:
            assert request.destination == attempt
            assert _expected_parent_identity is not None
            assert _replacement_source is None
            attacked = True
            scratch.rename(retained_scratch)
            scratch.mkdir(mode=0o700)
        return real_run(
            self,
            request,
            receipt_owner=receipt_owner,
            operation=operation,
            check_cancelled=check_cancelled,
            _expected_parent_identity=_expected_parent_identity,
            _replacement_source=_replacement_source,
        )

    try:
        with (
            LocalCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            context = _execution_context(
                catalog,
                fixture,
                bm25_source_job_profile(builder),
            )
            executor = _executor(
                fixture,
                cas,
                builder,
                attempt_generation=attempt,
                view_owner=view_owner,
                context_owner=context_owner,
                attempt_provider=attempt_provider,
            )
            monkeypatch.setattr(
                LocalWorkspaceProvider,
                "run_workspace",
                retarget_before_native_provision,
            )

            with pytest.raises(
                RuntimeError,
                match="native workspace parent differs from retained authority",
            ):
                executor.execute(context)

            assert attacked
            assert executor.attempt_output_owner.state == "empty"
            assert not attempt.exists()
            quarantined = tuple(scratch.iterdir())
            assert quarantined
            assert all(
                entry.name.startswith(".codenib-workspace-orphan-") and entry.is_dir()
                for entry in quarantined
            )
            assert not any(
                descendant.is_file()
                for entry in quarantined
                for descendant in entry.rglob("*")
            )
            assert not tuple(retained_scratch.iterdir())
            assert view_owner.state == "empty"
            assert context_owner.state == "empty"
            assert not any(path.is_file() for path in (tmp_path / "cas").rglob("*"))
    finally:
        context_owner.close()
        view_owner.close()
        fixture.close()


def test_bm25_source_executor_threads_stop_check_into_attempt_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = BM25IndexBuilder(languages=["python"])
    fixture = _source_fixture(tmp_path)
    view_owner = PublishedWorkspaceReceiptOwner()
    context_owner = PublishedWorkspaceReceiptOwner()
    observed: list[tuple[str, object, tuple[int, ...] | None]] = []

    def expected_check() -> None:
        return None

    class TrackingAttemptProvider:
        def require_support(self) -> None:
            fixture.attempt_provider.require_support()

        def run_workspace(
            self,
            request,
            *,
            receipt_owner,
            operation,
            check_cancelled=None,
            _expected_parent_identity=None,
        ):
            observed.append(
                (
                    request.purpose,
                    check_cancelled,
                    _expected_parent_identity,
                )
            )
            return fixture.attempt_provider.run_workspace(
                request,
                receipt_owner=receipt_owner,
                operation=operation,
                check_cancelled=check_cancelled,
                _expected_parent_identity=_expected_parent_identity,
            )

    attempt_provider = TrackingAttemptProvider()

    try:
        with (
            LocalCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            context = _execution_context(
                catalog,
                fixture,
                bm25_source_job_profile(builder),
            )
            executor = _executor(
                fixture,
                cas,
                builder,
                attempt_generation=tmp_path / "attempt-generation",
                view_owner=view_owner,
                context_owner=context_owner,
                attempt_provider=attempt_provider,
            )
            monkeypatch.setattr(
                source_job_module,
                "_compiler_cache_job_stop_check",
                lambda _token: expected_check,
            )
            result = executor.execute(context)

            assert result.publishable
            assert len(observed) == 1
            assert observed[0][0] == "private-bm25-compiler-cache"
            assert observed[0][1] is expected_check
            assert observed[0][2] is not None
    finally:
        context_owner.close()
        view_owner.close()
        fixture.close()


@pytest.mark.parametrize("mismatch", ["profile", "source", "mode", "required", "views"])
def test_bm25_source_executor_rejects_job_mismatch_before_output_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    builder = BM25IndexBuilder(languages=["python"], max_k=17)
    expected_profile = bm25_source_job_profile(builder)
    job_profile = (
        bm25_source_job_profile(BM25IndexBuilder(languages=["python"], max_k=18))
        if mismatch == "profile"
        else expected_profile
    )
    fixture = _source_fixture(tmp_path)
    view_owner = PublishedWorkspaceReceiptOwner()
    context_owner = PublishedWorkspaceReceiptOwner()
    attempt = tmp_path / "attempt-generation"
    try:
        with (
            LocalCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            changed_fingerprint = fixture.source.fingerprint[:-1] + (
                "0" if fixture.source.fingerprint[-1] != "0" else "1"
            )
            context = _execution_context(
                catalog,
                fixture,
                job_profile,
                source_fingerprint=(
                    changed_fingerprint if mismatch == "source" else None
                ),
                requested_mode="incremental" if mismatch == "mode" else "full",
                required=mismatch != "required",
                extra_view=mismatch == "views",
            )
            executor = _executor(
                fixture,
                cas,
                builder,
                attempt_generation=attempt,
                view_owner=view_owner,
                context_owner=context_owner,
            )
            monkeypatch.setattr(
                BM25IndexBuilder,
                "prepare_from_repository_source",
                lambda *_args, **_kwargs: pytest.fail(
                    "builder ran before source-job preflight completed"
                ),
            )

            with pytest.raises(StorageValidationError):
                executor.execute(context)

            assert not attempt.exists()
            assert view_owner.state == "empty"
            assert context_owner.state == "empty"
            assert not (fixture.workspace / "published-bm25").exists()
            assert not (fixture.workspace / "published-context").exists()
            assert not any(path.is_file() for path in (tmp_path / "cas").rglob("*"))
    finally:
        context_owner.close()
        view_owner.close()
        fixture.close()


def test_bm25_source_executor_rejects_symlinked_attempt_inside_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = BM25IndexBuilder()
    fixture = _source_fixture(tmp_path)
    view_owner = PublishedWorkspaceReceiptOwner()
    context_owner = PublishedWorkspaceReceiptOwner()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(fixture.repository, target_is_directory=True)
    attempt = linked_parent / "attempt-generation"
    try:
        with (
            LocalCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            context = _execution_context(
                catalog,
                fixture,
                bm25_source_job_profile(builder),
            )
            executor = _executor(
                fixture,
                cas,
                builder,
                attempt_generation=attempt,
                view_owner=view_owner,
                context_owner=context_owner,
            )
            monkeypatch.setattr(
                BM25IndexBuilder,
                "prepare_from_repository_source",
                lambda *_args, **_kwargs: pytest.fail(
                    "builder ran for a repository-overlapping attempt"
                ),
            )

            with pytest.raises(ValueError, match="overlaps repository"):
                executor.execute(context)

            assert not (fixture.repository / "attempt-generation").exists()
            assert view_owner.state == "empty"
            assert context_owner.state == "empty"
    finally:
        context_owner.close()
        view_owner.close()
        fixture.close()


@pytest.mark.parametrize("boundary", ["view", "context", "forbidden"])
def test_bm25_source_executor_rejects_attempt_output_and_policy_overlap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    builder = BM25IndexBuilder()
    fixture = _source_fixture(tmp_path)
    view_owner = PublishedWorkspaceReceiptOwner()
    context_owner = PublishedWorkspaceReceiptOwner()
    forbidden = tmp_path / "forbidden"
    forbidden.mkdir(mode=0o700)
    attempt_parents = {
        "view": fixture.workspace / "published-bm25",
        "context": fixture.workspace / "published-context",
        "forbidden": forbidden,
    }
    attempt = attempt_parents[boundary] / "attempt-generation"
    try:
        with (
            LocalCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            context = _execution_context(
                catalog,
                fixture,
                bm25_source_job_profile(builder),
            )
            executor = _executor(
                fixture,
                cas,
                builder,
                attempt_generation=attempt,
                view_owner=view_owner,
                context_owner=context_owner,
                forbidden_paths=(forbidden,),
            )
            monkeypatch.setattr(
                BM25IndexBuilder,
                "prepare_from_repository_source",
                lambda *_args, **_kwargs: pytest.fail(
                    "builder ran for an overlapping attempt"
                ),
            )

            with pytest.raises(ValueError, match="output or forbidden boundary"):
                executor.execute(context)

            assert not attempt.exists()
            assert view_owner.state == "empty"
            assert context_owner.state == "empty"
    finally:
        context_owner.close()
        view_owner.close()
        fixture.close()


def test_bm25_source_executor_preserves_stop_and_caller_retry_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = BM25IndexBuilder()
    fixture = _source_fixture(tmp_path)
    view_owner = PublishedWorkspaceReceiptOwner()
    context_owner = PublishedWorkspaceReceiptOwner()
    attempt = tmp_path / "attempt-generation"
    stopped = KeyboardInterrupt("injected source-job stop")
    armed = False

    def check_cancelled() -> None:
        if armed:
            raise stopped

    real_prepare = source_job_module.prepare_compiler_cache_job_view_from_generation

    def stop_during_prepare(cache_generation, **kwargs):
        nonlocal armed
        armed = True
        return real_prepare(cache_generation, **kwargs)

    try:
        with (
            LocalCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            context = _execution_context(
                catalog,
                fixture,
                bm25_source_job_profile(builder),
            )
            executor = _executor(
                fixture,
                cas,
                builder,
                attempt_generation=attempt,
                view_owner=view_owner,
                context_owner=context_owner,
            )
            monkeypatch.setattr(
                source_job_module,
                "_compiler_cache_job_stop_check",
                lambda _token: check_cancelled,
            )
            monkeypatch.setattr(
                source_job_module,
                "prepare_compiler_cache_job_view_from_generation",
                stop_during_prepare,
            )

            with pytest.raises(BaseException) as raised:
                executor.execute(context)
            assert raised.value is stopped
            assert attempt.is_dir()
            assert (attempt / "bm25").is_dir()
            assert (attempt / "repo_manifest.json").is_file()
            assert executor.attempt_output_owner.active
            assert view_owner.state == "empty"
            assert context_owner.state == "empty"

            armed = False
            with pytest.raises(RuntimeError, match="attempt owner must be empty"):
                executor.execute(context)
            assert attempt.is_dir()
    finally:
        context_owner.close()
        view_owner.close()
        fixture.close()


def test_bm25_source_executor_preserves_stop_during_source_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = BM25IndexBuilder()
    fixture = _source_fixture(tmp_path)
    view_owner = PublishedWorkspaceReceiptOwner()
    context_owner = PublishedWorkspaceReceiptOwner()
    attempt = tmp_path / "attempt-generation"
    stopped = KeyboardInterrupt("injected source-build stop")
    armed = False
    chunk_calls = 0
    real_json_chunks = source_job_module._json_byte_chunks

    def check_cancelled() -> None:
        if armed:
            raise stopped

    def stop_during_json(value, checker):
        nonlocal armed, chunk_calls
        for chunk in real_json_chunks(value, checker):
            chunk_calls += 1
            if chunk_calls == 3:
                armed = True
            yield chunk

    try:
        with (
            LocalCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            context = _execution_context(
                catalog,
                fixture,
                bm25_source_job_profile(builder),
            )
            executor = _executor(
                fixture,
                cas,
                builder,
                attempt_generation=attempt,
                view_owner=view_owner,
                context_owner=context_owner,
            )
            monkeypatch.setattr(
                source_job_module,
                "_compiler_cache_job_stop_check",
                lambda _token: check_cancelled,
            )
            monkeypatch.setattr(
                source_job_module,
                "_json_byte_chunks",
                stop_during_json,
            )
            monkeypatch.setattr(
                source_job_module,
                "prepare_compiler_cache_job_view_from_generation",
                lambda *_args, **_kwargs: pytest.fail(
                    "cache preparation ran after source-build cancellation"
                ),
            )

            with pytest.raises(BaseException) as raised:
                executor.execute(context)

            assert raised.value is stopped
            assert chunk_calls == 3
            assert fixture.source.usable
            assert not attempt.exists()
            assert executor.attempt_output_owner.state == "empty"
            assert view_owner.state == "empty"
            assert context_owner.state == "empty"
            assert not (fixture.workspace / "published-bm25").exists()
            assert not (fixture.workspace / "published-context").exists()
            assert not any(path.is_file() for path in (tmp_path / "cas").rglob("*"))
            assert catalog.get_job(context.job.job_id).status is IndexJobStatus.RUNNING
    finally:
        context_owner.close()
        view_owner.close()
        fixture.close()


def test_bm25_source_executor_api_has_no_publication_authority() -> None:
    assert compiler_module.BM25SourceJobExecutor is BM25SourceJobExecutor
    assert compiler_module.bm25_source_job_profile is bm25_source_job_profile
    assert (
        compiler_module.prepare_compiler_cache_job_view_from_generation
        is source_job_module.prepare_compiler_cache_job_view_from_generation
    )
    parameters = inspect.signature(BM25SourceJobExecutor).parameters
    assert list(parameters)[:13] == [
        "attempt_generation",
        "display_commit",
        "builder",
        "attempt_output_owner",
        "attempt_workspace_provider",
        "repository_source",
        "view_output_owner",
        "context_output_owner",
        "view_destination",
        "context_destination",
        "workspace_provider",
        "repository_key",
        "object_store",
    ]
    assert not {
        "catalog",
        "owner_id",
        "lease_owner",
        "fencing_token",
        "ref_name",
        "expected_generation",
        "generation_id",
    } & set(parameters)


def test_bm25_source_executor_repr_does_not_expose_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    variable = "CODENIB_SOURCE_JOB_SECRET_SENTINEL"
    secret = "source-job-secret-marker"
    monkeypatch.setenv(variable, secret)

    class UnusedProvider:
        def require_support(self) -> None:
            return None

        def run_workspace(self, *_args, **_kwargs):
            raise AssertionError("repr test unexpectedly used a workspace")

    attempt_owner = PublishedWorkspaceReceiptOwner()
    view_owner = PublishedWorkspaceReceiptOwner()
    context_owner = PublishedWorkspaceReceiptOwner()
    try:
        executor = BM25SourceJobExecutor(
            attempt_generation=tmp_path / "attempt",
            display_commit=_COMMIT,
            builder=BM25IndexBuilder(),
            attempt_output_owner=attempt_owner,
            attempt_workspace_provider=UnusedProvider(),
            repository_source=object(),  # type: ignore[arg-type]
            view_output_owner=view_owner,
            context_output_owner=context_owner,
            view_destination=tmp_path / "view",
            context_destination=tmp_path / "context",
            workspace_provider=object(),  # type: ignore[arg-type]
            repository_key=_REPOSITORY_KEY,
            object_store=object(),  # type: ignore[arg-type]
        )

        assert executor.environ[variable] == secret
        assert secret not in repr(executor)
    finally:
        context_owner.close()
        view_owner.close()
        attempt_owner.close()


def test_bm25_source_job_profile_covers_builder_schema_axes() -> None:
    baseline = bm25_source_job_profile(BM25IndexBuilder())
    config = baseline.config
    assert config["builder_schema"] == 8
    assert set(config["compatibility"]) == set(BM25_PROFILE_AXES) - {"builder_schema"}
    variants = (
        BM25IndexBuilder(languages=["python", "go"]),
        BM25IndexBuilder(max_k=17),
        BM25IndexBuilder(max_lines_per_chunk=41),
        BM25IndexBuilder(additional_ignore_dirs=["vendor"]),
        BM25IndexBuilder(source_selection=RepositorySourceSelection(("generated",))),
    )
    assert all(
        bm25_source_job_profile(builder).profile_id != baseline.profile_id
        for builder in variants
    )


def test_bm25_source_executor_rejects_noncanonical_display_commit(
    tmp_path: Path,
) -> None:
    attempt = tmp_path / "attempt"
    parameters = {
        "attempt_generation": attempt,
        "display_commit": "A" * 40,
        "builder": BM25IndexBuilder(),
        "attempt_output_owner": object(),
        "attempt_workspace_provider": object(),
        "repository_source": object(),
        "view_output_owner": object(),
        "context_output_owner": object(),
        "view_destination": Path("view"),
        "context_destination": Path("context"),
        "workspace_provider": object(),
        "repository_key": _REPOSITORY_KEY,
        "object_store": object(),
    }
    with pytest.raises(StorageValidationError, match="full lowercase Git SHA"):
        BM25SourceJobExecutor(**parameters)  # type: ignore[arg-type]
    assert not attempt.exists()
