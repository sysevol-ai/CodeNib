# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""LLM-driven investigation step for Repository Guardian.

Two public entry points:

* :func:`investigate_with_llm` — churn signal: reads the hotspot file directly
  and runs the agentic loop.
* :func:`investigate_signal` — generic entry point for any signal type; caller
  supplies a pre-built context string (see :func:`build_test_failure_context`).

Both share the same ``_agentic_loop`` core: the LLM may call ``search_code``
repeatedly before producing a final plain-text narrative.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, List

from ..log_utils import get_logger
from .signals import Hotspot

if TYPE_CHECKING:
    from ..llm.litellm_chat import LiteLLMChat

logger = get_logger(__name__)

FILE_CONTENT_MAX_LINES = 150

# ---------------------------------------------------------------------------
# Tool schema (OpenAI/litellm format)
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
# System prompts
# ---------------------------------------------------------------------------

_CHURN_SYSTEM_PROMPT = (
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

_TEST_FAILURE_SYSTEM_PROMPT = (
    "You are Repository Guardian, a proactive code-health monitor. "
    "You receive details about a failing test — the test source, error message, "
    "and optionally the source file it exercises. "
    "Use search_code to look up related implementation code if needed. "
    "Your final response must be a concise (<200 words) plain-text analysis "
    "that: (1) explains the root cause of the failure, "
    "(2) cites the specific failing assertion or symbol, and "
    "(3) recommends a concrete fix. "
    "Do NOT wrap the final answer in JSON or Markdown — plain text only."
)

# ---------------------------------------------------------------------------
# File reader (shared utility)
# ---------------------------------------------------------------------------


def read_file(path: str, max_lines: int = FILE_CONTENT_MAX_LINES) -> str:
    """Read a file and return its content, truncated to ``max_lines``."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError as exc:
        logger.warning("llm_investigator: cannot read %s: %s", path, exc)
        return ""
    if len(lines) <= max_lines:
        return "".join(lines)
    kept = "".join(lines[:max_lines])
    return kept + f"\n... ({len(lines) - max_lines} more lines truncated)"


def _read_hotspot_file(hotspot: Hotspot, repo_path: str) -> str:
    full_path = os.path.join(repo_path, hotspot.path) if repo_path else hotspot.path
    return read_file(full_path)


# ---------------------------------------------------------------------------
# Observation builders
# ---------------------------------------------------------------------------


def _observation_text(hotspot: Hotspot, since: str, file_content: str) -> str:
    lines = [
        f"File: {hotspot.path}",
        f"Churn: {hotspot.commit_count} commits over {since}",
        "",
    ]
    if file_content:
        lines += [f"Source ({hotspot.path}):", "```", file_content.rstrip(), "```", ""]
    else:
        lines += ["(file could not be read)", ""]
    lines.append(
        "Use search_code to find callers or related code in other files, "
        "then write your analysis."
    )
    return "\n".join(lines)


def build_test_failure_context(
    nodeid: str,
    error: str,
    *,
    test_content: str = "",
    source_content: str = "",
    source_path: str = "",
) -> str:
    """Build the LLM user-message for a failing test.

    Args:
        nodeid: Pytest node id, e.g. ``"test/guardian/test_cycle.py::test_foo"``.
        error: Failure message / short traceback from pytest output.
        test_content: Full source of the test file (truncated by caller).
        source_content: Full source of the inferred source file (optional).
        source_path: Relative path of the source file (for display).

    Returns:
        Plain-text context string ready to be placed in the ``user`` role.
    """
    lines = [f"Failing test: {nodeid}", ""]
    if error:
        lines += ["Error:", "```", error[:1200].rstrip(), "```", ""]
    if test_content:
        test_file = nodeid.split("::")[0]
        lines += [f"Test source ({test_file}):", "```python", test_content.rstrip(), "```", ""]
    if source_content and source_path:
        lines += [
            f"Source under test ({source_path}):",
            "```python",
            source_content.rstrip(),
            "```",
            "",
        ]
    lines.append(
        "Investigate why this test is failing. Use search_code to find related "
        "implementation code if needed. Explain the root cause and recommend a fix."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool executor
# ---------------------------------------------------------------------------


def _run_search(query: str, retriever: object, top_k: int) -> str:
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
# Core agentic loop (shared)
# ---------------------------------------------------------------------------


def _agentic_loop(
    messages: List[dict],
    retriever: object,
    *,
    llm: "LiteLLMChat",
    max_tool_rounds: int,
    top_k: int,
) -> str:
    """Run the tool-use loop until the model produces a final answer."""
    logger.debug(
        "agentic_loop: start max_rounds=%d\ninitial_messages:\n%s",
        max_tool_rounds,
        json.dumps(messages, indent=2, ensure_ascii=False),
    )

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

        msg = response.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None) or []

        if not tool_calls:
            logger.debug(
                "agentic_loop: round=%d final_answer:\n%s",
                round_idx,
                msg.content or "",
            )
            return (msg.content or "").strip()

        logger.debug(
            "agentic_loop: round=%d tool_calls:\n%s",
            round_idx,
            json.dumps(
                [
                    {
                        "id": tc.id,
                        "function": tc.function.name,
                        "args": tc.function.arguments,
                    }
                    for tc in tool_calls
                ],
                indent=2,
            ),
        )
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
                "agentic_loop: tool_result query=%r:\n%s", query, result
            )
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    try:
        response = llm._call_raw(messages)
        return (response.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm_investigator: final call failed: %s", exc)
        return f"(LLM investigation unavailable: {exc})"


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def investigate_with_llm(
    hotspot: Hotspot,
    retriever: object,
    *,
    repo_path: str = "",
    since: str = "90 days ago",
    llm: "LiteLLMChat",
    max_tool_rounds: int = 3,
    top_k: int = 5,
) -> str:
    """Run the LLM agentic loop to investigate a churn hotspot.

    Reads the hotspot file directly and passes its source to the model.
    """
    file_content = _read_hotspot_file(hotspot, repo_path)
    if file_content:
        logger.debug(
            "llm_investigator: read %d chars from %s", len(file_content), hotspot.path
        )
    else:
        logger.warning("llm_investigator: could not read %s", hotspot.path)

    messages: List[dict] = [
        {"role": "system", "content": _CHURN_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": _observation_text(hotspot, since, file_content),
        },
    ]
    return _agentic_loop(
        messages, retriever, llm=llm, max_tool_rounds=max_tool_rounds, top_k=top_k
    )


def investigate_signal(
    context: str,
    retriever: object,
    *,
    system_prompt: str = _TEST_FAILURE_SYSTEM_PROMPT,
    llm: "LiteLLMChat",
    max_tool_rounds: int = 3,
    top_k: int = 5,
) -> str:
    """Run the LLM agentic loop with a caller-supplied context string.

    Use :func:`build_test_failure_context` (or a custom builder) to construct
    ``context``.  The same ``search_code`` tool is available to the model.

    Args:
        context: Full user-message describing the signal to investigate.
        retriever: Duck-typed retriever with ``.query(str, top_k=int) -> list``.
        system_prompt: Override the default test-failure system prompt.
        llm: A :class:`~codeminer.llm.litellm_chat.LiteLLMChat` instance.
        max_tool_rounds: Max search rounds before forcing a final answer.
        top_k: Results per search query.

    Returns:
        Plain-text narrative from the LLM.
    """
    messages: List[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": context},
    ]
    return _agentic_loop(
        messages, retriever, llm=llm, max_tool_rounds=max_tool_rounds, top_k=top_k
    )
