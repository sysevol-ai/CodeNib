# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Codex Agent wrapper for code localization.

Mirror of ClaudeLocAgent using the official OpenAI Codex Python SDK
(``openai-codex``, top-level module ``openai_codex``, from
github.com/openai/codex sdk/python). Read-only: explores a repository to
identify code locations relevant to an issue/query, without modifying
files.

The SDK speaks JSON-RPC v2 to a co-installed ``openai-codex-cli-bin``
wheel that ships the platform-specific ``codex`` binary -- no system
PATH dependency. Auth is picked up from ``~/.codex/auth.json`` (set by
``codex login``) unless ``CODEX_API_KEY`` is exported.

This SDK is **not yet on PyPI** as of 2026-05; install from git, pinned
to a verified commit (see examples/codex_loc_agent.py docstring).
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List, Optional

from openai_codex import ApprovalMode, AsyncCodex
from openai_codex.types import SandboxMode

from ..log_utils import get_logger
from .claude_agent import (
    LOC_OUTPUT_SCHEMA,
    LOC_SYMBOL_NAMING_RULES,
    CodeSymbol,
    LocResult,
)

_DEFAULT_SYSTEM_PROMPT = (
    "You are an expert at code analysis and issue localization. "
    "Your task is to identify the exact code locations in a repository "
    "that are relevant to a given issue or query.\n\n"
    "CRITICAL RULES:\n"
    "- You are a READ-ONLY agent. DO NOT modify, create, or delete any files.\n"
    "- Thoroughly explore the codebase using your available read and shell "
    "tools.\n"
    "- Trace through call chains, imports, and dependencies to find ALL "
    "relevant locations.\n"
    "- For each relevant symbol (function, class, method, field, etc.), "
    "identify its location and classify the action to be done on it as "
    '"modify", "add", or "delete" based on what would be needed to '
    "resolve the issue.\n"
    "- " + LOC_SYMBOL_NAMING_RULES + "- Order symbols by relevance (most "
    "relevant first).\n"
    "\nBUDGET CONSTRAINT:\n"
    "- You have a LIMITED number of tool calls. Be efficient and focused.\n"
    "- Start with targeted searches (grep for keywords from the issue), "
    "NOT broad directory listings.\n"
    "- Once you have identified the relevant files, read only the "
    "necessary sections.\n"
    "- Do NOT exhaustively explore every file. Focus on the most likely "
    "locations first.\n"
    "- Return the final answer as a JSON object matching the provided "
    "output_schema (a `locations` array of symbol objects).\n"
)


