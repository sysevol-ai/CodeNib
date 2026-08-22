# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Server-side evidence packs for VLM-ready wiki media generation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Mapping

_MAX_TEXT_BYTES = 4096
_MAX_PAGE_SUMMARY_BYTES = 1024
_DEFAULT_MAX_SOURCES = 6
_DEFAULT_MAX_RELATIONS = 12
_DEFAULT_MAX_SNIPPET_BYTES = 1200

SourceReader = Callable[[Mapping[str, Any]], str | None]


@dataclass(frozen=True)
class WikiMediaSourceEvidence:
    """One bounded source citation made available to a media generator."""

    file: str
    symbol: str = ""
    start_line: int | None = None
    end_line: int | None = None
    snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WikiMediaRelationEvidence:
    """One compact graph/fact relation made available to a media generator."""

    source: str
    target: str
    relation: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WikiMediaEvidencePack:
    """Structured, source-grounded VLM input for one planned media slot."""

    slot_id: str
    kind: str
    title: str
    purpose: str
    page: dict[str, str]
    sources: tuple[WikiMediaSourceEvidence, ...] = ()
    relations: tuple[WikiMediaRelationEvidence, ...] = ()
    human_prior: dict[str, Any] = field(default_factory=dict)
    output_requirements: tuple[str, ...] = (
        "Use only the provided source citations and relation evidence.",
        "Prefer a deterministic teaching structure over decorative imagery.",
        "Keep labels grounded in source files, symbols, routes, or relations.",
        "Preserve provenance so the generated asset can be audited later.",
    )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["sources"] = [source.to_dict() for source in self.sources]
        data["relations"] = [relation.to_dict() for relation in self.relations]
        data["output_requirements"] = list(self.output_requirements)
        return data


def build_media_evidence_pack(
    slot: Mapping[str, Any],
    *,
    page_id: str = "",
    page_title: str = "",
    page_markdown: str = "",
    citations: Iterable[Mapping[str, Any]] = (),
    relations: Iterable[Mapping[str, Any]] = (),
    source_reader: SourceReader | None = None,
    max_sources: int = _DEFAULT_MAX_SOURCES,
    max_relations: int = _DEFAULT_MAX_RELATIONS,
    max_snippet_bytes: int = _DEFAULT_MAX_SNIPPET_BYTES,
) -> dict[str, Any]:
    """Build a bounded, provider-neutral evidence pack for one media slot.

    This helper does not read repository files by itself. Callers may pass a
    ``source_reader`` that returns small snippets for already-selected
    citations, which keeps source exposure explicitly server-side and bounded.
    """

    pack = WikiMediaEvidencePack(
        slot_id=_safe_text(slot.get("id")),
        kind=_safe_text(slot.get("kind")),
        title=_safe_text(slot.get("title")),
        purpose=_safe_text(slot.get("purpose")),
        page={
            "id": _safe_text(page_id),
            "title": _safe_text(page_title),
            "summary": _page_summary(page_markdown),
        },
        sources=_source_evidence(
            slot,
            citations,
            source_reader=source_reader,
            max_sources=max_sources,
            max_snippet_bytes=max_snippet_bytes,
        ),
        relations=_relation_evidence(relations, max_relations=max_relations),
        human_prior=_human_prior(slot.get("human_prior")),
    )
    return pack.to_dict()


def _source_evidence(
    slot: Mapping[str, Any],
    citations: Iterable[Mapping[str, Any]],
    *,
    source_reader: SourceReader | None,
    max_sources: int,
    max_snippet_bytes: int,
) -> tuple[WikiMediaSourceEvidence, ...]:
    allowed_files = {
        file
        for file in (_safe_file(value) for value in slot.get("source_citations") or ())
        if file
    }
    evidence: list[WikiMediaSourceEvidence] = []
    seen: set[tuple[str, str, int | None, int | None]] = set()
    for citation in citations or ():
        if not isinstance(citation, Mapping):
            continue
        file = _safe_file(citation.get("file"))
        if not file or (allowed_files and file not in allowed_files):
            continue
        symbol = _safe_text(citation.get("symbol") or citation.get("name"))
        start_line = _positive_int(citation.get("start_line") or citation.get("line"))
        end_line = _positive_int(citation.get("end_line"))
        key = (file, symbol, start_line, end_line)
        if key in seen:
            continue
        seen.add(key)

        snippet = _safe_text(citation.get("snippet"), max_bytes=max_snippet_bytes)
        if source_reader is not None and not snippet:
            snippet = _safe_text(source_reader(citation), max_bytes=max_snippet_bytes)
        evidence.append(
            WikiMediaSourceEvidence(
                file=file,
                symbol=symbol,
                start_line=start_line,
                end_line=end_line,
                snippet=snippet,
            )
        )
        if len(evidence) >= max(0, max_sources):
            break
    return tuple(evidence)


def _relation_evidence(
    relations: Iterable[Mapping[str, Any]],
    *,
    max_relations: int,
) -> tuple[WikiMediaRelationEvidence, ...]:
    evidence: list[WikiMediaRelationEvidence] = []
    seen: set[tuple[str, str, str]] = set()
    for relation in relations or ():
        if not isinstance(relation, Mapping):
            continue
        source = _safe_text(
            relation.get("source")
            or relation.get("source_symbol")
            or relation.get("from")
        )
        target = _safe_text(
            relation.get("target")
            or relation.get("target_symbol")
            or relation.get("to")
        )
        kind = _safe_text(relation.get("relation") or relation.get("kind") or "related")
        if not source or not target:
            continue
        key = (source, target, kind)
        if key in seen:
            continue
        seen.add(key)
        evidence.append(WikiMediaRelationEvidence(source, target, kind))
        if len(evidence) >= max(0, max_relations):
            break
    return tuple(evidence)


def _page_summary(markdown: str) -> str:
    lines = []
    for raw_line in str(markdown or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
        if len(" ".join(lines).encode("utf-8")) >= _MAX_PAGE_SUMMARY_BYTES:
            break
    return _safe_text(" ".join(lines), max_bytes=_MAX_PAGE_SUMMARY_BYTES)


def _human_prior(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"editable": True, "notes": []}
    result: dict[str, Any] = {}
    for key, item in value.items():
        safe_key = _safe_text(key, max_bytes=128)
        if safe_key:
            result[safe_key] = _bounded_json_value(item)
    return result


def _bounded_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _bounded_json_value(item)
            for raw_key, item in value.items()
            if (key := _safe_text(raw_key, max_bytes=128))
        }
    if isinstance(value, (list, tuple)):
        return [_bounded_json_value(item) for item in value[:16]]
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _safe_text(value)


def _safe_file(value: Any) -> str:
    file = _safe_text(value)
    if not file or any(ord(character) < 0x20 for character in file):
        return ""
    return file


def _safe_text(value: Any, *, max_bytes: int = _MAX_TEXT_BYTES) -> str:
    text = str(value or "").strip()
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    encoded = text.encode("utf-8")[: max(0, max_bytes - 1)]
    return encoded.decode("utf-8", errors="ignore").rstrip() + "…"


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


__all__ = [
    "SourceReader",
    "WikiMediaEvidencePack",
    "WikiMediaRelationEvidence",
    "WikiMediaSourceEvidence",
    "build_media_evidence_pack",
]
