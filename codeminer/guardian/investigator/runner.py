# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Generic narrative investigation helpers.

Repository Guardian's typed L3 loop lives in :mod:`.inner_loop`.  This module
retains the older, generic ``search_code`` narrative utility used for explaining
already-observed test failures.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional, Tuple

from ...log_utils import get_logger

if TYPE_CHECKING:
    from ...llm.litellm_chat import LiteLLMChat

logger = get_logger(__name__)


@dataclass
class LLMUsage:
    """Token counts accumulated across Guardian model calls."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, response: object) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
        self.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
        self.total_tokens += getattr(usage, "total_tokens", 0) or 0


_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_code",
        "description": (
            "Search for code relevant to a query. Returns symbols, locations, "
            "scores, and bounded source snippets."
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}

_TEST_FAILURE_SYSTEM_PROMPT = """\
You are Repository Guardian, a proactive code-health monitor. You receive an
already-observed failing test. Trace its root cause through the codebase with
search_code, then explain the failing contract and recommend a concrete fix.
"""

_CONTENT_MAX_LINES = 50


def _node_content(node: object, repo_path: str) -> str:
    content = getattr(node, "content", None)
    if content:
        return str(content).strip()
    path = getattr(node, "file", None)
    start = getattr(node, "start_line", None)
    end = getattr(node, "end_line", start)
    if not path or start is None:
        return ""
    full_path = (
        os.path.join(repo_path, path)
        if repo_path and not os.path.isabs(path)
        else path
    )
    try:
        with open(full_path, encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return ""
    selected = lines[max(0, int(start)) : int(end) + 1]
    if len(selected) > _CONTENT_MAX_LINES:
        selected = selected[:_CONTENT_MAX_LINES] + ["... [truncated]\n"]
    return "".join(selected).strip()


def _run_search(
    query: str, retriever: object, top_k: int, repo_path: str = ""
) -> str:
    try:
        nodes = retriever.query(query, top_k=top_k)  # type: ignore[union-attr]
    except Exception as exc:  # noqa: BLE001 - narrative search degrades gracefully
        logger.warning("narrative investigation search failed: %s", exc)
        return f"(search error: {exc})"
    rendered = []
    for node in nodes or []:
        path = getattr(node, "file", "?") or "?"
        name = getattr(node, "node_name", "?") or "?"
        kind = getattr(node, "type", "?") or "?"
        line = getattr(node, "start_line", None)
        score = getattr(node, "score", None)
        location = f"{path}:{line}" if line is not None else path
        score_text = f"{score:.3f}" if isinstance(score, float) else "—"
        header = f"{location} | {name} ({kind}) | score={score_text}"
        snippet = _node_content(node, repo_path)
        rendered.append(f"{header}\n```\n{snippet}\n```" if snippet else header)
    return "\n\n".join(rendered) if rendered else "(no results)"


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
    for round_index in range(max_tool_rounds + 1):
        kwargs = (
            {"tools": [_SEARCH_TOOL], "tool_choice": "auto"}
            if round_index < max_tool_rounds
            else {}
        )
        try:
            response = llm._call_raw(messages, **kwargs)
        except Exception as exc:  # noqa: BLE001 - diagnostic utility is best effort
            return f"(LLM investigation unavailable: {exc})"
        if usage_acc is not None:
            usage_acc.add(response)
        message = response.choices[0].message
        calls = list(getattr(message, "tool_calls", None) or [])
        if not calls:
            return (message.content or "").strip()
        messages.append(
            {
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                    for call in calls
                ],
            }
        )
        for call in calls:
            try:
                arguments = json.loads(call.function.arguments)
            except (json.JSONDecodeError, TypeError):
                arguments = {}
            result = _run_search(
                str(arguments.get("query", "")), retriever, top_k, repo_path
            )
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": result}
            )
    return "(investigation ended without a narrative)"


def build_test_failure_context(
    nodeid: str,
    error: str,
    *,
    test_content: str = "",
    source_content: str = "",
    source_path: str = "",
) -> str:
    """Build a bounded narrative-investigation context for a failing test."""

    lines = [f"Failing test: {nodeid}", ""]
    if error:
        lines.extend(["Error:", "```", error[:1200].rstrip(), "```", ""])
    if test_content:
        lines.extend(
            [
                f"Test source ({nodeid.split('::')[0]}):",
                "```python",
                test_content.rstrip(),
                "```",
                "",
            ]
        )
    if source_content and source_path:
        lines.extend(
            [
                f"Source under test ({source_path}):",
                "```python",
                source_content.rstrip(),
                "```",
                "",
            ]
        )
    lines.append(
        "Investigate why this test is failing. Use search_code as needed, "
        "explain the root cause, and recommend a fix."
    )
    return "\n".join(lines)


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
    """Run the generic narrative search loop for an already-known signal."""

    return _agentic_loop(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": context},
        ],
        retriever,
        llm=llm,
        max_tool_rounds=max_tool_rounds,
        top_k=top_k,
        usage_acc=usage_acc,
        repo_path=repo_path,
    )


def _parse_verdict(text: str) -> Tuple[str, str]:
    """Compatibility parser using L3's normalized verdict vocabulary."""

    verdict = "inconclusive"
    reasoning = []
    for line in text.splitlines():
        clean = re.sub(r"[*_`]+", "", line).strip().lower()
        if clean.startswith("verdict:"):
            candidate = clean.split(":", 1)[1].strip().rstrip(".,;:")
            if candidate in {"confirmed", "refuted", "inconclusive"}:
                verdict = candidate
            continue
        reasoning.append(line)
    return verdict, "\n".join(reasoning).strip()
