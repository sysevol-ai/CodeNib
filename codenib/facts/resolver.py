# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Deterministic, provenance-preserving resolution over ``FactBatch`` values."""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any, Iterable, Protocol, Sequence

from ..types import EDGE_TYPE_REFERENCE
from .model import (
    CAPABILITY_EDGES,
    PROVENANCE_FRAMEWORK,
    PROVENANCE_HEURISTIC,
    EdgeFact,
    FactBatch,
    SymbolFact,
)


@dataclass(frozen=True, slots=True)
class DefinitionRecord:
    file_path: str
    symbol: SymbolFact


class DefinitionIndex:
    """Snapshot-local lookup that keeps ambiguous monikers explicit."""

    def __init__(self, batches: Sequence[FactBatch]) -> None:
        global_records: dict[str, list[DefinitionRecord]] = {}
        local_records: dict[tuple[str, str], list[DefinitionRecord]] = {}
        for batch in batches:
            for symbol in batch.symbols:
                record = DefinitionRecord(batch.file_path, symbol)
                keys = {symbol.moniker, symbol.symbol_id}
                for key in keys:
                    if key.startswith("local "):
                        local_records.setdefault((batch.file_path, key), []).append(
                            record
                        )
                    else:
                        global_records.setdefault(key, []).append(record)
        self._global = {
            key: tuple(_unique_records(records))
            for key, records in global_records.items()
        }
        self._local = {
            key: tuple(_unique_records(records))
            for key, records in local_records.items()
        }

    def lookup(
        self,
        moniker: str,
        *,
        source_file: str | None = None,
        restrict_file: bool = False,
    ) -> tuple[DefinitionRecord, ...]:
        if moniker.startswith("local "):
            if source_file is None:
                return ()
            return self._local.get((source_file, moniker), ())
        matches = self._global.get(moniker, ())
        if restrict_file and source_file is not None:
            return tuple(
                record for record in matches if record.file_path == source_file
            )
        return matches

    def resolve_unique(
        self,
        moniker: str,
        *,
        source_file: str | None = None,
        restrict_file: bool = False,
    ) -> DefinitionRecord | None:
        matches = self.lookup(
            moniker,
            source_file=source_file,
            restrict_file=restrict_file,
        )
        return matches[0] if len(matches) == 1 else None


class DefinitionHotCache:
    """Thread-safe bounded LRU keyed by a complete published snapshot ID."""

    def __init__(self, max_entries: int = 4096) -> None:
        if (
            isinstance(max_entries, bool)
            or not isinstance(max_entries, int)
            or max_entries < 1
        ):
            raise ValueError("max_entries must be a positive integer")
        self.max_entries = max_entries
        self._entries: OrderedDict[
            tuple[str, str, str, bool], DefinitionRecord | None
        ] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def get(
        self, key: tuple[str, str, str, bool]
    ) -> tuple[bool, DefinitionRecord | None]:
        with self._lock:
            if key not in self._entries:
                self.misses += 1
                return False, None
            value = self._entries.pop(key)
            self._entries[key] = value
            self.hits += 1
            return True, value

    def put(
        self,
        key: tuple[str, str, str, bool],
        value: DefinitionRecord | None,
    ) -> None:
        with self._lock:
            self._entries.pop(key, None)
            self._entries[key] = value
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
                self.evictions += 1

    def clear_snapshot(self, snapshot_id: str) -> int:
        with self._lock:
            doomed = [key for key in self._entries if key[0] == snapshot_id]
            for key in doomed:
                del self._entries[key]
            return len(doomed)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


class SnapshotDefinitionResolver:
    """Lazy moniker resolution isolated by one immutable snapshot identity."""

    def __init__(
        self,
        snapshot_id: str,
        batches: Sequence[FactBatch],
        *,
        cache: DefinitionHotCache | None = None,
    ) -> None:
        if not isinstance(snapshot_id, str) or not snapshot_id.strip():
            raise ValueError("snapshot_id must not be empty")
        self.snapshot_id = snapshot_id
        self.batches = tuple(sorted(batches, key=lambda batch: batch.file_path))
        paths = [batch.file_path for batch in self.batches]
        if len(paths) != len(set(paths)):
            raise ValueError("snapshot resolver requires unique file batches")
        self._batch_by_path = {batch.file_path: batch for batch in self.batches}
        self.definitions = DefinitionIndex(self.batches)
        self.cache = cache if cache is not None else DefinitionHotCache()

    def _require_batch(self, batch: FactBatch) -> None:
        if self._batch_by_path.get(batch.file_path) != batch:
            raise ValueError("batch is outside the resolver snapshot")

    def resolve(
        self,
        moniker: str,
        *,
        source_file: str | None = None,
        restrict_file: bool = False,
    ) -> DefinitionRecord | None:
        if not isinstance(moniker, str) or not moniker.strip():
            raise ValueError("moniker must not be empty")
        key = (self.snapshot_id, source_file or "", moniker, restrict_file)
        found, value = self.cache.get(key)
        if found:
            return value
        value = self.definitions.resolve_unique(
            moniker,
            source_file=source_file,
            restrict_file=restrict_file,
        )
        self.cache.put(key, value)
        return value

    def resolve_edge(
        self,
        batch: FactBatch,
        edge: EdgeFact,
    ) -> DefinitionRecord | None:
        self._require_batch(batch)
        if edge.target_moniker is not None:
            return self.resolve(
                edge.target_moniker,
                source_file=batch.file_path,
            )
        if edge.target_symbol_id is not None:
            return self.resolve(
                edge.target_symbol_id,
                source_file=batch.file_path,
                restrict_file=True,
            )
        return None

    def outgoing(
        self,
        batch: FactBatch,
        *,
        source_symbol_id: str | None = None,
        limit: int = 256,
    ) -> tuple[tuple[EdgeFact, DefinitionRecord | None], ...]:
        self._require_batch(batch)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        output = []
        for edge in batch.edges:
            if (
                source_symbol_id is not None
                and edge.source_symbol_id != source_symbol_id
            ):
                continue
            output.append((edge, self.resolve_edge(batch, edge)))
            if len(output) >= limit:
                break
        return tuple(output)


