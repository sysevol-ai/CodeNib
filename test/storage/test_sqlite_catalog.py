# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the SQLite immutable-generation catalog."""

from __future__ import annotations

import sqlite3

import pytest

from codenib.storage.cas import LocalCAS
from codenib.storage.sqlite_catalog import (
    DEFAULT_NAMESPACE_ID,
    LATEST_SCHEMA_VERSION,
    CatalogConflictError,
    CatalogError,
    CatalogNotFoundError,
    CatalogValidationError,
    SQLiteCatalog,
)


def _source(catalog: SQLiteCatalog, repository_id: str, suffix: str = "a") -> str:
    return catalog.create_source_revision(
        repository_id,
        commit_sha=suffix * 40,
        tree_sha=suffix * 64,
    )


def _object(
    catalog: SQLiteCatalog,
    suffix: str,
    *,
    byte_size: int = 12,
) -> str:
    return catalog.register_object(
        suffix * 64,
        storage_key=f"sha256/{suffix * 2}/{suffix * 62}",
        byte_size=byte_size,
    )


def _stage(
    catalog: SQLiteCatalog,
    repository_id: str,
    source_revision_id: str,
    profile_id: str,
    object_digest: str,
    *,
    view_type: str = "bm25",
) -> str:
    return catalog.stage_view_generation(
        repository_id,
        source_revision_id,
        profile_id,
        view_type,
        object_digest,
        schema_version="1",
        metadata={"document_count": 4},
    )


def test_initializes_pragmas_default_namespace_and_idempotent_reopen(tmp_path):
    path = tmp_path / "catalog.sqlite3"

    with SQLiteCatalog(path, busy_timeout_ms=1_234) as catalog:
        assert catalog.schema_version == LATEST_SCHEMA_VERSION
        assert (
            catalog._connection.execute("PRAGMA user_version").fetchone()[0]
            == LATEST_SCHEMA_VERSION
        )
        assert catalog._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert catalog._connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert catalog._connection.execute("PRAGMA busy_timeout").fetchone()[0] == 1_234
        assert catalog._connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        repository_id = catalog.create_repository("sysevol-ai/CodeNib")

    with SQLiteCatalog(path) as reopened:
        assert reopened.schema_version == LATEST_SCHEMA_VERSION
        assert reopened.create_namespace("default") == DEFAULT_NAMESPACE_ID
        assert reopened.create_repository("sysevol-ai/CodeNib") == repository_id
        migration_count = reopened._connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]
        assert migration_count == 1


def test_reopen_rejects_mismatched_schema_version_records(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path):
        pass
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 0")
    connection.commit()
    connection.close()

    with pytest.raises(CatalogError, match="does not match schema_migrations"):
        SQLiteCatalog(path)


def test_reopen_rejects_catalog_newer_than_implementation(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path):
        pass
    future_version = LATEST_SCHEMA_VERSION + 1
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 'future')",
        (future_version,),
    )
    connection.execute(f"PRAGMA user_version = {future_version}")
    connection.commit()
    connection.close()

    with pytest.raises(CatalogError, match="newer than this CodeNib version"):
        SQLiteCatalog(path)


