# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for codenib.mcp.context.ServerContext."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codenib.compiler.manifest import IndexEntry, RepoManifest
from codenib.mcp.context import RUNTIME_VIEW_NAMES, ServerContext
from codenib.provider_routes import resolve_inference_route


@pytest.fixture()
def manifest_dir(tmp_path: Path) -> Path:
    """Create a minimal manifest on disk with a BM25 index."""
    bm25_dir = tmp_path / "bm25"
    bm25_dir.mkdir()
    (bm25_dir / "documents.json").write_text(
        json.dumps(
            [
                {
                    "page_content": "def foo(): pass",
                    "metadata": {"node_name": "foo", "type": "function"},
                }
            ]
        )
    )
    (bm25_dir / "bm25_metadata.json").write_text(
        json.dumps({"project_root": str(tmp_path), "max_k": 10, "language": "english"})
    )

    manifest = RepoManifest(
        repo_path=str(tmp_path),
        commit="abc123",
        languages=["python"],
        indexes={
            "bm25": IndexEntry(
                index_type="bm25",
                path=str(bm25_dir),
                built_at="2026-01-01T00:00:00",
                built_at_epoch=1735689600.0,
                status="fresh",
            ),
        },
    )
    manifest.derive_capabilities()
    manifest_path = tmp_path / "repo_manifest.json"
    manifest.save(manifest_path)
    return tmp_path


def _record_view_loads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    for view in ("symbol_graph", "bm25", "regex_index", "zoekt", "vector"):
        monkeypatch.setattr(
            ServerContext,
            f"_load_{view}",
            lambda _self, view=view: calls.append(view),
        )
    return calls


