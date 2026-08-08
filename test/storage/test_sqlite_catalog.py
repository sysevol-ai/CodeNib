# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the SQLite immutable-generation catalog."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

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


def _setup_staged_view(catalog: SQLiteCatalog, suffix: str = "a"):
    repository_id = catalog.create_repository("owner/repo")
    source_revision_id = _source(catalog, repository_id)
    profile_id = catalog.create_view_profile("bm25", {})
    object_digest = _object(catalog, suffix)
    view_id = _stage(
        catalog,
        repository_id,
        source_revision_id,
        profile_id,
        object_digest,
    )
    return repository_id, source_revision_id, profile_id, object_digest, view_id


def test_initializes_pragmas_default_namespace_and_idempotent_reopen(tmp_path):
    path = tmp_path / "catalog.sqlite3"

    with SQLiteCatalog(path, busy_timeout_ms=1_234) as catalog:
        assert catalog.schema_version == LATEST_SCHEMA_VERSION
        assert (
            catalog._connection.execute("PRAGMA user_version").fetchone()[0]
            == LATEST_SCHEMA_VERSION
        )
        assert catalog._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert (
            catalog._connection.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
        )
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
        assert migration_count == LATEST_SCHEMA_VERSION


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
        assert published["changed"] is True
        assert resolved["snapshot_id"] == published["snapshot_id"]
        assert resolved["generation"] == 1
        assert published["updated_at"] == resolved["updated_at"]
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


def test_publish_retry_for_current_snapshot_is_desired_state_idempotent(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_revision_id, _, _, view_id = _setup_staged_view(catalog)

        first = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [view_id],
            expected_generation=0,
        )
        retry_with_current_generation = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [view_id],
            expected_generation=1,
        )
        retry_with_original_generation = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [view_id],
            expected_generation=0,
        )

        assert first["changed"] is True
        for retry in (retry_with_current_generation, retry_with_original_generation):
            assert retry == {
                "snapshot_id": first["snapshot_id"],
                "repository_id": repository_id,
                "ref_name": "main",
                "generation": 1,
                "updated_at": first["updated_at"],
                "changed": False,
            }
        assert catalog.resolve_ref(repository_id)["generation"] == 1


@pytest.mark.parametrize(
    ("table", "key_column", "dependency"),
    (
        ("repositories", "repository_id", "repository"),
        ("source_revisions", "source_revision_id", "source"),
        ("view_profiles", "profile_id", "profile"),
        ("objects", "digest", "object"),
    ),
)
def test_idempotent_retry_revalidates_input_dependencies(
    tmp_path, table, key_column, dependency
):
    path = tmp_path / f"{dependency}.sqlite3"
    with SQLiteCatalog(path) as catalog:
        (
            repository_id,
            source_revision_id,
            profile_id,
            object_digest,
            view_id,
        ) = _setup_staged_view(catalog)
        catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [view_id],
            expected_generation=0,
        )

    identifiers = {
        "repositories": repository_id,
        "source_revisions": source_revision_id,
        "view_profiles": profile_id,
        "objects": object_digest,
    }
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    if table == "objects":
        connection.execute("DROP TRIGGER referenced_objects_cannot_be_deleted")
    connection.execute(
        f"DELETE FROM {table} WHERE {key_column} = ?", (identifiers[table],)
    )
    connection.commit()
    connection.close()

    with SQLiteCatalog(path) as catalog:
        with pytest.raises(CatalogNotFoundError, match="not found"):
            catalog.publish_snapshot(
                repository_id,
                source_revision_id,
                [view_id],
                expected_generation=0,
            )


