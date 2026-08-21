# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the repo manifest and ManifestIndexStateStore."""

from __future__ import annotations

import copy
import json
import stat
import time

import pytest

from codenib.compiler import manifest as manifest_module
from codenib.compiler.manifest import (
    LEGACY_MANIFEST_VERSION,
    MANIFEST_VERSION,
    IndexEntry,
    ManifestIndexStateStore,
    RepoManifest,
)
from codenib.compiler.resources import (
    IndexRequirement,
    IndexState,
    IndexStatus,
    ResourceResolver,
)
from codenib.repository_source_selection import RepositorySourceSelection

_SOURCE_V2 = f"sha256-v2:{'a' * 64}"
_OTHER_SOURCE_V2 = f"sha256-v2:{'b' * 64}"
_LEGACY_SOURCE_V1 = f"sha256:{'c' * 64}"

# ---------------------------------------------------------------------------
# IndexEntry
# ---------------------------------------------------------------------------


class TestIndexEntry:
    def test_to_dict(self):
        entry = IndexEntry(
            index_type="bm25",
            path="/tmp/cache/bm25",
            built_at="2024-01-15T10:30:00+00:00",
            built_at_epoch=1705312200.0,
            status="fresh",
            config={"max_k": 128},
            metadata={"file_count": 890},
        )
        d = entry.to_dict()
        assert d["index_type"] == "bm25"
        assert d["path"] == "/tmp/cache/bm25"
        assert d["status"] == "fresh"
        assert d["config"]["max_k"] == 128
        assert d["metadata"]["file_count"] == 890
        assert json.dumps(d)  # JSON-serialisable

    def test_from_dict_roundtrip(self):
        original = IndexEntry(
            index_type="vector",
            path="/tmp/cache/vector",
            built_at="2024-01-15T10:35:00+00:00",
            built_at_epoch=1705312500.0,
            status="fresh",
            config={"embedding_model": "nomic-ai/CodeRankEmbed"},
            metadata={"document_count": {"l0": 42, "l2": 350}},
            commit="abc123",
            source_fingerprint="sha256:source",
        )
        d = original.to_dict()
        restored = IndexEntry.from_dict(d)
        assert restored.index_type == original.index_type
        assert restored.path == original.path
        assert restored.built_at_epoch == original.built_at_epoch
        assert restored.status == original.status
        assert restored.config == original.config
        assert restored.metadata == original.metadata
        assert restored.commit == original.commit
        assert restored.source_fingerprint == original.source_fingerprint

    def test_from_dict_rejects_missing_versioned_fields(self):
        data = {
            "index_type": "bm25",
            "path": "/tmp",
            "built_at": "2024-01-01T00:00:00Z",
            "built_at_epoch": 1704067200.0,
            "status": "fresh",
        }
        with pytest.raises(ValueError, match="missing fields"):
            IndexEntry.from_dict(data)


# ---------------------------------------------------------------------------
# RepoManifest
# ---------------------------------------------------------------------------


def _sample_manifest(epoch: float | None = None) -> RepoManifest:
    """Helper to create a representative manifest."""
    now = epoch or time.time()
    return RepoManifest(
        repo_path="/tmp/my_repo",
        commit="abc123def",
        source_fingerprint=_SOURCE_V2,
        languages=["python", "typescript"],
        file_count=1234,
        indexes={
            "bm25": IndexEntry(
                index_type="bm25",
                path="/tmp/my_repo/.cache/bm25",
                built_at="2024-01-15T10:30:00+00:00",
                built_at_epoch=now,
                status="fresh",
                config={"max_k": 128},
                metadata={"file_count": 890},
                source_fingerprint=_SOURCE_V2,
            ),
            "vector": IndexEntry(
                index_type="vector",
                path="/tmp/my_repo/.cache/vector",
                built_at="2024-01-15T10:35:00+00:00",
                built_at_epoch=now,
                status="fresh",
                config={"embedding_model": "nomic-ai/CodeRankEmbed"},
                metadata={"document_count": {"l0": 42, "l2": 350}},
                source_fingerprint=_SOURCE_V2,
            ),
        },
        capabilities={"sparse_search": True, "dense_search": True},
        compiled_at="2024-01-15T10:40:00+00:00",
        compiled_at_epoch=now,
    )


