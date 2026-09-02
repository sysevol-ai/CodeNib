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
    CompilerCacheMultiViewImportResult,
    CompilerCacheViewRecaptureResult,
    compiler_cache_source_selection,
    import_compiler_cache,
    import_compiler_cache_bm25,
)
from codenib.compiler.index_builders import (
    BM25IndexBuilder,
    IndexBuilderRegistry,
    VectorIndexBuilder,
)
from codenib.compiler.index_compiler import IndexCompiler, IndexCompilerConfig
from codenib.compiler.manifest import IndexEntry, RepoManifest
from codenib.compiler.manifest_import import RepoManifestImportResult
from codenib.compiler.manifest_materialization import (
    materialize_retained_repo_manifest_ref,
)
from codenib.compiler.retained_manifest_contract import REPO_MANIFEST_PROJECTION_VIEW
from codenib.index.embedding.vector_store import CodeVectorStore
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


class _DeterministicEmbedding:
    """Small local embedding used to produce real schema-8 vector bytes."""

    def __init__(self, dimension: int) -> None:
        self.dimension = dimension

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [
            [float((index + offset) % 7 + 1) for offset in range(self.dimension)]
            for index, _text in enumerate(texts)
        ]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


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
                destination_binding=None,
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
        self.publications: list[dict[str, object]] = []
        super().__init__(path)

    def retained_import_contract(self) -> str:
        assert self._test_state["phase"] != "locked"
        self._test_state["events"].append(  # type: ignore[union-attr]
            ("catalog.retained_import_contract", self._test_state["phase"])
        )
        return super().retained_import_contract()

    def _record_data(self, name: str) -> None:
        assert self._test_state["phase"] == "released"
        self._test_state["events"].append(  # type: ignore[union-attr]
            (f"catalog.data.{name}", self._test_state["phase"])
        )

    def create_namespace(self, *args, **kwargs):
        self._record_data("create_namespace")
        return super().create_namespace(*args, **kwargs)

    def create_repository(self, *args, **kwargs):
        self._record_data("create_repository")
        return super().create_repository(*args, **kwargs)

    def create_source_revision(self, *args, **kwargs):
        self._record_data("create_source_revision")
        return super().create_source_revision(*args, **kwargs)

    def create_view_profile(self, *args, **kwargs):
        self._record_data("create_view_profile")
        return super().create_view_profile(*args, **kwargs)

    def register_object(self, *args, **kwargs):
        self._record_data("register_object")
        return super().register_object(*args, **kwargs)

    def stage_view_generation(self, *args, **kwargs):
        self._record_data("stage_view_generation")
        return super().stage_view_generation(*args, **kwargs)

    def publish_snapshot(self, *args, **kwargs):
        self._record_data("publish_snapshot")
        publication = super().publish_snapshot(*args, **kwargs)
        self.publications.append(copy.deepcopy(publication))
        return publication

    def resolve_ref(self, *args, **kwargs):
        self._record_data("resolve_ref")
        return super().resolve_ref(*args, **kwargs)

    def get_manifest_summary(self, *args, **kwargs):
        self._record_data("get_manifest_summary")
        return super().get_manifest_summary(*args, **kwargs)


class _PostCommitInterruptCatalog(SQLiteCatalog):
    def __init__(self, path: Path) -> None:
        self._interrupt_after_first_publish = True
        self.interrupted_publication: dict[str, object] | None = None
        super().__init__(path)

    def publish_snapshot(self, *args, **kwargs):
        publication = super().publish_snapshot(*args, **kwargs)
        if self._interrupt_after_first_publish:
            self._interrupt_after_first_publish = False
            self.interrupted_publication = copy.deepcopy(publication)
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


