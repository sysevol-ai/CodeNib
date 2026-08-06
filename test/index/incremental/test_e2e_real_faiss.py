# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
End-to-end integration tests with real FAISS (no mocked vector store).

These tests exercise the full incremental pipeline including actual FAISS
index operations, verifying that search results are correct after
incremental updates.

Marked as integration tests since they involve git repos and FAISS.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List

import faiss
import numpy as np
import pytest

from codenib.code_chunker import CodeChunker, RepoChunkingConfig
from codenib.index.embedding.vector_store import CodeVectorStore
from codenib.index.incremental import (
    EmbeddingsCache,
    GitDiffDetector,
    IncrementalChunkStore,
    IncrementalIndexUpdater,
)
from codenib.index.incremental.chunk_store import _hash_content
from codenib.index.incremental.state import IncrementalState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DIM = 32  # Bigger dimension for more realistic FAISS ops


def _run(cmd: list, cwd: str) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


class _DeterministicEmbeddings:
    """Mock embedding model with deterministic output (no LangChain dep)."""

    def __init__(self, dim: int = DIM):
        self._dim = dim

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        vecs = []
        for text in texts:
            np.random.seed(hash(text) % (2**31))
            vecs.append(np.random.randn(self._dim).tolist())
        return vecs

    def embed_query(self, text: str) -> List[float]:
        return self.embed_documents([text])[0]


def _make_mock_embedding_model(dim: int = DIM):
    return _DeterministicEmbeddings(dim)


