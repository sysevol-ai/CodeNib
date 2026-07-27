# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Deterministic quality reports for generated repository Wiki pages."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, List

from .evidence import (
    describes_private_entry,
    evidence_matches_claim,
    infer_claim_role,
    is_interaction_claim,
    promotional_phrases,
    relation_endpoints_named,
    relation_matches_claim,
)

_EVIDENCE_MARKER = re.compile(r"\[((?:E|R)\d+)\](?:\([^)]*\))?")
_CATALOG_SENTENCE = re.compile(
    r"^(?:the\s+)?(?:(?:class|function|method)\s+)?`[^`]+`"
    r"(?:\s+(?:class|function|method))?\s+"
    r"(?:builds?|constructs?|converts?|creates?|determines?|executes?|extracts?|"
    r"finds?|formats?|generates?|identifies?|initializes?|loads?|organizes?|"
    r"performs?|queries|ranks?|requires?|retrieves?|returns?|runs?|saves?|"
    r"serializes?|sets?|simplifies?|supports?|utilizes?|visualizes?)\b",
    re.IGNORECASE,
)
_RAW_EVIDENCE_ID = re.compile(r"(?<![\w`\[])\b(?:E|R)\d+\b(?![\w`])")
_SOURCE_FILE_SUBSYSTEM = re.compile(
    r"`?[\w./-]+\.(?:py|go|rs|ts|tsx|js|jsx|c|h|cc|cpp|java|rb|php|cs|"
    r"kt|kts)`?\s+(?:file\s+)?subsystem\b",
    re.IGNORECASE,
)
_REFERENCE_NARRATION = re.compile(
    r"\b(?:as (?:shown|indicated) by|according to) (?:the )?"
    r"(?:reference|relation)|"
    r"`[^`\n]+\.(?:py|pyi|go|rs|ts|tsx|js|jsx|c|h|cc|cpp|hpp|java|rb|php|"
    r"cs|kt|kts|swift|scala):\d+(?:-\d+)?`",
    re.IGNORECASE,
)


def _prose_sentences(markdown: str) -> list[str]:
    without_fences = re.sub(r"```[\s\S]*?```", "", markdown)
    without_headings = re.sub(
        r"^#{1,6}\s+.*$",
        "",
        without_fences,
        flags=re.MULTILINE,
    )
    sentences = []
    for sentence in re.split(r"(?<=[.!?])\s+", without_headings):
        cleaned = _EVIDENCE_MARKER.sub("", sentence)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        plain = re.sub(r"[`*_[\]()#>-]", "", cleaned).strip()
        if len(plain) >= 25:
            sentences.append(cleaned)
    return sentences


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


def narrative_density_report(markdown: str) -> dict[str, Any]:
    """Detect pages that enumerate isolated symbols instead of explaining a system."""

    sentences = _prose_sentences(markdown)
    interactions = [
        sentence for sentence in sentences if is_interaction_claim(sentence)
    ]
    catalog = [
        sentence
        for sentence in sentences
        if _CATALOG_SENTENCE.search(sentence) and not is_interaction_claim(sentence)
    ]
    ratio = len(catalog) / len(sentences) if sentences else 0.0
    return {
        "narrative_sentence_count": len(sentences),
        "catalog_sentence_count": len(catalog),
        "catalog_sentence_ratio": round(ratio, 3),
        "interaction_sentence_count": len(interactions),
        "catalog_sentences": catalog,
        "interaction_sentences": interactions,
        "narrative_density_valid": not (len(sentences) >= 4 and ratio > 0.65),
    }


