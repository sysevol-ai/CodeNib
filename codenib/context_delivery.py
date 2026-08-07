# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Bounded, query-focused delivery of repository evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

DEFAULT_TOTAL_CONTENT_CHARS = 10_000
DEFAULT_MIN_EXCERPT_CHARS = 800
DEFAULT_MAX_EXCERPT_CHARS = 2_400

_QUERY_STOP_TERMS = frozenset(
    {
        "about",
        "built",
        "code",
        "does",
        "explain",
        "from",
        "have",
        "into",
        "repository",
        "source",
        "that",
        "their",
        "this",
        "what",
        "when",
        "where",
        "which",
        "with",
    }
)


@dataclass(frozen=True, slots=True)
class ContentProjection:
    """One bounded source excerpt and its delivery metadata."""

    content: str
    original_chars: int
    returned_chars: int
    truncated: bool
    strategy: str

    def metadata(self) -> dict[str, Any]:
        return {
            "truncated": self.truncated,
            "original_chars": self.original_chars,
            "returned_chars": self.returned_chars,
            "strategy": self.strategy,
        }


def _render_line_selection(lines: Sequence[str], selected: set[int]) -> str:
    rendered: list[str] = []
    previous = -1
    for index in sorted(selected):
        if previous >= 0 and index > previous + 1:
            rendered.append(f"... {index - previous - 1} source lines omitted ...")
        rendered.append(lines[index])
        previous = index
    if selected and previous < len(lines) - 1:
        rendered.append(f"... {len(lines) - previous - 1} source lines omitted ...")
    return "\n".join(rendered)


def focus_source_content(content: str, query: str, max_chars: int) -> str:
    """Project a large source chunk into query-relevant, line-stable windows."""

    if max_chars <= 0:
        return ""
    if len(content) <= max_chars:
        return content

    lines = content.splitlines()
    if not lines:
        return content[:max_chars]
    query_terms = {
        term
        for term in re.findall(r"[a-z0-9_]+", query.lower())
        if len(term) >= 3 and term not in _QUERY_STOP_TERMS
    }
    selected = set(range(min(5, len(lines))))
    ranked_lines = sorted(
        (
            (sum(term in line.lower() for term in query_terms), index)
            for index, line in enumerate(lines)
        ),
        key=lambda item: (-item[0], item[1]),
    )

    for score, index in ranked_lines:
        if score <= 0:
            break
        candidate = selected.union(range(max(0, index - 2), min(len(lines), index + 3)))
        if len(_render_line_selection(lines, candidate)) <= max_chars:
            selected = candidate

    if len(selected) == min(5, len(lines)):
        for index in range(len(selected), len(lines)):
            candidate = selected.union({index})
            if len(_render_line_selection(lines, candidate)) > max_chars:
                break
            selected = candidate

    focused = _render_line_selection(lines, selected)
    if len(focused) <= max_chars:
        return focused
    suffix = "\n... source excerpt truncated ..."
    if max_chars <= len(suffix):
        return suffix[:max_chars]
    return focused[: max(0, max_chars - len(suffix))].rstrip() + suffix


def project_content(content: str, *, query: str, max_chars: int) -> ContentProjection:
    """Return one bounded content projection without changing source locations."""

    projected = focus_source_content(content, query, max_chars)
    truncated = projected != content
    return ContentProjection(
        content=projected,
        original_chars=len(content),
        returned_chars=len(projected),
        truncated=truncated,
        strategy="query_focused" if truncated else "complete",
    )


def _content_budgets(
    count: int,
    *,
    total_chars: int,
    min_excerpt_chars: int,
    max_excerpt_chars: int,
) -> list[int]:
    if count <= 0 or total_chars <= 0:
        return []
    if min_excerpt_chars <= 0 or max_excerpt_chars < min_excerpt_chars:
        raise ValueError("content excerpt bounds are invalid")

    admitted = min(count, max(1, total_chars // min_excerpt_chars))
    per_result = min(max_excerpt_chars, total_chars // admitted)
    budgets = [per_result] * admitted
    budgets[-1] += total_chars - sum(budgets)
    budgets[-1] = min(budgets[-1], max_excerpt_chars)
    return budgets


def project_evidence_payloads(
    payloads: Sequence[Mapping[str, Any]],
    *,
    query: str,
    total_chars: int = DEFAULT_TOTAL_CONTENT_CHARS,
    min_excerpt_chars: int = DEFAULT_MIN_EXCERPT_CHARS,
    max_excerpt_chars: int = DEFAULT_MAX_EXCERPT_CHARS,
) -> list[dict[str, Any]]:
    """Bound aggregate result content while retaining every ranked metadata row."""

    content_count = sum(
        isinstance(payload.get("content"), str) and bool(payload.get("content"))
        for payload in payloads
    )
    budgets = iter(
        _content_budgets(
            content_count,
            total_chars=total_chars,
            min_excerpt_chars=min_excerpt_chars,
            max_excerpt_chars=max_excerpt_chars,
        )
    )
    remaining = total_chars
    projected_payloads: list[dict[str, Any]] = []
    for payload in payloads:
        result = dict(payload)
        content = result.get("content")
        if not isinstance(content, str) or not content:
            projected_payloads.append(result)
            continue

        budget = min(next(budgets, 0), remaining)
        projection = project_content(content, query=query, max_chars=budget)
        remaining -= projection.returned_chars
        if projection.returned_chars:
            result["content"] = projection.content
        else:
            result.pop("content", None)
            projection = ContentProjection(
                content="",
                original_chars=len(content),
                returned_chars=0,
                truncated=True,
                strategy="metadata_only",
            )
        if projection.truncated:
            result["content_projection"] = projection.metadata()
        projected_payloads.append(result)

    return projected_payloads


__all__ = [
    "ContentProjection",
    "DEFAULT_MAX_EXCERPT_CHARS",
    "DEFAULT_MIN_EXCERPT_CHARS",
    "DEFAULT_TOTAL_CONTENT_CHARS",
    "focus_source_content",
    "project_content",
    "project_evidence_payloads",
]
