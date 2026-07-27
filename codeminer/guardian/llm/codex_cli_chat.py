# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Codex-backed chat adapters.

This is intentionally a transport adapter, not a replacement agent harness:
Repository Guardian still controls the hypothesis loop, tool dispatch, memory,
and stopping policy.  Codex is only used as the model backend when a Guardian
model name starts with ``codex:``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from ...log_utils import get_logger

logger = get_logger(__name__)


def _message_content(message: Dict[str, Any]) -> str:
    content = message.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def _render_messages(messages: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    for message in messages:
        role = message.get("role", "user")
        if role == "tool":
            parts.append(
                f"[tool result: {message.get('tool_call_id', '')}]\n"
                f"{_message_content(message)}"
            )
            continue
        tool_calls = message.get("tool_calls") or []
        suffix = ""
        if tool_calls:
            suffix = "\nTool calls:\n" + json.dumps(tool_calls, ensure_ascii=False)
        parts.append(f"[{role}]\n{_message_content(message)}{suffix}")
    return "\n\n".join(parts)


def _tool_names(tools: List[Dict[str, Any]]) -> List[str]:
    names: List[str] = []
    for tool in tools:
        fn = tool.get("function") or {}
        name = fn.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _completion_instruction(max_tokens: Optional[int]) -> str:
    if max_tokens is None:
        return ""
    return (
        "Keep this response, including private reasoning, within approximately "
        f"{max(1, int(max_tokens))} tokens.\n\n"
    )


def _extract_json_object(text: str) -> Optional[dict]:
    def decode(
        candidate: str, *, tolerate_closing_braces: bool = False
    ) -> Optional[dict]:
        candidate = candidate.strip()
        try:
            value, end = json.JSONDecoder().raw_decode(candidate)
        except json.JSONDecodeError:
            return None
        remainder = candidate[end:].strip()
        if remainder and not (
            tolerate_closing_braces and not remainder.strip("}").strip()
        ):
            return None
        return value if isinstance(value, dict) else None

    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass

    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if match:
        return decode(match.group(1), tolerate_closing_braces=True)

    start = text.find("{")
    if start >= 0:
        # Codex occasionally appends one redundant closing brace to an otherwise
        # valid tool envelope.  Recover that unambiguous object without accepting
        # arbitrary prose or a second JSON value after it.
        return decode(text[start:], tolerate_closing_braces=True)
    return None


def _usage_from_stdout(stdout: str) -> SimpleNamespace:
    totals = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        for key in totals:
            totals[key] += int(usage.get(key, 0) or 0)

    completion = totals["output_tokens"] + totals["reasoning_output_tokens"]
    total = totals["input_tokens"] + completion
    return SimpleNamespace(
        prompt_tokens=totals["input_tokens"],
        completion_tokens=completion,
        total_tokens=total,
        cached_input_tokens=totals["cached_input_tokens"],
        output_tokens=totals["output_tokens"],
        reasoning_output_tokens=totals["reasoning_output_tokens"],
    )


def _response(content: str, *, tool_calls: Optional[list] = None, usage: object = None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


class _ToolResponseParseError(ValueError):
    """The model did not return a valid Guardian tool-protocol envelope."""


def _merge_usage(left: object, right: object) -> SimpleNamespace:
    """Add usage from an initial response and its single repair attempt."""

    fields = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    )
    return SimpleNamespace(
        **{
            field: int(getattr(left, field, 0) or 0)
            + int(getattr(right, field, 0) or 0)
            for field in fields
        }
    )


def _repair_prompt(content: str, error: Exception) -> str:
    return (
        "Your preceding response could not be parsed as a Guardian JSON "
        f"envelope: {error}. Reissue the same intended response as exactly one "
        "valid JSON object. Do not add Markdown or commentary.\n\n"
        f"Invalid response:\n{content}"
    )


def _usage_from_sdk(result: object) -> SimpleNamespace:
    usage = getattr(result, "usage", None)
    if usage is None:
        return SimpleNamespace(
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            cached_input_tokens=0,
            output_tokens=0,
            reasoning_output_tokens=0,
        )
    total = getattr(usage, "total", None)
    last = getattr(usage, "last", None)
    # A persistent agent-loop thread reports both cumulative and most-recent
    # usage. Guardian accounts per call, so prefer ``last`` to avoid counting
    # the same earlier turns again.
    breakdown = last or total
    input_tokens = getattr(breakdown, "input_tokens", 0) or 0
    output_tokens = getattr(breakdown, "output_tokens", 0) or 0
    reasoning = getattr(breakdown, "reasoning_output_tokens", 0) or 0
    # Some Codex SDK versions report ``total_tokens`` without reasoning output
    # even though they expose reasoning separately.  Normalize the public
    # OpenAI-compatible fields so total always equals prompt + completion.
    total_tokens = input_tokens + output_tokens + reasoning
    return SimpleNamespace(
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens + reasoning,
        total_tokens=total_tokens,
        cached_input_tokens=getattr(breakdown, "cached_input_tokens", 0) or 0,
        output_tokens=output_tokens,
        reasoning_output_tokens=reasoning,
    )


def _parse_tool_response(
    content: str,
    tools: list,
    usage: object,
    *,
    strict: bool = False,
) -> Any:
    data = _extract_json_object(content)
    allowed = set(_tool_names(tools))
    if not data:
        if strict:
            raise _ToolResponseParseError("response is not one valid JSON object")
        return _response(content.strip(), usage=usage)

    kind = str(data.get("type", "")).lower()
    if kind == "final":
        return _response(str(data.get("content", "")).strip(), usage=usage)

    if kind == "tool_calls":
        raw_calls = data.get("calls")
        if not isinstance(raw_calls, list) or not raw_calls:
            if strict:
                raise _ToolResponseParseError(
                    "tool_calls requires a non-empty calls array"
                )
            return _response(content.strip(), usage=usage)
        tool_calls = []
        for index, raw_call in enumerate(raw_calls):
            if not isinstance(raw_call, dict):
                if strict:
                    raise _ToolResponseParseError(
                        f"tool_calls item {index} is not an object"
                    )
                return _response(content.strip(), usage=usage)
            name = str(raw_call.get("name", ""))
            if name not in allowed:
                if strict:
                    raise _ToolResponseParseError(
                        f"unknown Guardian tool {name!r}; allowed tools: "
                        + ", ".join(sorted(allowed))
                    )
                return _response(content.strip(), usage=usage)
            args = raw_call.get("arguments")
            if not isinstance(args, dict):
                args = {}
            tool_calls.append(
                SimpleNamespace(
                    id=f"call_{uuid.uuid4().hex[:12]}",
                    function=SimpleNamespace(
                        name=name,
                        arguments=json.dumps(args, ensure_ascii=False),
                    ),
                )
            )
        return _response("", tool_calls=tool_calls, usage=usage)

    if kind == "tool_call" or (kind in allowed and "name" not in data):
        # Also accept the compact shape Codex sometimes emits:
        # {"type":"read_code","arguments":{...}}.  Restrict this recovery to
        # an exact allowed tool name so arbitrary response types stay invalid.
        name = str(data.get("name", "")) if kind == "tool_call" else kind
        if name in allowed:
            args = data.get("arguments")
            if not isinstance(args, dict):
                args = {}
            tc = SimpleNamespace(
                id=f"call_{uuid.uuid4().hex[:12]}",
                function=SimpleNamespace(
                    name=name,
                    arguments=json.dumps(args, ensure_ascii=False),
                ),
            )
            return _response("", tool_calls=[tc], usage=usage)
        if strict:
            raise _ToolResponseParseError(
                f"unknown Guardian tool {name!r}; allowed tools: "
                + ", ".join(sorted(allowed))
            )

    if strict:
        raise _ToolResponseParseError(
            f"unsupported Guardian response type {data.get('type')!r}"
        )
    return _response(content.strip(), usage=usage)


def _parse_tool_response_with_retry(
    content: str,
    tools: list,
    usage: object,
    retry: Any,
) -> Any:
    """Parse one response envelope, asking the same model session to repair once."""

    try:
        return _parse_tool_response(content, tools, usage, strict=True)
    except _ToolResponseParseError as first_error:
        logger.warning(
            "Malformed Guardian tool response; requesting one repair: %s; raw=%r",
            first_error,
            content,
        )
        correction = _repair_prompt(content, first_error)

    repaired_content, repaired_usage = retry(correction)
    combined_usage = _merge_usage(usage, repaired_usage)
    try:
        return _parse_tool_response(
            repaired_content,
            tools,
            combined_usage,
            strict=True,
        )
    except _ToolResponseParseError as second_error:
        logger.warning(
            "Guardian tool response remained malformed after repair: %s; raw=%r",
            second_error,
            repaired_content,
        )
        return _response(
            "Guardian model returned malformed tool JSON after one repair: "
            f"{second_error}; raw response: {repaired_content!r}",
            usage=combined_usage,
        )


def _parse_text_response(content: str, usage: object) -> Any:
    """Accept either plain text or the adapter's normal JSON final envelope."""

    data = _extract_json_object(content)
    if data and str(data.get("type", "")).lower() == "final":
        return _response(str(data.get("content", "")).strip(), usage=usage)
    if data and str(data.get("type", "")).lower() == "tool_call":
        return _response("", usage=usage)
    return _response(content.strip(), usage=usage)


@dataclass(slots=True)
class CodexSdkChat:
    """Small OpenAI-compatible facade over the Codex Python SDK.

    The SDK controls the local Codex app-server over JSON-RPC.  ``codex_bin``
    defaults to the user's installed ``codex`` so this adapter can use a newer
    local runtime than the SDK's pinned fallback when needed.
    """

    model: str
    cwd: Optional[str] = None
    codex_bin: Optional[str] = None
    reasoning_effort: Optional[str] = None
    timeout: int = 300
    backend_name: str = field(default="codex-sdk", init=False)
    transport_history: List[str] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.codex_bin = os.environ.get("CODEMINER_CODEX_BIN") or self.codex_bin
        self.backend_name = "codex-sdk"
        self.transport_history = ["codex-sdk"]
        self.reasoning_effort = (
            self.reasoning_effort
            or os.environ.get("GUARDIAN_CODEX_REASONING_EFFORT")
            or os.environ.get("CODEX_REASONING_EFFORT")
        )

    def invoke(self, messages: List[Any]) -> str:
        raw_messages = [
            {"role": getattr(m, "role", "user"), "content": getattr(m, "content", "")}
            for m in messages
        ]
        return self._call_raw(raw_messages).choices[0].message.content

    def start_agent_loop(self) -> "_CodexSdkAgentLoop":
        """Return a persistent SDK thread owned by one L2 or L3 agent loop."""

        return _CodexSdkAgentLoop(self)

    def _call_raw(self, messages: List[Dict[str, Any]], **overrides: Any) -> Any:
        tools = overrides.pop("tools", None) or []
        text_response = bool(overrides.pop("_guardian_text_response", False))
        max_tokens = overrides.pop("max_tokens", None)
        overrides.pop("tool_choice", None)
        prompt = self._build_prompt(messages, tools, max_tokens=max_tokens)
        try:
            content, usage = self._run_codex(prompt)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Codex SDK call failed; falling back to codex exec: %s", exc)
            self.backend_name = "codex-cli-fallback"
            if "codex-cli-fallback" not in self.transport_history:
                self.transport_history.append("codex-cli-fallback")
            fallback = CodexCliChat(
                model=self.model,
                cwd=self.cwd,
                codex_bin=os.environ.get("CODEMINER_CODEX_BIN") or "codex",
                reasoning_effort=self.reasoning_effort,
                timeout=self.timeout,
            )
            try:
                return fallback._call_raw(
                    messages,
                    tools=tools,
                    _guardian_text_response=text_response,
                    max_tokens=max_tokens,
                )
            except Exception:
                self.backend_name = "codex-unavailable"
                self.transport_history.append("codex-unavailable")
                raise
        if text_response:
            return _parse_text_response(content, usage)
        if not tools:
            return _response(content.strip(), usage=usage)
        return _parse_tool_response_with_retry(
            content,
            tools,
            usage,
            lambda repair: self._run_codex(f"{prompt}\n\n{repair}"),
        )

    def _build_prompt(
        self,
        messages: List[Dict[str, Any]],
        tools: list,
        *,
        max_tokens: Optional[int] = None,
    ) -> str:
        rendered = _render_messages(messages)
        repo_note = f"Repository checkout path: {self.cwd}\n\n" if self.cwd else ""
        completion_note = _completion_instruction(max_tokens)
        if not tools:
            return f"{completion_note}{repo_note}{rendered}"

        names = ", ".join(_tool_names(tools))
        return (
            "You are being called as Repository Guardian's model backend. "
            "Do not inspect files or run commands yourself; Guardian owns all "
            "tool execution. Respond with exactly one JSON object.\n\n"
            "If you need a Guardian tool, return:\n"
            '{"type":"tool_call","name":"<tool name>",'
            '"arguments":{"key":"value"}}\n'
            "For independent tool calls known at the same time, return:\n"
            '{"type":"tool_calls","calls":[{"name":"<tool name>",'
            '"arguments":{"key":"value"}},{"name":"<tool name>",'
            '"arguments":{"key":"value"}}]}\n'
            "If you are ready to answer, return:\n"
            '{"type":"final","content":"<plain text answer>"}\n\n'
            f"Available Guardian tool names: {names}\n"
            "Tool schemas:\n"
            f"{json.dumps(tools, ensure_ascii=False, indent=2)}\n\n"
            f"{completion_note}"
            f"{repo_note}"
            "Conversation:\n"
            f"{rendered}"
        )

    def _run_codex(self, prompt: str) -> tuple[str, SimpleNamespace]:
        from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox

        with tempfile.TemporaryDirectory(prefix="codeminer-codex-") as tmp:
            work_dir = os.path.join(tmp, "work")
            os.makedirs(work_dir, exist_ok=True)
            config = CodexConfig(
                codex_bin=self.codex_bin,
                cwd=work_dir,
                env=dict(os.environ),
            )
            with Codex(config) as codex:
                thread = codex.thread_start(
                    model=self.model,
                    sandbox=Sandbox.workspace_write,
                    cwd=work_dir,
                    ephemeral=True,
                )
                kwargs: Dict[str, Any] = {
                    "approval_mode": ApprovalMode.deny_all,
                    "sandbox": Sandbox.workspace_write,
                    "cwd": work_dir,
                    "model": self.model,
                }
                if self.reasoning_effort:
                    kwargs["effort"] = self.reasoning_effort
                result = thread.run(prompt, **kwargs)
        return (result.final_response or ""), _usage_from_sdk(result)


class _CodexSdkAgentLoop:
    """Persistent Codex SDK conversation for one Guardian agent loop.

    Guardian still owns tool execution.  The first call sends the complete
    Guardian transcript; later calls send only newly appended tool results.
    If history compaction rewrites the prefix, ``reset`` starts a fresh thread
    and the next call seeds it from the compacted transcript.
    """

    def __init__(self, adapter: CodexSdkChat) -> None:
        self.adapter = adapter
        self._tmp: Optional[tempfile.TemporaryDirectory] = None
        self._codex: object = None
        self._thread: object = None
        self._prior_input: List[Dict[str, Any]] = []
        self._tools_key = ""
        self._fallback: Optional[CodexCliChat] = None
        try:
            self._open()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Persistent Codex SDK loop unavailable; using codex exec: %s", exc
            )
            self._mark_fallback()

    def _open(self) -> None:
        from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox

        self._approval_mode = ApprovalMode
        self._sandbox = Sandbox
        self._tmp = tempfile.TemporaryDirectory(prefix="codeminer-codex-loop-")
        work_dir = os.path.join(self._tmp.name, "work")
        os.makedirs(work_dir, exist_ok=True)
        config = CodexConfig(
            codex_bin=self.adapter.codex_bin,
            cwd=work_dir,
            env=dict(os.environ),
        )
        try:
            codex = Codex(config)
            self._codex = codex.__enter__()
            self._thread = self._codex.thread_start(
                model=self.adapter.model,
                sandbox=Sandbox.workspace_write,
                cwd=work_dir,
                ephemeral=True,
            )
        except Exception:
            self.close()
            raise

    def _mark_fallback(self) -> None:
        self.adapter.backend_name = "codex-cli-fallback"
        if "codex-cli-fallback" not in self.adapter.transport_history:
            self.adapter.transport_history.append("codex-cli-fallback")
        self._fallback = CodexCliChat(
            model=self.adapter.model,
            cwd=self.adapter.cwd,
            codex_bin=os.environ.get("CODEMINER_CODEX_BIN") or "codex",
            reasoning_effort=self.adapter.reasoning_effort,
            timeout=self.adapter.timeout,
        )

    def _is_append_only(self, messages: List[Dict[str, Any]], tools_key: str) -> bool:
        return (
            bool(self._prior_input)
            and tools_key == self._tools_key
            and len(messages) >= len(self._prior_input)
            and messages[: len(self._prior_input)] == self._prior_input
        )

    @staticmethod
    def _incremental_prompt(
        messages: List[Dict[str, Any]], max_tokens: Optional[int] = None
    ) -> str:
        delta = list(messages)
        # The SDK thread already contains its own preceding JSON response.
        if delta and delta[0].get("role") == "assistant":
            delta = delta[1:]
        return (
            f"{_completion_instruction(max_tokens)}"
            "Guardian executed your requested tool calls. Continue the same "
            "Guardian task from these new results:\n\n"
            f"{_render_messages(delta)}"
        )

    def _run(self, prompt: str) -> tuple[str, SimpleNamespace]:
        kwargs: Dict[str, Any] = {
            "approval_mode": self._approval_mode.deny_all,
            "sandbox": self._sandbox.workspace_write,
            "cwd": os.path.join(self._tmp.name, "work"),
            "model": self.adapter.model,
        }
        if self.adapter.reasoning_effort:
            kwargs["effort"] = self.adapter.reasoning_effort
        result = self._thread.run(prompt, **kwargs)
        return (result.final_response or ""), _usage_from_sdk(result)

    def _call_raw(self, messages: List[Dict[str, Any]], **overrides: Any) -> Any:
        tools = overrides.pop("tools", None) or []
        text_response = bool(overrides.pop("_guardian_text_response", False))
        max_tokens = overrides.pop("max_tokens", None)
        overrides.pop("tool_choice", None)
        tools_key = json.dumps(tools, sort_keys=True, default=str)
        if self._fallback is not None:
            return self._fallback._call_raw(
                messages,
                tools=tools,
                _guardian_text_response=text_response,
                max_tokens=max_tokens,
                **overrides,
            )

        append_only = self._is_append_only(messages, tools_key)
        if self._prior_input and not append_only:
            self.reset()
        if append_only:
            delta = messages[len(self._prior_input) :]
            prompt = self._incremental_prompt(delta, max_tokens=max_tokens)
        else:
            prompt = self.adapter._build_prompt(messages, tools, max_tokens=max_tokens)

        try:
            content, usage = self._run(prompt)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Persistent Codex SDK loop failed; falling back to codex exec: %s",
                exc,
            )
            self.close()
            self._mark_fallback()
            return self._fallback._call_raw(
                messages,
                tools=tools,
                _guardian_text_response=text_response,
                max_tokens=max_tokens,
                **overrides,
            )

        self._prior_input = deepcopy(messages)
        self._tools_key = tools_key
        if text_response:
            return _parse_text_response(content, usage)
        if not tools:
            return _response(content.strip(), usage=usage)
        return _parse_tool_response_with_retry(
            content,
            tools,
            usage,
            self._run,
        )

    def reset(self) -> None:
        """Discard provider history after a local transcript compaction."""

        self.close()
        self._fallback = None
        self._prior_input = []
        self._tools_key = ""
        try:
            self._open()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Codex SDK loop reset failed; using codex exec: %s", exc)
            self._mark_fallback()

    def close(self) -> None:
        if self._codex is not None:
            try:
                self._codex.__exit__(None, None, None)
            finally:
                self._codex = None
                self._thread = None
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None


