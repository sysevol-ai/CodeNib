# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for concrete index builders and the IndexCompiler."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

import pytest

import codenib.compiler.index_compiler as index_compiler_module
from codenib._atomic_directory import (
    capture_directory_ownership,
    directory_ownership_digest,
    directory_ownership_root_identity,
)
from codenib.compiler.artifact_fingerprints import regular_file_fingerprint
from codenib.compiler.index_builders import (
    BM25IndexBuilder,
    IndexBuilderRegistry,
    SymbolGraphBuilder,
    VectorIndexBuilder,
    ZoektIndexBuilder,
    register_default_builders,
)
from codenib.compiler.index_compiler import IndexCompiler, IndexCompilerConfig
from codenib.compiler.manifest import (
    MANIFEST_FILENAME,
    MANIFEST_VERSION,
    ManifestIndexStateStore,
    RepoManifest,
)
from codenib.compiler.resources import (
    IndexRequirement,
    IndexState,
    IndexStatus,
    ResourceResolver,
)
from codenib.index.embedding.artifact_integrity import VECTOR_VIEW_UPDATE_MARKER
from codenib.index.embedding.model_policy import DEFAULT_EMBEDDING_REVISION
from codenib.index.incremental import IncrementalState
from codenib.ls_router import GraphBuildResult
from codenib.native_index_authorization import (
    InvalidNativeIndexAuthorizationError,
    _mint_trusted_local_admin_authorization,
)
from codenib.repository_filters import (
    REPOSITORY_FILTER_POLICY_VERSION,
    default_exclude_patterns,
)
from codenib.source_fingerprint import is_secure_source_fingerprint_v2

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_fake_graph_artifact(output_dir: str | Path) -> Path:
    path = Path(output_dir) / "graph.pkl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"test-symbol-graph\n")
    return path


def _fake_graph_output_receipt(output_dir: str | Path) -> dict:
    path = _write_fake_graph_artifact(output_dir)
    return {
        "path": str(path.resolve()),
        **regular_file_fingerprint(path),
        "query_surface_sha256": "c" * 64,
    }


def _mock_builder(index_type: str = "mock", file_count: int = 42):
    """Create a mock builder that returns a canned IndexStatus."""

    class MockBuilder:
        def build(self, scope: str, **kwargs) -> IndexStatus:
            output_dir = kwargs.get("output_dir", "/tmp/mock")
            os.makedirs(output_dir, exist_ok=True)
            return IndexStatus(
                index_type=index_type,
                state=IndexState.FRESH,
                last_built=time.time(),
                age_seconds=0.0,
                scope=scope,
                path=output_dir,
                metadata={"file_count": file_count},
            )

        def incremental_update(self, scope: str, **kwargs) -> IndexStatus:
            return self.build(scope, **kwargs)

    return MockBuilder()


def _failing_builder():
    """Create a builder that always fails."""

    class FailBuilder:
        def build(self, scope: str, **kwargs) -> IndexStatus:
            raise RuntimeError("Build failed intentionally")

        def incremental_update(self, scope: str, **kwargs) -> IndexStatus:
            raise RuntimeError("Update failed intentionally")

    return FailBuilder()


# ---------------------------------------------------------------------------
# BM25IndexBuilder
# ---------------------------------------------------------------------------


class TestBM25IndexBuilder:
    @patch("codenib.index.sparse_idx.bm25_index.BM25CodeIndexer")
    @patch("codenib.code_chunker.CodeChunker")
    def test_build_returns_fresh_status(self, MockChunker, MockIndexer, tmp_path):
        # Mock chunker returns fake chunks
        mock_chunker_instance = MagicMock()
        mock_chunker_instance.chunk_repository.return_value = [
            SimpleNamespace(file="src/a.py"),
            SimpleNamespace(file="src/a.py"),
            SimpleNamespace(file="src/b.py"),
        ]
        MockChunker.return_value = mock_chunker_instance

        # Mock indexer
        mock_indexer_instance = MagicMock()

        def save_index(directory):
            root = Path(directory)
            root.mkdir(parents=True, exist_ok=True)
            (root / "documents.json").write_text("[]", encoding="utf-8")
            (root / "bm25_metadata.json").write_text("{}", encoding="utf-8")

        mock_indexer_instance.save_index.side_effect = save_index
        MockIndexer.return_value = mock_indexer_instance

        builder = BM25IndexBuilder(languages=["python"], max_k=64)
        output = str(tmp_path / "bm25")

        status = builder.build(
            scope="current_repo",
            repo_path="/fake/repo",
            output_dir=output,
        )

        assert status.index_type == "bm25"
        assert status.state == IndexState.FRESH
        assert status.metadata["file_count"] == 3
        assert status.metadata["chunk_count"] == 3
        assert status.metadata["source_file_count"] == 2
        assert status.metadata["builder_schema"] == 8
        assert status.metadata["chunking_failure_policy"] == "fail"
        assert status.metadata["include_header_epilogue"] is True
        assert status.metadata["max_k"] == 64
        assert set(status.metadata["artifact_file_fingerprints"]) == {
            "documents.json",
            "bm25_metadata.json",
        }
        assert status.path == output
        MockChunker.assert_called_once_with(
            language="python",
            repo_config=ANY,
            max_lines_per_chunk=300,
            include_header_epilogue=True,
        )
        mock_chunker_instance.chunk_repository.assert_called_once_with(
            repo_path="/fake/repo", strict=True
        )
        mock_indexer_instance.save_index.assert_called_once_with(output)

    def test_incremental_delegates_to_build(self):
        builder = BM25IndexBuilder()
        with patch.object(builder, "build", return_value="result") as mock_build:
            result = builder.incremental_update(
                scope="repo", repo_path="/x", output_dir="/y"
            )
            mock_build.assert_called_once_with("repo", repo_path="/x", output_dir="/y")
            assert result == "result"

    def test_artifact_identity_tracks_source_body_indexing(self):
        assert BM25IndexBuilder().artifact_identity()["builder_schema"] == 8

    def test_build_indexes_source_without_symbol_definitions(self, tmp_path):
        from codenib.index.sparse_idx.bm25_index import BM25CodeIndexer

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "settings.py").write_text(
            'UNIQUE_RUNTIME_MODE = "safe_local"\n',
            encoding="utf-8",
        )
        output = tmp_path / "bm25"

        status = BM25IndexBuilder(languages=["python"]).build(
            scope="current_repo",
            repo_path=str(repo),
            output_dir=str(output),
        )

        assert status.metadata["source_file_count"] == 1
        index = BM25CodeIndexer()
        index.load_index(str(output))
        results = index.search("safe_local", top_k=1)
        assert results
        assert results[0].file == "settings.py"


# ---------------------------------------------------------------------------
# VectorIndexBuilder
# ---------------------------------------------------------------------------


def _write_mock_vector_generation(root: Path, model_suffix: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"config_{model_suffix}.json").write_text(
        '{"level_artifacts": {}}',
        encoding="utf-8",
    )
    level = root / "l2"
    level.mkdir()
    (level / f"config_{model_suffix}.json").write_text("{}", encoding="utf-8")
    (level / f"index_{model_suffix}.faiss").write_bytes(b"new-vector")
    (level / f"documents_{model_suffix}.pkl").write_bytes(b"new-documents")


