# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
import subprocess
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import pytest

import codenib.compiler as compiler_module
import codenib.compiler.cache_import as cache_import_module
from codenib import LocalWorkspaceProvider
from codenib import cli as cli_module
from codenib._captured_directory import (
    OwnedWorkspaceAuthority,
    PublishedWorkspaceReceiptOwner,
    UnsupportedWorkspaceCreation,
)
from codenib._workspace_provider import (
    StrictWorkspaceRequest,
    StrictWorkspaceSession,
    run_adopted_workspace_operation,
)
from codenib.artifacts import query_context_artifact
from codenib.compiler.cache_import import (
    CompilerCacheBm25RecaptureResult,
    CompilerCacheImportResult,
    compiler_cache_source_selection,
    import_compiler_cache_bm25,
)
from codenib.compiler.index_builders import BM25IndexBuilder, IndexBuilderRegistry
from codenib.compiler.index_compiler import IndexCompiler, IndexCompilerConfig
from codenib.compiler.manifest import IndexEntry, RepoManifest
from codenib.compiler.manifest_import import RepoManifestImportResult
from codenib.compiler.manifest_materialization import (
    materialize_retained_repo_manifest_ref,
)
from codenib.mcp.context import ServerContext
from codenib.repository_source_selection import RepositorySourceSelection
from codenib.source_fingerprint import (
    RepositorySourceBinding,
    capture_repository_source,
    fingerprint_repository,
)
from codenib.storage import (
    RETAINED_IMPORT_CATALOG_CONTRACT,
    LocalCAS,
    SQLiteCatalog,
    StorageIntegrityError,
    StorageValidationError,
)

_Result = TypeVar("_Result")
_COMMIT = "a" * 40
_REPOSITORY_KEY = "owner/repo"


class _BackendTripwire:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def retained_import_contract(self) -> str:
        self.calls.append("contract")
        return RETAINED_IMPORT_CATALOG_CONTRACT

    def _unexpected(self, name: str):
        self.calls.append(name)
        raise AssertionError(f"unexpected backend data operation: {name}")

    def put_bytes(self, *args, **kwargs):
        return self._unexpected("put_bytes")

    def put_file(self, *args, **kwargs):
        return self._unexpected("put_file")

    def has(self, *args, **kwargs):
        return self._unexpected("has")

    def open(self, *args, **kwargs):
        return self._unexpected("open")

    def read_bytes(self, *args, **kwargs):
        return self._unexpected("read_bytes")

    def verify(self, *args, **kwargs):
        return self._unexpected("verify")

    def materialize(self, *args, **kwargs):
        return self._unexpected("materialize")

    def put_chunks(self, *args, **kwargs):
        return self._unexpected("put_chunks")

    def verify_receipt(self, *args, **kwargs):
        return self._unexpected("verify_receipt")

    def retain_receipts(self, *args, **kwargs):
        return self._unexpected("retain_receipts")

    def create_namespace(self, *args, **kwargs):
        return self._unexpected("create_namespace")

    def create_repository(self, *args, **kwargs):
        return self._unexpected("create_repository")

    def create_source_revision(self, *args, **kwargs):
        return self._unexpected("create_source_revision")

    def create_view_profile(self, *args, **kwargs):
        return self._unexpected("create_view_profile")

    def register_object(self, *args, **kwargs):
        return self._unexpected("register_object")

    def stage_view_generation(self, *args, **kwargs):
        return self._unexpected("stage_view_generation")

    def publish_snapshot(self, *args, **kwargs):
        return self._unexpected("publish_snapshot")

    def resolve_ref(self, *args, **kwargs):
        return self._unexpected("resolve_ref")

    def get_manifest_summary(self, *args, **kwargs):
        return self._unexpected("get_manifest_summary")


