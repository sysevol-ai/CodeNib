# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Use the person's own Codex as a text model, with no API key.

A model named ``codex/<model>`` (``codex/default``, ``codex/gpt-5.5``) is
served by running ``codex exec`` on this machine, signed in the way Codex
already is (ChatGPT or an API key set up with ``codex login``). ``chat_for``
returns a ``CodexChat`` for such a model, with the interface of
``LiteLLMChat``, so the Wiki writer works unchanged and LiteLLM need not be
installed.

Each call is a plain completion: the system messages and the prompt go in on
stdin, Codex runs in its read-only sandbox from an empty directory with no
saved session, and its final message is read from ``--output-last-message``.
A JSON schema is passed as ``--output-schema``; OpenAI's structured output
needs every property listed as required, so optional properties become
nullable there and nulls are dropped from the answer.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..log_utils import get_logger

logger = get_logger(__name__)

PROVIDER = "codex"
PREFIX = f"{PROVIDER}/"
DEFAULT_MODEL = f"{PREFIX}default"
DEFAULT_TIMEOUT_SECONDS = 600.0
EXECUTABLE_ENV = "CODENIB_CODEX_EXECUTABLE"
_SECRET_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN")
_DEFAULT_SYSTEM = (
    "You are a writing model called through an API. Answer the request "
    "exactly as asked, in the format it asks for, with no preamble. Do not "
    "run commands or read files; everything you need is in the request."
)


class CodexError(RuntimeError):
    """Codex is missing, not signed in, or returned no answer."""


def is_codex_model(model: Optional[str]) -> bool:
    return bool(model) and str(model).startswith(PREFIX)


def executable() -> str:
    return os.environ.get(EXECUTABLE_ENV, "codex")


def available() -> bool:
    return shutil.which(executable()) is not None


def strict_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """A JSON schema OpenAI's strict structured output accepts.

    Every object lists all its properties as required and allows no others;
    a property that was optional becomes nullable instead.
    """

    def visit(node: Any) -> Any:
        if isinstance(node, list):
            return [visit(item) for item in node]
        if not isinstance(node, dict):
            return node
        node = {key: visit(value) for key, value in node.items()}
        if node.get("type") == "object" and isinstance(node.get("properties"), dict):
            required = set(node.get("required") or [])
            properties = {}
            for name, prop in node["properties"].items():
                if name not in required and isinstance(prop, dict):
                    prop = dict(prop)
                    kind = prop.get("type")
                    if isinstance(kind, str):
                        prop["type"] = [kind, "null"]
                    elif isinstance(kind, list) and "null" not in kind:
                        prop["type"] = [*kind, "null"]
                properties[name] = prop
            node["properties"] = properties
            node["required"] = list(properties)
            node["additionalProperties"] = False
        return node

    return visit(copy.deepcopy(schema))


