# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Agent wiki pipeline: a DeepWiki-style, high-level conceptual wiki.

Stage 1 (:mod:`outline`) proposes a conceptual page tree. This module is stage
2: for each page it fuses the available retrieval views, builds source and
relationship evidence, asks an LLM for a fact plan, and then writes a page
whose citations are checked before publication. Outline + pages are disk-cached
so generation can resume one page at a time.

Drop-in for the demo's ``WikiBuilder``: exposes ``page_tree`` / ``page`` /
``source`` so ``codenib/web/app.py`` can serve it unchanged.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import html
import json
import os
import re
from typing import Any, Dict, List, Optional, Sequence

from ..log_utils import get_logger
from ..repository_summary import readme_summary
from .builder import WikiBuilder
from .evidence import (
    EvidenceItem,
    RelationItem,
    candidate_key,
    describes_private_entry,
    diversify_by_file,
    evidence_matches_claim,
    evidence_metadata,
    grounding_report,
    infer_claim_role,
    is_interaction_claim,
    parse_fact_plan,
    promotional_phrases,
    reciprocal_rank_fuse,
    relation_endpoints_named,
    relation_matches_claim,
    remove_promotional_sentences,
)
from .outline import (
    _is_supporting_file,
    _overview_file_score,
    _page_allows_supporting_files,
    generate_outline,
)
from .quality import duplicate_prose_blocks as _duplicate_prose_blocks
from .quality import leading_code_subject as _leading_code_subject
from .quality import narrative_density_report as _narrative_density_report
from .quality import page_quality_report as _page_quality_report
from .quality import prose_terms as _prose_terms
from .quality import redundancy_terms as _redundancy_terms
from .quality import section_synthesis_report as _section_synthesis_report

logger = get_logger(__name__)

_EXT_LANG = {
    "py": "python",
    "go": "go",
    "rs": "rust",
    "ts": "typescript",
    "tsx": "typescript",
    "js": "javascript",
    "jsx": "javascript",
    "c": "c",
    "h": "cpp",
    "cpp": "cpp",
    "cs": "csharp",
    "java": "java",
    "rb": "ruby",
    "php": "php",
    "kt": "kotlin",
    "kts": "kotlin",
}
_MAX_CONTEXT_CHARS = 14000
# A quoted body should carry an idea, not reproduce a file.
_MAX_EXCERPT_LINES = 14
_OUTLINE_PROMPT_VERSION = "12"
_PAGE_PROMPT_VERSION = "102"
_MAX_PLAN_REPAIRS = 3
_MAX_STYLE_REPAIRS = 2
# The overview is asked for a section per outline area, and a repository
# outline runs to 9-17 of them. Twelve symbols could not ground that many,
# so the coverage guard fired on pages that simply had nothing to say about
# an area -- feed it enough to answer instead of relaxing what it asks.
_OVERVIEW_RETRIEVAL_LIMIT = 24
_SOFT_PLAN_WARNING_PREFIXES = ("page thesis must contain exactly one sentence",)
# Plan diagnostics come in three tiers. Soft ones are noise. *Composition* ones
# say the page could be organised better -- a section leans on helpers, an area
# has no section of its own -- but every sentence still says something real and
# cites it. Only the third tier, an unsupported or misattributed fact, means the
# page must not be published as written.
#
# Composition notes still steer the repair loop; they just no longer decide
# publishability. They used to, and a richer page -- more sections, so more
# surface for them to fire on -- was marked degraded even with a clean grounding
# report, which made the flag fire on everything and mean nothing.
_COMPOSITION_PLAN_WARNING_MARKERS = (
    "elevates incidental helpers",
    "is not an explicit component handoff",
    "no supported component handoff",
    "dominated by isolated",
    "reads as a callable catalog",
    "grounded in its allocated evidence",
    "must use an allocated relation",
    "needs a section-level",
    "substantially repeat an admitted fact",
    "needs one publishable, non-redundant claim",
    "needs at least two supported facts",
    "names a source file as a subsystem",
)
_NARRATIVE_CALLABLE_KINDS = frozenset({"function", "method"})
_NARRATIVE_TYPE_KINDS = frozenset(
    {
        "class",
        "enum",
        "impl",
        "interface",
        "object",
        "struct",
        "trait",
    }
)
_GENERIC_PAGE_TERMS = frozenset(
    {
        "code",
        "component",
        "components",
        "data",
        "process",
        "processing",
        "representation",
        "source",
        "structure",
        "symbol",
        "symbols",
        "system",
    }
)
_OVERVIEW_SYMBOL_ROLE_WEIGHTS = {
    "main": 16,
    "run": 15,
    "serve": 14,
    "page": 14,
    "start": 13,
    "dispatch": 12,
    "query": 12,
    "request": 12,
    "resolve": 12,
    "route": 12,
    "search": 12,
    "handle": 11,
    "register": 11,
    "index": 10,
    "compile": 9,
    "generate": 12,
    "invoke": 12,
    "build": 8,
    "create": 7,
    "load": 7,
}


def _narrative_relation_endpoint(
    node: Dict[str, Any],
    *,
    allow_private: bool = False,
) -> bool:
    """Reject graph nodes whose inferred kind overstates a field or constant."""

    kind = str(node.get("kind") or "").lower()
    label = str(node.get("label") or node.get("short") or "")
    symbol = label.rsplit(":", 1)[-1].strip()
    leaf = symbol.removesuffix("()").rsplit(".", 1)[-1]
    if not allow_private and leaf.startswith("_") and not leaf.startswith("__"):
        return False
    if kind in _NARRATIVE_CALLABLE_KINDS:
        return symbol.endswith(")")
    if kind in _NARRATIVE_TYPE_KINDS:
        return bool(symbol and not re.fullmatch(r"[A-Z][A-Z0-9_]*", symbol))
    return False


def _overview_symbol_role_score(name: str) -> int:
    terminal = re.split(r"[:.]", name or "")[-1].split("(", 1)[0]
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", terminal).lower()
    tokens = re.findall(r"[a-z0-9]+", separated)
    normalized_tokens = list(tokens)
    for token in tokens:
        for suffix in ("ers", "ing", "ed", "er", "s"):
            if token.endswith(suffix) and len(token) - len(suffix) >= 4:
                normalized_tokens.append(token[: -len(suffix)])
                break
    return max(
        (_OVERVIEW_SYMBOL_ROLE_WEIGHTS.get(token, 0) for token in normalized_tokens),
        default=0,
    )


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "page"


def _lang(file: str) -> str:
    return _EXT_LANG.get((file or "").rsplit(".", 1)[-1].lower(), "")


def _canonical_symbol(value: str) -> str:
    """Identity of a symbol across retrieval routes.

    The same definition reaches evidence under different spellings depending on
    which index found it -- a sparse hit names it ``path/to/file.ts:thing()``
    while a dense hit names it ``thing``. Strip the path qualifier and the
    argument list so the two collapse onto one identity.
    """

    name = (value or "").strip().rsplit(":", 1)[-1].strip()
    return re.sub(r"\([^)]*\)$", "", name).strip().casefold()


def _link_evidence_markers(markdown: str) -> str:
    """Turn plain evidence markers into links to the page evidence ledger."""

    return re.sub(
        r"\[((?:E|R)\d+)\](?!\()",
        lambda match: f"[{match.group(1)}](#evidence-{match.group(1)})",
        markdown,
    )


def _clean_markdown(markdown: str) -> str:
    """Remove a model-added outer Markdown fence without touching code blocks."""

    cleaned = (markdown or "").strip()
    match = re.fullmatch(r"```(?:markdown|md)?\s*\n([\s\S]*?)\n```", cleaned)
    return match.group(1).strip() if match else cleaned


def _prepare_evidence_content(file: str, content: str, limit: int = 1800) -> str:
    """Remove README chrome and truncate evidence at a natural boundary."""

    text = (content or "").strip()
    if os.path.basename(file).lower().startswith("readme"):
        text = re.sub(r"<!--[\s\S]*?-->", "", text)
        closing_div = re.search(r"</div\s*>", text, flags=re.IGNORECASE)
        if closing_div is not None:
            body = text[closing_div.end() :].strip()
            if len(body) >= 80:
                text = body
        text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
        text = re.sub(r"<img\b[^>]*>", "", text, flags=re.IGNORECASE)
        text = re.sub(r"</?[^>]+>", "\n", text)
        text = html.unescape(text)
        text = re.sub(r"(?m)^\s*(?:[·|]|\s)+\s*$", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()

    if len(text) <= limit:
        return text
    prefix = text[:limit]
    minimum = int(limit * 0.6)
    boundaries = [
        prefix.rfind("\n\n"),
        max(
            (match.end() for match in re.finditer(r"[.!?](?=\s|$)", prefix)),
            default=-1,
        ),
        prefix.rfind("\n"),
    ]
    boundary = max((value for value in boundaries if value >= minimum), default=-1)
    return prefix[:boundary].rstrip() if boundary >= 0 else prefix.rstrip()


def _prune_uncited_blocks(markdown: str) -> str:
    """Drop unsupported prose blocks after a model repair attempt."""

    kept = []
    for block in re.split(r"\n\s*\n", markdown.strip()):
        stripped = block.strip()
        if (
            not stripped
            or stripped.startswith("#")
            # A fenced block is a diagram or a code excerpt; it states its
            # sources in the caption that follows, not inside the fence.
            or stripped.startswith("```")
            # A link list points at other pages of this wiki, which are named
            # rather than cited.
            or all(
                line.lstrip().startswith(("-", "*"))
                for line in stripped.splitlines()
                if line.strip()
            )
            or re.search(r"\[((?:E|R)\d+)\]", stripped)
            or len(re.sub(r"[`*_[\]()#>-]", "", stripped).strip()) < 40
        ):
            kept.append(stripped)
    return "\n\n".join(block for block in kept if block)


def _remove_orphan_headings(markdown: str) -> str:
    """Remove headings whose section contains no surviving prose."""

    blocks = [
        block.strip()
        for block in re.split(r"\n\s*\n", markdown.strip())
        if block.strip()
    ]
    heading_levels = []
    for block in blocks:
        match = re.match(r"^(#{1,6})\s+\S", block)
        heading_levels.append(len(match.group(1)) if match else None)

    kept = []
    for index, block in enumerate(blocks):
        level = heading_levels[index]
        if level is None:
            kept.append(block)
            continue
        has_content = False
        for later_index in range(index + 1, len(blocks)):
            later_level = heading_levels[later_index]
            if later_level is not None and later_level <= level:
                break
            if later_level is None:
                has_content = True
                break
        if has_content:
            kept.append(block)
    return "\n\n".join(kept)


def _format_supported_literals(text: str) -> str:
    """Render quoted commands, paths, and identifiers as inline code."""

    literal_re = re.compile(r"(?P<quote>['\"])(?P<value>[^'\"\n]{1,160})(?P=quote)")
    context_terms = re.compile(
        r"\b(class|command|directory|endpoint|file|flag|function|index|method|"
        r"module|option|path|profile|route|symbol|type|variable|view)\b",
        re.IGNORECASE,
    )

    def replace(match: re.Match[str]) -> str:
        value = match.group("value").strip()
        if not value or "`" in value:
            return match.group(0)
        before = text[max(0, match.start() - 28) : match.start()]
        after = text[match.end() : match.end() + 20]
        structural = any(marker in value for marker in ("/", "\\", ".", "_", "://"))
        contextual = bool(context_terms.search(before) or context_terms.search(after))
        if structural or contextual:
            return f"`{value}`"
        return match.group(0)

    return literal_re.sub(replace, text)


_CLAIM_SUPPORT_STOPWORDS = frozenset(
    {
        "based",
        "build",
        "builder",
        "builders",
        "builds",
        "call",
        "calls",
        "class",
        "code",
        "component",
        "create",
        "creates",
        "function",
        "handle",
        "handles",
        "initialize",
        "initializes",
        "invoke",
        "invokes",
        "maintain",
        "maintains",
        "manage",
        "manages",
        "method",
        "object",
        "objects",
        "perform",
        "performs",
        "process",
        "processes",
        "provided",
        "repository",
        "responsible",
        "retrieve",
        "retrieves",
        "return",
        "returns",
        "source",
        "specified",
        "system",
        "use",
        "used",
        "uses",
        "using",
        "value",
        "values",
        "various",
    }
)


def _normalized_support_terms(text: str) -> set[str]:
    expanded = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text or "")
    expanded = expanded.replace("_", " ")
    terms = _prose_terms(expanded)
    normalized = set()
    for term in terms:
        if term in _CLAIM_SUPPORT_STOPWORDS:
            continue
        value = term
        if value.endswith("ies") and len(value) > 4:
            value = value[:-3] + "y"
        elif value.endswith(("sses", "xes", "zes", "ches", "shes")):
            value = value[:-2]
        elif value.endswith("ing") and len(value) >= 7:
            value = value[:-3]
        elif value.endswith("ed") and len(value) >= 6:
            value = value[:-2]
        elif value.endswith("s") and not value.endswith("ss") and len(value) >= 5:
            value = value[:-1]
        if (
            len(value) >= 4
            and len(value) >= 2
            and value[-1] == value[-2]
            and value[-1] not in "aeiou"
        ):
            value = value[:-1]
        if value.endswith("e") and len(value) >= 5:
            value = value[:-1]
        if value not in _CLAIM_SUPPORT_STOPWORDS:
            normalized.add(value)
    return normalized


def _claim_has_local_support(
    statement: str,
    evidence: Sequence[EvidenceItem],
) -> bool:
    """Require concrete prose terms to occur in the evidence it cites."""

    prose = re.sub(r"`[^`\n]+`", " ", statement or "")
    claim_terms = _normalized_support_terms(prose)
    if not claim_terms:
        return True
    source_terms = _normalized_support_terms(
        "\n".join(f"{item.symbol}\n{item.content}" for item in evidence)
    )
    required = min(2, len(claim_terms))
    return len(claim_terms & source_terms) >= required


def _is_evidence_provenance_claim(
    statement: str,
    evidence: Sequence[EvidenceItem],
) -> bool:
    """Accept fallback claims that restate exact EvidenceItem metadata."""

    match = re.fullmatch(
        r"\s*`(?P<symbol>[^`\n]+)`\s+is indexed from\s+"
        r"`(?P<file>[^`\n]+)`(?:\s+at lines\s+\d+(?:-\d+)?)?\.?\s*",
        statement or "",
        re.IGNORECASE,
    )
    if not match:
        return False
    symbol = match.group("symbol").strip()
    file = match.group("file").strip().replace("\\", "/").lstrip("./")
    return any(
        item.symbol == symbol and item.file.replace("\\", "/").lstrip("./") == file
        for item in evidence
    )


def _method_owner_generalizations(
    statement: str,
    evidence: Sequence[EvidenceItem],
) -> List[EvidenceItem]:
    """Return method items used to claim uncited owner-class behavior."""

    statement_identifiers = {
        re.sub(r"\([^)]*\)$", "", item.strip()).lower()
        for item in re.findall(r"`([^`\n]+)`", statement or "")
    }
    class_symbols = {
        re.sub(
            r"\([^)]*\)$",
            "",
            item.symbol.rsplit(":", 1)[-1],
        ).lower()
        for item in evidence
        if item.kind == "class"
    }
    generalizations = []
    for item in evidence:
        if item.kind != "method":
            continue
        method_symbol = re.sub(
            r"\([^)]*\)$",
            "",
            item.symbol.rsplit(":", 1)[-1],
        ).lower()
        if "." not in method_symbol:
            continue
        owner = method_symbol.rsplit(".", 1)[0]
        if (
            owner in statement_identifiers
            and method_symbol not in statement_identifiers
            and owner not in class_symbols
        ):
            generalizations.append(item)
    return generalizations


def _normalize_callable_command_labels(
    statement: str,
    evidence: Sequence[EvidenceItem],
) -> str:
    """Replace a model's CLI label with the callable kind recorded in source."""

    def replace(match: re.Match[str]) -> str:
        identifier = match.group("identifier")
        leaf = re.sub(
            r"\([^)]*\)$",
            "",
            identifier.rsplit(":", 1)[-1],
        ).rsplit(
            ".", 1
        )[-1]
        kinds = {
            item.kind.lower()
            for item in evidence
            if item.kind.lower() in {"class", "function", "method"}
            and re.sub(
                r"\([^)]*\)$",
                "",
                item.symbol.rsplit(":", 1)[-1],
            ).rsplit(
                ".", 1
            )[-1]
            == leaf
        }
        if not kinds:
            return match.group(0)
        label = next(iter(kinds)) if len(kinds) == 1 else "callable"
        return f"{label} `{identifier}`"

    return re.sub(
        r"\bcommand\s+`(?P<identifier>[^`\n]+)`",
        replace,
        statement,
        flags=re.IGNORECASE,
    )


def _supported_thesis(
    thesis: Any,
    evidence: Sequence[EvidenceItem],
    relations: Sequence[RelationItem],
) -> tuple[str, list[str]] | None:
    """Return a thesis only when its cited source supports the concrete claim."""

    if not isinstance(thesis, dict):
        return None
    statement = re.sub(r"\s+", " ", str(thesis.get("statement") or "")).strip()
    allowed = {item.id for item in evidence} | {item.id for item in relations}
    ids = [str(item) for item in thesis.get("evidence") or [] if str(item) in allowed]
    cited_evidence = [item for item in evidence if item.id in ids]
    cited_relations = [item for item in relations if item.id in ids]
    if not statement or not cited_evidence:
        return None
    statement = _normalize_callable_command_labels(statement, cited_evidence)
    if len(re.findall(r"[.!?](?=\s|$)", statement)) > 1:
        return None
    if not _claim_has_local_support(statement, cited_evidence):
        return None
    if _method_owner_generalizations(statement, cited_evidence):
        return None
    candidate = f"{statement.rstrip('.')}." + "".join(f" [{item}]" for item in ids)
    report = grounding_report(candidate, cited_evidence, cited_relations)
    if any(
        report[key]
        for key in (
            "unknown_citations",
            "unknown_files",
            "unsupported_identifiers",
            "promotional_phrases",
        )
    ):
        return None
    return statement, ids


def _hard_plan_warnings(warnings: Sequence[str]) -> List[str]:
    """Return diagnostics that represent unsupported or incomplete facts."""

    return [
        warning
        for warning in warnings
        if not warning.startswith(_SOFT_PLAN_WARNING_PREFIXES)
    ]


