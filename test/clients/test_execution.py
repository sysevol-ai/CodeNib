# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for external-agent execution adapters."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from codenib.clients.execution import (
    AgentEventKind,
    AgentExecutor,
    AgentRole,
    AgentRunErrorCode,
    AgentRunRequest,
    CodexExecutor,
    ExecutableNotFoundError,
    ExecutionPolicy,
    FilesystemAccess,
    LocalProcessRunner,
    NetworkAccess,
    ProcessRequest,
    ProcessResult,
    RunStatus,
)


class FakeProcessRunner:
    def __init__(self, result: ProcessResult) -> None:
        self.result = result
        self.requests: list[ProcessRequest] = []

    async def run(self, request: ProcessRequest) -> ProcessResult:
        self.requests.append(request)
        return self.result


def _process_result(
    stdout: str,
    *,
    exit_code: int = 0,
    timed_out: bool = False,
    stdout_truncated: bool = False,
) -> ProcessResult:
    return ProcessResult(
        argv=("codex",),
        exit_code=exit_code,
        stdout=stdout,
        stderr="diagnostic",
        duration_seconds=1.25,
        timed_out=timed_out,
        stdout_truncated=stdout_truncated,
    )


def _request(tmp_path: Path, **overrides: object) -> AgentRunRequest:
    values = {
        "instruction": "Discover local specifications.",
        "workspace": tmp_path,
        "role": AgentRole.EXPLORER,
        "timeout_seconds": 30,
        "model": "gpt-test",
        "reasoning_effort": "medium",
    }
    values.update(overrides)
    return AgentRunRequest(**values)  # type: ignore[arg-type]


def _jsonl(*events: dict[str, object]) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


def test_codex_executor_normalizes_command_events_and_usage(tmp_path: Path) -> None:
    output = _jsonl(
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {
                "id": "item-1",
                "type": "command_execution",
                "command": "rg feature",
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "item-2",
                "type": "agent_message",
                "text": "The feature must preserve aliases.",
            },
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 40,
                "cache_write_input_tokens": 5,
                "output_tokens": 20,
                "reasoning_output_tokens": 7,
            },
        },
    )
    runner = FakeProcessRunner(_process_result(output))
    executor = CodexExecutor(process_runner=runner)

    result = asyncio.run(executor.run(_request(tmp_path)))

    assert isinstance(executor, AgentExecutor)
    assert result.status is RunStatus.COMPLETED
    assert result.final_message == "The feature must preserve aliases."
    assert result.usage.input_tokens == 100
    assert result.usage.cached_input_tokens == 40
    assert result.usage.output_tokens == 20
    assert result.usage.total_tokens == 120
    assert [event.kind for event in result.trajectory] == [
        AgentEventKind.LIFECYCLE,
        AgentEventKind.LIFECYCLE,
        AgentEventKind.TOOL,
        AgentEventKind.MESSAGE,
        AgentEventKind.USAGE,
    ]

    launched = runner.requests[0]
    assert launched.stdin == b"Discover local specifications."
    assert launched.cwd == tmp_path.resolve()
    assert launched.argv[:3] == ("codex", "exec", "--json")
    assert ("--sandbox", "read-only") == (
        launched.argv[launched.argv.index("--sandbox")],
        launched.argv[launched.argv.index("--sandbox") + 1],
    )
    assert launched.argv[-1] == "-"
    assert "gpt-test" in launched.argv
    assert 'model_reasoning_effort="medium"' in launched.argv


def test_codex_executor_translates_workspace_write_policy(tmp_path: Path) -> None:
    output = _jsonl(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "done"},
        }
    )
    runner = FakeProcessRunner(_process_result(output))
    policy = ExecutionPolicy(filesystem=FilesystemAccess.WORKSPACE_WRITE)

    result = asyncio.run(
        CodexExecutor(process_runner=runner).run(
            _request(tmp_path, policy=policy, model=None, reasoning_effort=None)
        )
    )

    assert result.succeeded
    command = runner.requests[0].argv
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert "--model" not in command


def test_codex_executor_rejects_unenforceable_network_policy(tmp_path: Path) -> None:
    runner = FakeProcessRunner(_process_result(""))
    policy = ExecutionPolicy(network=NetworkAccess.DISABLED)

    result = asyncio.run(
        CodexExecutor(process_runner=runner).run(_request(tmp_path, policy=policy))
    )

    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.code is AgentRunErrorCode.UNSUPPORTED_POLICY
    assert runner.requests == []


@pytest.mark.parametrize(
    ("process", "expected_code", "expected_status"),
    [
        (
            _process_result("", exit_code=2),
            AgentRunErrorCode.CLI_EXIT,
            RunStatus.FAILED,
        ),
        (
            _process_result("", exit_code=-9, timed_out=True),
            AgentRunErrorCode.TIMEOUT,
            RunStatus.TIMED_OUT,
        ),
        (
            _process_result("not-json\n"),
            AgentRunErrorCode.MALFORMED_EVENT_STREAM,
            RunStatus.FAILED,
        ),
        (
            _process_result(_jsonl({"type": "turn.completed", "usage": {}})),
            AgentRunErrorCode.EMPTY_RESPONSE,
            RunStatus.FAILED,
        ),
    ],
)
def test_codex_executor_classifies_operational_failures(
    tmp_path: Path,
    process: ProcessResult,
    expected_code: AgentRunErrorCode,
    expected_status: RunStatus,
) -> None:
    result = asyncio.run(
        CodexExecutor(process_runner=FakeProcessRunner(process)).run(
            _request(tmp_path)
        )
    )

    assert result.status is expected_status
    assert result.error is not None
    assert result.error.code is expected_code


def test_local_process_runner_bounds_output(tmp_path: Path) -> None:
    request = ProcessRequest(
        argv=(sys.executable, "-c", "print('x' * 200)"),
        cwd=tmp_path,
        max_output_bytes=32,
        timeout_seconds=2,
    )

    result = asyncio.run(LocalProcessRunner().run(request))

    assert result.exit_code == 0
    assert len(result.stdout.encode()) == 32
    assert result.stdout_truncated


def test_local_process_runner_kills_timed_out_process(tmp_path: Path) -> None:
    request = ProcessRequest(
        argv=(sys.executable, "-c", "import time; time.sleep(10)"),
        cwd=tmp_path,
        timeout_seconds=0.05,
    )

    result = asyncio.run(LocalProcessRunner().run(request))

    assert result.timed_out
    assert result.exit_code is not None


def test_local_process_runner_reports_missing_executable(tmp_path: Path) -> None:
    request = ProcessRequest(
        argv=("definitely-not-a-codenib-executable",),
        cwd=tmp_path,
        timeout_seconds=1,
    )

    with pytest.raises(ExecutableNotFoundError):
        asyncio.run(LocalProcessRunner().run(request))
