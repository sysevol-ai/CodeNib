# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Validated reader for the native ``FactBatchBuffer v1`` wire format.

The native extension returns a constant number of byte tables. This module
validates the complete envelope before exposing either a column-oriented
``CodeGraph`` materialization or the higher-level immutable ``FactBatch``
objects. The fixed envelope is checked immediately and every selected consumer
projection is checked completely before it is returned. Normal graph decoding
therefore avoids the former per-vertex dict and per-edge tuple transport;
logical fact objects are created only when a caller explicitly asks for them.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping

from ..facts.model import (
    CAPABILITY_DEFINITIONS,
    CAPABILITY_DIAGNOSTICS,
    CAPABILITY_EDGES,
    CAPABILITY_OCCURRENCES,
    COMPLETENESS_COMPLETE,
    COMPLETENESS_FAILED,
    COMPLETENESS_PARTIAL,
    COMPLETENESS_SYNTACTIC,
    PROVENANCE_CLANGD,
    PROVENANCE_FRAMEWORK,
    PROVENANCE_HEURISTIC,
    PROVENANCE_LSP,
    PROVENANCE_SCIP,
    PROVENANCE_SYNTACTIC,
    DiagnosticFact,
    EdgeFact,
    FactBatch,
    OccurrenceFact,
    SourceRange,
    SymbolFact,
)
from ..graph.code_graph import CodeGraph

FACT_BATCH_BUFFER_ABI_VERSION = 1
FACT_BATCH_SCHEMA_VERSION = 1
FACT_BATCH_BUFFER_NONE = 0xFFFFFFFF

FACT_BATCH_META_SIZE = 64
FACT_BATCH_ROW_SIZE = 56
FACT_SYMBOL_ROW_SIZE = 80
FACT_OCCURRENCE_ROW_SIZE = 48
FACT_EDGE_ROW_SIZE = 72
FACT_DIAGNOSTIC_ROW_SIZE = 40
FACT_GRAPH_VERTEX_ROW_SIZE = 56
FACT_GRAPH_EDGE_ROW_SIZE = 32

FACT_BUFFER_FLAG_GRAPH_COMPAT = 1 << 0
FACT_BUFFER_FLAG_CONTENT_DIGESTS_COMPLETE = 1 << 1

_META = struct.Struct("<4sHH12IQ")
_BATCH = struct.Struct("<12IBBHI")
_SYMBOL = struct.Struct("<20I")
_OCCURRENCE = struct.Struct("<12I")
_EDGE = struct.Struct("<IBBHd14I")
_DIAGNOSTIC = struct.Struct("<IBBH8I")
_GRAPH_VERTEX = struct.Struct("<14I")
_GRAPH_EDGE = struct.Struct("<8I")

_COMPLETENESS = {
    1: COMPLETENESS_COMPLETE,
    2: COMPLETENESS_PARTIAL,
    3: COMPLETENESS_SYNTACTIC,
    4: COMPLETENESS_FAILED,
}
_CAPABILITIES = {
    1 << 0: CAPABILITY_DEFINITIONS,
    1 << 1: CAPABILITY_OCCURRENCES,
    1 << 2: CAPABILITY_EDGES,
    1 << 3: CAPABILITY_DIAGNOSTICS,
}
_PROVENANCES = {
    1: PROVENANCE_SCIP,
    2: PROVENANCE_LSP,
    3: PROVENANCE_CLANGD,
    4: PROVENANCE_SYNTACTIC,
    5: PROVENANCE_FRAMEWORK,
    6: PROVENANCE_HEURISTIC,
}
_DIAGNOSTIC_SEVERITIES = {1: "debug", 2: "info", 3: "warning", 4: "error"}


class FactBatchBufferError(ValueError):
    """Raised when a native buffer fails its complete wire-contract check."""


@dataclass(frozen=True, slots=True)
class FactBatchBufferMeta:
    flags: int
    batch_count: int
    symbol_count: int
    occurrence_count: int
    edge_count: int
    diagnostic_count: int
    graph_vertex_count: int
    graph_edge_count: int
    arena_size: int
    project_root: str
    native_decode_ns: int | None = None
    native_encode_ns: int | None = None
    decode_profile_ns: Mapping[str, int] | None = None
    encode_profile_ns: Mapping[str, int] | None = None


