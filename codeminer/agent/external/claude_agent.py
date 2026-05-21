# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Claude Agent wrapper for code localization.

This module provides a wrapper around the Claude Agent SDK to serve as
a read-only code localization agent: given a repository and an issue/query,
it identifies the relevant code locations (with symbol-level detail)
without making any modifications.
"""

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from claude_agent_sdk import ClaudeAgentOptions, query

from ...log_utils import get_logger

# Canonical chunker-format rules for the `name` field. Shared with
# codex_agent so both system prompts derive from a single source of truth.
LOC_SYMBOL_NAMING_RULES = (
    "Symbol naming follows the dataset's canonical chunker format. "
    "Exactly one of three shapes:\n"
    "    bare_function()              (free function/var, any lang)\n"
    "    Class.method()               (method on class/type)\n"
    "    ClassName                    (class/struct/type decl, NO parens)\n"
    "  STRICT RULES:\n"
    '  * Use "." (period) between class and method, NEVER "::" '
    "(even for C++/Rust source). Convert `Type::method` -> `Type.method`.\n"
    "  * STRIP all prefix qualifiers (namespace / module / package):\n"
    "      C++   `fmt::detail::write_bytes()`         -> `write_bytes()`\n"
    "      Rust  `crate::sys::expand_glob()`          -> `expand_glob()`\n"
    "      Rust  `State::visit_token_kind()` -> `State.visit_token_kind()`\n"
    "      Python `xarray.core.var.Variable.quantile()` -> `Variable.quantile()`\n"
    "      Go    `logging.CookieFilter.Filter()`      -> `CookieFilter.Filter()`\n"
    "      Go    `(*Server).ServeHTTP()`              -> `Server.ServeHTTP()`\n"
    "  * STRIP parameter signatures: `Foo.bar(int x)` -> `Foo.bar()`.\n"
    "  * Keep the IMMEDIATE enclosing class/type qualifier only "
    '(at most one ".").\n'
    "  * Be consistent across ALL symbols.\n"
)


# JSON Schema for structured output from the localization agent
LOC_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "locations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        # GT (from the codeminer code-chunking pipeline) is
                        # exactly one of:
                        #   bare_function()
                        #   Class.method()
                        #   ClassName             (no parens)
                        # so the canonical form has 0 or 1 dot. The `::`
                        # operator and parameter signatures are rejected
                        # outright; namespace / module / package prefixes
                        # are partially rejected (any 2+ dot form will be
                        # forced into a single-dot form by schema retry).
                        "pattern": (
                            r"^[A-Za-z_][A-Za-z_0-9]*"
                            r"(\.[A-Za-z_][A-Za-z_0-9]*)?"
                            r"(\(\))?$"
                        ),
                    },
                    "type": {
                        "type": "string",
                        "enum": [
                            "function",
                            "class",
                            "method",
                            "field",
                            "module",
                            "variable",
                        ],
                    },
                    "file_path": {"type": "string"},
                    "line_start": {"type": "integer"},
                    "line_end": {"type": "integer"},
                    "action": {
                        "type": "string",
                        "enum": ["modify", "add", "delete"],
                    },
                    "description": {"type": "string"},
                },
                "required": [
                    "name",
                    "type",
                    "file_path",
                    "line_start",
                    "line_end",
                    "action",
                    "description",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["locations"],
    "additionalProperties": False,
}


@dataclass
class CodeSymbol:
    """A code symbol with its location, identified as relevant to an issue."""

    name: str
    type: str  # e.g. "function", "class", "method", "field"
    file_path: str
    line_start: int
    line_end: int
    action: str  # "modify", "add", "delete"
    description: str = ""  # brief description of the change or relevance


@dataclass
class LocResult:
    """Result from code localization."""

    success: bool
    repo_path: str
    locations: List[CodeSymbol] = field(default_factory=list)
    error_message: Optional[str] = None
    execution_log: List[str] = field(default_factory=list)
    usage: Optional[Dict[str, Any]] = None


class ClaudeLocAgent:
    """
    Read-only code localization agent powered by Claude Agent SDK.

    Given a repository path and an issue/query, this agent explores the
    codebase and identifies the code locations (with symbol-level detail)
    that are relevant or need modification — without actually making any changes.
    """

    def __init__(
        self,
        max_turns: int = 100,
        system_prompt: Optional[str] = None,
        allowed_tools: Optional[List[str]] = None,
        permission_mode: str = "bypassPermissions",
        model: str = "sonnet",
    ):
        self.logger = get_logger(__name__)
        self.max_turns = max_turns
        self.permission_mode = permission_mode
        self.model = model

        self.system_prompt = system_prompt or (
            "You are an expert at code analysis and issue localization. "
            "Your task is to identify the exact code locations in a repository "
            "that are relevant to a given issue or query.\n\n"
            "CRITICAL RULES:\n"
            "- You are a READ-ONLY agent. DO NOT modify, create, or delete any files.\n"
            "- Thoroughly explore the codebase using the available tools (Read, Glob, "
            "Grep, Bash for git commands, etc.).\n"
            "- Trace through call chains, imports, and dependencies to find ALL "
            "relevant locations.\n"
            "- For each relevant symbol (function, class, method, field, etc.), "
            "identify its location and classify the action to be done on it "
            'as "modify", "add", or "delete" based on what would be needed '
            "to resolve the issue.\n"
            "- At the END of your analysis, you MUST output a single fenced JSON "
            "code block (```json ... ```) containing an array of symbol objects.\n"
            "- Each symbol object must have these fields:\n"
            '    "name", "type", "file_path", "line_start", "line_end", '
            '"action", "description".\n'
            "- " + LOC_SYMBOL_NAMING_RULES + ""
            '- "type" is one of function/class/method/field; "action" is '
            'one of modify/add/delete; "file_path" is relative to repo root.\n'
            "- Order symbols by relevance (most relevant first).\n"
            "\nIMPORTANT BUDGET CONSTRAINT:\n"
            "- You have a LIMITED number of tool calls. Be efficient and focused.\n"
            "- Start with targeted searches (grep for keywords from the issue), "
            "NOT broad directory listings.\n"
            "- Once you have identified the relevant files, read only the "
            "necessary sections.\n"
            "- Do NOT exhaustively explore every file. Focus on the most likely "
            "locations first.\n"
            "- You MUST output the JSON result block before running out of turns. "
            "If you have a reasonable set of locations, output the JSON rather "
            "than continuing to explore.\n"
        )

        # Read-only tool set — no Edit/Write/NotebookEdit
        self.allowed_tools = allowed_tools or [
            "Task",
            "Bash",
            "Glob",
            "Grep",
            "Read",
        ]

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

        full_prompt = self._prepare_prompt(query_text, context)

        try:
            options = ClaudeAgentOptions(
                max_turns=self.max_turns,
                system_prompt=self.system_prompt,
                cwd=Path(repo_path),
                allowed_tools=self.allowed_tools,
                permission_mode=self.permission_mode,
                model=self.model,
                output_format={
                    "type": "json_schema",
                    "schema": LOC_OUTPUT_SCHEMA,
                },
            )

            self.logger.info(f"Starting code localization in: {repo_path}")
            self.logger.debug(f"Query: {query_text[:200]}...")

            execution_log = []
            structured_result = None
            turn_count = 0
            usage_info = {}
            async for message in query(prompt=full_prompt, options=options):
                msg_type = message.__class__.__name__

                # Count AssistantMessage instances (includes sub-agent messages)
                if msg_type == "AssistantMessage":
                    turn_count += 1
                    if turn_count % 10 == 0:
                        self.logger.info(
                            f"  ... {turn_count} assistant messages received"
                        )

                # Extract result from ResultMessage
                if msg_type == "ResultMessage":
                    # Prefer structured_output from output_format schema
                    structured = getattr(message, "structured_output", None)
                    if structured is not None:
                        structured_result = structured
                    if getattr(message, "result", None):
                        execution_log.append(message.result)
                    usage = getattr(message, "usage", None) or {}
                    usage_info = {
                        "num_turns": getattr(message, "num_turns", None),
                        "total_cost_usd": getattr(message, "total_cost_usd", None),
                        "duration_ms": getattr(message, "duration_ms", None),
                        "input_tokens": usage.get("input_tokens", 0),
                        "output_tokens": usage.get("output_tokens", 0),
                        "cache_read_input_tokens": usage.get(
                            "cache_read_input_tokens", 0
                        ),
                        "cache_creation_input_tokens": usage.get(
                            "cache_creation_input_tokens", 0
                        ),
                    }
                else:
                    execution_log.append(str(message))

            # Log usage summary
            if usage_info:
                self.logger.info(
                    f"Usage: turns={usage_info['num_turns']}, "
                    f"cost=${usage_info['total_cost_usd']:.4f}, "
                    f"input={usage_info['input_tokens']}, "
                    f"cache_read={usage_info['cache_read_input_tokens']}, "
                    f"cache_create={usage_info['cache_creation_input_tokens']}, "
                    f"output={usage_info['output_tokens']}"
                )

            # Check if agent was truncated by max_turns
            sdk_turns = usage_info.get("num_turns")
            if sdk_turns is not None and sdk_turns >= self.max_turns:
                self.logger.warning(
                    f"Agent reached max_turns limit ({self.max_turns}). "
                    "Results may be incomplete."
                )
            else:
                self.logger.info(
                    f"Agent finished in {sdk_turns} SDK turns "
                    f"({turn_count} assistant messages)."
                )

            # Parse locations: prefer structured output, fall back to regex
            if structured_result is not None:
                self.logger.info(
                    "Parsing locations from structured_output (schema-validated)"
                )
                locations = self._parse_structured(structured_result)
            else:
                self.logger.info(
                    "No structured_output found, falling back to regex parsing"
                )
                locations = self._parse_locations(execution_log)

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

    def _prepare_prompt(
        self, query_text: str, context: Optional[Dict[str, Any]]
    ) -> str:
        """
        Build the full prompt for the localization task.

        Args:
            query_text: The main query / issue description.
            context: Optional context dict with keys:
                     'issue_title', 'issue_body', 'diff', 'hints'.

        Returns:
            Assembled prompt string.
        """
        parts = []

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
            "3. For each relevant symbol, note the name, type, file path "
            "(relative to repo root), line range, and what action is needed.\n"
            "4. At the very end, output a single fenced JSON code block with "
            "the results in this format:\n"
            "```json\n"
            "[\n"
            "  {\n"
            '    "name": "Foo.bar()",\n'
            '    "type": "method",\n'
            '    "file_path": "src/foo.cpp",\n'
            '    "line_start": 42,\n'
            '    "line_end": 55,\n'
            '    "action": "modify",\n'
            '    "description": "Needs to handle + in email"\n'
            "  }\n"
            "]\n"
            "```\n"
            "5. The `name` field must follow the canonical chunker format "
            "described in your system instructions (no `::`, no namespace "
            "prefix, no parameter signatures).\n"
            "6. DO NOT modify any files. This is a read-only analysis task.\n"
        )

        return "\n".join(parts)

    def _parse_structured(self, data: Any) -> List[CodeSymbol]:
        """Parse CodeSymbol objects from structured_output (schema-validated JSON)."""
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                self.logger.warning("structured_output is a string but not valid JSON.")
                return []

        items = []
        if isinstance(data, dict) and isinstance(data.get("locations"), list):
            items = data["locations"]
        elif isinstance(data, list):
            items = data
        else:
            self.logger.warning("Unexpected structured_output format: %s", type(data))
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

    def _parse_locations(self, execution_log: List[str]) -> List[CodeSymbol]:
        """
        Parse CodeSymbol objects from the agent's execution log.

        Scans the log (last-to-first) for a fenced ```json code block
        containing an array of symbol objects.

        Args:
            execution_log: List of stringified agent messages.

        Returns:
            Parsed list of CodeSymbol, or empty list on failure.
        """
        json_pattern = re.compile(r"```json\s*\n(.*?)\n\s*```", re.DOTALL)

        for entry in reversed(execution_log):
            match = json_pattern.search(entry)
            if not match:
                continue

            try:
                data = json.loads(match.group(1))
            except json.JSONDecodeError:
                self.logger.warning("Found JSON block but failed to parse it.")
                continue

            if not isinstance(data, list):
                continue

            symbols = []
            for item in data:
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

        self.logger.warning("No valid JSON symbol block found in execution log.")
        return []


# Synchronous wrapper
class SyncClaudeLocAgent:
    """
    Synchronous wrapper for ClaudeLocAgent.
    """

    def __init__(self, **kwargs):
        self.agent = ClaudeLocAgent(**kwargs)
        self.logger = get_logger(__name__)

    def locate_code(
        self,
        query_text: str,
        repo_path: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> LocResult:
        """
        Synchronous wrapper for locate_code.

        Args:
            query_text: The issue description or query.
            repo_path: Path to the repository root.
            context: Optional additional context.

        Returns:
            LocResult with identified code locations.
        """
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
