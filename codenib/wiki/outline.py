# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Stage 1 of the agent wiki pipeline: a high-level *conceptual* outline.

Instead of one page per directory, an LLM reads the repo's README, its salient
files, and its top symbols and proposes a DeepWiki-style **subsystem** table of
contents (e.g. "Request Pipeline", "Interceptors", "Adapters & Transports") —
concepts, not paths. Each proposed page carries ``keywords`` + ``files`` that
stage 2 uses to retrieve the relevant code and write the page.

This module only produces the outline (the page tree); per-page generation is
a separate stage so the outline can be reviewed first.
"""

from __future__ import annotations

import copy
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List

from ..log_utils import get_logger
from ..repository_filters import walk_repository_files
from ..types import is_symbol_node
from .builder import Symbol, WikiBuilder
from .evidence import promotional_phrases

logger = get_logger(__name__)

_README_NAMES = ("README.md", "README.rst", "README.txt", "README", "readme.md")
_MAX_OUTLINE_PAGES = 10
_MAX_CHILDREN = 3
_MAX_OUTLINE_PLAN_REPAIRS = 3
_MAX_OUTLINE_STYLE_REPAIRS = 2
_SOURCE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".kts",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".scala",
    ".swift",
    ".ts",
    ".tsx",
}
_DOCUMENTED_ENTRY_SUFFIXES = _SOURCE_SUFFIXES | {".bash", ".sh"}
_README_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.@+-])"
    r"(?:\.{0,2}/)?(?:[A-Za-z0-9_.@+-]+/)*[A-Za-z0-9_.@+-]+"
    r"\.(?:bash|c|cc|cpp|cs|go|h|hpp|java|js|jsx|kt|kts|php|py|rb|rs|"
    r"scala|sh|swift|ts|tsx)"
    r"(?=$|[^A-Za-z0-9_.@+/-])",
    re.IGNORECASE,
)
_SUPPORTING_SEGMENTS = {
    "benchmark",
    "benchmarks",
    "docs",
    "eval",
    "evaluation",
    "examples",
    "fixtures",
    "scripts",
    "test",
    "tests",
}
_OVERVIEW_ROLE_WEIGHTS = {
    "__main__": 14,
    "cli": 14,
    "main": 12,
    "app": 12,
    "server": 12,
    "service": 11,
    "index_compiler": 11,
    "compiler": 10,
    "manifest": 10,
    "runtime": 9,
    "router": 9,
    "runner": 8,
    "orchestrator": 8,
    "builder": 7,
    "api": 7,
    "config": 5,
}
_LOW_LEVEL_OVERVIEW_TOKENS = {
    "decode",
    "decoder",
    "generated",
    "patch",
    "patcher",
    "protocol",
}
_SUPPORTING_PAGE_TOKENS = {
    "benchmark",
    "benchmarks",
    "docs",
    "documentation",
    "eval",
    "evaluation",
    "evaluations",
    "example",
    "examples",
    "fixture",
    "fixtures",
    "script",
    "scripts",
    "test",
    "testing",
    "tests",
    "workflow",
    "workflows",
}
_META_SUMMARY_RE = re.compile(
    r"^(?:this\s+(?:page|section|chapter)\s+)?"
    r"(?:covers?|describes?|details?|explains?|focuses?\s+on|outlines?)\b",
    re.IGNORECASE,
)
_GENERIC_BENEFIT_RE = re.compile(
    r"\b(?:aid(?:s|ed|ing)?|allow(?:s|ed|ing)|ensur(?:e|es|ed|ing)|"
    r"improv(?:e|es|ed|ing)|"
    r"provid(?:e|es|ed|ing)\s+[^.]{0,80}\b"
    r"(?:capabilit(?:y|ies)|effectiveness|insights?)|"
    r"support(?:s|ed|ing)\s+[^.]{0,80}\bunderstanding)\b",
    re.IGNORECASE,
)


def _is_supporting_file(file: str) -> bool:
    return bool(
        {segment.lower() for segment in Path(file).parts[:-1]} & _SUPPORTING_SEGMENTS
    )


def _page_allows_supporting_files(page: Dict[str, Any]) -> bool:
    values = [
        str(page.get("id") or ""),
        str(page.get("title") or ""),
    ]
    terms = set(re.findall(r"[a-z0-9_]{4,}", " ".join(values).lower()))
    return bool(terms & _SUPPORTING_PAGE_TOKENS)


def _read_readme(repo_dir: str, limit: int = 3500) -> str:
    for name in _README_NAMES:
        path = os.path.join(repo_dir, name)
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    return fh.read()[:limit]
            except OSError:
                return ""
    return ""


def _readme_entry_files(
    repo_dir: str,
    readme: str,
    *,
    limit: int = 12,
) -> List[str]:
    """Resolve source paths explicitly named by a repository README."""

    known = {
        path.relative_to(repo_dir).as_posix()
        for path in walk_repository_files(repo_dir)
        if path.suffix.lower() in _DOCUMENTED_ENTRY_SUFFIXES
    }
    by_basename: dict[str, List[str]] = defaultdict(list)
    for file in known:
        by_basename[Path(file).name].append(file)

    resolved = []
    for match in _README_PATH_RE.finditer(readme):
        candidate = match.group(0).replace("\\", "/").lstrip("./")
        file = candidate if candidate in known else ""
        if not file:
            suffix_matches = [
                known_file
                for known_file in known
                if known_file.endswith("/" + candidate)
            ]
            if len(suffix_matches) == 1:
                file = suffix_matches[0]
        if not file and "/" not in candidate:
            basename_matches = by_basename.get(candidate, [])
            if len(basename_matches) == 1:
                file = basename_matches[0]
        if file and file not in resolved:
            resolved.append(file)
            if len(resolved) >= limit:
                break
    return resolved


def _top_symbols(symbols: List[Symbol], limit: int = 70) -> List[Symbol]:
    """Rank symbols by API surface with deterministic ties."""

    return sorted(
        symbols,
        key=lambda symbol: (-symbol.lines, symbol.file, symbol.name),
    )[:limit]


def _top_files(symbols: List[Symbol], limit: int) -> List[str]:
    """Rank files by indexed symbol surface."""

    weights: Counter[str] = Counter()
    for symbol in symbols:
        weights[symbol.file] += symbol.lines
    return [
        file
        for file, _ in sorted(
            weights.items(),
            key=lambda item: (-item[1], item[0]),
        )[:limit]
    ]


def _repository_structure(repo_dir: str, limit: int = 16) -> str:
    counts: Counter[str] = Counter()
    for path in walk_repository_files(repo_dir):
        rel = path.relative_to(repo_dir).as_posix()
        parts = rel.split("/")
        area = "/".join(parts[:2]) if len(parts) > 2 else parts[0]
        counts[area] += 1
    if not counts:
        return "(none)"
    return "\n".join(
        f"- {area}: {count} files" for area, count in counts.most_common(limit)
    )


def _overview_file_score(file: str) -> int:
    """Rank files that explain a repository's public execution path."""

    path = Path(file)
    segments = {segment.lower() for segment in path.parts[:-1]}
    if segments & _SUPPORTING_SEGMENTS or path.suffix.lower() not in _SOURCE_SUFFIXES:
        return -100

    stem = path.stem.lower()
    tokens = set(re.findall(r"[a-z0-9]+", stem))
    score = _OVERVIEW_ROLE_WEIGHTS.get(stem, 0)
    score += max(
        (_OVERVIEW_ROLE_WEIGHTS.get(token, 0) for token in tokens),
        default=0,
    )
    score += max(0, 4 - len(path.parts))
    if stem.startswith("_") and stem != "__main__":
        score -= 5
    if tokens & _LOW_LEVEL_OVERVIEW_TOKENS:
        score -= 8
    return score


