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


def _extract_json_object(text: str) -> Optional[dict]:
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass

    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if match:
        try:
            value = json.loads(match.group(1).strip())
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(text[start : end + 1])
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None
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
    breakdown = total or last
    input_tokens = getattr(breakdown, "input_tokens", 0) or 0
    output_tokens = getattr(breakdown, "output_tokens", 0) or 0
    reasoning = getattr(breakdown, "reasoning_output_tokens", 0) or 0
    total_tokens = getattr(breakdown, "total_tokens", 0) or (
        input_tokens + output_tokens + reasoning
    )
    return SimpleNamespace(
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens + reasoning,
        total_tokens=total_tokens,
        cached_input_tokens=getattr(breakdown, "cached_input_tokens", 0) or 0,
        output_tokens=output_tokens,
        reasoning_output_tokens=reasoning,
    )


def _parse_tool_response(content: str, tools: list, usage: object) -> Any:
    data = _extract_json_object(content)
    allowed = set(_tool_names(tools))
    if not data:
        return _response(content.strip(), usage=usage)

    kind = str(data.get("type", "")).lower()
    if kind == "final":
        return _response(str(data.get("content", "")).strip(), usage=usage)

    if kind == "tool_call":
        name = str(data.get("name", ""))
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

    def _call_raw(self, messages: List[Dict[str, Any]], **overrides: Any) -> Any:
        tools = overrides.pop("tools", None) or []
        overrides.pop("tool_choice", None)
        prompt = self._build_prompt(messages, tools)
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
                return fallback._call_raw(messages, tools=tools)
            except Exception:
                self.backend_name = "codex-unavailable"
                self.transport_history.append("codex-unavailable")
                raise
        if not tools:
            return _response(content.strip(), usage=usage)
        return _parse_tool_response(content, tools, usage)

    def _build_prompt(self, messages: List[Dict[str, Any]], tools: list) -> str:
        rendered = _render_messages(messages)
        repo_note = f"Repository checkout path: {self.cwd}\n\n" if self.cwd else ""
        if not tools:
            return f"{repo_note}{rendered}"

        names = ", ".join(_tool_names(tools))
        return (
            "You are being called as Repository Guardian's model backend. "
            "Do not inspect files or run commands yourself; Guardian owns all "
            "tool execution. Respond with exactly one JSON object.\n\n"
            "If you need a Guardian tool, return:\n"
            '{"type":"tool_call","name":"<tool name>","arguments":{...}}\n'
            "If you are ready to answer, return:\n"
            '{"type":"final","content":"<plain text answer>"}\n\n'
            f"Available Guardian tool names: {names}\n"
            "Tool schemas:\n"
            f"{json.dumps(tools, ensure_ascii=False, indent=2)}\n\n"
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
        overrides.pop("tool_choice", None)
        prompt = self._build_prompt(messages, tools)
        stdout, content = self._run_codex(prompt)
        usage = _usage_from_stdout(stdout)
        if not tools:
            return _response(content.strip(), usage=usage)
        return self._parse_tool_response(content, tools, usage)

    def _build_prompt(self, messages: List[Dict[str, Any]], tools: list) -> str:
        rendered = _render_messages(messages)
        repo_note = f"Repository checkout path: {self.cwd}\n\n" if self.cwd else ""
        if not tools:
            return f"{repo_note}{rendered}"

        names = ", ".join(_tool_names(tools))
        return (
            "You are being called as Repository Guardian's model backend. "
            "Do not inspect files or run commands yourself; Guardian owns all "
            "tool execution. Respond with exactly one JSON object.\n\n"
            "If you need a Guardian tool, return:\n"
            '{"type":"tool_call","name":"<tool name>","arguments":{...}}\n'
            "If you are ready to answer, return:\n"
            '{"type":"final","content":"<plain text answer>"}\n\n'
            f"Available Guardian tool names: {names}\n"
            "Tool schemas:\n"
            f"{json.dumps(tools, ensure_ascii=False, indent=2)}\n\n"
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
