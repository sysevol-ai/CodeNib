# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Experimental lazy LSP provider over an admitted SCIP FactQueryIndex.

This module is intentionally not connected to the production MCP selector.  It
keeps symbol-only definition/reference calls graph-free and atomically loads
the already-persisted graph for position and route calls.  Physical backend
details remain in :meth:`SCIPHybridQueryProvider.diagnostics`; public LSP
metadata stays byte-compatible with the established persisted-graph provider.
"""

from __future__ import annotations

import copy
import os
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..agent.lsp_provider import (
    CAPABILITY_DEFINITION,
    CAPABILITY_REFERENCES,
    CAPABILITY_ROUTE,
    STATIC_LSP_PROVIDER,
    LSPProviderMetadata,
    LSPProviderNodes,
    StaticLSPProvider,
    normalize_lsp_capability,
)
from ..compiler.manifest import RepoManifest
from ..languages import normalize_language
from .query_surface import query_surface_sha256
from .scip_query import (
    FactQueryCandidate,
    load_fact_query_candidate,
    observe_fact_query_snapshot,
)

SCIP_CONSUMER_QUERY_ENV = "CODENIB_SCIP_FACT_QUERY_PROVIDER"
SCIP_CONSUMER_PROMOTED_LANGUAGES = frozenset()

NATIVE_READY = "NATIVE_READY"
GRAPH_LOADING = "GRAPH_LOADING"
GRAPH_READY = "GRAPH_READY"
FAILED = "FAILED"

_PUBLIC_BACKEND = "persisted-symbol-graph-v1"
_PUBLIC_BEHAVIOR_CONTRACT = "static_graph_lsp_v1"
_PHYSICAL_NATIVE_BACKEND = "native-scip-fact-query-v1"
_PHYSICAL_GRAPH_BACKEND = "lazy-persisted-symbol-graph-v1"
_MODES = frozenset({"off", "auto", "required"})

GraphLoader = Callable[[Path], Any]
SnapshotObserver = Callable[[Mapping[str, Any]], Mapping[str, Any]]
ProviderFactory = Callable[[], Any]


def _normalize_mode(value: object) -> str:
    if type(value) is not str:
        raise ValueError(f"{SCIP_CONSUMER_QUERY_ENV} must be off, auto, or required")
    normalized = value.strip().lower()
    if normalized not in _MODES:
        raise ValueError(
            f"{SCIP_CONSUMER_QUERY_ENV} must be off, auto, or required; "
            f"got {value!r}"
        )
    return normalized


def scip_consumer_query_mode(
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return the independent, default-off consumer experiment mode."""

    source = os.environ if environ is None else environ
    return _normalize_mode(source.get(SCIP_CONSUMER_QUERY_ENV, "off"))


def _default_graph_loader(path: Path, receipt: Mapping[str, Any]) -> Any:
    # Keep CodeGraph and igraph absent until an admitted request needs them.
    from ..compiler.graph_artifact import load_authenticated_graph_generation

    return load_authenticated_graph_generation(path.parent, receipt)


def _manifest_language(
    manifest_path: str | Path,
    requested_language: str | None,
) -> str | None:
    if requested_language is not None:
        return normalize_language(requested_language)
    manifest = RepoManifest.load(manifest_path)
    if len(manifest.languages) != 1:
        return None
    return normalize_language(manifest.languages[0])