@dataclass(frozen=True, slots=True)
class _BatchInfo:
    file_path: str
    language: str
    content_digest: str | None
    profile_digest: str
    provider: str
    position_encoding: str
    completeness: str
    capabilities: tuple[str, ...]


def _byte_view(value: Any, *, name: str) -> memoryview:
    try:
        view = memoryview(value)
    except TypeError as exc:
        raise FactBatchBufferError(f"{name} must be bytes-like") from exc
    if not view.contiguous:
        raise FactBatchBufferError(f"{name} must be contiguous")
    if not view.readonly:
        raise FactBatchBufferError(f"{name} must be read-only")
    return view.cast("B")


def _nonnegative_timing(value: Any, *, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FactBatchBufferError(f"{name} must be a non-negative integer")
    return value


def _normalized_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or path.as_posix() != normalized
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise FactBatchBufferError(
            f"non-canonical repository-relative file path {value!r}"
        )
    return normalized


class FactBatchBufferView:
    """Validated, zero-copy view over one native FactBatchBuffer envelope."""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        if not isinstance(payload, Mapping):
            raise FactBatchBufferError("FactBatchBuffer payload must be a mapping")
        self._meta_buffer = _byte_view(payload.get("meta"), name="meta")
        self._batches = _byte_view(payload.get("batches"), name="batches")
        self._symbols = _byte_view(payload.get("symbols"), name="symbols")
        self._occurrences = _byte_view(payload.get("occurrences"), name="occurrences")
        self._edges = _byte_view(payload.get("edges"), name="edges")
        self._diagnostics = _byte_view(payload.get("diagnostics"), name="diagnostics")
        self._graph_vertices = _byte_view(
            payload.get("graph_vertices"), name="graph_vertices"
        )
        self._graph_edges = _byte_view(payload.get("graph_edges"), name="graph_edges")
        self._arena = _byte_view(payload.get("arena"), name="arena")
        self._strings: dict[tuple[int, int], str] = {}
        self.meta = self._decode_meta(payload)
        self._validate_table_sizes()

    def _decode_meta(self, payload: Mapping[str, Any]) -> FactBatchBufferMeta:
        if len(self._meta_buffer) != FACT_BATCH_META_SIZE:
            raise FactBatchBufferError(
                f"meta has {len(self._meta_buffer)} bytes; expected "
                f"{FACT_BATCH_META_SIZE}"
            )
        values = _META.unpack(self._meta_buffer)
        magic, abi_version, schema_version = values[:3]
        if magic != b"CNFB":
            raise FactBatchBufferError(f"unsupported FactBatchBuffer magic {magic!r}")
        if abi_version != FACT_BATCH_BUFFER_ABI_VERSION:
            raise FactBatchBufferError(
                f"FactBatchBuffer ABI {abi_version} != expected "
                f"{FACT_BATCH_BUFFER_ABI_VERSION}"
            )
        if schema_version != FACT_BATCH_SCHEMA_VERSION:
            raise FactBatchBufferError(
                f"FactBatch schema {schema_version} != expected "
                f"{FACT_BATCH_SCHEMA_VERSION}"
            )
        (
            flags,
            batch_count,
            symbol_count,
            occurrence_count,
            edge_count,
            diagnostic_count,
            graph_vertex_count,
            graph_edge_count,
            arena_size,
            project_root_offset,
            project_root_length,
            reserved,
        ) = values[3:15]
        if reserved != 0 or values[15] != 0:
            raise FactBatchBufferError(
                "FactBatchBuffer reserved meta fields are nonzero"
            )
        if arena_size != len(self._arena):
            raise FactBatchBufferError(
                f"arena has {len(self._arena)} bytes; meta declares {arena_size}"
            )
        project_root = self._string(
            project_root_offset,
            project_root_length,
            label="project_root",
        )
        if project_root is None:
            raise FactBatchBufferError("project_root string reference is absent")
        profiles: dict[str, Mapping[str, int] | None] = {}
        for profile_name in ("decode_profile_ns", "encode_profile_ns"):
            raw_profile = payload.get(profile_name)
            profile = None
            if raw_profile is not None:
                if not isinstance(raw_profile, Mapping):
                    raise FactBatchBufferError(f"{profile_name} must be a mapping")
                profile = {
                    str(name): _nonnegative_timing(value, name=f"{profile_name}.{name}")
                    for name, value in raw_profile.items()
                }
                if any(value is None for value in profile.values()):
                    raise FactBatchBufferError(f"{profile_name} values must be present")
            profiles[profile_name] = profile
        return FactBatchBufferMeta(
            flags=flags,
            batch_count=batch_count,
            symbol_count=symbol_count,
            occurrence_count=occurrence_count,
            edge_count=edge_count,
            diagnostic_count=diagnostic_count,
            graph_vertex_count=graph_vertex_count,
            graph_edge_count=graph_edge_count,
            arena_size=arena_size,
            project_root=project_root,
            native_decode_ns=_nonnegative_timing(
                payload.get("native_decode_ns"), name="native_decode_ns"
            ),
            native_encode_ns=_nonnegative_timing(
                payload.get("native_encode_ns"), name="native_encode_ns"
            ),
            decode_profile_ns=profiles["decode_profile_ns"],
            encode_profile_ns=profiles["encode_profile_ns"],
        )

    def _validate_table_sizes(self) -> None:
        supported_flags = (
            FACT_BUFFER_FLAG_GRAPH_COMPAT | FACT_BUFFER_FLAG_CONTENT_DIGESTS_COMPLETE
        )
        if self.meta.flags & ~supported_flags:
            raise FactBatchBufferError(
                f"FactBatchBuffer has unsupported flags {self.meta.flags:#x}"
            )
        tables = (
            ("batches", self._batches, self.meta.batch_count, FACT_BATCH_ROW_SIZE),
            ("symbols", self._symbols, self.meta.symbol_count, FACT_SYMBOL_ROW_SIZE),
            (
                "occurrences",
                self._occurrences,
                self.meta.occurrence_count,
                FACT_OCCURRENCE_ROW_SIZE,
            ),
            ("edges", self._edges, self.meta.edge_count, FACT_EDGE_ROW_SIZE),
            (
                "diagnostics",
                self._diagnostics,
                self.meta.diagnostic_count,
                FACT_DIAGNOSTIC_ROW_SIZE,
            ),
            (
                "graph_vertices",
                self._graph_vertices,
                self.meta.graph_vertex_count,
                FACT_GRAPH_VERTEX_ROW_SIZE,
            ),
            (
                "graph_edges",
                self._graph_edges,
                self.meta.graph_edge_count,
                FACT_GRAPH_EDGE_ROW_SIZE,
            ),
        )
        for name, table, count, row_size in tables:
            expected = count * row_size
            if len(table) != expected:
                raise FactBatchBufferError(
                    f"{name} has {len(table)} bytes; expected {expected} "
                    f"for {count} rows"
                )
        if not self.has_graph_compat and (
            self.meta.graph_vertex_count or self.meta.graph_edge_count
        ):
            raise FactBatchBufferError(
                "FactBatchBuffer declares graph rows without graph compatibility"
            )

    @property
    def has_graph_compat(self) -> bool:
        """Whether exact legacy graph compatibility tables are present."""

        return bool(self.meta.flags & FACT_BUFFER_FLAG_GRAPH_COMPAT)

    def _string(
        self,
        offset: int,
        length: int,
        *,
        required: bool = False,
        label: str = "string",
    ) -> str | None:
        if offset == FACT_BATCH_BUFFER_NONE:
            if length != 0:
                raise FactBatchBufferError(
                    f"absent {label} has nonzero length {length}"
                )
            if required:
                raise FactBatchBufferError(f"required {label} is absent")
            return None
        end = offset + length
        if end < offset or end > len(self._arena):
            raise FactBatchBufferError(
                f"{label} range ({offset}, {length}) exceeds the string arena"
            )
        key = (offset, length)
        cached = self._strings.get(key)
        if cached is not None:
            return cached
        try:
            value = self._arena[offset:end].tobytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FactBatchBufferError(f"{label} is not valid UTF-8") from exc
        if "\x00" in value:
            raise FactBatchBufferError(f"{label} contains a NUL byte")
        if required and not value:
            raise FactBatchBufferError(f"required {label} is empty")
        self._strings[key] = value
        return value

    @staticmethod
    def _optional_line(value: int) -> int | None:
        return None if value == FACT_BATCH_BUFFER_NONE else value

    @staticmethod
    def _range(values: tuple[int, int, int, int], *, label: str) -> SourceRange | None:
        absent = [value == FACT_BATCH_BUFFER_NONE for value in values]
        if all(absent):
            return None
        if any(absent):
            raise FactBatchBufferError(f"{label} is only partially present")
        try:
            return SourceRange(*values)
        except (TypeError, ValueError) as exc:
            raise FactBatchBufferError(f"invalid {label}: {exc}") from exc

    def materialize_code_graph(self, *, project_root: str | None = None) -> CodeGraph:
        """Build the current graph contract directly from packed columns."""

        if not self.has_graph_compat:
            raise FactBatchBufferError(
                "FactBatchBuffer does not include graph compatibility tables"
            )

        names: list[str] = []
        types: list[str] = []
        files: list[str | None] = []
        start_lines: list[int | None] = []
        end_lines: list[int | None] = []
        selection_lines: list[int | None] = []
        unified_names: list[str | None] = []
        symbol_kinds: list[str | None] = []
        has_definitions: list[bool | None] = []

        for index in range(self.meta.graph_vertex_count):
            row = _GRAPH_VERTEX.unpack_from(
                self._graph_vertices, index * FACT_GRAPH_VERTEX_ROW_SIZE
            )
            names.append(
                self._string(row[0], row[1], required=True, label="vertex name")
            )
            types.append(
                self._string(row[2], row[3], required=True, label="vertex type")
            )
            files.append(self._string(row[4], row[5], label="vertex file"))
            unified_names.append(
                self._string(row[6], row[7], label="vertex unified_name")
            )
            symbol_kinds.append(
                self._string(row[8], row[9], label="vertex symbol_kind")
            )
            start_lines.append(self._optional_line(row[10]))
            end_lines.append(self._optional_line(row[11]))
            selection_lines.append(self._optional_line(row[12]))
            flags = row[13]
            if flags & ~0b11:
                raise FactBatchBufferError(
                    f"graph vertex {index} has unsupported flags {flags:#x}"
                )
            has_definitions.append(bool(flags & 0b10) if flags & 0b1 else None)

        if len(names) != len(set(names)):
            raise FactBatchBufferError(
                "FactBatchBuffer graph vertex names must be globally unique"
            )

        graph = CodeGraph(
            self.meta.project_root if project_root is None else str(project_root)
        )
        if names:
            graph.graph.add_vertices(
                len(names),
                attributes={
                    "name": names,
                    "type": types,
                    "file": files,
                    "start_line": start_lines,
                    "end_line": end_lines,
                    "selection_line": selection_lines,
                    "unified_name": unified_names,
                    "symbol_kind": symbol_kinds,
                    "has_definition": has_definitions,
                },
            )
        graph.name_to_vertex.update({name: index for index, name in enumerate(names)})
        graph.symbol_ranges.update(
            {
                name: (start, end)
                for name, start, end in zip(names, start_lines, end_lines, strict=True)
                if start is not None and end is not None
            }
        )

        pairs: list[tuple[int, int]] = []
        edge_types: list[str] = []
        anchor_files: list[str | None] = []
        anchor_lines: list[int | None] = []
        for index in range(self.meta.graph_edge_count):
            row = _GRAPH_EDGE.unpack_from(
                self._graph_edges, index * FACT_GRAPH_EDGE_ROW_SIZE
            )
            source, target = row[:2]
            if source >= len(names) or target >= len(names):
                raise FactBatchBufferError(
                    f"graph edge {index} endpoint ({source}, {target}) is out of range"
                )
            if row[7] != 0:
                raise FactBatchBufferError(
                    f"graph edge {index} has nonzero reserved flags"
                )
            pairs.append((source, target))
            edge_types.append(
                self._string(row[2], row[3], required=True, label="edge type")
            )
            anchor_files.append(self._string(row[4], row[5], label="edge anchor_file"))
            anchor_lines.append(self._optional_line(row[6]))
        if pairs:
            graph.graph.add_edges(
                pairs,
                attributes={
                    "type": edge_types,
                    "anchor_file": anchor_files,
                    "anchor_line": anchor_lines,
                },
            )
        return graph

    def _batch_infos(self) -> tuple[_BatchInfo, ...]:
        batches = []
        supported_capability_mask = sum(_CAPABILITIES)
        for index in range(self.meta.batch_count):
            row = _BATCH.unpack_from(self._batches, index * FACT_BATCH_ROW_SIZE)
            completeness = _COMPLETENESS.get(row[12])
            if completeness is None:
                raise FactBatchBufferError(
                    f"batch {index} has unsupported completeness {row[12]}"
                )
            capability_mask = row[13]
            if capability_mask & ~supported_capability_mask:
                raise FactBatchBufferError(
                    f"batch {index} has unsupported capabilities {capability_mask:#x}"
                )
            if row[14] != 0 or row[15] != 0:
                raise FactBatchBufferError(f"batch {index} reserved fields are nonzero")
            batches.append(
                _BatchInfo(
                    file_path=_normalized_path(
                        self._string(
                            row[0], row[1], required=True, label="batch file_path"
                        )
                    ),
                    language=self._string(
                        row[2], row[3], required=True, label="batch language"
                    ),
                    content_digest=self._string(
                        row[4], row[5], label="batch content_digest"
                    ),
                    profile_digest=self._string(
                        row[6], row[7], required=True, label="batch profile_digest"
                    ),
                    provider=self._string(
                        row[8], row[9], required=True, label="batch provider"
                    ),
                    position_encoding=self._string(
                        row[10],
                        row[11],
                        required=True,
                        label="batch position_encoding",
                    ),
                    completeness=completeness,
                    capabilities=tuple(
                        name
                        for bit, name in _CAPABILITIES.items()
                        if capability_mask & bit
                    ),
                )
            )
        paths = [batch.file_path for batch in batches]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise FactBatchBufferError(
                "FactBatchBuffer batch paths must be unique and sorted"
            )
        digests_complete = all(batch.content_digest is not None for batch in batches)
        declared_complete = bool(
            self.meta.flags & FACT_BUFFER_FLAG_CONTENT_DIGESTS_COMPLETE
        )
        if declared_complete != digests_complete:
            raise FactBatchBufferError(
                "FactBatchBuffer content-digest flag does not match batch rows"
            )
        return tuple(batches)

    @staticmethod
    def _check_batch_index(index: int, batch_count: int, *, table: str) -> None:
        if index >= batch_count:
            raise FactBatchBufferError(
                f"{table} row references out-of-range batch {index}"
            )

    def to_fact_batches(
        self, *, content_digests: Mapping[str, str] | None = None
    ) -> tuple[FactBatch, ...]:
        """Materialize logical facts; source digests must be authoritative."""

        try:
            return self._to_fact_batches(content_digests=content_digests)
        except FactBatchBufferError:
            raise
        except (TypeError, ValueError) as exc:
            raise FactBatchBufferError(f"invalid semantic facts: {exc}") from exc

    def _to_fact_batches(
        self, *, content_digests: Mapping[str, str] | None = None
    ) -> tuple[FactBatch, ...]:
        """Implement materialization behind the stable wire-error boundary."""

        infos = self._batch_infos()
        overrides = {
            _normalized_path(str(path)): str(digest)
            for path, digest in (content_digests or {}).items()
        }
        symbols: list[list[SymbolFact]] = [[] for _ in infos]
        occurrences: list[list[OccurrenceFact]] = [[] for _ in infos]
        edges: list[list[EdgeFact]] = [[] for _ in infos]
        diagnostics: list[list[DiagnosticFact]] = [[] for _ in infos]

        for index in range(self.meta.symbol_count):
            row = _SYMBOL.unpack_from(self._symbols, index * FACT_SYMBOL_ROW_SIZE)
            batch_index, flags = row[:2]
            self._check_batch_index(batch_index, len(infos), table="symbol")
            if flags & ~0b111 or not flags & 0b1:
                raise FactBatchBufferError(
                    f"symbol row {index} has invalid definition flags {flags:#x}"
                )
            definition = self._range(tuple(row[10:14]), label="definition_range")
            if definition is None:
                raise FactBatchBufferError(f"symbol row {index} lacks a definition")
            selection = self._range(tuple(row[14:18]), label="selection_range")
            enclosing = self._string(
                row[18], row[19], label="symbol enclosing_symbol_id"
            )
            if bool(flags & 0b10) != (selection is not None):
                raise FactBatchBufferError(
                    f"symbol row {index} selection flag does not match its range"
                )
            if bool(flags & 0b100) != (enclosing is not None):
                raise FactBatchBufferError(
                    f"symbol row {index} enclosing flag does not match its value"
                )
            symbols[batch_index].append(
                SymbolFact(
                    symbol_id=self._string(
                        row[2], row[3], required=True, label="symbol_id"
                    ),
                    moniker=self._string(
                        row[4], row[5], required=True, label="symbol moniker"
                    ),
                    display_name=self._string(
                        row[6], row[7], required=True, label="symbol display_name"
                    ),
                    kind=self._string(
                        row[8], row[9], required=True, label="symbol kind"
                    ),
                    definition_range=definition,
                    selection_range=selection,
                    enclosing_symbol_id=enclosing,
                )
            )

        for index in range(self.meta.occurrence_count):
            row = _OCCURRENCE.unpack_from(
                self._occurrences, index * FACT_OCCURRENCE_ROW_SIZE
            )
            batch_index, roles = row[:2]
            self._check_batch_index(batch_index, len(infos), table="occurrence")
            source = self._range(tuple(row[4:8]), label="occurrence source_range")
            if source is None:
                raise FactBatchBufferError(
                    f"occurrence row {index} lacks a source range"
                )
            occurrences[batch_index].append(
                OccurrenceFact(
                    symbol_moniker=self._string(
                        row[2],
                        row[3],
                        required=True,
                        label="occurrence symbol_moniker",
                    ),
                    source_range=source,
                    roles=roles,
                    enclosing_range=self._range(
                        tuple(row[8:12]), label="occurrence enclosing_range"
                    ),
                )
            )

        for index in range(self.meta.edge_count):
            row = _EDGE.unpack_from(self._edges, index * FACT_EDGE_ROW_SIZE)
            batch_index, provenance_code, flags, reserved = row[:4]
            self._check_batch_index(batch_index, len(infos), table="edge")
            if reserved != 0 or flags & ~0b11111:
                raise FactBatchBufferError(
                    f"edge row {index} has unsupported reserved fields"
                )
            provenance = _PROVENANCES.get(provenance_code)
            if provenance is None:
                raise FactBatchBufferError(
                    f"edge row {index} has unsupported provenance {provenance_code}"
                )
            confidence = row[4]
            if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise FactBatchBufferError(
                    f"edge row {index} has invalid confidence {confidence!r}"
                )
            source_symbol_id = self._string(
                row[7], row[8], label="edge source_symbol_id"
            )
            target_symbol_id = self._string(
                row[9], row[10], label="edge target_symbol_id"
            )
            target_moniker = self._string(row[11], row[12], label="edge target_moniker")
            resolver = self._string(row[13], row[14], label="edge resolver")
            anchor_range = self._range(tuple(row[15:19]), label="edge anchor_range")
            expected_flags = (
                int(source_symbol_id is not None)
                | int(target_symbol_id is not None) << 1
                | int(target_moniker is not None) << 2
                | int(anchor_range is not None) << 3
                | int(resolver is not None) << 4
            )
            if flags != expected_flags:
                raise FactBatchBufferError(
                    f"edge row {index} flags do not match optional fields"
                )
            edges[batch_index].append(
                EdgeFact(
                    kind=self._string(row[5], row[6], required=True, label="edge kind"),
                    provenance=provenance,
                    source_symbol_id=source_symbol_id,
                    target_symbol_id=target_symbol_id,
                    target_moniker=target_moniker,
                    resolver=resolver,
                    anchor_range=anchor_range,
                    confidence=confidence,
                )
            )

        for index in range(self.meta.diagnostic_count):
            row = _DIAGNOSTIC.unpack_from(
                self._diagnostics, index * FACT_DIAGNOSTIC_ROW_SIZE
            )
            batch_index, severity_code, flags, reserved = row[:4]
            self._check_batch_index(batch_index, len(infos), table="diagnostic")
            severity = _DIAGNOSTIC_SEVERITIES.get(severity_code)
            if severity is None or reserved != 0 or flags & ~0b1:
                raise FactBatchBufferError(
                    f"diagnostic row {index} has unsupported enum or flags"
                )
            source_range = self._range(
                tuple(row[8:12]), label="diagnostic source_range"
            )
            if bool(flags & 0b1) != (source_range is not None):
                raise FactBatchBufferError(
                    f"diagnostic row {index} range flag does not match its value"
                )
            diagnostics[batch_index].append(
                DiagnosticFact(
                    severity=severity,
                    code=self._string(
                        row[4], row[5], required=True, label="diagnostic code"
                    ),
                    message=self._string(
                        row[6], row[7], required=True, label="diagnostic message"
                    ),
                    source_range=source_range,
                )
            )

        result = []
        for index, info in enumerate(infos):
            content_digest = overrides.get(info.file_path, info.content_digest)
            if content_digest is None:
                raise FactBatchBufferError(
                    f"authoritative content digest is required for {info.file_path!r}"
                )
            result.append(
                FactBatch(
                    file_path=info.file_path,
                    language=info.language,
                    content_digest=content_digest,
                    profile_digest=info.profile_digest,
                    provider=info.provider,
                    completeness=info.completeness,
                    position_encoding=info.position_encoding,
                    capabilities=info.capabilities,
                    symbols=tuple(symbols[index]),
                    occurrences=tuple(occurrences[index]),
                    edges=tuple(edges[index]),
                    diagnostics=tuple(diagnostics[index]),
                )
            )
        return tuple(result)


def expected_contract() -> dict[str, int]:
    """Return the Python-side constants a compiled extension must match."""

    return {
        "abi_version": FACT_BATCH_BUFFER_ABI_VERSION,
        "schema_version": FACT_BATCH_SCHEMA_VERSION,
        "meta_size": FACT_BATCH_META_SIZE,
        "batch_row_size": FACT_BATCH_ROW_SIZE,
        "symbol_row_size": FACT_SYMBOL_ROW_SIZE,
        "occurrence_row_size": FACT_OCCURRENCE_ROW_SIZE,
        "edge_row_size": FACT_EDGE_ROW_SIZE,
        "diagnostic_row_size": FACT_DIAGNOSTIC_ROW_SIZE,
        "graph_vertex_row_size": FACT_GRAPH_VERTEX_ROW_SIZE,
        "graph_edge_row_size": FACT_GRAPH_EDGE_ROW_SIZE,
    }


def validate_compiled_contract(raw: Mapping[str, Any]) -> None:
    """Reject a stale extension before any native buffer is consumed."""

    if not isinstance(raw, Mapping):
        raise FactBatchBufferError(
            "compiled FactBatchBuffer contract must be a mapping"
        )
    expected = expected_contract()
    actual = {name: raw.get(name) for name in expected}
    if actual != expected or any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in actual.values()
    ):
        raise FactBatchBufferError(
            f"compiled FactBatchBuffer contract mismatch: {actual}; "
            f"expected {expected}"
        )


__all__ = [
    "FACT_BATCH_BUFFER_ABI_VERSION",
    "FACT_BATCH_SCHEMA_VERSION",
    "FactBatchBufferError",
    "FactBatchBufferMeta",
    "FactBatchBufferView",
    "expected_contract",
    "validate_compiled_contract",
]
