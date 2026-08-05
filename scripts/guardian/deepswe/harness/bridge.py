# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Filesystem bridge from a Pier coding agent to the Guardian client policy."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from codenib.clients.execution import CodexExecutor, ExecutionIsolation
from codenib.clients.guardian import (
    ContextMessage,
    GuardianAgent,
    GuardianConfig,
    GuardianRequest,
    GuardianResult,
    ReviewStatus,
    render_markdown,
)
from codenib.clients.guardian.interaction import MessageInbox


def _interpreter_diagnostics() -> dict[str, str]:
    task_virtualenv = os.environ.get("VIRTUAL_ENV", "")
    task_interpreter = os.environ.get("GUARDIAN_TASK_PYTHON", "")
    if not task_interpreter and task_virtualenv:
        task_interpreter = str(Path(task_virtualenv) / "bin" / "python")
    return {
        "guardian_interpreter": sys.executable,
        "guardian_runtime_python": os.environ.get(
            "GUARDIAN_RUNTIME_PYTHON", sys.executable
        ),
        "task_virtualenv": task_virtualenv,
        "task_interpreter": task_interpreter,
        "path_interpreter": shutil.which("python") or "",
    }


def _head(repo_path: str) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _write_json_atomic(path: Path, data: object) -> None:
    _write_text_atomic(path, json.dumps(data, indent=2, default=str) + "\n")


def _tokens(result: GuardianResult) -> dict[str, int]:
    usage = result.usage
    return {
        "prompt": usage.input_tokens,
        "cached_input": usage.cached_input_tokens,
        "completion": usage.output_tokens,
        "total": usage.total_tokens,
    }


def _write_rollouts(out_dir: Path, result: GuardianResult) -> None:
    rollouts = out_dir / "rollouts"
    rollouts.mkdir(parents=True, exist_ok=True)
    for index, rollout in enumerate(result.rollouts, 1):
        prefix = rollouts / f"rollout_{index:02d}"
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


def _write_report(
    out_dir: Path,
    result: GuardianResult,
    *,
    model: str,
) -> None:
    _write_text_atomic(out_dir / "findings.md", render_markdown(result))
    _write_json_atomic(out_dir / "findings.json", result.to_dict())
    _write_rollouts(out_dir, result)
    _write_json_atomic(
        out_dir / "status.json",
        {
            "commit": result.candidate_commit,
            "base_commit": result.base_commit,
            "findings": len(result.findings),
            "backlog": len(result.backlog),
            "high_confidence_backlog": sum(
                item.confidence >= 0.8 for item in result.backlog
            ),
            "candidate_count": len(result.candidates),
            "degraded": result.status is not ReviewStatus.COMPLETE,
            "analysis_status": result.status.value,
            "exit_reason": "ReviewCompleted",
            "llm_model": model,
            "llm_backend": "codex-cli",
            "llm_transport_history": ["codex-cli"],
            "llm_tokens": _tokens(result),
            "interpreters": _interpreter_diagnostics(),
            "running": False,
            "error": (
                ""
                if result.status is not ReviewStatus.FAILED
                else "; ".join(result.errors)
            ),
        },
    )


def _write_status(
    out_dir: Path,
    *,
    commit: str,
    running: bool,
    error: str = "",
    llm_model: str = "",
    llm_backend: str = "",
) -> None:
    findings_path = out_dir / "findings.md"
    if not findings_path.exists():
        state = "running" if running else "not running"
        _write_text_atomic(
            findings_path,
            "# Repository Guardian Report\n\n"
            f"Guardian analysis is {state} for commit `{commit[:12]}`.\n"
            "No completed findings are available yet.\n",
        )
    _write_json_atomic(
        out_dir / "status.json",
        {
            "commit": commit,
            "findings": 0,
            "backlog": 0,
            "high_confidence_backlog": 0,
            "candidate_count": 0,
            "degraded": bool(error),
            "analysis_status": (
                "failed" if error else ("running" if running else "pending")
            ),
            "exit_reason": "",
            "llm_model": llm_model,
            "llm_backend": llm_backend,
            "llm_transport_history": [llm_backend] if llm_backend else [],
            "llm_tokens": {"prompt": 0, "cached_input": 0, "completion": 0, "total": 0},
            "interpreters": _interpreter_diagnostics(),
            "running": running,
            "error": error,
        },
    )


def _context(inbox: MessageInbox) -> tuple[ContextMessage, ...]:
    return tuple(
        ContextMessage(
            content=message.content,
            sender=message.sender,
            scope=tuple(message.scope),
        )
        for message in inbox.read_recent(limit=20)
    )