def drop_nulls(value: Any) -> Any:
    """Remove the nulls strict output put in for optional properties."""

    if isinstance(value, dict):
        return {k: drop_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [drop_nulls(item) for item in value]
    return value


def _usage(stdout: str) -> Dict[str, int]:
    """Token counts from ``codex exec --json`` events, when it printed any."""

    tokens = {"input_tokens": 0, "output_tokens": 0}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        usage = event.get("usage") if isinstance(event, dict) else None
        if event.get("type") == "turn.completed" and isinstance(usage, dict):
            tokens["input_tokens"] += int(usage.get("input_tokens") or 0)
            tokens["output_tokens"] += int(usage.get("output_tokens") or 0)
    return tokens


def _failure(stdout: str, stderr: str) -> str:
    """The last error Codex reported, from its events or its stderr."""

    for line in reversed(stdout.splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") in ("error", "turn.failed"):
            detail = event.get("message") or (event.get("error") or {}).get("message")
            if detail:
                return str(detail)
    lines = [line for line in stderr.strip().splitlines() if line.strip()]
    return lines[-1][:300] if lines else ""


def exec_codex(
    prompt: str,
    *,
    cwd: Path,
    sandbox: str,
    model: Optional[str] = None,
    json_schema: Optional[Dict[str, Any]] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    log_dir: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    extra_args: Sequence[str] = (),
) -> Dict[str, Any]:
    """Run ``codex exec`` once; return ``{"text", "structured", "usage", "error"}``.

    ``error`` is empty on success. ``text`` is Codex's final message.
    """

    command = executable()
    with tempfile.TemporaryDirectory(prefix="codenib-codex-") as scratch:
        last = Path(scratch) / "last-message.txt"
        argv: List[str] = [
            command,
            "exec",
            "--json",
            "--ephemeral",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--sandbox",
            sandbox,
            "-c",
            'approval_policy="never"',
            "--cd",
            str(cwd),
            "--output-last-message",
            str(last),
        ]
        if json_schema:
            schema_file = Path(scratch) / "schema.json"
            schema_file.write_text(
                json.dumps(strict_schema(json_schema)), encoding="utf-8"
            )
            argv += ["--output-schema", str(schema_file)]
        if model:
            argv += ["--model", model]
        argv += list(extra_args)
        argv.append("-")
        run_env = dict(os.environ if env is None else env)
        for key in _SECRET_ENV:
            run_env.pop(key, None)
        try:
            completed = subprocess.run(
                argv,
                input=prompt,
                cwd=str(cwd),
                env=run_env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            return {
                "text": "",
                "structured": None,
                "usage": {},
                "error": f"{command} is not installed; install Codex and sign in",
            }
        except subprocess.TimeoutExpired:
            return {
                "text": "",
                "structured": None,
                "usage": {},
                "error": f"Codex did not answer within {int(timeout)} s",
            }
        text = last.read_text(encoding="utf-8").strip() if last.is_file() else ""
    if log_dir is not None:
        attempt = len(list(log_dir.glob("codex-events-*.jsonl"))) + 1
        (log_dir / f"codex-events-{attempt}.jsonl").write_text(
            completed.stdout, encoding="utf-8"
        )
        if completed.stderr:
            (log_dir / f"stderr-{attempt}.txt").write_text(
                completed.stderr, encoding="utf-8"
            )
    structured = None
    if json_schema and text:
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            structured = drop_nulls(parsed)
    error = ""
    if completed.returncode != 0:
        error = _failure(completed.stdout, completed.stderr) or (
            f"Codex exited with status {completed.returncode}"
        )
    elif not text:
        error = _failure(completed.stdout, completed.stderr) or (
            "Codex returned no answer"
        )
    return {
        "text": text,
        "structured": structured,
        "usage": _usage(completed.stdout),
        "error": error,
    }


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part)
            for part in content
        )
    return "" if content is None else str(content)


def render_prompt(messages: List[Dict[str, Any]]) -> str:
    """One stdin prompt: Codex takes no separate system prompt."""

    system = [
        _text(m.get("content")).strip()
        for m in messages
        if m.get("role") == "system" and _text(m.get("content")).strip()
    ]
    turns = [m for m in messages if m.get("role") != "system"]
    lines = ["# Instructions", "\n\n".join(system) or _DEFAULT_SYSTEM, ""]
    if len(turns) == 1:
        lines += ["# Request", _text(turns[0].get("content")).strip()]
    else:
        lines.append("# Conversation")
        for message in turns:
            speaker = "User" if message.get("role") == "user" else "Assistant"
            lines.append(f"{speaker}:\n{_text(message.get('content')).strip()}")
        lines.append("Reply as the assistant to the last user message.")
    return "\n".join(lines)


def run(
    prompt: str,
    *,
    model: str,
    json_schema: Optional[Dict[str, Any]] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Run one completion; return ``{"text", "structured", "usage"}``."""

    name = model[len(PREFIX) :] if is_codex_model(model) else model
    if name == "default":
        name = ""
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="codenib-codex-empty-") as empty:
        answer = exec_codex(
            prompt,
            cwd=Path(empty),
            sandbox="read-only",
            model=name or None,
            json_schema=json_schema,
            timeout=timeout,
        )
    if answer["error"]:
        raise CodexError(f"Codex failed: {answer['error']}")
    logger.info(
        "Codex (%s) answered in %.1f s",
        name or "default",
        time.monotonic() - started,
    )
    usage = answer["usage"]
    return {
        "text": answer["text"],
        "structured": answer["structured"],
        "usage": {
            "prompt_tokens": int(usage.get("input_tokens") or 0),
            "completion_tokens": int(usage.get("output_tokens") or 0),
            "cost_usd": 0.0,
        },
    }


class _Answer:
    """The slice of a chat-completion response that callers read."""

    def __init__(self, content: str):
        message = type("Message", (), {"content": content, "role": "assistant"})()
        choice = type("Choice", (), {"message": message, "finish_reason": "stop"})()
        self.choices = [choice]


class CodexChat:
    """``LiteLLMChat``'s interface, answered by the local ``codex`` CLI.

    Needs no API key and no LiteLLM. ``temperature`` and ``max_tokens`` are
    accepted for compatibility; Codex decides both.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **_ignored: Any,
    ):
        self.model = model if is_codex_model(model) else PREFIX + model
        self.temperature = temperature
        self.max_tokens = max_tokens

    @property
    def cache_identity(self) -> str:
        return hashlib.sha256(
            json.dumps({"model": self.model}, sort_keys=True).encode()
        ).hexdigest()[:20]

    def complete(self, messages: List[Dict[str, Any]], **_overrides: Any) -> str:
        return run(render_prompt(messages), model=self.model)["text"].strip()

    def invoke(self, messages: List[Any]) -> str:
        return self.complete([{"role": m.role, "content": m.content} for m in messages])

    def _call(self, messages: List[Any], **_overrides: Any) -> _Answer:
        return _Answer(self.invoke(messages))

    def with_structured_output(self, schema: Any) -> "_StructuredCodex":
        return _StructuredCodex(self, schema)


class _StructuredCodex:
    def __init__(self, chat: CodexChat, schema: Any):
        self.chat = chat
        self.schema = schema

    def invoke(self, messages: List[Any]) -> Any:
        prompt = render_prompt(
            [{"role": m.role, "content": m.content} for m in messages]
        )
        answer = run(
            prompt,
            model=self.chat.model,
            json_schema=self.schema.model_json_schema(),
        )
        if isinstance(answer["structured"], dict):
            return self.schema.model_validate(answer["structured"])
        return self.schema.model_validate_json(answer["text"])


def chat_for(model: str, **kwargs: Any) -> CodexChat:
    return CodexChat(model, **kwargs)
