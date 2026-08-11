#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Full-graph Python facade around the low-level `codenib_core` extension.

Exposes a `SCIPDecoderCore` that produces a fully-populated `CodeGraph`
identical to the pure-Python serial decoder, but with the C++ core/ doing
the parsing and parallel work under the hood. The extension's flat
``decode_scip`` result is an internal transport; this facade also applies
shared source-aware post-decode layers such as TypeScript import enrichment.

Falls back to ImportError if the extension isn't built (callers can catch
and fall back to the serial decoder).
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from ..graph.code_graph import CodeGraph
from ..languages import core_decoder_languages
from ..log_utils import get_logger
from .fact_batch_buffer import FactBatchBufferView, validate_compiled_contract

logger = get_logger(__name__)


try:
    # Primary import path — codenib_core must be on sys.path / installed.
    # Note: `codenib/__init__.py` also pre-imports this module so that the
    # extension's bundled libigraph wins the dynamic-linker race against the
    # system libigraph that Python's `igraph` package brings in. The try here
    # remains the source of truth for the optional-dependency contract.
    import codenib_core as _cpp  # type: ignore[import-not-found]
except ImportError as _import_err:  # pragma: no cover — optional dep
    _cpp = None
    _IMPORT_ERR: Optional[BaseException] = _import_err
else:
    _IMPORT_ERR = None


# Canonical languages accepted by the C++ backend. Aliases such as "ts" and
# "js" are configured in LanguageSpec.core_decoder_aliases.
SUPPORTED_LANGUAGES = core_decoder_languages(include_aliases=False)
ACCEPTED_LANGUAGES = core_decoder_languages(include_aliases=True)

_FACT_BUFFER_ENV = "CODENIB_CORE_FACT_BUFFER"
_FACT_BUFFER_OFF = frozenset({"0", "false", "no", "off", "disabled"})
_FACT_BUFFER_ON = frozenset({"", "1", "true", "yes", "on", "auto"})
_FACT_BUFFER_REQUIRED = frozenset({"required", "strict"})


def _ensure_available() -> None:
    if _cpp is None:
        raise ImportError(
            "codenib_core pybind module is not available. "
            "Build it with: cmake -S core -B build/core && "
            "cmake --build build/core && "
            "export PYTHONPATH=build/core:$PYTHONPATH. "
            f"Original error: {_IMPORT_ERR}"
        )


def _build_code_graph(
    vertices: List[Dict], edges: List, project_root: Optional[str] = None
) -> CodeGraph:
    """Build a Python CodeGraph from the C++ binding's flat dict output.

    Uses igraph batch APIs directly to avoid per-vertex / per-edge Python
    overhead. Restores the `name_to_vertex` and `symbol_ranges` indices so
    downstream code that queries them works identically to the Python
    decoders.
    """
    code_graph = CodeGraph(project_root)
    g = code_graph.graph

    # --- Vertices ---
    names = [v["name"] for v in vertices]
    attrs = {
        "name": names,
        "type": [v["type"] for v in vertices],
        "file": [v["file"] for v in vertices],
        "start_line": [v["start_line"] for v in vertices],
        "end_line": [v["end_line"] for v in vertices],
        "selection_line": [v.get("selection_line") for v in vertices],
        "unified_name": [v["unified_name"] for v in vertices],
        "symbol_kind": [v.get("symbol_kind") for v in vertices],
        "has_definition": [v.get("has_definition") for v in vertices],
    }
    g.add_vertices(len(vertices), attributes=attrs)

    n2v = code_graph.name_to_vertex
    ranges = code_graph.symbol_ranges
    for i, v in enumerate(vertices):
        n2v[v["name"]] = i
        if v["start_line"] is not None and v["end_line"] is not None:
            ranges[v["name"]] = (v["start_line"], v["end_line"])

    # --- Edges ---
    # Multi-edges between the same `(src, tgt)` pair are allowed (one per
    # call site). The current C++ binding emits 5-tuples
    # `(src, tgt, type, anchor_file_or_None, anchor_line_or_None)` so range
    # queries work on graphs produced by this backend. The 3-tuple branch
    # below is kept for backward compatibility with older binary builds.
    if edges:
        pairs: List = []
        types: List[str] = []
        anchor_files: List[Optional[str]] = []
        anchor_lines: List[Optional[int]] = []
        for edge in edges:
            # Standard format is 5-tuple; legacy 3-tuple is tolerated.
            if len(edge) >= 5:
                src, tgt, etype, a_file, a_line = edge[:5]
            else:
                src, tgt, etype = edge[:3]
                a_file = None
                a_line = None
            src_id = n2v.get(src)
            tgt_id = n2v.get(tgt)
            if src_id is None or tgt_id is None:
                continue
            pairs.append((src_id, tgt_id))
            types.append(etype)
            anchor_files.append(a_file)
            anchor_lines.append(a_line)
        if pairs:
            g.add_edges(
                pairs,
                attributes={
                    "type": types,
                    "anchor_file": anchor_files,
                    "anchor_line": anchor_lines,
                },
            )

    return code_graph


