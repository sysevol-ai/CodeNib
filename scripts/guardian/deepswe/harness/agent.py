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
import json
import logging
import re
import shlex
import shutil
import tempfile
import threading
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
from .delivery import (
    CompletedReport,
    completed_report,
    completed_responses,
    review_request_commits,
)
from .message import guardian_message_script
from .solver_logs import (
    CodexTurnLog,
    capture_codex_turn,
    codex_usage,
    persist_codex_logs,
    restore_codex_accounting,
)

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
        guardian_explorer_count: int = 4,
        guardian_targeted_explorer_count: int = 2,
        guardian_search_rounds: int = 2,
        guardian_max_findings: int = 10,
        guardian_max_patch_probes: int = 5,
        guardian_max_explorer_repairs: int = 1,
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
        guardian_codex_version: str = "0.145.0",
        guardian_max_patch_specifications: int | None = None,
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
        self._guardian_targeted_explorer_count = int(guardian_targeted_explorer_count)
        self._guardian_search_rounds = int(guardian_search_rounds)
        self._guardian_max_findings = int(guardian_max_findings)
        if guardian_max_patch_specifications is not None:
            guardian_max_patch_probes = guardian_max_patch_specifications
        self._guardian_max_patch_probes = int(guardian_max_patch_probes)
        self._guardian_max_explorer_repairs = int(guardian_max_explorer_repairs)
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
        if not re.fullmatch(
            r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", guardian_codex_version
        ):
            raise ValueError("guardian_codex_version must be an exact semantic version")
        self._guardian_codex_version = guardian_codex_version
        self._guardian_baseline_commit = ""
        self._guardian_image = ""
        # Pier's installed-agent post-run hook may replace the conventional
        # ``codex.txt`` file.  Keep immutable per-turn snapshots on the wrapper
        # until that hook has finished so cumulative usage remains authoritative.
        self._solver_turn_logs: list[CodexTurnLog] = []

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
        # Guardian deliberately disables Code Mode. Codex 0.147 routes Luna
        # through Code Mode even with the direct shell tool enabled, so keep a
        # separately pinned Guardian executable without changing Pier's solver.
        codex_step = InstallStep(
            user="root",
            run=(
                "set -euo pipefail; "
                "prefix=/opt/codenib-guardian-codex; "
                'npm install --global --prefix "$prefix" '
                f"@openai/codex@{self._guardian_codex_version}; "
                'source_path="$(find "$prefix" -type f '
                "-path '*/@openai/codex-*/vendor/*/bin/codex' "
                '-print -quit)"; '
                'test -n "$source_path"; '
                'install -m 0755 "$source_path" /usr/local/bin/codex-guardian'
            ),
        )
        task_environment_step = InstallStep(
            user="root",
            run=(
                "set -euo pipefail; "
                "profile=/etc/profile.d/zz-codenib-task-venv.sh; "
                "printf '%s\\n' "
                '\'if [ -n "${VIRTUAL_ENV:-}" ] && '
                '[ -d "${VIRTUAL_ENV}/bin" ]; then\' '
                "'    PATH=\"${VIRTUAL_ENV}/bin:${PATH}\"' "
                "'    export PATH' "
                "'fi' > \"$profile\"; "
                'chmod 0644 "$profile"'
            ),
        )
        return spec.model_copy(
            update={"steps": [*spec.steps, codex_step, task_environment_step]}
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        self._inner.populate_context_post_run(context)
        # The inner agent is allowed to normalize/replace its conventional log
        # during post-run collection.  Restore our cumulative view afterwards.
        if self._solver_name == "codex" and self._solver_turn_logs:
            totals = restore_codex_accounting(
                self._solver_logs_dir(), self._solver_turn_logs
            )
            context.n_input_tokens = totals["input_tokens"]
            context.n_cache_tokens = totals["cached_input_tokens"]
            context.n_output_tokens = totals["output_tokens"]
        # Populate token counts from the cumulative solver-log summary.
        # Only fills fields that the inner solver left null (codex + ChatGPT subscription
        # never reports tokens through Pier's normal path).
        if self._solver_name == "codex" and not self._solver_turn_logs:
            tokens_path = self._solver_logs_dir() / "codex_tokens.json"
            try:
                tok = json.loads(tokens_path.read_text())
                context.n_input_tokens = tok.get("input_tokens")
                context.n_cache_tokens = tok.get("cached_input_tokens")
                context.n_output_tokens = tok.get("output_tokens")
            except Exception:
                pass

    def _capture_solver_log(self, turn_index: int) -> tuple[Path, bytes] | None:
        """Archive and snapshot one solver turn before another turn can replace it."""
        return capture_codex_turn(self._solver_logs_dir(), turn_index)

    def _solver_logs_dir(self) -> Path:
        """Return the host directory mounted at ``/logs/agent`` for the solver."""
        return self._guardian_host_exchange_dir.parent

    def _restore_solver_logs(self, turn_logs: list[CodexTurnLog]) -> None:
        """Persist per-turn and combined logs from the in-memory snapshots."""
        persist_codex_logs(self._solver_logs_dir(), turn_logs)

    @staticmethod
    def _codex_usage(turn_logs: list[CodexTurnLog]) -> dict[str, int]:
        """Sum Codex usage events from immutable snapshots of every solver turn."""
        return codex_usage(turn_logs)

    def _log_codex_token_summary(self, turn_logs: list[CodexTurnLog]) -> None:
        """Log the cumulative usage calculated from immutable turn snapshots."""
        totals = self._codex_usage(turn_logs)
        self.logger.info("codex tokens: %s", json.dumps(totals))

    def _latest_report(self) -> tuple[str, str, tuple[str, ...], int] | None:
        latest = self._guardian_host_exchange_dir / "latest"
        status_path = latest / "status.json"
        report_path = latest / "findings.md"
        findings_path = latest / "findings.json"
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            report = report_path.read_text(encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(status, dict) or not status.get("review_performed", True):
            return None
        commit = str(status.get("commit") or "").strip()
        identifiers: list[str] = []
        try:
            payload = json.loads(findings_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                for key in ("findings", "uncertain_specifications"):
                    rows = payload.get(key, [])
                    if isinstance(rows, list):
                        identifiers.extend(
                            str(row.get("specification_id") or "").strip()
                            for row in rows
                            if isinstance(row, dict)
                        )
        except (OSError, json.JSONDecodeError):
            pass
        return (
            commit,
            report,
            tuple(value for value in identifiers if value),
            status_path.stat().st_mtime_ns,
        )

    @staticmethod
    def _solver_observed_report(
        turn_log: Path | None,
        *,
        report: str,
        identifiers: tuple[str, ...],
        completed_at_ns: int,
    ) -> bool:
        if turn_log is None:
            return False
        try:
            if turn_log.stat().st_mtime_ns < completed_at_ns:
                return False
        except OSError:
            return False
        try:
            transcript = turn_log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        if identifiers:
            return all(value in transcript for value in identifiers)
        marker = "No definite evidence-backed patch violation was found."
        return marker in report and marker in transcript

    @staticmethod
    def _revision_instruction(original: str, report: str) -> str:
        return (
            "The original implementation turn ended before it demonstrably consumed "
            "the completed Guardian report. Continue in the current repository and "
            "evaluate the report against the code and tests. Address only findings "
            "that you verify. If a finding is unsupported, explain why and leave the "
            "code unchanged. If you revise the patch, test and commit it, then run the "
            "Guardian checkpoint again when another review cycle remains.\n\n"
            "Original task:\n\n"
            f"{original}\n\n"
            "Completed Guardian report:\n\n"
            f"{report}"
        )

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

        # Guardian currently executes every review role through CodexExecutor.
        # A raw model name (for example ``gpt-5.6-luna``) is therefore no less
        # in need of the Codex credential than its ``codex:``-prefixed form.
        default = Path.home() / ".codex" / "auth.json"
        if default.is_file():
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
            targeted_explorer_count=self._guardian_targeted_explorer_count,
            max_rounds=self._guardian_search_rounds,
            rollout_timeout_seconds=self._guardian_rollout_timeout,
            max_findings=self._guardian_max_findings,
            max_patch_probes=self._guardian_max_patch_probes,
            max_explorer_repairs=self._guardian_max_explorer_repairs,
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
                task_description=instruction,
                max_cycles=self._guardian_max_cycles,
                poll_interval_seconds=self._guardian_poll_interval,
            )
            stop = threading.Event()
            controller_errors: list[Exception] = []

            def run_controller() -> None:
                try:
                    controller.serve_blocking(stop)
                except Exception as exc:  # noqa: BLE001
                    controller_errors.append(exc)

            controller_thread = threading.Thread(
                target=run_controller,
                name="guardian-host-controller",
                daemon=True,
            )
            controller_thread.start()
            turn_logs: list[CodexTurnLog] = []
            delivered_reviews: set[str] = set()
            next_instruction = augmented
            turn_index = 0
            try:
                while turn_index <= self._guardian_max_cycles:
                    turn_index += 1
                    turn_log: Path | None = None
                    requests_before = review_request_commits(
                        self._guardian_host_exchange_dir
                    )
                    try:
                        await self._inner.run(next_instruction, environment, context)
                    finally:
                        captured = self._capture_solver_log(turn_index)
                        if captured is not None:
                            turn_log, transcript = captured
                            turn_logs.append((turn_index, transcript))

                    # Controller idleness alone is racy: the solver can publish a
                    # manifest just after the controller's preceding poll. Wait
                    # for every request created by this turn to reach a terminal
                    # response before deciding whether another solver turn is
                    # necessary.
                    requested = (
                        review_request_commits(self._guardian_host_exchange_dir)
                        - requests_before
                    )
                    while requested - completed_responses(
                        self._guardian_host_exchange_dir, requested
                    ):
                        if controller_errors:
                            raise RuntimeError(
                                "Guardian host controller failed"
                            ) from controller_errors[0]
                        await asyncio.sleep(0.1)
                    while not controller.is_idle():
                        if controller_errors:
                            raise RuntimeError(
                                "Guardian host controller failed"
                            ) from controller_errors[0]
                        await asyncio.sleep(0.1)
                    completed = completed_report(
                        self._guardian_host_exchange_dir, requested
                    )
                    if completed is None and not requested:
                        latest = self._latest_report()
                        if latest is not None:
                            completed = CompletedReport(*latest)
                    if completed is None:
                        break
                    commit = completed.commit
                    report = completed.report
                    identifiers = completed.identifiers
                    completed_at_ns = completed.completed_at_ns
                    if not commit or commit in delivered_reviews:
                        break
                    delivered_reviews.add(commit)
                    if self._solver_observed_report(
                        turn_log,
                        report=report,
                        identifiers=identifiers,
                        completed_at_ns=completed_at_ns,
                    ):
                        break
                    next_instruction = f"{guardian_preamble}\n\n---\n\n" + (
                        self._revision_instruction(instruction, report)
                    )
            finally:
                stop.set()
                await asyncio.to_thread(controller_thread.join, 30)
                self._solver_turn_logs = list(turn_logs)
                self._restore_solver_logs(turn_logs)
                exported_memory = (
                    self._guardian_host_exchange_dir.parent / "guardian_memory"
                )
                shutil.copytree(memory_dir, exported_memory, dirs_exist_ok=True)
                self._log_codex_token_summary(turn_logs)
                if controller_thread.is_alive():
                    raise RuntimeError("Guardian host controller did not stop")
                if controller_errors:
                    raise RuntimeError("Guardian host controller failed") from (
                        controller_errors[0]
                    )
