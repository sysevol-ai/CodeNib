# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path, PurePosixPath

import pytest

import codenib._atomic_directory as atomic_directory_module
import codenib.compiler._directory_lease as directory_lease_module
import codenib.compiler.bm25_attempt_pool as bm25_attempt_pool_module
import codenib.compiler.job_resources as job_resources_module
from codenib import LocalWorkspaceProvider
from codenib._atomic_directory import (
    QuiescentDirectoryReclaimer,
    publication_parent_identity,
)
from codenib.compiler._directory_lease import (
    DirectoryLeaseMode,
    PrivateDirectoryLeaseOwner,
    PrivateDirectoryLeaseRoute,
    acquire_private_directory_lease,
)
from codenib.compiler.bm25_attempt_pool import bootstrap_local_bm25_attempt_pool
from codenib.compiler.index_builders import BM25IndexBuilder
from codenib.compiler.job_resources import (
    BM25AttemptPoolReclamation,
    LocalBM25AttemptPoolCoordinator,
    LocalBM25SourceJobTarget,
)
from codenib.source_fingerprint import pin_repository_source_root
from codenib.storage.models import (
    NamespaceIdentity,
    RepositoryIdentity,
    StorageIntegrityError,
    StorageValidationError,
)


def _directory_identity(path: Path) -> tuple[int, ...]:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return publication_parent_identity(descriptor)
    finally:
        os.close(descriptor)


def _repository_id() -> str:
    namespace = NamespaceIdentity("default")
    return RepositoryIdentity(
        namespace_id=namespace.namespace_id,
        repository_key="owner/repository",
    ).repository_id


def _open_descriptors_for_identity(identity: tuple[int, ...]) -> tuple[int, ...]:
    descriptors: list[int] = []
    for raw_descriptor in os.listdir("/proc/self/fd"):
        try:
            descriptor = int(raw_descriptor)
            metadata = os.fstat(descriptor)
        except (OSError, ValueError):
            continue
        observed = (
            metadata.st_dev,
            metadata.st_ino,
            stat.S_IFMT(metadata.st_mode),
            getattr(metadata, "st_file_attributes", 0),
        )
        if observed == identity:
            descriptors.append(descriptor)
    return tuple(sorted(descriptors))


def _probe_raw_lease(route, mode: DirectoryLeaseMode) -> BaseException | None:
    outcomes: list[BaseException | None] = []

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
            outcomes.append(error)
            return
        try:
            outcomes.append(None)
        finally:
            owner.close()

    thread = threading.Thread(target=acquire)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert len(outcomes) == 1
    return outcomes[0]


