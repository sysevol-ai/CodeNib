# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import dis
import os
import sys
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

import codenib.web.local_index_service as local_index_service
from codenib.storage import SQLiteCatalog
from codenib.web.config import LocalIndexStorageConfig, LocalIndexStorageRepository
from codenib.web.local_index_service import (
    ExistingLocalIndexCatalogFactory,
    LocalIndexServiceError,
    LocalIndexStorageTopology,
)


def _provision_catalog(path: Path) -> None:
    with SQLiteCatalog(path):
        pass


def _topology_config(
    tmp_path: Path,
) -> tuple[LocalIndexStorageConfig, dict[str, Path]]:
    catalog_path = tmp_path / "catalog.sqlite3"
    cas_root = tmp_path / "cas"
    worker_root = tmp_path / "worker"
    runtime_root = tmp_path / "runtime"
    repository_root = tmp_path / "repository"
    _provision_catalog(catalog_path)
    cas_root.mkdir()
    worker_root.mkdir(mode=0o700)
    runtime_root.mkdir(mode=0o700)
    repository_root.mkdir()
    return (
        LocalIndexStorageConfig(
            catalog_path=catalog_path,
            cas_root=cas_root,
            worker_workspace_root=worker_root,
            runtime_workspace_root=runtime_root,
            repositories=(
                LocalIndexStorageRepository(
                    repo_id="repo",
                    repository_key="org/repo",
                ),
            ),
        ),
        {"repo": repository_root},
    )


