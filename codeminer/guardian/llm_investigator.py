# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""LLM-driven investigation step for Repository Guardian.

Public entry points:

* :func:`investigate_signal` — generic entry point for any signal type; caller
  supplies a pre-built context string (see :func:`build_test_failure_context`).
* :func:`_agentic_loop` — shared tool-use loop (search_code tool).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional

from ..log_utils import get_logger

if TYPE_CHECKING:
    from ..llm.litellm_chat import LiteLLMChat

logger = get_logger(__name__)

FILE_CONTENT_MAX_LINES = 150


# ---------------------------------------------------------------------------
# Token usage accumulator
# ---------------------------------------------------------------------------


@dataclass
class LLMUsage:
    """Token counts accumulated across all LLM calls in one Guardian cycle."""

    prompt_tokens: int = field(default=0)
    completion_tokens: int = field(default=0)
    total_tokens: int = field(default=0)

    def add(self, response: object) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
        self.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
        self.total_tokens += getattr(usage, "total_tokens", 0) or 0

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

_TEST_FAILURE_SYSTEM_PROMPT = (
    "You are Repository Guardian, a proactive code-health monitor. "
    "You receive details about a failing test. "
    "Your job is to trace the root cause through the codebase before concluding. "
    "\n\n"
    "Investigate iteratively using search_code:\n"
    "- Search for the failing symbol or assertion to find the implementation.\n"
    "- Follow leads: search for related helpers, fixtures, or recently changed code.\n"
    "- Keep searching until you understand *why* the test fails, not just *where*.\n"
    "- Only stop and write your final answer when you have traced the root cause.\n"
    "\n"
    "Final answer format (plain text, no JSON or Markdown):\n"
    "(1) Root cause of the failure, traced through code.\n"
    "(2) The specific failing assertion or symbol, with evidence from your searches.\n"
    "(3) A concrete fix recommendation."
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


_CONTENT_MAX_LINES = 50


def _node_content(n: object, repo_path: str) -> str:
    """Return the source snippet for a result node.

    Uses the pre-embedded ``content`` field when available; falls back to
    reading lines ``start_line``..``end_line`` (0-based) from disk.
    """
    content = getattr(n, "content", None)
    if content:
        return content.strip()

    file = getattr(n, "file", None)
    start = getattr(n, "start_line", None)
    end = getattr(n, "end_line", None)
    if not file or start is None:
        return ""

    full_path = (
        os.path.join(repo_path, file)
        if repo_path and not os.path.isabs(file)
        else file
    )
    try:
        with open(full_path, encoding="utf-8", errors="replace") as fh:
            all_lines = fh.readlines()
        sl = max(0, start)
        el = min(len(all_lines), (end if end is not None else start) + 1)
        snippet = all_lines[sl:el]
        if len(snippet) > _CONTENT_MAX_LINES:
            snippet = snippet[:_CONTENT_MAX_LINES] + [
                f"... ({len(all_lines[sl:el]) - _CONTENT_MAX_LINES} more lines)\n"
            ]
        return "".join(snippet).strip()
    except OSError:
        return ""


def _run_search(query: str, retriever: object, top_k: int, repo_path: str = "") -> str:
    try:
        nodes = retriever.query(query, top_k=top_k)  # type: ignore[union-attr]
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm_investigator search failed: %s", exc)
        return f"(search error: {exc})"

    parts: List[str] = []
    for n in nodes or []:
        file = getattr(n, "file", "?") or "?"
        name = getattr(n, "node_name", "?") or "?"
        typ = getattr(n, "type", "?") or "?"
        line = getattr(n, "start_line", None)
        score = getattr(n, "score", None)
        loc = f"{file}:{line}" if line is not None else file
        score_str = f"{score:.3f}" if isinstance(score, float) else "—"
        header = f"{loc} | {name} ({typ}) | score={score_str}"
        snippet = _node_content(n, repo_path)
        if snippet:
            parts.append(f"{header}\n```\n{snippet}\n```")
        else:
            parts.append(header)
    return "\n\n".join(parts) if parts else "(no results)"


# ---------------------------------------------------------------------------
# Core agentic loop (shared)
# ---------------------------------------------------------------------------


def _format_messages(messages: List[dict]) -> str:
    """Render a messages list as readable plain text for debug logging."""
    parts: List[str] = []
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content") or ""
        tool_calls = msg.get("tool_calls")
        tool_call_id = msg.get("tool_call_id")

        if role == "tool":
            parts.append(f"[tool_result: {tool_call_id}]\n{content}")
        elif tool_calls:
            tc_lines = []
            for tc in tool_calls:
                fn = tc.get("function", {})
                tc_lines.append(f"  {fn.get('name')}  {fn.get('arguments')}")
            parts.append(f"[{role} → tool_calls]\n" + "\n".join(tc_lines))
        else:
            parts.append(f"[{role}]\n{content}")
    return "\n\n".join(parts)


def _log_usage(response: object, label: str) -> None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    logger.debug(
        "agentic_loop: %s usage: prompt=%d completion=%d total=%d",
        label,
        getattr(usage, "prompt_tokens", 0) or 0,
        getattr(usage, "completion_tokens", 0) or 0,
        getattr(usage, "total_tokens", 0) or 0,
    )


def _agentic_loop(
    messages: List[dict],
    retriever: object,
    *,
    llm: "LiteLLMChat",
    max_tool_rounds: int,
    top_k: int,
    usage_acc: Optional[LLMUsage] = None,
    repo_path: str = "",
) -> str:
    """Run the tool-use loop until the model produces a final answer."""
    for round_idx in range(max_tool_rounds + 1):
        use_tools = round_idx < max_tool_rounds
        kwargs: dict = {}
        if use_tools:
            kwargs["tools"] = [_SEARCH_TOOL]
            kwargs["tool_choice"] = "auto"

        logger.debug(
            "agentic_loop: round=%d calling LLM with %d message(s):\n%s",
            round_idx,
            len(messages),
            _format_messages(messages),
        )

        try:
            response = llm._call_raw(messages, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "llm_investigator: LLM call failed (round %d): %s", round_idx, exc
            )
            return f"(LLM investigation unavailable: {exc})"

        _log_usage(response, f"round={round_idx}")
        if usage_acc is not None:
            usage_acc.add(response)

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
            result = _run_search(query, retriever, top_k, repo_path)
            logger.debug(
                "agentic_loop: tool_result query=%r:\n%s", query, result
            )
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    try:
        response = llm._call_raw(messages)
        _log_usage(response, "round=final(forced)")
        if usage_acc is not None:
            usage_acc.add(response)
        return (response.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm_investigator: final call failed: %s", exc)
        return f"(LLM investigation unavailable: {exc})"


def investigate_signal(
    context: str,
    retriever: object,
    *,
    system_prompt: str = _TEST_FAILURE_SYSTEM_PROMPT,
    llm: "LiteLLMChat",
    max_tool_rounds: int = 3,
    top_k: int = 5,
    usage_acc: Optional[LLMUsage] = None,
    repo_path: str = "",
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
        messages,
        retriever,
        llm=llm,
        max_tool_rounds=max_tool_rounds,
        top_k=top_k,
        usage_acc=usage_acc,
        repo_path=repo_path,
    )
