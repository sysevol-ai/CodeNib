# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for Phase 2: Incremental L0 (file skeleton) updates.

Verifies that the chunk store and index updater correctly handle
L0 and L2 chunks as separate layers for the same files.
"""

from __future__ import annotations

from codeminer.code_chunking.base import CodeChunk
from codeminer.incremental.chunk_store import IncrementalChunkStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chunk(content: str, name: str, file: str) -> CodeChunk:
    return CodeChunk(
        content=content,
        start_line=0,
        end_line=2,
        chunk_type="function",
        name=name,
        file=file,
        node_id=f"{file}:{name}()",
    )


class TestChunkStoreLevelSeparation:
    """Verify L0 and L2 chunks are tracked independently."""

    def test_same_file_different_levels(self):
        store = IncrementalChunkStore()
        l2_chunk = _make_chunk("def foo():\n    pass\n", "foo", "/a.py")
        l0_chunk = _make_chunk("# file skeleton of a.py\n", "a.py", "/a.py")

        store.update_file("/a.py", [l2_chunk], "abc123", level="l2")
        store.update_file("/a.py", [l0_chunk], "abc123", level="l0")

        l2_chunks = store.get_chunks_for_file("/a.py", level="l2")
        l0_chunks = store.get_chunks_for_file("/a.py", level="l0")

        assert len(l2_chunks) == 1
        assert l2_chunks[0].name == "foo"
        assert len(l0_chunks) == 1
        assert l0_chunks[0].name == "a.py"

    def test_get_all_versioned_filters_by_level(self):
        store = IncrementalChunkStore()
        l2 = _make_chunk("def bar():\n    pass\n", "bar", "/b.py")
        l0 = _make_chunk("# skeleton\n", "b.py", "/b.py")

        store.update_file("/b.py", [l2], "sha1", level="l2")
        store.update_file("/b.py", [l0], "sha1", level="l0")

        all_vc = store.get_all_versioned()
        assert len(all_vc) == 2

        l2_only = store.get_all_versioned(level="l2")
        assert len(l2_only) == 1
        assert l2_only[0].level == "l2"

        l0_only = store.get_all_versioned(level="l0")
        assert len(l0_only) == 1
        assert l0_only[0].level == "l0"

    def test_delete_file_removes_both_levels(self):
        store = IncrementalChunkStore()
        l2 = _make_chunk("def x():\n    pass\n", "x", "/c.py")
        l0 = _make_chunk("# skeleton\n", "c.py", "/c.py")

        store.update_file("/c.py", [l2], "sha1", level="l2")
        store.update_file("/c.py", [l0], "sha1", level="l0")
        assert store.chunk_count() == 2

        removed = store.delete_file("/c.py")
        assert len(removed) == 2
        assert store.chunk_count() == 0

    def test_delete_file_single_level(self):
        store = IncrementalChunkStore()
        l2 = _make_chunk("def y():\n    pass\n", "y", "/d.py")
        l0 = _make_chunk("# skeleton\n", "d.py", "/d.py")

        store.update_file("/d.py", [l2], "sha1", level="l2")
        store.update_file("/d.py", [l0], "sha1", level="l0")

        removed = store.delete_file("/d.py", level="l0")
        assert len(removed) == 1
        # L2 should still be there
        assert len(store.get_chunks_for_file("/d.py", level="l2")) == 1
        assert len(store.get_chunks_for_file("/d.py", level="l0")) == 0

    def test_from_chunks_respects_level(self):
        chunks = [
            _make_chunk("def a():\n    pass\n", "a", "/e.py"),
            _make_chunk("def b():\n    pass\n", "b", "/e.py"),
        ]
        store = IncrementalChunkStore.from_chunks(chunks, "sha1", level="l0")
        l0 = store.get_all_versioned(level="l0")
        l2 = store.get_all_versioned(level="l2")

        assert len(l0) == 2
        assert len(l2) == 0
        assert all(vc.level == "l0" for vc in l0)

    def test_add_chunks_merges_levels(self):
        l2_chunks = [_make_chunk("def f():\n    pass\n", "f", "/g.py")]
        l0_chunks = [_make_chunk("# skeleton\n", "g.py", "/g.py")]

        store = IncrementalChunkStore.from_chunks(l2_chunks, "sha1", level="l2")
        store.add_chunks(l0_chunks, "sha1", level="l0")

        assert store.chunk_count() == 2
        assert len(store.get_all_versioned(level="l2")) == 1
        assert len(store.get_all_versioned(level="l0")) == 1

    def test_update_l0_detects_changes(self):
        store = IncrementalChunkStore()
        old_skel = _make_chunk("# v1 skeleton\n", "h.py", "/h.py")
        store.update_file("/h.py", [old_skel], "sha1", level="l0")

        new_skel = _make_chunk("# v2 skeleton with new func\n", "h.py", "/h.py")
        added, removed = store.update_file("/h.py", [new_skel], "sha2", level="l0")

        assert len(added) == 1
        assert len(removed) == 1
