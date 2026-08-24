# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Ground structured visual facts to repository files and symbols."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from itertools import islice
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping

from ..languages import extension_to_language_map
from ..repository_filters import walk_repository_files
from ..repository_source_selection import (
    DEFAULT_REPOSITORY_SOURCE_SELECTION,
    RepositorySourceSelection,
)
from ._safe_file_reads import read_regular_bytes

MEDIA_GROUNDING_SCHEMA = "codenib.media-grounding.v1"
MEDIA_GROUNDING_VERSION = 1

_SOURCE_EXTENSIONS = frozenset(extension_to_language_map("chunker"))
_MAX_SOURCE_BYTES = 1024 * 1024
_MAX_CANDIDATES = 8192
_MAX_FACT_PACKS = 4096
_MAX_ENTITIES_PER_ARTIFACT = 32
_MAX_TOTAL_ENTITIES = 4096
_MAX_GROUNDING_HINTS = 32
_MAX_BINDINGS_PER_ENTITY = 5
_MAX_TEXT_BYTES = 4096
_MAX_PATH_BYTES = 4096
_SYMBOL_RE = re.compile(
    r"\b(?:class|def|function|const|let|var|interface|type|struct|enum)\s+([A-Za-z_][A-Za-z0-9_]*)"
)
_CAMEL_RE = re.compile(r"\b[A-Z][A-Za-z0-9_]{2,}\b")
VisualGroundingScorer = Callable[
    [Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any] | None
]


@dataclass(frozen=True)
class SourceSymbolCandidate:
    """One repository source target that a visual entity can bind to."""

    path: str
    symbol: str = ""
    kind: str = "source"
    line: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VisualCodeBinding:
    """A candidate grounding from a visual entity to source evidence."""

    artifact_path: str
    entity_name: str
    source_path: str
    symbol: str = ""
    kind: str = "source"
    line: int = 0
    score: float = 0.0
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VisualGroundingManifest:
    """Stable visual-code binding manifest for one visual facts manifest."""

    schema: str
    version: int
    visual_facts_manifest_sha256: str
    bindings: tuple[VisualCodeBinding, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "visual_facts_manifest_sha256": self.visual_facts_manifest_sha256,
            "binding_count": len(self.bindings),
            "bindings": [binding.to_dict() for binding in self.bindings],
            "manifest_sha256": self.manifest_sha256,
        }

    @property
    def manifest_sha256(self) -> str:
        payload = {
            "schema": self.schema,
            "version": self.version,
            "visual_facts_manifest_sha256": self.visual_facts_manifest_sha256,
            "bindings": [binding.to_dict() for binding in self.bindings],
        }
        return _sha256_json(payload)


def discover_source_symbol_candidates(
    repo_path: str | Path,
    *,
    exclude_roots: Iterable[str | Path] = (),
    selection: RepositorySourceSelection = DEFAULT_REPOSITORY_SOURCE_SELECTION,
    max_candidates: int = _MAX_CANDIDATES,
) -> list[dict[str, Any]]:
    """Return a bounded, deterministic source-symbol inventory for grounding."""

    root = Path(repo_path).expanduser().resolve()
    selected = RepositorySourceSelection(selection.exclude_subtrees)
    candidate_limit = _validated_limit(
        max_candidates,
        name="max_candidates",
        maximum=_MAX_CANDIDATES,
    )
    candidates: list[SourceSymbolCandidate] = []
    seen: set[tuple[str, str, int]] = set()
    for path in walk_repository_files(
        root,
        exclude_roots=exclude_roots,
        selection=selected,
    ):
        if len(candidates) >= candidate_limit:
            break
        if path.is_symlink() or path.suffix.lower() not in _SOURCE_EXTENSIONS:
            continue
        payload = read_regular_bytes(path, max_bytes=_MAX_SOURCE_BYTES)
        if payload is None:
            continue
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            continue
        relative = path.relative_to(root).as_posix()
        candidates.append(SourceSymbolCandidate(path=relative))
        if len(candidates) >= candidate_limit:
            break
        for symbol, line in _symbols(text):
            key = (relative, symbol, line)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                SourceSymbolCandidate(
                    path=relative,
                    symbol=symbol,
                    kind="symbol",
                    line=line,
                )
            )
            if len(candidates) >= candidate_limit:
                break
    return [candidate.to_dict() for candidate in candidates[:candidate_limit]]


