# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for concrete index builders and the IndexCompiler."""

from __future__ import annotations

import json
import os
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

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
    ManifestIndexStateStore,
    RepoManifest,
)
from codenib.compiler.resources import (
    IndexRequirement,
    IndexState,
    IndexStatus,
    ResourceResolver,
)
from codenib.repository_filters import (
    REPOSITORY_FILTER_POLICY_VERSION,
    default_exclude_patterns,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
        assert status.metadata["builder_schema"] == 3
        assert status.metadata["max_k"] == 64
        assert status.path == output
        mock_indexer_instance.save_index.assert_called_once_with(output)

    def test_incremental_delegates_to_build(self):
        builder = BM25IndexBuilder()
        with patch.object(builder, "build", return_value="result") as mock_build:
            result = builder.incremental_update(
                scope="repo", repo_path="/x", output_dir="/y"
            )
            mock_build.assert_called_once_with("repo", repo_path="/x", output_dir="/y")
            assert result == "result"


# ---------------------------------------------------------------------------
# VectorIndexBuilder
# ---------------------------------------------------------------------------


class TestVectorIndexBuilder:
    @patch("codenib.index.embedding.builders.build_hierarchical_vector_store")
    def test_build_returns_status_with_stats(self, mock_build_fn, tmp_path):
        mock_vs = MagicMock()
        mock_vs.l0_documents = ["d1", "d2"]
        mock_vs.l2_documents = ["d3", "d4", "d5"]
        mock_build_fn.return_value = mock_vs

        builder = VectorIndexBuilder(
            languages=["python"],
            embedding_model="test-model",
            embedding_dimension=384,
        )
        output = str(tmp_path / "vector")
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
        assert status.metadata["document_count"] == {"l0": 2, "l2": 3}
        mock_build_fn.assert_called_once()

    def test_artifact_identity_is_shared_by_full_and_incremental_statuses(self):
        builder = VectorIndexBuilder(
            embedding_model="test-model",
            embedding_dimension=384,
            embedding_kwargs={"revision": "model-commit"},
            index_metric="l2",
        )

        assert builder.artifact_identity() == {
            "builder_schema": 2,
            "embedding_model": "test-model",
            "embedding_provider": "huggingface",
            "embedding_dimension": 384,
            "dimension": 384,
            "embedding_kwargs": {"revision": "model-commit"},
            "index_metric": "l2",
            "languages": ["python"],
            "levels": ["l0", "l2"],
            "max_lines_per_chunk": 300,
            "repository_filter_policy": REPOSITORY_FILTER_POLICY_VERSION,
        }


# ---------------------------------------------------------------------------
# SymbolGraphBuilder
# ---------------------------------------------------------------------------


class TestSymbolGraphBuilder:
    def test_build_returns_status(self, monkeypatch, tmp_path):
        from codenib import ls_router

        mock_graph = MagicMock()
        mock_graph.graph.vs = list(range(50))  # 50 nodes
        calls = []

        def fake_build_graph_for_languages(*args, **kwargs):
            calls.append((args, kwargs))
            return mock_graph

        monkeypatch.setattr(
            ls_router,
            "build_graph_for_languages",
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
        assert status.metadata["language"] == "python"
        assert status.metadata["languages"] == ["python"]
        assert calls == [
            (
                ("/fake/repo", output),
                {
                    "languages": ["python"],
                    "project_name": "repo",
                    "skip_level": None,
                    "exclude_patterns": default_exclude_patterns(),
                    "graph_route": "active",
                },
            )
        ]

    def test_build_rejects_none_graph(self, monkeypatch, tmp_path):
        from codenib import ls_router

        monkeypatch.setattr(
            ls_router,
            "build_graph_for_languages",
            lambda *args, **kwargs: None,
        )

        builder = SymbolGraphBuilder()
        output = str(tmp_path / "graph")
        with pytest.raises(RuntimeError, match="returned no graph"):
            builder.build(
                scope="current_repo",
                repo_path="/fake/repo",
                output_dir=output,
            )

    def test_build_forwards_multiple_languages(self, monkeypatch, tmp_path):
        from codenib import ls_router

        mock_graph = MagicMock()
        mock_graph.graph.vs = [MagicMock()]
        calls = []

        def fake_build_graph_for_languages(*args, **kwargs):
            calls.append((args, kwargs))
            return mock_graph

        monkeypatch.setattr(
            ls_router,
            "build_graph_for_languages",
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
            return mock_graph

        monkeypatch.setattr(
            ls_router,
            "build_graph_for_languages",
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

    def test_build_records_partial_language_coverage(self, monkeypatch, tmp_path):
        from codenib import ls_router
        from codenib.ls_router import GraphBuildResult

        mock_graph = MagicMock()
        mock_graph.graph.vs = list(range(25))
        calls = []

        def fake_build(*args, **kwargs):
            calls.append((args, kwargs))
            return GraphBuildResult(
                graph=mock_graph,
                requested_languages=["python", "cpp"],
                available_languages=["python"],
                failed_languages={"cpp": "compilation database missing"},
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
            "encode_kwargs": {"batch_size": 4},
            "max_seq_length": 8192,
            "revision": "model-commit",
        }

        symbol_graph = registry.get("symbol_graph")
        assert isinstance(symbol_graph, SymbolGraphBuilder)
        assert symbol_graph.language == "rust"
        assert symbol_graph.languages == ["rust", "python"]
        assert symbol_graph.graph_route == "scip-candidate"
        assert symbol_graph.allow_partial_languages is False

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
        assert data["version"] == "1.0"
        assert "bm25" in data["indexes"]

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

    def test_uses_incremental_path_when_head_moved(self, tmp_path):
        first = _git_repo(tmp_path)
        calls: list = []
        compiler = self._compiler(calls)
        compiler.compile_repo(str(tmp_path))
        second = _commit(tmp_path, "b.py")
        assert first != second

        compiler.update_repo(str(tmp_path))
        assert calls[-2] == ("incremental_update", first)

    def test_incremental_graph_preserves_partial_language_coverage(self, tmp_path):
        first = _git_repo(tmp_path)
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

        assert calls == [("build", None), ("incremental_update", first)]
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

        builder.incremental_update = boom

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
        healthy = builder.incremental_update

        def boom(scope, **kwargs):
            raise RuntimeError("builder exploded")

        builder.incremental_update = boom
        compiler.update_repo(str(tmp_path))

        builder.incremental_update = healthy
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
        graph.save_graph.assert_called_once_with(str(out / "graph.pkl"))