def _call_generic(
    fixture: _CacheFixture,
    *,
    catalog: object | None = None,
    object_store: object | None = None,
    **overrides,
) -> CompilerCacheMultiViewImportResult:
    if catalog is None:
        catalog = _BackendTripwire()
    if object_store is None:
        object_store = catalog
    arguments = {
        "views": ("bm25",),
        "repository_source": fixture.source,
        "view_output_owners": {"bm25": fixture.bm25_owner},
        "context_output_owner": fixture.context_owner,
        "view_destinations": {"bm25": fixture.bm25_destination},
        "context_destination": fixture.context_destination,
        "workspace_provider": fixture.provider,
        "repository_key": _REPOSITORY_KEY,
        "catalog": catalog,
        "object_store": object_store,
        "environ": {},
    }
    arguments.update(overrides)
    return import_compiler_cache(fixture.cache, **arguments)  # type: ignore[arg-type]


def test_compiler_cache_import_is_a_lazy_public_export() -> None:
    assert compiler_module.CompilerCacheImportResult is CompilerCacheImportResult
    assert (
        compiler_module.CompilerCacheMultiViewImportResult
        is CompilerCacheMultiViewImportResult
    )
    assert (
        compiler_module.CompilerCacheViewRecaptureResult
        is CompilerCacheViewRecaptureResult
    )
    assert (
        compiler_module.CompilerCacheBm25RecaptureResult
        is CompilerCacheBm25RecaptureResult
    )
    assert compiler_module.import_compiler_cache is import_compiler_cache
    assert compiler_module.import_compiler_cache_bm25 is import_compiler_cache_bm25
    assert list(inspect.signature(import_compiler_cache).parameters)[:11] == [
        "cache_dir",
        "views",
        "repository_source",
        "view_output_owners",
        "context_output_owner",
        "view_destinations",
        "context_destination",
        "workspace_provider",
        "repository_key",
        "catalog",
        "object_store",
    ]
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


def _persist_fixture_source_selection(
    fixture: _CacheFixture,
    selection: RepositorySourceSelection,
):
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
    entry.config["source_selection_digest"] = selection.digest
    entry.metadata["source_selection_digest"] = selection.digest
    manifest.save(manifest_path)
    manifest_path.chmod(0o600)
    return identity


def test_compiler_cache_source_selection_reads_exact_persisted_policy(
    tmp_path: Path,
) -> None:
    fixture = _cache_fixture(tmp_path)
    selection = RepositorySourceSelection(("generated",))
    _persist_fixture_source_selection(fixture, selection)

    observed = compiler_cache_source_selection(fixture.cache)

    assert observed == selection
    assert observed is not selection


def test_nondefault_source_selection_survives_portable_multiview_planning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _cache_fixture(tmp_path)
    selection = RepositorySourceSelection(("generated",))
    persisted_identity = _persist_fixture_source_selection(fixture, selection)
    fixture.source.close()
    fixture.source = capture_repository_source(
        fixture.repository,
        exclude_roots=(fixture.cache,),
        selection=selection,
    )
    monkeypatch.setattr(
        cache_import_module,
        "import_retained_repo_manifest",
        lambda *args, **kwargs: _fake_import_result(),
    )

    try:
        result = _call_generic(fixture)

        assert fixture.source.fingerprint == persisted_identity.value
        assert result.import_plan.manifest.source_selection == selection
        assert result.import_plan.source.source_selection == selection
        entry = result.import_plan.manifest.indexes["bm25"]
        assert entry.source_selection_digest == selection.digest
        assert entry.config["source_selection_digest"] == selection.digest
        assert (
            RepoManifest.load(result.context_artifact.manifest_path).source_selection
            == selection
        )
    finally:
        fixture.close()