def ground_visual_facts_to_sources(
    visual_facts_manifest: Mapping[str, Any],
    source_candidates: Iterable[Mapping[str, Any]],
    *,
    max_bindings_per_entity: int = _MAX_BINDINGS_PER_ENTITY,
    scorer: VisualGroundingScorer | None = None,
) -> dict[str, Any]:
    """Ground visual entities to a source inventory.

    The default scorer is deterministic and lexical. Callers can pass a scorer
    backed by BM25, embeddings, CodeGraph, LSP facts, or FactQueryIndex without
    changing the visual-code binding manifest schema.
    """

    binding_limit = _validated_limit(
        max_bindings_per_entity,
        name="max_bindings_per_entity",
        maximum=_MAX_BINDINGS_PER_ENTITY,
    )
    if scorer is not None and not callable(scorer):
        raise ValueError("scorer must be callable")
    candidates_by_key: dict[tuple[str, str, str, int], SourceSymbolCandidate] = {}
    for value in _mapping_items(source_candidates, limit=_MAX_CANDIDATES):
        candidate = _candidate_from_mapping(value)
        if not candidate.path:
            continue
        key = (candidate.path, candidate.symbol, candidate.kind, candidate.line)
        candidates_by_key.setdefault(key, candidate)
    candidates = list(candidates_by_key.values())
    candidate_payloads = (
        {candidate: candidate.to_dict() for candidate in candidates}
        if scorer is not None
        else {}
    )
    bindings: list[VisualCodeBinding] = []
    entity_count = 0
    for fact_pack in _mapping_items(
        visual_facts_manifest.get("facts"),
        limit=_MAX_FACT_PACKS,
    ):
        if entity_count >= _MAX_TOTAL_ENTITIES:
            break
        artifact_path = _safe_relative_path(fact_pack.get("artifact_path"))
        if not artifact_path:
            continue
        for entity in _mapping_items(
            fact_pack.get("entities"),
            limit=_MAX_ENTITIES_PER_ARTIFACT,
        ):
            if entity_count >= _MAX_TOTAL_ENTITIES:
                break
            entity_count += 1
            entity_name = _safe_text(entity.get("name"))
            if not entity_name:
                continue
            hints = [
                entity_name,
                *[
                    _safe_text(candidate)
                    for candidate in islice(
                        _non_string_iterable(entity.get("grounding_candidates")),
                        _MAX_GROUNDING_HINTS,
                    )
                ],
            ]
            scored = [
                binding
                for binding in (
                    _score_with_optional_scorer(
                        artifact_path=artifact_path,
                        entity=entity,
                        entity_name=entity_name,
                        hints=hints,
                        candidate=candidate,
                        candidate_payload=candidate_payloads.get(candidate),
                        scorer=scorer,
                    )
                    for candidate in candidates
                )
                if binding is not None
            ]
            scored.sort(
                key=lambda binding: (
                    -binding.score,
                    binding.source_path,
                    binding.symbol,
                    binding.line,
                )
            )
            bindings.extend(scored[:binding_limit])
    manifest = VisualGroundingManifest(
        schema=MEDIA_GROUNDING_SCHEMA,
        version=MEDIA_GROUNDING_VERSION,
        visual_facts_manifest_sha256=_safe_text(
            visual_facts_manifest.get("manifest_sha256")
        ),
        bindings=tuple(
            sorted(
                _dedupe_bindings(bindings),
                key=lambda binding: (
                    binding.artifact_path,
                    binding.entity_name,
                    -binding.score,
                    binding.source_path,
                    binding.symbol,
                ),
            )
        ),
    )
    return manifest.to_dict()


def _candidate_from_mapping(value: Mapping[str, Any]) -> SourceSymbolCandidate:
    return SourceSymbolCandidate(
        path=_safe_relative_path(value.get("path")),
        symbol=_safe_text(value.get("symbol")),
        kind=_safe_text(value.get("kind") or "source"),
        line=_positive_int(value.get("line")),
    )