def _composition_plan_warnings(warnings: Sequence[str]) -> List[str]:
    """Diagnostics about how the page is organised, not whether it is true."""

    return [
        warning
        for warning in warnings
        if any(marker in warning for marker in _COMPOSITION_PLAN_WARNING_MARKERS)
    ]


def _blocking_plan_warnings(warnings: Sequence[str]) -> List[str]:
    """Diagnostics that must stop a page from being published as written."""

    composition = set(_composition_plan_warnings(warnings))
    return [
        warning
        for warning in _hard_plan_warnings(warnings)
        if warning not in composition
    ]


def _plan_repair_score(
    plan: dict[str, Any],
    warnings: Sequence[str],
) -> tuple[int, int, int, int, int, int, int]:
    """Rank admitted plans while preserving grounding warnings as hard priority."""

    sections = plan.get("sections") or []
    claims = [claim for section in sections for claim in section.get("claims") or []]
    roles = {
        str(claim.get("role") or infer_claim_role(str(claim.get("statement") or "")))
        for claim in claims
    }
    coverage_gaps = sum(
        (
            warning.startswith("Overview needs a ")
            and " section grounded in its allocated evidence" in warning
        )
        or warning.startswith("parent page needs a section-level ")
        for warning in warnings
    )
    return (
        int(not bool(sections)),
        coverage_gaps,
        len(_hard_plan_warnings(warnings)),
        len(warnings),
        -min(len(roles), 6),
        -min(len(sections), 5),
        -min(len(claims), 12),
    )


