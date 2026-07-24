# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""LSP-compatible providers over CodeNib static indexes.

The agent-facing contract remains LSP-shaped. This module chooses the static
index implementation and records enough metadata for traces to prove which
provider served a dynamic LSP call and whether JSON-RPC fallback would be
required.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

from ..scip_interface.lsp_occurrence_index import SCIPOccurrenceIndex
from ..types import QueriedNode
from .lsp_graph import lsp_definition, lsp_references, lsp_route
from .route_context import fingerprint_lsp_route_nodes, summarize_lsp_route_nodes

STATIC_LSP_PROVIDER = "codeminer_static_index"
JSON_RPC_LSP_PROVIDER = "json_rpc"

CAPABILITY_DEFINITION = "definition"
CAPABILITY_REFERENCES = "references"
CAPABILITY_ROUTE = "route"

_LSP_METHODS = {
    CAPABILITY_DEFINITION: "textDocument/definition",
    CAPABILITY_REFERENCES: "textDocument/references",
    CAPABILITY_ROUTE: "codeminer/lspRoute",
}
_SUPPORTED_CAPABILITIES = frozenset(_LSP_METHODS)
_GRAPH_BEHAVIOR_CONTRACT = "static_graph_lsp_v1"
_GRAPH_POSITION_BEHAVIOR_CONTRACT = "static_symbol_graph_position_lsp_v1"
_OCCURRENCE_BEHAVIOR_CONTRACT = "static_scip_occurrence_lsp_v1"


@dataclass(frozen=True)
class LSPProviderMetadata:
    """Trace-safe metadata describing how an LSP-shaped call was served."""

    provider: str
    capability: str
    status: str
    lsp_method: str
    index_snapshot: Optional[str] = None
    fallback_reason: Optional[str] = None
    behavior_contract: str = _GRAPH_BEHAVIOR_CONTRACT
    position_granularity: str = "line"

    def to_dict(self) -> dict[str, Any]:
        out = {
            "provider": self.provider,
            "capability": self.capability,
            "status": self.status,
            "lsp_method": self.lsp_method,
            "behavior_contract": self.behavior_contract,
            "position_granularity": self.position_granularity,
        }
        if self.index_snapshot is not None:
            out["index_snapshot"] = self.index_snapshot
        if self.fallback_reason is not None:
            out["fallback_reason"] = self.fallback_reason
        return out


class LSPProviderNodes(list):
    """List-compatible LSP result carrying provider metadata for traces."""

    def __init__(
        self,
        nodes: Iterable[Any] = (),
        *,
        metadata: LSPProviderMetadata,
    ) -> None:
        super().__init__(nodes)
        self.lsp_provider_metadata = metadata

    def provider_metadata_dict(self) -> dict[str, Any]:
        return self.lsp_provider_metadata.to_dict()