def section_synthesis_report(markdown: str) -> dict[str, Any]:
    """Find pages dominated by sections that only inventory local operations."""

    without_fences = re.sub(r"```[\s\S]*?```", "", markdown)
    section_matches = list(
        re.finditer(r"^##\s+(.+?)\s*$", without_fences, flags=re.MULTILINE)
    )
    isolated_sections: list[str] = []
    section_details: dict[str, dict[str, int]] = {}
    for index, match in enumerate(section_matches):
        end = (
            section_matches[index + 1].start()
            if index + 1 < len(section_matches)
            else len(without_fences)
        )
        title = match.group(1).strip()
        sentences = _prose_sentences(without_fences[match.end() : end])
        catalog_count = sum(bool(_CATALOG_SENTENCE.search(item)) for item in sentences)
        interaction_count = sum(is_interaction_claim(item) for item in sentences)
        section_details[title] = {
            "sentences": len(sentences),
            "catalog_sentences": catalog_count,
            "interactions": interaction_count,
        }
        if (
            len(sentences) >= 2
            and interaction_count == 0
            and catalog_count * 2 >= len(sentences)
        ):
            isolated_sections.append(title)

    section_count = len(section_matches)
    valid = not (section_count >= 3 and len(isolated_sections) * 2 > section_count)
    return {
        "isolated_catalog_sections": isolated_sections,
        "section_synthesis": section_details,
        "section_synthesis_valid": valid,
    }


def section_sentence_redundancy_report(markdown: str) -> dict[str, Any]:
    """Find near-duplicate explanatory sentences inside one section."""

    def redundancy_terms(sentence: str) -> set[str]:
        terms = set()
        for token in prose_terms(sentence) - {"also", "function", "method"}:
            if token.endswith("ies") and len(token) > 4:
                token = token[:-3] + "y"
            elif token.endswith("ing") and len(token) > 6:
                token = token[:-3]
            elif token.endswith("ed") and len(token) > 5:
                token = token[:-2]
            elif token.endswith("s") and not token.endswith("ss") and len(token) > 4:
                token = token[:-1]
            terms.add(token)
        return terms

    without_fences = re.sub(r"```[\s\S]*?```", "", markdown)
    section_matches = list(
        re.finditer(r"^##\s+(.+?)\s*$", without_fences, flags=re.MULTILINE)
    )
    repetitions: list[dict[str, Any]] = []
    for index, match in enumerate(section_matches):
        end = (
            section_matches[index + 1].start()
            if index + 1 < len(section_matches)
            else len(without_fences)
        )
        sentences = _prose_sentences(without_fences[match.end() : end])
        terms = [redundancy_terms(sentence) for sentence in sentences]
        identifiers = [
            {
                re.sub(r"\([^)]*\)$", "", item).casefold()
                for item in re.findall(r"`([^`\n]+)`", sentence)
            }
            for sentence in sentences
        ]
        for left in range(len(sentences)):
            for right in range(left + 1, len(sentences)):
                smaller = min(len(terms[left]), len(terms[right]))
                overlap = (
                    len(terms[left] & terms[right]) / smaller if smaller >= 5 else 0.0
                )
                distinct_handoffs = bool(
                    is_interaction_claim(sentences[left])
                    and is_interaction_claim(sentences[right])
                    and len(identifiers[left]) >= 2
                    and len(identifiers[right]) >= 2
                    and identifiers[left] != identifiers[right]
                )
                if overlap >= 0.7 and not distinct_handoffs:
                    repetitions.append(
                        {
                            "section": match.group(1).strip(),
                            "left": sentences[left],
                            "right": sentences[right],
                            "overlap": round(overlap, 3),
                        }
                    )
    return {
        "redundant_sentence_pairs": repetitions,
        "sentence_redundancy_valid": not repetitions,
    }