def _view_summary(bundle: Any) -> str:
    indexes = getattr(getattr(bundle, "manifest", None), "indexes", {})
    if not indexes:
        return "(none)"
    return "\n".join(
        f"- {name}: {getattr(entry, 'status', 'unknown')}"
        for name, entry in sorted(indexes.items())
    )


def _graph_communities(bundle: Any, limit: int = 8) -> str:
    """Summarize central symbol clusters without exposing graph identities."""

    try:
        graph = bundle.code_graph()
        if graph is None:
            return "(graph unavailable)"
        raw = graph.get_graph()
        symbol_vids = [
            vertex.index
            for vertex in raw.vs
            if is_symbol_node(vertex.attributes().get("type"))
            and vertex.attributes().get("unified_name")
        ]
        if len(symbol_vids) < 3:
            return "(no symbol communities)"
        degree = raw.degree(symbol_vids, mode="all")
        ranked_vids = [
            vid
            for _, vid in sorted(
                zip(degree, symbol_vids, strict=True),
                key=lambda item: (-item[0], item[1]),
            )[:400]
        ]
        subgraph = raw.subgraph(ranked_vids)
        reference_edges = [
            edge.index
            for edge in subgraph.es
            if edge.attributes().get("type") == "reference"
        ]
        if not reference_edges:
            return "(no reference communities)"
        ref_graph = subgraph.subgraph_edges(reference_edges, delete_vertices=False)
        undirected = ref_graph.as_undirected(mode="collapse")
        clusters = undirected.community_leiden(
            objective_function="modularity",
            n_iterations=-1,
        )
        by_cluster: dict[int, List[tuple[int, str]]] = defaultdict(list)
        ref_degree = ref_graph.degree(mode="all")
        for index, cluster in enumerate(clusters.membership):
            label = ref_graph.vs[index].attributes().get("unified_name")
            if label:
                by_cluster[int(cluster)].append((ref_degree[index], label))
        ranked_clusters = sorted(
            by_cluster.values(),
            key=lambda values: -sum(degree for degree, _ in values),
        )
        lines = []
        for index, values in enumerate(ranked_clusters[:limit], start=1):
            labels = [
                label
                for _, label in sorted(
                    values,
                    key=lambda item: (-item[0], item[1]),
                )[:5]
            ]
            lines.append(f"- cluster {index}: {', '.join(labels)}")
        return "\n".join(lines) or "(no symbol communities)"
    except Exception as exc:  # noqa: BLE001 - graph context is optional
        logger.debug("outline graph summary unavailable: %s", exc)
        return "(graph unavailable)"


