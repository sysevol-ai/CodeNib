# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import codenib.web.app as web_app
import codenib.web.local_index_runtime as local_runtime_module
from codenib.compiler.manifest import IndexEntry, RepoManifest
from codenib.repository_source_selection import RepositorySourceSelection
from codenib.source_fingerprint import fingerprint_repository
from codenib.storage import (
    INDEX_JOB_REQUEST_CONTRACT,
    CatalogError,
    IndexJobRecord,
    IndexJobRequest,
    IndexJobStatus,
    LocalCAS,
    SQLiteCatalog,
    StorageIntegrityError,
    StorageNotFound,
)
from codenib.web.config import (
    LocalIndexRuntimeConfig,
    LocalIndexStorageConfig,
    LocalIndexStorageRepository,
    LocalIndexWorkerConfig,
    QAConfig,
    RepoEntry,
    save_registry,
)
from codenib.web.local_index_runtime import (
    LocalIndexRuntimeService,
    open_local_index_runtime_service,
)
from codenib.web.local_index_service import (
    LocalIndexServiceError,
    LocalIndexStorageTopology,
)
from codenib.web.repo_registry import RepoBundle, RepoRegistry


def _git(*arguments: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "CodeNib Tests",
            "GIT_AUTHOR_EMAIL": "tests@codenib.invalid",
            "GIT_COMMITTER_NAME": "CodeNib Tests",
            "GIT_COMMITTER_EMAIL": "tests@codenib.invalid",
        },
    )
    return result.stdout.strip()


def _repository(root: Path) -> tuple[Path, str]:
    repository = root / "repository"
    repository.mkdir()
    _git("init", "--quiet", cwd=repository)
    (repository / "sample.py").write_text(
        "def sample():\n    return 1\n",
        encoding="utf-8",
    )
    _git("add", "sample.py", cwd=repository)
    _git("commit", "--quiet", "-m", "initial", cwd=repository)
    return repository, _git("rev-parse", "HEAD", cwd=repository)


def _runtime_fixture(
    tmp_path: Path,
    *,
    registry_mode: str = "sparse",
) -> tuple[LocalIndexStorageConfig, RepoRegistry]:
    repository, commit = _repository(tmp_path)
    catalog_path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(catalog_path) as catalog:
        repository_id = catalog.create_repository("org/repo")
    cas_root = tmp_path / "cas"
    LocalCAS.provision(cas_root).close()
    worker_root = tmp_path / "worker"
    runtime_root = tmp_path / "runtime"
    worker_root.mkdir(mode=0o700)
    runtime_root.mkdir(mode=0o700)

    storage_binding = LocalIndexStorageRepository(
        repo_id="demo",
        repository_key="org/repo",
    )
    assert storage_binding.repository_id == repository_id
    storage = LocalIndexStorageConfig(
        catalog_path=catalog_path,
        cas_root=cas_root,
        worker_workspace_root=worker_root,
        runtime_workspace_root=runtime_root,
        repositories=(storage_binding,),
        worker=LocalIndexWorkerConfig(
            lease_duration_ms=3_000,
            heartbeat_interval_ms=500,
            scan_limit=4,
            initial_idle_delay_ms=10,
            max_idle_delay_ms=20,
            max_attempts=2,
        ),
        runtime=LocalIndexRuntimeConfig(poll_interval_ms=10),
    )
    registry = RepoRegistry(
        QAConfig(mode=registry_mode),
        allow_missing_native_index_authorization=registry_mode == "hybrid",
    )
    registry._bundles["demo"] = RepoBundle(
        entry=RepoEntry(
            instance_id="demo",
            repo="org/repo",
            base_commit=commit,
            language="python",
            repo_dir=str(repository),
            manifest_path=str(tmp_path / "repo_manifest.json"),
        ),
        manifest=RepoManifest(
            repo_path=str(repository),
            commit=commit,
            last_indexed_commit=commit,
            source_selection=RepositorySourceSelection(("generated",)),
            languages=["python"],
        ),
    )
    return storage, registry


