# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""LSP-compatible providers over CodeNib static indexes.

The agent-facing contract remains LSP-shaped. This module chooses the static
index implementation and records enough metadata for traces to prove which
provider served a dynamic LSP call and whether JSON-RPC fallback would be
required.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from ..types import QueriedNode
from .lsp_graph import lsp_definition, lsp_references, lsp_route
from .route_context import fingerprint_lsp_route_nodes, summarize_lsp_route_nodes

STATIC_LSP_PROVIDER = "codenib_static_index"
JSON_RPC_LSP_PROVIDER = "json_rpc"

CAPABILITY_DEFINITION = "definition"
CAPABILITY_REFERENCES = "references"
CAPABILITY_ROUTE = "route"

_LSP_METHODS = {
    CAPABILITY_DEFINITION: "textDocument/definition",
    CAPABILITY_REFERENCES: "textDocument/references",
    CAPABILITY_ROUTE: "codenib/lspRoute",
}
_SUPPORTED_CAPABILITIES = frozenset(_LSP_METHODS)
_GRAPH_BEHAVIOR_CONTRACT = "static_graph_lsp_v1"
_GRAPH_POSITION_BEHAVIOR_CONTRACT = "static_symbol_graph_position_lsp_v1"
_OCCURRENCE_BEHAVIOR_CONTRACT = "static_scip_occurrence_lsp_v1"
_NATIVE_OCCURRENCE_BEHAVIOR_CONTRACT = "static_native_occurrence_lsp_v1"
_POSITION_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class LSPProviderMetadata:
    """Trace-safe metadata describing how an LSP-shaped call was served."""

    provider: str
    capability: str
    status: str
    lsp_method: str
    backend: Optional[str] = None
    index_snapshot: Optional[str] = None
    fallback_reason: Optional[str] = None
    behavior_contract: str = _GRAPH_BEHAVIOR_CONTRACT
    position_granularity: str = "line"
    position_encoding: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "provider": self.provider,
            "capability": self.capability,
            "status": self.status,
            "lsp_method": self.lsp_method,
            "behavior_contract": self.behavior_contract,
            "position_granularity": self.position_granularity,
        }
        if self.backend is not None:
            out["backend"] = self.backend
        if self.index_snapshot is not None:
            out["index_snapshot"] = self.index_snapshot
        if self.fallback_reason is not None:
            out["fallback_reason"] = self.fallback_reason
        if self.position_encoding is not None:
            out["position_encoding"] = self.position_encoding
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


class LSPPositionFallback(ValueError):
    """Signal that an exact occurrence query must use the legacy provider."""

    def __init__(self, reason: str) -> None:
        self.reason = str(reason or "native_position_fallback")
        super().__init__(self.reason)


