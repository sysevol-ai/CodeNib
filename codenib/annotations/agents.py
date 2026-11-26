# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Run the person's own coding agent headless on one task.

CodeNib starts the agent itself with a prompt that already holds the
annotations and their context, so nothing depends on the agent choosing to
call a tool. The agent returns its results as structured output checked
against a JSON schema.

Claude Code runs in restricted mode: no shell, no web, no MCP servers, only
the file tools. A question gets Read, Grep and Glob; a change adds Edit and
Write, and runs in a worktree of its own.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence

READ_TOOLS = "Read,Grep,Glob"
EDIT_TOOLS = "Read,Grep,Glob,Edit,Write"
DEFAULT_TIMEOUT_SECONDS = 900.0
_SECRET_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN")


@dataclass
class AgentRun:
    structured: Optional[Dict[str, Any]]
    text: str = ""
    error: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)


class Agent(Protocol):
    name: str

    def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        system: str,
        schema: Dict[str, Any],
        edit: bool,
        log_dir: Path,
    ) -> AgentRun: ...


def agent_env() -> Dict[str, str]:
    env = dict(os.environ)
    for name in _SECRET_ENV:
        env.pop(name, None)
    return env


def _last_json_object(text: str) -> Optional[Dict[str, Any]]:
    decoder = json.JSONDecoder()
    found = None
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        if isinstance(value, dict):
            found = value
    return found


def parse_claude_output(stdout: str) -> AgentRun:
    """Read ``claude -p --output-format json``: one result record, or a list."""

    try:
        data: Any = json.loads(stdout)
    except ValueError:
        return AgentRun(None, error="the agent printed no JSON result")
    records = data if isinstance(data, list) else [data]
    result = next(
        (
            r
            for r in reversed(records)
            if isinstance(r, dict) and r.get("type") == "result"
        ),
        None,
    )
    if result is None:
        return AgentRun(None, error="the agent printed no result record")
    usage = result.get("usage") or {}
    tokens_in = sum(
        int(usage.get(key) or 0)
        for key in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
    )
    summary = {
        "input_tokens": tokens_in,
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cost_usd": float(result.get("total_cost_usd") or 0.0),
        "turns": int(result.get("num_turns") or 0),
        "duration_ms": int(result.get("duration_ms") or 0),
    }
    text = str(result.get("result") or "")
    structured = result.get("structured_output")
    if not isinstance(structured, dict):
        structured = _last_json_object(text)
    error = ""
    if result.get("is_error") or result.get("subtype") not in (None, "success"):
        error = text or str(result.get("subtype") or "the agent stopped with an error")
    return AgentRun(structured, text=text, error=error, usage=summary)