def _tree_file_bytes(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class TestVectorIndexBuilder:
    @patch("codenib.index.embedding.builders.build_hierarchical_vector_store")
    def test_build_returns_status_with_stats(self, mock_build_fn, tmp_path):
        mock_vs = MagicMock()
        mock_vs.l0_documents = ["d1", "d2"]
        mock_vs.l2_documents = ["d3", "d4", "d5"]

        def build_vector(**kwargs):
            build_path = Path(kwargs["index_path"])
            _write_mock_vector_generation(build_path, "test-model")
            return mock_vs

        mock_build_fn.side_effect = build_vector

        builder = VectorIndexBuilder(
            languages=["python"],
            embedding_model="test-model",
            embedding_dimension=384,
        )
        output_path = tmp_path / "vector"
        output_path.mkdir()
        (output_path / "config_test-model.json").write_text(
            '{"level_artifacts": {}}',
            encoding="utf-8",
        )
        output = str(output_path)
        status = builder.build(
            scope="current_repo",
            repo_path="/fake/repo",
            output_dir=output,
        )

        assert status.index_type == "vector"
        assert status.state == IndexState.FRESH
        assert status.metadata["embedding_model"] == "test-model"
        assert status.metadata["embedding_provider"] == "huggingface"
        assert status.metadata["embedding_dimension"] == 384
        assert status.metadata["dimension"] == 384
        assert status.metadata["embedding_kwargs"] == {}
        assert status.metadata["index_metric"] == "ip"
        assert status.metadata["persistence_config_fingerprint"]["file"] == (
            "config_test-model.json"
        )
        assert status.metadata["document_count"] == {"l0": 2, "l2": 3}
        mock_build_fn.assert_called_once()
        assert mock_build_fn.call_args.kwargs["force_rebuild"] is True
        assert mock_build_fn.call_args.kwargs["strict_chunking"] is True
        assert mock_build_fn.call_args.kwargs["_atomic_publish"] is False
        assert not (output_path / VECTOR_VIEW_UPDATE_MARKER).exists()

    def test_build_publishes_one_complete_generation_without_stale_state(
        self,
        monkeypatch,
        tmp_path,
    ):
        output_path = tmp_path / "vector"
        output_path.mkdir()
        (output_path / "config_test-model.json").write_bytes(b"old-config")
        (output_path / "incremental_state.json").write_bytes(b"old-state")
        (output_path / "chunk_store.json").write_bytes(b"old-chunks")
        (output_path / VECTOR_VIEW_UPDATE_MARKER).write_bytes(b"stale-marker")
        (output_path / "stale-owned-file").write_bytes(b"stale")

        document = SimpleNamespace(
            page_content="def fresh():\n    return 1\n",
            metadata={
                "file": "fresh.py",
                "start_line": 0,
                "end_line": 1,
                "chunk_type": "function",
                "name": "fresh",
                "node_id": "fresh.py::fresh",
            },
        )
        vector_store = MagicMock(
            dimension=384,
            l0_documents=[],
            l2_documents=[document],
        )
        vector_store.get_embeddings_by_content_hash.return_value = {
            "fresh-hash": [0.1, 0.2]
        }
        calls = []

        def build_vector(**kwargs):
            calls.append(kwargs)
            build_path = Path(kwargs["index_path"])
            assert (build_path / VECTOR_VIEW_UPDATE_MARKER).is_file()
            _write_mock_vector_generation(build_path, "test-model")
            return vector_store

        monkeypatch.setattr(
            "codenib.index.embedding.builders.build_hierarchical_vector_store",
            build_vector,
        )
        builder = VectorIndexBuilder(
            embedding_model="test-model",
            embedding_dimension=384,
        )

        status = builder.build(
            scope="current_repo",
            repo_path=str(tmp_path),
            output_dir=str(output_path),
        )

        files = _tree_file_bytes(output_path)
        assert set(files) == {
            "chunk_store.json",
            "chunk_store.pkl",
            "config_test-model.json",
            "embeddings_cache.json",
            "embeddings_cache.npz",
            "embeddings_cache.pkl",
            "incremental_state.json",
            "l2/config_test-model.json",
            "l2/documents_test-model.pkl",
            "l2/index_test-model.faiss",
        }
        assert files["config_test-model.json"] != b"old-config"
        assert VECTOR_VIEW_UPDATE_MARKER not in files
        state = IncrementalState.load(output_path)
        assert state is not None
        assert state.index_path == str(output_path.resolve())
        assert status.path == str(output_path)
        assert calls[0]["_atomic_publish"] is False

    @pytest.mark.parametrize("replacement", ["directory", "symlink"])
    def test_build_private_root_replacement_cannot_publish_or_write_outside(
        self,
        replacement,
        monkeypatch,
        tmp_path,
    ):
        output_path = tmp_path / "vector"
        _write_mock_vector_generation(output_path, "test-model")
        (output_path / "incremental_state.json").write_bytes(b"old-state")
        before_files = _tree_file_bytes(output_path)
        before_ownership = capture_directory_ownership(output_path)

        outside = tmp_path / "outside"
        outside.mkdir()
        victim = outside / "victim.bin"
        victim.write_bytes(b"outside-original")
        vector_store = MagicMock(
            dimension=384,
            l0_documents=[],
            l2_documents=[],
        )

        def replace_build_root(**kwargs):
            anchored_root = Path(kwargs["index_path"])
            named_root = anchored_root.resolve(strict=True)
            moved_root = named_root.with_name(f"{named_root.name}.moved")
            named_root.rename(moved_root)
            if replacement == "directory":
                named_root.mkdir()
                (named_root / "attacker-tree").write_bytes(b"attacker")
            else:
                named_root.symlink_to(outside, target_is_directory=True)
            (anchored_root / "victim.bin").write_bytes(b"private-only")
            _write_mock_vector_generation(anchored_root, "test-model")
            return vector_store

        monkeypatch.setattr(
            "codenib.index.embedding.builders.build_hierarchical_vector_store",
            replace_build_root,
        )
        builder = VectorIndexBuilder(
            embedding_model="test-model",
            embedding_dimension=384,
        )

        with pytest.raises(RuntimeError, match="private build root changed"):
            builder.build(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(output_path),
            )

        assert _tree_file_bytes(output_path) == before_files
        after_ownership = capture_directory_ownership(output_path)
        assert directory_ownership_root_identity(after_ownership) == (
            directory_ownership_root_identity(before_ownership)
        )
        assert directory_ownership_digest(after_ownership) == (
            directory_ownership_digest(before_ownership)
        )
        assert victim.read_bytes() == b"outside-original"

    @pytest.mark.parametrize(
        "phase",
        [
            "marker_begin",
            "vector_save",
            "after_vector",
            "seed",
            "save",
            "state_save",
            "marker_finish",
            "before_publish",
            "after_publish_rename",
        ],
    )
    @pytest.mark.parametrize(
        "exception_type",
        [KeyboardInterrupt, SystemExit, GeneratorExit],
    )
    def test_incremental_rebuild_base_exception_restores_previous_generation(
        self,
        phase,
        exception_type,
        monkeypatch,
        tmp_path,
    ):
        import codenib._atomic_directory as atomic_directory
        from codenib._captured_directory import OwnedDirectoryStage
        from codenib.index.incremental import EmbeddingsCache, IncrementalChunkStore
        from codenib.index.incremental import IncrementalState as StateType

        output_path = tmp_path / "vector"
        _write_mock_vector_generation(output_path, "test-model")
        (output_path / "chunk_store.json").write_bytes(b"old-chunks")
        (output_path / "embeddings_cache.json").write_bytes(b"old-cache")
        (output_path / "embeddings_cache.npz").write_bytes(b"old-vectors")
        IncrementalState(
            last_commit="a" * 40,
            chunk_store_path="chunk_store.pkl",
            embeddings_cache_path="embeddings_cache.pkl",
            index_path=str(output_path.resolve()),
            build_levels=["l0", "l2"],
        ).save(output_path)

        document = SimpleNamespace(
            page_content="def replacement():\n    return 2\n",
            metadata={
                "file": "replacement.py",
                "start_line": 0,
                "end_line": 1,
                "chunk_type": "function",
                "name": "replacement",
                "node_id": "replacement.py::replacement",
            },
        )
        vector_store = MagicMock(
            dimension=384,
            l0_documents=[],
            l2_documents=[document],
        )
        vector_store.get_embeddings_by_content_hash.return_value = {
            "replacement-hash": [0.3, 0.4]
        }

        def interrupt(*_args, **_kwargs):
            raise exception_type(f"interrupt during {phase}")

        def build_vector(**kwargs):
            _write_mock_vector_generation(Path(kwargs["index_path"]), "test-model")
            if phase == "vector_save":
                interrupt()
            return vector_store

        monkeypatch.setattr(
            "codenib.index.embedding.builders.build_hierarchical_vector_store",
            build_vector,
        )
        builder = VectorIndexBuilder(
            embedding_model="test-model",
            embedding_dimension=384,
        )
        previous_artifact = builder.artifact_identity()
        before_files = _tree_file_bytes(output_path)
        before_ownership = capture_directory_ownership(output_path)
        authorization = _mint_trusted_local_admin_authorization(
            before_ownership,
            view_type="vector",
            semantic_contract=previous_artifact,
            evidence=("verified-incremental-rollback-test",),
        )

        if phase == "marker_begin":
            monkeypatch.setattr(
                "codenib.index.embedding.artifact_integrity."
                "begin_vector_view_update",
                interrupt,
            )
        elif phase == "after_vector":
            monkeypatch.setattr(builder, "_validate_vector_dimension", interrupt)
        elif phase == "seed":
            monkeypatch.setattr(
                IncrementalChunkStore,
                "from_chunks",
                classmethod(interrupt),
            )
        elif phase == "save":
            monkeypatch.setattr(EmbeddingsCache, "save", interrupt)
        elif phase == "state_save":
            monkeypatch.setattr(StateType, "save", interrupt)
        elif phase == "marker_finish":
            monkeypatch.setattr(
                "codenib.index.embedding.artifact_integrity."
                "finish_vector_view_update",
                interrupt,
            )
        elif phase == "before_publish":
            real_publish = OwnedDirectoryStage.publish

            def interrupt_final_publish(stage, *args, **kwargs):
                if stage.destination == output_path.absolute():
                    return interrupt()
                return real_publish(stage, *args, **kwargs)

            monkeypatch.setattr(
                OwnedDirectoryStage,
                "publish",
                interrupt_final_publish,
            )
        elif phase == "after_publish_rename":
            real_rename = atomic_directory._rename_noreplace_at
            interrupted = False

            def interrupt_after_rename(source, destination, source_fd, dest_fd):
                nonlocal interrupted
                real_rename(source, destination, source_fd, dest_fd)
                if (
                    source.startswith(".vector.normalize-")
                    and destination == "vector"
                    and not interrupted
                ):
                    interrupted = True
                    interrupt()

            monkeypatch.setattr(
                atomic_directory,
                "_rename_noreplace_at",
                interrupt_after_rename,
            )

        with pytest.raises(exception_type, match=phase):
            builder.incremental_update(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(output_path),
                last_commit="a" * 40,
                previous_artifact_config=previous_artifact,
                native_index_authorization=authorization,
            )

        assert _tree_file_bytes(output_path) == before_files
        after_ownership = capture_directory_ownership(output_path)
        assert directory_ownership_root_identity(after_ownership) == (
            directory_ownership_root_identity(before_ownership)
        )
        assert directory_ownership_digest(after_ownership) == (
            directory_ownership_digest(before_ownership)
        )
        assert not list(tmp_path.glob(".vector.generation-build-*"))

    def test_incremental_failure_falls_back_to_full_build(self, tmp_path):
        builder = VectorIndexBuilder(
            embedding_model="test-model",
            embedding_dimension=384,
        )
        rebuilt = IndexStatus(
            index_type="vector",
            state=IndexState.FRESH,
            last_built=1.0,
            age_seconds=0.0,
            scope="current_repo",
            path=str(tmp_path / "vector"),
            metadata={},
        )

        with (
            patch.object(
                builder,
                "_incremental_update_once",
                side_effect=ValueError("corrupt cache"),
            ),
            patch.object(builder, "build", return_value=rebuilt) as mock_build,
        ):
            result = builder.incremental_update(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(tmp_path / "vector"),
                last_commit="a" * 40,
            )

        mock_build.assert_called_once_with(
            "current_repo",
            repo_path=str(tmp_path),
            output_dir=str(tmp_path / "vector"),
        )
        assert result is rebuilt
        assert result.metadata["update_mode"] == "full_rebuild"
        assert result.metadata["incremental_fallback_reason"] == "ValueError"

    def test_explicit_invalid_native_authority_propagates(self, tmp_path):
        builder = VectorIndexBuilder(
            embedding_model="test-model",
            embedding_dimension=384,
        )

        with patch.object(builder, "build") as mock_build:
            with pytest.raises(
                InvalidNativeIndexAuthorizationError,
                match="malformed",
            ):
                builder.incremental_update(
                    scope="current_repo",
                    repo_path=str(tmp_path),
                    output_dir=str(tmp_path / "vector"),
                    last_commit="a" * 40,
                    native_index_authorization=object(),
                )

        mock_build.assert_not_called()

    def test_invalid_native_authority_error_is_propagated_unchanged(self, tmp_path):
        builder = VectorIndexBuilder(
            embedding_model="test-model",
            embedding_dimension=384,
        )
        authorization_error = InvalidNativeIndexAuthorizationError(
            "authorization does not match captured bytes"
        )

        with (
            patch.object(
                builder,
                "_incremental_update_once",
                side_effect=authorization_error,
            ),
            patch.object(builder, "build") as mock_build,
        ):
            with pytest.raises(InvalidNativeIndexAuthorizationError) as raised:
                builder.incremental_update(
                    scope="current_repo",
                    repo_path=str(tmp_path),
                    output_dir=str(tmp_path / "vector"),
                    last_commit="a" * 40,
                    native_index_authorization=object(),
                )

        assert raised.value is authorization_error
        mock_build.assert_not_called()

    def test_valid_native_authority_is_passed_to_incremental_path(self, tmp_path):
        builder = VectorIndexBuilder(
            embedding_model="test-model",
            embedding_dimension=384,
        )
        authorization_root = tmp_path / "authorized-vector"
        authorization_root.mkdir()
        (authorization_root / "index.faiss").write_bytes(b"native")
        authorization = _mint_trusted_local_admin_authorization(
            capture_directory_ownership(authorization_root),
            view_type="vector",
            semantic_contract=builder.artifact_identity(),
            evidence=("verified-test-boundary",),
        )
        updated = IndexStatus(
            index_type="vector",
            state=IndexState.FRESH,
            path=str(tmp_path / "vector"),
            metadata={"update_mode": "incremental"},
        )

        with patch.object(
            builder,
            "_incremental_update_once",
            return_value=updated,
        ) as incremental:
            result = builder.incremental_update(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(tmp_path / "vector"),
                last_commit="a" * 40,
                native_index_authorization=authorization,
            )

        assert result is updated
        assert incremental.call_args.kwargs["native_index_authorization"] is (
            authorization
        )

    @pytest.mark.parametrize("mismatch", ["tree", "semantic"])
    def test_exact_native_authority_rejects_before_model_initialization(
        self,
        tmp_path,
        mismatch,
    ):
        output_path = tmp_path / "vector"
        IncrementalState(
            last_commit="a" * 40,
            chunk_store_path="chunk_store.pkl",
            embeddings_cache_path="embeddings_cache.pkl",
            index_path=str(output_path.resolve()),
            build_levels=["l0", "l2"],
        ).save(output_path)
        (output_path / "chunk_store.json").write_text("{}", encoding="utf-8")
        (output_path / "embeddings_cache.json").write_text("[]", encoding="utf-8")
        (output_path / "embeddings_cache.npz").write_bytes(b"placeholder")

        builder = VectorIndexBuilder(
            embedding_model="test-model",
            embedding_dimension=384,
        )
        expected_semantic = builder.artifact_identity()
        authorization_root = output_path
        authorization_semantic = expected_semantic
        if mismatch == "tree":
            authorization_root = tmp_path / "other-vector"
            authorization_root.mkdir()
            (authorization_root / "config_test-model.json").write_text(
                "{}",
                encoding="utf-8",
            )
        else:
            authorization_semantic = {"embedding_dimension": 1024}
        authorization = _mint_trusted_local_admin_authorization(
            capture_directory_ownership(authorization_root),
            view_type="vector",
            semantic_contract=authorization_semantic,
            evidence=(f"wrong-{mismatch}-test",),
        )

        with (
            patch(
                "codenib.index.embedding.vector_store.CodeVectorStore",
                side_effect=AssertionError(
                    "exact authorization must fail before model initialization"
                ),
            ) as store_type,
            patch.object(builder, "build") as full_build,
        ):
            with pytest.raises(
                InvalidNativeIndexAuthorizationError,
                match="does not match captured bytes",
            ):
                builder.incremental_update(
                    scope="current_repo",
                    repo_path=str(tmp_path),
                    output_dir=str(output_path),
                    last_commit="a" * 40,
                    previous_artifact_config=expected_semantic,
                    native_index_authorization=authorization,
                )

        store_type.assert_not_called()
        full_build.assert_not_called()

    def test_missing_native_authority_rebuilds_before_model_initialization(
        self,
        tmp_path,
    ):
        builder = VectorIndexBuilder(
            embedding_model="test-model",
            embedding_dimension=384,
        )
        rebuilt = IndexStatus(
            index_type="vector",
            state=IndexState.FRESH,
            path=str(tmp_path / "vector"),
            metadata={},
        )

        with (
            patch(
                "codenib.index.embedding.vector_store.CodeVectorStore",
                side_effect=AssertionError(
                    "missing authority must not initialize an embedding model"
                ),
            ) as store_type,
            patch.object(builder, "build", return_value=rebuilt) as mock_build,
        ):
            result = builder.incremental_update(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(tmp_path / "vector"),
                last_commit="a" * 40,
            )

        store_type.assert_not_called()
        mock_build.assert_called_once()
        assert result.metadata["update_mode"] == "full_rebuild"
        assert result.metadata["incremental_fallback_reason"] == (
            "MissingNativeIndexAuthorizationError"
        )

    def test_missing_incremental_state_attempts_one_rebuild(self, tmp_path):
        builder = VectorIndexBuilder(
            embedding_model="test-model",
            embedding_dimension=384,
        )

        with patch.object(
            builder,
            "build",
            side_effect=RuntimeError("rebuild failed"),
        ) as mock_build:
            with pytest.raises(RuntimeError, match="rebuild failed"):
                builder.incremental_update(
                    scope="current_repo",
                    repo_path=str(tmp_path),
                    output_dir=str(tmp_path / "vector"),
                    last_commit="a" * 40,
                )

        mock_build.assert_called_once()

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("last_commit", "b" * 40),
            ("build_levels", ["l2"]),
            ("chunk_store_path", "../chunk_store.pkl"),
            ("embeddings_cache_path", "../embeddings_cache.pkl"),
            ("index_path", "/different/vector/view"),
        ],
    )
    def test_incompatible_incremental_state_forces_rebuild(
        self,
        tmp_path,
        field,
        value,
    ):
        output_path = tmp_path / "vector"
        state = IncrementalState(
            last_commit="a" * 40,
            chunk_store_path="chunk_store.pkl",
            embeddings_cache_path="embeddings_cache.pkl",
            index_path=str(output_path.resolve()),
            build_levels=["l0", "l2"],
        )
        setattr(state, field, value)
        state.save(output_path)
        builder = VectorIndexBuilder(
            embedding_model="test-model",
            embedding_dimension=384,
        )
        rebuilt = IndexStatus(
            index_type="vector",
            state=IndexState.FRESH,
            path=str(output_path),
            metadata={},
        )

        with patch.object(builder, "build", return_value=rebuilt) as mock_build:
            result = builder.incremental_update(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(output_path),
                last_commit="a" * 40,
            )

        mock_build.assert_called_once()
        assert result.metadata["update_mode"] == "full_rebuild"

    def test_authorized_incremental_generation_rebuilds_before_model_init(
        self,
        tmp_path,
    ):
        output_path = tmp_path / "vector"
        IncrementalState(
            last_commit="a" * 40,
            chunk_store_path="chunk_store.pkl",
            embeddings_cache_path="embeddings_cache.pkl",
            index_path=str(output_path.resolve()),
            build_levels=["l0", "l2"],
        ).save(output_path)
        (output_path / "chunk_store.json").write_text("{}", encoding="utf-8")
        (output_path / "embeddings_cache.json").write_text("[]", encoding="utf-8")
        (output_path / "embeddings_cache.npz").write_bytes(b"placeholder")
        builder = VectorIndexBuilder(
            embedding_model="test-model",
            embedding_dimension=384,
        )
        previous = {
            **builder.artifact_identity(),
            "persistence_config_fingerprint": {
                "file": "config_test-model.json",
                "size": 1,
                "sha256": "0" * 64,
            },
        }
        authorization = _mint_trusted_local_admin_authorization(
            capture_directory_ownership(output_path),
            view_type="vector",
            semantic_contract=previous,
            evidence=("verified-previous-generation",),
        )
        rebuilt = IndexStatus(
            index_type="vector",
            state=IndexState.FRESH,
            path=str(output_path),
            metadata={},
        )
        before = _tree_file_bytes(output_path)

        with (
            patch(
                "codenib.index.embedding.vector_store.CodeVectorStore",
                side_effect=AssertionError(
                    "disabled in-place update must not initialize a model"
                ),
            ) as store_type,
            patch.object(builder, "build", return_value=rebuilt) as full_build,
        ):
            result = builder.incremental_update(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(output_path),
                last_commit="a" * 40,
                previous_artifact_config=previous,
                native_index_authorization=authorization,
            )

        store_type.assert_not_called()
        full_build.assert_called_once_with(
            "current_repo",
            repo_path=str(tmp_path),
            output_dir=str(output_path),
            native_index_authorization=authorization,
        )
        assert result is rebuilt
        assert result.metadata["update_mode"] == "full_rebuild"
        assert result.metadata["incremental_fallback_reason"] == (
            "_AtomicIncrementalRebuildRequired"
        )
        assert _tree_file_bytes(output_path) == before
        assert not (output_path / VECTOR_VIEW_UPDATE_MARKER).exists()

    @pytest.mark.parametrize("missing", ["chunk_json", "embedding_pair"])
    def test_pickle_only_incremental_state_forces_rebuild(self, tmp_path, missing):
        output_path = tmp_path / "vector"
        IncrementalState(
            last_commit="a" * 40,
            chunk_store_path="chunk_store.pkl",
            embeddings_cache_path="embeddings_cache.pkl",
            index_path=str(output_path.resolve()),
            build_levels=["l0", "l2"],
        ).save(output_path)
        (output_path / "chunk_store.pkl").write_bytes(b"legacy")
        (output_path / "embeddings_cache.pkl").write_bytes(b"legacy")
        if missing != "chunk_json":
            (output_path / "chunk_store.json").write_text("{}", encoding="utf-8")
        if missing != "embedding_pair":
            (output_path / "embeddings_cache.json").write_text(
                "[]",
                encoding="utf-8",
            )
            (output_path / "embeddings_cache.npz").write_bytes(b"placeholder")
        builder = VectorIndexBuilder(
            embedding_model="test-model",
            embedding_dimension=384,
        )
        rebuilt = IndexStatus(
            index_type="vector",
            state=IndexState.FRESH,
            path=str(output_path),
            metadata={},
        )

        with patch.object(builder, "build", return_value=rebuilt) as mock_build:
            result = builder.incremental_update(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(output_path),
                last_commit="a" * 40,
            )

        mock_build.assert_called_once()
        assert result.metadata["update_mode"] == "full_rebuild"

    def test_artifact_identity_is_shared_by_full_and_incremental_statuses(self):
        builder = VectorIndexBuilder(
            embedding_model="test-model",
            embedding_dimension=384,
            embedding_kwargs={"revision": "model-commit"},
            index_metric="l2",
        )

        identity = builder.artifact_identity()
        assert identity == {
            "builder_schema": 6,
            "embedding_model": "test-model",
            "embedding_provider": "huggingface",
            "embedding_dimension": 384,
            "dimension": 384,
            "embedding_endpoint": None,
            "embedding_kwargs": {"revision": "model-commit"},
            "embedding_route": {
                "schema": "codenib.inference-route.v1",
                "operation": "embeddings",
                "provider": "huggingface",
                "model": "test-model",
                "endpoint": None,
                "dimension": 384,
                "options": {"revision": "model-commit"},
            },
            "embedding_fingerprint": identity["embedding_fingerprint"],
            "index_metric": "l2",
            "languages": ["python"],
            "levels": ["l0", "l2"],
            "max_lines_per_chunk": 300,
            "chunking_failure_policy": "fail",
            "repository_filter_policy": REPOSITORY_FILTER_POLICY_VERSION,
        }

    def test_runtime_options_do_not_change_identity_or_appear_in_repr(self):
        first = VectorIndexBuilder(
            embedding_model="text-embedding-3-small",
            embedding_provider="openai",
            embedding_endpoint="https://inference.example.test/v1",
            embedding_dimension=1536,
            embedding_runtime_kwargs={"api_key": "first-secret", "timeout": 10},
        )
        second = VectorIndexBuilder(
            embedding_model="text-embedding-3-small",
            embedding_provider="openai",
            embedding_endpoint="https://inference.example.test/v1",
            embedding_dimension=1536,
            embedding_runtime_kwargs={"api_key": "second-secret", "timeout": 60},
        )

        assert first.artifact_identity() == second.artifact_identity()
        assert "first-secret" not in repr(first)

    def test_runtime_options_cannot_override_embedding_semantics(self):
        builder = VectorIndexBuilder(
            embedding_runtime_kwargs={"query_prompt": "different"},
        )

        with pytest.raises(ValueError, match="vector compatibility"):
            builder._embedding_call_kwargs()

    @patch("codenib.index.embedding.builders.build_hierarchical_vector_store")
    def test_build_rejects_provider_dimension_mismatch(self, mock_build_fn, tmp_path):
        mock_build_fn.return_value = SimpleNamespace(
            dimension=1024,
            l0_documents=[],
            l2_documents=["doc"],
        )
        builder = VectorIndexBuilder(
            embedding_model="test-model",
            embedding_dimension=768,
        )

        output_path = tmp_path / "vector"
        with pytest.raises(ValueError, match="returned dimension 1024, expected 768"):
            builder.build(
                scope="current_repo",
                repo_path="/fake/repo",
                output_dir=str(output_path),
            )
        assert not output_path.exists()

    @patch("codenib.index.embedding.builders.build_hierarchical_vector_store")
    def test_remote_runtime_secret_is_not_persisted(self, mock_build_fn, tmp_path):
        mock_vs = MagicMock(l0_documents=[], l2_documents=["doc"])

        def build_vector(**kwargs):
            build_path = Path(kwargs["index_path"])
            _write_mock_vector_generation(
                build_path,
                "text-embedding-3-small",
            )
            return mock_vs

        mock_build_fn.side_effect = build_vector
        output_path = tmp_path / "vector"
        output_path.mkdir()
        (output_path / "config_text-embedding-3-small.json").write_text(
            '{"level_artifacts": {}}',
            encoding="utf-8",
        )
        builder = VectorIndexBuilder(
            embedding_model="text-embedding-3-small",
            embedding_provider="openai",
            embedding_endpoint="https://inference.example.test/v1",
            embedding_dimension=1536,
            embedding_credential_env="MODELS_TOKEN",
            embedding_runtime_kwargs={"api_key": "runtime-secret", "timeout": 10},
        )

        status = builder.build(
            scope="current_repo",
            repo_path="/fake/repo",
            output_dir=str(output_path),
        )

        serialized = json.dumps(status.metadata, sort_keys=True)
        assert "runtime-secret" not in serialized
        assert "MODELS_TOKEN" not in serialized
        assert status.metadata["embedding_endpoint"] == (
            "https://inference.example.test/v1"
        )
        call = mock_build_fn.call_args.kwargs
        assert call["embedding_kwargs"]["api_key"] == "runtime-secret"
        assert call["embedding_kwargs"]["base_url"] == (
            "https://inference.example.test/v1"
        )