class StaticLSPProvider:
    """LSP-shaped provider backed by a loaded CodeNib symbol graph."""

    provider = STATIC_LSP_PROVIDER

    def __init__(
        self,
        graph: Any,
        *,
        snapshot_id: Optional[str] = None,
        occurrence_index: Optional[SCIPOccurrenceIndex] = None,
    ) -> None:
        self.graph = graph
        self.occurrence_index = occurrence_index or getattr(
            graph, "lsp_occurrence_index", None
        )
        self.snapshot_id = snapshot_id or _graph_snapshot_id(graph)

    def can_serve(self, capability: str) -> LSPProviderMetadata:
        """Return a non-throwing fast-path decision for one capability."""

        normalized = _normalize_capability(capability)
        if normalized not in _SUPPORTED_CAPABILITIES:
            return _metadata(
                normalized,
                status="unsupported",
                snapshot_id=self.snapshot_id,
                fallback_reason="unsupported_capability",
            )
        if (
            normalized in {CAPABILITY_DEFINITION, CAPABILITY_REFERENCES}
            and self.occurrence_index is not None
        ):
            return _metadata(
                normalized,
                status="ok",
                snapshot_id=self.snapshot_id,
                behavior_contract=_OCCURRENCE_BEHAVIOR_CONTRACT,
                position_granularity="character",
            )
        if self.graph is None:
            return _metadata(
                normalized,
                status="unavailable",
                snapshot_id=None,
                fallback_reason="symbol_graph_unavailable",
            )
        return _metadata(normalized, status="ok", snapshot_id=self.snapshot_id)

    def definition(
        self,
        *,
        file_path: Optional[str] = None,
        line: Optional[int] = None,
        character: Optional[int] = None,
        symbol: Optional[str] = None,
        top_k: int = 8,
    ) -> LSPProviderNodes:
        """Serve ``textDocument/definition`` from the static graph."""

        self._require(CAPABILITY_DEFINITION)
        if (
            self.occurrence_index is not None
            and file_path is not None
            and line is not None
            and character is not None
            and symbol is None
        ):
            try:
                locations = self.occurrence_index.definitions(
                    file_path=file_path,
                    line=line,
                    character=character,
                    top_k=top_k,
                )
            except ValueError:
                locations = []
            if locations:
                return self._wrap(
                    CAPABILITY_DEFINITION,
                    _nodes_from_locations(locations, capability=CAPABILITY_DEFINITION),
                    behavior_contract=_OCCURRENCE_BEHAVIOR_CONTRACT,
                    position_granularity="character",
                )
        nodes = lsp_definition(
            self.graph,
            file_path=file_path,
            line=line,
            character=character,
            symbol=symbol,
            top_k=top_k,
        )
        position_query = (
            file_path is not None
            and line is not None
            and character is not None
            and symbol is None
        )
        return self._wrap(
            CAPABILITY_DEFINITION,
            nodes,
            behavior_contract=(
                _GRAPH_POSITION_BEHAVIOR_CONTRACT
                if position_query
                else _GRAPH_BEHAVIOR_CONTRACT
            ),
            position_granularity="character" if position_query else "line",
        )

    def references(
        self,
        *,
        file_path: Optional[str] = None,
        line: Optional[int] = None,
        character: Optional[int] = None,
        symbol: Optional[str] = None,
        include_declaration: bool = True,
        top_k: int = 40,
    ) -> LSPProviderNodes:
        """Serve ``textDocument/references`` from the static graph."""

        self._require(CAPABILITY_REFERENCES)
        if (
            self.occurrence_index is not None
            and file_path is not None
            and line is not None
            and character is not None
            and symbol is None
        ):
            try:
                locations = self.occurrence_index.references(
                    file_path=file_path,
                    line=line,
                    character=character,
                    include_declaration=include_declaration,
                    top_k=top_k,
                )
            except ValueError:
                locations = []
            if locations:
                return self._wrap(
                    CAPABILITY_REFERENCES,
                    _nodes_from_locations(locations, capability=CAPABILITY_REFERENCES),
                    behavior_contract=_OCCURRENCE_BEHAVIOR_CONTRACT,
                    position_granularity="character",
                )
        nodes = lsp_references(
            self.graph,
            file_path=file_path,
            line=line,
            character=character,
            symbol=symbol,
            include_declaration=include_declaration,
            top_k=top_k,
        )
        position_query = (
            file_path is not None
            and line is not None
            and character is not None
            and symbol is None
        )
        return self._wrap(
            CAPABILITY_REFERENCES,
            nodes,
            behavior_contract=(
                _GRAPH_POSITION_BEHAVIOR_CONTRACT
                if position_query
                else _GRAPH_BEHAVIOR_CONTRACT
            ),
            position_granularity="character" if position_query else "line",
        )

    def route(
        self,
        *,
        symbols: Sequence[str],
        query: Optional[str] = None,
        top_k: int = 12,
        include_neighbors: bool = True,
    ) -> LSPProviderNodes:
        """Serve CodeNib's LSP-shaped route extension from the static graph."""

        self._require(CAPABILITY_ROUTE)
        nodes = lsp_route(
            self.graph,
            symbols=symbols,
            query=query,
            top_k=top_k,
            include_neighbors=include_neighbors,
        )
        return self._wrap(CAPABILITY_ROUTE, nodes, position_granularity="symbol")

    def _require(self, capability: str) -> None:
        decision = self.can_serve(capability)
        if decision.status != "ok":
            raise RuntimeError(
                f"static LSP provider cannot serve {capability!r}: "
                f"{decision.fallback_reason or decision.status}"
            )

    def _wrap(
        self,
        capability: str,
        nodes: Iterable[Any],
        *,
        behavior_contract: str = _GRAPH_BEHAVIOR_CONTRACT,
        position_granularity: str = "line",
    ) -> LSPProviderNodes:
        if capability in {CAPABILITY_DEFINITION, CAPABILITY_REFERENCES}:
            nodes = normalize_native_lsp_nodes(nodes, capability=capability)
        return LSPProviderNodes(
            nodes,
            metadata=_metadata(
                capability,
                status="ok",
                snapshot_id=self.snapshot_id,
                behavior_contract=behavior_contract,
                position_granularity=position_granularity,
            ),
        )


def resolve_lsp_provider(context: Any) -> Any:
    """Return an injected LSP provider or the default static graph provider."""

    provider = getattr(context, "lsp_provider", None) if context is not None else None
    if provider is not None:
        return provider
    graph = getattr(context, "code_graph", None) if context is not None else None
    if graph is None:
        raise RuntimeError("LSP provider and symbol graph are unavailable")
    return StaticLSPProvider(graph)


