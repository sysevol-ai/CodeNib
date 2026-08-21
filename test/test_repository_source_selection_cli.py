# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Product-path coverage for persisted repository source selection."""

from __future__ import annotations

import json
from pathlib import Path

from codenib import cli
from codenib.compiler.manifest import MANIFEST_FILENAME, RepoManifest
from codenib.paths import repo_index_dir
from codenib.repository_source_selection import RepositorySourceSelection


def _bm25_document_paths(manifest: RepoManifest) -> set[str]:
    entry = manifest.indexes["bm25"]
    payload = json.loads(
        (Path(entry.path) / "documents.json").read_text(encoding="utf-8")
    )
    return {
        document["metadata"]["file"]
        for document in payload
        if isinstance(document, dict)
        and isinstance(document.get("metadata"), dict)
        and isinstance(document["metadata"].get("file"), str)
    }


def test_index_cli_persists_reuses_and_clears_exact_source_selection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "ios" / "Pods").mkdir(parents=True)
    (repo / "packages" / "mobile" / "ios" / "Pods").mkdir(parents=True)
    (repo / "src" / "kept.py").write_text("KEPT = 1\n", encoding="utf-8")
    excluded = repo / "ios" / "Pods" / "excluded.py"
    excluded.write_text("EXCLUDED = 1\n", encoding="utf-8")
    sibling = repo / "packages" / "mobile" / "ios" / "Pods" / "kept.py"
    sibling.write_text("SIBLING = 1\n", encoding="utf-8")
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "state"))

    assert (
        cli.run(
            [
                "index",
                str(repo),
                "--preset",
                "fast",
                "--language",
                "python",
                "--exclude-dir",
                "ios/Pods",
            ]
        )
        == 0
    )
    manifest_path = repo_index_dir(repo) / MANIFEST_FILENAME
    first = RepoManifest.load(manifest_path)
    first_fingerprint = first.source_fingerprint
    first_paths = _bm25_document_paths(first)

    assert first.source_selection == RepositorySourceSelection(("ios/Pods",))
    assert not any(path.startswith("ios/Pods/") for path in first_paths)
    assert any(path.endswith("src/kept.py") for path in first_paths)
    assert any(
        path.endswith("packages/mobile/ios/Pods/kept.py") for path in first_paths
    )

    excluded.write_text("EXCLUDED = 2\n", encoding="utf-8")
    assert (
        cli.run(
            [
                "index",
                str(repo),
                "--preset",
                "fast",
                "--language",
                "python",
            ]
        )
        == 0
    )
    reused = RepoManifest.load(manifest_path)
    assert reused.source_selection == first.source_selection
    assert reused.source_fingerprint == first_fingerprint
    assert not any(
        path.startswith("ios/Pods/") for path in _bm25_document_paths(reused)
    )

    assert (
        cli.run(
            [
                "index",
                str(repo),
                "--preset",
                "fast",
                "--language",
                "python",
                "--clear-exclude-dirs",
            ]
        )
        == 0
    )
    cleared = RepoManifest.load(manifest_path)
    assert cleared.source_selection == RepositorySourceSelection()
    assert cleared.source_fingerprint != first_fingerprint
    assert any(
        path.endswith("ios/Pods/excluded.py") for path in _bm25_document_paths(cleared)
    )
