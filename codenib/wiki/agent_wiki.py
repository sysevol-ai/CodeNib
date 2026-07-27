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
    remove_promotional_sentences,
)
from .outline import generate_outline

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
_PROMPT_VERSION = "24"


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


def _prose_terms(text: str) -> set[str]:
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


def _duplicate_prose_blocks(markdown: str) -> List[List[int]]:
    """Find paragraph pairs where one largely restates the other."""

    without_fences = re.sub(r"```[\s\S]*?```", "", markdown)
    terms = []
    for raw in re.split(r"\n\s*\n", without_fences):
        block = raw.strip()
        if not block or block.startswith("#"):
            continue
        block_terms = _prose_terms(block)
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


def _page_quality_report(
    markdown: str,
    plan: dict[str, Any],
    *,
    require_dense_sections: bool = False,
) -> dict[str, Any]:
    """Measure whether a page represents the supported fact plan."""

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
    cited = set(re.findall(r"\[((?:E|R)\d+)\]", without_fences))
    covered_claims = sum(
        1 for claim in claims if cited.intersection(claim.get("evidence") or [])
    )
    claim_coverage = covered_claims / len(claims) if claims else 0.0
    duplicate_blocks = _duplicate_prose_blocks(markdown)
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
    required_blocks = 1 + required_sections
    valid = (
        bool(sections)
        and rendered_sections >= required_sections
        and substantive_blocks >= required_blocks
        and claim_coverage >= 0.6
        and not duplicate_blocks
        and not thin_sections
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
    }


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
    """Render an admitted fact plan when free-form narration is incomplete."""

    allowed = {item.id for item in evidence} | {item.id for item in relations}
    intro = _readme_intro(evidence)
    intro_terms = _prose_terms(intro[0]) if intro is not None else set()
    rendered_sections: List[tuple[str, str]] = []
    first_claim: tuple[str, List[str], str] | None = None
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
            if first_claim is None:
                first_claim = (
                    rendered_statement.rstrip(".") + ".",
                    ids,
                    rendered_claim,
                )
            sentences.append(rendered_claim)
        title = re.sub(r"\s+", " ", str(section.get("title") or "")).strip()
        if title and sentences:
            rendered_sections.append((title, " ".join(sentences)))

    if not rendered_sections:
        return ""
    if intro is not None:
        intro_text = f"{intro[0]} [{intro[1]}]"
    elif first_claim is not None:
        statement, ids, rendered_claim = first_claim
        intro_text = statement + " " + " ".join(f"[{item}]" for item in ids)
        title, paragraph = rendered_sections[0]
        if paragraph.startswith(rendered_claim):
            remainder = paragraph[len(rendered_claim) :].lstrip()
            prose = re.sub(r"\[(?:E|R)\d+\]", "", remainder).strip()
            if len(prose) >= 40:
                rendered_sections[0] = (title, remainder)
    else:
        return ""
    blocks = [intro_text]
    for title, paragraph in rendered_sections:
        blocks.extend((f"## {title}", paragraph))
    return "\n\n".join(blocks)


