#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Clangd .idx decoder: parse binary RIFF .idx files and build CodeGraph.

.idx format (clangd BackgroundIndex):
  RIFF container (form type "CdIx") with chunks:
    meta - version (uint32)
    stri - zlib-compressed string table (null-separated)
    symb - symbols
    refs - references
    rela - relations
    srcs - include graph / sources
    cmdl - compile command

Two-pass approach for graph building:
  Pass 1: Parse all .idx files, collect symbols/refs/relations
  Pass 2: Resolve definitions, build graph nodes and edges
"""

import logging
import os
import struct
import threading
import time
import zlib
from pathlib import Path
from typing import Any, List, Mapping, Optional, Tuple

from ..graph.code_graph import CodeGraph
from ..types import (
    EDGE_TYPE_CONTAIN,
    EDGE_TYPE_REFERENCE,
    NODE_TYPE_CLASS,
    NODE_TYPE_FIELD,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
    NODE_TYPE_SYMBOL,
    ROOT_NODE,
)
from .clangd_contract import (
    CLANGD_DEFAULT_POSITION_ENCODING,
    CLANGD_POSITION_ENCODING_ENV,
    CLANGD_SUPPORTED_POSITION_ENCODINGS,
    configured_clangd_position_encoding,
)

logger = logging.getLogger(__name__)


# ===================================================================
# Constants
# ===================================================================

# RefKind bitmask (from clangd/index/Ref.h)
REF_KIND_DECLARATION = 0x1
REF_KIND_DEFINITION = 0x2
REF_KIND_REFERENCE = 0x4
REF_KIND_SPELLED = 0x8

# SymbolKind integer constants for programmatic use
KIND_MACRO = 4
KIND_STRUCT = 6
KIND_CLASS = 7
KIND_FUNCTION = 12
KIND_FIELD = 14
KIND_INSTANCE_METHOD = 16
KIND_CLASS_METHOD = 17
KIND_STATIC_METHOD = 18
KIND_CONSTRUCTOR = 22
KIND_DESTRUCTOR = 23

# Relation predicates
RELATION_BASE_OF = 0
RELATION_OVERRIDDEN_BY = 1

# Zero SymbolID (8 bytes = 16 hex chars)
ZERO_SYMBOL_ID = "0" * 16

_NATIVE_QUERY_ENV = "CODENIB_NATIVE_CLANGD_FACT_QUERY_INDEX"
_NATIVE_POSITION_ENCODING_ENV = CLANGD_POSITION_ENCODING_ENV
_NATIVE_QUERY_OFF = frozenset({"0", "false", "no", "off", "disabled"})
_NATIVE_QUERY_AUTO = frozenset({"", "1", "true", "yes", "on", "auto"})
_NATIVE_QUERY_REQUIRED = frozenset({"required", "strict"})
_NATIVE_QUERY_PROMOTED = True
_NATIVE_QUERY_ABI_VERSION = 3
_NATIVE_QUERY_FORMAT = "clangd-riff-fact-query-v3"
_NATIVE_QUERY_NORMALIZATION_PROFILE = "clangd-native-normalization-v3"
_NATIVE_QUERY_SNAPSHOT_SCHEMA = "clangd-fact-query-snapshot-v1"
_NATIVE_QUERY_CAPABILITIES = {
    "definition_by_symbol": True,
    "references_by_symbol": True,
    "position_queries": True,
    "route_queries": True,
}


def native_clangd_query_mode() -> str:
    """Return the configured native clangd symbol-query selection mode."""

    value = os.environ.get(_NATIVE_QUERY_ENV, "auto").strip().lower()
    if value in _NATIVE_QUERY_OFF:
        return "off"
    if value in _NATIVE_QUERY_AUTO:
        return "auto"
    if value in _NATIVE_QUERY_REQUIRED:
        return "required"
    raise ValueError(
        f"{_NATIVE_QUERY_ENV} must be auto, required, or 0/off; got {value!r}"
    )


def native_clangd_query_promoted() -> bool:
    """Whether the checked symbol-only benchmark currently clears the gate."""

    return _NATIVE_QUERY_PROMOTED


def native_clangd_position_encoding(value: Optional[str] = None) -> str:
    """Return the explicit character-unit contract for clangd RIFF ranges."""

    raw = os.environ.get(_NATIVE_POSITION_ENCODING_ENV) if value is None else value
    try:
        return configured_clangd_position_encoding(value)
    except ValueError as exc:
        raise ValueError(
            f"{_NATIVE_POSITION_ENCODING_ENV} must be UTF8, UTF16, or UTF32; "
            f"got {raw!r}"
        ) from exc


def _validate_native_query_contract(contract: Mapping[str, Any]) -> None:
    abi_version = contract.get("abi_version")
    if type(abi_version) is not int or abi_version != _NATIVE_QUERY_ABI_VERSION:
        raise RuntimeError(
            "compiled clangd FactQueryIndex contract mismatch: "
            f"expected ABI {_NATIVE_QUERY_ABI_VERSION}, got {abi_version!r}"
        )
    if contract.get("format") != _NATIVE_QUERY_FORMAT:
        raise RuntimeError(
            "compiled clangd FactQueryIndex contract mismatch: "
            f"expected format {_NATIVE_QUERY_FORMAT!r}"
        )
    if contract.get("normalization_profile") != (_NATIVE_QUERY_NORMALIZATION_PROFILE):
        raise RuntimeError("compiled clangd normalization profile mismatch")
    if contract.get("snapshot_schema") != _NATIVE_QUERY_SNAPSHOT_SCHEMA:
        raise RuntimeError("compiled clangd snapshot schema mismatch")
    if contract.get("snapshot_digest") != "sha256":
        raise RuntimeError("compiled clangd snapshot digest mismatch")
    if contract.get("stable_filename_order") is not True:
        raise RuntimeError("compiled clangd reader does not guarantee stable order")
    if contract.get("preserves_unanchored_relations") is not True:
        raise RuntimeError("compiled clangd reader drops legacy relation rows")
    if contract.get("default_position_encoding") != CLANGD_DEFAULT_POSITION_ENCODING:
        raise RuntimeError("compiled clangd default position encoding mismatch")
    if set(contract.get("supported_position_encodings") or ()) != set(
        CLANGD_SUPPORTED_POSITION_ENCODINGS
    ):
        raise RuntimeError("compiled clangd supported position encodings mismatch")
    if contract.get("capabilities") != _NATIVE_QUERY_CAPABILITIES:
        raise RuntimeError(
            "compiled clangd FactQueryIndex capabilities do not match v3"
        )


# ===================================================================
# Low-level RIFF / .idx parsing
# ===================================================================


def read_riff(data: bytes) -> dict:
    """Parse RIFF container into chunks."""
    assert data[:4] == b"RIFF", f"Not a RIFF file, got {data[:4]!r}"
    total_len = struct.unpack_from("<I", data, 4)[0]
    form_type = data[8:12]
    assert form_type == b"CdIx", f"Not a clangd index, form type: {form_type!r}"

    offset = 12
    chunks = {}
    while offset < 8 + total_len:
        chunk_id = data[offset : offset + 4].decode("ascii")
        chunk_len = struct.unpack_from("<I", data, offset + 4)[0]
        chunk_data = data[offset + 8 : offset + 8 + chunk_len]
        chunks[chunk_id] = chunk_data
        offset += 8 + chunk_len
        if chunk_len % 2 == 1:
            offset += 1  # RIFF padding
    return chunks


def decode_string_table(stri_data: bytes) -> list:
    """Decode zlib-compressed string table."""
    uncompressed_size = struct.unpack_from("<I", stri_data, 0)[0]
    compressed = stri_data[4:]
    if uncompressed_size == 0:
        raw = compressed
    else:
        raw = zlib.decompress(compressed)
    strings = raw.split(b"\x00")
    if strings and strings[-1] == b"":
        strings = strings[:-1]
    return [s.decode("utf-8", errors="replace") for s in strings]


def read_varint(data: bytes, offset: int) -> tuple:
    """Read clangd-style varint (7 bits per byte, MSB = continue)."""
    result = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        result |= (byte & 0x7F) << shift
        offset += 1
        if not (byte & 0x80):
            break
        shift += 7
    return result, offset


def decode_location(data: bytes, offset: int, strings: list) -> tuple:
    """Decode a SymbolLocation: FileURI (varint idx), Start, End."""
    file_idx, offset = read_varint(data, offset)
    start_line, offset = read_varint(data, offset)
    start_col, offset = read_varint(data, offset)
    end_line, offset = read_varint(data, offset)
    end_col, offset = read_varint(data, offset)

    file_uri = strings[file_idx] if file_idx < len(strings) else f"<idx:{file_idx}>"
    return {
        "file": file_uri,
        "start": (start_line, start_col),
        "end": (end_line, end_col),
    }, offset


# ===================================================================
# Chunk decoders
# ===================================================================


def decode_symbols(symb_data: bytes, strings: list) -> list:
    """Decode the symb chunk into a list of symbol records."""
    symbols = []
    offset = 0

    while offset < len(symb_data):
        sym = {}

        # SymbolID: 8 bytes
        if offset + 8 > len(symb_data):
            break
        sym["id"] = symb_data[offset : offset + 8].hex()
        offset += 8

        # SymbolKind: uint8
        sym["kind"] = symb_data[offset]
        offset += 1

        # SymbolLanguage: uint8
        sym["language"] = symb_data[offset]
        offset += 1

        # Name: varint string index
        name_idx, offset = read_varint(symb_data, offset)
        sym["name"] = strings[name_idx] if name_idx < len(strings) else ""

        # Scope: varint string index
        scope_idx, offset = read_varint(symb_data, offset)
        sym["scope"] = strings[scope_idx] if scope_idx < len(strings) else ""

        # TemplateSpecializationArgs: varint string index
        tpl_idx, offset = read_varint(symb_data, offset)
        sym["template_args"] = strings[tpl_idx] if tpl_idx < len(strings) else ""

        # Definition location
        try:
            sym["definition"], offset = decode_location(symb_data, offset, strings)
        except (IndexError, struct.error):
            sym["definition"] = None
            break

        # CanonicalDeclaration location
        try:
            sym["declaration"], offset = decode_location(symb_data, offset, strings)
        except (IndexError, struct.error):
            sym["declaration"] = None
            break

        # References count: varint
        refs_count, offset = read_varint(symb_data, offset)
        sym["references"] = refs_count

        # Flags: uint8
        if offset < len(symb_data):
            sym["flags"] = symb_data[offset]
            offset += 1

        # Signature: varint string index
        sig_idx, offset = read_varint(symb_data, offset)
        sym["signature"] = strings[sig_idx] if sig_idx < len(strings) else ""

        # CompletionSnippetSuffix: varint string index
        snip_idx, offset = read_varint(symb_data, offset)

        # Documentation: varint string index
        doc_idx, offset = read_varint(symb_data, offset)

        # ReturnType: varint string index
        ret_idx, offset = read_varint(symb_data, offset)
        sym["return_type"] = strings[ret_idx] if ret_idx < len(strings) else ""

        # Type: varint string index
        type_idx, offset = read_varint(symb_data, offset)

        # IncludeHeaders
        n_headers, offset = read_varint(symb_data, offset)
        for _ in range(n_headers):
            _h_idx, offset = read_varint(symb_data, offset)
            _refs_and_dir, offset = read_varint(symb_data, offset)

        symbols.append(sym)

    return symbols


def decode_refs(refs_data: bytes, strings: list) -> list:
    """Decode the refs chunk into a list of per-symbol reference records."""
    refs = []
    offset = 0

    while offset < len(refs_data):
        if offset + 8 > len(refs_data):
            break
        sym_id = refs_data[offset : offset + 8].hex()
        offset += 8

        num_refs, offset = read_varint(refs_data, offset)

        sym_refs = []
        for _ in range(num_refs):
            if offset >= len(refs_data):
                break
            kind = refs_data[offset]
            offset += 1

            try:
                loc, offset = decode_location(refs_data, offset, strings)
            except (IndexError, struct.error):
                break

            if offset + 8 > len(refs_data):
                container_id = ""
            else:
                container_id = refs_data[offset : offset + 8].hex()
                offset += 8

            sym_refs.append(
                {
                    "kind": kind,
                    "location": loc,
                    "container": container_id,
                }
            )

        refs.append({"symbol_id": sym_id, "refs": sym_refs})

    return refs


def decode_relations(rela_data: bytes) -> list:
    """Decode the rela chunk into a list of relation records."""
    relations = []
    offset = 0

    while offset < len(rela_data):
        if offset + 17 > len(rela_data):  # 8 + 1 + 8
            break
        subject = rela_data[offset : offset + 8].hex()
        offset += 8
        predicate = rela_data[offset]
        offset += 1
        obj = rela_data[offset : offset + 8].hex()
        offset += 8

        relations.append(
            {
                "subject": subject,
                "predicate": predicate,
                "object": obj,
            }
        )

    return relations


def parse_idx_file(filepath: str) -> dict:
    """
    Parse a single .idx file and return structured data.

    Returns:
        dict with keys: 'symbols', 'refs', 'relations', 'version'
    """
    data = Path(filepath).read_bytes()
    chunks = read_riff(data)

    strings = []
    if "stri" in chunks:
        strings = decode_string_table(chunks["stri"])

    version = None
    if "meta" in chunks:
        version = struct.unpack("<I", chunks["meta"])[0]

    symbols = []
    if "symb" in chunks:
        symbols = decode_symbols(chunks["symb"], strings)

    refs = []
    if "refs" in chunks:
        refs = decode_refs(chunks["refs"], strings)

    relations = []
    if "rela" in chunks:
        relations = decode_relations(chunks["rela"])

    return {
        "symbols": symbols,
        "refs": refs,
        "relations": relations,
        "version": version,
    }


# ===================================================================
# ClangdGraphDecoder — .idx directory → CodeGraph
# ===================================================================


class _NativeClangdRouteView:
    """CodeGraph-shaped view over compact native adjacency and lazy spans."""

    fact_query_index = True

    def __init__(self, decoder: "ClangdGraphDecoder", query_index: Any):
        self.decoder = decoder
        self.index = query_index
        self.supports_graph_routes = bool(
            getattr(query_index, "supports_route_adjacency", False)
        )
        self._span_cache: dict[str, tuple[int | None, int | None]] = {}
        self._span_lock = threading.Lock()

    def __getattr__(self, name: str):
        return getattr(self.index, name)

    def get_route_span(self, name: str) -> tuple[int | None, int | None]:
        cached = self._span_cache.get(name)
        if cached is not None:
            return cached
        info = self.index.get_node_info_by_name(name) or {}
        start = info.get("selection_line")
        if start is None:
            start = info.get("start_line")
        file_path = info.get("file")
        if start is None or not file_path:
            return (info.get("start_line"), info.get("end_line"))
        with self._span_lock:
            cached = self._span_cache.get(name)
            if cached is None:
                started = time.perf_counter()
                cached = self.decoder._find_range(str(file_path), int(start))
                self.decoder.query_profile["native_route_range_enrichment"] = (
                    float(
                        self.decoder.query_profile.get(
                            "native_route_range_enrichment", 0.0
                        )
                    )
                    + time.perf_counter()
                    - started
                )
                self._span_cache[name] = cached
        return cached

    def prepare_query_route(self, query: str, top_k: int):
        return _PreparedNativeClangdRouteView(self, query=query, top_k=top_k)


class _PreparedNativeClangdRouteView:
    """Per-call bounded cache for deterministic query-seeded routing."""

    fact_query_index = True
    supports_graph_routes = True

    def __init__(self, base: _NativeClangdRouteView, *, query: str, top_k: int) -> None:
        from ..agent.lsp_graph import (
            _QUERY_SEED_CANDIDATE_BUDGET,
            _QUERY_SEED_MATCH_BUDGET,
            _QUERY_SEED_MIN_OVERLAP,
            _QUERY_SEED_SCAN_BUDGET,
            _ROUTE_SEED_NODE_TYPES,
            _candidate_score,
            _include_neighbor,
            _query_terms,
            _role,
            _route_neighbors,
            _terms,
        )

        self.base = base
        self.index = base.index
        self._node_by_name: dict[str, Optional[dict[str, Any]]] = {}
        self._node_by_id: dict[int, Optional[dict[str, Any]]] = {}
        self._successors: dict[str, list[int]] = {}
        self._predecessors: dict[str, list[int]] = {}
        query_terms = _query_terms(query)
        self._query_terms = frozenset(query_terms)
        candidates = []
        rows = self.index.route_scan_records(_QUERY_SEED_SCAN_BUDGET)
        for name, node_type, display in rows:
            if node_type and node_type not in _ROUTE_SEED_NODE_TYPES:
                continue
            overlap = _terms(f"{display} {name}") & query_terms
            if len(overlap) < _QUERY_SEED_MIN_OVERLAP:
                continue
            self.get_route_span(name)
            role = _role(self, name, query_terms=query_terms)
            candidate = {
                "name": name,
                "role": role,
                "source": "query_seed",
                "seed": ", ".join(sorted(overlap)[:4]),
                "direct_rank": len(candidates),
            }
            candidate["score"] = _candidate_score(
                self, candidate, query_terms=query_terms
            ) + 6.0 * len(overlap)
            candidates.append(candidate)
            if len(candidates) >= _QUERY_SEED_MATCH_BUDGET:
                break

        candidates.sort(
            key=lambda item: (
                -float(item.get("score") or 0.0),
                str(
                    (self.get_node_info_by_name(item["name"]) or {}).get("unified_name")
                    or item["name"]
                ),
            )
        )
        self._candidates = candidates[: max(1, int(top_k or 12))]

        # Warm exactly the bounded neighborhood that lsp_route may inspect so
        # its deadline covers scoring rather than repeated Python/C++ crossings.
        expanded = 0
        for candidate in self._candidates:
            if expanded >= _QUERY_SEED_CANDIDATE_BUDGET:
                break
            expanded += 1
            for neighbor, _direction in _route_neighbors(self, candidate["name"]):
                if expanded >= _QUERY_SEED_CANDIDATE_BUDGET:
                    break
                neighbor_role = _role(self, neighbor, query_terms=query_terms)
                if not _include_neighbor(
                    self,
                    neighbor,
                    role=neighbor_role,
                    query_terms=query_terms,
                ):
                    continue
                expanded += 1

    def __getattr__(self, name: str):
        return getattr(self.base, name)

    def get_node_info_by_name(self, name: str):
        if name not in self._node_by_name:
            info = self.index.get_node_info_by_name(name)
            self._node_by_name[name] = dict(info) if info is not None else None
        return self._node_by_name[name]

    def get_node_info_by_id(self, vertex_id: int):
        if vertex_id not in self._node_by_id:
            info = self.index.get_node_info_by_id(vertex_id)
            cached = dict(info) if info is not None else None
            self._node_by_id[vertex_id] = cached
            if cached is not None and cached.get("name"):
                self._node_by_name.setdefault(str(cached["name"]), cached)
        return self._node_by_id[vertex_id]

    def get_successors(self, name: str):
        if name not in self._successors:
            self._successors[name] = list(self.index.get_successors(name))
        return self._successors[name]

    def get_predecessors(self, name: str):
        if name not in self._predecessors:
            self._predecessors[name] = list(self.index.get_predecessors(name))
        return self._predecessors[name]

    def get_route_span(self, name: str):
        return self.base.get_route_span(name)

    def prepared_route_seed_candidates(self, *, query_terms, limit: int):
        if frozenset(query_terms) != self._query_terms:
            raise ValueError("prepared native route query terms changed")
        return self._candidates[:limit]


class ClangdHybridQueryProvider:
    """Serve supported symbol, position, and route queries graph-free.

    Unsupported or ambiguous positions and failed/incomplete route adjacency
    materialize the established Python CodeGraph exactly once.
    """

    def __init__(self, decoder: "ClangdGraphDecoder", query_index: Any):
        from ..agent.lsp_provider import StaticLSPProvider

        self.decoder = decoder
        self.graph = query_index
        self.provider_backend = decoder.query_backend
        self._symbol_provider = StaticLSPProvider(
            query_index,
            snapshot_id=decoder.query_snapshot_id,
            backend=self.provider_backend,
        )
        self._route_view = _NativeClangdRouteView(decoder, query_index)
        self._route_provider = (
            StaticLSPProvider(
                self._route_view,
                snapshot_id=decoder.query_snapshot_id,
                backend="native-clangd-route-adjacency-v1",
            )
            if self._route_view.supports_graph_routes
            else None
        )
        self.occurrence_index = None
        self.position_query_index = None
        self._position_provider = None
        self._graph_provider = None
        # Position-view decoding and legacy graph construction both read the
        # clangd snapshot. Serialize their first publication so a concurrent
        # caller never observes a partially initialized provider.
        self._materialization_lock = threading.Lock()
        self.graph_materialization_count = 0
        self.graph_materialization_seconds = 0.0
        self.position_initialization_count = 0
        self.position_initialization_seconds = 0.0
        self.last_fallback_reason = None
        self.last_position_fallback_reason = None
        self.provider = self._symbol_provider.provider
        self.snapshot_id = self._symbol_provider.snapshot_id
        self.position_encoding = decoder.position_encoding

    def _native_position_provider(self):
        from ..agent.lsp_provider import NativeOccurrenceQueryAdapter, StaticLSPProvider

        if self._position_provider is None:
            with self._materialization_lock:
                if self._position_provider is None:
                    # Once the complete graph owns position service, avoid a
                    # second snapshot decode that can no longer remain native.
                    if (
                        self._graph_provider is not None
                        or self.decoder._graph_materialized
                    ):
                        return None
                    started = time.perf_counter()
                    position_index = self.decoder.decode_native_position_query_index()
                    occurrence_index = NativeOccurrenceQueryAdapter(position_index)
                    position_provider = StaticLSPProvider(
                        position_index,
                        snapshot_id=self.snapshot_id,
                        occurrence_index=occurrence_index,
                        backend=self.provider_backend,
                    )
                    self.position_initialization_seconds += (
                        time.perf_counter() - started
                    )
                    self.position_initialization_count += 1
                    self.position_query_index = position_index
                    self.occurrence_index = occurrence_index
                    self._position_provider = position_provider
        return self._position_provider

    def _full_graph_provider(self):
        from ..agent.lsp_provider import StaticLSPProvider

        if self._graph_provider is None:
            with self._materialization_lock:
                if self._graph_provider is None:
                    started = time.perf_counter()
                    graph = self.decoder.materialize_code_graph()
                    # materialize_code_graph() completes range indexing before
                    # returning. Publish the provider only after that complete
                    # graph is safe for every waiting position/route caller.
                    graph_provider = StaticLSPProvider(
                        graph,
                        snapshot_id=self.snapshot_id,
                        backend="lazy-clangd-code-graph-v1",
                    )
                    self.graph_materialization_seconds += time.perf_counter() - started
                    self.graph_materialization_count += 1
                    self._graph_provider = graph_provider
        return self._graph_provider

    def _graph_fallback(self, capability: str, reason: str, arguments: Mapping):
        from dataclasses import replace

        from ..agent.lsp_provider import LSPProviderNodes

        self.last_fallback_reason = reason
        result = getattr(self._full_graph_provider(), capability)(**arguments)
        return LSPProviderNodes(
            result,
            metadata=replace(
                result.lsp_provider_metadata,
                fallback_reason=reason,
            ),
        )

    def can_serve(self, capability: str):
        # The hybrid can serve every established capability: native for symbol
        # definition/reference calls and the complete graph for everything else.
        decision = self._symbol_provider.can_serve(capability)
        if str(capability) in {"route", "codenib/lspRoute"}:
            from dataclasses import replace

            if self._route_provider is not None:
                return self._route_provider.can_serve(capability)
            return replace(
                decision,
                status="ok",
                backend="lazy-clangd-code-graph-v1",
                fallback_reason=None,
            )
        if str(capability) in {
            "definition",
            "references",
            "textDocument/definition",
            "textDocument/references",
        }:
            from dataclasses import replace

            return replace(
                decision,
                behavior_contract="static_native_occurrence_lsp_v1",
                position_granularity="character",
                position_encoding=self.position_encoding,
            )
        return decision

    def definition(self, **arguments):
        if arguments.get("symbol") is not None:
            return self._symbol_provider.definition(**arguments)
        if self._is_position_query(arguments):
            from ..agent.lsp_provider import LSPPositionFallback

            position_provider = self._native_position_provider()
            if position_provider is None:
                return self._position_fallback(
                    "definition",
                    "native_position_legacy_graph_already_materialized",
                    arguments,
                )
            try:
                result = position_provider.definition(**arguments)
            except LSPPositionFallback as exc:
                return self._position_fallback("definition", exc.reason, arguments)
            self.last_position_fallback_reason = None
            return result
        return self._graph_fallback(
            "definition",
            "native_clangd_position_query_requires_complete_graph",
            arguments,
        )

    def references(self, **arguments):
        if arguments.get("symbol") is not None:
            return self._symbol_provider.references(**arguments)
        if self._is_position_query(arguments):
            from ..agent.lsp_provider import LSPPositionFallback

            position_provider = self._native_position_provider()
            if position_provider is None:
                return self._position_fallback(
                    "references",
                    "native_position_legacy_graph_already_materialized",
                    arguments,
                )
            try:
                result = position_provider.references(**arguments)
            except LSPPositionFallback as exc:
                return self._position_fallback("references", exc.reason, arguments)
            self.last_position_fallback_reason = None
            return result
        return self._graph_fallback(
            "references",
            "native_clangd_position_query_requires_complete_graph",
            arguments,
        )

    def route(self, **arguments):
        if self._route_provider is None:
            return self._graph_fallback(
                "route",
                "native_clangd_route_adjacency_unavailable",
                arguments,
            )
        self.decoder._verify_native_snapshot("before native route")
        try:
            route_provider = self._route_provider
            if not (arguments.get("symbols") or []) and arguments.get("query"):
                from ..agent.lsp_provider import StaticLSPProvider

                prepare_started = time.perf_counter()
                prepared = self._route_view.prepare_query_route(
                    query=str(arguments["query"]),
                    top_k=int(arguments.get("top_k") or 12),
                )
                self.decoder.query_profile["native_route_query_preparation"] = (
                    float(
                        self.decoder.query_profile.get(
                            "native_route_query_preparation", 0.0
                        )
                    )
                    + time.perf_counter()
                    - prepare_started
                )
                route_provider = StaticLSPProvider(
                    prepared,
                    snapshot_id=self.snapshot_id,
                    backend="native-clangd-route-adjacency-v1",
                )
            result = route_provider.route(**arguments)
            self.last_fallback_reason = None
            return result
        except MemoryError:
            raise
        except Exception as exc:
            logger.warning(
                "Native clangd route adjacency failed; falling back to CodeGraph: %s",
                exc,
            )
            return self._graph_fallback(
                "route",
                "native_clangd_route_adjacency_failed",
                arguments,
            )

    def position_samples(self, limit: int = 100):
        """Return deterministic exact positions without materializing CodeGraph."""

        position_provider = self._native_position_provider()
        if position_provider is None or self.position_query_index is None:
            raise RuntimeError(
                "native clangd position samples require an unmaterialized snapshot"
            )
        return self.position_query_index.position_samples(limit)

    @staticmethod
    def _is_position_query(arguments: Mapping[str, Any]) -> bool:
        return (
            arguments.get("file_path") is not None
            and arguments.get("line") is not None
            and arguments.get("character") is not None
            and arguments.get("symbol") is None
        )

    def _position_fallback(
        self, capability: str, reason: str, arguments: Mapping[str, Any]
    ):
        from dataclasses import replace

        from ..agent.lsp_provider import LSPProviderNodes

        self.last_position_fallback_reason = reason
        graph_arguments = dict(arguments)
        graph_arguments["character"] = self._graph_character(arguments)
        result = getattr(self._full_graph_provider(), capability)(**graph_arguments)
        return LSPProviderNodes(
            result,
            metadata=replace(
                result.lsp_provider_metadata,
                fallback_reason=reason,
                position_encoding=self.position_encoding,
            ),
        )

    def _graph_character(self, arguments: Mapping[str, Any]) -> int:
        character = int(arguments["character"])
        if self.position_encoding == "UTF32":
            return character
        try:
            relative = Path(str(arguments["file_path"]))
            if relative.is_absolute() or any(
                part in {"", ".", ".."} for part in relative.parts
            ):
                return character
            root = self.decoder.project_root.resolve()
            source_path = (root / relative).resolve()
            source_path.relative_to(root)
            source_line = source_path.read_text(
                encoding="utf-8", errors="strict"
            ).splitlines()[int(arguments["line"])]
        except (OSError, IndexError, ValueError):
            return character
        try:
            if self.position_encoding == "UTF8":
                encoded = source_line.encode("utf-8")
                if character > len(encoded):
                    return character
                prefix = encoded[:character].decode("utf-8")
            else:
                encoded = source_line.encode("utf-16-le")
                if character * 2 > len(encoded):
                    return character
                prefix = encoded[: character * 2].decode("utf-16-le")
        except UnicodeError:
            return character
        return len(prefix)


class ClangdGraphDecoder:
    """
    Decoder that builds a CodeGraph from clangd .idx files.

    Usage:
        decoder = ClangdGraphDecoder(
            idx_directory=".cache/clangd/index",
            project_root="/path/to/project"
        )
        graph = decoder.decode()
    """

    def __init__(
        self,
        idx_directory: str,
        project_root: str,
        position_encoding: Optional[str] = None,
    ):
        self.idx_directory = Path(idx_directory)
        self.project_root = Path(project_root).resolve()
        self.position_encoding = native_clangd_position_encoding(position_encoding)
        self.code_graph = CodeGraph(str(self.project_root))

        # Accumulated data from all .idx files (Pass 1)
        self._symbols = {}  # sym_id -> symbol record dict
        self._refs = {}  # sym_id -> [ref record, ...]
        self._relations = []  # [relation record, ...]

        # Mappings built during Pass 2
        self._id_to_display = (
            {}
        )  # sym_id -> display name (e.g. "Namespace::Class::method")
        self._indexed_files = set()
        self._indexed_directories = set()

        # Buffered edges for batch insert — filled by _queue_edge during the
        # three _add_*_edges passes, flushed once at the end of _build_graph.
        # Each entry: (src_id, tgt_id, edge_type, anchor_file, anchor_line).
        # Multi-edges between the same (src, tgt) pair are allowed so each
        # call site survives as its own edge (matches CodeGraph._add_edge's
        # post-anchor-schema behavior).
        self._pending_edges: List[
            Tuple[int, int, str, Optional[str], Optional[int]]
        ] = []

        # The chunker is needed only for full graph range enrichment. Native
        # symbol/occurrence startup must not instantiate tree-sitter eagerly.
        self._chunker = None
        self._chunks_cache = {}
        self._graph_materialized = False
        self._range_indexes_built = False
        self._records_collected = False
        self.query_index = None
        self.position_query_index = None
        self.query_backend = "not-decoded"
        self.query_fallback_error = None
        self.query_profile: dict[str, Any] = {}
        self.position_query_profile: dict[str, Any] = {}
        self.query_snapshot_id: Optional[str] = None
        self._native_snapshot_error: Optional[str] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def decode(self) -> CodeGraph:
        """Main entry point. Parse all .idx files and build CodeGraph."""
        if self._graph_materialized:
            return self.code_graph
        logger.info(f"Starting clangd idx decode from {self.idx_directory}")

        # Pass 1: collect all data
        if self.query_snapshot_id:
            self._verify_native_snapshot("before legacy record collection")
        self._collect_all_idx()
        if self.query_snapshot_id:
            self._verify_native_snapshot("after legacy record collection")
        self._records_collected = True

        # Build name mapping
        self._build_id_to_display()

        # Pass 2: build the graph
        self.code_graph.add_root_node(ROOT_NODE)
        self._build_graph()
        if self.query_snapshot_id:
            self.code_graph.snapshot_id = self.query_snapshot_id
        self._graph_materialized = True

        return self.code_graph

    def materialize_code_graph(self) -> CodeGraph:
        """Build and range-index the established graph at most once."""

        graph = self.decode()
        if not self._range_indexes_built:
            graph.build_range_indexes()
            self._range_indexes_built = True
        return graph

    def _current_native_snapshot(self) -> Mapping[str, Any]:
        try:
            import codenib_core
        except ImportError as exc:
            raise RuntimeError(
                "compiled codenib_core extension is unavailable"
            ) from exc
        snapshot = getattr(codenib_core, "clangd_fact_query_snapshot", None)
        if not callable(snapshot):
            raise RuntimeError(
                "compiled core does not expose clangd snapshot verification"
            )
        receipt = snapshot(
            idx_directory=str(self.idx_directory),
            project_root=str(self.project_root),
            position_encoding=self.position_encoding,
        )
        if not isinstance(receipt, Mapping):
            raise RuntimeError(
                "compiled clangd snapshot verifier returned non-mapping data"
            )
        return receipt

    def _verify_native_snapshot(self, stage: str) -> Mapping[str, Any] | None:
        if self._native_snapshot_error is not None:
            raise RuntimeError(self._native_snapshot_error)
        if not self.query_snapshot_id:
            return None
        started = time.perf_counter()
        try:
            receipt = self._current_native_snapshot()
        except MemoryError:
            raise
        except Exception as exc:
            self._native_snapshot_error = (
                f"clangd index snapshot verification failed {stage}: "
                f"{type(exc).__name__}: {exc}"
            )
            raise RuntimeError(self._native_snapshot_error) from exc
        finally:
            self.query_profile["lazy_snapshot_verify"] = (
                float(self.query_profile.get("lazy_snapshot_verify", 0.0))
                + time.perf_counter()
                - started
            )
        observed = receipt.get("snapshot_id")
        if observed != self.query_snapshot_id:
            self._native_snapshot_error = (
                f"clangd index snapshot changed {stage}: expected "
                f"{self.query_snapshot_id}, observed {observed}"
            )
            raise RuntimeError(self._native_snapshot_error)
        return receipt

    def _decode_native_query_index(self, *, include_occurrences: bool = False):
        if self._records_collected or self._graph_materialized:
            raise RuntimeError(
                "native clangd snapshot cannot bind records collected before "
                "the receipt"
            )
        try:
            import codenib_core
        except ImportError as exc:
            raise RuntimeError(
                "compiled codenib_core extension is unavailable"
            ) from exc

        decode = getattr(codenib_core, "decode_clangd_fact_query_index", None)
        contract_reader = getattr(codenib_core, "clangd_fact_query_contract", None)
        if not callable(decode) or not callable(contract_reader):
            raise RuntimeError("compiled core does not expose clangd FactQueryIndex v3")
        _validate_native_query_contract(contract_reader())

        started = time.perf_counter()
        payload = decode(
            idx_directory=str(self.idx_directory),
            project_root=str(self.project_root),
            position_encoding=self.position_encoding,
            include_occurrences=include_occurrences,
        )
        binding_seconds = time.perf_counter() - started
        if not isinstance(payload, Mapping):
            raise RuntimeError("native clangd query decoder returned non-mapping data")
        if payload.get("format") != _NATIVE_QUERY_FORMAT:
            raise RuntimeError("native clangd query decoder returned unknown format")
        if payload.get("graph_materialized") is not False:
            raise RuntimeError("native clangd query decoder materialized a graph")
        if payload.get("position_records_included") is not include_occurrences:
            raise RuntimeError("native clangd query decoder changed occurrence scope")

        index = payload.get("index")
        if index is None or getattr(index, "fact_query_index", None) is not True:
            raise RuntimeError("native clangd query payload is missing its index")
        if getattr(index, "materializes_graph", None) is not False:
            raise RuntimeError(
                "native clangd query index does not guarantee graph-free use"
            )
        if getattr(index, "supports_route_adjacency", None) is not True:
            raise RuntimeError(
                "native clangd query index does not provide complete route adjacency"
            )
        expected_capabilities = {
            **_NATIVE_QUERY_CAPABILITIES,
            "position_queries": include_occurrences,
        }
        if getattr(index, "capabilities", None) != expected_capabilities:
            raise RuntimeError("native clangd query index capabilities do not match v3")
        if getattr(index, "requires_anchored_references", None) is not False:
            raise RuntimeError(
                "native clangd query index cannot preserve relation references"
            )
        if include_occurrences:
            required_position_api = (
                "has_occurrence_at",
                "position_definitions",
                "position_references",
                "position_samples",
            )
            if any(
                not callable(getattr(index, name, None))
                for name in required_position_api
            ):
                raise RuntimeError(
                    "compiled core does not expose the native clangd occurrence API"
                )
        if getattr(index, "position_encoding", None) != self.position_encoding:
            raise RuntimeError("native clangd index position encoding disagrees")
        snapshot_id = payload.get("snapshot_id")
        if not isinstance(snapshot_id, str) or not snapshot_id.startswith(
            "clangd_fact_query:sha256:"
        ):
            raise RuntimeError("native clangd decoder returned an invalid snapshot")
        if getattr(index, "snapshot_id", None) != snapshot_id:
            raise RuntimeError("native clangd index and receipt snapshots disagree")

        native_decode = int(payload.get("native_decode_ns") or 0) / 1_000_000_000
        native_index = int(payload.get("native_index_ns") or 0) / 1_000_000_000
        profile: dict[str, Any] = {
            "binding": binding_seconds,
            "native_decode": native_decode,
            "native_index": native_index,
            "boundary_residual": max(
                0.0, binding_seconds - native_decode - native_index
            ),
            "record_count": int(getattr(index, "record_count", 0)),
            "definition_count": int(getattr(index, "symbol_count", 0)),
            "reference_count": int(getattr(index, "reference_count", 0)),
            "occurrence_count": int(getattr(index, "occurrence_count", 0)),
            "position_encoding": self.position_encoding,
            "snapshot_id": snapshot_id,
            "snapshot_file_count": int(payload.get("snapshot_file_count") or 0),
            "snapshot_index_bytes": int(payload.get("snapshot_index_bytes") or 0),
        }
        count_fields = {
            "file_count",
            "raw_symbol_count",
            "raw_reference_count",
            "definition_count",
            "reference_count",
            "occurrence_count",
            "decoded_record_count",
            "index_bytes",
            "decompressed_string_bytes",
            "materialized_string_bytes",
            "string_entry_count",
        }
        for name, value in (payload.get("decode_profile_ns") or {}).items():
            profile[f"native_{name}"] = (
                int(value) if name in count_fields else int(value) / 1_000_000_000
            )
        if self.query_snapshot_id is not None and snapshot_id != self.query_snapshot_id:
            raise RuntimeError("native clangd position snapshot disagrees with symbols")
        self.query_snapshot_id = snapshot_id
        return index, profile

    def decode_query_index(self):
        """Return the gated symbol index or the complete compatible graph."""

        if self.query_index is not None:
            return self.query_index
        total_started = time.perf_counter()
        mode = native_clangd_query_mode()
        if mode == "off" or (mode == "auto" and not _NATIVE_QUERY_PROMOTED):
            self.query_index = self.materialize_code_graph()
            self.query_backend = (
                "legacy-query-disabled"
                if mode == "off"
                else "legacy-query-not-promoted"
            )
            self.query_profile = {
                "fact_query_native": 0,
                "query_total": time.perf_counter() - total_started,
            }
            return self.query_index

        try:
            self.query_index, self.query_profile = self._decode_native_query_index()
            self.query_backend = "native-clangd-fact-query-v1"
            self.query_profile["fact_query_native"] = 1
        except MemoryError:
            raise
        except Exception as exc:
            if mode == "required":
                raise
            self.query_fallback_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "Native clangd FactQueryIndex unavailable; falling back to "
                "ClangdGraphDecoder: %s",
                self.query_fallback_error,
            )
            self.query_index = self.materialize_code_graph()
            self.query_backend = "legacy-query-fallback"
            self.query_profile = {"fact_query_native": 0}

        self.query_profile["query_total"] = time.perf_counter() - total_started
        return self.query_index

    def decode_query_provider(self):
        """Return native symbol/position queries with lazy route fallback."""

        from ..agent.lsp_provider import StaticLSPProvider

        index = self.decode_query_index()
        if getattr(index, "fact_query_index", False):
            return ClangdHybridQueryProvider(self, index)
        return StaticLSPProvider(index)

    def decode_native_position_query_index(self):
        """Build the occurrence view lazily without constructing CodeGraph."""

        if self.position_query_index is not None:
            return self.position_query_index
        if self.query_index is None:
            self.decode_native_query_provider()
        if self.query_backend != "native-clangd-fact-query-v1":
            raise RuntimeError("native clangd position view requires native symbols")
        (
            self.position_query_index,
            self.position_query_profile,
        ) = self._decode_native_query_index(include_occurrences=True)
        # Publish the capability-specific profile after the complete index is
        # available. Route-first workers therefore retain the symbol-only
        # startup profile, while position workloads report occurrence costs.
        self.query_profile.update(self.position_query_profile)
        self.query_profile["fact_query_native"] = 1
        self.query_profile["position_fact_query_native"] = 1
        return self.position_query_index

    def decode_native_query_provider(self):
        """Return the native provider without silently building a legacy graph."""

        if self.query_index is None:
            total_started = time.perf_counter()
            self.query_index, self.query_profile = self._decode_native_query_index()
            self.query_backend = "native-clangd-fact-query-v1"
            self.query_profile["fact_query_native"] = 1
            self.query_profile["query_total"] = time.perf_counter() - total_started
        if self.query_backend != "native-clangd-fact-query-v1" or not getattr(
            self.query_index, "fact_query_index", False
        ):
            raise RuntimeError(
                "clangd query decoder is already bound to a non-native backend"
            )
        return ClangdHybridQueryProvider(self, self.query_index)

    def save_graph(self, output_path: str):
        """Save the code graph to a file."""
        self.code_graph.save_graph(output_path)

    # ------------------------------------------------------------------
    # Pass 1: Collect
    # ------------------------------------------------------------------

    def _collect_all_idx(self):
        """Parse all .idx files and merge symbols/refs/relations."""
        idx_files = sorted(self.idx_directory.glob("*.idx"))
        if not idx_files:
            logger.warning(f"No .idx files found in {self.idx_directory}")
            return

        for idx_file in idx_files:
            try:
                parsed = parse_idx_file(str(idx_file))
            except Exception as e:
                logger.warning(f"Failed to parse {idx_file.name}: {e}")
                continue

            # Merge symbols (first-seen wins for same ID)
            for sym in parsed["symbols"]:
                sym_id = sym["id"]
                if sym_id not in self._symbols:
                    self._symbols[sym_id] = sym
                else:
                    # If existing has no valid definition but this one does, update
                    existing_def = self._symbols[sym_id].get("definition")
                    new_def = sym.get("definition")
                    if (
                        not existing_def or existing_def.get("file", "") == ""
                    ) and new_def:
                        self._symbols[sym_id] = sym

            # Merge refs (union all)
            for ref_group in parsed["refs"]:
                sym_id = ref_group["symbol_id"]
                if sym_id not in self._refs:
                    self._refs[sym_id] = []
                self._refs[sym_id].extend(ref_group["refs"])

            # Merge relations
            self._relations.extend(parsed["relations"])

        logger.info(
            f"Collected {len(self._symbols)} symbols, "
            f"{sum(len(v) for v in self._refs.values())} refs, "
            f"{len(self._relations)} relations"
        )

    # ------------------------------------------------------------------
    # Name mapping
    # ------------------------------------------------------------------

    def _build_id_to_display(self):
        """Build sym_id -> display name mapping from symbols."""
        for sym_id, sym in self._symbols.items():
            name = sym.get("name", "")
            scope = sym.get("scope", "")
            qualified = f"{scope}{name}" if scope else name
            self._id_to_display[sym_id] = qualified

    def _resolve_sym_id(self, sym_id: str) -> Optional[str]:
        """Resolve a symbol ID to its vertex key, or None if unknown.

        Returns sym_id itself as the vertex key (each overload gets its
        own node). Returns None if the sym_id was not seen during parsing.
        """
        if sym_id in self._id_to_display:
            return sym_id
        return None

    def _sym_id_to_display_name(self, sym_id: str) -> str:
        """Get the display name for a symbol ID (e.g. 'Ns::Class::method')."""
        return self._id_to_display.get(sym_id, "")

    # ------------------------------------------------------------------
    # File URI handling
    # ------------------------------------------------------------------

    def _file_uri_to_relative(self, uri: str) -> str:
        """Convert file URI to project-relative path."""
        if uri.startswith("file://"):
            abs_path = uri[7:]
        else:
            abs_path = uri

        try:
            rel = Path(abs_path).relative_to(self.project_root)
            return str(rel)
        except ValueError:
            return abs_path

    # ------------------------------------------------------------------
    # Kind mapping
    # ------------------------------------------------------------------

    @staticmethod
    def _kind_to_node_type(kind: int) -> str:
        """Map clangd SymbolKind to CodeGraph NODE_TYPE."""
        if kind in (KIND_STRUCT, KIND_CLASS):
            return NODE_TYPE_CLASS
        elif kind == KIND_FUNCTION:
            return NODE_TYPE_FUNCTION
        elif kind in (
            KIND_INSTANCE_METHOD,
            KIND_CLASS_METHOD,
            KIND_STATIC_METHOD,
            KIND_CONSTRUCTOR,
            KIND_DESTRUCTOR,
        ):
            return NODE_TYPE_METHOD
        elif kind == KIND_FIELD:
            return NODE_TYPE_FIELD
        else:
            return NODE_TYPE_SYMBOL

    # ------------------------------------------------------------------
    # Definition resolution
    # ------------------------------------------------------------------

    def _resolve_definition(self, sym_id: str) -> dict:
        """
        Resolve the primary definition location for a symbol.

        Priority:
          1. [Definition|Spelled] ref -> hand-written, not macro-expanded
          2. [Definition] ref (no Spelled) -> macro expansion site
          3. symb definition -> fallback
        """
        result = {
            "file": None,
            "line": 0,
            "col": 0,
            "macro_expanded": False,
            "spelling_file": None,
            "spelling_line": None,
        }

        ref_list = self._refs.get(sym_id, [])
        sym = self._symbols.get(sym_id)

        # Collect definition refs
        spelled_defs = []
        expansion_defs = []
        for ref in ref_list:
            kind = ref["kind"]
            is_def = kind & REF_KIND_DEFINITION
            is_spelled = kind & REF_KIND_SPELLED
            if is_def:
                if is_spelled:
                    spelled_defs.append(ref)
                else:
                    expansion_defs.append(ref)

        # Priority 1: Spelled Definition
        if spelled_defs:
            loc = spelled_defs[0]["location"]
            result["file"] = loc["file"]
            result["line"] = loc["start"][0]
            result["col"] = loc["start"][1]
            result["macro_expanded"] = False
            return result

        # Priority 2: non-Spelled Definition (possible macro expansion)
        if expansion_defs:
            loc = expansion_defs[0]["location"]
            result["file"] = loc["file"]
            result["line"] = loc["start"][0]
            result["col"] = loc["start"][1]
            if sym and sym.get("definition"):
                sym_def = sym["definition"]
                if sym_def.get("file"):
                    spelling_file = sym_def["file"]
                    spelling_line = sym_def["start"][0]
                    expansion_file = loc["file"]
                    expansion_line = loc["start"][0]
                    if (
                        spelling_file != expansion_file
                        or spelling_line != expansion_line
                    ):
                        result["macro_expanded"] = True
                        result["spelling_file"] = spelling_file
                        result["spelling_line"] = spelling_line
            return result

        # Priority 3: symb definition (fallback)
        if sym and sym.get("definition") and sym["definition"].get("file"):
            sym_def = sym["definition"]
            result["file"] = sym_def["file"]
            result["line"] = sym_def["start"][0]
            result["col"] = sym_def["start"][1]
            return result

        return result

    # ------------------------------------------------------------------
    # Range detection via CppCodeChunker
    # ------------------------------------------------------------------

    def _find_range(self, file_path: str, line: int) -> Tuple[int, int]:
        """Find the scope range for a symbol at the given line."""
        chunks = self._get_file_chunks(file_path)
        if chunks:
            for chunk in chunks:
                if chunk.start_line <= line <= chunk.end_line:
                    return (chunk.start_line, chunk.end_line)
        return (line, line)

    def _get_file_chunks(self, file_path: str):
        """Get code chunks for a file, with caching."""
        if file_path in self._chunks_cache:
            return self._chunks_cache[file_path]

        full_path = self.project_root / file_path
        if not full_path.exists():
            self._chunks_cache[file_path] = None
            return None

        try:
            if self._chunker is None:
                from ..code_chunking import CppCodeChunker

                self._chunker = CppCodeChunker()
            chunks = self._chunker.chunk_file(str(full_path))
            self._chunks_cache[file_path] = chunks
            return chunks
        except Exception as e:
            logger.debug(f"Error chunking {file_path}: {e}")
            self._chunks_cache[file_path] = None
            return None

    # ------------------------------------------------------------------
    # Pass 2: Build graph
    # ------------------------------------------------------------------

    def _build_graph(self):
        """Build the complete CodeGraph from collected data."""
        self._add_symbol_nodes()
        self._add_contain_edges()
        self._add_reference_edges()
        self._add_relation_edges()
        self._flush_edges()

    # ------------------------------------------------------------------
    # Batch-edge helpers — accumulate (src_id, tgt_id) -> type during the
    # three edge-adding passes and flush once at the end. igraph's
    # per-edge add_edge() was 72% of decode wall time on fmt (483 .idx →
    # 27k edges); the single batched add_edges() call collapses that to
    # under 1% of wall time.
    # ------------------------------------------------------------------

    def _queue_edge(
        self,
        src_name: str,
        tgt_name: str,
        edge_type: str,
        anchor_file: Optional[str] = None,
        anchor_line: Optional[int] = None,
    ) -> None:
        """Buffer an edge for the batch flush.

        Multi-edges between the same `(src, tgt)` pair are allowed; each
        call site (or relation) becomes its own edge. Pass `anchor_file` /
        `anchor_line` for reference edges that need range-query support.
        """
        n2v = self.code_graph.name_to_vertex
        src_id = n2v.get(src_name)
        tgt_id = n2v.get(tgt_name)
        if src_id is None or tgt_id is None:
            return
        self._pending_edges.append(
            (src_id, tgt_id, edge_type, anchor_file, anchor_line)
        )

    def _flush_edges(self) -> None:
        """Insert all queued edges in one batched igraph call."""
        if not self._pending_edges:
            return
        g = self.code_graph.graph
        pairs = [(s, t) for s, t, *_ in self._pending_edges]
        types = [e[2] for e in self._pending_edges]
        anchor_files = [e[3] for e in self._pending_edges]
        anchor_lines = [e[4] for e in self._pending_edges]
        g.add_edges(
            pairs,
            attributes={
                "type": types,
                "anchor_file": anchor_files,
                "anchor_line": anchor_lines,
            },
        )
        self._pending_edges.clear()

    def _ensure_file_hierarchy(self, relative_path: str):
        """Add file and directory nodes to the graph if not already present."""
        if relative_path in self._indexed_files:
            return
        self._indexed_files.add(relative_path)

        # Build directory hierarchy
        dir_path = Path(relative_path).parent
        while dir_path != dir_path.parent and str(dir_path) != ".":
            dir_path_str = str(dir_path)
            if dir_path_str not in self._indexed_directories:
                self.code_graph.add_directory_node(dir_path_str)
                self._indexed_directories.add(dir_path_str)
                parent = str(dir_path.parent)
                if parent == ".":
                    parent = ROOT_NODE
                self.code_graph._add_edge(parent, dir_path_str, EDGE_TYPE_CONTAIN)
            dir_path = dir_path.parent

        # Add file node
        self.code_graph.add_file_node(relative_path)
        parent_dir = str(Path(relative_path).parent)
        if parent_dir == ".":
            parent_dir = ROOT_NODE
        self.code_graph._add_edge(parent_dir, relative_path, EDGE_TYPE_CONTAIN)

    def _add_symbol_nodes(self):
        """Add all symbol nodes to the graph.

        Uses sym_id as the unique vertex key so that overloaded functions
        each get their own node.  The human-readable qualified name is
        stored in the ``display_name`` attribute.
        """
        for sym_id, sym in self._symbols.items():
            kind = sym.get("kind", 0)

            # Skip macros
            if kind == KIND_MACRO:
                continue

            node_type = self._kind_to_node_type(kind)
            qualified_name = self._sym_id_to_display_name(sym_id)
            if not qualified_name:
                continue

            # Resolve definition location
            def_info = self._resolve_definition(sym_id)
            if not def_info["file"]:
                continue

            # Convert file URI to relative path
            relative_file = self._file_uri_to_relative(def_info["file"])

            # Skip symbols defined outside the project root
            if relative_file.startswith("/"):
                continue
            # Skip vendored/third-party code
            if "third_party/" in relative_file:
                continue

            # Ensure file hierarchy exists
            self._ensure_file_hierarchy(relative_file)

            # Find range
            line = def_info["line"]
            scope_start, scope_end = self._find_range(relative_file, line)

            # Add node — sym_id is the vertex key (unique per overload)
            node_key = sym_id
            self.code_graph.add_symbol_node(
                node_key,
                line,
                scope_start_line=scope_start,
                scope_end_line=scope_end,
                symbol_type=node_type,
            )

            # Set extra clangd-specific attributes
            if node_key in self.code_graph.name_to_vertex:
                vid = self.code_graph.name_to_vertex[node_key]
                # unified_name uses "." as member separator (cross-language convention)
                unified_display = qualified_name.replace("::", ".")
                if node_type in (NODE_TYPE_FUNCTION, NODE_TYPE_METHOD):
                    unified_display = f"{unified_display}()"
                self.code_graph.graph.vs[vid][
                    "unified_name"
                ] = f"{relative_file}:{unified_display}"
                self.code_graph.graph.vs[vid]["file"] = relative_file
                self.code_graph.graph.vs[vid]["macro_expanded"] = def_info[
                    "macro_expanded"
                ]
                if def_info["spelling_file"]:
                    self.code_graph.graph.vs[vid]["spelling_file"] = (
                        self._file_uri_to_relative(def_info["spelling_file"])
                    )
                if def_info["spelling_line"]:
                    self.code_graph.graph.vs[vid]["spelling_line"] = def_info[
                        "spelling_line"
                    ]

    def _add_contain_edges(self):
        """Add containment edges from refs container field."""
        contained_syms = set()

        for sym_id, ref_list in self._refs.items():
            sym_name = self._resolve_sym_id(sym_id)
            if not sym_name:
                continue
            if sym_name not in self.code_graph.name_to_vertex:
                continue

            for ref in ref_list:
                kind = ref["kind"]
                is_def = kind & REF_KIND_DEFINITION
                container_id = ref.get("container", "")

                if is_def and container_id and container_id != ZERO_SYMBOL_ID:
                    container_name = self._resolve_sym_id(container_id)
                    if (
                        container_name
                        and container_name in self.code_graph.name_to_vertex
                    ):
                        self._queue_edge(container_name, sym_name, EDGE_TYPE_CONTAIN)
                        contained_syms.add(sym_id)
                        break

        # Top-level symbols: file --contain--> symbol
        for sym_id, sym in self._symbols.items():
            if sym_id in contained_syms:
                continue
            if sym.get("kind", 0) == KIND_MACRO:
                continue

            sym_name = self._resolve_sym_id(sym_id)
            if not sym_name or sym_name not in self.code_graph.name_to_vertex:
                continue

            vid = self.code_graph.name_to_vertex[sym_name]
            attrs = self.code_graph.graph.vs[vid].attributes()
            file_path = attrs.get("file")
            if file_path and file_path in self.code_graph.name_to_vertex:
                self._queue_edge(file_path, sym_name, EDGE_TYPE_CONTAIN)

    def _add_reference_edges(self):
        """Add reference edges from refs with Reference kind.

        Each ref carries a ``location`` (file URI + (line, col)) — extract it
        as ``anchor_file`` / ``anchor_line`` so range queries can find call
        sites. Note that with multi-edge support (no `_add_edge` dedup),
        repeated calls between the same `(container, sym)` pair create one
        edge per call site instead of being collapsed into one.
        """
        for sym_id, ref_list in self._refs.items():
            sym_name = self._resolve_sym_id(sym_id)
            if not sym_name:
                continue
            if sym_name not in self.code_graph.name_to_vertex:
                continue

            for ref in ref_list:
                kind = ref["kind"]
                is_reference = kind & REF_KIND_REFERENCE
                container_id = ref.get("container", "")

                if is_reference and container_id and container_id != ZERO_SYMBOL_ID:
                    container_name = self._resolve_sym_id(container_id)
                    if (
                        container_name
                        and container_name in self.code_graph.name_to_vertex
                    ):
                        loc = ref.get("location") or {}
                        anchor_file_uri = loc.get("file")
                        anchor_file = (
                            self._file_uri_to_relative(anchor_file_uri)
                            if anchor_file_uri
                            else None
                        )
                        start = loc.get("start")
                        anchor_line = start[0] if start else None
                        self._queue_edge(
                            container_name,
                            sym_name,
                            EDGE_TYPE_REFERENCE,
                            anchor_file=anchor_file,
                            anchor_line=anchor_line,
                        )

    def _add_relation_edges(self):
        """Add edges from rela chunk (inheritance, override)."""
        for rel in self._relations:
            subject_id = rel["subject"]
            object_id = rel["object"]
            predicate = rel["predicate"]

            subject_name = self._resolve_sym_id(subject_id)
            object_name = self._resolve_sym_id(object_id)

            if not subject_name or not object_name:
                continue
            if subject_name not in self.code_graph.name_to_vertex:
                continue
            if object_name not in self.code_graph.name_to_vertex:
                continue

            if predicate == RELATION_BASE_OF:
                # "subject is base of object" → derived(object) references base(subject)
                self._queue_edge(object_name, subject_name, EDGE_TYPE_REFERENCE)
            elif predicate == RELATION_OVERRIDDEN_BY:
                self._queue_edge(object_name, subject_name, EDGE_TYPE_REFERENCE)