def add_usage(total: Dict[str, Any], run: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(total)
    for key, value in run.items():
        merged[key] = (merged.get(key) or 0) + value
    if "cost_usd" in merged:
        merged["cost_usd"] = round(float(merged["cost_usd"]), 6)
    return merged


class _ProcessAgent:
    name = "agent"
    label = "The agent"
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    def _argv(self, *, system: str, schema: Dict[str, Any], edit: bool) -> List[str]:
        raise NotImplementedError

    def _extra_env(self, *, system: str, schema: Dict[str, Any]) -> Dict[str, str]:
        return {}

    def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        system: str,
        schema: Dict[str, Any],
        edit: bool,
        log_dir: Path,
    ) -> AgentRun:
        log_dir.mkdir(parents=True, exist_ok=True)
        argv = self._argv(system=system, schema=schema, edit=edit)
        attempt = len(list(log_dir.glob("prompt-*.txt"))) + 1
        (log_dir / f"prompt-{attempt}.txt").write_text(
            f"# system\n{system}\n\n# prompt\n{prompt}", encoding="utf-8"
        )
        env = agent_env()
        env["CODENIB_TASK_EDIT"] = "1" if edit else "0"
        env.update(self._extra_env(system=system, schema=schema))
        try:
            completed = subprocess.run(
                argv,
                input=prompt,
                cwd=str(cwd),
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError:
            return AgentRun(None, error=f"{argv[0]} is not installed")
        except subprocess.TimeoutExpired:
            return AgentRun(
                None, error=f"the agent ran longer than {int(self.timeout)} s"
            )
        (log_dir / f"output-{attempt}.json").write_text(
            completed.stdout, encoding="utf-8"
        )
        if completed.stderr:
            (log_dir / f"stderr-{attempt}.txt").write_text(
                completed.stderr, encoding="utf-8"
            )
        run = parse_claude_output(completed.stdout)
        if completed.returncode != 0 and not run.error:
            run.error = (
                completed.stderr.strip().splitlines()[-1]
                if completed.stderr.strip()
                else f"the agent exited with status {completed.returncode}"
            )
        return run


class ClaudeCode(_ProcessAgent):
    """``claude -p`` in restricted mode, structured output, no saved session."""

    name = "claude-code"
    label = "Claude Code"

    def __init__(
        self,
        executable: str = "claude",
        *,
        model: Optional[str] = None,
        max_budget_usd: Optional[float] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.executable = executable
        self.model = model
        self.max_budget_usd = max_budget_usd
        self.timeout = timeout

    def _argv(self, *, system: str, schema: Dict[str, Any], edit: bool) -> List[str]:
        argv = [
            self.executable,
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema, separators=(",", ":")),
            "--restricted",
            "--tools",
            EDIT_TOOLS if edit else READ_TOOLS,
            "--strict-mcp-config",
            "--permission-prompts",
            "none",
            "--no-session-persistence",
            "--append-system-prompt",
            system,
        ]
        if edit:
            argv += ["--permission-mode", "acceptEdits"]
        if self.model:
            argv += ["--model", self.model]
        if self.max_budget_usd:
            argv += ["--max-budget-usd", str(self.max_budget_usd)]
        return argv


class CommandAgent(_ProcessAgent):
    """Any command that reads the prompt on stdin and prints Claude's JSON.

    Used by tests and by agents CodeNib has no adapter for yet. The schema
    and the system prompt arrive as the environment variables
    ``CODENIB_TASK_SCHEMA`` and ``CODENIB_TASK_SYSTEM``.
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        name: str = "command",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.command = list(argv)
        self.name = name
        self.label = name
        self.timeout = timeout

    def _argv(self, *, system: str, schema: Dict[str, Any], edit: bool) -> List[str]:
        return list(self.command)

    def _extra_env(self, *, system: str, schema: Dict[str, Any]) -> Dict[str, str]:
        return {
            "CODENIB_TASK_SCHEMA": json.dumps(schema),
            "CODENIB_TASK_SYSTEM": system,
        }


class Codex:
    """``codex exec`` on the person's own Codex sign-in.

    A question runs in Codex's read-only sandbox; a change runs with
    workspace-write inside the task's own worktree. Structured output comes
    from ``--output-schema``; no session is saved.
    """

    name = "codex"
    label = "Codex"

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.model = model
        self.timeout = timeout

    def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        system: str,
        schema: Dict[str, Any],
        edit: bool,
        log_dir: Path,
    ) -> AgentRun:
        from ..llm.codex_cli import exec_codex

        log_dir.mkdir(parents=True, exist_ok=True)
        attempt = len(list(log_dir.glob("prompt-*.txt"))) + 1
        (log_dir / f"prompt-{attempt}.txt").write_text(
            f"# system\n{system}\n\n# prompt\n{prompt}", encoding="utf-8"
        )
        env = agent_env()
        env["CODENIB_TASK_EDIT"] = "1" if edit else "0"
        result = exec_codex(
            f"{system}\n\n{prompt}",
            cwd=cwd,
            sandbox="workspace-write" if edit else "read-only",
            model=self.model,
            json_schema=schema,
            timeout=self.timeout,
            log_dir=log_dir,
            env=env,
        )
        text = str(result.get("text") or "")
        structured = result.get("structured")
        if not isinstance(structured, dict):
            structured = _last_json_object(text)
        usage = result.get("usage") or {}
        return AgentRun(
            structured,
            text=text,
            error=str(result.get("error") or ""),
            usage={
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
            },
        )


_SIGN_IN_CACHE_SECONDS = 60.0
_sign_in_cache: Dict[str, Any] = {}


def _signed_in(agent_id: str, executable: Optional[str]) -> bool:
    """Whether the agent's own CLI says it is signed in (cached briefly)."""

    if not executable:
        return False
    import time

    cached = _sign_in_cache.get(agent_id)
    now = time.monotonic()
    if cached and now - cached[0] < _SIGN_IN_CACHE_SECONDS:
        return cached[1]
    argv = (
        [executable, "auth", "status", "--json"]
        if agent_id == "claude-code"
        else [executable, "login", "status"]
    )
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        signed = False
    else:
        if agent_id == "claude-code":
            try:
                signed = bool(json.loads(completed.stdout).get("loggedIn"))
            except (ValueError, AttributeError):
                signed = False
        else:
            said = f"{completed.stdout}\n{completed.stderr}".lower()
            signed = completed.returncode == 0 and "not logged in" not in said
    _sign_in_cache[agent_id] = (now, signed)
    return signed


def detect_agents() -> List[Dict[str, Any]]:
    """The agent CLIs on this machine, in the order the picker shows them."""

    from ..llm.codex_cli import executable as codex_executable

    claude = shutil.which("claude")
    codex = shutil.which(codex_executable())
    return [
        {
            "id": "claude-code",
            "name": "Claude Code",
            "available": claude is not None,
            "signed_in": _signed_in("claude-code", claude),
            "install": "curl -fsSL https://claude.ai/install.sh | bash",
        },
        {
            "id": "codex",
            "name": "Codex",
            "available": codex is not None,
            "signed_in": _signed_in("codex", codex),
            "install": "npm install -g @openai/codex",
        },
    ]


AGENTS = ("claude-code", "codex")


def agent_for(name: str = "claude-code") -> Agent:
    command = os.environ.get("CODENIB_TASK_AGENT_COMMAND", "").strip()
    if command:
        import shlex

        return CommandAgent(shlex.split(command), name="command")
    model = os.environ.get("CODENIB_TASK_MODEL") or None
    if name == "codex":
        return Codex(model=os.environ.get("CODENIB_TASK_CODEX_MODEL") or None)
    if name != "claude-code":
        raise ValueError(f"no adapter for agent {name!r}")
    budget = os.environ.get("CODENIB_TASK_MAX_BUDGET_USD")
    return ClaudeCode(model=model, max_budget_usd=float(budget) if budget else None)
