# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for codenib.mcp.context.ServerContext."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codenib.compiler.artifact_fingerprints import bm25_artifact_file_fingerprints
from codenib.compiler.manifest import IndexEntry, RepoManifest
from codenib.index.embedding.artifact_integrity import (
    capture_authenticated_vector_view,
)
from codenib.mcp.context import RUNTIME_VIEW_NAMES, ServerContext
from codenib.native_index_authorization import (
    _mint_trusted_local_admin_authorization,
)
from codenib.provider_routes import resolve_inference_route
from codenib.source_fingerprint import (
    capture_repository_source,
    fingerprint_repository,
)


class _PendingSourceOwner:
    def __init__(self, source: object) -> None:
        self.source = source
        self.closed = False

    @property
    def pending_sources(self) -> tuple[object, ...]:
        return () if self.closed else (self.source,)

    def close(self) -> None:
        self.closed = True


def _authorize_test_vector(vector_dir: Path, semantic_contract):
    with capture_authenticated_vector_view(vector_dir) as vector_view:
        return _mint_trusted_local_admin_authorization(
            vector_view.ownership,
            view_type="vector",
            semantic_contract=semantic_contract,
            evidence=(
                "mcp-context-test-local-admin",
                "captured-vector-tree-subject",
            ),
        )


def _portable_vector_manifest(tmp_path: Path) -> tuple[RepoManifest, Path]:
    vector_dir = tmp_path / "portable-vector"
    vector_dir.mkdir()
    config = {
        "embedding_model": "test-model",
        "embedding_provider": "huggingface",
        "embedding_dimension": 4,
        "dimension": 4,
        "embedding_kwargs": {},
    }
    return (
        RepoManifest(
            repo_path=str(tmp_path),
            indexes={
                "vector": IndexEntry(
                    index_type="vector",
                    path=str(vector_dir),
                    built_at="2026-08-11T00:00:00Z",
                    built_at_epoch=0.0,
                    status="fresh",
                    config=config,
                )
            },
        ),
        vector_dir,
    )


def test_server_context_rejects_mismatched_source_authority(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "a.py").write_bytes(b"A\n")
    (second / "a.py").write_bytes(b"A\n")
    binding = capture_repository_source(first)
    identity = fingerprint_repository(second)
    manifest = RepoManifest(
        repo_path=str(second),
        source_fingerprint=identity.value,
        file_count=identity.file_count,
    )

    with pytest.raises(ValueError, match="does not match the manifest"):
        ServerContext.load(manifest, views=[], source_binding=binding)

    assert not binding.usable


def test_server_context_load_failure_closes_transferred_source_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_bytes(b"A\n")
    binding = capture_repository_source(repo)
    manifest = RepoManifest(
        repo_path=str(repo),
        source_fingerprint=binding.fingerprint,
        file_count=binding.file_count,
    )

    def fail_load_views(self, _views):
        raise RuntimeError("injected load failure")

    monkeypatch.setattr(ServerContext, "load_views", fail_load_views)
    with pytest.raises(RuntimeError, match="injected load failure"):
        ServerContext.load(manifest, views=[], source_binding=binding)

    assert not binding.usable


def test_server_context_provider_failure_closes_transferred_source_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_bytes(b"A\n")
    binding = capture_repository_source(repo)
    manifest = RepoManifest(
        repo_path=str(repo),
        source_fingerprint=binding.fingerprint,
        file_count=binding.file_count,
    )

    def fail_provider_configuration(self, **_kwargs):
        raise RuntimeError("injected provider configuration failure")

    monkeypatch.setattr(
        ServerContext,
        "configure_lsp_provider",
        fail_provider_configuration,
    )
    with pytest.raises(RuntimeError, match="provider configuration failure"):
        ServerContext.load(manifest, views=[], source_binding=binding)

    assert not binding.usable


def test_init_server_provider_reconfiguration_failure_closes_new_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.mcp import server as server_module

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_bytes(b"A\n")
    identity = fingerprint_repository(repo)
    manifest_path = tmp_path / "repo_manifest.json"
    RepoManifest(
        repo_path=str(repo),
        source_fingerprint=identity.value,
        file_count=identity.file_count,
        languages=["cpp"],
    ).save(manifest_path)
    captured = []
    configure_calls = []

    def capture(*args, **kwargs):
        source = capture_repository_source(*args, **kwargs)
        captured.append(source)
        return source

    def configure(
        self,
        *,
        allow_native: bool,
        native_disabled_reason: str = "native_provider_not_authorized",
    ):
        configure_calls.append((allow_native, native_disabled_reason))
        self._lsp_allow_native = allow_native
        self._lsp_native_disabled_reason = native_disabled_reason
        if allow_native:
            raise RuntimeError("injected native provider configuration failure")
        self.lsp_provider = None
        self.lsp_provider_selection = {"status": "unavailable"}
        return dict(self.lsp_provider_selection)

    monkeypatch.setattr(
        "codenib.source_fingerprint.capture_repository_source",
        capture,
    )
    monkeypatch.setattr(ServerContext, "configure_lsp_provider", configure)
    previous_context = object()
    monkeypatch.setattr(server_module, "_ctx", previous_context)

    with pytest.raises(RuntimeError, match="native provider configuration failure"):
        server_module.init_server(manifest_path)

    assert configure_calls == [
        (False, "local_source_not_verified"),
        (False, "local_source_not_verified"),
        (True, "local_source_not_verified"),
    ]
    assert len(captured) == 1
    assert captured[0].closed
    assert server_module._ctx is previous_context