class CodexLocAgent:
    """
    Read-only code localization agent powered by the official OpenAI Codex SDK.

    Sandbox is pinned to ``SandboxMode.read_only`` and approvals to
    ``ApprovalMode.deny_all`` so the agent cannot write to disk or escalate.
    The system prompt is passed via ``base_instructions`` (the SDK's
    canonical place) rather than folded into the user message.
    """

    def __init__(
        self,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        approval_mode: ApprovalMode = ApprovalMode.deny_all,
        sandbox: SandboxMode = SandboxMode.read_only,
        reasoning_effort: str = "medium",
    ):
        self.logger = get_logger(__name__)
        self.model = model
        self.system_prompt = system_prompt or _DEFAULT_SYSTEM_PROMPT
        self.approval_mode = approval_mode
        self.sandbox = sandbox
        self.reasoning_effort = reasoning_effort

    async def locate_code(
        self,
        query_text: str,
        repo_path: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> LocResult:
        """
        Locate relevant code positions for a given query in a repository.

        Args:
            query_text: The issue description or query to localize.
            repo_path: Path to the repository root.
            context: Additional context with keys like
                     'issue_title', 'issue_body', 'diff', 'hints'.

        Returns:
            LocResult with identified code locations and symbol details.
        """
        if not os.path.isdir(repo_path):
            return LocResult(
                success=False,
                repo_path=repo_path,
                error_message=f"Repository path does not exist: {repo_path}",
            )

        user_prompt = self._prepare_user_prompt(query_text, context)

        try:
            async with AsyncCodex() as codex:
                thread = await codex.thread_start(
                    model=self.model,
                    base_instructions=self.system_prompt,
                    cwd=str(repo_path),
                    sandbox=self.sandbox,
                    approval_mode=self.approval_mode,
                    config={"model_reasoning_effort": self.reasoning_effort},
                )

                self.logger.info(f"Starting code localization in: {repo_path}")
                self.logger.debug(f"Query: {query_text[:200]}...")

                result = await thread.run(
                    user_prompt,
                    output_schema=LOC_OUTPUT_SCHEMA,
                )

            usage_info = self._extract_usage(result)
            self._log_usage(usage_info)

            locations = self._parse_locations(result.final_response)
            execution_log = [str(item) for item in result.items]

            return LocResult(
                success=True,
                repo_path=repo_path,
                locations=locations,
                execution_log=execution_log,
                usage=usage_info or None,
            )

        except Exception as e:
            error_msg = f"Error during code localization: {str(e)}"
            self.logger.error(error_msg)
            return LocResult(
                success=False,
                repo_path=repo_path,
                error_message=error_msg,
            )

    def _prepare_user_prompt(
        self, query_text: str, context: Optional[Dict[str, Any]]
    ) -> str:
        """Build the user-facing prompt; system rules are sent via base_instructions."""
        parts: List[str] = []

        if context:
            if "issue_title" in context:
                parts.append(f"## Issue Title\n{context['issue_title']}\n")
            if "issue_body" in context:
                parts.append(f"## Issue Body\n{context['issue_body']}\n")
            if "diff" in context:
                parts.append(f"## Related Diff\n```diff\n{context['diff']}\n```\n")
            if "hints" in context:
                parts.append(f"## Hints\n{context['hints']}\n")

        parts.append(f"## Query\n{query_text}")

        parts.append(
            "\n## Instructions\n"
            "1. Explore the repository to understand its structure.\n"
            "2. Identify ALL code symbols relevant to the query above.\n"
            "3. For each relevant symbol, note the name (in the canonical "
            "format described in your base instructions), type, file path "
            "(relative to repo root), line range, and what action is needed.\n"
            "4. Return the final answer as a JSON object matching the "
            "provided output_schema.\n"
            "5. DO NOT modify any files. This is a read-only analysis task.\n"
        )

        return "\n".join(parts)

    def _extract_usage(self, result: Any) -> Dict[str, Any]:
        """Normalize the official SDK's TurnResult.usage into a flat dict."""
        info: Dict[str, Any] = {
            "duration_ms": getattr(result, "duration_ms", None),
            "status": (
                result.status.value if getattr(result, "status", None) else None
            ),
            "num_items": len(getattr(result, "items", []) or []),
        }
        usage = getattr(result, "usage", None)
        if usage is not None and getattr(usage, "total", None) is not None:
            total = usage.total
            info.update(
                {
                    "input_tokens": total.input_tokens,
                    "cached_input_tokens": total.cached_input_tokens,
                    "output_tokens": total.output_tokens,
                    "reasoning_output_tokens": total.reasoning_output_tokens,
                    "total_tokens": total.total_tokens,
                }
            )
        return info

    def _log_usage(self, usage_info: Dict[str, Any]) -> None:
        if not usage_info:
            return
        self.logger.info(
            "Usage: status=%s, input=%s, cached=%s, output=%s, "
            "reasoning=%s, total=%s, duration_ms=%s, items=%s",
            usage_info.get("status"),
            usage_info.get("input_tokens"),
            usage_info.get("cached_input_tokens"),
            usage_info.get("output_tokens"),
            usage_info.get("reasoning_output_tokens"),
            usage_info.get("total_tokens"),
            usage_info.get("duration_ms"),
            usage_info.get("num_items"),
        )

    def _parse_locations(self, final_response: Optional[str]) -> List[CodeSymbol]:
        """
        Parse CodeSymbol objects from Codex's schema-validated JSON response.

        With ``output_schema`` set, ``final_response`` is a JSON string
        matching LOC_OUTPUT_SCHEMA -- a direct ``json.loads`` of
        ``{"locations": [...]}``.
        """
        if not final_response:
            self.logger.warning("Codex returned empty final_response.")
            return []
        try:
            data = json.loads(final_response)
        except json.JSONDecodeError:
            self.logger.warning(
                "Codex final_response is not valid JSON: %r",
                final_response[:200],
            )
            return []

        if isinstance(data, dict) and isinstance(data.get("locations"), list):
            items = data["locations"]
        elif isinstance(data, list):
            items = data
        else:
            self.logger.warning("Unexpected output structure: %s", type(data))
            return []

        symbols = []
        for item in items:
            if not isinstance(item, dict) or "name" not in item:
                continue
            symbols.append(
                CodeSymbol(
                    name=item.get("name", ""),
                    type=item.get("type", ""),
                    file_path=item.get("file_path", ""),
                    line_start=int(item.get("line_start", 0)),
                    line_end=int(item.get("line_end", 0)),
                    action=item.get("action", ""),
                    description=item.get("description", ""),
                )
            )
        return symbols


class SyncCodexLocAgent:
    """Synchronous wrapper for CodexLocAgent."""

    def __init__(self, **kwargs):
        self.agent = CodexLocAgent(**kwargs)
        self.logger = get_logger(__name__)

    def locate_code(
        self,
        query_text: str,
        repo_path: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> LocResult:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(
                    self.agent.locate_code(
                        query_text=query_text,
                        repo_path=repo_path,
                        context=context,
                    )
                )
            finally:
                loop.close()
        except Exception as e:
            error_msg = f"Error in synchronous code localization: {str(e)}"
            self.logger.error(error_msg)
            return LocResult(
                success=False,
                repo_path=repo_path,
                error_message=error_msg,
            )
