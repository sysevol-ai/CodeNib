# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""LLM-driven investigation step for Repository Guardian.

Runs a manual tool-use loop so the model can search the codebase before
writing a narrative.  The initial context includes the **actual file content**
of the hotspot (up to ``FILE_CONTENT_MAX_LINES`` lines) so the LLM reasons
about real code, not just symbol names.  The ``search_code`` tool is then used
to find callers, related modules, or tests in other files.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, List, Optional, Sequence

from ..log_utils import get_logger
from .investigate import Evidence
from .signals import Hotspot

if TYPE_CHECKING:
    from ..llm.litellm_chat import LiteLLMChat

logger = get_logger(__name__)

FILE_CONTENT_MAX_LINES = 150

# ---------------------------------------------------------------------------
# Tool schema (OpenAI/litellm format — litellm translates to Anthropic wire)
# ---------------------------------------------------------------------------

_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_code",
        "description": (
            "Search the codebase for code relevant to a query using BM25 "
            "keyword matching. Returns matching symbol names and file locations. "
            "Use this to find callers, related modules, or tests in other files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Keyword or identifier query — e.g. a function name you "
                        "want to find callers of, or a concept like 'language registry'."
                    ),
                }
            },
            "required": ["query"],
        },
    },
}

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are Repository Guardian, a proactive code-health monitor. "
    "You receive the full source of a high-churn file — one modified in many "
    "commits recently, which often signals instability, unclear ownership, or "
    "hidden coupling. "
    "Read the file carefully, then use search_code to find callers, related "
    "modules, or tests in *other* files before writing your analysis. "
    "Your final response must be a concise (<200 words) plain-text analysis "
    "that: (1) names the specific risk you identified from the code, "
    "(2) cites at least one concrete symbol or location, and "
    "(3) recommends a specific action. "
    "Do NOT wrap the final answer in JSON or Markdown — plain text only."
)


# ---------------------------------------------------------------------------
# File reader
# ---------------------------------------------------------------------------


def _read_hotspot_file(hotspot: Hotspot, repo_path: str) -> str:
    """Read the hotspot file and return its content, truncated if necessary."""
    full_path = (
        os.path.join(repo_path, hotspot.path) if repo_path else hotspot.path
    )
    try:
        with open(full_path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError as exc:
        logger.warning("llm_investigator: cannot read %s: %s", full_path, exc)
        return ""

    if len(lines) <= FILE_CONTENT_MAX_LINES:
        return "".join(lines)

    kept = "".join(lines[:FILE_CONTENT_MAX_LINES])
    remaining = len(lines) - FILE_CONTENT_MAX_LINES
    return kept + f"\n... ({remaining} more lines truncated)"


# ---------------------------------------------------------------------------
# Observation builder
# ---------------------------------------------------------------------------


def _observation_text(
    hotspot: Hotspot,
    since: str,
    file_content: str,
    initial_evidence: Sequence[Evidence],
) -> str:
    lines = [
        f"File: {hotspot.path}",
        f"Churn: {hotspot.commit_count} commits over {since}",
        "",
    ]

    if file_content:
        lines += [
            f"Source ({hotspot.path}):",
            "```",
            file_content.rstrip(),
            "```",
            "",
        ]
    else:
        # Fallback: symbol list from BM25 when file cannot be read
        lines.append("Symbol index (file could not be read directly):")
        if initial_evidence:
            for e in initial_evidence:
                loc = f"{e.file}:{e.start_line}" if e.start_line is not None else e.file
                lines.append(f"  {loc} | {e.node_name or '?'} ({e.type or '?'})")
        else:
            lines.append("  (no evidence available)")
        lines.append("")

    lines.append(
        "Use search_code to find callers or related code in other files, "
        "then write your analysis."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool executor
# ---------------------------------------------------------------------------


def _run_search(query: str, retriever: object, top_k: int) -> str:
    """Execute a search_code tool call and return a text result for the model."""
    try:
        nodes = retriever.query(query, top_k=top_k)  # type: ignore[union-attr]
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm_investigator search failed: %s", exc)
        return f"(search error: {exc})"

    rows: List[str] = []
    for n in nodes or []:
        file = getattr(n, "file", "?") or "?"
        name = getattr(n, "node_name", "?") or "?"
        typ = getattr(n, "type", "?") or "?"
        line = getattr(n, "start_line", None)
        score = getattr(n, "score", None)
        loc = f"{file}:{line}" if line is not None else file
        score_str = f"{score:.3f}" if isinstance(score, float) else "—"
        rows.append(f"{loc} | {name} ({typ}) | score={score_str}")

    return "\n".join(rows) if rows else "(no results)"


# ---------------------------------------------------------------------------
# Agentic loop
# ---------------------------------------------------------------------------


def investigate_with_llm(
    hotspot: Hotspot,
    retriever: object,
    *,
    repo_path: str = "",
    since: str = "90 days ago",
    initial_evidence: Optional[Sequence[Evidence]] = None,
    llm: "LiteLLMChat",
    max_tool_rounds: int = 3,
    top_k: int = 5,
) -> str:
    """Run an LLM agentic loop to investigate a hotspot.

    Args:
        hotspot: The high-churn file to investigate.
        retriever: Any object with ``.query(str, top_k=int) -> list`` (duck-typed).
        repo_path: Root of the repository; used to resolve ``hotspot.path`` for
            direct file reading.  Pass ``""`` if the path is already absolute.
        since: Human-readable churn window used in the observation prompt.
        initial_evidence: Evidence from BM25 retrieval; used as fallback context
            when the file cannot be read directly.
        llm: A :class:`~codeminer.llm.litellm_chat.LiteLLMChat` instance.
        max_tool_rounds: Maximum number of search rounds before forcing a final answer.
        top_k: Results per search query.

    Returns:
        Plain-text narrative from the LLM.
    """
    file_content = _read_hotspot_file(hotspot, repo_path)
    if file_content:
        logger.debug(
            "llm_investigator: read %d chars from %s", len(file_content), hotspot.path
        )
    else:
        logger.warning(
            "llm_investigator: could not read %s; falling back to symbol list",
            hotspot.path,
        )

    evidence = initial_evidence or []
    messages: List[dict] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": _observation_text(hotspot, since, file_content, evidence),
        },
    ]

    for round_idx in range(max_tool_rounds + 1):
        use_tools = round_idx < max_tool_rounds
        kwargs: dict = {}
        if use_tools:
            kwargs["tools"] = [_SEARCH_TOOL]
            kwargs["tool_choice"] = "auto"

        try:
            response = llm._call_raw(messages, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "llm_investigator: LLM call failed (round %d): %s", round_idx, exc
            )
            return f"(LLM investigation unavailable: {exc})"

        choice = response.choices[0]
        msg = choice.message
        tool_calls = getattr(msg, "tool_calls", None) or []

        if not tool_calls:
            return (msg.content or "").strip()

        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            }
        )

        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}
            query = args.get("query", "")
            result = _run_search(query, retriever, top_k)
            logger.debug(
                "llm_investigator: search_code(%r) → %d chars", query, len(result)
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                }
            )

    try:
        response = llm._call_raw(messages)
        return (response.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm_investigator: final call failed: %s", exc)
        return f"(LLM investigation unavailable: {exc})"