@pytest.mark.parametrize(
    "interruption",
    [
        pytest.param(KeyboardInterrupt("context close interrupted"), id="keyboard"),
        pytest.param(SystemExit("context close exited"), id="system-exit"),
    ],
)
def test_server_context_mismatch_propagates_after_close_cancellation_and_old_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: BaseException,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "a.py").write_bytes(b"A\n")
    (second / "a.py").write_bytes(b"A\n")
    binding = capture_repository_source(first)
    identity = fingerprint_repository(second)
    manifest = RepoManifest(
        repo_path=str(second),
        source_fingerprint=identity.value,
        file_count=identity.file_count,
    )
    old_source = object()
    old_owner = _PendingSourceOwner(old_source)
    method = ServerContext.load.__func__
    source_type = type(binding)
    real_close = source_type.close
    close_calls = 0
    injected_owner = False

    def close_then_interrupt(source) -> None:
        nonlocal close_calls
        close_calls += 1
        real_close(source)
        raise interruption

    def attach_old_owner(frame, event, arg):
        nonlocal injected_owner
        if (
            not injected_owner
            and frame.f_code is method.__code__
            and event == "exception"
            and isinstance(arg[1], ValueError)
            and "does not match the manifest" in str(arg[1])
        ):
            injected_owner = True
            arg[1].source_cleanup_owner = old_owner
        return attach_old_owner

    monkeypatch.setattr(source_type, "close", close_then_interrupt)
    sys.settrace(attach_old_owner)
    try:
        with pytest.raises(type(interruption)) as caught:
            ServerContext.load(manifest, views=[], source_binding=binding)
    finally:
        sys.settrace(None)

    assert caught.value is interruption
    assert injected_owner
    assert close_calls == 1
    assert binding.closed
    assert caught.value.source_cleanup_owner is old_owner

    old_owner.close()
    assert old_owner.closed


def test_server_context_mismatch_merges_old_owner_with_persistent_close_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "a.py").write_bytes(b"A\n")
    (second / "a.py").write_bytes(b"A\n")
    binding = capture_repository_source(first)
    identity = fingerprint_repository(second)
    manifest = RepoManifest(
        repo_path=str(second),
        source_fingerprint=identity.value,
        file_count=identity.file_count,
    )
    old_source = object()
    old_owner = _PendingSourceOwner(old_source)
    method = ServerContext.load.__func__
    source_type = type(binding)
    real_close = source_type.close
    primary_error: BaseException | None = None

    def persistent_close(_source) -> None:
        raise OSError("injected persistent context close EIO")

    def attach_old_owner(frame, event, arg):
        nonlocal primary_error
        if (
            primary_error is None
            and frame.f_code is method.__code__
            and event == "exception"
            and isinstance(arg[1], ValueError)
            and "does not match the manifest" in str(arg[1])
        ):
            primary_error = arg[1]
            arg[1].source_cleanup_owner = old_owner
        return attach_old_owner

    monkeypatch.setattr(source_type, "close", persistent_close)
    sys.settrace(attach_old_owner)
    try:
        with pytest.raises(ValueError, match="does not match the manifest") as caught:
            ServerContext.load(manifest, views=[], source_binding=binding)
    finally:
        sys.settrace(None)

    assert caught.value is primary_error
    retry_owner = caught.value.source_cleanup_owner
    assert set(retry_owner.pending_sources) == {old_source, binding}
    assert not binding.closed

    monkeypatch.setattr(source_type, "close", real_close)
    retry_owner.close()
    assert retry_owner.closed
    assert old_owner.closed
    assert binding.closed