def _unique_records(records: Iterable[DefinitionRecord]) -> list[DefinitionRecord]:
    unique: dict[tuple[str, str], DefinitionRecord] = {}
    for record in records:
        unique[(record.file_path, record.symbol.symbol_id)] = record
    return [unique[key] for key in sorted(unique)]


@dataclass(frozen=True, slots=True)
class ResolutionContext:
    batches: tuple[FactBatch, ...]
    definitions: DefinitionIndex


class EdgeResolver(Protocol):
    """One ordered edge transformation or synthesis pass."""

    name: str

    def resolve(
        self,
        batch: FactBatch,
        context: ResolutionContext,
        edges: Sequence[EdgeFact],
    ) -> Iterable[EdgeFact]: ...


class ExactMonikerResolver:
    """Resolve an unresolved edge only when its moniker is snapshot-unique."""

    name = "exact_moniker_v1"

    def resolve(
        self,
        batch: FactBatch,
        context: ResolutionContext,
        edges: Sequence[EdgeFact],
    ) -> Iterable[EdgeFact]:
        resolved: list[EdgeFact] = []
        for edge in edges:
            if edge.target_moniker is None:
                resolved.append(edge)
                continue
            target = context.definitions.resolve_unique(
                edge.target_moniker,
                source_file=batch.file_path,
            )
            if target is None:
                resolved.append(edge)
                continue
            resolved.append(
                replace(
                    edge,
                    target_symbol_id=target.symbol.symbol_id,
                    target_moniker=None,
                    resolver=self.name,
                )
            )
        return resolved


@dataclass(frozen=True, slots=True)
class FrameworkRule:
    """One explicit framework relationship discovered by an adapter."""

    source_moniker: str
    target_moniker: str
    source_file: str | None = None
    target_file: str | None = None
    edge_kind: str = EDGE_TYPE_REFERENCE
    confidence: float = 0.9

    def __post_init__(self) -> None:
        for name in ("source_moniker", "target_moniker", "edge_kind"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must not be empty")
        if self.source_file is not None and not self.source_file.strip():
            raise ValueError("source_file must not be empty")
        if self.target_file is not None and not self.target_file.strip():
            raise ValueError("target_file must not be empty")
        if isinstance(self.confidence, bool) or not isinstance(
            self.confidence, (int, float)
        ):
            raise ValueError("confidence must be between 0 and 1")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")


class FrameworkRuleResolver:
    """Materialize already-discovered framework rules with strict uniqueness."""

    def __init__(self, framework: str, rules: Sequence[FrameworkRule]) -> None:
        normalized = framework.strip() if isinstance(framework, str) else ""
        if not normalized:
            raise ValueError("framework must not be empty")
        self.framework = normalized
        self.name = f"framework:{normalized}"
        self.rules = tuple(rules)

    def resolve(
        self,
        batch: FactBatch,
        context: ResolutionContext,
        edges: Sequence[EdgeFact],
    ) -> Iterable[EdgeFact]:
        output = list(edges)
        for rule in self.rules:
            source = context.definitions.resolve_unique(
                rule.source_moniker,
                source_file=rule.source_file or batch.file_path,
                restrict_file=True,
            )
            if source is None or source.file_path != batch.file_path:
                continue
            target = context.definitions.resolve_unique(
                rule.target_moniker,
                source_file=rule.target_file or batch.file_path,
                restrict_file=rule.target_file is not None,
            )
            if target is None:
                continue
            output.append(
                EdgeFact(
                    kind=rule.edge_kind,
                    provenance=PROVENANCE_FRAMEWORK,
                    source_symbol_id=source.symbol.symbol_id,
                    target_symbol_id=target.symbol.symbol_id,
                    confidence=rule.confidence,
                    resolver=self.name,
                )
            )
        return output


@dataclass(frozen=True, slots=True)
class ResolutionPassReport:
    name: str
    input_edges: int
    output_edges: int
    unresolved_before: int
    unresolved_after: int
    inferred_edges_after: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "input_edges": self.input_edges,
            "output_edges": self.output_edges,
            "unresolved_before": self.unresolved_before,
            "unresolved_after": self.unresolved_after,
            "inferred_edges_after": self.inferred_edges_after,
        }


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    batches: tuple[FactBatch, ...]
    passes: tuple[ResolutionPassReport, ...]

    @property
    def unresolved_edges(self) -> int:
        return sum(
            edge.target_moniker is not None
            for batch in self.batches
            for edge in batch.edges
        )


