# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Claude Code for a hosted Wiki visitor: their key, our container.

A visitor of the hosted Wiki asks questions and requests changes with their
own OpenRouter or Anthropic key. The key is checked with its provider before
anything starts (Claude Code retries a refused key for minutes), passed to the
container in its environment, never written down, and forgotten when the run
ends.

The container sees one folder: the code, read-only for a question, a private
clone for a change. It runs as the server's user with no capabilities, no new
privileges, a read-only root, a scratch home and limits on memory, processes
and CPU. Claude Code runs in ``--bare`` mode, so the repository's own Claude
Code settings, hooks and plugins are not loaded, and it has only the file
tools (no shell, no web).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .agents import EDIT_TOOLS, READ_TOOLS, AgentRun, parse_claude_output
from .model import AnnotationError

IMAGE_ENV = "CODENIB_SANDBOX_IMAGE"
DEFAULT_IMAGE = "debian:bookworm-slim"
WORKDIR = "/work"
DEFAULT_TIMEOUT_SECONDS = 300.0
#: The most one run may spend of the visitor's key.
DEFAULT_BUDGET_USD = 1.0
_KEY = re.compile(r"\A[\x21-\x7e]{20,512}\Z")
MODELS = {
    "openrouter": "anthropic/claude-sonnet-5.5",
    "anthropic": "claude-sonnet-5-5",
}


class KeyRefused(AnnotationError):
    """The provider refused the visitor's key, or it cannot pay for a run."""


def provider_of(key: str) -> str:
    """``openrouter`` or ``anthropic``, from the shape of the key."""

    if not _KEY.match(key or ""):
        raise KeyRefused("Paste an OpenRouter or Anthropic API key")
    if key.startswith("sk-or-"):
        return "openrouter"
    if key.startswith("sk-ant-"):
        return "anthropic"
    raise KeyRefused(
        "This key is neither an OpenRouter key (sk-or-…) nor an Anthropic "
        "key (sk-ant-…)"
    )


def _get(url: str, headers: Dict[str, str], timeout: float) -> Dict[str, Any]:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read(65536) or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise KeyRefused("The provider refused this key") from None
        raise AnnotationError(
            f"Could not check the key with its provider (HTTP {exc.code})"
        ) from None
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise AnnotationError("Could not reach the provider to check the key") from None


def check_key(key: str, *, timeout: float = 10.0) -> str:
    """Ask the provider whether the key works; return the provider.

    Neither call is billed. An OpenRouter key must be a normal inference key
    with credit left.
    """

    provider = provider_of(key)
    if provider == "openrouter":
        data = _get(
            "https://openrouter.ai/api/v1/key",
            {"Authorization": f"Bearer {key}"},
            timeout,
        ).get("data")
        if not isinstance(data, dict):
            raise AnnotationError("OpenRouter sent an unexpected answer")
        if data.get("is_management_key") or data.get("is_provisioning_key"):
            raise KeyRefused("Use an inference key, not a management key")
        remaining = data.get("limit_remaining")
        if isinstance(remaining, (int, float)) and remaining < 0.05:
            raise KeyRefused("This OpenRouter key has no credit left")
    else:
        _get(
            "https://api.anthropic.com/v1/models?limit=1",
            {"x-api-key": key, "anthropic-version": "2023-06-01"},
            timeout,
        )
    return provider


def claude_executable() -> Optional[Path]:
    """The Claude Code binary to mount into the container."""

    configured = os.environ.get("CODENIB_SANDBOX_CLAUDE", "").strip()
    found = configured or shutil.which("claude")
    if not found:
        return None
    path = Path(found).resolve()
    return path if path.is_file() else None


def available() -> bool:
    return shutil.which("docker") is not None and claude_executable() is not None


def _relative_files(structured: Optional[Dict[str, Any]]) -> None:
    """Paths the agent saw under /work, as paths in the repository."""

    for item in (structured or {}).get("results") or []:
        for entry in (item or {}).get("files") or [] if isinstance(item, dict) else []:
            path = entry.get("path") if isinstance(entry, dict) else None
            if isinstance(path, str) and path.startswith(WORKDIR + "/"):
                entry["path"] = path[len(WORKDIR) + 1 :]