@pytest.mark.parametrize(
    ("mismatch", "error_type", "message"),
    [
        ("binding", StorageIntegrityError, "source selection differs"),
        ("config", ValueError, "no exact current bm25"),
    ],
)
def test_source_selection_mismatch_fails_before_workspace_or_storage_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
    error_type: type[Exception],
    message: str,
) -> None:
    fixture = _cache_fixture(tmp_path)
    selection = RepositorySourceSelection(("generated",))
    if mismatch == "binding":
        # Exercise the independent identity axis after the byte fingerprint was
        # captured; the coordinator must not authorize a selection mismatch
        # merely because the retained bytes still match the manifest.
        fixture.source._source_selection_identity = selection  # type: ignore[attr-defined]
    else:
        manifest_path = fixture.cache / "repo_manifest.json"
        manifest = RepoManifest.load(manifest_path)
        manifest.indexes["bm25"].config["source_selection_digest"] = selection.digest
        manifest.save(manifest_path)
        manifest_path.chmod(0o600)

    backend = _BackendTripwire()
    planned = False

    def unexpected_plan(*args, **kwargs):
        nonlocal planned
        planned = True
        raise AssertionError("cache view planning must not run")

    monkeypatch.setattr(cache_import_module, "_plan_cache_view", unexpected_plan)
    monkeypatch.setattr(
        cache_import_module,
        "import_retained_repo_manifest",
        lambda *args, **kwargs: pytest.fail("storage import must not run"),
    )
    try:
        with pytest.raises(error_type, match=message):
            _call_generic(fixture, catalog=backend, object_store=backend)
        assert not planned
        assert backend.calls == ["contract"]
        assert fixture.provider.support_count == 1
        assert fixture.provider.run_count == 0
        assert fixture.bm25_owner.state == "empty"
        assert fixture.context_owner.state == "empty"
    finally:
        fixture.close()


def test_generic_bm25_import_returns_ordered_detached_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _cache_fixture(tmp_path)
    monkeypatch.setattr(
        cache_import_module,
        "import_retained_repo_manifest",
        lambda *args, **kwargs: _fake_import_result(),
    )
    try:
        result = _call_generic(fixture)

        assert type(result) is CompilerCacheMultiViewImportResult
        assert result.views == ("bm25",)
        assert tuple(result.recapture_map) == ("bm25",)
        recapture = result.recaptures[0]
        assert type(recapture) is CompilerCacheViewRecaptureResult
        assert recapture.view_type == "bm25"
        assert recapture.source_view == fixture.cache / "bm25"
        assert recapture.output_view == fixture.bm25_destination
        assert recapture.output_file_fingerprints == {
            record.path: {"size": record.size, "sha256": record.sha256}
            for record in recapture.output_records
        }
        assert result.context_artifact.manifest_path.read_bytes() == (
            result.canonical_manifest_bytes
        )
        assert result.import_plan.selection.selected_views == ("bm25",)
        assert result.import_result == _fake_import_result()
    finally:
        fixture.close()


