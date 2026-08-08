# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from codenib.storage.cas import LocalCAS
from codenib.storage.models import (
    ObjectRecord,
    PublishedSnapshot,
    RepositoryIdentity,
    SnapshotView,
    SourceRevision,
    ViewGeneration,
    ViewProfile,
)
from codenib.storage.protocols import IndexCatalog, JobCatalog, ObjectStore
from codenib.storage.sqlite_catalog import DEFAULT_NAMESPACE_ID, SQLiteCatalog


def test_embedded_backends_implement_storage_protocols(tmp_path) -> None:
    object_store = LocalCAS(tmp_path / "objects")
    catalog = SQLiteCatalog(tmp_path / "catalog.sqlite3")
    try:
        assert isinstance(object_store, ObjectStore)
        assert isinstance(catalog, IndexCatalog)
        assert isinstance(catalog, JobCatalog)
    finally:
        catalog.close()


def test_domain_and_sqlite_content_identities_match(tmp_path) -> None:
    object_store = LocalCAS(tmp_path / "objects")
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository = RepositoryIdentity(DEFAULT_NAMESPACE_ID, "owner/repo")
        repository_id = catalog.create_repository("owner/repo")
        assert repository_id == repository.repository_id

        source = SourceRevision.clean(
            repository_id,
            commit_sha="a" * 40,
            tree_sha="b" * 64,
        )
        source_revision_id = catalog.create_source_revision(
            repository_id,
            commit_sha="a" * 40,
            tree_sha="b" * 64,
        )
        assert source_revision_id == source.source_revision_id

        profile = ViewProfile.create("bm25", {"max_k": 128})
        profile_id = catalog.create_view_profile("bm25", {"max_k": 128})
        assert profile_id == profile.profile_id

        blob = object_store.put_bytes(b"immutable BM25 artifact")
        object_record = ObjectRecord(
            digest=blob.digest,
            byte_size=blob.byte_size,
            storage_key=blob.storage_key,
        )
        catalog.register_object(
            blob.digest,
            storage_key=blob.storage_key,
            byte_size=blob.byte_size,
        )

        generation = ViewGeneration.create(
            source,
            profile,
            object_record,
            schema_version="1",
            metadata={"document_count": 4},
        )
        generation_id = catalog.stage_view_generation(
            repository_id,
            source_revision_id,
            profile_id,
            "bm25",
            blob.digest,
            schema_version="1",
            metadata={"document_count": 4},
        )
        assert generation_id == generation.view_generation_id

        snapshot = PublishedSnapshot(
            repository_id,
            source_revision_id,
            (SnapshotView(generation),),
        )
        published = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [generation_id],
            expected_generation=0,
        )
        assert published["snapshot_id"] == snapshot.snapshot_id
