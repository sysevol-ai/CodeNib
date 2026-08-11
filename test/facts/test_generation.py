# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Durable per-file FactBatch generation and incremental reuse gates."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from codenib.facts import (
    CAPABILITY_DEFINITIONS,
    CAPABILITY_EDGES,
    CAPABILITY_OCCURRENCES,
    COMPLETENESS_PARTIAL,
    FACT_BATCH_GENERATION_MEDIA_TYPE,
    FACT_BATCH_GENERATION_SCHEMA,
    FACT_BATCH_VIEW_TYPE,
    PROVENANCE_CLANGD,
    ClangdFactProfile,
    DiagnosticFact,
    EdgeFact,
    FactBatch,
    FactBatchArtifactError,
    FactBatchGenerationError,
    FactBatchGenerationManifest,
    OccurrenceFact,
    SourceRange,
    SymbolFact,
    compare_fact_snapshots,
    decode_fact_batch_generation,
    load_fact_batch_generation,
    publish_fact_batch_generation,
    sha256_digest,
)
from codenib.storage import BlobInfo, LocalCAS, PublishConflict, SQLiteCatalog
from codenib.storage.models import canonical_json
from codenib.types import EDGE_TYPE_REFERENCE, NODE_TYPE_FUNCTION


def _profile() -> ClangdFactProfile:
    return ClangdFactProfile.from_runtime(
        analyzer_version="20.1.0",
        target_triple="x86_64-unknown-linux-gnu",
        toolchain_digest=sha256_digest("clangd"),
        compile_commands_digest=sha256_digest("compile_commands.json"),
        build_context_digest=sha256_digest("flags"),
        position_encoding="UTF16",
        native_contract={
            "format": "clangd-riff-fact-query-v3",
            "normalization_profile": "clangd-native-normalization-v3",
            "supported_versions": [18, 19, 20],
            "supported_position_encodings": ["UTF8", "UTF16", "UTF32"],
        },
    )


def _batch(
    profile: ClangdFactProfile,
    *,
    file_path: str,
    content: str,
    symbol_id: str,
    target_moniker: str | None = None,
) -> FactBatch:
    edges = (
        (
            EdgeFact(
                kind=EDGE_TYPE_REFERENCE,
                provenance=PROVENANCE_CLANGD,
                source_symbol_id=symbol_id,
                target_moniker=target_moniker,
                anchor_range=SourceRange(1, 2, 1, 5),
            ),
        )
        if target_moniker is not None
        else ()
    )
    occurrences = [
        OccurrenceFact(
            symbol_moniker=symbol_id,
            source_range=SourceRange(0, 0, 0, len(symbol_id)),
            roles=2,
        )
    ]
    if target_moniker is not None:
        occurrences.append(
            OccurrenceFact(
                symbol_moniker=target_moniker,
                source_range=SourceRange(1, 2, 1, 5),
                roles=4,
            )
        )
    return FactBatch(
        file_path=file_path,
        language="cpp",
        content_digest=sha256_digest(content),
        profile_digest=profile.digest,
        provider="clangd",
        completeness=COMPLETENESS_PARTIAL,
        position_encoding="UTF16",
        capabilities=(
            CAPABILITY_DEFINITIONS,
            CAPABILITY_EDGES,
            CAPABILITY_OCCURRENCES,
        ),
        symbols=(
            SymbolFact(
                symbol_id=symbol_id,
                moniker=symbol_id,
                display_name=symbol_id,
                kind=NODE_TYPE_FUNCTION,
                definition_range=SourceRange(0, 0, 0, len(symbol_id)),
            ),
        ),
        occurrences=tuple(occurrences),
        edges=edges,
    )


def _repository(catalog: SQLiteCatalog):
    repository_id = catalog.create_repository("owner/facts")
    source_one = catalog.create_source_revision(
        repository_id,
        commit_sha="commit-one",
        tree_sha="tree-one",
    )
    source_two = catalog.create_source_revision(
        repository_id,
        commit_sha="commit-two",
        tree_sha="tree-two",
    )
    source_three = catalog.create_source_revision(
        repository_id,
        commit_sha="commit-three",
        tree_sha="tree-three",
    )
    return repository_id, source_one, source_two, source_three


