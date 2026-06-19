# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for Phase 3: IncrementalState commit SHA state management.

Verifies that IncrementalState round-trips correctly as JSON and that
the compiler layer reads/writes it properly.
"""

from __future__ import annotations

from pathlib import Path

from codeminer.index.incremental.state import IncrementalState


class TestIncrementalState:
    """Unit tests for IncrementalState persistence."""

    def test_save_and_load(self, tmp_path: Path):
        state = IncrementalState(
            last_commit="abc123def456",
            chunk_store_path="chunk_store.pkl",
            embeddings_cache_path="embeddings_cache.pkl",
            index_path="/some/path",
            build_levels=["l0", "l2"],
        )
        state.save(tmp_path)

        loaded = IncrementalState.load(tmp_path)
        assert loaded is not None
        assert loaded.last_commit == "abc123def456"
        assert loaded.chunk_store_path == "chunk_store.pkl"
        assert loaded.embeddings_cache_path == "embeddings_cache.pkl"
        assert loaded.index_path == "/some/path"
        assert loaded.build_levels == ["l0", "l2"]

    def test_load_nonexistent_returns_none(self, tmp_path: Path):
        assert IncrementalState.load(tmp_path) is None

    def test_load_or_default_returns_default(self, tmp_path: Path):
        state = IncrementalState.load_or_default(tmp_path)
        assert state.last_commit == ""
        assert state.build_levels == ["l0", "l2"]

    def test_load_or_default_returns_saved(self, tmp_path: Path):
        IncrementalState(last_commit="xyz789").save(tmp_path)
        state = IncrementalState.load_or_default(tmp_path)
        assert state.last_commit == "xyz789"

    def test_state_file_is_json(self, tmp_path: Path):
        import json

        IncrementalState(last_commit="abc").save(tmp_path)
        state_file = tmp_path / "incremental_state.json"
        assert state_file.exists()

        data = json.loads(state_file.read_text())
        assert data["last_commit"] == "abc"
        assert isinstance(data["build_levels"], list)

    def test_overwrite_existing(self, tmp_path: Path):
        IncrementalState(last_commit="first").save(tmp_path)
        IncrementalState(last_commit="second").save(tmp_path)

        loaded = IncrementalState.load(tmp_path)
        assert loaded.last_commit == "second"

    def test_ignores_unknown_fields(self, tmp_path: Path):
        """Extra fields in JSON should not break loading."""
        import json

        state_file = tmp_path / "incremental_state.json"
        data = {
            "last_commit": "abc",
            "chunk_store_path": "cs.pkl",
            "embeddings_cache_path": "ec.pkl",
            "index_path": "",
            "build_levels": ["l2"],
            "future_field": "should be ignored",
        }
        state_file.write_text(json.dumps(data))

        loaded = IncrementalState.load(tmp_path)
        assert loaded is not None
        assert loaded.last_commit == "abc"
