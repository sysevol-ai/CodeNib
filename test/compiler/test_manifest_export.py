# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
import dis
import hashlib
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

import codenib.compiler.manifest_export as manifest_export_module
from codenib._captured_directory import PublishedWorkspaceReceiptOwner
from codenib.artifacts import stage_context_artifact_strict
from codenib.artifacts.portable_views import (
    MAX_PORTABLE_DOCUMENTS_JSON_BYTES,
    MAX_PORTABLE_FAISS_INDEX_BYTES,
)
from codenib.compiler.artifact_fingerprints import bm25_artifact_file_fingerprints
from codenib.compiler.manifest import RepoManifest
from codenib.compiler.manifest_export import (
    RepoManifestExportReceipt,
    RepoManifestExportResult,
    RepoManifestViewExportReceipt,
    export_retained_repo_manifest_ref,
    export_retained_repo_manifest_snapshot,
)
from codenib.compiler.manifest_import import (
    REPO_MANIFEST_PROJECTION_VIEW,
    import_retained_repo_manifest,
)
from codenib.compiler.manifest_storage import (
    plan_repo_manifest_import,
    plan_repo_manifest_import_bytes,
)
from codenib.compiler.retained_snapshot import attest_detached_retained_snapshot
from codenib.source_fingerprint import capture_repository_source
from codenib.storage import (
    RETAINED_IMPORT_CATALOG_CONTRACT,
    ArtifactMember,
    BlobInfo,
    LocalCAS,
    NamespaceIdentity,
    ObjectRecord,
    PublishConflict,
    RepositoryIdentity,
    SQLiteCatalog,
    StorageIntegrityError,
    StorageValidationError,
    build_view_bundle,
)

from .test_manifest_import import (
    _COMMIT,
    _context_fixture,
    _ContextFixture,
    _entry,
    _json_bytes,
    _publish_generation,
    _TestWorkspaceProvider,
    _vector_files,
)

_REPOSITORY_KEY = "owner/repo"


def _exception_notes(error: BaseException) -> tuple[str, ...]:
    return (
        *tuple(getattr(error, "__notes__", ())),
        *tuple(getattr(error, "_codenib_cleanup_notes", ())),
    )


def _interrupt_after_owned_acquire(
    callback: Any,
    *,
    error: BaseException,
) -> None:
    function = manifest_export_module._CloseableResourceOwner.acquire
    code = function.__code__
    instructions = tuple(dis.get_instructions(function))
    store_indexes = {
        index
        for index, instruction in enumerate(instructions[:-1])
        if instruction.opname == "STORE_ATTR" and instruction.argval == "_resource"
    }
    opcode_offsets_after_store = {
        instructions[index + 1].offset for index in store_indexes
    }
    store_offsets = {instructions[index].offset for index in store_indexes}
    line_offsets_after_store = {
        instruction.offset
        for instruction in instructions
        if instruction.starts_line is not None
        and any(instruction.offset > store_offset for store_offset in store_offsets)
    }
    assert len(store_indexes) == 1
    previous_trace = sys.gettrace()
    injected = False

    def trace(frame: Any, event: str, _arg: object) -> Any:
        nonlocal injected
        if event == "call" and frame.f_code is code:
            frame.f_trace_opcodes = True
            frame.f_trace_lines = True
            return trace
        if (
            not injected
            and frame.f_code is code
            and (
                (event == "opcode" and frame.f_lasti in opcode_offsets_after_store)
                or (event == "line" and frame.f_lasti in line_offsets_after_store)
            )
            and frame.f_locals["self"]._resource is not manifest_export_module._MISSING
        ):
            injected = True
            sys.settrace(None)
            raise error
        return trace

    sys.settrace(trace)
    try:
        callback()
    finally:
        sys.settrace(previous_trace)
        assert injected, "failed to interrupt after owned resource result store"


def _interrupt_after_owned_operation(
    callback: Any,
    *,
    error: BaseException,
) -> None:
    function = manifest_export_module._run_with_owned_close
    code = function.__code__
    instructions = tuple(dis.get_instructions(function))
    store_indexes = {
        index
        for index, instruction in enumerate(instructions[:-1])
        if instruction.opname == "STORE_FAST"
        and instruction.argval == "operation_succeeded"
        and instructions[index - 1].opname == "LOAD_CONST"
        and instructions[index - 1].argval is True
    }
    opcode_offsets_after_store = {
        instructions[index + 1].offset for index in store_indexes
    }
    store_offsets = {instructions[index].offset for index in store_indexes}
    line_offsets_after_store = {
        instruction.offset
        for instruction in instructions
        if instruction.starts_line is not None
        and any(instruction.offset > store_offset for store_offset in store_offsets)
    }
    assert len(store_indexes) == 1
    previous_trace = sys.gettrace()
    injected = False

    def trace(frame: Any, event: str, _arg: object) -> Any:
        nonlocal injected
        if event == "call" and frame.f_code is code:
            frame.f_trace_opcodes = True
            frame.f_trace_lines = True
            return trace
        if (
            not injected
            and frame.f_code is code
            and (
                (event == "opcode" and frame.f_lasti in opcode_offsets_after_store)
                or (event == "line" and frame.f_lasti in line_offsets_after_store)
            )
            and frame.f_locals["operation_succeeded"] is True
        ):
            injected = True
            sys.settrace(None)
            raise error
        return trace

    sys.settrace(trace)
    try:
        callback()
    finally:
        sys.settrace(previous_trace)
        assert injected, "failed to interrupt after owned operation result store"


