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
from pathlib import Path
from typing import Callable

from codenib.clients.execution import CodexExecutor, SandboxProcessRunner
from codenib.clients.guardian import (
    ContextMessage,
    GuardianAgent,
    GuardianConfig,
    GuardianRequest,
    GuardianResult,
    ReviewStatus,
    render_markdown,
)
from codenib.clients.guardian.exchange import (
    ReviewExchangeRequest,
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
    model: str,
    cycle_index: int,
    max_cycles: int,
) -> None:
    terminal = result.status is not ReviewStatus.FAILED and cycle_index >= max_cycles
    _write_text_atomic(path / "findings.md", render_markdown(result))
    _write_json_atomic(path / "findings.json", result.to_dict())
    rollout_dir = path / "rollouts"
    rollout_dir.mkdir(parents=True, exist_ok=True)
    for index, rollout in enumerate(result.rollouts, 1):
        prefix = rollout_dir / f"rollout_{index:02d}"
        _write_text_atomic(prefix.with_suffix(".jsonl"), rollout.raw_output)
        _write_text_atomic(prefix.with_suffix(".stderr.txt"), rollout.stderr)
        _write_json_atomic(
            prefix.with_suffix(".metadata.json"),
            {
                "status": rollout.status.value,
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
            "backlog": len(result.backlog),
            "high_confidence_backlog": sum(
                finding.confidence >= 0.8 for finding in result.backlog
            ),
            "candidate_count": len(result.candidates),
            "degraded": result.status is not ReviewStatus.COMPLETE,
            "analysis_status": result.status.value,
            "exit_reason": "ReviewCompleted",
            "review_performed": True,
            "cycle_index": cycle_index,
            "max_cycles": max_cycles,
            "terminal": terminal,
            "termination_reason": "max_cycles_reached" if terminal else "",
            "llm_model": model,
            "llm_backend": "codex-cli+codenib-sandbox",
            "llm_transport_history": ["codex-cli+codenib-sandbox"],
            "llm_tokens": _tokens(result),
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
    model: str,
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
            "backlog": 0,
            "high_confidence_backlog": 0,
            "candidate_count": 0,
            "degraded": False,
            "analysis_status": "not_run",
            "exit_reason": "ReviewLimitReached",
            "review_performed": False,
            "cycle_index": completed_cycles,
            "max_cycles": max_cycles,
            "terminal": True,
            "termination_reason": "max_cycles_reached",
            "llm_model": model,
            "llm_backend": "codex-cli+codenib-sandbox",
            "llm_tokens": {"prompt": 0, "cached_input": 0, "completion": 0, "total": 0},
            "running": False,
            "error": "",
        },
    )


def _write_failure(
    path: Path,
    *,
    request_id: str,
    error: str,
    model: str,
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
            "backlog": 0,
            "high_confidence_backlog": 0,
            "candidate_count": 0,
            "degraded": True,
            "analysis_status": "failed",
            "exit_reason": "OperationalFailure",
            "review_performed": False,
            "cycle_index": cycle_index,
            "max_cycles": max_cycles,
            "terminal": False,
            "termination_reason": "",
            "llm_model": model,
            "llm_backend": "codex-cli+codenib-sandbox",
            "llm_tokens": {"prompt": 0, "cached_input": 0, "completion": 0, "total": 0},
            "running": False,
            "error": error,
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
            self._cycle_state_loaded = True

    @staticmethod
    def _is_completed_review(path: Path) -> bool:
        try:
            status = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return bool(
            isinstance(status, dict)
            and status.get("review_performed", True)
            and status.get("exit_reason") == "ReviewCompleted"
            and status.get("analysis_status") in {"complete", "degraded"}
        )

    async def process_manifest(self, manifest: Path) -> None:
        self._prepare()
        request_id = manifest.stem
        response = self.exchange_root / "responses" / request_id
        if (response / "status.json").exists():
            return
        response.mkdir(parents=True, exist_ok=True)
        try:
            request, bundle = load_exchange_request(self.exchange_root, manifest)
            if (
                self._expected_base_commit is not None
                and request.base_commit != self._expected_base_commit
            ):
                raise ValueError(
                    "request base does not match the controller-owned review head: "
                    f"expected {self._expected_base_commit}, got {request.base_commit}"
                )
            if self._completed_cycles >= self.max_cycles:
                _write_limit_response(
                    response,
                    request_id=request_id,
                    base_commit=request.base_commit,
                    model=self.config.aggregator_model,
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
                model=self.config.aggregator_model,
                cycle_index=self._completed_cycles + 1,
                max_cycles=self.max_cycles,
            )
        episode = self.episodes_root / request_id
        _copy_report(response, episode)
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
            result = await reviewer.review(
                GuardianRequest(
                    workspace=workspace,
                    base_commit=request.base_commit,
                    candidate_commit=request.candidate_commit,
                    context=_context(self.exchange_root, workspace),
                    change_patch=patch,
                )
            )
            _write_result(
                response,
                result,
                model=self.config.aggregator_model,
                cycle_index=cycle_index,
                max_cycles=self.max_cycles,
            )
            if result.status is not ReviewStatus.FAILED:
                self._expected_base_commit = request.candidate_commit
                self._completed_cycles = cycle_index

    async def process_pending(self) -> int:
        self._prepare()
        processed = 0
        for manifest in sorted((self.exchange_root / "requests").glob("*.json")):
            response = self.exchange_root / "responses" / manifest.stem / "status.json"
            if response.exists():
                continue
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
    staged = {".guardian-runtime/auth.json": auth_json} if auth_json is not None else {}

    def factory(_workspace: Path) -> GuardianAgent:
        runner = SandboxProcessRunner(
            provider=provider,
            image=image,
            platform=platform,
            policy=policy,
            staged_files=staged,
            environment={"CODEX_HOME": "/workspace/.guardian-runtime"},
        )
        return GuardianAgent(
            config,
            executor=CodexExecutor(
                executable=codex_executable,
                process_runner=runner,
                max_output_bytes=limits.output_bytes,
            ),
        )

    return factory


__all__ = [
    "GuardianHostController",
    "ReviewerFactory",
    "sandbox_reviewer_factory",
]
