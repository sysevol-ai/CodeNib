# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Deterministic quality reports for generated repository Wiki pages."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, List

_EVIDENCE_MARKER = re.compile(r"\[((?:E|R)\d+)\]")


def prose_terms(text: str) -> set[str]:
    """Return stable content terms for conservative duplicate detection."""

    stopwords = {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "into",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "then",
        "this",
        "through",
        "to",
        "with",
    }
    plain = re.sub(r"\[((?:E|R)\d+)\](?:\([^)]*\))?", " ", text)
    plain = re.sub(r"[`*_[\]()#>/-]", " ", plain.lower())
    return {
        token
        for token in re.findall(r"[a-z0-9]+", plain)
        if len(token) > 1 and token not in stopwords
    }


def duplicate_prose_blocks(markdown: str) -> List[List[int]]:
    """Find paragraph pairs where one largely restates the other."""

    without_fences = re.sub(r"```[\s\S]*?```", "", markdown)
    terms = []
    for raw in re.split(r"\n\s*\n", without_fences):
        block = raw.strip()
        if not block or block.startswith("#"):
            continue
        block_terms = prose_terms(block)
        if len(block_terms) >= 6:
            terms.append(block_terms)

    duplicates: List[List[int]] = []
    for left in range(len(terms)):
        for right in range(left + 1, len(terms)):
            smaller = min(len(terms[left]), len(terms[right]))
            overlap = len(terms[left] & terms[right]) / smaller
            if overlap >= 0.85:
                duplicates.append([left + 1, right + 1])
    return duplicates


def section_evidence_report(markdown: str) -> dict[str, Any]:
    """Measure whether sections contribute evidence beyond the page intro."""

    without_fences = re.sub(r"```[\s\S]*?```", "", markdown)
    section_matches = list(
        re.finditer(r"^##\s+(.+?)\s*$", without_fences, flags=re.MULTILINE)
    )
    intro_end = section_matches[0].start() if section_matches else len(without_fences)
    intro_evidence = set(_EVIDENCE_MARKER.findall(without_fences[:intro_end]))
    seen = set(intro_evidence)
    evidence_by_section: dict[str, List[str]] = {}
    new_evidence_by_section: dict[str, List[str]] = {}
    sections_without_new_evidence: List[str] = []
    intro_only_sections: List[str] = []

    for index, match in enumerate(section_matches):
        end = (
            section_matches[index + 1].start()
            if index + 1 < len(section_matches)
            else len(without_fences)
        )
        title = match.group(1).strip()
        evidence = set(_EVIDENCE_MARKER.findall(without_fences[match.end() : end]))
        new_evidence = evidence - seen
        evidence_by_section[title] = sorted(evidence)
        new_evidence_by_section[title] = sorted(new_evidence)
        if evidence and not new_evidence:
            sections_without_new_evidence.append(title)
        if intro_evidence and evidence and evidence.issubset(intro_evidence):
            intro_only_sections.append(title)
        seen.update(evidence)

    return {
        "intro_evidence": sorted(intro_evidence),
        "evidence_by_section": evidence_by_section,
        "new_evidence_by_section": new_evidence_by_section,
        "sections_without_new_evidence": sections_without_new_evidence,
        "intro_only_sections": intro_only_sections,
    }


