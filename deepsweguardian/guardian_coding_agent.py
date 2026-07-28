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
    ├── setup(): records cycle-0 HEAD, delegates to the inner solver, and
    │            installs the solver-specific lazy Guardian action
    └── run():   delegates to the inner solver's run()

MCP-native solvers receive ``query_guardian``. Codex receives filesystem
``guardian-start`` and ``guardian-checkpoint`` actions because that bridge also
provides a synchronized final report. Only one Guardian transport is registered
per solver, avoiding duplicate analysis cycles and model spend.

For MCP-native solvers, the Guardian server is registered as a stdio
MCPServerConfig injected into the inner solver's ``mcp_servers``. The inner
solver (e.g. ClaudeCode) writes that config in its own setup().

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
from .lazy_start import guardian_start_script

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


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


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
        guardian_memory_dir: str = "/app/.guardian/memory",
        guardian_model: str = "codex:gpt-5.6-luna",
        guardian_top_n: int = 5,
        guardian_budget_tokens: int = 50_000,
        guardian_no_budget_limit: bool = False,
        guardian_max_context_tokens: int = 200_000,
        guardian_poll_interval: int = 10,
        guardian_findings_dir: str = "/app/.guardian/out",
        guardian_checkpoint_dir: str = "/app/.guardian/bin",
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
        self._guardian_no_budget_limit = _as_bool(guardian_no_budget_limit)
        self._guardian_max_context_tokens = int(guardian_max_context_tokens)
        self._guardian_poll_interval = int(guardian_poll_interval)
        self._guardian_findings_dir = guardian_findings_dir
        self._guardian_checkpoint_dir = guardian_checkpoint_dir
        self._guardian_bridge_pidfile = f"{guardian_findings_dir}/codex_bridge.pid"
        self._guardian_baseline_file = "/app/.guardian/base_commit"
        self._guardian_message_inbox = "/app/.guardian/inbox/messages.jsonl"
        self._guardian_start_path = f"{guardian_checkpoint_dir}/guardian-start"
        self._guardian_message_path = f"{guardian_checkpoint_dir}/guardian-message"
        self._guardian_codex_home = "/tmp/guardian-codex-home"
        self._guardian_codex_secrets_dir = "/tmp/guardian-codex-secrets"

        # Build the Guardian MCPServerConfig (stdio transport).
        # The inner solver's setup() writes this to its MCP config file.
        guardian_mcp_args = [
            "-m",
            "codeminer.guardian.mcp_server",
            "--repo",
            guardian_repo,
            "--arm",
            guardian_arm,
            "--memory-dir",
            guardian_memory_dir,
            "--message-inbox",
            self._guardian_message_inbox,
            "--model",
            guardian_model,
            "--top-n",
            str(guardian_top_n),
            "--max-context-tokens",
            str(self._guardian_max_context_tokens),
            "--poll-interval",
            str(guardian_poll_interval),
            "--trace-log",
            "/logs/agent/guardian_queries.jsonl",
            "--baseline-file",
            self._guardian_baseline_file,
        ]
        if self._guardian_no_budget_limit:
            guardian_mcp_args.append("--no-budget-limit")
        else:
            guardian_mcp_args.extend(
                ["--budget-tokens", str(self._guardian_budget_tokens)]
            )
        guardian_mcp = MCPServerConfig(
            name="guardian",
            transport="stdio",
            command="python",
            args=guardian_mcp_args,
        )

        # Codex uses the synchronized filesystem bridge below. Registering both
        # transports would run duplicate Guardian cycles for the same commit.
        combined_mcp = list(mcp_servers or [])
        if solver != "codex":
            combined_mcp.append(guardian_mcp)

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
        await self._record_guardian_baseline(environment)
        await self._inner.setup(environment)
        if self._solver_name == "codex":
            await self._install_codex_bridge_actions(environment)

    async def _record_guardian_baseline(self, environment: BaseEnvironment) -> None:
        """Persist cycle-0 HEAD without starting a Guardian model cycle."""

        baseline = shlex.quote(self._guardian_baseline_file)
        repo = shlex.quote(self._guardian_repo)
        checkpoint_dir = _quote_shell_path(self._guardian_checkpoint_dir)
        findings_dir = _quote_shell_path(self._guardian_findings_dir)
        await environment.exec(
            f"mkdir -p {checkpoint_dir} {findings_dir} && "
            f"git -C {repo} rev-parse HEAD > {baseline}.tmp && "
            f"mv {baseline}.tmp {baseline}"
        )

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
            "mkdir -p " f"{shlex.quote(codex_home)} {shlex.quote(secrets_dir)}"
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

    async def _install_codex_bridge_actions(self, environment: BaseEnvironment) -> None:
        """Install lazy, idempotent start and checkpoint actions."""

        findings_dir = _quote_shell_path(self._guardian_findings_dir)
        checkpoint_bin_dir = _quote_shell_path(self._guardian_checkpoint_dir)
        checkpoint_path = _quote_shell_path(
            f"{self._guardian_checkpoint_dir}/guardian-checkpoint"
        )
        start_path = _quote_shell_path(self._guardian_start_path)
        message_path = _quote_shell_path(self._guardian_message_path)
        checkpoint_script = shlex.quote(
            guardian_checkpoint_script(
                start_command=self._guardian_start_path,
                baseline_file=self._guardian_baseline_file,
            )
        )

        # The launcher runs later as an agent action, so bake the sidecar-only
        # credentials and Pier proxy settings into its child environment now.
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
        task_virtualenv = str(_persistent.get("VIRTUAL_ENV", "") or "").strip()
        bridge_environment = {
            "PYTHONPATH": self._codeminer_path,
            "GUARDIAN_RUNTIME_PYTHON": self._codeminer_python,
            "GUARDIAN_TASK_PYTHON": (
                f"{task_virtualenv.rstrip('/')}/bin/python" if task_virtualenv else ""
            ),
            "GOOGLE_OAUTH_ACCESS_TOKEN": gtoken,
            "HTTPS_PROXY": _egress.get("HTTPS_PROXY", ""),
            "HTTP_PROXY": _egress.get("HTTP_PROXY", ""),
            "https_proxy": _egress.get("HTTPS_PROXY", ""),
            "http_proxy": _egress.get("HTTP_PROXY", ""),
            "NO_PROXY": _egress.get("NO_PROXY", ""),
            "no_proxy": _egress.get("NO_PROXY", ""),
        }
        if codex_home:
            bridge_environment["CODEX_HOME"] = codex_home
        if codex_force_auth:
            bridge_environment["CODEX_FORCE_AUTH_JSON"] = codex_force_auth

        bridge_command = [
            self._codeminer_python,
            "-m",
            "deepsweguardian.codex_bridge",
            "--repo",
            self._guardian_repo,
            "--arm",
            self._guardian_arm,
            "--memory-dir",
            self._guardian_memory_dir,
            "--message-inbox",
            self._guardian_message_inbox,
            "--model",
            self._guardian_model,
            "--top-n",
            str(self._guardian_top_n),
            "--max-context-tokens",
            str(self._guardian_max_context_tokens),
            "--poll-interval",
            str(self._guardian_poll_interval),
            "--out-dir",
            self._guardian_findings_dir,
            "--baseline-file",
            self._guardian_baseline_file,
        ]
        if self._guardian_no_budget_limit:
            bridge_command.append("--no-budget-limit")
        else:
            bridge_command.extend(
                ["--budget-tokens", str(self._guardian_budget_tokens)]
            )
        start_script = shlex.quote(
            guardian_start_script(
                command=bridge_command,
                environment=bridge_environment,
                repo=self._guardian_repo,
                baseline_file=self._guardian_baseline_file,
                pid_file=self._guardian_bridge_pidfile,
                log_file="/logs/agent/codex_bridge.log",
            )
        )
        message_script = shlex.quote(
            "#!/bin/sh\n"
            f"exec {shlex.quote(self._codeminer_python)} "
            f"{shlex.quote(self._codeminer_path + '/codeminer/guardian/interaction.py')} "
            f"--inbox {shlex.quote(self._guardian_message_inbox)} "
            f"--repo {shlex.quote(self._guardian_repo)} "
            '"$@"\n'
        )
        await environment.exec(
            f"mkdir -p {findings_dir} {checkpoint_bin_dir} && "
            f"printf %s {start_script} > {start_path} && "
            f"chmod +x {start_path} && "
            f"printf %s {checkpoint_script} > {checkpoint_path} && "
            f"chmod +x {checkpoint_path} && "
            f"printf %s {message_script} > {message_path} && "
            f"chmod +x {message_path}"
        )

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
pidfile = os.path.expanduser({self._guardian_bridge_pidfile!r})
try:
    with open(pidfile, encoding="utf-8") as f:
        pid = int(f.read().strip())
    os.kill(pid, 0)
except (FileNotFoundError, OSError, ValueError):
    print("guardian bridge was not started")
    raise SystemExit(0)
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
        if self._solver_name == "codex":
            guardian_preamble = _CODEX_PROMPT_PATH.read_text()
        else:
            guardian_preamble = _PROMPT_PATH.read_text()
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
