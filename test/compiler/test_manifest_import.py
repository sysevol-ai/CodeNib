# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import inspect
import json
import os
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar

import pytest

import codenib.compiler as compiler_module
import codenib.compiler.manifest_import as manifest_import_module
from codenib._captured_directory import (
    OwnedWorkspaceAuthority,
    PublishedWorkspaceReceiptOwner,
    WorkspaceDirectory,
    WorkspaceFile,
    WorkspacePlan,
)
from codenib._workspace_provider import (
    StrictWorkspaceRequest,
    StrictWorkspaceSession,
    run_adopted_workspace_operation,
    run_strict_workspace,
)
from codenib.artifacts import stage_context_artifact_strict
from codenib.compiler.index_builders import BM25IndexBuilder, VectorIndexBuilder
from codenib.compiler.manifest import (
    LEGACY_MANIFEST_VERSION,
    MANIFEST_VERSION,
    IndexEntry,
    RepoManifest,
)
from codenib.compiler.manifest_import import (
    DEFAULT_MAX_CONTEXT_BYTES,
    DEFAULT_MAX_CONTEXT_FILES,
    DEFAULT_MAX_PROJECTION_BYTES,
    DEFAULT_NAMESPACE_NAME,
    DEFAULT_REF_NAME,
    REPO_MANIFEST_IMPORT_GENERATION_CONTRACT,
    REPO_MANIFEST_PROJECTION_MEDIA_TYPE,
    REPO_MANIFEST_PROJECTION_PROFILE_NAME,
    REPO_MANIFEST_PROJECTION_SCHEMA,
    REPO_MANIFEST_PROJECTION_VIEW,
    RepoManifestImportResult,
    import_retained_repo_manifest,
)
from codenib.compiler.manifest_storage import (
    RepoManifestImportPlan,
    plan_repo_manifest_import,
    plan_repo_manifest_import_bytes,
)
from codenib.index.embedding.artifact_integrity import VECTOR_PERSISTENCE_SCHEMA
from codenib.repository_source_selection import RepositorySourceSelection
from codenib.source_fingerprint import (
    RepositorySourceBinding,
    _capture_repository_source_legacy_manifest_v11,
    capture_repository_source,
)
from codenib.storage import (
    RETAINED_IMPORT_CATALOG_CONTRACT,
    BlobInfo,
    LocalCAS,
    NamespaceIdentity,
    ObjectRecord,
    PublishConflict,
    PublishedSnapshot,
    RepositoryIdentity,
    SnapshotView,
    SourceRevision,
    SQLiteCatalog,
    StorageIntegrityError,
    StorageValidationError,
    ViewGeneration,
    ViewProfile,
)

_Result = TypeVar("_Result")
_COMMIT = "a" * 40
_REPOSITORY_KEY = "owner/repo"
_DEFAULT_SOURCE_SELECTION = RepositorySourceSelection()


def _json_bytes(value: Any) -> bytes:
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


