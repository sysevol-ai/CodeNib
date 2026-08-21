# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for external-agent execution adapters."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

from codenib.clients.execution import (
    AgentEventKind,
    AgentExecutor,
    AgentRole,
    AgentRunErrorCode,
    AgentRunRequest,
    CodexExecutor,
    ExecutableNotFoundError,
    ExecutionIsolation,
    ExecutionPolicy,
    FilesystemAccess,
    LocalProcessRunner,
    NetworkAccess,
    ProcessRequest,
    ProcessResult,
    RunStatus,
    SandboxProcessRunner,
)
from codenib.repository_source_selection import RepositorySourceSelection
from codenib.sandbox import (
    ExecResult,
    NetworkMode,
    SandboxCapabilities,
    SandboxMetadata,
    SandboxPolicy,
)


class FakeProcessRunner:
    def __init__(self, result: ProcessResult) -> None:
        self.result = result
        self.requests: list[ProcessRequest] = []

    async def run(self, request: ProcessRequest) -> ProcessResult:
        self.requests.append(request)
        return self.result


class FakeSandboxSession:
    def __init__(self) -> None:
        self.requests = []
        self.writes = []
        self.closed = False
        self.sandbox_id = "sbx-test"
        self.capabilities = SandboxCapabilities(
            provider="fake",
            isolation="test",
            non_root_user=True,
            network_isolation=False,
            read_only_rootfs=True,
            resource_limits=True,
            process_tree_cleanup=True,
            disk_quota=False,
        )
        self.metadata = SandboxMetadata(
            sandbox_id="sbx-test",
            task_id="guardian-test",
            provider="fake",
            image="test",
            image_id="test",
            platform="linux/amd64",
            source_revision=None,
            network="bridge",
            rootless_runtime=False,
        )

    def write_file(self, path, data, *, create_parents=False):
        self.writes.append((path, data, create_parents))

    def execute(self, request):
        self.requests.append(request)
        return ExecResult(
            command_id="command",
            argv=tuple(request.argv),
            exit_code=0,
            stdout="sandbox output",
            stderr="",
            duration_ms=1250,
        )

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


class FakeSandboxProvider:
    def __init__(self) -> None:
        self.specs = []
        self.sessions = []

    @property
    def capabilities(self):
        return FakeSandboxSession().capabilities

    def create(self, spec):
        self.specs.append(spec)
        session = FakeSandboxSession()
        self.sessions.append(session)
        return session


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


def test_codex_executor_applies_explicit_feature_policy(tmp_path: Path) -> None:
    output = _jsonl(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "done"},
        }
    )
    runner = FakeProcessRunner(_process_result(output))
    executor = CodexExecutor(
        process_runner=runner,
        enabled_features=("unified_exec",),
        disabled_features=("code_mode", "code_mode_host"),
    )

    result = asyncio.run(executor.run(_request(tmp_path)))

    assert result.succeeded
    command = runner.requests[0].argv
    assert command.count("--enable") == 1
    assert command[command.index("--enable") + 1] == "unified_exec"
    assert command.count("--disable") == 2
    disabled = {
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--disable"
    }
    assert disabled == {"code_mode", "code_mode_host"}


def test_codex_executor_rejects_conflicting_feature_policy() -> None:
    with pytest.raises(ValueError, match="both enabled and disabled"):
        CodexExecutor(
            enabled_features=("unified_exec",),
            disabled_features=("unified_exec",),
        )


def test_codex_executor_delegates_sandboxing_only_when_explicit(tmp_path: Path) -> None:
    output = _jsonl(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "done"},
        }
    )
    runner = FakeProcessRunner(_process_result(output))
    policy = ExecutionPolicy(isolation=ExecutionIsolation.EXTERNAL)

    result = asyncio.run(
        CodexExecutor(process_runner=runner).run(_request(tmp_path, policy=policy))
    )

    assert result.succeeded
    command = runner.requests[0].argv
    assert "--dangerously-bypass-approvals-and-sandbox" in command
    assert "--sandbox" not in command


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
        CodexExecutor(process_runner=FakeProcessRunner(process)).run(_request(tmp_path))
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


def test_sandbox_process_runner_copies_source_and_rewrites_workspace(
    tmp_path: Path,
) -> None:
    subprocess.run(("git", "init", "--quiet"), cwd=tmp_path, check=True)
    subprocess.run(
        ("git", "config", "user.email", "sandbox@example.com"),
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(("git", "config", "user.name", "Sandbox"), cwd=tmp_path, check=True)
    (tmp_path / "file.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(("git", "add", "file.py"), cwd=tmp_path, check=True)
    subprocess.run(
        ("git", "commit", "--quiet", "-m", "source"), cwd=tmp_path, check=True
    )
    provider = FakeSandboxProvider()
    source_selection = RepositorySourceSelection(("generated",))
    runner = SandboxProcessRunner(
        provider=provider,
        image="guardian:test",
        platform="linux/amd64",
        policy=SandboxPolicy(
            network=NetworkMode.BRIDGE,
            require_rootless_runtime=False,
            allow_unpinned_image=True,
        ),
        staged_files={".guardian-runtime/auth.json": b"secret"},
        source_selection=source_selection,
    )
    request = ProcessRequest(
        argv=("codex", "exec", "--cd", str(tmp_path), "-"),
        cwd=tmp_path,
        stdin=b"review",
        environment={"CODEX_HOME": "/workspace/.guardian-runtime"},
        timeout_seconds=10,
    )

    result = asyncio.run(runner.run(request))

    assert result.stdout == "sandbox output"
    assert result.duration_seconds == 1.25
    assert provider.specs[0].source_dir == tmp_path.resolve()
    assert provider.specs[0].source_selection == source_selection
    session = provider.sessions[0]
    assert session.closed
    assert session.writes == [
        (PurePosixPath(".guardian-runtime/auth.json"), b"secret", True)
    ]
    launched = session.requests[0]
    assert launched.argv == ("codex", "exec", "--cd", "/workspace", "-")
    assert launched.stdin == b"review"
