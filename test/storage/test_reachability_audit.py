# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
from pathlib import Path

import pytest

from codenib.storage import _reachability_audit as audit_module
from codenib.storage._reachability_audit import _audit_local_storage_snapshot
from codenib.storage.cas import BlobInfo, LocalCAS
from codenib.storage.sqlite_catalog import SQLiteCatalog, _catalog_validation_snapshot


def _audit(
    catalog: SQLiteCatalog,
    cas_root: Path,
    *,
    sample_limit: int = 20,
) -> dict[str, object]:
    with _catalog_validation_snapshot(Path(catalog.path)) as snapshot:
        return _audit_local_storage_snapshot(
            snapshot,
            cas_root,
            sample_limit=sample_limit,
        )


def _register(catalog: SQLiteCatalog, cas: LocalCAS, payload: bytes) -> BlobInfo:
    receipt = cas.put_bytes(payload)
    catalog.register_object(
        receipt.digest,
        storage_key=receipt.storage_key,
        byte_size=receipt.byte_size,
    )
    return receipt


def _stage(
    catalog: SQLiteCatalog,
    repository_id: str,
    source_revision_id: str,
    profile_id: str,
    view_type: str,
    primary: BlobInfo,
    member: BlobInfo,
) -> str:
    return catalog.stage_view_generation(
        repository_id,
        source_revision_id,
        profile_id,
        view_type,
        primary.digest,
        schema_version="1",
        member_object_digests=(member.digest,),
    )


def test_audit_classifies_primary_and_member_reachability(tmp_path: Path) -> None:
    cas_root = tmp_path / "cas"
    with (
        SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog,
        LocalCAS(cas_root) as cas,
    ):
        repository_id = catalog.create_repository("owner/repo")
        source_revision_id = catalog.create_source_revision(
            repository_id,
            commit_sha="a" * 40,
            tree_sha="b" * 64,
        )
        bm25_profile = catalog.create_view_profile("bm25", {})

        historical = tuple(
            _register(catalog, cas, payload)
            for payload in (b"historical-primary", b"historical-member")
        )
        historical_view = _stage(
            catalog,
            repository_id,
            source_revision_id,
            bm25_profile,
            "bm25",
            historical[0],
            historical[1],
        )
        catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            (historical_view,),
        )

        current = tuple(
            _register(catalog, cas, payload)
            for payload in (b"current-primary", b"current-member")
        )
        current_view = _stage(
            catalog,
            repository_id,
            source_revision_id,
            bm25_profile,
            "bm25",
            current[0],
            current[1],
        )
        catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            (current_view,),
            expected_generation=1,
        )

        symbol_profile = catalog.create_view_profile("symbols", {})
        generation_only = tuple(
            _register(catalog, cas, payload)
            for payload in (b"generation-primary", b"generation-member")
        )
        _stage(
            catalog,
            repository_id,
            source_revision_id,
            symbol_profile,
            "symbols",
            generation_only[0],
            generation_only[1],
        )
        unbound = _register(catalog, cas, b"unbound")

        result = _audit(catalog, cas_root)

    reachability = result["catalog"]["reachability"]
    assert set(reachability["current_ref"]["sample_digests"]) == {
        receipt.digest for receipt in current
    }
    assert set(reachability["historical_snapshot"]["sample_digests"]) == {
        receipt.digest for receipt in historical
    }
    assert set(reachability["generation_only"]["sample_digests"]) == {
        receipt.digest for receipt in generation_only
    }
    assert reachability["unbound_registered"]["sample_digests"] == [unbound.digest]
    assert result["cas"]["observations"]["present"]["count"] == 7


def test_audit_reports_each_canonical_file_status(tmp_path: Path) -> None:
    cas_root = tmp_path / "cas"
    with (
        SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog,
        LocalCAS(cas_root) as cas,
    ):
        _register(catalog, cas, b"present")
        missing = _register(catalog, cas, b"missing")
        mismatch = _register(catalog, cas, b"size-mismatch")
        cas.put_bytes(b"unregistered")

        (cas_root / missing.storage_key).unlink()
        (cas_root / mismatch.storage_key).write_bytes(b"x")
        (cas_root / "sha256" / "not-a-shard").mkdir()

        result = _audit(catalog, cas_root)

    files = result["cas"]["observations"]
    assert {status: files[status]["count"] for status in files} == {
        "present": 1,
        "missing": 1,
        "size_mismatch": 1,
        "unregistered": 1,
        "invalid": 1,
    }
    assert files["size_mismatch"]["expected_bytes"] == mismatch.byte_size
    assert files["size_mismatch"]["observed_bytes"] == 1