# ---------------------------------------------------------------------------
# SymbolGraphBuilder
# ---------------------------------------------------------------------------


class TestSymbolGraphBuilder:
    @pytest.fixture(autouse=True)
    def _stable_query_surface_digest(self, monkeypatch):
        monkeypatch.setattr(
            "codenib.scip_interface.query_surface.query_surface_sha256",
            lambda _graph: "c" * 64,
        )

    def test_build_returns_status(self, monkeypatch, tmp_path):
        from codenib import ls_router

        mock_graph = MagicMock()
        mock_graph.graph.vs = list(range(50))  # 50 nodes
        calls = []

        def fake_build_graph_for_languages(*args, **kwargs):
            calls.append((args, kwargs))
            graph_receipt = _fake_graph_output_receipt(args[1])
            (Path(args[1]) / "index.decoded").write_text(
                "post-hoc artifact without a consumed receipt",
                encoding="utf-8",
            )
            return GraphBuildResult(
                graph=mock_graph,
                requested_languages=["python"],
                available_languages=["python"],
                failed_languages={},
                graph_output_receipt=graph_receipt,
            )

        monkeypatch.setattr(
            ls_router,
            "build_graph_for_languages_with_report",
            fake_build_graph_for_languages,
        )

        builder = SymbolGraphBuilder(language="python")
        output = str(tmp_path / "graph")
        status = builder.build(
            scope="current_repo",
            repo_path="/fake/repo",
            output_dir=output,
        )

        assert status.index_type == "symbol_graph"
        assert status.state == IndexState.FRESH
        assert status.metadata["node_count"] == 50
        assert status.metadata["edge_count"] == 0
        assert status.metadata["language"] == "python"
        assert status.metadata["languages"] == ["python"]
        assert status.metadata["builder_schema"] == 5
        assert status.metadata["allow_project_preparation"] is True
        assert status.metadata["target_dir"] is None
        assert status.metadata["update_mode"] == "full_rebuild"
        assert status.metadata["scip_decoded_artifacts"] == {}
        assert status.metadata["graph_artifact"] == {
            "relative_path": "graph.pkl",
            "size": len(b"test-symbol-graph\n"),
            "sha256": regular_file_fingerprint(Path(output) / "graph.pkl")["sha256"],
        }
        assert status.metadata["query_surface_sha256"] == "c" * 64
        assert calls == [
            (
                ("/fake/repo", output),
                {
                    "allow_partial": False,
                    "languages": ["python"],
                    "project_name": "repo",
                    "skip_level": None,
                    "exclude_patterns": default_exclude_patterns(),
                    "graph_route": "active",
                    "capture_artifact_receipts": True,
                },
            )
        ]

    def test_artifact_identity_tracks_project_preparation_policy(self):
        enabled = SymbolGraphBuilder(allow_project_preparation=True)
        disabled = SymbolGraphBuilder(allow_project_preparation=False)

        assert enabled.artifact_identity()["builder_schema"] == 5
        assert enabled.artifact_identity()["allow_project_preparation"] is True
        assert disabled.artifact_identity()["allow_project_preparation"] is False
        assert enabled.artifact_identity() != disabled.artifact_identity()

    def test_build_records_existing_decoded_scip_artifact(self, monkeypatch, tmp_path):
        from codenib import ls_router
        from codenib.compiler.artifact_fingerprints import regular_file_fingerprint

        mock_graph = MagicMock()
        mock_graph.graph.vs = [MagicMock()]
        output = tmp_path / "graph"
        output.mkdir()
        graph_receipt = _fake_graph_output_receipt(output)
        decoded = output / "index.decoded"
        decoded.write_text("documents {}\n", encoding="utf-8")
        decoded_receipt = {
            "path": str(decoded.resolve()),
            **regular_file_fingerprint(decoded),
        }

        monkeypatch.setattr(
            ls_router,
            "build_graph_for_languages_with_report",
            lambda *args, **kwargs: GraphBuildResult(
                graph=mock_graph,
                requested_languages=["python"],
                available_languages=["python"],
                failed_languages={},
                decoded_input_receipts={"python": decoded_receipt},
                graph_output_receipt=graph_receipt,
            ),
        )

        status = SymbolGraphBuilder(language="python").build(
            scope="current_repo",
            repo_path="/fake/repo",
            output_dir=str(output),
        )

        assert status.metadata["scip_decoded_artifacts"] == {
            "python": {
                "relative_path": "index.decoded",
                **regular_file_fingerprint(decoded),
            }
        }

    def test_build_rejects_graph_replaced_after_writer_receipt(
        self, monkeypatch, tmp_path
    ):
        from codenib import ls_router

        mock_graph = MagicMock()
        mock_graph.graph.vs = [MagicMock()]
        mock_graph.graph.es = []
        output = tmp_path / "graph"
        graph_receipt = _fake_graph_output_receipt(output)
        (output / "graph.pkl").write_bytes(b"replacement graph\n")
        monkeypatch.setattr(
            ls_router,
            "build_graph_for_languages_with_report",
            lambda *args, **kwargs: GraphBuildResult(
                graph=mock_graph,
                requested_languages=["python"],
                available_languages=["python"],
                failed_languages={},
                graph_output_receipt=graph_receipt,
            ),
        )

        with pytest.raises(ValueError, match="changed after its writer"):
            SymbolGraphBuilder(language="python").build(
                scope="current_repo",
                repo_path="/fake/repo",
                output_dir=str(output),
            )

    def test_build_rejects_same_count_query_surface_mutation(
        self, monkeypatch, tmp_path
    ):
        from codenib import ls_router

        mock_graph = MagicMock()
        mock_graph.graph.vs = [MagicMock()]
        mock_graph.graph.es = []
        output = tmp_path / "graph"
        graph_receipt = _fake_graph_output_receipt(output)
        graph_receipt["query_surface_sha256"] = "d" * 64
        monkeypatch.setattr(
            ls_router,
            "build_graph_for_languages_with_report",
            lambda *args, **kwargs: GraphBuildResult(
                graph=mock_graph,
                requested_languages=["python"],
                available_languages=["python"],
                failed_languages={},
                graph_output_receipt=graph_receipt,
            ),
        )

        with pytest.raises(ValueError, match="query surface changed"):
            SymbolGraphBuilder(language="python").build(
                scope="current_repo",
                repo_path="/fake/repo",
                output_dir=str(output),
            )

    def test_build_rejects_none_graph(self, monkeypatch, tmp_path):
        from codenib import ls_router

        monkeypatch.setattr(
            ls_router,
            "build_graph_for_languages_with_report",
            lambda *args, **kwargs: GraphBuildResult(
                graph=None,
                requested_languages=["python"],
                available_languages=[],
                failed_languages={"python": "indexer returned no graph"},
            ),
        )

        builder = SymbolGraphBuilder()
        output = str(tmp_path / "graph")
        with pytest.raises(RuntimeError, match="returned no graph"):
            builder.build(
                scope="current_repo",
                repo_path="/fake/repo",
                output_dir=output,
            )

    def test_build_can_fall_back_when_compiler_returns_no_graph(
        self, monkeypatch, tmp_path
    ):
        from codenib import ls_router

        monkeypatch.setattr(
            ls_router,
            "build_graph_for_languages_with_report",
            lambda *args, **kwargs: GraphBuildResult(
                graph=None,
                requested_languages=["python"],
                available_languages=[],
                failed_languages={"python": "indexer returned no graph"},
                index_generation_reports={
                    "python": {
                        "status": "failed",
                        "complete": False,
                        "partial": False,
                        "document_count": 0,
                    }
                },
            ),
        )
        monkeypatch.setattr(
            "codenib.git_snapshot.GitSourceSurface.load",
            lambda _repo_path: object(),
        )
        monkeypatch.setattr(
            "codenib.languages.extensions_for_language",
            lambda _language, _capability: {".py"},
        )

        def fake_supplement(graph, **_kwargs):
            assert graph.graph.vs[0]["type"] == "root"
            graph.add_file_node("fallback.py")
            graph.build_range_indexes()
            return {
                "coverage_before": 0.0,
                "coverage_after": 1.0,
                "supplemented_files": 1,
            }

        monkeypatch.setattr(
            "codenib.graph.source_coverage.supplement_graph_source_coverage",
            fake_supplement,
        )

        output = tmp_path / "graph"
        status = SymbolGraphBuilder(
            allow_partial_index=True,
            source_coverage_fallback=True,
        ).build(
            scope="current_repo",
            repo_path="/fake/repo",
            output_dir=str(output),
        )

        report = status.metadata["source_coverage_report"]
        assert status.state == IndexState.FRESH
        assert status.metadata["available_languages"] == ["python"]
        assert status.metadata["compiler_available_languages"] == []
        assert report["compiler_graph_available"] is False
        assert report["compiler_index_complete"] is False
        assert report["compiler_nodes"] == 0
        assert report["compiler_edges"] == 0
        assert report["coverage_after"] == 1.0
        assert (output / "graph.pkl").is_file()

    def test_build_rejects_incomplete_source_coverage_fallback(
        self, monkeypatch, tmp_path
    ):
        from codenib import ls_router

        monkeypatch.setattr(
            ls_router,
            "build_graph_for_languages_with_report",
            lambda *args, **kwargs: GraphBuildResult(
                graph=None,
                requested_languages=["python"],
                available_languages=[],
                failed_languages={"python": "indexer returned no graph"},
            ),
        )
        monkeypatch.setattr(
            "codenib.git_snapshot.GitSourceSurface.load",
            lambda _repo_path: object(),
        )
        monkeypatch.setattr(
            "codenib.languages.extensions_for_language",
            lambda _language, _capability: {".py"},
        )
        monkeypatch.setattr(
            "codenib.graph.source_coverage.supplement_graph_source_coverage",
            lambda *_args, **_kwargs: {"coverage_after": 0.5},
        )

        with pytest.raises(RuntimeError, match="did not cover"):
            SymbolGraphBuilder(source_coverage_fallback=True).build(
                scope="current_repo",
                repo_path="/fake/repo",
                output_dir=str(tmp_path / "graph"),
            )

    def test_build_forwards_multiple_languages(self, monkeypatch, tmp_path):
        from codenib import ls_router

        mock_graph = MagicMock()
        mock_graph.graph.vs = [MagicMock()]
        mock_graph.graph.es = []
        calls = []

        def fake_build_graph_for_languages(*args, **kwargs):
            calls.append((args, kwargs))
            graph_receipt = _fake_graph_output_receipt(args[1])
            return GraphBuildResult(
                graph=mock_graph,
                requested_languages=["python", "go"],
                available_languages=["python", "go"],
                failed_languages={},
                graph_output_receipt=graph_receipt,
            )

        monkeypatch.setattr(
            ls_router,
            "build_graph_for_languages_with_report",
            fake_build_graph_for_languages,
        )

        builder = SymbolGraphBuilder(language="python", languages=["python", "go"])
        output = str(tmp_path / "graph")
        status = builder.build(
            scope="current_repo",
            repo_path="/fake/repo",
            output_dir=output,
        )

        assert status.metadata["language"] == "python"
        assert status.metadata["languages"] == ["python", "go"]
        assert status.metadata["graph_route"] == "active"
        assert calls[0][1]["languages"] == ["python", "go"]

    def test_build_forwards_graph_route(self, monkeypatch, tmp_path):
        from codenib import ls_router

        mock_graph = MagicMock()
        mock_graph.graph.vs = [MagicMock()]
        calls = []

        def fake_build_graph_for_languages(*args, **kwargs):
            calls.append((args, kwargs))
            graph_receipt = _fake_graph_output_receipt(args[1])
            return GraphBuildResult(
                graph=mock_graph,
                requested_languages=["java"],
                available_languages=["java"],
                failed_languages={},
                graph_output_receipt=graph_receipt,
            )

        monkeypatch.setattr(
            ls_router,
            "build_graph_for_languages_with_report",
            fake_build_graph_for_languages,
        )

        builder = SymbolGraphBuilder(language="java", graph_route="scip-candidate")
        output = str(tmp_path / "graph")
        status = builder.build(
            scope="current_repo",
            repo_path="/fake/repo",
            output_dir=output,
        )

        assert status.metadata["graph_route"] == "scip-candidate"
        assert calls[0][1]["graph_route"] == "scip-candidate"

    def test_build_records_source_coverage_fallback_report(self, monkeypatch, tmp_path):
        from codenib import ls_router

        mock_graph = MagicMock()
        mock_graph.graph.vs = [MagicMock()]
        mock_graph.graph.es = []
        mock_graph.save_graph.side_effect = lambda path: Path(path).write_bytes(
            b"test-symbol-graph\n"
        )
        calls = []
        report = {
            "coverage_before": 0.5,
            "coverage_after": 1.0,
            "supplemented_files": ["missing.py"],
        }

        def fake_build_graph_for_languages(*args, **kwargs):
            calls.append((args, kwargs))
            return GraphBuildResult(
                graph=mock_graph,
                requested_languages=["python"],
                available_languages=["python"],
                failed_languages={},
                index_generation_reports={
                    "python": {
                        "status": "partial",
                        "complete": False,
                        "partial": True,
                        "document_count": 10,
                    }
                },
            )

        monkeypatch.setattr(
            ls_router,
            "build_graph_for_languages_with_report",
            fake_build_graph_for_languages,
        )
        monkeypatch.setattr(
            "codenib.compiler.artifact_quality.graph_file_paths",
            lambda _graph: {"covered.py"},
        )
        monkeypatch.setattr(
            "codenib.git_snapshot.GitSourceSurface.load",
            lambda _repo_path: object(),
        )
        monkeypatch.setattr(
            "codenib.languages.extensions_for_language",
            lambda _language, _capability: {".py"},
        )
        monkeypatch.setattr(
            "codenib.graph.source_coverage.supplement_graph_source_coverage",
            lambda *_args, **_kwargs: report,
        )

        builder = SymbolGraphBuilder(
            languages=["python"],
            allow_partial_index=True,
            source_coverage_fallback=True,
        )
        status = builder.build(
            scope="current_repo",
            repo_path="/fake/repo",
            output_dir=str(tmp_path / "graph"),
        )

        assert calls[0][1]["allow_partial_index"] is True
        assert status.metadata["source_coverage_fallback"] is True
        assert status.metadata["source_coverage_report"] == {
            "compiler_graph_available": True,
            "compiler_index_complete": False,
            "compiler_partial_languages": ["python"],
            "compiler_nodes": 1,
            "compiler_edges": 0,
            **report,
        }
        assert status.metadata["partial_index"] is True
        mock_graph.save_graph.assert_called_once()

    def test_build_records_partial_language_coverage(self, monkeypatch, tmp_path):
        from codenib import ls_router
        from codenib.ls_router import GraphBuildResult

        mock_graph = MagicMock()
        mock_graph.graph.vs = list(range(25))
        calls = []

        def fake_build(*args, **kwargs):
            calls.append((args, kwargs))
            graph_receipt = _fake_graph_output_receipt(args[1])
            return GraphBuildResult(
                graph=mock_graph,
                requested_languages=["python", "cpp"],
                available_languages=["python"],
                failed_languages={"cpp": "compilation database missing"},
                graph_output_receipt=graph_receipt,
            )

        monkeypatch.setattr(
            ls_router,
            "build_graph_for_languages_with_report",
            fake_build,
        )

        builder = SymbolGraphBuilder(
            languages=["python", "cpp"],
            allow_partial_languages=True,
        )
        status = builder.build(
            scope="current_repo",
            repo_path="/fake/repo",
            output_dir=str(tmp_path / "graph"),
        )

        assert status.state == IndexState.FRESH
        assert status.metadata["languages"] == ["python", "cpp"]
        assert status.metadata["available_languages"] == ["python"]
        assert status.metadata["failed_languages"] == {
            "cpp": "compilation database missing"
        }
        assert status.metadata["partial"] is True
        assert calls[0][1]["allow_partial"] is True