@pytest.mark.parametrize("dependency", ("repository", "source", "source_digest"))
def test_publish_recomputes_repository_and_source_content_identities(
    tmp_path, dependency
):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_revision_id, _, _, view_id = _setup_staged_view(catalog)
        mutations = {
            "repository": (
                "repositories_are_immutable",
                "UPDATE repositories SET repository_key = 'owner/tampered' "
                "WHERE repository_id = ?",
                repository_id,
            ),
            "source": (
                "source_revisions_are_immutable",
                "UPDATE source_revisions SET commit_sha = ? "
                "WHERE source_revision_id = ?",
                ("b" * 40, source_revision_id),
            ),
            "source_digest": (
                "source_revisions_are_immutable",
                "UPDATE source_revisions SET identity_digest = ? "
                "WHERE source_revision_id = ?",
                ("b" * 64, source_revision_id),
            ),
        }
        trigger, statement, parameters = mutations[dependency]
        catalog._connection.execute(f"DROP TRIGGER {trigger}")
        if isinstance(parameters, tuple):
            catalog._connection.execute(statement, parameters)
        else:
            catalog._connection.execute(statement, (parameters,))

        with pytest.raises(
            CatalogConflictError, match="repository or source revision identity"
        ):
            catalog.publish_snapshot(
                repository_id,
                source_revision_id,
                [view_id],
                expected_generation=0,
            )