def section_narrative_report(markdown: str) -> dict[str, Any]:
    """Find sections that substantially restate the intro or an earlier section."""

    without_fences = re.sub(r"```[\s\S]*?```", "", markdown)
    section_matches = list(
        re.finditer(r"^##\s+(.+?)\s*$", without_fences, flags=re.MULTILINE)
    )
    intro_end = section_matches[0].start() if section_matches else len(without_fences)
    intro_terms = prose_terms(without_fences[:intro_end])
    prior = [("intro", intro_terms)]
    redundant_sections: List[str] = []
    comparisons: dict[str, dict[str, Any]] = {}

    for index, match in enumerate(section_matches):
        end = (
            section_matches[index + 1].start()
            if index + 1 < len(section_matches)
            else len(without_fences)
        )
        title = match.group(1).strip()
        terms = prose_terms(without_fences[match.end() : end])
        best_against = ""
        best_overlap = 0.0
        for prior_title, prior_terms in prior:
            smaller = min(len(terms), len(prior_terms))
            overlap = len(terms & prior_terms) / smaller if smaller >= 6 else 0.0
            if overlap > best_overlap:
                best_against = prior_title
                best_overlap = overlap
        comparisons[title] = {
            "against": best_against or None,
            "overlap": round(best_overlap, 3),
        }
        if best_overlap >= 0.8:
            redundant_sections.append(title)
        prior.append((title, terms))

    return {
        "redundant_sections": redundant_sections,
        "section_similarity": comparisons,
    }


def page_quality_report(
    markdown: str,
    plan: dict[str, Any],
    *,
    require_dense_sections: bool = False,
    require_cited_intro: bool = False,
    require_narrative_novelty: bool = False,
) -> dict[str, Any]:
    """Measure whether a page represents its supported fact plan."""

    without_fences = re.sub(r"```[\s\S]*?```", "", markdown)
    rendered_sections = len(
        re.findall(r"^#{2,6}\s+\S", without_fences, flags=re.MULTILINE)
    )
    substantive_blocks = 0
    for raw in re.split(r"\n\s*\n", without_fences):
        block = raw.strip()
        if block and not block.startswith("#"):
            plain = re.sub(r"[`*_[\]()#>-]", "", block).strip()
            if len(plain) >= 40:
                substantive_blocks += 1

    sections = plan.get("sections") or []
    claims = [
        claim
        for section in sections
        for claim in section.get("claims") or []
        if claim.get("evidence")
    ]
    cited = set(_EVIDENCE_MARKER.findall(without_fences))
    covered_claims = sum(
        1 for claim in claims if cited.intersection(claim.get("evidence") or [])
    )
    claim_coverage = covered_claims / len(claims) if claims else 0.0
    duplicate_blocks = duplicate_prose_blocks(markdown)
    evidence_report = section_evidence_report(markdown)
    narrative_report = section_narrative_report(markdown)
    first_section = re.search(r"^##\s+\S", without_fences, flags=re.MULTILINE)
    intro_body = without_fences[: first_section.start()] if first_section else ""
    intro_plain = re.sub(r"^#\s+.*$", "", intro_body, flags=re.MULTILINE)
    intro_plain = re.sub(r"\[(?:E|R)\d+\](?:\([^)]*\))?", " ", intro_plain)
    intro_plain = re.sub(r"[`*_[\]()#>-]", " ", intro_plain)
    intro_plain = re.sub(r"\s+", " ", intro_plain).strip()
    cited_intro = bool(evidence_report["intro_evidence"]) and len(intro_plain) >= 40
    thin_sections = []
    if require_dense_sections:
        section_matches = list(
            re.finditer(r"^##\s+(.+?)\s*$", without_fences, flags=re.MULTILINE)
        )
        for index, match in enumerate(section_matches):
            end = (
                section_matches[index + 1].start()
                if index + 1 < len(section_matches)
                else len(without_fences)
            )
            body = without_fences[match.end() : end]
            plain = re.sub(r"\[(?:E|R)\d+\](?:\([^)]*\))?", " ", body)
            plain = re.sub(r"[`*_[\]()#>-]", " ", plain)
            plain = re.sub(r"\s+", " ", plain).strip()
            sentence_count = len(re.findall(r"[.!?](?=\s|$)", plain))
            if len(plain) < 80 or sentence_count < 2:
                thin_sections.append(match.group(1).strip())
    required_sections = min(3, len(sections))
    required_blocks = required_sections + int(require_cited_intro)
    valid = (
        bool(sections)
        and rendered_sections >= required_sections
        and substantive_blocks >= required_blocks
        and claim_coverage >= 0.6
        and not duplicate_blocks
        and not thin_sections
        and (not require_cited_intro or cited_intro)
        and (
            not require_narrative_novelty or not narrative_report["redundant_sections"]
        )
    )
    return {
        "valid": valid,
        "planned_sections": len(sections),
        "required_sections": required_sections,
        "rendered_sections": rendered_sections,
        "substantive_blocks": substantive_blocks,
        "required_blocks": required_blocks,
        "covered_claims": covered_claims,
        "planned_claims": len(claims),
        "claim_coverage": round(claim_coverage, 3),
        "duplicate_blocks": duplicate_blocks,
        "thin_sections": thin_sections,
        "cited_intro": cited_intro,
        "require_cited_intro": require_cited_intro,
        "require_narrative_novelty": require_narrative_novelty,
        **evidence_report,
        **narrative_report,
    }