def _candidate_score(
    report: dict[str, Any],
    quality: dict[str, Any],
) -> tuple[int, int, int, int, float]:
    """Rank publishable page candidates with grounding as the hard boundary."""

    return (
        int(bool(report.get("valid"))),
        int(bool(quality.get("valid"))),
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


_PLAN_PROMPT = """\
You are planning one source-grounded page of a developer wiki for the {repo}
codebase.

Page title: {title}
What it should cover: {summary}
Planning guidance: {guidance}

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
            "list. Pair README claims with implementation evidence, use at least "
            "four distinct evidence IDs across the plan when available, and "
            "prefer concrete actions and handoffs over generic 'provides' or "
            "'includes' claims. Treat single-language backends, decoders, "
            "patchers, and private helpers as implementation details unless they "
            "define the repository."
        )
    return (
        "Explain the page's subsystem responsibility, its public entry points, "
        "and interactions supported by the supplied evidence."
    )


def _plan_quality_warnings(
    meta: Dict[str, Any],
    plan: dict[str, Any],
    evidence: List[EvidenceItem],
    relations: Sequence[RelationItem] = (),
) -> List[str]:
    """Validate whether a fact plan is dense enough for its page role."""

    if meta.get("id") != "overview":
        return []

    sections = plan.get("sections") or []
    warnings = []
    if len(sections) != 3:
        warnings.append("Overview must have exactly three complementary sections")

    intro = _readme_intro(evidence)
    intro_terms = _prose_terms(intro[0]) if intro is not None else set()
    require_source_diversity = sum(item.id.startswith("E") for item in evidence) >= 4
    page_sources = set()
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
        if len(useful_claims) < 2:
            warnings.append(
                f"section {title!r} needs two publishable, non-redundant claims"
            )
    if require_source_diversity and len(page_sources) < 4:
        warnings.append("Overview must use at least four source evidence IDs")
    return warnings


_PLAN_REPAIR_PROMPT = """\
Revise the source-grounded fact plan below.

Page title: {title}
Planning guidance: {guidance}

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
- Start with a short, cited intro paragraph (no H1 title; the app adds it).
- Follow the approved fact plan. Explain the subsystem, its key pieces, and
  their interactions in clear prose with ## / ### subheadings.
- Put an evidence marker such as [E1] or [R1] in every substantive paragraph.
- Use `inline code` only for identifiers and paths present in the evidence.
- Do not invent files, symbols, APIs, relationships, or behavior.
- Use factual engineering language. Do not call anything powerful, efficient,
  comprehensive, crucial, user-friendly, invaluable, or productivity-enhancing.
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
planned section instead of deleting sections to fix wording. Rephrase benefit
claims as neutral implementation facts. Remove paragraphs that merely restate
the intro or another section.
The revision must contain at least {required_sections} H2 sections and
{required_blocks} substantive evidence-cited paragraphs. End every substantive
paragraph with its evidence marker(s); do not append unsupported sentences
after a citation.
Do not wrap the response in a Markdown code fence. Return only Markdown.
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
        raw = (
            f"{_PROMPT_VERSION}/{getattr(entry, 'instance_id', 'repo')}@{commit}/"
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
        selected_by_file: Dict[str, List[Any]] = {}
        for node in selected_nodes:
            selected_file = self._norm_hint_path(
                self._rel(self._node_attr(node, "file")) or ""
            )
            selected_by_file.setdefault(selected_file, []).append(node)
        anchors = []
        promoted_node_ids = set()
        anchored_files = set()
        anchor_limit = (
            min(6, max(1, top_k - 2))
            if meta.get("id") == "overview"
            else min(4, max(1, top_k // 2))
        )
        for raw_file in files:
            file = self._norm_hint_path(raw_file)
            if not file or file in anchored_files:
                continue
            matches = selected_by_file.get(file) or []
            if matches:
                node = matches[0]
                promoted_node_ids.add(id(node))
                existing_routes = self._retrieval_routes.get(
                    candidate_key(node, self._node_attr), ()
                )
                self._retrieval_routes[candidate_key(node, self._node_attr)] = tuple(
                    dict.fromkeys(("outline", *existing_routes))
                )
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
            selected_nodes = anchors + [
                node for node in selected_nodes if id(node) not in promoted_node_ids
            ]
        return selected_nodes[:top_k]

    @staticmethod
    def _norm_hint_path(path: str) -> str:
        return path.replace("\\", "/").strip().strip("/")

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
            if quality_warnings:
                repair_prompt = _PLAN_REPAIR_PROMPT.format(
                    title=meta.get("title", ""),
                    guidance=_page_planning_guidance(meta),
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
                    if repaired_plan.get("sections") and len(repaired_warnings) < len(
                        warnings
                    ):
                        plan = repaired_plan
                        warnings = repaired_warnings
                except Exception as exc:  # noqa: BLE001 - retain usable first plan
                    logger.debug("wiki fact-plan repair unavailable: %s", exc)
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
                        temperature=0.2,
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
                    temperature=0.1,
                )
            )
            return repaired or draft
        except Exception as exc:  # noqa: BLE001 - retain the first usable draft
            logger.warning("wiki grounding repair failed: %s", exc)
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
        markdown, model_failed = self._narrate(meta, evidence, relations, plan)
        report = grounding_report(markdown, evidence, relations)
        dense_sections = meta.get("id") == "overview"
        quality = _page_quality_report(
            markdown, plan, require_dense_sections=dense_sections
        )
        logger.debug(
            "wiki page candidate %s: grounding=%s quality=%s",
            meta.get("id"),
            report,
            quality,
        )
        best = (markdown, report, quality)
        repaired = False
        if (not report["valid"] or not quality["valid"]) and not model_failed:
            repaired_markdown = self._repair_markdown(
                markdown,
                {"grounding": report, "coverage": quality},
                evidence,
                relations,
                plan,
            )
            repaired = True
            repaired_report = grounding_report(repaired_markdown, evidence, relations)
            if repaired_report["promotional_phrases"]:
                repaired_markdown = remove_promotional_sentences(repaired_markdown)
                repaired_report = grounding_report(
                    repaired_markdown, evidence, relations
                )
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
                )
                if _candidate_score(
                    fallback_report, fallback_quality
                ) > _candidate_score(best[1], best[2]):
                    best = (fallback, fallback_report, fallback_quality)
                    fallback_used = True

        markdown, report, quality = best
        markdown = _remove_orphan_headings(markdown)
        report = grounding_report(markdown, evidence, relations)
        if markdown.lstrip().startswith("#"):
            intro = _readme_intro(evidence)
            if intro is not None:
                text, evidence_id = intro
                markdown = f"{text} [{evidence_id}]\n\n{markdown}"
                report = grounding_report(markdown, evidence, relations)
        quality = _page_quality_report(
            markdown, plan, require_dense_sections=dense_sections
        )
        markdown = _link_evidence_markers(markdown)
        publishable = bool(report["valid"] and quality["valid"])
        generated = publishable and not model_failed
        if generated:
            generation_reason = None
        elif model_failed:
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
                "fallback": "fact_plan" if fallback_used else None,
                "plan_warnings": plan_warnings,
                "reason": generation_reason,
            },
            "grounding": report,
            "quality": quality,
        }

    # -- passthrough -------------------------------------------------------

    def source(self, file: str, start: Optional[int] = None, end: Optional[int] = None):
        return self._wb.source(file, start, end)
