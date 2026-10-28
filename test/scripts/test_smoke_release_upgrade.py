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
        "index_persistence_importable": False,
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


@pytest.mark.parametrize(
    "baseline_schema,first_build,second_build,error",
    [
        (8, "candidate", "candidate", None),
        (9, "baseline", "baseline", None),
        (8, "baseline", "baseline", "incompatible"),
        (9, "candidate", "candidate", "compatible"),
        (8, "candidate", "repeated", "rebuilt again"),
    ],
)
def test_upgrade_checks_compatibility_before_requiring_reuse(
    monkeypatch, tmp_path, baseline_schema, first_build, second_build, error
):
    expected = {"builder_schema": 9, "languages": ["python"]}
    views = iter(
        [
            {
                "built_at": "baseline",
                "config": {**expected, "builder_schema": baseline_schema},
            },
            {"built_at": first_build, "config": expected},
            {"built_at": second_build, "config": expected},
        ]
    )
    monkeypatch.setattr(upgrade_smoke.venv.EnvBuilder, "create", lambda *_args: None)
    monkeypatch.setattr(
        upgrade_smoke, "_manifest_path", lambda *_args, **_kwargs: tmp_path / "manifest"
    )
    monkeypatch.setattr(upgrade_smoke, "_load_bm25", lambda *_args: next(views))
    monkeypatch.setattr(
        upgrade_smoke, "_installed_bm25_identity", lambda *_args, **_kwargs: expected
    )
    monkeypatch.setattr(
        upgrade_smoke, "_assert_storage_surface", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        upgrade_smoke,
        "_run",
        lambda command, **_kwargs: SimpleNamespace(
            stdout="codenib 0.2.4\n" if "--version" in command else ""
        ),
    )

    if error:
        with pytest.raises(RuntimeError, match=error):
            upgrade_smoke.smoke(
                tmp_path / "candidate.whl", expected_version="0.2.4", root=tmp_path
            )
    else:
        upgrade_smoke.smoke(
            tmp_path / "candidate.whl", expected_version="0.2.4", root=tmp_path
        )