class _TestWorkspaceProvider:
    """Quiescent test provider exercising the real workspace authority."""

    def __init__(self) -> None:
        self.support_count = 0
        self.run_count = 0

    def require_support(self) -> None:
        self.support_count += 1

    def run_workspace(
        self,
        request: StrictWorkspaceRequest,
        *,
        receipt_owner: PublishedWorkspaceReceiptOwner,
        operation: Callable[[StrictWorkspaceSession], _Result],
    ) -> _Result:
        self.run_count += 1
        plan = request.plan
        parent = request.destination.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        stage = parent / (
            f".{request.destination.name}.cache-import-{id(self):x}-"
            f"{self.run_count}"
        )
        stage.mkdir(mode=plan.root_mode)
        for directory in plan.directories:
            (stage / directory.path.as_posix()).mkdir(mode=directory.mode)

        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        parent_descriptor = os.open(parent, flags)
        root_descriptor = os.open(stage, flags)
        directory_descriptors = {
            directory.path.as_posix(): os.open(stage / directory.path.as_posix(), flags)
            for directory in plan.directories
        }
        workspace = OwnedWorkspaceAuthority()
        try:
            workspace.adopt(
                destination=request.destination,
                stage_name=stage.name,
                parent_descriptor=parent_descriptor,
                root_descriptor=root_descriptor,
                directory_descriptors=directory_descriptors,
                plan=plan,
                expected_destination=None,
            )
            try:
                return run_adopted_workspace_operation(
                    request,
                    workspace=workspace,
                    receipt_owner=receipt_owner,
                    operation=operation,
                )
            except BaseException:
                if receipt_owner.state == "empty":
                    workspace.close()
                raise
        finally:
            for descriptor in directory_descriptors.values():
                os.close(descriptor)
            os.close(root_descriptor)
            os.close(parent_descriptor)


class _LockAwareCAS(LocalCAS):
    def __init__(self, root: Path, state: dict[str, object]) -> None:
        self._test_state = state
        super().__init__(root)

    def put_chunks(
        self,
        chunks,
        expected_digest: str,
        expected_size: int,
    ):
        assert self._test_state["phase"] == "released"
        self._test_state["events"].append(  # type: ignore[union-attr]
            ("cas.put_chunks", self._test_state["phase"])
        )
        return super().put_chunks(chunks, expected_digest, expected_size)


class _LockAwareCatalog(SQLiteCatalog):
    def __init__(self, path: Path, state: dict[str, object]) -> None:
        self._test_state = state
        super().__init__(path)

    def retained_import_contract(self) -> str:
        assert self._test_state["phase"] != "locked"
        self._test_state["events"].append(  # type: ignore[union-attr]
            ("catalog.retained_import_contract", self._test_state["phase"])
        )
        return super().retained_import_contract()

    def create_namespace(self, name: str) -> str:
        assert self._test_state["phase"] == "released"
        self._test_state["events"].append(  # type: ignore[union-attr]
            ("catalog.create_namespace", self._test_state["phase"])
        )
        return super().create_namespace(name)


class _PostCommitInterruptCatalog(SQLiteCatalog):
    def __init__(self, path: Path) -> None:
        self._interrupt_after_first_publish = True
        super().__init__(path)

    def publish_snapshot(self, *args, **kwargs):
        publication = super().publish_snapshot(*args, **kwargs)
        if self._interrupt_after_first_publish:
            self._interrupt_after_first_publish = False
            raise RuntimeError("postcommit interruption")
        return publication


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
            separators=(",", ": "),
        )
        + "\n"
    ).encode("utf-8")