def _fact_buffer_mode() -> str:
    # The v1 transport remains opt-in until a measured end-to-end benchmark
    # clears the acceleration gate. It is useful as a semantic batch boundary,
    # but graph-compatible and eager logical-object consumers remain slower
    # than legacy transport on representative repositories.
    value = os.environ.get(_FACT_BUFFER_ENV, "off").strip().lower()
    if value in _FACT_BUFFER_OFF:
        return "off"
    if value in _FACT_BUFFER_ON:
        return "auto"
    if value in _FACT_BUFFER_REQUIRED:
        return "required"
    raise ValueError(
        f"{_FACT_BUFFER_ENV} must be auto, required, or 0/off; got {value!r}"
    )


def _fact_buffer_profile_digest(language: str) -> str:
    payload = f"codenib-core-fact-buffer-v1:{language}".encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class SCIPDecoderCore:
    """Decoder facade that runs the C++ core/ pipeline and returns a CodeGraph.

    Args:
        index_file_path: path to index.decoded
        project_root: project root (for Go/Rust config file parsing)
        language: one of the registry languages with core decoder support.
    """

    def __init__(
        self,
        index_file_path: str,
        project_root: Optional[str] = None,
        language: str = "python",
    ):
        self.index_file_path = index_file_path
        self.project_root = str(project_root) if project_root else None
        self.language = language.lower()
        if self.language not in ACCEPTED_LANGUAGES:
            raise ValueError(
                f"Unknown language {language!r}. " f"Supported: {SUPPORTED_LANGUAGES}."
            )
        _ensure_available()
        self.timings: Dict[str, float | int] = {}
        self.code_graph: Optional[CodeGraph] = None
        self.fact_buffer_view: FactBatchBufferView | None = None
        self.transport_backend = "uninitialized"
        self.transport_fallback_error: str | None = None

    def _decode_fact_buffer(self) -> tuple[CodeGraph, dict[str, float | int]]:
        if not hasattr(_cpp, "decode_scip_fact_buffer") or not hasattr(
            _cpp, "fact_batch_buffer_contract"
        ):
            raise RuntimeError("compiled core does not expose FactBatchBuffer v1")
        validate_compiled_contract(_cpp.fact_batch_buffer_contract())
        started = time.perf_counter()
        payload = _cpp.decode_scip_fact_buffer(
            index_file=self.index_file_path,
            profile_digest=_fact_buffer_profile_digest(self.language),
            project_root=self.project_root,
            language=self.language,
            copy_buffers=False,
        )
        binding_seconds = time.perf_counter() - started
        view = FactBatchBufferView(payload)
        started = time.perf_counter()
        graph = view.materialize_code_graph(project_root=self.project_root)
        # Preserve the established facade contract: an omitted project root is
        # represented as None even when the native envelope stores an empty
        # effective root string.
        graph.project_root = self.project_root
        materialize_seconds = time.perf_counter() - started
        native_decode = (view.meta.native_decode_ns or 0) / 1_000_000_000
        native_encode = (view.meta.native_encode_ns or 0) / 1_000_000_000
        self.fact_buffer_view = view
        timings: dict[str, float | int] = {
            "core_decode": binding_seconds,
            "native_decode": native_decode,
            "native_fact_buffer_encode": native_encode,
            "boundary_transport_residual": max(
                0.0, binding_seconds - native_decode - native_encode
            ),
            "build_code_graph": materialize_seconds,
            "node_count": view.meta.graph_vertex_count,
            "edge_count": view.meta.graph_edge_count,
            "fact_batch_count": view.meta.batch_count,
            "fact_symbol_count": view.meta.symbol_count,
            "fact_edge_count": view.meta.edge_count,
        }
        for name, nanoseconds in (view.meta.decode_profile_ns or {}).items():
            if name in {"worker_count", "document_count"}:
                timings[f"native_{name}"] = nanoseconds
            else:
                timings[f"native_{name}"] = nanoseconds / 1_000_000_000
        return graph, timings

    def _decode_legacy(self) -> tuple[CodeGraph, dict[str, float | int]]:
        started = time.perf_counter()
        result = _cpp.decode_scip(
            index_file=self.index_file_path,
            project_root=self.project_root,
            language=self.language,
        )
        binding_seconds = time.perf_counter() - started
        started = time.perf_counter()
        graph = _build_code_graph(
            result["vertices"],
            result["edges"],
            project_root=self.project_root,
        )
        materialize_seconds = time.perf_counter() - started
        return graph, {
            "core_decode": binding_seconds,
            "build_code_graph": materialize_seconds,
            "node_count": len(result["vertices"]),
            "edge_count": len(result["edges"]),
        }

    def _enrich_graph(self, graph: CodeGraph) -> None:
        if self.language not in {"ts", "typescript", "js", "javascript"}:
            return
        from .typescript_semantics import enrich_typescript_import_edges

        decoded_content = Path(self.index_file_path).read_text(encoding="utf-8")
        enrich_typescript_import_edges(
            graph, decoded_content, project_root=self.project_root
        )

    def decode(self) -> CodeGraph:
        t_total = time.perf_counter()
        self.fact_buffer_view = None
        self.transport_backend = "uninitialized"
        self.transport_fallback_error = None
        mode = _fact_buffer_mode()
        if mode == "off":
            graph, timings = self._decode_legacy()
            self.transport_backend = "legacy-disabled"
        else:
            try:
                graph, timings = self._decode_fact_buffer()
                self.transport_backend = "fact-buffer-v1"
            except MemoryError:
                raise
            except Exception as exc:
                if mode == "required":
                    raise
                self.transport_fallback_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "FactBatchBuffer transport unavailable; falling back to the "
                    f"legacy core transport: {self.transport_fallback_error}"
                )
                graph, timings = self._decode_legacy()
                self.transport_backend = "legacy-fallback"

        enrichment_started = time.perf_counter()
        self._enrich_graph(graph)
        enrichment_seconds = time.perf_counter() - enrichment_started
        timings["source_enrichment"] = enrichment_seconds
        # build_code_graph historically included TypeScript source enrichment.
        # Keep that aggregate timing stable while exposing the new substage.
        timings["build_code_graph"] += enrichment_seconds

        self.code_graph = graph
        timings["fact_buffer_enabled"] = int(self.transport_backend == "fact-buffer-v1")
        timings["total"] = time.perf_counter() - t_total
        self.timings = timings
        return graph

    def save_graph(self, output_path: str) -> None:
        if self.code_graph is None:
            raise RuntimeError("decode() must be called before save_graph()")
        self.code_graph.save_graph(output_path)
