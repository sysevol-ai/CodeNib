# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""GuardianCodingAgent — Pier solver plus host-controlled Guardian reviews.

Registers with Pier via::

    pier run \\
      --agent-import-path scripts.guardian.deepswe.harness.agent:GuardianCodingAgent \\
      --model gpt-5.6-luna \\
      --ak solver=codex \\
      --ak reasoning_effort=max \\
      --ae "CODEX_FORCE_AUTH_JSON=1" \\
      --mounts-json '<log mounts>' \\
      -p deep-swe/tasks/<task>

Architecture:

    host controller
    ├── Pier solver container publishes commit bundles through /logs/agent
    └── each Guardian rollout runs in a fresh codenib.sandbox container

The solver never shares its writable checkout with Guardian.  It receives
filesystem ``guardian-start``, ``guardian-message``, and
``guardian-checkpoint`` actions while the installed Codex harness continues to
own the implementation loop.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import re
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

_PROMPTS = Path(__file__).parent / "prompts"
_CODEX_PROMPT_PATH = _PROMPTS / "codex_file_bridge.md"

from pier.agents.base import BaseAgent
from pier.agents.installed.base import BaseInstalledAgent
from pier.environments.base import BaseEnvironment
from pier.models.agent.context import AgentContext
from pier.models.agent.install import InstallStep
from pier.models.agent.network import NetworkAllowlist

from codenib.clients.execution import ExecutionIsolation
from codenib.clients.guardian import GuardianConfig

from .checkpoint import guardian_checkpoint_script
from .controller import GuardianHostController, sandbox_reviewer_factory
from .message import guardian_message_script

if TYPE_CHECKING:
    from pier.models.agent.install import AgentInstallSpec

# ---------------------------------------------------------------------------
# Solver registry — maps --ak solver=<name> to the Pier agent class
# ---------------------------------------------------------------------------