def _file_fingerprint(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _fake_import_result() -> RepoManifestImportResult:
    return RepoManifestImportResult(
        repository_id="repository-test",
        source_revision_id="source-test",
        snapshot_id="snapshot-test",
        ref_name="main",
        generation=1,
        changed=True,
        views=("bm25",),
        skipped_items=(),
        view_generation_items=(("bm25", "generation-test"),),
    )


@dataclass
class _CacheFixture:
    repository: Path
    cache: Path
    source: RepositorySourceBinding
    workspace: Path
    provider: _TestWorkspaceProvider
    bm25_owner: PublishedWorkspaceReceiptOwner
    context_owner: PublishedWorkspaceReceiptOwner

    @property
    def bm25_destination(self) -> Path:
        return self.workspace / "published-bm25"

    @property
    def context_destination(self) -> Path:
        return self.workspace / "published-context"

    def close(self) -> None:
        self.context_owner.close()
        self.bm25_owner.close()
        self.source.close()


def _cache_fixture(tmp_path: Path) -> _CacheFixture:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    source_file = repository / "sample.py"
    source_text = "VALUE = 1\n"
    source_file.write_text(source_text, encoding="utf-8")

    cache = repository / ".codenib_cache"
    bm25 = cache / "bm25"
    bm25.mkdir(mode=0o700, parents=True)
    # Mirror a real IndexCompiler cache: its fixed coordination inode already
    # exists before the existing-only import bridge borrows it.
    with cache_import_module.compiler_cache_lock(cache):
        pass
    documents = [
        {
            "page_content": source_text,
            "metadata": {
                "file": str(source_file),
                "node_id": "sample.py",
                "start_line": 0,
                "end_line": 0,
                "type": "file",
            },
        }
    ]
    (bm25 / "documents.json").write_bytes(_json_bytes(documents))
    (bm25 / "bm25_metadata.json").write_bytes(
        _json_bytes(
            {
                "project_root": str(repository),
                "max_k": 17,
                "language": "python",
            }
        )
    )
    for path in bm25.iterdir():
        path.chmod(0o600)

    source = capture_repository_source(repository, exclude_roots=(cache,))
    fingerprints = {
        name: _file_fingerprint(bm25 / name)
        for name in ("bm25_metadata.json", "documents.json")
    }
    config = BM25IndexBuilder(
        languages=["python"],
        max_k=17,
        max_lines_per_chunk=300,
    ).artifact_identity()
    config.update(
        {
            "artifact_file_fingerprints": fingerprints,
            "chunk_count": 1,
            "source_file_count": 1,
            "file_count": 1,
        }
    )
    entry = IndexEntry(
        index_type="bm25",
        path=str(bm25),
        built_at="2026-01-01T00:00:00+00:00",
        built_at_epoch=1.0,
        status="fresh",
        config=copy.deepcopy(config),
        metadata={**copy.deepcopy(config), "build_duration_seconds": 0.1},
        commit=_COMMIT,
        source_fingerprint=source.fingerprint,
    )
    manifest = RepoManifest(
        repo_path=str(repository),
        commit=_COMMIT,
        last_indexed_commit=_COMMIT,
        source_fingerprint=source.fingerprint,
        last_indexed_source_fingerprint=source.fingerprint,
        languages=["python"],
        file_count=source.file_count,
        indexes={"bm25": entry},
        compiled_at="2026-01-01T00:00:00+00:00",
        compiled_at_epoch=1.0,
    )
    manifest.derive_capabilities()
    manifest.save(cache / "repo_manifest.json")
    (cache / "repo_manifest.json").chmod(0o600)

    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    return _CacheFixture(
        repository=repository,
        cache=cache,
        source=source,
        workspace=workspace,
        provider=_TestWorkspaceProvider(),
        bm25_owner=PublishedWorkspaceReceiptOwner(),
        context_owner=PublishedWorkspaceReceiptOwner(),
    )


def _call(
    fixture: _CacheFixture,
    *,
    catalog: object | None = None,
    object_store: object | None = None,
    **overrides,
) -> CompilerCacheImportResult:
    if catalog is None:
        catalog = _BackendTripwire()
    if object_store is None:
        object_store = catalog
    arguments = {
        "repository_source": fixture.source,
        "bm25_output_owner": fixture.bm25_owner,
        "context_output_owner": fixture.context_owner,
        "bm25_destination": fixture.bm25_destination,
        "context_destination": fixture.context_destination,
        "workspace_provider": fixture.provider,
        "repository_key": _REPOSITORY_KEY,
        "catalog": catalog,
        "object_store": object_store,
        "environ": {},
    }
    arguments.update(overrides)
    return import_compiler_cache_bm25(fixture.cache, **arguments)  # type: ignore[arg-type]


def test_compiler_cache_import_is_a_lazy_public_export() -> None:
    assert compiler_module.CompilerCacheImportResult is CompilerCacheImportResult
    assert (
        compiler_module.CompilerCacheBm25RecaptureResult
        is CompilerCacheBm25RecaptureResult
    )
    assert compiler_module.import_compiler_cache_bm25 is import_compiler_cache_bm25
    assert list(inspect.signature(import_compiler_cache_bm25).parameters)[:10] == [
        "cache_dir",
        "repository_source",
        "bm25_output_owner",
        "context_output_owner",
        "bm25_destination",
        "context_destination",
        "workspace_provider",
        "repository_key",
        "catalog",
        "object_store",
    ]


def test_compiler_cache_source_selection_reads_exact_persisted_policy(
    tmp_path: Path,
) -> None:
    fixture = _cache_fixture(tmp_path)
    selection = RepositorySourceSelection(("generated",))
    identity = fingerprint_repository(
        fixture.repository,
        exclude_roots=(fixture.cache,),
        selection=selection,
    )
    manifest_path = fixture.cache / "repo_manifest.json"
    manifest = RepoManifest.load(manifest_path)
    manifest.source_selection = selection
    manifest.last_indexed_source_selection_digest = selection.digest
    manifest.source_fingerprint = identity.value
    manifest.last_indexed_source_fingerprint = identity.value
    manifest.file_count = identity.file_count
    entry = manifest.indexes["bm25"]
    entry.source_selection_digest = selection.digest
    entry.source_fingerprint = identity.value
    manifest.save(manifest_path)

    observed = compiler_cache_source_selection(fixture.cache)

    assert observed == selection
    assert observed is not selection


def test_import_recaptures_inside_cache_lock_and_imports_after_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _cache_fixture(tmp_path)
    lock_state = {"held": False}
    real_lock = cache_import_module.compiler_cache_lock
    real_plan = cache_import_module.plan_repo_manifest_import_bytes
    planned_before_publish = {"value": False}

    @contextmanager
    def tracked_lock(cache: Path, *, create: bool = True):
        assert create is False
        with real_lock(cache, create=create):
            lock_state["held"] = True
            try:
                yield
            finally:
                lock_state["held"] = False

    def tracked_plan(*args, **kwargs):
        assert lock_state["held"]
        planned_before_publish["value"] = True
        return real_plan(*args, **kwargs)

    real_publish = cache_import_module._publish_recaptured_bm25_view

    def tracked_publish(*args, **kwargs):
        assert planned_before_publish["value"]
        return real_publish(*args, **kwargs)

    def imported(plan, **kwargs):
        assert not lock_state["held"]
        assert plan.selection.selected_views == ("bm25",)
        assert kwargs["artifact_owner"] is fixture.context_owner
        assert kwargs["repository_source"] is fixture.source
        return _fake_import_result()

    monkeypatch.setattr(cache_import_module, "compiler_cache_lock", tracked_lock)
    monkeypatch.setattr(
        cache_import_module,
        "plan_repo_manifest_import_bytes",
        tracked_plan,
    )
    monkeypatch.setattr(
        cache_import_module,
        "_publish_recaptured_bm25_view",
        tracked_publish,
    )
    monkeypatch.setattr(
        cache_import_module,
        "import_retained_repo_manifest",
        imported,
    )

    try:
        result = _call(fixture)

        assert result.import_result == _fake_import_result()
        assert result.import_plan.selection.selected_views == ("bm25",)
        assert result.context_artifact.manifest_path.read_bytes() == (
            result.recapture.canonical_manifest_bytes
        )
        assert result.recapture.output_view == fixture.bm25_destination
        assert fixture.provider.support_count == 3
        assert fixture.provider.run_count == 2
        assert fixture.bm25_owner.active
        assert fixture.context_owner.active
        assert fixture.source.usable
    finally:
        fixture.close()


def test_manifest_fingerprint_mismatch_fails_before_workspace_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _cache_fixture(tmp_path)
    manifest = RepoManifest.load(fixture.cache / "repo_manifest.json")
    manifest.indexes["bm25"].config["artifact_file_fingerprints"]["documents.json"][
        "sha256"
    ] = ("0" * 64)
    manifest.save(fixture.cache / "repo_manifest.json")
    (fixture.cache / "repo_manifest.json").chmod(0o600)
    imported = False

    def unexpected_import(*args, **kwargs):
        nonlocal imported
        imported = True
        raise AssertionError("storage import must not run")

    monkeypatch.setattr(
        cache_import_module,
        "import_retained_repo_manifest",
        unexpected_import,
    )
    try:
        with pytest.raises(StorageIntegrityError, match="manifest fingerprints"):
            _call(fixture)
        assert fixture.provider.run_count == 0
        assert fixture.bm25_owner.state == "empty"
        assert fixture.context_owner.state == "empty"
        assert not imported
    finally:
        fixture.close()


@pytest.mark.parametrize(
    ("tamper", "error"),
    [
        ("source", "repository source"),
        ("documents", "manifest fingerprints"),
        ("manifest", "manifest fingerprints"),
    ],
)
def test_source_and_cache_tamper_fail_before_storage_or_workspace_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
    error: str,
) -> None:
    fixture = _cache_fixture(tmp_path)
    if tamper == "source":
        (fixture.repository / "sample.py").write_text(
            "SOURCE_CHANGED = True\n",
            encoding="utf-8",
        )
    elif tamper == "documents":
        with (fixture.cache / "bm25/documents.json").open("ab") as handle:
            handle.write(b"\n")
    else:
        manifest = RepoManifest.load(fixture.cache / "repo_manifest.json")
        manifest.indexes["bm25"].metadata["artifact_file_fingerprints"][
            "documents.json"
        ]["sha256"] = ("f" * 64)
        manifest.save(fixture.cache / "repo_manifest.json")
        (fixture.cache / "repo_manifest.json").chmod(0o600)

    storage_calls: list[str] = []

    def unexpected_import(*args, **kwargs):
        storage_calls.append("import")
        raise AssertionError("storage import must not run")

    monkeypatch.setattr(
        cache_import_module,
        "import_retained_repo_manifest",
        unexpected_import,
    )
    backend = _BackendTripwire()
    try:
        with pytest.raises((RuntimeError, ValueError), match=error):
            _call(fixture, catalog=backend, object_store=backend)
        assert storage_calls == []
        assert backend.calls == ["contract"]
        assert fixture.provider.support_count == 1
        assert fixture.provider.run_count == 0
        assert fixture.bm25_owner.state == "empty"
        assert fixture.context_owner.state == "empty"
        fixture.bm25_owner.close()
        fixture.context_owner.close()
        assert fixture.bm25_owner.closed
        assert fixture.context_owner.closed
    finally:
        fixture.close()


