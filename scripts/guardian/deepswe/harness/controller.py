# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Host-side controller for commit-addressed DeepSWE Guardian reviews."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable

from codenib.clients.execution import CodexExecutor, SandboxProcessRunner
from codenib.clients.guardian import (
    ContextMessage,
    GuardianAgent,
    GuardianConfig,
    GuardianMemoryStore,
    GuardianRequest,
    GuardianResult,
    ReviewStatus,
    render_markdown,
)
from codenib.clients.guardian.exchange import (
    ExchangeProtocolError,
    ReviewExchangeRequest,
    is_request_id,
    load_exchange_request,
    materialize_exchange_request,
)
from codenib.clients.guardian.interaction import MessageInbox
from codenib.sandbox import (
    DockerSandboxProvider,
    NetworkMode,
    SandboxLimits,
    SandboxPolicy,
)

from .delivery import completed_responses, review_request_commits

ReviewerFactory = Callable[[Path], GuardianAgent]


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _write_json_atomic(path: Path, value: object) -> None:
    _write_text_atomic(path, json.dumps(value, indent=2, default=str) + "\n")


def _copy_report(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for path in source.iterdir():
        target = destination / path.name
        if path.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(path, target)
        else:
            shutil.copy2(path, target)


def _tokens(result: GuardianResult) -> dict[str, int]:
    return {
        "prompt": result.usage.input_tokens,
        "cached_input": result.usage.cached_input_tokens,
        "completion": result.usage.output_tokens,
        "total": result.usage.total_tokens,
    }


def _write_result(
    path: Path,
    result: GuardianResult,
    *,
    explorer_model: str,
    aggregator_model: str,
    explorer_count: int,
    cycle_index: int,
    max_cycles: int,
    memory_specifications: int,
    memory_snapshots: int,
) -> None:
    terminal = result.status is not ReviewStatus.FAILED and cycle_index >= max_cycles
    exit_reason = (
        "ReviewFailed" if result.status is ReviewStatus.FAILED else "ReviewCompleted"
    )
    _write_text_atomic(path / "findings.md", render_markdown(result))
    _write_json_atomic(path / "findings.json", result.to_dict())
    rollout_dir = path / "rollouts"
    rollout_dir.mkdir(parents=True, exist_ok=True)
    for index, rollout in enumerate(result.rollouts, 1):
        prefix = rollout_dir / f"rollout_{index:02d}"
        stage = (
            result.rollout_stages[index - 1]
            if index <= len(result.rollout_stages)
            else "unknown"
        )
        is_explorer = stage in {"exploration", "investigation"}
        _write_text_atomic(prefix.with_suffix(".jsonl"), rollout.raw_output)
        _write_text_atomic(prefix.with_suffix(".stderr.txt"), rollout.stderr)
        _write_json_atomic(
            prefix.with_suffix(".metadata.json"),
            {
                "status": rollout.status.value,
                "role": stage,
                "model": explorer_model if is_explorer else aggregator_model,
                "exit_code": rollout.exit_code,
                "duration_seconds": rollout.duration_seconds,
                "error": (
                    {
                        "code": rollout.error.code.value,
                        "message": rollout.error.message,
                    }
                    if rollout.error
                    else None
                ),
                "usage": {
                    "input_tokens": rollout.usage.input_tokens,
                    "cached_input_tokens": rollout.usage.cached_input_tokens,
                    "output_tokens": rollout.usage.output_tokens,
                    "reasoning_output_tokens": (rollout.usage.reasoning_output_tokens),
                },
            },
        )
    _write_json_atomic(
        path / "status.json",
        {
            "commit": result.candidate_commit,
            "base_commit": result.base_commit,
            "findings": len(result.findings),
            "uncertain_specifications": len(result.uncertain_specifications),
            "candidate_count": len(result.candidates),
            "degraded": result.status is not ReviewStatus.COMPLETE,
            "analysis_status": result.status.value,
            "exit_reason": exit_reason,
            "review_performed": True,
            "cycle_index": cycle_index,
            "max_cycles": max_cycles,
            "terminal": terminal,
            "termination_reason": "max_cycles_reached" if terminal else "",
            "llm_model": aggregator_model,
            "explorer_model": explorer_model,
            "aggregator_model": aggregator_model,
            "llm_backend": "codex-cli+codenib-sandbox",
            "llm_transport_history": ["codex-cli+codenib-sandbox"],
            "llm_tokens": _tokens(result),
            "analysis_warnings": list(result.errors),
            "probe_metrics": result.probe_metrics.to_dict(),
            "memory_specifications": memory_specifications,
            "memory_snapshots": memory_snapshots,
            "running": False,
            "error": (
                ""
                if result.status is not ReviewStatus.FAILED
                else "; ".join(result.errors)
            ),
        },
    )


def _write_limit_response(
    path: Path,
    *,
    request_id: str,
    base_commit: str,
    explorer_model: str,
    aggregator_model: str,
    completed_cycles: int,
    max_cycles: int,
) -> None:
    _write_text_atomic(
        path / "findings.md",
        "# Repository Guardian Review Limit\n\n"
        f"Candidate `{request_id}` was not reviewed because Guardian already "
        f"completed its configured {max_cycles} review cycles. Any findings "
        "from the terminal reviewed commit remain unresolved unless the solver "
        "verified them independently.\n",
    )
    _write_json_atomic(
        path / "status.json",
        {
            "commit": request_id,
            "base_commit": base_commit,
            "findings": 0,
            "uncertain_specifications": 0,
            "candidate_count": 0,
            "degraded": False,
            "analysis_status": "not_run",
            "exit_reason": "ReviewLimitReached",
            "review_performed": False,
            "cycle_index": completed_cycles,
            "max_cycles": max_cycles,
            "terminal": True,
            "termination_reason": "max_cycles_reached",
            "llm_model": aggregator_model,
            "explorer_model": explorer_model,
            "aggregator_model": aggregator_model,
            "llm_backend": "codex-cli+codenib-sandbox",
            "llm_tokens": {"prompt": 0, "cached_input": 0, "completion": 0, "total": 0},
            "analysis_warnings": [],
            "running": False,
            "error": "",
        },
    )


def _write_superseded_response(
    path: Path,
    *,
    request_id: str,
    base_commit: str,
    expected_base_commit: str,
    explorer_model: str,
    aggregator_model: str,
    completed_cycles: int,
    max_cycles: int,
) -> None:
    _write_text_atomic(
        path / "findings.md",
        "# Repository Guardian Superseded Review\n\n"
        f"Candidate `{request_id}` was not reviewed because its base "
        f"`{base_commit}` is no longer Guardian's current review head "
        f"`{expected_base_commit}`.\n",
    )
    _write_json_atomic(
        path / "status.json",
        {
            "commit": request_id,
            "base_commit": base_commit,
            "expected_base_commit": expected_base_commit,
            "findings": 0,
            "uncertain_specifications": 0,
            "candidate_count": 0,
            "degraded": False,
            "analysis_status": "not_run",
            "exit_reason": "ReviewSuperseded",
            "review_performed": False,
            "cycle_index": completed_cycles,
            "max_cycles": max_cycles,
            "terminal": False,
            "termination_reason": "stale_base_commit",
            "llm_model": aggregator_model,
            "explorer_model": explorer_model,
            "aggregator_model": aggregator_model,
            "llm_backend": "codex-cli+codenib-sandbox",
            "llm_tokens": {
                "prompt": 0,
                "cached_input": 0,
                "completion": 0,
                "total": 0,
            },
            "analysis_warnings": [],
            "running": False,
            "error": "",
        },
    )


def _write_failure(
    path: Path,
    *,
    request_id: str,
    error: str,
    explorer_model: str,
    aggregator_model: str,
    cycle_index: int,
    max_cycles: int,
) -> None:
    _write_text_atomic(
        path / "findings.md",
        "# Repository Guardian Report\n\n"
        f"Guardian review failed for `{request_id}`: {error}\n",
    )
    _write_json_atomic(
        path / "status.json",
        {
            "commit": request_id,
            "findings": 0,
            "uncertain_specifications": 0,
            "candidate_count": 0,
            "degraded": True,
            "analysis_status": "failed",
            "exit_reason": "OperationalFailure",
            "review_performed": False,
            "cycle_index": cycle_index,
            "max_cycles": max_cycles,
            "terminal": False,
            "termination_reason": "",
            "llm_model": aggregator_model,
            "explorer_model": explorer_model,
            "aggregator_model": aggregator_model,
            "llm_backend": "codex-cli+codenib-sandbox",
            "llm_tokens": {"prompt": 0, "cached_input": 0, "completion": 0, "total": 0},
            "analysis_warnings": [],
            "running": False,
            "error": error,
        },
    )


def _write_running(
    path: Path,
    *,
    request_id: str,
    base_commit: str,
    explorer_model: str,
    aggregator_model: str,
    cycle_index: int,
    max_cycles: int,
) -> None:
    """Publish observable progress before long model rollouts begin."""

    _write_json_atomic(
        path / "status.json",
        {
            "commit": request_id,
            "base_commit": base_commit,
            "findings": 0,
            "uncertain_specifications": 0,
            "candidate_count": 0,
            "degraded": False,
            "analysis_status": "running",
            "exit_reason": "",
            "review_performed": False,
            "cycle_index": cycle_index,
            "max_cycles": max_cycles,
            "terminal": False,
            "termination_reason": "",
            "llm_model": aggregator_model,
            "explorer_model": explorer_model,
            "aggregator_model": aggregator_model,
            "llm_backend": "codex-cli+codenib-sandbox",
            "llm_tokens": {"prompt": 0, "cached_input": 0, "completion": 0, "total": 0},
            "analysis_warnings": [],
            "running": True,
            "error": "",
        },
    )


def _git_patch(workspace: Path, base: str, candidate: str) -> str:
    return subprocess.run(
        ("git", "diff", "--no-ext-diff", "--binary", f"{base}..{candidate}"),
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        env={
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
        },
    ).stdout


def _copy_bundle_to_controller_storage(
    bundle: Path, destination: Path, expected_sha256: str
) -> Path:
    """Copy a shared bundle and verify the controller-owned copy."""

    digest = hashlib.sha256()
    with bundle.open("rb") as source, destination.open("xb") as target:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            target.write(block)
            digest.update(block)
    if digest.hexdigest() != expected_sha256:
        destination.unlink(missing_ok=True)
        raise ValueError("bundle changed while the controller copied it")
    return destination


def _context(exchange_root: Path, workspace: Path) -> tuple[ContextMessage, ...]:
    inbox = MessageInbox(exchange_root / "messages.jsonl", repo_path=workspace)
    return tuple(
        ContextMessage(
            content=message.content,
            sender=message.sender,
            scope=tuple(message.scope),
        )
        for message in inbox.read_recent(limit=20)
    )


class GuardianHostController:
    """Consume solver checkpoints and publish reports without sharing a workspace."""

    def __init__(
        self,
        *,
        exchange_root: Path,
        episodes_root: Path,
        config: GuardianConfig,
        reviewer_factory: ReviewerFactory,
        initial_base_commit: str | None = None,
        memory_root: Path | None = None,
        task_description: str = "",
        max_cycles: int = 3,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        if isinstance(max_cycles, bool) or not isinstance(max_cycles, int):
            raise TypeError("max_cycles must be an integer")
        if max_cycles < 1:
            raise ValueError("max_cycles must be positive")
        self.exchange_root = exchange_root
        self.episodes_root = episodes_root
        self.config = config
        self.reviewer_factory = reviewer_factory
        if not isinstance(task_description, str):
            raise TypeError("task_description must be a string")
        self.task_description = task_description.strip()
        self.memory_store = GuardianMemoryStore(
            memory_root or self.episodes_root.parent / "guardian_state"
        )
        self._expected_base_commit = initial_base_commit
        self.max_cycles = max_cycles
        self._completed_cycles = 0
        self._cycle_state_loaded = False
        self.poll_interval_seconds = poll_interval_seconds

    def _prepare(self) -> None:
        for name in ("requests", "bundles", "responses", "latest"):
            (self.exchange_root / name).mkdir(parents=True, exist_ok=True)
        self.episodes_root.mkdir(parents=True, exist_ok=True)
        if not self._cycle_state_loaded:
            self._completed_cycles = sum(
                1
                for path in self.episodes_root.glob("*/status.json")
                if self._is_completed_review(path)
            )
            try:
                reviewed_commit = (
                    self.exchange_root.joinpath("last_reviewed_commit")
                    .read_text(encoding="utf-8")
                    .strip()
                )
            except OSError:
                reviewed_commit = ""
            reviewed_status = (
                self.episodes_root / reviewed_commit / "status.json"
                if reviewed_commit
                else None
            )
            if reviewed_status is not None and self._is_completed_review(
                reviewed_status
            ):
                self._expected_base_commit = reviewed_commit
            self._cycle_state_loaded = True

    @staticmethod
    def _is_completed_review(path: Path) -> bool:
        try:
            status = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return bool(
            isinstance(status, dict)
            and status.get("commit") == path.parent.name
            and status.get("review_performed", True)
            and status.get("exit_reason") == "ReviewCompleted"
            and status.get("analysis_status") in {"complete", "degraded"}
        )

    async def process_manifest(self, manifest: Path) -> None:
        request_id = manifest.stem
        if manifest.name != f"{request_id}.json" or not is_request_id(request_id):
            raise ExchangeProtocolError(
                "manifest filename must be a full lowercase Git SHA"
            )
        self._prepare()
        response = self.exchange_root / "responses" / request_id
        if (response / "status.json").exists():
            return
        response.mkdir(parents=True, exist_ok=True)
        publish_latest = True
        try:
            request, bundle = load_exchange_request(self.exchange_root, manifest)
            if (
                self._expected_base_commit is not None
                and request.base_commit != self._expected_base_commit
            ):
                _write_superseded_response(
                    response,
                    request_id=request_id,
                    base_commit=request.base_commit,
                    expected_base_commit=self._expected_base_commit,
                    explorer_model=self.config.explorer_model,
                    aggregator_model=self.config.aggregator_model,
                    completed_cycles=self._completed_cycles,
                    max_cycles=self.max_cycles,
                )
                publish_latest = False
            elif self._completed_cycles >= self.max_cycles:
                _write_limit_response(
                    response,
                    request_id=request_id,
                    base_commit=request.base_commit,
                    explorer_model=self.config.explorer_model,
                    aggregator_model=self.config.aggregator_model,
                    completed_cycles=self._completed_cycles,
                    max_cycles=self.max_cycles,
                )
            else:
                await self._review_request(
                    request=request,
                    bundle=bundle,
                    response=response,
                )
        except Exception as exc:  # noqa: BLE001
            _write_failure(
                response,
                request_id=request_id,
                error=f"{type(exc).__name__}: {exc}",
                explorer_model=self.config.explorer_model,
                aggregator_model=self.config.aggregator_model,
                cycle_index=self._completed_cycles + 1,
                max_cycles=self.max_cycles,
            )
        episode = self.episodes_root / request_id
        _copy_report(response, episode)
        if publish_latest:
            latest = self.exchange_root / "latest"
            latest.mkdir(parents=True, exist_ok=True)
            for stale in latest.iterdir():
                if stale.is_dir():
                    shutil.rmtree(stale)
                else:
                    stale.unlink()
            _copy_report(response, latest)

    async def _review_request(
        self,
        *,
        request: ReviewExchangeRequest,
        bundle: Path,
        response: Path,
    ) -> None:
        cycle_index = self._completed_cycles + 1
        _write_running(
            response,
            request_id=request.request_id,
            base_commit=request.base_commit,
            explorer_model=self.config.explorer_model,
            aggregator_model=self.config.aggregator_model,
            cycle_index=cycle_index,
            max_cycles=self.max_cycles,
        )
        with tempfile.TemporaryDirectory(prefix="guardian-review-") as temporary:
            trusted_bundle = _copy_bundle_to_controller_storage(
                bundle,
                Path(temporary) / request.bundle_name,
                request.bundle_sha256,
            )
            workspace = materialize_exchange_request(
                request, trusted_bundle, Path(temporary) / "repository"
            )
            patch = _git_patch(workspace, request.base_commit, request.candidate_commit)
            reviewer = self.reviewer_factory(workspace)
            memory = self.memory_store.load(workspace)
            result = await reviewer.review(
                GuardianRequest(
                    workspace=workspace,
                    base_commit=request.base_commit,
                    candidate_commit=request.candidate_commit,
                    task_description=self.task_description,
                    context=_context(self.exchange_root, workspace),
                    change_patch=patch,
                    memory=memory,
                    previous_session_id=(
                        memory.session_id if memory.observed_snapshots else ""
                    ),
                    artifact_dir=response,
                )
            )
            updated_memory = memory
            if result.status is not ReviewStatus.FAILED:
                updated_memory = self.memory_store.update(result, workspace)
            _write_result(
                response,
                result,
                explorer_model=self.config.explorer_model,
                aggregator_model=self.config.aggregator_model,
                explorer_count=self.config.explorer_count,
                cycle_index=cycle_index,
                max_cycles=self.max_cycles,
                memory_specifications=len(updated_memory.specifications),
                memory_snapshots=len(updated_memory.observed_snapshots),
            )
            if result.status is not ReviewStatus.FAILED:
                _write_text_atomic(
                    self.exchange_root / "last_reviewed_commit",
                    request.candidate_commit + "\n",
                )
                self._expected_base_commit = request.candidate_commit
                self._completed_cycles = cycle_index

    async def process_pending(self) -> int:
        self._prepare()
        processed = 0
        while True:
            pending = [
                manifest
                for manifest in (self.exchange_root / "requests").glob("*.json")
                if is_request_id(manifest.stem)
                if not (
                    self.exchange_root / "responses" / manifest.stem / "status.json"
                ).exists()
            ]
            if not pending:
                break

            ordered = sorted(
                pending,
                key=lambda path: (path.lstat().st_mtime_ns, path.name),
            )
            eligible: list[Path] = []
            malformed: list[Path] = []
            for manifest in ordered:
                try:
                    if manifest.is_symlink():
                        raise ValueError("request manifest must not be a symlink")
                    request = ReviewExchangeRequest.from_dict(
                        json.loads(manifest.read_text(encoding="utf-8"))
                    )
                except (OSError, json.JSONDecodeError, ValueError):
                    malformed.append(manifest)
                    continue
                if (
                    self._expected_base_commit is None
                    or request.base_commit == self._expected_base_commit
                ):
                    eligible.append(manifest)

            # A malformed request must not block later valid work. Otherwise,
            # follow the commit chain from the controller-owned review head.
            # Requests left behind after the chain advances are handled as
            # superseded rather than operational failures.
            manifest = (
                eligible[0] if eligible else malformed[0] if malformed else ordered[0]
            )
            await self.process_manifest(manifest)
            processed += 1
        return processed

    async def serve(self, stop: asyncio.Event) -> None:
        self._prepare()
        while not stop.is_set():
            await self.process_pending()
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=max(0.1, self.poll_interval_seconds)
                )
            except asyncio.TimeoutError:
                pass
        await self.process_pending()

    def serve_blocking(self, stop: threading.Event) -> None:
        """Run the controller on a dedicated event-loop thread.

        Some installed agent harnesses perform blocking subprocess I/O inside
        their async ``run`` method.  A sibling asyncio task would then starve
        exactly while the solver waits for a checkpoint response.  The native
        thread keeps review processing independent of the solver's event loop.
        """

        async def serve_thread() -> None:
            self._prepare()
            while not stop.is_set():
                await self.process_pending()
                await asyncio.sleep(max(0.1, self.poll_interval_seconds))
            await self.process_pending()

        asyncio.run(serve_thread())

    def is_idle(self) -> bool:
        """Return whether every published checkpoint has a terminal response."""

        self._prepare()
        commits = review_request_commits(self.exchange_root)
        return completed_responses(self.exchange_root, commits) == commits

    async def wait_until_idle(self) -> None:
        """Wait until every published checkpoint has a terminal response.

        ``serve`` performs the actual work.  This method gives the harness a
        synchronization point after a solver turn, including when the solver's
        CLI returned while its checkpoint shell process was still waiting.
        """

        self._prepare()
        while True:
            if self.is_idle():
                return
            await asyncio.sleep(min(0.25, max(0.05, self.poll_interval_seconds)))


def sandbox_reviewer_factory(
    *,
    config: GuardianConfig,
    image: str,
    codex_executable: str,
    auth_json: bytes | None,
    docker_host: str = "unix:///var/run/docker.sock",
    platform: str = "linux/amd64",
) -> ReviewerFactory:
    """Build reviewers whose every rollout receives a fresh sibling sandbox."""

    provider = DockerSandboxProvider(
        allowed_images={image},
        docker_host=docker_host,
        work_root=None,
        retain_audit_logs=False,
    )
    limits = SandboxLimits(
        cpus=2.0,
        memory_bytes=4 * 1024**3,
        pids=512,
        command_timeout_seconds=config.rollout_timeout_seconds,
        output_bytes=8 * 1024**2,
        audit_log_bytes=16 * 1024**2,
        stdin_bytes=4 * 1024**2,
        tmpfs_bytes=512 * 1024**2,
    )
    policy = SandboxPolicy(
        network=NetworkMode.BRIDGE,
        require_rootless_runtime=False,
        allow_unpinned_image=True,
        limits=limits,
    )
    # Codex validates that CODEX_HOME exists before it considers brokered
    # credentials.  Always materialize the directory, including deployments
    # where no auth.json needs to be copied into the review sandbox.
    staged = {".guardian-runtime/.keep": b""}
    if auth_json is not None:
        staged[".guardian-runtime/auth.json"] = auth_json

    def factory(_workspace: Path) -> GuardianAgent:
        runner = SandboxProcessRunner(
            provider=provider,
            image=image,
            platform=platform,
            policy=policy,
            staged_files=staged,
            environment={
                "CODEX_HOME": "/workspace/.guardian-runtime",
                "PYTHONPATH": "/workspace",
            },
        )
        return GuardianAgent(
            config,
            executor=CodexExecutor(
                executable=codex_executable,
                process_runner=runner,
                max_output_bytes=limits.output_bytes,
                enabled_features=("unified_exec",),
                disabled_features=("code_mode", "code_mode_host"),
            ),
        )

    return factory


__all__ = [
    "GuardianHostController",
    "ReviewerFactory",
    "sandbox_reviewer_factory",
]
