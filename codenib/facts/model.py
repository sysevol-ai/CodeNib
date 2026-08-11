# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Immutable, provider-neutral semantic facts for one repository file.

The contract deliberately sits before graph materialization and storage. SCIP,
clangd, generic LSP, and lower-confidence syntactic analyzers can emit the same
shape while resolvers add edges without mutating the provider's original
batch. Positions are zero-based, half-open, and interpreted using the batch's
``position_encoding``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence

FACT_BATCH_SCHEMA_VERSION = 1

CAPABILITY_DEFINITIONS = "definitions"
CAPABILITY_OCCURRENCES = "occurrences"
CAPABILITY_EDGES = "edges"
CAPABILITY_DIAGNOSTICS = "diagnostics"
FACT_CAPABILITIES = frozenset(
    {
        CAPABILITY_DEFINITIONS,
        CAPABILITY_OCCURRENCES,
        CAPABILITY_EDGES,
        CAPABILITY_DIAGNOSTICS,
    }
)

COMPLETENESS_COMPLETE = "complete"
COMPLETENESS_PARTIAL = "partial"
COMPLETENESS_SYNTACTIC = "syntactic"
COMPLETENESS_FAILED = "failed"
FACT_COMPLETENESS = frozenset(
    {
        COMPLETENESS_COMPLETE,
        COMPLETENESS_PARTIAL,
        COMPLETENESS_SYNTACTIC,
        COMPLETENESS_FAILED,
    }
)

PROVENANCE_SCIP = "scip"
PROVENANCE_LSP = "lsp"
PROVENANCE_CLANGD = "clangd"
PROVENANCE_SYNTACTIC = "syntactic"
PROVENANCE_FRAMEWORK = "framework"
PROVENANCE_HEURISTIC = "heuristic"
FACT_PROVENANCES = frozenset(
    {
        PROVENANCE_SCIP,
        PROVENANCE_LSP,
        PROVENANCE_CLANGD,
        PROVENANCE_SYNTACTIC,
        PROVENANCE_FRAMEWORK,
        PROVENANCE_HEURISTIC,
    }
)

