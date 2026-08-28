# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import builtins
import dis
import inspect
import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

import codenib.cli as cli_module
import codenib.compiler as compiler_module
import codenib.compiler.bm25_attempt_pool as bm25_attempt_pool_module
import codenib.compiler.cache_import as cache_import_module
import codenib.compiler.job_resolver as job_resolver_module
import codenib.compiler.job_resources as job_resources_module
import codenib.compiler.source_job as source_job_module
from codenib import LocalWorkspaceProvider
from codenib._atomic_directory import DirectoryOrphan, publication_parent_identity
from codenib._captured_directory import (
    PublishedWorkspaceReceiptOwner,
    UnsupportedWorkspaceCreation,
)
from codenib.code_chunker import CodeChunker
from codenib.code_chunking.base import BaseCodeChunker
from codenib.compiler._directory_lease import (
    DirectoryLeaseMode,
    PrivateDirectoryLeaseOwner,
    PrivateDirectoryLeaseRoute,
    acquire_private_directory_lease,
)
from codenib.compiler.bm25_attempt_pool import bootstrap_local_bm25_attempt_pool
from codenib.compiler.index_builders import BM25IndexBuilder
from codenib.compiler.job_resolver import (
    BM25SourceJobResolver,
    BM25SourceJobResourceFactory,
    BM25SourceJobResourceScope,
    CompilerCacheJobResourceScope,
)
from codenib.compiler.job_resources import (
    LocalBM25SourceJobResourceFactory,
    LocalBM25SourceJobTarget,
)
from codenib.compiler.manifest import RepoManifest
from codenib.compiler.manifest_storage import BM25_PROFILE_AXES
from codenib.compiler.retained_manifest_contract import repo_manifest_projection_profile
from codenib.compiler.source_job import BM25SourceJobExecutor, bm25_source_job_profile
from codenib.repository_source_selection import RepositorySourceSelection
from codenib.source_fingerprint import (
    RepositorySourceBinding,
    capture_repository_source,
    pin_repository_source_root,
)
from codenib.storage import (
    INDEX_JOB_REQUEST_CONTRACT,
    VIEW_BUNDLE_SCHEMA,
    IndexJobExecutionContext,
    IndexJobExecutionResult,
    IndexJobStatus,
    IndexJobStopReason,
    IndexJobWorker,
    IndexJobWorkerDisposition,
    LocalCAS,
    SourceRevision,
    SQLiteCatalog,
    StorageIntegrityError,
    StorageValidationError,
    ViewProfile,
)
from codenib.storage.models import NamespaceIdentity, RepositoryIdentity

_COMMIT = "a" * 40
_REPOSITORY_KEY = "owner/repo"