def _publication_cleanup_owners(error: BaseException) -> tuple[Any, ...]:
    return BaseException.__getattribute__(error, "publication_cleanup_owners")


def _vector_context_fixture(
    root: Path,
    *,
    document_count: dict[str, int] | None,
) -> _ContextFixture:
    source_text = "VALUE = 1\n"
    repository = root / "repo"
    repository.mkdir(parents=True)
    (repository / "sample.py").write_text(source_text, encoding="utf-8")
    repository_source = capture_repository_source(repository)
    generation_owner = _publish_generation(
        root / "state" / "vector",
        _vector_files(source_text),
    )
    context_owner = PublishedWorkspaceReceiptOwner()
    try:
        entry = _entry(
            "vector",
            source_fingerprint=repository_source.fingerprint,
        )
        if document_count is None:
            entry.config.pop("document_count", None)
            entry.metadata.pop("document_count", None)
        else:
            entry.config["document_count"] = dict(document_count)
            entry.metadata["document_count"] = dict(document_count)
        manifest = RepoManifest(
            repo_path=str(repository),
            commit=_COMMIT,
            last_indexed_commit=_COMMIT,
            source_fingerprint=repository_source.fingerprint,
            last_indexed_source_fingerprint=repository_source.fingerprint,
            languages=["python"],
            file_count=repository_source.file_count,
            indexes={"vector": entry},
            compiled_at="2026-01-01T00:00:00Z",
            compiled_at_epoch=1.0,
        )
        manifest.derive_capabilities()
        context = root / "published" / "context"
        stage_context_artifact_strict(
            context,
            manifest=manifest,
            repository=_REPOSITORY_KEY,
            repository_source=repository_source,
            view_generations={"vector": generation_owner},
            workspace_provider=_TestWorkspaceProvider(),
            output_receipt_owner=context_owner,
            environ={},
        )
        return _ContextFixture(
            repository=repository,
            repository_source=repository_source,
            context=context,
            context_owner=context_owner,
            generation_owners=(generation_owner,),
        )
    except BaseException:
        if context_owner.active:
            context_owner.close()
        generation_owner.close()
        repository_source.close()
        raise


@contextmanager
def _retained_fixture(
    root: Path,
    views: tuple[str, ...] = ("bm25", "vector"),
    *,
    selected_views: tuple[str, ...] | None = None,
    source_text: str = "VALUE = 1\n",
    ref_name: str = "main",
) -> Iterator[tuple[_ContextFixture, Any, LocalCAS, SQLiteCatalog]]:
    fixture = _context_fixture(root / "fixture", views, source_text=source_text)
    plan = fixture.plan(selected_views)
    object_store = LocalCAS(root / "cas")
    catalog = SQLiteCatalog(root / "catalog.sqlite")
    try:
        imported = import_retained_repo_manifest(
            plan,
            artifact_owner=fixture.context_owner,
            repository_source=fixture.repository_source,
            repository_key=_REPOSITORY_KEY,
            catalog=catalog,
            object_store=object_store,
            ref_name=ref_name,
            environ={},
        )
        yield fixture, imported, object_store, catalog
    finally:
        catalog.close()
        fixture.close()


def test_real_import_round_trips_ref_and_snapshot_as_canonical_v11_bytes(
    tmp_path: Path,
) -> None:
    with _retained_fixture(tmp_path / "roundtrip") as (
        fixture,
        imported,
        object_store,
        catalog,
    ):
        expected = fixture.plan().canonical_manifest_bytes
        ref_export = export_retained_repo_manifest_ref(
            imported.repository_id,
            catalog=catalog,
            object_store=object_store,
            expected_generation=1,
        )

        assert isinstance(ref_export, RepoManifestExportResult)
        assert ref_export.canonical_manifest_bytes == expected
        summary = catalog.get_manifest_summary(imported.snapshot_id)
        assert ref_export.receipt.namespace_id == summary["namespace"]["namespace_id"]
        assert ref_export.receipt.repository_id == imported.repository_id
        assert ref_export.receipt.repository_key == _REPOSITORY_KEY
        assert ref_export.receipt.snapshot_id == imported.snapshot_id
        assert ref_export.receipt.ref_name == "main"
        assert ref_export.receipt.ref_generation == 1
        assert (
            ref_export.receipt.manifest_digest == hashlib.sha256(expected).hexdigest()
        )
        assert ref_export.receipt.manifest_byte_size == len(expected)
        assert tuple(
            receipt.view_type for receipt in ref_export.receipt.view_receipts
        ) == (
            "bm25",
            "vector",
        )
        assert all(
            receipt.member_receipt_items for receipt in ref_export.receipt.view_receipts
        )

        replanned = plan_repo_manifest_import_bytes(ref_export.canonical_manifest_bytes)
        assert replanned.canonical_manifest_bytes == expected
        assert replanned.selection.selected_views == ("bm25", "vector")

        snapshot_export = export_retained_repo_manifest_snapshot(
            imported.repository_id,
            imported.snapshot_id,
            catalog=catalog,
            object_store=object_store,
        )
        assert snapshot_export.canonical_manifest_bytes == expected
        assert snapshot_export.receipt.ref_name is None
        assert snapshot_export.receipt.ref_generation is None
        assert snapshot_export.receipt.ref_updated_at is None
        assert snapshot_export.receipt.snapshot_published_at == (
            ref_export.receipt.snapshot_published_at
        )