def test_existing_context_destination_denies_before_bm25_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _cache_fixture(tmp_path)
    fixture.context_destination.mkdir()
    monkeypatch.setattr(
        cache_import_module,
        "import_retained_repo_manifest",
        lambda *args, **kwargs: pytest.fail("storage import must not run"),
    )
    try:
        with pytest.raises(FileExistsError, match="must be missing"):
            _call(fixture)
        assert fixture.provider.run_count == 0
        assert fixture.bm25_owner.state == "empty"
        assert fixture.context_owner.state == "empty"
    finally:
        fixture.close()


@pytest.mark.parametrize(
    ("overrides", "error", "contract_probes"),
    [
        ({"repository_key": "Owner/Repo"}, "repository key", 0),
        ({"max_context_files": 0}, "context file limit", 0),
        ({"expected_generation": -1}, "expected ref generation", 0),
    ],
)
def test_invalid_import_inputs_fail_before_support_contract_or_workspace(
    tmp_path: Path,
    overrides: dict[str, object],
    error: str,
    contract_probes: int,
) -> None:
    fixture = _cache_fixture(tmp_path)
    backend = _BackendTripwire()
    try:
        with pytest.raises(StorageValidationError, match=error):
            _call(
                fixture,
                catalog=backend,
                object_store=backend,
                **overrides,
            )
        assert backend.calls == ["contract"] * contract_probes
        assert fixture.provider.support_count == 0
        assert fixture.provider.run_count == 0
        assert fixture.bm25_owner.state == "empty"
        assert fixture.context_owner.state == "empty"
    finally:
        fixture.close()