class FactResolverPipeline:
    """Apply ordered resolver plugins and publish new immutable batches."""

    def __init__(self, passes: Sequence[EdgeResolver] | None = None) -> None:
        selected = tuple(passes) if passes is not None else (ExactMonikerResolver(),)
        names = [resolver.name for resolver in selected]
        if len(names) != len(set(names)):
            raise ValueError("resolver pass names must be unique")
        self.passes = selected

    def resolve(
        self,
        batches: Sequence[FactBatch],
        *,
        profile_digest: str,
        provider: str | None = None,
    ) -> ResolutionResult:
        ordered = tuple(sorted(batches, key=lambda batch: batch.file_path))
        paths = [batch.file_path for batch in ordered]
        if len(paths) != len(set(paths)):
            raise ValueError("resolver input must contain at most one batch per file")
        context = ResolutionContext(ordered, DefinitionIndex(ordered))
        edges_by_file = {batch.file_path: batch.edges for batch in ordered}
        reports: list[ResolutionPassReport] = []

        for resolver in self.passes:
            input_edges = sum(len(edges) for edges in edges_by_file.values())
            unresolved_before = _unresolved_count(edges_by_file.values())
            for batch in ordered:
                resolved = tuple(
                    resolver.resolve(
                        batch,
                        context,
                        edges_by_file[batch.file_path],
                    )
                )
                if any(not isinstance(edge, EdgeFact) for edge in resolved):
                    raise TypeError(
                        f"resolver {resolver.name!r} returned a non-EdgeFact value"
                    )
                edges_by_file[batch.file_path] = tuple(
                    sorted(set(resolved), key=lambda edge: edge.sort_key)
                )
            output_edges = sum(len(edges) for edges in edges_by_file.values())
            unresolved_after = _unresolved_count(edges_by_file.values())
            inferred_edges = sum(
                edge.provenance in {PROVENANCE_FRAMEWORK, PROVENANCE_HEURISTIC}
                for edges in edges_by_file.values()
                for edge in edges
            )
            reports.append(
                ResolutionPassReport(
                    name=resolver.name,
                    input_edges=input_edges,
                    output_edges=output_edges,
                    unresolved_before=unresolved_before,
                    unresolved_after=unresolved_after,
                    inferred_edges_after=inferred_edges,
                )
            )

        resolved_batches = []
        for batch in ordered:
            edges = edges_by_file[batch.file_path]
            capabilities = batch.capabilities
            if edges and CAPABILITY_EDGES not in capabilities:
                capabilities = (*capabilities, CAPABILITY_EDGES)
            resolved_batches.append(
                FactBatch(
                    file_path=batch.file_path,
                    language=batch.language,
                    content_digest=batch.content_digest,
                    profile_digest=profile_digest,
                    provider=provider or batch.provider,
                    completeness=batch.completeness,
                    position_encoding=batch.position_encoding,
                    capabilities=capabilities,
                    symbols=batch.symbols,
                    occurrences=batch.occurrences,
                    edges=edges,
                    diagnostics=batch.diagnostics,
                )
            )
        return ResolutionResult(tuple(resolved_batches), tuple(reports))


def _unresolved_count(edge_groups: Iterable[Sequence[EdgeFact]]) -> int:
    return sum(
        edge.target_moniker is not None for edges in edge_groups for edge in edges
    )


class ResolverRegistry:
    """Insertion-ordered registry for independently owned resolver plugins."""

    def __init__(self) -> None:
        self._passes: dict[str, EdgeResolver] = {}

    def register(self, resolver: EdgeResolver) -> None:
        if resolver.name in self._passes:
            raise ValueError(f"resolver {resolver.name!r} is already registered")
        self._passes[resolver.name] = resolver

    def names(self) -> tuple[str, ...]:
        return tuple(self._passes)

    def pipeline(self, *, include_exact: bool = True) -> FactResolverPipeline:
        passes: list[EdgeResolver] = []
        if include_exact:
            passes.append(ExactMonikerResolver())
        passes.extend(self._passes.values())
        return FactResolverPipeline(passes)


__all__ = [
    "DefinitionHotCache",
    "DefinitionIndex",
    "DefinitionRecord",
    "EdgeResolver",
    "ExactMonikerResolver",
    "FactResolverPipeline",
    "FrameworkRule",
    "FrameworkRuleResolver",
    "ResolutionContext",
    "ResolutionPassReport",
    "ResolutionResult",
    "ResolverRegistry",
    "SnapshotDefinitionResolver",
]
