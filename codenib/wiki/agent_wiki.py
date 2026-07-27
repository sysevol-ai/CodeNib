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

import hashlib
import html
import json
import os
import re
from typing import Any, Dict, List, Optional, Sequence

from ..log_utils import get_logger
from .builder import WikiBuilder
from .evidence import (
    EvidenceItem,
    RelationItem,
    candidate_key,
    diversify_by_file,
    evidence_metadata,
    grounding_report,
    parse_fact_plan,
    reciprocal_rank_fuse,
)
from .outline import (
    _is_supporting_file,
    _page_allows_supporting_files,
    generate_outline,
)
from .quality import page_quality_report as _page_quality_report
from .quality import prose_terms as _prose_terms

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
_OUTLINE_PROMPT_VERSION = "3"
_PAGE_PROMPT_VERSION = "43"
_MAX_PLAN_REPAIRS = 2
_MAX_STYLE_REPAIRS = 2


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "page"


def _lang(file: str) -> str:
    return _EXT_LANG.get((file or "").rsplit(".", 1)[-1].lower(), "")


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


def _fact_plan_markdown(
    plan: dict[str, Any],
    evidence: List[EvidenceItem],
    relations: List[RelationItem],
) -> str:
    """Render an admitted fact plan as citation-stable Markdown."""

    allowed = {item.id for item in evidence} | {item.id for item in relations}
    intro = _readme_intro(evidence)
    intro_terms = _prose_terms(intro[0]) if intro is not None else set()
    rendered_sections: List[tuple[str, str]] = []
    for section in plan.get("sections") or []:
        sentences = []
        for claim in section.get("claims") or []:
            statement = re.sub(r"\s+", " ", str(claim.get("statement") or "")).strip()
            ids = [
                str(item)
                for item in claim.get("evidence") or []
                if str(item) in allowed
            ]
            if not statement or not ids:
                continue
            statement_terms = _prose_terms(statement)
            if (
                intro_terms
                and len(statement_terms) >= 4
                and len(statement_terms & intro_terms) / len(statement_terms) >= 0.8
            ):
                continue
            rendered_statement = _format_supported_literals(statement)
            candidate = f"{rendered_statement.rstrip('.')}." + "".join(
                f" [{item}]" for item in ids
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
            rendered_claim = (
                rendered_statement.rstrip(".")
                + ". "
                + " ".join(f"[{item}]" for item in ids)
            )
            sentences.append(rendered_claim)
        title = re.sub(r"\s+", " ", str(section.get("title") or "")).strip()
        if title and sentences:
            rendered_sections.append((title, " ".join(sentences)))

    if not rendered_sections:
        return ""
    blocks = []
    if intro is not None:
        blocks.append(f"{intro[0]} [{intro[1]}]")
    for title, paragraph in rendered_sections:
        blocks.extend((f"## {title}", paragraph))
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
        text = re.sub(r"<!--[\s\S]*?-->", "", item.content)
        for paragraph in re.split(r"\n\s*\n", text):
            lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
            if not lines or any(
                line.startswith(("<", "#", "```", "|", "![", "[![")) for line in lines
            ):
                continue
            prose = " ".join(lines)
            prose = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", prose)
            prose = re.sub(r"[*_`]", "", prose).strip()
            if len(prose.split()) >= 8:
                return _prepare_evidence_content("", prose, limit=600), item.id
    return None


def _ensure_cited_intro(
    markdown: str,
    evidence: List[EvidenceItem],
    *,
    canonical_readme: bool = False,
) -> str:
    """Prepend the README synopsis when a model starts directly with a section."""

    first_section = re.search(r"^##\s+\S", markdown, flags=re.MULTILINE)
    intro = _readme_intro(evidence)
    if canonical_readme and intro is not None:
        text, evidence_id = intro
        sections = markdown[first_section.start() :] if first_section else ""
        return f"{text} [{evidence_id}]\n\n{sections.lstrip()}".rstrip()

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
What it should cover: {summary}
Planning guidance: {guidance}
Evidence allocation: {constraints}

Source evidence:
{evidence}

Static reference relations:
{relations}

Return ONLY JSON with this shape:
{{"thesis":"one concise supported thesis","sections":[{{"title":"Section title",
"claims":[{{"statement":"one concrete claim","evidence":["E1","R1"]}}]}}]}}

Use 2-5 sections and 1-3 claims per section. Every claim must cite one or more
provided evidence IDs. Do not invent files, symbols, APIs, relationships, or
behavior. State implementation facts, not expected benefits or marketing
judgments. Write direct subject-verb-object claims. Avoid benefit language such
as allows, enables, facilitates, efficient, optimize, quick, flexible, easy, or
powerful. Put commands, paths, and code identifiers in backticks. Omit a desired
topic when the evidence does not support it.
"""


def _page_planning_guidance(meta: Dict[str, Any]) -> str:
    if meta.get("id") == "overview":
        return (
            "Build a repository mental model with three complementary sections: "
            "(1) the concrete public workflow from a user entry point to its "
            "result, (2) the main execution or data-flow handoffs, and (3) the "
            "responsibilities of at least two major subsystems. The intro thesis "
            "already states the repository purpose, so do not repeat its channel "
            "list. README evidence may support a distinct public command, while "
            "implementation claims should use source evidence. Use at least "
            "four distinct evidence IDs across the plan when available, and "
            "prefer concrete actions and handoffs over generic 'provides' or "
            "'includes' claims. Describe the public workflow with an actual "
            "command, route, or API from the evidence; never say that users call "
            "a private underscore-prefixed helper. Name major components by "
            "their class, service, or responsibility, never as a filename "
            "subsystem. Treat single-language backends, decoders, "
            "patchers, and private helpers as implementation details unless they "
            "define the repository."
        )
    return (
        "Explain the page's concrete responsibility and interactions supported "
        "by the supplied evidence. Include a public entry point only when the "
        "evidence shows a command, route, class, or non-underscore callable that "
        "serves that role. Otherwise describe the internal control or data flow "
        "without relabeling private helpers as APIs. Attribute behavior only to "
        "the function or class whose cited body implements it; a nearby field, "
        "constructor argument, or static relation does not establish purpose."
    )


def _plan_evidence_constraints(
    meta: Dict[str, Any],
    evidence: Sequence[EvidenceItem],
) -> str:
    """Describe evidence allocation rules before the model writes claims."""

    if meta.get("id") != "overview":
        return "Match each claim to the source item that directly supports it."

    intro = _readme_intro(list(evidence))
    intro_id = intro[1] if intro is not None else None
    implementation = [
        item for item in evidence if item.id != intro_id and item.id.startswith("E")
    ]
    catalog = ", ".join(
        f"{item.id}=`{item.file}`::{item.symbol}" for item in implementation
    )
    reserved = f"{intro_id} supplies the intro; " if intro_id else ""
    return (
        f"{reserved}a section may reuse an evidence item only for a different "
        "supported fact. Use at least four source evidence IDs across the page "
        "when four are available. The public-workflow section may reuse README "
        "evidence; each later section must introduce at least one implementation "
        "source not used by an earlier section. Keep the three sections "
        "semantically distinct and assign sources by their actual responsibility. "
        f"Implementation source catalog: "
        f"{catalog or '(none)'}."
    )


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
    public_evidence = sum(
        not re.search(
            r"(?:^|[:.])_[A-Za-z]\w*(?:\(\))?$",
            item.symbol,
        )
        for item in evidence
    )
    for section in sections:
        title = str(section.get("title") or "untitled")
        if (
            not page_is_about_helpers
            and public_evidence >= 2
            and re.search(
                r"\b(?:helpers?|utilit(?:y|ies))\b",
                title,
                re.IGNORECASE,
            )
        ):
            warnings.append(
                f"section {title!r} elevates incidental helpers over the page's "
                "core responsibility"
            )
        if not re.search(r"\b(?:public|entry\s+points?)\b", title, re.IGNORECASE):
            continue
        for claim in section.get("claims") or []:
            statement = str(claim.get("statement") or "")
            if re.search(
                r"(?:\b[A-Za-z]\w*\.)?_[A-Za-z]\w*\s*\(",
                statement,
            ):
                warning = (
                    f"section {title!r} describes a private helper as a user "
                    "entry point"
                )
                if warning not in warnings:
                    warnings.append(warning)

    if meta.get("id") != "overview":
        return warnings

    if len(sections) != 3:
        warnings.append("Overview must have exactly three complementary sections")

    intro = _readme_intro(evidence)
    intro_terms = _prose_terms(intro[0]) if intro is not None else set()
    require_source_diversity = sum(item.id.startswith("E") for item in evidence) >= 4
    page_sources = set()
    prior_section_terms: List[tuple[str, set[str]]] = []
    seen_section_sources: set[str] = set()
    for section_index, section in enumerate(sections):
        title = str(section.get("title") or "untitled")
        useful_claims = []
        section_sources = set()
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
            if re.search(
                r"\busers?\b[^.]{0,120}`_[A-Za-z]\w*(?:\([^`]*\))?`",
                statement,
                flags=re.IGNORECASE,
            ):
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
            section_sources.update(source_ids)
            page_sources.update(source_ids)
        if len(useful_claims) < 2:
            warnings.append(
                f"section {title!r} needs two publishable, non-redundant claims"
            )
        if (
            section_index > 0
            and section_sources
            and not (section_sources - seen_section_sources)
        ):
            warnings.append(
                f"section {title!r} must introduce an implementation source "
                "not used by earlier sections"
            )
        seen_section_sources.update(section_sources)
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

Return ONLY JSON with the same thesis/sections/claims/evidence shape. Resolve
every listed problem without inventing files, symbols, APIs, relationships, or
behavior. Keep every claim concrete and cite only provided evidence IDs. Use
direct subject-verb-object facts; do not use allows, enables, facilitates,
efficient, optimize, quick, flexible, easy, powerful, or other benefit claims.
Put commands, paths, and code identifiers in backticks.
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
        llm_identity = getattr(self._llm, "cache_identity", "")
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
                f"{getattr(view, 'built_at_epoch', '')}:{config_hash}"
            )
        view_identity = ",".join(view_parts)
        prompt_version = (
            _OUTLINE_PROMPT_VERSION if suffix == "outline" else _PAGE_PROMPT_VERSION
        )
        raw = (
            f"{prompt_version}/{getattr(entry, 'instance_id', 'repo')}@{commit}/"
            f"{self._model}/{self._api_base or ''}/{llm_identity}/"
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
                json.dump({"model": self._model, "data": data}, fh)
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
        cached = self._read_cache(f"page_{page_id}")
        if cached:
            self._pages[page_id] = cached
            return cached
        meta = self._find(page_id)
        if meta is None:
            return None
        page = self._generate_page(meta)
        self._pages[page_id] = page
        self._write_cache(f"page_{page_id}", page)
        return page

    def _retrieve(self, meta: Dict[str, Any], top_k: int = 8) -> List[Any]:
        ensure_views = getattr(self._bundle, "ensure_views", None)
        if callable(ensure_views):
            ensure_views()
        else:
            ensure_runtime = getattr(self._bundle, "ensure_runtime", None)
            if callable(ensure_runtime):
                ensure_runtime()
        files = [f for f in meta.get("files") or [] if isinstance(f, str)]
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
        if not _page_allows_supporting_files(meta):
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
        reranked = self._rerank_for_page(meta, [node for node, _, _ in fused])
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
        overview = meta.get("id") == "overview"
        anchor_limit = min(8, top_k) if overview else min(4, max(1, top_k // 2))
        for raw_file in files:
            file = self._norm_hint_path(raw_file)
            if not file or file in anchored_files:
                continue
            matches = selected_by_file.get(file) or []
            if matches:
                matches = sorted(
                    matches,
                    key=lambda item: self._outline_anchor_rank(meta, item[0]),
                    reverse=True,
                )
                per_file_limit = 1 if overview else 2
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
            ]
            remaining.sort(
                key=lambda node: self._outline_anchor_rank(meta, node),
                reverse=True,
            )
            selected_nodes = anchors + remaining
        return selected_nodes[:top_k]

    @staticmethod
    def _norm_hint_path(path: str) -> str:
        return path.replace("\\", "/").strip().strip("/")

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
    ) -> tuple[int, int, int, int]:
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
        start = self._node_attr(node, "start_line")
        end = self._node_attr(node, "end_line")
        span = (
            max(1, end - start + 1)
            if isinstance(start, int) and isinstance(end, int)
            else 1
        )
        return (
            self._outline_anchor_score(node),
            name_hits,
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
        total = 0
        for node in nodes:
            raw_file = self._node_attr(node, "file") or ""
            file = self._rel(raw_file) or ""
            if not file:
                continue
            start = self._node_attr(node, "start_line")
            end = self._node_attr(node, "end_line")
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
            total += block_size
        return items

    @staticmethod
    def _evidence_context(evidence: List[EvidenceItem]) -> str:
        return "\n\n".join(item.prompt_block() for item in evidence) or "(none)"

    def _relation_items(self, evidence: List[EvidenceItem]) -> List[RelationItem]:
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
            return []
        if not result.get("available"):
            return []
        nodes = {node["id"]: node for node in result.get("nodes") or []}
        relations = []
        for edge in result.get("edges") or []:
            source = nodes.get(edge.get("source"))
            target = nodes.get(edge.get("target"))
            if not source or not target:
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
        return relations

    @staticmethod
    def _relations_context(relations: List[RelationItem]) -> str:
        return "\n".join(item.prompt_line() for item in relations) or "(none)"

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
            constraints=_plan_evidence_constraints(meta, evidence),
            evidence=self._evidence_context(evidence),
            relations=self._relations_context(relations),
        )
        allowed = [item.id for item in evidence] + [item.id for item in relations]
        errors: List[str] = []
        try:
            text = self._client().complete(
                [{"role": "user", "content": prompt}],
                max_tokens=1200,
                temperature=0.1,
            )
            plan, errors = parse_fact_plan(text, allowed)
        except Exception as exc:  # noqa: BLE001 - use structural fallback
            logger.warning("wiki fact planning failed: %s", exc)
            plan = {"thesis": "", "sections": []}
            errors = ["model planning unavailable"]
        if plan.get("sections"):
            quality_warnings = _plan_quality_warnings(meta, plan, evidence, relations)
            warnings = [*errors, *quality_warnings]
            repairs = 0
            while quality_warnings and repairs < _MAX_PLAN_REPAIRS:
                repairs += 1
                repair_prompt = _PLAN_REPAIR_PROMPT.format(
                    title=meta.get("title", ""),
                    guidance=_page_planning_guidance(meta),
                    constraints=_plan_evidence_constraints(meta, evidence),
                    problems=json.dumps(warnings, indent=2),
                    plan=json.dumps(plan, indent=2),
                    evidence=self._evidence_context(evidence),
                    relations=self._relations_context(relations),
                )
                try:
                    repaired_text = self._client().complete(
                        [{"role": "user", "content": repair_prompt}],
                        max_tokens=1400,
                        temperature=0.0,
                    )
                    repaired_plan, repaired_errors = parse_fact_plan(
                        repaired_text, allowed
                    )
                    repaired_warnings = [
                        *repaired_errors,
                        *_plan_quality_warnings(
                            meta, repaired_plan, evidence, relations
                        ),
                    ]
                    if repaired_plan.get("sections") and (
                        len(repaired_warnings) < len(warnings)
                    ):
                        plan = repaired_plan
                        warnings = repaired_warnings
                        quality_warnings = _plan_quality_warnings(
                            meta, plan, evidence, relations
                        )
                    else:
                        break
                except Exception as exc:  # noqa: BLE001 - retain usable first plan
                    logger.debug("wiki fact-plan repair unavailable: %s", exc)
                    break
            return plan, warnings

        claims = [
            {
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
        fallback = {
            "thesis": meta.get("summary", ""),
            "sections": [{"title": "Source-backed components", "claims": claims}],
        }
        return fallback, errors

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
                    self._iroot = p[: len(p) - len(rel)]
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
        nodes = self._retrieve(meta)
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

        relations = self._relation_items(evidence)
        plan, plan_warnings = self._fact_plan(meta, evidence, relations)
        dense_sections = meta.get("id") == "overview"
        structured_render = True
        if structured_render:
            markdown = _fact_plan_markdown(plan, evidence, relations)
            model_failed = not bool(markdown)
            if model_failed:
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
        if dense_sections:
            markdown = _ensure_cited_intro(
                markdown,
                evidence,
                canonical_readme=True,
            )
        report = grounding_report(markdown, evidence, relations)
        quality = _page_quality_report(
            markdown,
            plan,
            require_dense_sections=dense_sections,
            require_cited_intro=dense_sections,
            require_narrative_novelty=dense_sections,
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
            not report["valid"] or not quality["valid"] or report["promotional_phrases"]
        ) and not model_failed:
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
            if dense_sections:
                repaired_markdown = _ensure_cited_intro(
                    repaired_markdown,
                    evidence,
                    canonical_readme=True,
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
                require_dense_sections=dense_sections,
                require_cited_intro=dense_sections,
                require_narrative_novelty=dense_sections,
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

        fallback_used = False
        if not best[1]["valid"] or not best[2]["valid"]:
            fallback = _fact_plan_markdown(plan, evidence, relations)
            if fallback:
                fallback_report = grounding_report(fallback, evidence, relations)
                fallback_quality = _page_quality_report(
                    fallback,
                    plan,
                    require_dense_sections=dense_sections,
                    require_cited_intro=dense_sections,
                    require_narrative_novelty=dense_sections,
                )
                if _candidate_score(
                    fallback_report, fallback_quality
                ) > _candidate_score(best[1], best[2]):
                    best = (fallback, fallback_report, fallback_quality)
                    fallback_used = True

        markdown, report, quality = best
        markdown = _remove_orphan_headings(markdown)
        report = grounding_report(markdown, evidence, relations)
        if dense_sections:
            markdown = _ensure_cited_intro(
                markdown,
                evidence,
                canonical_readme=True,
            )
        report = grounding_report(markdown, evidence, relations)
        quality = _page_quality_report(
            markdown,
            plan,
            require_dense_sections=dense_sections,
            require_cited_intro=dense_sections,
            require_narrative_novelty=dense_sections,
        )
        markdown = _link_evidence_markers(markdown)
        publishable = bool(report["valid"] and quality["valid"])
        planning_failed = "model planning unavailable" in plan_warnings
        generated = publishable and not model_failed and not planning_failed
        if generated:
            generation_reason = None
        elif model_failed or planning_failed:
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
                "fallback": ("fact_plan" if fallback_used or planning_failed else None),
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