def test_existing_catalog_factory_opens_fresh_exact_sessions(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.sqlite3"
    _provision_catalog(catalog_path)
    factory = ExistingLocalIndexCatalogFactory(catalog_path, busy_timeout_ms=250)

    with factory() as first:
        assert first.schema_version > 0
    with factory() as second:
        assert second.schema_version > 0
        assert second is not first

    assert factory.catalog_identity == (
        catalog_path.stat().st_dev,
        catalog_path.stat().st_ino,
        1,
    )


def test_existing_catalog_factory_closes_session_after_store_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog_path = tmp_path / "catalog.sqlite3"
    _provision_catalog(catalog_path)
    factory = ExistingLocalIndexCatalogFactory(catalog_path)
    interruption = KeyboardInterrupt("catalog session stored")

    class Session:
        closed = False

        def close(self) -> None:
            self.closed = True

    session = Session()
    monkeypatch.setattr(
        local_index_service,
        "SQLiteCatalog",
        lambda *_args, **_kwargs: session,
    )

    acquire = local_index_service._CatalogSessionOwner.acquire
    instructions = tuple(dis.get_instructions(acquire))
    store_indexes = {
        index
        for index, instruction in enumerate(instructions[:-1])
        if instruction.opname == "STORE_ATTR" and instruction.argval == "_catalog"
    }
    assert len(store_indexes) == 1
    store_index = next(iter(store_indexes))
    opcode_offsets_after_store = {instructions[store_index + 1].offset}
    store_offset = instructions[store_index].offset
    line_offsets_after_store = {
        instruction.offset
        for instruction in instructions
        if instruction.starts_line is not None and instruction.offset > store_offset
    }
    injected = False
    previous_trace = sys.gettrace()

    def trace(frame, event: str, _arg: object):
        nonlocal injected
        if event == "call" and frame.f_code is acquire.__code__:
            frame.f_trace_opcodes = True
            frame.f_trace_lines = True
            return trace
        if (
            not injected
            and frame.f_code is acquire.__code__
            and (
                event == "opcode"
                and frame.f_lasti in opcode_offsets_after_store
                or event == "line"
                and frame.f_lasti in line_offsets_after_store
            )
            and frame.f_locals["self"].closed is False
        ):
            injected = True
            sys.settrace(None)
            raise interruption
        return trace

    def open_session() -> None:
        with factory():
            pytest.fail("interruption must precede the context body")

    sys.settrace(trace)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            open_session()
    finally:
        sys.settrace(previous_trace)

    assert injected
    assert raised.value is interruption
    assert session.closed


def test_existing_catalog_factory_rejects_symbolic_path_components(
    tmp_path: Path,
) -> None:
    catalog_path = tmp_path / "catalog.sqlite3"
    alias = tmp_path / "alias.sqlite3"
    _provision_catalog(catalog_path)
    alias.symlink_to(catalog_path)

    with pytest.raises(LocalIndexServiceError, match="single-linked file"):
        ExistingLocalIndexCatalogFactory(alias)


def test_existing_catalog_factory_rejects_replaced_catalog(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.sqlite3"
    displaced = tmp_path / "displaced.sqlite3"
    _provision_catalog(catalog_path)
    factory = ExistingLocalIndexCatalogFactory(catalog_path)
    catalog_path.rename(displaced)
    _provision_catalog(catalog_path)

    with pytest.raises(LocalIndexServiceError, match="binding changed"):
        with factory():
            pass


def test_existing_catalog_factory_revalidates_after_session_body(
    tmp_path: Path,
) -> None:
    catalog_path = tmp_path / "catalog.sqlite3"
    displaced = tmp_path / "displaced.sqlite3"
    _provision_catalog(catalog_path)
    factory = ExistingLocalIndexCatalogFactory(catalog_path)

    with pytest.raises(LocalIndexServiceError, match="cannot be inspected safely"):
        with factory() as catalog:
            assert catalog.schema_version > 0
            catalog_path.rename(displaced)

    with SQLiteCatalog(displaced, create=False) as reopened:
        assert reopened.schema_version > 0


@pytest.mark.parametrize("timeout", (True, -1, 86_400_001))
def test_existing_catalog_factory_rejects_invalid_timeout(
    tmp_path: Path,
    timeout: object,
) -> None:
    catalog_path = tmp_path / "catalog.sqlite3"
    _provision_catalog(catalog_path)

    with pytest.raises(ValueError, match="busy timeout"):
        ExistingLocalIndexCatalogFactory(
            catalog_path,
            busy_timeout_ms=timeout,  # type: ignore[arg-type]
        )


def test_local_index_topology_retains_and_revalidates_every_authority(
    tmp_path: Path,
) -> None:
    config, repositories = _topology_config(tmp_path)

    topology = LocalIndexStorageTopology.acquire(config, repositories)

    topology.verify()
    assert topology.closed is False
    assert topology.worker_workspace_identity[:2] == (
        config.worker_workspace_root.stat().st_dev,
        config.worker_workspace_root.stat().st_ino,
    )
    assert topology.runtime_workspace_identity[:2] == (
        config.runtime_workspace_root.stat().st_dev,
        config.runtime_workspace_root.stat().st_ino,
    )
    assert topology.repository_authority("repo").root == repositories["repo"]
    with topology.catalog_factory() as catalog:
        assert catalog.schema_version > 0

    topology.close()
    topology.close()
    assert topology.closed is True
    with pytest.raises(LocalIndexServiceError, match="closed"):
        topology.verify()


def test_local_index_topology_detects_replaced_workspace_root(
    tmp_path: Path,
) -> None:
    config, repositories = _topology_config(tmp_path)
    topology = LocalIndexStorageTopology.acquire(config, repositories)
    displaced = tmp_path / "worker-displaced"
    config.worker_workspace_root.rename(displaced)
    config.worker_workspace_root.mkdir(mode=0o700)

    try:
        with pytest.raises(LocalIndexServiceError, match="binding changed"):
            topology.verify()
    finally:
        topology.close()


def test_local_index_topology_normalizes_replaced_repository_authority(
    tmp_path: Path,
) -> None:
    config, repositories = _topology_config(tmp_path)
    topology = LocalIndexStorageTopology.acquire(config, repositories)
    repository = repositories["repo"]
    displaced = tmp_path / "repository-displaced"
    repository.rename(displaced)
    repository.mkdir()

    try:
        with pytest.raises(
            LocalIndexServiceError,
            match="repository 'repo' binding changed",
        ):
            topology.repository_authority("repo")
    finally:
        topology.close()


@pytest.mark.parametrize("target", ("catalog", "workspace", "repository"))
def test_local_index_topology_rechecks_bindings_after_physical_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    config, repositories = _topology_config(tmp_path)
    topology = LocalIndexStorageTopology.acquire(config, repositories)
    real_require = local_index_service._require_disjoint_topology
    mutated = False

    def inspect_then_replace(paths) -> None:
        nonlocal mutated
        real_require(paths)
        if target == "catalog":
            config.catalog_path.rename(tmp_path / "catalog-displaced.sqlite3")
            _provision_catalog(config.catalog_path)
        elif target == "workspace":
            config.worker_workspace_root.rename(tmp_path / "worker-displaced")
            config.worker_workspace_root.mkdir(mode=0o700)
        else:
            repositories["repo"].rename(tmp_path / "repository-displaced")
            repositories["repo"].mkdir()
        mutated = True

    monkeypatch.setattr(
        local_index_service,
        "_require_disjoint_topology",
        inspect_then_replace,
    )
    try:
        with pytest.raises(LocalIndexServiceError, match="binding changed"):
            topology.verify()
        assert mutated
    finally:
        topology.close()


def test_local_index_topology_rejects_symlinked_storage_root(
    tmp_path: Path,
) -> None:
    config, repositories = _topology_config(tmp_path)
    actual_runtime = tmp_path / "actual-runtime"
    config.runtime_workspace_root.rename(actual_runtime)
    config.runtime_workspace_root.symlink_to(actual_runtime, target_is_directory=True)

    with pytest.raises(LocalIndexServiceError, match="real private owner-only"):
        LocalIndexStorageTopology.acquire(config, repositories)


def test_local_index_topology_rejects_repository_storage_overlap(
    tmp_path: Path,
) -> None:
    config, _repositories = _topology_config(tmp_path)
    repository = config.cas_root / "repository"
    repository.mkdir()

    with pytest.raises(LocalIndexServiceError, match="must not overlap"):
        LocalIndexStorageTopology.acquire(config, {"repo": repository})


@pytest.mark.parametrize(
    ("suffix", "error"),
    (
        ("-wal", "catalog WAL sidecar must be one real"),
        ("-shm", "catalog SHM sidecar must be one real"),
        ("-journal", "rollback journal is not allowed"),
    ),
)
def test_local_index_topology_reserves_catalog_sidecar_namespace(
    tmp_path: Path,
    suffix: str,
    error: str,
) -> None:
    config, repositories = _topology_config(tmp_path)
    sidecar = Path(f"{config.catalog_path}{suffix}")
    config.worker_workspace_root.rename(sidecar)
    config = replace(config, worker_workspace_root=sidecar)

    with pytest.raises(LocalIndexServiceError, match=error):
        LocalIndexStorageTopology.acquire(config, repositories)


@pytest.mark.parametrize(
    ("suffix", "error"),
    (
        ("-wal", "catalog WAL sidecar must be one real"),
        ("-shm", "catalog SHM sidecar must be one real"),
        ("-journal", "rollback journal is not allowed"),
    ),
)
def test_local_index_topology_rejects_unconfigured_unsafe_catalog_sidecar(
    tmp_path: Path,
    suffix: str,
    error: str,
) -> None:
    config, repositories = _topology_config(tmp_path)
    sidecar = Path(f"{config.catalog_path}{suffix}")
    assert not sidecar.exists()
    sidecar.mkdir()

    with pytest.raises(LocalIndexServiceError, match=error):
        LocalIndexStorageTopology.acquire(config, repositories)


def test_local_index_topology_rejects_retained_resource_overcommit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, repositories = _topology_config(tmp_path)
    required = sum(
        len(path.parts)
        for path in (
            *repositories.values(),
            config.cas_root,
            config.worker_workspace_root,
            config.runtime_workspace_root,
        )
    )
    monkeypatch.setattr(
        local_index_service,
        "_available_topology_resource_budget",
        lambda: required - 1,
    )
    monkeypatch.setattr(
        local_index_service,
        "pin_repository_source_root",
        lambda *_args, **_kwargs: pytest.fail(
            "repository pinning must follow resource admission"
        ),
    )

    with pytest.raises(LocalIndexServiceError, match="retained resource budget"):
        LocalIndexStorageTopology.acquire(config, repositories)


def test_local_index_topology_accepts_exact_retained_resource_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, repositories = _topology_config(tmp_path)
    required = sum(
        len(path.parts)
        for path in (
            *repositories.values(),
            config.cas_root,
            config.worker_workspace_root,
            config.runtime_workspace_root,
        )
    )
    monkeypatch.setattr(
        local_index_service,
        "_available_topology_resource_budget",
        lambda: required,
    )

    topology = LocalIndexStorageTopology.acquire(config, repositories)
    topology.close()


def test_local_index_topology_rejects_mapped_physical_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, repositories = _topology_config(tmp_path)
    device = config.cas_root.stat().st_dev
    mount_device = f"{os.major(device)}:{os.minor(device)}"
    mappings = (
        local_index_service._LinuxMountMapping(
            device=mount_device,
            root=PurePosixPath("/physical/shared/cache"),
            mount_point=config.cas_root,
        ),
        local_index_service._LinuxMountMapping(
            device=mount_device,
            root=PurePosixPath("/physical/shared"),
            mount_point=repositories["repo"],
        ),
    )
    monkeypatch.setattr(
        local_index_service,
        "_linux_mount_mappings",
        lambda: mappings,
    )

    with pytest.raises(LocalIndexServiceError, match="physical alias"):
        LocalIndexStorageTopology.acquire(config, repositories)


def test_local_index_topology_rejects_descendant_mount_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, repositories = _topology_config(tmp_path)
    device = config.cas_root.stat().st_dev
    mount_device = f"{os.major(device)}:{os.minor(device)}"
    mappings = (
        local_index_service._LinuxMountMapping(
            device=mount_device,
            root=PurePosixPath("/physical/cache"),
            mount_point=config.cas_root,
        ),
        local_index_service._LinuxMountMapping(
            device=mount_device,
            root=PurePosixPath("/physical/repository"),
            mount_point=repositories["repo"],
        ),
        local_index_service._LinuxMountMapping(
            device=mount_device,
            root=PurePosixPath("/physical/cache"),
            mount_point=repositories["repo"] / "mounted-cache",
        ),
    )
    monkeypatch.setattr(
        local_index_service,
        "_linux_mount_mappings",
        lambda: mappings,
    )

    with pytest.raises(LocalIndexServiceError, match="physical alias"):
        LocalIndexStorageTopology.acquire(config, repositories)


def test_local_index_topology_rejects_catalog_mount_point(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, repositories = _topology_config(tmp_path)
    device = config.catalog_path.stat().st_dev
    mappings = (
        local_index_service._LinuxMountMapping(
            device=f"{os.major(device)}:{os.minor(device)}",
            root=PurePosixPath("/physical/catalog.sqlite3"),
            mount_point=config.catalog_path,
        ),
    )
    monkeypatch.setattr(
        local_index_service,
        "_linux_mount_mappings",
        lambda: mappings,
    )

    with pytest.raises(LocalIndexServiceError, match="catalog.*mount point"):
        LocalIndexStorageTopology.acquire(config, repositories)


@pytest.mark.parametrize("mode", (0o755, 0o1700, 0o2700))
def test_local_index_topology_requires_private_workspace_roots(
    tmp_path: Path,
    mode: int,
) -> None:
    config, repositories = _topology_config(tmp_path)
    config.runtime_workspace_root.chmod(mode)

    with pytest.raises(LocalIndexServiceError, match="private owner-only"):
        LocalIndexStorageTopology.acquire(config, repositories)


def test_local_index_topology_gates_posix_private_mode_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, repositories = _topology_config(tmp_path)
    config.runtime_workspace_root.chmod(0o755)
    monkeypatch.setattr(
        local_index_service,
        "_POSIX_PRIVATE_MODE_SEMANTICS",
        False,
    )

    topology = LocalIndexStorageTopology.acquire(config, repositories)
    topology.close()


def test_local_index_topology_requires_exact_repository_mapping(
    tmp_path: Path,
) -> None:
    config, repositories = _topology_config(tmp_path)

    with pytest.raises(ValueError, match="match configured bindings"):
        LocalIndexStorageTopology.acquire(
            config,
            {"other": repositories["repo"]},
        )
