# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Use the person's own Claude Code as a text model, with no API key.

A model named ``claude-code/<model>`` (``claude-code/sonnet``,
``claude-code/claude-sonnet-5-5``) is served by running ``claude -p`` on this
machine, signed in the way Claude Code already is. ``chat_for`` returns a
``ClaudeCodeChat`` for such a model, with the interface of ``LiteLLMChat``, so
the Wiki writer works unchanged and LiteLLM need not be installed.

Each call is a plain completion: the prompt goes in on stdin, the system
messages become the system prompt, and Claude Code runs with no tools, no MCP
servers and no slash commands, from an empty directory so no project
instructions are picked up. Tool calls are not supported.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional

from ..log_utils import get_logger

logger = get_logger(__name__)

PROVIDER = "claude-code"
PREFIX = f"{PROVIDER}/"
DEFAULT_MODEL = f"{PREFIX}sonnet"
DEFAULT_TIMEOUT_SECONDS = 600.0
_SECRET_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN")
_DEFAULT_SYSTEM = (
    "You are a writing model called through an API. Answer the request "
    "exactly as asked, in the format it asks for, with no preamble."
)


class ClaudeCodeError(RuntimeError):
    """Claude Code is missing, not signed in, or returned no answer."""


def is_claude_code_model(model: Optional[str]) -> bool:
    return bool(model) and str(model).startswith(PREFIX)


def available() -> bool:
    return (
        shutil.which(os.environ.get("CODENIB_CLAUDE_EXECUTABLE", "claude")) is not None
    )


def _text(content: Any) -> str:
    """Message content as text: a string, or the text parts of a list."""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return "" if content is None else str(content)


def render_prompt(messages: List[Dict[str, Any]]) -> tuple[str, str]:
    """System text and the conversation as one prompt.

    A single user message is passed as it is. A longer conversation is
    written out turn by turn, ending with the message to answer.
    """

    system = "\n\n".join(
        _text(m.get("content")) for m in messages if m.get("role") == "system"
    ).strip()
    turns = [m for m in messages if m.get("role") in ("user", "assistant")]
    if any(m.get("tool_calls") or m.get("role") == "tool" for m in messages):
        raise ClaudeCodeError("claude-code models do not support tool calls")
    if len(turns) == 1:
        return system, _text(turns[0].get("content"))
    lines = []
    for message in turns:
        speaker = "User" if message["role"] == "user" else "Assistant"
        lines.append(f"{speaker}:\n{_text(message.get('content')).strip()}")
    lines.append("Reply as the assistant to the last user message.")
    return system, "\n\n".join(lines)


def run(
    prompt: str,
    *,
    model: str,
    system: str = "",
    json_schema: Optional[Dict[str, Any]] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Run one completion; return ``{"text", "structured", "usage"}``."""

    executable = os.environ.get("CODENIB_CLAUDE_EXECUTABLE", "claude")
    name = model[len(PREFIX) :] if is_claude_code_model(model) else model
    argv = [
        executable,
        "-p",
        "--output-format",
        "json",
        "--tools",
        "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--system-prompt",
        system or _DEFAULT_SYSTEM,
    ]
    if name:
        argv += ["--model", name]
    if json_schema:
        argv += ["--json-schema", json.dumps(json_schema, separators=(",", ":"))]
    env = dict(os.environ)
    for key in _SECRET_ENV:
        env.pop(key, None)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="codenib-claude-") as empty:
        try:
            completed = subprocess.run(
                argv,
                input=prompt,
                cwd=empty,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ClaudeCodeError(
                f"{executable} is not installed; install Claude Code and sign in"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ClaudeCodeError(
                f"Claude Code did not answer within {int(timeout)} s"
            ) from exc
    try:
        data = json.loads(completed.stdout)
    except ValueError:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise ClaudeCodeError(
            "Claude Code returned no result"
            + (f": {detail[-1][:300]}" if detail else "")
        ) from None
    record = data[-1] if isinstance(data, list) else data
    text = str(record.get("result") or "")
    if record.get("is_error") or completed.returncode != 0:
        raise ClaudeCodeError(
            f"Claude Code failed: {text[:300] or completed.returncode}"
        )
    usage = record.get("usage") or {}
    logger.info(
        "Claude Code (%s) answered in %.1f s, $%.4f",
        name or "default",
        time.monotonic() - started,
        float(record.get("total_cost_usd") or 0.0),
    )
    return {
        "text": text,
        "structured": record.get("structured_output"),
        "usage": {
            "prompt_tokens": sum(
                int(usage.get(key) or 0)
                for key in (
                    "input_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                )
            ),
            "completion_tokens": int(usage.get("output_tokens") or 0),
            "cost_usd": float(record.get("total_cost_usd") or 0.0),
        },
    }


class _Answer:
    """The slice of a chat-completion response that callers read."""

    def __init__(self, content: str):
        message = type("Message", (), {"content": content, "role": "assistant"})()
        choice = type("Choice", (), {"message": message, "finish_reason": "stop"})()
        self.choices = [choice]


class ClaudeCodeChat:
    """``LiteLLMChat``'s interface, answered by the local ``claude`` CLI.

    Needs no API key and no LiteLLM. ``temperature`` and ``max_tokens`` are
    accepted for compatibility; Claude Code decides both.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **_ignored: Any,
    ):
        self.model = model if is_claude_code_model(model) else PREFIX + model
        self.temperature = temperature
        self.max_tokens = max_tokens

    @property
    def cache_identity(self) -> str:
        return hashlib.sha256(
            json.dumps({"model": self.model}, sort_keys=True).encode()
        ).hexdigest()[:20]

    def complete(self, messages: List[Dict[str, Any]], **_overrides: Any) -> str:
        system, prompt = render_prompt(messages)
        return run(prompt, model=self.model, system=system)["text"].strip()

    def invoke(self, messages: List[Any]) -> str:
        return self.complete([{"role": m.role, "content": m.content} for m in messages])

    def _call(self, messages: List[Any], **_overrides: Any) -> _Answer:
        return _Answer(self.invoke(messages))

    def with_structured_output(self, schema: Any) -> "_StructuredClaudeCode":
        return _StructuredClaudeCode(self, schema)


class _StructuredClaudeCode:
    def __init__(self, chat: ClaudeCodeChat, schema: Any):
        self.chat = chat
        self.schema = schema

    def invoke(self, messages: List[Any]) -> Any:
        system, prompt = render_prompt(
            [{"role": m.role, "content": m.content} for m in messages]
        )
        answer = run(
            prompt,
            model=self.chat.model,
            system=system,
            json_schema=self.schema.model_json_schema(),
        )
        if isinstance(answer["structured"], dict):
            return self.schema.model_validate(answer["structured"])
        return self.schema.model_validate_json(answer["text"])


def chat_for(model: str, **kwargs: Any) -> Any:
    """The chat client for a model: Claude Code for ``claude-code/*``."""

    if is_claude_code_model(model):
        return ClaudeCodeChat(model, **kwargs)
    from .litellm_chat import LiteLLMChat

    return LiteLLMChat(model=model, **kwargs)