def test_load_defaults_to_all_runtime_views(
    manifest_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_view_loads(monkeypatch)

    ServerContext.load(manifest_dir / "repo_manifest.json")

    assert calls == ["symbol_graph", "bm25", "regex_index", "zoekt", "vector"]
    assert RUNTIME_VIEW_NAMES == frozenset(calls)


def test_load_selects_only_requested_views(
    manifest_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_view_loads(monkeypatch)

    ServerContext.load(manifest_dir / "repo_manifest.json", views={"bm25"})

    assert calls == ["bm25"]


def test_load_views_adds_resources_idempotently(
    manifest_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = ServerContext.load(manifest_dir / "repo_manifest.json", views=())
    loaded_bm25 = object()
    calls = []

    def load_bm25(self):
        calls.append("bm25")
        self.bm25 = loaded_bm25

    monkeypatch.setattr(ServerContext, "_load_bm25", load_bm25)

    assert ctx.load_views({"bm25"}) == {}
    assert ctx.load_views({"bm25"}) == {}

    assert calls == ["bm25"]
    assert ctx.loaded_views == frozenset({"bm25"})


def test_close_releases_live_runtime_resources(manifest_dir: Path) -> None:
    ctx = ServerContext.load(manifest_dir / "repo_manifest.json", views=())
    vector = MagicMock()
    zoekt = MagicMock()
    ctx.vector = vector
    ctx.zoekt = zoekt

    ctx.close()

    assert ctx.vector is None
    assert ctx.zoekt is None
    vector.close.assert_called_once_with()
    zoekt.stop.assert_called_once_with()


def test_load_expands_runtime_view_dependencies(
    manifest_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_view_loads(monkeypatch)

    ServerContext.load(manifest_dir / "repo_manifest.json", views={"regex_index"})

    assert calls == ["symbol_graph", "regex_index"]


@pytest.mark.parametrize("views", ["bm25", {"unknown"}, {1}])
def test_load_rejects_invalid_view_selections(
    manifest_dir: Path,
    views: object,
) -> None:
    expected = ValueError if views == {"unknown"} else TypeError

    with pytest.raises(expected):
        ServerContext.load(manifest_dir / "repo_manifest.json", views=views)


def test_load_with_bm25_only(manifest_dir: Path) -> None:
    """ServerContext loads BM25 when symbol_graph is absent."""
    ctx = ServerContext.load(manifest_dir / "repo_manifest.json")

    assert ctx.bm25 is not None
    assert ctx.symbol_graph is None
    assert ctx.regex_index is None
    assert "symbol_graph" not in ctx.errors


def test_load_missing_bm25_path(tmp_path: Path) -> None:
    """BM25 load failure is recorded in errors, not raised."""
    manifest = RepoManifest(
        repo_path=str(tmp_path),
        indexes={
            "bm25": IndexEntry(
                index_type="bm25",
                path=str(tmp_path / "nonexistent"),
                built_at="2026-01-01T00:00:00",
                built_at_epoch=0.0,
                status="fresh",
            ),
        },
    )
    manifest.save(tmp_path / "repo_manifest.json")

    ctx = ServerContext.load(tmp_path / "repo_manifest.json")
    assert ctx.bm25 is None
    assert "bm25" in ctx.errors


def test_skip_non_fresh_index(tmp_path: Path) -> None:
    """Indexes with status != 'fresh' are skipped."""
    manifest = RepoManifest(
        repo_path=str(tmp_path),
        indexes={
            "bm25": IndexEntry(
                index_type="bm25",
                path=str(tmp_path / "bm25"),
                built_at="2026-01-01T00:00:00",
                built_at_epoch=0.0,
                status="failed",
            ),
        },
    )
    manifest.save(tmp_path / "repo_manifest.json")

    ctx = ServerContext.load(tmp_path / "repo_manifest.json")
    assert ctx.bm25 is None
    assert "bm25" not in ctx.errors


def test_load_vector_accepts_compiler_manifest_identity(tmp_path: Path) -> None:
    vector_dir = tmp_path / "vector"
    vector_dir.mkdir()
    manifest = RepoManifest(
        repo_path=str(tmp_path),
        indexes={
            "vector": IndexEntry(
                index_type="vector",
                path=str(vector_dir),
                built_at="2026-01-01T00:00:00",
                built_at_epoch=0.0,
                status="fresh",
                config={
                    "embedding_model": "test-model",
                    "embedding_provider": "huggingface",
                    "embedding_dimension": 384,
                    "embedding_kwargs": {
                        "max_seq_length": 8192,
                        "revision": "model-commit",
                    },
                    "index_metric": "ip",
                },
            ),
        },
    )
    manifest.save(tmp_path / "repo_manifest.json")

    vector = MagicMock()
    vector.embedding_model = "test-model"
    vector.get_stats.return_value = {"total_documents": 3}
    with patch(
        "codenib.index.embedding.vector_store.CodeVectorStore",
        return_value=vector,
    ) as cls:
        ctx = ServerContext.load(tmp_path / "repo_manifest.json")

    cls.assert_called_once_with(
        embedding_model="test-model",
        embedding_provider="huggingface",
        dimension=384,
        index_metric="ip",
        store_path=str(vector_dir),
        artifact_metadata={
            "embedding_model": "test-model",
            "embedding_provider": "huggingface",
            "embedding_dimension": 384,
            "embedding_kwargs": {
                "max_seq_length": 8192,
                "revision": "model-commit",
            },
            "index_metric": "ip",
        },
        max_seq_length=8192,
        revision="model-commit",
    )
    vector.load.assert_called_once_with()
    assert ctx.vector is vector
    assert "vector" not in ctx.errors


def test_validate_views_probes_vector_without_loading_embedding_model(
    tmp_path: Path,
) -> None:
    vector_dir = tmp_path / "vector"
    vector_dir.mkdir()
    manifest = RepoManifest(
        repo_path=str(tmp_path),
        indexes={
            "vector": IndexEntry(
                index_type="vector",
                path=str(vector_dir),
                built_at="2026-01-01T00:00:00",
                built_at_epoch=0.0,
                status="fresh",
                config={
                    "embedding_model": "test-model",
                    "embedding_provider": "huggingface",
                    "embedding_dimension": 384,
                },
            ),
        },
    )
    vector = MagicMock()
    vector.embedding_model = "test-model"
    vector.get_stats.return_value = {"total_documents": 3}

    with patch(
        "codenib.index.embedding.vector_store.CodeVectorStore",
        return_value=vector,
    ) as cls:
        errors = ServerContext.validate_views(manifest, views={"vector"})

    assert errors == {}
    assert cls.call_args.kwargs["embedding"].dimension == 384


def test_load_vector_rebinds_openai_credential_without_persisting_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vector_dir = tmp_path / "vector"
    vector_dir.mkdir()
    route = resolve_inference_route(
        operation="embeddings",
        provider="openai",
        model="text-embedding-3-small",
        dimension=1536,
        environ={},
    )
    config = {
        "builder_schema": 3,
        "embedding_model": route.model,
        "embedding_provider": route.provider,
        "embedding_dimension": route.dimension,
        "dimension": route.dimension,
        "embedding_endpoint": route.endpoint,
        "embedding_kwargs": route.compatibility_options,
        "embedding_route": route.public_identity(),
        "embedding_fingerprint": route.compatibility_fingerprint,
    }
    manifest = RepoManifest(
        repo_path=str(tmp_path),
        indexes={
            "vector": IndexEntry(
                index_type="vector",
                path=str(vector_dir),
                built_at="2026-01-01T00:00:00",
                built_at_epoch=0.0,
                status="fresh",
                config=config,
            ),
        },
    )
    manifest.save(tmp_path / "repo_manifest.json")
    monkeypatch.setenv("OPENAI_API_KEY", "runtime-secret")

    vector = MagicMock()
    vector.embedding_model = route.model
    vector.get_stats.return_value = {"total_documents": 3}
    with patch(
        "codenib.index.embedding.vector_store.CodeVectorStore",
        return_value=vector,
    ) as cls:
        ctx = ServerContext.load(tmp_path / "repo_manifest.json")

    kwargs = cls.call_args.kwargs
    assert kwargs["embedding_provider"] == "openai"
    assert "base_url" not in kwargs
    assert kwargs["api_key"] == "runtime-secret"
    assert "runtime-secret" not in json.dumps(config)
    assert ctx.vector is vector


def test_validate_views_does_not_require_remote_embedding_credentials(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vector_dir = tmp_path / "vector"
    vector_dir.mkdir()
    route = resolve_inference_route(
        operation="embeddings",
        provider="openai",
        model="text-embedding-3-small",
        dimension=1536,
        environ={},
    )
    config = {
        "builder_schema": 3,
        "embedding_model": route.model,
        "embedding_provider": route.provider,
        "embedding_dimension": route.dimension,
        "dimension": route.dimension,
        "embedding_endpoint": route.endpoint,
        "embedding_kwargs": {},
        "embedding_route": route.public_identity(),
        "embedding_fingerprint": route.compatibility_fingerprint,
    }
    manifest = RepoManifest(
        repo_path=str(tmp_path),
        indexes={
            "vector": IndexEntry(
                index_type="vector",
                path=str(vector_dir),
                built_at="2026-01-01T00:00:00",
                built_at_epoch=0.0,
                status="fresh",
                config=config,
            ),
        },
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    vector = MagicMock()
    vector.embedding_model = route.model
    vector.get_stats.return_value = {"total_documents": 3}

    with patch(
        "codenib.index.embedding.vector_store.CodeVectorStore",
        return_value=vector,
    ) as cls:
        errors = ServerContext.validate_views(manifest, views={"vector"})

    assert errors == {}
    assert "api_key" not in cls.call_args.kwargs
    assert cls.call_args.kwargs["embedding"].dimension == 1536
    vector.load.assert_called_once_with()
    vector.close.assert_called_once_with()


def test_validate_views_rejects_empty_vector_artifact(tmp_path: Path) -> None:
    vector_dir = tmp_path / "vector"
    vector_dir.mkdir()
    manifest = RepoManifest(
        repo_path=str(tmp_path),
        indexes={
            "vector": IndexEntry(
                index_type="vector",
                path=str(vector_dir),
                built_at="2026-01-01T00:00:00",
                built_at_epoch=0.0,
                status="fresh",
                config={
                    "embedding_model": "test-model",
                    "embedding_provider": "huggingface",
                    "embedding_dimension": 384,
                },
            ),
        },
    )
    vector = MagicMock()
    vector.embedding_model = "test-model"
    vector.get_stats.return_value = {"total_documents": 0}

    with patch(
        "codenib.index.embedding.vector_store.CodeVectorStore",
        return_value=vector,
    ):
        errors = ServerContext.validate_views(manifest, views={"vector"})

    assert errors == {"vector": "vector index contains no documents"}
    vector.close.assert_called_once_with()


def test_regex_index_built_when_graph_available(manifest_dir: Path) -> None:
    """RegexNodeIndex is built when symbol_graph loads successfully."""
    graph_dir = manifest_dir / "symbol_graph"
    graph_dir.mkdir()

    mock_graph = MagicMock()
    mock_vs = [MagicMock()]
    mock_vs[0].index = 0
    mock_vs[0].__getitem__ = lambda self, key: "my_func" if key == "name" else None
    mock_vs[0].attributes.return_value = {
        "type": "function",
        "file": "main.py",
        "start_line": 1,
        "end_line": 5,
    }
    mock_graph.get_graph.return_value.vs = mock_vs
    mock_graph.get_node_content.return_value = "def my_func(): pass"

    manifest = RepoManifest.load(manifest_dir / "repo_manifest.json")
    manifest.indexes["symbol_graph"] = IndexEntry(
        index_type="symbol_graph",
        path=str(graph_dir / "graph.pkl"),
        built_at="2026-01-01T00:00:00",
        built_at_epoch=0.0,
        status="fresh",
    )
    manifest.save(manifest_dir / "repo_manifest.json")

    with patch(
        "codenib.graph.code_graph.CodeGraph.load_graph",
        return_value=mock_graph,
    ):
        ctx = ServerContext.load(manifest_dir / "repo_manifest.json")

    assert ctx.symbol_graph is mock_graph
    assert ctx.regex_index is not None
    assert len(ctx.regex_index.nodes) == 1


# ------------------------------------------------------------------
# Zoekt loading
# ------------------------------------------------------------------


def _add_zoekt_entry(manifest_path: Path, shard_dir: Path) -> None:
    manifest = RepoManifest.load(manifest_path)
    manifest.indexes["zoekt"] = IndexEntry(
        index_type="zoekt",
        path=str(shard_dir),
        built_at="2026-01-01T00:00:00",
        built_at_epoch=0.0,
        status="fresh",
    )
    manifest.save(manifest_path)


def test_zoekt_started_when_entry_fresh(manifest_dir: Path) -> None:
    """When the manifest declares a fresh zoekt entry, the searcher is started."""
    shard_dir = manifest_dir / "zoekt"
    shard_dir.mkdir()
    _add_zoekt_entry(manifest_dir / "repo_manifest.json", shard_dir)

    fake_searcher = MagicMock()
    fake_searcher.port = 9999

    with patch(
        "codenib.index.trigram.ZoektSearcher",
        return_value=fake_searcher,
    ):
        ctx = ServerContext.load(manifest_dir / "repo_manifest.json")

    fake_searcher.start.assert_called_once()
    assert ctx.zoekt is fake_searcher
    assert "zoekt" not in ctx.errors


def test_validate_views_stops_zoekt_probe(manifest_dir: Path) -> None:
    shard_dir = manifest_dir / "zoekt"
    shard_dir.mkdir()
    manifest_path = manifest_dir / "repo_manifest.json"
    _add_zoekt_entry(manifest_path, shard_dir)
    fake_searcher = MagicMock()
    fake_searcher.port = 9999

    with patch(
        "codenib.index.trigram.ZoektSearcher",
        return_value=fake_searcher,
    ):
        errors = ServerContext.validate_views(manifest_path, views={"zoekt"})

    assert errors == {}
    fake_searcher.start.assert_called_once_with()
    fake_searcher.stop.assert_called_once_with()


def test_zoekt_unavailable_recorded_in_errors(manifest_dir: Path) -> None:
    """If the zoekt binary is missing, ServerContext records the error and keeps zoekt=None."""
    from codenib.index.trigram import ZoektUnavailableError

    shard_dir = manifest_dir / "zoekt"
    shard_dir.mkdir()
    _add_zoekt_entry(manifest_dir / "repo_manifest.json", shard_dir)

    fake_searcher = MagicMock()
    fake_searcher.start.side_effect = ZoektUnavailableError("binary not found")

    with patch(
        "codenib.index.trigram.ZoektSearcher",
        return_value=fake_searcher,
    ):
        ctx = ServerContext.load(manifest_dir / "repo_manifest.json")

    assert ctx.zoekt is None
    assert "zoekt" in ctx.errors
    assert "binary not found" in ctx.errors["zoekt"]


def test_zoekt_skipped_when_entry_absent(manifest_dir: Path) -> None:
    ctx = ServerContext.load(manifest_dir / "repo_manifest.json")
    assert ctx.zoekt is None
    assert "zoekt" not in ctx.errors


def test_zoekt_skipped_when_entry_failed(manifest_dir: Path) -> None:
    shard_dir = manifest_dir / "zoekt"
    shard_dir.mkdir()
    manifest = RepoManifest.load(manifest_dir / "repo_manifest.json")
    manifest.indexes["zoekt"] = IndexEntry(
        index_type="zoekt",
        path=str(shard_dir),
        built_at="2026-01-01T00:00:00",
        built_at_epoch=0.0,
        status="failed",
    )
    manifest.save(manifest_dir / "repo_manifest.json")

    with patch("codenib.index.trigram.ZoektSearcher") as mock_cls:
        ctx = ServerContext.load(manifest_dir / "repo_manifest.json")

    mock_cls.assert_not_called()
    assert ctx.zoekt is None
    assert "zoekt" not in ctx.errors
