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
from pathlib import Path
from typing import TYPE_CHECKING, Any

_PROMPT_PATH = Path(__file__).parent / "prompts" / "coding_agent.md"

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
        return self._inner.network_allowlist()

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

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        guardian_preamble = _PROMPT_PATH.read_text()
        augmented = f"{guardian_preamble}\n\n---\n\n{instruction}"
        await self._inner.run(augmented, environment, context)
