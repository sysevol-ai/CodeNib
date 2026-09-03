# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.smoke_release_upgrade as upgrade_smoke
from scripts.smoke_release_upgrade import (
    _assert_builder_contract,
    _assert_storage_surface,
    _candidate_install_command,
)


def test_upgrade_smoke_accepts_current_builder_identity_with_build_metadata():
    expected = {
        "builder_schema": 8,
        "repository_filter_policy": 3,
        "languages": ["python"],
    }
    actual = {
        **expected,
        "artifact_file_fingerprints": {"documents.json": {"sha256": "abc"}},
        "chunk_count": 1,
    }

    _assert_builder_contract(actual, expected)


def test_upgrade_smoke_rejects_stale_builder_contract():
    with pytest.raises(RuntimeError, match="unexpected BM25 builder contract"):
        _assert_builder_contract(
            {"builder_schema": 7, "repository_filter_policy": 3},
            {"builder_schema": 8, "repository_filter_policy": 3},
        )


def test_upgrade_smoke_reinstalls_same_version_candidate() -> None:
    command = _candidate_install_command(
        Path("pip"),
        Path("candidate.whl"),
        expected_version="0.2.2",
    )

    assert command == (
        Path("pip"),
        "install",
        "--upgrade",
        "--force-reinstall",
        Path("candidate.whl"),
    )


def test_upgrade_smoke_normally_upgrades_new_release_candidate() -> None:
    command = _candidate_install_command(
        Path("pip"),
        Path("candidate.whl"),
        expected_version="0.2.3",
    )

    assert command == (
        Path("pip"),
        "install",
        "--upgrade",
        Path("candidate.whl"),
    )


def test_upgrade_smoke_accepts_wiki_only_storage_surface(monkeypatch):
    payload = {
        "storage_kind": "module",
        "exports_resolved": True,
        "wiki_roundtrip": True,
        "storage_exports": [
            "SQLiteWikiStore",
            "WIKI_ENVELOPE_MAX_BYTES",
            "WikiStore",
            "WikiStoreCorruptionError",
            "WikiStoreError",
            "WikiStoreSchemaError",
            "WikiStoreValidationError",
            "WikiStoredEntry",
        ],
        "retired_exports": [],
        "artifact_commands": ["fetch", "mcp-config", "pack", "verify"],
    }
    monkeypatch.setattr(
        upgrade_smoke,
        "_run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=json.dumps(payload)),
    )

    _assert_storage_surface(Path("python"), root=Path("."), env={})


def test_upgrade_smoke_rejects_stale_storage_surface(monkeypatch):
    payload = {
        "storage_kind": "package",
        "storage_exports": ["LocalCAS", "SQLiteCatalog"],
        "retired_exports": ["LocalCAS", "SQLiteCatalog"],
        "artifact_commands": [
            "fetch",
            "import-cache",
            "materialize",
            "mcp-config",
            "pack",
            "verify",
        ],
    }
    monkeypatch.setattr(
        upgrade_smoke,
        "_run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=json.dumps(payload)),
    )

    with pytest.raises(RuntimeError, match="unexpected candidate storage surface"):
        _assert_storage_surface(Path("python"), root=Path("."), env={})
