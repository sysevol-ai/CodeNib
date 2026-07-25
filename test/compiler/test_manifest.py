# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the repo manifest and ManifestIndexStateStore."""

from __future__ import annotations

import json
import time

import pytest

from codenib.compiler.manifest import (
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
        )
        d = original.to_dict()
        restored = IndexEntry.from_dict(d)
        assert restored.index_type == original.index_type
        assert restored.path == original.path
        assert restored.built_at_epoch == original.built_at_epoch
        assert restored.status == original.status
        assert restored.config == original.config
        assert restored.metadata == original.metadata

    def test_from_dict_defaults(self):
        data = {
            "index_type": "bm25",
            "path": "/tmp",
            "built_at": "2024-01-01T00:00:00Z",
            "built_at_epoch": 1704067200.0,
            "status": "fresh",
        }
        entry = IndexEntry.from_dict(data)
        assert entry.config == {}
        assert entry.metadata == {}


# ---------------------------------------------------------------------------
# RepoManifest
# ---------------------------------------------------------------------------


def _sample_manifest(epoch: float | None = None) -> RepoManifest:
    """Helper to create a representative manifest."""
    now = epoch or time.time()
    return RepoManifest(
        repo_path="/tmp/my_repo",
        commit="abc123def",
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
            ),
            "vector": IndexEntry(
                index_type="vector",
                path="/tmp/my_repo/.cache/vector",
                built_at="2024-01-15T10:35:00+00:00",
                built_at_epoch=now,
                status="fresh",
                config={"embedding_model": "nomic-ai/CodeRankEmbed"},
                metadata={"document_count": {"l0": 42, "l2": 350}},
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

    def test_derive_capabilities_full(self):
        m = _sample_manifest()
        m.indexes["symbol_graph"] = IndexEntry(
            index_type="symbol_graph",
            path="/tmp/graph",
            built_at="2024-01-15T10:30:00+00:00",
            built_at_epoch=time.time(),
            status="fresh",
        )
        m.derive_capabilities()

        assert m.capabilities["sparse_search"] is True
        assert m.capabilities["dense_search"] is True
        assert m.capabilities["hybrid_search"] is True
        assert m.capabilities["symbol_navigation"] is True

    def test_derive_capabilities_sparse_only(self):
        m = RepoManifest(
            indexes={
                "bm25": IndexEntry(
                    index_type="bm25",
                    path="/tmp/bm25",
                    built_at="2024-01-15T10:30:00+00:00",
                    built_at_epoch=time.time(),
                    status="fresh",
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
            indexes={
                "bm25": IndexEntry(
                    index_type="bm25",
                    path="/tmp/bm25",
                    built_at="2024-01-01T00:00:00Z",
                    built_at_epoch=now - 7200,  # 2 hours ago
                    status="fresh",
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