def test_local_bm25_without_source_binding_is_persisted_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codenib.index.sparse_idx.bm25_index as bm25_module

    view = tmp_path / "bm25"
    view.mkdir()
    secret = tmp_path / "secret.py"
    secret.write_text("API_KEY = 'LOCAL_BM25_SECRET'\n", encoding="utf-8")
    (view / "documents.json").write_text(
        json.dumps(
            [
                {
                    "page_content": "safe persisted needle",
                    "metadata": {
                        "file": str(secret),
                        "name": "needle",
                        "node_id": "secret.py:needle",
                        "type": "variable",
                        "start_line": 0,
                        "end_line": 0,
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    (view / "bm25_metadata.json").write_text(
        json.dumps(
            {
                "project_root": str(tmp_path),
                "max_k": 10,
                "language": "english",
            }
        ),
        encoding="utf-8",
    )
    legacy = "sha256:" + "a" * 64
    manifest = RepoManifest(
        repo_path=str(tmp_path),
        source_fingerprint=legacy,
        last_indexed_source_fingerprint=legacy,
        indexes={
            "bm25": IndexEntry(
                index_type="bm25",
                path=str(view),
                built_at="2026-01-01T00:00:00+00:00",
                built_at_epoch=1.0,
                status="fresh",
                source_fingerprint=legacy,
                config={
                    "artifact_file_fingerprints": bm25_artifact_file_fingerprints(view)
                },
            )
        },
    )
    monkeypatch.setattr(
        bm25_module,
        "_read_source_text",
        lambda *_args: pytest.fail("unbound BM25 must not read live source"),
    )

    context = ServerContext.load(manifest, views={"bm25"})

    assert context.bm25 is not None
    assert context.bm25.source_mode == bm25_module.SOURCE_MODE_PERSISTED_ONLY
    results = context.bm25.search("needle", return_code_content=True)
    assert results and results[0].content is None
    assert "LOCAL_BM25_SECRET" not in repr(results)


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


def test_portable_artifact_context_never_loads_graph_or_other_native_views(
    manifest_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_view_loads(monkeypatch)

    with patch("codenib.ls_router.LSIndexer") as indexer:
        ctx = ServerContext.load(
            manifest_dir / "repo_manifest.json",
            artifact={"schema": "codenib.context-artifact.v1"},
        )

    indexer.assert_not_called()
    assert calls == ["bm25", "vector"]
    assert ctx.lsp_provider is None
    assert ctx.lsp_provider_selection["backend"] == "unavailable"
    assert ctx.lsp_provider_selection["fallback_reason"] == (
        "portable_artifact_uses_persisted_graph"
    )
    with pytest.raises(ValueError, match="cannot load native views"):
        ctx.load_views({"symbol_graph"})


def test_empty_portable_artifact_context_stays_native_view_inert(
    manifest_dir: Path,
) -> None:
    ctx = ServerContext.load(
        manifest_dir / "repo_manifest.json",
        views=(),
        artifact={},
    )

    assert ctx.artifact == {}
    with pytest.raises(
        ValueError,
        match="portable artifact contexts cannot load native views: symbol_graph",
    ):
        ctx.load_views({"symbol_graph"})


@pytest.mark.parametrize("origin", ["plain", "binding"])
def test_portable_artifact_eager_load_rejects_exact_native_authority_before_parser(
    tmp_path: Path,
    origin: str,
) -> None:
    manifest, vector_dir = _portable_vector_manifest(tmp_path)
    authorization = _authorize_test_vector(
        vector_dir,
        manifest.indexes["vector"].config,
    )
    origin_kwargs = (
        {"artifact": {"schema": "codenib.context-artifact.v1"}}
        if origin == "plain"
        else {"artifact_binding": MagicMock()}
    )

    with (
        patch("codenib.mcp.context.require_authorized_vector_view") as exact_gate,
        patch(
            "codenib.index.embedding.vector_store.CodeVectorStore"
        ) as vector_constructor,
        patch("codenib.index.embedding.vector_store.faiss.read_index") as faiss_parser,
        patch(
            "codenib.index.embedding.vector_store.compat_pickle.load"
        ) as pickle_parser,
    ):
        with pytest.raises(
            ValueError,
            match="portable artifact contexts cannot authorize native vector",
        ):
            ServerContext.load(
                manifest,
                views={"vector"},
                native_index_authorization=authorization,
                **origin_kwargs,
            )

    exact_gate.assert_not_called()
    vector_constructor.assert_not_called()
    faiss_parser.assert_not_called()
    pickle_parser.assert_not_called()


def test_plain_portable_artifact_lazy_load_stays_inert_with_exact_authority(
    tmp_path: Path,
) -> None:
    manifest, vector_dir = _portable_vector_manifest(tmp_path)
    authorization = _authorize_test_vector(
        vector_dir,
        manifest.indexes["vector"].config,
    )
    context = ServerContext.load(
        manifest,
        views=(),
        artifact={"schema": "codenib.context-artifact.v1"},
    )

    with (
        patch("codenib.mcp.context.require_authorized_vector_view") as exact_gate,
        patch(
            "codenib.index.embedding.vector_store.CodeVectorStore"
        ) as vector_constructor,
        patch("codenib.index.embedding.vector_store.faiss.read_index") as faiss_parser,
        patch(
            "codenib.index.embedding.vector_store.compat_pickle.load"
        ) as pickle_parser,
    ):
        with pytest.raises(
            ValueError,
            match="portable artifact contexts cannot authorize native parsing",
        ):
            context.load_views(
                {"vector"},
                native_index_authorization=authorization,
            )

        assert context._native_index_authorization is None
        errors = context.load_views({"vector"})

    assert "external authorization" in errors["vector"]
    exact_gate.assert_not_called()
    vector_constructor.assert_not_called()
    faiss_parser.assert_not_called()
    pickle_parser.assert_not_called()


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


def test_load_views_rebinds_persisted_provider_after_lazy_graph_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = RepoManifest(repo_path=str(tmp_path), languages=["cpp"])
    ctx = ServerContext.load(manifest, views=())
    graph = object()

    def load_graph(self):
        self.symbol_graph = graph

    monkeypatch.setattr(ServerContext, "_load_symbol_graph", load_graph)

    assert ctx.lsp_provider is None
    assert ctx.lsp_provider_selection["status"] == "unavailable"
    assert ctx.load_views({"symbol_graph"}) == {}
    assert ctx.lsp_provider.graph is graph
    assert ctx.lsp_provider_selection == {
        "provider": "codenib_static_index",
        "backend": "persisted-symbol-graph-v1",
        "status": "ok",
        "index_snapshot": "symbol_graph:unknown:0:0",
        "fallback_reason": "local_source_not_verified",
        "capabilities": {
            "definition": True,
            "references": True,
            "route": True,
        },
    }


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


def test_load_rejects_bm25_that_no_longer_matches_manifest(
    manifest_dir: Path,
) -> None:
    manifest_path = manifest_dir / "repo_manifest.json"
    manifest = RepoManifest.load(manifest_path)
    entry = manifest.indexes["bm25"]
    entry.config["artifact_file_fingerprints"] = bm25_artifact_file_fingerprints(
        entry.path
    )
    manifest.save(manifest_path)
    documents = Path(entry.path) / "documents.json"
    documents.write_text(documents.read_text().replace("foo", "bar"))

    ctx = ServerContext.load(manifest_path)

    assert ctx.bm25 is None
    assert "manifest fingerprints" in ctx.errors["bm25"]


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
    authorization = _authorize_test_vector(
        vector_dir,
        manifest.indexes["vector"].config,
    )
    with patch(
        "codenib.index.embedding.vector_store.CodeVectorStore",
        return_value=vector,
    ) as cls:
        ctx = ServerContext.load(
            tmp_path / "repo_manifest.json",
            native_index_authorization=authorization,
        )

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
    vector.load.assert_called_once_with(
        native_index_authorization=authorization,
    )
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
    authorization = _authorize_test_vector(
        vector_dir,
        manifest.indexes["vector"].config,
    )

    with patch(
        "codenib.index.embedding.vector_store.CodeVectorStore",
        return_value=vector,
    ) as cls:
        errors = ServerContext.validate_views(
            manifest,
            views={"vector"},
            native_index_authorization=authorization,
        )

    assert errors == {}
    assert cls.call_args.kwargs["embedding"].dimension == 384
    vector.load.assert_called_once_with(
        native_index_authorization=authorization,
    )


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
    authorization = _authorize_test_vector(
        vector_dir,
        manifest.indexes["vector"].config,
    )
    with patch(
        "codenib.index.embedding.vector_store.CodeVectorStore",
        return_value=vector,
    ) as cls:
        ctx = ServerContext.load(
            tmp_path / "repo_manifest.json",
            native_index_authorization=authorization,
        )

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
    authorization = _authorize_test_vector(
        vector_dir,
        manifest.indexes["vector"].config,
    )

    with patch(
        "codenib.index.embedding.vector_store.CodeVectorStore",
        return_value=vector,
    ) as cls:
        errors = ServerContext.validate_views(
            manifest,
            views={"vector"},
            native_index_authorization=authorization,
        )

    assert errors == {}
    assert "api_key" not in cls.call_args.kwargs
    assert cls.call_args.kwargs["embedding"].dimension == 1536
    vector.load.assert_called_once_with(
        native_index_authorization=authorization,
    )
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
    authorization = _authorize_test_vector(
        vector_dir,
        manifest.indexes["vector"].config,
    )

    with patch(
        "codenib.index.embedding.vector_store.CodeVectorStore",
        return_value=vector,
    ):
        errors = ServerContext.validate_views(
            manifest,
            views={"vector"},
            native_index_authorization=authorization,
        )

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