def test_repository_requires_an_existing_non_null_namespace(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        namespace_id = catalog.create_namespace("team-one")
        repository_id = catalog.create_repository(
            "owner/repo", namespace_id=namespace_id
        )
        repository = catalog._connection.execute(
            "SELECT namespace_id FROM repositories WHERE repository_id = ?",
            (repository_id,),
        ).fetchone()

        assert repository["namespace_id"] == namespace_id
        with pytest.raises(CatalogNotFoundError, match="namespace"):
            catalog.create_repository("owner/missing", namespace_id="ns_missing")
        with pytest.raises(CatalogValidationError, match="namespace ID"):
            catalog.create_repository("owner/null", namespace_id="")


def test_namespace_and_repository_content_identities_are_immutable(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        namespace_id = catalog.create_namespace("team-one")
        repository_id = catalog.create_repository(
            "owner/repo", namespace_id=namespace_id
        )

        with pytest.raises(sqlite3.IntegrityError, match="namespace.*immutable"):
            catalog._connection.execute(
                "UPDATE namespaces SET name = 'renamed' WHERE namespace_id = ?",
                (namespace_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="repository.*immutable"):
            catalog._connection.execute(
                """
                UPDATE repositories SET repository_key = 'owner/renamed'
                WHERE repository_id = ?
                """,
                (repository_id,),
            )


def test_commit_failure_rolls_back_and_leaves_connection_reusable(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog._connection.execute("PRAGMA defer_foreign_keys = ON")

        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            with catalog._transaction():
                catalog._connection.execute("""
                    INSERT INTO repositories(
                        repository_id, namespace_id, repository_key, created_at
                    ) VALUES ('invalid-repo', 'missing-namespace', 'invalid', 'now')
                    """)

        assert catalog._connection.in_transaction is False
        assert (
            catalog._connection.execute(
                "SELECT COUNT(*) FROM repositories WHERE repository_id = 'invalid-repo'"
            ).fetchone()[0]
            == 0
        )
        assert catalog.create_repository("owner/valid").startswith("repo_")


def test_clean_dirty_source_and_profile_identities_are_canonical(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id = catalog.create_repository("owner/repo")

        clean_one = catalog.create_source_revision(
            repository_id,
            commit_sha="A" * 40,
            tree_sha="B" * 64,
        )
        clean_two = catalog.create_source_revision(
            repository_id,
            commit_sha="a" * 40,
            tree_sha="b" * 64,
            source_fingerprint="ignored-for-clean-git-sources",
        )
        dirty_one = catalog.create_source_revision(
            repository_id,
            commit_sha="a" * 40,
            dirty=True,
            source_fingerprint="sha256:working-copy-one",
        )
        dirty_two = catalog.create_source_revision(
            repository_id,
            commit_sha="A" * 40,
            dirty=True,
            source_fingerprint="sha256:working-copy-one",
        )
        dirty_other = catalog.create_source_revision(
            repository_id,
            commit_sha="a" * 40,
            dirty=True,
            source_fingerprint="sha256:working-copy-two",
        )

        assert clean_one == clean_two
        assert dirty_one == dirty_two
        assert clean_one != dirty_one
        assert dirty_one != dirty_other
        with pytest.raises(CatalogValidationError, match="clean source tree"):
            catalog.create_source_revision(repository_id, commit_sha="a" * 40)
        with pytest.raises(CatalogValidationError, match="source fingerprint"):
            catalog.create_source_revision(repository_id, dirty=True)
        with pytest.raises(
            CatalogValidationError, match="must not include a base tree"
        ):
            catalog.create_source_revision(
                repository_id,
                commit_sha="a" * 40,
                tree_sha="b" * 64,
                dirty=True,
                source_fingerprint="sha256:working-copy-one",
            )

        profile_one = catalog.create_view_profile(
            "bm25",
            {"languages": ["python"], "options": {"b": 2, "a": 1}},
            name="default",
        )
        profile_two = catalog.create_view_profile(
            "bm25",
            {"options": {"a": 1, "b": 2}, "languages": ["python"]},
            name="default",
        )
        profile_other = catalog.create_view_profile(
            "vector",
            {"options": {"a": 1, "b": 2}, "languages": ["python"]},
            name="default",
        )

        assert profile_one == profile_two
        assert profile_one != profile_other
        with pytest.raises(CatalogValidationError, match="keys must be strings"):
            catalog.create_view_profile(
                "bm25",
                {"nested": [{1: "numeric key"}]},  # type: ignore[dict-item]
            )


def test_registered_objects_are_idempotent_but_immutable(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        digest = catalog.register_object(
            "A" * 64,
            storage_key="sha256/aa/object",
            byte_size=10,
            media_type="application/x-codenib-bm25",
        )

        assert digest == "a" * 64
        assert (
            catalog.register_object(
                f"sha256:{'a' * 64}",
                storage_key="sha256/aa/object",
                byte_size=10,
                media_type="application/x-codenib-bm25",
            )
            == digest
        )
        with pytest.raises(CatalogConflictError, match="immutable"):
            catalog.register_object(
                digest,
                storage_key="sha256/aa/replaced",
                byte_size=10,
                media_type="application/x-codenib-bm25",
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            catalog._connection.execute(
                "UPDATE objects SET storage_key = 'mutated' WHERE digest = ?",
                (digest,),
            )
        with pytest.raises(CatalogValidationError, match="relative and contained"):
            catalog.register_object(
                "b" * 64,
                storage_key="../../outside",
                byte_size=10,
            )
        with pytest.raises(CatalogValidationError, match="canonical"):
            catalog.register_object(
                "c" * 64,
                storage_key="sha256/./object",
                byte_size=10,
            )


def test_register_object_accepts_local_cas_blob_identity(tmp_path):
    blob = LocalCAS(tmp_path / "objects").put_bytes(b"compiled bm25 generation")

    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        digest = catalog.register_object(
            blob.digest,
            storage_key=blob.storage_key,
            byte_size=blob.byte_size,
        )
        stored = catalog._connection.execute(
            "SELECT digest, storage_key, byte_size FROM objects WHERE digest = ?",
            (digest,),
        ).fetchone()

        assert dict(stored) == {
            "digest": blob.digest,
            "storage_key": blob.storage_key,
            "byte_size": blob.byte_size,
        }


def test_stage_requires_registered_object_and_matching_repository(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_one = catalog.create_repository("owner/one")
        repository_two = catalog.create_repository("owner/two")
        source_one = _source(catalog, repository_one)
        profile_id = catalog.create_view_profile("bm25", {})

        with pytest.raises(CatalogNotFoundError, match="object"):
            _stage(
                catalog,
                repository_one,
                source_one,
                profile_id,
                "f" * 64,
            )

        digest = _object(catalog, "a")
        with pytest.raises(CatalogValidationError, match="another repository"):
            _stage(catalog, repository_two, source_one, profile_id, digest)
        with pytest.raises(CatalogValidationError, match="does not match"):
            _stage(
                catalog,
                repository_one,
                source_one,
                profile_id,
                digest,
                view_type="vector",
            )
        with pytest.raises(CatalogValidationError, match="keys must be strings"):
            catalog.stage_view_generation(
                repository_one,
                source_one,
                profile_id,
                "bm25",
                digest,
                schema_version="1",
                metadata={"nested": [{1: "numeric key"}]},  # type: ignore[dict-item]
            )


def test_publish_resolves_complete_pinned_manifest(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id = catalog.create_repository("owner/repo")
        source_revision_id = _source(catalog, repository_id)
        bm25_profile = catalog.create_view_profile(
            "bm25",
            {"languages": ["python"], "max_k": 128},
            name="python-sparse",
        )
        vector_profile = catalog.create_view_profile(
            "vector",
            {"languages": ["python"], "model": "test-embedding"},
            name="python-dense",
        )
        bm25 = _stage(
            catalog,
            repository_id,
            source_revision_id,
            bm25_profile,
            _object(catalog, "a", byte_size=20),
        )
        vector = _stage(
            catalog,
            repository_id,
            source_revision_id,
            vector_profile,
            _object(catalog, "b", byte_size=30),
            view_type="vector",
        )

        published = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [vector, bm25],
            expected_generation=0,
        )
        resolved = catalog.resolve_ref(repository_id)
        direct = catalog.get_manifest_summary(published["snapshot_id"])

        assert published["generation"] == 1
        assert resolved["snapshot_id"] == published["snapshot_id"]
        assert resolved["generation"] == 1
        assert resolved["manifest"] == direct
        assert direct["status"] == "ready"
        assert direct["source"]["source_revision_id"] == source_revision_id
        assert direct["source"]["kind"] == "clean"
        assert direct["views"]["bm25"]["profile"] == {
            "profile_id": bm25_profile,
            "name": "python-sparse",
            "config": {"languages": ["python"], "max_k": 128},
        }
        assert direct["views"]["vector"]["profile"] == {
            "profile_id": vector_profile,
            "name": "python-dense",
            "config": {"languages": ["python"], "model": "test-embedding"},
        }
        assert list(direct["views"]) == ["bm25", "vector"]
        assert direct["views"]["bm25"]["object"]["byte_size"] == 20
        assert direct["views"]["vector"]["object"]["byte_size"] == 30

        statuses = catalog._connection.execute(
            "SELECT DISTINCT status FROM view_generations"
        ).fetchall()
        assert [row["status"] for row in statuses] == ["ready"]


def test_stale_cas_rolls_back_and_keeps_old_ref(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id = catalog.create_repository("owner/repo")
        source_revision_id = _source(catalog, repository_id)
        profile_id = catalog.create_view_profile("bm25", {})
        old_view = _stage(
            catalog,
            repository_id,
            source_revision_id,
            profile_id,
            _object(catalog, "a"),
        )
        first = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [old_view],
            expected_generation=0,
        )
        replacement_view = _stage(
            catalog,
            repository_id,
            source_revision_id,
            profile_id,
            _object(catalog, "b"),
        )

        with pytest.raises(CatalogConflictError, match="generation is 1"):
            catalog.publish_snapshot(
                repository_id,
                source_revision_id,
                [replacement_view],
                expected_generation=0,
            )

        resolved = catalog.resolve_ref(repository_id)
        replacement_status = catalog._connection.execute(
            """
            SELECT status FROM view_generations WHERE view_generation_id = ?
            """,
            (replacement_view,),
        ).fetchone()["status"]
        snapshot_count = catalog._connection.execute(
            "SELECT COUNT(*) FROM snapshots"
        ).fetchone()[0]
        building_count = catalog._connection.execute(
            "SELECT COUNT(*) FROM snapshots WHERE status = 'building'"
        ).fetchone()[0]
        assert resolved["snapshot_id"] == first["snapshot_id"]
        assert resolved["generation"] == 1
        assert replacement_status == "staged"
        assert snapshot_count == 1
        assert building_count == 0

        second = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [replacement_view],
            expected_generation=1,
        )
        assert second["generation"] == 2
        assert (
            catalog.resolve_ref(repository_id)["snapshot_id"] == second["snapshot_id"]
        )


def test_ready_generation_can_be_reused_in_a_later_snapshot(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id = catalog.create_repository("owner/repo")
        source_revision_id = _source(catalog, repository_id)
        bm25_profile = catalog.create_view_profile("bm25", {"max_k": 128})
        vector_profile = catalog.create_view_profile(
            "vector", {"model": "test-embedding"}
        )
        bm25 = _stage(
            catalog,
            repository_id,
            source_revision_id,
            bm25_profile,
            _object(catalog, "a"),
        )
        first = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [bm25],
            expected_generation=0,
        )
        alias = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [bm25],
            ref_name="stable",
            expected_generation=0,
        )
        assert alias["snapshot_id"] == first["snapshot_id"]
        vector = _stage(
            catalog,
            repository_id,
            source_revision_id,
            vector_profile,
            _object(catalog, "b"),
            view_type="vector",
        )

        second = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [bm25, vector],
            expected_generation=1,
        )
        manifest = catalog.get_manifest_summary(second["snapshot_id"])

        assert second["generation"] == 2
        assert manifest["views"]["bm25"]["view_generation_id"] == bm25
        assert manifest["views"]["vector"]["view_generation_id"] == vector


def test_ready_snapshot_membership_stays_immutable_after_ref_moves(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id = catalog.create_repository("owner/repo")
        source_revision_id = _source(catalog, repository_id)
        bm25_profile = catalog.create_view_profile("bm25", {})
        old_view = _stage(
            catalog,
            repository_id,
            source_revision_id,
            bm25_profile,
            _object(catalog, "a"),
        )
        old_snapshot = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [old_view],
            expected_generation=0,
        )
        replacement_view = _stage(
            catalog,
            repository_id,
            source_revision_id,
            bm25_profile,
            _object(catalog, "b"),
        )
        catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [replacement_view],
            expected_generation=1,
        )
        vector_profile = catalog.create_view_profile("vector", {})
        vector_view = _stage(
            catalog,
            repository_id,
            source_revision_id,
            vector_profile,
            _object(catalog, "c"),
            view_type="vector",
        )

        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            catalog._connection.execute(
                """
                INSERT INTO snapshot_views(snapshot_id, view_type, view_generation_id)
                VALUES (?, 'vector', ?)
                """,
                (old_snapshot["snapshot_id"], vector_view),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            catalog._connection.execute(
                """
                DELETE FROM snapshot_views
                WHERE snapshot_id = ? AND view_generation_id = ?
                """,
                (old_snapshot["snapshot_id"], old_view),
            )

        catalog._connection.execute(
            "DELETE FROM snapshots WHERE snapshot_id = ?",
            (old_snapshot["snapshot_id"],),
        )
        assert (
            catalog._connection.execute(
                "SELECT COUNT(*) FROM snapshot_views WHERE snapshot_id = ?",
                (old_snapshot["snapshot_id"],),
            ).fetchone()[0]
            == 0
        )


def test_direct_sql_cannot_seal_invalid_or_expose_building_snapshots(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_one = catalog.create_repository("owner/one")
        source_one = _source(catalog, repository_one, "a")
        profile_id = catalog.create_view_profile("bm25", {})
        ready_view = _stage(
            catalog,
            repository_one,
            source_one,
            profile_id,
            _object(catalog, "a"),
        )
        catalog.publish_snapshot(
            repository_one,
            source_one,
            [ready_view],
            expected_generation=0,
        )

        repository_two = catalog.create_repository("owner/two")
        source_two = _source(catalog, repository_two, "b")
        staged_view = _stage(
            catalog,
            repository_two,
            source_two,
            profile_id,
            _object(catalog, "b"),
        )

        catalog._connection.execute(
            """
            INSERT INTO snapshots(
                snapshot_id, repository_id, source_revision_id,
                content_digest, status, published_at
            ) VALUES ('cross-source', ?, ?, 'cross', 'building', NULL)
            """,
            (repository_two, source_two),
        )
        catalog._connection.execute(
            """
            INSERT INTO snapshot_views(snapshot_id, view_type, view_generation_id)
            VALUES ('cross-source', 'bm25', ?)
            """,
            (ready_view,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="snapshot seal"):
            catalog._connection.execute("""
                UPDATE snapshots SET status = 'ready', published_at = 'now'
                WHERE snapshot_id = 'cross-source'
                """)

        catalog._connection.execute(
            """
            INSERT INTO snapshots(
                snapshot_id, repository_id, source_revision_id,
                content_digest, status, published_at
            ) VALUES ('staged-view', ?, ?, 'staged', 'building', NULL)
            """,
            (repository_two, source_two),
        )
        catalog._connection.execute(
            """
            INSERT INTO snapshot_views(snapshot_id, view_type, view_generation_id)
            VALUES ('staged-view', 'bm25', ?)
            """,
            (staged_view,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="snapshot seal"):
            catalog._connection.execute("""
                UPDATE snapshots SET status = 'ready', published_at = 'now'
                WHERE snapshot_id = 'staged-view'
                """)

        catalog._connection.execute(
            """
            INSERT INTO snapshots(
                snapshot_id, repository_id, source_revision_id,
                content_digest, status, published_at
            ) VALUES ('empty', ?, ?, 'empty', 'building', NULL)
            """,
            (repository_two, source_two),
        )
        with pytest.raises(sqlite3.IntegrityError, match="snapshot seal"):
            catalog._connection.execute("""
                UPDATE snapshots SET status = 'ready', published_at = 'now'
                WHERE snapshot_id = 'empty'
                """)
        with pytest.raises(CatalogValidationError, match="not ready"):
            catalog.get_manifest_summary("empty")

        with pytest.raises(sqlite3.IntegrityError, match="ready snapshots"):
            catalog._connection.execute(
                """
                INSERT INTO refs(
                    repository_id, ref_name, snapshot_id, generation, updated_at
                ) VALUES (?, 'unsafe', 'empty', 1, 'now')
                """,
                (repository_two,),
            )

        catalog._connection.execute(
            """
            INSERT INTO snapshots(
                snapshot_id, repository_id, source_revision_id,
                content_digest, status, published_at
            ) VALUES ('update-target', ?, ?, 'target', 'building', NULL)
            """,
            (repository_one, source_one),
        )
        with pytest.raises(CatalogValidationError, match="not ready"):
            catalog.get_manifest_summary("update-target")
        with pytest.raises(sqlite3.IntegrityError, match="ready snapshots"):
            catalog._connection.execute(
                """
                UPDATE refs SET snapshot_id = 'update-target', generation = 2
                WHERE repository_id = ? AND ref_name = 'main'
                """,
                (repository_one,),
            )


def test_late_ref_failure_rolls_back_building_snapshot_and_view_seal(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id = catalog.create_repository("owner/repo")
        source_revision_id = _source(catalog, repository_id)
        profile_id = catalog.create_view_profile("bm25", {})
        view_id = _stage(
            catalog,
            repository_id,
            source_revision_id,
            profile_id,
            _object(catalog, "a"),
        )
        catalog._connection.execute("""
            CREATE TRIGGER fail_ref_publication
            BEFORE INSERT ON refs
            BEGIN
                SELECT RAISE(ABORT, 'simulated ref failure');
            END
            """)

        with pytest.raises(sqlite3.IntegrityError, match="simulated ref failure"):
            catalog.publish_snapshot(
                repository_id,
                source_revision_id,
                [view_id],
                expected_generation=0,
            )

        assert (
            catalog._connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            == 0
        )
        assert (
            catalog._connection.execute(
                "SELECT status FROM view_generations WHERE view_generation_id = ?",
                (view_id,),
            ).fetchone()["status"]
            == "staged"
        )
        with pytest.raises(CatalogNotFoundError, match="ref not found"):
            catalog.resolve_ref(repository_id)


def test_cross_source_view_is_rejected_and_old_ref_is_unchanged(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id = catalog.create_repository("owner/repo")
        source_one = _source(catalog, repository_id, "a")
        source_two = _source(catalog, repository_id, "b")
        profile_id = catalog.create_view_profile("bm25", {})
        first_view = _stage(
            catalog,
            repository_id,
            source_one,
            profile_id,
            _object(catalog, "a"),
        )
        first = catalog.publish_snapshot(
            repository_id,
            source_one,
            [first_view],
            expected_generation=0,
        )
        other_source_view = _stage(
            catalog,
            repository_id,
            source_two,
            profile_id,
            _object(catalog, "b"),
        )

        with pytest.raises(CatalogValidationError, match="source identity"):
            catalog.publish_snapshot(
                repository_id,
                source_one,
                [other_source_view],
                expected_generation=1,
            )

        resolved = catalog.resolve_ref(repository_id)
        staged_status = catalog._connection.execute(
            """
            SELECT status FROM view_generations WHERE view_generation_id = ?
            """,
            (other_source_view,),
        ).fetchone()["status"]
        assert resolved["snapshot_id"] == first["snapshot_id"]
        assert resolved["generation"] == 1
        assert staged_status == "staged"


def test_ready_generation_cannot_be_mutated_or_deleted(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id = catalog.create_repository("owner/repo")
        source_revision_id = _source(catalog, repository_id)
        profile_id = catalog.create_view_profile("bm25", {})
        view_id = _stage(
            catalog,
            repository_id,
            source_revision_id,
            profile_id,
            _object(catalog, "a"),
        )
        catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [view_id],
            expected_generation=0,
        )

        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            catalog._connection.execute(
                """
                UPDATE view_generations SET metadata_json = '{}'
                WHERE view_generation_id = ?
                """,
                (view_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            catalog._connection.execute(
                "DELETE FROM view_generations WHERE view_generation_id = ?",
                (view_id,),
            )
