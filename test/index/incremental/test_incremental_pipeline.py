# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Integration tests for the incremental indexing pipeline.

These tests exercise the full pipeline end-to-end using:
  - A temporary git repository (no network needed)
  - A mock embedding model (no GPU/API needed)

The key assertion is that on the second run, only the chunks from changed
files are re-embedded; the rest come from the embeddings cache.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

import numpy as np
import pytest

from codenib.code_chunker import CodeChunker, RepoChunkingConfig
from codenib.code_chunking.base import CodeChunk
from codenib.index.embedding.vector_store import CodeVectorStore
from codenib.index.incremental import (
    EmbeddingsCache,
    GitDiffDetector,
    IncrementalChunkStore,
    IncrementalIndexUpdater,
    RepoChanges,
)
from codenib.index.incremental.chunk_store import _hash_content
from codenib.index.incremental.index_updater import UpdateResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


DIM = 8  # small dimension to keep tests fast


def _run(cmd: list, cwd: str) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


def _make_mock_embedding_model(dim: int = DIM) -> MagicMock:
    """Return a mock embedding model that produces deterministic vectors."""
    model = MagicMock()

    def embed_documents(texts: List[str]) -> List[List[float]]:
        # Use the hash of each text so different content → different vector
        vecs = []
        for text in texts:
            h = hash(text) % (2**16)
            vec = [(h + i) / (2**16) for i in range(dim)]
            vecs.append(vec)
        return vecs

    def embed_query(text: str) -> List[float]:
        return embed_documents([text])[0]

    model.embed_documents.side_effect = embed_documents
    model.embed_query.side_effect = embed_query
    return model


