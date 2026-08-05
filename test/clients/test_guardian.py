# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the local-specification Guardian policy."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from codenib.clients.execution import (
    AgentRunError,
    AgentRunErrorCode,
    AgentRunRequest,
    AgentRunResult,
    RunStatus,
    TokenUsage,
)
from codenib.clients.guardian import (
    ContextMessage,
    GuardianAgent,
    GuardianConfig,
    GuardianRequest,
    ReviewStatus,
    render_markdown,
)


def _result(message: str, *, success: bool = True) -> AgentRunResult:
    return AgentRunResult(
        status=RunStatus.COMPLETED if success else RunStatus.FAILED,
        final_message=message if success else "",
        trajectory=(),
        usage=TokenUsage(input_tokens=100, output_tokens=20),
        duration_seconds=1,
        raw_output="",
        stderr="",
        exit_code=0 if success else 1,
        error=(
            None
            if success
            else AgentRunError(AgentRunErrorCode.CLI_EXIT, "synthetic explorer failure")
        ),
    )


class ScriptedExecutor:
    def __init__(self, results: list[AgentRunResult]) -> None:
        self.results = results
        self.requests: list[AgentRunRequest] = []

    async def run(self, request: AgentRunRequest) -> AgentRunResult:
        self.requests.append(request)
        return self.results.pop(0)


def _candidate(statement: str, authority: str = "test") -> dict[str, object]:
    return {
        "statement": statement,
        "condition": "when the public entry point copies state",
        "evidence": [
            {
                "path": "tests/test_contract.py",
                "line_start": 10,
                "line_end": 14,
                "description": "the public-path contract",
                "authority": authority,
            }
        ],
        "patch_assessment": "the new copy omits the field",
        "confidence": 0.9,
        "uncertainty": "",
    }


def _finding(
    statement: str, status: str, authority: str = "test", confidence: float = 0.9
) -> dict[str, object]:
    value = _candidate(statement, authority)
    return {
        "statement": value["statement"],
        "status": status,
        "evidence": value["evidence"],
        "patch_assessment": value["patch_assessment"],
        "recommendation": "preserve the field on the public copy path",
        "confidence": confidence,
    }


def _request(tmp_path: Path) -> GuardianRequest:
    return GuardianRequest(
        workspace=tmp_path,
        base_commit="a" * 40,
        candidate_commit="b" * 40,
        context=(
            ContextMessage(
                content="I think all state copies are covered.",
                sender="solver:test",
                scope=("src/state.py",),
            ),
        ),
    )


def test_guardian_discovers_aggregates_and_filters_delivery(tmp_path: Path) -> None:
    executor = ScriptedExecutor(
        [
            _result(json.dumps({"candidates": [_candidate("Copies preserve mode")]})),
            _result(
                json.dumps(
                    {"candidates": [_candidate("Failure leaves state unchanged")]}
                )
            ),
            _result(
                json.dumps(
                    {
                        "summary": "One repository-backed mismatch remains.",
                        "findings": [
                            _finding("Copies preserve mode", "violated"),
                            _finding(
                                "Solver intended an extra alias",
                                "violated",
                                authority="solver",
                            ),
                            _finding("Failure is atomic", "uncertain"),
                            _finding("Legacy path is preserved", "satisfied"),
                        ],
                    }
                )
            ),
        ]
    )
    agent = GuardianAgent(
        GuardianConfig(explorer_model="cheap", aggregator_model="strong"),
        executor=executor,
    )

    result = asyncio.run(agent.review(_request(tmp_path)))

    assert result.status is ReviewStatus.COMPLETE
    assert [finding.statement for finding in result.findings] == [
        "Copies preserve mode"
    ]
    assert [finding.statement for finding in result.backlog] == ["Failure is atomic"]
    assert result.usage.input_tokens == 300
    assert result.usage.output_tokens == 60
    assert [request.model for request in executor.requests] == [
        "cheap",
        "cheap",
        "strong",
    ]
    assert all(
        request.policy.filesystem.value == "read-only" for request in executor.requests
    )
    markdown = render_markdown(result)
    assert "Copies preserve mode" in markdown
    assert "Solver intended an extra alias" not in markdown


def test_guardian_marks_partial_exploration_degraded(tmp_path: Path) -> None:
    executor = ScriptedExecutor(
        [
            _result("", success=False),
            _result(json.dumps({"candidates": [_candidate("Copies preserve mode")]})),
            _result(
                json.dumps(
                    {
                        "summary": "Review completed with one explorer.",
                        "findings": [_finding("Copies preserve mode", "violated")],
                    }
                )
            ),
        ]
    )
    agent = GuardianAgent(
        GuardianConfig(explorer_model="cheap", aggregator_model="strong"),
        executor=executor,
    )

    result = asyncio.run(agent.review(_request(tmp_path)))

    assert result.status is ReviewStatus.DEGRADED
    assert result.findings
    assert result.errors == ("explorer_1: synthetic explorer failure",)


def test_guardian_fails_closed_without_candidates(tmp_path: Path) -> None:
    executor = ScriptedExecutor(
        [
            _result(json.dumps({"candidates": []})),
            _result("not JSON"),
        ]
    )
    agent = GuardianAgent(
        GuardianConfig(explorer_model="cheap", aggregator_model="strong"),
        executor=executor,
    )

    result = asyncio.run(agent.review(_request(tmp_path)))

    assert result.status is ReviewStatus.FAILED
    assert not result.findings
    assert len(executor.requests) == 2
    assert result.errors
