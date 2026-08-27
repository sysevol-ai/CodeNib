# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for concrete index builders and the IndexCompiler."""

from __future__ import annotations

import copy
import hashlib
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

import faiss
import numpy as np
import pytest

import codenib.compiler.index_compiler as index_compiler_module
from codenib._atomic_directory import (
    capture_directory_ownership,
    directory_ownership_digest,
    directory_ownership_root_identity,
)
from codenib._bounded_json import canonical_json_array_chunks
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
    LEGACY_MANIFEST_VERSION,
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
from codenib.index.embedding.artifact_integrity import (
    VECTOR_PERSISTENCE_SCHEMA,
    VECTOR_ROW_MAPPING_CONTRACT,
    VECTOR_VIEW_UPDATE_MARKER,
    vector_level_artifact_records,
)
from codenib.index.embedding.model_policy import (
    DEFAULT_EMBEDDING_REVISION,
    resolve_embedding_artifact_load_policy_from_options,
)
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
from codenib.repository_source_selection import RepositorySourceSelection
from codenib.source_fingerprint import (
    RepositoryChangedError,
    RepositorySourceBinding,
    capture_repository_source,
    is_secure_source_fingerprint_v2,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_fake_graph_artifact(output_dir: str | Path) -> Path:
    path = Path(output_dir) / "graph.pkl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"test-symbol-graph\n")
    return path


def _minimal_code_graph(
    repo_path: str = "/fake/repo",
    *,
    file_count: int = 1,
):
    from codenib.graph.code_graph import CodeGraph

    graph = CodeGraph(repo_path)
    graph.add_root_node(".")
    for index in range(file_count):
        graph.add_file_node(f"src/file_{index}.py")
    graph.build_range_indexes()
    return graph


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
            selection = kwargs.get("source_selection", RepositorySourceSelection())
            return IndexStatus(
                index_type=index_type,
                state=IndexState.FRESH,
                last_built=time.time(),
                age_seconds=0.0,
                scope=scope,
                path=output_dir,
                metadata={
                    "file_count": file_count,
                    "source_selection_digest": selection.digest,
                },
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

        selection = RepositorySourceSelection(["src/private"])
        builder = BM25IndexBuilder(
            languages=["python"],
            max_k=64,
            source_selection=selection,
        )
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
        assert status.metadata["source_selection_digest"] == selection.digest
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
        repo_config = MockChunker.call_args.kwargs["repo_config"]
        assert repo_config.source_selection == selection
        assert repo_config.source_selection is not selection
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

    @patch("codenib.index.sparse_idx.bm25_index.BM25CodeIndexer")
    @patch("codenib.code_chunker.CodeChunker")
    def test_build_rejects_chunks_outside_source_selection(
        self,
        MockChunker,
        MockIndexer,
        tmp_path,
    ):
        MockChunker.return_value.chunk_repository.return_value = [
            SimpleNamespace(file="src/private/secret.py")
        ]
        builder = BM25IndexBuilder(
            source_selection=RepositorySourceSelection(["src/private"]),
        )

        with pytest.raises(RuntimeError, match="BM25 chunks leaked excluded"):
            builder.build(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(tmp_path / "bm25"),
            )

        MockIndexer.assert_not_called()

    @patch("codenib.index.sparse_idx.bm25_index.BM25CodeIndexer")
    @patch("codenib.code_chunker.CodeChunker")
    def test_build_rejects_chunks_ignored_by_default_policy(
        self,
        MockChunker,
        MockIndexer,
        tmp_path,
    ):
        MockChunker.return_value.chunk_repository.return_value = [
            SimpleNamespace(file="node_modules/dependency.js")
        ]

        with pytest.raises(RuntimeError, match="BM25 chunks leaked excluded"):
            BM25IndexBuilder().build(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(tmp_path / "bm25"),
            )

        MockIndexer.assert_not_called()

    @patch("codenib.index.sparse_idx.bm25_index.BM25CodeIndexer")
    @patch("codenib.code_chunker.CodeChunker")
    def test_build_rejects_documents_outside_source_selection(
        self,
        MockChunker,
        MockIndexer,
        tmp_path,
    ):
        MockChunker.return_value.chunk_repository.return_value = [
            SimpleNamespace(file="src/public.py")
        ]
        MockIndexer.return_value.documents = [
            SimpleNamespace(metadata={"file": "src/private/secret.py"})
        ]
        builder = BM25IndexBuilder(
            source_selection=RepositorySourceSelection(["src/private"]),
        )

        with pytest.raises(RuntimeError, match="BM25 documents leaked excluded"):
            builder.build(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(tmp_path / "bm25"),
            )

        MockIndexer.return_value.save_index.assert_not_called()

    def test_builder_snapshots_selection_and_rejects_non_exact_types(self):
        selection = RepositorySourceSelection(["src/private"])
        builder = BM25IndexBuilder(source_selection=selection)
        expected_digest = selection.digest

        object.__setattr__(selection, "exclude_subtrees", ("src/changed",))

        assert builder.source_selection is not selection
        assert builder.artifact_identity()["source_selection_digest"] == expected_digest
        with pytest.raises(TypeError, match="RepositorySourceSelection"):
            BM25IndexBuilder(source_selection=object())
        with pytest.raises(TypeError, match="RepositorySourceSelection"):
            VectorIndexBuilder(source_selection=object())

    @patch("codenib.code_chunker.CodeChunker")
    def test_build_rejects_runtime_selection_mismatch_before_reading_source(
        self,
        MockChunker,
        tmp_path,
    ):
        builder = BM25IndexBuilder()

        with pytest.raises(RuntimeError, match="does not match"):
            builder.build(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(tmp_path / "bm25"),
                source_selection=RepositorySourceSelection(("generated",)),
            )

        MockChunker.assert_not_called()
        assert not (tmp_path / "bm25").exists()

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

    def test_build_from_repository_source_uses_retained_bytes(
        self,
        tmp_path,
        monkeypatch,
    ):
        from codenib.code_chunker import CodeChunker
        from codenib.index.sparse_idx.bm25_index import BM25CodeIndexer

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "settings.py").write_bytes(b"UNIQUE_RETAINED_MODE = 'safe_source'\r\n")
        private_root = tmp_path / "private"
        private_root.mkdir()
        output = private_root / "bm25"
        monkeypatch.setattr(
            CodeChunker,
            "chunk_repository",
            lambda *_args, **_kwargs: pytest.fail(
                "retained BM25 build used path discovery"
            ),
        )

        with capture_repository_source(repo) as source:
            status = BM25IndexBuilder(
                languages=["python"]
            ).build_from_repository_source(
                "current_repo",
                repository_source=source,
                output_dir=str(output),
            )

        assert status.state is IndexState.FRESH
        assert status.path == str(output)
        assert status.metadata["source_file_count"] == 1
        assert status.metadata["source_selection_digest"] == (
            RepositorySourceSelection().digest
        )
        index = BM25CodeIndexer()
        index.load_index(str(output))
        results = index.search("safe_source", top_k=1)
        assert results
        assert results[0].file == "settings.py"

    def test_build_from_repository_source_requires_a_missing_private_output(
        self,
        tmp_path,
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("def app():\n    return 1\n", encoding="utf-8")
        output = tmp_path / "private" / "bm25"
        output.mkdir(parents=True)
        marker = output / "owned.txt"
        marker.write_text("keep", encoding="utf-8")

        with capture_repository_source(repo) as source:
            with pytest.raises(FileExistsError, match="missing private generation"):
                BM25IndexBuilder().build_from_repository_source(
                    "current_repo",
                    repository_source=source,
                    output_dir=str(output),
                )

        assert marker.read_text(encoding="utf-8") == "keep"

    def test_build_from_repository_source_rejects_output_symlinked_into_repository(
        self,
        tmp_path,
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("def app():\n    return 1\n", encoding="utf-8")
        alias = tmp_path / "repository-alias"
        alias.symlink_to(repo, target_is_directory=True)
        output = alias / "bm25"

        with capture_repository_source(repo) as source:
            with pytest.raises(ValueError, match="inside the repository"):
                BM25IndexBuilder().build_from_repository_source(
                    "current_repo",
                    repository_source=source,
                    output_dir=str(output),
                )

        assert not output.exists()

    def test_build_from_repository_source_stops_before_output_mutation(
        self,
        tmp_path,
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("def app():\n    return 1\n", encoding="utf-8")
        private_root = tmp_path / "private"
        private_root.mkdir()
        output = private_root / "bm25"

        def stop() -> None:
            raise RuntimeError("stop requested")

        with capture_repository_source(repo) as source:
            with pytest.raises(RuntimeError, match="stop requested"):
                BM25IndexBuilder().build_from_repository_source(
                    "current_repo",
                    repository_source=source,
                    output_dir=str(output),
                    check_cancelled=stop,
                )

        assert not output.exists()

    def test_build_from_repository_source_threads_cancellation_into_identity(
        self,
        tmp_path,
        monkeypatch,
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("def app():\n    return 1\n", encoding="utf-8")
        private_root = tmp_path / "private"
        private_root.mkdir()
        output = private_root / "bm25"
        real_snapshot = RepositorySourceBinding.authenticated_identity_snapshot
        observed = []

        def tracked_snapshot(binding, *args, **kwargs):
            observed.append(kwargs.get("check_cancelled"))
            return real_snapshot(binding, *args, **kwargs)

        monkeypatch.setattr(
            RepositorySourceBinding,
            "authenticated_identity_snapshot",
            tracked_snapshot,
        )

        def check_cancelled() -> None:
            return None

        with capture_repository_source(repo) as source:
            BM25IndexBuilder().build_from_repository_source(
                "current_repo",
                repository_source=source,
                output_dir=str(output),
                check_cancelled=check_cancelled,
            )

        assert observed == [check_cancelled, check_cancelled]

    def test_build_from_repository_source_stops_during_persistence(
        self,
        tmp_path,
        monkeypatch,
    ):
        import codenib.compiler.artifact_fingerprints as fingerprints
        from codenib.index.sparse_idx.bm25_index import BM25CodeIndexer

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text(
            "def app():\n    return 1\n",
            encoding="utf-8",
        )
        private_root = tmp_path / "private"
        private_root.mkdir()
        output = private_root / "bm25"
        stopped = KeyboardInterrupt("stop during BM25 persistence")
        armed = False
        real_save = BM25CodeIndexer.save_index

        def check_cancelled() -> None:
            if armed:
                raise stopped

        def arm_before_save(indexer, directory, **kwargs):
            nonlocal armed
            armed = True
            return real_save(indexer, directory, **kwargs)

        monkeypatch.setattr(BM25CodeIndexer, "save_index", arm_before_save)
        monkeypatch.setattr(
            fingerprints,
            "bm25_artifact_file_fingerprints",
            lambda *_args, **_kwargs: pytest.fail(
                "artifact fingerprinting ran after cancellation"
            ),
        )

        with capture_repository_source(repo) as source:
            with pytest.raises(BaseException) as raised:
                BM25IndexBuilder().build_from_repository_source(
                    "current_repo",
                    repository_source=source,
                    output_dir=str(output),
                    check_cancelled=check_cancelled,
                )
            assert raised.value is stopped
            assert source.usable

        assert output.is_dir()
        assert not (output / "documents.json").exists()
        assert not (output / "bm25_metadata.json").exists()

    def test_build_from_repository_source_rejects_changed_authority(
        self,
        tmp_path,
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        path = repo / "app.py"
        path.write_text("def app():\n    return 1\n", encoding="utf-8")
        private_root = tmp_path / "private"
        private_root.mkdir()
        output = private_root / "bm25"

        with capture_repository_source(repo) as source:
            path.write_text("def changed():\n    return 2\n", encoding="utf-8")
            with pytest.raises(RepositoryChangedError):
                BM25IndexBuilder().build_from_repository_source(
                    "current_repo",
                    repository_source=source,
                    output_dir=str(output),
                )

        assert not output.exists()


# ---------------------------------------------------------------------------
# VectorIndexBuilder
# ---------------------------------------------------------------------------


def _write_mock_vector_generation(
    root: Path,
    model_suffix: str,
    *,
    artifact_metadata: dict | None = None,
    l0_count: int = 0,
    l2_count: int = 1,
    dimension: int = 384,
    index_metric: str = "ip",
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if artifact_metadata is None:
        (root / f"config_{model_suffix}.json").write_text(
            '{"level_artifacts": {}}',
            encoding="utf-8",
        )
        level = root / "l2"
        level.mkdir()
        (level / f"config_{model_suffix}.json").write_text("{}", encoding="utf-8")
        (level / f"index_{model_suffix}.faiss").write_bytes(b"new-vector")
        (level / f"documents_{model_suffix}.pkl").write_bytes(b"new-documents")
        return

    level_artifacts = {}
    embedding_model = artifact_metadata["embedding_model"]
    embedding_provider = artifact_metadata["embedding_provider"]
    embedding_revision = (
        resolve_embedding_artifact_load_policy_from_options(
            embedding_model,
            artifact_metadata["embedding_kwargs"],
        ).revision
        if embedding_provider == "huggingface"
        else None
    )
    for level, count in (("l0", l0_count), ("l2", l2_count)):
        if count == 0:
            continue
        level_path = root / level
        level_path.mkdir()
        (level_path / f"config_{model_suffix}.json").write_text(
            json.dumps(
                {
                    "embedding_model": embedding_model,
                    "embedding_provider": embedding_provider,
                    "embedding_revision": embedding_revision,
                    "dimension": dimension,
                    "index_type": "flat",
                    "index_metric": index_metric,
                    "level": level,
                    "num_documents": count,
                }
            ),
            encoding="utf-8",
        )
        if index_metric == "ip":
            index = faiss.IndexFlatIP(dimension)
        else:
            index = faiss.IndexFlatL2(dimension)
        vectors = np.zeros((count, dimension), dtype=np.float32)
        vectors[:, 0] = np.arange(1, count + 1, dtype=np.float32)
        index.add(vectors)
        faiss.write_index(
            index,
            str(level_path / f"index_{model_suffix}.faiss"),
        )
        documents_file = f"documents_{model_suffix}.json"

        def documents(
            count=count,
            level=level,
        ):
            for index in range(count):
                content = f"{level}-document-{index}"
                yield {
                    "page_content": content,
                    "metadata": {
                        "chunk_id": index,
                        "chunk_type": "function",
                        "name": f"document_{index}",
                        "file": f"source_{index}.py",
                        "start_line": index,
                        "end_line": index,
                        "node_id": f"source_{index}.py::document_{index}",
                        "level": level,
                        "content_hash": hashlib.md5(  # noqa: S324
                            content.encode("utf-8"),
                            usedforsecurity=False,
                        ).hexdigest(),
                    },
                }

        (level_path / documents_file).write_bytes(
            b"".join(canonical_json_array_chunks(documents()))
        )
        level_artifacts[level] = vector_level_artifact_records(
            level_path,
            model_suffix,
            documents_file=documents_file,
        )

    (root / f"config_{model_suffix}.json").write_text(
        json.dumps(
            {
                "artifact": artifact_metadata,
                "dimension": dimension,
                "embedding_model": artifact_metadata["embedding_model"],
                "embedding_provider": artifact_metadata["embedding_provider"],
                "embedding_revision": embedding_revision,
                "index_metric": index_metric,
                "index_type": "flat",
                "l0_documents": l0_count,
                "l2_documents": l2_count,
                "level_artifacts": level_artifacts,
                "persistence_schema": VECTOR_PERSISTENCE_SCHEMA,
                "row_mapping": VECTOR_ROW_MAPPING_CONTRACT,
            }
        ),
        encoding="utf-8",
    )


def _tree_file_bytes(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class TestVectorIndexBuilder:
    @patch("codenib._captured_directory.OwnedDirectoryStage.prepare")
    def test_build_rejects_runtime_selection_mismatch_before_staging_output(
        self,
        prepare_stage,
        tmp_path,
    ):
        builder = VectorIndexBuilder()

        with pytest.raises(RuntimeError, match="does not match"):
            builder.build(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(tmp_path / "vector"),
                source_selection=RepositorySourceSelection(("generated",)),
            )

        prepare_stage.assert_not_called()
        assert not (tmp_path / "vector").exists()

    @patch(
        "codenib.native_index_authorization."
        "require_native_index_authorization_preflight"
    )
    def test_incremental_rejects_runtime_selection_mismatch_before_preflight(
        self,
        authorization_preflight,
        tmp_path,
    ):
        builder = VectorIndexBuilder()

        with pytest.raises(RuntimeError, match="does not match"):
            builder.incremental_update(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(tmp_path / "vector"),
                last_commit="a" * 40,
                source_selection=RepositorySourceSelection(("generated",)),
            )

        authorization_preflight.assert_not_called()
        assert not (tmp_path / "vector").exists()

    @patch("codenib.index.embedding.builders.build_hierarchical_vector_store")
    def test_build_returns_status_with_stats(self, mock_build_fn, tmp_path):
        def document(file_path):
            return SimpleNamespace(
                page_content="value = 1\n",
                metadata={"file": file_path},
            )

        mock_vs = MagicMock()
        mock_vs.l0_documents = [document("src/a.py"), document("src/b.py")]
        mock_vs.l2_documents = [
            document("src/a.py"),
            document("src/b.py"),
            document("src/c.py"),
        ]

        def build_vector(**kwargs):
            build_path = Path(kwargs["index_path"])
            _write_mock_vector_generation(
                build_path,
                "test-model",
                artifact_metadata=kwargs["artifact_metadata"],
                l0_count=2,
                l2_count=3,
            )
            return mock_vs

        mock_build_fn.side_effect = build_vector

        selection = RepositorySourceSelection(["src/private"])
        builder = VectorIndexBuilder(
            languages=["python"],
            embedding_model="test-model",
            embedding_dimension=384,
            source_selection=selection,
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
        assert status.metadata["source_selection_digest"] == selection.digest
        assert status.metadata["persistence_config_fingerprint"]["file"] == (
            "config_test-model.json"
        )
        assert status.metadata["document_count"] == {"l0": 2, "l2": 3}
        mock_build_fn.assert_called_once()
        assert mock_build_fn.call_args.kwargs["force_rebuild"] is True
        assert mock_build_fn.call_args.kwargs["strict_chunking"] is True
        passed_selection = mock_build_fn.call_args.kwargs["source_selection"]
        assert passed_selection == selection
        assert passed_selection is not selection
        assert mock_build_fn.call_args.kwargs["_atomic_publish"] is False
        assert not (output_path / VECTOR_VIEW_UPDATE_MARKER).exists()

    @patch("codenib.index.embedding.builders.build_hierarchical_vector_store")
    def test_build_rejects_documents_outside_source_selection(
        self,
        mock_build_fn,
        tmp_path,
    ):
        def build_vector(**kwargs):
            _write_mock_vector_generation(Path(kwargs["index_path"]), "test-model")
            return SimpleNamespace(
                dimension=384,
                l0_documents=[],
                l2_documents=[
                    SimpleNamespace(
                        page_content="secret\n",
                        metadata={"file": "src/private/secret.py"},
                    )
                ],
            )

        mock_build_fn.side_effect = build_vector
        builder = VectorIndexBuilder(
            embedding_model="test-model",
            embedding_dimension=384,
            source_selection=RepositorySourceSelection(["src/private"]),
        )

        with pytest.raises(
            RuntimeError,
            match="vector l2 documents leaked excluded",
        ):
            builder.build(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(tmp_path / "vector"),
            )

    @patch("codenib.index.embedding.builders.build_hierarchical_vector_store")
    def test_build_rejects_documents_ignored_by_default_policy(
        self,
        mock_build_fn,
        tmp_path,
    ):
        def build_vector(**kwargs):
            _write_mock_vector_generation(Path(kwargs["index_path"]), "test-model")
            return SimpleNamespace(
                dimension=384,
                l0_documents=[],
                l2_documents=[
                    SimpleNamespace(
                        page_content="dependency\n",
                        metadata={"file": "node_modules/dependency.js"},
                    )
                ],
            )

        mock_build_fn.side_effect = build_vector
        builder = VectorIndexBuilder(
            embedding_model="test-model",
            embedding_dimension=384,
        )

        with pytest.raises(
            RuntimeError,
            match="vector l2 documents leaked excluded",
        ):
            builder.build(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(tmp_path / "vector"),
            )

    @patch("codenib.index.embedding.builders.build_hierarchical_vector_store")
    def test_build_rejects_unreceipted_schema_8_generation(
        self,
        mock_build_fn,
        tmp_path,
    ):
        mock_vs = MagicMock(
            l0_documents=[],
            l2_documents=[SimpleNamespace(metadata={"file": "source_0.py"})],
        )

        def build_vector(**kwargs):
            build_path = Path(kwargs["index_path"])
            _write_mock_vector_generation(build_path, "test-model")
            level_artifacts = {
                "l2": vector_level_artifact_records(
                    build_path / "l2",
                    "test-model",
                    documents_file="documents_test-model.pkl",
                )
            }
            (build_path / "config_test-model.json").write_text(
                json.dumps(
                    {
                        "artifact": kwargs["artifact_metadata"],
                        "l0_documents": 0,
                        "l2_documents": 1,
                        "level_artifacts": level_artifacts,
                        "persistence_schema": VECTOR_PERSISTENCE_SCHEMA,
                        "row_mapping": VECTOR_ROW_MAPPING_CONTRACT,
                    }
                ),
                encoding="utf-8",
            )
            return mock_vs

        mock_build_fn.side_effect = build_vector
        output_path = tmp_path / "vector"
        builder = VectorIndexBuilder(
            embedding_model="test-model",
            embedding_dimension=384,
        )

        with pytest.raises(ValueError, match="invalid l2 documents"):
            builder.build(
                scope="current_repo",
                repo_path="/fake/repo",
                output_dir=str(output_path),
            )

        assert not output_path.exists()

    @pytest.mark.parametrize("mutation", ["corrupt", "wrong-count"])
    @patch("codenib.index.embedding.builders.build_hierarchical_vector_store")
    def test_build_rejects_invalid_persisted_faiss_receipt(
        self,
        mock_build_fn,
        tmp_path,
        mutation,
    ):
        mock_vs = MagicMock(
            l0_documents=[],
            l2_documents=[SimpleNamespace(metadata={"file": "source_0.py"})],
        )

        def build_vector(**kwargs):
            build_path = Path(kwargs["index_path"])
            _write_mock_vector_generation(
                build_path,
                "test-model",
                artifact_metadata=kwargs["artifact_metadata"],
            )
            index_path = build_path / "l2/index_test-model.faiss"
            if mutation == "corrupt":
                index_path.write_bytes(b"not-a-faiss-index")
            else:
                index = faiss.IndexFlatIP(384)
                index.add(np.zeros((2, 384), dtype=np.float32))
                faiss.write_index(index, str(index_path))
            config_path = build_path / "config_test-model.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["level_artifacts"]["l2"] = vector_level_artifact_records(
                build_path / "l2",
                "test-model",
                documents_file="documents_test-model.json",
            )
            config_path.write_text(json.dumps(config), encoding="utf-8")
            return mock_vs

        mock_build_fn.side_effect = build_vector
        output_path = tmp_path / "vector"
        builder = VectorIndexBuilder(
            embedding_model="test-model",
            embedding_dimension=384,
        )
        expected_error = RuntimeError if mutation == "corrupt" else ValueError

        with pytest.raises(expected_error):
            builder.build(
                scope="current_repo",
                repo_path="/fake/repo",
                output_dir=str(output_path),
            )

        assert not output_path.exists()

    @pytest.mark.parametrize("mutation", ["root", "level", "metadata"])
    @patch("codenib.index.embedding.builders.build_hierarchical_vector_store")
    def test_build_rejects_incomplete_schema_8_structural_contract(
        self,
        mock_build_fn,
        tmp_path,
        mutation,
    ):
        mock_vs = MagicMock(
            l0_documents=[],
            l2_documents=[SimpleNamespace(metadata={"file": "source_0.py"})],
        )

        def build_vector(**kwargs):
            build_path = Path(kwargs["index_path"])
            _write_mock_vector_generation(
                build_path,
                "test-model",
                artifact_metadata=kwargs["artifact_metadata"],
            )
            config_path = build_path / "config_test-model.json"
            if mutation == "root":
                config = json.loads(config_path.read_text(encoding="utf-8"))
                config.pop("embedding_model")
                config_path.write_text(json.dumps(config), encoding="utf-8")
            elif mutation == "level":
                (build_path / "l2/config_test-model.json").write_text(
                    "{}",
                    encoding="utf-8",
                )
            else:
                documents_path = build_path / "l2/documents_test-model.json"
                documents = json.loads(documents_path.read_text(encoding="utf-8"))
                documents[0]["metadata"].pop("content_hash")
                documents_path.write_bytes(
                    b"".join(canonical_json_array_chunks(documents))
                )
                config = json.loads(config_path.read_text(encoding="utf-8"))
                config["level_artifacts"]["l2"] = vector_level_artifact_records(
                    build_path / "l2",
                    "test-model",
                    documents_file="documents_test-model.json",
                )
                config_path.write_text(json.dumps(config), encoding="utf-8")
            return mock_vs

        mock_build_fn.side_effect = build_vector
        output_path = tmp_path / "vector"
        builder = VectorIndexBuilder(
            embedding_model="test-model",
            embedding_dimension=384,
        )

        with pytest.raises(ValueError):
            builder.build(
                scope="current_repo",
                repo_path="/fake/repo",
                output_dir=str(output_path),
            )

        assert not output_path.exists()

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
            _write_mock_vector_generation(
                build_path,
                "test-model",
                artifact_metadata=kwargs["artifact_metadata"],
            )
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
            "l2/documents_test-model.json",
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
            _write_mock_vector_generation(
                Path(kwargs["index_path"]),
                "test-model",
                artifact_metadata=kwargs["artifact_metadata"],
            )
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
            source_selection=RepositorySourceSelection(),
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
            source_selection=RepositorySourceSelection(),
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
            "builder_schema": 8,
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
            "source_selection_digest": RepositorySourceSelection().digest,
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
            l2_documents=[SimpleNamespace(metadata={"file": "src/a.py"})],
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
        mock_vs = MagicMock(
            l0_documents=[],
            l2_documents=[SimpleNamespace(metadata={"file": "src/a.py"})],
        )

        def build_vector(**kwargs):
            build_path = Path(kwargs["index_path"])
            _write_mock_vector_generation(
                build_path,
                "text-embedding-3-small",
                artifact_metadata=kwargs["artifact_metadata"],
                dimension=1536,
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
        from codenib.graph.code_graph import CodeGraph

        mock_graph = CodeGraph("/fake/repo")
        mock_graph.add_root_node(".")
        for index in range(49):
            mock_graph.add_file_node(f"src/file_{index}.py")
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
        assert status.metadata["builder_schema"] == 6
        assert status.metadata["allow_project_preparation"] is True
        assert status.metadata["target_dir"] is None
        assert status.metadata["update_mode"] == "full_rebuild"
        assert status.metadata["scip_decoded_artifacts"] == {}
        assert status.metadata["graph_artifact"] == {
            "relative_path": "graph.pkl",
            "size": (Path(output) / "graph.pkl").stat().st_size,
            "sha256": regular_file_fingerprint(Path(output) / "graph.pkl")["sha256"],
        }
        assert status.metadata["lsp_occurrence_artifact"] is None
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

        assert enabled.artifact_identity()["builder_schema"] == 6
        assert enabled.artifact_identity()["allow_project_preparation"] is True
        assert disabled.artifact_identity()["allow_project_preparation"] is False
        assert enabled.artifact_identity() != disabled.artifact_identity()

    def test_full_rebuild_does_not_reuse_stale_occurrence_sidecar(
        self, monkeypatch, tmp_path
    ):
        from codenib import ls_router
        from codenib.scip_interface.lsp_occurrence_index import (
            SCIPOccurrence,
            SCIPOccurrenceIndex,
        )

        output = tmp_path / "graph"
        output.mkdir()
        SCIPOccurrenceIndex(
            [SCIPOccurrence("src/main.py", 99, 0, 99, 5, "stale")]
        ).save(output / "lsp_index.pkl")
        current_graph = _minimal_code_graph("/fake/repo")

        monkeypatch.setattr(
            ls_router,
            "build_graph_for_languages_with_report",
            lambda *args, **kwargs: GraphBuildResult(
                graph=current_graph,
                requested_languages=["python"],
                available_languages=["python"],
                failed_languages={},
                graph_output_receipt=_fake_graph_output_receipt(args[1]),
            ),
        )

        status = SymbolGraphBuilder(language="python").build(
            scope="current_repo",
            repo_path="/fake/repo",
            output_dir=str(output),
        )

        assert status.state is IndexState.FRESH
        assert not (output / "lsp_index.pkl").exists()
        assert status.metadata["lsp_occurrence_artifact"] is None

    @pytest.mark.parametrize(
        ("language", "graph_route"),
        [("python", "active"), ("java", "lsp"), ("cpp", "active")],
    )
    def test_custom_selection_is_central_gate_when_backend_ignores_hint(
        self,
        monkeypatch,
        tmp_path,
        language,
        graph_route,
    ):
        from codenib import ls_router
        from codenib.graph.code_graph import CodeGraph
        from codenib.scip_interface.lsp_occurrence_index import (
            SCIPOccurrence,
            SCIPOccurrenceIndex,
        )

        graph = CodeGraph("/repo")
        graph.add_root_node(".")
        graph.add_directory_node("src")
        graph.add_file_node("src/main.py")
        graph.add_symbol_node("kept", 0, 0, 1, "function")
        graph.add_directory_node("private")
        graph.add_file_node("private/secret.py")
        graph.add_symbol_node("secret", 0, 0, 1, "function")
        graph.add_directory_node("node_modules")
        graph.add_file_node("node_modules/dependency.js")
        graph.add_symbol_node("dependency", 0, 0, 1, "function")
        graph.lsp_occurrence_index = SCIPOccurrenceIndex(
            [
                SCIPOccurrence("src/main.py", 0, 0, 0, 4, "kept"),
                SCIPOccurrence("private/secret.py", 0, 0, 0, 4, "secret"),
                SCIPOccurrence(
                    "node_modules/dependency.js",
                    0,
                    0,
                    0,
                    4,
                    "dependency",
                ),
            ]
        )
        calls = []

        def backend_ignores_exclude_hints(*args, **kwargs):
            calls.append(kwargs)
            receipt = _fake_graph_output_receipt(args[1])
            return GraphBuildResult(
                graph=graph,
                requested_languages=[language],
                available_languages=[language],
                failed_languages={},
                graph_output_receipt=receipt,
            )

        monkeypatch.setattr(
            ls_router,
            "build_graph_for_languages_with_report",
            backend_ignores_exclude_hints,
        )
        selection = RepositorySourceSelection(["private"])
        output = tmp_path / f"graph-{language}"

        status = SymbolGraphBuilder(
            language=language,
            graph_route=graph_route,
            source_selection=selection,
        ).build(
            scope="current_repo",
            repo_path="/fake/repo",
            output_dir=str(output),
            source_selection=selection,
        )

        persisted = CodeGraph.load_graph(output / "graph.pkl")
        occurrences = SCIPOccurrenceIndex.load(output / "lsp_index.pkl")
        assert "src/main.py" in persisted.name_to_vertex
        assert "private/secret.py" not in persisted.name_to_vertex
        assert "secret" not in persisted.name_to_vertex
        assert "node_modules/dependency.js" not in persisted.name_to_vertex
        assert "dependency" not in persisted.name_to_vertex
        assert [item.file_path for item in occurrences.occurrences] == ["src/main.py"]
        assert status.metadata["lsp_occurrence_artifact"] == {
            "relative_path": "lsp_index.pkl",
            **regular_file_fingerprint(output / "lsp_index.pkl"),
        }
        assert calls[0]["exclude_patterns"][-2:] == ["private", "private/**"]
        assert status.metadata["source_selection_digest"] == selection.digest
        report = status.metadata["source_selection_report"]
        assert report["removed_excluded_path_count"] == 4
        assert report["excluded_path_count_after"] == 0
        assert report["invalid_path_count_after"] == 0
        assert "removed_excluded_paths" not in report

    def test_build_records_existing_decoded_scip_artifact(self, monkeypatch, tmp_path):
        from codenib import ls_router
        from codenib.compiler.artifact_fingerprints import regular_file_fingerprint
        from codenib.graph.code_graph import CodeGraph

        mock_graph = CodeGraph("/fake/repo")
        mock_graph.add_root_node(".")
        mock_graph.add_file_node("src/main.py")
        mock_graph.build_range_indexes()
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

        mock_graph = _minimal_code_graph()
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

        mock_graph = _minimal_code_graph()
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

        mock_graph = _minimal_code_graph()
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

        mock_graph = _minimal_code_graph()
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

        mock_graph = _minimal_code_graph()
        save_graph = MagicMock(wraps=mock_graph.save_graph)
        monkeypatch.setattr(mock_graph, "save_graph", save_graph)
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
            "compiler_nodes": 2,
            "compiler_edges": 0,
            **report,
        }
        assert status.metadata["partial_index"] is True
        save_graph.assert_called_once()

    def test_build_records_partial_language_coverage(self, monkeypatch, tmp_path):
        from codenib import ls_router
        from codenib.ls_router import GraphBuildResult

        mock_graph = _minimal_code_graph(file_count=24)
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

    def test_source_selection_is_snapshotted_for_in_process_builders(self):
        registry = IndexBuilderRegistry()
        selection = RepositorySourceSelection(["generated/private"])
        expected_digest = selection.digest

        register_default_builders(registry, source_selection=selection)
        object.__setattr__(selection, "exclude_subtrees", ("changed",))

        bm25 = registry.get("bm25")
        vector = registry.get("vector")
        symbol_graph = registry.get("symbol_graph")
        zoekt = registry.get("zoekt")
        assert isinstance(bm25, BM25IndexBuilder)
        assert isinstance(vector, VectorIndexBuilder)
        assert isinstance(symbol_graph, SymbolGraphBuilder)
        assert isinstance(zoekt, ZoektIndexBuilder)
        assert bm25.source_selection.exclude_subtrees == ("generated/private",)
        assert vector.source_selection.exclude_subtrees == ("generated/private",)
        assert symbol_graph.source_selection.exclude_subtrees == ("generated/private",)
        assert zoekt.source_selection.exclude_subtrees == ("generated/private",)
        assert bm25.source_selection is not selection
        assert vector.source_selection is not selection
        assert symbol_graph.source_selection is not selection
        assert zoekt.source_selection is not selection
        assert bm25.artifact_identity()["source_selection_digest"] == expected_digest
        assert vector.artifact_identity()["source_selection_digest"] == expected_digest
        assert (
            symbol_graph.artifact_identity()["source_selection_digest"]
            == expected_digest
        )
        assert zoekt.artifact_identity()["source_selection_digest"] == expected_digest

        with pytest.raises(TypeError, match="RepositorySourceSelection"):
            register_default_builders(
                IndexBuilderRegistry(),
                source_selection=object(),
            )

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
    @staticmethod
    def _successful_runner(
        calls,
        *,
        shard_bytes=b"zoekt-shard\n",
        binary_kwargs=None,
    ):
        real_run = subprocess.run

        def run(command, *args, **kwargs):
            if command[0] != "/fake/zoekt-git-index":
                return real_run(command, *args, **kwargs)
            calls.append(command)
            if binary_kwargs is not None:
                binary_kwargs.append(kwargs)
            output_dir = Path(command[command.index("-index") + 1])
            (output_dir / "repository_v16.00000.zoekt").write_bytes(shard_bytes)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        return run

    @staticmethod
    def _commit_all(repo, message="fixture"):
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-qm", message],
            check=True,
        )
        return subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def test_custom_selection_fails_before_any_output_side_effect(self, tmp_path):
        output_dir = tmp_path / "shards"
        builder = ZoektIndexBuilder(
            source_selection=RepositorySourceSelection(["private"])
        )

        with (
            patch(
                "codenib.compiler.index_builders.shutil.which",
                side_effect=AssertionError("binary lookup must not run"),
            ),
            patch(
                "codenib.compiler.index_builders.subprocess.run",
                side_effect=AssertionError("subprocess must not run"),
            ),
            pytest.raises(RuntimeError, match="does not support custom"),
        ):
            builder.build(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(output_dir),
            )

        assert not output_dir.exists()

    def test_default_excluded_tracked_source_fails_before_output(self, tmp_path):
        _git_repo(tmp_path)
        excluded = tmp_path / "node_modules" / "dependency.js"
        excluded.parent.mkdir()
        excluded.write_text("export const privateValue = 1;\n")
        self._commit_all(tmp_path, "excluded tracked source")
        output_dir = tmp_path / "shards"
        builder = ZoektIndexBuilder()

        with (
            patch(
                "codenib.compiler.index_builders.shutil.which",
                side_effect=AssertionError("binary lookup must not run"),
            ),
            pytest.raises(RuntimeError, match="tracked files are excluded"),
        ):
            builder.build(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(output_dir),
            )

        assert not output_dir.exists()

    def test_build_invokes_zoekt_git_index(self, tmp_path):
        builder = ZoektIndexBuilder()
        source_commit = _git_repo(tmp_path)
        output_dir = tmp_path / "shards"
        calls = []
        binary_kwargs = []
        shard_bytes = b"authenticated zoekt shard\n"

        with (
            patch(
                "codenib.compiler.index_builders.shutil.which",
                return_value="/fake/zoekt-git-index",
            ),
            patch(
                "codenib.compiler.index_builders.subprocess.run",
                side_effect=self._successful_runner(
                    calls,
                    shard_bytes=shard_bytes,
                    binary_kwargs=binary_kwargs,
                ),
            ),
        ):
            status = builder.build(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(output_dir),
                source_commit=source_commit,
            )

        assert status.index_type == "zoekt"
        assert status.state == IndexState.FRESH
        assert status.path == str(output_dir)
        assert output_dir.exists()
        assert len(calls) == 1
        cmd = calls[0]
        assert cmd[0] == "/fake/zoekt-git-index"
        assert "-index" in cmd
        private_index = Path(cmd[cmd.index("-index") + 1])
        assert private_index != output_dir
        assert binary_kwargs[0]["pass_fds"] == (int(private_index.name),)
        assert str(tmp_path) in cmd
        assert "-submodules=false" in cmd
        assert f"-branches={source_commit}" in cmd
        assert "-prefix=" in cmd
        assert status.metadata["tracked_source_count"] == 1
        assert status.metadata["source_commit"] == source_commit
        assert status.metadata["builder_schema"] == 3
        assert status.metadata["source_contract"] == ("immutable-git-commit-tree-v2")
        assert status.metadata["worktree_contract"] == (
            "selected-files-match-source-commit-v2"
        )
        assert status.metadata["shard_tree_receipt_schema"] == (
            "codenib.zoekt-shard-tree.v1"
        )
        assert status.metadata["submodules"] is False
        files = [
            {
                "path": "repository_v16.00000.zoekt",
                "size": len(shard_bytes),
                "sha256": hashlib.sha256(shard_bytes).hexdigest(),
            }
        ]
        canonical = json.dumps(
            {
                "schema": "codenib.zoekt-shard-tree.v1",
                "files": files,
            },
            allow_nan=False,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        assert status.metadata["shard_tree_receipt"] == {
            "schema": "codenib.zoekt-shard-tree.v1",
            "digest": f"sha256:{hashlib.sha256(canonical).hexdigest()}",
            "file_count": 1,
            "total_bytes": len(shard_bytes),
            "files": files,
        }

    @pytest.mark.skipif(os.name != "posix", reason="descriptor path is POSIX-only")
    def test_live_binary_writes_through_inherited_private_descriptor(self, tmp_path):
        source_commit = _git_repo(tmp_path)
        tool_dir = tmp_path / ".codenib_cache" / "tools"
        tool_dir.mkdir(parents=True)
        binary = tool_dir / "fake-zoekt-git-index"
        binary.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "root = pathlib.Path(args[args.index('-index') + 1])\n"
            "(root / 'live_v16.00000.zoekt').write_bytes(b'live shard\\n')\n"
        )
        binary.chmod(0o755)
        output_dir = tmp_path / ".codenib_cache" / "zoekt"

        status = ZoektIndexBuilder(binary=str(binary)).build(
            scope="current_repo",
            repo_path=str(tmp_path),
            output_dir=str(output_dir),
            source_commit=source_commit,
        )

        assert (output_dir / "live_v16.00000.zoekt").read_bytes() == b"live shard\n"
        assert status.metadata["shard_count"] == 1

    def test_build_uses_compiler_source_commit_without_reading_head(self, tmp_path):
        builder = ZoektIndexBuilder()
        source_commit = _git_repo(tmp_path)
        output_dir = tmp_path / "shards"
        binary_calls = []
        commands = []
        successful = self._successful_runner(binary_calls)

        def record_run(command, *args, **kwargs):
            commands.append(command)
            return successful(command, *args, **kwargs)

        with (
            patch(
                "codenib.compiler.index_builders.shutil.which",
                return_value="/fake/zoekt-git-index",
            ),
            patch(
                "codenib.compiler.index_builders.subprocess.run",
                side_effect=record_run,
            ),
        ):
            status = builder.build(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(output_dir),
                source_commit=source_commit,
            )

        assert not any("HEAD^{commit}" in command for command in commands)
        assert commands[0][-1] == f"{source_commit}^{{commit}}"
        assert f"-branches={source_commit}" in binary_calls[0]
        assert status.metadata["source_commit"] == source_commit

    @pytest.mark.parametrize("change", ["tracked", "untracked"])
    def test_build_rejects_selected_worktree_not_in_source_commit(
        self, tmp_path, change
    ):
        _git_repo(tmp_path)
        if change == "tracked":
            (tmp_path / "a.py").write_text("def changed():\n    return 1\n")
        else:
            (tmp_path / "untracked.py").write_text("VALUE = 1\n")
        output_dir = tmp_path / "shards"

        with (
            patch(
                "codenib.compiler.index_builders.shutil.which",
                side_effect=AssertionError("binary lookup must not run"),
            ),
            pytest.raises(RuntimeError, match="immutable commit"),
        ):
            ZoektIndexBuilder().build(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(output_dir),
            )

        assert not output_dir.exists()

    def test_build_raises_when_binary_missing(self, tmp_path):
        builder = ZoektIndexBuilder(binary="this-binary-does-not-exist-12345")
        _git_repo(tmp_path)

        with (
            patch(
                "codenib.compiler.index_builders.shutil.which",
                return_value=None,
            ),
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
        builder = ZoektIndexBuilder()
        _git_repo(tmp_path)
        real_run = subprocess.run

        def fail_binary(command, *args, **kwargs):
            if command[0] == "/fake/zoekt-git-index":
                raise subprocess.CalledProcessError(
                    returncode=2,
                    cmd=command,
                    stderr="boom",
                )
            return real_run(command, *args, **kwargs)

        with (
            patch(
                "codenib.compiler.index_builders.shutil.which",
                return_value="/fake/zoekt-git-index",
            ),
            patch(
                "codenib.compiler.index_builders.subprocess.run",
                side_effect=fail_binary,
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

    def test_rejects_visible_gitlink_before_binary_or_output(self, tmp_path):
        target_commit = _git_repo(tmp_path)
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "update-index",
                "--add",
                "--cacheinfo",
                f"160000,{target_commit},dependency",
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-qm", "gitlink"],
            check=True,
        )
        output_dir = tmp_path / "shards"

        with (
            patch(
                "codenib.compiler.index_builders.shutil.which",
                side_effect=AssertionError("binary lookup must not run"),
            ),
            pytest.raises(RuntimeError, match="visible gitlink/submodule"),
        ):
            ZoektIndexBuilder().build(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(output_dir),
            )

        assert not output_dir.exists()

    def test_rejects_sparse_checkout_missing_visible_tree_path(self, tmp_path):
        _git_repo(tmp_path)
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("VALUE = 1\n")
        (tmp_path / "secrets").mkdir()
        (tmp_path / "secrets" / "private.py").write_text("TOKEN = 'private'\n")
        self._commit_all(tmp_path, "sparse fixture")
        subprocess.run(
            ["git", "-C", str(tmp_path), "sparse-checkout", "init", "--cone"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "sparse-checkout", "set", "src"],
            check=True,
        )
        assert not (tmp_path / "secrets" / "private.py").exists()
        output_dir = tmp_path / "shards"

        with (
            patch(
                "codenib.compiler.index_builders.shutil.which",
                side_effect=AssertionError("binary lookup must not run"),
            ),
            pytest.raises(RuntimeError, match="worktree path is missing"),
        ):
            ZoektIndexBuilder().build(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(output_dir),
            )

        assert not output_dir.exists()

    def test_rejects_skip_worktree_bytes_hidden_from_git_diff(self, tmp_path):
        source_commit = _git_repo(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "update-index", "--skip-worktree", "a.py"],
            check=True,
        )
        (tmp_path / "a.py").write_text("def hidden_change():\n    return 1\n")
        assert (
            subprocess.run(
                ["git", "-C", str(tmp_path), "diff", "--quiet", source_commit, "--"],
                check=False,
            ).returncode
            == 0
        )
        output_dir = tmp_path / "shards"

        with (
            patch(
                "codenib.compiler.index_builders.shutil.which",
                side_effect=AssertionError("binary lookup must not run"),
            ),
            pytest.raises(RuntimeError, match="worktree bytes differ"),
        ):
            ZoektIndexBuilder().build(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(output_dir),
            )

        assert not output_dir.exists()

    def test_rejects_skip_worktree_symlink_target_mismatch(self, tmp_path):
        _git_repo(tmp_path)
        link = tmp_path / "current.py"
        link.symlink_to("a.py")
        source_commit = self._commit_all(tmp_path, "tracked symlink")
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "update-index",
                "--skip-worktree",
                "current.py",
            ],
            check=True,
        )
        link.unlink()
        link.symlink_to("missing.py")
        assert (
            subprocess.run(
                ["git", "-C", str(tmp_path), "diff", "--quiet", source_commit, "--"],
                check=False,
            ).returncode
            == 0
        )
        output_dir = tmp_path / "shards"

        with (
            patch(
                "codenib.compiler.index_builders.shutil.which",
                side_effect=AssertionError("binary lookup must not run"),
            ),
            pytest.raises(RuntimeError, match="worktree link differs"),
        ):
            ZoektIndexBuilder().build(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(output_dir),
            )

        assert not output_dir.exists()

    def test_rejects_crlf_bytes_normalized_clean_by_git(self, tmp_path):
        _git_repo(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "core.autocrlf", "true"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "--renormalize", "a.py"],
            check=True,
        )
        (tmp_path / "a.py").unlink()
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "--", "a.py"],
            check=True,
        )
        assert b"\r\n" in (tmp_path / "a.py").read_bytes()
        assert (
            subprocess.run(
                ["git", "-C", str(tmp_path), "status", "--porcelain"],
                check=True,
                capture_output=True,
            ).stdout
            == b""
        )
        output_dir = tmp_path / "shards"

        with (
            patch(
                "codenib.compiler.index_builders.shutil.which",
                side_effect=AssertionError("binary lookup must not run"),
            ),
            pytest.raises(RuntimeError, match="worktree bytes differ"),
        ):
            ZoektIndexBuilder().build(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(output_dir),
            )

        assert not output_dir.exists()

    def test_rejects_smudged_bytes_normalized_clean_by_filter(self, tmp_path):
        _git_repo(tmp_path)
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "config",
                "filter.zoekt-proof.clean",
                "sed -e 's/worktree/commit/g'",
            ],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "config",
                "filter.zoekt-proof.smudge",
                "cat",
            ],
            check=True,
        )
        (tmp_path / ".gitattributes").write_text("*.txt filter=zoekt-proof\n")
        (tmp_path / "filtered.txt").write_text("commit\n")
        self._commit_all(tmp_path, "filtered fixture")
        (tmp_path / "filtered.txt").write_text("worktree\n")
        assert (
            subprocess.run(
                ["git", "-C", str(tmp_path), "diff", "--quiet", "HEAD", "--"],
                check=False,
            ).returncode
            == 0
        )
        output_dir = tmp_path / "shards"

        with (
            patch(
                "codenib.compiler.index_builders.shutil.which",
                side_effect=AssertionError("binary lookup must not run"),
            ),
            pytest.raises(RuntimeError, match="worktree bytes differ"),
        ):
            ZoektIndexBuilder().build(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(output_dir),
            )

        assert not output_dir.exists()

    def test_git_replace_cannot_substitute_the_proved_commit_tree(self, tmp_path):
        replacement_commit = _git_repo(tmp_path)
        replacement_branch = subprocess.run(
            ["git", "-C", str(tmp_path), "branch", "--show-current"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(tmp_path), "switch", "-qc", "secret-tree"],
            check=True,
        )
        (tmp_path / "a.py").unlink()
        secret = tmp_path / "node_modules" / "secret.txt"
        secret.parent.mkdir()
        secret.write_text("must not be indexed\n")
        original_commit = self._commit_all(tmp_path, "secret tree")
        subprocess.run(
            ["git", "-C", str(tmp_path), "switch", "-q", replacement_branch],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "replace",
                original_commit,
                replacement_commit,
            ],
            check=True,
        )
        substituted = subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "ls-tree",
                "-r",
                "--name-only",
                original_commit,
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        assert substituted == ["a.py"]
        output_dir = tmp_path / "shards"

        with (
            patch(
                "codenib.compiler.index_builders.shutil.which",
                side_effect=AssertionError("binary lookup must not run"),
            ),
            pytest.raises(RuntimeError, match="tracked files are excluded"),
        ):
            ZoektIndexBuilder().build(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(output_dir),
                source_commit=original_commit,
            )

        assert not output_dir.exists()

    @pytest.mark.parametrize(
        "extra_args",
        [
            ["--"],
            ["positional"],
            ["-parallelism", "4"],
            ["-parallelism"],
            ["-branches=HEAD"],
            ["--submodules=true"],
        ],
    )
    def test_rejects_unsafe_extra_args_before_subprocess_or_output(
        self,
        tmp_path,
        extra_args,
    ):
        output_dir = tmp_path / "shards"
        builder = ZoektIndexBuilder(extra_args=extra_args)

        with (
            patch(
                "codenib.compiler.index_builders.shutil.which",
                side_effect=AssertionError("binary lookup must not run"),
            ),
            patch(
                "codenib.compiler.index_builders.subprocess.run",
                side_effect=AssertionError("subprocess must not run"),
            ),
            pytest.raises((TypeError, ValueError), match="Zoekt extra_args"),
        ):
            builder.build(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(output_dir),
            )

        assert not output_dir.exists()

    def test_rejects_nonregular_shard_tree_without_fresh_receipt(self, tmp_path):
        _git_repo(tmp_path)
        output_dir = tmp_path / "shards"
        real_run = subprocess.run

        def write_link(command, *args, **kwargs):
            if command[0] != "/fake/zoekt-git-index":
                return real_run(command, *args, **kwargs)
            private_index = Path(command[command.index("-index") + 1])
            (private_index / "target").write_bytes(b"shard")
            (private_index / "repository_v16.00000.zoekt").symlink_to("target")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with (
            patch(
                "codenib.compiler.index_builders.shutil.which",
                return_value="/fake/zoekt-git-index",
            ),
            patch(
                "codenib.compiler.index_builders.subprocess.run",
                side_effect=write_link,
            ),
            pytest.raises(RuntimeError, match="refuses linked content"),
        ):
            ZoektIndexBuilder().build(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(output_dir),
            )

    def test_private_generation_replaces_preexisting_shards(self, tmp_path):
        source_commit = _git_repo(tmp_path)
        output_dir = tmp_path / ".codenib_cache" / "zoekt"
        output_dir.mkdir(parents=True)
        (output_dir / "old_v16.00000.zoekt").write_bytes(b"stale secret shard\n")
        calls = []
        new_shard = b"current source shard\n"

        with (
            patch(
                "codenib.compiler.index_builders.shutil.which",
                return_value="/fake/zoekt-git-index",
            ),
            patch(
                "codenib.compiler.index_builders.subprocess.run",
                side_effect=self._successful_runner(
                    calls,
                    shard_bytes=new_shard,
                ),
            ),
        ):
            status = ZoektIndexBuilder().build(
                scope="current_repo",
                repo_path=str(tmp_path),
                output_dir=str(output_dir),
                source_commit=source_commit,
            )

        assert not (output_dir / "old_v16.00000.zoekt").exists()
        published = output_dir / "repository_v16.00000.zoekt"
        assert published.read_bytes() == new_shard
        assert status.metadata["shard_count"] == 1
        assert [
            record["path"] for record in status.metadata["shard_tree_receipt"]["files"]
        ] == ["repository_v16.00000.zoekt"]


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
            str(tmp_path), cache_dir=str(tmp_path / ".codenib_cache")
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
            str(tmp_path), cache_dir=str(tmp_path / ".codenib_cache")
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
            str(tmp_path), cache_dir=str(tmp_path / ".codenib_cache")
        )

        assert "bm25" in manifest.indexes
        assert "vector" not in manifest.indexes

    @pytest.mark.parametrize(
        ("field", "value"),
        (
            ("index_type", "other"),
            ("state", IndexState.STALE),
            ("scope", "other_repo"),
            ("path", "/outside/compiler-output"),
        ),
    )
    def test_rejects_builder_status_outside_compiler_contract(
        self,
        tmp_path,
        field,
        value,
    ):
        class InvalidStatusBuilder:
            def build(self, scope: str, **kwargs) -> IndexStatus:
                status = IndexStatus(
                    index_type="rec",
                    state=IndexState.FRESH,
                    scope=scope,
                    path=kwargs["output_dir"],
                    metadata={},
                )
                setattr(status, field, value)
                return status

            def incremental_update(self, scope: str, **kwargs) -> IndexStatus:
                return self.build(scope, **kwargs)

        registry = IndexBuilderRegistry()
        registry.register("rec", InvalidStatusBuilder())
        manifest = IndexCompiler(
            registry,
            IndexCompilerConfig(index_types=["rec"]),
        ).compile_repo(str(tmp_path), cache_dir=str(tmp_path / ".codenib_cache"))

        assert manifest.indexes["rec"].status == "failed"
        assert manifest.capabilities == {
            "sparse_search": False,
            "dense_search": False,
            "hybrid_search": False,
            "symbol_navigation": False,
        }

    def test_rejects_zoekt_receipt_for_another_source_commit(self, tmp_path):
        expected_commit = "a" * 40
        observed = []

        class WrongCommitZoektBuilder:
            def build(self, scope: str, **kwargs) -> IndexStatus:
                observed.append(kwargs["source_commit"])
                selection = kwargs["source_selection"]
                return IndexStatus(
                    index_type="zoekt",
                    state=IndexState.FRESH,
                    scope=scope,
                    path=kwargs["output_dir"],
                    metadata={
                        "source_selection_digest": selection.digest,
                        "source_commit": "b" * 40,
                    },
                )

            def incremental_update(self, scope: str, **kwargs) -> IndexStatus:
                return self.build(scope, **kwargs)

        registry = IndexBuilderRegistry()
        registry.register("zoekt", WrongCommitZoektBuilder())
        compiler = IndexCompiler(
            registry,
            IndexCompilerConfig(index_types=["zoekt"]),
        )
        with patch.object(
            IndexCompiler,
            "_get_head_commit",
            return_value=expected_commit,
        ):
            manifest = compiler.compile_repo(
                str(tmp_path),
                cache_dir=str(tmp_path / ".codenib_cache"),
            )

        assert observed == [expected_commit]
        assert manifest.indexes["zoekt"].status == "failed"
        assert "compiler source commit" in manifest.indexes["zoekt"].metadata["error"]

    def test_manifest_file_written(self, tmp_path):
        registry = IndexBuilderRegistry()
        registry.register("bm25", _mock_builder("bm25"))

        config = IndexCompilerConfig(index_types=["bm25"])
        compiler = IndexCompiler(registry, config)
        cache_dir = str(tmp_path / ".codenib_cache")
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

    def test_visible_in_repository_cache_is_rejected_before_build(self, tmp_path):
        builder = _mock_builder("bm25")
        registry = IndexBuilderRegistry()
        registry.register("bm25", builder)

        with pytest.raises(ValueError, match="index cache must be excluded"):
            IndexCompiler(
                registry,
                IndexCompilerConfig(index_types=["bm25"]),
            ).compile_repo(
                str(tmp_path),
                cache_dir=str(tmp_path / "visible-cache"),
            )

    def test_visible_in_repository_cache_is_rejected_before_update_fast_path(
        self,
        tmp_path,
    ):
        registry = IndexBuilderRegistry()
        registry.register("bm25", _mock_builder("bm25"))
        compiler = IndexCompiler(
            registry,
            IndexCompilerConfig(index_types=["bm25"]),
        )
        hidden_cache = tmp_path / ".codenib_cache"
        compiler.compile_repo(str(tmp_path), cache_dir=str(hidden_cache))

        visible_cache = tmp_path / "visible-cache"
        shutil.copytree(hidden_cache, visible_cache)

        with pytest.raises(ValueError, match="index cache must be excluded"):
            compiler.update_repo(str(tmp_path), cache_dir=str(visible_cache))

    def test_explicitly_excluded_in_repository_cache_is_allowed(self, tmp_path):
        selection = RepositorySourceSelection(("visible-cache",))
        registry = IndexBuilderRegistry()
        registry.register("bm25", _mock_builder("bm25"))

        manifest = IndexCompiler(
            registry,
            IndexCompilerConfig(
                index_types=["bm25"],
                source_selection=selection,
            ),
        ).compile_repo(
            str(tmp_path),
            cache_dir=str(tmp_path / "visible-cache"),
        )

        assert manifest.indexes["bm25"].status == "fresh"
        assert manifest.source_selection == selection

    def test_manifest_can_be_loaded(self, tmp_path):
        registry = IndexBuilderRegistry()
        registry.register("bm25", _mock_builder("bm25"))

        config = IndexCompilerConfig(index_types=["bm25"])
        compiler = IndexCompiler(registry, config)
        cache_dir = str(tmp_path / ".codenib_cache")
        compiler.compile_repo(str(tmp_path), cache_dir=cache_dir)

        manifest_path = os.path.join(cache_dir, MANIFEST_FILENAME)
        loaded = RepoManifest.load(manifest_path)
        assert loaded.indexes["bm25"].status == "fresh"

    def test_selection_is_bound_to_fingerprints_builder_and_manifest(self, tmp_path):
        selection = RepositorySourceSelection(["generated"])
        observed = []

        class SelectionAwareBuilder:
            def build(self, scope: str, **kwargs) -> IndexStatus:
                selected = kwargs["source_selection"]
                observed.append(selected)
                return IndexStatus(
                    index_type="bm25",
                    state=IndexState.FRESH,
                    scope=scope,
                    path=kwargs["output_dir"],
                    metadata={"source_selection_digest": selected.digest},
                )

            def incremental_update(self, scope: str, **kwargs) -> IndexStatus:
                return self.build(scope, **kwargs)

        registry = IndexBuilderRegistry()
        registry.register("bm25", SelectionAwareBuilder())
        compiler = IndexCompiler(
            registry,
            IndexCompilerConfig(
                index_types=["bm25"],
                source_selection=selection,
            ),
        )
        real_fingerprint = index_compiler_module.fingerprint_repository

        with patch.object(
            index_compiler_module,
            "fingerprint_repository",
            wraps=real_fingerprint,
        ) as fingerprint:
            manifest = compiler.compile_repo(
                str(tmp_path), cache_dir=str(tmp_path / ".codenib_cache")
            )

        assert observed == [selection]
        assert fingerprint.call_count == 2
        assert all(
            call.kwargs["selection"] == selection for call in fingerprint.mock_calls
        )
        assert manifest.source_selection == selection
        assert manifest.source_selection is not selection
        assert manifest.source_selection_digest == selection.digest
        assert manifest.last_indexed_source_selection_digest == selection.digest
        assert manifest.indexes["bm25"].source_selection_digest == selection.digest
        assert (
            RepoManifest.load(
                tmp_path / ".codenib_cache" / MANIFEST_FILENAME
            ).source_selection
            == selection
        )

    @pytest.mark.parametrize(
        "selection",
        [
            RepositorySourceSelection(),
            RepositorySourceSelection(["generated"]),
        ],
    )
    def test_selection_fails_closed_without_builder_confirmation(
        self,
        tmp_path,
        selection,
    ):
        class UnconfirmedBuilder:
            def build(self, scope: str, **kwargs) -> IndexStatus:
                return IndexStatus(
                    index_type="bm25",
                    state=IndexState.FRESH,
                    scope=scope,
                    path=kwargs["output_dir"],
                    metadata={},
                )

            def incremental_update(self, scope: str, **kwargs) -> IndexStatus:
                return self.build(scope, **kwargs)

        registry = IndexBuilderRegistry()
        registry.register("bm25", UnconfirmedBuilder())
        compiler = IndexCompiler(
            registry,
            IndexCompilerConfig(
                index_types=["bm25"],
                source_selection=selection,
            ),
        )

        manifest = compiler.compile_repo(
            str(tmp_path), cache_dir=str(tmp_path / ".codenib_cache")
        )

        entry = manifest.indexes["bm25"]
        assert entry.status == "failed"
        assert entry.source_selection_digest == ""
        assert "did not confirm" in entry.metadata["error"]
        assert manifest.last_indexed_source_selection_digest == ""

    def test_invalid_builder_result_is_recorded_as_failed(self, tmp_path):
        class InvalidBuilder:
            def build(self, scope: str, **kwargs):
                return None

            def incremental_update(self, scope: str, **kwargs):
                return None

        registry = IndexBuilderRegistry()
        registry.register("bm25", InvalidBuilder())
        manifest = IndexCompiler(
            registry,
            IndexCompilerConfig(index_types=["bm25"]),
        ).compile_repo(str(tmp_path), cache_dir=str(tmp_path / ".codenib_cache"))

        assert manifest.indexes["bm25"].status == "failed"
        assert (
            "must return an IndexStatus" in manifest.indexes["bm25"].metadata["error"]
        )

    def test_index_types_override(self, tmp_path):
        registry = IndexBuilderRegistry()
        registry.register("bm25", _mock_builder("bm25"))
        registry.register("vector", _mock_builder("vector"))

        config = IndexCompilerConfig(index_types=["bm25", "vector"])
        compiler = IndexCompiler(registry, config)
        manifest = compiler.compile_repo(
            str(tmp_path),
            cache_dir=str(tmp_path / ".codenib_cache"),
            index_types=["bm25"],  # override: only build bm25
        )

        assert "bm25" in manifest.indexes
        assert "vector" not in manifest.indexes

    def test_partial_rebuild_preserves_unrequested_views(self, tmp_path):
        registry = IndexBuilderRegistry()
        registry.register("bm25", _mock_builder("bm25"))
        registry.register("vector", _mock_builder("vector"))

        compiler = IndexCompiler(registry)
        cache_dir = str(tmp_path / ".codenib_cache")
        compiler.compile_repo(str(tmp_path), cache_dir=cache_dir, index_types=["bm25"])
        manifest = compiler.compile_repo(
            str(tmp_path), cache_dir=cache_dir, index_types=["vector"]
        )

        assert set(manifest.indexes) == {"bm25", "vector"}

    def test_rebuild_does_not_keep_requested_view_fresh_without_builder(self, tmp_path):
        initial_registry = IndexBuilderRegistry()
        initial_registry.register("bm25", _mock_builder("bm25"))
        cache_dir = str(tmp_path / ".codenib_cache")
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
        cache_dir = str(tmp_path / ".codenib_cache")

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
            str(tmp_path), cache_dir=str(tmp_path / ".codenib_cache")
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
        cache_dir = str(tmp_path / ".codenib_cache")
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
        cache_dir = str(tmp_path / ".codenib_cache")
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
            selection = kwargs.get("source_selection", RepositorySourceSelection())
            return IndexStatus(
                index_type="rec",
                state=IndexState.FRESH,
                last_built=time.time(),
                age_seconds=0.0,
                scope=scope,
                path=output_dir,
                metadata={"source_selection_digest": selection.digest},
            )

        def incremental_update(self, scope: str, **kwargs) -> IndexStatus:
            calls.append(("incremental_update", kwargs.get("last_commit")))
            rest = {k: v for k, v in kwargs.items() if k != "last_commit"}
            return self.build(scope, **rest)

    return RecordingBuilder()


