# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Graph-free and atomic-publication tests for the SCIP query provider."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from codenib.agent.lsp_provider import LSPProviderNodes, StaticLSPProvider
from codenib.compiler.artifact_fingerprints import regular_file_fingerprint
from codenib.compiler.manifest import MANIFEST_VERSION, IndexEntry, RepoManifest
from codenib.compiler.manifest_source import repository_filter_policy_for_manifest
from codenib.graph.code_graph import CodeGraph
from codenib.mcp.tools.lsp import (
    lsp_definition_impl,
    lsp_references_impl,
    lsp_route_impl,
)
from codenib.repository_source_selection import RepositorySourceSelection
from codenib.scip_interface.query_surface import query_surface_sha256
from codenib.scip_interface.scip_query import (
    FACT_QUERY_CAPABILITIES,
    FactQueryCandidate,
    FactQueryCandidateRejected,
    _allowed_files_digest,
    _source_and_allowed_files,
    observe_fact_query_snapshot,
)
from codenib.scip_interface.scip_query_provider import (
    FAILED,
    GRAPH_READY,
    NATIVE_READY,
    SCIPHybridQueryProvider,
    scip_consumer_query_mode,
    select_scip_query_provider,
)
from codenib.source_fingerprint import fingerprint_repository
from codenib.types import EDGE_TYPE_CONTAIN, EDGE_TYPE_REFERENCE


class _NativeIndex:
    """FactQueryIndex-shaped view over a fixture graph with call counters."""

    fact_query_index = True
    materializes_graph = False
    supports_graph_routes = False
    capabilities = FACT_QUERY_CAPABILITIES

    def __init__(self, graph: CodeGraph) -> None:
        self._fixture = graph
        self.resolve_calls = 0

    def resolve_symbol_candidates(self, symbol: str, limit: int = 8) -> list[str]:
        self.resolve_calls += 1
        from codenib.agent.lsp_graph import resolve_symbol_candidates

        return resolve_symbol_candidates(self._fixture, symbol, limit)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._fixture, name)


def _graph(root: Path) -> CodeGraph:
    graph = CodeGraph(str(root))
    graph._add_vertex(str(root), {"type": "root"})
    graph._add_vertex("caller.py", {"type": "file", "file": "caller.py"})
    graph._add_vertex("target.py", {"type": "file", "file": "target.py"})
    graph._add_vertex(
        "demo/caller().",
        {
            "type": "function",
            "file": "caller.py",
            "start_line": 0,
            "end_line": 5,
            "selection_line": 0,
            "unified_name": "caller()",
            "symbol_kind": "function",
            "has_definition": True,
        },
    )
    graph._add_vertex(
        "demo/target().",
        {
            "type": "function",
            "file": "target.py",
            "start_line": 0,
            "end_line": 2,
            "selection_line": 0,
            "unified_name": "target()",
            "symbol_kind": "function",
            "has_definition": True,
        },
    )
    graph._add_edge(str(root), "caller.py", EDGE_TYPE_CONTAIN)
    graph._add_edge(str(root), "target.py", EDGE_TYPE_CONTAIN)
    graph._add_edge("caller.py", "demo/caller().", EDGE_TYPE_CONTAIN)
    graph._add_edge("target.py", "demo/target().", EDGE_TYPE_CONTAIN)
    graph._add_edge(
        "demo/caller().",
        "demo/target().",
        EDGE_TYPE_REFERENCE,
        anchor_file="caller.py",
        anchor_line=2,
    )
    graph.build_range_indexes()
    return graph


def _candidate(graph: CodeGraph, root: Path) -> tuple[FactQueryCandidate, _NativeIndex]:
    artifact_root = root / "artifact" / "symbol_graph"
    artifact_root.mkdir(parents=True)
    native = _NativeIndex(graph)
    receipt = {
        "schema_version": 1,
        "language": "python",
        "project_root": str(root),
        "manifest": {
            "entry_path": str(artifact_root),
            "node_count": graph.graph.vcount(),
            "edge_count": graph.graph.ecount(),
        },
        "graph": {
            "relative_path": "graph.pkl",
            "query_surface_sha256": query_surface_sha256(graph),
        },
        "receipt_sha256": "f" * 64,
    }
    return FactQueryCandidate(index=native, receipt=receipt, timings={}), native