class TestRepoManifest:
    def test_to_dict_structure(self):
        m = _sample_manifest()
        d = m.to_dict()

        assert d["version"] == MANIFEST_VERSION
        assert d["repo"]["path"] == "/tmp/my_repo"
        assert d["repo"]["commit"] == "abc123def"
        assert d["repo"]["languages"] == ["python", "typescript"]
        assert d["repo"]["file_count"] == 1234
        assert "bm25" in d["indexes"]
        assert "vector" in d["indexes"]
        assert d["capabilities"]["sparse_search"] is True
        assert json.dumps(d)  # JSON-serialisable

    def test_from_dict_roundtrip(self):
        original = _sample_manifest()
        d = original.to_dict()
        json_str = json.dumps(d)
        loaded = json.loads(json_str)
        restored = RepoManifest.from_dict(loaded)

        assert restored.repo_path == original.repo_path
        assert restored.commit == original.commit
        assert restored.languages == original.languages
        assert restored.file_count == original.file_count
        assert set(restored.indexes.keys()) == set(original.indexes.keys())
        assert restored.indexes["bm25"].config == {"max_k": 128}

    def test_v12_roundtrip_persists_canonical_source_selection(self):
        selection = RepositorySourceSelection(["ios/Pods", "vendor/generated"])
        manifest = RepoManifest(
            source_fingerprint=_SOURCE_V2,
            last_indexed_source_fingerprint=_SOURCE_V2,
            source_selection=selection,
            last_indexed_source_selection_digest=selection.digest,
            indexes={
                "bm25": IndexEntry(
                    index_type="bm25",
                    path="/tmp/bm25",
                    built_at="2024-01-15T10:30:00+00:00",
                    built_at_epoch=time.time(),
                    status="fresh",
                    source_fingerprint=_SOURCE_V2,
                    source_selection_digest=selection.digest,
                )
            },
        )

        payload = manifest.to_dict()
        restored = RepoManifest.from_dict(json.loads(json.dumps(payload)))

        assert payload["repo"]["source_selection"] == selection.to_dict()
        assert "source_selection_digest" not in payload["repo"]
        assert payload["indexes"]["bm25"]["source_selection_digest"] == (
            selection.digest
        )
        assert restored.source_selection == selection
        assert restored.source_selection_digest == selection.digest
        assert restored.last_indexed_source_selection_digest == selection.digest

    def test_v11_roundtrip_retains_legacy_selection_sentinel(self):
        payload = {
            "version": LEGACY_MANIFEST_VERSION,
            "repo": {
                "path": "/tmp/repo",
                "commit": "abc123",
                "last_indexed_commit": "abc123",
                "source_fingerprint": _SOURCE_V2,
                "last_indexed_source_fingerprint": _SOURCE_V2,
                "languages": ["python"],
                "file_count": 1,
            },
            "indexes": {
                "bm25": {
                    "index_type": "bm25",
                    "path": "/tmp/bm25",
                    "built_at": "2024-01-15T10:30:00+00:00",
                    "built_at_epoch": 1.0,
                    "status": "fresh",
                    "config": {},
                    "metadata": {},
                    "commit": "",
                    "source_fingerprint": _SOURCE_V2,
                }
            },
            "capabilities": {"sparse_search": True},
            "compiled_at": "2024-01-15T10:40:00+00:00",
            "compiled_at_epoch": 2.0,
        }

        restored = RepoManifest.from_dict(copy.deepcopy(payload))

        assert restored.version == LEGACY_MANIFEST_VERSION
        assert restored.source_selection is None
        assert restored.source_selection_digest is None
        assert restored.indexes["bm25"].source_selection_digest == ""
        assert restored.indexes["bm25"].commit == ""
        assert restored.index_is_current("bm25") is True
        assert restored.to_dict() == payload

    @pytest.mark.parametrize(
        "mutation",
        ["unknown_version", "missing_selection", "unknown_repo", "unknown_index"],
    )
    def test_versioned_parser_rejects_unknown_or_missing_fields(self, mutation):
        payload = _sample_manifest().to_dict()
        if mutation == "unknown_version":
            payload["version"] = "1.3"
        elif mutation == "missing_selection":
            del payload["repo"]["source_selection"]
        elif mutation == "unknown_repo":
            payload["repo"]["future"] = True
        else:
            payload["indexes"]["bm25"]["future"] = True

        with pytest.raises(ValueError):
            RepoManifest.from_dict(payload)

    def test_v12_parser_rejects_fresh_entry_for_different_selection(self):
        payload = _sample_manifest().to_dict()
        payload["indexes"]["bm25"]["source_selection_digest"] = (
            RepositorySourceSelection(["ios/Pods"]).digest
        )

        with pytest.raises(ValueError, match="does not match"):
            RepoManifest.from_dict(payload)

    @pytest.mark.parametrize(
        ("mutation", "message"),
        [
            ("empty_repository_source", "requires a secure v2"),
            ("legacy_repository_source", "must be a secure v2 identity"),
            ("different_entry_source", "source fingerprint does not match"),
            ("different_entry_commit", "commit does not match"),
        ],
    )
    def test_v12_parser_rejects_fresh_entry_without_exact_source_closure(
        self, mutation, message
    ):
        payload = _sample_manifest().to_dict()
        if mutation == "empty_repository_source":
            payload["repo"]["source_fingerprint"] = ""
            payload["indexes"]["bm25"]["source_fingerprint"] = ""
        elif mutation == "legacy_repository_source":
            payload["repo"]["source_fingerprint"] = _LEGACY_SOURCE_V1
            payload["indexes"]["bm25"]["source_fingerprint"] = _LEGACY_SOURCE_V1
        elif mutation == "different_entry_source":
            payload["indexes"]["bm25"]["source_fingerprint"] = _OTHER_SOURCE_V2
        else:
            payload["indexes"]["bm25"]["commit"] = "different"

        with pytest.raises(ValueError, match=message):
            RepoManifest.from_dict(payload)

    def test_v12_parser_allows_stale_entry_generation_digest(self):
        payload = _sample_manifest().to_dict()
        payload["indexes"]["bm25"]["status"] = "stale"
        old_digest = RepositorySourceSelection(["ios/Pods"]).digest
        payload["indexes"]["bm25"]["source_selection_digest"] = old_digest

        restored = RepoManifest.from_dict(payload)

        assert restored.indexes["bm25"].source_selection_digest == old_digest
        assert restored.index_is_current("bm25") is False

    def test_v12_last_indexed_fingerprint_and_selection_are_atomic(self):
        payload = _sample_manifest().to_dict()
        payload["repo"]["last_indexed_source_fingerprint"] = _SOURCE_V2

        with pytest.raises(ValueError, match="must be present together"):
            RepoManifest.from_dict(payload)

    def test_v12_rejects_mismatched_last_selection_for_same_source_identity(self):
        payload = _sample_manifest().to_dict()
        payload["repo"]["last_indexed_source_fingerprint"] = _SOURCE_V2
        payload["repo"]["last_indexed_source_selection_digest"] = (
            RepositorySourceSelection(("generated",)).digest
        )

        with pytest.raises(ValueError, match="same repository source selection"):
            RepoManifest.from_dict(payload)

    def test_load_rejects_duplicate_json_keys(self, tmp_path):
        payload = json.dumps(_sample_manifest().to_dict()).replace(
            '"version": "1.2"',
            '"version": "1.2", "version": "1.2"',
            1,
        )
        path = tmp_path / "repo_manifest.json"
        path.write_text(payload, encoding="utf-8")

        with pytest.raises(ValueError, match="duplicate key"):
            RepoManifest.load(path)

    def test_load_rejects_bom_and_oversized_documents(self, tmp_path, monkeypatch):
        path = tmp_path / "repo_manifest.json"
        path.write_bytes(
            b"\xef\xbb\xbf" + json.dumps(_sample_manifest().to_dict()).encode()
        )
        with pytest.raises(ValueError, match="BOM-free UTF-8"):
            RepoManifest.load(path)

        monkeypatch.setattr(manifest_module, "_MAX_MANIFEST_BYTES", 64)
        path.write_bytes(b"{" + b" " * 64)
        with pytest.raises(ValueError, match="exceeds 64 bytes"):
            RepoManifest.load(path)

    def test_nested_nonfinite_metadata_is_rejected_on_parse_and_save(self, tmp_path):
        payload = _sample_manifest().to_dict()
        payload["indexes"]["bm25"]["metadata"] = {"nested": [float("inf")]}
        with pytest.raises(ValueError, match="numbers must be finite"):
            RepoManifest.from_dict(payload)

        manifest = _sample_manifest()
        manifest.indexes["bm25"].metadata = {"nested": [float("nan")]}
        with pytest.raises(ValueError, match="JSON compliant"):
            manifest.save(tmp_path / "repo_manifest.json")

    def test_save_and_load(self, tmp_path):
        original = _sample_manifest()
        manifest_path = tmp_path / "repo_manifest.json"
        original.save(manifest_path)

        assert manifest_path.exists()
        loaded = RepoManifest.load(manifest_path)

        assert loaded.repo_path == original.repo_path
        assert loaded.commit == original.commit
        assert set(loaded.indexes.keys()) == {"bm25", "vector"}
        assert (
            loaded.indexes["vector"].config["embedding_model"]
            == "nomic-ai/CodeRankEmbed"
        )

    def test_save_creates_parent_dirs(self, tmp_path):
        m = _sample_manifest()
        nested = tmp_path / "a" / "b" / "manifest.json"
        m.save(nested)
        assert nested.exists()

    def test_new_manifest_is_not_world_readable(self, tmp_path):
        manifest_path = tmp_path / "repo_manifest.json"

        _sample_manifest().save(manifest_path)

        assert stat.S_IMODE(manifest_path.stat().st_mode) & 0o077 == 0

    def test_replaced_manifest_preserves_existing_permissions(self, tmp_path):
        manifest_path = tmp_path / "repo_manifest.json"
        manifest_path.write_text("{}", encoding="utf-8")
        manifest_path.chmod(0o640)

        _sample_manifest().save(manifest_path)

        assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o640

    def test_failed_manifest_replace_preserves_previous_generation(
        self, tmp_path, monkeypatch
    ):
        manifest_path = tmp_path / "repo_manifest.json"
        previous = _sample_manifest()
        previous.commit = "previous"
        for entry in previous.indexes.values():
            entry.commit = previous.commit
        previous.save(manifest_path)
        previous_bytes = manifest_path.read_bytes()

        replacement = _sample_manifest()
        replacement.commit = "replacement"
        for entry in replacement.indexes.values():
            entry.commit = replacement.commit

        def fail_replace(_source, _destination):
            raise OSError("simulated publication failure")

        monkeypatch.setattr(manifest_module.os, "replace", fail_replace)

        with pytest.raises(OSError, match="publication failure"):
            replacement.save(manifest_path)

        assert manifest_path.read_bytes() == previous_bytes
        assert RepoManifest.load(manifest_path).commit == "previous"
        assert list(tmp_path.glob(".repo_manifest.json.*.tmp")) == []

    def test_derive_capabilities_full(self):
        m = _sample_manifest()
        m.indexes["symbol_graph"] = IndexEntry(
            index_type="symbol_graph",
            path="/tmp/graph",
            built_at="2024-01-15T10:30:00+00:00",
            built_at_epoch=time.time(),
            status="fresh",
            commit=m.commit,
            source_fingerprint=m.source_fingerprint,
            source_selection_digest=m.source_selection_digest or "",
        )
        m.derive_capabilities()

        assert m.capabilities["sparse_search"] is True
        assert m.capabilities["dense_search"] is True
        assert m.capabilities["hybrid_search"] is True
        assert m.capabilities["symbol_navigation"] is True

    def test_derive_capabilities_sparse_only(self):
        m = RepoManifest(
            source_fingerprint=_SOURCE_V2,
            indexes={
                "bm25": IndexEntry(
                    index_type="bm25",
                    path="/tmp/bm25",
                    built_at="2024-01-15T10:30:00+00:00",
                    built_at_epoch=time.time(),
                    status="fresh",
                    source_fingerprint=_SOURCE_V2,
                ),
            },
        )
        m.derive_capabilities()

        assert m.capabilities["sparse_search"] is True
        assert m.capabilities["dense_search"] is False
        assert m.capabilities["hybrid_search"] is False
        assert m.capabilities["symbol_navigation"] is False

    def test_derive_capabilities_failed_index_excluded(self):
        m = RepoManifest(
            indexes={
                "bm25": IndexEntry(
                    index_type="bm25",
                    path="/tmp/bm25",
                    built_at="2024-01-15T10:30:00+00:00",
                    built_at_epoch=time.time(),
                    status="failed",
                ),
            },
        )
        m.derive_capabilities()
        assert m.capabilities["sparse_search"] is False

    def test_derive_capabilities_stale_commit_excluded(self):
        m = RepoManifest(
            commit="new",
            indexes={
                "bm25": IndexEntry(
                    index_type="bm25",
                    path="/tmp/bm25",
                    built_at="2024-01-15T10:30:00+00:00",
                    built_at_epoch=time.time(),
                    status="fresh",
                    commit="old",
                ),
            },
        )
        m.derive_capabilities()
        assert m.capabilities["sparse_search"] is False

    def test_v12_empty_head_does_not_alias_entry_with_a_git_commit(self):
        selection = RepositorySourceSelection()
        m = RepoManifest(
            commit="",
            source_fingerprint=_SOURCE_V2,
            source_selection=selection,
            indexes={
                "bm25": IndexEntry(
                    index_type="bm25",
                    path="/tmp/bm25",
                    built_at="2024-01-15T10:30:00+00:00",
                    built_at_epoch=time.time(),
                    status="fresh",
                    commit="old-git-generation",
                    source_fingerprint=_SOURCE_V2,
                    source_selection_digest=selection.digest,
                ),
            },
        )

        assert m.index_is_current("bm25") is False

    def test_derive_capabilities_stale_source_excluded(self):
        m = RepoManifest(
            source_fingerprint=_SOURCE_V2,
            indexes={
                "bm25": IndexEntry(
                    index_type="bm25",
                    path="/tmp/bm25",
                    built_at="2024-01-15T10:30:00+00:00",
                    built_at_epoch=time.time(),
                    status="fresh",
                    source_fingerprint=_OTHER_SOURCE_V2,
                ),
            },
        )
        m.derive_capabilities()
        assert m.capabilities["sparse_search"] is False

    def test_derive_capabilities_stale_selection_excluded(self):
        selection = RepositorySourceSelection(["ios/Pods"])
        m = RepoManifest(
            source_selection=selection,
            indexes={
                "bm25": IndexEntry(
                    index_type="bm25",
                    path="/tmp/bm25",
                    built_at="2024-01-15T10:30:00+00:00",
                    built_at_epoch=time.time(),
                    status="fresh",
                    source_selection_digest=RepositorySourceSelection().digest,
                ),
            },
        )
        m.derive_capabilities()
        assert m.capabilities["sparse_search"] is False

    def test_source_identity_roundtrip(self):
        m = RepoManifest(
            source_fingerprint=_SOURCE_V2,
            last_indexed_source_fingerprint=_OTHER_SOURCE_V2,
            indexes={
                "bm25": IndexEntry(
                    index_type="bm25",
                    path="/tmp/bm25",
                    built_at="2024-01-15T10:30:00+00:00",
                    built_at_epoch=time.time(),
                    status="fresh",
                    source_fingerprint=_SOURCE_V2,
                )
            },
        )

        restored = RepoManifest.from_dict(m.to_dict())

        assert restored.source_fingerprint == _SOURCE_V2
        assert restored.last_indexed_source_fingerprint == _OTHER_SOURCE_V2
        assert restored.index_is_current("bm25") is True

    def test_v12_rejects_legacy_source_identity(self):
        payload = _sample_manifest().to_dict()
        payload["repo"]["source_fingerprint"] = _LEGACY_SOURCE_V1
        payload["indexes"]["bm25"]["source_fingerprint"] = _LEGACY_SOURCE_V1

        with pytest.raises(ValueError, match="must be a secure v2 identity"):
            RepoManifest.from_dict(payload)

    def test_empty_manifest(self):
        m = RepoManifest()
        m.derive_capabilities()
        assert all(v is False for v in m.capabilities.values())
        d = m.to_dict()
        assert json.dumps(d)