# ---------------------------------------------------------------------------
# register_default_builders
# ---------------------------------------------------------------------------


class TestRegisterDefaultBuilders:
    def test_registers_all_defaults(self):
        registry = IndexBuilderRegistry()
        register_default_builders(registry, languages=["python"])

        assert registry.has("bm25")
        assert registry.has("vector")
        assert registry.has("symbol_graph")
        assert registry.has("zoekt")
        vector = registry.get("vector")
        assert isinstance(vector, VectorIndexBuilder)
        assert vector.embedding_kwargs == {
            "model_kwargs": {"trust_remote_code": True},
            "revision": DEFAULT_EMBEDDING_REVISION,
        }
        assert vector.embedding_runtime_kwargs == {}

    def test_custom_params_forwarded(self):
        registry = IndexBuilderRegistry()
        register_default_builders(
            registry,
            languages=["rust", "python"],
            graph_route="scip-candidate",
            embedding_model="custom-model",
            embedding_revision="model-commit",
            embedding_dimension=512,
            embedding_batch_size=4,
            embedding_max_seq_length=8192,
        )

        bm25 = registry.get("bm25")
        assert isinstance(bm25, BM25IndexBuilder)
        assert bm25.languages == ["rust", "python"]

        vector = registry.get("vector")
        assert isinstance(vector, VectorIndexBuilder)
        assert vector.embedding_model == "custom-model"
        assert vector.embedding_dimension == 512
        assert vector.embedding_kwargs == {
            "max_seq_length": 8192,
            "revision": "model-commit",
        }
        assert vector.embedding_runtime_kwargs == {"default_batch_size": 4}

        symbol_graph = registry.get("symbol_graph")
        assert isinstance(symbol_graph, SymbolGraphBuilder)
        assert symbol_graph.language == "rust"
        assert symbol_graph.languages == ["rust", "python"]
        assert symbol_graph.graph_route == "scip-candidate"
        assert symbol_graph.allow_partial_languages is False
        assert symbol_graph.allow_partial_index is False
        assert symbol_graph.source_coverage_fallback is False

    def test_provider_alias_is_normalized_before_model_policy(self):
        registry = IndexBuilderRegistry()
        register_default_builders(
            registry,
            languages=["python"],
            embedding_provider="HUGGING-FACE",
        )

        vector = registry.get("vector")
        assert isinstance(vector, VectorIndexBuilder)
        assert vector.embedding_provider == "huggingface"
        assert vector.embedding_kwargs["revision"] == DEFAULT_EMBEDDING_REVISION

    def test_can_register_partial_multi_language_graph_builder(self):
        registry = IndexBuilderRegistry()
        register_default_builders(
            registry,
            languages=["python", "cpp"],
            allow_partial_graph_languages=True,
        )

        symbol_graph = registry.get("symbol_graph")
        assert isinstance(symbol_graph, SymbolGraphBuilder)
        assert symbol_graph.allow_partial_languages is True

    def test_can_register_partial_index_graph_builder(self):
        registry = IndexBuilderRegistry()
        register_default_builders(
            registry,
            languages=["python"],
            allow_partial_graph_index=True,
            graph_source_coverage_fallback=True,
        )

        symbol_graph = registry.get("symbol_graph")
        assert isinstance(symbol_graph, SymbolGraphBuilder)
        assert symbol_graph.allow_partial_index is True
        assert symbol_graph.source_coverage_fallback is True

    def test_partial_index_requires_source_coverage_fallback(self):
        with pytest.raises(ValueError, match="requires source_coverage_fallback"):
            SymbolGraphBuilder(allow_partial_index=True)