def _add_second_runtime_repository(
    tmp_path: Path,
    storage: LocalIndexStorageConfig,
    registry: RepoRegistry,
) -> tuple[LocalIndexStorageConfig, dict[str, Path]]:
    parent = tmp_path / "other-root"
    parent.mkdir()
    repository, commit = _repository(parent)
    with SQLiteCatalog(storage.catalog_path, create=False) as catalog:
        repository_id = catalog.create_repository("org/other")
    binding = LocalIndexStorageRepository(
        repo_id="other",
        repository_key="org/other",
    )
    assert binding.repository_id == repository_id
    registry._bundles["other"] = RepoBundle(
        entry=RepoEntry(
            instance_id="other",
            repo="org/other",
            base_commit=commit,
            language="python",
            repo_dir=str(repository),
            manifest_path=str(parent / "repo_manifest.json"),
        ),
        manifest=RepoManifest(
            repo_path=str(repository),
            commit=commit,
            last_indexed_commit=commit,
            languages=["python"],
        ),
    )
    return (
        replace(storage, repositories=(*storage.repositories, binding)),
        {
            "demo": Path(registry._bundles["demo"].entry.repo_dir),
            "other": repository,
        },
    )


def _candidate(repository_id: str, *, idempotency_key: str) -> IndexJobRecord:
    request = IndexJobRequest.create(
        repository_id,
        "src_" + "a" * 64,
        idempotency_key,
        {
            "contract": INDEX_JOB_REQUEST_CONTRACT,
            "views": {
                "bm25": {
                    "profile_id": "profile_" + "b" * 64,
                    "requested_mode": "full",
                    "required": True,
                }
            },
        },
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


def test_local_index_runtime_composes_and_settles_production_loops(
    tmp_path: Path,
) -> None:
    storage, registry = _runtime_fixture(tmp_path)
    service = None
    try:
        with open_local_index_runtime_service(storage, registry) as opened:
            service = opened
            assert service.state == "running"
            assert service.healthy is True
            assert service.reader.active("demo") is None
            capabilities = service.capabilities("demo")
            assert capabilities["bm25"].mode == "rebuild"
            assert capabilities["bm25"].enabled is True
            assert "vector" not in capabilities
            with pytest.raises(KeyError):
                service.capabilities("other")
    finally:
        registry.close()

    assert service is not None
    assert service.state == "closed"
    assert service.closed is True


def test_local_index_runtime_rejects_loaded_repository_identity_mismatch(
    tmp_path: Path,
) -> None:
    storage, registry = _runtime_fixture(tmp_path)
    bundle = registry._bundles["demo"]
    registry._bundles["demo"] = RepoBundle(
        entry=replace(bundle.entry, repo="other/repo"),
        manifest=bundle.manifest,
    )
    try:
        with pytest.raises(LocalIndexServiceError, match="identity differs"):
            LocalIndexRuntimeService.acquire(storage, registry)
    finally:
        registry.close()


@pytest.mark.parametrize(
    "drift",
    (
        "repository-key",
        "repository-root",
        "manifest-root",
        "languages",
        "source-selection",
        "multi-view",
    ),
)
def test_local_index_runtime_rejects_reloaded_build_input_drift(
    tmp_path: Path,
    drift: str,
) -> None:
    storage, registry = _runtime_fixture(tmp_path)
    service = LocalIndexRuntimeService.acquire(storage, registry)
    original = registry._bundles["demo"]
    entry = original.entry
    manifest = original.manifest
    if drift == "repository-key":
        entry = replace(entry, repo="org/other")
    elif drift == "repository-root":
        replacement_root = tmp_path / "replacement-repository"
        replacement_root.mkdir()
        entry = replace(entry, repo_dir=str(replacement_root))
    elif drift == "manifest-root":
        replacement_root = tmp_path / "replacement-manifest-repository"
        replacement_root.mkdir()
        manifest = replace(manifest, repo_path=str(replacement_root))
    elif drift == "languages":
        manifest = replace(manifest, languages=["javascript"])
    elif drift == "source-selection":
        manifest = replace(
            manifest,
            source_selection=RepositorySourceSelection(
                (*manifest.source_selection.exclude_subtrees, "policy-change")
            ),
        )
    else:
        manifest = replace(
            manifest,
            indexes={"bm25": object(), "vector": object()},
        )
    replacement = RepoBundle(entry=entry, manifest=manifest)
    equivalent = RepoBundle(
        entry=replace(original.entry),
        manifest=replace(
            original.manifest,
            languages=list(original.manifest.languages),
            source_selection=RepositorySourceSelection(
                original.manifest.source_selection.exclude_subtrees
            ),
        ),
    )

    try:
        assert service.accepts_repository("demo", original) is True
        assert service.accepts_repository("demo", equivalent) is True
        assert service.accepts_repository("demo", replacement) is False
        assert service.accepts_repository("other", original) is False
    finally:
        service.close()
        registry.close()


def test_local_index_runtime_rejects_hybrid_registry_before_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage, registry = _runtime_fixture(tmp_path, registry_mode="hybrid")
    monkeypatch.setattr(
        LocalIndexStorageTopology,
        "acquire",
        lambda *_args, **_kwargs: pytest.fail(
            "hybrid mode must be rejected before topology acquisition"
        ),
    )
    try:
        with pytest.raises(LocalIndexServiceError, match="sparse Web mode"):
            LocalIndexRuntimeService.acquire(storage, registry)
    finally:
        registry.close()


def test_local_index_runtime_attests_catalog_before_attempt_pool_bootstrap(
    tmp_path: Path,
) -> None:
    storage, registry = _runtime_fixture(tmp_path)
    storage.catalog_path.unlink()
    with SQLiteCatalog(storage.catalog_path):
        pass
    try:
        with pytest.raises(StorageNotFound, match="not found"):
            LocalIndexRuntimeService.acquire(storage, registry)
        assert tuple(storage.worker_workspace_root.iterdir()) == ()
    finally:
        registry.close()


def test_local_index_runtime_rejects_invalid_catalog_before_attempt_pool_bootstrap(
    tmp_path: Path,
) -> None:
    storage, registry = _runtime_fixture(tmp_path)
    storage.catalog_path.unlink()
    storage.catalog_path.write_bytes(b"not a SQLite catalog")
    try:
        with pytest.raises(CatalogError, match="existing SQLite catalog"):
            LocalIndexRuntimeService.acquire(storage, registry)
        assert tuple(storage.worker_workspace_root.iterdir()) == ()
    finally:
        registry.close()


def test_local_index_runtime_filters_before_scoped_topology_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage, registry = _runtime_fixture(tmp_path)
    service = LocalIndexRuntimeService.acquire(storage, registry)
    expected_repository_id = storage.repositories[0].repository_id
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        local_runtime_module.LocalBM25SourceJobResourceFactory,
        "accepts_candidate",
        lambda _resources, candidate: (
            candidate.repository_id == expected_repository_id
        ),
    )
    monkeypatch.setattr(
        LocalIndexStorageTopology,
        "verify",
        lambda _topology: calls.append(("global", None)),
    )
    monkeypatch.setattr(
        LocalIndexStorageTopology,
        "verify_repository",
        lambda _topology, repo_id: calls.append(("repository", repo_id)),
    )
    candidate_filter = service._background._worker._worker._candidate_filter
    assert candidate_filter is not None
    try:
        assert (
            candidate_filter(
                _candidate("repo_unconfigured", idempotency_key="unconfigured")
            )
            is False
        )
        assert calls == []
        assert (
            candidate_filter(
                _candidate(expected_repository_id, idempotency_key="configured")
            )
            is True
        )
        assert calls == [("repository", "demo")]
        target = service._background._worker._worker._resolver.resource_factory.targets[
            0
        ]
        assert target.topology_verifier is not None
        target.topology_verifier()
        assert calls == [
            ("repository", "demo"),
            ("repository", "demo"),
        ]
    finally:
        service.close()
        registry.close()