class SCIPHybridQueryProvider:
    """Serve symbol calls natively with one atomic persisted-graph fallback."""

    provider = STATIC_LSP_PROVIDER

    def __init__(
        self,
        candidate: FactQueryCandidate,
        *,
        graph_loader: GraphLoader | None = None,
        snapshot_observer: SnapshotObserver | None = None,
        public_fallback_reason: str | None = None,
    ) -> None:
        self.query_index = candidate.index
        self._candidate_receipt = copy.deepcopy(candidate.receipt)
        self._snapshot_observer = snapshot_observer or observe_fact_query_snapshot
        self._public_fallback_reason = public_fallback_reason

        manifest = self._receipt_mapping("manifest")
        graph = self._receipt_mapping("graph")
        project_root = self._candidate_receipt.get("project_root")
        language = self._candidate_receipt.get("language")
        node_count = manifest.get("node_count")
        edge_count = manifest.get("edge_count")
        if type(project_root) is not str or not project_root:
            raise ValueError("FactQuery candidate receipt has no project root")
        if type(language) is not str or not language:
            raise ValueError("FactQuery candidate receipt has no language")
        if type(node_count) is not int or node_count < 0:
            raise ValueError("FactQuery candidate receipt has no node count")
        if type(edge_count) is not int or edge_count < 0:
            raise ValueError("FactQuery candidate receipt has no edge count")
        query_digest = graph.get("query_surface_sha256")
        if type(query_digest) is not str or len(query_digest) != 64:
            raise ValueError("FactQuery candidate receipt has no graph query digest")
        if graph_loader is None:
            graph_size = graph.get("size_bytes")
            graph_digest = graph.get("sha256")
            if (
                isinstance(graph_size, bool)
                or not isinstance(graph_size, int)
                or graph_size < 0
                or not isinstance(graph_digest, str)
                or len(graph_digest) != 64
            ):
                raise ValueError(
                    "FactQuery candidate receipt has no authenticated graph artifact"
                )
            graph_receipt = {
                "relative_path": "graph.pkl",
                "size": graph_size,
                "sha256": graph_digest,
            }

            def authenticated_graph_loader(path: Path) -> Any:
                return _default_graph_loader(path, graph_receipt)

            self._graph_loader = authenticated_graph_loader
        else:
            self._graph_loader = graph_loader

        entry_path = manifest.get("entry_path")
        relative_graph = graph.get("relative_path")
        if type(entry_path) is not str or not entry_path:
            raise ValueError("FactQuery candidate receipt has no graph view path")
        if type(relative_graph) is not str or relative_graph != "graph.pkl":
            raise ValueError("FactQuery candidate receipt has no graph.pkl binding")
        graph_root = Path(entry_path).resolve()
        graph_path = (graph_root / relative_graph).resolve()
        try:
            graph_path.relative_to(graph_root)
        except ValueError as exc:
            raise ValueError("FactQuery graph path escapes its receipt view") from exc

        self.project_root = project_root
        self.language = language
        self.snapshot_id = f"symbol_graph:{project_root}:{node_count}:{edge_count}"
        self._graph_path = graph_path
        self._expected_node_count = node_count
        self._expected_edge_count = edge_count
        self._expected_query_surface_sha256 = query_digest
        self._symbol_provider = StaticLSPProvider(
            self.query_index,
            snapshot_id=self.snapshot_id,
            backend=_PUBLIC_BACKEND,
            fallback_reason=public_fallback_reason,
        )

        self._condition = threading.Condition(threading.Lock())
        self._state = NATIVE_READY
        self._graph_provider: StaticLSPProvider | None = None
        self._terminal_failure: Exception | None = None
        self._snapshot_before: dict[str, Any] | None = None
        self._snapshot_after: dict[str, Any] | None = None
        self._graph_load_attempt_count = 0
        self._graph_publication_count = 0
        self._graph_waiter_count = 0
        self._native_definition_count = 0
        self._native_references_count = 0
        self._fallback_call_count = 0
        self._graph_materialization_seconds: float | None = None
        self._receipt_observation_count = 0

    @property
    def candidate_receipt(self) -> dict[str, Any]:
        """Return a detached copy of the immutable admission receipt."""

        return copy.deepcopy(self._candidate_receipt)

    def diagnostics(self) -> dict[str, Any]:
        """Return thread-safe physical-backend and publication observations."""

        with self._condition:
            failure = self._terminal_failure
            return {
                "schema_version": 1,
                "state": self._state,
                "language": self.language,
                "logical_backend": _PUBLIC_BACKEND,
                "native_physical_backend": _PHYSICAL_NATIVE_BACKEND,
                "fallback_physical_backend": _PHYSICAL_GRAPH_BACKEND,
                "snapshot_id": self.snapshot_id,
                "candidate_receipt_sha256": self._candidate_receipt.get(
                    "receipt_sha256"
                ),
                "graph_load_attempt_count": self._graph_load_attempt_count,
                "graph_load_count": self._graph_load_attempt_count,
                "graph_publication_count": self._graph_publication_count,
                "graph_waiter_count": self._graph_waiter_count,
                "graph_materialization_seconds": (self._graph_materialization_seconds),
                "receipt_observation_count": self._receipt_observation_count,
                "native_definition_count": self._native_definition_count,
                "native_references_count": self._native_references_count,
                "native_symbol_call_count": (
                    self._native_definition_count + self._native_references_count
                ),
                "fallback_call_count": self._fallback_call_count,
                "graph_fallback_count": self._fallback_call_count,
                "graph_call_count": self._fallback_call_count,
                "snapshot_before": copy.deepcopy(self._snapshot_before),
                "snapshot_after": copy.deepcopy(self._snapshot_after),
                "terminal_failure": (
                    {
                        "type": type(failure).__name__,
                        "message": str(failure),
                    }
                    if failure is not None
                    else None
                ),
            }

    def can_serve(self, capability: str) -> LSPProviderMetadata:
        """Advertise the complete logical provider without loading the graph."""

        normalized = normalize_lsp_capability(capability)
        with self._condition:
            if self._state == FAILED:
                return self._metadata(
                    normalized,
                    status="unavailable",
                    fallback_reason="scip_provider_terminal_failure",
                )
        if normalized not in {
            CAPABILITY_DEFINITION,
            CAPABILITY_REFERENCES,
            CAPABILITY_ROUTE,
        }:
            return self._metadata(
                normalized,
                status="unsupported",
                fallback_reason="unsupported_capability",
            )
        return self._metadata(normalized, status="ok")

    def definition(self, **arguments: Any) -> LSPProviderNodes:
        """Use native symbol lookup or the atomic graph fallback."""

        if arguments.get("symbol"):
            self._begin_symbol_call("definition")
            return self._symbol_provider.definition(**arguments)
        self._require_not_failed()
        if not arguments.get("file_path") or arguments.get("line") is None:
            raise ValueError(
                "lsp_definition requires either symbol or file_path + line"
            )
        return self._fallback("definition", arguments)

    def references(self, **arguments: Any) -> LSPProviderNodes:
        """Use native symbol lookup or the atomic graph fallback."""

        if arguments.get("symbol"):
            self._begin_symbol_call("references")
            return self._symbol_provider.references(**arguments)
        self._require_not_failed()
        if not arguments.get("file_path") or arguments.get("line") is None:
            raise ValueError(
                "lsp_references requires either symbol or file_path + line"
            )
        return self._fallback("references", arguments)

    def route(self, **arguments: Any) -> LSPProviderNodes:
        """Use the complete graph for non-empty route requests."""

        self._require_not_failed()
        if not (arguments.get("symbols") or []) and not arguments.get("query"):
            return LSPProviderNodes(
                [],
                metadata=self._metadata(
                    CAPABILITY_ROUTE,
                    status="ok",
                    position_granularity="symbol",
                ),
            )
        return self._fallback("route", arguments)

    def _fallback(
        self,
        capability: str,
        arguments: Mapping[str, Any],
    ) -> LSPProviderNodes:
        with self._condition:
            self._fallback_call_count += 1
        provider, before = self._full_graph_provider()
        with self._condition:
            if self._state == FAILED:
                self._raise_terminal_failure()
            self._snapshot_before = copy.deepcopy(before)

        result: LSPProviderNodes | None = None
        query_failure: Exception | None = None
        try:
            result = getattr(provider, capability)(**dict(arguments))
        except Exception as exc:
            query_failure = exc

        self._complete_graph_query(provider, before)
        if query_failure is not None:
            raise query_failure
        assert result is not None
        return result

    def _full_graph_provider(
        self,
    ) -> tuple[StaticLSPProvider, dict[str, Any]]:
        published_provider: StaticLSPProvider | None = None
        published_snapshot: dict[str, Any] | None = None
        with self._condition:
            if self._state == GRAPH_READY:
                assert self._graph_provider is not None
                assert self._snapshot_after is not None
                published_provider = self._graph_provider
                published_snapshot = copy.deepcopy(self._snapshot_after)
            if self._state == FAILED:
                self._raise_terminal_failure()
            if self._state == GRAPH_LOADING:
                self._graph_waiter_count += 1
                while self._state == GRAPH_LOADING:
                    self._condition.wait()
                if self._state == GRAPH_READY:
                    assert self._graph_provider is not None
                    assert self._snapshot_after is not None
                    published_provider = self._graph_provider
                    published_snapshot = copy.deepcopy(self._snapshot_after)
                else:
                    self._raise_terminal_failure()
            if published_provider is None and self._state != NATIVE_READY:
                raise RuntimeError(f"unknown SCIP provider state: {self._state}")
            if published_provider is None:
                self._state = GRAPH_LOADING
                self._graph_load_attempt_count += 1

        if published_provider is not None:
            assert published_snapshot is not None
            return self._revalidate_published_provider(
                published_provider,
                published_snapshot,
            )

        before: dict[str, Any] | None = None
        after: dict[str, Any] | None = None
        try:
            before = self._observe_snapshot()
            materialization_started = time.perf_counter()
            try:
                graph = self._graph_loader(self._graph_path)
                self._validate_loaded_graph(graph)
            finally:
                materialization_seconds = time.perf_counter() - materialization_started
                with self._condition:
                    if self._graph_materialization_seconds is None:
                        self._graph_materialization_seconds = materialization_seconds
            after = self._observe_snapshot()
            if after != before:
                raise RuntimeError("FactQuery snapshot changed during lazy graph load")
            graph_provider = StaticLSPProvider(
                graph,
                snapshot_id=self.snapshot_id,
                backend=_PUBLIC_BACKEND,
                fallback_reason=self._public_fallback_reason,
            )
        except Exception as exc:
            with self._condition:
                if before is not None:
                    self._snapshot_before = copy.deepcopy(before)
                if after is not None:
                    self._snapshot_after = copy.deepcopy(after)
                failure = self._set_terminal_failure_locked(exc)
            raise failure

        with self._condition:
            self._snapshot_before = before
            self._snapshot_after = after
            self._graph_provider = graph_provider
            self._graph_publication_count += 1
            self._state = GRAPH_READY
            self._condition.notify_all()
            assert after is not None
            return graph_provider, copy.deepcopy(after)

    def _revalidate_published_provider(
        self,
        provider: StaticLSPProvider,
        published_snapshot: Mapping[str, Any],
    ) -> tuple[StaticLSPProvider, dict[str, Any]]:
        try:
            observed = self._observe_snapshot()
        except Exception as exc:
            with self._condition:
                if self._state != FAILED:
                    self._snapshot_after = {
                        "observation_error": {
                            "type": type(exc).__name__,
                            "message": str(exc),
                        }
                    }
                failure = self._set_terminal_failure_locked(exc)
            raise failure

        with self._condition:
            if self._state == FAILED:
                self._raise_terminal_failure()
            if self._state != GRAPH_READY or self._graph_provider is not provider:
                raise RuntimeError("SCIP graph provider changed during revalidation")
            if observed != published_snapshot:
                self._snapshot_after = copy.deepcopy(observed)
                failure = self._set_terminal_failure_locked(
                    RuntimeError(
                        "FactQuery snapshot changed after lazy graph publication"
                    )
                )
                raise failure
            self._snapshot_after = copy.deepcopy(observed)
            return provider, copy.deepcopy(observed)

    def _complete_graph_query(
        self,
        provider: StaticLSPProvider,
        before: Mapping[str, Any],
    ) -> None:
        try:
            after = self._observe_snapshot()
        except Exception as exc:
            with self._condition:
                if self._state != FAILED:
                    self._snapshot_after = {
                        "observation_error": {
                            "type": type(exc).__name__,
                            "message": str(exc),
                        }
                    }
                failure = self._set_terminal_failure_locked(exc)
            raise failure

        with self._condition:
            if self._state == FAILED:
                self._raise_terminal_failure()
            if self._state != GRAPH_READY or self._graph_provider is not provider:
                failure = self._set_terminal_failure_locked(
                    RuntimeError("SCIP graph provider changed during query execution")
                )
                raise failure
            self._snapshot_after = copy.deepcopy(after)
            if after != before:
                failure = self._set_terminal_failure_locked(
                    RuntimeError("FactQuery snapshot changed during graph query")
                )
                raise failure

    def _observe_snapshot(self) -> dict[str, Any]:
        with self._condition:
            self._receipt_observation_count += 1
        observed = self._snapshot_observer(copy.deepcopy(self._candidate_receipt))
        if not isinstance(observed, Mapping):
            raise RuntimeError("FactQuery snapshot observer returned a non-mapping")
        return copy.deepcopy(dict(observed))

    def _validate_loaded_graph(self, graph: Any) -> None:
        storage = getattr(graph, "graph", None)
        vcount = getattr(storage, "vcount", None)
        ecount = getattr(storage, "ecount", None)
        if not callable(vcount) or not callable(ecount):
            raise RuntimeError("lazy graph loader returned an incompatible graph")
        if int(vcount()) != self._expected_node_count or (
            int(ecount()) != self._expected_edge_count
        ):
            raise RuntimeError("lazy graph shape does not match the candidate receipt")
        loaded_root = getattr(graph, "project_root", None)
        if (
            loaded_root is None
            or Path(str(loaded_root)).resolve() != Path(self.project_root).resolve()
        ):
            raise RuntimeError("lazy graph project root does not match the receipt")
        if query_surface_sha256(graph) != self._expected_query_surface_sha256:
            raise RuntimeError("lazy graph query surface does not match the receipt")

    def _receipt_mapping(self, key: str) -> Mapping[str, Any]:
        value = self._candidate_receipt.get(key)
        if not isinstance(value, Mapping):
            raise ValueError(f"FactQuery candidate receipt {key!r} is malformed")
        return value

    def _metadata(
        self,
        capability: str,
        *,
        status: str,
        fallback_reason: str | None = None,
        position_granularity: str = "line",
    ) -> LSPProviderMetadata:
        methods = {
            CAPABILITY_DEFINITION: "textDocument/definition",
            CAPABILITY_REFERENCES: "textDocument/references",
            CAPABILITY_ROUTE: "codenib/lspRoute",
        }
        return LSPProviderMetadata(
            provider=STATIC_LSP_PROVIDER,
            capability=capability,
            status=status,
            lsp_method=methods.get(capability, capability),
            backend=_PUBLIC_BACKEND,
            index_snapshot=self.snapshot_id,
            fallback_reason=(
                fallback_reason
                if fallback_reason is not None
                else self._public_fallback_reason
            ),
            behavior_contract=_PUBLIC_BEHAVIOR_CONTRACT,
            position_granularity=position_granularity,
        )

    def _raise_terminal_failure(self) -> None:
        failure = self._terminal_failure
        if failure is None:
            raise RuntimeError("SCIP provider failed without a cached error")
        raise failure

    def _require_not_failed(self) -> None:
        with self._condition:
            if self._state == FAILED:
                self._raise_terminal_failure()

    def _begin_symbol_call(self, capability: str) -> None:
        with self._condition:
            while self._state == GRAPH_LOADING:
                self._condition.wait()
            if self._state == FAILED:
                self._raise_terminal_failure()
            if capability == "definition":
                self._native_definition_count += 1
            elif capability == "references":
                self._native_references_count += 1
            else:  # pragma: no cover - private invariant
                raise RuntimeError(f"unknown native symbol capability: {capability}")

    def _set_terminal_failure_locked(self, failure: Exception) -> Exception:
        if self._state != FAILED:
            self._terminal_failure = failure
            self._state = FAILED
            self._condition.notify_all()
        assert self._terminal_failure is not None
        return self._terminal_failure


