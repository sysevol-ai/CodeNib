# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from types import SimpleNamespace

import pytest

from codenib.compiler.artifact_fingerprints import (
    bm25_artifact_file_fingerprints,
    require_bm25_manifest_artifact,
)


def _bm25_artifact(tmp_path):
    root = tmp_path / "bm25"
    root.mkdir()
    (root / "documents.json").write_text('[{"content":"alpha"}]')
    (root / "bm25_metadata.json").write_text('{"max_k":10}')
    return root


def test_manifest_bm25_integrity_accepts_recorded_artifact(tmp_path):
    root = _bm25_artifact(tmp_path)
    entry = SimpleNamespace(
        path=str(root),
        config={"artifact_file_fingerprints": bm25_artifact_file_fingerprints(root)},
        metadata={},
    )

    assert require_bm25_manifest_artifact(entry) is True


def test_manifest_bm25_integrity_rejects_same_size_tamper(tmp_path):
    root = _bm25_artifact(tmp_path)
    entry = SimpleNamespace(
        path=str(root),
        config={"artifact_file_fingerprints": bm25_artifact_file_fingerprints(root)},
        metadata={},
    )
    documents = root / "documents.json"
    documents.write_text(documents.read_text().replace("alpha", "omega"))

    with pytest.raises(ValueError, match="manifest fingerprints"):
        require_bm25_manifest_artifact(entry)


def test_manifest_bm25_integrity_allows_legacy_entry_without_record(tmp_path):
    root = _bm25_artifact(tmp_path)
    entry = SimpleNamespace(path=str(root), config={}, metadata={})

    assert require_bm25_manifest_artifact(entry) is False


def test_manifest_bm25_integrity_does_not_hide_malformed_config_record(tmp_path):
    root = _bm25_artifact(tmp_path)
    entry = SimpleNamespace(
        path=str(root),
        config={"artifact_file_fingerprints": None},
        metadata={"artifact_file_fingerprints": bm25_artifact_file_fingerprints(root)},
    )

    with pytest.raises(ValueError, match="manifest fingerprints"):
        require_bm25_manifest_artifact(entry)