# ---------------------------------------------------------------------------
# ZoektIndexBuilder
# ---------------------------------------------------------------------------


class TestZoektIndexBuilder:
    def test_build_invokes_zoekt_git_index(self, tmp_path):
        builder = ZoektIndexBuilder()
        output_dir = tmp_path / "shards"

        completed = MagicMock()
        completed.returncode = 0
        completed.stderr = ""

        with (
            patch(
                "codenib.compiler.index_builders.shutil.which",
                return_value="/fake/zoekt-git-index",
            ),
            patch(
                "codenib.compiler.index_builders.subprocess.run",
                return_value=completed,
            ) as mock_run,
        ):
            status = builder.build(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(output_dir),
            )

        assert status.index_type == "zoekt"
        assert status.state == IndexState.FRESH
        assert status.path == str(output_dir)
        assert output_dir.exists()
        cmd = mock_run.call_args.args[0]
        assert cmd[0] == "/fake/zoekt-git-index"
        assert "-index" in cmd
        assert str(output_dir) in cmd
        assert str(tmp_path) in cmd

    def test_build_raises_when_binary_missing(self, tmp_path):
        builder = ZoektIndexBuilder(binary="this-binary-does-not-exist-12345")

        with patch(
            "codenib.compiler.index_builders.shutil.which",
            return_value=None,
        ):
            try:
                builder.build(
                    scope="current_repo",
                    repo_path=str(tmp_path),
                    output_dir=str(tmp_path / "shards"),
                )
            except RuntimeError as exc:
                assert "Zoekt binary not found" in str(exc)
            else:
                raise AssertionError("Expected RuntimeError when binary missing")

    def test_build_raises_when_subprocess_fails(self, tmp_path):
        import subprocess

        builder = ZoektIndexBuilder()
        with (
            patch(
                "codenib.compiler.index_builders.shutil.which",
                return_value="/fake/zoekt-git-index",
            ),
            patch(
                "codenib.compiler.index_builders.subprocess.run",
                side_effect=subprocess.CalledProcessError(
                    returncode=2, cmd=["zoekt-git-index"], stderr="boom"
                ),
            ),
        ):
            try:
                builder.build(
                    scope="current_repo",
                    repo_path=str(tmp_path),
                    output_dir=str(tmp_path / "shards"),
                )
            except RuntimeError as exc:
                assert "zoekt-git-index failed" in str(exc)
                assert "boom" in str(exc)
            else:
                raise AssertionError("Expected RuntimeError when subprocess fails")


# ---------------------------------------------------------------------------
# IndexCompiler
# ---------------------------------------------------------------------------


