# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Compatibility adapters from current CodeNib semantic products."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..types import (
    EDGE_TYPE_CONTAIN,
    NODE_TYPE_FILE,
    NODE_TYPE_SYMBOL,
    is_symbol_node,
    node_has_definition,
)
from .model import (
    CAPABILITY_DEFINITIONS,
    CAPABILITY_EDGES,
    CAPABILITY_OCCURRENCES,
    COMPLETENESS_PARTIAL,
    PROVENANCE_SCIP,
    EdgeFact,
    FactBatch,
    OccurrenceFact,
    SourceRange,
    SymbolFact,
)


def _normalize_path(value: str | Path) -> str:
    normalized = str(value).replace("\\", "/")
    return normalized[2:] if normalized.startswith("./") else normalized


def _definition_symbol_id(
    file_path: str,
    moniker: str,
    source_range: SourceRange,
) -> str:
    scoped = f"{file_path}::{moniker}" if moniker.startswith("local ") else moniker
    return (
        f"{scoped}@{source_range.start_line}:{source_range.start_character}-"
        f"{source_range.end_line}:{source_range.end_character}"
    )


def fact_batch_from_scip_occurrences(
    occurrence_index: Any,
    *,
    file_path: str,
    language: str,
    content_digest: str,
    profile_digest: str,
    provider: str = "scip_occurrence_index",
) -> FactBatch:
    """Project one file from ``SCIPOccurrenceIndex`` into ``FactBatch v1``.

    The current occurrence index does not retain symbol kinds, enclosing
    ranges, or caller attribution, so the adapter reports partial completeness
    and intentionally emits no inferred edges.
    """

    normalized_path = _normalize_path(file_path)
    occurrences: list[OccurrenceFact] = []
    symbols: dict[str, SymbolFact] = {}
    for occurrence in getattr(occurrence_index, "occurrences", ()):
        if _normalize_path(occurrence.file_path) != normalized_path:
            continue
        source_range = SourceRange(
            start_line=occurrence.start_line,
            start_character=occurrence.start_character,
            end_line=occurrence.end_line,
            end_character=occurrence.end_character,
        )
        occurrence_fact = OccurrenceFact(
            symbol_moniker=occurrence.symbol,
            source_range=source_range,
            roles=occurrence.symbol_roles,
        )
        occurrences.append(occurrence_fact)
        if not occurrence_fact.is_definition:
            continue
        symbol_id = _definition_symbol_id(
            normalized_path,
            occurrence.symbol,
            source_range,
        )
        symbols[symbol_id] = SymbolFact(
            symbol_id=symbol_id,
            moniker=occurrence.symbol,
            display_name=occurrence.symbol,
            kind=NODE_TYPE_SYMBOL,
            definition_range=source_range,
            selection_range=source_range,
        )

    return FactBatch(
        file_path=normalized_path,
        language=language,
        content_digest=content_digest,
        profile_digest=profile_digest,
        provider=provider,
        completeness=COMPLETENESS_PARTIAL,
        position_encoding=str(
            getattr(occurrence_index, "position_encoding", None) or "UTF8"
        ),
        capabilities=(CAPABILITY_DEFINITIONS, CAPABILITY_OCCURRENCES),
        symbols=tuple(symbols.values()),
        occurrences=tuple(occurrences),
    )


def _language_for_file(language: str | Mapping[str, str], file_path: str) -> str:
    if isinstance(language, str):
        return language
    value = language.get(file_path)
    if value is None:
        raise KeyError(f"language is unavailable for {file_path!r}")
    return value


def _graph_vertices(graph: Any) -> list[dict[str, Any]]:
    return [dict(vertex.attributes()) for vertex in graph.graph.vs]


def _graph_definition_range(attributes: Mapping[str, Any]) -> SourceRange:
    start_line = attributes["start_line"]
    end_line = attributes["end_line"]
    return SourceRange(
        start_line=start_line,
        start_character=0,
        end_line=end_line + 1,
        end_character=0,
    )


def _graph_selection_range(
    attributes: Mapping[str, Any],
) -> SourceRange | None:
    line = attributes.get("selection_line")
    if isinstance(line, bool) or not isinstance(line, int) or line < 0:
        return None
    return SourceRange(
        start_line=line,
        start_character=0,
        end_line=line + 1,
        end_character=0,
    )


def _edge_anchor(attributes: Mapping[str, Any]) -> SourceRange | None:
    line = attributes.get("anchor_line")
    if isinstance(line, bool) or not isinstance(line, int) or line < 0:
        return None
    return SourceRange(
        start_line=line,
        start_character=0,
        end_line=line + 1,
        end_character=0,
    )