@dataclass(slots=True)
class CodexCliChat:
    """Small OpenAI-compatible facade over ``codex exec``.

    ``model`` should be the Codex model name after the ``codex:`` prefix, e.g.
    ``gpt-5.6-luna``.  Auth is delegated to the installed Codex CLI, so Pier can
    use the existing subscription-backed ``~/.codex/auth.json`` flow.
    """

    model: str
    cwd: Optional[str] = None
    codex_bin: str = "codex"
    reasoning_effort: Optional[str] = None
    timeout: int = 300
    backend_name: str = field(default="codex-cli", init=False)
    transport_history: List[str] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.codex_bin = os.environ.get("CODEMINER_CODEX_BIN", self.codex_bin)
        self.backend_name = "codex-cli"
        self.transport_history = ["codex-cli"]
        self.reasoning_effort = (
            self.reasoning_effort
            or os.environ.get("GUARDIAN_CODEX_REASONING_EFFORT")
            or os.environ.get("CODEX_REASONING_EFFORT")
        )

    def invoke(self, messages: List[Any]) -> str:
        raw_messages = [
            {"role": getattr(m, "role", "user"), "content": getattr(m, "content", "")}
            for m in messages
        ]
        return self._call_raw(raw_messages).choices[0].message.content

    def _call_raw(self, messages: List[Dict[str, Any]], **overrides: Any) -> Any:
        tools = overrides.pop("tools", None) or []
        text_response = bool(overrides.pop("_guardian_text_response", False))
        max_tokens = overrides.pop("max_tokens", None)
        overrides.pop("tool_choice", None)
        prompt = self._build_prompt(messages, tools, max_tokens=max_tokens)
        stdout, content = self._run_codex(prompt)
        usage = _usage_from_stdout(stdout)
        if text_response:
            return _parse_text_response(content, usage)
        if not tools:
            return _response(content.strip(), usage=usage)
        return _parse_tool_response_with_retry(
            content,
            tools,
            usage,
            lambda repair: (lambda result: (result[1], _usage_from_stdout(result[0])))(
                self._run_codex(f"{prompt}\n\n{repair}")
            ),
        )

    def _build_prompt(
        self,
        messages: List[Dict[str, Any]],
        tools: list,
        *,
        max_tokens: Optional[int] = None,
    ) -> str:
        rendered = _render_messages(messages)
        repo_note = f"Repository checkout path: {self.cwd}\n\n" if self.cwd else ""
        completion_note = _completion_instruction(max_tokens)
        if not tools:
            return f"{completion_note}{repo_note}{rendered}"

        names = ", ".join(_tool_names(tools))
        return (
            "You are being called as Repository Guardian's model backend. "
            "Do not inspect files or run commands yourself; Guardian owns all "
            "tool execution. Respond with exactly one JSON object.\n\n"
            "If you need a Guardian tool, return:\n"
            '{"type":"tool_call","name":"<tool name>",'
            '"arguments":{"key":"value"}}\n'
            "For independent tool calls known at the same time, return:\n"
            '{"type":"tool_calls","calls":[{"name":"<tool name>",'
            '"arguments":{"key":"value"}},{"name":"<tool name>",'
            '"arguments":{"key":"value"}}]}\n'
            "If you are ready to answer, return:\n"
            '{"type":"final","content":"<plain text answer>"}\n\n'
            f"Available Guardian tool names: {names}\n"
            "Tool schemas:\n"
            f"{json.dumps(tools, ensure_ascii=False, indent=2)}\n\n"
            f"{completion_note}"
            f"{repo_note}"
            "Conversation:\n"
            f"{rendered}"
        )

    def _run_codex(self, prompt: str) -> tuple[str, str]:
        with tempfile.TemporaryDirectory(prefix="codeminer-codex-") as tmp:
            work_dir = os.path.join(tmp, "work")
            os.makedirs(work_dir, exist_ok=True)
            output_path = os.path.join(tmp, "last_message.txt")
            cmd = [
                self.codex_bin,
                "exec",
                "--model",
                self.model,
                "--sandbox",
                "workspace-write",
                "--skip-git-repo-check",
                "--ephemeral",
                "--json",
                "--output-last-message",
                output_path,
                "-C",
                work_dir,
            ]
            if self.reasoning_effort:
                cmd.extend(["-c", f"model_reasoning_effort={self.reasoning_effort}"])

            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip()
                raise RuntimeError(f"codex exec failed ({proc.returncode}): {detail}")
            try:
                content = open(output_path, encoding="utf-8").read()
            except OSError:
                content = proc.stdout
            return proc.stdout, content

    def _parse_tool_response(self, content: str, tools: list, usage: object) -> Any:
        return _parse_tool_response(content, tools, usage)