class SandboxedClaude:
    """``claude -p --bare`` in a locked-down container, on a visitor's key."""

    name = "claude-code"
    label = "Claude Code"

    def __init__(
        self,
        key: str,
        *,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        max_budget_usd: float = DEFAULT_BUDGET_USD,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        image: Optional[str] = None,
        docker: str = "docker",
        executable: Optional[Path] = None,
    ):
        self.provider = provider or provider_of(key)
        self._key = key
        self.model = model or os.environ.get(
            f"CODENIB_HOSTED_MODEL_{self.provider.upper()}", MODELS[self.provider]
        )
        self.max_budget_usd = max_budget_usd
        self.timeout = timeout
        self.image = image or os.environ.get(IMAGE_ENV, DEFAULT_IMAGE)
        self.docker = docker
        self.executable = executable

    def forget_key(self) -> None:
        self._key = ""

    def _key_env(self) -> Dict[str, str]:
        if self.provider == "openrouter":
            return {
                "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
                "CODENIB_PROVIDER_KEY": self._key,
            }
        return {"ANTHROPIC_API_KEY": self._key}

    def argv(
        self,
        *,
        cwd: Path,
        system: str,
        schema: Dict[str, Any],
        edit: bool,
        name: str,
    ) -> List[str]:
        executable = self.executable or claude_executable()
        if executable is None:
            raise AnnotationError("Claude Code is not installed on this server")
        uid, gid = os.getuid(), os.getgid()
        argv = [
            self.docker,
            "run",
            "--rm",
            "-i",
            "--name",
            name,
            "--read-only",
            "--tmpfs",
            "/tmp:rw,size=256m",
            "--tmpfs",
            f"/home/agent:rw,size=256m,uid={uid},gid={gid}",
            "--user",
            f"{uid}:{gid}",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "256",
            "--memory",
            "2g",
            "--cpus",
            "1",
            "-e",
            "HOME=/home/agent",
        ]
        # Values come from the docker client's environment, not its argv,
        # so the key never shows in a process listing.
        for variable in self._key_env():
            argv += ["-e", variable]
        argv += [
            "-v",
            f"{executable}:/usr/local/bin/claude:ro",
            "-v",
            f"{Path(cwd).resolve()}:{WORKDIR}:{'rw' if edit else 'ro'}",
            "-w",
            WORKDIR,
            self.image,
            "claude",
            "-p",
            "--bare",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema, separators=(",", ":")),
            "--tools",
            EDIT_TOOLS if edit else READ_TOOLS,
            "--strict-mcp-config",
            "--permission-prompts",
            "none",
            "--no-session-persistence",
            "--append-system-prompt",
            system,
            "--model",
            self.model,
            "--max-budget-usd",
            str(self.max_budget_usd),
        ]
        if edit:
            argv += ["--permission-mode", "acceptEdits"]
        if self.provider == "openrouter":
            # --bare reads only ANTHROPIC_API_KEY or a key helper; the helper's
            # value is sent as the bearer token OpenRouter expects.
            argv += [
                "--settings",
                json.dumps({"apiKeyHelper": "printenv CODENIB_PROVIDER_KEY"}),
            ]
        return argv

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
        if not self._key:
            return AgentRun(None, error="the key was already used and forgotten")
        log_dir.mkdir(parents=True, exist_ok=True)
        attempt = len(list(log_dir.glob("prompt-*.txt"))) + 1
        (log_dir / f"prompt-{attempt}.txt").write_text(
            f"# system\n{system}\n\n# prompt\n{prompt}", encoding="utf-8"
        )
        name = f"codenib-task-{uuid.uuid4().hex[:16]}"
        try:
            argv = self.argv(
                cwd=cwd, system=system, schema=schema, edit=edit, name=name
            )
        except AnnotationError as exc:
            return AgentRun(None, error=str(exc))
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            in ("PATH", "DOCKER_HOST", "DOCKER_CONFIG", "HOME", "XDG_RUNTIME_DIR")
        }
        env.update(self._key_env())
        try:
            completed = subprocess.run(
                argv,
                input=prompt,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError:
            return AgentRun(None, error="docker is not installed on this server")
        except subprocess.TimeoutExpired:
            subprocess.run(
                [self.docker, "kill", name], capture_output=True, check=False
            )
            return AgentRun(
                None, error=f"the agent ran longer than {int(self.timeout)} s"
            )
        (log_dir / f"output-{attempt}.json").write_text(
            completed.stdout, encoding="utf-8"
        )
        stderr = completed.stderr.replace(self._key, "[key]") if self._key else ""
        if stderr:
            (log_dir / f"stderr-{attempt}.txt").write_text(stderr, encoding="utf-8")
        run = parse_claude_output(completed.stdout)
        _relative_files(run.structured)
        if completed.returncode != 0 and not run.error:
            run.error = (
                stderr.strip().splitlines()[-1]
                if stderr.strip()
                else f"the agent exited with status {completed.returncode}"
            )
        return run
