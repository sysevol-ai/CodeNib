# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Codex CLI implementation of the external-agent execution protocol."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .process import (
    ExecutableNotFoundError,
    LocalProcessRunner,
    ProcessLaunchError,
    ProcessRequest,
    ProcessResult,
    ProcessRunner,
)
from .types import (
    AgentEvent,
    AgentEventKind,
    AgentRunError,
    AgentRunErrorCode,
    AgentRunRequest,
    AgentRunResult,
    ExecutionIsolation,
    FilesystemAccess,
    NetworkAccess,
    RunStatus,
    TokenUsage,
)


@dataclass(frozen=True, slots=True)
class _ParsedCodexStream:
    events: tuple[AgentEvent, ...]
    final_message: str
    usage: TokenUsage
    malformed_lines: tuple[str, ...]


def _event_kind(event_type: str, payload: Mapping[str, object]) -> AgentEventKind:
    if event_type in {"thread.started", "turn.started"}:
        return AgentEventKind.LIFECYCLE
    if event_type == "turn.completed":
        return AgentEventKind.USAGE
    if event_type == "error":
        return AgentEventKind.ERROR
    if event_type in {"item.started", "item.completed"}:
        item = payload.get("item")
        item_type = item.get("type") if isinstance(item, dict) else None
        if item_type == "agent_message":
            return AgentEventKind.MESSAGE
        return AgentEventKind.TOOL
    return AgentEventKind.UNKNOWN


def _token_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _parse_codex_stream(raw_output: str) -> _ParsedCodexStream:
    events: list[AgentEvent] = []
    malformed: list[str] = []
    final_message = ""
    usage = TokenUsage()

    for line in raw_output.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            malformed.append(line)
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
            malformed.append(line)
            continue

        event_type = payload["type"]
        events.append(
            AgentEvent(
                sequence=len(events),
                kind=_event_kind(event_type, payload),
                provider_type=event_type,
                payload=payload,
            )
        )
        if event_type == "item.completed":
            item = payload.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    final_message = text
        elif event_type == "turn.completed":
            raw_usage = payload.get("usage")
            if isinstance(raw_usage, dict):
                usage = TokenUsage(
                    input_tokens=_token_count(raw_usage.get("input_tokens")),
                    cached_input_tokens=_token_count(
                        raw_usage.get("cached_input_tokens")
                    ),
                    cache_write_input_tokens=_token_count(
                        raw_usage.get("cache_write_input_tokens")
                    ),
                    output_tokens=_token_count(raw_usage.get("output_tokens")),
                    reasoning_output_tokens=_token_count(
                        raw_usage.get("reasoning_output_tokens")
                    ),
                )

    return _ParsedCodexStream(
        events=tuple(events),
        final_message=final_message,
        usage=usage,
        malformed_lines=tuple(malformed),
    )