def test_generic_import_rejects_nonexact_or_misaligned_view_mappings(
    tmp_path: Path,
) -> None:
    fixture = _cache_fixture(tmp_path)
    backend = _BackendTripwire()

    class DictSubclass(dict):
        pass

    extra_owner = PublishedWorkspaceReceiptOwner()
    cases = (
        (
            {"views": ["bm25"]},
            (TypeError, "exact tuple"),
        ),
        (
            {"views": ("vector", "bm25")},
            (ValueError, "canonical portable subset"),
        ),
        (
            {"view_output_owners": DictSubclass({"bm25": fixture.bm25_owner})},
            (TypeError, "exact dict"),
        ),
        (
            {
                "view_destinations": {
                    "bm25": fixture.bm25_destination,
                    "vector": fixture.workspace / "vector",
                }
            },
            (ValueError, "exact selected keys and order"),
        ),
        (
            {
                "views": ("bm25", "vector"),
                "view_output_owners": {
                    "vector": extra_owner,
                    "bm25": fixture.bm25_owner,
                },
                "view_destinations": {
                    "bm25": fixture.bm25_destination,
                    "vector": fixture.workspace / "vector",
                },
            },
            (ValueError, "exact selected keys and order"),
        ),
        (
            {
                "views": ("bm25", "vector"),
                "view_output_owners": {
                    "bm25": fixture.bm25_owner,
                    "vector": fixture.bm25_owner,
                },
                "view_destinations": {
                    "bm25": fixture.bm25_destination,
                    "vector": fixture.workspace / "vector",
                },
            },
            (ValueError, "distinct receipt owners"),
        ),
        (
            {"view_output_owners": {"bm25": fixture.context_owner}},
            (ValueError, "distinct receipt owners"),
        ),
        (
            {"view_destinations": {"bm25": str(fixture.bm25_destination)}},
            (TypeError, "exact paths"),
        ),
    )
    try:
        for overrides, (error_type, message) in cases:
            with pytest.raises(error_type, match=message):
                _call_generic(
                    fixture,
                    catalog=backend,
                    object_store=backend,
                    **overrides,
                )
        assert backend.calls == []
        assert fixture.provider.support_count == 0
        assert fixture.provider.run_count == 0
        assert fixture.bm25_owner.state == "empty"
        assert fixture.context_owner.state == "empty"
        assert extra_owner.state == "empty"
    finally:
        extra_owner.close()
        fixture.close()


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
    real_context_plan = cache_import_module.plan_context_artifact_strict
    real_context_publish = cache_import_module.publish_planned_context_artifact_strict
    context_planned = {"value": False}

    def tracked_publish(*args, **kwargs):
        assert planned_before_publish["value"]
        return real_publish(*args, **kwargs)

    def tracked_context_plan(*args, **kwargs):
        assert lock_state["held"]
        assert fixture.bm25_owner.active
        assert fixture.context_owner.state == "empty"
        context_planned["value"] = True
        return real_context_plan(*args, **kwargs)

    def tracked_context_publish(*args, **kwargs):
        assert lock_state["held"]
        assert context_planned["value"]
        return real_context_publish(*args, **kwargs)

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
        "plan_context_artifact_strict",
        tracked_context_plan,
    )
    monkeypatch.setattr(
        cache_import_module,
        "publish_planned_context_artifact_strict",
        tracked_context_publish,
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

            assert catalog.interrupted_publication is not None
            assert catalog.interrupted_publication["changed"] is True
            assert retry.import_result.generation == 1
            assert retry.import_result.changed is False
            assert (
                retry.import_result.snapshot_id
                == catalog.interrupted_publication["snapshot_id"]
            )
            assert (
                retry.import_result.generation
                == catalog.interrupted_publication["generation"]
            )
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


@dataclass(frozen=True)
class _MultiViewCompilerFixture:
    repository: Path
    source_file: Path
    cache: Path
    compiler: IndexCompiler
    commit: str


def _multiview_compiler_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    marker: str,
) -> _MultiViewCompilerFixture:
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
        f"MULTIVIEW_CACHE_MARKER = {marker!r}\n",
        encoding="utf-8",
    )
    commit = _git_commit(repository, f"multiview-{marker.lower()}")

    vector_builder = VectorIndexBuilder(
        languages=["python"],
        embedding_model="test/model",
        embedding_dimension=4,
        build_levels=["l2"],
    )

    def build_vector(**kwargs):
        assert kwargs["artifact_metadata"]["builder_schema"] == 8
        assert kwargs["_atomic_publish"] is False
        store = CodeVectorStore(
            embedding_model=kwargs["embedding_model"],
            embedding_provider=kwargs["embedding_provider"],
            dimension=kwargs["embedding_dimension"],
            index_metric=kwargs["index_metric"],
            store_path=kwargs["index_path"],
            embedding=_DeterministicEmbedding(kwargs["embedding_dimension"]),
            artifact_metadata=kwargs["artifact_metadata"],
            **kwargs["embedding_kwargs"],
        )
        store.add_code_chunks(
            [
                {
                    "content": source_file.read_text(encoding="utf-8"),
                    "chunk_type": "file",
                    "name": "sample",
                    "file": "sample.py",
                    "start_line": 0,
                    "end_line": 0,
                    "node_id": "sample.py",
                }
            ],
            level="l2",
        )
        store.save(kwargs["index_path"])
        return store

    monkeypatch.setattr(
        "codenib.index.embedding.builders.build_hierarchical_vector_store",
        build_vector,
    )
    registry = IndexBuilderRegistry()
    registry.register(
        "bm25",
        BM25IndexBuilder(languages=["python"], max_k=17),
    )
    registry.register("vector", vector_builder)
    compiler = IndexCompiler(
        registry,
        IndexCompilerConfig(
            index_types=["bm25", "vector"],
            languages=["python"],
        ),
    )
    cache = repository / ".codenib_cache"
    manifest = compiler.compile_repo(
        str(repository),
        index_types=["bm25", "vector"],
        cache_dir=str(cache),
    )
    assert manifest.commit == commit
    assert manifest.indexes["vector"].config["builder_schema"] == 8
    assert (cache / "vector/l2/documents_test__model.json").is_file()
    assert not (cache / "vector/l2/documents_test__model.pkl").exists()
    return _MultiViewCompilerFixture(
        repository=repository,
        source_file=source_file,
        cache=cache,
        compiler=compiler,
        commit=commit,
    )


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
                if event.startswith(("cas.", "catalog.data."))
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