_OUTLINE_PROMPT = """\
You are documenting the {repo} codebase like a senior engineer writing a \
developer wiki for new contributors.

Propose a HIGH-LEVEL, CONCEPTUAL table of contents organized by real subsystem \
or capability, never by directory or file. Derive every concept from the \
supplied file paths or symbol names; do not copy categories from a generic \
software-architecture template.

Repository: {repo}   (languages: {languages})

README (excerpt):
{readme}

Core implementation files:
{core_files}

Supporting evaluation, test, example, script, and documentation files:
{supporting_files}

README-declared source entrypoints:
{documented_files}

Repository structure:
{structure}

Core symbols (name — file — kind):
{core_symbols}

Supporting symbols (name — file — kind):
{supporting_symbols}

Central dependency communities:
{communities}

Available repository views:
{views}

Return ONLY JSON, no prose, of exactly this shape. Aim for 5-8 top-level pages \
that together cover the WHOLE system, and give the major subsystems 1-3 children \
so the tree is genuinely TWO LEVELS deep — not a flat list. Start with "Overview".
{{"pages":[{{"id":"kebab-case-id","title":"Concept Title","summary":"1-2 \
sentences on what this page explains","keywords":["specific","symbol or \
feature","search","terms"],"files":["likely/relevant/path.ext"],"children":[\
{{"id":"child-id","title":"Sub-topic","summary":"...","keywords":["..."],\
"files":["..."],"children":[]}}]}}]}}

Rules:
- Structure it like a senior engineer's onboarding docs: top-level = major areas; \
children = specific capabilities or flows inside them.
- Overview is the landing page and must have no children. Major subsystems such \
as indexing, serving, navigation, or agent runtime belong at the top level when \
the supplied repository evidence supports them.
- Include a cross-cutting page only when a supplied path or symbol explicitly \
names and implements that concern.
- Titles are CONCEPTS, not paths (no "lib/core", no file names as titles).
- Each summary must state a concrete responsibility, entry path, handoff, or
  boundary. Do not write "this page/section explains/covers/describes", and do
  not use marketing adjectives such as comprehensive, advanced, powerful,
  efficient, easy, or flexible.
- keywords drive a code search, so make them specific symbol/feature terms.
- Every page and child MUST name 1-4 exact files from the supplied evidence.
- A README-declared source entrypoint is a real user-facing workflow even when
  it lives under tests, examples, or scripts. Group such files under a concrete
  workflow title grounded in their path or symbols, rather than treating them
  as generic test infrastructure.
- Keep supporting evaluation, test, example, script, and documentation files
  out of core subsystem pages. Use them only for a page whose title explicitly
  names that supporting concern or a README-declared workflow.
- At least one named file or symbol must lexically anchor the page title. A real \
but unrelated file does not make an invented concept valid.
- Make Overview a product mental model: purpose, user entry points, the main
  execution/data flow, and the major subsystems. Do not elevate one language
  backend, decoder, patcher, or private helper unless it defines the repository.
- Omit generic sections that lack concrete evidence. Do not add installation,
  testing, plugins, communication, or error-handling merely because most
  projects have them.
- Cover the whole system, not just a few files; prefer depth over a flat list.
"""

_OUTLINE_REPAIR_PROMPT = """\
{original_prompt}

The previous attempt retained only {accepted} source-anchored top-level pages,
but this repository needs at least {required}. Return a COMPLETE replacement
outline, not a patch. Keep the accepted concepts when useful, add distinct
major subsystems from the supplied paths and symbols, and do not attach a real
but unrelated file to justify a generic category.

Quality problems:
{problems}

Previously retained outline:
{outline}
"""

_OUTLINE_REFINEMENT_PROMPT = """\
{original_prompt}

Produce one alternative COMPLETE outline for the same repository. The previous
outline already passed the minimum breadth check; improve source coverage by
representing distinct major user-facing and implementation areas from the
supplied paths and symbols. Do not add generic categories, duplicate an
existing responsibility under another name, or attach unrelated real files.

Quality problems:
{problems}

Previous outline:
{outline}
"""

_OUTLINE_STYLE_REPAIR_PROMPT = """\
Rewrite only the flagged summary strings below. Each path is an opaque tree
address and must be copied unchanged. A repaired summary must state the
component's concrete responsibility, input/output, or handoff using only the
title, keywords, and files in its record. Delete an unsupported benefit clause
instead of replacing it with a synonym.

Do not use "this page/section explains/covers/describes" or benefit language
such as allows, enables, facilitates, improves, aids, ensures, comprehensive,
advanced, powerful, efficient, easy, or flexible.

Flagged summaries:
{summaries}

Return ONLY this JSON shape:
{{"summaries":[{{"path":"0/1","summary":"concrete replacement"}}]}}
"""


def _format_symbols(symbols: List[Symbol]) -> str:
    return "\n".join(f"- {s.name} — {s.file} — {s.type}" for s in symbols)