def _provider(
    graph: CodeGraph,
    root: Path,
    *,
    graph_loader=None,
    snapshot_observer=None,
    public_fallback_reason: str | None = None,
) -> tuple[SCIPHybridQueryProvider, _NativeIndex]:
    candidate, native = _candidate(graph, root)
    provider = SCIPHybridQueryProvider(
        candidate,
        graph_loader=graph_loader or (lambda _path: graph),
        snapshot_observer=snapshot_observer or (lambda _receipt: {"generation": 1}),
        public_fallback_reason=public_fallback_reason,
    )
    return provider, native


def test_default_graph_loader_uses_authenticated_artifact_receipt(
    tmp_path: Path,
) -> None:
    graph = _graph(tmp_path)
    candidate, native = _candidate(graph, tmp_path)
    graph_path = Path(candidate.receipt["manifest"]["entry_path"]) / "graph.pkl"
    graph.save_graph(str(graph_path))
    payload = graph_path.read_bytes()
    candidate.receipt["graph"].update(
        {
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    )
    provider = SCIPHybridQueryProvider(
        FactQueryCandidate(
            index=native,
            receipt=candidate.receipt,
            timings={},
        ),
        snapshot_observer=lambda _receipt: {"generation": 1},
    )

    result = provider.definition(file_path="caller.py", line=2, top_k=8)

    assert result
    assert provider.diagnostics()["state"] == GRAPH_READY


def _serialized(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def test_provider_module_import_does_not_import_graph_or_igraph() -> None:
    script = """
import sys
from codenib.scip_interface.scip_query_provider import (
    SCIPHybridQueryProvider,
    scip_consumer_query_mode,
)
assert SCIPHybridQueryProvider.__name__ == 'SCIPHybridQueryProvider'
assert scip_consumer_query_mode({}) == 'off'
for name in ('igraph', 'codenib.graph.code_graph', 'codenib.ls_router'):
    assert name not in sys.modules, name
"""
    subprocess.run([sys.executable, "-c", script], check=True, text=True)


def test_symbol_mcp_results_and_public_metadata_are_byte_exact(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    fallback_reason = "scip_fact_query_consumer_measurement"
    provider, native = _provider(
        graph,
        tmp_path,
        public_fallback_reason=fallback_reason,
    )
    snapshot_id = (
        f"symbol_graph:{tmp_path}:{graph.graph.vcount()}:{graph.graph.ecount()}"
    )
    legacy_provider = StaticLSPProvider(
        graph,
        snapshot_id=snapshot_id,
        backend="persisted-symbol-graph-v1",
        fallback_reason=fallback_reason,
    )
    legacy = SimpleNamespace(lsp_provider=legacy_provider, symbol_graph=graph)
    candidate = SimpleNamespace(lsp_provider=provider, symbol_graph=None)

    calls = (
        lambda context: lsp_definition_impl(context, symbol="demo/target().", top_k=8),
        lambda context: lsp_references_impl(
            context,
            symbol="target",
            include_declaration=False,
            top_k=40,
        ),
        lambda context: lsp_references_impl(
            context,
            symbol="`target`",
            include_declaration=True,
            top_k=100,
        ),
        lambda context: lsp_definition_impl(context, symbol="missing", top_k=8),
    )
    for call in calls:
        assert _serialized(call(candidate)) == _serialized(call(legacy))
    for capability in ("definition", "references", "route", "unsupported"):
        assert _serialized(provider.can_serve(capability).to_dict()) == _serialized(
            legacy_provider.can_serve(capability).to_dict()
        )

    assert native.resolve_calls > 0
    assert provider.diagnostics()["state"] == NATIVE_READY
    assert provider.diagnostics()["graph_load_attempt_count"] == 0
    assert provider.diagnostics()["fallback_call_count"] == 0
    assert provider.diagnostics()["native_physical_backend"] == (
        "native-scip-fact-query-v1"
    )


def test_position_route_and_mixed_calls_match_one_loaded_graph(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    loads = 0

    def load(_path: Path) -> CodeGraph:
        nonlocal loads
        loads += 1
        return graph

    provider, native = _provider(graph, tmp_path, graph_loader=load)
    snapshot_id = (
        f"symbol_graph:{tmp_path}:{graph.graph.vcount()}:{graph.graph.ecount()}"
    )
    legacy_provider = StaticLSPProvider(
        graph,
        snapshot_id=snapshot_id,
        backend="persisted-symbol-graph-v1",
    )
    legacy = SimpleNamespace(lsp_provider=legacy_provider, symbol_graph=graph)
    candidate = SimpleNamespace(lsp_provider=provider, symbol_graph=None)

    position_calls = (
        lambda context: lsp_definition_impl(
            context, file_path="caller.py", line=3, top_k=8
        ),
        lambda context: lsp_references_impl(
            context,
            file_path="caller.py",
            line=3,
            include_declaration=False,
            top_k=40,
        ),
        lambda context: lsp_route_impl(
            context,
            symbols=["demo/target()."],
            query="target caller",
            top_k=12,
            include_neighbors=True,
        ),
    )
    for call in position_calls:
        assert _serialized(call(candidate)) == _serialized(call(legacy))

    native_before = native.resolve_calls
    assert _serialized(
        lsp_definition_impl(candidate, symbol="target", top_k=8)
    ) == _serialized(lsp_definition_impl(legacy, symbol="target", top_k=8))
    assert native.resolve_calls > native_before
    diagnostics = provider.diagnostics()
    assert loads == 1
    assert diagnostics["state"] == GRAPH_READY
    assert diagnostics["graph_load_attempt_count"] == 1
    assert diagnostics["graph_load_count"] == 1
    assert diagnostics["graph_publication_count"] == 1
    assert diagnostics["graph_materialization_seconds"] >= 0
    assert diagnostics["receipt_observation_count"] == 7
    assert diagnostics["native_definition_count"] == 1
    assert diagnostics["native_symbol_call_count"] == 1
    assert diagnostics["graph_call_count"] == 3
    assert diagnostics["graph_fallback_count"] == 3


def test_invalid_shapes_and_empty_route_do_not_load_graph(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    loads = 0

    def load(_path: Path) -> CodeGraph:
        nonlocal loads
        loads += 1
        return graph

    provider, _native = _provider(graph, tmp_path, graph_loader=load)

    with pytest.raises(
        ValueError,
        match="lsp_definition requires either symbol or file_path \\+ line",
    ):
        provider.definition()
    with pytest.raises(
        ValueError,
        match="lsp_references requires either symbol or file_path \\+ line",
    ):
        provider.references(file_path="caller.py")
    empty = provider.route(symbols=[], query="", top_k=12)
    assert empty == []
    assert empty.provider_metadata_dict() == {
        "provider": "codenib_static_index",
        "capability": "route",
        "status": "ok",
        "lsp_method": "codenib/lspRoute",
        "behavior_contract": "static_graph_lsp_v1",
        "position_granularity": "symbol",
        "backend": "persisted-symbol-graph-v1",
        "index_snapshot": (
            f"symbol_graph:{tmp_path}:{graph.graph.vcount()}:{graph.graph.ecount()}"
        ),
    }
    assert loads == 0
    assert provider.diagnostics()["fallback_call_count"] == 0


def test_concurrent_first_fallback_publishes_one_complete_graph(
    tmp_path: Path,
) -> None:
    graph = _graph(tmp_path)
    load_count = 0
    load_lock = threading.Lock()

    def load(_path: Path) -> CodeGraph:
        nonlocal load_count
        time.sleep(0.03)
        with load_lock:
            load_count += 1
        return graph

    provider, _native = _provider(graph, tmp_path, graph_loader=load)
    start = threading.Barrier(16)

    def query(_index: int) -> bytes:
        start.wait()
        result = provider.definition(file_path="caller.py", line=2, top_k=8)
        return _serialized(
            {
                "rows": [node.model_dump() for node in result],
                "metadata": result.provider_metadata_dict(),
            }
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(query, range(16)))

    assert len(set(results)) == 1
    assert load_count == 1
    diagnostics = provider.diagnostics()
    assert diagnostics["state"] == GRAPH_READY
    assert diagnostics["graph_load_attempt_count"] == 1
    assert diagnostics["graph_publication_count"] == 1
    assert diagnostics["graph_waiter_count"] >= 1
    assert diagnostics["receipt_observation_count"] == 33


@pytest.mark.parametrize("loader_fails", [False, True])
def test_symbol_call_waits_for_graph_publication_result(
    tmp_path: Path,
    loader_fails: bool,
) -> None:
    graph = _graph(tmp_path)
    load_started = threading.Event()
    release_load = threading.Event()
    symbol_start = threading.Barrier(2)
    failure = RuntimeError("injected publication failure")

    def load(_path: Path) -> CodeGraph:
        load_started.set()
        assert release_load.wait(timeout=2)
        if loader_fails:
            raise failure
        return graph

    provider, native = _provider(graph, tmp_path, graph_loader=load)

    def symbol_query() -> LSPProviderNodes:
        symbol_start.wait()
        return provider.definition(symbol="target", top_k=8)

    with ThreadPoolExecutor(max_workers=2) as executor:
        graph_future = executor.submit(
            provider.definition,
            file_path="caller.py",
            line=2,
            top_k=8,
        )
        assert load_started.wait(timeout=2)
        symbol_future = executor.submit(symbol_query)
        symbol_start.wait()
        time.sleep(0.03)
        assert not symbol_future.done()
        assert native.resolve_calls == 0
        release_load.set()

        if loader_fails:
            with pytest.raises(RuntimeError) as graph_error:
                graph_future.result(timeout=2)
            with pytest.raises(RuntimeError) as symbol_error:
                symbol_future.result(timeout=2)
            assert graph_error.value is failure
            assert symbol_error.value is failure
        else:
            assert graph_future.result(timeout=2)
            assert symbol_future.result(timeout=2)
            assert native.resolve_calls > 0


@pytest.mark.parametrize(
    ("failure", "expected_type"),
    [
        (RuntimeError("injected loader failure"), RuntimeError),
        (MemoryError("injected allocation failure"), MemoryError),
    ],
)
def test_loader_failure_is_terminal_cached_and_memoryerror_is_unchanged(
    tmp_path: Path,
    failure: Exception,
    expected_type: type[Exception],
) -> None:
    graph = _graph(tmp_path)
    loads = 0

    def fail(_path: Path) -> CodeGraph:
        nonlocal loads
        loads += 1
        raise failure

    provider, _native = _provider(graph, tmp_path, graph_loader=fail)
    observed = []
    for _ in range(2):
        with pytest.raises(expected_type) as caught:
            provider.definition(file_path="caller.py", line=2, top_k=8)
        observed.append(caught.value)

    with pytest.raises(expected_type) as symbol_definition:
        provider.definition(symbol="target", top_k=8)
    with pytest.raises(expected_type) as symbol_references:
        provider.references(symbol="target", top_k=40)
    with pytest.raises(expected_type) as empty_route:
        provider.route(symbols=[], query="", top_k=12)

    assert observed == [failure, failure]
    assert symbol_definition.value is failure
    assert symbol_references.value is failure
    assert empty_route.value is failure
    assert provider.can_serve("definition").status == "unavailable"
    assert loads == 1
    diagnostics = provider.diagnostics()
    assert diagnostics["state"] == FAILED
    assert diagnostics["graph_load_attempt_count"] == 1
    assert diagnostics["graph_publication_count"] == 0
    assert diagnostics["terminal_failure"] == {
        "type": expected_type.__name__,
        "message": str(failure),
    }


def test_snapshot_mismatch_fails_before_publication_and_is_cached(
    tmp_path: Path,
) -> None:
    graph = _graph(tmp_path)
    observations = 0

    def observe(_receipt: Any) -> dict[str, int]:
        nonlocal observations
        observations += 1
        return {"generation": observations}

    provider, _native = _provider(graph, tmp_path, snapshot_observer=observe)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="snapshot changed"):
            provider.route(symbols=["target"], top_k=12)

    assert observations == 2
    diagnostics = provider.diagnostics()
    assert diagnostics["state"] == FAILED
    assert diagnostics["graph_load_attempt_count"] == 1
    assert diagnostics["graph_publication_count"] == 0


def test_candidate_receipt_and_diagnostics_are_detached(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    provider, _native = _provider(graph, tmp_path)

    receipt = provider.candidate_receipt
    receipt["language"] = "rust"
    diagnostics = provider.diagnostics()
    diagnostics["state"] = "tampered"

    assert provider.candidate_receipt["language"] == "python"
    assert provider.diagnostics()["state"] == NATIVE_READY


def test_snapshot_observer_receives_a_fresh_detached_receipt(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    observations: list[dict[str, Any]] = []

    def observe(receipt: dict[str, Any]) -> dict[str, int]:
        observations.append(receipt)
        receipt["language"] = "mutated-by-observer"
        return {"generation": 1}

    provider, _native = _provider(graph, tmp_path, snapshot_observer=observe)
    assert provider.definition(file_path="caller.py", line=2, top_k=8)

    assert len(observations) == 3
    assert len({id(receipt) for receipt in observations}) == 3
    assert all(receipt["language"] == "mutated-by-observer" for receipt in observations)
    assert provider.candidate_receipt["language"] == "python"


def test_consumer_mode_is_independent_and_default_off() -> None:
    assert scip_consumer_query_mode({}) == "off"
    for mode in ("off", "auto", "required"):
        assert (
            scip_consumer_query_mode({"CODENIB_SCIP_FACT_QUERY_PROVIDER": mode.upper()})
            == mode
        )
    with pytest.raises(ValueError, match="must be off, auto, or required"):
        scip_consumer_query_mode({"CODENIB_SCIP_FACT_QUERY_PROVIDER": "sometimes"})


def test_selector_limits_fallback_to_auto_startup(monkeypatch, tmp_path: Path) -> None:
    import codenib.scip_interface.scip_query_provider as module

    legacy = object()
    legacy_calls = 0

    def legacy_factory() -> object:
        nonlocal legacy_calls
        legacy_calls += 1
        return legacy

    def reject(*_args, **_kwargs):
        raise FactQueryCandidateRejected("candidate unavailable")

    monkeypatch.setattr(module, "SCIP_CONSUMER_PROMOTED_LANGUAGES", {"python"})
    monkeypatch.setattr(module, "load_scip_query_provider", reject)

    assert (
        select_scip_query_provider(
            tmp_path / "manifest.json",
            legacy_provider_factory=legacy_factory,
            mode="off",
            language="python",
        )
        is legacy
    )
    assert (
        select_scip_query_provider(
            tmp_path / "manifest.json",
            legacy_provider_factory=legacy_factory,
            mode="auto",
            language="python",
        )
        is legacy
    )
    with pytest.raises(FactQueryCandidateRejected, match="candidate unavailable"):
        select_scip_query_provider(
            tmp_path / "manifest.json",
            legacy_provider_factory=legacy_factory,
            mode="required",
            language="python",
        )
    assert legacy_calls == 2


def test_selector_propagates_startup_memoryerror(monkeypatch, tmp_path: Path) -> None:
    import codenib.scip_interface.scip_query_provider as module

    def fail(*_args, **_kwargs):
        raise MemoryError("injected allocation failure")

    monkeypatch.setattr(module, "SCIP_CONSUMER_PROMOTED_LANGUAGES", {"python"})
    monkeypatch.setattr(module, "load_scip_query_provider", fail)
    with pytest.raises(MemoryError, match="injected allocation failure"):
        select_scip_query_provider(
            tmp_path / "manifest.json",
            legacy_provider_factory=lambda: object(),
            mode="auto",
            language="python",
        )


def test_auto_selector_does_not_retry_a_failing_legacy_factory(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import codenib.scip_interface.scip_query_provider as module

    legacy_calls = 0

    def fail_legacy() -> object:
        nonlocal legacy_calls
        legacy_calls += 1
        raise RuntimeError("legacy startup failure")

    monkeypatch.setattr(module, "SCIP_CONSUMER_PROMOTED_LANGUAGES", frozenset())
    with pytest.raises(RuntimeError, match="legacy startup failure"):
        select_scip_query_provider(
            tmp_path / "manifest.json",
            legacy_provider_factory=fail_legacy,
            mode="auto",
            language="python",
        )
    assert legacy_calls == 1


def test_auto_selector_does_not_wrap_post_publication_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import codenib.scip_interface.scip_query_provider as module

    graph = _graph(tmp_path)

    def fail(_path: Path) -> CodeGraph:
        raise RuntimeError("post-publication snapshot failure")

    provider, _native = _provider(graph, tmp_path, graph_loader=fail)
    legacy_calls = 0

    def legacy_factory() -> object:
        nonlocal legacy_calls
        legacy_calls += 1
        return object()

    monkeypatch.setattr(module, "SCIP_CONSUMER_PROMOTED_LANGUAGES", {"python"})
    monkeypatch.setattr(module, "load_scip_query_provider", lambda *_a, **_k: provider)
    selected = select_scip_query_provider(
        tmp_path / "manifest.json",
        legacy_provider_factory=legacy_factory,
        mode="auto",
        language="python",
    )
    assert selected is provider
    with pytest.raises(RuntimeError, match="post-publication snapshot failure"):
        selected.route(symbols=["target"], top_k=12)
    assert legacy_calls == 0
    assert selected.diagnostics()["state"] == FAILED


def _git(command: list[str], root: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *command],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _observer_receipt(tmp_path: Path) -> tuple[dict[str, Any], Path, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "demo.py").write_text("def demo():\n    return 1\n", encoding="utf-8")
    _git(["init", "-q"], root)
    _git(["config", "user.email", "tests@example.com"], root)
    _git(["config", "user.name", "CodeNib Tests"], root)
    _git(["add", "demo.py"], root)
    _git(["commit", "-qm", "fixture"], root)
    commit = _git(["rev-parse", "HEAD"], root)

    cache = tmp_path / "cache"
    graph_root = cache / "symbol_graph"
    graph_root.mkdir(parents=True)
    manifest_path = cache / "repo_manifest.json"
    index_path = graph_root / "index.decoded"
    graph_path = graph_root / "graph.pkl"
    index_path.write_bytes(b"decoded-index")
    graph_path.write_bytes(b"persisted-graph")
    index_fp = regular_file_fingerprint(index_path)
    graph_fp = regular_file_fingerprint(graph_path)
    source, allowed_files = _source_and_allowed_files(
        root,
        cache_root=cache,
        exclude_patterns=[],
        target_dir=None,
    )
    assert source == fingerprint_repository(root, exclude_roots=(cache,))
    selection = RepositorySourceSelection()
    entry = IndexEntry(
        index_type="symbol_graph",
        path=str(graph_root),
        built_at="2026-01-01T00:00:00+00:00",
        built_at_epoch=0.0,
        status="fresh",
        commit=commit,
        source_fingerprint=source.value,
        source_selection_digest=selection.digest,
    )
    embedded_manifest = RepoManifest(
        repo_path=str(root),
        commit=commit,
        last_indexed_commit=commit,
        source_fingerprint=source.value,
        last_indexed_source_fingerprint=source.value,
        source_selection=selection,
        last_indexed_source_selection_digest=selection.digest,
        languages=["python"],
        file_count=source.file_count,
        indexes={"symbol_graph": entry},
    )
    embedded_manifest.save(manifest_path)
    manifest_fp = regular_file_fingerprint(manifest_path)
    repository_filter_policy = repository_filter_policy_for_manifest(embedded_manifest)
    query_digest = hashlib.sha256(b"query-surface").hexdigest()
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "language": "python",
        "project_root": str(root),
        "manifest": {
            "path": str(manifest_path),
            "version": MANIFEST_VERSION,
            "size_bytes": manifest_fp["size"],
            "sha256": manifest_fp["sha256"],
            "commit": commit,
            "builder_schema": 4,
            "node_count": 4,
            "edge_count": 3,
            "document_count": 1,
            "query_surface_sha256": query_digest,
            "source_fingerprint": source.value,
            "file_count": source.file_count,
            "source_selection": selection.to_dict(),
            "source_selection_digest": selection.digest,
            "entry_path": str(graph_root),
            "entry_commit": commit,
            "entry_source_fingerprint": source.value,
            "entry_source_selection_digest": selection.digest,
        },
        "index": {
            "relative_path": "index.decoded",
            "size_bytes": index_fp["size"],
            "sha256": index_fp["sha256"],
        },
        "graph": {
            "relative_path": "graph.pkl",
            "size_bytes": graph_fp["size"],
            "sha256": graph_fp["sha256"],
            "query_surface_sha256": query_digest,
        },
        "fact_query": {
            "abi_version": 1,
            "format": "fact-query-index-v1",
            "capabilities": dict(FACT_QUERY_CAPABILITIES),
        },
        "source": {
            "fingerprint": source.value,
            "file_count": source.file_count,
            "repository_filter_policy": repository_filter_policy,
            "source_selection": selection.to_dict(),
            "source_selection_digest": selection.digest,
        },
        "filters": {
            "graph_route": "active",
            "update_mode": "full_rebuild",
            "repository_filter_policy": repository_filter_policy,
            "source_selection": selection.to_dict(),
            "source_selection_digest": selection.digest,
            "exclude_patterns": [],
            "target_dir": None,
            "allow_partial_languages": False,
            "allow_partial_index": False,
            "source_coverage_fallback": False,
            "allowed_file_count": len(allowed_files),
            "allowed_files_sha256": _allowed_files_digest(allowed_files),
        },
        "native_input": {
            "schema_version": 1,
            "index": {
                "path": str(index_path),
                "size_bytes": index_fp["size"],
                "sha256": index_fp["sha256"],
            },
            "metadata": {
                "kind": "none",
                "complete": True,
                "inputs": [],
                "internal_crates": [],
            },
        },
        "filter_identity": {
            "identity": True,
            "repository_filter_policy": repository_filter_policy,
            "source_selection_digest": selection.digest,
            "allowed_file_count": len(allowed_files),
            "allowed_files_sha256": _allowed_files_digest(allowed_files),
        },
    }
    unsigned = json.dumps(
        receipt,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    receipt["receipt_sha256"] = hashlib.sha256(unsigned).hexdigest()
    return receipt, graph_path, root / "demo.py"


def _bind_receipt_to_graph(
    receipt: dict[str, Any],
    graph: CodeGraph,
) -> None:
    query_digest = query_surface_sha256(graph)
    receipt["manifest"]["node_count"] = graph.graph.vcount()
    receipt["manifest"]["edge_count"] = graph.graph.ecount()
    receipt["manifest"]["query_surface_sha256"] = query_digest
    receipt["graph"]["query_surface_sha256"] = query_digest
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def test_snapshot_observer_rechecks_graph_and_source_inputs(tmp_path: Path) -> None:
    receipt, graph_path, source_path = _observer_receipt(tmp_path)

    before = observe_fact_query_snapshot(receipt)
    assert before["receipt_sha256"] == receipt["receipt_sha256"]
    assert before["graph"]["path"] == str(graph_path)
    assert before["source"]["file_count"] == receipt["source"]["file_count"]

    graph_path.write_bytes(b"mutated-graph")
    with pytest.raises(FactQueryCandidateRejected, match="graph changed"):
        observe_fact_query_snapshot(receipt)

    graph_path.write_bytes(b"persisted-graph")
    source_path.write_text("def demo():\n    return 2\n", encoding="utf-8")
    with pytest.raises(FactQueryCandidateRejected, match="repository source changed"):
        observe_fact_query_snapshot(receipt)


def test_snapshot_observer_rechecks_artifacts_after_source_scan(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import codenib.scip_interface.scip_query as module

    receipt, graph_path, _source_path = _observer_receipt(tmp_path)
    real_source_and_allowed_files = module._source_and_allowed_files
    calls = 0

    def mutate_after_scan(*args, **kwargs):
        nonlocal calls
        result = real_source_and_allowed_files(*args, **kwargs)
        calls += 1
        if calls == 1:
            graph_path.write_bytes(b"mutated-during-observation")
        return result

    monkeypatch.setattr(
        module,
        "_source_and_allowed_files",
        mutate_after_scan,
    )
    with pytest.raises(
        FactQueryCandidateRejected,
        match="inputs changed during snapshot observation",
    ):
        observe_fact_query_snapshot(receipt)


def test_published_graph_fails_closed_after_artifact_mutation(
    tmp_path: Path,
) -> None:
    receipt, graph_path, _source_path = _observer_receipt(tmp_path)
    project_root = Path(receipt["project_root"])
    graph = _graph(project_root)
    _bind_receipt_to_graph(receipt, graph)
    native = _NativeIndex(graph)
    provider = SCIPHybridQueryProvider(
        FactQueryCandidate(index=native, receipt=receipt, timings={}),
        graph_loader=lambda _path: graph,
    )

    provider.definition(file_path="caller.py", line=2, top_k=8)
    assert provider.diagnostics()["state"] == GRAPH_READY
    assert provider.diagnostics()["receipt_observation_count"] == 3

    graph_path.write_bytes(b"mutated-after-publication")
    observed = []
    for _ in range(2):
        with pytest.raises(FactQueryCandidateRejected, match="graph changed") as caught:
            provider.definition(file_path="caller.py", line=2, top_k=8)
        observed.append(caught.value)

    assert observed[0] is observed[1]
    diagnostics = provider.diagnostics()
    assert diagnostics["state"] == FAILED
    assert diagnostics["graph_load_count"] == 1
    assert diagnostics["graph_publication_count"] == 1
    assert diagnostics["receipt_observation_count"] == 4
    assert diagnostics["snapshot_after"] == {
        "observation_error": {
            "type": "FactQueryCandidateRejected",
            "message": "FactQuery graph changed after candidate admission",
        }
    }


@pytest.mark.parametrize("already_ready", [False, True], ids=["first", "ready"])
def test_graph_query_discards_result_when_source_changes_during_execution(
    tmp_path: Path,
    already_ready: bool,
) -> None:
    receipt, _graph_path, source_path = _observer_receipt(tmp_path)
    project_root = Path(receipt["project_root"])
    graph = _graph(project_root)
    _bind_receipt_to_graph(receipt, graph)
    provider = SCIPHybridQueryProvider(
        FactQueryCandidate(index=_NativeIndex(graph), receipt=receipt, timings={}),
        graph_loader=lambda _path: graph,
    )
    if already_ready:
        assert provider.definition(file_path="caller.py", line=2, top_k=8)

    query_result_ready = threading.Event()
    release_query = threading.Event()
    real_query_range = graph.query_range

    def blocked_query_range(*args, **kwargs):
        result = real_query_range(*args, **kwargs)
        query_result_ready.set()
        assert release_query.wait(timeout=2)
        return result

    graph.query_range = blocked_query_range
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            provider.definition,
            file_path="caller.py",
            line=2,
            top_k=8,
        )
        assert query_result_ready.wait(timeout=2)
        source_path.write_text("def demo():\n    return 2\n", encoding="utf-8")
        release_query.set()
        with pytest.raises(
            FactQueryCandidateRejected,
            match="repository source changed",
        ) as caught:
            future.result(timeout=2)

    with pytest.raises(FactQueryCandidateRejected) as cached:
        provider.definition(file_path="caller.py", line=2, top_k=8)
    assert cached.value is caught.value
    diagnostics = provider.diagnostics()
    assert diagnostics["state"] == FAILED
    assert diagnostics["graph_load_count"] == 1
    assert diagnostics["graph_publication_count"] == 1
    assert diagnostics["snapshot_before"]["source"]["fingerprint"] == (
        receipt["source"]["fingerprint"]
    )
    assert diagnostics["snapshot_after"] == {
        "observation_error": {
            "type": "FactQueryCandidateRejected",
            "message": (
                "FactQuery repository source changed after candidate admission"
            ),
        }
    }
