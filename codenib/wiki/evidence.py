# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Grounding primitives for generated repository Wiki pages."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable, List, Sequence

FACT_CLAIM_ROLES = frozenset(
    {
        "purpose",
        "entry",
        "flow",
        "responsibility",
        "contract",
        "component",
    }
)
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
    r"\b(accurate(?:ly)?|adapt(?:s|ing)?|adaptable|advanced|aids?|better|"
    r"allows(?: for| the system| developers| users)|"
    r"allowing (?:for|developers|users)|allow(?:s|ed|ing)?|"
    r"comprehensive|crucial|dynamic(?:ally)?|"
    r"cater(?:s|ing)?|easy access|easy to use|easier|effectively|"
    r"efficient|efficiently|fast|"
    r"enabl(?:e|es|ing)(?: developers| users)?|"
    r"enhanc(?:e|es|ing)(?: productivity)?|"
    r"ensur(?:e|es|ing) (?:that )?(?:all relevant|everything|resources?)|"
    r"ensur(?:e|es|ing)|"
    r"essential|flexible|"
    r"facilitat(?:e|es|ing)|for clarity|gain insights?|"
    r"help(?:s|ed|ing)? (?:developers|users)|improv(?:e|es|ing)|"
    r"intuitive|invaluable|"
    r"important (?:for|to)|key functionalit(?:y|ies)|making it|"
    r"powerful|provid(?:e|es|ing) (?:easy|quick)|quickly|responsive|significantly|"
    r"supports? (?:interactions?|management)|vital|"
    r"optimiz(?:e|es|ing)|sophisticated|user-friendly|versatile)\b",
    re.IGNORECASE,
)
_FLOW_RE = re.compile(
    r"\b(call(?:s|ed|ing)?|delegat(?:e|es|ed|ing)|dispatch(?:es|ed|ing)?|"
    r"feed(?:s|ing)?|hand(?:s|ed)?\s+(?:off|to)|"
    r"interact(?:s|ed|ing)?\s+with|invok(?:e|es|ed|ing)|"
    r"load(?:s|ed|ing)?\s+.+\s+from|pass(?:es|ed|ing)?\s+.+\s+to|"
    r"publish(?:es|ed)?|read(?:s)?\s+.+\s+from|"
    r"retriev(?:e|es|ed|ing)\s+.+\s+from|rout(?:e|es|ed|ing)|"
    r"send(?:s|ing)?\s+.+\s+to|us(?:e|es|ed|ing)|"
    r"utiliz(?:e|es|ed|ing)|"
    r"writ(?:e|es|ten|ing)\s+.+\s+to)\b",
    re.IGNORECASE,
)
_RETURN_FLOW_RE = re.compile(
    r"\breturn(?:s|ed|ing)?\s+.+?\s+to\s+`[^`\n]+`",
    re.IGNORECASE,
)
_ENTRY_RE = re.compile(
    r"\b(command|entry\s*point|endpoint|public|request|users?)\b",
    re.IGNORECASE,
)
_EXPLICIT_ENTRY_RE = re.compile(
    r"\b(?:"
    r"entry\s*point|public\s+(?:api|callable|command|endpoint|function|method)|"
    r"users?\s+(?:call|execute|invoke|run)"
    r")\b",
    re.IGNORECASE,
)
_PRIVATE_IDENTIFIER_RE = re.compile(
    r"`(?:[^`\n]*[:.])?_[A-Za-z]\w*(?:\([^`\n]*\))?`",
)
_RESPONSIBILITY_RE = re.compile(
    r"\b(build(?:s)?|compile(?:s)?|coordinate(?:s)?|manage(?:s)?|"
    r"own(?:s)?|persist(?:s)?|responsib(?:le|ility)|serve(?:s)?|"
    r"rout(?:e|es|ing)|store(?:s)?|validat(?:e|es))\b",
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


def _endpoint_symbol(endpoint: str) -> str:
    symbol = (endpoint or "").rsplit(":", 1)[-1].strip()
    return re.sub(r"\([^)]*\)$", "", symbol).strip().lower()


def relation_matches_claim(statement: str, relation: RelationItem) -> bool:
    """Whether a claim names both endpoints of a cited static relation."""

    identifiers = {
        re.sub(r"\([^)]*\)$", "", item.strip()).lower()
        for item in _CODE_RE.findall(statement or "")
    }

    def named(endpoint: str) -> bool:
        symbol = _endpoint_symbol(endpoint)
        if not symbol:
            return False
        return any(
            identifier == symbol or identifier.endswith(":" + symbol)
            for identifier in identifiers
        )

    return named(relation.source) and named(relation.target)


def evidence_matches_claim(statement: str, evidence: EvidenceItem) -> bool:
    """Whether one cited source body contains every named claim endpoint."""

    identifiers = {
        re.sub(r"\([^)]*\)$", "", item.strip()).lower()
        for item in _CODE_RE.findall(statement or "")
    }
    if len(identifiers) < 2:
        return False
    corpus = "\n".join((evidence.file, evidence.symbol, evidence.content)).lower()

    def present(identifier: str) -> bool:
        if identifier in corpus:
            return True
        if "/" in identifier or re.search(
            r"\.(?:py|go|rs|ts|tsx|js|jsx|c|h|cc|cpp|java|rb|php|cs|kt|kts)$",
            identifier,
        ):
            return False
        symbol = identifier.rsplit(":", 1)[-1]
        leaf = symbol.rsplit(".", 1)[-1]
        return bool(
            len(leaf) >= 3 and re.search(rf"(?<!\w){re.escape(leaf)}(?!\w)", corpus)
        )

    return all(present(identifier) for identifier in identifiers)


def is_interaction_claim(statement: str) -> bool:
    """Whether a claim explicitly describes a component-to-component handoff."""

    identifiers = re.findall(r"`([^`\n]+)`", statement or "")
    return bool(
        len(set(identifiers)) >= 2
        and (
            _FLOW_RE.search(statement or "") or _RETURN_FLOW_RE.search(statement or "")
        )
    )


def infer_claim_role(statement: str) -> str:
    """Infer a conservative role for plans from older prompts."""

    text = statement or ""
    if is_interaction_claim(text):
        return "flow"
    if _ENTRY_RE.search(text):
        return "entry"
    if _RESPONSIBILITY_RE.search(text):
        return "responsibility"
    return "component"


def describes_private_entry(statement: str, *, role: str = "") -> bool:
    """Whether prose presents a private identifier as a public/user entry."""

    text = statement or ""
    return bool(
        _PRIVATE_IDENTIFIER_RE.search(text)
        and (role == "entry" or _EXPLICIT_ENTRY_RE.search(text))
    )


def promotional_phrases(text: str) -> List[str]:
    """Return deterministic marketing or unsupported benefit language."""

    return sorted(
        {match.group(0).lower() for match in _PROMOTIONAL_RE.finditer(text or "")}
    )


def _supported_evidence_ids(
    raw: Any,
    allowed: set[str],
    *,
    label: str,
    errors: List[str],
) -> list[str]:
    requested = [str(item) for item in raw or []]
    unknown = [item for item in requested if item not in allowed]
    if unknown:
        errors.append(f"{label} references unknown evidence: {', '.join(unknown)}")
    return [item for item in requested if item in allowed]


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
        return {
            "thesis": {"statement": "", "evidence": []},
            "sections": [],
        }, ["plan is not a JSON object"]
    try:
        raw = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return {
            "thesis": {"statement": "", "evidence": []},
            "sections": [],
        }, [f"invalid plan JSON: {exc}"]

    raw_thesis = raw.get("thesis")
    if isinstance(raw_thesis, dict):
        thesis_statement = str(raw_thesis.get("statement") or "").strip()
        thesis_evidence = _supported_evidence_ids(
            raw_thesis.get("evidence"),
            allowed,
            label="thesis",
            errors=errors,
        )
    else:
        thesis_statement = str(raw_thesis or "").strip()
        thesis_evidence = []

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
            evidence = _supported_evidence_ids(
                claim.get("evidence"),
                allowed,
                label="claim",
                errors=errors,
            )
            if statement and evidence:
                role = str(claim.get("role") or "").strip().lower()
                if role not in FACT_CLAIM_ROLES:
                    role = infer_claim_role(statement)
                elif role == "flow" and not is_interaction_claim(statement):
                    role = infer_claim_role(statement)
                claims.append(
                    {
                        "role": role,
                        "statement": statement,
                        "evidence": evidence,
                    }
                )
        if title and claims:
            sections.append({"title": title, "claims": claims})
    if not sections:
        errors.append("plan has no supported sections")
    return {
        "thesis": {
            "statement": thesis_statement,
            "evidence": thesis_evidence,
        },
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
    promotional = promotional_phrases(without_fences)
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
        "promotional_phrases": promotional,
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
    "describes_private_entry",
    "diversify_by_file",
    "evidence_metadata",
    "evidence_matches_claim",
    "grounding_report",
    "parse_fact_plan",
    "promotional_phrases",
    "relation_matches_claim",
    "reciprocal_rank_fuse",
    "remove_promotional_sentences",
]