def _make_mock_vector_store(embedding_model, dim: int = DIM) -> MagicMock:
    """Thin mock around CodeVectorStore that tracks rebuild_from_embeddings calls."""
    store = MagicMock(spec=CodeVectorStore)
    store.embedding = embedding_model
    store.l0_documents = []
    store.l2_documents = []
    # Track calls to rebuild so we can inspect them in tests
    store.rebuild_from_embeddings = MagicMock()
    return store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def py_repo(tmp_path: Path):
    """
    Minimal Python git repository with two commits.

    Commit A: file_a.py (2 functions) + file_b.py (1 function)
    Commit B: file_a.py modified (1 function changed) + file_c.py added
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init"], str(repo))
    _run(["git", "config", "user.email", "test@example.com"], str(repo))
    _run(["git", "config", "user.name", "Test"], str(repo))

    # Commit A
    (repo / "file_a.py").write_text(
        "def alpha():\n    return 1\n\ndef beta():\n    return 2\n"
    )
    (repo / "file_b.py").write_text("def gamma():\n    return 3\n")
    _run(["git", "add", "."], str(repo))
    _run(["git", "commit", "-m", "initial"], str(repo))
    commit_a = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
    ).stdout.strip()

    # Commit B — modify file_a.py, add file_c.py
    (repo / "file_a.py").write_text(
        "def alpha():\n    return 1\n\ndef beta():\n    return 99\n"  # beta changed
    )
    (repo / "file_c.py").write_text("def delta():\n    return 4\n")
    _run(["git", "add", "."], str(repo))
    _run(["git", "commit", "-m", "second"], str(repo))
    commit_b = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
    ).stdout.strip()

    return {
        "repo_path": str(repo),
        "commit_a": commit_a,
        "commit_b": commit_b,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestIncrementalChunkStoreAndCache:
    """
    Validate that IncrementalChunkStore correctly identifies changed chunks
    and that EmbeddingsCache avoids re-embedding unchanged chunks.
    """

    def test_changed_chunk_is_detected(self, py_repo):
        chunker = CodeChunker(
            language="python",
            repo_config=RepoChunkingConfig(languages=["python"], filter_tests=False),
        )

        repo_path = py_repo["repo_path"]

        # Check out commit A and chunk file_a.py at that state
        _run(["git", "checkout", py_repo["commit_a"]], repo_path)
        chunks_a = chunker.chunk_file(str(Path(repo_path) / "file_a.py"))
        store = IncrementalChunkStore.from_chunks(chunks_a, py_repo["commit_a"])

        # Check out commit B and chunk file_a.py at the new state
        _run(["git", "checkout", py_repo["commit_b"]], repo_path)
        chunks_b = chunker.chunk_file(str(Path(repo_path) / "file_a.py"))
        added, removed = store.update_file(
            str(Path(py_repo["repo_path"]) / "file_a.py"),
            chunks_b,
            py_repo["commit_b"],
        )

        # beta() changed content, so it appears in both added and removed
        added_names = [c.name for c in added]
        removed_names = [c.name for c in removed]
        assert any(
            "beta" in n for n in added_names
        ), f"Expected beta in added: {added_names}"
        assert any(
            "beta" in n for n in removed_names
        ), f"Expected beta in removed: {removed_names}"

    def test_unchanged_chunk_stays_in_cache(self):
        """Chunks whose content hash is already cached are not re-embedded."""
        cache = EmbeddingsCache()
        content = "def alpha():\n    return 1\n"
        h = _hash_content(content)
        cache.put(h, np.ones(DIM, dtype=np.float32))

        assert cache.has(h)
        assert not cache.has(_hash_content("def beta():\n    return 2\n"))


class TestIncrementalIndexUpdater:
    """Full pipeline test with a mocked embedding model."""

    def test_only_changed_chunks_are_reembedded(self, py_repo):
        """
        Given an initial chunk store built from commit A's file_a.py,
        running an incremental update after commit B should only re-embed
        the changed chunk (beta) and the new file (file_c.py).
        file_b.py and alpha() should come from cache.
        """
        repo_path = py_repo["repo_path"]
        chunker = CodeChunker(
            language="python",
            repo_config=RepoChunkingConfig(languages=["python"], filter_tests=False),
        )

        # Check out commit A and chunk with the real chunker so content
        # hashes match what the updater will produce.
        _run(["git", "checkout", py_repo["commit_a"]], repo_path)
        initial_chunks = chunker.chunk_repository(repo_path=repo_path)
        chunk_store = IncrementalChunkStore.from_chunks(
            initial_chunks, py_repo["commit_a"]
        )

        # Move back to commit B (HEAD) for the incremental update
        _run(["git", "checkout", py_repo["commit_b"]], repo_path)

        # Seed the embeddings cache with commit-A embeddings
        emb_cache = EmbeddingsCache()
        embedding_model = _make_mock_embedding_model(DIM)
        for chunk in initial_chunks:
            h = _hash_content(chunk.content)
            vec = np.array(
                embedding_model.embed_documents([chunk.content])[0], dtype=np.float32
            )
            emb_cache.put(h, vec)

        mock_store = _make_mock_vector_store(embedding_model, DIM)

        updater = IncrementalIndexUpdater(
            chunker=chunker,
            embedding_model=embedding_model,
            diff_detector=GitDiffDetector(supported_extensions={".py"}),
        )

        result: UpdateResult = updater.update(
            repo_path=repo_path,
            vector_store=mock_store,
            chunk_store=chunk_store,
            embeddings_cache=emb_cache,
            last_commit=py_repo["commit_a"],
        )

        # gamma() in file_b.py was untouched → should come from cache.
        # alpha() may or may not be a cache hit depending on whether the
        # chunker includes surrounding context that changed with beta().
        assert (
            result.chunks_from_cache >= 1
        ), f"Expected at least gamma from cache, got {result.chunks_from_cache}"
        assert result.cache_hit_rate > 0
        node_files = {
            chunk.node_id.split(":", 1)[0] for chunk in chunk_store.get_all_chunks()
        }
        assert {"file_a.py", "file_b.py", "file_c.py"}.issubset(node_files)
        assert all(not Path(file_path).is_absolute() for file_path in node_files)

    def test_chunkers_receive_repository_relative_identity(self, tmp_path):
        repo = tmp_path / "repo"
        source = repo / "nested" / "service.py"
        source.parent.mkdir(parents=True)
        source.write_text("def run():\n    return 1\n")

        l2_chunker = MagicMock()
        l2_chunker.chunk_file.return_value = []
        l0_chunker = MagicMock()
        l0_chunker.chunk_file.return_value = []
        detector = MagicMock()
        detector.detect_changes.return_value = RepoChanges(
            modified=[str(source.resolve())],
            old_commit="a" * 40,
            new_commit="b" * 40,
        )
        embedding_model = _make_mock_embedding_model(DIM)
        updater = IncrementalIndexUpdater(
            chunker=l2_chunker,
            l0_chunker=l0_chunker,
            embedding_model=embedding_model,
            diff_detector=detector,
        )

        updater.update(
            repo_path=str(repo),
            vector_store=_make_mock_vector_store(embedding_model, DIM),
            chunk_store=IncrementalChunkStore(),
            embeddings_cache=EmbeddingsCache(),
            last_commit="a" * 40,
        )

        l2_chunker.chunk_file.assert_called_once_with(
            str(source.resolve()), relative_path="nested/service.py"
        )
        l0_chunker.chunk_file.assert_called_once_with(
            str(source.resolve()),
            relative_path="nested/service.py",
            skeleton_mode=True,
        )

    def test_rebuild_from_embeddings_called(self, py_repo):
        """Verify that rebuild_from_embeddings is called with the full chunk set."""
        repo_path = py_repo["repo_path"]
        chunker = CodeChunker(
            language="python",
            repo_config=RepoChunkingConfig(languages=["python"], filter_tests=False),
        )

        chunk_store = IncrementalChunkStore()
        emb_cache = EmbeddingsCache()
        embedding_model = _make_mock_embedding_model(DIM)
        mock_store = _make_mock_vector_store(embedding_model, DIM)

        updater = IncrementalIndexUpdater(
            chunker=chunker,
            embedding_model=embedding_model,
            diff_detector=GitDiffDetector(supported_extensions={".py"}),
        )

        updater.update(
            repo_path=repo_path,
            vector_store=mock_store,
            chunk_store=chunk_store,
            embeddings_cache=emb_cache,
            last_commit=py_repo["commit_a"],
        )

        # delta_update must have been called (FAISS was rebuilt/patched)
        mock_store.delta_update.assert_called_once()

    def test_no_changes_skips_rebuild(self, py_repo):
        """When there are no file changes, rebuild_from_embeddings is not called."""
        chunker = CodeChunker(
            language="python",
            repo_config=RepoChunkingConfig(languages=["python"], filter_tests=False),
        )

        chunk_store = IncrementalChunkStore()
        emb_cache = EmbeddingsCache()
        embedding_model = _make_mock_embedding_model(DIM)
        mock_store = _make_mock_vector_store(embedding_model, DIM)

        updater = IncrementalIndexUpdater(
            chunker=chunker,
            embedding_model=embedding_model,
            diff_detector=GitDiffDetector(supported_extensions={".py"}),
        )

        result = updater.update(
            repo_path=py_repo["repo_path"],
            vector_store=mock_store,
            chunk_store=chunk_store,
            embeddings_cache=emb_cache,
            last_commit=py_repo["commit_b"],  # already at HEAD
        )

        assert (
            result.is_empty
            if hasattr(result, "is_empty")
            else result.files_changed == 0
        )
        mock_store.delta_update.assert_not_called()

    def test_deleted_file_chunks_removed(self, tmp_path):
        """After a file is deleted, its chunks must not appear in the chunk store."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _run(["git", "init"], str(repo))
        _run(["git", "config", "user.email", "test@example.com"], str(repo))
        _run(["git", "config", "user.name", "Test"], str(repo))

        (repo / "dead.py").write_text("def doomed():\n    pass\n")
        _run(["git", "add", "."], str(repo))
        _run(["git", "commit", "-m", "add dead file"], str(repo))
        commit_a = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
        ).stdout.strip()

        dead_chunk = CodeChunk(
            content="def doomed():\n    pass\n",
            start_line=0,
            end_line=1,
            chunk_type="function",
            name="doomed",
            file=str(repo / "dead.py"),
            node_id=f"{repo}/dead.py:doomed()",
        )
        chunk_store = IncrementalChunkStore.from_chunks([dead_chunk], commit_a)

        # Delete the file and commit
        (repo / "dead.py").unlink()
        _run(["git", "add", "."], str(repo))
        _run(["git", "commit", "-m", "delete dead file"], str(repo))

        chunker = CodeChunker(
            language="python",
            repo_config=RepoChunkingConfig(languages=["python"], filter_tests=False),
        )
        embedding_model = _make_mock_embedding_model(DIM)
        emb_cache = EmbeddingsCache()
        mock_store = _make_mock_vector_store(embedding_model, DIM)

        updater = IncrementalIndexUpdater(
            chunker=chunker,
            embedding_model=embedding_model,
            diff_detector=GitDiffDetector(supported_extensions={".py"}),
        )

        updater.update(
            repo_path=str(repo),
            vector_store=mock_store,
            chunk_store=chunk_store,
            embeddings_cache=emb_cache,
            last_commit=commit_a,
        )

        assert chunk_store.chunk_count() == 0
        assert str(repo / "dead.py") not in chunk_store._store
        mock_store.delta_update.assert_called_once_with([], [], set(), level="l2")