_SOLVER_REGISTRY: dict[str, tuple[str, str]] = {
    "codex": ("pier.agents.installed.codex", "Codex"),
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
    except KeyError as exc:
        known = ", ".join(_SOLVER_REGISTRY)
        raise ValueError(
            f"Unknown solver {name!r}. Known solvers: {known}. "
            "Pass --ak solver=<name> to choose."
        ) from exc
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


# ---------------------------------------------------------------------------
# GuardianCodingAgent
# ---------------------------------------------------------------------------


class GuardianCodingAgent(BaseInstalledAgent):
    """Pier custom agent: Codex solver plus a Guardian review sidecar."""

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        logger: logging.Logger | None = None,
        mcp_servers: list[Any] | None = None,
        skills_dir: str | None = None,
        # Guardian kwargs (from --ak)
        solver: str = "codex",
        guardian_repo: str = "/app",
        guardian_model: str | None = None,
        guardian_explorer_model: str | None = None,
        guardian_aggregator_model: str | None = None,
        guardian_explorer_count: int = 2,
        guardian_max_findings: int = 5,
        guardian_max_cycles: int = 3,
        guardian_rollout_timeout: float = 600,
        guardian_poll_interval: int = 10,
        guardian_findings_dir: str = "/logs/agent/guardian_exchange/latest",
        guardian_checkpoint_dir: str = "/app/.guardian/bin",
        guardian_exchange_dir: str = "/logs/agent/guardian_exchange",
        guardian_host_exchange_dir: str | None = None,
        guardian_docker_host: str = "unix:///var/run/docker.sock",
        guardian_platform: str = "linux/amd64",
        guardian_codex_bin: str = "/usr/local/bin/codex-guardian",
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
        self._guardian_repo = guardian_repo
        shared_guardian_model = guardian_model or "codex:gpt-5.6-luna"
        self._guardian_explorer_model = guardian_explorer_model or shared_guardian_model
        self._guardian_aggregator_model = (
            guardian_aggregator_model or shared_guardian_model
        )
        self._guardian_explorer_count = int(guardian_explorer_count)
        self._guardian_max_findings = int(guardian_max_findings)
        self._guardian_max_cycles = int(guardian_max_cycles)
        self._guardian_rollout_timeout = float(guardian_rollout_timeout)
        self._guardian_poll_interval = int(guardian_poll_interval)
        self._guardian_findings_dir = guardian_findings_dir
        self._guardian_checkpoint_dir = guardian_checkpoint_dir
        self._guardian_baseline_file = "/app/.guardian/base_commit"
        self._guardian_exchange_dir = guardian_exchange_dir
        self._guardian_host_exchange_dir = (
            Path(guardian_host_exchange_dir).expanduser().resolve()
            if guardian_host_exchange_dir
            else self.logs_dir / "guardian_exchange"
        )
        self._guardian_message_inbox = f"{guardian_exchange_dir}/messages.jsonl"
        self._guardian_start_path = f"{guardian_checkpoint_dir}/guardian-start"
        self._guardian_message_path = f"{guardian_checkpoint_dir}/guardian-message"
        self._guardian_docker_host = guardian_docker_host
        self._guardian_platform = guardian_platform
        self._guardian_codex_bin = guardian_codex_bin
        self._guardian_baseline_commit = ""
        self._guardian_image = ""

        # Instantiate the inner solver, forwarding all remaining kwargs
        # so that solver-specific flags (e.g. reasoning_effort for codex) pass through.
        solver_cls = _load_solver_class(solver)
        self._inner: BaseAgent = solver_cls(
            logs_dir,
            *args,
            model_name=model_name,
            logger=logger,
            mcp_servers=list(mcp_servers or []),
            skills_dir=skills_dir,
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
        extra = [
            "chatgpt.com",
            "ab.chatgpt.com",
            "auth.openai.com",
            "api.openai.com",
        ]
        return NetworkAllowlist(domains=inner.domains + extra)

    def install_spec(self) -> "AgentInstallSpec | None":
        spec = self._inner.install_spec()
        if spec is None or self._solver_name != "codex":
            return spec
        # Pier's Codex installation may leave /usr/local/bin/codex as a
        # symlink into an agent home. Docker's no-new-privileges policy can
        # deny a different non-root UID access through that home. Materialize
        # an ordinary executable in the prepared image for sibling sandboxes.
        step = InstallStep(
            user="root",
            run=(
                "set -euo pipefail; "
                'source_path="$(readlink -f /usr/local/bin/codex '
                '2>/dev/null || true)"; '
                'if [ ! -f "$source_path" ]; then '
                'source_path="$(find /root/.nvm /usr/local/lib /usr/lib '
                "-type f -path '*/@openai/codex-*/vendor/*/bin/codex' "
                '-print -quit 2>/dev/null)"; fi; '
                'test -n "$source_path"; '
                'install -m 0755 "$source_path" /usr/local/bin/codex-guardian'
            ),
        )
        return spec.model_copy(update={"steps": [*spec.steps, step]})

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
        await self._record_guardian_baseline(environment)
        await self._inner.setup(environment)
        self._guardian_image = self._resolve_environment_image(environment)
        await self._install_codex_actions(environment)

    async def _record_guardian_baseline(self, environment: BaseEnvironment) -> None:
        """Persist cycle-0 HEAD without starting a Guardian model cycle."""

        baseline = shlex.quote(self._guardian_baseline_file)
        repo = shlex.quote(self._guardian_repo)
        checkpoint_dir = _quote_shell_path(self._guardian_checkpoint_dir)
        findings_dir = _quote_shell_path(self._guardian_findings_dir)
        result = await environment.exec(
            f"mkdir -p {checkpoint_dir} {findings_dir} && "
            f"git -C {repo} rev-parse HEAD > {baseline}.tmp && "
            f"mv {baseline}.tmp {baseline} && cat {baseline}"
        )
        self._guardian_baseline_commit = result.stdout.strip()

    @staticmethod
    def _resolve_environment_image(environment: BaseEnvironment) -> str:
        """Resolve the already-built Pier image used by a local Docker trial."""

        if getattr(environment, "_use_prebuilt", False):
            task_config = getattr(environment, "task_env_config", None)
            image = getattr(task_config, "docker_image", None)
        else:
            session_id = getattr(environment, "session_id", None)
            if isinstance(session_id, str) and session_id:
                project = session_id.lower()
                if not re.match(r"^[a-z0-9]", project):
                    project = "0" + project
                project = re.sub(r"[^a-z0-9_-]", "-", project)
                image = f"{project}-main:latest"
            else:
                image = None
        if not isinstance(image, str) or not image:
            raise RuntimeError(
                "Guardian sibling sandboxes currently require Pier's Docker "
                "environment and its prepared agent image"
            )
        return image

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
        if (
            any(
                model.startswith("codex:")
                for model in (
                    self._guardian_explorer_model,
                    self._guardian_aggregator_model,
                )
            )
            and default.is_file()
        ):
            return default

        return None

    async def _install_codex_actions(self, environment: BaseEnvironment) -> None:
        """Install exchange-backed start, message, and checkpoint actions."""

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
                exchange_dir=self._guardian_exchange_dir,
            )
        )
        start_script = shlex.quote(
            "#!/bin/sh\n"
            f"mkdir -p {shlex.quote(self._guardian_exchange_dir)}/requests "
            f"{shlex.quote(self._guardian_exchange_dir)}/bundles "
            f"{shlex.quote(self._guardian_exchange_dir)}/responses\n"
            "echo 'Guardian host controller is ready for checkpoints.'\n"
        )
        message_script = shlex.quote(
            guardian_message_script(
                inbox=self._guardian_message_inbox,
                repo=self._guardian_repo,
            )
        )
        await environment.exec(
            f"mkdir -p {findings_dir} {checkpoint_bin_dir} "
            f"{shlex.quote(self._guardian_exchange_dir)} && "
            f"printf %s {start_script} > {start_path} && "
            f"chmod +x {start_path} && "
            f"printf %s {checkpoint_script} > {checkpoint_path} && "
            f"chmod +x {checkpoint_path} && "
            f"printf %s {message_script} > {message_path} && "
            f"chmod +x {message_path}"
        )

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        guardian_preamble = _CODEX_PROMPT_PATH.read_text()
        augmented = f"{guardian_preamble}\n\n---\n\n{instruction}"
        auth_path = self._resolve_codex_auth_json_path()
        auth_json = auth_path.read_bytes() if auth_path is not None else None
        config = GuardianConfig(
            explorer_model=self._guardian_explorer_model.removeprefix("codex:"),
            aggregator_model=self._guardian_aggregator_model.removeprefix("codex:"),
            explorer_count=self._guardian_explorer_count,
            rollout_timeout_seconds=self._guardian_rollout_timeout,
            max_findings=self._guardian_max_findings,
            execution_isolation=ExecutionIsolation.EXTERNAL,
        )
        reviewer_factory = sandbox_reviewer_factory(
            config=config,
            image=self._guardian_image,
            codex_executable=self._guardian_codex_bin,
            auth_json=auth_json,
            docker_host=self._guardian_docker_host,
            platform=self._guardian_platform,
        )
        with tempfile.TemporaryDirectory(prefix="guardian-memory-") as memory_dir:
            controller = GuardianHostController(
                exchange_root=self._guardian_host_exchange_dir,
                episodes_root=(
                    self._guardian_host_exchange_dir.parent / "guardian_episodes"
                ),
                memory_root=Path(memory_dir),
                config=config,
                reviewer_factory=reviewer_factory,
                initial_base_commit=self._guardian_baseline_commit,
                max_cycles=self._guardian_max_cycles,
                poll_interval_seconds=self._guardian_poll_interval,
            )
            stop = asyncio.Event()
            controller_task = asyncio.create_task(controller.serve(stop))
            try:
                await self._inner.run(augmented, environment, context)
            finally:
                stop.set()
                await controller_task
                exported_memory = (
                    self._guardian_host_exchange_dir.parent / "guardian_memory"
                )
                shutil.copytree(memory_dir, exported_memory, dirs_exist_ok=True)
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