def _selection_recording_builder(calls: list, index_type: str = "rec"):
    class SelectionRecordingBuilder:
        def build(self, scope: str, **kwargs) -> IndexStatus:
            selection = kwargs["source_selection"]
            calls.append((index_type, selection.digest))
            output_dir = kwargs["output_dir"]
            os.makedirs(output_dir, exist_ok=True)
            return IndexStatus(
                index_type=index_type,
                state=IndexState.FRESH,
                scope=scope,
                path=output_dir,
                metadata={"source_selection_digest": selection.digest},
            )

        def incremental_update(self, scope: str, **kwargs) -> IndexStatus:
            return self.build(scope, **kwargs)

    return SelectionRecordingBuilder()


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

    def test_selection_change_rebuilds_same_bytes_and_binds_all_observations(
        self, tmp_path
    ):
        _git_repo(tmp_path)
        calls = []
        registry = IndexBuilderRegistry()
        registry.register("rec", _selection_recording_builder(calls))
        first_selection = RepositorySourceSelection()
        second_selection = RepositorySourceSelection(["generated"])
        first = IndexCompiler(
            registry,
            IndexCompilerConfig(
                index_types=["rec"],
                source_selection=first_selection,
            ),
        ).compile_repo(str(tmp_path))
        real_fingerprint = index_compiler_module.fingerprint_repository

        with patch.object(
            index_compiler_module,
            "fingerprint_repository",
            wraps=real_fingerprint,
        ) as fingerprint:
            updated = IndexCompiler(
                registry,
                IndexCompilerConfig(
                    index_types=["rec"],
                    source_selection=second_selection,
                ),
            ).update_repo(str(tmp_path))

        assert calls == [
            ("rec", first_selection.digest),
            ("rec", second_selection.digest),
        ]
        assert fingerprint.call_count == 2
        assert all(
            call.kwargs["selection"] == second_selection
            for call in fingerprint.mock_calls
        )
        assert first.source_fingerprint != updated.source_fingerprint
        assert updated.source_selection == second_selection
        assert updated.indexes["rec"].source_selection_digest == (
            second_selection.digest
        )
        assert updated.last_indexed_source_selection_digest == (second_selection.digest)

    def test_omitted_selection_reuses_persisted_manifest_policy(self, tmp_path):
        _git_repo(tmp_path)
        selection = RepositorySourceSelection(("generated",))
        initial_calls: list = []
        initial_registry = IndexBuilderRegistry()
        initial_registry.register(
            "rec",
            _selection_recording_builder(initial_calls),
        )
        IndexCompiler(
            initial_registry,
            IndexCompilerConfig(
                index_types=["rec"],
                source_selection=selection,
            ),
        ).compile_repo(str(tmp_path))
        _commit(tmp_path, "b.py")

        reuse_calls: list = []
        reuse_registry = IndexBuilderRegistry()
        reuse_registry.register("rec", _selection_recording_builder(reuse_calls))
        updated = IndexCompiler(
            reuse_registry,
            IndexCompilerConfig(index_types=["rec"]),
        ).update_repo(str(tmp_path))

        assert reuse_calls == [("rec", selection.digest)]
        assert updated.source_selection == selection
        assert updated.indexes["rec"].source_selection_digest == selection.digest

    def test_selection_change_replaces_languages_from_current_source_surface(
        self, tmp_path
    ):
        _git_repo(tmp_path)
        registry = IndexBuilderRegistry()
        registry.register("rec", _selection_recording_builder([]))
        IndexCompiler(
            registry,
            IndexCompilerConfig(
                index_types=["rec"],
                languages=["go", "python"],
            ),
        ).compile_repo(str(tmp_path))

        updated = IndexCompiler(
            registry,
            IndexCompilerConfig(
                index_types=["rec"],
                languages=["python"],
                source_selection=RepositorySourceSelection(("generated",)),
            ),
        ).update_repo(str(tmp_path))

        assert updated.languages == ["python"]

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
                    metadata={
                        **self.artifact_identity(),
                        "source_selection_digest": kwargs["source_selection"].digest,
                    },
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

            def _status(
                self,
                scope: str,
                output_dir: str,
                source_selection: RepositorySourceSelection,
            ) -> IndexStatus:
                return IndexStatus(
                    index_type="rec",
                    state=IndexState.FRESH,
                    last_built=time.time(),
                    age_seconds=0.0,
                    scope=scope,
                    path=output_dir,
                    metadata={
                        **self.artifact_identity(),
                        "source_selection_digest": source_selection.digest,
                    },
                )

            def build(self, scope: str, **kwargs) -> IndexStatus:
                calls.append(("build", self.version, kwargs.get("last_commit")))
                return self._status(
                    scope,
                    kwargs["output_dir"],
                    kwargs["source_selection"],
                )

            def incremental_update(self, scope: str, **kwargs) -> IndexStatus:
                calls.append(
                    ("incremental_update", self.version, kwargs.get("last_commit"))
                )
                return self._status(
                    scope,
                    kwargs["output_dir"],
                    kwargs["source_selection"],
                )

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

    def test_v11_manifest_forces_full_rebuild_instead_of_incremental_update(
        self,
        tmp_path,
    ):
        _git_repo(tmp_path)
        calls: list = []
        compiler = self._compiler(calls)
        manifest = compiler.compile_repo(str(tmp_path))
        legacy = f"sha256:{'a' * 64}"
        manifest.version = LEGACY_MANIFEST_VERSION
        manifest.source_selection = None
        manifest.last_indexed_source_selection_digest = ""
        manifest.source_fingerprint = legacy
        manifest.last_indexed_source_fingerprint = legacy
        manifest.indexes["rec"].source_fingerprint = legacy
        manifest.indexes["rec"].source_selection_digest = ""
        manifest.save(tmp_path / ".codenib_cache" / MANIFEST_FILENAME)
        _commit(tmp_path, "b.py")
        calls.clear()

        rebuilt = compiler.update_repo(str(tmp_path))

        assert calls == [("build", None)]
        assert rebuilt.source_fingerprint.startswith("sha256-v2:")
        assert rebuilt.indexes["rec"].source_fingerprint == (rebuilt.source_fingerprint)

    def test_v11_manifest_migrates_without_blessing_unrequested_entries(self, tmp_path):
        _git_repo(tmp_path)
        calls = []
        registry = IndexBuilderRegistry()
        registry.register("bm25", _selection_recording_builder(calls, "bm25"))
        registry.register(
            "symbol_graph",
            _selection_recording_builder(calls, "symbol_graph"),
        )
        config = IndexCompilerConfig(
            index_types=["bm25", "symbol_graph"],
            languages=["python"],
        )
        compiler = IndexCompiler(registry, config)
        current = compiler.compile_repo(str(tmp_path))
        payload = current.to_dict()
        payload["version"] = LEGACY_MANIFEST_VERSION
        payload["repo"].pop("source_selection")
        payload["repo"].pop("last_indexed_source_selection_digest")
        for entry in payload["indexes"].values():
            entry.pop("source_selection_digest")
        manifest_path = tmp_path / ".codenib_cache" / MANIFEST_FILENAME
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        calls.clear()

        migrated = compiler.update_repo(str(tmp_path), index_types=["bm25"])

        assert len(calls) == 1
        assert migrated.version == MANIFEST_VERSION
        assert migrated.source_selection == RepositorySourceSelection()
        assert migrated.indexes["bm25"].status == "fresh"
        assert migrated.indexes["bm25"].source_selection_digest == (
            RepositorySourceSelection().digest
        )
        assert migrated.indexes["symbol_graph"].status == "stale"
        assert migrated.indexes["symbol_graph"].source_selection_digest == ""
        assert migrated.last_indexed_commit == ""
        assert migrated.last_indexed_source_fingerprint == ""
        assert migrated.last_indexed_source_selection_digest == ""

    def test_failed_selection_rebuild_preserves_complete_previous_generation(
        self, tmp_path
    ):
        _git_repo(tmp_path)
        first_selection = RepositorySourceSelection(["generated-a"])
        second_selection = RepositorySourceSelection(["generated-b"])
        initial_calls = []
        initial_registry = IndexBuilderRegistry()
        initial_registry.register("rec", _selection_recording_builder(initial_calls))
        initial = IndexCompiler(
            initial_registry,
            IndexCompilerConfig(
                index_types=["rec"],
                source_selection=first_selection,
            ),
        ).compile_repo(str(tmp_path))
        previous_entry = copy.deepcopy(initial.indexes["rec"])

        failing_registry = IndexBuilderRegistry()
        failing_registry.register("rec", _failing_builder())
        failed = IndexCompiler(
            failing_registry,
            IndexCompilerConfig(
                index_types=["rec"],
                source_selection=second_selection,
            ),
        ).update_repo(str(tmp_path))

        entry = failed.indexes["rec"]
        assert failed.source_selection == second_selection
        assert entry.status == "failed"
        assert entry.path == previous_entry.path
        assert entry.config == previous_entry.config
        assert entry.commit == previous_entry.commit
        assert entry.source_fingerprint == previous_entry.source_fingerprint
        assert entry.source_selection_digest == previous_entry.source_selection_digest
        assert entry.metadata["source_selection_digest"] == first_selection.digest
        assert "Build failed intentionally" in entry.metadata["error"]
        assert failed.last_indexed_commit == initial.last_indexed_commit
        assert failed.last_indexed_source_fingerprint == (
            initial.last_indexed_source_fingerprint
        )
        assert failed.last_indexed_source_selection_digest == (first_selection.digest)

    def test_last_indexed_triple_advances_only_when_every_view_matches(self, tmp_path):
        _git_repo(tmp_path)
        first_selection = RepositorySourceSelection(["generated-a"])
        second_selection = RepositorySourceSelection(["generated-b"])
        calls = []
        registry = IndexBuilderRegistry()
        registry.register("bm25", _selection_recording_builder(calls, "bm25"))
        registry.register(
            "symbol_graph", _selection_recording_builder(calls, "symbol_graph")
        )
        first = IndexCompiler(
            registry,
            IndexCompilerConfig(
                index_types=["bm25", "symbol_graph"],
                source_selection=first_selection,
            ),
        ).compile_repo(str(tmp_path))

        partial = IndexCompiler(
            registry,
            IndexCompilerConfig(
                index_types=["bm25", "symbol_graph"],
                source_selection=second_selection,
            ),
        ).update_repo(str(tmp_path), index_types=["bm25"])

        assert partial.indexes["bm25"].status == "fresh"
        assert partial.indexes["bm25"].source_selection_digest == (
            second_selection.digest
        )
        assert partial.indexes["symbol_graph"].status == "stale"
        assert partial.indexes["symbol_graph"].source_selection_digest == (
            first_selection.digest
        )
        assert partial.last_indexed_commit == first.last_indexed_commit
        assert partial.last_indexed_source_fingerprint == (
            first.last_indexed_source_fingerprint
        )
        assert partial.last_indexed_source_selection_digest == (first_selection.digest)

    def test_full_rebuild_does_not_receive_previous_manifest_config(self, tmp_path):
        _git_repo(tmp_path)
        observed = []

        class ManifestAwareBuilder:
            def artifact_identity(self):
                return {"builder_schema": 7}

            def _status(self, scope, output_dir, source_selection):
                return IndexStatus(
                    index_type="rec",
                    state=IndexState.FRESH,
                    scope=scope,
                    path=output_dir,
                    metadata={
                        **self.artifact_identity(),
                        "generation": "recorded",
                        "source_selection_digest": source_selection.digest,
                    },
                )

            def build(self, scope: str, **kwargs) -> IndexStatus:
                return self._status(
                    scope,
                    kwargs["output_dir"],
                    kwargs["source_selection"],
                )

            def incremental_update(self, scope: str, **kwargs) -> IndexStatus:
                observed.append(kwargs["previous_artifact_config"])
                return self._status(
                    scope,
                    kwargs["output_dir"],
                    kwargs["source_selection"],
                )

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

            def _status(
                self,
                scope: str,
                output_dir: str,
                metadata,
                source_selection: RepositorySourceSelection,
            ) -> IndexStatus:
                return IndexStatus(
                    index_type="symbol_graph",
                    state=IndexState.FRESH,
                    last_built=time.time(),
                    age_seconds=0.0,
                    scope=scope,
                    path=output_dir,
                    metadata={
                        **self.artifact_identity(),
                        **metadata,
                        "source_selection_digest": source_selection.digest,
                    },
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
                    kwargs["source_selection"],
                )

            def incremental_update(self, scope: str, **kwargs) -> IndexStatus:
                calls.append(("incremental_update", kwargs["last_commit"]))
                return self._status(
                    scope,
                    kwargs["output_dir"],
                    {"update_mode": "incremental"},
                    kwargs["source_selection"],
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

    def test_changed_source_with_occurrence_sidecar_requests_full_rebuild(
        self, tmp_path, monkeypatch
    ):
        from codenib.graph.incremental.graph_patcher import GraphPatcher
        from codenib.scip_interface.lsp_occurrence_index import (
            SCIPOccurrence,
            SCIPOccurrenceIndex,
        )

        first = _git_repo(tmp_path)
        _commit(tmp_path, "b.py")
        out = tmp_path / "out"
        out.mkdir()
        _minimal_code_graph(str(tmp_path)).save_graph(out / "graph.pkl")
        SCIPOccurrenceIndex([SCIPOccurrence("a.py", 0, 0, 0, 5, "a")]).save(
            out / "lsp_index.pkl"
        )
        monkeypatch.setattr(
            GraphPatcher,
            "detect_changed_files",
            lambda *args, **kwargs: {
                "added": ["b.py"],
                "modified": [],
                "deleted": [],
                "renamed": [],
            },
        )
        monkeypatch.setattr(
            GraphPatcher,
            "patch_files",
            lambda *args, **kwargs: pytest.fail(
                "occurrence-bearing generation must rebuild before patching"
            ),
        )

        status = self._builder()._patch_graph(
            "current_repo", str(tmp_path), str(out), first
        )

        assert status is None
        occurrences = SCIPOccurrenceIndex.load(out / "lsp_index.pkl")
        assert [
            (item.file_path, item.start_line) for item in occurrences.occurrences
        ] == [("a.py", 0)]

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
