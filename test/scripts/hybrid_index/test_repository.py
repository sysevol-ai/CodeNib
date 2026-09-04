# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
import stat
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

import codenib.mcp.server as server_module
import scripts.experimental.hybrid_index.repository as repository_module
from codenib.artifacts import (
    CONTEXT_ARTIFACT_MANIFEST,
    query_context_artifact,
    stage_context_artifact,
    verify_context_artifact,
)
from codenib.compiler.index_builders import BM25IndexBuilder, IndexBuilderRegistry
from codenib.compiler.index_compiler import IndexCompiler, IndexCompilerConfig
from codenib.compiler.manifest import MANIFEST_FILENAME
from scripts.experimental.hybrid_index.contracts import (
    PublishConflict,
    StorageIntegrityError,
    StorageNotFound,
)
from scripts.experimental.hybrid_index.repository import (
    IndexRepository,
    _write_deterministic_archive,
)

_REPOSITORY = "example/h1-bm25"


@dataclass(frozen=True, slots=True)
class _BuiltArtifact:
    root: Path
    commit: str
    cache: Path


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _initialize_repository(root: Path, source: str) -> Path:
    repository = root / "checkout"
    repository.mkdir()
    (repository / "billing.py").write_text(source, encoding="utf-8")
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.name", "CodeNib Test")
    _git(repository, "config", "user.email", "codenib@example.invalid")
    _git(repository, "add", "billing.py")
    _git(repository, "commit", "--quiet", "-m", "initial fixture")
    return repository


