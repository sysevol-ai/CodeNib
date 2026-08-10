# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

from codenib.compiler.checkout_identity import validate_checkout_identity
from codenib.compiler.manifest import RepoManifest
from codenib.source_fingerprint import fingerprint_repository


def test_checkout_identity_does_not_exclude_repo_under_manifest_parent(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "source.py").write_text("value = 1\n", encoding="utf-8")
    fingerprint = fingerprint_repository(repo)
    manifest = RepoManifest(
        repo_path=str(repo),
        source_fingerprint=fingerprint.value,
        file_count=fingerprint.file_count,
        languages=["python"],
    )

    validate_checkout_identity(repo, manifest, artifact_root=tmp_path)


def test_checkout_identity_excludes_only_internal_artifact_tree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    artifact_root = repo / ".codenib"
    artifact_root.mkdir(parents=True)
    (repo / "source.py").write_text("value = 1\n", encoding="utf-8")
    fingerprint = fingerprint_repository(repo, exclude_roots=(artifact_root,))
    manifest = RepoManifest(
        repo_path=str(repo),
        source_fingerprint=fingerprint.value,
        file_count=fingerprint.file_count,
        languages=["python"],
    )
    (artifact_root / "runtime.json").write_text("{}\n", encoding="utf-8")

    validate_checkout_identity(repo, manifest, artifact_root=artifact_root)


def test_checkout_identity_rejects_legacy_v1_fingerprint(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "source.py").write_text("value = 1\n", encoding="utf-8")
    manifest = RepoManifest(
        repo_path=str(repo),
        source_fingerprint=f"sha256:{'a' * 64}",
        languages=["python"],
    )

    with pytest.raises(ValueError, match="legacy or unsupported"):
        validate_checkout_identity(repo, manifest, artifact_root=tmp_path)


def test_checkout_identity_rejects_missing_source_fingerprint(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "source.py").write_text("value = 1\n", encoding="utf-8")
    manifest = RepoManifest(
        repo_path=str(repo),
        commit="a" * 40,
        source_fingerprint="",
        languages=["python"],
    )

    with pytest.raises(ValueError, match="legacy or unsupported"):
        validate_checkout_identity(repo, manifest, artifact_root=tmp_path)


def test_checkout_identity_never_resolves_replaceable_source_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "source.py").write_text("value = 1\n", encoding="utf-8")
    fingerprint = fingerprint_repository(repo)
    manifest = RepoManifest(
        repo_path=str(repo),
        source_fingerprint=fingerprint.value,
        file_count=fingerprint.file_count,
        languages=["python"],
    )
    resolve_calls = 0

    def forbidden_resolve(*_args, **_kwargs):
        nonlocal resolve_calls
        resolve_calls += 1
        raise AssertionError("checkout identity followed a replaceable path")

    monkeypatch.setattr(Path, "resolve", forbidden_resolve)

    validate_checkout_identity(repo, manifest, artifact_root=tmp_path / "artifact")
    assert resolve_calls == 0