def test_subset_export_removes_unretained_view_and_keeps_capability_extensions(
    tmp_path: Path,
) -> None:
    with _retained_fixture(
        tmp_path / "subset",
        selected_views=("bm25",),
    ) as (_fixture, imported, object_store, catalog):
        exported = export_retained_repo_manifest_snapshot(
            imported.repository_id,
            imported.snapshot_id,
            catalog=catalog,
            object_store=object_store,
        )

        manifest = json.loads(exported.canonical_manifest_bytes)
        assert set(manifest["indexes"]) == {"bm25"}
        assert manifest["capabilities"] == {
            "dense_search": False,
            "future_query": True,
            "hybrid_search": False,
            "sparse_search": True,
            "symbol_navigation": False,
        }
        assert tuple(
            receipt.view_type for receipt in exported.receipt.view_receipts
        ) == ("bm25",)
        assert exported.receipt.skipped_items == ()


def test_export_receipts_close_projection_bundle_and_member_inventory(
    tmp_path: Path,
) -> None:
    with _retained_fixture(tmp_path / "receipt-closure") as (
        _fixture,
        imported,
        object_store,
        catalog,
    ):
        exported = export_retained_repo_manifest_snapshot(
            imported.repository_id,
            imported.snapshot_id,
            catalog=catalog,
            object_store=object_store,
        )
        summary = catalog.get_manifest_summary(imported.snapshot_id)
        projection = summary["views"][REPO_MANIFEST_PROJECTION_VIEW]
        projection_payload = json.loads(
            object_store.read_bytes(projection["object"]["digest"])
        )

        assert exported.receipt.projection_generation_id == (
            projection["view_generation_id"]
        )
        assert exported.receipt.projection_receipt == BlobInfo(
            digest=projection["object"]["digest"],
            byte_size=projection["object"]["byte_size"],
            storage_key=projection["object"]["storage_key"],
        )
        for view_receipt in exported.receipt.view_receipts:
            view = summary["views"][view_receipt.view_type]
            assert view_receipt.view_generation_id == view["view_generation_id"]
            assert view_receipt.bundle_receipt == BlobInfo(
                digest=view["object"]["digest"],
                byte_size=view["object"]["byte_size"],
                storage_key=view["object"]["storage_key"],
            )
            observed_members = dict(view_receipt.member_receipt_items)
            expected_members = {
                member["path"]: member["object"]
                for member in projection_payload["views"][view_receipt.view_type][
                    "members"
                ]
            }
            assert tuple(observed_members) == tuple(sorted(expected_members))
            assert {receipt.digest for receipt in observed_members.values()} == {
                member["digest"] for member in view["member_objects"]
            }
            for path, receipt in observed_members.items():
                assert receipt == BlobInfo(
                    digest=expected_members[path]["digest"],
                    byte_size=expected_members[path]["byte_size"],
                    storage_key=next(
                        member["storage_key"]
                        for member in view["member_objects"]
                        if member["digest"] == expected_members[path]["digest"]
                    ),
                )


def test_ref_export_resolves_once_and_stays_on_resolved_snapshot_when_ref_moves(
    tmp_path: Path,
) -> None:
    root = tmp_path / "moving-ref"
    with _retained_fixture(root / "first", views=("bm25",)) as (
        first_fixture,
        first,
        first_store,
        first_catalog,
    ):
        second_fixture = _context_fixture(
            root / "second-fixture",
            ("bm25",),
            source_text="VALUE = 2\n",
        )
        try:
            second = import_retained_repo_manifest(
                second_fixture.plan(),
                artifact_owner=second_fixture.context_owner,
                repository_source=second_fixture.repository_source,
                repository_key=_REPOSITORY_KEY,
                catalog=first_catalog,
                object_store=first_store,
                ref_name="next",
                environ={},
            )

            class MovingCatalog:
                calls = 0

                @staticmethod
                def retained_import_contract() -> str:
                    return RETAINED_IMPORT_CATALOG_CONTRACT

                def resolve_ref(
                    self,
                    repository_id: str,
                    ref_name: str = "main",
                ) -> dict[str, Any]:
                    self.calls += 1
                    resolved = first_catalog.resolve_ref(repository_id, ref_name)
                    first_catalog.publish_snapshot(
                        second.repository_id,
                        second.source_revision_id,
                        tuple(dict(second.view_generation_items).values()),
                        ref_name="main",
                        expected_generation=1,
                    )
                    return resolved

                def get_manifest_summary(self, _snapshot_id: str) -> dict[str, Any]:
                    raise AssertionError("resolved ref summary must not be reread")

            moving_catalog = MovingCatalog()
            exported = export_retained_repo_manifest_ref(
                first.repository_id,
                catalog=moving_catalog,
                object_store=first_store,
            )

            assert moving_catalog.calls == 1
            assert exported.receipt.snapshot_id == first.snapshot_id
            assert exported.canonical_manifest_bytes == (
                first_fixture.plan().canonical_manifest_bytes
            )
            assert first_catalog.resolve_ref(first.repository_id)["snapshot_id"] == (
                second.snapshot_id
            )
        finally:
            second_fixture.close()