def prose_integrity_report(markdown: str) -> dict[str, Any]:
    """Detect prose artifacts that should never reach a published Wiki page."""

    without_citations = _EVIDENCE_MARKER.sub("", markdown)
    citation_only_blocks = []
    for raw in re.split(r"\n\s*\n", markdown):
        if not _EVIDENCE_MARKER.search(raw):
            continue
        plain = _EVIDENCE_MARKER.sub("", raw)
        plain = re.sub(r"[`*_[\]()#>-]", " ", plain)
        if not re.sub(r"\s+", " ", plain).strip():
            citation_only_blocks.append(raw.strip())
    narrated_ids = sorted(set(_RAW_EVIDENCE_ID.findall(without_citations)))
    private_entries = [
        sentence
        for sentence in _prose_sentences(without_citations)
        if describes_private_entry(sentence)
    ]
    section_matches = list(
        re.finditer(r"^##\s+(.+?)\s*$", without_citations, flags=re.MULTILINE)
    )
    for index, match in enumerate(section_matches):
        if not re.search(
            r"\b(?:public|entry\s+points?)\b",
            match.group(1),
            re.IGNORECASE,
        ):
            continue
        end = (
            section_matches[index + 1].start()
            if index + 1 < len(section_matches)
            else len(without_citations)
        )
        for sentence in _prose_sentences(without_citations[match.end() : end]):
            if (
                not is_interaction_claim(sentence)
                and describes_private_entry(sentence, role="entry")
                and sentence not in private_entries
            ):
                private_entries.append(sentence)
    file_subsystems = [
        sentence
        for sentence in _prose_sentences(without_citations)
        if _SOURCE_FILE_SUBSYSTEM.search(sentence)
    ]
    reference_narration = [
        sentence
        for sentence in _prose_sentences(without_citations)
        if _REFERENCE_NARRATION.search(sentence)
    ]
    return {
        "citation_only_blocks": citation_only_blocks,
        "narrated_evidence_ids": narrated_ids,
        "private_entry_sentences": private_entries,
        "file_subsystem_sentences": file_subsystems,
        "reference_narration_sentences": reference_narration,
        "prose_integrity_valid": not (
            citation_only_blocks
            or narrated_ids
            or private_entries
            or file_subsystems
            or reference_narration
        ),
    }


def plan_narrative_report(
    plan: dict[str, Any],
    *,
    require_relation_backing: bool = False,
    relations: Iterable[Any] = (),
    evidence_items: Iterable[Any] = (),
) -> dict[str, Any]:
    """Summarize the semantic roles admitted by a structured fact plan."""

    claims = [
        claim
        for section in plan.get("sections") or []
        for claim in section.get("claims") or []
    ]
    roles: dict[str, int] = {}
    supported_interactions = 0
    invalid_flow_claims: list[str] = []
    invalid_source_claims: list[str] = []
    private_entry_claims: list[str] = []
    multi_sentence_claims: list[str] = []
    relations_by_id = {
        str(getattr(item, "id", "")): item
        for item in relations
        if str(getattr(item, "id", ""))
    }
    evidence_by_id = {
        str(getattr(item, "id", "")): item
        for item in evidence_items
        if str(getattr(item, "id", ""))
    }
    for claim in claims:
        statement = str(claim.get("statement") or "")
        if len(re.findall(r"[.!?](?=\s|$)", statement)) > 1:
            multi_sentence_claims.append(statement)
        role = str(claim.get("role") or infer_claim_role(statement))
        roles[role] = roles.get(role, 0) + 1
        evidence = [str(item) for item in claim.get("evidence") or []]
        if role == "flow":
            explicit = is_interaction_claim(statement)
            cited_relations = [
                relations_by_id[item] for item in evidence if item in relations_by_id
            ]
            cited_evidence = [
                evidence_by_id[item] for item in evidence if item in evidence_by_id
            ]
            relation_backed = any(item.startswith("R") for item in evidence)
            relation_stated = (
                any(relation_matches_claim(statement, item) for item in cited_relations)
                if relations_by_id
                else relation_backed
            )
            source_stated = any(
                evidence_matches_claim(statement, item) for item in cited_evidence
            )
            known_relation_pair = any(
                relation_endpoints_named(statement, item)
                for item in relations_by_id.values()
            )
            support_catalog_available = bool(relations_by_id or evidence_by_id)
            if support_catalog_available:
                supported = explicit and (
                    (relation_backed and relation_stated)
                    or (
                        not relation_backed
                        and not known_relation_pair
                        and source_stated
                    )
                )
            else:
                supported = explicit and (
                    not require_relation_backing or relation_backed
                )
            if supported:
                supported_interactions += 1
            else:
                invalid_flow_claims.append(statement)
        elif not any(item.startswith("E") for item in evidence):
            invalid_source_claims.append(statement)
        if describes_private_entry(statement, role=role):
            private_entry_claims.append(statement)

    thesis = plan.get("thesis") or {}
    thesis_evidence = (
        list(thesis.get("evidence") or []) if isinstance(thesis, dict) else []
    )
    thesis_statement = (
        str(thesis.get("statement") or "") if isinstance(thesis, dict) else ""
    )
    thesis_sentence_count = len(re.findall(r"[.!?](?=\s|$)", thesis_statement))
    thesis_grounded = bool(
        isinstance(thesis, dict)
        and thesis_statement.strip()
        and any(str(item).startswith("E") for item in thesis_evidence)
    )
    component_claims = roles.get("component", 0)
    component_ratio = component_claims / len(claims) if claims else 0.0
    component_dominated = bool(len(claims) >= 4 and component_ratio > 0.65)
    return {
        "claim_roles": roles,
        "supported_interaction_claims": supported_interactions,
        "invalid_flow_claims": invalid_flow_claims,
        "invalid_source_claims": invalid_source_claims,
        "private_entry_claims": private_entry_claims,
        "multi_sentence_claims": multi_sentence_claims,
        "thesis_sentence_count": thesis_sentence_count,
        "component_claim_ratio": round(component_ratio, 3),
        "component_dominated": component_dominated,
        "plan_role_integrity_valid": not (
            invalid_flow_claims
            or invalid_source_claims
            or private_entry_claims
            or multi_sentence_claims
            or thesis_sentence_count > 1
            or component_dominated
        ),
        "thesis_grounded": thesis_grounded,
    }


