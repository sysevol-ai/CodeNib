# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""GuardianCodingAgent — Pier custom agent that runs a solver + Guardian sidecar.

Registers with Pier via::

    pier run \\
      --agent-import-path codeminer.guardian.guardian_coding_agent:GuardianCodingAgent \\
      --model gpt-5.6-luna \\
      --ak solver=codex \\
      --ak guardian_arm=memory \\
      --ak reasoning_effort=max \\
      --ae "CODEX_FORCE_AUTH_JSON=1" \\
      --mounts-json '[{"type":"bind","source":"/home/xiangye/CodeMiner","target":"/codeminer"}]' \\
      -p deep-swe/tasks/<task>

Architecture (single Pier container):

    GuardianCodingAgent (this class)
    ├── setup(): installs codeminer in container, then delegates to inner solver's setup()
    │           (inner solver's setup() writes Guardian MCP config for the agent)
    └── run():  delegates to inner solver's run()
               (coding agent spawns Guardian MCP server via stdio on first tool call)

Guardian MCP server is registered as a stdio MCPServerConfig injected into the
inner solver's ``mcp_servers``.  The inner solver (e.g. ClaudeCode) handles
writing that config to ~/.claude.json in its own setup().

A/B/C arms are controlled via ``--ak guardian_arm=<arm>``:
    A: pass ``--agent codex`` (no GuardianCodingAgent)
    B: ``--ak guardian_arm=memoryless``
    C: ``--ak guardian_arm=memory``  (default)
"""

from __future__ import annotations

import importlib
import logging
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any

_PROMPTS = Path(__file__).parent / "prompts"
_PROMPT_PATH = _PROMPTS / "coding_agent.md"
_CODEX_PROMPT_PATH = _PROMPTS / "codex_file_bridge.md"

from pier.agents.base import BaseAgent
from pier.environments.base import BaseEnvironment
from pier.models.agent.context import AgentContext
from pier.models.agent.network import NetworkAllowlist
from pier.models.task.config import MCPServerConfig

if TYPE_CHECKING:
    from pier.models.agent.install import AgentInstallSpec

# ---------------------------------------------------------------------------
# Solver registry — maps --ak solver=<name> to the Pier agent class
# ---------------------------------------------------------------------------

_SOLVER_REGISTRY: dict[str, tuple[str, str]] = {
    "codex": ("pier.agents.installed.codex", "Codex"),
    "claude-code": ("pier.agents.installed.claude_code", "ClaudeCode"),
}


def _quote_shell_path(path: str) -> str:
    """Quote a shell path while preserving the conventional ``~/`` shortcut."""
    if path == "~":
        return "$HOME"
    if path.startswith("~/"):
        return "$HOME/" + shlex.quote(path[2:])
    return shlex.quote(path)


def _load_solver_class(name: str) -> type[BaseAgent]:
    try:
        module_path, class_name = _SOLVER_REGISTRY[name]
    except KeyError:
        known = ", ".join(_SOLVER_REGISTRY)
        raise ValueError(
            f"Unknown solver '{name}'. Known solvers: {known}. "
            "Pass --ak solver=<name> to choose."
        )
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


# ---------------------------------------------------------------------------
# GuardianCodingAgent
# ---------------------------------------------------------------------------

class GuardianCodingAgent(BaseAgent):
    """Pier custom agent: any solver + Guardian as an MCP sidecar."""

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        logger: logging.Logger | None = None,
        mcp_servers: list[MCPServerConfig] | None = None,
        skills_dir: str | None = None,
        # Guardian kwargs (from --ak)
        solver: str = "codex",
        guardian_arm: str = "memory",
        guardian_repo: str = "/app",
        guardian_memory_dir: str = "/app/.guardian/memory",
        guardian_model: str = "vertex_ai/gemini-2.5-flash",
        guardian_top_n: int = 5,
        guardian_budget_tokens: int = 50_000,
        guardian_poll_interval: int = 10,
        guardian_findings_dir: str = "/app/.guardian",
        # Path to codeminer source tree inside the container (pip install -e)
        codeminer_path: str = "/codeminer",
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            logs_dir,
            model_name=model_name,
            logger=logger,
            mcp_servers=mcp_servers,
            skills_dir=skills_dir,
        )

        self._solver_name = solver
        self._codeminer_path = codeminer_path
        self._guardian_repo = guardian_repo
        self._guardian_arm = guardian_arm
        self._guardian_memory_dir = guardian_memory_dir
        self._guardian_model = guardian_model
        self._guardian_top_n = int(guardian_top_n)
        self._guardian_budget_tokens = int(guardian_budget_tokens)
        self._guardian_poll_interval = int(guardian_poll_interval)
        self._guardian_findings_dir = guardian_findings_dir
        self._guardian_bridge_pidfile = f"{guardian_findings_dir}/codex_bridge.pid"

        # Build the Guardian MCPServerConfig (stdio transport).
        # The inner solver's setup() writes this to its MCP config file.
        guardian_mcp = MCPServerConfig(
            name="guardian",
            transport="stdio",
            command="python",
            args=[
                "-m", "codeminer.guardian.mcp_server",
                "--repo", guardian_repo,
                "--arm", guardian_arm,
                "--memory-dir", guardian_memory_dir,
                "--model", guardian_model,
                "--top-n", str(guardian_top_n),
                "--budget-tokens", str(guardian_budget_tokens),
                "--poll-interval", str(guardian_poll_interval),
                "--trace-log", "/logs/agent/guardian_queries.jsonl",
            ],
        )

        # Merge caller-supplied MCP servers with Guardian's entry
        combined_mcp = list(mcp_servers or []) + [guardian_mcp]

        # Instantiate the inner solver, forwarding all remaining kwargs
        # so that solver-specific flags (e.g. reasoning_effort for codex) pass through.
        solver_cls = _load_solver_class(solver)
        self._inner: BaseAgent = solver_cls(
            logs_dir,
            model_name=model_name,
            logger=logger,
            mcp_servers=combined_mcp,
            skills_dir=skills_dir,
            *args,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # Pier agent identity
    # ------------------------------------------------------------------

    @staticmethod
    def name() -> str:
        return "guardian-coding-agent"

    def version(self) -> str | None:
        return self._inner.version()

    # ------------------------------------------------------------------
    # Pier lifecycle — delegate to inner solver
    # ------------------------------------------------------------------

    def network_allowlist(self) -> NetworkAllowlist:
        inner = self._inner.network_allowlist()
        extra: list[str] = []
        model = self._guardian_model
        if model.startswith("vertex_ai/") or model.startswith("gemini/"):
            # Vertex AI / Google AI Studio inference + auth
            extra = [".googleapis.com", "accounts.google.com"]
        elif model.startswith("anthropic/"):
            extra = ["api.anthropic.com"]
        elif model.startswith("openai/") or model.startswith("gpt"):
            extra = ["api.openai.com"]
        return NetworkAllowlist(domains=inner.domains + extra)

    def install_spec(self) -> "AgentInstallSpec | None":
        return self._inner.install_spec()

    def populate_context_post_run(self, context: AgentContext) -> None:
        self._inner.populate_context_post_run(context)

    async def setup(self, environment: BaseEnvironment) -> None:
        # Make codeminer available in the container so Guardian MCP server can run.
        # The caller mounts the CodeMiner source tree at self._codeminer_path via
        # --mounts-json '[{"type":"bind","source":"/path/to/CodeMiner","target":"/codeminer"}]'
        await environment.exec(
            f"pip install -q -e {self._codeminer_path} 2>&1 | tail -3 || true",
        )
        # Delegate to inner solver — it handles MCP config registration, skills, etc.
        await self._inner.setup(environment)

    async def _start_codex_bridge(self, environment: BaseEnvironment) -> None:
        """Start Guardian's Codex filesystem bridge inside the Pier container."""
        findings_dir = _quote_shell_path(self._guardian_findings_dir)
        pidfile = _quote_shell_path(self._guardian_bridge_pidfile)
        repo = shlex.quote(self._guardian_repo)
        memory_dir = shlex.quote(self._guardian_memory_dir)
        model = shlex.quote(self._guardian_model)
        arm = shlex.quote(self._guardian_arm)
        cmd = (
            f"mkdir -p {findings_dir} && "
            "nohup python -m codeminer.guardian.codex_bridge "
            f"--repo {repo} "
            f"--arm {arm} "
            f"--memory-dir {memory_dir} "
            f"--model {model} "
            f"--top-n {self._guardian_top_n} "
            f"--budget-tokens {self._guardian_budget_tokens} "
            f"--poll-interval {self._guardian_poll_interval} "
            f"--out-dir {findings_dir} "
            f"> {findings_dir}/codex_bridge.log 2>&1 & "
            f"echo $! > {pidfile}"
        )
        await environment.exec(cmd)

    async def _stop_codex_bridge(self, environment: BaseEnvironment) -> None:
        pidfile = _quote_shell_path(self._guardian_bridge_pidfile)
        await environment.exec(
            f"if [ -f {pidfile} ]; then "
            f"kill $(cat {pidfile}) 2>/dev/null || true; "
            f"rm -f {pidfile}; "
            "fi"
        )

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        guardian_preamble = _PROMPT_PATH.read_text()
        if self._solver_name == "codex":
            await self._start_codex_bridge(environment)
            guardian_preamble = (
                f"{guardian_preamble}\n\n{_CODEX_PROMPT_PATH.read_text()}"
            )
        augmented = f"{guardian_preamble}\n\n---\n\n{instruction}"
        try:
            await self._inner.run(augmented, environment, context)
        finally:
            if self._solver_name == "codex":
                await self._stop_codex_bridge(environment)
