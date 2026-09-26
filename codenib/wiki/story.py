# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Versioned editorial story IR shared by live and static Wiki pages.

The story contract is deliberately smaller than rendered Markdown.  It records
the reader's question, the ordered job of each section, and how source evidence
is allocated.  Rendering remains an adapter concern, so the same IR can later
drive HTML, Markdown, diagrams, or review tooling without asking a model to
rewrite the underlying facts.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Iterable, Mapping, Sequence

from .fences import strip_code_fences

STORY_SCHEMA_VERSION = 1
STORY_BEAT_ROLES = frozenset(
    {
        "orientation",
        "entry",
        "mechanism",
        "decision",
        "handoff",
        "boundary",
        "outcome",
    }
)

_NON_STORY_SECTION_TITLES = frozenset(
    {
        "files",
        "functions",
        "key components",
        "related modules",
        "related pages",
        "source evidence",
    }
)


def _title_key(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _unique_ids(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if re.fullmatch(r"(?:E|R)\d+", item) and item not in result:
            result.append(item)
    return result


def section_evidence_ids(section: Mapping[str, Any]) -> list[str]:
    """Return evidence allocated to one final, renderable section."""

    values: list[Any] = []
    lead = section.get("lead") or {}
    if isinstance(lead, Mapping):
        values.extend(lead.get("evidence") or ())
    for claim in section.get("claims") or ():
        if isinstance(claim, Mapping):
            values.extend(claim.get("evidence") or ())
    excerpt = section.get("excerpt") or {}
    if isinstance(excerpt, Mapping) and excerpt.get("evidence"):
        values.append(excerpt["evidence"])
    return _unique_ids(values)


def infer_story_beat_role(
    section: Mapping[str, Any],
    *,
    index: int,
    total: int,
) -> str:
    """Infer a conservative editorial role for legacy or offline sections."""

    title = _title_key(section.get("title"))
    claims = [
        claim for claim in section.get("claims") or () if isinstance(claim, Mapping)
    ]
    claim_roles = {str(claim.get("role") or "").strip().casefold() for claim in claims}
    claim_text = " ".join(str(claim.get("statement") or "") for claim in claims)
    text = f"{title} {claim_text}".casefold()

    if "flow" in claim_roles or re.search(
        r"\b(?:flow|handoff|pipeline|lifecycle|request path|data path)\b", text
    ):
        return "handoff"
    if "contract" in claim_roles or re.search(
        r"\b(?:contract|boundary|input|output|failure|compatib|invariant)\b", text
    ):
        return "boundary"
    if re.search(r"\b(?:decision|select|choice|route|dispatch|branch)\b", text):
        return "decision"
    if index == 0 and (
        "entry" in claim_roles
        or re.search(r"\b(?:entry|start|setup|install|invoke|command)\b", text)
    ):
        return "entry"
    if index == 0:
        return "orientation"
    if total > 1 and index == total - 1:
        return "outcome"
    return "mechanism"


def _story_budget(
    beats: Sequence[Mapping[str, Any]],
    question: Mapping[str, Any] | None,
    available_ids: Iterable[str],
) -> dict[str, Any]:
    available = _unique_ids(available_ids)
    allocated: list[str] = []
    beat_uses: dict[str, int] = {}
    allocations: list[dict[str, Any]] = []
    for beat in beats:
        ids = _unique_ids(beat.get("evidence") or ())
        allocations.append(
            {
                "section": str(beat.get("section") or ""),
                "evidence": ids,
            }
        )
        for item in ids:
            if item not in allocated:
                allocated.append(item)
            beat_uses[item] = beat_uses.get(item, 0) + 1
    if question:
        for item in _unique_ids(question.get("evidence") or ()):
            if item not in allocated:
                allocated.append(item)

    source_available = [item for item in available if item.startswith("E")]
    source_allocated = [item for item in allocated if item.startswith("E")]
    relation_allocated = [item for item in allocated if item.startswith("R")]
    beat_count = len(beats)
    beats_with_evidence = sum(bool(row["evidence"]) for row in allocations)
    max_beat_reuse = max(beat_uses.values(), default=0)
    return {
        "available_source_evidence": len(source_available),
        "allocated_source_evidence": len(source_allocated),
        "allocated_relations": len(relation_allocated),
        "beat_coverage": round(
            beats_with_evidence / beat_count if beat_count else 0.0,
            3,
        ),
        "max_beat_reuse": round(
            max_beat_reuse / beat_count if beat_count else 0.0,
            3,
        ),
        "unallocated_source_evidence": [
            item for item in source_available if item not in source_allocated
        ],
        "sections": allocations,
    }


def finalize_story_ir(
    plan: Mapping[str, Any],
    *,
    available_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Align a story with the final sections and derive its evidence budget.

    Model-authored beats survive only when their section survives source
    admission.  Missing beats are filled with deterministic editorial metadata,
    but never with invented transition prose.  The ``origin`` field makes that
    degradation visible to quality gates and reviewers.
    """

    result = copy.deepcopy(dict(plan))
    sections = [
        section
        for section in result.get("sections") or ()
        if isinstance(section, Mapping) and _title_key(section.get("title"))
    ]
    raw_story = result.get("story")
    story = copy.deepcopy(raw_story) if isinstance(raw_story, Mapping) else {}
    origin = str(story.get("origin") or "").strip().casefold()
    if origin not in {"planned", "mixed", "inferred", "derived"}:
        origin = "planned" if story else "inferred"

    raw_beats = [
        beat
        for beat in story.get("beats") or ()
        if isinstance(beat, Mapping) and _title_key(beat.get("section"))
    ]
    beats_by_section: dict[str, Mapping[str, Any]] = {}
    for beat in raw_beats:
        beats_by_section.setdefault(_title_key(beat.get("section")), beat)

    beats: list[dict[str, Any]] = []
    filled = False
    for index, section in enumerate(sections):
        title = re.sub(r"\s+", " ", str(section.get("title") or "")).strip()
        existing = beats_by_section.get(_title_key(title))
        if existing is None:
            filled = True
            beat: dict[str, Any] = {
                "section": title,
                "role": infer_story_beat_role(
                    section,
                    index=index,
                    total=len(sections),
                ),
            }
        else:
            role = str(existing.get("role") or "").strip().casefold()
            beat = {
                "section": title,
                "role": (
                    role
                    if role in STORY_BEAT_ROLES
                    else infer_story_beat_role(
                        section,
                        index=index,
                        total=len(sections),
                    )
                ),
            }
            transition = existing.get("transition")
            if isinstance(transition, Mapping):
                statement = re.sub(
                    r"\s+", " ", str(transition.get("statement") or "")
                ).strip()
                ids = _unique_ids(transition.get("evidence") or ())
                if statement and ids:
                    beat["transition"] = {
                        "statement": statement,
                        "evidence": ids,
                    }

        evidence = section_evidence_ids(section)
        transition = beat.get("transition") or {}
        if isinstance(transition, Mapping):
            evidence = _unique_ids([*evidence, *(transition.get("evidence") or ())])
        beat["evidence"] = evidence
        beats.append(beat)

    if filled and origin == "planned":
        origin = "mixed"
    elif not story:
        origin = "inferred"

    final_story: dict[str, Any] = {
        "version": STORY_SCHEMA_VERSION,
        "origin": origin,
        "beats": beats,
    }
    question = story.get("reader_question")
    if isinstance(question, Mapping):
        statement = re.sub(r"\s+", " ", str(question.get("statement") or "")).strip()
        ids = _unique_ids(question.get("evidence") or ())
        if statement and ids:
            final_story["reader_question"] = {
                "statement": statement,
                "evidence": ids,
            }
    final_story["evidence_budget"] = _story_budget(
        beats,
        final_story.get("reader_question"),
        available_ids,
    )
    result["story"] = final_story
    return result


def story_quality_report(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Measure continuity and evidence allocation in a finalized story IR."""

    story = plan.get("story")
    story = story if isinstance(story, Mapping) else {}
    sections = [
        str(section.get("title") or "").strip()
        for section in plan.get("sections") or ()
        if isinstance(section, Mapping) and str(section.get("title") or "").strip()
    ]
    beats = [beat for beat in story.get("beats") or () if isinstance(beat, Mapping)]
    beat_sections = [str(beat.get("section") or "").strip() for beat in beats]
    roles = [str(beat.get("role") or "").strip().casefold() for beat in beats]

    question = story.get("reader_question")
    question_statement = (
        str(question.get("statement") or "").strip()
        if isinstance(question, Mapping)
        else ""
    )
    question_ids = (
        _unique_ids(question.get("evidence") or ())
        if isinstance(question, Mapping)
        else []
    )
    question_valid = bool(
        question_statement
        and question_statement.endswith("?")
        and question_statement.count("?") == 1
        and question_ids
    )
    aligned = [_title_key(item) for item in beat_sections] == [
        _title_key(item) for item in sections
    ]
    unique_sections = len({_title_key(item) for item in beat_sections}) == len(
        beat_sections
    )
    invalid_roles = sorted({role for role in roles if role not in STORY_BEAT_ROLES})
    required_role_count = min(2, len(beats))
    role_progression_valid = len(set(roles)) >= required_role_count
    beats_with_evidence = sum(
        bool(_unique_ids(beat.get("evidence") or ())) for beat in beats
    )
    beat_evidence_coverage = beats_with_evidence / len(beats) if beats else 0.0

    required_transitions = max(0, len(beats) - 1)
    transition_count = 0
    for beat in beats[1:]:
        transition = beat.get("transition")
        if not isinstance(transition, Mapping):
            continue
        if str(transition.get("statement") or "").strip() and _unique_ids(
            transition.get("evidence") or ()
        ):
            transition_count += 1
    transition_coverage = (
        transition_count / required_transitions if required_transitions else 1.0
    )
    minimum_transitions = (required_transitions + 1) // 2

    # Only structural defects block a page: beats that do not match the
    # rendered sections, unknown roles, or a beat with no evidence at all.
    # Whether the story *reads* well (question, varied roles, bridges) is a
    # judgement, not a regex; it is reported as advice and left to the
    # rubric review in ``quality.story_rubric_report``.
    errors = []
    advisories = []
    if not question_valid:
        advisories.append("reader question is missing, uncited, or not one question")
    if not aligned or not unique_sections:
        errors.append("story beats do not align one-to-one with rendered sections")
    if invalid_roles:
        errors.append("story beats contain unsupported editorial roles")
    if not role_progression_valid:
        advisories.append("story beats do not form a varied narrative progression")
    if beat_evidence_coverage < 1.0:
        errors.append("one or more story beats have no allocated evidence")
    if transition_count < minimum_transitions:
        advisories.append("story lacks enough grounded transitions between its beats")

    return {
        "story_valid": bool(beats) and not errors,
        "story_origin": str(story.get("origin") or "none"),
        "story_errors": errors,
        "story_advisories": advisories,
        "story_question_present": bool(question_statement),
        "reader_question_valid": question_valid,
        "story_beat_count": len(beats),
        "story_section_alignment": aligned and unique_sections,
        "story_role_progression_valid": role_progression_valid,
        "story_roles": roles,
        "story_beat_evidence_coverage": round(beat_evidence_coverage, 3),
        "story_transition_coverage": round(transition_coverage, 3),
        "story_minimum_transitions": minimum_transitions,
        "story_evidence_budget": copy.deepcopy(story.get("evidence_budget") or {}),
    }


def derive_story_from_markdown(markdown: str) -> dict[str, Any]:
    """Provide the story schema for deterministic/legacy Wiki renderers."""

    without_fences = strip_code_fences(markdown or "")
    titles = [
        match.group(1).strip()
        for match in re.finditer(r"^##(?!#)\s+(.+?)\s*$", without_fences, re.MULTILINE)
        if _title_key(match.group(1)) not in _NON_STORY_SECTION_TITLES
    ]
    plan = {
        "sections": [{"title": title, "claims": []} for title in titles],
        "story": {"origin": "derived", "beats": []},
    }
    return finalize_story_ir(plan)["story"]


__all__ = [
    "STORY_BEAT_ROLES",
    "STORY_SCHEMA_VERSION",
    "derive_story_from_markdown",
    "finalize_story_ir",
    "infer_story_beat_role",
    "section_evidence_ids",
    "story_quality_report",
]
