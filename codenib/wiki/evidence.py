# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Grounding primitives for generated repository Wiki pages."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable, List, Sequence

_CITATION_RE = re.compile(r"\[((?:E|R)\d+)\]")
_CITATION_TAIL_RE = re.compile(
    r"(?:\s*\[(?:E|R)\d+\])+\s*[.!?]?\s*$",
)
_CODE_RE = re.compile(r"`([^`\n]+)`")
_PATH_RE = re.compile(
    r"(?<![\w/])([\w./-]+\.(?:py|pyi|go|rs|ts|tsx|js|jsx|c|h|cc|cpp|hpp|"
    r"java|rb|php|cs|kt|kts|swift|scala))(?![\w/])",
    re.IGNORECASE,
)
_COMMON_CODE_TERMS = frozenset(
    {
        "api",
        "async",
        "await",
        "bool",
        "class",
        "dict",
        "false",
        "float",
        "function",
        "int",
        "list",
        "main",
        "method",
        "none",
        "null",
        "object",
        "self",
        "str",
        "string",
        "true",
        "void",
    }
)
_PROMOTIONAL_RE = re.compile(
    r"\b(adapt(?:s|ing)?|adaptable|advanced|aids?|"
    r"allows(?: for| the system| developers| users)|"
    r"allowing (?:for|developers|users)|comprehensive|crucial|dynamic(?:ally)?|"
    r"cater(?:s|ing)?|easy access|easy to use|easier|effectively|"
    r"efficient|efficiently|"
    r"enabl(?:e|es|ing)(?: developers| users)?|"
    r"enhanc(?:e|es|ing)(?: productivity)?|"
    r"ensur(?:e|es|ing) (?:that )?(?:all relevant|everything|resources?)|"
    r"essential|flexible|"
    r"facilitat(?:e|es|ing)|gain insights?|helps? users|intuitive|invaluable|"
    r"important (?:for|to)|key functionalit(?:y|ies)|making it|"
    r"powerful|provid(?:e|es|ing) (?:easy|quick)|quickly|responsive|significantly|"
    r"supports? (?:interactions?|management)|"
    r"optimiz(?:e|es|ing)|sophisticated|user-friendly|versatile)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One source-backed symbol supplied to page planning and writing."""

    id: str
    file: str
    start_line: int | None
    end_line: int | None
    symbol: str
    kind: str
    content: str
    routes: tuple[str, ...] = ()

    def prompt_block(self) -> str:
        line = ""
        if self.start_line is not None:
            line = f":{self.start_line}"
            if self.end_line is not None and self.end_line != self.start_line:
                line += f"-{self.end_line}"
        routes = ", ".join(self.routes) if self.routes else "repository index"
        return (
            f"### [{self.id}] `{self.symbol or self.file}`\n"
            f"Source: `{self.file}{line}` | kind: {self.kind or 'code'} | "
            f"routes: {routes}\n"
            f"```\n{self.content}\n```"
        )


@dataclass(frozen=True, slots=True)
class RelationItem:
    """One static reference relation among page evidence symbols."""

    id: str
    source: str
    target: str
    anchors: tuple[str, ...] = ()

    def prompt_line(self) -> str:
        anchors = f" at {', '.join(self.anchors)}" if self.anchors else ""
        return f"- [{self.id}] `{self.source}` references `{self.target}`{anchors}"


def candidate_key(node: Any, get: Callable[[Any, str, Any], Any]) -> tuple:
    """Stable source identity for a retrieval candidate."""

    return (
        get(node, "file", "") or "",
        get(node, "start_line", None),
        get(node, "end_line", None),
        get(node, "node_name", "") or get(node, "name", "") or "",
    )


def reciprocal_rank_fuse(
    routes: Sequence[tuple[str, Sequence[Any]]],
    *,
    key: Callable[[Any], tuple],
    rank_constant: int = 60,
) -> List[tuple[Any, tuple[str, ...], float]]:
    """Fuse independently ranked routes while preserving provenance."""

    by_key: dict[tuple, dict[str, Any]] = {}
    for route, nodes in routes:
        for rank, node in enumerate(nodes, start=1):
            item = by_key.setdefault(
                key(node),
                {"node": node, "routes": [], "score": 0.0, "first": rank},
            )
            if route not in item["routes"]:
                item["routes"].append(route)
                item["score"] += 1.0 / (rank_constant + rank)
            item["first"] = min(item["first"], rank)
    ordered = sorted(
        by_key.values(),
        key=lambda item: (-item["score"], item["first"]),
    )
    return [
        (item["node"], tuple(item["routes"]), float(item["score"])) for item in ordered
    ]


def diversify_by_file(
    ranked: Sequence[tuple[Any, tuple[str, ...], float]],
    *,
    file_of: Callable[[Any], str],
    limit: int,
    max_per_file: int = 2,
) -> List[tuple[Any, tuple[str, ...], float]]:
    """Bound one file's ability to crowd every other subsystem out."""

    selected: List[tuple[Any, tuple[str, ...], float]] = []
    deferred: List[tuple[Any, tuple[str, ...], float]] = []
    counts: dict[str, int] = {}
    for item in ranked:
        file = file_of(item[0])
        if counts.get(file, 0) >= max_per_file:
            deferred.append(item)
            continue
        selected.append(item)
        counts[file] = counts.get(file, 0) + 1
        if len(selected) >= limit:
            return selected
    selected.extend(deferred[: max(0, limit - len(selected))])
    return selected


def parse_fact_plan(
    text: str,
    allowed_ids: Iterable[str],
) -> tuple[dict[str, Any], List[str]]:
    """Parse and structurally validate a model-authored page plan."""

    allowed = set(allowed_ids)
    errors: List[str] = []
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", cleaned).strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return {"thesis": "", "sections": []}, ["plan is not a JSON object"]
    try:
        raw = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return {"thesis": "", "sections": []}, [f"invalid plan JSON: {exc}"]

    sections = []
    for section in raw.get("sections") or []:
        if not isinstance(section, dict):
            continue
        title = str(section.get("title") or "").strip()
        claims = []
        for claim in section.get("claims") or []:
            if not isinstance(claim, dict):
                continue
            statement = str(claim.get("statement") or "").strip()
            evidence = [
                str(item)
                for item in claim.get("evidence") or []
                if str(item) in allowed
            ]
            unknown = [
                str(item)
                for item in claim.get("evidence") or []
                if str(item) not in allowed
            ]
            if unknown:
                errors.append(
                    f"claim references unknown evidence: {', '.join(unknown)}"
                )
            if statement and evidence:
                claims.append({"statement": statement, "evidence": evidence})
        if title and claims:
            sections.append({"title": title, "claims": claims})
    if not sections:
        errors.append("plan has no supported sections")
    return {
        "thesis": str(raw.get("thesis") or "").strip(),
        "sections": sections,
    }, errors


def grounding_report(
    markdown: str,
    evidence: Sequence[EvidenceItem],
    relations: Sequence[RelationItem],
) -> dict[str, Any]:
    """Check citation coverage and source-backed identifiers before publish."""

    allowed_ids = {item.id for item in evidence} | {item.id for item in relations}
    cited = set(_CITATION_RE.findall(markdown))
    unknown_citations = sorted(cited - allowed_ids)

    without_fences = re.sub(r"```[\s\S]*?```", "", markdown)
    blocks = []
    for raw in re.split(r"\n\s*\n", without_fences):
        block = raw.strip()
        if not block or block.startswith("#"):
            continue
        plain = re.sub(r"^[-*]\s+", "", block, flags=re.MULTILINE)
        if len(re.sub(r"[`*_[\]()#>-]", "", plain).strip()) >= 40:
            blocks.append(block)
    cited_blocks = sum(1 for block in blocks if _CITATION_TAIL_RE.search(block))
    coverage = cited_blocks / len(blocks) if blocks else 0.0

    corpus = "\n".join(
        [part for item in evidence for part in (item.file, item.symbol, item.content)]
        + [
            part
            for item in relations
            for part in (item.source, item.target, *item.anchors)
        ]
    ).lower()
    unsupported_identifiers = []
    for identifier in _CODE_RE.findall(without_fences):
        normalized = identifier.strip()
        source_name = re.sub(r":\d+(?:-\d+)?$", "", normalized)
        call_name_match = re.match(r"([A-Za-z_][\w.]*)\s*\(", source_name)
        call_name = call_name_match.group(1) if call_name_match else ""
        call_leaf = call_name.rsplit(".", 1)[-1]
        if (
            not normalized
            or normalized.lower() in _COMMON_CODE_TERMS
            or normalized.lower() in corpus
            or source_name.lower() in corpus
            or (call_name and call_name.lower() in corpus)
            or (call_leaf and call_leaf.lower() in corpus)
        ):
            continue
        unsupported_identifiers.append(normalized)

    known_files = {item.file.lower() for item in evidence}
    unknown_files = sorted(
        {
            path
            for path in _PATH_RE.findall(without_fences)
            if path.lower().lstrip("./") not in known_files
            and path.lower() not in corpus
            and not any(file.endswith("/" + path.lower()) for file in known_files)
        }
    )
    unsupported_identifiers = sorted(set(unsupported_identifiers))
    promotional_phrases = sorted(
        {match.group(0).lower() for match in _PROMOTIONAL_RE.finditer(without_fences)}
    )
    valid = (
        bool(blocks)
        and coverage == 1.0
        and not unknown_citations
        and not unknown_files
        and not unsupported_identifiers
    )
    return {
        "valid": valid,
        "citation_coverage": round(coverage, 3),
        "cited_evidence": len(cited & allowed_ids),
        "evidence_count": len(evidence),
        "relation_count": len(relations),
        "unknown_citations": unknown_citations,
        "unknown_files": unknown_files,
        "unsupported_identifiers": unsupported_identifiers,
        "promotional_phrases": promotional_phrases,
    }


def remove_promotional_sentences(markdown: str) -> str:
    """Remove benefit claims while retaining citations for factual sentences."""

    cleaned_blocks = []
    for block in re.split(r"\n\s*\n", markdown.strip()):
        stripped = block.strip()
        if not stripped or stripped.startswith("#"):
            cleaned_blocks.append(stripped)
            continue
        sentences = re.split(r"(?<=[.!?])\s+", stripped)
        kept = [
            sentence for sentence in sentences if not _PROMOTIONAL_RE.search(sentence)
        ]
        if not kept:
            continue
        rebuilt = " ".join(kept).strip()
        citations = list(dict.fromkeys(_CITATION_RE.findall(stripped)))
        present = set(_CITATION_RE.findall(rebuilt))
        missing = [citation for citation in citations if citation not in present]
        if missing:
            rebuilt += " " + "".join(f"[{citation}]" for citation in missing)
        cleaned_blocks.append(rebuilt)
    return "\n\n".join(block for block in cleaned_blocks if block)


def evidence_metadata(
    evidence: Sequence[EvidenceItem],
    relations: Sequence[RelationItem],
) -> dict[str, Any]:
    return {
        "items": [
            {key: value for key, value in asdict(item).items() if key != "content"}
            for item in evidence
        ],
        "relations": [asdict(item) for item in relations],
    }


__all__ = [
    "EvidenceItem",
    "RelationItem",
    "candidate_key",
    "diversify_by_file",
    "evidence_metadata",
    "grounding_report",
    "parse_fact_plan",
    "reciprocal_rank_fuse",
    "remove_promotional_sentences",
]