def page_quality_report(
    markdown: str,
    plan: dict[str, Any],
    *,
    require_dense_sections: bool = False,
    require_cited_intro: bool = False,
    require_narrative_novelty: bool = False,
    require_narrative_density: bool = False,
    require_interaction: bool = False,
    require_grounded_thesis: bool = False,
    minimum_source_evidence: int = 0,
    required_claim_roles: Iterable[str] = (),
    relations: Iterable[Any] = (),
    evidence_items: Iterable[Any] = (),
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
    density_report = narrative_density_report(markdown)
    synthesis_report = section_synthesis_report(markdown)
    sentence_report = section_sentence_redundancy_report(markdown)
    integrity_report = prose_integrity_report(markdown)
    plan_report = plan_narrative_report(
        plan,
        require_relation_backing=require_interaction,
        relations=relations,
        evidence_items=evidence_items,
    )
    first_section = re.search(r"^##\s+\S", without_fences, flags=re.MULTILINE)
    intro_body = without_fences[: first_section.start()] if first_section else ""
    intro_plain = re.sub(r"^#\s+.*$", "", intro_body, flags=re.MULTILINE)
    intro_plain = re.sub(r"\[(?:E|R)\d+\](?:\([^)]*\))?", " ", intro_plain)
    intro_plain = re.sub(r"[`*_[\]()#>-]", " ", intro_plain)
    intro_plain = re.sub(r"\s+", " ", intro_plain).strip()
    cited_intro = bool(evidence_report["intro_evidence"]) and len(intro_plain) >= 40
    source_evidence_count = len({item for item in cited if str(item).startswith("E")})
    required_roles = tuple(dict.fromkeys(str(role) for role in required_claim_roles))
    missing_claim_roles = [
        role for role in required_roles if not plan_report["claim_roles"].get(role)
    ]
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
            if len(plain) < 60:
                thin_sections.append(match.group(1).strip())
    required_sections = 3 if require_dense_sections else min(3, len(sections))
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
        and (not require_narrative_density or density_report["narrative_density_valid"])
        and (
            not require_narrative_density or synthesis_report["section_synthesis_valid"]
        )
        and integrity_report["prose_integrity_valid"]
        and sentence_report["sentence_redundancy_valid"]
        and plan_report["plan_role_integrity_valid"]
        and (not require_grounded_thesis or plan_report["thesis_grounded"])
        and source_evidence_count >= minimum_source_evidence
        and not missing_claim_roles
        and (
            not require_interaction
            or (
                plan_report["supported_interaction_claims"] >= 1
                and density_report["interaction_sentence_count"] >= 1
            )
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
        "require_narrative_density": require_narrative_density,
        "require_interaction": require_interaction,
        "require_grounded_thesis": require_grounded_thesis,
        "minimum_source_evidence": minimum_source_evidence,
        "source_evidence_count": source_evidence_count,
        "required_claim_roles": list(required_roles),
        "missing_claim_roles": missing_claim_roles,
        **evidence_report,
        **narrative_report,
        **density_report,
        **synthesis_report,
        **sentence_report,
        **integrity_report,
        **plan_report,
    }


def audit_page(page: dict[str, Any]) -> dict[str, Any]:
    """Audit one serialized page, including pages produced by older prompts."""

    markdown = str(page.get("markdown") or "")
    grounding = page.get("grounding") or {}
    quality = page.get("quality") or {}
    generation = page.get("generation") or {}
    serialized_evidence = page.get("evidence") or {}
    relation_count = len(serialized_evidence.get("relations") or [])
    evidence_count = len(serialized_evidence.get("items") or [])
    evidence = section_evidence_report(markdown)
    narrative = section_narrative_report(markdown)
    density = narrative_density_report(markdown)
    synthesis = section_synthesis_report(markdown)
    sentences = section_sentence_redundancy_report(markdown)
    integrity = prose_integrity_report(markdown)
    duplicate_blocks = duplicate_prose_blocks(markdown)
    grounding_valid = bool(grounding.get("valid"))
    current_promotional_phrases = promotional_phrases(markdown)
    style_valid = not current_promotional_phrases
    if "valid" in quality:
        structural_valid = bool(quality.get("valid"))
    else:
        structural_valid = bool(
            int(quality.get("rendered_sections") or 0)
            >= int(quality.get("required_sections") or 1)
            and int(quality.get("substantive_blocks") or 0)
            >= int(quality.get("required_blocks") or 1)
            and float(quality.get("claim_coverage") or 0.0) >= 0.6
            and not quality.get("thin_sections")
            and not duplicate_blocks
        )
    require_novelty = page.get("id") == "overview"
    require_interaction = bool(
        quality.get("require_interaction") or (relation_count and evidence_count >= 2)
    )
    interaction_valid = (
        not require_interaction or density["interaction_sentence_count"] >= 1
    )
    narrative_valid = (
        density["narrative_density_valid"]
        and synthesis["section_synthesis_valid"]
        and sentences["sentence_redundancy_valid"]
        and integrity["prose_integrity_valid"]
        and interaction_valid
        and (
            not require_novelty
            or (
                bool(evidence["intro_evidence"])
                and not narrative["redundant_sections"]
                and not duplicate_blocks
            )
        )
    )
    return {
        "id": page.get("id"),
        "title": page.get("title"),
        "publishable": (
            grounding_valid and style_valid and structural_valid and narrative_valid
        ),
        "grounding_valid": grounding_valid,
        "style_valid": style_valid,
        "promotional_phrases": current_promotional_phrases,
        "structural_valid": structural_valid,
        "narrative_valid": narrative_valid,
        "interaction_valid": interaction_valid,
        "require_interaction": require_interaction,
        "citation_coverage": float(grounding.get("citation_coverage") or 0.0),
        "claim_coverage": float(quality.get("claim_coverage") or 0.0),
        "repaired": bool(generation.get("repaired")),
        "fallback": generation.get("fallback"),
        "generation_mode": generation.get("mode"),
        "markdown_chars": len(markdown),
        "duplicate_blocks": duplicate_blocks,
        **evidence,
        **narrative,
        **density,
        **synthesis,
        **sentences,
        **integrity,
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
    "narrative_density_report",
    "page_quality_report",
    "plan_narrative_report",
    "prose_integrity_report",
    "prose_terms",
    "section_evidence_report",
    "section_narrative_report",
    "section_sentence_redundancy_report",
    "section_synthesis_report",
    "summarize_page_audits",
]