def _build_real_vector_store(embedding_model, dim=DIM) -> CodeVectorStore:
    """Create a real CodeVectorStore with a mock embedding model."""
    store = CodeVectorStore.__new__(CodeVectorStore)
    store.embedding = embedding_model
    store.dimension = dim
    store.index_metric = "ip"
    store.profiler = None
    store.embedding_model = "test-model"
    store.embedding_provider = "test"
    store.index_type = "flat"
    store.store_path = None

    store.l0_index = faiss.IndexFlatIP(dim)
    store.l0_documents = []
    store.l2_index = faiss.IndexFlatIP(dim)
    store.l2_documents = []

    return store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def py_repo(tmp_path: Path):
    """Git repo with two commits for end-to-end testing."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init"], str(repo))
    _run(["git", "config", "user.email", "test@example.com"], str(repo))
    _run(["git", "config", "user.name", "Test"], str(repo))

    # Commit A: two files
    (repo / "math_ops.py").write_text(
        "def add(a, b):\n    return a + b\n\n" "def subtract(a, b):\n    return a - b\n"
    )
    (repo / "string_ops.py").write_text("def concat(a, b):\n    return a + b\n")
    _run(["git", "add", "."], str(repo))
    _run(["git", "commit", "-m", "initial"], str(repo))
    commit_a = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
    ).stdout.strip()

    # Commit B: modify math_ops.py, add new file, delete string_ops.py
    (repo / "math_ops.py").write_text(
        "def add(a, b):\n    return a + b\n\n"
        "def subtract(a, b):\n    return abs(a - b)\n"  # changed
    )
    (repo / "new_utils.py").write_text("def helper():\n    return 42\n")
    (repo / "string_ops.py").unlink()
    _run(["git", "add", "."], str(repo))
    _run(["git", "commit", "-m", "update"], str(repo))
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


@pytest.mark.integration
class TestEndToEndRealFaiss:
    """E2E tests with real FAISS index operations."""

    def test_incremental_update_produces_searchable_index(self, py_repo):
        """
        After a full initial build + incremental update, the FAISS index
        should contain all current chunks and be searchable.
        """
        import subprocess as _sp

        repo_path = py_repo["repo_path"]
        embedding_model = _make_mock_embedding_model()
        vector_store = _build_real_vector_store(embedding_model)

        chunker = CodeChunker(
            language="python",
            repo_config=RepoChunkingConfig(languages=["python"], filter_tests=False),
        )

        # Check out commit A and chunk the repo at that state
        _sp.run(
            ["git", "checkout", py_repo["commit_a"]],
            cwd=repo_path,
            capture_output=True,
            check=True,
        )
        alpha_chunks = chunker.chunk_repository(repo_path=repo_path)
        chunk_store = IncrementalChunkStore.from_chunks(
            alpha_chunks, py_repo["commit_a"]
        )

        # Seed embeddings cache from commit-A chunks
        emb_cache = EmbeddingsCache()
        for chunk in alpha_chunks:
            h = _hash_content(chunk.content)
            vec = np.array(
                embedding_model.embed_documents([chunk.content])[0], dtype=np.float32
            )
            emb_cache.put(h, vec)

        # Move back to commit B (HEAD)
        _sp.run(
            ["git", "checkout", py_repo["commit_b"]],
            cwd=repo_path,
            capture_output=True,
            check=True,
        )

        # Run incremental update
        updater = IncrementalIndexUpdater(
            chunker=chunker,
            embedding_model=embedding_model,
            diff_detector=GitDiffDetector(supported_extensions={".py"}),
        )

        result = updater.update(
            repo_path=repo_path,
            vector_store=vector_store,
            chunk_store=chunk_store,
            embeddings_cache=emb_cache,
            last_commit=py_repo["commit_a"],
        )

        # Verify update metrics
        assert result.files_changed > 0
        assert result.total_chunks > 0

        # Verify FAISS index is searchable
        assert vector_store.l2_index.ntotal > 0
        search_results = vector_store.search_with_content(
            "subtract numbers", top_k=5, level="l2"
        )
        assert len(search_results) > 0

        # Verify deleted file's chunks are gone from chunk store
        deleted_path = f"{repo_path}/string_ops.py"
        for vc in chunk_store.get_all_versioned():
            assert vc.chunk.file != deleted_path

    def test_state_persistence_round_trip(self, py_repo, tmp_path):
        """Verify IncrementalState survives save/load and provides last_commit."""
        state = IncrementalState(
            last_commit=py_repo["commit_a"],
            index_path=str(tmp_path),
            build_levels=["l0", "l2"],
        )
        state.save(tmp_path)

        loaded = IncrementalState.load(tmp_path)
        assert loaded is not None
        assert loaded.last_commit == py_repo["commit_a"]

    def test_cache_seeding_gives_full_hit_rate_on_no_change(self, py_repo):
        """
        When nothing changed between commits, the incremental update should
        get 100% cache hit rate (zero re-embeddings).
        """
        repo_path = py_repo["repo_path"]
        embedding_model = _make_mock_embedding_model()
        vector_store = _build_real_vector_store(embedding_model)

        chunker = CodeChunker(
            language="python",
            repo_config=RepoChunkingConfig(languages=["python"], filter_tests=False),
        )

        # Build at commit B (HEAD)
        all_chunks = chunker.chunk_repository(repo_path=repo_path)
        commit_b = py_repo["commit_b"]
        chunk_store = IncrementalChunkStore.from_chunks(all_chunks, commit_b)

        emb_cache = EmbeddingsCache()
        for chunk in all_chunks:
            h = _hash_content(chunk.content)
            vec = np.array(
                embedding_model.embed_documents([chunk.content])[0], dtype=np.float32
            )
            emb_cache.put(h, vec)

        updater = IncrementalIndexUpdater(
            chunker=chunker,
            embedding_model=embedding_model,
            diff_detector=GitDiffDetector(supported_extensions={".py"}),
        )

        result = updater.update(
            repo_path=repo_path,
            vector_store=vector_store,
            chunk_store=chunk_store,
            embeddings_cache=emb_cache,
            last_commit=commit_b,  # same as HEAD → no changes
        )

        assert result.files_changed == 0
        assert result.chunks_reembedded == 0


@pytest.mark.integration
class TestEdgeCases:
    """Edge cases: empty files, renamed files, binary files in diff."""

    def test_empty_file_handled(self, tmp_path):
        """An empty .py file should not crash the pipeline."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _run(["git", "init"], str(repo))
        _run(["git", "config", "user.email", "t@t.com"], str(repo))
        _run(["git", "config", "user.name", "T"], str(repo))

        (repo / "empty.py").write_text("")
        _run(["git", "add", "."], str(repo))
        _run(["git", "commit", "-m", "empty"], str(repo))
        commit_a = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
        ).stdout.strip()

        (repo / "notempty.py").write_text("x = 1\n")
        _run(["git", "add", "."], str(repo))
        _run(["git", "commit", "-m", "add notempty"], str(repo))

        embedding_model = _make_mock_embedding_model()
        vector_store = _build_real_vector_store(embedding_model)
        chunk_store = IncrementalChunkStore()
        emb_cache = EmbeddingsCache()

        chunker = CodeChunker(
            language="python",
            repo_config=RepoChunkingConfig(languages=["python"], filter_tests=False),
        )

        updater = IncrementalIndexUpdater(
            chunker=chunker,
            embedding_model=embedding_model,
            diff_detector=GitDiffDetector(supported_extensions={".py"}),
        )

        # Should not raise
        result = updater.update(
            repo_path=str(repo),
            vector_store=vector_store,
            chunk_store=chunk_store,
            embeddings_cache=emb_cache,
            last_commit=commit_a,
        )
        assert result.files_changed >= 1

    def test_renamed_file_treated_as_modify(self, tmp_path):
        """A rename should replace the old vector address without a ghost entry."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _run(["git", "init"], str(repo))
        _run(["git", "config", "user.email", "t@t.com"], str(repo))
        _run(["git", "config", "user.name", "T"], str(repo))

        (repo / "old_name.py").write_text("def foo():\n    pass\n")
        _run(["git", "add", "."], str(repo))
        _run(["git", "commit", "-m", "original"], str(repo))
        commit_a = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
        ).stdout.strip()

        _run(["git", "mv", "old_name.py", "new_name.py"], str(repo))
        _run(["git", "commit", "-m", "rename"], str(repo))
        commit_b = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        embedding_model = _make_mock_embedding_model()
        vector_store = _build_real_vector_store(embedding_model)
        chunker = CodeChunker(
            language="python",
            repo_config=RepoChunkingConfig(languages=["python"], filter_tests=False),
        )

        _run(["git", "checkout", commit_a], str(repo))
        initial_chunks = chunker.chunk_repository(repo_path=str(repo))
        chunk_store = IncrementalChunkStore.from_chunks(initial_chunks, commit_a)
        initial_vectors = [
            np.asarray(embedding_model.embed_documents([chunk.content])[0])
            for chunk in initial_chunks
        ]
        initial_docs = IncrementalIndexUpdater._build_doc_embedding_pairs(
            [
                (versioned, vector)
                for versioned, vector in zip(
                    chunk_store.get_all_versioned(), initial_vectors, strict=True
                )
            ]
        )
        vector_store.rebuild_from_embeddings(*initial_docs, level="l2")
        emb_cache = EmbeddingsCache()
        for chunk, vector in zip(initial_chunks, initial_vectors, strict=True):
            emb_cache.put(_hash_content(chunk.content), vector)

        _run(["git", "checkout", commit_b], str(repo))
        updater = IncrementalIndexUpdater(
            chunker=chunker,
            embedding_model=embedding_model,
            diff_detector=GitDiffDetector(supported_extensions={".py"}),
        )
        result = updater.update(
            repo_path=str(repo),
            vector_store=vector_store,
            chunk_store=chunk_store,
            embeddings_cache=emb_cache,
            last_commit=commit_a,
        )

        assert result.total_chunks == len(initial_chunks)
        assert vector_store.l2_index.ntotal == len(vector_store.l2_documents)
        node_ids = [doc.metadata["node_id"] for doc in vector_store.l2_documents]
        assert node_ids
        assert all(node_id.startswith("new_name.py") for node_id in node_ids)
        assert all("old_name.py" not in node_id for node_id in node_ids)

    def test_binary_file_in_diff_ignored(self, tmp_path):
        """Binary files in git diff should be silently skipped."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _run(["git", "init"], str(repo))
        _run(["git", "config", "user.email", "t@t.com"], str(repo))
        _run(["git", "config", "user.name", "T"], str(repo))

        (repo / "code.py").write_text("x = 1\n")
        _run(["git", "add", "."], str(repo))
        _run(["git", "commit", "-m", "init"], str(repo))
        commit_a = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
        ).stdout.strip()

        # Add a binary file
        (repo / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        (repo / "code.py").write_text("x = 2\n")
        _run(["git", "add", "."], str(repo))
        _run(["git", "commit", "-m", "add binary"], str(repo))

        detector = GitDiffDetector(supported_extensions={".py"})
        changes = detector.detect_changes(str(repo), commit_a)

        # Only .py file should appear
        all_paths = changes.added + changes.modified + changes.deleted
        assert all(p.endswith(".py") for p in all_paths)