def audit_page(page: dict[str, Any]) -> dict[str, Any]:
    """Audit one serialized page, including pages produced by older prompts."""

    markdown = str(page.get("markdown") or "")
    grounding = page.get("grounding") or {}
    quality = page.get("quality") or {}
    generation = page.get("generation") or {}
    evidence = section_evidence_report(markdown)
    narrative = section_narrative_report(markdown)
    duplicate_blocks = duplicate_prose_blocks(markdown)
    grounding_valid = bool(grounding.get("valid"))
    structural_valid = bool(quality.get("valid")) or (
        int(quality.get("rendered_sections") or 0)
        >= int(quality.get("required_sections") or 1)
        and int(quality.get("substantive_blocks") or 0)
        >= int(quality.get("required_blocks") or 1)
        and float(quality.get("claim_coverage") or 0.0) >= 0.6
        and not quality.get("thin_sections")
        and not duplicate_blocks
    )
    require_novelty = page.get("id") == "overview"
    narrative_valid = not require_novelty or (
        bool(evidence["intro_evidence"])
        and not narrative["redundant_sections"]
        and not duplicate_blocks
    )
    return {
        "id": page.get("id"),
        "title": page.get("title"),
        "publishable": grounding_valid and structural_valid and narrative_valid,
        "grounding_valid": grounding_valid,
        "structural_valid": structural_valid,
        "narrative_valid": narrative_valid,
        "citation_coverage": float(grounding.get("citation_coverage") or 0.0),
        "claim_coverage": float(quality.get("claim_coverage") or 0.0),
        "repaired": bool(generation.get("repaired")),
        "fallback": generation.get("fallback"),
        "generation_mode": generation.get("mode"),
        "markdown_chars": len(markdown),
        "duplicate_blocks": duplicate_blocks,
        **evidence,
        **narrative,
    }


def summarize_page_audits(pages: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate deterministic quality gates over serialized Wiki pages."""

    details = [audit_page(page) for page in pages]
    count = len(details)
    return {
        "pages": count,
        "publishable": sum(bool(item["publishable"]) for item in details),
        "grounding_valid": sum(bool(item["grounding_valid"]) for item in details),
        "structural_valid": sum(bool(item["structural_valid"]) for item in details),
        "narrative_valid": sum(bool(item["narrative_valid"]) for item in details),
        "repaired": sum(bool(item["repaired"]) for item in details),
        "fallbacks": sum(bool(item["fallback"]) for item in details),
        "details": details,
    }


def audit_cache(cache_dir: str | Path) -> dict[str, Any]:
    """Load and summarize generated page records from an AgentWiki cache."""

    pages = []
    for path in sorted(Path(cache_dir).glob("agentwiki_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict) and data.get("id") and data.get("markdown"):
            pages.append(data)
    return summarize_page_audits(pages)


__all__ = [
    "audit_cache",
    "audit_page",
    "duplicate_prose_blocks",
    "page_quality_report",
    "prose_terms",
    "section_evidence_report",
    "section_narrative_report",
    "summarize_page_audits",
]