def test_short_manifest_commit_fails_after_contract_probe_without_data_io(
    tmp_path: Path,
) -> None:
    fixture = _cache_fixture(tmp_path)
    manifest = RepoManifest.load(fixture.cache / "repo_manifest.json")
    manifest.commit = "abc123"
    manifest.last_indexed_commit = "abc123"
    manifest.indexes["bm25"].commit = "abc123"
    manifest.save(fixture.cache / "repo_manifest.json")
    (fixture.cache / "repo_manifest.json").chmod(0o600)
    backend = _BackendTripwire()
    try:
        with pytest.raises(ValueError, match="full lowercase Git SHA"):
            _call(fixture, catalog=backend, object_store=backend)
        assert backend.calls == ["contract"]
        assert fixture.provider.support_count == 1
        assert fixture.provider.run_count == 0
        assert fixture.bm25_owner.state == "empty"
        assert fixture.context_owner.state == "empty"
    finally:
        fixture.close()


def test_nested_cache_must_be_absent_from_retained_source_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _cache_fixture(tmp_path)
    fixture.source.close()
    included_cache = fixture.repository / "compiler-state"
    fixture.cache.rename(included_cache)
    fixture.cache = included_cache
    # Stabilize the lock file, then deliberately capture the cache as source.
    with cache_import_module.compiler_cache_lock(fixture.cache):
        pass
    fixture.source = capture_repository_source(fixture.repository)
    monkeypatch.setattr(
        cache_import_module,
        "import_retained_repo_manifest",
        lambda *args, **kwargs: pytest.fail("storage import must not run"),
    )
    try:
        with pytest.raises(StorageIntegrityError, match="source records"):
            _call(fixture)
        assert fixture.provider.run_count == 0
    finally:
        fixture.close()


def test_manifest_is_read_bounded_without_following_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _cache_fixture(tmp_path)
    manifest = fixture.cache / "repo_manifest.json"
    target = fixture.cache / "manifest-target.json"
    manifest.rename(target)
    manifest.symlink_to(target.name)
    monkeypatch.setattr(
        cache_import_module,
        "import_retained_repo_manifest",
        lambda *args, **kwargs: pytest.fail("storage import must not run"),
    )
    try:
        with pytest.raises(ValueError, match="not a private regular file"):
            _call(fixture)
        assert fixture.provider.run_count == 0
    finally:
        fixture.close()


def test_missing_cache_fails_without_creating_directory_or_lock(tmp_path: Path) -> None:
    fixture = _cache_fixture(tmp_path)
    missing_cache = tmp_path / "missing-cache"
    fixture.cache = missing_cache
    backend = _BackendTripwire()
    try:
        with pytest.raises(RuntimeError, match="open compiler cache directory"):
            _call(fixture, catalog=backend, object_store=backend)
        assert not missing_cache.exists()
        assert backend.calls == ["contract"]
        assert fixture.provider.support_count == 1
        assert fixture.provider.run_count == 0
        assert fixture.bm25_owner.state == "empty"
        assert fixture.context_owner.state == "empty"
    finally:
        fixture.close()