def test_local_index_runtime_scopes_attempt_routes_and_runtime_providers(
    tmp_path: Path,
) -> None:
    storage, registry = _runtime_fixture(tmp_path)
    storage, repositories = _add_second_runtime_repository(
        tmp_path,
        storage,
        registry,
    )
    service = LocalIndexRuntimeService.acquire(storage, registry)
    service.start()
    repository_ids = {
        binding.repo_id: binding.repository_id for binding in storage.repositories
    }
    registrations = service._attempt_pool._by_repository
    assert set(registrations) == set(repository_ids.values())
    assert (
        registrations[repository_ids["demo"]].target.attempt_pool_root
        != registrations[repository_ids["other"]].target.attempt_pool_root
    )
    displaced = tmp_path / "demo-displaced"
    repositories["demo"].rename(displaced)
    try:
        loader = service._background._runtime._reconciler._publisher._loader
        targets = {
            target.binding.repo_id: target for target in loader._by_storage.values()
        }
        assert set(targets) == {"demo", "other"}
        assert (
            targets["demo"].workspace_provider
            is not targets["other"].workspace_provider
        )
        targets["other"].workspace_provider.require_support()
        with pytest.raises(
            StorageIntegrityError,
            match="repository topology changed",
        ):
            targets["demo"].workspace_provider.require_support()

        created = service.writer.create(
            "other",
            indexes=("bm25",),
            mode="full",
            force=False,
            idempotency_key="healthy-route-after-foreign-drift",
        )
        deadline = time.monotonic() + 15
        observed = created
        while observed.status in {"queued", "running"}:
            if time.monotonic() >= deadline:
                pytest.fail("healthy repository job did not settle after foreign drift")
            time.sleep(0.02)
            observed = service.reader.get(created.job_id)
        assert observed.status == "succeeded"

        healthy_registration = registrations[repository_ids["other"]]
        while tuple(
            healthy_registration.target.attempt_pool_root.glob(".*codenib-source-job-*")
        ):
            if time.monotonic() >= deadline:
                pytest.fail("healthy repository receipt was not routed to its shard")
            time.sleep(0.02)
        service._attempt_pool.reclaim_stale(repository_ids["other"])
    finally:
        displaced.rename(repositories["demo"])
        service.close()
        registry.close()