def test_expected_ref_generation_conflicts_before_snapshot_or_object_access() -> None:
    repository_id = "repo_" + "0" * 64

    class ConflictCatalog:
        @staticmethod
        def retained_import_contract() -> str:
            return RETAINED_IMPORT_CATALOG_CONTRACT

        @staticmethod
        def resolve_ref(
            observed_repository_id: str,
            ref_name: str = "main",
        ) -> dict[str, Any]:
            return {
                "repository_id": observed_repository_id,
                "ref_name": ref_name,
                "snapshot_id": "snapshot_unread",
                "generation": 2,
                "updated_at": "2026-08-10T00:00:00+00:00",
                "manifest": object(),
            }

        @staticmethod
        def get_manifest_summary(_snapshot_id: str) -> dict[str, Any]:
            raise AssertionError("generation conflict reread a snapshot")

    class ForbiddenStore:
        @staticmethod
        def _forbidden() -> None:
            raise AssertionError("generation conflict reached object storage")

        def put_bytes(self, _data: bytes) -> BlobInfo:
            self._forbidden()

        def put_file(self, _source: str | Path) -> BlobInfo:
            self._forbidden()

        def has(self, _digest: str) -> bool:
            self._forbidden()

        def open(self, _digest: str) -> Any:
            self._forbidden()

        def read_bytes(self, _digest: str) -> bytes:
            self._forbidden()

        def verify(self, _digest: str) -> BlobInfo:
            self._forbidden()

        def materialize(self, _digest: str, _destination: str | Path) -> Path:
            self._forbidden()

        def verify_receipt(self, _expected: BlobInfo) -> BlobInfo:
            self._forbidden()

    with pytest.raises(PublishConflict, match="expected_generation"):
        export_retained_repo_manifest_ref(
            repository_id,
            catalog=ConflictCatalog(),
            object_store=ForbiddenStore(),
            expected_generation=1,
        )