def test_missing_fixed_lock_fails_without_mutating_existing_cache(
    tmp_path: Path,
) -> None:
    fixture = _cache_fixture(tmp_path)
    lock_path = fixture.cache / ".index-compiler.lock"
    lock_path.unlink()
    before = tuple(
        sorted(
            path.relative_to(fixture.cache).as_posix()
            for path in fixture.cache.rglob("*")
        )
    )
    backend = _BackendTripwire()
    try:
        with pytest.raises(RuntimeError, match="lock does not exist"):
            _call(fixture, catalog=backend, object_store=backend)
        after = tuple(
            sorted(
                path.relative_to(fixture.cache).as_posix()
                for path in fixture.cache.rglob("*")
            )
        )
        assert after == before
        assert not lock_path.exists()
        assert backend.calls == ["contract"]
        assert fixture.provider.support_count == 1
        assert fixture.provider.run_count == 0
        assert fixture.bm25_owner.state == "empty"
        assert fixture.context_owner.state == "empty"
    finally:
        fixture.close()


def test_import_failure_preserves_published_evidence_and_caller_owners(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _cache_fixture(tmp_path)

    def fail_import(*args, **kwargs):
        raise RuntimeError("catalog unavailable")

    monkeypatch.setattr(
        cache_import_module,
        "import_retained_repo_manifest",
        fail_import,
    )
    try:
        with pytest.raises(RuntimeError, match="catalog unavailable"):
            _call(fixture)
        assert fixture.bm25_destination.is_dir()
        assert fixture.context_destination.is_dir()
        assert fixture.bm25_owner.active
        assert fixture.context_owner.active
        assert fixture.source.usable
    finally:
        fixture.close()


def test_postcommit_interruption_retries_as_exact_unchanged_import(
    tmp_path: Path,
) -> None:
    fixture = _cache_fixture(tmp_path)
    first_bm25_owner = fixture.bm25_owner
    first_context_owner = fixture.context_owner
    first_bm25_destination = fixture.bm25_destination
    first_context_destination = fixture.context_destination
    try:
        with (
            LocalCAS(tmp_path / "cas") as cas,
            _PostCommitInterruptCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            with pytest.raises(RuntimeError, match="postcommit interruption"):
                _call(fixture, catalog=catalog, object_store=cas)

            assert first_bm25_destination.is_dir()
            assert first_context_destination.is_dir()
            assert first_bm25_owner.active
            assert first_context_owner.active
            assert fixture.source.usable

            fixture.workspace = tmp_path / "retry-workspace"
            fixture.workspace.mkdir(mode=0o700)
            fixture.bm25_owner = PublishedWorkspaceReceiptOwner()
            fixture.context_owner = PublishedWorkspaceReceiptOwner()
            retry = _call(fixture, catalog=catalog, object_store=cas)

            assert retry.import_result.generation == 1
            assert retry.import_result.changed is False
            assert retry.import_result.snapshot_id
            assert fixture.provider.support_count == 6
            assert fixture.provider.run_count == 4
            assert first_bm25_owner.active
            assert first_context_owner.active
    finally:
        fixture.close()
        first_context_owner.close()
        first_bm25_owner.close()


def _git_commit(repository: Path, message: str) -> str:
    subprocess.run(["git", "-C", str(repository), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", message],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_real_compiler_cache_import_retry_update_and_latest_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "CodeNib Test"],
        check=True,
    )
    source_file = repository / "sample.py"
    source_file.write_text(
        'GENERATION_MARKER = "GENERATION_A_ONLY"\n',
        encoding="utf-8",
    )
    commit_a = _git_commit(repository, "generation-a")

    registry = IndexBuilderRegistry()
    registry.register(
        "bm25",
        BM25IndexBuilder(languages=["python"], max_k=17),
    )
    compiler = IndexCompiler(
        registry,
        IndexCompilerConfig(index_types=["bm25"], languages=["python"]),
    )
    cache = repository / ".codenib_cache"
    manifest_a = compiler.compile_repo(
        str(repository),
        index_types=["bm25"],
        cache_dir=str(cache),
    )
    assert manifest_a.commit == commit_a

    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    provider = _TestWorkspaceProvider()
    state: dict[str, object] = {"phase": "before", "events": []}
    real_lock = cache_import_module.compiler_cache_lock

    @contextmanager
    def tracked_lock(cache_path: Path, *, create: bool = True):
        assert create is False
        with real_lock(cache_path, create=create):
            state["phase"] = "locked"
            state["events"].append(  # type: ignore[union-attr]
                ("cache.lock.acquired", state["phase"])
            )
            try:
                yield
            finally:
                state["phase"] = "released"
                state["events"].append(  # type: ignore[union-attr]
                    ("cache.lock.released", state["phase"])
                )

    monkeypatch.setattr(cache_import_module, "compiler_cache_lock", tracked_lock)

    owners: list[PublishedWorkspaceReceiptOwner] = []
    source_a = capture_repository_source(repository, exclude_roots=(cache,))
    source_b: RepositorySourceBinding | None = None
    materialized_owner = PublishedWorkspaceReceiptOwner()
    binding = None
    context: ServerContext | None = None

    def import_generation(
        name: str,
        source: RepositorySourceBinding,
        *,
        expected_generation: int,
        catalog: SQLiteCatalog,
        cas: LocalCAS,
    ) -> CompilerCacheImportResult:
        bm25_owner = PublishedWorkspaceReceiptOwner()
        context_owner = PublishedWorkspaceReceiptOwner()
        owners.extend((bm25_owner, context_owner))
        return import_compiler_cache_bm25(
            cache,
            repository_source=source,
            bm25_output_owner=bm25_owner,
            context_output_owner=context_owner,
            bm25_destination=workspace / f"{name}-bm25",
            context_destination=workspace / f"{name}-context",
            workspace_provider=provider,
            repository_key=_REPOSITORY_KEY,
            catalog=catalog,
            object_store=cas,
            expected_generation=expected_generation,
            environ={},
        )

    try:
        with (
            _LockAwareCAS(tmp_path / "cas", state) as cas,
            _LockAwareCatalog(tmp_path / "catalog.sqlite", state) as catalog,
        ):
            first = import_generation(
                "a-first",
                source_a,
                expected_generation=0,
                catalog=catalog,
                cas=cas,
            )
            assert first.import_result.generation == 1
            assert first.import_result.changed is True
            assert state["events"][:4] == [  # type: ignore[index]
                ("catalog.retained_import_contract", "before"),
                ("cache.lock.acquired", "locked"),
                ("cache.lock.released", "released"),
                ("catalog.retained_import_contract", "released"),
            ]
            assert all(
                phase == "released"
                for event, phase in state["events"]  # type: ignore[union-attr]
                if event in {"cas.put_chunks", "catalog.create_namespace"}
            )

            retry = import_generation(
                "a-retry",
                source_a,
                expected_generation=0,
                catalog=catalog,
                cas=cas,
            )
            assert retry.import_plan.plan_digest == first.import_plan.plan_digest
            assert retry.import_result.snapshot_id == first.import_result.snapshot_id
            assert retry.import_result.generation == 1
            assert retry.import_result.changed is False

            source_a.close()
            source_file.write_text(
                'GENERATION_MARKER = "GENERATION_B_ONLY"\n',
                encoding="utf-8",
            )
            commit_b = _git_commit(repository, "generation-b")
            manifest_b = compiler.update_repo(
                str(repository),
                index_types=["bm25"],
                cache_dir=str(cache),
            )
            assert commit_b != commit_a
            assert manifest_b.commit == commit_b
            source_b = capture_repository_source(repository, exclude_roots=(cache,))

            updated = import_generation(
                "b-updated",
                source_b,
                expected_generation=1,
                catalog=catalog,
                cas=cas,
            )
            assert updated.import_result.generation == 2
            assert updated.import_result.changed is True
            assert updated.import_result.snapshot_id != first.import_result.snapshot_id

            materialized = materialize_retained_repo_manifest_ref(
                _REPOSITORY_KEY,
                workspace / "latest-materialized",
                catalog=catalog,
                object_store=cas,
                workspace_provider=provider,
                output_receipt_owner=materialized_owner,
                expected_generation=2,
                environ={},
            )
            assert materialized.export_receipt.ref_generation == 2
            assert materialized.artifact.commit == commit_b

            binding = query_context_artifact(
                materialized.artifact.output_dir,
                expected_repository=_REPOSITORY_KEY,
                expected_commit=commit_b,
            )
            context = ServerContext.load(
                binding.manifest,
                views=("bm25",),
                artifact_binding=binding,
            )
            assert context.loaded_views == frozenset({"bm25"})
            assert context.errors == {}
            assert context.bm25 is not None
            assert any(
                "generation b only" in document.page_content
                for document in context.bm25.documents
            )
            assert all(
                "generation a only" not in document.page_content
                for document in context.bm25.documents
            )
            assert context.bm25.search("GENERATION_B_ONLY", top_k=5)
    finally:
        if context is not None:
            context.close()
        if binding is not None:
            binding.close()
        materialized_owner.close()
        for owner in reversed(owners):
            owner.close()
        if source_b is not None:
            source_b.close()
        source_a.close()


def test_real_cli_import_cache_materialize_snapshot_and_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    workspace.chmod(0o700)
    provider = LocalWorkspaceProvider(workspace)
    try:
        provider.require_support()
    except UnsupportedWorkspaceCreation as error:
        pytest.skip(f"native local workspace provider is unavailable: {error}")

    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "CodeNib Test"],
        check=True,
    )
    source_file = repository / "sample.py"
    source_file.write_text(
        'CLI_E2E_MARKER = "PRODUCTION_SHAPED_CACHE_IMPORT"\n',
        encoding="utf-8",
    )
    commit = _git_commit(repository, "cli-cache-import")

    registry = IndexBuilderRegistry()
    registry.register(
        "bm25",
        BM25IndexBuilder(languages=["python"], max_k=17),
    )
    compiler = IndexCompiler(
        registry,
        IndexCompilerConfig(index_types=["bm25"], languages=["python"]),
    )
    cache = repository / ".codenib_cache"
    manifest = compiler.compile_repo(
        str(repository),
        index_types=["bm25"],
        cache_dir=str(cache),
    )
    assert manifest.commit == commit

    catalog_path = tmp_path / "catalog.sqlite"
    with SQLiteCatalog(catalog_path):
        pass
    cas_root = tmp_path / "cas"
    with LocalCAS.provision(cas_root):
        pass

    nonce = "0123456789abcdef0123456789abcdef"
    monkeypatch.setattr(cli_module, "_new_compiler_cache_import_nonce", lambda: nonce)
    parser = cli_module.build_parser()
    import_args = parser.parse_args(
        [
            "artifact",
            "import-cache",
            os.fspath(repository),
            "--cache-dir",
            os.fspath(cache),
            "--catalog",
            os.fspath(catalog_path),
            "--cas-root",
            os.fspath(cas_root),
            "--workspace-root",
            os.fspath(workspace),
            "--repository",
            _REPOSITORY_KEY,
        ]
    )
    assert import_args.handler is cli_module._run_artifact_import_cache
    assert import_args.handler(import_args) == 0

    import_output = capsys.readouterr().out
    snapshot_id = next(
        line.partition(":")[2].strip()
        for line in import_output.splitlines()
        if line.startswith("Snapshot:")
    )
    assert snapshot_id
    assert "Generation:         1" in import_output

    prefix = f".codenib-cache-import-{nonce}"
    bm25_generation = workspace / f"{prefix}-bm25"
    context_generation = workspace / f"{prefix}-context"
    suggested_output = workspace / f"{prefix}-materialized"
    materialized_output = workspace / "materialized"
    assert bm25_generation.is_dir()
    assert context_generation.is_dir()
    assert not suggested_output.exists()
    assert not materialized_output.exists()

    materialize_args = parser.parse_args(
        [
            "artifact",
            "materialize",
            "--catalog",
            os.fspath(catalog_path),
            "--cas-root",
            os.fspath(cas_root),
            "--workspace-root",
            os.fspath(workspace),
            "--repository",
            _REPOSITORY_KEY,
            "--snapshot",
            snapshot_id,
            "--output",
            os.fspath(materialized_output),
        ]
    )
    assert materialize_args.handler is cli_module._run_artifact_materialize
    assert materialize_args.handler(materialize_args) == 0

    materialize_output = capsys.readouterr().out
    assert f"Snapshot:         {snapshot_id}" in materialize_output
    assert materialized_output.is_dir()
    assert bm25_generation.is_dir()
    assert context_generation.is_dir()

    binding = query_context_artifact(
        materialized_output,
        expected_repository=_REPOSITORY_KEY,
        expected_commit=commit,
    )
    context: ServerContext | None = None
    try:
        context = ServerContext.load(
            binding.manifest,
            views=("bm25",),
            artifact_binding=binding,
        )
        assert context.errors == {}
        assert context.loaded_views == frozenset({"bm25"})
        assert context.bm25 is not None
        hits = context.bm25.search(
            "PRODUCTION_SHAPED_CACHE_IMPORT",
            top_k=5,
            return_code_content=True,
        )
        assert hits
        assert hits[0].file == "sample.py"
        assert any(
            "production shaped cache import" in document.page_content
            for document in context.bm25.documents
        )
    finally:
        if context is not None:
            context.close()
        binding.close()