def test_incremental_generation_reuses_unchanged_units_and_converges_with_clean(
    tmp_path,
) -> None:
    profile = _profile()
    store = LocalCAS(tmp_path / "objects")
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_one, source_two, source_three = _repository(catalog)
        caller = _batch(
            profile,
            file_path="src/caller.cc",
            content="caller v1",
            symbol_id="caller",
            target_moniker="target",
        )
        target = _batch(
            profile,
            file_path="src/target.cc",
            content="target",
            symbol_id="target",
        )
        first = publish_fact_batch_generation(
            catalog,
            store,
            repository_id=repository_id,
            source_revision_id=source_one,
            profile=profile,
            upserts=(caller, target),
            replace_all=True,
        )
        changed_caller = _batch(
            profile,
            file_path="src/caller.cc",
            content="caller v2",
            symbol_id="caller",
            target_moniker="target",
        )
        incremental = publish_fact_batch_generation(
            catalog,
            store,
            repository_id=repository_id,
            source_revision_id=source_two,
            profile=profile,
            upserts=(changed_caller,),
            expected_generation=first.ref_generation,
        )

        assert incremental.reused_units == 1
        assert incremental.published_units == 1
        loaded_incremental = load_fact_batch_generation(catalog, store, repository_id)
        clean = publish_fact_batch_generation(
            catalog,
            store,
            repository_id=repository_id,
            source_revision_id=source_two,
            profile=profile,
            upserts=(changed_caller, target),
            replace_all=True,
            ref_name="semantic-facts-clean",
        )
        loaded_clean = load_fact_batch_generation(
            catalog,
            store,
            repository_id,
            ref_name="semantic-facts-clean",
        )

        report = compare_fact_snapshots(
            loaded_incremental.batches,
            loaded_clean.batches,
            strict=True,
        )
        assert report.converged is True
        assert incremental.manifest.batch_set_digest == clean.manifest.batch_set_digest
        assert {pointer.moniker for pointer in incremental.manifest.definitions} == {
            "caller",
            "target",
        }

        summary = catalog.resolve_ref(repository_id, "semantic-facts")["manifest"]
        view = summary["views"][FACT_BATCH_VIEW_TYPE]
        member_digests = {member["digest"] for member in view["member_objects"]}
        assert member_digests == {
            unit.receipt.object_digest for unit in incremental.manifest.units
        }
        assert loaded_incremental.reachable_object_digests == (
            member_digests | {view["object"]["digest"]}
        )

        after_delete = publish_fact_batch_generation(
            catalog,
            store,
            repository_id=repository_id,
            source_revision_id=source_three,
            profile=profile,
            upserts=(),
            deletes=("src/target.cc",),
            expected_generation=incremental.ref_generation,
        )
        loaded_delete = load_fact_batch_generation(catalog, store, repository_id)
        assert after_delete.reused_units == 1
        assert [batch.file_path for batch in loaded_delete.batches] == ["src/caller.cc"]
        assert loaded_delete.batches[0].edges[0].target_moniker == "target"
        assert {pointer.moniker for pointer in after_delete.manifest.definitions} == {
            "caller"
        }


def test_failed_publication_keeps_previous_ready_ref_authoritative(tmp_path) -> None:
    profile = _profile()
    store = LocalCAS(tmp_path / "objects")
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_one, source_two, _ = _repository(catalog)
        first = publish_fact_batch_generation(
            catalog,
            store,
            repository_id=repository_id,
            source_revision_id=source_one,
            profile=profile,
            upserts=(
                _batch(
                    profile,
                    file_path="src/main.cc",
                    content="v1",
                    symbol_id="main",
                ),
            ),
            replace_all=True,
        )

        with pytest.raises(PublishConflict, match="generation is 1"):
            publish_fact_batch_generation(
                catalog,
                store,
                repository_id=repository_id,
                source_revision_id=source_two,
                profile=profile,
                upserts=(
                    _batch(
                        profile,
                        file_path="src/main.cc",
                        content="v2",
                        symbol_id="main",
                    ),
                ),
                replace_all=True,
                expected_generation=0,
            )

        resolved = catalog.resolve_ref(repository_id, "semantic-facts")
        assert resolved["snapshot_id"] == first.snapshot_id
        assert resolved["generation"] == first.ref_generation
        assert load_fact_batch_generation(catalog, store, repository_id).batches[
            0
        ].content_digest == sha256_digest("v1")