class TestIndexCompiler:
    def test_compile_repo_with_mock_builders(self, tmp_path):
        registry = IndexBuilderRegistry()
        registry.register("bm25", _mock_builder("bm25", file_count=100))
        registry.register("vector", _mock_builder("vector", file_count=200))

        config = IndexCompilerConfig(
            index_types=["bm25", "vector"],
            languages=["python"],
        )
        compiler = IndexCompiler(registry, config)
        manifest = compiler.compile_repo(
            str(tmp_path), cache_dir=str(tmp_path / "cache")
        )

        assert manifest.repo_path == str(tmp_path)
        assert "bm25" in manifest.indexes
        assert "vector" in manifest.indexes
        assert manifest.indexes["bm25"].status == "fresh"
        assert manifest.indexes["vector"].status == "fresh"
        assert manifest.capabilities["sparse_search"] is True
        assert manifest.capabilities["dense_search"] is True
        assert manifest.capabilities["hybrid_search"] is True

    def test_failed_builder_records_error(self, tmp_path):
        registry = IndexBuilderRegistry()
        registry.register("bm25", _failing_builder())

        config = IndexCompilerConfig(index_types=["bm25"])
        compiler = IndexCompiler(registry, config)
        manifest = compiler.compile_repo(
            str(tmp_path), cache_dir=str(tmp_path / "cache")
        )

        assert manifest.indexes["bm25"].status == "failed"
        assert "error" in manifest.indexes["bm25"].metadata
        assert (
            "Build failed intentionally" in manifest.indexes["bm25"].metadata["error"]
        )
        assert manifest.capabilities["sparse_search"] is False

    def test_missing_builder_skipped(self, tmp_path):
        registry = IndexBuilderRegistry()
        registry.register("bm25", _mock_builder("bm25"))
        # "vector" not registered

        config = IndexCompilerConfig(index_types=["bm25", "vector"])
        compiler = IndexCompiler(registry, config)
        manifest = compiler.compile_repo(
            str(tmp_path), cache_dir=str(tmp_path / "cache")
        )

        assert "bm25" in manifest.indexes
        assert "vector" not in manifest.indexes

    def test_manifest_file_written(self, tmp_path):
        registry = IndexBuilderRegistry()
        registry.register("bm25", _mock_builder("bm25"))

        config = IndexCompilerConfig(index_types=["bm25"])
        compiler = IndexCompiler(registry, config)
        cache_dir = str(tmp_path / "cache")
        compiler.compile_repo(str(tmp_path), cache_dir=cache_dir)

        manifest_path = os.path.join(cache_dir, MANIFEST_FILENAME)
        assert os.path.exists(manifest_path)

        with open(manifest_path) as f:
            data = json.load(f)
        assert data["version"] == MANIFEST_VERSION
        assert "bm25" in data["indexes"]
        assert is_secure_source_fingerprint_v2(data["repo"]["source_fingerprint"])
        assert data["indexes"]["bm25"]["source_fingerprint"] == (
            data["repo"]["source_fingerprint"]
        )

    def test_manifest_can_be_loaded(self, tmp_path):
        registry = IndexBuilderRegistry()
        registry.register("bm25", _mock_builder("bm25"))

        config = IndexCompilerConfig(index_types=["bm25"])
        compiler = IndexCompiler(registry, config)
        cache_dir = str(tmp_path / "cache")
        compiler.compile_repo(str(tmp_path), cache_dir=cache_dir)

        manifest_path = os.path.join(cache_dir, MANIFEST_FILENAME)
        loaded = RepoManifest.load(manifest_path)
        assert loaded.indexes["bm25"].status == "fresh"

    def test_index_types_override(self, tmp_path):
        registry = IndexBuilderRegistry()
        registry.register("bm25", _mock_builder("bm25"))
        registry.register("vector", _mock_builder("vector"))

        config = IndexCompilerConfig(index_types=["bm25", "vector"])
        compiler = IndexCompiler(registry, config)
        manifest = compiler.compile_repo(
            str(tmp_path),
            cache_dir=str(tmp_path / "cache"),
            index_types=["bm25"],  # override: only build bm25
        )

        assert "bm25" in manifest.indexes
        assert "vector" not in manifest.indexes

    def test_partial_rebuild_preserves_unrequested_views(self, tmp_path):
        registry = IndexBuilderRegistry()
        registry.register("bm25", _mock_builder("bm25"))
        registry.register("vector", _mock_builder("vector"))

        compiler = IndexCompiler(registry)
        cache_dir = str(tmp_path / "cache")
        compiler.compile_repo(str(tmp_path), cache_dir=cache_dir, index_types=["bm25"])
        manifest = compiler.compile_repo(
            str(tmp_path), cache_dir=cache_dir, index_types=["vector"]
        )

        assert set(manifest.indexes) == {"bm25", "vector"}

    def test_rebuild_does_not_keep_requested_view_fresh_without_builder(self, tmp_path):
        initial_registry = IndexBuilderRegistry()
        initial_registry.register("bm25", _mock_builder("bm25"))
        cache_dir = str(tmp_path / "cache")
        IndexCompiler(initial_registry).compile_repo(
            str(tmp_path), cache_dir=cache_dir, index_types=["bm25"]
        )

        manifest = IndexCompiler(IndexBuilderRegistry()).compile_repo(
            str(tmp_path), cache_dir=cache_dir, index_types=["bm25"]
        )

        assert manifest.indexes["bm25"].status == "failed"
        assert "No builder registered" in manifest.indexes["bm25"].metadata["error"]

    def test_compiles_for_same_cache_are_serialized(self, tmp_path):
        state_lock = threading.Lock()
        second_entered = threading.Event()
        active = 0
        max_active = 0
        entries = 0

        class TrackingBuilder:
            def build(self, scope: str, **kwargs) -> IndexStatus:
                nonlocal active, entries, max_active
                with state_lock:
                    active += 1
                    entries += 1
                    max_active = max(max_active, active)
                    if entries == 2:
                        second_entered.set()
                second_entered.wait(timeout=0.25)
                with state_lock:
                    active -= 1
                output_dir = kwargs["output_dir"]
                os.makedirs(output_dir, exist_ok=True)
                return IndexStatus(
                    index_type="bm25",
                    state=IndexState.FRESH,
                    last_built=time.time(),
                    age_seconds=0.0,
                    scope=scope,
                    path=output_dir,
                )

        registry = IndexBuilderRegistry()
        registry.register("bm25", TrackingBuilder())
        compiler = IndexCompiler(registry, IndexCompilerConfig(index_types=["bm25"]))
        cache_dir = str(tmp_path / "cache")

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(compiler.compile_repo, str(tmp_path), cache_dir=cache_dir)
                for _ in range(2)
            ]
            for future in futures:
                future.result()

        assert max_active == 1

    def test_build_duration_recorded(self, tmp_path):
        registry = IndexBuilderRegistry()
        registry.register("bm25", _mock_builder("bm25"))

        config = IndexCompilerConfig(index_types=["bm25"])
        compiler = IndexCompiler(registry, config)
        manifest = compiler.compile_repo(
            str(tmp_path), cache_dir=str(tmp_path / "cache")
        )

        meta = manifest.indexes["bm25"].metadata
        assert "build_duration_seconds" in meta
        assert meta["build_duration_seconds"] >= 0


# ---------------------------------------------------------------------------
# End-to-end: Phase 1 → Phase 2 integration
# ---------------------------------------------------------------------------


class TestTwoPhaseIntegration:
    def test_phase1_phase2_roundtrip(self, tmp_path):
        """IndexCompiler → manifest → ManifestIndexStateStore → ResourceResolver."""
        # Phase 1: build indexes
        registry = IndexBuilderRegistry()
        registry.register("bm25", _mock_builder("bm25", file_count=500))

        config = IndexCompilerConfig(index_types=["bm25"])
        compiler = IndexCompiler(registry, config)
        cache_dir = str(tmp_path / "cache")
        compiler.compile_repo(str(tmp_path), cache_dir=cache_dir)

        # Phase 2: load manifest, resolve resources
        manifest_path = os.path.join(cache_dir, MANIFEST_FILENAME)
        loaded = RepoManifest.load(manifest_path)
        state_store = ManifestIndexStateStore(loaded)
        resolver = ResourceResolver(state_store)

        plan = resolver.resolve(
            [
                IndexRequirement(index_type="bm25", max_age_seconds=3600),
            ]
        )

        assert plan.can_execute
        assert plan.decisions[0].action == "use"
        assert plan.decisions[0].state == IndexState.FRESH

    def test_phase2_detects_missing_index(self, tmp_path):
        """Phase 2 correctly blocks when a needed index wasn't built."""
        registry = IndexBuilderRegistry()
        registry.register("bm25", _mock_builder("bm25"))

        config = IndexCompilerConfig(index_types=["bm25"])
        compiler = IndexCompiler(registry, config)
        cache_dir = str(tmp_path / "cache")
        compiler.compile_repo(str(tmp_path), cache_dir=cache_dir)

        manifest_path = os.path.join(cache_dir, MANIFEST_FILENAME)
        loaded = RepoManifest.load(manifest_path)
        state_store = ManifestIndexStateStore(loaded)
        resolver = ResourceResolver(state_store)

        plan = resolver.resolve(
            [
                IndexRequirement(index_type="bm25", max_age_seconds=3600),
                IndexRequirement(index_type="vector", max_age_seconds=3600),
            ]
        )

        assert not plan.can_execute
        assert "vector" in plan.blocking_builds


# ---------------------------------------------------------------------------
# IndexCompiler.update_repo — incremental advance of an existing manifest
# ---------------------------------------------------------------------------


def _recording_builder(calls: list):
    """Builder that records whether build or incremental_update was invoked."""

    class RecordingBuilder:
        def build(self, scope: str, **kwargs) -> IndexStatus:
            output_dir = kwargs.get("output_dir", "/tmp/rec")
            os.makedirs(output_dir, exist_ok=True)
            calls.append(("build", kwargs.get("last_commit")))
            return IndexStatus(
                index_type="rec",
                state=IndexState.FRESH,
                last_built=time.time(),
                age_seconds=0.0,
                scope=scope,
                path=output_dir,
                metadata={},
            )

        def incremental_update(self, scope: str, **kwargs) -> IndexStatus:
            calls.append(("incremental_update", kwargs.get("last_commit")))
            rest = {k: v for k, v in kwargs.items() if k != "last_commit"}
            return self.build(scope, **rest)

    return RecordingBuilder()


def _git_repo(path) -> str:
    """Init a git repo with one commit; return the commit sha."""
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "a.py").write_text("def a():\n    pass\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "one"], check=True)
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _commit(path, name: str) -> str:
    (path / name).write_text("def b():\n    pass\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", name], check=True)
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