def fact_batches_from_code_graph(
    graph: Any,
    *,
    language: str | Mapping[str, str],
    content_digests: Mapping[str, str],
    profile_digest: str,
    provider: str = "legacy_code_graph",
    edge_provenance: str = PROVENANCE_SCIP,
    position_encoding: str = "UTF8",
) -> tuple[FactBatch, ...]:
    """Project the current materialized graph into per-file compatibility facts.

    This is a dual-write bridge, not a replacement decoder. Existing graph
    ranges are line-based and inclusive, so their definition facts become
    half-open full-line ranges. Exact SCIP occurrences can then be merged from
    :func:`fact_batch_from_scip_occurrences`.
    """

    normalized_digests: dict[str, str] = {}
    for raw_path, digest in content_digests.items():
        file_path = _normalize_path(raw_path)
        if file_path in normalized_digests:
            raise ValueError(f"duplicate normalized content digest path {file_path!r}")
        normalized_digests[file_path] = digest

    vertices = _graph_vertices(graph)
    names = [str(attributes.get("name") or "") for attributes in vertices]
    symbol_by_file: dict[str, list[SymbolFact]] = {
        file_path: [] for file_path in normalized_digests
    }
    occurrence_by_file: dict[str, list[OccurrenceFact]] = {
        file_path: [] for file_path in normalized_digests
    }
    edge_by_file: dict[str, list[EdgeFact]] = {
        file_path: [] for file_path in normalized_digests
    }

    parent_symbols: dict[int, str] = {}
    for edge in graph.graph.es:
        attributes = edge.attributes()
        if attributes.get("type") != EDGE_TYPE_CONTAIN:
            continue
        source = vertices[edge.source]
        target = vertices[edge.target]
        if is_symbol_node(source.get("type")) and is_symbol_node(target.get("type")):
            parent_symbols[edge.target] = names[edge.source]

    for index, attributes in enumerate(vertices):
        if not is_symbol_node(attributes.get("type")) or not node_has_definition(
            attributes
        ):
            continue
        raw_file = attributes.get("file")
        if not isinstance(raw_file, str):
            continue
        file_path = _normalize_path(raw_file)
        if file_path not in normalized_digests:
            continue
        definition_range = _graph_definition_range(attributes)
        symbol_id = names[index]
        moniker = str(attributes.get("unified_name") or symbol_id)
        symbol_by_file[file_path].append(
            SymbolFact(
                symbol_id=symbol_id,
                moniker=moniker,
                display_name=moniker,
                kind=str(attributes.get("type") or NODE_TYPE_SYMBOL),
                definition_range=definition_range,
                selection_range=_graph_selection_range(attributes),
                enclosing_symbol_id=parent_symbols.get(index),
            )
        )
        occurrence_by_file[file_path].append(
            OccurrenceFact(
                symbol_moniker=moniker,
                source_range=definition_range,
                roles=1,
            )
        )

    for edge in graph.graph.es:
        attributes = edge.attributes()
        kind = attributes.get("type")
        if not isinstance(kind, str) or not kind:
            continue
        source = vertices[edge.source]
        target = vertices[edge.target]
        if not is_symbol_node(target.get("type")):
            continue

        if kind == EDGE_TYPE_CONTAIN:
            raw_file = target.get("file")
        else:
            raw_file = attributes.get("anchor_file") or source.get("file")
            if raw_file is None and source.get("type") == NODE_TYPE_FILE:
                raw_file = names[edge.source]
        if not isinstance(raw_file, str):
            continue
        file_path = _normalize_path(raw_file)
        if file_path not in normalized_digests:
            continue

        source_symbol_id = (
            names[edge.source] if is_symbol_node(source.get("type")) else None
        )
        edge_by_file[file_path].append(
            EdgeFact(
                kind=kind,
                provenance=edge_provenance,
                source_symbol_id=source_symbol_id,
                target_symbol_id=names[edge.target],
                anchor_range=_edge_anchor(attributes),
                confidence=1.0,
                resolver="legacy_code_graph",
            )
        )

    batches = []
    for file_path, content_digest in sorted(normalized_digests.items()):
        batches.append(
            FactBatch(
                file_path=file_path,
                language=_language_for_file(language, file_path),
                content_digest=content_digest,
                profile_digest=profile_digest,
                provider=provider,
                completeness=COMPLETENESS_PARTIAL,
                position_encoding=position_encoding,
                capabilities=(
                    CAPABILITY_DEFINITIONS,
                    CAPABILITY_OCCURRENCES,
                    CAPABILITY_EDGES,
                ),
                symbols=tuple(symbol_by_file[file_path]),
                occurrences=tuple(occurrence_by_file[file_path]),
                edges=tuple(edge_by_file[file_path]),
            )
        )
    return tuple(batches)


__all__ = [
    "fact_batch_from_scip_occurrences",
    "fact_batches_from_code_graph",
]