def _fingerprint(name: str, payload: bytes) -> dict[str, object]:
    return {
        "file": name,
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


class _TestWorkspaceProvider:
    """Quiescent provisioner used to exercise the real authority layer."""

    def __init__(self) -> None:
        self.run_count = 0

    def require_support(self) -> None:
        pass

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
        stage = parent / f".{request.destination.name}.import-{id(self):x}"
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


def _publish_generation(
    destination: Path,
    files: Mapping[str, bytes],
) -> PublishedWorkspaceReceiptOwner:
    directories = {
        parent.as_posix()
        for relative in files
        for parent in PurePosixPath(relative).parents
        if parent != PurePosixPath(".")
    }
    identity = [
        (relative, len(payload), hashlib.sha256(payload).hexdigest())
        for relative, payload in sorted(files.items())
    ]
    plan = WorkspacePlan(
        subject_digest=hashlib.sha256(_json_bytes(identity)).hexdigest(),
        directories=tuple(
            WorkspaceDirectory(PurePosixPath(path)) for path in sorted(directories)
        ),
        files=tuple(
            WorkspaceFile(PurePosixPath(relative), max_bytes=len(payload))
            for relative, payload in sorted(files.items())
        ),
    )
    request = StrictWorkspaceRequest(
        purpose="manifest-import-test-source",
        destination=destination,
        plan=plan,
    )
    owner = PublishedWorkspaceReceiptOwner()

    def publish(session: StrictWorkspaceSession) -> None:
        for relative, payload in sorted(files.items()):
            session.write_file(relative, (payload,))
        session.publish_validated(lambda _reader: None)

    run_strict_workspace(
        _TestWorkspaceProvider(),
        request,
        receipt_owner=owner,
        operation=publish,
    )
    return owner


def _bm25_files(source_text: str) -> dict[str, bytes]:
    return {
        "bm25_metadata.json": _json_bytes(
            {"project_root": "source", "max_k": 17, "language": "english"}
        ),
        "documents.json": _json_bytes(
            [
                {
                    "page_content": source_text.strip(),
                    "metadata": {"file": "sample.py", "node_id": "sample.VALUE"},
                }
            ]
        ),
    }


def _vector_identity(
    source_selection: RepositorySourceSelection = _DEFAULT_SOURCE_SELECTION,
    *,
    manifest_version: str = MANIFEST_VERSION,
) -> dict[str, Any]:
    identity = VectorIndexBuilder(
        languages=["python"],
        embedding_model="test/model",
        embedding_dimension=4,
        build_levels=["l2"],
        max_lines_per_chunk=300,
        source_selection=source_selection,
    ).artifact_identity()
    if manifest_version == LEGACY_MANIFEST_VERSION:
        identity.pop("source_selection_digest")
        identity["repository_filter_policy"] = 3
    # This fixture deliberately exercises compatibility with an already-retained
    # schema-7 portable generation.  Current raw compiler caches use schema 8 and
    # must carry the ordered JSON row-mapping receipt.
    identity["builder_schema"] = 7
    return identity


def _vector_files(
    source_text: str,
    source_selection: RepositorySourceSelection = _DEFAULT_SOURCE_SELECTION,
    *,
    manifest_version: str = MANIFEST_VERSION,
) -> dict[str, bytes]:
    documents_name = "documents_test__model.json"
    index_name = "index_test__model.faiss"
    documents = _json_bytes(
        [
            {
                "page_content": source_text.strip(),
                "metadata": {"file": "sample.py", "node_id": "sample.VALUE"},
            }
        ]
    )
    # Import authenticates this native artifact as inert bytes; it never loads FAISS.
    index = b"not-loaded-as-native-faiss\x00\x01"
    config = _json_bytes(
        {
            "embedding_model": "test/model",
            "embedding_provider": "huggingface",
            "dimension": 4,
            "index_type": "flat",
            "index_metric": "ip",
            "l0_documents": 0,
            "l2_documents": 1,
            "persistence_schema": VECTOR_PERSISTENCE_SCHEMA,
            "artifact": _vector_identity(
                source_selection,
                manifest_version=manifest_version,
            ),
            "level_artifacts": {
                "l2": {
                    "documents": _fingerprint(documents_name, documents),
                    "index": _fingerprint(index_name, index),
                }
            },
        }
    )
    return {
        "config_test__model.json": config,
        f"l2/{documents_name}": documents,
        f"l2/{index_name}": index,
    }


def _entry(
    view: str,
    *,
    source_fingerprint: str,
    source_selection_digest: str = "",
    source_selection: RepositorySourceSelection = _DEFAULT_SOURCE_SELECTION,
    manifest_version: str = MANIFEST_VERSION,
) -> IndexEntry:
    if view == "bm25":
        config = BM25IndexBuilder(
            languages=["python"],
            max_k=17,
            max_lines_per_chunk=300,
            source_selection=source_selection,
        ).artifact_identity()
    else:
        config = _vector_identity(
            source_selection,
            manifest_version=manifest_version,
        )
    if manifest_version == LEGACY_MANIFEST_VERSION:
        config.pop("source_selection_digest", None)
        config["repository_filter_policy"] = 3
    entry = IndexEntry(
        index_type=view,
        path=f"state/{view}",
        built_at="2026-01-01T00:00:00Z",
        built_at_epoch=1.0,
        status="fresh",
        config=config,
        metadata={},
        commit=_COMMIT,
        source_fingerprint=source_fingerprint,
        source_selection_digest=source_selection_digest,
    )
    if view == "vector":
        for field, value in (
            ("document_count", {"l2": 1}),
            ("last_commit", _COMMIT),
        ):
            entry.config[field] = value
            entry.metadata[field] = value
    return entry


@dataclass
class _ContextFixture:
    repository: Path
    repository_source: RepositorySourceBinding
    context: Path
    context_owner: PublishedWorkspaceReceiptOwner
    generation_owners: tuple[PublishedWorkspaceReceiptOwner, ...]

    def plan(self, views: Iterable[str] | None = None) -> RepoManifestImportPlan:
        selected = None if views is None else tuple(views)
        return plan_repo_manifest_import_bytes(
            (self.context / "repo_manifest.json").read_bytes(),
            views=selected,
        )

    def close(self) -> None:
        self.context_owner.close()
        for owner in self.generation_owners:
            owner.close()
        self.repository_source.close()


def _context_fixture(
    root: Path,
    views: tuple[str, ...],
    *,
    source_text: str = "VALUE = 1\n",
    source_selection: RepositorySourceSelection = _DEFAULT_SOURCE_SELECTION,
    manifest_version: str = MANIFEST_VERSION,
) -> _ContextFixture:
    repository = root / "repo"
    repository.mkdir(parents=True)
    (repository / "sample.py").write_text(source_text, encoding="utf-8")
    repository_source = (
        _capture_repository_source_legacy_manifest_v11(repository)
        if manifest_version == LEGACY_MANIFEST_VERSION
        else capture_repository_source(repository, selection=source_selection)
    )
    captured_selection = (
        repository_source.authenticated_identity_snapshot().source_selection
    )
    expected_selection = (
        None if manifest_version == LEGACY_MANIFEST_VERSION else source_selection
    )
    assert captured_selection == expected_selection
    owners: dict[str, PublishedWorkspaceReceiptOwner] = {}
    context_owner = PublishedWorkspaceReceiptOwner()
    try:
        for view in views:
            files = (
                _bm25_files(source_text)
                if view == "bm25"
                else _vector_files(
                    source_text,
                    source_selection,
                    manifest_version=manifest_version,
                )
            )
            owners[view] = _publish_generation(root / "state" / view, files)
        manifest = RepoManifest(
            version=manifest_version,
            repo_path=str(repository),
            commit=_COMMIT,
            last_indexed_commit=_COMMIT,
            source_fingerprint=repository_source.fingerprint,
            last_indexed_source_fingerprint=repository_source.fingerprint,
            source_selection=expected_selection,
            last_indexed_source_selection_digest=(
                "" if expected_selection is None else expected_selection.digest
            ),
            languages=["python"],
            file_count=repository_source.file_count,
            indexes={
                view: _entry(
                    view,
                    source_fingerprint=repository_source.fingerprint,
                    source_selection_digest=(
                        "" if expected_selection is None else expected_selection.digest
                    ),
                    source_selection=source_selection,
                    manifest_version=manifest_version,
                )
                for view in views
            },
            compiled_at="2026-01-01T00:00:00Z",
            compiled_at_epoch=1.0,
        )
        manifest.derive_capabilities()
        manifest.capabilities["future_query"] = True
        context = root / "published" / "context"
        stage_context_artifact_strict(
            context,
            manifest=manifest,
            repository=_REPOSITORY_KEY,
            repository_source=repository_source,
            view_generations=owners,
            workspace_provider=_TestWorkspaceProvider(),
            output_receipt_owner=context_owner,
            environ={},
        )
        return _ContextFixture(
            repository=repository,
            repository_source=repository_source,
            context=context,
            context_owner=context_owner,
            generation_owners=tuple(owners.values()),
        )
    except BaseException:
        if context_owner.active:
            context_owner.close()
        for owner in owners.values():
            owner.close()
        repository_source.close()
        raise


class _InstrumentedCAS(LocalCAS):
    def __init__(self, root: Path, state: dict[str, Any]) -> None:
        super().__init__(root)
        self.state = state
        self.put_chunks_count = 0
        self.receipt_counts: Counter[str] = Counter()
        self.fail_after_catalog = False

    def put_chunks(
        self,
        chunks: Iterable[bytes],
        expected_digest: str,
        expected_size: int,
    ) -> BlobInfo:
        if self.put_chunks_count == 0:
            check = self.state.get("before_first_put")
            if check is not None:
                check()
        self.put_chunks_count += 1
        self.state.setdefault("events", []).append(("put_chunks", expected_digest))
        return super().put_chunks(chunks, expected_digest, expected_size)

    def verify_receipt(self, expected: BlobInfo) -> BlobInfo:
        self.receipt_counts[expected.digest] += 1
        self.state.setdefault("events", []).append(
            ("verify_receipt", expected.digest, self.receipt_counts[expected.digest])
        )
        if self.fail_after_catalog and self.state.get("catalog_started", False):
            raise StorageIntegrityError("injected final receipt failure")
        return super().verify_receipt(expected)

    def retain_receipts(
        self,
        expected: tuple[BlobInfo, ...],
        callback: Callable[[], _Result],
    ) -> _Result:
        self.state.setdefault("events", []).append(("retention", "request"))

        def retained() -> _Result:
            assert not self.state.get("retention_active", False)
            assert self.receipt_counts
            assert set(self.receipt_counts.values()) == {1}
            self.state["retention_active"] = True
            self.state.setdefault("events", []).append(("retention", "enter"))
            try:
                return callback()
            finally:
                self.state["retention_active"] = False
                self.state.setdefault("events", []).append(("retention", "exit"))

        return super().retain_receipts(expected, retained)


class _InstrumentedCatalog(SQLiteCatalog):
    def __init__(self, path: Path, state: dict[str, Any]) -> None:
        super().__init__(path)
        self.state = state
        self.publish_count = 0

    def _record(self, name: str) -> None:
        assert self.state.get(
            "consume_returned", True
        ), f"catalog {name} ran before retained context consume postflight"
        assert self.state.get(
            "retention_active", False
        ), f"catalog {name} ran outside the object retention scope"
        self.state["catalog_started"] = True
        self.state.setdefault("events", []).append(("catalog", name))

    def create_namespace(self, *args: Any, **kwargs: Any) -> str:
        self._record("create_namespace")
        return super().create_namespace(*args, **kwargs)

    def create_repository(self, *args: Any, **kwargs: Any) -> str:
        self._record("create_repository")
        return super().create_repository(*args, **kwargs)

    def create_source_revision(self, *args: Any, **kwargs: Any) -> str:
        self._record("create_source_revision")
        return super().create_source_revision(*args, **kwargs)

    def create_view_profile(self, *args: Any, **kwargs: Any) -> str:
        self._record("create_view_profile")
        return super().create_view_profile(*args, **kwargs)

    def register_object(self, *args: Any, **kwargs: Any) -> str:
        self._record("register_object")
        return super().register_object(*args, **kwargs)

    def stage_view_generation(self, *args: Any, **kwargs: Any) -> str:
        self._record("stage_view_generation")
        return super().stage_view_generation(*args, **kwargs)

    def publish_snapshot(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self._record("publish_snapshot")
        assert self.state["cas"].receipt_counts
        assert set(self.state["cas"].receipt_counts.values()) == {2}
        self.publish_count += 1
        return super().publish_snapshot(*args, **kwargs)

    def resolve_ref(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return super().resolve_ref(*args, **kwargs)

    def get_manifest_summary(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return super().get_manifest_summary(*args, **kwargs)


class _ExtendedCatalog(_InstrumentedCatalog):
    @staticmethod
    def _extend_summary(summary: dict[str, Any]) -> dict[str, Any]:
        summary["backend_revision"] = 7
        summary["namespace"]["etag"] = "namespace-extension"
        summary["repository"]["etag"] = "repository-extension"
        summary["source"]["etag"] = "source-extension"
        for view in summary["views"].values():
            view["etag"] = "view-extension"
            view["profile"]["etag"] = "profile-extension"
            view["object"]["etag"] = "object-extension"
            for member in view["member_objects"]:
                member["etag"] = "member-extension"
        return summary

    def publish_snapshot(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        result = super().publish_snapshot(*args, **kwargs)
        result["etag"] = "publication-extension"
        return result

    def resolve_ref(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        result = super().resolve_ref(*args, **kwargs)
        result["etag"] = "ref-extension"
        result["manifest"] = self._extend_summary(result["manifest"])
        return result

    def get_manifest_summary(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._extend_summary(super().get_manifest_summary(*args, **kwargs))


class _MismatchedResolvedSummaryCatalog(_InstrumentedCatalog):
    def resolve_ref(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        result = super().resolve_ref(*args, **kwargs)
        result["manifest"]["published_at"] = "2000-01-01T00:00:00+00:00"
        return result


class _PostCommitInterruptCatalog(_InstrumentedCatalog):
    def __init__(self, path: Path, state: dict[str, Any]) -> None:
        super().__init__(path, state)
        self._interrupt_once = True

    def publish_snapshot(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        result = super().publish_snapshot(*args, **kwargs)
        if self._interrupt_once:
            self._interrupt_once = False
            raise KeyboardInterrupt("injected postcommit interruption")
        return result


class _BackendTripwire:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def _called(self, name: str) -> Any:
        self.calls.append(name)
        pytest.fail(f"backend method {name} must not be called")

    def retained_import_contract(self) -> str:
        return RETAINED_IMPORT_CATALOG_CONTRACT

    def put_bytes(self, *args: Any, **kwargs: Any) -> Any:
        return self._called("put_bytes")

    def put_file(self, *args: Any, **kwargs: Any) -> Any:
        return self._called("put_file")

    def has(self, *args: Any, **kwargs: Any) -> Any:
        return self._called("has")

    def open(self, *args: Any, **kwargs: Any) -> Any:
        return self._called("open")

    def read_bytes(self, *args: Any, **kwargs: Any) -> Any:
        return self._called("read_bytes")

    def verify(self, *args: Any, **kwargs: Any) -> Any:
        return self._called("verify")

    def materialize(self, *args: Any, **kwargs: Any) -> Any:
        return self._called("materialize")

    def put_chunks(self, *args: Any, **kwargs: Any) -> Any:
        return self._called("put_chunks")

    def verify_receipt(self, *args: Any, **kwargs: Any) -> Any:
        return self._called("verify_receipt")

    def retain_receipts(self, *args: Any, **kwargs: Any) -> Any:
        return self._called("retain_receipts")

    def create_namespace(self, *args: Any, **kwargs: Any) -> Any:
        return self._called("create_namespace")

    def create_repository(self, *args: Any, **kwargs: Any) -> Any:
        return self._called("create_repository")

    def create_source_revision(self, *args: Any, **kwargs: Any) -> Any:
        return self._called("create_source_revision")

    def create_view_profile(self, *args: Any, **kwargs: Any) -> Any:
        return self._called("create_view_profile")

    def register_object(self, *args: Any, **kwargs: Any) -> Any:
        return self._called("register_object")

    def stage_view_generation(self, *args: Any, **kwargs: Any) -> Any:
        return self._called("stage_view_generation")

    def publish_snapshot(self, *args: Any, **kwargs: Any) -> Any:
        return self._called("publish_snapshot")

    def resolve_ref(self, *args: Any, **kwargs: Any) -> Any:
        return self._called("resolve_ref")

    def get_manifest_summary(self, *args: Any, **kwargs: Any) -> Any:
        return self._called("get_manifest_summary")


def test_public_api_is_lazy_and_has_stable_signature_and_constants() -> None:
    assert compiler_module.RepoManifestImportResult is RepoManifestImportResult
    assert (
        compiler_module.import_retained_repo_manifest is import_retained_repo_manifest
    )
    assert "RepoManifestImportResult" in compiler_module.__all__
    assert "import_retained_repo_manifest" in compiler_module.__all__
    assert set(manifest_import_module.__all__) == {
        "DEFAULT_MAX_CONTEXT_BYTES",
        "DEFAULT_MAX_CONTEXT_FILES",
        "DEFAULT_MAX_PROJECTION_BYTES",
        "DEFAULT_NAMESPACE_NAME",
        "DEFAULT_REF_NAME",
        "REPO_MANIFEST_IMPORT_GENERATION_CONTRACT",
        "REPO_MANIFEST_PROJECTION_MEDIA_TYPE",
        "REPO_MANIFEST_PROJECTION_PROFILE_NAME",
        "REPO_MANIFEST_PROJECTION_SCHEMA",
        "REPO_MANIFEST_PROJECTION_VIEW",
        "RepoManifestImportResult",
        "import_retained_repo_manifest",
    }
    assert list(inspect.signature(import_retained_repo_manifest).parameters) == [
        "plan",
        "artifact_owner",
        "repository_source",
        "repository_key",
        "catalog",
        "object_store",
        "namespace_name",
        "ref_name",
        "expected_generation",
        "max_context_files",
        "max_context_bytes",
        "max_bundle_files",
        "max_bundle_bytes",
        "max_bundle_metadata_bytes",
        "max_projection_bytes",
        "forbidden_paths",
        "environ",
    ]
    assert DEFAULT_NAMESPACE_NAME == "default"
    assert DEFAULT_REF_NAME == "main"
    assert DEFAULT_MAX_CONTEXT_FILES == 100_000
    assert DEFAULT_MAX_CONTEXT_BYTES == 64 * 1024 * 1024 * 1024
    assert DEFAULT_MAX_PROJECTION_BYTES == 16 * 1024 * 1024
    assert (
        REPO_MANIFEST_IMPORT_GENERATION_CONTRACT
        == "codenib.repo-manifest-import-generation.v2"
    )
    assert REPO_MANIFEST_PROJECTION_VIEW == "codenib.internal.repo-manifest"
    assert REPO_MANIFEST_PROJECTION_SCHEMA == "codenib.internal.repo-manifest.v2"
    assert (
        REPO_MANIFEST_PROJECTION_MEDIA_TYPE
        == "application/vnd.codenib.repo-manifest-projection.v2+json"
    )
    assert REPO_MANIFEST_PROJECTION_PROFILE_NAME == "canonical"


def test_real_import_publishes_members_projection_subset_and_exact_retry(
    tmp_path: Path,
) -> None:
    fixture = _context_fixture(tmp_path / "fixture", ("bm25", "vector"))
    plan = fixture.plan(("bm25",))
    state: dict[str, Any] = {"consume_returned": True, "catalog_started": False}
    cas = _InstrumentedCAS(tmp_path / "cas", state)
    state["cas"] = cas
    catalog = _InstrumentedCatalog(tmp_path / "catalog.sqlite", state)
    try:
        first = import_retained_repo_manifest(
            plan,
            artifact_owner=fixture.context_owner,
            repository_source=fixture.repository_source,
            repository_key=_REPOSITORY_KEY,
            catalog=catalog,
            object_store=cas,
            environ={},
        )

        assert first.views == ("bm25",)
        assert first.skipped_items == ()
        assert tuple(dict(first.view_generation_items)) == (
            "bm25",
            REPO_MANIFEST_PROJECTION_VIEW,
        )
        assert first.generation == 1
        assert first.changed is True
        assert fixture.context_owner.active
        assert fixture.repository_source.usable
        assert all(owner.active for owner in fixture.generation_owners)

        summary = catalog.get_manifest_summary(first.snapshot_id)
        assert set(summary["views"]) == {
            "bm25",
            REPO_MANIFEST_PROJECTION_VIEW,
        }
        bm25 = summary["views"]["bm25"]
        projection = summary["views"][REPO_MANIFEST_PROJECTION_VIEW]
        assert bm25["member_objects"]
        assert all(item["byte_size"] > 0 for item in bm25["member_objects"])
        assert projection["member_objects"]
        assert bm25["object"]["digest"] in {
            member["digest"] for member in projection["member_objects"]
        }
        projection_payload = json.loads(cas.read_bytes(projection["object"]["digest"]))
        assert projection_payload["schema"] == REPO_MANIFEST_PROJECTION_SCHEMA
        assert set(projection_payload["views"]) == {"bm25"}
        assert set(projection_payload["manifest"]["indexes"]) == {
            "bm25",
            "vector",
        }
        assert projection_payload["plan"]["digest"] == plan.plan_digest
        assert projection_payload["selection"]["selected_views"] == ["bm25"]
        assert projection_payload["manifest"]["repo"]["path"] == "source"
        assert projection_payload["manifest"]["capabilities"]["sparse_search"]
        assert projection_payload["manifest"]["capabilities"]["future_query"]
        assert projection_payload["artifact"]["postflight"] == (
            "completed-before-catalog"
        )
        assert all(
            "storage_key" not in member["object"]
            for member in projection_payload["views"]["bm25"]["members"]
        )

        state["catalog_started"] = False
        cas.receipt_counts.clear()
        retry = import_retained_repo_manifest(
            plan,
            artifact_owner=fixture.context_owner,
            repository_source=fixture.repository_source,
            repository_key=_REPOSITORY_KEY,
            catalog=catalog,
            object_store=cas,
            environ={},
        )
        assert retry.snapshot_id == first.snapshot_id
        assert retry.generation == first.generation
        assert retry.changed is False
        assert retry.view_generation_items == first.view_generation_items
        assert fixture.context_owner.active
        assert fixture.repository_source.usable
    finally:
        catalog.close()
        fixture.close()


def test_v12_import_binds_nonempty_source_selection_into_projection(
    tmp_path: Path,
) -> None:
    source_selection = RepositorySourceSelection(("generated", "vendor/cache"))
    fixture = _context_fixture(
        tmp_path / "fixture-selection",
        ("bm25",),
        source_selection=source_selection,
    )
    plan = fixture.plan()
    cas = LocalCAS(tmp_path / "selection-cas")
    catalog = SQLiteCatalog(tmp_path / "selection-catalog.sqlite")
    try:
        imported = import_retained_repo_manifest(
            plan,
            artifact_owner=fixture.context_owner,
            repository_source=fixture.repository_source,
            repository_key=_REPOSITORY_KEY,
            catalog=catalog,
            object_store=cas,
            environ={},
        )
        summary = catalog.get_manifest_summary(imported.snapshot_id)
        projection = summary["views"][REPO_MANIFEST_PROJECTION_VIEW]
        payload = json.loads(cas.read_bytes(projection["object"]["digest"]))

        assert plan.source.source_selection == source_selection
        assert payload["source"]["source_selection"] == source_selection.to_dict()
        assert payload["source"]["source_selection_digest"] == source_selection.digest
        assert payload["plan"]["schema"].endswith(".v2")
        assert (
            payload["plan"]["source"]["source_selection_digest"]
            == source_selection.digest
        )
        assert payload["manifest"]["repo"]["source_selection"] == (
            source_selection.to_dict()
        )
    finally:
        catalog.close()
        fixture.close()


def test_postcommit_interruption_converges_on_exact_retry(tmp_path: Path) -> None:
    fixture = _context_fixture(tmp_path / "fixture", ("bm25",))
    plan = fixture.plan()
    state: dict[str, Any] = {"consume_returned": True, "catalog_started": False}
    cas = _InstrumentedCAS(tmp_path / "cas", state)
    state["cas"] = cas
    catalog = _PostCommitInterruptCatalog(tmp_path / "catalog.sqlite", state)
    try:
        with pytest.raises(KeyboardInterrupt, match="postcommit interruption"):
            import_retained_repo_manifest(
                plan,
                artifact_owner=fixture.context_owner,
                repository_source=fixture.repository_source,
                repository_key=_REPOSITORY_KEY,
                catalog=catalog,
                object_store=cas,
                environ={},
            )

        assert not state["retention_active"]
        assert catalog.publish_count == 1

        state["catalog_started"] = False
        cas.receipt_counts.clear()
        retry = import_retained_repo_manifest(
            plan,
            artifact_owner=fixture.context_owner,
            repository_source=fixture.repository_source,
            repository_key=_REPOSITORY_KEY,
            catalog=catalog,
            object_store=cas,
            environ={},
        )

        assert retry.generation == 1
        assert retry.changed is False
        assert catalog.publish_count == 2
        assert not state["retention_active"]
        assert fixture.context_owner.active
        assert fixture.repository_source.usable
    finally:
        catalog.close()
        fixture.close()


def test_all_selected_views_finish_validation_and_planning_before_cas_and_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _context_fixture(tmp_path / "fixture", ("bm25", "vector"))
    plan = fixture.plan()
    selected = set(plan.selection.selected_views)
    validated: list[str] = []
    planned: list[str] = []
    state: dict[str, Any] = {
        "consume_returned": False,
        "catalog_started": False,
        "events": [],
    }
    cas = _InstrumentedCAS(tmp_path / "cas", state)
    state["cas"] = cas
    catalog = _InstrumentedCatalog(tmp_path / "catalog.sqlite", state)
    real_validate = (
        manifest_import_module.validate_content_bound_portable_query_view_reader
    )
    real_plan = manifest_import_module.plan_view_bundle_reader
    real_consume = PublishedWorkspaceReceiptOwner.consume

    def validating(*args: Any, **kwargs: Any) -> Any:
        validated.append(kwargs["view_type"])
        return real_validate(*args, **kwargs)

    def planning(*args: Any, **kwargs: Any) -> Any:
        planned.append(kwargs["view_type"])
        return real_plan(*args, **kwargs)

    def consuming(
        owner: PublishedWorkspaceReceiptOwner,
        operation: Callable[..., _Result],
    ) -> _Result:
        result = real_consume(owner, operation)
        state["consume_returned"] = True
        return result

    def before_first_put() -> None:
        assert set(validated) == selected
        assert set(planned) == selected
        assert len(validated) == len(selected)
        assert len(planned) == len(selected)
        assert not state["catalog_started"]

    state["before_first_put"] = before_first_put
    monkeypatch.setattr(
        manifest_import_module,
        "validate_content_bound_portable_query_view_reader",
        validating,
    )
    monkeypatch.setattr(manifest_import_module, "plan_view_bundle_reader", planning)
    monkeypatch.setattr(PublishedWorkspaceReceiptOwner, "consume", consuming)
    try:
        result = import_retained_repo_manifest(
            plan,
            artifact_owner=fixture.context_owner,
            repository_source=fixture.repository_source,
            repository_key=_REPOSITORY_KEY,
            catalog=catalog,
            object_store=cas,
            environ={},
        )
        assert set(result.views) == selected
        assert selected == {"bm25", "vector"}
        first_catalog = next(
            index
            for index, event in enumerate(state["events"])
            if event[:1] == ("catalog",)
        )
        retention_enter = state["events"].index(("retention", "enter"))
        retention_exit = state["events"].index(("retention", "exit"))
        assert retention_enter < first_catalog < retention_exit
        assert all(
            event[:1] != ("catalog",) for event in state["events"][:first_catalog]
        )
        assert state["retention_active"] is False
        assert state["consume_returned"] is True
        assert fixture.context_owner.active
        assert fixture.repository_source.usable
    finally:
        catalog.close()
        fixture.close()


def test_inert_source_mismatch_and_mutated_plan_reject_before_backends(
    tmp_path: Path,
) -> None:
    fixture = _context_fixture(tmp_path / "fixture", ("bm25",))
    plan = fixture.plan()
    backend = _BackendTripwire()
    other_root = tmp_path / "other"
    other_root.mkdir()
    (other_root / "sample.py").write_text("OTHER = 2\n", encoding="utf-8")
    other_source = capture_repository_source(other_root)
    try:
        legacy_manifest = json.loads(plan.manifest_json)
        legacy_manifest["version"] = LEGACY_MANIFEST_VERSION
        legacy_manifest["repo"].pop("source_selection")
        legacy_manifest["repo"].pop("last_indexed_source_selection_digest")
        legacy_fingerprint = "sha256:" + "e" * 64
        legacy_manifest["repo"]["source_fingerprint"] = legacy_fingerprint
        legacy_manifest["repo"]["last_indexed_source_fingerprint"] = legacy_fingerprint
        for entry in legacy_manifest["indexes"].values():
            entry["source_fingerprint"] = legacy_fingerprint
            entry.pop("source_selection_digest")
            for contract in (entry["config"], entry["metadata"]):
                contract.pop("source_selection_digest", None)
                if "repository_filter_policy" in contract:
                    contract["repository_filter_policy"] = 3
        inert = plan_repo_manifest_import(legacy_manifest, views=["bm25"])
        assert inert.inert is True
        with pytest.raises(StorageValidationError, match="import-inert"):
            import_retained_repo_manifest(
                inert,
                artifact_owner=fixture.context_owner,
                repository_source=fixture.repository_source,
                repository_key=_REPOSITORY_KEY,
                catalog=backend,
                object_store=backend,
                environ={},
            )
        assert backend.calls == []

        with pytest.raises(StorageIntegrityError, match="retained repository source"):
            import_retained_repo_manifest(
                plan,
                artifact_owner=fixture.context_owner,
                repository_source=other_source,
                repository_key=_REPOSITORY_KEY,
                catalog=backend,
                object_store=backend,
                environ={},
            )
        assert backend.calls == []

        expected_selection = plan.source.source_selection
        assert expected_selection is not None
        fixture.repository_source._source_selection_identity = RepositorySourceSelection(  # type: ignore[attr-defined]
            ("different-policy",)
        )
        with pytest.raises(StorageIntegrityError, match="retained repository source"):
            import_retained_repo_manifest(
                plan,
                artifact_owner=fixture.context_owner,
                repository_source=fixture.repository_source,
                repository_key=_REPOSITORY_KEY,
                catalog=backend,
                object_store=backend,
                environ={},
            )
        assert backend.calls == []
        fixture.repository_source._source_selection_identity = (  # type: ignore[attr-defined]
            expected_selection
        )

        mutated = replace(plan)
        object.__setattr__(mutated, "manifest_json", mutated.manifest_json + " ")
        with pytest.raises(StorageValidationError, match="canonical"):
            import_retained_repo_manifest(
                mutated,
                artifact_owner=fixture.context_owner,
                repository_source=fixture.repository_source,
                repository_key=_REPOSITORY_KEY,
                catalog=backend,
                object_store=backend,
                environ={},
            )
        assert backend.calls == []
        assert fixture.context_owner.active
        assert fixture.repository_source.usable
    finally:
        other_source.close()
        fixture.close()


def test_default_environment_snapshot_rejects_ambient_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "ambient-runtime-secret"
    fixture = _context_fixture(
        tmp_path / "fixture",
        ("bm25",),
        source_text=f"VALUE = {secret!r}\n",
    )
    backend = _BackendTripwire()
    monkeypatch.setenv("MODELS_TOKEN", secret)
    try:
        with pytest.raises(ValueError, match="configured credential"):
            import_retained_repo_manifest(
                fixture.plan(),
                artifact_owner=fixture.context_owner,
                repository_source=fixture.repository_source,
                repository_key=_REPOSITORY_KEY,
                catalog=backend,
                object_store=backend,
            )
        assert backend.calls == []
        assert fixture.context_owner.active
        assert not fixture.repository_source.usable
    finally:
        fixture.close()


def test_final_receipt_failure_does_not_move_existing_ref(tmp_path: Path) -> None:
    first_fixture = _context_fixture(
        tmp_path / "first-fixture",
        ("bm25",),
        source_text="VALUE = 1\n",
    )
    second_fixture = _context_fixture(
        tmp_path / "second-fixture",
        ("bm25",),
        source_text="VALUE = 2\n",
    )
    state: dict[str, Any] = {"consume_returned": True, "catalog_started": False}
    cas = _InstrumentedCAS(tmp_path / "cas", state)
    state["cas"] = cas
    catalog = _InstrumentedCatalog(tmp_path / "catalog.sqlite", state)
    try:
        first = import_retained_repo_manifest(
            first_fixture.plan(),
            artifact_owner=first_fixture.context_owner,
            repository_source=first_fixture.repository_source,
            repository_key=_REPOSITORY_KEY,
            catalog=catalog,
            object_store=cas,
            environ={},
        )
        old_ref = catalog.resolve_ref(first.repository_id)
        old_publish_count = catalog.publish_count

        state["catalog_started"] = False
        cas.receipt_counts.clear()
        cas.fail_after_catalog = True
        with pytest.raises(StorageIntegrityError, match="final receipt failure"):
            import_retained_repo_manifest(
                second_fixture.plan(),
                artifact_owner=second_fixture.context_owner,
                repository_source=second_fixture.repository_source,
                repository_key=_REPOSITORY_KEY,
                catalog=catalog,
                object_store=cas,
                expected_generation=old_ref["generation"],
                environ={},
            )

        current_ref = catalog.resolve_ref(first.repository_id)
        assert current_ref["snapshot_id"] == old_ref["snapshot_id"]
        assert current_ref["generation"] == old_ref["generation"]
        assert catalog.publish_count == old_publish_count
        assert cas.receipt_counts
        assert 1 in cas.receipt_counts.values()
        assert state["retention_active"] is False
        assert second_fixture.context_owner.active
        assert second_fixture.repository_source.usable
    finally:
        catalog.close()
        second_fixture.close()
        first_fixture.close()


def test_backend_exact_json_extensions_do_not_break_core_attestation(
    tmp_path: Path,
) -> None:
    fixture = _context_fixture(tmp_path / "fixture", ("bm25", "vector"))
    state: dict[str, Any] = {"consume_returned": True, "catalog_started": False}
    cas = _InstrumentedCAS(tmp_path / "cas", state)
    state["cas"] = cas
    catalog = _ExtendedCatalog(tmp_path / "catalog.sqlite", state)
    try:
        result = import_retained_repo_manifest(
            fixture.plan(),
            artifact_owner=fixture.context_owner,
            repository_source=fixture.repository_source,
            repository_key=_REPOSITORY_KEY,
            catalog=catalog,
            object_store=cas,
            environ={},
        )
        assert set(result.views) == {"bm25", "vector"}
        assert result.changed is True
    finally:
        catalog.close()
        fixture.close()


def test_backend_attestation_preserves_exact_numeric_identity() -> None:
    same = manifest_import_module._same_exact_json

    assert same({"value": -0.0}, {"value": -0.0})
    assert not same({"value": True}, {"value": 1})
    assert not same({"value": 1}, {"value": 1.0})
    assert not same({"value": -0.0}, {"value": 0.0})
    assert not manifest_import_module._has_exact_json_core(
        {"identity": {"size": 1.0}, "etag": "extension"},
        {"identity": {"size": 1}},
    )


def test_ref_manifest_must_repeat_the_same_immutable_summary_timestamp(
    tmp_path: Path,
) -> None:
    fixture = _context_fixture(tmp_path / "fixture", ("bm25",))
    state: dict[str, Any] = {"consume_returned": True, "catalog_started": False}
    cas = _InstrumentedCAS(tmp_path / "cas", state)
    state["cas"] = cas
    catalog = _MismatchedResolvedSummaryCatalog(
        tmp_path / "catalog.sqlite",
        state,
    )
    try:
        with pytest.raises(StorageIntegrityError, match="timestamp differs"):
            import_retained_repo_manifest(
                fixture.plan(),
                artifact_owner=fixture.context_owner,
                repository_source=fixture.repository_source,
                repository_key=_REPOSITORY_KEY,
                catalog=catalog,
                object_store=cas,
                environ={},
            )
    finally:
        catalog.close()
        fixture.close()


def _identity_closed_summary() -> tuple[dict[str, Any], PublishedSnapshot]:
    namespace = NamespaceIdentity("default")
    repository = RepositoryIdentity(namespace.namespace_id, _REPOSITORY_KEY)
    source = SourceRevision.dirty(
        repository.repository_id,
        source_fingerprint="sha256-v2:" + "3" * 64,
    )
    profile = ViewProfile.create("bm25", {"contract": "test"})
    primary = ObjectRecord(
        digest="4" * 64,
        storage_key="objects/44/payload",
        byte_size=7,
        media_type="application/octet-stream",
    )
    generation = ViewGeneration.create(
        source,
        profile,
        primary,
        schema_version="test-v1",
    )
    snapshot = PublishedSnapshot(
        repository.repository_id,
        source.source_revision_id,
        (SnapshotView(generation),),
    )
    summary = {
        "snapshot_id": snapshot.snapshot_id,
        "repository_id": repository.repository_id,
        "namespace": {
            "namespace_id": namespace.namespace_id,
            "name": namespace.name,
        },
        "repository": {
            "namespace_id": namespace.namespace_id,
            "repository_key": repository.repository_key,
        },
        "status": "ready",
        "published_at": "2026-08-12T11:59:59+00:00",
        "source": {
            "source_revision_id": source.source_revision_id,
            "kind": source.source_kind,
            "commit_sha": source.commit_sha,
            "tree_sha": source.tree_sha,
            "source_fingerprint": source.source_fingerprint,
        },
        "views": {
            "bm25": {
                "view_generation_id": generation.view_generation_id,
                "schema_version": generation.schema_version,
                "metadata": json.loads(generation.metadata_json),
                "profile": {
                    "profile_id": profile.profile_id,
                    "name": profile.name,
                    "config": profile.config,
                },
                "object": {
                    "digest": primary.digest,
                    "storage_key": primary.storage_key,
                    "byte_size": primary.byte_size,
                    "media_type": primary.media_type,
                },
                "member_objects": [],
            }
        },
    }
    return summary, snapshot


def test_ref_attestation_distinguishes_supersede_from_invalid_shape() -> None:
    validate = manifest_import_module._validate_ref_resolution
    manifest, snapshot = _identity_closed_summary()
    expected = {
        "repository_id": snapshot.repository_id,
        "ref_name": "main",
        "snapshot_id": snapshot.snapshot_id,
        "generation": 3,
        "updated_at": "2026-08-12T12:00:00+00:00",
        "manifest": manifest,
    }
    validate(
        expected,
        repository_id=snapshot.repository_id,
        ref_name="main",
        snapshot_id=snapshot.snapshot_id,
        generation=3,
        updated_at="2026-08-12T12:00:00+00:00",
    )

    newer = {**expected, "generation": 4}
    with pytest.raises(PublishConflict, match="superseded"):
        validate(
            newer,
            repository_id=snapshot.repository_id,
            ref_name="main",
            snapshot_id=snapshot.snapshot_id,
            generation=3,
            updated_at="2026-08-12T12:00:00+00:00",
        )

    for invalid in (
        {**newer, "snapshot_id": 1},
        {**newer, "manifest": None},
        {**newer, "snapshot_id": "not-a-content-id"},
        {**newer, "manifest": {}},
        {**newer, "manifest": {**manifest, "namespace": {}}},
        {**newer, "manifest": {**manifest, "views": {}}},
        {**newer, "updated_at": "now"},
        {**expected, "generation": 2},
    ):
        with pytest.raises(StorageIntegrityError, match="ref resolution"):
            validate(
                invalid,
                repository_id=snapshot.repository_id,
                ref_name="main",
                snapshot_id=snapshot.snapshot_id,
                generation=3,
                updated_at="2026-08-12T12:00:00+00:00",
            )
