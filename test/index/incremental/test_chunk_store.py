# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for IncrementalChunkStore.
"""

from codenib.code_chunking.base import CodeChunk
from codenib.index.incremental.chunk_store import IncrementalChunkStore, _hash_content

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_chunk(content: str, name: str = "fn", file: str = "/repo/a.py") -> CodeChunk:
    return CodeChunk(
        content=content,
        start_line=0,
        end_line=5,
        chunk_type="function",
        name=name,
        file=file,
        node_id=f"{file}:{name}()",
    )


COMMIT_A = "aaaaaaa"
COMMIT_B = "bbbbbbb"


# ---------------------------------------------------------------------------
# Tests: from_chunks / basic structure
# ---------------------------------------------------------------------------


class TestFromChunks:
    def test_groups_by_file(self):
        chunks = [
            make_chunk("def foo(): pass", "foo", "/a.py"),
            make_chunk("def bar(): pass", "bar", "/a.py"),
            make_chunk("def baz(): pass", "baz", "/b.py"),
        ]
        store = IncrementalChunkStore.from_chunks(chunks, COMMIT_A)
        assert store.file_count() == 2
        assert store.chunk_count() == 3

    def test_assigns_commit_sha(self):
        chunk = make_chunk("def x(): pass")
        store = IncrementalChunkStore.from_chunks([chunk], COMMIT_A)
        vc = store.get_all_versioned()[0]
        assert vc.commit_sha == COMMIT_A

    def test_assigns_content_hash(self):
        content = "def x(): pass"
        chunk = make_chunk(content)
        store = IncrementalChunkStore.from_chunks([chunk], COMMIT_A)
        vc = store.get_all_versioned()[0]
        assert vc.content_hash == _hash_content(content)

    def test_empty_chunks(self):
        store = IncrementalChunkStore.from_chunks([], COMMIT_A)
        assert store.file_count() == 0
        assert store.chunk_count() == 0


# ---------------------------------------------------------------------------
# Tests: update_file
# ---------------------------------------------------------------------------


class TestUpdateFile:
    def _make_store(self) -> IncrementalChunkStore:
        chunks = [
            make_chunk("def foo(): return 1", "foo"),
            make_chunk("def bar(): return 2", "bar"),
        ]
        return IncrementalChunkStore.from_chunks(chunks, COMMIT_A)

    def test_unchanged_content_produces_no_delta(self):
        store = self._make_store()
        same_chunks = [
            make_chunk("def foo(): return 1", "foo"),
            make_chunk("def bar(): return 2", "bar"),
        ]
        added, removed = store.update_file("/repo/a.py", same_chunks, COMMIT_B)
        assert added == []
        assert removed == []

    def test_modified_content_detected(self):
        store = self._make_store()
        new_chunks = [
            make_chunk("def foo(): return 99", "foo"),  # changed
            make_chunk("def bar(): return 2", "bar"),  # unchanged
        ]
        added, removed = store.update_file("/repo/a.py", new_chunks, COMMIT_B)
        assert len(added) == 1
        assert added[0].content == "def foo(): return 99"
        assert len(removed) == 1
        assert removed[0].content == "def foo(): return 1"

    def test_new_chunk_detected_as_added(self):
        store = self._make_store()
        new_chunks = [
            make_chunk("def foo(): return 1", "foo"),
            make_chunk("def bar(): return 2", "bar"),
            make_chunk("def baz(): return 3", "baz"),  # new
        ]
        added, removed = store.update_file("/repo/a.py", new_chunks, COMMIT_B)
        assert len(added) == 1
        assert added[0].name == "baz"
        assert removed == []

    def test_deleted_chunk_detected_as_removed(self):
        store = self._make_store()
        new_chunks = [make_chunk("def foo(): return 1", "foo")]  # bar removed
        added, removed = store.update_file("/repo/a.py", new_chunks, COMMIT_B)
        assert added == []
        assert len(removed) == 1
        assert removed[0].name == "bar"

    def test_updates_commit_sha(self):
        store = self._make_store()
        store.update_file(
            "/repo/a.py",
            [make_chunk("def foo(): return 99", "foo")],
            COMMIT_B,
        )
        versioned = store.get_all_versioned()
        assert all(vc.commit_sha == COMMIT_B for vc in versioned)

    def test_adds_new_file(self):
        store = self._make_store()
        new_file_chunks = [make_chunk("def new_fn(): pass", "new_fn", "/repo/c.py")]
        added, removed = store.update_file("/repo/c.py", new_file_chunks, COMMIT_B)
        assert len(added) == 1
        assert store.file_count() == 2  # /a.py + /c.py


# ---------------------------------------------------------------------------
# Tests: delete_file
# ---------------------------------------------------------------------------


class TestDeleteFile:
    def test_removes_file_from_store(self):
        store = IncrementalChunkStore.from_chunks(
            [make_chunk("def foo(): pass", file="/a.py")], COMMIT_A
        )
        store.delete_file("/a.py")
        assert store.file_count() == 0

    def test_returns_removed_chunks(self):
        chunk = make_chunk("def foo(): pass", file="/a.py")
        store = IncrementalChunkStore.from_chunks([chunk], COMMIT_A)
        removed = store.delete_file("/a.py")
        assert len(removed) == 1
        assert removed[0].content == "def foo(): pass"

    def test_delete_nonexistent_file_is_noop(self):
        store = IncrementalChunkStore()
        removed = store.delete_file("/nonexistent.py")
        assert removed == []


# ---------------------------------------------------------------------------
# Tests: get_all_content_hashes
# ---------------------------------------------------------------------------


class TestGetAllContentHashes:
    def test_returns_all_hashes(self):
        chunks = [
            make_chunk("def a(): pass"),
            make_chunk("def b(): pass"),
        ]
        store = IncrementalChunkStore.from_chunks(chunks, COMMIT_A)
        hashes = store.get_all_content_hashes()
        assert hashes == {
            _hash_content("def a(): pass"),
            _hash_content("def b(): pass"),
        }


# ---------------------------------------------------------------------------
# Tests: save / load round-trip
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_save_load_roundtrip(self, tmp_path):
        chunks = [
            make_chunk("def foo(): pass", "foo", "/a.py"),
            make_chunk("def bar(): pass", "bar", "/b.py"),
        ]
        store = IncrementalChunkStore.from_chunks(chunks, COMMIT_A)
        path = tmp_path / "chunk_store.pkl"
        store.save(path)

        loaded = IncrementalChunkStore.load(path)
        assert loaded.file_count() == store.file_count()
        assert loaded.chunk_count() == store.chunk_count()
        assert loaded.get_all_content_hashes() == store.get_all_content_hashes()

    def test_load_or_empty_missing_file(self, tmp_path):
        store = IncrementalChunkStore.load_or_empty(tmp_path / "nonexistent.pkl")
        assert store.chunk_count() == 0

    def test_load_or_empty_existing_file(self, tmp_path):
        chunk = make_chunk("def x(): pass")
        original = IncrementalChunkStore.from_chunks([chunk], COMMIT_A)
        path = tmp_path / "store.pkl"
        original.save(path)

        loaded = IncrementalChunkStore.load_or_empty(path)
        assert loaded.chunk_count() == 1