def test_real_multiview_compiler_cache_imports_one_context_and_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled = _multiview_compiler_fixture(
        tmp_path,
        monkeypatch,
        marker="GENERATION_A_ONLY",
    )
    repository = compiled.repository
    source_file = compiled.source_file
    cache = compiled.cache
    compiler = compiled.compiler
    commit_a = compiled.commit
    source_a = capture_repository_source(repository, exclude_roots=(cache,))
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    provider = _TestWorkspaceProvider()
    retained_owners: list[PublishedWorkspaceReceiptOwner] = []
    materialized_owner = PublishedWorkspaceReceiptOwner()
    source_b: RepositorySourceBinding | None = None
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
    events: list[str] = []
    real_plan_view = cache_import_module._plan_cache_view
    real_import_plan = cache_import_module.plan_repo_manifest_import_bytes
    real_publish_view = cache_import_module._publish_cache_view
    real_context_plan = cache_import_module.plan_context_artifact_strict
    real_context_publish = cache_import_module.publish_planned_context_artifact_strict

    def tracked_plan_view(view, *args, **kwargs):
        events.append(f"plan:{view}")
        return real_plan_view(view, *args, **kwargs)

    def tracked_import_plan(*args, **kwargs):
        events.append("plan:retained")
        return real_import_plan(*args, **kwargs)

    def tracked_publish_view(view, *args, **kwargs):
        expected = ["plan:bm25", "plan:vector", "plan:retained"]
        if view == "vector":
            expected.append("publish:bm25")
        assert events == expected
        events.append(f"publish:{view}")
        return real_publish_view(view, *args, **kwargs)

    def tracked_context_plan(*args, **kwargs):
        assert events == [
            "plan:bm25",
            "plan:vector",
            "plan:retained",
            "publish:bm25",
            "publish:vector",
        ]
        events.append("plan:context")
        return real_context_plan(*args, **kwargs)

    def tracked_context_publish(*args, **kwargs):
        assert events[-1] == "plan:context"
        events.append("publish:context")
        return real_context_publish(*args, **kwargs)

    monkeypatch.setattr(cache_import_module, "_plan_cache_view", tracked_plan_view)
    monkeypatch.setattr(
        cache_import_module,
        "plan_repo_manifest_import_bytes",
        tracked_import_plan,
    )
    monkeypatch.setattr(
        cache_import_module,
        "_publish_cache_view",
        tracked_publish_view,
    )
    monkeypatch.setattr(
        cache_import_module,
        "plan_context_artifact_strict",
        tracked_context_plan,
    )
    monkeypatch.setattr(
        cache_import_module,
        "publish_planned_context_artifact_strict",
        tracked_context_publish,
    )

    expected_events = [
        "plan:bm25",
        "plan:vector",
        "plan:retained",
        "publish:bm25",
        "publish:vector",
        "plan:context",
        "publish:context",
    ]

    def import_generation(
        name: str,
        source: RepositorySourceBinding,
        *,
        expected_generation: int,
        catalog: SQLiteCatalog,
        cas: LocalCAS,
    ) -> CompilerCacheMultiViewImportResult:
        view_owners = {
            "bm25": PublishedWorkspaceReceiptOwner(),
            "vector": PublishedWorkspaceReceiptOwner(),
        }
        context_owner = PublishedWorkspaceReceiptOwner()
        retained_owners.extend((*view_owners.values(), context_owner))
        events.clear()
        result = import_compiler_cache(
            cache,
            views=("bm25", "vector"),
            repository_source=source,
            view_output_owners=view_owners,
            context_output_owner=context_owner,
            view_destinations={
                "bm25": workspace / f"{name}-bm25",
                "vector": workspace / f"{name}-vector",
            },
            context_destination=workspace / f"{name}-context",
            workspace_provider=provider,
            repository_key=_REPOSITORY_KEY,
            catalog=catalog,
            object_store=cas,
            expected_generation=expected_generation,
            environ={},
        )
        assert events == expected_events
        return result

    binding = None
    context: ServerContext | None = None
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
            assert first.views == ("bm25", "vector")
            assert tuple(first.recapture_map) == ("bm25", "vector")
            assert first.import_plan.selection.selected_views == (
                "bm25",
                "vector",
            )
            assert first.context_artifact.views == ("bm25", "vector")
            assert first.import_result.views == ("bm25", "vector")
            assert first.import_result.generation == 1
            assert first.import_result.changed is True
            assert tuple(
                view for view, _generation in first.import_result.view_generation_items
            ) == ("bm25", REPO_MANIFEST_PROJECTION_VIEW, "vector")
            assert provider.support_count == 4
            assert provider.run_count == 3
            first_view_generations = {
                view: generation
                for view, generation in first.import_result.view_generation_items
                if view in first.views
            }

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
            assert {
                view: generation
                for view, generation in retry.import_result.view_generation_items
                if view in retry.views
            } == first_view_generations

            source_a.close()
            source_file.write_text(
                'MULTIVIEW_CACHE_MARKER = "GENERATION_B_ONLY"\n',
                encoding="utf-8",
            )
            commit_b = _git_commit(repository, "multiview-generation-b")
            manifest_b = compiler.update_repo(
                str(repository),
                index_types=["bm25", "vector"],
                cache_dir=str(cache),
            )
            assert commit_b != commit_a
            assert manifest_b.commit == commit_b
            assert manifest_b.indexes["vector"].config["builder_schema"] == 8
            source_b = capture_repository_source(
                repository,
                exclude_roots=(cache,),
            )

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
            updated_view_generations = {
                view: generation
                for view, generation in updated.import_result.view_generation_items
                if view in updated.views
            }
            assert tuple(updated_view_generations) == ("bm25", "vector")
            assert all(
                updated_view_generations[view] != first_view_generations[view]
                for view in ("bm25", "vector")
            )
            assert [
                (publication["generation"], publication["changed"])
                for publication in catalog.publications
            ] == [(1, True), (1, False), (2, True)]
            assert provider.support_count == 12
            assert provider.run_count == 9
            lock_events = [
                event
                for event, _phase in state["events"]  # type: ignore[union-attr]
                if event.startswith("cache.lock.")
            ]
            assert (
                lock_events
                == [
                    "cache.lock.acquired",
                    "cache.lock.released",
                ]
                * 3
            )
            assert all(
                phase == "released"
                for event, phase in state["events"]  # type: ignore[union-attr]
                if event.startswith(("cas.", "catalog.data."))
            )

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
            assert materialized.artifact.views == ("bm25", "vector")
            binding = query_context_artifact(
                materialized.artifact.output_dir,
                expected_repository=_REPOSITORY_KEY,
                expected_commit=commit_b,
            )
            assert tuple(sorted(binding.manifest.indexes)) == ("bm25", "vector")
            context = ServerContext.load(
                binding.manifest,
                views=("bm25",),
                artifact_binding=binding,
            )
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
        for owner in reversed(retained_owners):
            owner.close()
        if source_b is not None:
            source_b.close()
        source_a.close()