class CodexExecutor:
    """Invoke Codex CLI while leaving its native agent loop intact."""

    def __init__(
        self,
        *,
        executable: str = "codex",
        process_runner: ProcessRunner | None = None,
        max_output_bytes: int = 8 * 1024**2,
        enabled_features: Sequence[str] = (),
        disabled_features: Sequence[str] = (),
    ) -> None:
        if not executable or "\x00" in executable:
            raise ValueError("executable must be a non-empty, NUL-free string")
        if max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be positive")
        for name, features in (
            ("enabled_features", enabled_features),
            ("disabled_features", disabled_features),
        ):
            if isinstance(features, (str, bytes)) or any(
                not isinstance(feature, str) or not feature or "\x00" in feature
                for feature in features
            ):
                raise ValueError(f"{name} must contain non-empty, NUL-free strings")
        enabled = tuple(enabled_features)
        disabled = tuple(disabled_features)
        overlap = sorted(set(enabled) & set(disabled))
        if overlap:
            raise ValueError(
                "Codex features cannot be both enabled and disabled: "
                + ", ".join(overlap)
            )
        self._executable = executable
        self._process_runner = process_runner or LocalProcessRunner()
        self._max_output_bytes = max_output_bytes
        self._enabled_features = enabled
        self._disabled_features = disabled

    def _unsupported_policy(self, request: AgentRunRequest) -> AgentRunResult | None:
        if request.policy.network is NetworkAccess.DISABLED:
            return self._failure(
                code=AgentRunErrorCode.UNSUPPORTED_POLICY,
                message=(
                    "Codex native sandbox cannot guarantee disabled network access; "
                    "run it inside a codenib.sandbox environment instead"
                ),
            )
        return None

    def _command(self, request: AgentRunRequest) -> tuple[str, ...]:
        sandbox = {
            FilesystemAccess.READ_ONLY: "read-only",
            FilesystemAccess.WORKSPACE_WRITE: "workspace-write",
        }[request.policy.filesystem]
        command = [
            self._executable,
            "exec",
            "--json",
            "--ephemeral",
            "--color",
            "never",
            "--skip-git-repo-check",
            "--cd",
            str(request.workspace),
        ]
        for feature in self._enabled_features:
            command.extend(("--enable", feature))
        for feature in self._disabled_features:
            command.extend(("--disable", feature))
        if request.policy.isolation is ExecutionIsolation.EXTERNAL:
            command.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            command.extend(
                ("--sandbox", sandbox, "--config", 'approval_policy="never"')
            )
        if request.model:
            command.extend(("--model", request.model))
        if request.reasoning_effort:
            encoded = json.dumps(request.reasoning_effort)
            command.extend(("--config", f"model_reasoning_effort={encoded}"))
        command.append("-")
        return tuple(command)

    @staticmethod
    def _failure(
        *,
        code: AgentRunErrorCode,
        message: str,
        status: RunStatus = RunStatus.FAILED,
        duration_seconds: float = 0.0,
        process: ProcessResult | None = None,
        events: Sequence[AgentEvent] = (),
        usage: TokenUsage | None = None,
        final_message: str = "",
    ) -> AgentRunResult:
        return AgentRunResult(
            status=status,
            final_message=final_message,
            trajectory=events,
            usage=usage or TokenUsage(),
            duration_seconds=(
                process.duration_seconds if process is not None else duration_seconds
            ),
            raw_output=process.stdout if process is not None else "",
            stderr=process.stderr if process is not None else "",
            exit_code=process.exit_code if process is not None else None,
            error=AgentRunError(code=code, message=message),
        )

    async def run(self, request: AgentRunRequest) -> AgentRunResult:
        unsupported = self._unsupported_policy(request)
        if unsupported is not None:
            return unsupported

        process_request = ProcessRequest(
            argv=self._command(request),
            cwd=request.workspace,
            stdin=request.instruction.encode("utf-8"),
            environment=request.environment,
            timeout_seconds=request.timeout_seconds,
            max_output_bytes=self._max_output_bytes,
        )
        started = time.monotonic()
        try:
            process = await self._process_runner.run(process_request)
        except ExecutableNotFoundError as exc:
            return self._failure(
                code=AgentRunErrorCode.EXECUTABLE_NOT_FOUND,
                message=str(exc),
                duration_seconds=time.monotonic() - started,
            )
        except ProcessLaunchError as exc:
            return self._failure(
                code=AgentRunErrorCode.LAUNCH_FAILED,
                message=str(exc),
                duration_seconds=time.monotonic() - started,
            )
        except asyncio.CancelledError:
            return self._failure(
                code=AgentRunErrorCode.CANCELLED,
                message="Codex execution was cancelled",
                status=RunStatus.CANCELLED,
                duration_seconds=time.monotonic() - started,
            )

        parsed = _parse_codex_stream(process.stdout)
        common: dict[str, Any] = {
            "process": process,
            "events": parsed.events,
            "usage": parsed.usage,
            "final_message": parsed.final_message,
        }
        if process.timed_out:
            return self._failure(
                code=AgentRunErrorCode.TIMEOUT,
                message=f"Codex exceeded {request.timeout_seconds:g} seconds",
                status=RunStatus.TIMED_OUT,
                **common,
            )
        if process.exit_code != 0:
            return self._failure(
                code=AgentRunErrorCode.CLI_EXIT,
                message=f"Codex exited with status {process.exit_code}",
                **common,
            )
        if parsed.malformed_lines or process.stdout_truncated:
            detail = (
                f"{len(parsed.malformed_lines)} malformed JSONL line(s)"
                if parsed.malformed_lines
                else "JSONL output exceeded the configured size limit"
            )
            return self._failure(
                code=AgentRunErrorCode.MALFORMED_EVENT_STREAM,
                message=detail,
                **common,
            )
        if not parsed.final_message:
            return self._failure(
                code=AgentRunErrorCode.EMPTY_RESPONSE,
                message="Codex completed without an agent message",
                **common,
            )
        return AgentRunResult(
            status=RunStatus.COMPLETED,
            final_message=parsed.final_message,
            trajectory=parsed.events,
            usage=parsed.usage,
            duration_seconds=process.duration_seconds,
            raw_output=process.stdout,
            stderr=process.stderr,
            exit_code=process.exit_code,
        )


__all__ = ["CodexExecutor"]