def test_open_object_acquisition_interruption_closes_owned_reader() -> None:
    interruption = KeyboardInterrupt("object reader acquisition interrupted")

    class Reader:
        closed = False

        def close(self) -> None:
            self.closed = True

    class Store:
        def __init__(self) -> None:
            self.reader = Reader()

        @staticmethod
        def verify_receipt(expected: BlobInfo) -> BlobInfo:
            return expected

        def open(self, _digest: str) -> Reader:
            return self.reader

    store = Store()
    record = ObjectRecord(
        digest="0" * 64,
        byte_size=0,
        storage_key="objects/00/acquisition",
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        _interrupt_after_owned_acquire(
            lambda: manifest_export_module._run_open_object(
                store,  # type: ignore[arg-type]
                record,
                lambda _reader: None,
            ),
            error=interruption,
        )

    assert caught.value is interruption
    assert store.reader.closed


def test_zip_entry_acquisition_interruption_closes_owned_reader() -> None:
    interruption = SystemExit("ZIP entry acquisition interrupted")

    class Entry:
        closed = False

        def close(self) -> None:
            self.closed = True

    class Archive:
        def __init__(self) -> None:
            self.entry = Entry()

        def open(self, _path: str, *, mode: str) -> Entry:
            assert mode == "r"
            return self.entry

    archive = Archive()
    member = ArtifactMember(
        path="config.json",
        digest="0" * 64,
        byte_size=1,
    )

    with pytest.raises(SystemExit) as caught:
        _interrupt_after_owned_acquire(
            lambda: manifest_export_module._read_zip_json(
                archive,  # type: ignore[arg-type]
                {member.path: member},
                member.path,
                label="snapshot test config",
            ),
            error=interruption,
        )

    assert caught.value is interruption
    assert archive.entry.closed


@pytest.mark.parametrize(
    ("path", "label", "require_source_field"),
    (
        ("documents.json", "snapshot BM25 documents", False),
        (
            "root/documents_root.json",
            "snapshot vector root documents",
            True,
        ),
    ),
    ids=("bm25", "vector"),
)
def test_canonical_documents_acquisition_interruption_closes_owned_reader(
    path: str,
    label: str,
    require_source_field: bool,
) -> None:
    interruption = KeyboardInterrupt(f"{label} acquisition interrupted")

    class Entry:
        closed = False

        def close(self) -> None:
            self.closed = True

    class Archive:
        def __init__(self) -> None:
            self.entry = Entry()

        def open(self, observed_path: str, *, mode: str) -> Entry:
            assert observed_path == f"payload/{path}"
            assert mode == "r"
            return self.entry

    archive = Archive()
    member = ArtifactMember(path=path, digest="0" * 64, byte_size=1)

    with pytest.raises(KeyboardInterrupt) as caught:
        _interrupt_after_owned_acquire(
            lambda: manifest_export_module._read_canonical_documents(
                archive,  # type: ignore[arg-type]
                member,
                path,
                label=label,
                require_source_field=require_source_field,
            ),
            error=interruption,
        )

    assert caught.value is interruption
    assert archive.entry.closed


def test_canonical_documents_rejects_oversized_member_before_open() -> None:
    class Archive:
        @staticmethod
        def open(_path: str, *, mode: str) -> None:
            raise AssertionError(f"oversized member was opened in {mode!r} mode")

    member = ArtifactMember(
        path="documents.json",
        digest="0" * 64,
        byte_size=MAX_PORTABLE_DOCUMENTS_JSON_BYTES + 1,
    )

    with pytest.raises(StorageIntegrityError, match="exceeds its byte limit"):
        manifest_export_module._read_canonical_documents(
            Archive(),  # type: ignore[arg-type]
            member,
            member.path,
            label="snapshot test documents",
            require_source_field=False,
        )


@pytest.mark.parametrize(
    ("relative_path", "expected_file", "max_bytes"),
    (
        (
            "l2/documents_test__model.json",
            "documents_test__model.json",
            MAX_PORTABLE_DOCUMENTS_JSON_BYTES,
        ),
        (
            "l2/index_test__model.faiss",
            "index_test__model.faiss",
            MAX_PORTABLE_FAISS_INDEX_BYTES,
        ),
    ),
    ids=("documents", "faiss-index"),
)
def test_vector_record_rejects_member_above_portable_limit(
    relative_path: str,
    expected_file: str,
    max_bytes: int,
) -> None:
    digest = "0" * 64
    byte_size = max_bytes + 1
    member = ArtifactMember(
        path=relative_path,
        digest=digest,
        byte_size=byte_size,
    )
    record = {
        "file": expected_file,
        "size": byte_size,
        "sha256": digest,
    }

    with pytest.raises(StorageIntegrityError, match="exceeds its byte limit"):
        manifest_export_module._require_vector_record(
            record,
            {relative_path: member},
            relative_path=relative_path,
            expected_file=expected_file,
            label="snapshot vector l2 artifact",
            max_bytes=max_bytes,
        )


def test_zip_archive_acquisition_interruption_closes_owned_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interruption = KeyboardInterrupt("ZIP archive acquisition interrupted")

    class Archive:
        def __init__(self) -> None:
            self.fp: object | None = object()

        def close(self) -> None:
            self.fp = None

    archive = Archive()
    monkeypatch.setattr(
        manifest_export_module.zipfile,
        "ZipFile",
        lambda _handle, mode: archive,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        _interrupt_after_owned_acquire(
            lambda: manifest_export_module._run_zip_archive(
                object(),  # type: ignore[arg-type]
                label="snapshot test archive",
                operation=lambda _archive: None,
            ),
            error=interruption,
        )

    assert caught.value is interruption
    assert archive.fp is None


def test_owned_close_preserves_post_operation_ordinary_interruption() -> None:
    primary = LookupError("post-operation ordinary interruption")

    class Reader:
        closed = False

        def close(self) -> None:
            self.closed = True

    reader = Reader()
    with pytest.raises(LookupError) as caught:
        _interrupt_after_owned_operation(
            lambda: manifest_export_module._run_with_owned_close(
                lambda: reader,
                lambda _reader: "result",
                complete=lambda resource: resource.closed is True,
                cleanup_label="test reader close also failed",
                close_failure="test reader close failed",
            ),
            error=primary,
        )

    assert caught.value is primary
    assert reader.closed


def test_owned_close_preserves_unrelated_finalizer_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = RuntimeError("unrelated cleanup finalizer failure")

    class Reader:
        closed = False

        def close(self) -> None:
            self.closed = True

    reader = Reader()
    real_context = manifest_export_module._run_context_with_cleanup_actions

    @contextmanager
    def fail_after_cleanup(actions: Any) -> Iterator[None]:
        with real_context(actions):
            yield
        raise primary

    monkeypatch.setattr(
        manifest_export_module,
        "_run_context_with_cleanup_actions",
        fail_after_cleanup,
    )
    with pytest.raises(RuntimeError) as caught:
        manifest_export_module._run_with_owned_close(
            lambda: reader,
            lambda _reader: "result",
            complete=lambda resource: resource.closed is True,
            cleanup_label="test reader close also failed",
            close_failure="test reader close failed",
        )

    assert caught.value is primary
    assert reader.closed


def test_owned_close_preserves_completion_observer_cancellation() -> None:
    primary = KeyboardInterrupt("one-shot close observation cancellation")

    class Reader:
        physically_closed = False
        observations = 0

        @property
        def closed(self) -> bool:
            self.observations += 1
            if self.observations == 1:
                raise primary
            return self.physically_closed

        def close(self) -> None:
            self.physically_closed = True

    reader = Reader()
    with pytest.raises(KeyboardInterrupt) as caught:
        manifest_export_module._run_with_owned_close(
            lambda: reader,
            lambda _reader: "result",
            complete=lambda resource: resource.closed is True,
            cleanup_label="test reader close also failed",
            close_failure="test reader close failed",
        )

    assert caught.value is primary
    assert reader.physically_closed
    assert reader.observations >= 2


def test_owned_close_persistent_observer_failure_exposes_retry_owner() -> None:
    primary = OSError("persistent close observation failure")
    close_error = OSError("persistent physical close failure")

    class Reader:
        physically_closed = False
        allow_close = False
        allow_observation = False

        @property
        def closed(self) -> bool:
            if not self.allow_observation:
                raise primary
            return self.physically_closed

        def close(self) -> None:
            if not self.allow_close:
                raise close_error
            self.physically_closed = True

    reader = Reader()
    with pytest.raises(OSError) as caught:
        manifest_export_module._run_with_owned_close(
            lambda: reader,
            lambda _reader: "result",
            complete=lambda resource: resource.closed is True,
            cleanup_label="test reader close also failed",
            close_failure="test reader close failed",
        )

    assert caught.value is primary
    owners = _publication_cleanup_owners(primary)
    assert len(owners) == 1
    with pytest.raises(OSError) as retry:
        owners[0].close()
    assert retry.value is close_error
    reader.allow_close = True
    reader.allow_observation = True
    owners[0].close()
    assert owners[0].closed
    assert reader.physically_closed


def test_owned_close_does_not_wrap_shared_operation_primary() -> None:
    primary = OSError("shared operation and close failure")

    class Reader:
        closed = False
        allow_close = False

        def close(self) -> None:
            if not self.allow_close:
                raise primary
            self.closed = True

    reader = Reader()
    with pytest.raises(OSError) as caught:
        manifest_export_module._run_with_owned_close(
            lambda: reader,
            lambda _reader: (_ for _ in ()).throw(primary),
            complete=lambda resource: resource.closed is True,
            cleanup_label="test reader close also failed",
            close_failure="test reader close failed",
        )

    assert caught.value is primary
    owners = _publication_cleanup_owners(primary)
    assert len(owners) == 1
    reader.allow_close = True
    owners[0].close()
    assert reader.closed


def test_open_object_persistent_close_exposes_retry_owner() -> None:
    class Reader:
        closed = False
        allow_close = False

        def close(self) -> None:
            if not self.allow_close:
                raise OSError("persistent object reader close failure")
            self.closed = True

    class Store:
        def __init__(self) -> None:
            self.reader = Reader()

        @staticmethod
        def verify_receipt(expected: BlobInfo) -> BlobInfo:
            return expected

        def open(self, _digest: str) -> Reader:
            return self.reader

    store = Store()
    record = ObjectRecord(
        digest="0" * 64,
        byte_size=0,
        storage_key="objects/00/persistent-close",
    )

    with pytest.raises(StorageIntegrityError, match="reader close failed") as caught:
        manifest_export_module._run_open_object(
            store,  # type: ignore[arg-type]
            record,
            lambda _reader: None,
        )

    owners = _publication_cleanup_owners(caught.value)
    assert len(owners) == 1
    assert not owners[0].closed
    store.reader.allow_close = True
    owners[0].close()
    assert owners[0].closed
    assert store.reader.closed


def test_zip_archive_persistent_close_exposes_retry_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Archive:
        def __init__(self) -> None:
            self.fp: object | None = object()
            self.allow_close = False

        def close(self) -> None:
            if not self.allow_close:
                raise OSError("persistent ZIP archive close failure")
            self.fp = None

    archive = Archive()
    monkeypatch.setattr(
        manifest_export_module.zipfile,
        "ZipFile",
        lambda _handle, mode: archive,
    )

    with pytest.raises(StorageIntegrityError, match="archive close failed") as caught:
        manifest_export_module._run_zip_archive(
            object(),  # type: ignore[arg-type]
            label="snapshot test archive",
            operation=lambda _archive: None,
        )

    owners = _publication_cleanup_owners(caught.value)
    assert len(owners) == 1
    assert not owners[0].closed
    archive.allow_close = True
    owners[0].close()
    assert owners[0].closed
    assert archive.fp is None


def test_zip_json_entry_close_failure_preserves_read_cancellation() -> None:
    primary = KeyboardInterrupt("cancelled while reading ZIP entry")

    class FailingEntry:
        closed = False
        allow_close = False

        @staticmethod
        def read(_limit: int) -> bytes:
            raise primary

        def close(self) -> None:
            if not self.allow_close:
                raise OSError("ZIP entry close failed")
            self.closed = True

    class Archive:
        def __init__(self) -> None:
            self.entry = FailingEntry()

        def open(self, _path: str, *, mode: str) -> FailingEntry:
            assert mode == "r"
            return self.entry

    member = ArtifactMember(
        path="config.json",
        digest="0" * 64,
        byte_size=1,
    )

    archive = Archive()
    with pytest.raises(BaseException) as caught:
        manifest_export_module._read_zip_json(
            archive,  # type: ignore[arg-type]
            {member.path: member},
            member.path,
            label="snapshot test config",
        )

    assert caught.value is primary
    assert any(
        "snapshot test config close also failed" in note
        and "ZIP entry close failed" in note
        for note in _exception_notes(primary)
    )
    owners = _publication_cleanup_owners(primary)
    assert len(owners) == 1
    assert not owners[0].closed
    archive.entry.allow_close = True
    owners[0].close()
    assert owners[0].closed
    assert archive.entry.closed


@pytest.mark.parametrize(
    ("metadata", "documents"),
    (
        (
            {
                "project_root": "../../outside",
                "max_k": 17,
                "language": "english",
            },
            [
                {
                    "page_content": "VALUE = 1",
                    "metadata": {"file": "sample.py", "node_id": "sample.VALUE"},
                }
            ],
        ),
        (
            {"project_root": "source", "max_k": 17, "language": "english"},
            [
                {
                    "page_content": "VALUE = 1",
                    "metadata": {"file": "../escape.py", "node_id": "escape"},
                }
            ],
        ),
        (
            {"project_root": "source", "max_k": 17, "language": "english"},
            [
                {
                    "page_content": "Bearer audit-secret-value",
                    "metadata": {"file": "sample.py", "node_id": "sample.VALUE"},
                }
            ],
        ),
    ),
    ids=("metadata-conflict", "document-traversal", "document-secret"),
)
def test_coherently_reclosed_bm25_bundle_rejects_bad_portable_semantics(
    tmp_path: Path,
    metadata: dict[str, object],
    documents: list[dict[str, object]],
) -> None:
    fixture = _context_fixture(tmp_path / "fixture", ("bm25",))
    try:
        view_root = tmp_path / "malicious-bm25"
        view_root.mkdir()
        (view_root / "bm25_metadata.json").write_bytes(_json_bytes(metadata))
        (view_root / "documents.json").write_bytes(_json_bytes(documents))

        manifest = copy.deepcopy(fixture.plan().manifest)
        fingerprints = bm25_artifact_file_fingerprints(view_root)
        entry = manifest.indexes["bm25"]
        entry.config["artifact_file_fingerprints"] = copy.deepcopy(fingerprints)
        entry.metadata["artifact_file_fingerprints"] = copy.deepcopy(fingerprints)
        plan = plan_repo_manifest_import(manifest, views=("bm25",))
        bundle = build_view_bundle(
            view_root,
            tmp_path / "malicious-bm25.zip",
            view_type="bm25",
        )
        projected = manifest_export_module._ProjectedView(
            intent=plan.views[0],
            catalog_view=None,  # type: ignore[arg-type]
            members=bundle.members,
            member_receipt_items=(),
        )

        with bundle.path.open("rb") as archive_handle:
            with pytest.raises(StorageIntegrityError):
                manifest_export_module._require_bundle_semantics(
                    archive_handle,
                    projected,
                )
    finally:
        fixture.close()


def test_real_vector_import_and_export_allow_missing_document_count(
    tmp_path: Path,
) -> None:
    fixture = _vector_context_fixture(
        tmp_path / "missing-vector-count",
        document_count=None,
    )
    object_store = LocalCAS(tmp_path / "cas")
    catalog = SQLiteCatalog(tmp_path / "catalog.sqlite")
    try:
        plan = fixture.plan()
        assert "document_count" not in plan.manifest.indexes["vector"].config
        assert "document_count" not in plan.manifest.indexes["vector"].metadata
        imported = import_retained_repo_manifest(
            plan,
            artifact_owner=fixture.context_owner,
            repository_source=fixture.repository_source,
            repository_key=_REPOSITORY_KEY,
            catalog=catalog,
            object_store=object_store,
            environ={},
        )

        exported = export_retained_repo_manifest_snapshot(
            imported.repository_id,
            imported.snapshot_id,
            catalog=catalog,
            object_store=object_store,
        )

        exported_manifest = json.loads(exported.canonical_manifest_bytes)
        assert "document_count" not in exported_manifest["indexes"]["vector"]["config"]
        assert exported.receipt.views == ("vector",)
    finally:
        catalog.close()
        fixture.close()


def test_vector_export_semantics_reject_present_mismatched_document_count(
    tmp_path: Path,
) -> None:
    fixture = _vector_context_fixture(
        tmp_path / "mismatched-vector-count",
        document_count={"l2": 1},
    )
    try:
        manifest = copy.deepcopy(fixture.plan().manifest)
        entry = manifest.indexes["vector"]
        entry.config["document_count"] = {"l2": 2}
        entry.metadata["document_count"] = {"l2": 2}
        plan = plan_repo_manifest_import(manifest, views=("vector",))
        bundle = build_view_bundle(
            fixture.context / "views" / "vector",
            tmp_path / "mismatched-vector-count.zip",
            view_type="vector",
        )
        projected = manifest_export_module._ProjectedView(
            intent=plan.views[0],
            catalog_view=None,  # type: ignore[arg-type]
            members=bundle.members,
            member_receipt_items=(),
        )

        with bundle.path.open("rb") as archive_handle:
            with pytest.raises(StorageIntegrityError, match="semantic inventory"):
                manifest_export_module._require_bundle_semantics(
                    archive_handle,
                    projected,
                )
    finally:
        fixture.close()


def test_projection_member_object_envelope_conflict_is_rejected(
    tmp_path: Path,
) -> None:
    with _retained_fixture(tmp_path / "member-envelope", views=("bm25",)) as (
        _fixture,
        imported,
        object_store,
        catalog,
    ):
        summary = catalog.get_manifest_summary(imported.snapshot_id)
        retained = attest_detached_retained_snapshot(
            summary,
            label="test retained snapshot",
            expected_repository_id=imported.repository_id,
            expected_snapshot_id=imported.snapshot_id,
        )
        projection_record = summary["views"][REPO_MANIFEST_PROJECTION_VIEW]["object"]
        projection = json.loads(object_store.read_bytes(projection_record["digest"]))
        projection["views"]["bm25"]["members"][0]["object"][
            "media_type"
        ] = "application/json"

        with pytest.raises(StorageIntegrityError, match="identity is inconsistent"):
            manifest_export_module._validate_projection(
                projection,
                retained=retained,
                max_manifest_bytes=manifest_export_module.DEFAULT_MAX_MANIFEST_BYTES,
                max_bundle_files=manifest_export_module.DEFAULT_MAX_BUNDLE_FILES,
            )


def _receipt_fixture() -> tuple[BlobInfo, RepoManifestViewExportReceipt]:
    blob = BlobInfo(
        digest="0" * 64,
        byte_size=1,
        storage_key="objects/00/receipt",
    )
    view = RepoManifestViewExportReceipt(
        view_type="bm25",
        view_generation_id="view_generation",
        bundle_receipt=blob,
        member_receipt_items=(("documents.json", blob),),
    )
    return blob, view


@pytest.mark.parametrize("path", ("../escape", "/absolute"))
def test_view_export_receipt_rejects_noncanonical_member_paths(path: str) -> None:
    blob, _view = _receipt_fixture()

    with pytest.raises(StorageValidationError, match="path is not canonical"):
        RepoManifestViewExportReceipt(
            view_type="bm25",
            view_generation_id="view_generation",
            bundle_receipt=blob,
            member_receipt_items=((path, blob),),
        )


@pytest.mark.parametrize(
    "skipped_items",
    (
        (("vector", "not retained"), ("vector", "not retained")),
        (("bm25", "not retained"),),
    ),
    ids=("duplicate", "overlaps-exported-view"),
)
def test_export_receipt_rejects_noncanonical_skipped_view_closure(
    skipped_items: tuple[tuple[str, str], ...],
) -> None:
    blob, view = _receipt_fixture()
    namespace = NamespaceIdentity("default")
    repository = RepositoryIdentity(namespace.namespace_id, _REPOSITORY_KEY)

    with pytest.raises(StorageValidationError, match="not canonical"):
        RepoManifestExportReceipt(
            namespace_id=namespace.namespace_id,
            repository_id=repository.repository_id,
            repository_key=repository.repository_key,
            source_revision_id="source_revision",
            snapshot_id="snapshot",
            snapshot_published_at="2026-08-10T00:00:00+00:00",
            ref_name=None,
            ref_generation=None,
            ref_updated_at=None,
            manifest_digest="1" * 64,
            manifest_byte_size=1,
            projection_generation_id="projection_generation",
            projection_receipt=blob,
            view_receipts=(view,),
            skipped_items=skipped_items,
        )


def test_export_receipt_rejects_inconsistent_repository_identity() -> None:
    blob, view = _receipt_fixture()
    namespace = NamespaceIdentity("default")

    with pytest.raises(StorageValidationError, match="identity is inconsistent"):
        RepoManifestExportReceipt(
            namespace_id=namespace.namespace_id,
            repository_id="repo_" + "0" * 64,
            repository_key=_REPOSITORY_KEY,
            source_revision_id="source_revision",
            snapshot_id="snapshot",
            snapshot_published_at="2026-08-10T00:00:00+00:00",
            ref_name=None,
            ref_generation=None,
            ref_updated_at=None,
            manifest_digest="1" * 64,
            manifest_byte_size=1,
            projection_generation_id="projection_generation",
            projection_receipt=blob,
            view_receipts=(view,),
            skipped_items=(),
        )
