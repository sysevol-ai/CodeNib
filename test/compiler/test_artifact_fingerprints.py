# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from types import SimpleNamespace

import pytest

from codenib.compiler.artifact_fingerprints import (
    bm25_artifact_file_fingerprints,
    regular_file_fingerprint,
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


def test_regular_file_fingerprint_rejects_a_changed_open_generation(
    tmp_path, monkeypatch
):
    import codenib.compiler.artifact_fingerprints as fingerprints

    path = tmp_path / "artifact.bin"
    path.write_bytes(b"stable bytes")
    real_fstat = fingerprints.os.fstat
    calls = 0

    def changing_fstat(descriptor):
        nonlocal calls
        observed = real_fstat(descriptor)
        calls += 1
        if calls == 2:
            return SimpleNamespace(
                st_mode=observed.st_mode,
                st_dev=observed.st_dev,
                st_ino=observed.st_ino,
                st_size=observed.st_size + 1,
                st_mtime_ns=observed.st_mtime_ns,
            )
        return observed

    monkeypatch.setattr(fingerprints.os, "fstat", changing_fstat)

    with pytest.raises(ValueError, match="changed while hashing"):
        regular_file_fingerprint(path)