def test_real_vector_only_cache_import_materializes_exact_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled = _multiview_compiler_fixture(
        tmp_path,
        monkeypatch,
        marker="VECTOR_ONLY",
    )
    source = capture_repository_source(
        compiled.repository,
        exclude_roots=(compiled.cache,),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    provider = _TestWorkspaceProvider()
    vector_owner = PublishedWorkspaceReceiptOwner()
    context_owner = PublishedWorkspaceReceiptOwner()
    materialized_owner = PublishedWorkspaceReceiptOwner()
    binding = None
    vector_destination = workspace / "vector-only-vector"
    context_destination = workspace / "vector-only-context"
    materialized_destination = workspace / "vector-only-materialized"
    try:
        with (
            LocalCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            result = import_compiler_cache(
                compiled.cache,
                views=("vector",),
                repository_source=source,
                view_output_owners={"vector": vector_owner},
                context_output_owner=context_owner,
                view_destinations={"vector": vector_destination},
                context_destination=context_destination,
                workspace_provider=provider,
                repository_key=_REPOSITORY_KEY,
                catalog=catalog,
                object_store=cas,
                expected_generation=0,
                environ={},
            )

            assert result.views == ("vector",)
            assert tuple(result.recapture_map) == ("vector",)
            assert tuple(result.import_plan.manifest.indexes) == ("vector",)
            assert result.import_plan.manifest.capabilities == {
                "sparse_search": False,
                "dense_search": True,
                "hybrid_search": False,
                "symbol_navigation": False,
            }
            assert result.context_artifact.views == ("vector",)
            assert result.context_artifact.output_dir == context_destination
            assert result.import_result.views == ("vector",)
            assert result.import_result.generation == 1
            assert result.import_result.changed is True
            assert tuple(
                view for view, _generation in result.import_result.view_generation_items
            ) == (REPO_MANIFEST_PROJECTION_VIEW, "vector")
            resolved = catalog.resolve_ref(result.import_result.repository_id)
            assert resolved["ref_name"] == "main"
            assert resolved["snapshot_id"] == result.import_result.snapshot_id
            assert resolved["generation"] == 1
            assert vector_destination.is_dir()
            assert context_destination.is_dir()
            assert not (workspace / "vector-only-bm25").exists()

            materialized = materialize_retained_repo_manifest_ref(
                _REPOSITORY_KEY,
                materialized_destination,
                catalog=catalog,
                object_store=cas,
                workspace_provider=provider,
                output_receipt_owner=materialized_owner,
                expected_generation=1,
                environ={},
            )
            assert materialized.export_receipt.snapshot_id == (
                result.import_result.snapshot_id
            )
            assert materialized.artifact.views == ("vector",)
            assert (materialized_destination / "views/vector").is_dir()
            assert not (materialized_destination / "views/bm25").exists()
            binding = query_context_artifact(
                materialized_destination,
                expected_repository=_REPOSITORY_KEY,
                expected_commit=compiled.commit,
            )
            assert tuple(binding.manifest.indexes) == ("vector",)
            assert binding.manifest.capabilities == {
                "sparse_search": False,
                "dense_search": True,
                "hybrid_search": False,
                "symbol_navigation": False,
            }
            assert provider.run_count == 3
    finally:
        if binding is not None:
            binding.close()
        materialized_owner.close()
        context_owner.close()
        vector_owner.close()
        source.close()


def test_multiview_postcommit_interruption_retries_with_fresh_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled = _multiview_compiler_fixture(
        tmp_path,
        monkeypatch,
        marker="POSTCOMMIT",
    )
    source = capture_repository_source(
        compiled.repository,
        exclude_roots=(compiled.cache,),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    provider = _TestWorkspaceProvider()
    first_owners = {
        "bm25": PublishedWorkspaceReceiptOwner(),
        "vector": PublishedWorkspaceReceiptOwner(),
    }
    first_context_owner = PublishedWorkspaceReceiptOwner()
    retry_owners = {
        "bm25": PublishedWorkspaceReceiptOwner(),
        "vector": PublishedWorkspaceReceiptOwner(),
    }
    retry_context_owner = PublishedWorkspaceReceiptOwner()
    first_destinations = {
        "bm25": workspace / "first-bm25",
        "vector": workspace / "first-vector",
    }
    first_context_destination = workspace / "first-context"
    retry_destinations = {
        "bm25": workspace / "retry-bm25",
        "vector": workspace / "retry-vector",
    }
    retry_context_destination = workspace / "retry-context"
    try:
        with (
            LocalCAS(tmp_path / "cas") as cas,
            _PostCommitInterruptCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            with pytest.raises(RuntimeError, match="postcommit interruption"):
                import_compiler_cache(
                    compiled.cache,
                    views=("bm25", "vector"),
                    repository_source=source,
                    view_output_owners=first_owners,
                    context_output_owner=first_context_owner,
                    view_destinations=first_destinations,
                    context_destination=first_context_destination,
                    workspace_provider=provider,
                    repository_key=_REPOSITORY_KEY,
                    catalog=catalog,
                    object_store=cas,
                    expected_generation=0,
                    environ={},
                )

            assert all(path.is_dir() for path in first_destinations.values())
            assert first_context_destination.is_dir()
            assert all(owner.active for owner in first_owners.values())
            assert first_context_owner.active
            assert all(not path.exists() for path in retry_destinations.values())
            assert not retry_context_destination.exists()

            retry = import_compiler_cache(
                compiled.cache,
                views=("bm25", "vector"),
                repository_source=source,
                view_output_owners=retry_owners,
                context_output_owner=retry_context_owner,
                view_destinations=retry_destinations,
                context_destination=retry_context_destination,
                workspace_provider=provider,
                repository_key=_REPOSITORY_KEY,
                catalog=catalog,
                object_store=cas,
                expected_generation=0,
                environ={},
            )

            assert catalog.interrupted_publication is not None
            assert catalog.interrupted_publication["changed"] is True
            assert retry.import_result.generation == 1
            assert retry.import_result.changed is False
            assert (
                retry.import_result.snapshot_id
                == catalog.interrupted_publication["snapshot_id"]
            )
            assert (
                retry.import_result.generation
                == catalog.interrupted_publication["generation"]
            )
            assert retry.import_result.views == ("bm25", "vector")
            assert tuple(
                view for view, _generation in retry.import_result.view_generation_items
            ) == ("bm25", REPO_MANIFEST_PROJECTION_VIEW, "vector")
            assert all(path.is_dir() for path in retry_destinations.values())
            assert retry_context_destination.is_dir()
            assert all(owner.active for owner in retry_owners.values())
            assert retry_context_owner.active
            assert all(owner.active for owner in first_owners.values())
            assert first_context_owner.active
            assert provider.support_count == 8
            assert provider.run_count == 6
    finally:
        retry_context_owner.close()
        for owner in reversed(tuple(retry_owners.values())):
            owner.close()
        first_context_owner.close()
        for owner in reversed(tuple(first_owners.values())):
            owner.close()
        source.close()


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

    compiled = _multiview_compiler_fixture(
        tmp_path,
        monkeypatch,
        marker="PRODUCTION_SHAPED_CACHE_IMPORT",
    )
    repository = compiled.repository
    cache = compiled.cache
    commit = compiled.commit

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
            "--view",
            "vector,bm25",
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
    assert "Views:              bm25,vector" in import_output

    prefix = f".codenib-cache-import-{nonce}"
    bm25_generation = workspace / f"{prefix}-bm25"
    vector_generation = workspace / f"{prefix}-vector"
    context_generation = workspace / f"{prefix}-context"
    suggested_output = workspace / f"{prefix}-materialized"
    materialized_output = workspace / "materialized"
    assert bm25_generation.is_dir()
    assert vector_generation.is_dir()
    assert (vector_generation / "l2/documents_test__model.json").is_file()
    assert not (vector_generation / "l2/documents_test__model.pkl").exists()
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
    assert "Views:            bm25, vector" in materialize_output
    assert materialized_output.is_dir()
    assert (materialized_output / "views/bm25").is_dir()
    assert (materialized_output / "views/vector").is_dir()
    assert bm25_generation.is_dir()
    assert vector_generation.is_dir()
    assert context_generation.is_dir()

    binding = query_context_artifact(
        materialized_output,
        expected_repository=_REPOSITORY_KEY,
        expected_commit=commit,
    )
    context: ServerContext | None = None
    try:
        assert tuple(sorted(binding.manifest.indexes)) == ("bm25", "vector")
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