def run_bridge(
    config: GuardianConfig,
    *,
    repo_path: str,
    message_inbox: str,
    out_dir: str,
    poll_interval: int,
    executor: CodexExecutor | None = None,
    once: bool = False,
    baseline_commit: Optional[str] = None,
) -> None:
    """Review each commit after the setup-time baseline."""

    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    if baseline_commit is None:
        baseline_commit = _head(repo_path)
    last_commit = baseline_commit
    cycle_index = 0
    inbox = MessageInbox(message_inbox, repo_path=repo_path)
    reviewer = GuardianAgent(
        config,
        executor=executor
        or CodexExecutor(executable=os.environ.get("CODENIB_CODEX_BIN") or "codex"),
    )
    _write_status(
        output,
        commit=baseline_commit,
        running=False,
        llm_model=config.aggregator_model,
        llm_backend="not_started",
    )

    while True:
        commit = _head(repo_path)
        if commit and commit != last_commit:
            cycle_index += 1
            episode_root = Path(
                os.environ.get("GUARDIAN_EPISODES_DIR", "/logs/agent/guardian_episodes")
            )
            try:
                episode_root.mkdir(parents=True, exist_ok=True)
            except OSError:
                episode_root = output / "episodes"
                episode_root.mkdir(parents=True, exist_ok=True)
            episode_dir = episode_root / f"{cycle_index:04d}_{commit[:12]}"
            _write_status(
                output,
                commit=commit,
                running=True,
                llm_model=config.aggregator_model,
                llm_backend="pending",
            )
            print(
                f"guardian-codex-bridge: reviewing {commit[:8]}",
                file=sys.stderr,
                flush=True,
            )
            try:
                result = asyncio.run(
                    reviewer.review(
                        GuardianRequest(
                            workspace=Path(repo_path),
                            base_commit=last_commit,
                            candidate_commit=commit,
                            context=_context(inbox),
                        )
                    )
                )
                _write_report(output, result, model=config.aggregator_model)
                _write_report(episode_dir, result, model=config.aggregator_model)
                last_commit = commit
                print(
                    "guardian-codex-bridge: admitted "
                    f"{len(result.findings)} finding(s) for {commit[:8]}",
                    file=sys.stderr,
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001
                _write_status(
                    output,
                    commit=commit,
                    running=False,
                    error=str(exc),
                    llm_model=config.aggregator_model,
                    llm_backend="error",
                )
                print(
                    f"guardian-codex-bridge: review failed for {commit[:8]}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )

        if once:
            return
        time.sleep(max(1, poll_interval))


def _model(value: str) -> str:
    return value.removeprefix("codex:")


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--out-dir", default="~/.guardian")
    parser.add_argument("--message-inbox", default="~/.guardian/inbox/messages.jsonl")
    parser.add_argument("--explorer-model", default="codex:gpt-5.6-luna")
    parser.add_argument("--aggregator-model", default=None)
    parser.add_argument("--explorer-count", type=int, default=2)
    parser.add_argument("--max-findings", type=int, default=5)
    parser.add_argument("--rollout-timeout", type=float, default=600)
    parser.add_argument("--poll-interval", type=int, default=10)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--baseline-commit", default=None)
    parser.add_argument("--baseline-file", default=None)
    args = parser.parse_args(argv)

    baseline_commit = args.baseline_commit
    if args.baseline_file:
        try:
            baseline_commit = (
                Path(os.path.expanduser(args.baseline_file))
                .read_text(encoding="utf-8")
                .strip()
            )
        except OSError as exc:
            parser.error(f"cannot read --baseline-file: {exc}")
    explorer_model = _model(args.explorer_model)
    aggregator_model = _model(args.aggregator_model or args.explorer_model)
    config = GuardianConfig(
        explorer_model=explorer_model,
        aggregator_model=aggregator_model,
        explorer_count=args.explorer_count,
        rollout_timeout_seconds=args.rollout_timeout,
        max_findings=args.max_findings,
        execution_isolation=ExecutionIsolation.EXTERNAL,
    )
    run_bridge(
        config,
        repo_path=args.repo,
        message_inbox=os.path.expanduser(args.message_inbox),
        out_dir=os.path.expanduser(args.out_dir),
        poll_interval=args.poll_interval,
        once=args.once,
        baseline_commit=baseline_commit,
    )


if __name__ == "__main__":
    main()
