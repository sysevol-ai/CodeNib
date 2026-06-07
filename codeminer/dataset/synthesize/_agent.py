# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Shared agent runner for the query synthesis pipeline."""

from __future__ import annotations

import asyncio
import json
import re
from typing import List, Optional

from claude_agent_sdk import ClaudeAgentOptions, query

from codeminer.log_utils import get_logger

logger = get_logger(__name__)


class AgentRunner:
    """Thin wrapper around the Claude Agent SDK for running LLM calls.

    Each synthesis module (ContextLoader, QueryCurator, Verifier) gets its
    own ``AgentRunner`` instance, potentially with different ``allowed_tools``
    configurations.
    """

    def __init__(
        self,
        *,
        model: str = "opus",
        max_turns: int = 10,
        system_prompt: str = "",
        allowed_tools: Optional[List[str]] = None,
        permission_mode: str = "bypassPermissions",
    ) -> None:
        self.model = model
        self.max_turns = max_turns
        self.system_prompt = system_prompt
        self.allowed_tools = allowed_tools or []
        self.permission_mode = permission_mode

    def run(self, prompt: str, *, cwd: Optional[str] = None) -> str:
        """Synchronous agent call (delegates to async via ``asyncio.run``)."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.run_async(prompt, cwd=cwd))
        raise RuntimeError(
            "Running inside an active event loop. Use run_async instead."
        )

    async def run_async(self, prompt: str, *, cwd: Optional[str] = None) -> str:
        """Asynchronous agent call — streams messages from Claude."""
        options = ClaudeAgentOptions(
            max_turns=self.max_turns,
            system_prompt=self.system_prompt,
            allowed_tools=self.allowed_tools,
            permission_mode=self.permission_mode,
            model=self.model,
            cwd=cwd,
        )
        text_chunks: List[str] = []
        final_text: Optional[str] = None
        query_gen = query(prompt=prompt, options=options)
        completed = False

        try:
            async for message in query_gen:
                msg_type = message.__class__.__name__
                logger.debug(f"Agent message type: {msg_type}")

                if msg_type == "ResultMessage":
                    result = getattr(message, "result", None)
                    completed = True
                    if result and isinstance(result, str):
                        final_text = result.strip()
                    else:
                        final_text = "\n".join(text_chunks).strip()
                    continue

                if msg_type == "ErrorMessage":
                    error_msg = getattr(message, "error", "Unknown error")
                    logger.error(f"Agent error message: {error_msg}")
                    raise RuntimeError(f"Agent returned error: {error_msg}")

                if msg_type == "AssistantMessage":
                    content = getattr(message, "content", None)
                    if not content:
                        continue
                    block_texts: List[str] = []
                    for block in content:
                        text = getattr(block, "text", None)
                        if isinstance(text, str) and text.strip():
                            block_texts.append(text.strip())
                    if block_texts:
                        text_chunks.append("\n".join(block_texts))
        finally:
            if not completed:
                await query_gen.aclose()

        return final_text if final_text is not None else "\n".join(text_chunks).strip()

    @staticmethod
    def extract_json(text: str) -> str:
        """Extract the first valid JSON object from *text*."""
        text = text.strip()
        if not text:
            return text

        fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", text)
        if fenced:
            text = fenced.group(1).strip()

        decoder = json.JSONDecoder()
        for idx, ch in enumerate(text):
            if ch != "{":
                continue
            try:
                _obj, end = decoder.raw_decode(text[idx:])
                return text[idx : idx + end]
            except json.JSONDecodeError:
                continue
        return text