def load_scip_query_provider(
    manifest_path: str | Path,
    *,
    language: str | None = None,
    project_root: str | Path | None = None,
    core_module: Any | None = None,
    graph_loader: GraphLoader | None = None,
    snapshot_observer: SnapshotObserver | None = None,
    public_fallback_reason: str | None = None,
) -> SCIPHybridQueryProvider:
    """Load the required candidate path without making a fallback decision."""

    candidate = load_fact_query_candidate(
        manifest_path,
        language=language,
        project_root=project_root,
        core_module=core_module,
    )
    return SCIPHybridQueryProvider(
        candidate,
        graph_loader=graph_loader,
        snapshot_observer=snapshot_observer,
        public_fallback_reason=public_fallback_reason,
    )


def select_scip_query_provider(
    manifest_path: str | Path,
    *,
    legacy_provider_factory: ProviderFactory,
    mode: str | None = None,
    language: str | None = None,
    project_root: str | Path | None = None,
    core_module: Any | None = None,
    graph_loader: GraphLoader | None = None,
    snapshot_observer: SnapshotObserver | None = None,
    public_fallback_reason: str | None = None,
) -> Any:
    """Select an experimental startup provider without wrapping later calls.

    Only candidate construction is eligible for ``auto`` fallback.  Once this
    function publishes a :class:`SCIPHybridQueryProvider`, any later receipt or
    lazy-loader failure remains terminal and cannot switch generations.
    """

    selected_mode = (
        scip_consumer_query_mode() if mode is None else _normalize_mode(mode)
    )
    if selected_mode == "off":
        return legacy_provider_factory()
    if selected_mode == "auto":
        try:
            candidate_language = _manifest_language(manifest_path, language)
        except MemoryError:
            raise
        except Exception:
            return legacy_provider_factory()
        if candidate_language not in SCIP_CONSUMER_PROMOTED_LANGUAGES:
            return legacy_provider_factory()
        try:
            return load_scip_query_provider(
                manifest_path,
                language=language,
                project_root=project_root,
                core_module=core_module,
                graph_loader=graph_loader,
                snapshot_observer=snapshot_observer,
                public_fallback_reason=public_fallback_reason,
            )
        except MemoryError:
            raise
        except Exception:
            return legacy_provider_factory()
    return load_scip_query_provider(
        manifest_path,
        language=language,
        project_root=project_root,
        core_module=core_module,
        graph_loader=graph_loader,
        snapshot_observer=snapshot_observer,
        public_fallback_reason=public_fallback_reason,
    )


__all__ = [
    "FAILED",
    "GRAPH_LOADING",
    "GRAPH_READY",
    "NATIVE_READY",
    "SCIP_CONSUMER_PROMOTED_LANGUAGES",
    "SCIP_CONSUMER_QUERY_ENV",
    "SCIPHybridQueryProvider",
    "load_scip_query_provider",
    "scip_consumer_query_mode",
    "select_scip_query_provider",
]