def test_profile_change_requires_complete_rebuild(tmp_path) -> None:
    profile = _profile()
    changed_profile = ClangdFactProfile.from_runtime(
        analyzer_version="20.2.0",
        target_triple=profile.target_triple,
        toolchain_digest=profile.toolchain_digest,
        compile_commands_digest=profile.compile_commands_digest,
        build_context_digest=profile.build_context_digest,
        position_encoding=profile.position_encoding,
        native_contract={
            "format": profile.native_format,
            "normalization_profile": profile.normalization_profile,
            "supported_versions": list(profile.supported_riff_versions),
            "supported_position_encodings": ["UTF16"],
        },
    )
    store = LocalCAS(tmp_path / "objects")
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_one, source_two, _ = _repository(catalog)
        first = publish_fact_batch_generation(
            catalog,
            store,
            repository_id=repository_id,
            source_revision_id=source_one,
            profile=profile,
            upserts=(
                _batch(
                    profile,
                    file_path="src/main.cc",
                    content="v1",
                    symbol_id="main",
                ),
            ),
            replace_all=True,
        )
        rebuilt = _batch(
            changed_profile,
            file_path="src/main.cc",
            content="v2",
            symbol_id="main",
        )

        with pytest.raises(FactBatchGenerationError, match="complete.*rebuild"):
            publish_fact_batch_generation(
                catalog,
                store,
                repository_id=repository_id,
                source_revision_id=source_two,
                profile=changed_profile,
                upserts=(rebuilt,),
                expected_generation=first.ref_generation,
            )
        replacement = publish_fact_batch_generation(
            catalog,
            store,
            repository_id=repository_id,
            source_revision_id=source_two,
            profile=changed_profile,
            upserts=(rebuilt,),
            replace_all=True,
            expected_generation=first.ref_generation,
        )
        assert replacement.reused_units == 0
        assert replacement.published_units == 1


def test_same_reuse_identity_rejects_nondeterministic_generation(tmp_path) -> None:
    profile = _profile()
    store = LocalCAS(tmp_path / "objects")
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_one, source_two, _ = _repository(catalog)
        original = _batch(
            profile,
            file_path="src/main.cc",
            content="unchanged",
            symbol_id="main",
        )
        first = publish_fact_batch_generation(
            catalog,
            store,
            repository_id=repository_id,
            source_revision_id=source_one,
            profile=profile,
            upserts=(original,),
            replace_all=True,
        )
        nondeterministic = replace(
            original,
            diagnostics=(
                DiagnosticFact(
                    severity="warning",
                    code="changed-output",
                    message="same identity produced different facts",
                ),
            ),
        )

        with pytest.raises(FactBatchGenerationError, match="different facts"):
            publish_fact_batch_generation(
                catalog,
                store,
                repository_id=repository_id,
                source_revision_id=source_two,
                profile=profile,
                upserts=(nondeterministic,),
                expected_generation=first.ref_generation,
            )

        assert (
            catalog.resolve_ref(repository_id, "semantic-facts")["snapshot_id"]
            == first.snapshot_id
        )


def test_generation_rejects_non_cpp_batch(tmp_path) -> None:
    profile = _profile()
    store = LocalCAS(tmp_path / "objects")
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_one, _, _ = _repository(catalog)
        batch = replace(
            _batch(
                profile,
                file_path="src/main.cc",
                content="main",
                symbol_id="main",
            ),
            language="python",
        )

        with pytest.raises(ValueError, match="differs from the clangd profile"):
            publish_fact_batch_generation(
                catalog,
                store,
                repository_id=repository_id,
                source_revision_id=source_one,
                profile=profile,
                upserts=(batch,),
                replace_all=True,
            )


class _DishonestManifestStore:
    def __init__(self, delegate: LocalCAS) -> None:
        self.delegate = delegate
        self.put_count = 0
        self.false_receipt: BlobInfo | None = None

    def put_bytes(self, payload: bytes) -> BlobInfo:
        self.put_count += 1
        receipt = self.delegate.put_bytes(payload)
        if self.put_count == 1:
            return receipt
        self.false_receipt = BlobInfo(
            digest="0" * 64,
            byte_size=receipt.byte_size,
            storage_key=receipt.storage_key,
        )
        return self.false_receipt

    def verify(self, digest: str) -> BlobInfo:
        if self.false_receipt is not None and digest == self.false_receipt.digest:
            return self.false_receipt
        return self.delegate.verify(digest)

    def __getattr__(self, name: str):
        return getattr(self.delegate, name)


