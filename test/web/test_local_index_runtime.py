# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

import codenib.web.local_index_runtime as local_runtime_module
from codenib.compiler.cache_lock import COMPILER_CACHE_LOCK_FILENAME
from codenib.compiler.manifest import RepoManifest
from codenib.repository_source_selection import RepositorySourceSelection
from codenib.storage import LocalCAS, SQLiteCatalog
from codenib.web.config import (
    LocalIndexRuntimeConfig,
    LocalIndexStorageConfig,
    LocalIndexStorageRepository,
    LocalIndexWorkerConfig,
    QAConfig,
    RepoEntry,
)
from codenib.web.local_index_runtime import (
    LocalIndexRuntimeService,
    open_local_index_runtime_service,
)
from codenib.web.local_index_service import LocalIndexServiceError
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
    registry = RepoRegistry(QAConfig(mode="sparse"))
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


def test_local_index_runtime_rejects_a_second_attempt_pool_owner(
    tmp_path: Path,
) -> None:
    storage, registry = _runtime_fixture(tmp_path)
    try:
        with open_local_index_runtime_service(storage, registry) as service:
            with pytest.raises(LocalIndexServiceError, match="workspace lease"):
                LocalIndexRuntimeService.acquire(storage, registry)
            assert service.healthy is True
        with open_local_index_runtime_service(storage, registry) as restarted:
            assert restarted.healthy is True
    finally:
        registry.close()


def test_attempt_pool_lease_owner_retains_interrupted_enter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = False
    events: list[str] = []

    class InterruptedContext:
        def __enter__(self):
            nonlocal active
            active = True
            events.append("enter")
            raise KeyboardInterrupt("interrupt after lease acquisition")

        def __exit__(self, *_args):
            nonlocal active
            active = False
            events.append("exit")
            return False

    def lease_factory(root: Path, *, create: bool, check_cancelled):
        assert root == tmp_path
        assert create is True
        assert callable(check_cancelled)
        return InterruptedContext()

    monkeypatch.setattr(
        local_runtime_module,
        "compiler_cache_lock",
        lease_factory,
    )
    owner = local_runtime_module._LocalAttemptPoolLeaseOwner()

    with pytest.raises(KeyboardInterrupt, match="after lease acquisition"):
        owner.acquire(
            tmp_path,
            wait_timeout_ms=0,
            topology_verifier=lambda: events.append("verify"),
        )

    assert active is True
    assert owner.closed is False
    owner.close()
    assert active is False
    assert owner.closed is True
    assert events == ["verify", "enter", "exit"]


def test_local_index_runtime_reclaims_stale_attempts_before_start(
    tmp_path: Path,
) -> None:
    storage, registry = _runtime_fixture(tmp_path)
    stale = storage.worker_workspace_root / (
        f".codenib-source-job-{'a' * 32}.normalize-{'b' * 24}"
    )
    stale.mkdir()
    (stale / "payload").write_text("stale", encoding="utf-8")
    try:
        with open_local_index_runtime_service(storage, registry):
            assert not stale.exists()
    finally:
        registry.close()


def test_local_index_runtime_retains_lease_when_reaper_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage, registry = _runtime_fixture(tmp_path)
    service = LocalIndexRuntimeService.acquire(storage, registry)
    service.start()
    reaper_type = type(service._attempt_pool)
    real_close = reaper_type.close
    attempts = 0

    def fail_once(reaper) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("reaper cleanup failed")
        real_close(reaper)

    monkeypatch.setattr(reaper_type, "close", fail_once)
    try:
        with pytest.raises(RuntimeError, match="reaper cleanup failed"):
            service.close()

        assert service.state == "closed"
        assert service._attempt_pool.closed is False
        assert service._attempt_pool_lease_owner.closed is False
        assert service._object_store_owner.closed is True
        assert service._topology.closed is False

        service.close()
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
        assert service._attempt_pool_lease_owner.closed is False
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
    try:
        with open_local_index_runtime_service(storage, registry) as service:
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
            while tuple(storage.worker_workspace_root.glob(".*codenib-source-job-*")):
                if time.monotonic() >= deadline:
                    pytest.fail("background BM25 attempt receipt was not reclaimed")
                time.sleep(0.02)
            assert service.healthy is True
    finally:
        registry.close()

    workspace_entries = tuple(storage.worker_workspace_root.iterdir())
    assert [entry.name for entry in workspace_entries] == [COMPILER_CACHE_LOCK_FILENAME]
    assert not any(
        "attempt-root orphan" in record.getMessage() for record in caplog.records
    )