def _register_projection_profile(catalog: SQLiteCatalog) -> None:
    profile = repo_manifest_projection_profile()
    assert (
        catalog.create_view_profile(
            profile.view_type,
            profile.config,
            name=profile.name,
        )
        == profile.profile_id
    )


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
    git_checkout: bool = False,
) -> _SourceFixture:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    (repository / "sample.py").write_text(
        "def answer():\n    return 42\n",
        encoding="utf-8",
    )
    if git_checkout:
        for command in (
            ("init",),
            ("config", "user.email", "source-job@example.invalid"),
            ("config", "user.name", "Source Job Test"),
            ("add", "sample.py"),
            ("commit", "-m", "fixture"),
        ):
            subprocess.run(
                ("git", "-C", os.fspath(repository), *command),
                check=True,
                capture_output=True,
                text=True,
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


def _directory_identity(path: Path) -> tuple[int, ...]:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return publication_parent_identity(descriptor)
    finally:
        os.close(descriptor)


def _source_repository_id() -> str:
    namespace = NamespaceIdentity("default")
    return RepositoryIdentity(
        namespace_id=namespace.namespace_id,
        repository_key=_REPOSITORY_KEY,
    ).repository_id


def _probe_nonblocking_reaper(reaper_route) -> BaseException | None:
    results: list[BaseException | None] = []

    def acquire() -> None:
        installed: list[object] = []
        try:
            owner = reaper_route._acquire(
                blocking=False,
                check_cancelled=None,
                construction_owner=installed.append,
            )
        except BaseException as error:  # noqa: B036 - exact contention is asserted
            results.append(error)
            return
        try:
            results.append(None)
        finally:
            owner.close()

    thread = threading.Thread(target=acquire)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert len(results) == 1
    return results[0]


def _probe_nonblocking_directory_lease(
    route: PrivateDirectoryLeaseRoute,
    mode: DirectoryLeaseMode,
) -> BaseException | None:
    results: list[BaseException | None] = []

    def acquire() -> None:
        installed: list[PrivateDirectoryLeaseOwner] = []
        try:
            owner = acquire_private_directory_lease(
                route,
                mode=mode,
                blocking=False,
                _construction_owner=installed.append,
            )
        except BaseException as error:  # noqa: B036 - contention is asserted
            results.append(error)
            return
        try:
            results.append(None)
        finally:
            owner.close()

    thread = threading.Thread(target=acquire)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert len(results) == 1
    return results[0]


def _git_head(repository: Path) -> str:
    return subprocess.run(
        ("git", "-C", os.fspath(repository), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit_source(repository: Path, content: str, message: str) -> str:
    (repository / "sample.py").write_text(content, encoding="utf-8")
    for command in (("add", "sample.py"), ("commit", "-m", message)):
        subprocess.run(
            ("git", "-C", os.fspath(repository), *command),
            check=True,
            capture_output=True,
            text=True,
        )
    return _git_head(repository)


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
    _register_projection_profile(catalog)
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
    attempt_provider: LocalWorkspaceProvider | None = None,
    view_destination: Path | None = None,
    context_destination: Path | None = None,
    forbidden_paths=(),
    environ=None,
) -> BM25SourceJobExecutor:
    return BM25SourceJobExecutor(
        attempt_generation=attempt_generation,
        display_commit=_COMMIT,
        builder=builder,
        attempt_output_owner=(
            fixture.owner() if attempt_owner is None else attempt_owner
        ),
        attempt_workspace_provider=(
            fixture.attempt_provider if attempt_provider is None else attempt_provider
        ),
        repository_source=fixture.source,
        view_output_owner=view_owner,
        context_output_owner=context_owner,
        view_destination=(
            fixture.workspace / "published-bm25"
            if view_destination is None
            else view_destination
        ),
        context_destination=(
            fixture.workspace / "published-context"
            if context_destination is None
            else context_destination
        ),
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

    real_run_workspace = LocalWorkspaceProvider.run_workspace

    def tracking_run_workspace(
        self,
        request,
        *,
        receipt_owner,
        operation,
        check_cancelled=None,
        _expected_parent_identity=None,
        _replacement_source=None,
    ):
        if self is fixture.attempt_provider:
            observed.append(
                (
                    request.purpose,
                    check_cancelled,
                    _expected_parent_identity,
                )
            )
        return real_run_workspace(
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
                attempt_generation=tmp_path / "attempt-generation",
                view_owner=view_owner,
                context_owner=context_owner,
            )
            monkeypatch.setattr(
                LocalWorkspaceProvider,
                "run_workspace",
                tracking_run_workspace,
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


@pytest.mark.parametrize(
    ("case", "error", "message"),
    [
        ("same", ValueError, "output destinations overlap"),
        ("nested", ValueError, "output destinations overlap"),
        ("physical-alias", ValueError, "output destinations overlap"),
        ("existing-view", FileExistsError, "destination must be missing"),
        ("existing-context", FileExistsError, "destination must be missing"),
        ("repository", ValueError, "destination overlaps an input authority"),
        ("forbidden", ValueError, "destination overlaps an input authority"),
    ],
)
def test_bm25_source_executor_rejects_invalid_output_topology_before_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    error: type[Exception],
    message: str,
) -> None:
    builder = BM25IndexBuilder()
    fixture = _source_fixture(tmp_path)
    view_owner = PublishedWorkspaceReceiptOwner()
    context_owner = PublishedWorkspaceReceiptOwner()
    attempt = tmp_path / "attempt-generation"
    output_root = fixture.workspace / "outputs"
    output_root.mkdir(mode=0o700)
    forbidden = tmp_path / "forbidden"
    forbidden.mkdir(mode=0o700)
    view_destination = output_root / "view"
    context_destination = output_root / "context"
    if case == "same":
        view_destination = context_destination = output_root / "shared"
    elif case == "nested":
        context_destination = view_destination / "context"
    elif case == "physical-alias":
        real_parent = output_root / "real"
        real_parent.mkdir(mode=0o700)
        alias_parent = output_root / "alias"
        alias_parent.symlink_to(real_parent, target_is_directory=True)
        view_destination = real_parent / "artifact"
        context_destination = alias_parent / "artifact"
    elif case == "existing-view":
        view_destination.mkdir(mode=0o700)
    elif case == "existing-context":
        context_destination.mkdir(mode=0o700)
    elif case == "repository":
        view_destination = fixture.repository / "view"
    elif case == "forbidden":
        view_destination = forbidden / "view"
    else:  # pragma: no cover - parameter invariant
        raise AssertionError(f"unknown topology case: {case}")

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
                view_destination=view_destination,
                context_destination=context_destination,
                forbidden_paths=(forbidden,),
            )
            monkeypatch.setattr(
                BM25IndexBuilder,
                "prepare_from_repository_source",
                lambda *_args, **_kwargs: pytest.fail(
                    "builder ran before output topology was validated"
                ),
            )

            with pytest.raises(error, match=message):
                executor.execute(context)

            assert not attempt.exists()
            assert executor.attempt_output_owner.state == "empty"
            assert view_owner.state == "empty"
            assert context_owner.state == "empty"
            assert not any(path.is_file() for path in (tmp_path / "cas").rglob("*"))
    finally:
        context_owner.close()
        view_owner.close()
        fixture.close()


@pytest.mark.parametrize(
    ("case", "error", "message"),
    [
        ("same", ValueError, "output destinations overlap"),
        ("physical-alias", ValueError, "output destinations overlap"),
        ("existing-context", FileExistsError, "destination must be missing"),
    ],
)
def test_retained_generation_rechecks_output_topology_before_recapture_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    error: type[Exception],
    message: str,
) -> None:
    builder = BM25IndexBuilder()
    fixture = _source_fixture(tmp_path)
    initial_view_owner = PublishedWorkspaceReceiptOwner()
    initial_context_owner = PublishedWorkspaceReceiptOwner()
    retry_view_owner = PublishedWorkspaceReceiptOwner()
    retry_context_owner = PublishedWorkspaceReceiptOwner()
    attempt = tmp_path / "attempt-generation"
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
                view_owner=initial_view_owner,
                context_owner=initial_context_owner,
            )
            assert executor.execute(context).publishable
            expected_manifest = RepoManifest.load(attempt / "repo_manifest.json")

            retry_root = fixture.workspace / "retry"
            retry_root.mkdir(mode=0o700)
            view_destination = retry_root / "view"
            context_destination = retry_root / "context"
            if case == "same":
                view_destination = context_destination = retry_root / "shared"
            elif case == "physical-alias":
                real_parent = retry_root / "real"
                real_parent.mkdir(mode=0o700)
                alias_parent = retry_root / "alias"
                alias_parent.symlink_to(real_parent, target_is_directory=True)
                view_destination = real_parent / "artifact"
                context_destination = alias_parent / "artifact"
            elif case == "existing-context":
                context_destination.mkdir(mode=0o700)
            else:  # pragma: no cover - parameter invariant
                raise AssertionError(f"unknown topology case: {case}")

            monkeypatch.setattr(
                cache_import_module,
                "_plan_retained_bm25_publication_view",
                lambda *_args, **_kwargs: pytest.fail(
                    "retained BM25 recapture was planned before topology validation"
                ),
            )
            with pytest.raises(error, match=message):
                cache_import_module.prepare_compiler_cache_job_view_from_generation(
                    executor.attempt_output_owner,
                    expected_manifest=expected_manifest,
                    job=context.job,
                    views=context.views,
                    repository_source=fixture.source,
                    view_output_owner=retry_view_owner,
                    context_output_owner=retry_context_owner,
                    view_destination=view_destination,
                    context_destination=context_destination,
                    workspace_provider=fixture.provider,
                    repository_key=_REPOSITORY_KEY,
                    object_store=cas,
                    environ={},
                )

            assert retry_view_owner.state == "empty"
            assert retry_context_owner.state == "empty"
    finally:
        retry_context_owner.close()
        retry_view_owner.close()
        initial_context_owner.close()
        initial_view_owner.close()
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

    attempt_workspace = tmp_path / "attempt-workspace"
    attempt_workspace.mkdir(mode=0o700)
    attempt_owner = PublishedWorkspaceReceiptOwner()
    view_owner = PublishedWorkspaceReceiptOwner()
    context_owner = PublishedWorkspaceReceiptOwner()
    try:
        executor = BM25SourceJobExecutor(
            attempt_generation=attempt_workspace / "attempt",
            display_commit=_COMMIT,
            builder=BM25IndexBuilder(),
            attempt_output_owner=attempt_owner,
            attempt_workspace_provider=LocalWorkspaceProvider(attempt_workspace),
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


def test_bm25_source_executor_rejects_protocol_only_attempt_provider(
    tmp_path: Path,
) -> None:
    class ProtocolOnlyAttemptProvider:
        def require_support(self) -> None:
            raise AssertionError("incompatible provider support check was called")

        def run_workspace(self, *_args, **_kwargs):
            raise AssertionError("incompatible provider was used")

    attempt_owner = PublishedWorkspaceReceiptOwner()
    view_owner = PublishedWorkspaceReceiptOwner()
    context_owner = PublishedWorkspaceReceiptOwner()
    try:
        with pytest.raises(TypeError, match="exact local workspace provider"):
            BM25SourceJobExecutor(
                attempt_generation=tmp_path / "attempt",
                display_commit=_COMMIT,
                builder=BM25IndexBuilder(),
                attempt_output_owner=attempt_owner,
                attempt_workspace_provider=ProtocolOnlyAttemptProvider(),  # type: ignore[arg-type]
                repository_source=object(),  # type: ignore[arg-type]
                view_output_owner=view_owner,
                context_output_owner=context_owner,
                view_destination=tmp_path / "view",
                context_destination=tmp_path / "context",
                workspace_provider=object(),  # type: ignore[arg-type]
                repository_key=_REPOSITORY_KEY,
                object_store=object(),  # type: ignore[arg-type]
            )
    finally:
        context_owner.close()
        view_owner.close()
        attempt_owner.close()


@pytest.mark.parametrize("case", ["equal", "outside", "physical-escape"])
def test_bm25_source_executor_rejects_attempt_outside_provider_root(
    tmp_path: Path,
    case: str,
) -> None:
    fixture = _source_fixture(tmp_path)
    view_owner = PublishedWorkspaceReceiptOwner()
    context_owner = PublishedWorkspaceReceiptOwner()
    attempt_root = tmp_path / "attempt-root"
    attempt_root.mkdir(mode=0o700)
    attempt_provider = LocalWorkspaceProvider(attempt_root)
    if case == "equal":
        attempt = attempt_root
    elif case == "outside":
        attempt = tmp_path / "outside-attempt"
    elif case == "physical-escape":
        outside_parent = tmp_path / "outside-parent"
        outside_parent.mkdir(mode=0o700)
        alias = attempt_root / "alias"
        alias.symlink_to(outside_parent, target_is_directory=True)
        attempt = alias / "attempt"
    else:  # pragma: no cover - parameter invariant
        raise AssertionError(f"unknown provider-root case: {case}")

    try:
        with LocalCAS(tmp_path / "cas") as cas:
            with pytest.raises(ValueError, match="strictly below"):
                _executor(
                    fixture,
                    cas,
                    BM25IndexBuilder(),
                    attempt_generation=attempt,
                    view_owner=view_owner,
                    context_owner=context_owner,
                    attempt_provider=attempt_provider,
                )

            if case == "equal":
                assert attempt.is_dir()
                assert not tuple(attempt.iterdir())
            else:
                assert not attempt.exists()
            assert view_owner.state == "empty"
            assert context_owner.state == "empty"
    finally:
        context_owner.close()
        view_owner.close()
        fixture.close()


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


def test_local_bm25_source_target_binds_exact_repository_root_authority(
    tmp_path: Path,
) -> None:
    fixture = _source_fixture(tmp_path)
    foreign = tmp_path / "foreign-repository"
    foreign.mkdir(mode=0o700)
    try:
        positional_environment = {"CODENIB_SOURCE_POSITIONAL_TEST": "preserved"}
        positional_target = LocalBM25SourceJobTarget(
            fixture.repository,
            fixture.provider,
            _REPOSITORY_KEY,
            _COMMIT,
            BM25IndexBuilder(),
            "default",
            positional_environment,
        )
        assert positional_target.environ == positional_environment
        assert positional_target.repository_root_authority is None
        assert positional_target.attempt_pool_root == fixture.workspace

        with pin_repository_source_root(fixture.repository) as authority:
            target = LocalBM25SourceJobTarget(
                repository_root=fixture.repository,
                workspace_provider=fixture.provider,
                repository_key=_REPOSITORY_KEY,
                display_commit=_COMMIT,
                builder=BM25IndexBuilder(),
                repository_root_authority=authority,
                environ={},
            )
            assert target.repository_root_authority is authority

        with pin_repository_source_root(foreign) as foreign_authority:
            with pytest.raises(
                ValueError,
                match="repository authority differs from its root",
            ):
                LocalBM25SourceJobTarget(
                    repository_root=fixture.repository,
                    workspace_provider=fixture.provider,
                    repository_key=_REPOSITORY_KEY,
                    display_commit=_COMMIT,
                    builder=BM25IndexBuilder(),
                    repository_root_authority=foreign_authority,
                    environ={},
                )

        with pytest.raises(TypeError, match="authority has an invalid type"):
            LocalBM25SourceJobTarget(
                repository_root=fixture.repository,
                workspace_provider=fixture.provider,
                repository_key=_REPOSITORY_KEY,
                display_commit=_COMMIT,
                builder=BM25IndexBuilder(),
                repository_root_authority=object(),  # type: ignore[arg-type]
                environ={},
            )
        with pytest.raises(TypeError, match="orphan sink is invalid"):
            LocalBM25SourceJobTarget(
                repository_root=fixture.repository,
                workspace_provider=fixture.provider,
                repository_key=_REPOSITORY_KEY,
                display_commit=_COMMIT,
                builder=BM25IndexBuilder(),
                attempt_orphan_sink=object(),  # type: ignore[arg-type]
                environ={},
            )
        assert not tuple(fixture.workspace.iterdir())
    finally:
        fixture.close()


def test_local_bm25_source_job_factory_runs_worker_and_cleans_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = BM25IndexBuilder(languages=["python"], max_k=17)
    profile = bm25_source_job_profile(builder)
    fixture = _source_fixture(tmp_path)
    catalog_path = tmp_path / "catalog.sqlite"
    environment = {"CODENIB_SOURCE_RESOURCE_TEST": "before"}
    accepted_orphans: list[DirectoryOrphan] = []
    target = LocalBM25SourceJobTarget(
        repository_root=fixture.repository,
        workspace_provider=fixture.provider,
        repository_key=_REPOSITORY_KEY,
        display_commit=_COMMIT,
        builder=builder,
        environ=environment,
        attempt_orphan_sink=accepted_orphans.append,
    )
    assert target.profile_id == profile.profile_id
    assert target.environ == environment

    builder.languages[:] = ["javascript"]
    builder.max_k = 99
    builder.source_selection = RepositorySourceSelection(("sample.py",))
    environment["CODENIB_SOURCE_RESOURCE_TEST"] = "after"

    try:
        with SQLiteCatalog(catalog_path) as catalog:
            repository_id = catalog.create_repository(_REPOSITORY_KEY)
            assert repository_id == target.repository_id
            source_revision_id = catalog.create_source_revision(
                repository_id,
                commit_sha=None,
                dirty=True,
                source_fingerprint=fixture.source.fingerprint,
            )
            profile_id = catalog.create_view_profile(
                "bm25",
                profile.config,
                name=profile.name,
            )
            _register_projection_profile(catalog)
            assert profile_id == target.profile_id
            queued = catalog.create_job(
                repository_id,
                source_revision_id,
                "local-bm25-source-worker",
                {
                    "contract": INDEX_JOB_REQUEST_CONTRACT,
                    "views": {
                        "bm25": {
                            "profile_id": profile_id,
                            "requested_mode": "full",
                            "required": True,
                        }
                    },
                },
            )

        with LocalCAS(tmp_path / "cas") as cas:
            monkeypatch.setattr(
                job_resources_module,
                "discard_owned_directory",
                lambda *_args, **_kwargs: pytest.fail(
                    "BM25 cleanup used the legacy per-directory discard path"
                ),
            )
            resources = LocalBM25SourceJobResourceFactory((target,))
            assert resources.accepts_candidate(queued)
            worker = IndexJobWorker(
                catalog_factory=lambda: SQLiteCatalog(catalog_path, create=False),
                object_store=cas,
                resolver=BM25SourceJobResolver(
                    resource_factory=resources,
                    object_store=cas,
                ),
                candidate_filter=resources.accepts_candidate,
                lease_duration_ms=60_000,
                heartbeat_interval_ms=5,
                owner_id_factory=lambda: "local-bm25-source-worker",
            )

            outcome = worker.run_once()

            assert outcome.disposition is IndexJobWorkerDisposition.SUCCEEDED
            assert outcome.job_id == queued.job_id
            assert not tuple(fixture.workspace.glob(".codenib-source-job-*"))
            orphans = tuple(fixture.workspace.glob(".*.discarded-*"))
            assert len(orphans) == 1
            assert len(accepted_orphans) == 1
            assert accepted_orphans[0].path == orphans[0]
            inventory = accepted_orphans[0].reopen(lambda reader: reader.inventory())
            assert {entry for entry in inventory if "/" not in entry[0]} == {
                ("attempt", "directory"),
                ("bm25", "directory"),
                ("context", "directory"),
            }
            with SQLiteCatalog(catalog_path, create=False) as catalog:
                completed = catalog.get_job(queued.job_id)
                assert completed.status is IndexJobStatus.SUCCEEDED
                assert completed.result_snapshot_id is not None
                events = catalog.list_job_events(queued.job_id)
                assert any(
                    json.loads(event.payload_json).get("adapter") == "bm25_source"
                    for event in events
                )
    finally:
        fixture.close()


def test_local_bm25_source_scope_nests_outputs_and_orders_outer_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = BM25IndexBuilder()
    fixture = _source_fixture(tmp_path)
    accepted_orphans: list[DirectoryOrphan] = []
    order: list[str] = []

    def accept(orphan: DirectoryOrphan) -> None:
        order.append("accept outer orphan")
        accepted_orphans.append(orphan)

    target = LocalBM25SourceJobTarget(
        repository_root=fixture.repository,
        workspace_provider=fixture.provider,
        repository_key=_REPOSITORY_KEY,
        display_commit=_COMMIT,
        builder=builder,
        attempt_orphan_sink=accept,
        environ={},
    )
    real_receipt_close = job_resources_module._AttemptWorkspaceCleanupOwner.close
    real_isolate = job_resources_module.OwnedPathBuildDirectory.isolate
    real_root_close = job_resources_module.OwnedPathBuildDirectory.close

    def close_receipt(cleanup) -> None:
        order.append(f"close {cleanup.label}")
        real_receipt_close(cleanup)

    def isolate_root(owner):
        order.append("isolate outer root")
        return real_isolate(owner)

    def close_root(owner) -> None:
        order.append("ack outer orphan")
        real_root_close(owner)

    monkeypatch.setattr(
        job_resources_module._AttemptWorkspaceCleanupOwner,
        "close",
        close_receipt,
    )
    monkeypatch.setattr(
        job_resources_module.OwnedPathBuildDirectory,
        "isolate",
        isolate_root,
    )
    monkeypatch.setattr(
        job_resources_module.OwnedPathBuildDirectory,
        "close",
        close_root,
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
            resources = LocalBM25SourceJobResourceFactory((target,))

            with resources.create_scope(
                context,
                object_store=cas,
            ).resources as executor:
                attempt_root = executor.attempt_workspace_provider.allowed_root
                assert attempt_root.parent == fixture.workspace
                assert executor.attempt_generation == attempt_root / "attempt"
                assert executor.view_destination == attempt_root / "bm25"
                assert executor.context_destination == attempt_root / "context"
                descriptor = os.open(attempt_root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    assert executor.attempt_parent_identity == (
                        publication_parent_identity(descriptor)
                    )
                finally:
                    os.close(descriptor)

        assert order == [
            "close source context",
            "close source BM25",
            "close source attempt",
            "isolate outer root",
            "accept outer orphan",
            "ack outer orphan",
        ]
        assert len(accepted_orphans) == 1
        assert accepted_orphans[0].reopen(lambda reader: reader.inventory()) == ()
    finally:
        fixture.close()


def test_local_bm25_source_scope_redelivers_outer_orphan_until_sink_ack(
    tmp_path: Path,
) -> None:
    builder = BM25IndexBuilder()
    fixture = _source_fixture(tmp_path)
    accepted_orphans: list[DirectoryOrphan] = []
    rejection = RuntimeError("attempt orphan sink rejected receipt")

    def reject_once(orphan: DirectoryOrphan) -> None:
        accepted_orphans.append(orphan)
        if len(accepted_orphans) == 1:
            raise rejection

    target = LocalBM25SourceJobTarget(
        repository_root=fixture.repository,
        workspace_provider=fixture.provider,
        repository_key=_REPOSITORY_KEY,
        display_commit=_COMMIT,
        builder=builder,
        attempt_orphan_sink=reject_once,
        environ={},
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
            resources = LocalBM25SourceJobResourceFactory((target,))

            with pytest.raises(
                StorageIntegrityError,
                match="attempt resource cleanup did not settle",
            ) as caught:
                with resources.create_scope(
                    context,
                    object_store=cas,
                ).resources:
                    pass

        assert len(accepted_orphans) == 1
        orphan = accepted_orphans[0]
        cleanup_owners = getattr(
            caught.value,
            "publication_cleanup_owners",
            (),
        )
        attempt_root_cleanup = next(
            owner
            for owner in cleanup_owners
            if isinstance(
                owner,
                job_resources_module._BM25AttemptRootCleanupOwner,
            )
        )
        assert not attempt_root_cleanup.closed
        assert attempt_root_cleanup.orphan is orphan

        attempt_root_cleanup.close()

        assert attempt_root_cleanup.closed
        assert accepted_orphans == [orphan, orphan]
        assert orphan.reopen(lambda reader: reader.inventory()) == ()
    finally:
        fixture.close()


def test_local_bm25_source_scope_retries_cancelled_orphan_sink_before_ack(
    tmp_path: Path,
) -> None:
    builder = BM25IndexBuilder()
    fixture = _source_fixture(tmp_path)
    accepted_orphans: list[DirectoryOrphan] = []
    interruption = KeyboardInterrupt("attempt orphan sink was interrupted")

    def interrupt_once(orphan: DirectoryOrphan) -> None:
        accepted_orphans.append(orphan)
        if len(accepted_orphans) == 1:
            raise interruption

    target = LocalBM25SourceJobTarget(
        repository_root=fixture.repository,
        workspace_provider=fixture.provider,
        repository_key=_REPOSITORY_KEY,
        display_commit=_COMMIT,
        builder=builder,
        attempt_orphan_sink=interrupt_once,
        environ={},
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
            resources = LocalBM25SourceJobResourceFactory((target,))

            with pytest.raises(KeyboardInterrupt) as caught:
                with resources.create_scope(
                    context,
                    object_store=cas,
                ).resources:
                    pass

        assert caught.value is interruption
        assert len(accepted_orphans) == 2
        assert accepted_orphans[0] is accepted_orphans[1]
        assert accepted_orphans[0].reopen(lambda reader: reader.inventory()) == ()
    finally:
        fixture.close()


@pytest.mark.parametrize("reclaim_quiescent_attempts", (False, True))
def test_jobs_run_once_source_bm25_executes_matching_catalog_job(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    reclaim_quiescent_attempts: bool,
) -> None:
    builder = BM25IndexBuilder(languages=["python"])
    profile = bm25_source_job_profile(builder)
    fixture = _source_fixture(tmp_path, git_checkout=True)
    catalog_path = tmp_path / "catalog.sqlite"
    cas_root = tmp_path / "cas"
    queued = None
    try:
        try:
            fixture.provider.require_support()
        except UnsupportedWorkspaceCreation as exc:
            pytest.skip(str(exc))
        with LocalCAS.provision(cas_root):
            pass
        with SQLiteCatalog(catalog_path) as catalog:
            repository_id = catalog.create_repository(_REPOSITORY_KEY)
            source_revision_id = catalog.create_source_revision(
                repository_id,
                commit_sha=None,
                dirty=True,
                source_fingerprint=fixture.source.fingerprint,
            )
            profile_id = catalog.create_view_profile(
                "bm25",
                profile.config,
                name=profile.name,
            )
            _register_projection_profile(catalog)
            assert profile_id == profile.profile_id
            queued = catalog.create_job(
                repository_id,
                source_revision_id,
                "source-cli-worker",
                {
                    "contract": INDEX_JOB_REQUEST_CONTRACT,
                    "views": {
                        "bm25": {
                            "profile_id": profile_id,
                            "requested_mode": "full",
                            "required": True,
                        }
                    },
                },
            )

        command = [
            "jobs",
            "run-once",
            os.fspath(fixture.repository),
            "--source-bm25",
            "--language",
            "python",
            "--catalog",
            os.fspath(catalog_path),
            "--cas-root",
            os.fspath(cas_root),
            "--workspace-root",
            os.fspath(fixture.workspace),
            "--repository",
            _REPOSITORY_KEY,
            "--lease-duration-ms",
            "60000",
            "--heartbeat-interval-ms",
            "5",
            "--json",
        ]
        if reclaim_quiescent_attempts:
            command.append("--reclaim-quiescent-attempts")
        args = cli_module.build_parser().parse_args(command)

        assert args.handler(args) == 0

        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload == {
            "attempt_count": 1,
            "disposition": "succeeded",
            "job_id": queued.job_id,
        }
        assert not tuple(fixture.workspace.glob(".codenib-source-job-*"))
        assert not tuple(fixture.workspace.glob(".codenib-source-worker-topology-*"))
        shards = tuple(fixture.workspace.glob(".codenib-bm25-attempt-pool-v1-repo_*"))
        assert len(shards) == 1
        orphans = tuple(shards[0].glob(".*.discarded-*"))
        if reclaim_quiescent_attempts:
            assert orphans == ()
            assert captured.err.endswith(
                "BM25 attempt-pool reclamation: scanned=1 reclaimed=1 "
                "current=1 legacy=0 discarded=1 retained=0\n"
            )
        else:
            assert len(orphans) == 1
            assert orphans[0].is_dir()
            assert "BM25 attempt-pool reclamation:" not in captured.err
        with SQLiteCatalog(catalog_path, create=False) as catalog:
            completed = catalog.get_job(queued.job_id)
            assert completed.status is IndexJobStatus.SUCCEEDED
            assert completed.result_snapshot_id is not None
    finally:
        fixture.close()


def test_local_bm25_source_worker_preserves_current_ref_after_source_changes(
    tmp_path: Path,
) -> None:
    builder = BM25IndexBuilder(languages=["python"])
    profile = bm25_source_job_profile(builder)
    fixture = _source_fixture(tmp_path)
    catalog_path = tmp_path / "catalog.sqlite"
    target = LocalBM25SourceJobTarget(
        repository_root=fixture.repository,
        workspace_provider=fixture.provider,
        repository_key=_REPOSITORY_KEY,
        display_commit=_COMMIT,
        builder=builder,
        environ={},
    )
    request = {
        "contract": INDEX_JOB_REQUEST_CONTRACT,
        "views": {
            "bm25": {
                "profile_id": profile.profile_id,
                "requested_mode": "full",
                "required": True,
            }
        },
    }
    try:
        with SQLiteCatalog(catalog_path) as catalog:
            repository_id = catalog.create_repository(_REPOSITORY_KEY)
            source_revision_id = catalog.create_source_revision(
                repository_id,
                commit_sha=None,
                dirty=True,
                source_fingerprint=fixture.source.fingerprint,
            )
            assert (
                catalog.create_view_profile(
                    "bm25",
                    profile.config,
                    name=profile.name,
                )
                == profile.profile_id
            )
            _register_projection_profile(catalog)
            first = catalog.create_job(
                repository_id,
                source_revision_id,
                "source-before-mutation",
                request,
                max_attempts=1,
            )

        with LocalCAS(tmp_path / "cas") as cas:
            resources = LocalBM25SourceJobResourceFactory((target,))
            worker = IndexJobWorker(
                catalog_factory=lambda: SQLiteCatalog(catalog_path, create=False),
                object_store=cas,
                resolver=BM25SourceJobResolver(
                    resource_factory=resources,
                    object_store=cas,
                ),
                candidate_filter=resources.accepts_candidate,
                lease_duration_ms=60_000,
                heartbeat_interval_ms=5,
                owner_id_factory=lambda: "local-bm25-source-worker",
            )

            first_outcome = worker.run_once()

            assert first_outcome.disposition is IndexJobWorkerDisposition.SUCCEEDED
            assert first_outcome.job_id == first.job_id
            with SQLiteCatalog(catalog_path, create=False) as catalog:
                preserved_ref = catalog.resolve_ref(repository_id)
                stale = catalog.create_job(
                    repository_id,
                    source_revision_id,
                    "source-after-mutation",
                    request,
                    expected_ref_generation=preserved_ref["generation"],
                    max_attempts=1,
                )
            orphans_before = frozenset(fixture.workspace.glob(".*.discarded-*"))
            (fixture.repository / "sample.py").write_text(
                "def answer():\n    return 43\n",
                encoding="utf-8",
            )

            stale_outcome = worker.run_once()

            assert stale_outcome.disposition is IndexJobWorkerDisposition.FAILED
            assert stale_outcome.job_id == stale.job_id
            assert not tuple(fixture.workspace.glob(".codenib-source-job-*"))
            assert frozenset(fixture.workspace.glob(".*.discarded-*")) == orphans_before
            with SQLiteCatalog(catalog_path, create=False) as catalog:
                failed = catalog.get_job(stale.job_id)
                assert failed.status is IndexJobStatus.FAILED
                assert failed.error_code == "worker_executor_failed"
                assert failed.result_snapshot_id is None
                assert catalog.resolve_ref(repository_id) == preserved_ref
    finally:
        fixture.close()


def test_bm25_source_resource_types_are_lazy_public_exports() -> None:
    assert compiler_module.BM25SourceJobResolver is BM25SourceJobResolver
    assert compiler_module.BM25SourceJobResourceFactory is BM25SourceJobResourceFactory
    assert compiler_module.BM25SourceJobResourceScope is BM25SourceJobResourceScope
    assert (
        compiler_module.LocalBM25SourceJobResourceFactory
        is LocalBM25SourceJobResourceFactory
    )
    assert compiler_module.LocalBM25SourceJobTarget is LocalBM25SourceJobTarget


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("matching", True),
        ("profile", False),
        ("incremental", False),
        ("optional", False),
        ("vector", False),
        ("multi-view", False),
        ("foreign", False),
    ],
)
def test_local_bm25_source_candidate_filter_requires_exact_target(
    tmp_path: Path,
    case: str,
    expected: bool,
) -> None:
    builder = BM25IndexBuilder(max_k=17)
    profile = bm25_source_job_profile(builder)
    fixture = _source_fixture(tmp_path)
    target = LocalBM25SourceJobTarget(
        repository_root=fixture.repository,
        workspace_provider=fixture.provider,
        repository_key=_REPOSITORY_KEY,
        display_commit=_COMMIT,
        builder=builder,
        environ={},
    )
    try:
        with SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog:
            repository_id = catalog.create_repository(
                "owner/foreign" if case == "foreign" else _REPOSITORY_KEY
            )
            source_revision_id = catalog.create_source_revision(
                repository_id,
                commit_sha=None,
                dirty=True,
                source_fingerprint=fixture.source.fingerprint,
            )
            requested_profile = (
                bm25_source_job_profile(BM25IndexBuilder(max_k=18))
                if case == "profile"
                else profile
            )
            profile_id = catalog.create_view_profile(
                "bm25",
                requested_profile.config,
                name=requested_profile.name,
            )
            view_type = "vector" if case == "vector" else "bm25"
            if view_type == "vector":
                profile_id = catalog.create_view_profile(
                    "vector",
                    {"fixture": True},
                )
            views: dict[str, dict[str, object]] = {
                view_type: {
                    "profile_id": profile_id,
                    "requested_mode": (
                        "incremental" if case == "incremental" else "full"
                    ),
                    "required": case != "optional",
                }
            }
            if case == "multi-view":
                views["vector"] = {
                    "profile_id": catalog.create_view_profile(
                        "vector",
                        {"fixture": "multi"},
                    ),
                    "requested_mode": "full",
                    "required": True,
                }
            queued = catalog.create_job(
                repository_id,
                source_revision_id,
                f"source-candidate-{case}",
                {"contract": INDEX_JOB_REQUEST_CONTRACT, "views": views},
            )

        resources = LocalBM25SourceJobResourceFactory((target,))
        assert resources.accepts_candidate(queued) is expected
    finally:
        fixture.close()


def test_local_bm25_source_scope_is_side_effect_free_until_enter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = BM25IndexBuilder()
    fixture = _source_fixture(tmp_path)
    target = LocalBM25SourceJobTarget(
        repository_root=fixture.repository,
        workspace_provider=fixture.provider,
        repository_key=_REPOSITORY_KEY,
        display_commit=_COMMIT,
        builder=builder,
        environ={},
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
            resources = LocalBM25SourceJobResourceFactory((target,))
            monkeypatch.setattr(
                job_resources_module,
                "capture_repository_source",
                lambda *_args, **_kwargs: pytest.fail(
                    "source authority was acquired while declaring the scope"
                ),
            )

            scope = resources.create_scope(context, object_store=cas)

            assert type(scope) is BM25SourceJobResourceScope
            assert scope.object_store is cas
            assert not tuple(fixture.workspace.iterdir())
    finally:
        fixture.close()


def test_local_bm25_source_scope_threads_repository_root_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = BM25IndexBuilder()
    fixture = _source_fixture(tmp_path)
    observed: list[object] = []
    accepted_orphans: list[DirectoryOrphan] = []
    real_capture = job_resources_module.capture_repository_source
    try:
        with (
            pin_repository_source_root(fixture.repository) as authority,
            LocalCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            target = LocalBM25SourceJobTarget(
                repository_root=fixture.repository,
                workspace_provider=fixture.provider,
                repository_key=_REPOSITORY_KEY,
                display_commit=_COMMIT,
                builder=builder,
                repository_root_authority=authority,
                attempt_orphan_sink=accepted_orphans.append,
                environ={},
            )
            context = _execution_context(
                catalog,
                fixture,
                bm25_source_job_profile(builder),
            )

            def capture(*args: object, **kwargs: object):
                observed.append(kwargs.get("expected_root_authority"))
                return real_capture(*args, **kwargs)

            monkeypatch.setattr(
                job_resources_module,
                "capture_repository_source",
                capture,
            )
            resources = LocalBM25SourceJobResourceFactory((target,))

            with resources.create_scope(
                context, object_store=cas
            ).resources as executor:
                assert type(executor) is BM25SourceJobExecutor

            assert observed == [authority]
            assert len(accepted_orphans) == 1
            assert accepted_orphans[0].reopen(lambda reader: reader.inventory()) == ()
    finally:
        fixture.close()


def test_routed_bm25_source_scope_holds_shared_lease_through_orphan_ack(
    tmp_path: Path,
) -> None:
    builder = BM25IndexBuilder()
    fixture = _source_fixture(tmp_path)
    workspace_identity = _directory_identity(fixture.workspace)
    accepted_orphans: list[DirectoryOrphan] = []
    try:
        with (
            pin_repository_source_root(fixture.repository) as authority,
            LocalCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            binding = bootstrap_local_bm25_attempt_pool(
                workspace_root=fixture.workspace,
                workspace_identity=workspace_identity,
                repository_id=_source_repository_id(),
                repository_authority=authority,
                topology_verifier=lambda: None,
            )

            def accept(orphan: DirectoryOrphan) -> None:
                contention = _probe_nonblocking_reaper(binding.reaper_route)
                assert type(contention) is BlockingIOError
                accepted_orphans.append(orphan)

            target = LocalBM25SourceJobTarget(
                repository_root=fixture.repository,
                workspace_provider=fixture.provider,
                repository_key=_REPOSITORY_KEY,
                display_commit=_COMMIT,
                builder=builder,
                repository_root_authority=authority,
                workspace_parent_identity=workspace_identity,
                topology_verifier=lambda: None,
                attempt_orphan_sink=accept,
                attempt_pool_writer_route=binding.writer_route,
                environ={},
            )
            context = _execution_context(
                catalog,
                fixture,
                bm25_source_job_profile(builder),
            )
            resources = LocalBM25SourceJobResourceFactory((target,))

            with resources.create_scope(
                context, object_store=cas
            ).resources as executor:
                assert (
                    executor.attempt_workspace_provider.allowed_root.parent
                    == binding.writer_route._shard_path
                )
                assert type(_probe_nonblocking_reaper(binding.reaper_route)) is (
                    BlockingIOError
                )

            assert len(accepted_orphans) == 1
            assert accepted_orphans[0].path.parent == binding.writer_route._shard_path
            assert _probe_nonblocking_reaper(binding.reaper_route) is None
    finally:
        fixture.close()


def test_routed_bm25_source_acquisition_cancellation_settles_empty_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Cancelled(BaseException):
        pass

    builder = BM25IndexBuilder()
    fixture = _source_fixture(tmp_path)
    workspace_identity = _directory_identity(fixture.workspace)
    cancellation = Cancelled("writer lease acquisition cancelled")
    real_acquire = bm25_attempt_pool_module._LocalBM25AttemptPoolWriterRoute._acquire
    try:
        with (
            pin_repository_source_root(fixture.repository) as authority,
            LocalCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            binding = bootstrap_local_bm25_attempt_pool(
                workspace_root=fixture.workspace,
                workspace_identity=workspace_identity,
                repository_id=_source_repository_id(),
                repository_authority=authority,
                topology_verifier=lambda: None,
            )
            target = LocalBM25SourceJobTarget(
                repository_root=fixture.repository,
                workspace_provider=fixture.provider,
                repository_key=_REPOSITORY_KEY,
                display_commit=_COMMIT,
                builder=builder,
                repository_root_authority=authority,
                workspace_parent_identity=workspace_identity,
                topology_verifier=lambda: None,
                attempt_pool_writer_route=binding.writer_route,
                environ={},
            )
            context = _execution_context(
                catalog,
                fixture,
                bm25_source_job_profile(builder),
            )

            def cancel_acquire(route, *, check_cancelled, construction_owner):
                assert route is binding.writer_route

                def cancel() -> None:
                    raise cancellation

                return real_acquire(
                    route,
                    check_cancelled=cancel,
                    construction_owner=construction_owner,
                )

            monkeypatch.setattr(
                bm25_attempt_pool_module._LocalBM25AttemptPoolWriterRoute,
                "_acquire",
                cancel_acquire,
            )
            resources = LocalBM25SourceJobResourceFactory((target,))

            with pytest.raises(Cancelled) as caught:
                with resources.create_scope(context, object_store=cas).resources:
                    pass

            assert caught.value is cancellation
            assert tuple(binding.writer_route._shard_path.iterdir()) == ()
            assert _probe_nonblocking_reaper(binding.reaper_route) is None
    finally:
        fixture.close()


def test_routed_bm25_source_route_return_cancellation_skips_global_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Cancelled(BaseException):
        pass

    builder = BM25IndexBuilder()
    fixture = _source_fixture(tmp_path)
    workspace_identity = _directory_identity(fixture.workspace)
    cancellation = Cancelled("writer lease route return cancelled")
    retry_calls = 0
    real_acquire = bm25_attempt_pool_module._LocalBM25AttemptPoolWriterRoute._acquire
    try:
        with (
            pin_repository_source_root(fixture.repository) as authority,
            LocalCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            binding = bootstrap_local_bm25_attempt_pool(
                workspace_root=fixture.workspace,
                workspace_identity=workspace_identity,
                repository_id=_source_repository_id(),
                repository_authority=authority,
                topology_verifier=lambda: None,
            )
            target = LocalBM25SourceJobTarget(
                repository_root=fixture.repository,
                workspace_provider=fixture.provider,
                repository_key=_REPOSITORY_KEY,
                display_commit=_COMMIT,
                builder=builder,
                repository_root_authority=authority,
                workspace_parent_identity=workspace_identity,
                topology_verifier=lambda: None,
                attempt_pool_writer_route=binding.writer_route,
                environ={},
            )
            context = _execution_context(
                catalog,
                fixture,
                bm25_source_job_profile(builder),
            )

            def cancel_after_route_return(
                route,
                *,
                check_cancelled,
                construction_owner,
            ):
                real_acquire(
                    route,
                    check_cancelled=check_cancelled,
                    construction_owner=construction_owner,
                )
                raise cancellation

            def reject_global_retry(*_args, **_kwargs) -> None:
                nonlocal retry_calls
                retry_calls += 1
                raise AssertionError("unstarted lease cleanup entered global retry")

            monkeypatch.setattr(
                bm25_attempt_pool_module._LocalBM25AttemptPoolWriterRoute,
                "_acquire",
                cancel_after_route_return,
            )
            monkeypatch.setattr(
                job_resources_module,
                "_retry_retained_owned_path_build_cleanup_for_group",
                reject_global_retry,
            )
            resources = LocalBM25SourceJobResourceFactory((target,))

            with pytest.raises(Cancelled) as caught:
                with resources.create_scope(context, object_store=cas).resources:
                    pass

            assert caught.value is cancellation
            assert retry_calls == 0
            assert tuple(binding.writer_route._shard_path.iterdir()) == ()
            assert _probe_nonblocking_reaper(binding.reaper_route) is None
    finally:
        fixture.close()


def test_routed_bm25_source_failure_precedes_shared_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = BM25IndexBuilder()
    fixture = _source_fixture(tmp_path)
    workspace_identity = _directory_identity(fixture.workspace)
    source_failure = OSError("source capture failed before writer lease")
    acquire_calls = 0
    try:
        with (
            pin_repository_source_root(fixture.repository) as authority,
            LocalCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            binding = bootstrap_local_bm25_attempt_pool(
                workspace_root=fixture.workspace,
                workspace_identity=workspace_identity,
                repository_id=_source_repository_id(),
                repository_authority=authority,
                topology_verifier=lambda: None,
            )
            target = LocalBM25SourceJobTarget(
                repository_root=fixture.repository,
                workspace_provider=fixture.provider,
                repository_key=_REPOSITORY_KEY,
                display_commit=_COMMIT,
                builder=builder,
                repository_root_authority=authority,
                workspace_parent_identity=workspace_identity,
                topology_verifier=lambda: None,
                attempt_pool_writer_route=binding.writer_route,
                environ={},
            )
            context = _execution_context(
                catalog,
                fixture,
                bm25_source_job_profile(builder),
            )

            def reject_source(*_args, **_kwargs):
                raise source_failure

            def reject_acquire(*_args, **_kwargs):
                nonlocal acquire_calls
                acquire_calls += 1
                pytest.fail("source failure must precede writer lease acquisition")

            monkeypatch.setattr(
                job_resources_module,
                "capture_repository_source",
                reject_source,
            )
            monkeypatch.setattr(
                bm25_attempt_pool_module._LocalBM25AttemptPoolWriterRoute,
                "_acquire",
                reject_acquire,
            )
            resources = LocalBM25SourceJobResourceFactory((target,))

            with pytest.raises(OSError) as caught:
                with resources.create_scope(context, object_store=cas).resources:
                    pass

            assert caught.value is source_failure
            assert acquire_calls == 0
            assert tuple(binding.writer_route._shard_path.iterdir()) == ()
            assert _probe_nonblocking_reaper(binding.reaper_route) is None
    finally:
        fixture.close()


def test_routed_bm25_rejects_wrong_writer_lease_before_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = BM25IndexBuilder()
    fixture = _source_fixture(tmp_path)
    workspace_identity = _directory_identity(fixture.workspace)
    wrong_owner: PrivateDirectoryLeaseOwner | None = None
    try:
        with (
            pin_repository_source_root(fixture.repository) as authority,
            LocalCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            binding = bootstrap_local_bm25_attempt_pool(
                workspace_root=fixture.workspace,
                workspace_identity=workspace_identity,
                repository_id=_source_repository_id(),
                repository_authority=authority,
                topology_verifier=lambda: None,
            )
            target = LocalBM25SourceJobTarget(
                repository_root=fixture.repository,
                workspace_provider=fixture.provider,
                repository_key=_REPOSITORY_KEY,
                display_commit=_COMMIT,
                builder=builder,
                repository_root_authority=authority,
                workspace_parent_identity=workspace_identity,
                topology_verifier=lambda: None,
                attempt_pool_writer_route=binding.writer_route,
                environ={},
            )
            context = _execution_context(
                catalog,
                fixture,
                bm25_source_job_profile(builder),
            )
            installed: list[PrivateDirectoryLeaseOwner] = []
            wrong_owner = acquire_private_directory_lease(
                binding.writer_route._state.directory_lease_route,
                mode=DirectoryLeaseMode.EXCLUSIVE,
                blocking=True,
                _construction_owner=installed.append,
            )

            def hand_off_wrong_mode(
                _route,
                *,
                check_cancelled,
                construction_owner,
            ) -> PrivateDirectoryLeaseOwner:
                assert callable(check_cancelled)
                construction_owner(wrong_owner)
                return wrong_owner

            monkeypatch.setattr(
                bm25_attempt_pool_module._LocalBM25AttemptPoolWriterRoute,
                "_acquire",
                hand_off_wrong_mode,
            )
            resources = LocalBM25SourceJobResourceFactory((target,))

            with pytest.raises(
                StorageIntegrityError,
                match="writer lease authority is inconsistent",
            ):
                with resources.create_scope(context, object_store=cas).resources:
                    pass

            assert tuple(binding.writer_route._shard_path.iterdir()) == ()
    finally:
        if wrong_owner is not None:
            wrong_owner.close()
        fixture.close()


def test_routed_bm25_sink_failure_retains_composite_and_shared_lease(
    tmp_path: Path,
) -> None:
    builder = BM25IndexBuilder()
    fixture = _source_fixture(tmp_path)
    workspace_identity = _directory_identity(fixture.workspace)
    accepted_orphans: list[DirectoryOrphan] = []
    rejection = RuntimeError("routed attempt sink refused receipt")

    def accept(orphan: DirectoryOrphan) -> None:
        accepted_orphans.append(orphan)
        if len(accepted_orphans) == 1:
            raise rejection

    try:
        with (
            pin_repository_source_root(fixture.repository) as authority,
            LocalCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            binding = bootstrap_local_bm25_attempt_pool(
                workspace_root=fixture.workspace,
                workspace_identity=workspace_identity,
                repository_id=_source_repository_id(),
                repository_authority=authority,
                topology_verifier=lambda: None,
            )
            target = LocalBM25SourceJobTarget(
                repository_root=fixture.repository,
                workspace_provider=fixture.provider,
                repository_key=_REPOSITORY_KEY,
                display_commit=_COMMIT,
                builder=builder,
                repository_root_authority=authority,
                workspace_parent_identity=workspace_identity,
                topology_verifier=lambda: None,
                attempt_orphan_sink=accept,
                attempt_pool_writer_route=binding.writer_route,
                environ={},
            )
            context = _execution_context(
                catalog,
                fixture,
                bm25_source_job_profile(builder),
            )
            resources = LocalBM25SourceJobResourceFactory((target,))

            with pytest.raises(
                StorageIntegrityError,
                match="attempt resource cleanup did not settle",
            ) as caught:
                with resources.create_scope(context, object_store=cas).resources:
                    pass

            cleanup = next(
                owner
                for owner in getattr(
                    caught.value,
                    "publication_cleanup_owners",
                    (),
                )
                if type(owner)
                is job_resources_module._BM25AttemptPoolWriterCleanupOwner
            )
            assert {name for name in dir(cleanup) if not name.startswith("_")} == {
                "close",
                "closed",
            }
            assert not cleanup.closed
            assert type(_probe_nonblocking_reaper(binding.reaper_route)) is (
                BlockingIOError
            )

            cleanup.close()

            assert cleanup.closed
            assert len(accepted_orphans) == 2
            assert accepted_orphans[0] is accepted_orphans[1]
            assert _probe_nonblocking_reaper(binding.reaper_route) is None
    finally:
        fixture.close()


def test_routed_bm25_shard_replacement_fails_before_writer_unlock(
    tmp_path: Path,
) -> None:
    builder = BM25IndexBuilder()
    fixture = _source_fixture(tmp_path)
    workspace_identity = _directory_identity(fixture.workspace)
    moved_route: PrivateDirectoryLeaseRoute | None = None
    try:
        with (
            pin_repository_source_root(fixture.repository) as authority,
            LocalCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            binding = bootstrap_local_bm25_attempt_pool(
                workspace_root=fixture.workspace,
                workspace_identity=workspace_identity,
                repository_id=_source_repository_id(),
                repository_authority=authority,
                topology_verifier=lambda: None,
            )
            shard = binding.writer_route._shard_path
            moved = shard.with_name(shard.name + ".moved")

            def replace_shard(_orphan: DirectoryOrphan) -> None:
                nonlocal moved_route
                shard.rename(moved)
                shard.mkdir(mode=0o700)
                moved_route = PrivateDirectoryLeaseRoute(
                    moved,
                    binding.writer_route._shard_identity,
                    os.getpid(),
                )
                assert (
                    type(
                        _probe_nonblocking_directory_lease(
                            moved_route,
                            DirectoryLeaseMode.EXCLUSIVE,
                        )
                    )
                    is BlockingIOError
                )

            target = LocalBM25SourceJobTarget(
                repository_root=fixture.repository,
                workspace_provider=fixture.provider,
                repository_key=_REPOSITORY_KEY,
                display_commit=_COMMIT,
                builder=builder,
                repository_root_authority=authority,
                workspace_parent_identity=workspace_identity,
                topology_verifier=lambda: None,
                attempt_orphan_sink=replace_shard,
                attempt_pool_writer_route=binding.writer_route,
                environ={},
            )
            context = _execution_context(
                catalog,
                fixture,
                bm25_source_job_profile(builder),
            )
            resources = LocalBM25SourceJobResourceFactory((target,))

            with pytest.raises(
                StorageIntegrityError,
                match="shard identity changed",
            ):
                with resources.create_scope(context, object_store=cas).resources:
                    pass

            assert moved_route is not None
            assert (
                _probe_nonblocking_directory_lease(
                    moved_route,
                    DirectoryLeaseMode.EXCLUSIVE,
                )
                is None
            )
            with pytest.raises(
                StorageIntegrityError,
                match="shard identity changed",
            ):
                binding.writer_route._acquire(
                    check_cancelled=None,
                    construction_owner=lambda _owner: None,
                )
            assert tuple(shard.iterdir()) == ()
    finally:
        fixture.close()


def test_routed_bm25_prepare_return_failure_recovers_only_its_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = BM25IndexBuilder()
    fixture = _source_fixture(tmp_path)
    workspace_identity = _directory_identity(fixture.workspace)
    interruption = KeyboardInterrupt("attempt root prepare return interrupted")
    accepted_orphans: list[DirectoryOrphan] = []
    real_prepare = job_resources_module.OwnedPathBuildDirectory.prepare
    try:
        with (
            pin_repository_source_root(fixture.repository) as authority,
            LocalCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            binding = bootstrap_local_bm25_attempt_pool(
                workspace_root=fixture.workspace,
                workspace_identity=workspace_identity,
                repository_id=_source_repository_id(),
                repository_authority=authority,
                topology_verifier=lambda: None,
            )
            target = LocalBM25SourceJobTarget(
                repository_root=fixture.repository,
                workspace_provider=fixture.provider,
                repository_key=_REPOSITORY_KEY,
                display_commit=_COMMIT,
                builder=builder,
                repository_root_authority=authority,
                workspace_parent_identity=workspace_identity,
                topology_verifier=lambda: None,
                attempt_orphan_sink=accepted_orphans.append,
                attempt_pool_writer_route=binding.writer_route,
                environ={},
            )
            context = _execution_context(
                catalog,
                fixture,
                bm25_source_job_profile(builder),
            )

            def interrupt_prepare(_cls, destination, **kwargs):
                real_prepare(destination, **kwargs)
                raise interruption

            monkeypatch.setattr(
                job_resources_module.OwnedPathBuildDirectory,
                "prepare",
                classmethod(interrupt_prepare),
            )
            resources = LocalBM25SourceJobResourceFactory((target,))

            with pytest.raises(KeyboardInterrupt) as caught:
                with resources.create_scope(context, object_store=cas).resources:
                    pass

            assert caught.value is interruption
            assert len(accepted_orphans) == 1
            assert accepted_orphans[0].path.parent == binding.writer_route._shard_path
            assert _probe_nonblocking_reaper(binding.reaper_route) is None
    finally:
        fixture.close()


def test_local_bm25_source_scope_resolves_display_commit_per_attempt(
    tmp_path: Path,
) -> None:
    builder = BM25IndexBuilder(languages=["python"])
    fixture = _source_fixture(tmp_path, git_checkout=True)
    initial_commit = _git_head(fixture.repository)
    accepted_orphans: list[DirectoryOrphan] = []
    target = LocalBM25SourceJobTarget(
        repository_root=fixture.repository,
        workspace_provider=fixture.provider,
        repository_key=_REPOSITORY_KEY,
        display_commit=initial_commit,
        builder=builder,
        display_commit_resolver=lambda: _git_head(fixture.repository),
        attempt_orphan_sink=accepted_orphans.append,
        environ={},
    )
    try:
        fixture.source.close()
        current_commit = _commit_source(
            fixture.repository,
            "def answer():\n    return 43\n",
            "advance fixture",
        )
        fixture.source = capture_repository_source(fixture.repository)
        assert current_commit != initial_commit

        with (
            LocalCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            context = _execution_context(
                catalog,
                fixture,
                bm25_source_job_profile(builder),
            )
            resources = LocalBM25SourceJobResourceFactory((target,))

            with resources.create_scope(
                context,
                object_store=cas,
            ).resources as executor:
                assert executor.display_commit == current_commit

        assert len(accepted_orphans) == 1
        assert accepted_orphans[0].reopen(lambda reader: reader.inventory()) == ()
    finally:
        fixture.close()


def test_local_bm25_source_scope_rejects_replaced_retained_workspace(
    tmp_path: Path,
) -> None:
    builder = BM25IndexBuilder(languages=["python"])
    fixture = _source_fixture(tmp_path)
    descriptor = os.open(fixture.workspace, os.O_RDONLY | os.O_DIRECTORY)
    try:
        workspace_identity = publication_parent_identity(descriptor)
    finally:
        os.close(descriptor)
    target = LocalBM25SourceJobTarget(
        repository_root=fixture.repository,
        workspace_provider=fixture.provider,
        repository_key=_REPOSITORY_KEY,
        display_commit=_COMMIT,
        builder=builder,
        workspace_parent_identity=workspace_identity,
        environ={},
    )
    displaced = tmp_path / "displaced-workspace"
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
            resources = LocalBM25SourceJobResourceFactory((target,))
            resolved = BM25SourceJobResolver(
                resource_factory=resources,
                object_store=cas,
            ).resolve(context.job, context.views)
            fixture.workspace.rename(displaced)
            fixture.workspace.mkdir(mode=0o700)

            with pytest.raises(
                RuntimeError,
                match="publication parent identity does not match authority",
            ):
                resolved.execute(context)

            assert not tuple(fixture.workspace.iterdir())
            assert not tuple(displaced.iterdir())
    finally:
        fixture.close()


def test_local_bm25_source_scope_rechecks_topology_before_attempt_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = BM25IndexBuilder(languages=["python"])
    fixture = _source_fixture(tmp_path)
    revoked = False
    display_commit_calls = 0
    displaced_workspace = tmp_path / "displaced-workspace"
    accepted_orphans: list[DirectoryOrphan] = []

    def verify_topology() -> None:
        if revoked:
            raise StorageValidationError("BM25 source worker topology was revoked")

    target = LocalBM25SourceJobTarget(
        repository_root=fixture.repository,
        workspace_provider=fixture.provider,
        repository_key=_REPOSITORY_KEY,
        display_commit=_COMMIT,
        builder=builder,
        topology_verifier=verify_topology,
        attempt_orphan_sink=accepted_orphans.append,
        environ={},
    )
    real_current_display_commit = LocalBM25SourceJobTarget.current_display_commit

    def current_display_commit(candidate: LocalBM25SourceJobTarget) -> str:
        nonlocal display_commit_calls, revoked
        display_commit = real_current_display_commit(candidate)
        display_commit_calls += 1
        if display_commit_calls == 2:
            fixture.workspace.rename(displaced_workspace)
            fixture.workspace.mkdir(mode=0o700)
            revoked = True
        return display_commit

    monkeypatch.setattr(
        LocalBM25SourceJobTarget,
        "current_display_commit",
        current_display_commit,
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
            resources = LocalBM25SourceJobResourceFactory((target,))

            with pytest.raises(
                StorageValidationError,
                match="worker topology was revoked",
            ):
                with resources.create_scope(context, object_store=cas).resources:
                    pass

        assert display_commit_calls == 2
        assert not tuple(fixture.workspace.iterdir())
        assert not tuple(displaced_workspace.iterdir())
        assert accepted_orphans == []
    finally:
        fixture.close()


def test_local_bm25_source_scope_preserves_workspace_timeout(
    tmp_path: Path,
) -> None:
    builder = BM25IndexBuilder()
    fixture = _source_fixture(tmp_path)
    provision_timeout_ns = 123_456_789
    provider = LocalWorkspaceProvider(
        fixture.workspace,
        provision_timeout_ns=provision_timeout_ns,
    )
    accepted_orphans: list[DirectoryOrphan] = []
    target = LocalBM25SourceJobTarget(
        repository_root=fixture.repository,
        workspace_provider=provider,
        repository_key=_REPOSITORY_KEY,
        display_commit=_COMMIT,
        builder=builder,
        attempt_orphan_sink=accepted_orphans.append,
        environ={},
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
            resources = LocalBM25SourceJobResourceFactory((target,))

            with resources.create_scope(
                context,
                object_store=cas,
            ).resources as executor:
                assert (
                    executor.attempt_workspace_provider.provision_timeout_ns
                    == provision_timeout_ns
                )
                assert (
                    executor.workspace_provider.delegate
                    is executor.attempt_workspace_provider
                )

        assert len(accepted_orphans) == 1
        assert accepted_orphans[0].reopen(lambda reader: reader.inventory()) == ()
    finally:
        fixture.close()


@pytest.mark.parametrize("malformed_notes", ((), None))
def test_bm25_source_scope_cleanup_cannot_replace_executor_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    malformed_notes: object,
) -> None:
    builder = BM25IndexBuilder()
    fixture = _source_fixture(tmp_path)
    view_owner = PublishedWorkspaceReceiptOwner()
    context_owner = PublishedWorkspaceReceiptOwner()
    primary = StorageIntegrityError("primary BM25 source failure")
    BaseException.__setattr__(primary, "__notes__", malformed_notes)
    cleanup = OSError("injected BM25 source cleanup failure")
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
                attempt_generation=tmp_path / "attempt",
                view_owner=view_owner,
                context_owner=context_owner,
            )

            @contextmanager
            def failing_cleanup():
                try:
                    yield executor
                finally:
                    raise cleanup

            class FailingCleanupFactory:
                def create_scope(self, attempt_context, *, object_store):
                    assert attempt_context is context
                    assert object_store is cas
                    return BM25SourceJobResourceScope(
                        object_store=cas,
                        resources=failing_cleanup(),
                    )

            def fail_execute(*_args, **_kwargs):
                raise primary

            monkeypatch.setattr(
                BM25SourceJobExecutor,
                "execute",
                fail_execute,
            )
            resolved = BM25SourceJobResolver(
                resource_factory=FailingCleanupFactory(),
                object_store=cas,
            ).resolve(context.job, context.views)

            with pytest.raises(StorageIntegrityError) as caught:
                resolved.execute(context)

            assert caught.value is primary
            native_notes = getattr(primary, "__notes__", ())
            if type(native_notes) is not list:
                native_notes = ()
            notes = (*native_notes, *getattr(primary, "_codenib_cleanup_notes", ()))
            assert any(
                "BM25 source resource cleanup also failed: OSError" in note
                for note in notes
            )
    finally:
        context_owner.close()
        view_owner.close()
        fixture.close()


def _run_in_isolated_thread(callback: Callable[[], None]) -> None:
    """Keep trace-raised exception state out of the pytest worker thread."""

    failures: list[str] = []

    def run() -> None:
        try:
            callback()
        except (Exception, KeyboardInterrupt) as failure:
            failures.append(type(failure).__name__)

    worker = threading.Thread(target=run)
    worker.start()
    worker.join()
    assert not worker.is_alive()
    assert failures == []


def _warm_scoped_job_capture_tracing(capture: Callable[..., object]) -> None:
    """Warm one handler under tracing without injecting an exception."""

    warm_error = RuntimeError("warm scoped capture tracing")
    warm_outcome = job_resolver_module._ScopedJobExecutionOutcome()

    @contextmanager
    def warm_resources():
        yield object()

    def fail_warm_operation(_executor):
        raise warm_error

    def warm_trace(frame, _event, _arg):
        if frame.f_code is capture.__code__:
            frame.f_trace_opcodes = True
        return warm_trace

    sys.settrace(warm_trace)
    try:
        job_resolver_module._capture_scoped_job_resource_exit(
            warm_resources(),
            fail_warm_operation,
            warm_outcome,
        )
    finally:
        sys.settrace(None)
    assert warm_outcome.operation_error is warm_error
    assert warm_outcome.scope_error is warm_error


def _native_handler_offset(capture: Callable[..., object]) -> int:
    instructions = tuple(dis.get_instructions(capture))
    return next(
        instruction.offset
        for instruction in instructions
        if instruction.opname in {"PUSH_EXC_INFO", "WITH_EXCEPT_START"}
    )


@pytest.mark.parametrize("scope_kind", ("cache", "source"))
@pytest.mark.parametrize(
    ("capture_name", "trace_point", "cleanup_mode"),
    (
        ("_capture_scoped_job_execution", "handler-entry", "raise"),
        ("_capture_scoped_job_execution", "handler-after-store", "raise"),
        ("_capture_scoped_job_execution", "handler-entry", "suppress"),
        ("_capture_scoped_job_execution", "handler-after-store", "suppress"),
        ("_capture_scoped_job_resource_exit", "handler-entry", "raise"),
        ("_capture_scoped_job_resource_exit", "handler-after-store", "raise"),
    ),
)
def test_scoped_job_handler_interruption_preserves_executor_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope_kind: str,
    capture_name: str,
    trace_point: str,
    cleanup_mode: str,
) -> None:
    primary = StorageIntegrityError("primary scoped executor failure")
    cleanup = OSError("scoped resource cleanup failure")
    injected = KeyboardInterrupt("handler interrupted")
    cleanup_owner = object()
    BaseException.__setattr__(
        cleanup,
        "publication_cleanup_owners",
        (cleanup_owner,),
    )
    exits: list[str] = []

    class FakeExecutor:
        def __init__(self, object_store) -> None:
            self.object_store = object_store
            self.view_type = "bm25"

        def execute(self, _context):
            raise primary

    if capture_name == "_capture_scoped_job_execution":
        capture_type = job_resolver_module._ScopedJobOperationCapture
        captured_attribute = "operation_error"
    else:
        capture_type = job_resolver_module._ScopedJobResourceExitCapture
        captured_attribute = "scope_error"
    capture = capture_type.__exit__
    instructions = tuple(dis.get_instructions(capture))
    error_store_index = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname == "STORE_ATTR"
        and instruction.argval == captured_attribute
    )
    if trace_point == "handler-entry":
        handler_offset = next(
            instruction.offset
            for instruction in instructions[:error_store_index]
            if instruction.opname not in {"CACHE", "COPY_FREE_VARS", "NOP", "RESUME"}
        )
    else:
        handler_offset = instructions[error_store_index + 1].offset
    hit = False

    def trace(frame, event, _arg):
        nonlocal hit
        if frame.f_code is capture.__code__:
            frame.f_trace_opcodes = True
            if event == "opcode" and frame.f_lasti == handler_offset and not hit:
                hit = True
                sys.settrace(None)
                raise injected
        return trace

    with LocalCAS(tmp_path / "cas") as cas:
        executor = FakeExecutor(cas)

        @contextmanager
        def failing_cleanup():
            try:
                yield executor
            except BaseException as exiting:  # noqa: B036 - inspect scope exit
                exits.append("exit")
                assert exiting is primary
                if cleanup_mode == "raise":
                    raise cleanup

        if scope_kind == "cache":
            monkeypatch.setattr(
                job_resolver_module,
                "CompilerCacheJobExecutor",
                FakeExecutor,
            )
            scope = CompilerCacheJobResourceScope(
                object_store=cas,
                view_type="bm25",
                resources=failing_cleanup(),
            )
            execute = job_resolver_module._execute_in_resource_scope
            expected_note = "compiler cache resource cleanup also failed: OSError"
        else:
            monkeypatch.setattr(
                job_resolver_module,
                "BM25SourceJobExecutor",
                FakeExecutor,
            )
            scope = BM25SourceJobResourceScope(
                object_store=cas,
                resources=failing_cleanup(),
            )
            execute = job_resolver_module._execute_in_bm25_source_resource_scope
            expected_note = "BM25 source resource cleanup also failed: OSError"

        def run_traced_execute() -> None:
            _warm_scoped_job_capture_tracing(capture)
            sys.settrace(trace)
            try:
                with pytest.raises(StorageIntegrityError) as caught:
                    execute(scope, object())
            finally:
                sys.settrace(None)
            assert caught.value is primary

        _run_in_isolated_thread(run_traced_execute)

    assert hit
    assert exits == ["exit"]
    retained_owners = getattr(primary, "publication_cleanup_owners", ())
    if cleanup_mode == "raise":
        assert retained_owners == (cleanup_owner,)
    else:
        assert retained_owners == ()
    notes = (
        *getattr(primary, "__notes__", ()),
        *getattr(primary, "_codenib_cleanup_notes", ()),
    )
    assert (expected_note in notes) is (cleanup_mode == "raise")


@pytest.mark.parametrize("cleanup_mode", ("raise", "suppress", "propagate"))
@pytest.mark.parametrize("primary_context", ("none", "internal", "ambient"))
def test_scoped_job_native_operation_handler_preserves_provenance(
    cleanup_mode: str,
    primary_context: str,
) -> None:
    primary = StorageIntegrityError("primary scoped executor failure")
    internal = ValueError("operation-internal context")
    ambient = RuntimeError("caller ambient context")
    cleanup = OSError("scoped resource cleanup failure")
    injected = KeyboardInterrupt("native operation handler interrupted")
    cleanup_owner = object()
    BaseException.__setattr__(
        cleanup,
        "publication_cleanup_owners",
        (cleanup_owner,),
    )
    capture = job_resolver_module._capture_scoped_job_execution
    handler_offset = _native_handler_offset(capture)
    hit = False
    exits: list[BaseException] = []

    def trace(frame, event, _arg):
        nonlocal hit
        if frame.f_code is capture.__code__:
            frame.f_trace_opcodes = True
            if event == "opcode" and frame.f_lasti == handler_offset and not hit:
                hit = True
                sys.settrace(None)
                raise injected
        return trace

    @contextmanager
    def resources():
        try:
            yield object()
        except BaseException as exiting:  # noqa: B036 - inspect scope exit
            exits.append(exiting)
            if cleanup_mode == "raise":
                raise cleanup from exiting
            if cleanup_mode == "propagate":
                raise

    def operation(_executor):
        if primary_context == "internal":
            try:
                raise internal
            except ValueError as internal_error:
                raise primary from internal_error
        raise primary

    def execute_once() -> None:
        _warm_scoped_job_capture_tracing(capture)
        sys.settrace(trace)
        try:
            with pytest.raises(StorageIntegrityError) as caught:
                job_resolver_module._execute_scoped_job_resource(
                    resources(),
                    operation,
                    label="test resource",
                    missing_result_message="test resource returned no result",
                )
        finally:
            sys.settrace(None)
        assert caught.value is primary

    def run_traced_execute() -> None:
        if primary_context == "ambient":
            try:
                raise ambient
            except RuntimeError as active_error:
                assert active_error is ambient
                execute_once()
        else:
            execute_once()

    _run_in_isolated_thread(run_traced_execute)

    assert hit
    assert exits == [primary]
    retained_owners = getattr(primary, "publication_cleanup_owners", ())
    if cleanup_mode == "raise":
        assert retained_owners == (cleanup_owner,)
        notes = (
            *getattr(primary, "__notes__", ()),
            *getattr(primary, "_codenib_cleanup_notes", ()),
        )
        assert "test resource cleanup also failed: OSError" in notes
    else:
        assert retained_owners == ()


@pytest.mark.parametrize("caller_ambient", (False, True))
def test_scoped_job_native_resource_handler_preserves_cleanup_failure(
    caller_ambient: bool,
) -> None:
    class HostileCleanupError(OSError):
        def __bool__(self) -> bool:
            raise AssertionError("cleanup exception truthiness was evaluated")

    cleanup = HostileCleanupError("scoped resource cleanup failure")
    injected = KeyboardInterrupt("native resource handler interrupted")
    cleanup_owner = object()
    BaseException.__setattr__(
        cleanup,
        "publication_cleanup_owners",
        (cleanup_owner,),
    )
    ambient = RuntimeError("caller ambient context")
    capture = job_resolver_module._capture_scoped_job_resource_exit
    handler_offset = _native_handler_offset(capture)
    hit = False
    exits = 0

    def trace(frame, event, _arg):
        nonlocal hit
        if frame.f_code is capture.__code__:
            frame.f_trace_opcodes = True
            if event == "opcode" and frame.f_lasti == handler_offset and not hit:
                hit = True
                sys.settrace(None)
                raise injected
        return trace

    @contextmanager
    def resources():
        nonlocal exits
        try:
            yield object()
        finally:
            exits += 1
            raise cleanup

    def execute_once() -> None:
        _warm_scoped_job_capture_tracing(capture)
        sys.settrace(trace)
        try:
            with pytest.raises(OSError) as caught:
                job_resolver_module._execute_scoped_job_resource(
                    resources(),
                    lambda _executor: object(),
                    label="test resource",
                    missing_result_message="test resource returned no result",
                )
        finally:
            sys.settrace(None)
        assert caught.value is cleanup

    def run_traced_execute() -> None:
        if caller_ambient:
            try:
                raise ambient
            except RuntimeError as active_error:
                assert active_error is ambient
                execute_once()
        else:
            execute_once()

    _run_in_isolated_thread(run_traced_execute)

    assert hit
    assert exits == 1
    assert getattr(cleanup, "publication_cleanup_owners", ()) == (cleanup_owner,)


def test_scoped_job_resource_recovery_entry_preserves_boundary_error() -> None:
    injected = KeyboardInterrupt("scoped resource recovery interrupted")
    recover = job_resolver_module._recover_scoped_job_resource_exit
    target_offset = next(
        instruction.offset
        for instruction in dis.get_instructions(recover)
        if instruction.argval == "_invoke_scoped_job_resource_exit"
    )
    hit = False
    resource_enters = 0
    operation_calls = 0

    def trace(frame, event, _arg):
        nonlocal hit
        if frame.f_code is recover.__code__:
            frame.f_trace_opcodes = True
            if event == "opcode" and frame.f_lasti == target_offset and not hit:
                hit = True
                sys.settrace(None)
                raise injected
        return trace

    @contextmanager
    def resources():
        nonlocal resource_enters
        resource_enters += 1
        yield object()

    def operation(_executor):
        nonlocal operation_calls
        operation_calls += 1
        return object()

    def run_traced_execute() -> None:
        warm_outcome = job_resolver_module._ScopedJobExecutionOutcome()

        def warm_trace(frame, _event, _arg):
            if frame.f_code is recover.__code__:
                frame.f_trace_opcodes = True
            return warm_trace

        sys.settrace(warm_trace)
        try:
            recover(resources(), operation, warm_outcome)
        finally:
            sys.settrace(None)
        resource_enters_before = resource_enters
        operation_calls_before = operation_calls

        sys.settrace(trace)
        try:
            with pytest.raises(KeyboardInterrupt) as caught:
                job_resolver_module._execute_scoped_job_resource(
                    resources(),
                    operation,
                    label="test resource",
                    missing_result_message="test resource returned no result",
                )
        finally:
            sys.settrace(None)
        assert caught.value is injected
        assert resource_enters == resource_enters_before
        assert operation_calls == operation_calls_before

    _run_in_isolated_thread(run_traced_execute)

    assert hit


def test_scoped_job_same_ambient_instance_remains_local_primary() -> None:
    execute = job_resolver_module._execute_scoped_job_resource
    sys_loads = tuple(
        instruction.offset
        for instruction in dis.get_instructions(execute)
        if instruction.opname == "LOAD_GLOBAL" and instruction.argval == "sys"
    )
    target_offset = sys_loads[1]
    ambient = RuntimeError("caller and local scoped failure")
    cleanup = OSError("scoped resource cleanup failure")
    cleanup_owner = object()
    BaseException.__setattr__(
        cleanup,
        "publication_cleanup_owners",
        (cleanup_owner,),
    )
    hit = False
    exits = 0
    operation_calls = 0

    def trace(frame, event, _arg):
        nonlocal hit
        if frame.f_code is execute.__code__:
            frame.f_trace_opcodes = True
            if event == "opcode" and frame.f_lasti == target_offset and not hit:
                hit = True
                sys.settrace(None)
                raise ambient
        return trace

    @contextmanager
    def resources():
        nonlocal exits
        try:
            yield object()
        finally:
            exits += 1
            raise cleanup

    def operation(_executor):
        nonlocal operation_calls
        operation_calls += 1
        return object()

    def run_traced_execute() -> None:
        def warm_trace(frame, _event, _arg):
            if frame.f_code is execute.__code__:
                frame.f_trace_opcodes = True
            return warm_trace

        warm_primary = RuntimeError("warm scoped execution")
        sys.settrace(warm_trace)
        try:
            with pytest.raises(RuntimeError) as warm_caught:
                execute(
                    resources(),
                    lambda _executor: (_ for _ in ()).throw(warm_primary),
                    label="test resource",
                    missing_result_message="test resource returned no result",
                )
        finally:
            sys.settrace(None)
        assert warm_caught.value is warm_primary
        exits_before = exits

        try:
            raise ambient
        except RuntimeError as active_error:
            assert active_error is ambient
            sys.settrace(trace)
            try:
                with pytest.raises(RuntimeError) as caught:
                    execute(
                        resources(),
                        operation,
                        label="test resource",
                        missing_result_message="test resource returned no result",
                    )
            finally:
                sys.settrace(None)
            assert caught.value is ambient
        assert exits == exits_before + 1

    _run_in_isolated_thread(run_traced_execute)

    assert hit
    assert exits == 2
    assert operation_calls == 1
    assert getattr(ambient, "publication_cleanup_owners", ()) == (cleanup_owner,)
    notes = (
        *getattr(ambient, "__notes__", ()),
        *getattr(ambient, "_codenib_cleanup_notes", ()),
    )
    assert "test resource cleanup also failed: OSError" in notes


@pytest.mark.parametrize("cleanup_mode", ("success", "raise"))
def test_scoped_job_caught_ambient_reuse_does_not_become_primary(
    monkeypatch: pytest.MonkeyPatch,
    cleanup_mode: str,
) -> None:
    class FakeResult:
        pass

    monkeypatch.setattr(job_resolver_module, "IndexJobExecutionResult", FakeResult)
    result = FakeResult()
    ambient = RuntimeError("caller ambient reused inside operation")
    cleanup = OSError("scoped resource cleanup failure")
    cleanup_owner = object()
    BaseException.__setattr__(
        cleanup,
        "publication_cleanup_owners",
        (cleanup_owner,),
    )
    exits = 0

    @contextmanager
    def resources():
        nonlocal exits
        try:
            yield object()
        finally:
            exits += 1
            if cleanup_mode == "raise":
                raise cleanup

    def operation(_executor):
        try:
            raise ambient
        except RuntimeError as caught:
            assert caught is ambient
        return result

    try:
        raise ambient
    except RuntimeError as active_error:
        assert active_error is ambient
        if cleanup_mode == "success":
            assert (
                job_resolver_module._execute_scoped_job_resource(
                    resources(),
                    operation,
                    label="test resource",
                    missing_result_message="test resource returned no result",
                )
                is result
            )
        else:
            with pytest.raises(OSError) as caught:
                job_resolver_module._execute_scoped_job_resource(
                    resources(),
                    operation,
                    label="test resource",
                    missing_result_message="test resource returned no result",
                )
            assert caught.value is cleanup

    assert exits == 1
    assert getattr(ambient, "publication_cleanup_owners", ()) == ()
    assert getattr(cleanup, "publication_cleanup_owners", ()) == (cleanup_owner,)


def test_scoped_job_ambient_classification_interruption_becomes_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResult:
        pass

    monkeypatch.setattr(job_resolver_module, "IndexJobExecutionResult", FakeResult)
    result = FakeResult()
    ambient = RuntimeError("caller ambient reused inside operation")
    injected = KeyboardInterrupt("ambient classification interrupted")
    classify = job_resolver_module._has_current_scoped_job_execution_provenance
    target_offset = next(
        instruction.offset
        for instruction in dis.get_instructions(classify)
        if instruction.argval == "__traceback__"
    )
    hit = False
    exits = 0
    operation_calls = 0

    def trace(frame, event, _arg):
        nonlocal hit
        if frame.f_code is classify.__code__:
            frame.f_trace_opcodes = True
            if event == "opcode" and frame.f_lasti == target_offset and not hit:
                hit = True
                sys.settrace(None)
                raise injected
        return trace

    @contextmanager
    def resources():
        nonlocal exits
        try:
            yield object()
        finally:
            exits += 1

    def operation(active_ambient):
        def run(_executor):
            nonlocal operation_calls
            operation_calls += 1
            try:
                raise active_ambient
            except RuntimeError as caught:
                assert caught is active_ambient
            return result

        return run

    def execute_with_ambient(active_ambient) -> FakeResult:
        try:
            raise active_ambient
        except RuntimeError as caught:
            assert caught is active_ambient
            return job_resolver_module._execute_scoped_job_resource(
                resources(),
                operation(active_ambient),
                label="test resource",
                missing_result_message="test resource returned no result",
            )

    def run_traced_execute() -> None:
        def warm_trace(frame, _event, _arg):
            if frame.f_code is classify.__code__:
                frame.f_trace_opcodes = True
            return warm_trace

        sys.settrace(warm_trace)
        try:
            assert execute_with_ambient(RuntimeError("warm ambient")) is result
        finally:
            sys.settrace(None)

        sys.settrace(trace)
        try:
            with pytest.raises(KeyboardInterrupt) as caught:
                execute_with_ambient(ambient)
        finally:
            sys.settrace(None)
        assert caught.value is injected

    _run_in_isolated_thread(run_traced_execute)

    assert hit
    assert exits == 2
    assert operation_calls == 2


def test_scoped_job_final_delivery_interruption_retains_settled_outcome() -> None:
    execute = job_resolver_module._execute_scoped_job_resource
    instructions = tuple(dis.get_instructions(execute))
    target_offset = next(
        instruction.offset
        for index, instruction in enumerate(instructions[:-1])
        if instruction.opname == "RAISE_VARARGS"
        and instructions[index + 1].opname == "LOAD_GLOBAL"
        and instructions[index + 1].argval == "type"
    )
    primary = StorageIntegrityError("primary scoped executor failure")
    cleanup = OSError("scoped resource cleanup failure")
    injected = KeyboardInterrupt("final scoped delivery interrupted")
    cleanup_owner = object()
    BaseException.__setattr__(
        cleanup,
        "publication_cleanup_owners",
        (cleanup_owner,),
    )
    hit = False
    exits = 0
    observed_outcomes: list[object] = []

    def trace(frame, event, _arg):
        nonlocal hit
        if frame.f_code is execute.__code__:
            frame.f_trace_opcodes = True
            if event == "opcode" and frame.f_lasti == target_offset and not hit:
                hit = True
                sys.settrace(None)
                raise injected
        return trace

    @contextmanager
    def resources():
        nonlocal exits
        try:
            yield object()
        finally:
            exits += 1
            raise cleanup

    def operation(_executor):
        raise primary

    def run_traced_execute() -> None:
        def warm_trace(frame, _event, _arg):
            if frame.f_code is execute.__code__:
                frame.f_trace_opcodes = True
            return warm_trace

        warm_primary = RuntimeError("warm scoped primary")

        @contextmanager
        def warm_resources():
            try:
                yield object()
            finally:
                raise OSError("warm scoped cleanup")

        sys.settrace(warm_trace)
        try:
            with pytest.raises(RuntimeError) as warm_caught:
                execute(
                    warm_resources(),
                    lambda _executor: (_ for _ in ()).throw(warm_primary),
                    label="test resource",
                    missing_result_message="test resource returned no result",
                )
        finally:
            sys.settrace(None)
        assert warm_caught.value is warm_primary

        sys.settrace(trace)
        try:
            with pytest.raises(KeyboardInterrupt) as caught:
                execute(
                    resources(),
                    operation,
                    label="test resource",
                    missing_result_message="test resource returned no result",
                )
        finally:
            sys.settrace(None)
        assert caught.value is injected
        traceback = BaseException.__traceback__.__get__(injected, type(injected))
        while traceback is not None:
            frame = traceback.tb_frame
            if frame.f_code is execute.__code__:
                observed_outcomes.append(frame.f_locals["outcome"])
                break
            traceback = traceback.tb_next

    _run_in_isolated_thread(run_traced_execute)

    assert hit
    assert exits == 1
    assert len(observed_outcomes) == 1
    outcome = observed_outcomes[0]
    assert type(outcome) is job_resolver_module._ScopedJobExecutionOutcome
    assert outcome.settlement_started
    assert outcome.settlement_complete
    assert outcome.operation_error is primary
    assert outcome.scope_error is cleanup
    assert outcome.settled_primary is primary
    assert getattr(primary, "publication_cleanup_owners", ()) == (cleanup_owner,)
    assert getattr(cleanup, "publication_cleanup_owners", ()) == (cleanup_owner,)
    notes = (
        *getattr(primary, "__notes__", ()),
        *getattr(primary, "_codenib_cleanup_notes", ()),
    )
    assert "test resource cleanup also failed: OSError" in notes


def test_scoped_job_unattachable_primary_retains_cleanup_owner_in_outcome() -> None:
    class UnattachablePrimary(StorageIntegrityError):
        @property
        def publication_cleanup_owners(self):
            return ()

        @publication_cleanup_owners.setter
        def publication_cleanup_owners(self, _value):
            raise RuntimeError("primary rejects cleanup-owner attachment")

    execute = job_resolver_module._execute_scoped_job_resource
    primary = UnattachablePrimary("primary scoped executor failure")
    cleanup = OSError("scoped resource cleanup failure")
    cleanup_owner = object()
    BaseException.__setattr__(
        cleanup,
        "publication_cleanup_owners",
        (cleanup_owner,),
    )
    exits = 0

    @contextmanager
    def resources():
        nonlocal exits
        try:
            yield object()
        finally:
            exits += 1
            raise cleanup

    with pytest.raises(UnattachablePrimary) as caught:
        execute(
            resources(),
            lambda _executor: (_ for _ in ()).throw(primary),
            label="test resource",
            missing_result_message="test resource returned no result",
        )

    assert caught.value is primary
    assert exits == 1
    observed_outcome = None
    traceback = BaseException.__traceback__.__get__(primary, type(primary))
    while traceback is not None:
        frame = traceback.tb_frame
        if frame.f_code is execute.__code__:
            observed_outcome = frame.f_locals["outcome"]
            break
        traceback = traceback.tb_next
    assert type(observed_outcome) is job_resolver_module._ScopedJobExecutionOutcome
    assert observed_outcome.settlement_complete
    assert observed_outcome.settled_primary is primary
    assert observed_outcome.scope_error is cleanup
    assert cleanup.publication_cleanup_owners == (cleanup_owner,)
    notes = (
        *getattr(primary, "__notes__", ()),
        *getattr(primary, "_codenib_cleanup_notes", ()),
    )
    assert "test resource cleanup also failed: OSError" in notes


def test_scoped_job_post_invoke_handler_keeps_captured_primary() -> None:
    invoke = job_resolver_module._invoke_scoped_job_execution
    instructions = tuple(dis.get_instructions(invoke))
    boundary_store_indices = tuple(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname == "STORE_FAST" and instruction.argval == "boundary_error"
    )
    handler_store_index = boundary_store_indices[-1]
    pop_except_index = next(
        index
        for index in range(handler_store_index + 1, len(instructions))
        if instructions[index].opname == "POP_EXCEPT"
    )
    target_offset = instructions[pop_except_index + 1].offset
    primary = StorageIntegrityError("primary scoped executor failure")
    cleanup = OSError("scoped resource cleanup failure")
    injected = KeyboardInterrupt("post-handler interruption")
    cleanup_owner = object()
    BaseException.__setattr__(
        cleanup,
        "publication_cleanup_owners",
        (cleanup_owner,),
    )
    hit = False
    exits: list[BaseException] = []

    def trace(frame, event, _arg):
        nonlocal hit
        if frame.f_code is invoke.__code__:
            frame.f_trace_opcodes = True
            if event == "opcode" and frame.f_lasti == target_offset and not hit:
                hit = True
                sys.settrace(None)
                raise injected
        return trace

    @contextmanager
    def resources():
        try:
            yield object()
        except BaseException as exiting:  # noqa: B036 - inspect exact primary
            exits.append(exiting)
            raise cleanup

    def run_traced_execute() -> None:
        def warm_trace(frame, _event, _arg):
            if frame.f_code is invoke.__code__:
                frame.f_trace_opcodes = True
            return warm_trace

        warm_primary = RuntimeError("warm invoke handler")
        warm_outcome = job_resolver_module._ScopedJobExecutionOutcome()
        sys.settrace(warm_trace)
        try:
            with pytest.raises(RuntimeError) as warm_caught:
                invoke(
                    object(),
                    lambda _executor: (_ for _ in ()).throw(warm_primary),
                    warm_outcome,
                )
        finally:
            sys.settrace(None)
        assert warm_caught.value is warm_primary

        sys.settrace(trace)
        try:
            with pytest.raises(StorageIntegrityError) as caught:
                job_resolver_module._execute_scoped_job_resource(
                    resources(),
                    lambda _executor: (_ for _ in ()).throw(primary),
                    label="test resource",
                    missing_result_message="test resource returned no result",
                )
        finally:
            sys.settrace(None)
        assert caught.value is primary

    _run_in_isolated_thread(run_traced_execute)

    assert hit
    assert exits == [primary]
    assert getattr(primary, "publication_cleanup_owners", ()) == (cleanup_owner,)
    notes = (
        *getattr(primary, "__notes__", ()),
        *getattr(primary, "_codenib_cleanup_notes", ()),
    )
    assert "test resource cleanup also failed: OSError" in notes


def test_scoped_job_recovery_interruption_keeps_captured_scope_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResult:
        pass

    monkeypatch.setattr(job_resolver_module, "IndexJobExecutionResult", FakeResult)
    result = FakeResult()
    cleanup = OSError("scoped resource cleanup failure")
    injected = KeyboardInterrupt("operation-context recovery interrupted")
    cleanup_owner = object()
    BaseException.__setattr__(
        cleanup,
        "publication_cleanup_owners",
        (cleanup_owner,),
    )
    recover_operation = job_resolver_module._scoped_job_operation_context
    target_offset = next(
        instruction.offset
        for instruction in dis.get_instructions(recover_operation)
        if instruction.opname not in {"CACHE", "COPY_FREE_VARS", "NOP", "RESUME"}
    )
    hit = False
    exits = 0
    operation_calls = 0

    def trace(frame, event, _arg):
        nonlocal hit
        if frame.f_code is recover_operation.__code__:
            frame.f_trace_opcodes = True
            if event == "opcode" and frame.f_lasti == target_offset and not hit:
                hit = True
                sys.settrace(None)
                raise injected
        return trace

    @contextmanager
    def resources():
        nonlocal exits
        try:
            yield object()
        finally:
            exits += 1
            raise cleanup

    def operation(_executor):
        nonlocal operation_calls
        operation_calls += 1
        return result

    def run_traced_execute() -> None:
        def warm_trace(frame, _event, _arg):
            if frame.f_code is recover_operation.__code__:
                frame.f_trace_opcodes = True
            return warm_trace

        sys.settrace(warm_trace)
        try:
            with pytest.raises(OSError) as warm_caught:
                job_resolver_module._execute_scoped_job_resource(
                    resources(),
                    operation,
                    label="test resource",
                    missing_result_message="test resource returned no result",
                )
        finally:
            sys.settrace(None)
        assert warm_caught.value is cleanup
        exits_before = exits
        operation_calls_before = operation_calls

        sys.settrace(trace)
        try:
            with pytest.raises(OSError) as caught:
                job_resolver_module._execute_scoped_job_resource(
                    resources(),
                    operation,
                    label="test resource",
                    missing_result_message="test resource returned no result",
                )
        finally:
            sys.settrace(None)
        assert caught.value is cleanup
        assert exits == exits_before + 1
        assert operation_calls == operation_calls_before + 1

    _run_in_isolated_thread(run_traced_execute)

    assert hit
    assert exits == 2
    assert operation_calls == 2
    assert getattr(cleanup, "publication_cleanup_owners", ()) == (cleanup_owner,)


def test_local_bm25_source_scope_rejects_physical_workspace_overlap_before_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = BM25IndexBuilder()
    fixture = _source_fixture(tmp_path)
    physical_workspace = fixture.repository / "physical-workspace"
    physical_workspace.mkdir(mode=0o700)
    workspace_alias = tmp_path / "workspace-alias"
    workspace_alias.symlink_to(fixture.repository, target_is_directory=True)
    target = LocalBM25SourceJobTarget(
        repository_root=fixture.repository,
        workspace_provider=LocalWorkspaceProvider(
            workspace_alias / physical_workspace.name
        ),
        repository_key=_REPOSITORY_KEY,
        display_commit=_COMMIT,
        builder=builder,
        environ={},
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
            resources = LocalBM25SourceJobResourceFactory((target,))
            monkeypatch.setattr(
                job_resources_module,
                "capture_repository_source",
                lambda *_args, **_kwargs: pytest.fail(
                    "source capture ran before physical topology validation"
                ),
            )

            scope = resources.create_scope(context, object_store=cas)

            with pytest.raises(
                ValueError,
                match="physical workspace and repository must not overlap",
            ):
                with scope.resources:
                    pass
            assert not tuple(physical_workspace.iterdir())
    finally:
        fixture.close()


def test_bm25_source_resolver_rejects_foreign_scope_store_before_enter(
    tmp_path: Path,
) -> None:
    builder = BM25IndexBuilder()
    fixture = _source_fixture(tmp_path)
    entered = False
    try:
        with (
            LocalCAS(tmp_path / "worker-cas") as worker_store,
            LocalCAS(tmp_path / "foreign-cas") as foreign_store,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            context = _execution_context(
                catalog,
                fixture,
                bm25_source_job_profile(builder),
            )

            @contextmanager
            def unexpected_resources():
                nonlocal entered
                entered = True
                raise AssertionError("foreign resource scope was entered")
                yield  # pragma: no cover

            class ForeignStoreFactory:
                def create_scope(self, attempt_context, *, object_store):
                    assert attempt_context is context
                    assert object_store is worker_store
                    return BM25SourceJobResourceScope(
                        object_store=foreign_store,
                        resources=unexpected_resources(),
                    )

            resolved = BM25SourceJobResolver(
                resource_factory=ForeignStoreFactory(),
                object_store=worker_store,
            ).resolve(context.job, context.views)

            with pytest.raises(StorageIntegrityError, match="resolver object store"):
                resolved.execute(context)

            assert entered is False
            assert not tuple(fixture.workspace.iterdir())
    finally:
        fixture.close()
