# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from codenib.storage.models import (
    ArtifactMember,
    ObjectRecord,
    PublishedSnapshot,
    RepositoryIdentity,
    SnapshotView,
    SourceRevision,
    StorageValidationError,
    ViewGeneration,
    ViewProfile,
    assert_no_secret_fields,
    canonical_json,
)


@pytest.mark.parametrize(
    "field",
    [
        "access_key",
        "cookie",
        "credential",
        "refresh_token",
        "secret",
        "Refresh-Token",
        "accessKey",
        "headers",
        "Proxy-Authorization",
        "proxy_authorization_options",
        "X-API-Key",
        "x-api-key-secondary",
        "X-Auth-Token",
        "aws_secret_access_key",
        "github_token",
        "auth_token",
        "my_client_secret",
    ],
)
def test_shared_secret_classifier_rejects_normalized_field_names(field: str) -> None:
    with pytest.raises(StorageValidationError, match="secret field"):
        assert_no_secret_fields({"nested": [{field: "value"}]}, source="profile")


@pytest.mark.parametrize(
    "value",
    [
        "https://user:password@example.test/api",
        "//user:password@example.test/api",
        "Bearer token-value",
        "Basic dXNlcjpwYXNz",
    ],
)
def test_shared_secret_classifier_rejects_credential_values(value: str) -> None:
    with pytest.raises(StorageValidationError, match="credentials"):
        assert_no_secret_fields({"endpoint": value}, source="profile")


def test_shared_secret_classifier_does_not_reject_noncredential_token_words() -> None:
    assert_no_secret_fields(
        {
            "authorization_url": "https://example.test/oauth/authorize",
            "token_count": 10,
            "tokenizer": "cl100k_base",
        },
        source="profile",
    )


def _object(suffix: str) -> ObjectRecord:
    return ObjectRecord(
        digest=suffix * 64,
        byte_size=12,
        storage_key=f"sha256/{suffix * 2}/{suffix * 62}",
    )


def test_canonical_json_and_profile_identity_ignore_mapping_order() -> None:
    first = {"languages": ["python"], "options": {"b": 2, "a": 1}}
    second = {"options": {"a": 1, "b": 2}, "languages": ["python"]}

    assert canonical_json(first) == canonical_json(second)
    assert (
        ViewProfile.create("vector", first).profile_id
        == ViewProfile.create("vector", second).profile_id
    )
    assert (
        ViewProfile.create("vector", first).profile_id
        != ViewProfile.create("bm25", first).profile_id
    )


def test_canonical_json_rejects_non_object_and_non_finite_value() -> None:
    with pytest.raises(StorageValidationError, match="must be an object"):
        canonical_json([])  # type: ignore[arg-type]
    with pytest.raises(StorageValidationError, match="canonical JSON"):
        canonical_json({"score": float("nan")})
    with pytest.raises(StorageValidationError, match="keys must be strings"):
        canonical_json({1: "numeric key"})  # type: ignore[dict-item]
    with pytest.raises(StorageValidationError, match="keys must be strings"):
        canonical_json({"nested": [{1: "numeric key"}]})  # type: ignore[dict-item]


def test_repository_requires_non_empty_namespace() -> None:
    with pytest.raises(StorageValidationError, match="namespace ID"):
        RepositoryIdentity("", "owner/repo")

    identity = RepositoryIdentity("default", "owner/repo")
    assert (
        identity.repository_id
        == RepositoryIdentity("default", "owner/repo").repository_id
    )


def test_clean_and_dirty_source_identities_are_distinct_and_deterministic() -> None:
    repository_id = RepositoryIdentity("default", "owner/repo").repository_id
    clean = SourceRevision.clean(
        repository_id,
        commit_sha="A" * 40,
        tree_sha="B" * 64,
    )
    same_clean = SourceRevision.clean(
        repository_id,
        commit_sha="a" * 40,
        tree_sha="b" * 64,
    )
    dirty = SourceRevision.dirty(
        repository_id,
        commit_sha="a" * 40,
        source_fingerprint="sha256:dirty-tree",
    )
    dirty_without_commit = SourceRevision.dirty(
        repository_id,
        commit_sha="   ",
        source_fingerprint="sha256:dirty-tree",
    )

    assert clean.source_revision_id == same_clean.source_revision_id
    assert clean.source_revision_id != dirty.source_revision_id
    assert dirty_without_commit.commit_sha is None
    with pytest.raises(StorageValidationError, match="commit and tree"):
        SourceRevision(
            repository_id,
            "clean",
            "a" * 40,
            None,
            "git-tree:missing",
        )
    with pytest.raises(StorageValidationError, match="source fingerprint"):
        SourceRevision.dirty(repository_id, source_fingerprint="")
    with pytest.raises(StorageValidationError, match="must not include a base tree"):
        SourceRevision(
            repository_id,
            "dirty",
            "a" * 40,
            "b" * 64,
            "sha256:dirty-tree",
        )