def test_audit_rejects_symlinked_sha256_root(tmp_path: Path) -> None:
    cas_root = tmp_path / "cas"
    target = tmp_path / "elsewhere"
    cas_root.mkdir()
    target.mkdir()
    (cas_root / "sha256").symlink_to(target, target_is_directory=True)
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        files = _audit(catalog, cas_root)["cas"]["observations"]
    assert files["invalid"]["count"] == 1
    assert files["invalid"]["samples"] == ["sha256"]


def test_audit_marks_multiply_linked_object_invalid(tmp_path: Path) -> None:
    cas_root = tmp_path / "cas"
    with (
        SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog,
        LocalCAS(cas_root) as cas,
    ):
        receipt = _register(catalog, cas, b"linked")
        os.link(cas_root / receipt.storage_key, tmp_path / "second-link")
        files = _audit(catalog, cas_root)["cas"]["observations"]
    assert files["invalid"]["samples"] == [receipt.storage_key]
    assert files["present"]["count"] == 0


def test_audit_samples_are_stable_and_bounded(tmp_path: Path) -> None:
    cas_root = tmp_path / "cas"
    with (
        SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog,
        LocalCAS(cas_root) as cas,
    ):
        receipts = [
            _register(catalog, cas, payload)
            for payload in (b"delta", b"alpha", b"charlie", b"bravo")
        ]
        first = _audit(catalog, cas_root, sample_limit=2)
        second = _audit(catalog, cas_root, sample_limit=2)

    assert first == second
    expected_digests = sorted(receipt.digest for receipt in receipts)[:2]
    assert (
        first["catalog"]["reachability"]["unbound_registered"]["sample_digests"]
        == expected_digests
    )
    assert first["cas"]["observations"]["present"]["samples"] == [
        f"sha256/{digest[:2]}/{digest[2:]}" for digest in expected_digests
    ]


@pytest.mark.parametrize("sample_limit", (-1, 1_001, True))
def test_audit_rejects_invalid_sample_limit(
    tmp_path: Path, sample_limit: object
) -> None:
    cas_root = tmp_path / "cas"
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog, LocalCAS(cas_root):
        with pytest.raises(ValueError, match="sample_limit"):
            _audit(catalog, cas_root, sample_limit=sample_limit)  # type: ignore[arg-type]


def test_audit_missing_root_is_invalid_even_without_objects(tmp_path: Path) -> None:
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        result = _audit(catalog, tmp_path / "missing")
    assert result["cas"]["observations"]["invalid"]["samples"] == ["."]
    assert result["reclamation"] == {
        "assessed": False,
        "reclaimable_bytes": None,
    }
    assert result["cross_store_atomic"] is False
    assert result["content_hashes_verified"] is False
    assert result["writers_quiesced"] is False


def test_audit_marks_noncanonical_object_mode_invalid(tmp_path: Path) -> None:
    cas_root = tmp_path / "cas"
    with (
        SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog,
        LocalCAS(cas_root) as cas,
    ):
        receipt = _register(catalog, cas, b"wrong mode")
        (cas_root / receipt.storage_key).chmod(0o644)

        files = _audit(catalog, cas_root)["cas"]["observations"]

    assert files["invalid"]["samples"] == [receipt.storage_key]
    assert files["present"]["count"] == 0


def test_audit_does_not_follow_replaced_shard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cas_root = tmp_path / "cas"
    with (
        SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog,
        LocalCAS(cas_root) as cas,
    ):
        receipt = _register(catalog, cas, b"registered")
        shard_name = receipt.digest[:2]
        object_name = receipt.digest[2:]
        shard = cas_root / "sha256" / shard_name
        displaced = cas_root / "sha256" / f"{shard_name}.displaced"
        external = tmp_path / "external"
        external.mkdir()
        external_object = external / object_name
        external_object.write_bytes(b"registered")
        external_object.chmod(0o600)

        original = audit_module._entry_metadata
        replaced = False

        def replace_after_stat(descriptor: int, name: str):
            nonlocal replaced
            metadata = original(descriptor, name)
            if name == shard_name and not replaced:
                shard.rename(displaced)
                shard.symlink_to(external, target_is_directory=True)
                replaced = True
            return metadata

        monkeypatch.setattr(audit_module, "_entry_metadata", replace_after_stat)
        files = _audit(catalog, cas_root)["cas"]["observations"]

    assert replaced is True
    assert files["present"]["count"] == 0
    assert files["missing"]["count"] == 1
    assert files["invalid"]["samples"] == [f"sha256/{shard_name}"]


def test_audit_fails_when_cas_traversal_is_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cas_root = tmp_path / "cas"

    def deny_traversal(descriptor: int) -> list[str]:
        raise PermissionError("denied")

    with (
        SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog,
        LocalCAS(cas_root),
    ):
        monkeypatch.setattr(audit_module, "_directory_names", deny_traversal)

        with pytest.raises(PermissionError, match="denied"):
            _audit(catalog, cas_root)