def _commit_source(repository: Path, source: str, message: str) -> str:
    (repository / "billing.py").write_text(source, encoding="utf-8")
    _git(repository, "add", "billing.py")
    _git(repository, "commit", "--quiet", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _build_portable_bm25(
    root: Path,
    repository: Path,
    *,
    generation: str,
) -> _BuiltArtifact:
    registry = IndexBuilderRegistry()
    registry.register("bm25", BM25IndexBuilder(languages=["python"]))
    compiler = IndexCompiler(
        registry,
        IndexCompilerConfig(index_types=["bm25"], languages=["python"]),
    )
    cache = root / f"compiler-{generation}"
    manifest = compiler.compile_repo(str(repository), cache_dir=str(cache))
    assert manifest.indexes["bm25"].status == "fresh"

    artifact = root / f"artifact-{generation}"
    stage_context_artifact(
        repository,
        cache / MANIFEST_FILENAME,
        artifact,
        repository=_REPOSITORY,
        views=["bm25"],
    )
    verified = verify_context_artifact(
        artifact,
        expected_repository=_REPOSITORY,
        expected_commit=manifest.commit,
    )
    assert verified.views == ("bm25",)
    return _BuiltArtifact(root=artifact, commit=manifest.commit, cache=cache)


def _initial_source(token: str = "rare_invoice_token") -> str:
    return (
        "def calculate_tax(invoice_total):\n"
        f'    """Apply the {token} formula."""\n'
        "    return invoice_total * 0.07\n"
    )


def test_real_bm25_round_trip_reaches_the_mcp_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _initialize_repository(tmp_path, _initial_source())
    built = _build_portable_bm25(
        tmp_path,
        repository,
        generation="first",
    )
    store = IndexRepository.open(tmp_path / "h1-store")

    publication = store.publish_bm25(built.root)
    resolved = store.resolve_ref(_REPOSITORY)
    materialized = tmp_path / "materialized"
    artifact = store.materialize_snapshot(publication.snapshot_id, materialized)

    assert resolved.ref is not None
    assert resolved.ref.snapshot_id == publication.snapshot_id
    assert resolved.ref.revision == 1
    assert artifact.commit == built.commit
    assert artifact.views == ("bm25",)

    built.cache.rename(tmp_path / "compiler-cache-offline")
    repository.rename(tmp_path / "checkout-offline")
    store.root.rename(tmp_path / "h1-store-offline")
    assert not built.cache.exists()
    assert not repository.exists()
    assert not store.root.exists()

    monkeypatch.setattr(server_module, "_ctx", None)
    binding = query_context_artifact(
        materialized,
        expected_repository=_REPOSITORY,
        expected_commit=built.commit,
    )
    context = None
    try:
        server_module.init_server(
            binding.manifest,
            artifact={
                "verified": True,
                "schema": artifact.metadata["schema"],
                "repository": artifact.repository,
                "commit": artifact.commit,
                "views": list(artifact.views),
            },
            artifact_binding=binding,
        )
        binding.close()
        context = server_module.get_context()

        results = asyncio.run(
            server_module.search_bm25(
                query="rare_invoice_token calculate_tax",
                top_k=10,
            )
        )

        assert context.loaded_views == frozenset({"bm25"})
        assert not context.source_verified
        assert any(result["file"] == "billing.py" for result in results)
        assert any(
            "calculate_tax" in result["node_name"] for result in results
        ), results
        assert all(result.get("content") is None for result in results)
    finally:
        if context is not None:
            context.close()
        binding.close()
        server_module._ctx = None


def test_archive_bytes_and_zip_metadata_are_deterministic(tmp_path: Path) -> None:
    repository = _initialize_repository(tmp_path, _initial_source())
    built = _build_portable_bm25(
        tmp_path,
        repository,
        generation="deterministic",
    )
    artifact = verify_context_artifact(built.root)
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"

    _write_deterministic_archive(artifact, first)
    _write_deterministic_archive(artifact, second)

    first_bytes = first.read_bytes()
    assert first_bytes == second.read_bytes()
    assert (
        hashlib.sha256(first_bytes).hexdigest()
        == hashlib.sha256(second.read_bytes()).hexdigest()
    )
    with zipfile.ZipFile(first) as archive:
        members = archive.infolist()
    assert [member.filename for member in members] == sorted(
        member.filename for member in members
    )
    assert all(not member.is_dir() for member in members)
    assert all(member.date_time == (1980, 1, 1, 0, 0, 0) for member in members)
    assert all(member.compress_type == zipfile.ZIP_STORED for member in members)
    assert all(member.create_system == 3 for member in members)
    assert all(stat.S_IMODE(member.external_attr >> 16) == 0o644 for member in members)


def test_exact_publication_retry_reuses_one_archive_and_revision(
    tmp_path: Path,
) -> None:
    repository = _initialize_repository(tmp_path, _initial_source())
    built = _build_portable_bm25(tmp_path, repository, generation="retry")
    store = IndexRepository.open(tmp_path / "h1-store")

    first = store.publish_bm25(built.root, expected_revision=0)
    object_paths = tuple((store.root / "objects/sha256").glob("*/*"))
    first_bytes = sum(path.stat().st_size for path in object_paths)
    second = store.publish_bm25(built.root, expected_revision=0)
    retried_paths = tuple((store.root / "objects/sha256").glob("*/*"))

    assert second == first
    assert retried_paths == object_paths
    assert sum(path.stat().st_size for path in retried_paths) == first_bytes


def test_invalid_artifact_does_not_publish_catalog_or_object_state(
    tmp_path: Path,
) -> None:
    invalid = tmp_path / "invalid-artifact"
    invalid.mkdir()
    (invalid / "not-context.txt").write_text("invalid", encoding="utf-8")
    store = IndexRepository.open(tmp_path / "h1-store")

    with pytest.raises(ValueError):
        store.publish_bm25(invalid)

    with pytest.raises(StorageNotFound):
        store.resolve_ref(_REPOSITORY)
    assert tuple((store.root / "objects/sha256").glob("*/*")) == ()


def test_artifact_change_during_pack_does_not_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _initialize_repository(tmp_path, _initial_source())
    built = _build_portable_bm25(tmp_path, repository, generation="mutation")
    store = IndexRepository.open(tmp_path / "h1-store")
    real_reopen = repository_module.reopen_authenticated_directory
    changed = False

    def mutate_then_reopen(path, ownership, callback):
        nonlocal changed
        if not changed:
            changed = True
            documents = path / "views/bm25/documents.json"
            documents.write_bytes(documents.read_bytes() + b" ")
        return real_reopen(path, ownership, callback)

    monkeypatch.setattr(
        repository_module,
        "reopen_authenticated_directory",
        mutate_then_reopen,
    )

    with pytest.raises((RuntimeError, ValueError), match="changed|ownership"):
        store.publish_bm25(built.root)

    with pytest.raises(StorageNotFound):
        store.resolve_ref(_REPOSITORY)
    assert tuple((store.root / "objects/sha256").glob("*/*")) == ()


def test_catalog_failure_after_cas_leaves_only_an_unreachable_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _initialize_repository(tmp_path, _initial_source())
    built = _build_portable_bm25(tmp_path, repository, generation="db-failure")
    store = IndexRepository.open(tmp_path / "h1-store")

    def fail_before_ref_update() -> None:
        raise RuntimeError("injected catalog failure")

    monkeypatch.setattr(store.catalog, "_before_ref_update", fail_before_ref_update)

    with pytest.raises(RuntimeError, match="injected catalog failure"):
        store.publish_bm25(built.root)

    objects = tuple((store.root / "objects/sha256").glob("*/*"))
    assert len(objects) == 1
    assert store.objects.verify(objects[0].parent.name + objects[0].name)
    with pytest.raises(StorageNotFound):
        store.resolve_ref(_REPOSITORY)


def test_ref_advance_preserves_history_and_rejects_a_stale_writer(
    tmp_path: Path,
) -> None:
    repository = _initialize_repository(tmp_path, _initial_source("first_token"))
    first_artifact = _build_portable_bm25(
        tmp_path,
        repository,
        generation="first",
    )
    store = IndexRepository.open(tmp_path / "h1-store")
    first = store.publish_bm25(first_artifact.root)

    second_commit = _commit_source(
        repository,
        _initial_source("second_token"),
        "change formula token",
    )
    second_artifact = _build_portable_bm25(
        tmp_path,
        repository,
        generation="second",
    )
    second = store.publish_bm25(
        second_artifact.root,
        expected_revision=first.ref_revision,
    )

    current = store.resolve_ref(_REPOSITORY)
    assert current.ref is not None
    assert current.ref.snapshot_id == second.snapshot_id
    assert current.ref.revision == 2
    assert current.snapshot.commit == second_commit
    assert first.snapshot_id != second.snapshot_id

    old_destination = tmp_path / "materialized-first"
    historical = store.materialize_snapshot(first.snapshot_id, old_destination)
    assert historical.commit == first_artifact.commit
    assert historical.commit != second_commit
    assert (
        store.catalog.get_snapshot(first.snapshot_id).snapshot.snapshot_id
        == first.snapshot_id
    )

    with pytest.raises(PublishConflict, match="is at revision 2, not 1"):
        store.publish_bm25(
            first_artifact.root,
            expected_revision=first.ref_revision,
        )

    unchanged = store.resolve_ref(_REPOSITORY)
    assert unchanged.ref == current.ref
    assert unchanged.snapshot == current.snapshot


def test_corrupt_cas_archive_cannot_materialize_a_published_snapshot(
    tmp_path: Path,
) -> None:
    repository = _initialize_repository(tmp_path, _initial_source())
    built = _build_portable_bm25(
        tmp_path,
        repository,
        generation="corruption",
    )
    store = IndexRepository.open(tmp_path / "h1-store")
    publication = store.publish_bm25(built.root)
    closure = store.catalog.get_snapshot(publication.snapshot_id)
    generation = closure.snapshot.generations[0]
    archive = store.objects.verified_path(
        generation.archive_digest,
        expected_size=generation.archive_size,
    )
    corrupted = bytearray(archive.read_bytes())
    corrupted[0] ^= 0xFF
    archive.write_bytes(corrupted)
    destination = tmp_path / "must-not-publish"

    with pytest.raises(StorageIntegrityError, match="digest mismatch"):
        store.materialize_snapshot(publication.snapshot_id, destination)

    assert not destination.exists()
    assert store.resolve_ref(_REPOSITORY).snapshot.snapshot_id == (
        publication.snapshot_id
    )
    assert (built.root / CONTEXT_ARTIFACT_MANIFEST).is_file()

    archive.unlink()
    missing_destination = tmp_path / "missing-must-not-publish"
    with pytest.raises(FileNotFoundError):
        store.materialize_snapshot(publication.snapshot_id, missing_destination)
    assert not missing_destination.exists()

    archive.symlink_to(built.root / CONTEXT_ARTIFACT_MANIFEST)
    replaced_destination = tmp_path / "replaced-must-not-publish"
    with pytest.raises(StorageIntegrityError, match="not a regular file"):
        store.materialize_snapshot(publication.snapshot_id, replaced_destination)
    assert not replaced_destination.exists()


def test_materialize_rejects_a_symlinked_destination_ancestor(
    tmp_path: Path,
) -> None:
    repository = _initialize_repository(tmp_path, _initial_source())
    built = _build_portable_bm25(
        tmp_path,
        repository,
        generation="symlink-ancestor",
    )
    store = IndexRepository.open(tmp_path / "h1-store")
    publication = store.publish_bm25(built.root)
    victim = tmp_path / "victim"
    victim.mkdir()
    marker = victim / "valuable.txt"
    marker.write_text("preserve", encoding="utf-8")
    alias = tmp_path / "destination-alias"
    alias.symlink_to(victim, target_is_directory=True)

    with pytest.raises(ValueError, match="real directory"):
        store.materialize_snapshot(
            publication.snapshot_id,
            alias / "published",
        )

    assert alias.is_symlink()
    assert marker.read_text(encoding="utf-8") == "preserve"
    assert tuple(victim.iterdir()) == (marker,)
    assert not list(victim.glob(".*.normalize-*"))