def test_local_index_runtime_preserves_context_failure_after_cleanup(
    tmp_path: Path,
) -> None:
    storage, registry = _runtime_fixture(tmp_path)
    failure = RuntimeError("requesting shutdown")
    service = None
    try:
        with pytest.raises(RuntimeError) as raised:
            with open_local_index_runtime_service(storage, registry) as opened:
                service = opened
                raise failure
        assert raised.value is failure
    finally:
        registry.close()

    assert service is not None
    assert service.closed is True


def test_local_index_runtime_allows_cooperating_workers_on_the_same_shard(
    tmp_path: Path,
) -> None:
    storage, registry = _runtime_fixture(tmp_path)
    try:
        with open_local_index_runtime_service(storage, registry) as first:
            with open_local_index_runtime_service(storage, registry) as second:
                assert first.healthy is True
                assert second.healthy is True
                first_target = next(
                    iter(first._attempt_pool._by_repository.values())
                ).target
                second_target = next(
                    iter(second._attempt_pool._by_repository.values())
                ).target
                assert first_target.attempt_pool_root == second_target.attempt_pool_root
        with open_local_index_runtime_service(storage, registry) as restarted:
            assert restarted.healthy is True
    finally:
        registry.close()


def test_local_index_runtime_reclaims_stale_attempts_before_start(
    tmp_path: Path,
) -> None:
    storage, registry = _runtime_fixture(tmp_path)
    shard = storage.worker_workspace_root / (
        ".codenib-bm25-attempt-pool-v1-" + storage.repositories[0].repository_id
    )
    shard.mkdir(mode=0o700)
    stale = shard / (f".codenib-source-job-{'a' * 32}.normalize-{'b' * 24}")
    stale.mkdir()
    (stale / "payload").write_text("stale", encoding="utf-8")
    try:
        with open_local_index_runtime_service(storage, registry):
            assert not stale.exists()
    finally:
        registry.close()