def normalize_native_lsp_nodes(
    nodes: Iterable[Any], *, capability: str
) -> list[QueriedNode]:
    """Normalize native LSP results to a provider-independent location list."""

    normalized_capability = normalize_lsp_capability(capability)
    if normalized_capability not in {CAPABILITY_DEFINITION, CAPABILITY_REFERENCES}:
        raise ValueError(f"unsupported native LSP capability: {capability!r}")

    locations: dict[tuple[str, int], QueriedNode] = {}
    for node in nodes:
        file_path = _node_value(node, "file") or _node_value(node, "file_path")
        start_line = _coerce_line(_node_value(node, "start_line"))
        if not file_path or start_line is None:
            continue
        file_text = str(file_path)
        display_line = start_line + 1
        locations[(file_text, start_line)] = QueriedNode(
            node_name=f"{file_text}:{display_line}",
            type=normalized_capability,
            file=file_text,
            node_id=f"{file_text}:{display_line}:{normalized_capability}",
            start_line=start_line,
            end_line=start_line,
            score=1.0,
            content=f"lsp {normalized_capability}",
        )
    return [locations[key] for key in sorted(locations)]


def lsp_result_metadata(result: Any) -> Optional[dict[str, Any]]:
    """Return provider metadata from a list-compatible LSP result."""

    if hasattr(result, "provider_metadata_dict"):
        raw = result.provider_metadata_dict()
    else:
        raw = getattr(result, "lsp_provider_metadata", None)
        if hasattr(raw, "to_dict"):
            raw = raw.to_dict()
    return dict(raw) if isinstance(raw, Mapping) else None


def fingerprint_lsp_result(nodes: Sequence[Any]) -> str:
    """Stable fingerprint for ordered LSP-shaped result nodes."""

    return fingerprint_lsp_route_nodes(nodes)


def preview_lsp_result(
    nodes: Sequence[Any], *, max_nodes: int = 5
) -> list[dict[str, str]]:
    """Trace-safe preview for ordered LSP-shaped result nodes."""

    return summarize_lsp_route_nodes(nodes, max_nodes=max_nodes)


def _metadata(
    capability: str,
    *,
    status: str,
    snapshot_id: Optional[str],
    fallback_reason: Optional[str] = None,
    behavior_contract: str = _GRAPH_BEHAVIOR_CONTRACT,
    position_granularity: str = "line",
) -> LSPProviderMetadata:
    normalized = normalize_lsp_capability(capability)
    return LSPProviderMetadata(
        provider=STATIC_LSP_PROVIDER,
        capability=normalized,
        status=status,
        lsp_method=_LSP_METHODS.get(normalized, normalized),
        index_snapshot=snapshot_id,
        fallback_reason=fallback_reason,
        behavior_contract=behavior_contract,
        position_granularity=position_granularity,
    )


def normalize_lsp_capability(capability: str) -> str:
    """Normalize LSP method names to provider capability keys."""

    text = str(capability or "").strip()
    if text.startswith("textDocument/"):
        text = text.split("/", 1)[1]
    if text == "codeminer/lspRoute":
        return CAPABILITY_ROUTE
    return text


def _normalize_capability(capability: str) -> str:
    return normalize_lsp_capability(capability)


def _graph_snapshot_id(graph: Any) -> Optional[str]:
    if graph is None:
        return None
    igraph_obj = getattr(graph, "graph", None)
    node_count = _call_int(igraph_obj, "vcount")
    edge_count = _call_int(igraph_obj, "ecount")
    project_root = getattr(graph, "project_root", None)
    return f"symbol_graph:{project_root or 'unknown'}:{node_count}:{edge_count}"


def _call_int(obj: Any, method: str) -> int:
    fn = getattr(obj, method, None)
    if not callable(fn):
        return 0
    try:
        return int(fn())
    except Exception:  # noqa: BLE001 - snapshot metadata is best-effort.
        return 0


def _nodes_from_locations(
    locations: Iterable[Any], *, capability: str
) -> list[QueriedNode]:
    nodes = []
    for location in locations:
        file_path = str(location.file_path)
        start_line = int(location.start_line)
        display_line = start_line + 1
        nodes.append(
            QueriedNode(
                node_name=f"{file_path}:{display_line}",
                type=capability,
                file=file_path,
                node_id=f"{file_path}:{display_line}:{capability}",
                start_line=start_line,
                end_line=int(location.end_line),
                score=1.0,
                content=f"lsp {capability}",
            )
        )
    return nodes


def _node_value(node: Any, key: str) -> Any:
    if isinstance(node, Mapping):
        return node.get(key)
    return getattr(node, key, None)


def _coerce_line(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = [
    "CAPABILITY_DEFINITION",
    "CAPABILITY_REFERENCES",
    "CAPABILITY_ROUTE",
    "JSON_RPC_LSP_PROVIDER",
    "LSPProviderMetadata",
    "LSPProviderNodes",
    "STATIC_LSP_PROVIDER",
    "StaticLSPProvider",
    "resolve_lsp_provider",
    "fingerprint_lsp_result",
    "lsp_result_metadata",
    "normalize_native_lsp_nodes",
    "normalize_lsp_capability",
    "preview_lsp_result",
]
