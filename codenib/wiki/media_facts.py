# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Structured visual facts extracted from repository media artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from itertools import islice
from pathlib import PurePosixPath
from typing import Any, Callable, Iterable, Mapping

MEDIA_FACTS_SCHEMA = "codenib.media-facts.v1"
MEDIA_FACTS_VERSION = 1

_MAX_PROMPT_BYTES = 32 * 1024
_MAX_TEXT_BYTES = 4096
_MAX_ARTIFACTS = 4096
_MAX_FACTS_PER_ARTIFACT = 32
_MAX_FACT_PACK_BYTES = 64 * 1024
_MAX_GROUNDING_CANDIDATES = 32
_MAX_METADATA_BYTES = 8 * 1024
_MAX_METADATA_ITEMS = 32
_MAX_REFERENCES_PER_ARTIFACT = 64
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")
_STOPWORDS = frozenset(
    {
        "and",
        "are",
        "asset",
        "below",
        "code",
        "diagram",
        "docs",
        "image",
        "maps",
        "media",
        "overview",
        "png",
        "repository",
        "screenshot",
        "service",
        "shown",
        "svg",
        "the",
        "this",
        "webp",
        "wiki",
    }
)

VisualFactExtractor = Callable[[Mapping[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True)
class VisualEntity:
    """One entity mentioned or depicted by a repository media artifact."""

    name: str
    type: str
    evidence: str = ""
    confidence: float = 0.0
    grounding_candidates: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["grounding_candidates"] = list(self.grounding_candidates)
        return data


@dataclass(frozen=True)
class VisualRelation:
    """One relation between extracted visual entities."""

    source: str
    relation: str
    target: str
    evidence: str = ""
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VisualClaim:
    """One source-grounded claim inferred from a media artifact."""

    text: str
    evidence: str
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VisualFactPack:
    """Structured facts for one repository media artifact."""

    artifact_path: str
    artifact_sha256: str
    role_hint: str
    extractor: str
    entities: tuple[VisualEntity, ...] = ()
    relations: tuple[VisualRelation, ...] = ()
    claims: tuple[VisualClaim, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_path": self.artifact_path,
            "artifact_sha256": self.artifact_sha256,
            "role_hint": self.role_hint,
            "extractor": self.extractor,
            "entities": [entity.to_dict() for entity in self.entities],
            "relations": [relation.to_dict() for relation in self.relations],
            "claims": [claim.to_dict() for claim in self.claims],
            "metadata": dict(self.metadata),
            "fact_pack_sha256": self.fact_pack_sha256,
        }

    @property
    def fact_pack_sha256(self) -> str:
        payload = {
            "artifact_path": self.artifact_path,
            "artifact_sha256": self.artifact_sha256,
            "role_hint": self.role_hint,
            "extractor": self.extractor,
            "entities": [entity.to_dict() for entity in self.entities],
            "relations": [relation.to_dict() for relation in self.relations],
            "claims": [claim.to_dict() for claim in self.claims],
            "metadata": dict(self.metadata),
        }
        return _sha256_json(payload)


@dataclass(frozen=True)
class VisualFactsManifest:
    """A stable collection of visual fact packs for one media manifest."""

    schema: str
    version: int
    media_manifest_sha256: str
    facts: tuple[VisualFactPack, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "media_manifest_sha256": self.media_manifest_sha256,
            "fact_count": len(self.facts),
            "facts": [fact.to_dict() for fact in self.facts],
            "manifest_sha256": self.manifest_sha256,
        }

    @property
    def manifest_sha256(self) -> str:
        return compute_visual_facts_manifest_sha256(
            schema=self.schema,
            version=self.version,
            media_manifest_sha256=self.media_manifest_sha256,
            facts=(fact.to_dict() for fact in self.facts),
        )


def compute_visual_facts_manifest_sha256(
    *,
    schema: str,
    version: int,
    media_manifest_sha256: str,
    facts: Iterable[Mapping[str, Any]],
) -> str:
    """Return the canonical digest used by :class:`VisualFactsManifest`."""

    return _sha256_json(
        {
            "schema": schema,
            "version": version,
            "media_manifest_sha256": media_manifest_sha256,
            "facts": list(facts),
        }
    )


def build_visual_fact_extraction_prompt(artifact: Mapping[str, Any]) -> str:
    """Build a bounded VLM prompt that asks for structured visual facts."""

    payload = {
        "task": "extract_repository_visual_facts",
        "artifact": _artifact_prompt_payload(artifact),
        "output_schema": {
            "entities": [
                {
                    "name": "string",
                    "type": "component|ui_element|data_store|process|unknown",
                    "evidence": "string",
                    "confidence": "number between 0 and 1",
                    "grounding_candidates": ["symbol_or_file_name"],
                }
            ],
            "relations": [
                {
                    "source": "entity name",
                    "relation": "calls|depends_on|renders|routes_to|contains|unknown",
                    "target": "entity name",
                    "evidence": "string",
                    "confidence": "number between 0 and 1",
                }
            ],
            "claims": [
                {
                    "text": "source-grounded visual claim",
                    "evidence": "caption, surrounding markdown, visual region, or label",
                    "confidence": "number between 0 and 1",
                }
            ],
        },
        "requirements": [
            "Return JSON only.",
            "Do not invent repository symbols that are not visually or textually supported.",
            "Prefer entities that can later be grounded to files, symbols, routes, or dependencies.",
            "Use the artifact caption and surrounding markdown as supporting context, not as proof of unseen code.",
        ],
    }
    prompt = (
        "Extract structured repository knowledge from this visual artifact.\n\n"
        + json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2)
    )
    if len(prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
        raise ValueError("visual fact extraction prompt exceeds the byte limit")
    return prompt


def deterministic_visual_facts(artifact: Mapping[str, Any]) -> dict[str, Any]:
    """Return a conservative local fact pack from artifact metadata only."""

    path = _safe_relative_path(artifact.get("path"))
    sha256 = _safe_text(artifact.get("sha256"))
    role = _safe_text(artifact.get("role_hint") or "repository_image")
    caption = _safe_text(artifact.get("caption"))
    surrounding = _safe_text(artifact.get("surrounding_text"))
    references = artifact.get("references") or ()
    entities = _metadata_entities(path, role, caption, surrounding)
    claims = _metadata_claims(path, role, caption, surrounding, references)
    pack = VisualFactPack(
        artifact_path=path,
        artifact_sha256=sha256,
        role_hint=role,
        extractor="local/metadata",
        entities=tuple(entities[:_MAX_FACTS_PER_ARTIFACT]),
        claims=tuple(claims[:_MAX_FACTS_PER_ARTIFACT]),
        metadata={"source": "artifact-path-caption-surrounding-markdown"},
    )
    return pack.to_dict()


def build_visual_facts_manifest(
    media_manifest: Mapping[str, Any],
    *,
    extractor: VisualFactExtractor = deterministic_visual_facts,
) -> dict[str, Any]:
    """Extract visual fact packs for every artifact in a media manifest."""

    facts = []
    for artifact in _mapping_items(
        media_manifest.get("artifacts"),
        limit=_MAX_ARTIFACTS,
    ):
        artifact_path = _safe_relative_path(artifact.get("path"))
        if not artifact_path:
            continue
        pack = extractor(artifact)
        if not isinstance(pack, Mapping):
            raise ValueError("visual fact extractor must return a mapping")
        normalized = _fact_pack_from_mapping(
            pack,
            artifact_path=artifact_path,
            artifact_sha256=_safe_text(artifact.get("sha256")),
            role_hint=_safe_text(artifact.get("role_hint") or "repository_image"),
        )
        if _json_size(normalized.to_dict()) > _MAX_FACT_PACK_BYTES:
            raise ValueError("visual fact pack exceeds the byte limit")
        facts.append(normalized)
    manifest = VisualFactsManifest(
        schema=MEDIA_FACTS_SCHEMA,
        version=MEDIA_FACTS_VERSION,
        media_manifest_sha256=_safe_text(media_manifest.get("manifest_sha256")),
        facts=tuple(sorted(facts, key=lambda fact: fact.artifact_path)),
    )
    return manifest.to_dict()


def normalize_visual_fact_pack(
    value: Mapping[str, Any],
    *,
    artifact: Mapping[str, Any],
) -> dict[str, Any]:
    """Normalize untrusted extractor output under trusted artifact provenance."""

    artifact_path = _safe_relative_path(artifact.get("path"))
    if not artifact_path:
        raise ValueError("visual fact artifact path must be repository-relative")
    normalized = _fact_pack_from_mapping(
        value,
        artifact_path=artifact_path,
        artifact_sha256=_safe_text(artifact.get("sha256")),
        role_hint=_safe_text(artifact.get("role_hint") or "repository_image"),
    )
    payload = normalized.to_dict()
    if _json_size(payload) > _MAX_FACT_PACK_BYTES:
        raise ValueError("visual fact pack exceeds the byte limit")
    return payload


def _artifact_prompt_payload(artifact: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "path": _safe_text(artifact.get("path")),
        "mime_type": _safe_text(artifact.get("mime_type")),
        "role_hint": _safe_text(artifact.get("role_hint")),
        "caption": _safe_text(artifact.get("caption")),
        "surrounding_text": _safe_text(artifact.get("surrounding_text")),
        "references": [
            {
                "markdown_path": _safe_text(reference.get("markdown_path")),
                "line": reference.get("line")
                if type(reference.get("line")) is int
                else 0,
                "alt_text": _safe_text(reference.get("alt_text")),
                "title": _safe_text(reference.get("title")),
            }
            for reference in _mapping_items(artifact.get("references"), limit=8)
        ],
    }


def _metadata_entities(
    path: str,
    role: str,
    caption: str,
    surrounding: str,
) -> list[VisualEntity]:
    names = _entity_names(" ".join([path, caption, surrounding]))
    entity_type = {
        "architecture_diagram": "component",
        "ui_screenshot": "ui_element",
    }.get(role, "unknown")
    entities = [
        VisualEntity(
            name=name,
            type=entity_type,
            evidence=caption or path,
            confidence=0.35,
            grounding_candidates=(name,),
        )
        for name in names
    ]
    if role and not entities:
        entities.append(
            VisualEntity(
                name=role.replace("_", " "),
                type=entity_type,
                evidence=path,
                confidence=0.25,
            )
        )
    return entities


def _metadata_claims(
    path: str,
    role: str,
    caption: str,
    surrounding: str,
    references: Any,
) -> list[VisualClaim]:
    claims = [
        VisualClaim(
            text=f"{path} is a repository visual artifact with role hint {role}.",
            evidence=path,
            confidence=0.5,
        )
    ]
    if caption:
        claims.append(
            VisualClaim(
                text=f"{path} is described as: {caption}",
                evidence=caption,
                confidence=0.45,
            )
        )
    if surrounding:
        claims.append(
            VisualClaim(
                text=f"{path} is referenced from nearby documentation context.",
                evidence=surrounding,
                confidence=0.4,
            )
        )
    reference_count = sum(
        1
        for _ in _mapping_items(
            references,
            limit=_MAX_REFERENCES_PER_ARTIFACT,
        )
    )
    if reference_count:
        claims.append(
            VisualClaim(
                text=f"{path} has {reference_count} repository documentation reference(s).",
                evidence="markdown references",
                confidence=0.5,
            )
        )
    return claims


def _fact_pack_from_mapping(
    value: Mapping[str, Any],
    *,
    artifact_path: str,
    artifact_sha256: str,
    role_hint: str,
) -> VisualFactPack:
    return VisualFactPack(
        artifact_path=artifact_path,
        artifact_sha256=artifact_sha256,
        role_hint=role_hint,
        extractor=_safe_text(value.get("extractor")),
        entities=tuple(
            VisualEntity(
                name=_safe_text(entity.get("name")),
                type=_safe_text(entity.get("type") or "unknown"),
                evidence=_safe_text(entity.get("evidence")),
                confidence=_confidence(entity.get("confidence")),
                grounding_candidates=tuple(
                    _safe_text(candidate)
                    for candidate in islice(
                        _non_string_iterable(entity.get("grounding_candidates")),
                        _MAX_GROUNDING_CANDIDATES,
                    )
                    if _safe_text(candidate)
                ),
            )
            for entity in _mapping_items(
                value.get("entities"),
                limit=_MAX_FACTS_PER_ARTIFACT,
            )
        ),
        relations=tuple(
            VisualRelation(
                source=_safe_text(relation.get("source")),
                relation=_safe_text(relation.get("relation") or "unknown"),
                target=_safe_text(relation.get("target")),
                evidence=_safe_text(relation.get("evidence")),
                confidence=_confidence(relation.get("confidence")),
            )
            for relation in _mapping_items(
                value.get("relations"),
                limit=_MAX_FACTS_PER_ARTIFACT,
            )
        ),
        claims=tuple(
            VisualClaim(
                text=_safe_text(claim.get("text")),
                evidence=_safe_text(claim.get("evidence")),
                confidence=_confidence(claim.get("confidence")),
            )
            for claim in _mapping_items(
                value.get("claims"),
                limit=_MAX_FACTS_PER_ARTIFACT,
            )
        ),
        metadata=_bounded_metadata(value.get("metadata")),
    )


def _entity_names(text: str) -> list[str]:
    seen = set()
    names = []
    for match in _WORD_RE.finditer(text):
        value = match.group(0)
        normalized = value.lower()
        if normalized in _STOPWORDS or normalized in seen:
            continue
        seen.add(normalized)
        names.append(value)
        if len(names) >= 12:
            break
    return names


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


def _confidence(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, confidence))


def _bounded_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    metadata = {
        key: item
        for raw_key, item in islice(value.items(), _MAX_METADATA_ITEMS)
        if (key := _safe_text(raw_key, max_bytes=256))
    }
    try:
        size = _json_size(metadata)
    except (RecursionError, TypeError, ValueError) as exc:
        raise ValueError("visual fact metadata must contain bounded JSON") from exc
    if size > _MAX_METADATA_BYTES:
        raise ValueError("visual fact metadata exceeds the byte limit")
    return metadata


def _json_size(payload: Mapping[str, Any]) -> int:
    return len(
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def _sha256_json(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_text(value: Any, *, max_bytes: int = _MAX_TEXT_BYTES) -> str:
    text = str(value or "").strip()
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    encoded = text.encode("utf-8")[: max(0, max_bytes - 1)]
    return encoded.decode("utf-8", errors="ignore").rstrip() + "…"


def _safe_relative_path(value: Any) -> str:
    text = _safe_text(value)
    if not text or "\\" in text:
        return ""
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return ""
    return path.as_posix()


__all__ = [
    "MEDIA_FACTS_SCHEMA",
    "MEDIA_FACTS_VERSION",
    "VisualClaim",
    "VisualEntity",
    "VisualFactPack",
    "VisualFactsManifest",
    "VisualRelation",
    "build_visual_fact_extraction_prompt",
    "build_visual_facts_manifest",
    "compute_visual_facts_manifest_sha256",
    "deterministic_visual_facts",
    "normalize_visual_fact_pack",
]