def generate_outline(
    bundle: Any,
    model: str,
    # A large repo's outline (sympy, matplotlib) truncates at 4096, which
    # silently degrades the page tree instead of failing. Restored from the
    # closed PR #327 ("sympy dense embeddings + wiki outline budget").
    max_tokens: int = 16000,
    *,
    llm: Any = None,
) -> Dict[str, Any]:
    """Produce a conceptual wiki page tree for *bundle* using *model*.

    Returns ``{"pages": [...]}``. On a model/parse failure returns
    ``{"pages": [], "raw": <text>, "error": <msg>}`` so callers can inspect.
    """
    wb = WikiBuilder(bundle)
    symbols = list(wb._symbols())
    core_symbols = [
        symbol for symbol in symbols if not _is_supporting_file(symbol.file)
    ]
    supporting_symbols = [
        symbol for symbol in symbols if _is_supporting_file(symbol.file)
    ]
    core_files = _top_files(core_symbols, limit=40)
    supporting_files = _top_files(supporting_symbols, limit=12)
    repo_dir = getattr(bundle.entry, "repo_dir", "") or ""
    readme = _read_readme(repo_dir)
    documented_files = _readme_entry_files(repo_dir, readme)
    files = list(dict.fromkeys([*core_files, *documented_files, *supporting_files]))
    languages = ", ".join(getattr(bundle.manifest, "languages", []) or [])

    prompt = _OUTLINE_PROMPT.format(
        repo=getattr(bundle.entry, "repo", "this repository"),
        languages=languages or "unknown",
        readme=readme or "(no README found)",
        core_files="\n".join(f"- {f}" for f in core_files) or "(none)",
        supporting_files=("\n".join(f"- {f}" for f in supporting_files) or "(none)"),
        documented_files=(
            "\n".join(
                f"- {file}" + (" (primary quickstart entry)" if index == 0 else "")
                for index, file in enumerate(documented_files)
            )
            or "(none)"
        ),
        structure=_repository_structure(repo_dir),
        core_symbols=_format_symbols(_top_symbols(core_symbols, limit=60)) or "(none)",
        supporting_symbols=_format_symbols(_top_symbols(supporting_symbols, limit=10))
        or "(none)",
        communities=_graph_communities(bundle),
        views=_view_summary(bundle),
    )

    try:
        if llm is None:
            from ..llm.litellm_chat import LiteLLMChat

            llm = LiteLLMChat(model=model, temperature=0.2, max_tokens=max_tokens)
        text = llm.complete(
            [{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.1,
        )
    except Exception as exc:  # noqa: BLE001 - surface a clean failure to caller
        logger.warning("outline generation failed (%s): %s", model, exc)
        fallback = _fallback_outline(files)
        fallback["error"] = str(exc)
        return fallback

    data = _validate_outline(
        _parse_outline(text),
        repo_dir,
        symbols=symbols,
        fallback_files=files,
        documented_files=documented_files,
    )
    required_pages = _required_top_level_pages(len(files))
    initial_pages = len(data.get("pages") or [])
    initial_warnings = _outline_plan_warnings(data, documented_files)
    should_retry = (
        initial_pages < required_pages or len(files) > 12 or bool(initial_warnings)
    )
    refined = False
    plan_repairs = (
        _MAX_OUTLINE_PLAN_REPAIRS
        if initial_pages < required_pages
        else int(should_retry)
    )
    for _ in range(plan_repairs):
        current_pages = len(data.get("pages") or [])
        current_warnings = _outline_plan_warnings(data, documented_files)
        if current_pages < required_pages:
            retry_prompt = _OUTLINE_REPAIR_PROMPT.format(
                original_prompt=prompt,
                accepted=current_pages,
                required=required_pages,
                problems=json.dumps(current_warnings, indent=2),
                outline=json.dumps(data.get("pages") or [], indent=2),
            )
        else:
            retry_prompt = _OUTLINE_REFINEMENT_PROMPT.format(
                original_prompt=prompt,
                problems=json.dumps(current_warnings, indent=2),
                outline=json.dumps(data.get("pages") or [], indent=2),
            )
        try:
            repaired_text = llm.complete(
                [{"role": "user", "content": retry_prompt}],
                max_tokens=max_tokens,
                temperature=0.1,
            )
            repaired = _validate_outline(
                _parse_outline(repaired_text),
                repo_dir,
                symbols=symbols,
                fallback_files=files,
                documented_files=documented_files,
            )
            merged = _validate_outline(
                _merge_outlines(data, repaired),
                repo_dir,
                symbols=symbols,
                fallback_files=files,
                documented_files=documented_files,
            )
            best_candidate = max(
                (repaired, merged),
                key=lambda candidate: _outline_score(
                    candidate,
                    required_pages,
                    documented_files=documented_files,
                ),
            )
            if _outline_score(
                best_candidate,
                required_pages,
                documented_files=documented_files,
            ) > _outline_score(
                data,
                required_pages,
                documented_files=documented_files,
            ):
                data = best_candidate
                refined = True
        except Exception as exc:  # noqa: BLE001 - keep the first valid outline
            logger.debug("outline repair unavailable: %s", exc)
            break
        if len(data.get("pages") or []) >= required_pages:
            break
    for _ in range(_MAX_OUTLINE_STYLE_REPAIRS):
        flagged = _flagged_outline_summaries(data)
        if not flagged:
            break
        retry_prompt = _OUTLINE_STYLE_REPAIR_PROMPT.format(
            summaries=json.dumps(flagged, indent=2),
        )
        try:
            repaired_text = llm.complete(
                [{"role": "user", "content": retry_prompt}],
                max_tokens=max_tokens,
                temperature=0.0,
            )
            repaired = _apply_outline_summary_rewrites(
                data,
                repaired_text,
                allowed_paths={item["path"] for item in flagged},
            )
            if repaired is not None and _outline_score(
                repaired, required_pages
            ) > _outline_score(data, required_pages):
                data = repaired
                refined = True
            else:
                break
        except Exception as exc:  # noqa: BLE001 - keep the best usable outline
            logger.debug("outline quality repair unavailable: %s", exc)
            break
    if not data.get("pages"):
        fallback = _fallback_outline(files)
        fallback["error"] = data.get("error") or "model returned no usable pages"
        fallback["raw"] = data.get("raw")
        return fallback
    data["mode"] = "generated"
    warnings = _outline_plan_warnings(data, documented_files)
    data["quality"] = {
        "required_top_level_pages": required_pages,
        "top_level_pages": len(data["pages"]),
        "valid": len(data["pages"]) >= required_pages and not warnings,
        "refined": refined,
        "warnings": warnings,
    }
    return data


def _required_top_level_pages(salient_file_count: int) -> int:
    """Scale the minimum table-of-contents breadth with repository evidence."""

    if salient_file_count <= 5:
        return 2
    if salient_file_count <= 12:
        return 3
    return 5


def _outline_score(
    data: Dict[str, Any],
    required_pages: int,
    *,
    documented_files: List[str] | None = None,
) -> tuple[int, int, int, int, int, int, int]:
    """Prefer broad, source-diverse outlines without overriding validation."""

    pages = data.get("pages") or []
    children = sum(len(page.get("children") or []) for page in pages)
    files = {
        file
        for page in pages
        for file in page.get("files") or []
        if isinstance(file, str)
    }
    documented = set(documented_files or ())
    workflow_files = {
        file
        for page in pages[1:]
        for item in [page, *(page.get("children") or [])]
        if isinstance(item, dict)
        for file in item.get("files") or []
        if file in documented
    }
    return (
        int(len(pages) >= required_pages),
        -len(_outline_plan_warnings(data, documented_files or [])),
        min(len(pages), required_pages),
        int(bool(documented_files) and documented_files[0] in workflow_files),
        len(workflow_files),
        len(files),
        children,
    )


def _outline_page_keys(page: Dict[str, Any]) -> set[str]:
    """Return stable identities used to merge independently valid outlines."""

    values = (
        str(page.get("id") or ""),
        str(page.get("title") or ""),
    )
    return {
        re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
        for value in values
        if value.strip()
    }


def _concept_terms(value: str) -> set[str]:
    generic = {
        "code",
        "component",
        "components",
        "process",
        "system",
        "tool",
        "tools",
    }
    terms = set()
    for raw in re.findall(r"[a-z0-9]+", value.lower()):
        if raw in generic:
            continue
        variants = {raw}
        for suffix in (
            "ations",
            "ation",
            "ments",
            "ment",
            "ings",
            "ing",
            "ers",
            "er",
            "s",
        ):
            if raw.endswith(suffix) and len(raw) - len(suffix) >= 4:
                variants.add(raw[: -len(suffix)])
                break
        terms.update(variants)
    return terms


def _outline_page_files(page: Dict[str, Any]) -> set[str]:
    return {
        file
        for item in [page, *(page.get("children") or [])]
        if isinstance(item, dict)
        for file in item.get("files") or []
        if isinstance(file, str)
    }


def _same_outline_concept(
    left: Dict[str, Any],
    right: Dict[str, Any],
) -> bool:
    if _outline_page_keys(left) & _outline_page_keys(right):
        return True
    left_files = _outline_page_files(left)
    right_files = _outline_page_files(right)
    if not left_files or not right_files:
        return False
    overlap = len(left_files & right_files) / min(len(left_files), len(right_files))
    if overlap < 0.5:
        return False
    return bool(
        _concept_terms(str(left.get("title") or ""))
        & _concept_terms(str(right.get("title") or ""))
    )


def _merge_outline_page(
    base: Dict[str, Any],
    candidate: Dict[str, Any],
) -> Dict[str, Any]:
    merged = copy.deepcopy(base)
    if len(
        _summary_quality_warnings(
            str(candidate.get("title") or ""),
            str(candidate.get("summary") or ""),
        )
    ) < len(
        _summary_quality_warnings(
            str(base.get("title") or ""),
            str(base.get("summary") or ""),
        )
    ):
        merged["summary"] = candidate.get("summary", "")
    merged["keywords"] = list(
        dict.fromkeys(
            [
                *(base.get("keywords") or []),
                *(candidate.get("keywords") or []),
            ]
        )
    )[:10]
    merged["files"] = list(
        dict.fromkeys(
            [
                *(base.get("files") or []),
                *(candidate.get("files") or []),
            ]
        )
    )[:12]

    children = copy.deepcopy(base.get("children") or [])
    for incoming in candidate.get("children") or []:
        existing = next(
            (child for child in children if _same_outline_concept(child, incoming)),
            None,
        )
        if existing is None:
            if len(children) < _MAX_CHILDREN:
                children.append(copy.deepcopy(incoming))
            continue
        replacement = _merge_outline_page(existing, incoming)
        children[children.index(existing)] = replacement
    merged["children"] = children[:_MAX_CHILDREN]
    return merged


def _merge_outlines(
    base: Dict[str, Any],
    candidate: Dict[str, Any],
) -> Dict[str, Any]:
    """Union distinct source-validated concepts from bounded repair attempts."""

    pages = copy.deepcopy(base.get("pages") or [])
    for incoming in candidate.get("pages") or []:
        existing = next(
            (page for page in pages if _same_outline_concept(page, incoming)),
            None,
        )
        if existing is None:
            if len(pages) < _MAX_OUTLINE_PAGES:
                pages.append(copy.deepcopy(incoming))
            continue
        replacement = _merge_outline_page(existing, incoming)
        pages[pages.index(existing)] = replacement
    return {**base, "pages": pages[:_MAX_OUTLINE_PAGES]}


def _summary_quality_warnings(title: str, summary: str) -> List[str]:
    warnings = []
    if not summary:
        warnings.append(f"{title!r} needs a concrete summary")
    elif _META_SUMMARY_RE.search(summary):
        warnings.append(
            f"{title!r} summary describes the document instead of the subsystem"
        )
    phrases = promotional_phrases(summary)
    phrases.extend(
        match.group(0).lower() for match in _GENERIC_BENEFIT_RE.finditer(summary)
    )
    phrases = sorted(set(phrases))
    if phrases:
        warnings.append(
            f"{title!r} summary uses promotional language: " + ", ".join(phrases)
        )
    return warnings


def _outline_quality_warnings(data: Dict[str, Any]) -> List[str]:
    """Find vague or promotional page summaries before they steer retrieval."""

    warnings = []

    def visit(pages: Any) -> None:
        for page in pages or []:
            if not isinstance(page, dict):
                continue
            title = str(page.get("title") or page.get("id") or "untitled")
            summary = str(page.get("summary") or "").strip()
            warnings.extend(_summary_quality_warnings(title, summary))
            visit(page.get("children"))

    visit(data.get("pages"))
    return warnings


def _outline_plan_warnings(
    data: Dict[str, Any],
    documented_files: List[str],
) -> List[str]:
    """Combine prose checks with coverage of README-declared workflows."""

    warnings = _outline_quality_warnings(data)
    documented = set(documented_files)
    if not documented:
        return warnings
    pages = data.get("pages") or []
    covered = {
        file
        for page in pages[1:]
        for item in [page, *(page.get("children") or [])]
        if isinstance(item, dict)
        for file in item.get("files") or []
        if file in documented
    }
    required = min(2, len(documented))
    primary = documented_files[0]
    if primary not in covered:
        warnings.append(
            "the primary README quickstart entry needs a dedicated workflow page: "
            f"{primary}"
        )
    if len(covered) < required:
        missing = [file for file in documented_files if file not in covered]
        warnings.append(
            "README-declared execution workflows need dedicated source coverage; "
            f"cover at least {required} of: {', '.join(missing[:6])}"
        )
    return warnings


def _flagged_outline_summaries(data: Dict[str, Any]) -> List[dict[str, Any]]:
    """Return only summary records that need a bounded style repair."""

    flagged = []

    def visit(pages: Any, prefix: str = "") -> None:
        for index, page in enumerate(pages or []):
            if not isinstance(page, dict):
                continue
            path = f"{prefix}/{index}".lstrip("/")
            title = str(page.get("title") or page.get("id") or "untitled")
            summary = str(page.get("summary") or "").strip()
            problems = _summary_quality_warnings(title, summary)
            if problems:
                flagged.append(
                    {
                        "path": path,
                        "title": title,
                        "summary": summary,
                        "keywords": page.get("keywords") or [],
                        "files": page.get("files") or [],
                        "problems": problems,
                    }
                )
            visit(page.get("children"), path)

    visit(data.get("pages"))
    return flagged


def _apply_outline_summary_rewrites(
    current: Dict[str, Any],
    text: str,
    *,
    allowed_paths: set[str],
) -> Dict[str, Any] | None:
    """Apply model-written summaries without admitting any structural edits."""

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", cleaned).strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    rewrites = {}
    for item in payload.get("summaries") or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        summary = re.sub(r"\s+", " ", str(item.get("summary") or "")).strip()
        if path in allowed_paths and summary:
            rewrites[path] = summary[:400]
    if not rewrites:
        return None

    def apply(pages: Any, prefix: str = "") -> List[dict[str, Any]]:
        result = []
        for index, page in enumerate(pages or []):
            path = f"{prefix}/{index}".lstrip("/")
            result.append(
                {
                    **page,
                    "summary": rewrites.get(path, page.get("summary", "")),
                    "children": apply(page.get("children"), path),
                }
            )
        return result

    return {**current, "pages": apply(current.get("pages"))}


def _parse_outline(text: str) -> Dict[str, Any]:
    """Extract the JSON object from a model response (tolerant of code fences)."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", cleaned).strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return {"pages": [], "raw": text}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return {"pages": [], "raw": text, "error": f"json: {exc}"}
    if not isinstance(data.get("pages"), list):
        return {"pages": [], "raw": text}
    return data


def _validate_outline(
    data: Dict[str, Any],
    repo_dir: str,
    *,
    symbols: List[Symbol] | None = None,
    fallback_files: List[str] | None = None,
    documented_files: List[str] | None = None,
) -> Dict[str, Any]:
    known_files = {
        path.relative_to(repo_dir).as_posix()
        for path in walk_repository_files(repo_dir)
    }
    symbols = symbols or []
    fallback_files = fallback_files or []
    documented_order = list(
        dict.fromkeys(
            str(file).replace("\\", "/").strip().lstrip("./")
            for file in documented_files or []
        )
    )
    documented = {
        str(file).replace("\\", "/").strip().lstrip("./") for file in documented_order
    }
    by_basename: dict[str, List[str]] = defaultdict(list)
    for file in known_files:
        by_basename[file.rsplit("/", 1)[-1]].append(file)
    searchable: dict[str, str] = {
        file: re.sub(r"[^a-z0-9]+", " ", file.lower()) for file in known_files
    }
    for symbol in symbols:
        file = str(symbol.file).replace("\\", "/").lstrip("./")
        if file in searchable:
            searchable[file] += " " + re.sub(
                r"[^a-z0-9]+",
                " ",
                f"{symbol.name} {symbol.type}".lower(),
            )

    generic_terms = {
        "architecture",
        "code",
        "component",
        "components",
        "handling",
        "overview",
        "project",
        "repository",
        "system",
        "types",
    }

    def tokens(value: str) -> set[str]:
        return set(re.findall(r"[a-z0-9_]{4,}", value.lower()))

    def variants(value: str) -> set[str]:
        values = {value}
        for suffix in ("ing", "ed", "es", "s"):
            if value.endswith(suffix) and len(value) - len(suffix) >= 4:
                values.add(value[: -len(suffix)])
        return values

    def matches(term: str, document_tokens: set[str]) -> bool:
        return any(
            left == right
            or (
                min(len(left), len(right)) >= 4
                and (left.startswith(right) or right.startswith(left))
            )
            for left in variants(term)
            for token in document_tokens
            for right in variants(token)
        )

    def title_is_anchored(
        title: str,
        files: List[str],
        *,
        overview: bool = False,
    ) -> bool:
        if overview:
            return True
        title_terms = tokens(title) - generic_terms
        if not title_terms:
            return False
        return any(
            matches(term, set(searchable.get(file, "").split()))
            for file in files
            for term in title_terms
        )

    def inferred_files(page: Dict[str, Any]) -> List[str]:
        raw_terms = [
            str(page.get("title") or ""),
            str(page.get("summary") or ""),
            *[str(value) for value in page.get("keywords") or []],
        ]
        terms = {
            token
            for token in re.findall(r"[a-z0-9_]{4,}", " ".join(raw_terms).lower())
            if token not in generic_terms
        }
        scored = []
        for file, document in searchable.items():
            document_tokens = set(document.split())
            score = sum(
                1
                for term in terms
                if term in document_tokens
                or any(
                    len(token) >= 4
                    and (term.startswith(token) or token.startswith(term))
                    for token in document_tokens
                )
            )
            # A single generic word (for example "agent") is not enough to
            # admit a whole page. Missing model-provided anchors need at least
            # two independent lexical signals from a real source unit.
            if score >= 2:
                scored.append((score, file))
        return [
            file for _, file in sorted(scored, key=lambda item: (-item[0], item[1]))[:4]
        ]

    def valid_files(values: Any) -> List[str]:
        result = []
        for value in values or []:
            file = str(value).replace("\\", "/").strip().lstrip("./")
            if file in known_files:
                result.append(file)
                continue
            matches = by_basename.get(file.rsplit("/", 1)[-1], [])
            if len(matches) == 1:
                result.append(matches[0])
        return list(dict.fromkeys(result))[:12]

    def overview_files(existing: List[str]) -> List[str]:
        role_files = sorted(
            known_files,
            key=lambda file: (-_overview_file_score(file), file),
        )
        candidates = list(
            dict.fromkeys(
                file
                for file in [
                    *existing,
                    *documented_order,
                    *role_files,
                    *fallback_files,
                ]
                if file in known_files
            )
        )
        readme_names = {name.lower() for name in _README_NAMES}
        readmes = sorted(
            (
                file
                for file in known_files
                if os.path.basename(file).lower() in readme_names
            ),
            key=lambda file: (file.count("/"), file.lower()),
        )
        preferred = sorted(
            (
                file
                for file in candidates
                if file in documented or _overview_file_score(file) > -100
            ),
            key=lambda file: (
                file not in documented,
                -_overview_file_score(file),
                candidates.index(file),
            ),
        )
        chosen = list(dict.fromkeys(readmes))[:1]
        areas = set()
        for file in preferred:
            parts = file.split("/")
            area = "/".join(parts[:2]) if len(parts) > 2 else parts[0]
            if area in areas:
                continue
            areas.add(area)
            chosen.append(file)
            if len(chosen) >= 8:
                break
        for file in candidates:
            if preferred and _overview_file_score(file) <= -100:
                continue
            if file not in chosen:
                chosen.append(file)
            if len(chosen) >= 8:
                break
        return chosen

    def normalize(
        page: Any,
        *,
        child: bool = False,
        parent_files: List[str] | None = None,
        overview: bool = False,
    ) -> Dict[str, Any] | None:
        if not isinstance(page, dict):
            return None
        title = re.sub(r"\s+", " ", str(page.get("title") or "")).strip()
        if not title:
            return None
        if overview:
            title = "Overview"
        summary = re.sub(r"\s+", " ", str(page.get("summary") or "")).strip()
        keywords = [
            re.sub(r"\s+", " ", str(value)).strip()
            for value in page.get("keywords") or []
            if str(value).strip()
        ]
        allows_supporting_files = _page_allows_supporting_files(
            {"title": title, "keywords": keywords}
        )
        files = valid_files(page.get("files"))
        if not overview and not allows_supporting_files:
            files = [
                file
                for file in files
                if not _is_supporting_file(file) or file in documented
            ]
        if overview:
            files = overview_files(files)
        if not files:
            files = inferred_files(page)
            if not overview and not allows_supporting_files:
                files = [
                    file
                    for file in files
                    if not _is_supporting_file(file) or file in documented
                ]
        if not files and overview:
            files = [file for file in fallback_files if file in known_files][:8]
        if not files and child and parent_files:
            parent_matches = [
                file for file in inferred_files(page) if file in parent_files
            ]
            if not allows_supporting_files:
                parent_matches = [
                    file
                    for file in parent_matches
                    if not _is_supporting_file(file) or file in documented
                ]
            files = parent_matches[:4]
        if not files:
            return None
        if not title_is_anchored(title, files, overview=overview):
            return None

        children = []
        if not child:
            for candidate in (page.get("children") or [])[:_MAX_CHILDREN]:
                item = normalize(candidate, child=True, parent_files=files)
                if item is not None:
                    children.append(item)
        return {
            "id": str(page.get("id") or ""),
            "title": title[:80],
            "summary": summary[:400],
            "keywords": list(dict.fromkeys(keywords))[:10],
            "files": files,
            "children": children,
        }

    candidates = list((data.get("pages") or [])[:_MAX_OUTLINE_PAGES])
    overview_index = next(
        (
            index
            for index, candidate in enumerate(candidates)
            if isinstance(candidate, dict)
            and (
                str(candidate.get("id") or "").strip().lower() == "overview"
                or "overview" in str(candidate.get("title") or "").lower()
            )
        ),
        None,
    )
    if overview_index is None:
        overview_candidate = {
            "id": "overview",
            "title": "Overview",
            "summary": "Repository purpose, public workflow, and major subsystems.",
            "keywords": ["entry point", "runtime", "compiler"],
            "files": fallback_files,
            "children": [],
        }
        ordered = [(overview_candidate, True), *[(item, False) for item in candidates]]
    else:
        overview_candidate = candidates.pop(overview_index)
        ordered = [(overview_candidate, True), *[(item, False) for item in candidates]]

    pages = []
    for candidate, is_overview in ordered[:_MAX_OUTLINE_PAGES]:
        page = normalize(candidate, overview=is_overview)
        if page is None:
            continue
        existing = next(
            (accepted for accepted in pages if _same_outline_concept(accepted, page)),
            None,
        )
        if existing is None:
            pages.append(page)
            continue
        replacement = _merge_outline_page(existing, page)
        pages[pages.index(existing)] = replacement
    if pages:
        promoted = pages[0].get("children", [])
        pages[0]["children"] = []
        existing = {str(page.get("title") or "").lower() for page in pages}
        promoted = [
            page
            for page in promoted
            if str(page.get("title") or "").lower() not in existing
        ]
        pages[1:1] = promoted[: max(0, _MAX_OUTLINE_PAGES - len(pages))]
    return {**data, "pages": pages}


def _fallback_outline(files: List[str]) -> Dict[str, Any]:
    grouped: dict[str, List[str]] = defaultdict(list)
    for file in files:
        area = file.split("/", 1)[0] if "/" in file else "Project"
        grouped[area].append(file)

    pages: List[Dict[str, Any]] = [
        {
            "id": "overview",
            "title": "Overview",
            "summary": "Repository purpose, architecture, and major components.",
            "keywords": ["architecture", "entry point", "core"],
            "files": files[:10],
            "children": [],
        }
    ]
    for area, area_files in list(grouped.items())[:8]:
        title = (
            "Project Foundation"
            if area == "Project"
            else re.sub(r"[-_]+", " ", area).title()
        )
        pages.append(
            {
                "id": re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-"),
                "title": title,
                "summary": f"Responsibilities and key components in {area}.",
                "keywords": [
                    area,
                    *[os.path.basename(file) for file in area_files[:4]],
                ],
                "files": area_files[:10],
                "children": [],
            }
        )
    return {"pages": pages, "mode": "fallback"}
