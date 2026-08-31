# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from codenib import cli
from codenib.storage.cas import LocalCAS
from codenib.storage.sqlite_catalog import LATEST_SCHEMA_VERSION, SQLiteCatalog


def _namespace_bytes(path: Path) -> dict[str, bytes | None]:
    return {
        suffix: (
            sidecar.read_bytes()
            if (sidecar := Path(f"{path}{suffix}")).exists()
            else None
        )
        for suffix in ("", "-wal", "-shm", "-journal")
    }


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_storage_audit_parser_exposes_only_report_options() -> None:
    args = cli.build_parser().parse_args(
        [
            "storage",
            "audit",
            "--catalog",
            "/state/catalog.sqlite3",
            "--cas-root",
            "/state/cas",
            "--sample-limit",
            "7",
            "--json",
        ]
    )

    assert args.handler is cli._run_storage_audit
    assert args.catalog == "/state/catalog.sqlite3"
    assert args.cas_root == "/state/cas"
    assert args.sample_limit == 7
    assert args.json is True
    assert not hasattr(args, "delete")
    assert not hasattr(args, "apply")

    with pytest.raises(SystemExit) as invalid:
        cli.build_parser().parse_args(
            [
                "storage",
                "audit",
                "--catalog",
                "/state/catalog.sqlite3",
                "--cas-root",
                "/state/cas",
                "--sample-limit",
                "-1",
            ]
        )
    assert invalid.value.code == 2


def test_storage_audit_uses_a_copy_and_does_not_change_inputs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    catalog_path = tmp_path / "catalog.sqlite3"
    cas_root = tmp_path / "cas"
    with LocalCAS(cas_root) as store, SQLiteCatalog(catalog_path) as catalog:
        receipt = store.put_bytes(b"audit object")
        catalog.register_object(
            receipt.digest,
            storage_key=receipt.storage_key,
            byte_size=receipt.byte_size,
        )
    catalog_before = _namespace_bytes(catalog_path)
    cas_before = _tree_bytes(cas_root)

    result = cli.run(
        [
            "storage",
            "audit",
            "--catalog",
            str(catalog_path),
            "--cas-root",
            str(cas_root),
            "--json",
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["contract"] == "codenib.local-storage-audit.v1"
    assert payload["catalog_source_open_mode"] == "copied-validation-snapshot"
    assert payload["catalog"]["schema_version"] == LATEST_SCHEMA_VERSION
    assert payload["reclamation"]["assessed"] is False

    assert (
        cli.run(
            [
                "storage",
                "audit",
                "--catalog",
                str(catalog_path),
                "--cas-root",
                str(cas_root),
            ]
        )
        == 0
    )
    human = capsys.readouterr().out
    assert "writers not quiesced; stores non-atomic; hashes not verified" in human
    assert catalog_before == _namespace_bytes(catalog_path)
    assert cas_before == _tree_bytes(cas_root)


def test_storage_audit_reports_missing_catalog_objects_without_health_exit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    catalog_path = tmp_path / "catalog.sqlite3"
    cas_root = tmp_path / "cas"
    with LocalCAS(cas_root):
        pass
    digest = "a" * 64
    with SQLiteCatalog(catalog_path) as catalog:
        catalog.register_object(
            digest,
            storage_key=f"sha256/{digest[:2]}/{digest[2:]}",
            byte_size=12,
        )

    result = cli.run(
        [
            "storage",
            "audit",
            "--catalog",
            str(catalog_path),
            "--cas-root",
            str(cas_root),
            "--json",
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["cas"]["observations"]["missing"]["count"] == 1


def test_storage_audit_rejects_old_schema_without_migrating_source(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    catalog_path = tmp_path / "catalog.sqlite3"
    cas_root = tmp_path / "cas"
    with LocalCAS(cas_root), SQLiteCatalog(catalog_path):
        pass
    connection = sqlite3.connect(catalog_path)
    try:
        connection.execute(f"PRAGMA user_version = {LATEST_SCHEMA_VERSION - 1}")
        connection.commit()
    finally:
        connection.close()
    before = _namespace_bytes(catalog_path)

    result = cli.run(
        [
            "storage",
            "audit",
            "--catalog",
            str(catalog_path),
            "--cas-root",
            str(cas_root),
        ]
    )

    assert result == 2
    assert "requires the current SQLite catalog schema" in capsys.readouterr().err
    assert before == _namespace_bytes(catalog_path)