# ---------------------------------------------------------------------------
# ManifestIndexStateStore
# ---------------------------------------------------------------------------


class TestManifestIndexStateStore:
    def test_get_existing_index(self):
        now = time.time()
        m = _sample_manifest(epoch=now - 100)  # built 100s ago
        store = ManifestIndexStateStore(m)

        status = store.get_status("bm25", "current_repo")
        assert status is not None
        assert status.index_type == "bm25"
        assert status.path == "/tmp/my_repo/.cache/bm25"
        assert status.last_built is not None
        assert status.age_seconds is not None
        assert 95 < status.age_seconds < 110  # roughly 100 seconds

    def test_get_missing_index(self):
        m = _sample_manifest()
        store = ManifestIndexStateStore(m)
        assert store.get_status("nonexistent", "current_repo") is None

    def test_get_failed_index_returns_none(self):
        m = RepoManifest(
            source_fingerprint=_SOURCE_V2,
            indexes={
                "bm25": IndexEntry(
                    index_type="bm25",
                    path="/tmp/bm25",
                    built_at="2024-01-15T10:30:00+00:00",
                    built_at_epoch=time.time(),
                    status="failed",
                ),
            },
        )
        store = ManifestIndexStateStore(m)
        assert store.get_status("bm25", "current_repo") is None

    def test_set_status_raises(self):
        m = _sample_manifest()
        store = ManifestIndexStateStore(m)
        with pytest.raises(NotImplementedError, match="read-only"):
            store.set_status(IndexStatus(index_type="bm25", state=IndexState.FRESH))

    def test_integrates_with_resource_resolver_fresh(self):
        """Fresh index in manifest → ResourceResolver says 'use'."""
        now = time.time()
        m = _sample_manifest(epoch=now - 10)  # 10s ago → fresh
        store = ManifestIndexStateStore(m)
        resolver = ResourceResolver(store)

        plan = resolver.resolve(
            [
                IndexRequirement(index_type="bm25", max_age_seconds=3600),
            ]
        )
        assert plan.can_execute
        assert plan.decisions[0].action == "use"

    def test_integrates_with_resource_resolver_stale(self):
        """Old index in manifest → ResourceResolver says 'incremental_update'."""
        now = time.time()
        m = RepoManifest(
            source_fingerprint=_SOURCE_V2,
            indexes={
                "bm25": IndexEntry(
                    index_type="bm25",
                    path="/tmp/bm25",
                    built_at="2024-01-01T00:00:00Z",
                    built_at_epoch=now - 7200,  # 2 hours ago
                    status="fresh",
                    source_fingerprint=_SOURCE_V2,
                ),
            },
        )
        store = ManifestIndexStateStore(m)
        resolver = ResourceResolver(store)

        plan = resolver.resolve(
            [
                IndexRequirement(index_type="bm25", max_age_seconds=3600),
            ]
        )
        assert plan.can_execute  # stale is not blocking
        assert plan.decisions[0].state == IndexState.STALE
        assert plan.decisions[0].action == "incremental_update"

    def test_integrates_with_resource_resolver_missing(self):
        """Missing index in manifest → ResourceResolver blocks."""
        m = _sample_manifest()
        store = ManifestIndexStateStore(m)
        resolver = ResourceResolver(store)

        plan = resolver.resolve(
            [
                IndexRequirement(index_type="bm25", max_age_seconds=3600),
                IndexRequirement(index_type="symbol_graph", max_age_seconds=3600),
            ]
        )
        assert not plan.can_execute
        assert "symbol_graph" in plan.blocking_builds