def test_local_index_runtime_defers_busy_attempt_sweeps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage, registry = _runtime_fixture(tmp_path)
    calls: list[bool] = []

    def defer_busy(
        _coordinator,
        *,
        caller_asserts_quiescence=False,
        blocking=True,
    ):
        assert caller_asserts_quiescence is True
        calls.append(blocking)
        return None

    monkeypatch.setattr(
        local_runtime_module.LocalBM25AttemptPoolCoordinator,
        "reclaim",
        defer_busy,
    )
    service = LocalIndexRuntimeService.acquire(storage, registry)
    try:
        service.start()
        assert service.healthy is True
        service.close()
        assert service.closed is True
        assert calls == [False, False]
    finally:
        if not service.closed:
            service.close()
        registry.close()


def test_local_index_runtime_retains_routed_reaper_cleanup_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage, registry = _runtime_fixture(tmp_path)
    service = LocalIndexRuntimeService.acquire(storage, registry)
    service.start()
    real_reclaim = local_runtime_module.LocalBM25AttemptPoolCoordinator.reclaim
    attempts = 0

    class RetryOwner:
        closed = False

        def close(self) -> None:
            self.closed = True

    cleanup_owner = RetryOwner()
    failure = RuntimeError("routed reaper cleanup failed")
    failure.publication_cleanup_owners = (cleanup_owner,)  # type: ignore[attr-defined]

    def fail_once(
        coordinator,
        *,
        caller_asserts_quiescence=False,
        blocking=True,
    ):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise failure
        return real_reclaim(
            coordinator,
            caller_asserts_quiescence=caller_asserts_quiescence,
            blocking=blocking,
        )

    monkeypatch.setattr(
        local_runtime_module.LocalBM25AttemptPoolCoordinator,
        "reclaim",
        fail_once,
    )
    try:
        with pytest.raises(RuntimeError, match="routed reaper cleanup failed"):
            service.close()

        assert service.state == "closed"
        assert service._attempt_pool.closed is False
        assert service._object_store_owner.closed is True
        assert service._topology.closed is False
        registration = next(iter(service._attempt_pool._by_repository.values()))
        assert registration.cleanup_owners == [cleanup_owner]
        assert cleanup_owner.closed is False

        service.close()
        assert cleanup_owner.closed is True
        assert registration.cleanup_owners == []
        assert service.closed is True
    finally:
        if not service.closed:
            service.close()
        registry.close()


def test_local_index_runtime_retains_resources_when_background_join_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage, registry = _runtime_fixture(tmp_path)
    service = LocalIndexRuntimeService.acquire(storage, registry)
    service.start()
    owner_type = type(service._background_owner)
    real_close = owner_type.close
    attempts = 0

    def fail_background_once(owner) -> None:
        nonlocal attempts
        if owner is service._background_owner:
            attempts += 1
            if attempts == 1:
                raise RuntimeError("background join failed")
        real_close(owner)

    monkeypatch.setattr(owner_type, "close", fail_background_once)
    try:
        with pytest.raises(RuntimeError, match="background join failed"):
            service.close()

        assert service.state == "running"
        assert service._attempt_pool.closed is False
        assert service._object_store_owner.closed is False
        assert service._topology.closed is False

        service.close()
        assert service.closed is True
    finally:
        if not service.closed:
            service.close()
        registry.close()