def test_bm25_attempt_pool_bootstrap_creates_stable_private_shard(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    workspace_identity = _directory_identity(workspace)
    topology_checks: list[bool] = []

    def verify_topology() -> None:
        assert _directory_identity(workspace) == workspace_identity
        topology_checks.append(True)

    with pin_repository_source_root(repository) as repository_authority:
        first = bootstrap_local_bm25_attempt_pool(
            workspace_root=workspace,
            workspace_identity=workspace_identity,
            repository_id=_repository_id(),
            repository_authority=repository_authority,
            topology_verifier=verify_topology,
        )
        second = bootstrap_local_bm25_attempt_pool(
            workspace_root=workspace,
            workspace_identity=workspace_identity,
            repository_id=_repository_id(),
            repository_authority=repository_authority,
            topology_verifier=verify_topology,
        )

        shard = workspace / f".codenib-bm25-attempt-pool-v1-{_repository_id()}"
        assert first.writer_route._shard_path == shard
        assert second.writer_route._shard_path == shard
        assert first.writer_route._shard_identity == second.writer_route._shard_identity
        assert stat.S_IMODE(shard.stat().st_mode) == 0o700
        assert tuple(workspace.iterdir()) == (shard,)
        assert topology_checks


def test_bm25_attempt_pool_bootstrap_rejects_malformed_existing_shard(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    workspace_identity = _directory_identity(workspace)
    shard = workspace / f".codenib-bm25-attempt-pool-v1-{_repository_id()}"
    shard.write_text("foreign", encoding="utf-8")

    with pin_repository_source_root(repository) as repository_authority:
        with pytest.raises(PermissionError, match="owner-controlled 0700 directory"):
            bootstrap_local_bm25_attempt_pool(
                workspace_root=workspace,
                workspace_identity=workspace_identity,
                repository_id=_repository_id(),
                repository_authority=repository_authority,
                topology_verifier=lambda: None,
            )

    assert shard.read_text(encoding="utf-8") == "foreign"


def test_bm25_attempt_pool_bootstrap_rejects_parent_identity_before_creation(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    wrong_identity = list(_directory_identity(workspace))
    wrong_identity[1] += 1

    with pin_repository_source_root(repository) as repository_authority:
        with pytest.raises(StorageIntegrityError, match="workspace identity changed"):
            bootstrap_local_bm25_attempt_pool(
                workspace_root=workspace,
                workspace_identity=tuple(wrong_identity),
                repository_id=_repository_id(),
                repository_authority=repository_authority,
                topology_verifier=lambda: None,
            )

    assert not tuple(workspace.iterdir())


def test_bm25_attempt_pool_bootstrap_rejects_repository_as_workspace(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    repository_identity = _directory_identity(repository)

    with pin_repository_source_root(repository) as repository_authority:
        with pytest.raises(
            StorageIntegrityError,
            match="physically aliases the repository",
        ):
            bootstrap_local_bm25_attempt_pool(
                workspace_root=repository,
                workspace_identity=repository_identity,
                repository_id=_repository_id(),
                repository_authority=repository_authority,
                topology_verifier=lambda: None,
            )

    assert tuple(repository.iterdir()) == ()


def test_bm25_attempt_pool_bootstrap_rejects_mapped_repository_subtree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    workspace_identity = _directory_identity(workspace)
    device = (os.major(workspace_identity[0]), os.minor(workspace_identity[0]))
    mappings = (
        bm25_attempt_pool_module._BM25LinuxMountMapping(
            mount_id=1,
            device=device,
            root=PurePosixPath("/physical/repository"),
            mount_point=repository,
        ),
        bm25_attempt_pool_module._BM25LinuxMountMapping(
            mount_id=2,
            device=device,
            root=PurePosixPath("/physical/repository/subtree"),
            mount_point=workspace,
        ),
    )
    monkeypatch.setattr(
        bm25_attempt_pool_module,
        "_bm25_linux_mount_mappings",
        lambda: mappings,
    )

    with pin_repository_source_root(repository) as repository_authority:
        with pytest.raises(
            StorageIntegrityError,
            match="must share one mount",
        ):
            bootstrap_local_bm25_attempt_pool(
                workspace_root=workspace,
                workspace_identity=workspace_identity,
                repository_id=_repository_id(),
                repository_authority=repository_authority,
                topology_verifier=lambda: None,
            )

    assert tuple(workspace.iterdir()) == ()


def test_bm25_attempt_pool_bootstrap_rejects_separate_mount_authorities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    repository_identity = _directory_identity(repository)
    workspace_identity = _directory_identity(workspace)
    mappings = (
        bm25_attempt_pool_module._BM25LinuxMountMapping(
            mount_id=1,
            device=(
                os.major(repository_identity[0]),
                os.minor(repository_identity[0]),
            ),
            root=PurePosixPath("/physical/repository"),
            mount_point=repository,
        ),
        bm25_attempt_pool_module._BM25LinuxMountMapping(
            mount_id=2,
            device=(
                os.major(workspace_identity[0]),
                os.minor(workspace_identity[0]),
            ),
            root=PurePosixPath("/physical/workspace"),
            mount_point=workspace,
        ),
    )
    monkeypatch.setattr(
        bm25_attempt_pool_module,
        "_bm25_linux_mount_mappings",
        lambda: mappings,
    )

    with pin_repository_source_root(repository) as repository_authority:
        with pytest.raises(StorageIntegrityError, match="must share one mount"):
            bootstrap_local_bm25_attempt_pool(
                workspace_root=workspace,
                workspace_identity=workspace_identity,
                repository_id=_repository_id(),
                repository_authority=repository_authority,
                topology_verifier=lambda: None,
            )

    assert tuple(workspace.iterdir()) == ()


def test_bm25_attempt_pool_bootstrap_rejects_repository_descendant_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    nested = repository / "nested"
    nested.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    workspace_identity = _directory_identity(workspace)
    repository_identity = _directory_identity(repository)
    workspace_device = (
        os.major(workspace_identity[0]),
        os.minor(workspace_identity[0]),
    )
    repository_device = (
        os.major(repository_identity[0]),
        os.minor(repository_identity[0]),
    )
    mappings = (
        bm25_attempt_pool_module._BM25LinuxMountMapping(
            mount_id=1,
            device=repository_device,
            root=PurePosixPath("/physical/repository"),
            mount_point=repository,
        ),
        bm25_attempt_pool_module._BM25LinuxMountMapping(
            mount_id=2,
            device=workspace_device,
            root=PurePosixPath("/physical/workspace"),
            mount_point=nested,
        ),
        bm25_attempt_pool_module._BM25LinuxMountMapping(
            mount_id=3,
            device=workspace_device,
            root=PurePosixPath("/physical/workspace"),
            mount_point=workspace,
        ),
    )
    monkeypatch.setattr(
        bm25_attempt_pool_module,
        "_bm25_linux_mount_mappings",
        lambda: mappings,
    )

    with pin_repository_source_root(repository) as repository_authority:
        with pytest.raises(
            StorageIntegrityError,
            match="repository contains a nested mount",
        ):
            bootstrap_local_bm25_attempt_pool(
                workspace_root=workspace,
                workspace_identity=workspace_identity,
                repository_id=_repository_id(),
                repository_authority=repository_authority,
                topology_verifier=lambda: None,
            )

    assert tuple(workspace.iterdir()) == ()


def test_bm25_attempt_pool_bootstrap_rejects_symlinked_workspace_ancestry(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    nested_workspace = repository / "workspace"
    nested_workspace.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(repository, target_is_directory=True)
    lexical_workspace = alias / "workspace"
    workspace_identity = _directory_identity(nested_workspace)

    with pin_repository_source_root(repository) as repository_authority:
        with pytest.raises(
            StorageIntegrityError,
            match="must not traverse a symbolic link",
        ):
            bootstrap_local_bm25_attempt_pool(
                workspace_root=lexical_workspace,
                workspace_identity=workspace_identity,
                repository_id=_repository_id(),
                repository_authority=repository_authority,
                topology_verifier=lambda: None,
            )

    assert tuple(nested_workspace.iterdir()) == ()


def test_bm25_attempt_pool_rechecks_topology_after_open_before_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    workspace_identity = _directory_identity(workspace)
    shard = workspace / f".codenib-bm25-attempt-pool-v1-{_repository_id()}"
    real_opened_check = bm25_attempt_pool_module._require_opened_workspace_lexical_path
    real_disjoint_check = (
        bm25_attempt_pool_module._require_repository_workspace_disjoint
    )
    root_opened = False

    def mark_root_opened(
        descriptor: int,
        *,
        workspace_root: Path,
    ) -> None:
        nonlocal root_opened
        real_opened_check(descriptor, workspace_root=workspace_root)
        root_opened = True

    def reject_changed_topology(**kwargs):
        if root_opened:
            raise StorageIntegrityError(
                "BM25 attempt-pool repository contains a nested mount"
            )
        return real_disjoint_check(**kwargs)

    monkeypatch.setattr(
        bm25_attempt_pool_module,
        "_require_opened_workspace_lexical_path",
        mark_root_opened,
    )
    monkeypatch.setattr(
        bm25_attempt_pool_module,
        "_require_repository_workspace_disjoint",
        reject_changed_topology,
    )
    with pin_repository_source_root(repository) as repository_authority:
        with pytest.raises(
            StorageIntegrityError,
            match="repository contains a nested mount",
        ):
            bootstrap_local_bm25_attempt_pool(
                workspace_root=workspace,
                workspace_identity=workspace_identity,
                repository_id=_repository_id(),
                repository_authority=repository_authority,
                topology_verifier=lambda: None,
            )

    assert root_opened
    assert not shard.exists()
    assert tuple(workspace.iterdir()) == ()


def test_bm25_attempt_pool_bootstrap_rejects_mounted_shard_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    workspace_identity = _directory_identity(workspace)
    shard = workspace / f".codenib-bm25-attempt-pool-v1-{_repository_id()}"
    shard.mkdir(mode=0o700)
    real_mount_check = bm25_attempt_pool_module._path_is_mount_point
    observed: list[Path] = []

    def classify_mount(path: Path, *, mount_points) -> bool:
        observed.append(path)
        if path == shard:
            return True
        return real_mount_check(path, mount_points=mount_points)

    monkeypatch.setattr(
        bm25_attempt_pool_module,
        "_path_is_mount_point",
        classify_mount,
    )
    with pin_repository_source_root(repository) as repository_authority:
        with pytest.raises(StorageIntegrityError, match="must not be a mount point"):
            bootstrap_local_bm25_attempt_pool(
                workspace_root=workspace,
                workspace_identity=workspace_identity,
                repository_id=_repository_id(),
                repository_authority=repository_authority,
                topology_verifier=lambda: None,
            )

    assert observed == [shard]
    assert tuple(shard.iterdir()) == ()
    assert _open_descriptors_for_identity(_directory_identity(shard)) == ()


@pytest.mark.parametrize("interrupted_open", (1, 2))
def test_bm25_attempt_pool_bootstrap_native_open_handoff_retains_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupted_open: int,
) -> None:
    class Interrupted(BaseException):
        pass

    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    workspace_identity = _directory_identity(workspace)
    shard = workspace / f".codenib-bm25-attempt-pool-v1-{_repository_id()}"
    primary = Interrupted(f"native bootstrap open {interrupted_open} interrupted")
    calls = 0
    real_open = directory_lease_module._native_workspace_owner._open_directory_fd

    def open_then_interrupt(owner: object, path: bytes) -> None:
        nonlocal calls
        real_open(owner, path)
        calls += 1
        if calls == interrupted_open:
            raise primary

    monkeypatch.setattr(
        directory_lease_module._native_workspace_owner,
        "_open_directory_fd",
        open_then_interrupt,
    )
    with pin_repository_source_root(repository) as repository_authority:
        with pytest.raises(Interrupted) as caught:
            bootstrap_local_bm25_attempt_pool(
                workspace_root=workspace,
                workspace_identity=workspace_identity,
                repository_id=_repository_id(),
                repository_authority=repository_authority,
                topology_verifier=lambda: None,
            )

    assert caught.value is primary
    assert calls == interrupted_open
    assert _open_descriptors_for_identity(workspace_identity) == ()
    if shard.exists():
        assert _open_descriptors_for_identity(_directory_identity(shard)) == ()


def test_bm25_attempt_pool_route_rejects_mount_tamper_before_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    workspace_identity = _directory_identity(workspace)

    with pin_repository_source_root(repository) as repository_authority:
        binding = bootstrap_local_bm25_attempt_pool(
            workspace_root=workspace,
            workspace_identity=workspace_identity,
            repository_id=_repository_id(),
            repository_authority=repository_authority,
            topology_verifier=lambda: None,
        )
        shard = binding.writer_route._shard_path

        def classify_mount(path: Path, *, mount_points) -> bool:
            assert mount_points
            return path == shard

        monkeypatch.setattr(
            bm25_attempt_pool_module,
            "_path_is_mount_point",
            classify_mount,
        )
        monkeypatch.setattr(
            bm25_attempt_pool_module,
            "acquire_private_directory_lease",
            lambda *_args, **_kwargs: pytest.fail(
                "lease acquisition must follow mount rejection"
            ),
        )

        with pytest.raises(StorageIntegrityError, match="must not be a mount point"):
            binding.writer_route._acquire(
                check_cancelled=None,
                construction_owner=lambda _owner: None,
            )

        assert tuple(shard.iterdir()) == ()


def test_bm25_source_target_accepts_only_its_exact_writer_route(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    workspace_identity = _directory_identity(workspace)
    provider = LocalWorkspaceProvider(workspace)

    with pin_repository_source_root(repository) as repository_authority:
        binding = bootstrap_local_bm25_attempt_pool(
            workspace_root=workspace,
            workspace_identity=workspace_identity,
            repository_id=_repository_id(),
            repository_authority=repository_authority,
            topology_verifier=lambda: None,
        )
        target = LocalBM25SourceJobTarget(
            repository_root=repository,
            workspace_provider=provider,
            repository_key="owner/repository",
            display_commit="d" * 40,
            builder=BM25IndexBuilder(),
            repository_root_authority=repository_authority,
            workspace_parent_identity=workspace_identity,
            topology_verifier=lambda: None,
            attempt_pool_writer_route=binding.writer_route,
        )

    assert target.attempt_pool_root == binding.writer_route._shard_path
    assert target.attempt_pool_identity == binding.writer_route._shard_identity
    assert target.attempt_pool_writer_route is binding.writer_route
    assert binding.writer_route._shard_path.parent == workspace


def test_leased_attempt_pool_reclaims_shard_only_under_exclusive_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    workspace_identity = _directory_identity(workspace)
    provider = LocalWorkspaceProvider(workspace)
    close_observations: list[type[BaseException] | None] = []

    with pin_repository_source_root(repository) as repository_authority:
        binding = bootstrap_local_bm25_attempt_pool(
            workspace_root=workspace,
            workspace_identity=workspace_identity,
            repository_id=_repository_id(),
            repository_authority=repository_authority,
            topology_verifier=lambda: None,
        )
        target = LocalBM25SourceJobTarget(
            repository_root=repository,
            workspace_provider=provider,
            repository_key="owner/repository",
            display_commit="d" * 40,
            builder=BM25IndexBuilder(),
            repository_root_authority=repository_authority,
            workspace_parent_identity=workspace_identity,
            topology_verifier=lambda: None,
            attempt_pool_writer_route=binding.writer_route,
        )
        discarded_name = (
            "..codenib-source-job-"
            + "a" * 32
            + ".normalize-"
            + "b" * 24
            + ".discarded-"
            + "c" * 32
        )
        discarded = binding.writer_route._shard_path / discarded_name
        discarded.mkdir(mode=0o700)
        (discarded / "payload.txt").write_text("payload", encoding="utf-8")

        with pytest.raises(
            StorageValidationError,
            match="requires its reaper route",
        ):
            LocalBM25AttemptPoolCoordinator(target).reclaim(
                caller_asserts_quiescence=True,
            )

        real_close = job_resources_module.QuiescentDirectoryReclaimer._close

        def observe_close(reclaimer) -> None:
            before = _probe_raw_lease(
                binding.reaper_route._state.directory_lease_route,
                DirectoryLeaseMode.SHARED,
            )
            close_observations.append(type(before) if before is not None else None)
            real_close(reclaimer)
            after = _probe_raw_lease(
                binding.reaper_route._state.directory_lease_route,
                DirectoryLeaseMode.SHARED,
            )
            close_observations.append(type(after) if after is not None else None)

        monkeypatch.setattr(
            job_resources_module.QuiescentDirectoryReclaimer,
            "_close",
            observe_close,
        )
        result = LocalBM25AttemptPoolCoordinator(
            target,
            reaper_route=binding.reaper_route,
        ).reclaim(caller_asserts_quiescence=True)

        assert result == BM25AttemptPoolReclamation(
            scanned_children=1,
            reclaimed_children=1,
            current_children=1,
            legacy_children=0,
            discarded_children=1,
            retained_unrelated_children=0,
        )
        assert close_observations == [BlockingIOError, BlockingIOError]
        assert tuple(binding.writer_route._shard_path.iterdir()) == ()
        assert (
            _probe_raw_lease(
                binding.reaper_route._state.directory_lease_route,
                DirectoryLeaseMode.SHARED,
            )
            is None
        )

        legacy = LocalBM25AttemptPoolCoordinator(
            target,
            _legacy_workspace=True,
        ).reclaim(caller_asserts_quiescence=True)
        assert legacy == BM25AttemptPoolReclamation(
            scanned_children=1,
            reclaimed_children=0,
            current_children=0,
            legacy_children=0,
            discarded_children=0,
            retained_unrelated_children=1,
        )


def test_reclaimer_construction_owner_precedes_authority_and_can_settle_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    identity = _directory_identity(parent)
    installed: list[QuiescentDirectoryReclaimer] = []
    failure = OSError("reclaimer authority open failed")

    def fail_open(*_args, **_kwargs):
        assert len(installed) == 1
        assert type(installed[0]) is QuiescentDirectoryReclaimer
        raise failure

    monkeypatch.setattr(
        atomic_directory_module,
        "_open_publication_authority",
        fail_open,
    )
    with pytest.raises(OSError) as caught:
        QuiescentDirectoryReclaimer(
            parent,
            expected_parent_identity=identity,
            _construction_owner=installed.append,
        )

    assert caught.value is failure
    assert len(installed) == 1
    installed[0].close()
    assert installed[0].closed


def test_reclaimer_construction_owner_failure_precedes_authority_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    identity = _directory_identity(parent)
    installed: list[QuiescentDirectoryReclaimer] = []
    failure = RuntimeError("reclaimer construction owner refused handoff")

    def reject(reclaimer: QuiescentDirectoryReclaimer) -> None:
        installed.append(reclaimer)
        raise failure

    monkeypatch.setattr(
        atomic_directory_module,
        "_open_publication_authority",
        lambda *_args, **_kwargs: pytest.fail(
            "authority open must follow construction-owner handoff"
        ),
    )
    with pytest.raises(RuntimeError) as caught:
        QuiescentDirectoryReclaimer(
            parent,
            expected_parent_identity=identity,
            _construction_owner=reject,
        )

    assert caught.value is failure
    assert len(installed) == 1
    installed[0].close()
    assert installed[0].closed


@pytest.mark.parametrize("foreign_mode", (DirectoryLeaseMode.SHARED, None))
def test_reaper_cleanup_rejects_wrong_lease_authority_before_install(
    tmp_path: Path,
    foreign_mode: DirectoryLeaseMode | None,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    workspace_identity = _directory_identity(workspace)

    with pin_repository_source_root(repository) as repository_authority:
        binding = bootstrap_local_bm25_attempt_pool(
            workspace_root=workspace,
            workspace_identity=workspace_identity,
            repository_id=_repository_id(),
            repository_authority=repository_authority,
            topology_verifier=lambda: None,
        )
        route = binding.reaper_route._state.directory_lease_route
        mode = foreign_mode
        if mode is None:
            foreign = tmp_path / "foreign"
            foreign.mkdir(mode=0o700)
            route = PrivateDirectoryLeaseRoute(
                foreign,
                _directory_identity(foreign),
                os.getpid(),
            )
            mode = DirectoryLeaseMode.EXCLUSIVE
        installed: list[PrivateDirectoryLeaseOwner] = []
        lease = acquire_private_directory_lease(
            route,
            mode=mode,
            blocking=True,
            _construction_owner=installed.append,
        )
        cleanup = job_resources_module._BM25AttemptPoolReaperCleanupOwner(
            binding.reaper_route
        )
        try:
            with pytest.raises(
                StorageIntegrityError,
                match="lease authority is inconsistent",
            ):
                cleanup._install_lease(lease)
            assert cleanup.closed
            with pytest.raises(
                StorageIntegrityError,
                match="active exclusive lease",
            ):
                cleanup._open_reclaimer()
        finally:
            lease.close()


def test_incomplete_reclaimer_close_retains_exclusive_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    workspace_identity = _directory_identity(workspace)

    with pin_repository_source_root(repository) as repository_authority:
        binding = bootstrap_local_bm25_attempt_pool(
            workspace_root=workspace,
            workspace_identity=workspace_identity,
            repository_id=_repository_id(),
            repository_authority=repository_authority,
            topology_verifier=lambda: None,
        )
        cleanup = job_resources_module._BM25AttemptPoolReaperCleanupOwner(
            binding.reaper_route
        )
        cleanup._acquire()
        reclaimer = cleanup._open_reclaimer()
        real_close = job_resources_module.QuiescentDirectoryReclaimer.close
        monkeypatch.setattr(
            job_resources_module.QuiescentDirectoryReclaimer,
            "close",
            lambda _reclaimer: None,
        )

        with pytest.raises(
            StorageIntegrityError,
            match="remained active under its EX lease",
        ):
            cleanup.close()

        assert not cleanup.closed
        assert not reclaimer.closed
        assert (
            type(
                _probe_raw_lease(
                    binding.reaper_route._state.directory_lease_route,
                    DirectoryLeaseMode.SHARED,
                )
            )
            is BlockingIOError
        )
        monkeypatch.setattr(
            job_resources_module.QuiescentDirectoryReclaimer,
            "close",
            real_close,
        )
        cleanup.close()
        assert cleanup.closed
        assert (
            _probe_raw_lease(
                binding.reaper_route._state.directory_lease_route,
                DirectoryLeaseMode.SHARED,
            )
            is None
        )


def test_reclaim_failure_retains_composite_and_exclusive_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    workspace_identity = _directory_identity(workspace)
    provider = LocalWorkspaceProvider(workspace)
    removal_failure = OSError("attempt payload removal failed")
    real_reclaim_entry = atomic_directory_module._quiescent_reclaim_entry

    with pin_repository_source_root(repository) as repository_authority:
        binding = bootstrap_local_bm25_attempt_pool(
            workspace_root=workspace,
            workspace_identity=workspace_identity,
            repository_id=_repository_id(),
            repository_authority=repository_authority,
            topology_verifier=lambda: None,
        )
        target = LocalBM25SourceJobTarget(
            repository_root=repository,
            workspace_provider=provider,
            repository_key="owner/repository",
            display_commit="d" * 40,
            builder=BM25IndexBuilder(),
            repository_root_authority=repository_authority,
            workspace_parent_identity=workspace_identity,
            topology_verifier=lambda: None,
            attempt_pool_writer_route=binding.writer_route,
        )
        discarded_name = (
            "..codenib-source-job-"
            + "a" * 32
            + ".normalize-"
            + "b" * 24
            + ".discarded-"
            + "c" * 32
        )
        discarded = binding.writer_route._shard_path / discarded_name
        discarded.mkdir(mode=0o700)
        (discarded / "payload.txt").write_text("payload", encoding="utf-8")

        def fail_payload(_operation):
            raise removal_failure

        monkeypatch.setattr(
            atomic_directory_module,
            "_quiescent_reclaim_entry",
            fail_payload,
        )
        with pytest.raises(OSError) as caught:
            LocalBM25AttemptPoolCoordinator(
                target,
                reaper_route=binding.reaper_route,
            ).reclaim(caller_asserts_quiescence=True)

        assert caught.value is removal_failure
        cleanup = next(
            owner
            for owner in getattr(
                caught.value,
                "publication_cleanup_owners",
                (),
            )
            if type(owner) is job_resources_module._BM25AttemptPoolReaperCleanupOwner
        )
        assert {name for name in dir(cleanup) if not name.startswith("_")} == {
            "close",
            "closed",
        }
        assert not cleanup.closed
        assert (
            type(
                _probe_raw_lease(
                    binding.reaper_route._state.directory_lease_route,
                    DirectoryLeaseMode.SHARED,
                )
            )
            is BlockingIOError
        )

        monkeypatch.setattr(
            atomic_directory_module,
            "_quiescent_reclaim_entry",
            real_reclaim_entry,
        )
        cleanup.close()
        assert cleanup.closed
        assert not discarded.exists()
        assert (
            _probe_raw_lease(
                binding.reaper_route._state.directory_lease_route,
                DirectoryLeaseMode.SHARED,
            )
            is None
        )