def test_generation_rejects_inconsistent_manifest_store_receipt(tmp_path) -> None:
    profile = _profile()
    store = _DishonestManifestStore(LocalCAS(tmp_path / "objects"))
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_one, _, _ = _repository(catalog)

        with pytest.raises(FactBatchGenerationError, match="receipt is inconsistent"):
            publish_fact_batch_generation(
                catalog,
                store,
                repository_id=repository_id,
                source_revision_id=source_one,
                profile=profile,
                upserts=(
                    _batch(
                        profile,
                        file_path="src/main.cc",
                        content="main",
                        symbol_id="main",
                    ),
                ),
                replace_all=True,
            )


def test_generation_rejects_tampered_unit_receipt(tmp_path) -> None:
    profile = _profile()
    store = LocalCAS(tmp_path / "objects")
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_one, _, _ = _repository(catalog)
        published = publish_fact_batch_generation(
            catalog,
            store,
            repository_id=repository_id,
            source_revision_id=source_one,
            profile=profile,
            upserts=(
                _batch(
                    profile,
                    file_path="src/main.cc",
                    content="main",
                    symbol_id="main",
                ),
            ),
            replace_all=True,
        )
        payload = published.manifest.to_dict()
        payload["units"][0]["receipt"]["byte_size"] += 1
        tampered = canonical_json(payload).encode("utf-8")
        assert decode_fact_batch_generation(tampered).units[0].receipt.byte_size > 1
        manifest_object = store.put_bytes(tampered)
        catalog.register_object(
            manifest_object.digest,
            storage_key=manifest_object.storage_key,
            byte_size=manifest_object.byte_size,
            media_type=FACT_BATCH_GENERATION_MEDIA_TYPE,
        )
        profile_id = catalog.create_view_profile(
            FACT_BATCH_VIEW_TYPE,
            profile.catalog_config(),
            name="clangd",
        )
        unit_digest = published.manifest.units[0].receipt.object_digest
        view_id = catalog.stage_view_generation(
            repository_id,
            source_one,
            profile_id,
            FACT_BATCH_VIEW_TYPE,
            manifest_object.digest,
            schema_version=FACT_BATCH_GENERATION_SCHEMA,
            member_object_digests=(unit_digest,),
        )
        catalog.publish_snapshot(
            repository_id,
            source_one,
            (view_id,),
            ref_name="tampered",
            expected_generation=0,
        )

        with pytest.raises(FactBatchArtifactError, match="differs from object store"):
            load_fact_batch_generation(
                catalog,
                store,
                repository_id,
                ref_name="tampered",
            )


def test_ready_generation_members_are_gc_roots_even_without_sqlite_fks(
    tmp_path,
) -> None:
    profile = _profile()
    store = LocalCAS(tmp_path / "objects")
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        repository_id, source_one, _, _ = _repository(catalog)
        published = publish_fact_batch_generation(
            catalog,
            store,
            repository_id=repository_id,
            source_revision_id=source_one,
            profile=profile,
            upserts=(
                _batch(
                    profile,
                    file_path="src/main.cc",
                    content="main",
                    symbol_id="main",
                ),
            ),
            replace_all=True,
        )
        member_digest = published.manifest.units[0].receipt.object_digest

    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("PRAGMA recursive_triggers = OFF")
        with pytest.raises(sqlite3.IntegrityError, match="member objects"):
            connection.execute("DELETE FROM objects WHERE digest = ?", (member_digest,))
    finally:
        connection.close()


def test_generation_encoding_is_canonical() -> None:
    profile = _profile()
    # A published manifest is exercised above; this checks canonical read
    # rejection without reaching into CAS internals.
    with pytest.raises(FactBatchGenerationError, match="size must be"):
        decode_fact_batch_generation(b"")
    with pytest.raises(ValueError, match="unsupported FactBatch schema"):
        FactBatchGenerationManifest(
            repository_id="repo",
            source_revision_id="source",
            profile_digest=profile.digest,
            provider="clangd",
            units=(),
            definitions=(),
            batch_set_digest=sha256_digest(canonical_json({"units": []})),
            fact_batch_schema_version=True,
        )
    assert profile.digest.startswith("sha256:")
