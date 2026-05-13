# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Lightweight agent runner using LLM tool calling.

Implements a think → act → observe loop that lets an LLM decide which
CodeMiner skills to invoke, execute them, and iterate until the LLM
produces a final answer.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Set

from ..llm.litellm_chat import LiteLLMChat
from ..llm.usage import UsageTracker
from ..log_utils import get_logger
from .agent_types import AgentResult, ToolCallRecord
from .skills.registry import SkillRegistry
from .tool_schema import registry_to_tools

logger = get_logger(__name__)

_DEFAULT_SYSTEM_PROMPT = """\
You are a code search agent. You have access to tools that search a \
codebase and retrieve relevant code snippets. Use the tools iteratively \
to find the information needed, then provide a concise answer.

Guidelines:
- Start with broad searches, then narrow down.
- Use graph_expand to find structurally related code after an initial search.
- When you have enough context, provide a final answer directly.
- Prefer lower-cost skills unless the query clearly requires semantic understanding.
- For simple exact-name lookups use bm25_search; \
for conceptual / intent queries use embedding_search; \
for maximum coverage use hybrid_search.
"""

# Maximum characters for a single tool result to avoid context blowup.
_MAX_RESULT_CHARS = 16_000


class AgentRunner:
    """LLM-driven agent loop over the CodeMiner skill registry.

    Usage::

        from codeminer.agent.skills.registry import SkillRegistry

        runner = AgentRunner(model="gpt-4o", registry=SkillRegistry())
        result = runner.run("How does authentication work in this repo?")
        print(result.answer)
    """

    def __init__(
        self,
        llm: Optional[LiteLLMChat] = None,
        registry: Optional[SkillRegistry] = None,
        *,
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 512,
        system_prompt: Optional[str] = None,
        max_turns: int = 10,
        allow_skills: Optional[Set[str]] = None,
        exclude_skills: Optional[Set[str]] = None,
        manifest: Optional[Any] = None,
        session_ctx: Optional[Any] = None,
    ) -> None:
        if llm is not None:
            self.llm = llm
        elif model is not None:
            self.llm = LiteLLMChat(
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        else:
            raise ValueError("Either 'llm' or 'model' must be provided")
        self.registry = registry or SkillRegistry()
        self.max_turns = max_turns
        self.session_ctx = session_ctx

        # Resource guard: filter unavailable skills and collect warnings
        allow = set(allow_skills) if allow_skills is not None else None
        exclude = set(exclude_skills) if exclude_skills else set()
        resource_warnings: List[str] = []

        if manifest is not None:
            from .resource_guard import ResourceGuard

            guard = ResourceGuard(manifest, self.registry)
            report = guard.preflight()
            exclude |= report.unavailable
            resource_warnings = report.warnings

        self.tools = registry_to_tools(self.registry, allow=allow, exclude=exclude)

        # Build system prompt with optional resource warnings
        base_prompt = system_prompt or _DEFAULT_SYSTEM_PROMPT
        if resource_warnings:
            warnings_text = "\n".join(f"- {w}" for w in resource_warnings)
            base_prompt += f"\nIndex warnings:\n{warnings_text}\n"
        self.system_prompt = base_prompt

    def run(
        self,
        query: str,
        *,
        max_turns: Optional[int] = None,
    ) -> AgentResult:
        """Execute the agent loop and return the result."""
        max_turns = max_turns or self.max_turns
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": query},
        ]
        all_tool_calls: List[ToolCallRecord] = []
        usage_tracker = UsageTracker()
        start = time.monotonic()

        for turn in range(max_turns):
            logger.debug("agent turn %d/%d", turn + 1, max_turns)

            call_kwargs: Dict[str, Any] = {
                "usage_tracker": usage_tracker,
                "usage_turn": turn + 1,
            }
            if self.tools:
                call_kwargs["tools"] = self.tools

            response = self.llm._call_raw(messages, **call_kwargs)
            choice = response.choices[0]
            assistant_msg = choice.message

            # Append assistant message to conversation
            messages.append(_message_to_dict(assistant_msg))

            # Check for tool calls
            tool_calls = getattr(assistant_msg, "tool_calls", None)
            if not tool_calls:
                # Terminal: LLM produced a final answer
                answer = getattr(assistant_msg, "content", None) or ""
                elapsed = (time.monotonic() - start) * 1000
                return AgentResult(
                    answer=answer,
                    tool_calls=all_tool_calls,
                    messages=messages,
                    total_turns=turn + 1,
                    total_duration_ms=elapsed,
                    usage=usage_tracker.totals(),
                    usage_records=list(usage_tracker.records),
                )

            # Execute each tool call
            for tc in tool_calls:
                record = self._execute_tool_call(tc)
                all_tool_calls.append(record)

                # Append tool response message
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": _serialize_result(
                            record.result if record.error is None else record.error
                        ),
                    }
                )

        # Max turns exhausted — return whatever we have
        elapsed = (time.monotonic() - start) * 1000
        last_content = ""
        for msg in reversed(messages):
            if msg.get("role") == "assistant" and msg.get("content"):
                last_content = msg["content"]
                break

        return AgentResult(
            answer=last_content,
            tool_calls=all_tool_calls,
            messages=messages,
            total_turns=max_turns,
            total_duration_ms=elapsed,
            usage=usage_tracker.totals(),
            usage_records=list(usage_tracker.records),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _execute_tool_call(self, tc: Any) -> ToolCallRecord:
        """Execute a single tool call from the LLM response."""
        skill_id = tc.function.name
        try:
            arguments = json.loads(tc.function.arguments)
        except (json.JSONDecodeError, TypeError):
            arguments = {}

        meta = self.registry.get(skill_id)
        if meta is None or meta.executor_fn is None:
            return ToolCallRecord(
                tool_call_id=tc.id,
                skill_id=skill_id,
                arguments=arguments,
                error=f"Skill {skill_id!r} not available",
            )

        # Apply parameter scaling if session context is available
        resolved_args = self._resolve_params(meta, arguments)

        start = time.monotonic()
        try:
            result = meta.executor_fn(**resolved_args)
            elapsed = (time.monotonic() - start) * 1000
            logger.debug(
                "tool %s completed in %.0fms",
                skill_id,
                elapsed,
            )
            return ToolCallRecord(
                tool_call_id=tc.id,
                skill_id=skill_id,
                arguments=arguments,
                result=result,
                duration_ms=elapsed,
            )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            logger.warning("tool %s failed: %s", skill_id, exc)
            return ToolCallRecord(
                tool_call_id=tc.id,
                skill_id=skill_id,
                arguments=arguments,
                error=str(exc),
                duration_ms=elapsed,
            )

    def _resolve_params(self, meta: Any, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Merge config defaults + session adjustments + LLM arguments."""
        if self.session_ctx is None:
            return arguments

        from ..compiler.params import resolve_params

        resolved = resolve_params(
            defaults=meta.defaults or {},
            session_ctx=self.session_ctx,
            query_params=arguments,
        )
        return resolved.params


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _message_to_dict(msg: Any) -> Dict[str, Any]:
    """Convert a litellm response message to a raw dict."""
    if hasattr(msg, "model_dump"):
        d = msg.model_dump(exclude_none=True)
    elif hasattr(msg, "to_dict"):
        d = msg.to_dict()
    else:
        d = {
            "role": getattr(msg, "role", "assistant"),
            "content": getattr(msg, "content", None),
        }
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            d["tool_calls"] = [
                tc.model_dump(exclude_none=True) if hasattr(tc, "model_dump") else tc
                for tc in tool_calls
            ]
    return d


def _serialize_result(result: Any) -> str:
    """Serialize a tool result to a string for the LLM."""
    if isinstance(result, str):
        text = result
    elif isinstance(result, (list, tuple)):
        items = []
        for item in result:
            if hasattr(item, "model_dump"):
                items.append(item.model_dump(exclude_none=True))
            elif hasattr(item, "__dict__"):
                items.append(item.__dict__)
            else:
                items.append(item)
        text = json.dumps(items, default=str, ensure_ascii=False)
    elif hasattr(result, "model_dump"):
        text = json.dumps(result.model_dump(exclude_none=True), default=str)
    else:
        try:
            text = json.dumps(result, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(result)

    if len(text) > _MAX_RESULT_CHARS:
        text = text[:_MAX_RESULT_CHARS] + "\n... (truncated)"
    return text