class NativeOccurrenceQueryAdapter:
    """Occurrence adapter whose rows and interval postings remain in C++."""

    behavior_contract = _NATIVE_OCCURRENCE_BEHAVIOR_CONTRACT
    authoritative_empty = True

    def __init__(self, query_index: Any) -> None:
        self.query_index = query_index
        self.position_encoding = str(
            getattr(query_index, "position_encoding", None) or "UTF16"
        )
        self.occurrence_count = int(getattr(query_index, "occurrence_count", 0))
        self.project_root = Path(str(getattr(query_index, "project_root", "") or ""))

    def occurrence_at(self, file_path: str, line: int, character: int) -> Any:
        if self.query_index.has_occurrence_at(file_path, line, character):
            return {
                "file_path": file_path,
                "line": int(line),
                "character": int(character),
            }
        return None

    def definitions(
        self,
        *,
        file_path: str,
        line: int,
        character: int,
        top_k: int = 8,
    ) -> list[Mapping[str, Any]]:
        return self._locations(
            self.query_index.position_definitions(
                file_path=file_path,
                line=line,
                character=character,
                top_k=top_k,
            ),
            file_path=file_path,
            line=line,
            character=character,
        )

    def references(
        self,
        *,
        file_path: str,
        line: int,
        character: int,
        include_declaration: bool = True,
        top_k: int = 40,
    ) -> list[Mapping[str, Any]]:
        return self._locations(
            self.query_index.position_references(
                file_path=file_path,
                line=line,
                character=character,
                include_declaration=include_declaration,
                top_k=top_k,
            ),
            file_path=file_path,
            line=line,
            character=character,
        )

    def _locations(
        self,
        payload: Any,
        *,
        file_path: str,
        line: int,
        character: int,
    ) -> list[Mapping[str, Any]]:
        if not isinstance(payload, Mapping):
            raise RuntimeError("native occurrence query returned an invalid result")
        if payload.get("served") is not True:
            raise LSPPositionFallback(
                str(payload.get("fallback_reason") or "native_position_fallback")
            )
        locations = payload.get("locations")
        if not isinstance(locations, list) or not all(
            isinstance(location, Mapping) for location in locations
        ):
            raise RuntimeError("native occurrence query returned invalid locations")
        targets = payload.get("targets")
        if not isinstance(targets, list) or not all(
            isinstance(target, int) and not isinstance(target, bool)
            for target in targets
        ):
            raise RuntimeError("native occurrence query returned invalid targets")
        self._require_legacy_token_match(
            file_path=file_path,
            line=line,
            character=character,
            targets=targets,
        )
        return locations

    def _require_legacy_token_match(
        self,
        *,
        file_path: str,
        line: int,
        character: int,
        targets: Sequence[int],
    ) -> None:
        token = self._source_token(file_path, line, character)
        if token is None or not targets:
            raise LSPPositionFallback("native_position_token_requires_legacy_graph")
        for target in targets:
            info = self.query_index.get_node_info_by_id(target)
            if not isinstance(info, Mapping):
                raise LSPPositionFallback("native_position_token_requires_legacy_graph")
            anchors = {
                self._bare_symbol(str(value))
                for value in (info.get("name"), info.get("unified_name"))
                if value
            }
            if token not in anchors:
                raise LSPPositionFallback("native_position_token_requires_legacy_graph")

    def _source_token(self, file_path: str, line: int, character: int) -> Optional[str]:
        relative = Path(file_path)
        if (
            relative.is_absolute()
            or relative != Path(*relative.parts)
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            return None
        try:
            root = self.project_root.resolve()
            source = (root / relative).resolve()
            source.relative_to(root)
            source_line = source.read_text(
                encoding="utf-8", errors="strict"
            ).splitlines()[int(line)]
            codepoint_character = self._codepoint_character(source_line, character)
        except (OSError, IndexError, ValueError):
            return None
        for match in _POSITION_WORD_RE.finditer(source_line):
            if match.start() <= codepoint_character < match.end():
                return match.group(0)
        return None

    def _codepoint_character(self, source_line: str, character: int) -> int:
        if character < 0:
            raise ValueError("character must be non-negative")
        if self.position_encoding == "UTF32":
            if character > len(source_line):
                raise ValueError("character exceeds source line")
            return character
        codec = "utf-8" if self.position_encoding == "UTF8" else "utf-16-le"
        width = 1 if self.position_encoding == "UTF8" else 2
        encoded = source_line.encode(codec)
        offset = character * width
        if offset > len(encoded):
            raise ValueError("character exceeds source line")
        return len(encoded[:offset].decode(codec))

    @staticmethod
    def _bare_symbol(symbol: str) -> str:
        return symbol.split(":")[-1].split(".")[-1].split("#")[-1].rstrip("()")


class StaticLSPProvider:
    """LSP-shaped provider backed by a loaded CodeNib symbol graph."""

    provider = STATIC_LSP_PROVIDER

    def __init__(
        self,
        graph: Any,
        *,
        snapshot_id: Optional[str] = None,
        occurrence_index: Optional[Any] = None,
        backend: Optional[str] = None,
        fallback_reason: Optional[str] = None,
    ) -> None:
        self.graph = graph
        self.occurrence_index = occurrence_index or getattr(
            graph, "lsp_occurrence_index", None
        )
        self.snapshot_id = snapshot_id or _graph_snapshot_id(graph)
        self.backend = backend
        self.fallback_reason = fallback_reason
        self.position_encoding = getattr(
            self.occurrence_index, "position_encoding", None
        )

    def _occurrence_behavior_contract(self) -> str:
        return str(
            getattr(self.occurrence_index, "behavior_contract", None)
            or _OCCURRENCE_BEHAVIOR_CONTRACT
        )

    def can_serve(self, capability: str) -> LSPProviderMetadata:
        """Return a non-throwing fast-path decision for one capability."""

        normalized = _normalize_capability(capability)
        if normalized not in _SUPPORTED_CAPABILITIES:
            return _metadata(
                normalized,
                status="unsupported",
                snapshot_id=self.snapshot_id,
                backend=self.backend,
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
                backend=self.backend,
                fallback_reason=self.fallback_reason,
                behavior_contract=self._occurrence_behavior_contract(),
                position_granularity="character",
                position_encoding=self.position_encoding,
            )
        if (
            normalized == CAPABILITY_ROUTE
            and getattr(self.graph, "supports_graph_routes", True) is False
        ):
            return _metadata(
                normalized,
                status="unavailable",
                snapshot_id=self.snapshot_id,
                backend=self.backend,
                fallback_reason="fact_index_does_not_support_routes",
            )
        if self.graph is None:
            return _metadata(
                normalized,
                status="unavailable",
                snapshot_id=None,
                backend=self.backend,
                fallback_reason="symbol_graph_unavailable",
            )
        return _metadata(
            normalized,
            status="ok",
            snapshot_id=self.snapshot_id,
            backend=self.backend,
            fallback_reason=self.fallback_reason,
        )

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
            except LSPPositionFallback:
                raise
            except ValueError:
                locations = []
            if locations or getattr(
                self.occurrence_index, "authoritative_empty", False
            ):
                return self._wrap(
                    CAPABILITY_DEFINITION,
                    _nodes_from_locations(locations, capability=CAPABILITY_DEFINITION),
                    behavior_contract=self._occurrence_behavior_contract(),
                    position_granularity="character",
                    position_encoding=self.position_encoding,
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
            position_encoding="UTF32" if position_query else None,
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
            except LSPPositionFallback:
                raise
            except ValueError:
                locations = []
            if locations or getattr(
                self.occurrence_index, "authoritative_empty", False
            ):
                return self._wrap(
                    CAPABILITY_REFERENCES,
                    _nodes_from_locations(locations, capability=CAPABILITY_REFERENCES),
                    behavior_contract=self._occurrence_behavior_contract(),
                    position_granularity="character",
                    position_encoding=self.position_encoding,
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
            position_encoding="UTF32" if position_query else None,
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
        position_encoding: Optional[str] = None,
    ) -> LSPProviderNodes:
        if capability in {CAPABILITY_DEFINITION, CAPABILITY_REFERENCES}:
            nodes = normalize_native_lsp_nodes(nodes, capability=capability)
        return LSPProviderNodes(
            nodes,
            metadata=_metadata(
                capability,
                status="ok",
                snapshot_id=self.snapshot_id,
                backend=self.backend,
                fallback_reason=self.fallback_reason,
                behavior_contract=behavior_contract,
                position_granularity=position_granularity,
                position_encoding=position_encoding,
            ),
        )


def resolve_lsp_provider(context: Any) -> Any:
    """Return an injected LSP provider or the default static graph provider."""

    provider = _context_attribute(context, "lsp_provider")
    if provider is not None:
        return provider
    graph = _context_attribute(context, "code_graph")
    if graph is None:
        graph = _context_attribute(context, "symbol_graph")
    if graph is None:
        raise RuntimeError("LSP provider and symbol graph are unavailable")
    return StaticLSPProvider(graph)


def _context_attribute(context: Any, name: str) -> Any:
    """Read a real context field without materializing MagicMock children."""

    if context is None:
        return None
    attributes = getattr(context, "__dict__", {})
    mock_children = (
        attributes.get("_mock_children") if isinstance(attributes, Mapping) else None
    )
    if (
        isinstance(mock_children, Mapping)
        and name not in mock_children
        and name not in attributes
    ):
        return None
    return getattr(context, name, None)


def select_checkout_lsp_provider(
    *,
    project_root: str,
    languages: Iterable[str],
    symbol_graph: Any = None,
    allow_native: bool = True,
    native_disabled_reason: str = "native_provider_not_authorized",
) -> tuple[Any, dict[str, Any]]:
    """Select an existing local clangd generation or an explicit graph fallback.

    Selection never generates a clangd index. Mixed-language and portable
    contexts retain the persisted graph so the native C++ slice cannot hide
    other languages or become part of a portable artifact contract.
    """

    from ..languages import normalize_graph_language

    normalized_languages = [
        normalize_graph_language(str(language)) for language in languages
    ]
    canonical_languages = {
        normalized for normalized in normalized_languages if normalized is not None
    }
    has_unknown_language = any(
        normalized is None for normalized in normalized_languages
    )
    mode = "auto"
    fallback_reason: Optional[str] = None
    if not allow_native:
        fallback_reason = native_disabled_reason or "native_provider_not_authorized"
    elif canonical_languages != {"cpp"} or has_unknown_language:
        fallback_reason = (
            "mixed_language_requires_persisted_graph"
            if "cpp" in canonical_languages
            else "native_clangd_not_applicable"
        )
    else:
        from ..ls_index.clangd_decode import (
            native_clangd_query_mode,
            native_clangd_query_promoted,
        )

        mode = native_clangd_query_mode()
        if mode == "off":
            fallback_reason = "native_clangd_provider_disabled"
        elif mode == "auto" and not native_clangd_query_promoted():
            fallback_reason = "native_clangd_provider_below_threshold"

    if fallback_reason is None:
        try:
            from ..ls_router import LSIndexer

            provider = LSIndexer(
                project_root=project_root,
                output_dir=project_root,
                language="cpp",
            ).process_query_provider(require_native=True)
        except MemoryError:
            raise
        except Exception:
            if mode == "required":
                raise
            fallback_reason = "native_clangd_provider_unavailable"
        else:
            backend = getattr(
                provider,
                "provider_backend",
                "native-clangd-fact-query-v1",
            )
            return provider, {
                "provider": getattr(provider, "provider", STATIC_LSP_PROVIDER),
                "backend": backend,
                "status": "ok",
                "index_snapshot": getattr(provider, "snapshot_id", None),
                "capabilities": {
                    CAPABILITY_DEFINITION: True,
                    CAPABILITY_REFERENCES: True,
                    CAPABILITY_ROUTE: True,
                },
            }

    if symbol_graph is not None:
        provider = StaticLSPProvider(
            symbol_graph,
            backend="persisted-symbol-graph-v1",
            fallback_reason=fallback_reason,
        )
        return provider, {
            "provider": provider.provider,
            "backend": provider.backend,
            "status": "ok",
            "index_snapshot": provider.snapshot_id,
            "fallback_reason": fallback_reason,
            "capabilities": {
                CAPABILITY_DEFINITION: True,
                CAPABILITY_REFERENCES: True,
                CAPABILITY_ROUTE: True,
            },
        }

    return None, {
        "provider": STATIC_LSP_PROVIDER,
        "backend": "unavailable",
        "status": "unavailable",
        "fallback_reason": fallback_reason or "symbol_graph_unavailable",
        "capabilities": {
            CAPABILITY_DEFINITION: False,
            CAPABILITY_REFERENCES: False,
            CAPABILITY_ROUTE: False,
        },
    }


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
    backend: Optional[str] = None,
    fallback_reason: Optional[str] = None,
    behavior_contract: str = _GRAPH_BEHAVIOR_CONTRACT,
    position_granularity: str = "line",
    position_encoding: Optional[str] = None,
) -> LSPProviderMetadata:
    normalized = normalize_lsp_capability(capability)
    return LSPProviderMetadata(
        provider=STATIC_LSP_PROVIDER,
        capability=normalized,
        status=status,
        lsp_method=_LSP_METHODS.get(normalized, normalized),
        backend=backend,
        index_snapshot=snapshot_id,
        fallback_reason=fallback_reason,
        behavior_contract=behavior_contract,
        position_granularity=position_granularity,
        position_encoding=position_encoding,
    )


def normalize_lsp_capability(capability: str) -> str:
    """Normalize LSP method names to provider capability keys."""

    text = str(capability or "").strip()
    if text.startswith("textDocument/"):
        text = text.split("/", 1)[1]
    if text == "codenib/lspRoute":
        return CAPABILITY_ROUTE
    return text


def _normalize_capability(capability: str) -> str:
    return normalize_lsp_capability(capability)


def _graph_snapshot_id(graph: Any) -> Optional[str]:
    if graph is None:
        return None
    content_snapshot = getattr(graph, "snapshot_id", None)
    if content_snapshot:
        return str(content_snapshot)
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
        file_path = str(_node_value(location, "file_path"))
        start_line = int(_node_value(location, "start_line"))
        display_line = start_line + 1
        nodes.append(
            QueriedNode(
                node_name=f"{file_path}:{display_line}",
                type=capability,
                file=file_path,
                node_id=f"{file_path}:{display_line}:{capability}",
                start_line=start_line,
                end_line=int(_node_value(location, "end_line")),
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
    "LSPPositionFallback",
    "LSPProviderMetadata",
    "LSPProviderNodes",
    "NativeOccurrenceQueryAdapter",
    "STATIC_LSP_PROVIDER",
    "StaticLSPProvider",
    "resolve_lsp_provider",
    "select_checkout_lsp_provider",
    "fingerprint_lsp_result",
    "lsp_result_metadata",
    "normalize_native_lsp_nodes",
    "normalize_lsp_capability",
    "preview_lsp_result",
]