def test_snapshot_accepts_independent_profiles_for_one_source() -> None:
    repository_id = RepositoryIdentity("default", "owner/repo").repository_id
    source = SourceRevision.clean(
        repository_id,
        commit_sha="a" * 40,
        tree_sha="b" * 64,
    )
    bm25 = ViewGeneration.create(
        source,
        ViewProfile.create("bm25", {"max_k": 128}),
        _object("a"),
        schema_version="1",
    )
    vector = ViewGeneration.create(
        source,
        ViewProfile.create("vector", {"model": "test"}),
        _object("b"),
        schema_version="2",
    )

    snapshot = PublishedSnapshot(
        repository_id,
        source.source_revision_id,
        (SnapshotView(vector), SnapshotView(bm25)),
    )

    assert [view.view_type for view in snapshot.views] == ["bm25", "vector"]
    assert (
        snapshot.snapshot_id
        == PublishedSnapshot(
            repository_id,
            source.source_revision_id,
            (SnapshotView(bm25), SnapshotView(vector)),
        ).snapshot_id
    )


def test_snapshot_rejects_cross_source_and_duplicate_view_type() -> None:
    repository_id = RepositoryIdentity("default", "owner/repo").repository_id
    source_one = SourceRevision.clean(
        repository_id,
        commit_sha="a" * 40,
        tree_sha="a" * 64,
    )
    source_two = SourceRevision.clean(
        repository_id,
        commit_sha="b" * 40,
        tree_sha="b" * 64,
    )
    profile = ViewProfile.create("bm25", {})
    first = ViewGeneration.create(source_one, profile, _object("a"), schema_version="1")
    second_source = ViewGeneration.create(
        source_two, profile, _object("b"), schema_version="1"
    )
    duplicate = ViewGeneration.create(
        source_one, profile, _object("c"), schema_version="1"
    )

    with pytest.raises(StorageValidationError, match="source identity"):
        PublishedSnapshot(
            repository_id,
            source_one.source_revision_id,
            (SnapshotView(first), SnapshotView(second_source)),
        )
    with pytest.raises(StorageValidationError, match="duplicate view type"):
        PublishedSnapshot(
            repository_id,
            source_one.source_revision_id,
            (SnapshotView(first), SnapshotView(duplicate)),
        )


def test_artifact_member_is_contained_and_models_are_frozen() -> None:
    member = ArtifactMember("l2/index.faiss", "a" * 64, 10)
    assert member.path == "l2/index.faiss"

    with pytest.raises(StorageValidationError, match="relative and contained"):
        ArtifactMember("../outside", "a" * 64, 10)
    with pytest.raises(StorageValidationError, match="canonical"):
        ArtifactMember("./index.faiss", "a" * 64, 10)
    with pytest.raises(StorageValidationError, match="POSIX"):
        ArtifactMember("l2\\index.faiss", "a" * 64, 10)
    with pytest.raises(FrozenInstanceError):
        member.path = "changed"  # type: ignore[misc]


def test_object_storage_key_is_canonical_and_contained() -> None:
    record = _object("a")
    assert record.storage_key == f"sha256/{'a' * 2}/{'a' * 62}"

    with pytest.raises(StorageValidationError, match="relative and contained"):
        ObjectRecord("a" * 64, 1, "../../outside")
    with pytest.raises(StorageValidationError, match="canonical"):
        ObjectRecord("a" * 64, 1, "sha256/./object")


def test_generation_derives_view_identity_from_profile() -> None:
    repository_id = RepositoryIdentity("default", "owner/repo").repository_id
    source = SourceRevision.clean(
        repository_id,
        commit_sha="a" * 40,
        tree_sha="b" * 64,
    )
    profile = ViewProfile.create("bm25", {})
    generation = ViewGeneration.create(
        source,
        profile,
        _object("a"),
        schema_version="1",
    )

    assert generation.profile is profile
    assert generation.profile_id == profile.profile_id
    assert generation.view_type == "bm25"
    with pytest.raises(StorageValidationError, match="must be a ViewProfile"):
        ViewGeneration(
            repository_id,
            source.source_revision_id,
            "profile_not_a_profile",  # type: ignore[arg-type]
            "a" * 64,
            "1",
        )