class TestUpdateRepo:
    def _compiler(self, calls):
        registry = IndexBuilderRegistry()
        registry.register("rec", _recording_builder(calls))
        return IndexCompiler(
            registry,
            IndexCompilerConfig(index_types=["rec"], languages=["python"]),
        )

    def test_falls_back_to_full_build_without_manifest(self, tmp_path):
        _git_repo(tmp_path)
        calls: list = []
        self._compiler(calls).update_repo(str(tmp_path))
        assert calls == [("build", None)]

    def test_noop_when_head_unchanged(self, tmp_path):
        _git_repo(tmp_path)
        calls: list = []
        compiler = self._compiler(calls)
        compiler.compile_repo(str(tmp_path))
        assert len(calls) == 1
        compiler.update_repo(str(tmp_path))
        # HEAD did not move, so no builder should run again.
        assert len(calls) == 1

    def test_rebuilds_when_source_changes_at_same_head(self, tmp_path):
        _git_repo(tmp_path)
        calls: list = []
        compiler = self._compiler(calls)
        first = compiler.compile_repo(str(tmp_path))
        first_source = first.source_fingerprint

        (tmp_path / "a.py").write_text("def changed():\n    return 2\n")
        updated = compiler.update_repo(str(tmp_path))

        assert calls == [("build", None), ("build", None)]
        assert updated.source_fingerprint != first_source
        assert updated.indexes["rec"].source_fingerprint == (updated.source_fingerprint)
        assert updated.indexes["rec"].status == "fresh"

    def test_dirty_new_head_uses_full_build(self, tmp_path):
        _git_repo(tmp_path)
        calls: list = []
        compiler = self._compiler(calls)
        compiler.compile_repo(str(tmp_path))
        _commit(tmp_path, "b.py")
        (tmp_path / "a.py").write_text("def dirty():\n    return 3\n")

        compiler.update_repo(str(tmp_path))

        assert calls == [("build", None), ("build", None)]

    def test_rebuilds_only_view_with_outdated_builder_identity(self, tmp_path):
        _git_repo(tmp_path)
        calls = []

        class VersionedBuilder:
            version = 1

            def artifact_identity(self):
                return {"builder_schema": self.version}

            def build(self, scope: str, **kwargs) -> IndexStatus:
                calls.append(self.version)
                return IndexStatus(
                    index_type="rec",
                    state=IndexState.FRESH,
                    last_built=time.time(),
                    age_seconds=0.0,
                    scope=scope,
                    path=kwargs["output_dir"],
                    metadata=self.artifact_identity(),
                )

            def incremental_update(self, scope: str, **kwargs) -> IndexStatus:
                return self.build(scope, **kwargs)

        builder = VersionedBuilder()
        registry = IndexBuilderRegistry()
        registry.register("rec", builder)
        compiler = IndexCompiler(
            registry,
            IndexCompilerConfig(index_types=["rec"], languages=["python"]),
        )
        compiler.compile_repo(str(tmp_path))
        builder.version = 2

        manifest = compiler.update_repo(str(tmp_path))

        assert calls == [1, 2]
        assert manifest.indexes["rec"].config["builder_schema"] == 2

    def test_builder_identity_change_at_new_head_forces_full_build(self, tmp_path):
        _git_repo(tmp_path)
        calls = []

        class VersionedBuilder:
            version = 1

            def artifact_identity(self):
                return {"builder_schema": self.version}

            def _status(self, scope: str, output_dir: str) -> IndexStatus:
                return IndexStatus(
                    index_type="rec",
                    state=IndexState.FRESH,
                    last_built=time.time(),
                    age_seconds=0.0,
                    scope=scope,
                    path=output_dir,
                    metadata=self.artifact_identity(),
                )

            def build(self, scope: str, **kwargs) -> IndexStatus:
                calls.append(("build", self.version, kwargs.get("last_commit")))
                return self._status(scope, kwargs["output_dir"])

            def incremental_update(self, scope: str, **kwargs) -> IndexStatus:
                calls.append(
                    ("incremental_update", self.version, kwargs.get("last_commit"))
                )
                return self._status(scope, kwargs["output_dir"])

        builder = VersionedBuilder()
        registry = IndexBuilderRegistry()
        registry.register("rec", builder)
        compiler = IndexCompiler(
            registry,
            IndexCompilerConfig(index_types=["rec"], languages=["python"]),
        )
        compiler.compile_repo(str(tmp_path))
        _commit(tmp_path, "b.py")
        builder.version = 2

        manifest = compiler.update_repo(str(tmp_path))

        assert calls == [("build", 1, None), ("build", 2, None)]
        assert manifest.indexes["rec"].config["builder_schema"] == 2

    def test_clean_new_head_uses_authority_safe_full_rebuild(self, tmp_path):
        first = _git_repo(tmp_path)
        calls: list = []
        compiler = self._compiler(calls)
        compiler.compile_repo(str(tmp_path))
        second = _commit(tmp_path, "b.py")
        assert first != second

        compiler.update_repo(str(tmp_path))
        assert calls == [("build", None), ("build", None)]

    def test_path_status_a_b_a_cannot_authorize_incremental_reuse(
        self,
        tmp_path,
        monkeypatch,
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_repo(repo)
        calls: list = []
        compiler = self._compiler(calls)
        compiler.compile_repo(str(repo))
        _commit(repo, "b.py")

        clean_replacement = tmp_path / "clean-replacement"
        shutil.copytree(repo, clean_replacement, symlinks=True)
        (repo / "a.py").write_text("def dirty_a():\n    return 9\n")
        parked = tmp_path / "parked-a"
        status_calls = []

        def observe_temporary_clean_replacement(*args, **kwargs):
            status_calls.append((args, kwargs))
            os.rename(repo, parked)
            os.rename(clean_replacement, repo)
            try:
                return False
            finally:
                os.rename(repo, clean_replacement)
                os.rename(parked, repo)

        monkeypatch.setattr(
            index_compiler_module,
            "repository_source_is_dirty",
            observe_temporary_clean_replacement,
            raising=False,
        )

        compiler.update_repo(str(repo))

        assert status_calls == []
        assert calls == [("build", None), ("build", None)]

    def test_v1_manifest_forces_full_rebuild_instead_of_incremental_update(
        self,
        tmp_path,
    ):
        _git_repo(tmp_path)
        calls: list = []
        compiler = self._compiler(calls)
        manifest = compiler.compile_repo(str(tmp_path))
        legacy = f"sha256:{'a' * 64}"
        manifest.source_fingerprint = legacy
        manifest.last_indexed_source_fingerprint = legacy
        manifest.indexes["rec"].source_fingerprint = legacy
        manifest.save(tmp_path / ".codenib_cache" / MANIFEST_FILENAME)
        _commit(tmp_path, "b.py")
        calls.clear()

        rebuilt = compiler.update_repo(str(tmp_path))

        assert calls == [("build", None)]
        assert rebuilt.source_fingerprint.startswith("sha256-v2:")
        assert rebuilt.indexes["rec"].source_fingerprint == (rebuilt.source_fingerprint)

    def test_full_rebuild_does_not_receive_previous_manifest_config(self, tmp_path):
        _git_repo(tmp_path)
        observed = []

        class ManifestAwareBuilder:
            def artifact_identity(self):
                return {"builder_schema": 7}

            def _status(self, scope, output_dir):
                return IndexStatus(
                    index_type="rec",
                    state=IndexState.FRESH,
                    scope=scope,
                    path=output_dir,
                    metadata={
                        **self.artifact_identity(),
                        "generation": "recorded",
                    },
                )

            def build(self, scope: str, **kwargs) -> IndexStatus:
                return self._status(scope, kwargs["output_dir"])

            def incremental_update(self, scope: str, **kwargs) -> IndexStatus:
                observed.append(kwargs["previous_artifact_config"])
                return self._status(scope, kwargs["output_dir"])

        registry = IndexBuilderRegistry()
        registry.register("rec", ManifestAwareBuilder())
        compiler = IndexCompiler(
            registry,
            IndexCompilerConfig(index_types=["rec"], languages=["python"]),
        )
        compiler.compile_repo(str(tmp_path))
        _commit(tmp_path, "b.py")

        compiler.update_repo(str(tmp_path))

        assert observed == []

    def test_full_graph_rebuild_recomputes_partial_language_coverage(self, tmp_path):
        _git_repo(tmp_path)
        calls = []

        class PartialGraphBuilder:
            def artifact_identity(self):
                return {"builder_schema": 1, "languages": ["python", "cpp"]}

            def _status(self, scope: str, output_dir: str, metadata) -> IndexStatus:
                return IndexStatus(
                    index_type="symbol_graph",
                    state=IndexState.FRESH,
                    last_built=time.time(),
                    age_seconds=0.0,
                    scope=scope,
                    path=output_dir,
                    metadata={**self.artifact_identity(), **metadata},
                )

            def build(self, scope: str, **kwargs) -> IndexStatus:
                calls.append(("build", None))
                return self._status(
                    scope,
                    kwargs["output_dir"],
                    {
                        "available_languages": ["python"],
                        "failed_languages": {"cpp": "compile database unavailable"},
                        "partial": True,
                    },
                )

            def incremental_update(self, scope: str, **kwargs) -> IndexStatus:
                calls.append(("incremental_update", kwargs["last_commit"]))
                return self._status(
                    scope,
                    kwargs["output_dir"],
                    {"update_mode": "incremental"},
                )

        registry = IndexBuilderRegistry()
        registry.register("symbol_graph", PartialGraphBuilder())
        compiler = IndexCompiler(
            registry,
            IndexCompilerConfig(
                index_types=["symbol_graph"],
                languages=["python", "cpp"],
            ),
        )
        compiler.compile_repo(str(tmp_path))
        _commit(tmp_path, "notes.md")

        manifest = compiler.update_repo(str(tmp_path))
        metadata = manifest.indexes["symbol_graph"].metadata

        assert calls == [("build", None), ("build", None)]
        assert metadata["available_languages"] == ["python"]
        assert metadata["failed_languages"] == {"cpp": "compile database unavailable"}
        assert metadata["partial"] is True

    def test_manifest_records_new_commit_after_update(self, tmp_path):
        _git_repo(tmp_path)
        calls: list = []
        compiler = self._compiler(calls)
        compiler.compile_repo(str(tmp_path))
        second = _commit(tmp_path, "b.py")

        manifest = compiler.update_repo(str(tmp_path))
        assert manifest.commit == second
        assert manifest.last_indexed_commit == second

    def test_rebuilds_when_manifest_unreadable(self, tmp_path):
        _git_repo(tmp_path)
        calls: list = []
        compiler = self._compiler(calls)
        compiler.compile_repo(str(tmp_path))
        cache = tmp_path / ".codenib_cache"
        (cache / "repo_manifest.json").write_text("{not json")

        calls.clear()
        compiler.update_repo(str(tmp_path))
        assert calls == [("build", None)]

    def test_failed_build_does_not_advance_indexed_commit(self, tmp_path):
        """A failed index must leave last_indexed_commit behind.

        Otherwise the next update_repo() sees HEAD == last_indexed_commit,
        reports "nothing to update", and the broken index stays stale forever.
        """
        first = _git_repo(tmp_path)
        calls: list = []
        compiler = self._compiler(calls)
        compiler.compile_repo(str(tmp_path))
        second = _commit(tmp_path, "b.py")
        assert first != second

        builder = compiler._builders.get("rec")

        def boom(scope, **kwargs):
            raise RuntimeError("builder exploded")

        builder.build = boom

        manifest = compiler.update_repo(str(tmp_path))
        assert manifest.commit == second
        # HEAD moved, but nothing successfully indexed it.
        assert manifest.last_indexed_commit == first
        assert manifest.indexes["rec"].status == "failed"

    def test_failed_build_leaves_repo_updatable(self, tmp_path):
        """The retry after a failure must reach the builder, not short-circuit."""
        _git_repo(tmp_path)
        calls: list = []
        compiler = self._compiler(calls)
        compiler.compile_repo(str(tmp_path))
        _commit(tmp_path, "b.py")

        builder = compiler._builders.get("rec")
        healthy = builder.build

        def boom(scope, **kwargs):
            raise RuntimeError("builder exploded")

        builder.build = boom
        compiler.update_repo(str(tmp_path))

        builder.build = healthy
        calls.clear()
        compiler.update_repo(str(tmp_path))
        # Not a no-op: the failure left work to do.
        assert calls, "update_repo short-circuited after a failed build"

    def test_failed_initial_build_retries_at_unchanged_head(self, tmp_path):
        """An empty baseline after a failed full build must not alias HEAD."""
        head = _git_repo(tmp_path)
        attempts = []

        class FailOnceBuilder:
            def build(self, scope: str, **kwargs) -> IndexStatus:
                attempts.append("build")
                if len(attempts) == 1:
                    raise RuntimeError("transient build failure")
                return _mock_builder("rec").build(scope, **kwargs)

            def incremental_update(self, scope: str, **kwargs) -> IndexStatus:
                attempts.append("incremental_update")
                return _mock_builder("rec").build(scope, **kwargs)

        registry = IndexBuilderRegistry()
        registry.register("rec", FailOnceBuilder())
        compiler = IndexCompiler(
            registry,
            IndexCompilerConfig(index_types=["rec"], languages=["python"]),
        )

        failed = compiler.compile_repo(str(tmp_path))
        assert failed.commit == head
        assert failed.last_indexed_commit == ""
        assert failed.indexes["rec"].status == "failed"

        recovered = compiler.update_repo(str(tmp_path))
        assert attempts == ["build", "build"]
        assert recovered.last_indexed_commit == head
        assert recovered.indexes["rec"].status == "fresh"

    def test_partial_build_preserves_other_fresh_views(self, tmp_path):
        head = _git_repo(tmp_path)
        registry = IndexBuilderRegistry()
        registry.register("bm25", _mock_builder("bm25"))
        registry.register("symbol_graph", _mock_builder("symbol_graph"))
        compiler = IndexCompiler(
            registry,
            IndexCompilerConfig(
                index_types=["bm25", "symbol_graph"],
                languages=["python"],
            ),
        )

        compiler.compile_repo(str(tmp_path), index_types=["bm25"])
        manifest = compiler.update_repo(str(tmp_path), index_types=["symbol_graph"])

        assert set(manifest.indexes) == {"bm25", "symbol_graph"}
        assert manifest.indexes["bm25"].status == "fresh"
        assert manifest.indexes["symbol_graph"].status == "fresh"
        assert manifest.indexes["bm25"].commit == head
        assert manifest.indexes["symbol_graph"].commit == head
        assert manifest.capabilities["sparse_search"] is True
        assert manifest.capabilities["symbol_navigation"] is True

    def test_partial_update_marks_unrequested_old_view_stale(self, tmp_path):
        first = _git_repo(tmp_path)
        registry = IndexBuilderRegistry()
        registry.register("bm25", _mock_builder("bm25"))
        registry.register("symbol_graph", _mock_builder("symbol_graph"))
        compiler = IndexCompiler(
            registry,
            IndexCompilerConfig(
                index_types=["bm25", "symbol_graph"],
                languages=["python"],
            ),
        )
        compiler.compile_repo(str(tmp_path))
        second = _commit(tmp_path, "b.py")

        manifest = compiler.update_repo(str(tmp_path), index_types=["bm25"])

        assert manifest.indexes["bm25"].status == "fresh"
        assert manifest.indexes["bm25"].commit == second
        assert manifest.indexes["symbol_graph"].status == "stale"
        assert manifest.indexes["symbol_graph"].commit == first
        assert manifest.capabilities["sparse_search"] is True
        assert manifest.capabilities["symbol_navigation"] is False
        assert manifest.last_indexed_commit == first

    def test_partial_update_marks_same_head_old_source_view_stale(self, tmp_path):
        _git_repo(tmp_path)
        registry = IndexBuilderRegistry()
        registry.register("bm25", _mock_builder("bm25"))
        registry.register("symbol_graph", _mock_builder("symbol_graph"))
        compiler = IndexCompiler(
            registry,
            IndexCompilerConfig(
                index_types=["bm25", "symbol_graph"],
                languages=["python"],
            ),
        )
        initial = compiler.compile_repo(str(tmp_path))
        old_source = initial.source_fingerprint
        (tmp_path / "a.py").write_text("def changed():\n    return 4\n")

        manifest = compiler.update_repo(str(tmp_path), index_types=["bm25"])

        assert manifest.source_fingerprint != old_source
        assert manifest.indexes["bm25"].status == "fresh"
        assert manifest.indexes["symbol_graph"].status == "stale"
        assert manifest.capabilities["sparse_search"] is True
        assert manifest.capabilities["symbol_navigation"] is False

    def test_source_change_during_build_never_publishes_fresh_view(self, tmp_path):
        _git_repo(tmp_path)

        class MutatingBuilder:
            def build(self, scope: str, **kwargs) -> IndexStatus:
                (tmp_path / "late.py").write_text("VALUE = 1\n")
                return _mock_builder("bm25").build(scope, **kwargs)

            def incremental_update(self, scope: str, **kwargs) -> IndexStatus:
                return self.build(scope, **kwargs)

        registry = IndexBuilderRegistry()
        registry.register("bm25", MutatingBuilder())
        compiler = IndexCompiler(
            registry,
            IndexCompilerConfig(index_types=["bm25"], languages=["python"]),
        )

        manifest = compiler.compile_repo(str(tmp_path))

        assert manifest.indexes["bm25"].status == "stale"
        assert "changed during index compilation" in (
            manifest.indexes["bm25"].metadata["stale_reason"]
        )
        assert manifest.capabilities["sparse_search"] is False


# ---------------------------------------------------------------------------
# SymbolGraphBuilder incremental repair + admission control
# ---------------------------------------------------------------------------


class TestSymbolGraphIncremental:
    def _builder(self, **kw):
        from codenib.compiler.index_builders import SymbolGraphBuilder

        return SymbolGraphBuilder(languages=["python"], **kw)

    # -- blocker logic (pure, no LSP) ------------------------------------

    def test_blocked_without_last_commit(self, tmp_path):
        reason = self._builder()._incremental_blocker(str(tmp_path), str(tmp_path), "")
        assert reason and "no previously indexed commit" in reason

    def test_blocked_without_existing_graph(self, tmp_path):
        _git_repo(tmp_path)
        reason = self._builder()._incremental_blocker(
            str(tmp_path), str(tmp_path / "out"), "abc123"
        )
        assert reason and "graph.pkl" in reason

    def test_blocked_when_already_at_indexed_commit(self, tmp_path):
        head = _git_repo(tmp_path)
        out = tmp_path / "out"
        out.mkdir()
        (out / "graph.pkl").write_bytes(b"x")
        reason = self._builder()._incremental_blocker(str(tmp_path), str(out), head)
        assert reason and "already at the indexed commit" in reason

    def test_blocked_when_worktree_dirty(self, tmp_path):
        first = _git_repo(tmp_path)
        _commit(tmp_path, "b.py")
        out = tmp_path / "out"
        out.mkdir()
        (out / "graph.pkl").write_bytes(b"x")
        # Modify a *tracked* file: its disk content no longer matches the
        # commit, which is the case the patcher cannot reason about.
        (tmp_path / "a.py").write_text("def a():\n    return 'changed'\n")
        reason = self._builder()._incremental_blocker(str(tmp_path), str(out), first)
        assert reason and "uncommitted" in reason

    def test_untracked_files_do_not_block(self, tmp_path):
        """The cache dir lives inside the repo; untracked paths must not block."""
        first = _git_repo(tmp_path)
        _commit(tmp_path, "b.py")
        out = tmp_path / ".codenib_cache" / "symbol_graph"
        out.mkdir(parents=True)
        (out / "graph.pkl").write_bytes(b"x")
        assert (
            self._builder()._incremental_blocker(str(tmp_path), str(out), first) is None
        )

    def test_not_blocked_when_clean_and_moved(self, tmp_path):
        first = _git_repo(tmp_path)
        _commit(tmp_path, "b.py")
        out = tmp_path / "out"
        out.mkdir()
        (out / "graph.pkl").write_bytes(b"x")
        assert (
            self._builder()._incremental_blocker(str(tmp_path), str(out), first) is None
        )

    # -- fallback + admission -------------------------------------------

    def test_falls_back_to_build_when_blocked(self, tmp_path, monkeypatch):
        built = []
        builder = self._builder()
        monkeypatch.setattr(
            builder, "build", lambda scope, **kw: built.append(kw) or "BUILT"
        )
        # No last_commit => blocked => build()
        assert (
            builder.incremental_update(
                "current_repo", repo_path=str(tmp_path), output_dir=str(tmp_path)
            )
            == "BUILT"
        )
        assert len(built) == 1
        # last_commit must not leak into build()'s kwargs
        assert "last_commit" not in built[0]

    def test_source_coverage_fallback_rebuilds_instead_of_patching(
        self, tmp_path, monkeypatch
    ):
        built = []
        builder = self._builder(source_coverage_fallback=True)
        monkeypatch.setattr(
            builder, "build", lambda scope, **kw: built.append(kw) or "BUILT"
        )
        monkeypatch.setattr(
            builder,
            "_patch_graph",
            lambda *_args, **_kwargs: pytest.fail("fallback graph must not be patched"),
        )

        result = builder.incremental_update(
            "current_repo",
            repo_path=str(tmp_path),
            output_dir=str(tmp_path / "out"),
            last_commit="a" * 40,
        )

        assert result == "BUILT"
        assert len(built) == 1
        assert "last_commit" not in built[0]

    def test_unverified_update_is_discarded_and_rebuilt(self, tmp_path, monkeypatch):
        """Default NullVerifier proves nothing, so the patch must be dropped."""
        builder = self._builder()  # require_verification=True by default
        monkeypatch.setattr(builder, "_patch_graph", lambda *a, **k: None)
        monkeypatch.setattr(builder, "build", lambda scope, **kw: "BUILT")
        first = _git_repo(tmp_path)
        _commit(tmp_path, "b.py")
        out = tmp_path / "out"
        out.mkdir()
        (out / "graph.pkl").write_bytes(b"x")

        result = builder.incremental_update(
            "current_repo",
            repo_path=str(tmp_path),
            output_dir=str(out),
            last_commit=first,
        )
        assert result == "BUILT"

    def test_patch_error_falls_back_to_build(self, tmp_path, monkeypatch):
        builder = self._builder()

        def boom(*a, **k):
            raise RuntimeError("lsp exploded")

        monkeypatch.setattr(builder, "_patch_graph", boom)
        monkeypatch.setattr(builder, "build", lambda scope, **kw: "BUILT")
        first = _git_repo(tmp_path)
        _commit(tmp_path, "b.py")
        out = tmp_path / "out"
        out.mkdir()
        (out / "graph.pkl").write_bytes(b"x")

        assert (
            builder.incremental_update(
                "current_repo",
                repo_path=str(tmp_path),
                output_dir=str(out),
                last_commit=first,
            )
            == "BUILT"
        )

    def test_admitted_update_is_kept(self, tmp_path, monkeypatch):
        """An explicitly-admitting verifier keeps the patched result."""
        from codenib.compiler.verification import AlwaysAdmitVerifier

        builder = self._builder(verifier=AlwaysAdmitVerifier())
        sentinel = object()
        monkeypatch.setattr(builder, "_patch_graph", lambda *a, **k: sentinel)
        monkeypatch.setattr(
            builder, "build", lambda scope, **kw: pytest.fail("should not rebuild")
        )
        first = _git_repo(tmp_path)
        _commit(tmp_path, "b.py")
        out = tmp_path / "out"
        out.mkdir()
        (out / "graph.pkl").write_bytes(b"x")

        assert (
            builder.incremental_update(
                "current_repo",
                repo_path=str(tmp_path),
                output_dir=str(out),
                last_commit=first,
            )
            is sentinel
        )

    def test_empty_relevant_diff_is_admitted_without_verifier(
        self, tmp_path, monkeypatch
    ):
        """A docs-only transition leaves the configured graph unchanged."""
        from codenib.graph.code_graph import CodeGraph
        from codenib.graph.incremental.graph_patcher import GraphPatcher

        first = _git_repo(tmp_path)
        _commit(tmp_path, "README.md")
        out = tmp_path / "out"
        out.mkdir()

        graph = MagicMock()
        graph.graph.vs = []
        monkeypatch.setattr(CodeGraph, "load_graph", lambda path: graph)
        monkeypatch.setattr(
            GraphPatcher,
            "detect_changed_files",
            lambda *args, **kwargs: {
                "added": [],
                "modified": [],
                "deleted": [],
                "renamed": [],
            },
        )

        status = self._builder()._patch_graph(
            "current_repo", str(tmp_path), str(out), first
        )

        assert status is not None
        assert status.metadata["changed_files"] == 0
        assert status.metadata["verified"] is True
        assert status.metadata["verification_details"] == {
            "method": "empty-relevant-diff"
        }
        assert "scip_decoded_artifacts" not in status.metadata
        graph.save_graph.assert_called_once_with(str(out / "graph.pkl"))

    def test_contract_rebuild_is_requested_before_lsp_start(
        self, tmp_path, monkeypatch
    ):
        from codenib.compiler.index_builders import SymbolGraphBuilder
        from codenib.graph.code_graph import CodeGraph
        from codenib.graph.incremental.graph_patcher import GraphPatcher
        from codenib.graph.incremental.patcher_base import (
            IncrementalPatchRebuildRequired,
        )

        first = _git_repo(tmp_path)
        _commit(tmp_path, "b.ts")
        out = tmp_path / "out"
        out.mkdir()
        graph = MagicMock()
        monkeypatch.setattr(CodeGraph, "load_graph", lambda _path: graph)
        monkeypatch.setattr(
            GraphPatcher,
            "detect_changed_files",
            lambda *args, **kwargs: {
                "added": [],
                "modified": ["b.ts"],
                "deleted": [],
                "renamed": [],
            },
        )
        monkeypatch.setattr(
            GraphPatcher,
            "start_lsp",
            lambda _self: pytest.fail("compiler started LSP before contract guard"),
        )
        monkeypatch.setattr(GraphPatcher, "stop_lsp", lambda _self: None)

        def require_rebuild(*_args, **_kwargs):
            raise IncrementalPatchRebuildRequired("imports changed")

        monkeypatch.setattr(
            GraphPatcher,
            "patch_files",
            require_rebuild,
        )

        status = SymbolGraphBuilder(languages=["ts"])._patch_graph(
            "current_repo", str(tmp_path), str(out), first
        )

        assert status is None
        graph.save_graph.assert_not_called()