@pytest.mark.parametrize("dependency", ("repository", "source"))
def test_publish_rejects_raw_replace_of_repository_or_source_identity(
    tmp_path, dependency
):
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        repository_id, source_revision_id, _, _, view_id = _setup_staged_view(catalog)

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("PRAGMA recursive_triggers = OFF")
    if dependency == "repository":
        row = connection.execute(
            "SELECT namespace_id, created_at FROM repositories "
            "WHERE repository_id = ?",
            (repository_id,),
        ).fetchone()
        connection.execute(
            """
            INSERT OR REPLACE INTO repositories(
                repository_id, namespace_id, repository_key, created_at
            ) VALUES (?, ?, 'owner/tampered', ?)
            """,
            (repository_id, row[0], row[1]),
        )
    else:
        row = connection.execute(
            "SELECT * FROM source_revisions WHERE source_revision_id = ?",
            (source_revision_id,),
        ).fetchone()
        connection.execute(
            """
            INSERT OR REPLACE INTO source_revisions(
                source_revision_id, repository_id, source_kind, commit_sha,
                tree_sha, source_fingerprint, identity_digest, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row[0],
                row[1],
                row[2],
                "b" * 40,
                row[4],
                row[5],
                row[6],
                row[7],
            ),
        )
    connection.commit()
    connection.close()

    with SQLiteCatalog(path) as catalog:
        with pytest.raises(
            CatalogConflictError, match="repository or source revision identity"
        ):
            catalog.publish_snapshot(
                repository_id,
                source_revision_id,
                [view_id],
                expected_generation=0,
            )


def test_raw_connection_cannot_replace_or_delete_referenced_object(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        _, _, _, object_digest, _ = _setup_staged_view(catalog)

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("PRAGMA recursive_triggers = OFF")
    original = connection.execute(
        "SELECT digest, storage_key, byte_size, media_type FROM objects"
    ).fetchone()
    assert original is not None
    with pytest.raises(sqlite3.IntegrityError, match="duplicate object insert"):
        connection.execute(
            """
            INSERT OR REPLACE INTO objects(
                digest, storage_key, byte_size, media_type, created_at
            ) VALUES (?, 'sha256/changed/object', 99, 'application/evil', 'now')
            """,
            (object_digest,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="duplicate object insert"):
        connection.execute(
            """
            INSERT OR REPLACE INTO objects(
                digest, storage_key, byte_size, media_type, created_at
            ) VALUES (?, ?, 99, 'application/evil', 'now')
            """,
            ("b" * 64, original[1]),
        )
    with pytest.raises(sqlite3.IntegrityError, match="referenced objects"):
        connection.execute("DELETE FROM objects WHERE digest = ?", (object_digest,))
    connection.rollback()
    assert (
        connection.execute(
            "SELECT digest, storage_key, byte_size, media_type FROM objects"
        ).fetchone()
        == original
    )
    connection.close()


def test_raw_connection_can_delete_unreferenced_object_for_future_gc(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        digest = _object(catalog, "a")

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("PRAGMA recursive_triggers = OFF")
    connection.execute("DELETE FROM objects WHERE digest = ?", (digest,))
    connection.commit()
    assert connection.execute("SELECT COUNT(*) FROM objects").fetchone()[0] == 0
    connection.close()


@pytest.mark.parametrize(
    "timestamp", ("now", "2026-01-01T00:00:00", "2026-01-01T01:00:00+01:00")
)
def test_idempotent_publish_rejects_noncanonical_publication_timestamps(
    tmp_path, timestamp
):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_revision_id, _, _, view_id = _setup_staged_view(catalog)
        published = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [view_id],
            expected_generation=0,
        )
        catalog._connection.execute(
            "UPDATE refs SET updated_at = ? WHERE repository_id = ?",
            (timestamp, repository_id),
        )

        with pytest.raises(CatalogConflictError, match="publication metadata"):
            catalog.publish_snapshot(
                repository_id,
                source_revision_id,
                [view_id],
                expected_generation=0,
            )
        with pytest.raises(CatalogConflictError, match="publication metadata"):
            catalog.resolve_ref(repository_id)
        assert published["changed"] is True


def test_publish_rejects_real_ref_generation_without_integer_coercion(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_revision_id, _, _, view_id = _setup_staged_view(catalog)
        catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [view_id],
            expected_generation=0,
        )
        catalog._connection.execute(
            "UPDATE refs SET generation = 1.5 WHERE repository_id = ?",
            (repository_id,),
        )

        with pytest.raises(CatalogConflictError, match="publication metadata"):
            catalog.publish_snapshot(
                repository_id,
                source_revision_id,
                [view_id],
                expected_generation=1,
            )
        with pytest.raises(CatalogConflictError, match="publication metadata"):
            catalog.resolve_ref(repository_id)


def test_new_publish_rejects_invalid_staged_readiness_and_rolls_back(tmp_path):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_revision_id, _, _, view_id = _setup_staged_view(catalog)
        catalog._connection.execute("PRAGMA ignore_check_constraints = ON")
        catalog._connection.execute(
            "UPDATE view_generations SET ready_at = 'now' "
            "WHERE view_generation_id = ?",
            (view_id,),
        )

        with pytest.raises(CatalogConflictError, match="readiness metadata"):
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


@pytest.mark.parametrize("target", ("snapshot", "view"))
def test_publish_rejects_noncanonical_ready_timestamps_on_all_reuse_paths(
    tmp_path, target
):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_revision_id, _, _, view_id = _setup_staged_view(catalog)
        published = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [view_id],
            expected_generation=0,
        )
        if target == "snapshot":
            catalog._connection.execute("DROP TRIGGER snapshot_seal_is_the_only_update")
            catalog._connection.execute(
                "UPDATE snapshots SET published_at = 'now' WHERE snapshot_id = ?",
                (published["snapshot_id"],),
            )
            message = "seal state conflicts"
        else:
            catalog._connection.execute(
                "DROP TRIGGER ready_view_generations_are_immutable"
            )
            catalog._connection.execute(
                "UPDATE view_generations SET ready_at = 'now' "
                "WHERE view_generation_id = ?",
                (view_id,),
            )
            message = "ready_at"

        for ref_name in ("main", "stable"):
            with pytest.raises(CatalogConflictError, match=message):
                catalog.publish_snapshot(
                    repository_id,
                    source_revision_id,
                    [view_id],
                    ref_name=ref_name,
                    expected_generation=0,
                )


@pytest.mark.parametrize(
    ("trigger", "corruption", "target", "message"),
    (
        (
            "ready_snapshot_views_cannot_be_deleted",
            "DELETE FROM snapshot_views WHERE view_generation_id = ?",
            "view",
            "membership conflicts",
        ),
        (
            "snapshot_seal_is_the_only_update",
            """
            UPDATE snapshots SET status = 'building', published_at = NULL
            WHERE snapshot_id = ?
            """,
            "snapshot",
            "seal state conflicts",
        ),
        (
            "ready_view_generations_are_immutable",
            """
            UPDATE view_generations SET status = 'staged', ready_at = NULL
            WHERE view_generation_id = ?
            """,
            "view",
            "membership conflicts",
        ),
        (
            "ready_view_generations_are_immutable",
            """
            UPDATE view_generations SET ready_at = ''
            WHERE view_generation_id = ?
            """,
            "view",
            "ready_at",
        ),
    ),
)
def test_idempotent_retry_rejects_corrupt_ready_state(
    tmp_path, trigger, corruption, target, message
):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_revision_id, _, _, view_id = _setup_staged_view(catalog)
        published = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [view_id],
            expected_generation=0,
        )
        catalog._connection.execute(f"DROP TRIGGER {trigger}")
        target_id = published["snapshot_id"] if target == "snapshot" else view_id
        catalog._connection.execute(corruption, (target_id,))

        with pytest.raises(CatalogConflictError, match=message):
            catalog.publish_snapshot(
                repository_id,
                source_revision_id,
                [view_id],
                expected_generation=0,
            )


@pytest.mark.parametrize("identity_column", ("profile_id", "object_digest"))
def test_idempotent_retry_rejects_generation_dependency_mismatch(
    tmp_path, identity_column
):
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository_id, source_revision_id, _, _, view_id = _setup_staged_view(catalog)
        catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [view_id],
            expected_generation=0,
        )
        replacements = {
            "profile_id": catalog.create_view_profile(
                "bm25", {"variant": "tampered"}, name="tampered"
            ),
            "object_digest": _object(catalog, "b"),
        }
        catalog._connection.execute(
            "DROP TRIGGER staged_view_generation_identity_is_immutable"
        )
        catalog._connection.execute("DROP TRIGGER ready_view_generations_are_immutable")
        catalog._connection.execute(
            f"""
            UPDATE view_generations SET {identity_column} = ?
            WHERE view_generation_id = ?
            """,
            (replacements[identity_column], view_id),
        )

        with pytest.raises(CatalogConflictError, match="generation identity conflicts"):
            catalog.publish_snapshot(
                repository_id,
                source_revision_id,
                [view_id],
                expected_generation=0,
            )


def test_two_connections_converge_on_the_same_desired_snapshot(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        repository_id, source_revision_id, _, _, view_id = _setup_staged_view(catalog)

    barrier = Barrier(2)

    def publish():
        with SQLiteCatalog(path, busy_timeout_ms=5_000) as catalog:
            barrier.wait()
            return catalog.publish_snapshot(
                repository_id,
                source_revision_id,
                [view_id],
                expected_generation=0,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(publish) for _ in range(2)]
        results = [future.result() for future in futures]

    assert sorted(result["changed"] for result in results) == [False, True]
    assert {result["generation"] for result in results} == {1}
    assert {result["snapshot_id"] for result in results} == {results[0]["snapshot_id"]}
    assert {result["updated_at"] for result in results} == {results[0]["updated_at"]}
    with SQLiteCatalog(path) as catalog:
        assert catalog.resolve_ref(repository_id)["generation"] == 1


def test_two_connections_keep_cas_for_different_desired_snapshots(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(path) as catalog:
        repository_id, source_revision_id, profile_id, _, first_view = (
            _setup_staged_view(catalog)
        )
        second_view = _stage(
            catalog,
            repository_id,
            source_revision_id,
            profile_id,
            _object(catalog, "b"),
        )
        views = (first_view, second_view)

    barrier = Barrier(2)

    def publish(view_id):
        with SQLiteCatalog(path, busy_timeout_ms=5_000) as catalog:
            barrier.wait()
            try:
                result = catalog.publish_snapshot(
                    repository_id,
                    source_revision_id,
                    [view_id],
                    expected_generation=0,
                )
            except CatalogConflictError:
                return view_id, None
            return view_id, result

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(publish, view_id) for view_id in views]
        outcomes = [future.result() for future in futures]

    successes = [
        (view_id, result) for view_id, result in outcomes if result is not None
    ]
    conflicts = [(view_id, result) for view_id, result in outcomes if result is None]
    assert len(successes) == 1
    assert len(conflicts) == 1
    winning_view, published = successes[0]
    losing_view, _ = conflicts[0]
    assert published["changed"] is True
    assert published["generation"] == 1

    with SQLiteCatalog(path) as catalog:
        resolved = catalog.resolve_ref(repository_id)
        statuses = {
            row["view_generation_id"]: row["status"]
            for row in catalog._connection.execute(
                """
                SELECT view_generation_id, status FROM view_generations
                WHERE view_generation_id IN (?, ?)
                """,
                views,
            ).fetchall()
        }
        assert resolved["snapshot_id"] == published["snapshot_id"]
        assert statuses == {winning_view: "ready", losing_view: "staged"}


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
        assert alias["changed"] is True
        assert alias["generation"] == 1
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