def _condense_relation_free_overview(
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Keep one admitted boundary fact per overview topic without relations."""

    condensed = copy.deepcopy(plan)
    role_priority = {
        "flow": 0,
        "contract": 1,
        "responsibility": 2,
        "purpose": 3,
        "entry": 4,
        "component": 5,
    }
    for section in condensed.get("sections") or []:
        claims = list(section.get("claims") or [])
        if len(claims) < 2:
            continue

        def rank(claim: dict[str, Any]) -> tuple[int, int, int, int, int]:
            statement = str(claim.get("statement") or "")
            density = _narrative_density_report(statement)
            role = str(claim.get("role") or infer_claim_role(statement))
            return (
                int(bool(density["catalog_sentence_count"])),
                int(not is_interaction_claim(statement)),
                role_priority.get(role, 6),
                -len(_redundancy_terms(statement)),
                -len(statement),
            )

        section["claims"] = [min(claims, key=rank)]
    return condensed


def _merge_fact_plans(
    base: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Merge complementary admitted facts without replacing prior progress."""

    merged = copy.deepcopy(base)
    if not merged.get("thesis") and candidate.get("thesis"):
        merged["thesis"] = copy.deepcopy(candidate["thesis"])
    # A repair pass may be the first attempt that produced usable framing or a
    # scan table; keep it rather than losing it to the earlier plan.
    for key in ("purpose", "map", "flow", "see_also"):
        if not merged.get(key) and candidate.get(key):
            merged[key] = copy.deepcopy(candidate[key])

    sections = merged.setdefault("sections", [])
    for incoming in candidate.get("sections") or []:
        incoming_title = re.sub(
            r"\s+",
            " ",
            str(incoming.get("title") or ""),
        ).strip()
        if not incoming_title:
            continue
        incoming_terms = _prose_terms(incoming_title)
        target = next(
            (
                section
                for section in sections
                if str(section.get("title") or "").strip().casefold()
                == incoming_title.casefold()
            ),
            None,
        )
        if target is None and incoming_terms:
            for section in sections:
                section_terms = _prose_terms(str(section.get("title") or ""))
                if (
                    section_terms
                    and len(section_terms & incoming_terms)
                    / min(len(section_terms), len(incoming_terms))
                    >= 0.6
                ):
                    target = section
                    break
        if target is None:
            if len(sections) >= 5:
                continue
            target = {"title": incoming_title, "claims": []}
            sections.append(target)

        claims = target.setdefault("claims", [])
        for claim in incoming.get("claims") or []:
            if len(claims) >= 3:
                break
            statement = re.sub(
                r"\s+",
                " ",
                str(claim.get("statement") or ""),
            ).strip()
            if not statement:
                continue
            role = str(claim.get("role") or infer_claim_role(statement))
            terms = _redundancy_terms(statement)
            duplicate = False
            for index, existing in enumerate(claims):
                existing_statement = re.sub(
                    r"\s+",
                    " ",
                    str(existing.get("statement") or ""),
                ).strip()
                existing_role = str(
                    existing.get("role") or infer_claim_role(existing_statement)
                )
                existing_terms = _redundancy_terms(existing_statement)
                semantic_duplicate = bool(
                    terms
                    and existing_terms
                    and len(terms & existing_terms)
                    / min(len(terms), len(existing_terms))
                    >= 0.8
                )
                if (
                    existing_statement.casefold() == statement.casefold()
                    or semantic_duplicate
                ):
                    duplicate = True
                    if existing_role == "component" and role != "component":
                        claims[index] = copy.deepcopy(claim)
                    break
            if not duplicate:
                claims.append(copy.deepcopy(claim))
    return merged


def _supplement_topic_relation_flows(
    meta: Dict[str, Any],
    plan: dict[str, Any],
    relations: Sequence[RelationItem],
    evidence: Sequence[EvidenceItem] = (),
) -> dict[str, Any]:
    """Align or materialize callable edges omitted by the model plan."""

    if not relations:
        return plan

    supplemented = copy.deepcopy(plan)
    sections = supplemented.setdefault("sections", [])

    def endpoint_symbol(endpoint: str) -> str:
        return endpoint.rsplit(":", 1)[-1].strip()

    def normalized_symbol(value: str) -> str:
        return re.sub(r"\([^)]*\)$", "", value.strip()).casefold()

    def canonical_relation_claim(
        statement: str,
        relation: RelationItem,
    ) -> Optional[str]:
        if relation_matches_claim(statement, relation):
            return statement
        if not is_interaction_claim(statement):
            return None

        identifiers = list(re.finditer(r"`([^`\n]+)`", statement))
        source = normalized_symbol(endpoint_symbol(relation.source))
        target = normalized_symbol(endpoint_symbol(relation.target))
        source_leaf = source.rsplit(".", 1)[-1]
        target_leaf = target.rsplit(".", 1)[-1]
        source_matches = [
            match
            for match in identifiers
            if normalized_symbol(match.group(1)) in {source, source_leaf}
        ]
        target_matches = [
            match
            for match in identifiers
            if normalized_symbol(match.group(1)) in {target, target_leaf}
        ]
        pair = next(
            (
                (source_match, target_match)
                for source_match in source_matches
                for target_match in target_matches
                if source_match.start() < target_match.start()
            ),
            None,
        )
        if pair is None:
            return None

        replacements = (
            (pair[0].start(1), pair[0].end(1), endpoint_symbol(relation.source)),
            (pair[1].start(1), pair[1].end(1), endpoint_symbol(relation.target)),
        )
        canonical = statement
        for start, end, replacement in sorted(replacements, reverse=True):
            canonical = canonical[:start] + replacement + canonical[end:]
        return canonical if relation_matches_claim(canonical, relation) else None

    for section in sections:
        for claim in section.get("claims") or []:
            statement = str(claim.get("statement") or "")
            matches = [
                (relation, canonical)
                for relation in relations
                if (canonical := canonical_relation_claim(statement, relation))
                is not None
            ]
            if len(matches) != 1:
                continue
            relation, canonical = matches[0]
            claim["statement"] = canonical
            claim["role"] = "flow"
            claim["evidence"] = list(
                dict.fromkeys(
                    [
                        *[str(item) for item in claim.get("evidence") or []],
                        relation.id,
                    ]
                )
            )

    if meta.get("id") != "overview":
        covered_relations = {
            str(item)
            for section in sections
            for claim in section.get("claims") or []
            if str(claim.get("role") or "") == "flow"
            for item in claim.get("evidence") or []
            if str(item).startswith("R")
        }
        if covered_relations:
            return supplemented

        evidence_by_id = {item.id: item for item in evidence}
        candidates = []
        for relation in relations:
            source = endpoint_symbol(relation.source)
            target = endpoint_symbol(relation.target)
            if not source.endswith("()") or not target.endswith("()"):
                continue
            source_file = AgentWiki._norm_hint_path(relation.source.split(":", 1)[0])
            target_file = AgentWiki._norm_hint_path(relation.target.split(":", 1)[0])
            for section_index, section in enumerate(sections):
                section_files = {
                    AgentWiki._norm_hint_path(evidence_by_id[item].file)
                    for claim in section.get("claims") or []
                    for item in claim.get("evidence") or []
                    if item in evidence_by_id
                }
                score = 2 * int(source_file in section_files) + int(
                    target_file in section_files
                )
                if score:
                    candidates.append((score, -section_index, relation, section))
        if not candidates:
            return supplemented
        _, _, relation, section = max(
            candidates,
            key=lambda item: (item[0], item[1], item[2].id),
        )
        claims = section.setdefault("claims", [])
        flow = {
            "role": "flow",
            "statement": (
                f"`{endpoint_symbol(relation.source)}` calls "
                f"`{endpoint_symbol(relation.target)}`"
            ),
            "evidence": [relation.id],
        }
        if len(claims) < 3:
            claims.append(flow)
        else:
            replaceable = next(
                (
                    index
                    for index, existing in enumerate(claims)
                    if str(existing.get("role") or "") == "component"
                ),
                None,
            )
            if replaceable is not None:
                claims[replaceable] = flow
        return supplemented

    for topic in meta.get("major_topics") or []:
        if not isinstance(topic, dict):
            continue
        topic_title = str(topic.get("title") or "").strip()
        title_terms = _prose_terms(topic_title)
        topic_files = {
            AgentWiki._norm_hint_path(str(file))
            for file in topic.get("files") or []
            if str(file).strip()
        }
        topic_relations = sorted(
            (
                item
                for item in relations
                if any(
                    AgentWiki._norm_hint_path(endpoint.split(":", 1)[0]) in topic_files
                    for endpoint in (item.source, item.target)
                )
                and item.source.rsplit(":", 1)[-1].endswith("()")
                and item.target.rsplit(":", 1)[-1].endswith("()")
            ),
            key=lambda item: (
                sum(
                    AgentWiki._norm_hint_path(endpoint.split(":", 1)[0]) in topic_files
                    for endpoint in (item.source, item.target)
                ),
                AgentWiki._norm_hint_path(item.source.split(":", 1)[0]) in topic_files,
            ),
            reverse=True,
        )
        section = next(
            (
                item
                for item in sections
                if title_terms
                and title_terms & _prose_terms(str(item.get("title") or ""))
            ),
            None,
        )
        if not topic_relations:
            continue
        created = section is None
        if created:
            if not topic_title or len(sections) >= 5:
                continue
            section = {"title": topic_title, "claims": []}
            sections.append(section)
        covered = {
            str(evidence_id)
            for claim in section.get("claims") or []
            for evidence_id in claim.get("evidence") or []
        }
        claims = section.setdefault("claims", [])
        has_topic_relation = any(item.id in covered for item in topic_relations)
        target_count = max(
            int(not has_topic_relation),
            max(0, 2 - len(claims)),
        )
        target_count = min(
            target_count,
            sum(item.id not in covered for item in topic_relations),
        )
        if target_count == 0:
            continue
        added = 0
        for relation in topic_relations:
            if relation.id in covered:
                continue
            source = relation.source.rsplit(":", 1)[-1]
            target = relation.target.rsplit(":", 1)[-1]
            claim = {
                "role": "flow",
                "statement": f"`{source}` calls `{target}`",
                "evidence": [relation.id],
            }
            if len(claims) < 3:
                claims.append(claim)
            else:
                replaceable = next(
                    (
                        index
                        for index, existing in enumerate(claims)
                        if str(existing.get("role") or "") == "component"
                    ),
                    None,
                )
                if replaceable is None:
                    break
                claims[replaceable] = claim
            covered.add(relation.id)
            added += 1
            if added >= target_count:
                break
    return supplemented


def _renderable_plan(
    plan: dict[str, Any],
    evidence: Sequence[EvidenceItem],
    relations: Sequence[RelationItem],
) -> dict[str, Any]:
    """Return the exact supported plan consumed by rendering and quality gates."""

    rendered = copy.deepcopy(plan)
    allowed = {item.id for item in evidence} | {item.id for item in relations}
    admitted_thesis = _supported_thesis(
        rendered.get("thesis"),
        evidence,
        relations,
    )
    if admitted_thesis is not None:
        thesis_statement, thesis_ids = admitted_thesis
        rendered["thesis"] = {
            "statement": thesis_statement,
            "evidence": thesis_ids,
        }
        intro = _format_supported_literals(thesis_statement).rstrip(".") + "."
    else:
        readme_intro = _readme_intro(list(evidence))
        intro = readme_intro[0] if readme_intro is not None else ""
    intro_terms = _prose_terms(intro)

    # Framing and the scan table are grounded like any claim: a statement or row
    # that cites nothing admissible is dropped rather than rendered unsupported.
    purpose = rendered.get("purpose") or {}
    purpose_ids = [
        str(item) for item in (purpose.get("evidence") or []) if str(item) in allowed
    ]
    purpose_statements = [
        sentence
        for sentence in (
            re.sub(r"\s+", " ", str(raw or "")).strip()
            for raw in (purpose.get("statements") or [])
        )
        if sentence
    ]
    if purpose_statements and purpose_ids:
        rendered["purpose"] = {
            "statements": purpose_statements[:3],
            "evidence": purpose_ids,
        }
    else:
        rendered.pop("purpose", None)

    rendered_map = []
    for row in rendered.get("map") or []:
        concern = re.sub(r"\s+", " ", str(row.get("concern") or "")).strip()
        entity = re.sub(r"\s+", " ", str(row.get("entity") or "")).strip()
        ids = [str(item) for item in row.get("evidence") or [] if str(item) in allowed]
        if not concern or not entity or not ids:
            continue
        rendered_map.append({"concern": concern, "entity": entity, "evidence": ids})
    # A two-row table is not worth the chrome; below that, prose carries it.
    if len(rendered_map) >= 3:
        rendered["map"] = rendered_map[:6]
    else:
        rendered.pop("map", None)

    # A diagram must name symbols the page actually has evidence for; anything
    # else is an invented sequence, which is worse than showing no diagram.
    flow = rendered.get("flow") or {}
    if flow:
        corpus = " ".join(
            [
                part
                for item in evidence
                for part in (item.file, item.symbol, item.content)
            ]
            + [part for item in relations for part in (item.source, item.target)]
        ).casefold()

        def endpoint_is_supported(endpoint: str) -> bool:
            name = endpoint.strip().strip("`").strip()
            name = re.sub(r":\d+(?:-\d+)?$", "", name)
            leaf = name.rsplit(":", 1)[-1].rsplit(".", 1)[-1]
            leaf = re.sub(r"\([^)]*\)$", "", leaf).strip()
            return bool(leaf) and leaf.casefold() in corpus

        steps = [
            step
            for step in flow.get("steps") or []
            if endpoint_is_supported(str(step.get("from") or ""))
            and endpoint_is_supported(str(step.get("to") or ""))
        ]
        # A lifecycle has to connect. Disconnected pairs are a relation list
        # drawn with arrows, which reads as noise rather than a path, so keep
        # only the largest connected component and drop the diagram if what
        # survives is no longer a path.
        if steps:
            parent: dict[str, str] = {}

            def find(node: str) -> str:
                parent.setdefault(node, node)
                while parent[node] != node:
                    parent[node] = parent[parent[node]]
                    node = parent[node]
                return node

            def union(a: str, b: str) -> None:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[ra] = rb

            def key(endpoint: str) -> str:
                return endpoint.strip().strip("`").strip().casefold()

            for step in steps:
                union(key(str(step.get("from"))), key(str(step.get("to"))))
            groups: dict[str, List[dict[str, Any]]] = {}
            for step in steps:
                groups.setdefault(find(key(str(step.get("from")))), []).append(step)
            steps = max(groups.values(), key=len)
        if len(steps) >= 2:
            rendered["flow"] = {**flow, "steps": steps}
        else:
            rendered.pop("flow", None)

    rendered_sections = []
    for section in rendered.get("sections") or []:
        rendered_claims = []
        for claim in section.get("claims") or []:
            statement = re.sub(
                r"\s+",
                " ",
                str(claim.get("statement") or ""),
            ).strip()
            ids = [
                str(item)
                for item in claim.get("evidence") or []
                if str(item) in allowed
            ]
            if not statement or not ids:
                continue
            cited_evidence = [item for item in evidence if item.id in ids]
            statement = _normalize_callable_command_labels(
                statement,
                cited_evidence,
            )
            role = str(claim.get("role") or infer_claim_role(statement))
            if (
                role != "flow"
                and cited_evidence
                and not _claim_has_local_support(statement, cited_evidence)
                and not _is_evidence_provenance_claim(statement, cited_evidence)
            ):
                continue
            statement_terms = _prose_terms(statement)
            if (
                intro_terms
                and len(statement_terms) >= 4
                and len(statement_terms & intro_terms) / len(statement_terms) >= 0.8
            ):
                continue
            candidate = (
                _format_supported_literals(statement).rstrip(".")
                + "."
                + "".join(f" [{item}]" for item in ids)
            )
            report = grounding_report(candidate, evidence, relations)
            if any(
                report[key]
                for key in (
                    "unknown_citations",
                    "unknown_files",
                    "unsupported_identifiers",
                    "promotional_phrases",
                )
            ):
                continue
            rendered_claim = copy.deepcopy(claim)
            rendered_claim["statement"] = statement
            rendered_claim["evidence"] = ids
            rendered_claim["role"] = role
            rendered_claims.append(rendered_claim)
        deduplicated_claims = []
        for claim in rendered_claims:
            statement = str(claim.get("statement") or "")
            subject = _leading_code_subject(statement)
            terms = _redundancy_terms(statement)
            duplicate_index = None
            for index, existing in enumerate(deduplicated_claims):
                existing_statement = str(existing.get("statement") or "")
                existing_subject = _leading_code_subject(existing_statement)
                existing_terms = _redundancy_terms(existing_statement)
                smaller = min(len(terms), len(existing_terms))
                contained = (
                    len(terms & existing_terms) / smaller if smaller >= 5 else 0.0
                )
                if subject and subject == existing_subject and contained >= 0.7:
                    duplicate_index = index
                    break
            if duplicate_index is None:
                deduplicated_claims.append(claim)
                continue
            existing = deduplicated_claims[duplicate_index]
            existing_terms = _redundancy_terms(str(existing.get("statement") or ""))
            role_priority = {
                "flow": 0,
                "contract": 1,
                "responsibility": 2,
                "entry": 3,
                "purpose": 4,
                "component": 5,
            }
            candidate_score = (
                len(terms),
                -role_priority.get(str(claim.get("role") or ""), 6),
            )
            existing_score = (
                len(existing_terms),
                -role_priority.get(str(existing.get("role") or ""), 6),
            )
            if candidate_score > existing_score:
                deduplicated_claims[duplicate_index] = claim
        if deduplicated_claims:
            rendered_section = copy.deepcopy(section)
            rendered_section["claims"] = deduplicated_claims
            rendered_sections.append(rendered_section)
    rendered["sections"] = rendered_sections
    return rendered


def _fact_plan_markdown(
    plan: dict[str, Any],
    evidence: List[EvidenceItem],
    relations: List[RelationItem],
) -> str:
    """Render an admitted fact plan as citation-stable Markdown."""

    plan = _renderable_plan(plan, evidence, relations)
    intro: tuple[str, list[str]] | None = None
    admitted_thesis = _supported_thesis(
        plan.get("thesis"),
        evidence,
        relations,
    )
    if admitted_thesis is not None:
        statement, ids = admitted_thesis
        intro = (_format_supported_literals(statement).rstrip(".") + ".", ids)
    if intro is None:
        readme_intro = _readme_intro(evidence)
        if readme_intro is not None:
            intro = (readme_intro[0], [readme_intro[1]])
    # Framing first, then the scan table, then the detail -- a reader who has
    # not seen the codebase needs to know what this area is for before being
    # handed symbol-level handoffs.
    purpose_block = ""
    purpose = plan.get("purpose") or {}
    purpose_statements = [str(item).strip() for item in purpose.get("statements") or []]
    purpose_statements = [item for item in purpose_statements if item]
    if purpose_statements:
        sentences = [
            _format_supported_literals(item).rstrip(".") + "."
            for item in purpose_statements
        ]
        purpose_block = " ".join(sentences)
        ids = [str(item) for item in purpose.get("evidence") or []]
        if ids:
            purpose_block += " " + " ".join(f"[{item}]" for item in ids)

    map_block = ""
    rows = plan.get("map") or []
    if rows:
        lines = ["| Capability | Implemented by | Source |", "|---|---|---|"]
        for row in rows:
            concern = _format_supported_literals(str(row.get("concern") or "")).strip()
            entity = _format_supported_literals(str(row.get("entity") or "")).strip()
            ids = " ".join(f"[{item}]" for item in row.get("evidence") or [])
            lines.append(f"| {concern} | {entity} | {ids} |")
        map_block = "\n".join(lines)

    # Mermaid node ids must be opaque; the labels carry the real names.
    flow_block = ""
    flow = plan.get("flow") or {}
    if flow.get("steps"):
        node_ids: dict[str, str] = {}

        def node_id(label: str) -> str:
            key = label.strip().strip("`").strip()
            if key not in node_ids:
                node_ids[key] = f"n{len(node_ids)}"
            return node_ids[key]

        lines = ["```mermaid", "flowchart LR"]
        edges = []
        for step in flow["steps"]:
            src = str(step.get("from") or "").strip().strip("`")
            tgt = str(step.get("to") or "").strip().strip("`")
            label = re.sub(r'["\n|]', " ", str(step.get("label") or "")).strip()
            sid, tid = node_id(src), node_id(tgt)
            edges.append((sid, tid, label))
        for name, ident in node_ids.items():
            safe = re.sub(r'["\n|]', " ", name)
            # Mermaid needs the label in literal double quotes, so build it
            # rather than let a repr choose the quoting.
            lines.append("  " + ident + '["' + safe + '"]')
        for sid, tid, label in edges:
            lines.append(
                f"  {sid} -->|{label}| {tid}" if label else f"  {sid} --> {tid}"
            )
        lines.append("```")
        caption_ids: List[str] = []
        for step in flow["steps"]:
            for item in step.get("evidence") or []:
                if item not in caption_ids:
                    caption_ids.append(str(item))
        caption = str(flow.get("title") or "").strip()
        if caption and caption_ids:
            # The fence is stripped before the prose checks run, so the caption
            # is where this block states its sources.
            lines.append("")
            lines.append(
                caption.rstrip(".")
                + ". "
                + " ".join(f"[{item}]" for item in caption_ids)
            )
        flow_block = "\n".join(lines)

    see_also_block = ""
    refs = plan.get("see_also") or []
    if refs:
        parts = []
        for ref in refs:
            page = str(ref.get("page") or "").strip()
            title = str(ref.get("title") or page).strip()
            why = str(ref.get("why") or "").strip()
            link = f"[{title}](?p={page})"
            parts.append(f"- {link}" + (f" — {why}" if why else ""))
        see_also_block = "\n".join(parts)

    evidence_by_id = {item.id: item for item in evidence}

    def excerpt_block(section: dict[str, Any]) -> str:
        """Render a cited body verbatim from the evidence we hold."""

        spec = section.get("excerpt") or {}
        item = evidence_by_id.get(str(spec.get("evidence") or ""))
        if item is None or not (item.content or "").strip():
            return ""
        lines = [
            line
            for line in item.content.splitlines()
            if line.strip() and not line.strip().startswith("…")
        ][:_MAX_EXCERPT_LINES]
        if len(lines) < 2:
            return ""
        language = _lang(item.file)
        span = ""
        if item.start_line is not None:
            span = f":{item.start_line}"
            if item.end_line is not None and item.end_line != item.start_line:
                span += f"-{item.end_line}"
        why = re.sub(r"\s+", " ", str(spec.get("why") or "")).strip()
        caption = f"`{item.file}{span}`" + (f" — {why.rstrip('.')}." if why else "")
        return "\n".join(
            [f"```{language}", *lines, "```", "", f"{caption} [{item.id}]"]
        )

    rendered_sections: List[tuple[str, List[str]]] = []
    for section in plan.get("sections") or []:
        sentences = []
        section_ids: list[str] = []
        for claim in section.get("claims") or []:
            statement = re.sub(r"\s+", " ", str(claim.get("statement") or "")).strip()
            ids = [str(item) for item in claim.get("evidence") or []]
            rendered_statement = _format_supported_literals(statement)
            rendered_claim = rendered_statement.rstrip(".") + "."
            sentences.append(rendered_claim)
            section_ids.extend(item for item in ids if item not in section_ids)
        title = re.sub(r"\s+", " ", str(section.get("title") or "")).strip()
        if not title or not sentences:
            continue
        parts: List[str] = []
        lead = section.get("lead") or {}
        lead_sentences = [
            _format_supported_literals(str(item).strip()).rstrip(".") + "."
            for item in lead.get("statements") or []
            if str(item or "").strip()
        ]
        lead_ids = [str(item) for item in lead.get("evidence") or []]
        if lead_sentences and lead_ids:
            parts.append(
                " ".join(lead_sentences)
                + " "
                + " ".join(f"[{item}]" for item in lead_ids)
            )
        paragraph = " ".join(sentences)
        if section_ids:
            paragraph += " " + " ".join(f"[{item}]" for item in section_ids)
        parts.append(paragraph)
        block = excerpt_block(section)
        if block:
            parts.append(block)
        rendered_sections.append((title, parts))

    if not rendered_sections:
        if intro is None:
            return ""
        return intro[0] + " " + " ".join(f"[{item}]" for item in intro[1])
    blocks = []
    if intro is not None:
        blocks.append(intro[0] + " " + " ".join(f"[{item}]" for item in intro[1]))
    if purpose_block:
        blocks.extend(("## Purpose and scope", purpose_block))
    if map_block:
        blocks.extend(("## At a glance", map_block))
    if flow_block:
        blocks.extend((f"## {flow.get('title') or 'How it fits together'}", flow_block))
    for title, parts in rendered_sections:
        blocks.append(f"## {title}")
        blocks.extend(parts)
    if see_also_block:
        blocks.extend(("## Related pages", see_also_block))
    return "\n\n".join(blocks)


def _candidate_score(
    report: dict[str, Any],
    quality: dict[str, Any],
) -> tuple[int, int, int, int, int, float]:
    """Rank publishable page candidates with grounding as the hard boundary."""

    return (
        int(bool(report.get("valid"))),
        int(bool(quality.get("valid"))),
        -len(report.get("promotional_phrases") or []),
        int(quality.get("rendered_sections") or 0),
        int(quality.get("substantive_blocks") or 0),
        float(quality.get("claim_coverage") or 0.0),
    )


def _readme_intro(evidence: List[EvidenceItem]) -> tuple[str, str] | None:
    """Return the first descriptive README paragraph and its evidence id."""

    for item in evidence:
        if not os.path.basename(item.file).lower().startswith("readme"):
            continue
        summary = readme_summary(item.content, limit=600)
        if summary:
            return summary, item.id
    return None


def _canonical_readme_sentence(text: str, repository_name: str = "") -> str:
    """Turn a README heading into a neutral, grammatical repository statement."""

    value = re.sub(r"\s+", " ", text).strip()
    if not value:
        return ""
    phrases = promotional_phrases(value)
    complete_clause = re.compile(
        r"\b(?:is|are|provides?|implements?|builds?|compiles?|serves?|"
        r"manages?|contains?|offers?|uses?)\b",
        re.IGNORECASE,
    )

    def is_complete_clause(candidate: str) -> bool:
        words = re.findall(r"[A-Za-z0-9][A-Za-z0-9+{}_.-]*", candidate)
        return bool(
            len(words) >= 4
            and complete_clause.search(candidate)
            and not re.search(
                r"\b(?:a|an|and|are|is|or|providing|offering|featuring|with)\s*$",
                candidate,
                re.IGNORECASE,
            )
        )

    if phrases and is_complete_clause(value):
        starts = [
            match.start()
            for phrase in phrases
            for match in [re.search(rf"\b{re.escape(phrase)}\b", value, re.IGNORECASE)]
            if match is not None
        ]
        if starts:
            prefix = value[: min(starts)].rstrip(" ,;:-")
            previous = None
            while prefix != previous:
                previous = prefix
                prefix = re.sub(
                    r"(?:\b(?:a|an|and|or|providing|offering|featuring|with)\b"
                    r"|[\s,;:-])+$",
                    "",
                    prefix,
                    flags=re.IGNORECASE,
                ).rstrip()
            if is_complete_clause(prefix):
                return prefix.rstrip(".!?") + "."
        return ""
    if re.search(r"[.!?]\s*$", value):
        return value
    if is_complete_clause(value):
        return value.rstrip(".!?") + "."

    for phrase in sorted(phrases, key=len, reverse=True):
        value = re.sub(rf"\b{re.escape(phrase)}\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"^\s*(?:and|or)\b\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(?:and|or)\s*$", "", value, flags=re.IGNORECASE)
    value = value.strip(" ,;:-")
    value = re.sub(r"^(?:a|an|the)\s+", "", value, flags=re.IGNORECASE)
    if not value:
        return ""

    words = []
    for token in value.split():
        bare = re.sub(r"[^A-Za-z0-9]+", "", token)
        if bare.isupper() or any(char.isupper() for char in bare[1:]):
            words.append(token)
        else:
            words.append(token.lower())
    phrase = " ".join(words)

    subject = repository_name.strip().rstrip("/").rsplit("/", 1)[-1]
    if not subject:
        return phrase[:1].upper() + phrase[1:] + "."
    first = re.sub(r"[^A-Za-z]+", "", words[0])
    vowel_sound_acronym = first.isupper() and first[:1] in "AEFHILMNORSX"
    article = "an" if first[:1].lower() in "aeiou" or vowel_sound_acronym else "a"
    return f"{subject} is {article} {phrase}."


def _ensure_cited_intro(
    markdown: str,
    evidence: List[EvidenceItem],
    *,
    canonical_readme: bool = False,
    repository_name: str = "",
) -> str:
    """Prepend the README synopsis when a model starts directly with a section."""

    first_section = re.search(r"^##\s+\S", markdown, flags=re.MULTILINE)
    intro = _readme_intro(evidence)
    if canonical_readme and intro is not None:
        text, evidence_id = intro
        canonical = _canonical_readme_sentence(text, repository_name)
        if canonical:
            sections = markdown[first_section.start() :] if first_section else ""
            return f"{canonical} [{evidence_id}]\n\n{sections.lstrip()}".rstrip()
        intro = None

    intro_body = markdown[: first_section.start()] if first_section else markdown
    intro_plain = re.sub(r"^#\s+.*$", "", intro_body, flags=re.MULTILINE)
    intro_plain = re.sub(r"\[(?:E|R)\d+\](?:\([^)]*\))?", " ", intro_plain)
    intro_plain = re.sub(r"[`*_[\]()#>-]", " ", intro_plain)
    if len(re.sub(r"\s+", " ", intro_plain).strip()) >= 40:
        return markdown
    if intro is None:
        return markdown
    text, evidence_id = intro
    return f"{text} [{evidence_id}]\n\n{markdown.lstrip()}"


_PLAN_PROMPT = """\
You are planning one source-grounded page of a developer wiki for the {repo}
codebase.

Page title: {title}
Outline topic hint (not source evidence; do not repeat it as a fact): {summary}
Planning guidance: {guidance}
Evidence allocation: {constraints}

Source evidence:
{evidence}

Static reference relations:
{relations}

Other pages of this wiki (do not restate what they own):
{wiki_pages}

Return ONLY JSON with this shape:
{{"thesis":{{"statement":"one concise supported thesis","evidence":["E1"]}},
"purpose":{{"statements":["what this area is responsible for",
"what a reader can do with it"],"evidence":["E1","E2"]}},
"map":[{{"concern":"Short capability name","entity":"`Class.method()`",
"evidence":["E1"]}}],
"flow":{{"title":"What the diagram shows","steps":[{{"from":"`Class.method()`",
"to":"`Other.call()`","label":"what moves","evidence":["R1"]}}]}},
"see_also":[{{"page":"page-id-from-the-list","why":"what that page covers"}}],
"sections":[{{"title":"Section title",
"lead":{{"statements":["what problem this part solves",
"how it is arranged"],"evidence":["E1"]}},
"excerpt":{{"evidence":"E2","why":"what to notice in these lines"}},
"claims":[{{"role":"flow",
"statement":"one concrete claim","evidence":["E1","E2","R1"]}}]}}]}}

Use 3-6 sections and 2-4 claims per section. Every thesis, purpose, and claim
statement must be exactly one sentence, and every claim must cite one or more
provided evidence IDs.

Write `purpose` for a reader who has not seen this codebase: two or three
sentences saying what this area is responsible for and what it does for the
caller, in plain language, before any symbol-by-symbol detail. Name concrete
components, but explain the responsibility rather than reciting a call. It is
still grounded -- cite the evidence the responsibility rests on.

The no-benefit-language rule below applies to `purpose` and `map` exactly as it
does to claims: describe what the code does, never how good it is. Do not write
that something is fast, flexible, easy, powerful, efficient, or that it allows,
enables, ensures, facilitates or optimizes anything -- not even when the
project's own README says so. Say what it does instead.

Write `map` as 3-6 rows pairing a capability a reader would look for with the
concrete code entity that implements it, so the page can be scanned before it
is read. Each row cites the evidence for that pairing. Omit `map` when the
evidence does not support at least three distinct pairings.

Open each section with `lead`: one or two sentences at the level of mechanism
-- what problem this part of the system solves and how it is arranged -- before
any symbol-by-symbol detail. A lead may reason across its sentences and does
not have to name two endpoints; it must still cite the evidence it rests on.
Write it for someone meeting this codebase for the first time. Do not restate
the section title, and do not simply name the callables the claims will cover.

Give a section an `excerpt` when one cited body is worth reading directly,
naming the E# whose source shows it and one sentence on what to notice. Choose
the passage that carries the idea, not the longest one. At most two sections on
a page should carry an excerpt; a page of code excerpts is no better than
reading the file.

Write `flow` when the evidence supports an ordered path a reader can follow --
a request being handled, a value being transformed, a lifecycle advancing. Use
3-6 steps. Every `from` and `to` must be a symbol that appears in the supplied
evidence or relations, written the same way it appears there; a step that names
anything else is dropped. Prefer steps backed by an R# relation. Omit `flow`
entirely when the evidence shows no ordered path -- an invented sequence is
worse than none.

Write `see_also` to point at 1-3 of the other wiki pages listed below when a
responsibility genuinely belongs to them. Use their exact bracketed id. Never
name a page that is not listed, and do not point at this page.

Do not repeat a relation across sections: each source-to-target handoff belongs
to exactly one section, in the section whose responsibility it serves.

You are writing one page of a wiki, not a standalone document. When a
responsibility clearly belongs to another listed page, name that area in one
clause and move on instead of re-explaining it; spend this page's evidence on
what this page owns. Never invent a page that is not listed.

Do not invent files, symbols, APIs, relationships, or
behavior. State implementation facts, not expected benefits or marketing
judgments. Write direct subject-verb-object claims. Avoid benefit language such
as allows, enables, facilitates, efficient, optimize, quick, flexible, easy, or
powerful. Put commands, paths, and code identifiers in backticks. Use the word
command only for literal CLI syntax shown in source evidence; a method or
function is a callable, not a command. When evidence is a method body, attribute
its behavior to the full method name, not to the owner class, unless separate
class evidence is provided. Omit a desired topic when the evidence does not
support it.

The thesis must name a concrete code component and its source-supported action.
Never say that a page documents, describes, covers, or explains a topic, and
never use the outline topic hint itself as the thesis.

Assign each claim exactly one role:
- purpose: the page or component's source-supported responsibility;
- entry: a real public command, endpoint, class, or callable;
- flow: an explicit handoff between at least two named components. A flow claim
  must cite either the matching source-to-target R# relation or, when no static
  relation exists for that endpoint pair, one E# source body that contains both
  endpoints and the handoff;
- responsibility: state or work owned by one component;
- contract: an input, output, invariant, failure, or compatibility boundary;
- component: a supporting implementation fact that fits none of the above.

Write a system explanation, not a symbol catalog. Organize sections around a
request path, lifecycle stage, state transition, decision boundary, or shared
contract; do not mirror one method per section. When several operations access
the same object or state, explain that shared lifecycle instead of listing the
operations independently. Every section must combine facts into one
responsibility or flow. When static relations are provided, include
relation-backed flow claims that name the source before the target and say how
work or data moves between them. Only when no static relation exists for that
endpoint pair may a flow instead cite one source body that contains both named
endpoints and the handoff. Every claim labeled flow must satisfy one of those
rules. Every cross-component call, transfer, read, or interaction must be a
flow claim and must put both concrete code endpoints in backticks; do not hide
an unsupported handoff behind generic subsystem names. Do not label an
isolated "X does Y" sentence as flow. Use
responsibility or contract for a local behavior when neither a relation nor one
source body supports a handoff; never attach an unrelated R# relation. Never
create a workflow or integration section by combining separate source bodies
when the static reference relations are "(none)"; keep those components in
their independently grounded topic sections instead. Never
mention E# or R# IDs,
relation anchors, source line numbers, or phrases such as "as indicated by the
reference" inside a thesis or claim statement; record evidence only in the
corresponding evidence array.
"""


def _page_planning_guidance(meta: Dict[str, Any]) -> str:
    if meta.get("id") == "overview":
        major_topics = [
            str(topic.get("title") or "").strip()
            for topic in meta.get("major_topics") or []
            if isinstance(topic, dict) and str(topic.get("title") or "").strip()
        ]
        topic_guidance = (
            " Use the source-anchored outline areas as the breadth check for this "
            "mental model: "
            + ", ".join(major_topics)
            + ". Give every named area its own recognizable section unless one "
            "provided relation or source body explicitly supports merging two "
            "areas. Do not spend multiple sections on one area while omitting "
            "the others."
            if major_topics
            else ""
        )
        return (
            "Build a repository mental model around at least three complementary "
            "concerns: "
            "(1) the concrete public workflow from a user entry point to its "
            "result, (2) the main execution or data-flow handoffs, and (3) the "
            "responsibilities of at least two major subsystems. The intro thesis "
            "already states the repository purpose, so do not repeat its channel "
            "list. README evidence may support a distinct public command, while "
            "implementation claims should use source evidence. Use at least "
            "four distinct evidence IDs across the plan when available, and "
            "prefer concrete actions and handoffs over generic 'provides' or "
            "'includes' claims. Give every topic section at least two "
            "complementary facts: pair its responsibility with an entry point, "
            "handoff, mechanism, or contract from the same evidence. Describe "
            "the public workflow with an actual "
            "command, route, or API from the evidence; never say that users call "
            "a private underscore-prefixed helper. Name major components by "
            "their class, service, or responsibility, never as a filename "
            "subsystem. Treat single-language backends, decoders, "
            "patchers, and private helpers as implementation details unless they "
            "define the repository." + topic_guidance
        )
    guidance = (
        "Start from the page's source-supported purpose, then explain the path "
        "from a real entry point through the components that own the work and "
        "state. Use static relations for explicit handoffs when available. "
        "Include a public entry point only when the evidence shows a command, "
        "route, class, or non-underscore callable that serves that role. "
        "Otherwise describe the internal control or data flow without relabeling "
        "private helpers as APIs. Attribute behavior only to the function or "
        "class whose cited body implements it; a nearby field, constructor "
        "argument, or static relation does not establish purpose. Group related "
        "symbols by lifecycle, state, or request boundary; do not write one "
        "section per symbol or enumerate unrelated methods."
    )
    children = [
        str(child.get("title") or "").strip()
        for child in meta.get("children") or []
        if isinstance(child, dict) and str(child.get("title") or "").strip()
    ]
    if children:
        guidance += (
            " This is a parent page: explain orchestration, shared contracts, "
            "and boundaries across its child topics instead of repeating each "
            "child's implementation inventory. Give every child topic at least "
            "one section-level boundary or responsibility fact; mentioning it "
            "only in the intro does not establish coverage. Reserve detailed "
            "mechanics for: " + ", ".join(children) + "."
        )
    return guidance


def _plan_evidence_constraints(
    meta: Dict[str, Any],
    evidence: Sequence[EvidenceItem],
    relations: Sequence[RelationItem] = (),
) -> str:
    """Describe evidence allocation rules before the model writes claims."""

    if meta.get("id") != "overview":
        constraint = "Match each claim to the source item that directly supports it."
        child_allocations = []
        for child in meta.get("children") or []:
            if not isinstance(child, dict):
                continue
            title = str(child.get("title") or "").strip()
            files = {
                AgentWiki._norm_hint_path(str(file))
                for file in child.get("files") or []
                if str(file).strip()
            }
            child_items = [
                item
                for item in evidence
                if AgentWiki._norm_hint_path(item.file) in files
            ]
            if title and child_items:
                child_allocations.append(
                    f"{title}="
                    + ", ".join(f"{item.id} (`{item.symbol}`)" for item in child_items)
                )
        if child_allocations:
            constraint += (
                " Child-topic evidence allocation: "
                + "; ".join(child_allocations)
                + ". Every child topic must contribute at least one allocated "
                "source fact in a section; intro evidence alone does not count."
            )
        return constraint

    intro = _readme_intro(list(evidence))
    intro_id = intro[1] if intro is not None else None
    implementation = [
        item for item in evidence if item.id != intro_id and item.id.startswith("E")
    ]
    catalog = ", ".join(
        f"{item.id}=`{item.file}`::{item.symbol}" for item in implementation
    )
    topic_allocations = []
    for topic in meta.get("major_topics") or []:
        if not isinstance(topic, dict):
            continue
        title = str(topic.get("title") or "").strip()
        topic_files = {
            AgentWiki._norm_hint_path(str(file))
            for file in topic.get("files") or []
            if str(file).strip()
        }
        topic_items = [
            item
            for item in evidence
            if AgentWiki._norm_hint_path(item.file) in topic_files
        ]
        topic_relations = [
            item
            for item in relations
            if any(
                AgentWiki._norm_hint_path(
                    endpoint.split(":", 1)[0],
                )
                in topic_files
                for endpoint in (item.source, item.target)
            )
        ]
        if title and topic_items:
            allocation_items = ", ".join(
                f"{item.id} (`{item.symbol}`)" for item in topic_items
            )
            if topic_relations:
                allocation_items += ", " + ", ".join(
                    f"{item.id} (`{item.source.rsplit(':', 1)[-1]}` -> "
                    f"`{item.target.rsplit(':', 1)[-1]}`)"
                    for item in topic_relations
                )
            topic_allocations.append(f"{title}={allocation_items}")
    allocation = (
        " Major-topic evidence allocation: "
        + "; ".join(topic_allocations)
        + ". Each topic section must cite its allocated evidence; it may add "
        "cross-cutting entry evidence for a supported handoff. When a topic "
        "has an allocated R# relation, use it for that topic's concrete flow "
        "claim. Use the listed "
        "symbol as the claim subject; do not shorten a method to its owner class."
        if topic_allocations
        else ""
    )
    relation_guidance = (
        ""
        if relations
        else (
            " No static relation facts are available. Do not fabricate a "
            "workflow between separate source items. For each major topic, "
            "prefer one substantial source-supported mechanism, contract, or "
            "responsibility over multiple sentences that only say what "
            "individual callables do; leave callable inventories to child "
            "pages."
        )
    )
    reserved = f"{intro_id} supplies the intro; " if intro_id else ""
    return (
        f"{reserved}a section may reuse an evidence item only for a different "
        "supported fact. Use at least four source evidence IDs across the page "
        "when four are available. Sections may reuse a core implementation "
        "source when they make distinct claims about it. Keep sections semantically "
        "distinct and assign sources by their actual responsibility. "
        f"Implementation source catalog: "
        f"{catalog or '(none)'}.{allocation}{relation_guidance}"
    )


def _normalize_plan_support(
    plan: dict[str, Any],
    evidence: Sequence[EvidenceItem],
    relations: Sequence[RelationItem],
) -> dict[str, Any]:
    """Retain only claims and relation citations with executable evidence support."""

    evidence_by_id = {item.id: item for item in evidence}
    relations_by_id = {item.id: item for item in relations}
    normalized_sections = []
    for section in plan.get("sections") or []:
        normalized_claims = []
        for claim in section.get("claims") or []:
            ids = [str(item) for item in claim.get("evidence") or []]
            source_ids = [item for item in ids if item in evidence_by_id]
            relation_ids = [item for item in ids if item in relations_by_id]
            statement = str(claim.get("statement") or "")
            cited_evidence = [evidence_by_id[item] for item in source_ids]
            statement = _normalize_callable_command_labels(
                statement,
                cited_evidence,
            )
            claim["statement"] = statement
            role = str(claim.get("role") or infer_claim_role(statement))
            if role != "flow":
                if (
                    not cited_evidence
                    or (
                        not _claim_has_local_support(statement, cited_evidence)
                        and not _is_evidence_provenance_claim(
                            statement,
                            cited_evidence,
                        )
                    )
                    or _method_owner_generalizations(statement, cited_evidence)
                    or describes_private_entry(statement, role=role)
                ):
                    continue
                if relation_ids and source_ids:
                    claim["evidence"] = [
                        item for item in ids if item not in relations_by_id
                    ]
                normalized_claims.append(claim)
                continue

            matching_relations = {
                item
                for item in relation_ids
                if relation_matches_claim(statement, relations_by_id[item])
            }
            known_relation_pair = any(
                relation_endpoints_named(statement, item) for item in relations
            )
            source_supported = not known_relation_pair and any(
                evidence_matches_claim(statement, evidence_by_id[item])
                for item in source_ids
            )
            if not matching_relations and not source_supported:
                continue
            claim["evidence"] = [
                item
                for item in ids
                if item not in relations_by_id or item in matching_relations
            ]
            normalized_claims.append(claim)
        section["claims"] = normalized_claims
        if normalized_claims:
            normalized_sections.append(section)
    plan["sections"] = normalized_sections

    admitted_thesis = _supported_thesis(plan.get("thesis"), evidence, relations)
    if admitted_thesis is None:
        candidates = [
            claim
            for section in normalized_sections
            for claim in section.get("claims") or []
        ]
        role_priority = {
            "purpose": 0,
            "entry": 1,
            "responsibility": 2,
            "contract": 3,
            "flow": 4,
            "component": 5,
        }
        candidates.sort(
            key=lambda claim: role_priority.get(
                str(
                    claim.get("role")
                    or infer_claim_role(str(claim.get("statement") or ""))
                ),
                6,
            )
        )
        for claim in candidates:
            candidate = {
                "statement": claim.get("statement"),
                "evidence": list(claim.get("evidence") or []),
            }
            admitted_thesis = _supported_thesis(candidate, evidence, relations)
            if admitted_thesis is not None:
                plan["thesis"] = candidate
                break

    return plan


def _plan_quality_warnings(
    meta: Dict[str, Any],
    plan: dict[str, Any],
    evidence: List[EvidenceItem],
    relations: Sequence[RelationItem] = (),
) -> List[str]:
    """Validate whether a fact plan is dense enough for its page role."""

    sections = plan.get("sections") or []
    warnings = []
    page_title = str(meta.get("title") or "")
    page_is_about_helpers = bool(
        re.search(r"\b(?:helpers?|utilit(?:y|ies))\b", page_title, re.IGNORECASE)
    )
    declared_child_terms = [
        _prose_terms(str(child.get("title") or ""))
        for child in meta.get("children") or []
        if isinstance(child, dict)
    ]
    public_evidence = sum(
        not re.search(
            r"(?:^|[:.])_[A-Za-z]\w*(?:\(\))?$",
            item.symbol,
        )
        for item in evidence
    )
    for section in sections:
        title = str(section.get("title") or "untitled")
        entry_section = bool(
            re.search(r"\b(?:public|entry\s+points?)\b", title, re.IGNORECASE)
        )
        if (
            not page_is_about_helpers
            and public_evidence >= 2
            and re.search(
                r"\b(?:helpers?|utilit(?:y|ies))\b",
                title,
                re.IGNORECASE,
            )
            and not any(
                terms and len(terms & _prose_terms(title)) / len(terms) >= 0.6
                for terms in declared_child_terms
            )
        ):
            warnings.append(
                f"section {title!r} elevates incidental helpers over the page's "
                "core responsibility"
            )
        for claim in section.get("claims") or []:
            statement = str(claim.get("statement") or "")
            role = str(claim.get("role") or infer_claim_role(statement))
            if describes_private_entry(statement, role=role) or (
                entry_section
                and not is_interaction_claim(statement)
                and describes_private_entry(statement, role="entry")
            ):
                warning = (
                    f"section {title!r} describes a private helper as a user "
                    "entry point"
                )
                if warning not in warnings:
                    warnings.append(warning)
            command_identifiers = re.findall(
                r"\bcommand\s+`([^`\n]+)`",
                statement,
                re.IGNORECASE,
            )
            if command_identifiers:
                cited = {str(item) for item in claim.get("evidence") or []}
                for identifier in command_identifiers:
                    leaf = re.sub(
                        r"\([^)]*\)$",
                        "",
                        identifier.rsplit(":", 1)[-1],
                    ).rsplit(".", 1)[-1]
                    matching = [
                        item
                        for item in evidence
                        if item.id in cited
                        and re.sub(
                            r"\([^)]*\)$",
                            "",
                            item.symbol.rsplit(":", 1)[-1],
                        ).rsplit(".", 1)[-1]
                        == leaf
                    ]
                    if matching and all(
                        item.kind in {"class", "function", "method"}
                        for item in matching
                    ):
                        warnings.append(
                            f"claim {statement!r} describes a code callable as "
                            "a CLI command"
                        )
            cited_evidence = [
                item
                for item in evidence
                if item.id in {str(value) for value in claim.get("evidence") or []}
            ]
            for item in _method_owner_generalizations(statement, cited_evidence):
                warnings.append(
                    f"claim {statement!r} generalizes method evidence "
                    f"`{item.symbol}` to its owner class"
                )
            for identifier, stated_kind in re.findall(
                r"`([^`\n]+)`\s+(class|function|method)\b",
                statement,
                re.IGNORECASE,
            ):
                normalized_identifier = re.sub(
                    r"\([^)]*\)$",
                    "",
                    identifier.rsplit(":", 1)[-1],
                ).lower()
                matching = [
                    item
                    for item in cited_evidence
                    if re.sub(
                        r"\([^)]*\)$",
                        "",
                        item.symbol.rsplit(":", 1)[-1],
                    ).lower()
                    == normalized_identifier
                ]
                actual_kinds = {
                    item.kind.lower()
                    for item in matching
                    if item.kind.lower() in {"class", "function", "method"}
                }
                if actual_kinds and stated_kind.lower() not in actual_kinds:
                    warnings.append(
                        f"claim {statement!r} labels `{identifier}` as "
                        f"{stated_kind.lower()}, but cited evidence records "
                        f"{'/'.join(sorted(actual_kinds))}"
                    )

    source_ids = {item.id for item in evidence}
    evidence_by_id = {item.id: item for item in evidence}
    relations_by_id = {item.id: item for item in relations}
    thesis = plan.get("thesis") or {}
    admitted_thesis = _supported_thesis(thesis, evidence, relations)
    if admitted_thesis is None:
        warnings.append("page thesis must be a source-grounded claim")
    else:
        thesis_statement, _thesis_ids = admitted_thesis
        if len(re.findall(r"[.!?](?=\s|$)", thesis_statement)) > 1:
            warnings.append("page thesis must contain exactly one sentence")

    claims = [claim for section in sections for claim in section.get("claims") or []]
    role_counts: dict[str, int] = {}
    supported_flows = []
    invalid_flows: list[tuple[str, list[str]]] = []
    invalid_source_claims: list[str] = []
    multi_sentence_claims = []
    narrates_evidence_ids = False
    narrates_relation_anchors = False
    for claim in claims:
        statement = str(claim.get("statement") or "")
        if len(re.findall(r"[.!?](?=\s|$)", statement)) > 1:
            multi_sentence_claims.append(statement)
        narrates_evidence_ids |= bool(re.search(r"(?<!\[)\b[ER]\d+\b", statement))
        narrates_relation_anchors |= bool(
            re.search(
                r"\b(?:as (?:shown|indicated) by|according to) (?:the )?"
                r"(?:reference|relation)|"
                r"`[^`\n]+\.(?:py|pyi|go|rs|ts|tsx|js|jsx|c|h|cc|cpp|hpp|"
                r"java|rb|php|cs|kt|kts|swift|scala):\d+(?:-\d+)?`",
                statement,
                re.IGNORECASE,
            )
        )
        role = str(claim.get("role") or infer_claim_role(statement))
        role_counts[role] = role_counts.get(role, 0) + 1
        claim_evidence = [str(item) for item in claim.get("evidence") or []]
        cited_source_items = [
            evidence_by_id[item] for item in claim_evidence if item in evidence_by_id
        ]
        if (
            role != "flow"
            and cited_source_items
            and not _claim_has_local_support(statement, cited_source_items)
        ):
            warnings.append(
                f"claim {statement!r} is not supported by concrete terms in "
                "its cited source evidence"
            )
        if role != "flow" and not any(item in source_ids for item in claim_evidence):
            invalid_source_claims.append(statement)
        if role == "flow":
            cited_relations = [
                relations_by_id[item]
                for item in claim_evidence
                if item in relations_by_id
            ]
            cited_evidence = [
                evidence_by_id[item]
                for item in claim_evidence
                if item in evidence_by_id
            ]
            known_relation_pair = any(
                relation_endpoints_named(statement, item) for item in relations
            )
            if is_interaction_claim(statement) and (
                (
                    bool(cited_relations)
                    and any(
                        relation_matches_claim(statement, item)
                        for item in cited_relations
                    )
                )
                or (
                    not cited_relations
                    and not known_relation_pair
                    and any(
                        evidence_matches_claim(statement, item)
                        for item in cited_evidence
                    )
                )
            ):
                supported_flows.append(claim)
            else:
                invalid_flows.append((statement, claim_evidence))

    if narrates_evidence_ids:
        warnings.append(
            "claim statements must not narrate evidence IDs; use only the "
            "evidence array"
        )
    if narrates_relation_anchors:
        warnings.append(
            "claim statements must not narrate relation anchors or source line "
            "references"
        )
    if multi_sentence_claims:
        warnings.append(
            f"{len(multi_sentence_claims)} claim statement(s) contain multiple "
            "sentences; keep each claim to one sentence"
        )
    if relations and len(evidence) >= 2 and not supported_flows:
        examples = "; ".join(
            f"{item.id}: `{item.source}` -> `{item.target}`" for item in relations[:3]
        )
        warnings.append(
            "page has static relations but no supported component handoff"
            + (f"; use a real pair such as {examples}" if examples else "")
        )
    if invalid_flows:
        for statement, ids in invalid_flows:
            cited_relations = [
                relations_by_id[item] for item in ids if item in relations_by_id
            ]
            endpoints = "; ".join(
                f"{item.id}: `{item.source}` -> `{item.target}`"
                for item in cited_relations
            )
            detail = (
                f"; name both endpoints from {endpoints}"
                if endpoints
                else (
                    "; cite one matching source-to-target R# relation, or one E# "
                    "source body only when no static relation exists for the "
                    "named endpoint pair"
                )
            )
            warnings.append(
                f"flow claim {statement!r} is not an explicit component "
                f"handoff{detail}"
            )
    if invalid_source_claims:
        warnings.append(
            f"{len(invalid_source_claims)} non-flow claim(s) cite only static "
            "relations; purpose, responsibility, contract, entry, and component "
            "claims require E# source evidence"
        )
    section_evidence_ids = {
        str(item)
        for claim in claims
        for item in claim.get("evidence") or []
        if str(item).startswith("E")
    }
    for child in meta.get("children") or []:
        if not isinstance(child, dict):
            continue
        child_title = str(child.get("title") or "").strip()
        child_files = {
            AgentWiki._norm_hint_path(str(file))
            for file in child.get("files") or []
            if str(file).strip()
        }
        child_ids = {
            item.id
            for item in evidence
            if AgentWiki._norm_hint_path(item.file) in child_files
        }
        if child_title and child_ids and not child_ids & section_evidence_ids:
            warnings.append(
                f"parent page needs a section-level {child_title!r} fact "
                "grounded in its allocated evidence"
            )
    component_claims = role_counts.get("component", 0)
    if len(claims) >= 4 and component_claims / len(claims) > 0.65:
        warnings.append(
            "page is dominated by isolated component facts instead of a system "
            "explanation"
        )
    plan_markdown = "\n\n".join(
        [
            f"## {section.get('title', '')}\n\n"
            + " ".join(
                str(claim.get("statement") or "").rstrip(".") + "."
                for claim in section.get("claims") or []
            )
            for section in sections
            if section.get("title") and section.get("claims")
        ]
    )
    supported_thesis = _supported_thesis(
        plan.get("thesis"),
        evidence,
        relations,
    )
    thesis_markdown = supported_thesis[0] if supported_thesis is not None else ""
    full_plan_markdown = "\n\n".join(
        item for item in (thesis_markdown, plan_markdown) if item
    )
    if _duplicate_prose_blocks(full_plan_markdown):
        warnings.append(
            "page thesis or sections substantially repeat an admitted fact; "
            "keep the richer fact once and use a distinct thesis"
        )
    synthesis = _section_synthesis_report(plan_markdown)
    if not synthesis["section_synthesis_valid"]:
        warnings.append(
            "page plan is dominated by isolated operation sections: "
            + ", ".join(synthesis["isolated_catalog_sections"])
            + "; reorganize around shared state, lifecycle, or request handoffs"
        )

    if meta.get("id") != "overview":
        density = _narrative_density_report(full_plan_markdown)
        if not density["narrative_density_valid"]:
            warnings.append(
                "page reads as a callable catalog; combine related operations "
                "around shared state, lifecycle, or request handoffs"
            )
        distinct_thesis = supported_thesis is not None
        if supported_thesis is not None:
            thesis_statement = supported_thesis[0]
            thesis_subject = _leading_code_subject(thesis_statement)
            thesis_terms = _redundancy_terms(thesis_statement)
            for claim in claims:
                claim_statement = str(claim.get("statement") or "")
                claim_terms = _redundancy_terms(claim_statement)
                smaller = min(len(thesis_terms), len(claim_terms))
                repeats_thesis = (
                    thesis_statement.strip().casefold()
                    == claim_statement.strip().casefold()
                ) or bool(
                    thesis_subject
                    and thesis_subject == _leading_code_subject(claim_statement)
                    and smaller >= 5
                    and len(thesis_terms & claim_terms) / smaller >= 0.8
                )
                if repeats_thesis:
                    distinct_thesis = False
                    break
        supported_facts = len(claims) + int(distinct_thesis)
        if len(evidence) >= 2 and supported_facts < 2:
            warnings.append(
                "page needs at least two supported facts when multiple source "
                "evidence items are available"
            )
        return warnings

    major_topics = [
        topic
        for topic in meta.get("major_topics") or []
        if isinstance(topic, dict) and str(topic.get("title") or "").strip()
    ]
    required_facts = max(4, len(sections) + 1, len(major_topics) + 1)
    narrative_facts = len(claims) + int(supported_thesis is not None)
    if narrative_facts < required_facts:
        warnings.append(
            f"Overview needs at least {required_facts} supported narrative "
            "facts: one thesis and one concrete fact for every planned or "
            "allocated topic"
        )
    density = _narrative_density_report(plan_markdown)
    if not density["narrative_density_valid"]:
        warnings.append(
            "Overview reads as a callable catalog; explain responsibilities, "
            "mechanisms, or handoffs instead of listing one operation per topic"
        )
    if len(sections) < 3:
        warnings.append("Overview needs at least three complementary sections")
    for topic in meta.get("major_topics") or []:
        if not isinstance(topic, dict):
            continue
        topic_title = str(topic.get("title") or "").strip()
        topic_terms = _prose_terms(topic_title)
        topic_files = {
            AgentWiki._norm_hint_path(str(file))
            for file in topic.get("files") or []
            if str(file).strip()
        }
        topic_ids = {
            item.id
            for item in evidence
            if AgentWiki._norm_hint_path(item.file) in topic_files
        }
        topic_relation_ids = {
            item.id
            for item in relations
            if any(
                AgentWiki._norm_hint_path(endpoint.split(":", 1)[0]) in topic_files
                for endpoint in (item.source, item.target)
            )
        }
        matching_sections = [
            section
            for section in sections
            if topic_terms
            and topic_terms & _prose_terms(str(section.get("title") or ""))
        ]
        covered_ids = {
            str(item)
            for section in matching_sections
            for claim in section.get("claims") or []
            for item in claim.get("evidence") or []
        }
        allocated_ids = topic_ids | topic_relation_ids
        if topic_title and (
            not matching_sections or (allocated_ids and not allocated_ids & covered_ids)
        ):
            warnings.append(
                f"Overview needs a {topic_title!r} section grounded in its "
                "allocated evidence"
            )
        elif topic_relation_ids and not topic_relation_ids & covered_ids:
            warnings.append(
                f"Overview {topic_title!r} must use an allocated relation "
                f"({', '.join(sorted(topic_relation_ids))}) for its concrete "
                "flow claim"
            )
        topic_claims = [
            claim
            for section in matching_sections
            for claim in section.get("claims") or []
        ]
        topic_body = " ".join(
            str(claim.get("statement") or "") for claim in topic_claims
        )
        topic_plain = re.sub(r"[`*_[\]()#>-]", " ", topic_body)
        topic_plain = re.sub(r"\s+", " ", topic_plain).strip()
        if (
            matching_sections
            and len(allocated_ids) >= 2
            and len(topic_claims) < 2
            and len(topic_plain) < 60
        ):
            warnings.append(
                f"Overview {topic_title!r} needs two complementary supported "
                "facts when multiple source items are allocated"
            )
    required_roles = ["flow"] if relations else []
    for role in required_roles:
        if role_counts.get(role, 0) == 0:
            warnings.append(f"Overview needs at least one {role} claim")

    intro = _readme_intro(evidence)
    intro_terms = _prose_terms(intro[0]) if intro is not None else set()
    require_source_diversity = sum(item.id.startswith("E") for item in evidence) >= 4
    page_sources = (
        {item for item in supported_thesis[1] if str(item).startswith("E")}
        if supported_thesis is not None
        else set()
    )
    prior_section_terms: List[tuple[str, set[str]]] = []
    for section in sections:
        title = str(section.get("title") or "untitled")
        useful_claims = []
        for claim in section.get("claims") or []:
            statement = _format_supported_literals(
                str(claim.get("statement") or "").strip()
            )
            terms = _prose_terms(statement)
            redundant = (
                bool(intro_terms)
                and len(terms) >= 4
                and len(terms & intro_terms) / len(terms) >= 0.8
            )
            if redundant:
                continue
            role = str(claim.get("role") or infer_claim_role(statement))
            if describes_private_entry(statement, role=role):
                warning = f"section {title!r} describes a private helper as a user entry point"
                if warning not in warnings:
                    warnings.append(warning)
                continue
            if re.search(
                r"`?[\w./-]+\.(?:py|go|rs|ts|tsx|js|jsx|c|h|cc|cpp|java|rb|"
                r"php|cs|kt|kts)`?\s+(?:file\s+)?subsystem\b",
                statement,
                flags=re.IGNORECASE,
            ):
                warnings.append(f"section {title!r} names a source file as a subsystem")
                continue
            ids = [str(item) for item in claim.get("evidence") or []]
            candidate = f"{statement.rstrip('.')}." + "".join(
                f" [{item}]" for item in ids
            )
            claim_report = grounding_report(candidate, evidence, relations)
            if any(
                claim_report[key]
                for key in (
                    "unknown_citations",
                    "unknown_files",
                    "unsupported_identifiers",
                    "promotional_phrases",
                )
            ):
                continue
            useful_claims.append(claim)
            source_ids = {
                str(item)
                for item in claim.get("evidence") or []
                if str(item).startswith("E")
            }
            page_sources.update(source_ids)
        if not useful_claims:
            warnings.append(
                f"section {title!r} needs one publishable, non-redundant claim"
            )
        section_terms = set().union(
            *(
                _prose_terms(str(claim.get("statement") or ""))
                for claim in useful_claims
            )
        )
        for prior_title, prior_terms in prior_section_terms:
            smaller = min(len(section_terms), len(prior_terms))
            overlap = (
                len(section_terms & prior_terms) / smaller if smaller >= 6 else 0.0
            )
            if overlap >= 0.8:
                warnings.append(
                    f"section {title!r} substantially repeats section {prior_title!r}"
                )
                break
        prior_section_terms.append((title, section_terms))
    if require_source_diversity and len(page_sources) < 4:
        warnings.append("Overview must use at least four source evidence IDs")
    return warnings


_PLAN_REPAIR_PROMPT = """\
Revise the source-grounded fact plan below.

Page title: {title}
Planning guidance: {guidance}
Evidence allocation: {constraints}

Problems:
{problems}

Current plan:
{plan}

Source evidence:
{evidence}

Static reference relations:
{relations}

Return ONLY JSON with the same grounded thesis and
sections/claims/role/evidence shape. Resolve every listed problem without
inventing files, symbols, APIs, relationships, or behavior. Keep every claim
already admitted by the current plan unless a listed problem identifies that
claim or its section as repetitive, catalog-like, or component-dominated. For
missing breadth, roles, evidence, or relation use, add complementary claims
instead of deleting valid claims. For repetition or catalog-density problems,
consolidate related claims into a richer sentence while preserving their
supported facts and evidence IDs. Count the claims in the returned JSON before
responding. Keep every claim concrete, use exactly one sentence per thesis or
claim statement, and cite only provided evidence IDs. A flow claim must
explicitly name at least two components,
describe their handoff in the listed source-to-target direction, and cite its
matching R# relation. Only when no static relation exists for that endpoint
pair may it cite one E# source body that contains both endpoints. Use
responsibility or contract when neither form of evidence supports a component
handoff; do not force such a claim into flow or attach an unrelated relation.
Use direct subject-verb-object facts; do not use allows, enables, facilitates,
efficient, optimize, quick, flexible, easy, powerful, or other benefit claims.
Put commands, paths, and code identifiers in backticks. Build a system
explanation rather than an inventory of isolated symbols. Never mention E# or
R# IDs, relation anchors, source line numbers, or phrases such as "as indicated
by the reference" inside a statement; record evidence only in the evidence
array.
When a problem reports isolated operation sections or component-dominated
facts, reorganize related calls around the state or lifecycle they implement.
Prefer one richer supported claim that connects sequential operations over
separate sentences that only list what each method does. Do not invent a call
edge when the evidence does not show one. When the thesis repeats a section
fact, keep the richer fact in its section and replace the thesis with a distinct
supported statement that scopes the whole page.
"""


_PAGE_PROMPT = """\
You are writing one page of a developer wiki for the {repo} codebase.

Page title: {title}
What it should cover: {summary}

Approved fact plan:
{plan}

Source evidence:
{evidence}

Static reference relations:
{relations}

Write the page as GitHub-flavored Markdown:
- Start with a short, cited intro paragraph that states only the repository
  purpose and page scope (no H1 title; the app adds it). Leave commands,
  execution steps, and subsystem details to their planned sections.
- Follow the approved fact plan. Explain the subsystem, its key pieces, and
  their interactions in clear prose with ## / ### subheadings.
- Render every planned claim exactly once as one concrete sentence. Use one
  paragraph per section and do not add transition, implication, justification,
  or benefit sentences that are not themselves planned claims.
- Put an evidence marker such as [E1] or [R1] in every substantive paragraph.
- End each intro or section paragraph with its combined evidence markers; do
  not place citations only in the middle of a paragraph.
- Use `inline code` only for identifiers and paths present in the evidence.
- Do not invent files, symbols, APIs, relationships, or behavior.
- Use factual engineering language. Do not call anything powerful, efficient,
  comprehensive, crucial, flexible, responsive, user-friendly, invaluable, or
  productivity-enhancing. End a sentence after the supported mechanism instead
  of adding a generic benefit or justification clause.
- Name a subsystem by its concrete component or responsibility, not by appending
  "subsystem" to a source filename. Avoid "this page/document explains" prose.
- Do not draw a diagram; the application renders the static graph separately.
Return only the Markdown (no JSON, no commentary)."""


_REPAIR_PROMPT = """\
Revise the Markdown below so it satisfies the source-grounding checks.

Problems:
{problems}

Approved fact plan:
{plan}

Evidence:
{evidence}

Relations:
{relations}

Draft:
{draft}

Keep useful explanations, but remove unsupported names and paths. Put [E#] or
[R#] evidence markers in every substantive paragraph, including the intro.
Preserve the supported fact plan: write an intro and a prose section for every
planned section instead of deleting sections to fix wording. Render every
planned claim exactly once as one concrete sentence. Remove unplanned benefit
or justification clauses instead of paraphrasing them. Remove paragraphs that
merely restate the intro or another section. Reusing an evidence item is valid
when the paragraph states a different supported fact.
The revision must contain at least {required_sections} H2 sections and
{required_blocks} substantive evidence-cited paragraphs. End every substantive
paragraph with its evidence marker(s); do not append unsupported sentences
after a citation.
Do not wrap the response in a Markdown code fence. Return only Markdown.
"""

_STYLE_REPAIR_PROMPT = """\
Make a minimal style edit to the Markdown below.

Flagged phrases:
{phrases}

Draft:
{draft}

Remove a flagged clause or sentence when it contains only a generic benefit;
do not replace it with a synonym. Otherwise replace only the flagged wording
and minimum surrounding grammar needed for factual prose. Preserve every
heading, paragraph, identifier, path, concrete mechanism, and evidence marker.
Do not merge or reorder technical claims. Return only Markdown.
"""


class AgentWiki:
    """Generate + serve a conceptual wiki for one repo (outline + pages)."""

    def __init__(
        self,
        bundle: Any,
        model: str,
        cache_dir: Optional[str] = None,
        *,
        llm: Any = None,
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        self._bundle = bundle
        self._model = model
        self._llm = llm
        self._cache_llm_identity = getattr(llm, "cache_identity", "")
        self._api_base = api_base
        self._api_key = api_key
        self._cache_dir = cache_dir
        self._wb = WikiBuilder(bundle)  # reuse source() + symbol/citation helpers
        self._outline: Optional[Dict[str, Any]] = None
        self._pages: Dict[str, Dict[str, Any]] = {}
        self._retrieval_routes: Dict[tuple, tuple[str, ...]] = {}

    # -- caching -----------------------------------------------------------

    def _key(self, suffix: str) -> str:
        entry = self._bundle.entry
        commit = getattr(entry, "commit_short", "") or getattr(entry, "base_commit", "")
        indexes = getattr(getattr(self._bundle, "manifest", None), "indexes", {})
        view_parts = []
        for name, view in sorted(indexes.items()):
            config = getattr(view, "config", {}) or {}
            config_hash = hashlib.sha1(
                json.dumps(
                    config,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()[:10]
            view_parts.append(
                f"{name}:{getattr(view, 'status', '')}:"
                f"{getattr(view, 'commit', '')}:"
                f"{getattr(view, 'source_fingerprint', '')}:"
                f"{getattr(view, 'built_at_epoch', '')}:{config_hash}"
            )
        view_identity = ",".join(view_parts)
        prompt_version = (
            _OUTLINE_PROMPT_VERSION if suffix == "outline" else _PAGE_PROMPT_VERSION
        )
        # Deliberately model-independent: the key identifies *what* was asked
        # (prompt version + repo snapshot + index state), not *who* answered.
        # Binding the model/endpoint into the key made every backend swap
        # discard the whole corpus (~800 pages, hours of generation). The
        # producing model is recorded in the cache entry instead — see
        # ``_write_cache`` — mirroring EdgeLabeler's namespace-only keying.
        raw = (
            f"{prompt_version}/{getattr(entry, 'instance_id', 'repo')}@{commit}/"
            f"{view_identity}/{suffix}"
        )
        return hashlib.sha1(raw.encode()).hexdigest()[:16]

    def _client(self):
        if self._llm is None:
            from ..llm.litellm_chat import LiteLLMChat

            self._llm = LiteLLMChat(
                model=self._model,
                temperature=0.2,
                max_tokens=4096,
                api_base=self._api_base,
                api_key=self._api_key,
            )
        return self._llm

    def _cache_path(self, suffix: str) -> Optional[str]:
        if not self._cache_dir:
            return None
        os.makedirs(self._cache_dir, exist_ok=True)
        return os.path.join(self._cache_dir, f"agentwiki_{self._key(suffix)}.json")

    @staticmethod
    def _page_cache_suffix(meta: Dict[str, Any]) -> str:
        """Bind a cached page to the outline metadata that steered retrieval."""

        identity = hashlib.sha1(
            json.dumps(
                meta,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()[:12]
        return f"page_{meta.get('id', 'page')}_{identity}"

    def _read_cache(self, suffix: str) -> Optional[Any]:
        path = self._cache_path(suffix)
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    return json.load(fh).get("data")
            except (OSError, json.JSONDecodeError):
                return None
        return None

    def _write_cache(self, suffix: str, data: Any) -> None:
        path = self._cache_path(suffix)
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        # Provenance only — none of this is part of the cache
                        # key, so a later backend swap reuses this entry.
                        "model": self._model,
                        "api_base": self._api_base or "",
                        "llm_identity": self._cache_llm_identity,
                        "data": data,
                    },
                    fh,
                )
        except OSError:
            pass

    # -- outline + nav -----------------------------------------------------

    def outline(self) -> Dict[str, Any]:
        if self._outline is not None:
            return self._outline
        cached = self._read_cache("outline")
        if cached and cached.get("pages"):
            self._outline = cached
            return cached
        data = generate_outline(self._bundle, self._model, llm=self._client())
        self._normalize(data.get("pages", []), seen=set(), first=True)
        self._outline = data
        if data.get("pages"):
            self._write_cache("outline", data)
        return data

    def _normalize(self, pages: List[Dict[str, Any]], seen: set, first: bool) -> None:
        """Ensure unique slug ids; force the first page id to 'overview'."""
        for i, p in enumerate(pages):
            pid = (
                "overview"
                if (first and i == 0)
                else _slug(p.get("id") or p.get("title"))
            )
            while pid in seen:
                pid += "-x"
            seen.add(pid)
            p["id"] = pid
            self._normalize(p.get("children", []) or [], seen, first=False)

    def page_tree(self) -> List[dict]:
        def refs(pages: List[Dict[str, Any]]) -> List[dict]:
            return [
                {
                    "id": p["id"],
                    "title": p.get("title", p["id"]),
                    "children": refs(p.get("children", []) or []),
                }
                for p in pages
            ]

        return refs(self.outline().get("pages", []))

    def _find(
        self, page_id: str, pages: Optional[List[Dict[str, Any]]] = None
    ) -> Optional[Dict[str, Any]]:
        if pages is None:
            pages = self.outline().get("pages", [])
        for p in pages:
            if p.get("id") == page_id:
                return p
            hit = self._find(page_id, p.get("children", []) or [])
            if hit:
                return hit
        return None

    # -- page generation ---------------------------------------------------

    def page(self, page_id: str) -> Optional[dict]:
        if page_id in self._pages:
            return self._pages[page_id]
        meta = self._find(page_id)
        if meta is None:
            return None
        if page_id == "overview":
            meta = self._overview_page_meta(
                meta,
                self.outline().get("pages", [])[1:],
            )
        cache_suffix = self._page_cache_suffix(meta)
        cached = self._read_cache(cache_suffix)
        if cached:
            self._pages[page_id] = cached
            return cached
        page = self._generate_page(meta)
        self._pages[page_id] = page
        self._write_cache(cache_suffix, page)
        return page

    def _retrieve(self, meta: Dict[str, Any], top_k: int = 12) -> List[Any]:
        ensure_views = getattr(self._bundle, "ensure_views", None)
        if callable(ensure_views):
            ensure_views()
        else:
            ensure_runtime = getattr(self._bundle, "ensure_runtime", None)
            if callable(ensure_runtime):
                ensure_runtime()
        files = self._page_retrieval_files(meta)
        parent_page = bool(meta.get("children"))
        parent_boundary_files = self._parent_boundary_files(meta)
        parent_has_core_files = bool(
            parent_page
            and {
                self._norm_hint_path(file)
                for file in files
                if self._norm_hint_path(file) not in parent_boundary_files
            }
        )
        retrieval_meta = {
            **meta,
            "files": files,
            "_parent_boundary_fallback": parent_page and not parent_has_core_files,
        }
        query = " ".join(
            [meta.get("title", ""), meta.get("summary", "")]
            + (meta.get("keywords") or [])
            + files
        ).strip()
        pool_k = max(top_k, top_k * 4)
        store = self._bundle.vector_store
        routes: List[tuple[str, List[Any]]] = []
        try:
            if store is not None and hasattr(store, "search_with_content"):
                routes.append(
                    ("dense", list(store.search_with_content(query, top_k=pool_k)))
                )
            elif store is not None:
                routes.append(("dense", list(store.search(query, top_k=pool_k))))
        except Exception as exc:  # noqa: BLE001 - fall back to BM25 below
            logger.warning("wiki retrieve (vector) failed: %s", exc)
        bm25 = self._bundle.bm25
        if bm25 is not None:
            try:
                try:
                    lexical = bm25.search(
                        query,
                        pool_k,
                        return_code_content=True,
                        wrap_with_ln=False,
                    )
                except TypeError:
                    lexical = bm25.search(query, pool_k)
                routes.append(("bm25", list(lexical)))
            except Exception as exc:  # noqa: BLE001 - retain another usable route
                logger.warning("wiki retrieve (BM25) failed: %s", exc)
        if not routes:
            return []

        fused = reciprocal_rank_fuse(
            routes,
            key=lambda node: candidate_key(node, self._node_attr),
        )
        if not _page_allows_supporting_files(retrieval_meta):
            fused = [
                item
                for item in fused
                if not _is_supporting_file(
                    self._norm_hint_path(
                        self._rel(self._node_attr(item[0], "file")) or ""
                    )
                )
            ]
        by_key = {
            candidate_key(node, self._node_attr): (route_names, score)
            for node, route_names, score in fused
        }
        reranked = self._rerank_for_page(
            retrieval_meta,
            [node for node, _, _ in fused],
        )
        reranked_with_routes = [
            (
                node,
                by_key[candidate_key(node, self._node_attr)][0],
                by_key[candidate_key(node, self._node_attr)][1],
            )
            for node in reranked
        ]
        selected = diversify_by_file(
            reranked_with_routes,
            file_of=lambda node: self._norm_hint_path(
                self._rel(self._node_attr(node, "file")) or ""
            ),
            limit=top_k,
        )
        self._retrieval_routes = {
            candidate_key(node, self._node_attr): route_names
            for node, route_names, _ in selected
        }
        selected_nodes = [node for node, _, _ in selected]

        # Outline files are explicit planning evidence. Ensure a bounded subset
        # reaches the writer even when lexical/semantic ranking favors generic
        # symbols elsewhere in the repository. This also admits README content,
        # which code-only indexes intentionally omit.
        selected_by_file: Dict[str, List[tuple[Any, tuple[str, ...]]]] = {}
        for node, route_names, _ in reranked_with_routes:
            selected_file = self._norm_hint_path(
                self._rel(self._node_attr(node, "file")) or ""
            )
            selected_by_file.setdefault(selected_file, []).append(
                (node, tuple(route_names))
            )
        outline_files = {self._norm_hint_path(file) for file in files}
        selected_keys_by_file = {
            file: {self._source_span_key(candidate) for candidate, _ in candidates}
            for file, candidates in selected_by_file.items()
        }
        try:
            for node in self._wb._symbols():
                selected_file = self._norm_hint_path(
                    self._rel(self._node_attr(node, "file")) or ""
                )
                if selected_file not in outline_files:
                    continue
                key = self._source_span_key(node)
                existing = selected_keys_by_file.setdefault(selected_file, set())
                if key not in existing:
                    selected_by_file.setdefault(selected_file, []).append(
                        (node, ("outline",))
                    )
                    existing.add(key)
        except Exception as exc:  # noqa: BLE001 - ranked retrieval remains usable
            logger.debug("wiki outline symbol enumeration unavailable: %s", exc)
        anchors = []
        promoted_source_keys = set()
        anchored_files = set()
        overview = retrieval_meta.get("id") == "overview"
        overview_topic_files = {
            self._norm_hint_path(file)
            for topic in retrieval_meta.get("major_topics") or []
            if isinstance(topic, dict)
            for file in topic.get("files") or []
            if isinstance(file, str)
        }
        if overview:
            anchor_limit = min(_OVERVIEW_RETRIEVAL_LIMIT, top_k)
        elif parent_page:
            if not parent_has_core_files:
                anchor_limit = min(top_k, min(6, max(4, len(files) * 2)))
            else:
                anchor_limit = min(
                    top_k,
                    min(4, len(files) * 2),
                )
        else:
            anchor_limit = min(4, max(1, top_k // 2))
        for raw_file in files:
            file = self._norm_hint_path(raw_file)
            if not file or file in anchored_files:
                continue
            matches = selected_by_file.get(file) or []
            if matches:
                anchor_meta = retrieval_meta
                if overview:
                    topic = next(
                        (
                            item
                            for item in retrieval_meta.get("major_topics") or []
                            if isinstance(item, dict)
                            and file
                            in {
                                self._norm_hint_path(str(topic_file))
                                for topic_file in item.get("files") or []
                            }
                        ),
                        None,
                    )
                    if topic is not None:
                        anchor_meta = {
                            **retrieval_meta,
                            "title": topic.get("title", ""),
                            "summary": topic.get("summary", ""),
                            "keywords": topic.get("keywords") or [],
                        }
                elif parent_page and len(files) > 1:
                    child = next(
                        (
                            item
                            for item in retrieval_meta.get("children") or []
                            if isinstance(item, dict)
                            and file
                            in {
                                self._norm_hint_path(str(child_file))
                                for child_file in item.get("files") or []
                            }
                        ),
                        None,
                    )
                    if child is not None:
                        anchor_meta = {
                            **retrieval_meta,
                            "title": child.get("title", ""),
                            "summary": child.get("summary", ""),
                            "keywords": child.get("keywords") or [],
                            "children": [],
                        }
                matches = sorted(
                    matches,
                    key=lambda item: self._outline_anchor_rank(
                        anchor_meta,
                        item[0],
                    ),
                    reverse=True,
                )
                if overview:
                    per_file_limit = 2 if file in overview_topic_files else 1
                elif parent_page and not parent_has_core_files:
                    per_file_limit = 4 if len(files) == 1 else 2
                else:
                    per_file_limit = 1 if file in parent_boundary_files else 2
                for node, route_names in matches[:per_file_limit]:
                    promoted_source_keys.add(self._source_span_key(node))
                    key = candidate_key(node, self._node_attr)
                    existing_routes = self._retrieval_routes.get(key, route_names)
                    self._retrieval_routes[key] = tuple(
                        dict.fromkeys(("outline", *existing_routes))
                    )
                    anchors.append(node)
                    if len(anchors) >= anchor_limit:
                        break
            else:
                source = self._wb.source(file)
                if not source or not source.get("content"):
                    continue
                node = {
                    "file": file,
                    "node_name": file,
                    "type": "file",
                    "start_line": 0,
                    "end_line": max(0, int(source.get("end_line") or 1) - 1),
                    "content": source["content"],
                }
                self._retrieval_routes[candidate_key(node, self._node_attr)] = (
                    "outline",
                )
                anchors.append(node)
            anchored_files.add(file)
            if len(anchors) >= anchor_limit:
                break
        if anchors:
            remaining = [
                node
                for node in selected_nodes
                if self._source_span_key(node) not in promoted_source_keys
                and (
                    not parent_page
                    or self._norm_hint_path(
                        self._rel(self._node_attr(node, "file")) or ""
                    )
                    in outline_files
                )
            ]
            remaining.sort(
                key=lambda node: self._outline_anchor_rank(
                    retrieval_meta,
                    node,
                ),
                reverse=True,
            )
            selected_nodes = anchors if parent_page else anchors + remaining
        return selected_nodes[:top_k]

    @staticmethod
    def _norm_hint_path(path: str) -> str:
        return path.replace("\\", "/").strip().strip("/")

    @staticmethod
    def _page_retrieval_files(meta: Dict[str, Any]) -> List[str]:
        """Keep parent-owned files plus one boundary file from each child."""

        files = [file for file in meta.get("files") or [] if isinstance(file, str)]
        child_files = {
            file
            for child in meta.get("children") or []
            if isinstance(child, dict)
            for file in child.get("files") or []
            if isinstance(file, str)
        }
        parent_files = [file for file in files if file not in child_files]
        boundaries = [
            file
            for child in meta.get("children") or []
            if isinstance(child, dict)
            for file in (child.get("files") or [])[:1]
            if isinstance(file, str)
        ]
        return list(dict.fromkeys([*parent_files, *boundaries])) or files

    @classmethod
    def _parent_boundary_files(cls, meta: Dict[str, Any]) -> set[str]:
        return {
            cls._norm_hint_path(file)
            for child in meta.get("children") or []
            if isinstance(child, dict)
            for file in (child.get("files") or [])[:1]
            if isinstance(file, str)
        }

    @classmethod
    def _overview_page_meta(
        cls,
        meta: Dict[str, Any],
        topic_pages: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Bind each top-level outline area to one representative core file."""

        def representative_file(page: Dict[str, Any]) -> str | None:
            page_files = [
                file
                for file in page.get("files") or []
                if isinstance(file, str)
                and (
                    not _is_supporting_file(file) or _page_allows_supporting_files(page)
                )
            ]
            child_files = {
                file
                for child in page.get("children") or []
                if isinstance(child, dict)
                for file in child.get("files") or []
                if isinstance(file, str)
            }

            def internal_implementation(file: str) -> bool:
                return bool(
                    re.search(
                        r"(?:^|[/_.-])(?:detail|impl|inl|internal|private)"
                        r"(?:[/_.-]|$)",
                        file,
                        re.IGNORECASE,
                    )
                )

            ranked = sorted(
                enumerate(page_files),
                key=lambda item: (
                    internal_implementation(item[1]),
                    item[1] in child_files,
                    -_overview_file_score(item[1]),
                    item[0],
                ),
            )
            return ranked[0][1] if ranked else None

        major_topics = []
        topic_files = []
        for page in topic_pages:
            if not isinstance(page, dict):
                continue
            representative = representative_file(page)
            files = [representative] if representative else []
            major_topics.append(
                {
                    "title": page.get("title", page.get("id", "")),
                    "summary": page.get("summary", ""),
                    "keywords": page.get("keywords") or [],
                    "files": files,
                }
            )
            topic_files.extend(files)

        base_files = [file for file in meta.get("files") or [] if isinstance(file, str)]
        readmes = [
            file
            for file in base_files
            if os.path.basename(file).lower().startswith("readme")
        ][:1]
        documented_entries = [
            file
            for file in base_files
            if file not in readmes and _is_supporting_file(file)
        ][:2]
        topic_file_set = set(topic_files)
        entry_files = sorted(
            (
                file
                for file in base_files
                if file not in readmes
                and file not in topic_file_set
                and file not in documented_entries
                and _overview_file_score(file) > 0
            ),
            key=lambda file: (-_overview_file_score(file), base_files.index(file)),
        )[:3]
        files = list(
            dict.fromkeys([*readmes, *documented_entries, *topic_files, *entry_files])
        )[:8]
        if not files:
            files = base_files[:8]
        return {
            **meta,
            "files": files,
            "major_topics": major_topics,
        }

    @classmethod
    def _child_specific_terms(cls, meta: Dict[str, Any]) -> set[str]:
        if not meta.get("children"):
            return set()
        parent_terms = set(
            cls._keyword_terms(
                [
                    str(meta.get("title") or ""),
                    *[str(value) for value in meta.get("keywords") or []],
                ]
            )
        )
        child_terms = set()
        for child in meta.get("children") or []:
            if not isinstance(child, dict):
                continue
            child_terms.update(
                cls._keyword_terms(
                    [
                        str(child.get("title") or ""),
                        *[str(value) for value in child.get("keywords") or []],
                    ]
                )
            )
        return child_terms - parent_terms - _GENERIC_PAGE_TERMS

    def _outline_anchor_score(self, node: Any) -> int:
        """Prefer repository-facing symbols when anchoring an outline file."""

        raw_name = str(
            self._node_attr(node, "node_name") or self._node_attr(node, "name") or ""
        )
        terminal = re.split(r"[:.]", raw_name)[-1].split("(", 1)[0]
        if terminal.startswith("_") and not terminal.startswith("__"):
            return 0
        kind = str(self._node_attr(node, "type") or "").lower()
        return 2 if kind in {"class", "interface", "module"} else 1

    def _source_span_key(self, node: Any) -> tuple[str, Any, Any]:
        return (
            self._norm_hint_path(self._rel(self._node_attr(node, "file")) or ""),
            self._node_attr(node, "start_line"),
            self._node_attr(node, "end_line"),
        )

    def _outline_anchor_rank(
        self,
        meta: Dict[str, Any],
        node: Any,
    ) -> tuple[int, int, int, int, int, int, int, int]:
        raw_name = str(
            self._node_attr(node, "node_name") or self._node_attr(node, "name") or ""
        ).lower()
        content = str(self._node_attr(node, "content") or "").lower()
        terms = self._keyword_terms(
            [
                str(meta.get("title") or ""),
                *[str(value) for value in meta.get("keywords") or []],
            ]
        )
        name_hits = sum(term in raw_name for term in terms)
        content_hits = sum(term in content for term in terms)
        child_hits = sum(term in raw_name for term in self._child_specific_terms(meta))
        overview_role = (
            _overview_symbol_role_score(raw_name) if meta.get("id") == "overview" else 0
        )
        parent_role = (
            _overview_symbol_role_score(raw_name)
            if meta.get("children") and meta.get("id") != "overview"
            else 0
        )
        start = self._node_attr(node, "start_line")
        end = self._node_attr(node, "end_line")
        span = (
            max(1, end - start + 1)
            if isinstance(start, int) and isinstance(end, int)
            else 1
        )
        substantive_body = int(
            bool(meta.get("id") == "overview" or meta.get("children")) and span >= 3
        )
        return (
            -child_hits,
            substantive_body,
            name_hits,
            overview_role,
            self._outline_anchor_score(node),
            parent_role,
            content_hits,
            min(span, 500),
        )

    @staticmethod
    def _path_matches_hint(file: str, hint: str) -> bool:
        if not file or not hint:
            return False
        hint = hint.rstrip("/")
        return (
            file == hint
            or file.endswith("/" + hint)
            or file.startswith(hint + "/")
            or ("/" + hint + "/") in ("/" + file)
        )

    @staticmethod
    def _keyword_terms(keywords: List[str]) -> List[str]:
        terms: List[str] = []
        seen = set()
        for keyword in keywords:
            for term in re.findall(r"[a-zA-Z0-9_]{3,}", keyword.lower()):
                if term not in seen:
                    seen.add(term)
                    terms.append(term)
        return terms

    def _candidate_hint_score(
        self, meta: Dict[str, Any], node: Any, file_hints: List[str], terms: List[str]
    ) -> float:
        file = self._norm_hint_path(self._rel(self._node_attr(node, "file")) or "")
        name = self._node_attr(node, "node_name") or self._node_attr(node, "name") or ""
        content = (self._node_attr(node, "content") or "")[:1200]
        haystack = f"{file} {name} {content}".lower()
        score = 0.0

        for hint in file_hints:
            if self._path_matches_hint(file, hint):
                score += 5.0
            else:
                base = hint.rsplit("/", 1)[-1]
                if base and base in file:
                    score += 1.0

        phrase_hits = 0
        for keyword in meta.get("keywords") or []:
            phrase = str(keyword).strip().lower()
            if len(phrase) >= 3 and phrase in haystack:
                phrase_hits += 1
        score += min(4.0, phrase_hits * 2.0)

        term_hits = sum(1 for term in terms if term in haystack)
        score += min(3.0, term_hits * 0.5)
        child_hits = sum(term in haystack for term in self._child_specific_terms(meta))
        score -= min(4.0, child_hits * 2.0)
        return score

    def _rerank_for_page(self, meta: Dict[str, Any], nodes: List[Any]) -> List[Any]:
        file_hints = [
            self._norm_hint_path(f)
            for f in meta.get("files") or []
            if isinstance(f, str) and f.strip()
        ]
        terms = self._keyword_terms(
            [str(k) for k in meta.get("keywords") or [] if str(k).strip()]
        )
        if not file_hints and not terms:
            return nodes

        scored: List[tuple[float, int, Any]] = []
        seen = set()
        for i, node in enumerate(nodes):
            file = self._norm_hint_path(self._rel(self._node_attr(node, "file")) or "")
            key = (
                file,
                self._node_attr(node, "start_line"),
                self._node_attr(node, "end_line"),
                self._node_attr(node, "node_name")
                or self._node_attr(node, "name")
                or "",
            )
            if key in seen:
                continue
            seen.add(key)
            scored.append(
                (self._candidate_hint_score(meta, node, file_hints, terms), i, node)
            )
        if any(score > 0 for score, _, _ in scored):
            scored = [item for item in scored if item[0] > 0]
            scored.sort(key=lambda item: (-item[0], item[1]))
        return [node for _, _, node in scored]

    def _node_attr(self, node: Any, key: str, default: Any = None) -> Any:
        if isinstance(node, dict):
            return node.get(key, default)
        return getattr(node, key, default)

    def _evidence_items(self, nodes: List[Any]) -> List[EvidenceItem]:
        items: List[EvidenceItem] = []
        by_identity: Dict[tuple[str, str], int] = {}
        total = 0
        for node in nodes:
            raw_file = self._node_attr(node, "file") or ""
            file = self._rel(raw_file) or ""
            if not file:
                continue
            start = self._node_attr(node, "start_line")
            end = self._node_attr(node, "end_line")
            # A 0/0 span is the "no line data" signature of an index whose
            # document metadata carries no spans (``_compute_symbols`` coerces
            # a missing span to 0). Emitting it would cite line 1 of the file
            # with false confidence; a citation without a line anchor is the
            # honest degradation. A genuine whole-file anchor pairs start 0
            # with the file's real end line, so it is unaffected.
            if start == 0 and end == 0:
                start = end = None
            start_line = (start + 1) if isinstance(start, int) else None
            end_line = (end + 1) if isinstance(end, int) else start_line
            content = self._node_attr(node, "content") or ""
            if not content:
                source = self._wb.source(file, start_line, end_line)
                content = (source or {}).get("content", "") if source else ""
            content = _prepare_evidence_content(file, content)
            if not content:
                continue
            symbol = (
                self._node_attr(node, "node_name")
                or self._node_attr(node, "name")
                or file
            )
            routes = self._retrieval_routes.get(
                candidate_key(node, self._node_attr),
                (),
            )
            # One definition can surface once per retrieval route. Keeping both
            # copies spends the context budget twice and, when the extra copy
            # came from a span-less index, leaves the page citing the same
            # symbol once with a line anchor and once without.
            identity = (file, _canonical_symbol(str(symbol)))
            seen_at = by_identity.get(identity)
            if seen_at is not None:
                prior = items[seen_at]
                merged_routes = tuple(dict.fromkeys((*prior.routes, *routes)))
                if prior.start_line is None and start_line is not None:
                    # This copy knows where it lives; prefer it, and take the
                    # content that matches the span it reports.
                    upgraded = dataclasses.replace(
                        prior,
                        start_line=start_line,
                        end_line=end_line,
                        content=content,
                        routes=merged_routes,
                    )
                elif merged_routes != prior.routes:
                    upgraded = dataclasses.replace(prior, routes=merged_routes)
                else:
                    continue
                total += len(upgraded.prompt_block()) - len(prior.prompt_block())
                items[seen_at] = upgraded
                continue

            item = EvidenceItem(
                id=f"E{len(items) + 1}",
                file=file,
                start_line=start_line,
                end_line=end_line,
                symbol=str(symbol),
                kind=str(self._node_attr(node, "type") or "code"),
                content=content,
                routes=routes,
            )
            block_size = len(item.prompt_block())
            if items and total + block_size > _MAX_CONTEXT_CHARS:
                break
            items.append(item)
            by_identity[identity] = len(items) - 1
            total += block_size
        return items

    @staticmethod
    def _evidence_context(evidence: List[EvidenceItem]) -> str:
        return "\n\n".join(item.prompt_block() for item in evidence) or "(none)"

    @staticmethod
    def _graph_endpoint(vertex: Any) -> dict[str, str]:
        attrs = vertex.attributes()
        return {
            "label": str(attrs.get("unified_name") or attrs.get("name") or ""),
            "kind": str(attrs.get("type") or ""),
            "file": str(attrs.get("file") or ""),
        }

    @staticmethod
    def _relation_action_score(label: str) -> int:
        terminal = re.split(r"[:.]", label or "")[-1].split("(", 1)[0]
        separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", terminal).lower()
        tokens = set(re.findall(r"[a-z0-9]+", separated))
        return sum(_OVERVIEW_SYMBOL_ROLE_WEIGHTS.get(token, 0) for token in tokens)

    def _anchored_relation_items(
        self,
        graph: Any,
        evidence: Sequence[EvidenceItem],
        relations: Sequence[RelationItem],
        *,
        limit: int,
    ) -> List[RelationItem]:
        """Select one source-range-anchored callable handoff per evidence item."""

        if limit <= 0 or not hasattr(graph, "query_range"):
            return []
        try:
            raw_graph = graph.get_graph()
        except Exception:  # noqa: BLE001 - optional graph enrichment
            return []

        existing = {(item.source, item.target) for item in relations}
        selected: List[RelationItem] = []
        repo_dir = os.path.realpath(
            str(getattr(self._bundle.entry, "repo_dir", "") or "")
        )
        for item in evidence:
            if item.start_line is None or item.end_line is None:
                continue
            try:
                result = graph.query_range(
                    item.file,
                    item.start_line - 1,
                    item.end_line - 1,
                )
            except Exception as exc:  # noqa: BLE001 - retain other evidence
                logger.debug(
                    "wiki anchored relations unavailable for %s: %s",
                    item.symbol,
                    exc,
                )
                continue

            candidates: dict[tuple[str, str], dict[str, Any]] = {}
            for edge in result.outgoing:
                if edge.anchor_file is None or edge.anchor_line is None:
                    continue
                source = self._graph_endpoint(raw_graph.vs[edge.source_vid])
                target = self._graph_endpoint(raw_graph.vs[edge.target_vid])
                if (
                    source["kind"].lower() not in _NARRATIVE_CALLABLE_KINDS
                    or target["kind"].lower() not in _NARRATIVE_CALLABLE_KINDS
                    or not _narrative_relation_endpoint(
                        source,
                        allow_private=True,
                    )
                    or not _narrative_relation_endpoint(
                        target,
                        allow_private=True,
                    )
                ):
                    continue
                target_file = target["file"]
                if not target_file:
                    continue
                if os.path.isabs(target_file) and repo_dir:
                    try:
                        if (
                            os.path.commonpath(
                                (repo_dir, os.path.realpath(target_file))
                            )
                            != repo_dir
                        ):
                            continue
                    except ValueError:
                        continue
                key = (source["label"], target["label"])
                if not all(key) or key in existing:
                    continue
                anchor_file = self._rel(edge.anchor_file)
                if not anchor_file:
                    continue
                leaf = target["label"].rsplit(":", 1)[-1].removesuffix("()")
                public = int(
                    not leaf.rsplit(".", 1)[-1].startswith("_")
                    or leaf.rsplit(".", 1)[-1].startswith("__")
                )
                score = (
                    self._relation_action_score(target["label"]),
                    int(target["kind"].lower() == "method"),
                    int(self._rel(target_file) == item.file),
                    public,
                )
                candidate = candidates.setdefault(
                    key,
                    {
                        "score": score,
                        "anchors": set(),
                    },
                )
                candidate["score"] = max(candidate["score"], score)
                candidate["anchors"].add(f"{anchor_file}:{edge.anchor_line + 1}")

            if not candidates:
                continue
            (source, target), candidate = min(
                candidates.items(),
                key=lambda entry: (
                    tuple(-value for value in entry[1]["score"]),
                    entry[0],
                ),
            )
            selected.append(
                RelationItem(
                    id=f"R{len(relations) + len(selected) + 1}",
                    source=source,
                    target=target,
                    anchors=tuple(sorted(candidate["anchors"])),
                )
            )
            existing.add((source, target))
            if len(selected) >= limit:
                break
        return selected

    def _relation_items(
        self,
        evidence: List[EvidenceItem],
        meta: Optional[Dict[str, Any]] = None,
    ) -> List[RelationItem]:
        if not evidence:
            return []
        try:
            graph = self._bundle.code_graph()
        except Exception:  # noqa: BLE001 - graph evidence is optional
            graph = None
        if graph is None:
            return []
        try:
            from ..web.codemap import build_page_subgraph

            citations = [
                {
                    "file": item.file,
                    "start_line": item.start_line,
                    "end_line": item.end_line,
                    "node_name": item.symbol,
                }
                for item in evidence
            ]
            result = build_page_subgraph(
                graph,
                citations,
                max_nodes=18,
                repo_dir=getattr(self._bundle.entry, "repo_dir", None),
            )
        except Exception as exc:  # noqa: BLE001 - source evidence remains usable
            logger.debug("wiki relationship evidence unavailable: %s", exc)
            result = {}
        nodes = {node["id"]: node for node in result.get("nodes") or []}
        relations = []
        for edge in result.get("edges") or []:
            source = nodes.get(edge.get("source"))
            target = nodes.get(edge.get("target"))
            if not source or not target:
                continue
            if not _narrative_relation_endpoint(
                source
            ) or not _narrative_relation_endpoint(target):
                continue
            anchors = tuple(
                f"{self._rel(anchor.get('file'))}:{anchor.get('line')}"
                for anchor in edge.get("anchors") or []
                if anchor.get("file")
            )
            relations.append(
                RelationItem(
                    id=f"R{len(relations) + 1}",
                    source=source.get("label") or source.get("short") or "",
                    target=target.get("label") or target.get("short") or "",
                    anchors=anchors,
                )
            )
            if len(relations) >= 12:
                break
        if meta and (meta.get("id") == "overview" or meta.get("children")):
            relations.extend(
                self._anchored_relation_items(
                    graph,
                    evidence,
                    relations,
                    limit=12 - len(relations),
                )
            )
        return relations

    @staticmethod
    def _relations_context(relations: List[RelationItem]) -> str:
        return "\n".join(item.prompt_line() for item in relations) or "(none)"

    def _resolve_page_references(
        self,
        plan: Dict[str, Any],
        current_id: str,
    ) -> Dict[str, Any]:
        """Keep only cross-references that name a real page of this wiki.

        The model is given the page list, but a link to a page that does not
        exist is a dead end for the reader, so ids are checked rather than
        trusted. Titles come from the outline so the link text stays correct
        even if the model paraphrased.
        """

        refs = plan.get("see_also") or []
        if not refs:
            return plan
        titles: Dict[str, str] = {}

        def walk(pages: Sequence[Dict[str, Any]]) -> None:
            for page in pages:
                if not isinstance(page, dict):
                    continue
                page_id = str(page.get("id") or "")
                if page_id:
                    titles[page_id] = str(page.get("title") or page_id)
                walk(page.get("children") or [])

        walk((self._outline or {}).get("pages") or [])
        if not titles:
            return plan
        resolved = []
        seen = set()
        for ref in refs:
            page_id = str(ref.get("page") or "").strip()
            if page_id not in titles or page_id == current_id or page_id in seen:
                continue
            seen.add(page_id)
            resolved.append({**ref, "page": page_id, "title": titles[page_id]})
        plan = dict(plan)
        if resolved:
            plan["see_also"] = resolved
        else:
            plan.pop("see_also", None)
        return plan

    def _wiki_context(self, current_id: str) -> str:
        """The other pages of this wiki, so a page knows where it sits.

        Pages are planned independently from their own retrieved evidence, which
        left every page re-explaining whatever its neighbours had already
        covered and never pointing at them. This is derived from the outline,
        so it costs no extra model call.
        """

        lines: List[str] = []

        def walk(pages: Sequence[Dict[str, Any]], depth: int) -> None:
            for page in pages:
                if not isinstance(page, dict):
                    continue
                page_id = str(page.get("id") or "")
                title = str(page.get("title") or "").strip()
                if not title:
                    continue
                summary = re.sub(r"\s+", " ", str(page.get("summary") or "")).strip()[
                    :160
                ]
                marker = " (this page)" if page_id == current_id else ""
                indent = "  " * depth
                # The id is what a cross-reference has to name, so hand it over.
                lines.append(
                    f"{indent}- [{page_id}] {title}{marker}"
                    + (f": {summary}" if summary else "")
                )
                walk(page.get("children") or [], depth + 1)

        # Read the resolved outline only. Generating one here would make page
        # rendering depend on an outline round-trip it does not otherwise need.
        outline = self._outline or {}
        try:
            walk(outline.get("pages") or [], 0)
        except Exception:  # noqa: BLE001 - context is an enhancement, never fatal
            return "(unavailable)"
        return "\n".join(lines) or "(none)"

    def _fact_plan(
        self,
        meta: Dict[str, Any],
        evidence: List[EvidenceItem],
        relations: List[RelationItem],
    ) -> tuple[dict[str, Any], List[str]]:
        prompt = _PLAN_PROMPT.format(
            repo=getattr(self._bundle.entry, "repo", "this repository"),
            title=meta.get("title", ""),
            summary=meta.get("summary", ""),
            guidance=_page_planning_guidance(meta),
            constraints=_plan_evidence_constraints(meta, evidence, relations),
            evidence=self._evidence_context(evidence),
            relations=self._relations_context(relations),
            wiki_pages=self._wiki_context(str(meta.get("id") or "")),
        )
        allowed = [item.id for item in evidence] + [item.id for item in relations]
        errors: List[str] = []
        try:
            text = self._client().complete(
                [{"role": "user", "content": prompt}],
                # A plan now carries framing, a scan table and up to six
                # sections; 1200 truncated the JSON mid-object, which parsed as
                # "no supported sections" and sent every page through repair.
                max_tokens=3000,
                temperature=0.0,
            )
            plan, errors = parse_fact_plan(text, allowed)
            # Framing and the scan table are validated on their own evidence and
            # are orthogonal to the section repairs that follow. Repairs rewrite
            # sections and can drop them, so hold the first admitted copy and put
            # it back on whichever plan finally wins.
            sticky_blocks = {
                key: copy.deepcopy(plan[key])
                for key in ("purpose", "map", "flow", "see_also")
                if plan.get(key)
            }
            plan = _normalize_plan_support(plan, evidence, relations)
            plan = _renderable_plan(plan, evidence, relations)
        except Exception as exc:  # noqa: BLE001 - use structural fallback
            logger.warning("wiki fact planning failed: %s", exc)
            plan = {
                "thesis": {"statement": "", "evidence": []},
                "sections": [],
            }
            errors = ["model planning unavailable"]
        empty_warning = (
            "page plan has no claims that survived source-support admission; "
            "rebuild it from the concrete symbols and actions in the evidence"
        )
        quality_warnings = (
            _plan_quality_warnings(meta, plan, evidence, relations)
            if plan.get("sections")
            else [empty_warning]
        )
        warnings = [*errors, *quality_warnings]
        best_plan = plan
        best_warnings = warnings
        best_score = _plan_repair_score(plan, warnings)
        catalog_warnings = (
            "page reads as a callable catalog",
            "page plan is dominated by isolated operation sections:",
            "Overview reads as a callable catalog",
        )
        if (
            meta.get("id") == "overview"
            and not relations
            and any(warning.startswith(catalog_warnings) for warning in best_warnings)
        ):
            condensed_plan = _condense_relation_free_overview(best_plan)
            condensed_warnings = [
                *errors,
                *_plan_quality_warnings(
                    meta,
                    condensed_plan,
                    evidence,
                    relations,
                ),
            ]
            condensed_score = _plan_repair_score(
                condensed_plan,
                condensed_warnings,
            )
            if condensed_score < best_score:
                best_plan = condensed_plan
                best_warnings = condensed_warnings
                best_score = condensed_score
                plan = best_plan
                warnings = best_warnings
        supplemented_plan = _supplement_topic_relation_flows(
            meta,
            best_plan,
            relations,
            evidence,
        )
        if supplemented_plan != best_plan:
            supplemented_plan = _normalize_plan_support(
                supplemented_plan,
                evidence,
                relations,
            )
            supplemented_plan = _renderable_plan(
                supplemented_plan,
                evidence,
                relations,
            )
            supplemented_warnings = [
                *errors,
                *_plan_quality_warnings(
                    meta,
                    supplemented_plan,
                    evidence,
                    relations,
                ),
            ]
            supplemented_score = _plan_repair_score(
                supplemented_plan,
                supplemented_warnings,
            )
            if supplemented_score < best_score:
                best_plan = supplemented_plan
                best_warnings = supplemented_warnings
                best_score = supplemented_score
                plan = best_plan
                warnings = best_warnings
        repairs = 0
        hard_warnings = _hard_plan_warnings(best_warnings)
        planning_unavailable = "model planning unavailable" in errors
        while (
            hard_warnings and not planning_unavailable and repairs < _MAX_PLAN_REPAIRS
        ):
            repairs += 1
            repair_prompt = _PLAN_REPAIR_PROMPT.format(
                title=meta.get("title", ""),
                guidance=_page_planning_guidance(meta),
                constraints=_plan_evidence_constraints(
                    meta,
                    evidence,
                    relations,
                ),
                problems=json.dumps(warnings, indent=2),
                plan=json.dumps(plan, indent=2),
                evidence=self._evidence_context(evidence),
                relations=self._relations_context(relations),
            )
            try:
                repaired_text = self._client().complete(
                    [{"role": "user", "content": repair_prompt}],
                    max_tokens=2600,
                    temperature=0.0,
                )
                repaired_plan, repaired_errors = parse_fact_plan(repaired_text, allowed)
                repaired_plan = _normalize_plan_support(
                    repaired_plan,
                    evidence,
                    relations,
                )
                repaired_plan = _renderable_plan(
                    repaired_plan,
                    evidence,
                    relations,
                )
                merged_plan = _merge_fact_plans(best_plan, repaired_plan)
                variants = [repaired_plan, merged_plan]
                variants.extend(
                    _supplement_topic_relation_flows(
                        meta,
                        admitted_plan,
                        relations,
                        evidence,
                    )
                    for admitted_plan in tuple(variants)
                )
                for admitted_plan in variants:
                    admitted_plan = _normalize_plan_support(
                        admitted_plan,
                        evidence,
                        relations,
                    )
                    admitted_plan = _renderable_plan(
                        admitted_plan,
                        evidence,
                        relations,
                    )
                    admitted_quality_warnings = (
                        _plan_quality_warnings(
                            meta,
                            admitted_plan,
                            evidence,
                            relations,
                        )
                        if admitted_plan.get("sections")
                        else [empty_warning]
                    )
                    admitted_warnings = [
                        *repaired_errors,
                        *admitted_quality_warnings,
                    ]
                    admitted_score = _plan_repair_score(
                        admitted_plan,
                        admitted_warnings,
                    )
                    if admitted_score < best_score:
                        best_plan = admitted_plan
                        best_warnings = admitted_warnings
                        best_score = admitted_score
                plan = best_plan
                warnings = best_warnings
                hard_warnings = _hard_plan_warnings(best_warnings)
            except Exception as exc:  # noqa: BLE001 - retain usable first plan
                logger.debug("wiki fact-plan repair unavailable: %s", exc)
                break
        if best_plan.get("sections"):
            for key, value in sticky_blocks.items():
                best_plan.setdefault(key, value)
            return best_plan, best_warnings

        claims = [
            {
                "role": "component",
                "statement": (
                    f"`{item.symbol}` is indexed from `{item.file}`"
                    + (
                        f" at lines {item.start_line}-{item.end_line}"
                        if item.start_line is not None
                        else ""
                    )
                    + "."
                ),
                "evidence": [item.id],
            }
            for item in evidence[:6]
        ]
        thesis_evidence = [evidence[0].id] if evidence else []
        fallback = {
            "thesis": {
                "statement": meta.get("summary", ""),
                "evidence": thesis_evidence,
            },
            "sections": [{"title": "Source-backed components", "claims": claims}],
        }
        fallback = _normalize_plan_support(fallback, evidence, relations)
        fallback = _renderable_plan(fallback, evidence, relations)
        return fallback, best_warnings

    def _rel(self, file: Optional[str]) -> Optional[str]:
        """Normalize an index path to repo-relative — the longest suffix that
        exists under the repo dir (handles ``.codenib/`` and ``/repo/`` roots
        alike). The resolved prefix is cached after the first lookup."""
        if not file:
            return file
        p = file.replace("\\", "/")
        root = getattr(self, "_iroot", None)
        if root is not None:
            return p[len(root) :] if root and p.startswith(root) else p.lstrip("/")
        repo_dir = getattr(self._bundle.entry, "repo_dir", "") or ""
        if repo_dir:
            rd = repo_dir.replace("\\", "/").rstrip("/") + "/"
            if p.startswith(rd):
                self._iroot = rd
                return p[len(rd) :]
            parts = [x for x in p.split("/") if x]
            for i in range(len(parts)):
                rel = "/".join(parts[i:])
                if os.path.exists(os.path.join(repo_dir, rel)):
                    prefix = p[: len(p) - len(rel)]
                    # Only cache a real prefix. An already-relative input (a
                    # sparse hit) matches at i=0 with an empty prefix, and
                    # caching that would make every later absolute path (a
                    # dense hit, indexed under the builder's own root) fall
                    # through unstripped.
                    if prefix:
                        self._iroot = prefix
                    return rel
        mi = p.rfind("/repo/")
        if mi != -1:
            self._iroot = p[: mi + 6]
            return p[mi + 6 :]
        self._iroot = ""
        return p.lstrip("/")

    def _citation(self, node: Any) -> dict:
        start = self._node_attr(node, "start_line")
        end = self._node_attr(node, "end_line")
        return {
            "file": self._rel(self._node_attr(node, "file")),
            "start_line": (start + 1) if isinstance(start, int) else None,
            "end_line": (end + 1) if isinstance(end, int) else None,
            "node_name": self._node_attr(node, "node_name")
            or self._node_attr(node, "name")
            or "",
            "type": self._node_attr(node, "type") or "",
            "score": self._node_attr(node, "score"),
            "content": None,
        }

    def _narrate(
        self,
        meta: Dict[str, Any],
        evidence: List[EvidenceItem],
        relations: List[RelationItem],
        plan: dict[str, Any],
    ) -> tuple[str, bool]:
        prompt = _PAGE_PROMPT.format(
            repo=getattr(self._bundle.entry, "repo", "this repository"),
            title=meta.get("title", ""),
            summary=meta.get("summary", ""),
            plan=json.dumps(plan, indent=2),
            evidence=self._evidence_context(evidence),
            relations=self._relations_context(relations),
        )
        try:
            return (
                _clean_markdown(
                    self._client().complete(
                        [{"role": "user", "content": prompt}],
                        max_tokens=2200,
                        temperature=0.1,
                    )
                ),
                False,
            )
        except Exception as exc:  # noqa: BLE001 - fail soft to a stub page
            logger.warning("wiki page narration failed: %s", exc)
            first = evidence[0]
            return (
                f"{meta.get('summary', '')} [{first.id}]\n\n"
                "## Source evidence\n\n"
                f"`{first.symbol}` is defined in `{first.file}`. [{first.id}]",
                True,
            )

    def _repair_markdown(
        self,
        draft: str,
        report: dict[str, Any],
        evidence: List[EvidenceItem],
        relations: List[RelationItem],
        plan: dict[str, Any],
    ) -> str:
        problems = {
            key: value
            for key, value in report.items()
            if key != "valid" and value not in ([], 0, 0.0, None)
        }
        prompt = _REPAIR_PROMPT.format(
            problems=json.dumps(problems, indent=2),
            plan=json.dumps(plan, indent=2),
            evidence=self._evidence_context(evidence),
            relations=self._relations_context(relations),
            draft=draft,
            required_sections=(report.get("coverage", {}).get("required_sections", 1)),
            required_blocks=report.get("coverage", {}).get("required_blocks", 2),
        )
        try:
            repaired = _clean_markdown(
                self._client().complete(
                    [{"role": "user", "content": prompt}],
                    max_tokens=2200,
                    temperature=0.0,
                )
            )
            return repaired or draft
        except Exception as exc:  # noqa: BLE001 - retain the first usable draft
            logger.warning("wiki grounding repair failed: %s", exc)
            return draft

    def _repair_style(
        self,
        draft: str,
        phrases: Sequence[str],
    ) -> str:
        prompt = _STYLE_REPAIR_PROMPT.format(
            phrases=json.dumps(list(phrases), indent=2),
            draft=draft,
        )
        try:
            repaired = _clean_markdown(
                self._client().complete(
                    [{"role": "user", "content": prompt}],
                    max_tokens=2200,
                    temperature=0.0,
                )
            )
            return repaired or draft
        except Exception as exc:  # noqa: BLE001 - style is a soft constraint
            logger.debug("wiki style repair unavailable: %s", exc)
            return draft

    def _generate_page(self, meta: Dict[str, Any]) -> dict:
        repo_dir = str(getattr(self._bundle.entry, "repo_dir", "") or "").rstrip(os.sep)
        repository_name = (
            os.path.basename(repo_dir)
            or str(getattr(self._bundle.entry, "repo", "") or "")
            or str(getattr(self._bundle.entry, "instance_id", "") or "")
        )
        nodes = self._retrieve(
            meta,
            top_k=(_OVERVIEW_RETRIEVAL_LIMIT if meta.get("id") == "overview" else 12),
        )
        evidence = self._evidence_items(nodes)
        if not evidence:
            return {
                "id": meta["id"],
                "title": meta.get("title", meta["id"]),
                "markdown": (
                    f"# {meta.get('title', '')}\n\n"
                    "_No source evidence was available for this page._"
                ),
                "citations": [],
                "diagram": "",
                "generation": {
                    "mode": "degraded",
                    "model": self._model,
                    "reason": "no_source_evidence",
                },
                "grounding": {
                    "valid": False,
                    "citation_coverage": 0.0,
                    "evidence_count": 0,
                    "relation_count": 0,
                },
            }

        relations = self._relation_items(evidence, meta)
        plan, plan_warnings = self._fact_plan(meta, evidence, relations)
        plan = self._resolve_page_references(plan, str(meta.get("id") or ""))
        model_planning_failed = "model planning unavailable" in plan_warnings
        dense_sections = meta.get("id") == "overview"
        quality_requirements = {
            "require_dense_sections": dense_sections,
            "require_cited_intro": True,
            "require_narrative_novelty": dense_sections,
            "require_narrative_density": True,
            "require_interaction": bool(relations and len(evidence) >= 2),
            "require_grounded_thesis": True,
            "minimum_source_evidence": (min(4, len(evidence)) if dense_sections else 0),
            "required_claim_roles": (("flow",) if dense_sections and relations else ()),
            "relations": relations,
            "evidence_items": evidence,
        }
        structured_render = True
        if structured_render:
            markdown = _fact_plan_markdown(plan, evidence, relations)
            model_failed = not bool(markdown)
            if model_failed and not model_planning_failed:
                markdown, model_failed = self._narrate(
                    meta,
                    evidence,
                    relations,
                    plan,
                )
                structured_render = False
        else:
            markdown, model_failed = self._narrate(
                meta,
                evidence,
                relations,
                plan,
            )
        markdown = _ensure_cited_intro(
            markdown,
            evidence,
            canonical_readme=dense_sections,
            repository_name=repository_name,
        )
        report = grounding_report(markdown, evidence, relations)
        quality = _page_quality_report(
            markdown,
            plan,
            **quality_requirements,
        )
        logger.debug(
            "wiki page candidate %s: grounding=%s quality=%s",
            meta.get("id"),
            report,
            quality,
        )
        best = (markdown, report, quality)
        repaired = False
        if (
            (
                not report["valid"]
                or not quality["valid"]
                or report["promotional_phrases"]
            )
            and not structured_render
            and not model_failed
            and not model_planning_failed
        ):
            style_only = bool(
                report["valid"] and quality["valid"] and report["promotional_phrases"]
            )
            if style_only:
                repaired_markdown = markdown
                remaining_phrases = list(report["promotional_phrases"])
                for _ in range(_MAX_STYLE_REPAIRS):
                    candidate = self._repair_style(
                        repaired_markdown,
                        remaining_phrases,
                    )
                    candidate_report = grounding_report(
                        candidate,
                        evidence,
                        relations,
                    )
                    candidate_phrases = list(candidate_report["promotional_phrases"])
                    if len(candidate_phrases) >= len(remaining_phrases):
                        break
                    repaired_markdown = candidate
                    remaining_phrases = candidate_phrases
                    if not remaining_phrases:
                        break
            else:
                repaired_markdown = self._repair_markdown(
                    markdown,
                    {"grounding": report, "coverage": quality},
                    evidence,
                    relations,
                    plan,
                )
            repaired_markdown = _ensure_cited_intro(
                repaired_markdown,
                evidence,
                canonical_readme=dense_sections,
                repository_name=repository_name,
            )
            repaired = True
            repaired_report = grounding_report(repaired_markdown, evidence, relations)
            if (
                not repaired_report["valid"]
                and repaired_report["citation_coverage"] < 1.0
                and not repaired_report["unknown_citations"]
                and not repaired_report["unknown_files"]
                and not repaired_report["unsupported_identifiers"]
                and not repaired_report["promotional_phrases"]
            ):
                repaired_markdown = _prune_uncited_blocks(repaired_markdown)
                repaired_report = grounding_report(
                    repaired_markdown, evidence, relations
                )
            repaired_markdown = _remove_orphan_headings(repaired_markdown)
            repaired_quality = _page_quality_report(
                repaired_markdown,
                plan,
                **quality_requirements,
            )
            logger.debug(
                "wiki page repaired candidate %s: grounding=%s quality=%s",
                meta.get("id"),
                repaired_report,
                repaired_quality,
            )
            if _candidate_score(repaired_report, repaired_quality) > _candidate_score(
                best[1], best[2]
            ):
                best = (repaired_markdown, repaired_report, repaired_quality)

        fallback_used = any(
            warning.startswith(
                "page plan has no claims that survived source-support admission"
            )
            for warning in plan_warnings
        )
        if not best[1]["valid"] or not best[2]["valid"]:
            fallback = _fact_plan_markdown(plan, evidence, relations)
            if fallback:
                fallback_report = grounding_report(fallback, evidence, relations)
                fallback_quality = _page_quality_report(
                    fallback,
                    plan,
                    **quality_requirements,
                )
                if _candidate_score(
                    fallback_report, fallback_quality
                ) > _candidate_score(best[1], best[2]):
                    best = (fallback, fallback_report, fallback_quality)
                    fallback_used = True

        markdown, report, quality = best
        markdown = _remove_orphan_headings(markdown)
        report = grounding_report(markdown, evidence, relations)
        markdown = _ensure_cited_intro(
            markdown,
            evidence,
            canonical_readme=dense_sections,
            repository_name=repository_name,
        )
        report = grounding_report(markdown, evidence, relations)
        quality = _page_quality_report(
            markdown,
            plan,
            **quality_requirements,
        )
        if (
            report["promotional_phrases"]
            and not structured_render
            and not model_failed
            and not model_planning_failed
        ):
            remaining_phrases = list(report["promotional_phrases"])
            for _ in range(_MAX_STYLE_REPAIRS):
                candidate = self._repair_style(markdown, remaining_phrases)
                candidate = _remove_orphan_headings(
                    _ensure_cited_intro(
                        candidate,
                        evidence,
                        canonical_readme=dense_sections,
                        repository_name=repository_name,
                    )
                )
                candidate_report = grounding_report(
                    candidate,
                    evidence,
                    relations,
                )
                candidate_quality = _page_quality_report(
                    candidate,
                    plan,
                    **quality_requirements,
                )
                candidate_phrases = list(candidate_report["promotional_phrases"])
                if (
                    candidate_report["valid"]
                    and candidate_quality["valid"]
                    and len(candidate_phrases) < len(remaining_phrases)
                ):
                    markdown = candidate
                    report = candidate_report
                    quality = candidate_quality
                    remaining_phrases = candidate_phrases
                    repaired = True
                    if not remaining_phrases:
                        break
                else:
                    break

            if report["promotional_phrases"]:
                candidate = remove_promotional_sentences(markdown)
                candidate = _remove_orphan_headings(
                    _ensure_cited_intro(
                        candidate,
                        evidence,
                        canonical_readme=dense_sections,
                        repository_name=repository_name,
                    )
                )
                candidate_report = grounding_report(
                    candidate,
                    evidence,
                    relations,
                )
                candidate_quality = _page_quality_report(
                    candidate,
                    plan,
                    **quality_requirements,
                )
                if (
                    candidate_report["valid"]
                    and candidate_quality["valid"]
                    and len(candidate_report["promotional_phrases"])
                    < len(report["promotional_phrases"])
                ):
                    markdown = candidate
                    report = candidate_report
                    quality = candidate_quality
                    repaired = True
        markdown = _link_evidence_markers(markdown)
        blocking_plan_warnings = _blocking_plan_warnings(plan_warnings)
        # Publish on the grounding floor, not on a spotless report. Coverage,
        # unresolved identifiers, marketing words and composition notes are all
        # still computed and returned -- they describe the page, and surfacing
        # them is the point. Gating on every one of them made "degraded" fire on
        # pages whose only fault was a shape the checker could not spell.
        publishable = bool(
            report.get("grounded", report["valid"]) and not blocking_plan_warnings
        )
        generated = publishable and not model_failed and not model_planning_failed
        if generated:
            generation_reason = None
        elif model_failed or model_planning_failed:
            generation_reason = "model_unavailable"
        else:
            generation_reason = "quality_guard"

        citations = [
            {
                "file": item.file,
                "start_line": item.start_line,
                "end_line": item.end_line,
                "node_name": item.symbol,
                "type": item.kind,
                "score": None,
                "content": None,
            }
            for item in evidence
        ]
        return {
            "id": meta["id"],
            "title": meta.get("title", meta["id"]),
            "markdown": f"# {meta.get('title', '')}\n\n{markdown}",
            "citations": citations,
            "diagram": "",
            "evidence": evidence_metadata(evidence, relations),
            "generation": {
                "mode": "generated" if generated else "degraded",
                "model": self._model,
                "repaired": repaired,
                "fallback": (
                    "fact_plan" if fallback_used or model_planning_failed else None
                ),
                "renderer": "fact_plan" if structured_render else "narrative",
                "plan_warnings": plan_warnings,
                "reason": generation_reason,
            },
            "grounding": report,
            "quality": quality,
        }

    # -- passthrough -------------------------------------------------------

    def source(self, file: str, start: Optional[int] = None, end: Optional[int] = None):
        return self._wb.source(file, start, end)
