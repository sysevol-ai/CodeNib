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
from pier.agents.installed.base import BaseInstalledAgent
from pier.environments.base import BaseEnvironment
from pier.models.agent.context import AgentContext
from pier.models.agent.network import NetworkAllowlist
from pier.models.task.config import MCPServerConfig

from .checkpoint import guardian_checkpoint_script

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


class GuardianCodingAgent(BaseInstalledAgent):
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
        guardian_memory_dir: str = "~/.guardian/memory",
        guardian_model: str = "codex:gpt-5.6-luna",
        guardian_top_n: int = 5,
        guardian_budget_tokens: int = 50_000,
        guardian_poll_interval: int = 10,
        guardian_findings_dir: str = "~/.guardian",
        # Path to codeminer source tree inside the container
        codeminer_path: str = "/codeminer",
        # Path to the mounted host Python env that has codeminer's deps installed
        # (litellm, rich, pydantic, etc.) — avoids PyPI downloads inside the container
        codeminer_python: str = "/opt/codeminer-env/bin/python",
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
        self._codeminer_python = codeminer_python
        self._guardian_repo = guardian_repo
        self._guardian_arm = guardian_arm
        self._guardian_memory_dir = guardian_memory_dir
        self._guardian_model = guardian_model
        self._guardian_top_n = int(guardian_top_n)
        self._guardian_budget_tokens = int(guardian_budget_tokens)
        self._guardian_poll_interval = int(guardian_poll_interval)
        self._guardian_findings_dir = guardian_findings_dir
        self._guardian_bridge_pidfile = f"{guardian_findings_dir}/codex_bridge.pid"
        self._guardian_codex_home = "/tmp/guardian-codex-home"
        self._guardian_codex_secrets_dir = "/tmp/guardian-codex-secrets"

        # Build the Guardian MCPServerConfig (stdio transport).
        # The inner solver's setup() writes this to its MCP config file.
        guardian_mcp = MCPServerConfig(
            name="guardian",
            transport="stdio",
            command="python",
            args=[
                "-m",
                "codeminer.guardian.mcp_server",
                "--repo",
                guardian_repo,
                "--arm",
                guardian_arm,
                "--memory-dir",
                guardian_memory_dir,
                "--model",
                guardian_model,
                "--top-n",
                str(guardian_top_n),
                "--budget-tokens",
                str(guardian_budget_tokens),
                "--poll-interval",
                str(guardian_poll_interval),
                "--trace-log",
                "/logs/agent/guardian_queries.jsonl",
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
        elif model.startswith("codex:"):
            extra = [
                "chatgpt.com",
                "ab.chatgpt.com",
                "auth.openai.com",
                "api.openai.com",
            ]
        return NetworkAllowlist(domains=inner.domains + extra)

    def install_spec(self) -> "AgentInstallSpec | None":
        return self._inner.install_spec()

    def populate_context_post_run(self, context: AgentContext) -> None:
        self._inner.populate_context_post_run(context)
        # Populate token counts from codex_tokens.json written by _write_codex_token_summary.
        # Only fills fields that the inner solver left null (codex + ChatGPT subscription
        # never reports tokens through Pier's normal path).
        if self._solver_name == "codex" and context.n_input_tokens is None:
            import json as _json

            tokens_path = self.logs_dir / "codex_tokens.json"
            try:
                tok = _json.loads(tokens_path.read_text())
                context.n_input_tokens = tok.get("input_tokens")
                context.n_cache_tokens = tok.get("cached_input_tokens")
                context.n_output_tokens = tok.get("output_tokens")
            except Exception:
                pass

    async def setup(self, environment: BaseEnvironment) -> None:
        # Delegate to inner solver — it handles MCP config registration, skills, etc.
        # Guardian runs via a mounted host Python env (self._codeminer_python) that
        # already has codeminer's deps installed; no container pip install needed.
        await self._inner.setup(environment)

    def _resolve_codex_auth_json_path(self) -> Path | None:
        """Resolve the host Codex auth.json path for Guardian's nested Codex calls."""
        explicit = self._get_env("CODEX_AUTH_JSON_PATH")
        if explicit:
            path = Path(explicit)
            if not path.is_file():
                raise ValueError(
                    f"CODEX_AUTH_JSON_PATH points to non-existent file: {explicit}"
                )
            return path

        force = (self._get_env("CODEX_FORCE_AUTH_JSON") or "").strip().lower()
        if force in {"1", "true", "yes", "y", "on"}:
            path = Path.home() / ".codex" / "auth.json"
            if not path.is_file():
                raise ValueError(
                    f"CODEX_FORCE_AUTH_JSON is set but {path} does not exist"
                )
            return path

        default = Path.home() / ".codex" / "auth.json"
        if self._guardian_model.startswith("codex:") and default.is_file():
            return default

        return None

    async def _prepare_guardian_codex_auth(
        self,
        environment: BaseEnvironment,
        *,
        codex_home: str,
    ) -> None:
        """Upload subscription auth for Guardian's separate Codex runtime."""
        auth_json_path = self._resolve_codex_auth_json_path()
        if auth_json_path is None:
            return

        secrets_dir = self._guardian_codex_secrets_dir
        remote_auth_path = f"{secrets_dir}/auth.json"
        await environment.exec(
            "mkdir -p "
            f"{shlex.quote(codex_home)} {shlex.quote(secrets_dir)}"
        )
        await environment.upload_file(auth_json_path, remote_auth_path)
        default_user = getattr(environment, "default_user", None)
        if default_user is not None:
            await self.exec_as_root(
                environment,
                command=f"chown {default_user} {shlex.quote(remote_auth_path)}",
            )
        await environment.exec(
            f"ln -sf {shlex.quote(remote_auth_path)} "
            f"{shlex.quote(codex_home)}/auth.json"
        )

    async def _start_codex_bridge(self, environment: BaseEnvironment) -> None:
        """Start Guardian's Codex filesystem bridge inside the Pier container."""
        findings_dir = _quote_shell_path(self._guardian_findings_dir)
        checkpoint_bin_dir = _quote_shell_path(f"{self._guardian_findings_dir}/bin")
        checkpoint_path = _quote_shell_path(
            f"{self._guardian_findings_dir}/bin/guardian-checkpoint"
        )
        checkpoint_script = shlex.quote(guardian_checkpoint_script())
        pidfile = _quote_shell_path(self._guardian_bridge_pidfile)
        repo = shlex.quote(self._guardian_repo)
        memory_dir = shlex.quote(self._guardian_memory_dir)
        model = shlex.quote(self._guardian_model)
        arm = shlex.quote(self._guardian_arm)

        # Bake the auth/proxy vars directly into the nohup env command. exec()
        # calls only inherit _persistent_env, not _egress_proxy_env (which Pier
        # applies to the agent's main process via agent_process_env()).
        _persistent = getattr(environment, "_persistent_env", {})
        gtoken = _persistent.get("GOOGLE_OAUTH_ACCESS_TOKEN", "")
        auth_json_path = self._resolve_codex_auth_json_path()
        codex_home = _persistent.get("CODEX_HOME", "") or self._get_env("CODEX_HOME")
        codex_force_auth = _persistent.get(
            "CODEX_FORCE_AUTH_JSON", ""
        ) or self._get_env("CODEX_FORCE_AUTH_JSON")
        if auth_json_path is not None and not codex_home:
            codex_home = self._guardian_codex_home
        if auth_json_path is not None and not codex_force_auth:
            codex_force_auth = "1"
        if codex_home:
            await self._prepare_guardian_codex_auth(
                environment,
                codex_home=codex_home,
            )
        _egress = getattr(environment, "_egress_proxy_env", {})
        https_proxy = _egress.get("HTTPS_PROXY", "")
        http_proxy = _egress.get("HTTP_PROXY", "")
        no_proxy = _egress.get("NO_PROXY", "")
        codex_env = ""
        if codex_home:
            codex_env += f"CODEX_HOME={shlex.quote(codex_home)} "
        if codex_force_auth:
            codex_env += f"CODEX_FORCE_AUTH_JSON={shlex.quote(codex_force_auth)} "
        auth_probe = ""
        if codex_home:
            quoted_home = shlex.quote(codex_home)
            auth_probe = (
                f"if [ -s {quoted_home}/auth.json ]; then "
                "echo guardian-codex-auth-ready >> /logs/agent/codex_bridge.log; "
                "else "
                "echo guardian-codex-auth-missing >> /logs/agent/codex_bridge.log; "
                "fi && "
            )

        cmd = (
            f"mkdir -p {findings_dir} {checkpoint_bin_dir} && "
            f"printf %s {checkpoint_script} > {checkpoint_path} && "
            f"chmod +x {checkpoint_path} && "
            "echo guardian-bridge-starting > /logs/agent/codex_bridge.log && "
            f"{auth_probe}"
            "if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi && "
            "CODEMINER_CODEX_BIN=$(command -v codex || true) && "
            f"nohup env PYTHONPATH=/codeminer "
            'PATH="$PATH" '
            'CODEMINER_CODEX_BIN="$CODEMINER_CODEX_BIN" '
            f"GOOGLE_OAUTH_ACCESS_TOKEN={shlex.quote(gtoken)} "
            f"{codex_env}"
            f"HTTPS_PROXY={shlex.quote(https_proxy)} "
            f"HTTP_PROXY={shlex.quote(http_proxy)} "
            f"https_proxy={shlex.quote(https_proxy)} "
            f"http_proxy={shlex.quote(http_proxy)} "
            f"NO_PROXY={shlex.quote(no_proxy)} "
            f"no_proxy={shlex.quote(no_proxy)} "
            f"{self._codeminer_python} -m deepsweguardian.codex_bridge "
            f"--repo {repo} "
            f"--arm {arm} "
            f"--memory-dir {memory_dir} "
            f"--model {model} "
            f"--top-n {self._guardian_top_n} "
            f"--budget-tokens {self._guardian_budget_tokens} "
            f"--poll-interval {self._guardian_poll_interval} "
            f"--out-dir {findings_dir} "
            f">> /logs/agent/codex_bridge.log 2>&1 & "
            f"echo $! | tee {pidfile} > /logs/agent/codex_bridge.pid"
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

    async def _wait_for_codex_bridge_idle(
        self,
        environment: BaseEnvironment,
        timeout_sec: int = 240,
    ) -> None:
        status_path = f"{self._guardian_findings_dir}/status.json"
        script = f"""
import json
import os
import sys
import time

path = os.path.expanduser({status_path!r})
deadline = time.time() + {int(timeout_sec)}
while time.time() < deadline:
    try:
        with open(path, encoding="utf-8") as f:
            status = json.load(f)
    except FileNotFoundError:
        time.sleep(2)
        continue
    except Exception as exc:
        print(f"guardian status read failed: {{exc}}", file=sys.stderr)
        time.sleep(2)
        continue
    if not status.get("running"):
        print("guardian bridge idle")
        raise SystemExit(0)
    time.sleep(2)
print("guardian bridge still running after wait", file=sys.stderr)
"""
        try:
            await environment.exec(f"python3 -c {shlex.quote(script)}")
        except Exception:
            # Do not fail the Pier task solely because the advisory sidecar timed out.
            pass

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
                await self._wait_for_codex_bridge_idle(environment)
                await self._stop_codex_bridge(environment)
                await self._write_codex_token_summary(environment)

    async def _write_codex_token_summary(self, environment: BaseEnvironment) -> None:
        """Parse codex turn.completed events and write token totals to codex_tokens.json."""
        script = r"""
import json, sys
keys = ['input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens']
totals = {k: 0 for k in keys}
try:
    with open('/logs/agent/codex.txt') as f:
        for line in f:
            line = line.strip()
            if '"turn.completed"' not in line:
                continue
            try:
                u = json.loads(line).get('usage', {})
                for k in keys:
                    totals[k] += u.get(k, 0)
            except Exception:
                pass
except FileNotFoundError:
    pass
with open('/logs/agent/codex_tokens.json', 'w') as f:
    json.dump(totals, f, indent=2)
print(json.dumps(totals))
"""
        try:
            result = await environment.exec(f"python3 -c {shlex.quote(script)}")
            if result.stdout:
                self.logger.info("codex tokens: %s", result.stdout.strip())
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("failed to write codex token summary: %s", exc)