_DIAGNOSTIC_SEVERITIES = frozenset({"debug", "info", "warning", "error"})
_MAX_DIAGNOSTIC_CODE_CHARS = 256
_MAX_DIAGNOSTIC_MESSAGE_CHARS = 4096
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _required_text(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be empty")
    if value != value.strip():
        raise ValueError(f"{name} must not have surrounding whitespace")
    if "\x00" in value:
        raise ValueError(f"{name} must not contain NUL bytes")
    return value


def _zero_based_integer(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _canonical_file_path(value: Any) -> str:
    text = _required_text(value, name="file_path")
    if "\\" in text:
        raise ValueError("file_path must be a repository-relative POSIX path")
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or path.as_posix() != text
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("file_path must be a repository-relative POSIX path")
    return text


def _sha256(value: Any, *, name: str) -> str:
    text = _required_text(value, name=name)
    if not _SHA256_RE.fullmatch(text):
        raise ValueError(f"{name} must use canonical sha256:<64 lowercase hex>")
    return text


def sha256_digest(value: bytes | str) -> str:
    """Return the canonical digest spelling used by ``FactBatch``."""

    payload = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


@dataclass(frozen=True, slots=True)
class SourceRange:
    """Zero-based half-open source range."""

    start_line: int
    start_character: int
    end_line: int
    end_character: int

    def __post_init__(self) -> None:
        for name in (
            "start_line",
            "start_character",
            "end_line",
            "end_character",
        ):
            _zero_based_integer(getattr(self, name), name=name)
        if (self.end_line, self.end_character) < (
            self.start_line,
            self.start_character,
        ):
            raise ValueError("source range end must not precede its start")

    @property
    def sort_key(self) -> tuple[int, int, int, int]:
        return (
            self.start_line,
            self.start_character,
            self.end_line,
            self.end_character,
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "start_line": self.start_line,
            "start_character": self.start_character,
            "end_line": self.end_line,
            "end_character": self.end_character,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SourceRange:
        return cls(
            start_line=payload.get("start_line"),
            start_character=payload.get("start_character"),
            end_line=payload.get("end_line"),
            end_character=payload.get("end_character"),
        )


@dataclass(frozen=True, slots=True)
class SymbolFact:
    """One observed symbol definition."""

    symbol_id: str
    moniker: str
    display_name: str
    kind: str
    definition_range: SourceRange
    selection_range: SourceRange | None = None
    enclosing_symbol_id: str | None = None

    def __post_init__(self) -> None:
        _required_text(self.symbol_id, name="symbol_id")
        _required_text(self.moniker, name="moniker")
        _required_text(self.display_name, name="display_name")
        _required_text(self.kind, name="kind")
        if self.enclosing_symbol_id is not None:
            _required_text(self.enclosing_symbol_id, name="enclosing_symbol_id")

    @property
    def sort_key(self) -> tuple[Any, ...]:
        return self.symbol_id, self.definition_range.sort_key

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "symbol_id": self.symbol_id,
            "moniker": self.moniker,
            "display_name": self.display_name,
            "kind": self.kind,
            "definition_range": self.definition_range.to_dict(),
        }
        if self.selection_range is not None:
            payload["selection_range"] = self.selection_range.to_dict()
        if self.enclosing_symbol_id is not None:
            payload["enclosing_symbol_id"] = self.enclosing_symbol_id
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SymbolFact:
        selection = payload.get("selection_range")
        return cls(
            symbol_id=payload.get("symbol_id"),
            moniker=payload.get("moniker"),
            display_name=payload.get("display_name"),
            kind=payload.get("kind"),
            definition_range=SourceRange.from_dict(payload["definition_range"]),
            selection_range=(
                SourceRange.from_dict(selection)
                if isinstance(selection, Mapping)
                else None
            ),
            enclosing_symbol_id=payload.get("enclosing_symbol_id"),
        )


@dataclass(frozen=True, slots=True)
class OccurrenceFact:
    """One exact symbol occurrence; the moniker may remain unresolved."""

    symbol_moniker: str
    source_range: SourceRange
    roles: int = 0
    enclosing_range: SourceRange | None = None

    def __post_init__(self) -> None:
        _required_text(self.symbol_moniker, name="symbol_moniker")
        _zero_based_integer(self.roles, name="roles")

    @property
    def is_definition(self) -> bool:
        return bool(self.roles & 1)

    @property
    def sort_key(self) -> tuple[Any, ...]:
        return self.source_range.sort_key, self.symbol_moniker, self.roles

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "symbol_moniker": self.symbol_moniker,
            "source_range": self.source_range.to_dict(),
            "roles": self.roles,
        }
        if self.enclosing_range is not None:
            payload["enclosing_range"] = self.enclosing_range.to_dict()
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> OccurrenceFact:
        enclosing = payload.get("enclosing_range")
        return cls(
            symbol_moniker=payload.get("symbol_moniker"),
            source_range=SourceRange.from_dict(payload["source_range"]),
            roles=payload.get("roles", 0),
            enclosing_range=(
                SourceRange.from_dict(enclosing)
                if isinstance(enclosing, Mapping)
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class EdgeFact:
    """One resolved or unresolved semantic relationship."""

    kind: str
    provenance: str
    target_symbol_id: str | None = None
    target_moniker: str | None = None
    source_symbol_id: str | None = None
    anchor_range: SourceRange | None = None
    confidence: float = 1.0
    resolver: str | None = None

    def __post_init__(self) -> None:
        _required_text(self.kind, name="edge kind")
        if self.provenance not in FACT_PROVENANCES:
            raise ValueError(
                f"unsupported edge provenance {self.provenance!r}; "
                f"supported: {sorted(FACT_PROVENANCES)}"
            )
        targets = int(self.target_symbol_id is not None) + int(
            self.target_moniker is not None
        )
        if targets != 1:
            raise ValueError(
                "exactly one of target_symbol_id or target_moniker is required"
            )
        if self.target_symbol_id is not None:
            _required_text(self.target_symbol_id, name="target_symbol_id")
        if self.target_moniker is not None:
            _required_text(self.target_moniker, name="target_moniker")
        if self.source_symbol_id is not None:
            _required_text(self.source_symbol_id, name="source_symbol_id")
        if isinstance(self.confidence, bool) or not isinstance(
            self.confidence, (int, float)
        ):
            raise ValueError("confidence must be a finite number between 0 and 1")
        if not math.isfinite(float(self.confidence)) or not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be a finite number between 0 and 1")
        if self.resolver is not None:
            _required_text(self.resolver, name="resolver")
        if self.provenance in {PROVENANCE_FRAMEWORK, PROVENANCE_HEURISTIC} and (
            self.resolver is None
        ):
            raise ValueError(
                "framework and heuristic edges must name the resolver that "
                "synthesized them"
            )

    @property
    def sort_key(self) -> tuple[Any, ...]:
        anchor = self.anchor_range.sort_key if self.anchor_range else (-1, -1, -1, -1)
        return (
            self.source_symbol_id or "",
            self.kind,
            self.target_symbol_id or "",
            self.target_moniker or "",
            anchor,
            self.provenance,
            float(self.confidence),
            self.resolver or "",
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "provenance": self.provenance,
            "confidence": float(self.confidence),
        }
        for name in (
            "target_symbol_id",
            "target_moniker",
            "source_symbol_id",
            "resolver",
        ):
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        if self.anchor_range is not None:
            payload["anchor_range"] = self.anchor_range.to_dict()
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EdgeFact:
        anchor = payload.get("anchor_range")
        return cls(
            kind=payload.get("kind"),
            provenance=payload.get("provenance"),
            target_symbol_id=payload.get("target_symbol_id"),
            target_moniker=payload.get("target_moniker"),
            source_symbol_id=payload.get("source_symbol_id"),
            anchor_range=(
                SourceRange.from_dict(anchor) if isinstance(anchor, Mapping) else None
            ),
            confidence=payload.get("confidence", 1.0),
            resolver=payload.get("resolver"),
        )


@dataclass(frozen=True, slots=True)
class DiagnosticFact:
    """Bounded analyzer diagnostic retained with its source batch."""

    severity: str
    code: str
    message: str
    source_range: SourceRange | None = None

    def __post_init__(self) -> None:
        if self.severity not in _DIAGNOSTIC_SEVERITIES:
            raise ValueError(
                f"unsupported diagnostic severity {self.severity!r}; "
                f"supported: {sorted(_DIAGNOSTIC_SEVERITIES)}"
            )
        code = _required_text(self.code, name="diagnostic code")
        message = _required_text(self.message, name="diagnostic message")
        if len(code) > _MAX_DIAGNOSTIC_CODE_CHARS:
            raise ValueError(
                "diagnostic code must be at most "
                f"{_MAX_DIAGNOSTIC_CODE_CHARS} characters"
            )
        if len(message) > _MAX_DIAGNOSTIC_MESSAGE_CHARS:
            raise ValueError(
                "diagnostic message must be at most "
                f"{_MAX_DIAGNOSTIC_MESSAGE_CHARS} characters"
            )

    @property
    def sort_key(self) -> tuple[Any, ...]:
        source = self.source_range.sort_key if self.source_range else (-1, -1, -1, -1)
        return self.severity, self.code, source, self.message

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        if self.source_range is not None:
            payload["source_range"] = self.source_range.to_dict()
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> DiagnosticFact:
        source = payload.get("source_range")
        return cls(
            severity=payload.get("severity"),
            code=payload.get("code"),
            message=payload.get("message"),
            source_range=(
                SourceRange.from_dict(source) if isinstance(source, Mapping) else None
            ),
        )


@dataclass(frozen=True, slots=True)
class FactBatch:
    """Deterministically ordered semantic facts for exactly one source file."""

    file_path: str
    language: str
    content_digest: str
    profile_digest: str
    provider: str
    completeness: str
    position_encoding: str = "UTF8"
    capabilities: tuple[str, ...] = ()
    symbols: tuple[SymbolFact, ...] = ()
    occurrences: tuple[OccurrenceFact, ...] = ()
    edges: tuple[EdgeFact, ...] = ()
    diagnostics: tuple[DiagnosticFact, ...] = ()
    schema_version: int = FACT_BATCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "file_path", _canonical_file_path(self.file_path))
        _required_text(self.language, name="language")
        object.__setattr__(
            self,
            "content_digest",
            _sha256(self.content_digest, name="content_digest"),
        )
        object.__setattr__(
            self,
            "profile_digest",
            _sha256(self.profile_digest, name="profile_digest"),
        )
        _required_text(self.provider, name="provider")
        _required_text(self.position_encoding, name="position_encoding")
        if self.completeness not in FACT_COMPLETENESS:
            raise ValueError(
                f"unsupported completeness {self.completeness!r}; "
                f"supported: {sorted(FACT_COMPLETENESS)}"
            )
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != FACT_BATCH_SCHEMA_VERSION
        ):
            raise ValueError(
                f"FactBatch schema_version={self.schema_version!r}, "
                f"expected {FACT_BATCH_SCHEMA_VERSION}"
            )

        capabilities = tuple(sorted(set(self.capabilities)))
        if any(capability not in FACT_CAPABILITIES for capability in capabilities):
            unknown = sorted(set(capabilities) - FACT_CAPABILITIES)
            raise ValueError(f"unsupported fact capabilities: {unknown}")
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(
            self, "symbols", tuple(sorted(self.symbols, key=lambda item: item.sort_key))
        )
        object.__setattr__(
            self,
            "occurrences",
            tuple(sorted(set(self.occurrences), key=lambda item: item.sort_key)),
        )
        object.__setattr__(
            self,
            "edges",
            tuple(sorted(set(self.edges), key=lambda item: item.sort_key)),
        )
        object.__setattr__(
            self,
            "diagnostics",
            tuple(sorted(set(self.diagnostics), key=lambda item: item.sort_key)),
        )
        symbol_ids = [symbol.symbol_id for symbol in self.symbols]
        if len(symbol_ids) != len(set(symbol_ids)):
            raise ValueError("FactBatch symbol_id values must be unique per file")
        if self.completeness == COMPLETENESS_FAILED and (
            self.symbols or self.occurrences or self.edges
        ):
            raise ValueError("failed FactBatch values must not contain semantic facts")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "file_path": self.file_path,
            "language": self.language,
            "content_digest": self.content_digest,
            "profile_digest": self.profile_digest,
            "provider": self.provider,
            "completeness": self.completeness,
            "position_encoding": self.position_encoding,
            "capabilities": list(self.capabilities),
            "symbols": [symbol.to_dict() for symbol in self.symbols],
            "occurrences": [item.to_dict() for item in self.occurrences],
            "edges": [edge.to_dict() for edge in self.edges],
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> FactBatch:
        return cls(
            schema_version=payload.get("schema_version"),
            file_path=payload.get("file_path"),
            language=payload.get("language"),
            content_digest=payload.get("content_digest"),
            profile_digest=payload.get("profile_digest"),
            provider=payload.get("provider"),
            completeness=payload.get("completeness"),
            position_encoding=payload.get("position_encoding", "UTF8"),
            capabilities=tuple(payload.get("capabilities") or ()),
            symbols=tuple(
                SymbolFact.from_dict(item) for item in payload.get("symbols") or ()
            ),
            occurrences=tuple(
                OccurrenceFact.from_dict(item)
                for item in payload.get("occurrences") or ()
            ),
            edges=tuple(
                EdgeFact.from_dict(item) for item in payload.get("edges") or ()
            ),
            diagnostics=tuple(
                DiagnosticFact.from_dict(item)
                for item in payload.get("diagnostics") or ()
            ),
        )

    def canonical_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )

    def digest(self) -> str:
        return sha256_digest(self.canonical_json())

    def semantic_dict(self) -> dict[str, Any]:
        """Return observable facts without build-route identity fields."""

        payload = self.to_dict()
        payload.pop("profile_digest")
        payload.pop("provider")
        return payload

    def semantic_digest(self) -> str:
        return sha256_digest(
            json.dumps(
                self.semantic_dict(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
        )


def merge_fact_batches(
    batches: Sequence[FactBatch],
    *,
    provider: str,
    profile_digest: str,
    completeness: str = COMPLETENESS_PARTIAL,
) -> FactBatch:
    """Merge compatible provider batches without losing their provenance."""

    values = tuple(batches)
    if not values:
        raise ValueError("at least one FactBatch is required")
    first = values[0]
    compatibility = (
        first.file_path,
        first.language,
        first.content_digest,
        first.position_encoding,
    )
    for batch in values[1:]:
        candidate = (
            batch.file_path,
            batch.language,
            batch.content_digest,
            batch.position_encoding,
        )
        if candidate != compatibility:
            raise ValueError(
                "FactBatch values must share file, language, content digest, "
                "and position encoding"
            )

    symbols: dict[str, SymbolFact] = {}
    for batch in values:
        for symbol in batch.symbols:
            existing = symbols.get(symbol.symbol_id)
            if existing is not None and existing != symbol:
                raise ValueError(
                    f"conflicting facts for symbol_id {symbol.symbol_id!r}"
                )
            symbols[symbol.symbol_id] = symbol
    return FactBatch(
        file_path=first.file_path,
        language=first.language,
        content_digest=first.content_digest,
        profile_digest=profile_digest,
        provider=provider,
        completeness=completeness,
        position_encoding=first.position_encoding,
        capabilities=tuple(
            capability for batch in values for capability in batch.capabilities
        ),
        symbols=tuple(symbols.values()),
        occurrences=tuple(item for batch in values for item in batch.occurrences),
        edges=tuple(edge for batch in values for edge in batch.edges),
        diagnostics=tuple(item for batch in values for item in batch.diagnostics),
    )


__all__ = [
    "CAPABILITY_DEFINITIONS",
    "CAPABILITY_DIAGNOSTICS",
    "CAPABILITY_EDGES",
    "CAPABILITY_OCCURRENCES",
    "COMPLETENESS_COMPLETE",
    "COMPLETENESS_FAILED",
    "COMPLETENESS_PARTIAL",
    "COMPLETENESS_SYNTACTIC",
    "DiagnosticFact",
    "EdgeFact",
    "FACT_BATCH_SCHEMA_VERSION",
    "FACT_CAPABILITIES",
    "FACT_COMPLETENESS",
    "FACT_PROVENANCES",
    "FactBatch",
    "OccurrenceFact",
    "PROVENANCE_CLANGD",
    "PROVENANCE_FRAMEWORK",
    "PROVENANCE_HEURISTIC",
    "PROVENANCE_LSP",
    "PROVENANCE_SCIP",
    "PROVENANCE_SYNTACTIC",
    "SourceRange",
    "SymbolFact",
    "merge_fact_batches",
    "sha256_digest",
]