def test_local_index_runtime_executes_submitted_bm25_job(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage, registry = _runtime_fixture(tmp_path)
    attempt_pool_root: Path | None = None
    try:
        with open_local_index_runtime_service(storage, registry) as service:
            attempt_pool_root = next(
                iter(service._attempt_pool._by_repository.values())
            ).target.attempt_pool_root
            created = service.writer.create(
                "demo",
                indexes=("bm25",),
                mode="full",
                force=False,
                idempotency_key="integration-job",
            )
            deadline = time.monotonic() + 15
            observed = created
            while observed.status in {"queued", "running"}:
                if time.monotonic() >= deadline:
                    pytest.fail("background BM25 job did not settle")
                time.sleep(0.02)
                observed = service.reader.get(created.job_id)

            assert observed.status == "succeeded"
            assert observed.indexes[0].index_type == "bm25"
            assert observed.result_snapshot_id is not None
            assert observed.finished_at_ms is not None
            assert any(
                event.kind == "view_result"
                and event.index_type == "bm25"
                and event.outcome == "succeeded"
                for event in observed.events
            )
            while tuple(attempt_pool_root.glob(".*codenib-source-job-*")):
                if time.monotonic() >= deadline:
                    pytest.fail("background BM25 attempt receipt was not reclaimed")
                time.sleep(0.02)
            assert service.healthy is True
    finally:
        registry.close()

    assert attempt_pool_root is not None
    workspace_entries = tuple(storage.worker_workspace_root.iterdir())
    assert workspace_entries == (attempt_pool_root,)
    assert tuple(attempt_pool_root.iterdir()) == ()
    assert not any(
        "attempt-root orphan" in record.getMessage() for record in caplog.records
    )


@pytest.mark.parametrize(
    ("reload_drift", "incumbent_status"),
    (
        (None, None),
        (None, "stale"),
        (None, "failed"),
        ("source-selection", None),
        ("languages", None),
    ),
    ids=(
        "matching-generation",
        "stale-bm25",
        "failed-bm25",
        "reloaded-policy",
        "reloaded-languages",
    ),
)
def test_web_lifespan_executes_job_and_guards_registry_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reload_drift: str | None,
    incumbent_status: str | None,
) -> None:
    activation_failures = []
    monkeypatch.setattr(
        local_runtime_module,
        "_safe_runtime_failure",
        activation_failures.append,
    )
    storage, seed_registry = _runtime_fixture(tmp_path)
    seed_bundle = seed_registry.get("demo")
    assert seed_bundle is not None
    repository = Path(seed_bundle.entry.repo_dir)
    commit = seed_bundle.entry.base_commit
    selection = seed_bundle.manifest.source_selection
    seed_registry.close()

    source = fingerprint_repository(repository, selection=selection)
    manifest_path = tmp_path / "artifacts" / "repo_manifest.json"
    initial_manifest = RepoManifest(
        repo_path=str(repository),
        commit=commit,
        last_indexed_commit=commit,
        source_fingerprint=source.value,
        source_selection=selection,
        languages=["python"],
        file_count=source.file_count,
    )
    if incumbent_status is not None:
        initial_manifest.indexes["bm25"] = IndexEntry(
            index_type="bm25",
            path=str(tmp_path / "stale-bm25"),
            built_at="2026-08-30T00:00:00Z",
            built_at_epoch=0.0,
            status=incumbent_status,
        )
        assert initial_manifest.index_is_current("bm25") is False
    initial_manifest.save(manifest_path)
    config = QAConfig(
        mode="sparse",
        data_dir=str(tmp_path / "web-data"),
        wiki_agent=False,
        index_storage=storage,
    )
    save_registry(
        config.registry_path,
        [
            RepoEntry(
                instance_id="demo",
                repo="org/repo",
                base_commit=commit,
                language="python",
                repo_dir=str(repository),
                manifest_path=str(manifest_path),
            )
        ],
    )
    monkeypatch.setattr(web_app, "load_config", lambda: config)
    monkeypatch.setattr(
        web_app,
        "_wiki_narrator",
        lambda _config: SimpleNamespace(model="model", enabled=False, cache_dir=None),
    )
    application = SimpleNamespace(state=SimpleNamespace())
    captured = {}

    async def run_lifespan() -> None:
        async with web_app.lifespan(application):
            registry = application.state.registry
            service = application.state.index_runtime_service
            captured["registry"] = registry
            captured["service"] = service
            initial = registry.get("demo")
            assert initial is not None
            assert initial.index_job_activation is None
            expected_active = initial
            if reload_drift is not None:
                reloaded_selection = (
                    RepositorySourceSelection(
                        (*selection.exclude_subtrees, "policy-change")
                    )
                    if reload_drift == "source-selection"
                    else selection
                )
                reloaded_source = fingerprint_repository(
                    repository,
                    selection=reloaded_selection,
                )
                RepoManifest(
                    repo_path=str(repository),
                    commit=commit,
                    last_indexed_commit=commit,
                    source_fingerprint=reloaded_source.value,
                    source_selection=reloaded_selection,
                    languages=(
                        ["javascript"] if reload_drift == "languages" else ["python"]
                    ),
                    file_count=reloaded_source.file_count,
                ).save(manifest_path)
                registry.load_all()
                expected_active = registry.get("demo")
                assert expected_active is not None
                assert expected_active is not initial
                assert expected_active.index_job_activation is None

            writer = application.state.index_job_writer
            if reload_drift is not None:
                assert (
                    application.state.index_update_capabilities_resolver("demo") is None
                )
                with pytest.raises(
                    web_app.IndexJobWriteError,
                    match="no longer matches",
                ):
                    writer.create(
                        "demo",
                        indexes=("bm25",),
                        mode="full",
                        force=False,
                        idempotency_key="reloaded-runtime-binding",
                    )
                # Model work durably accepted immediately before the reload.
                # The publication guard must still reject that old-input result.
                writer = service.writer
            created = writer.create(
                "demo",
                indexes=("bm25",),
                mode="full",
                force=False,
                idempotency_key="lifespan-integration-job",
            )
            deadline = time.monotonic() + 15
            observed = created
            active = initial
            while True:
                observed = application.state.index_job_reader.get(created.job_id)
                active = registry.get("demo")
                activation = None if active is None else active.index_job_activation
                if reload_drift is not None:
                    if activation is not None:
                        pytest.fail(
                            "drifted runtime inputs reached registry publication"
                        )
                    if observed.status == "succeeded" and activation_failures:
                        break
                elif (
                    observed.status == "succeeded"
                    and activation is not None
                    and activation.snapshot_id == observed.result_snapshot_id
                ):
                    break
                if observed.status == "failed":
                    pytest.fail("lifespan BM25 job failed")
                if time.monotonic() >= deadline:
                    if activation_failures:
                        chain = []
                        failure = activation_failures[-1]
                        while failure is not None:
                            chain.append(f"{type(failure).__name__}: {failure}")
                            failure = failure.__cause__
                        pytest.fail(" <- ".join(chain))
                    pytest.fail("lifespan runtime did not publish the BM25 result")
                await asyncio.sleep(0.02)

            assert active is not None
            if reload_drift is not None:
                assert active is expected_active
                assert active.index_job_activation is None
                if reload_drift == "source-selection":
                    assert active.manifest.source_selection != selection
                    expected_failure = "source selection differs"
                else:
                    assert active.manifest.languages == ["javascript"]
                    expected_failure = "languages differ"
                assert activation_failures
                failure_chain = []
                failure = activation_failures[-1]
                while failure is not None:
                    failure_chain.append(str(failure))
                    failure = failure.__cause__
                assert any(expected_failure in message for message in failure_chain)
            else:
                assert active is not initial
                assert active.bm25 is not None
                assert active.index_job_activation.job_id == created.job_id
            assert service.healthy is True

    asyncio.run(run_lifespan())

    assert captured["service"].closed is True
    for name in (
        "index_runtime_service",
        "index_job_reader",
        "index_job_writer",
        "index_update_capabilities_resolver",
    ):
        assert not hasattr(application.state, name)