def _score_with_optional_scorer(
    *,
    artifact_path: str,
    entity: Mapping[str, Any],
    entity_name: str,
    hints: list[str],
    candidate: SourceSymbolCandidate,
    candidate_payload: Mapping[str, Any] | None,
    scorer: VisualGroundingScorer | None,
) -> VisualCodeBinding | None:
    if scorer is None:
        return _score_candidate(
            artifact_path=artifact_path,
            entity_name=entity_name,
            hints=hints,
            candidate=candidate,
        )
    scorer_entity = {
        "name": entity_name,
        "type": _safe_text(entity.get("type") or "unknown"),
        "evidence": _safe_text(entity.get("evidence")),
        "confidence": _confidence(entity.get("confidence")),
        "grounding_candidates": [_safe_text(hint) for hint in hints[1:]],
    }
    raw = scorer(
        scorer_entity,
        candidate_payload if candidate_payload is not None else candidate.to_dict(),
    )
    if not isinstance(raw, Mapping):
        return None
    score = _positive_score(raw.get("score"))
    if score <= 0:
        return None
    return VisualCodeBinding(
        artifact_path=artifact_path,
        entity_name=entity_name,
        source_path=candidate.path,
        symbol=candidate.symbol,
        kind=candidate.kind,
        line=candidate.line,
        score=round(score, 4),
        evidence=_safe_text(raw.get("evidence") or "custom scorer"),
    )


def _score_candidate(
    *,
    artifact_path: str,
    entity_name: str,
    hints: Iterable[str],
    candidate: SourceSymbolCandidate,
) -> VisualCodeBinding | None:
    normalized_hints = [_normalize(hint) for hint in hints if _normalize(hint)]
    candidate_symbol = _normalize(candidate.symbol)
    candidate_path = _normalize(Path(candidate.path).stem)
    candidate_full_path = _normalize(candidate.path)
    score = 0.0
    evidence = ""
    for hint in normalized_hints:
        if candidate_symbol and hint == candidate_symbol:
            score = max(score, 1.0)
            evidence = "exact symbol match"
        elif candidate_symbol and (
            hint in candidate_symbol or candidate_symbol in hint
        ):
            score = max(score, 0.75)
            evidence = "partial symbol match"
        elif hint == candidate_path:
            score = max(score, 0.6)
            evidence = "file stem match"
        elif hint in candidate_full_path:
            score = max(score, 0.45)
            evidence = "path match"
    if score <= 0:
        return None
    return VisualCodeBinding(
        artifact_path=artifact_path,
        entity_name=entity_name,
        source_path=candidate.path,
        symbol=candidate.symbol,
        kind=candidate.kind,
        line=candidate.line,
        score=round(score, 4),
        evidence=evidence,
    )


def _symbols(text: str) -> Iterable[tuple[str, int]]:
    seen = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        for regex in (_SYMBOL_RE, _CAMEL_RE):
            for match in regex.finditer(line):
                symbol = match.group(1) if regex is _SYMBOL_RE else match.group(0)
                if symbol in seen:
                    continue
                seen.add(symbol)
                yield symbol, line_number


def _dedupe_bindings(
    bindings: Iterable[VisualCodeBinding],
) -> tuple[VisualCodeBinding, ...]:
    best: dict[tuple[str, str, str, str, int], VisualCodeBinding] = {}
    for binding in bindings:
        key = (
            binding.artifact_path,
            binding.entity_name,
            binding.source_path,
            binding.symbol,
            binding.line,
        )
        previous = best.get(key)
        if previous is None or binding.score > previous.score:
            best[key] = binding
    return tuple(best.values())


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _positive_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _mapping_items(
    value: Any,
    *,
    limit: int,
) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        return ()
    try:
        values = iter(value or ())
    except TypeError:
        return ()
    return (item for item in islice(values, limit) if isinstance(item, Mapping))


def _non_string_iterable(value: Any) -> Iterable[Any]:
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        return ()
    try:
        return iter(value or ())
    except TypeError:
        return ()


def _validated_limit(value: Any, *, name: str, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= maximum
    ):
        raise ValueError(f"{name} must be an integer between 0 and {maximum}")
    return value


def _confidence(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(confidence):
        return 0.0
    return min(1.0, max(0.0, confidence))


def _positive_score(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(score) or score <= 0:
        return 0.0
    return score


def _sha256_json(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_relative_path(value: Any) -> str:
    text = _safe_text(value, max_bytes=_MAX_PATH_BYTES)
    if not text or "\\" in text:
        return ""
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return ""
    return path.as_posix()


def _safe_text(value: Any, *, max_bytes: int = _MAX_TEXT_BYTES) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = "".join(
        character
        for character in text
        if ord(character) >= 0x20 and ord(character) != 0x7F
    )
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", errors="ignore").rstrip()


__all__ = [
    "MEDIA_GROUNDING_SCHEMA",
    "MEDIA_GROUNDING_VERSION",
    "SourceSymbolCandidate",
    "VisualGroundingScorer",
    "VisualCodeBinding",
    "VisualGroundingManifest",
    "discover_source_symbol_candidates",
    "ground_visual_facts_to_sources",
]
