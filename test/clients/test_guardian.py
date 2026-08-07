# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the local-specification Guardian policy."""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import replace
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
    Evidence,
    FindingStatus,
    GuardianAgent,
    GuardianConfig,
    GuardianMemory,
    GuardianRequest,
    RememberedEvidence,
    RememberedSpecification,
    ReviewStatus,
    SpecificationAssessment,
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


def _git(tmp_path: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _request(tmp_path: Path) -> GuardianRequest:
    evidence = tmp_path / "tests" / "test_contract.py"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text("\n".join(f"line {line}" for line in range(1, 21)) + "\n")
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "add", ".")
    _git(
        tmp_path,
        "-c",
        "user.name=CodeNib Test",
        "-c",
        "user.email=test@codenib.ai",
        "commit",
        "--quiet",
        "-m",
        "base",
    )
    base = _git(tmp_path, "rev-parse", "HEAD")
    evidence.write_text(evidence.read_text() + "candidate line\n")
    _git(tmp_path, "add", ".")
    _git(
        tmp_path,
        "-c",
        "user.name=CodeNib Test",
        "-c",
        "user.email=test@codenib.ai",
        "commit",
        "--quiet",
        "-m",
        "candidate",
    )
    candidate = _git(tmp_path, "rev-parse", "HEAD")
    return GuardianRequest(
        workspace=tmp_path,
        base_commit=base,
        candidate_commit=candidate,
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
        "reopen every cited path" in " ".join(request.instruction.lower().split())
        for request in executor.requests
    )
    assert "invented filename" in executor.requests[0].instruction
    assert "synthetic paths" in executor.requests[-1].instruction
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


def test_guardian_fails_closed_when_workspace_is_not_candidate(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    executor = ScriptedExecutor([])
    agent = GuardianAgent(
        GuardianConfig(explorer_model="cheap", aggregator_model="strong"),
        executor=executor,
    )

    result = asyncio.run(
        agent.review(replace(request, candidate_commit=request.base_commit))
    )

    assert result.status is ReviewStatus.FAILED
    assert result.errors == (
        "workspace HEAD does not match the advertised candidate commit",
    )
    assert not executor.requests


def test_guardian_rejects_candidates_with_unverifiable_evidence(
    tmp_path: Path,
) -> None:
    candidate = _candidate("Copies preserve mode")
    candidate["evidence"] = [
        {
            "path": "../outside.py",
            "line_start": 1,
            "line_end": 1,
            "description": "a path outside the candidate workspace",
            "authority": "repository",
        },
        {
            "path": "tests/test_contract.py",
            "line_start": 100,
            "line_end": 100,
            "description": "a line beyond the file",
            "authority": "test",
        },
    ]
    executor = ScriptedExecutor([_result(json.dumps({"candidates": [candidate]}))])
    agent = GuardianAgent(
        GuardianConfig(
            explorer_model="cheap",
            aggregator_model="strong",
            explorer_count=1,
        ),
        executor=executor,
    )

    result = asyncio.run(agent.review(_request(tmp_path)))

    assert result.status is ReviewStatus.FAILED
    assert not result.candidates
    assert len(executor.requests) == 1
    assert any("not repository-relative" in error for error in result.errors)
    assert any("line range exceeds" in error for error in result.errors)


def test_guardian_rejects_aggregate_findings_with_invalid_lines(
    tmp_path: Path,
) -> None:
    finding = _finding("Copies preserve mode", "violated")
    finding["evidence"][0]["line_start"] = 999
    finding["evidence"][0]["line_end"] = 999
    executor = ScriptedExecutor(
        [
            _result(json.dumps({"candidates": [_candidate("Copies preserve mode")]})),
            _result(json.dumps({"summary": "Unverified.", "findings": [finding]})),
        ]
    )
    agent = GuardianAgent(
        GuardianConfig(
            explorer_model="cheap",
            aggregator_model="strong",
            explorer_count=1,
        ),
        executor=executor,
    )

    result = asyncio.run(agent.review(_request(tmp_path)))

    assert result.status is ReviewStatus.DEGRADED
    assert not result.findings
    assert not result.backlog
    assert any("aggregator finding" in error for error in result.errors)


def test_guardian_prompts_treat_memory_as_fallible_and_preserve_ids(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    remembered = RememberedSpecification(
        memory_id="spec:0123456789abcdef",
        statement="Copies preserve mode",
        condition="when state is copied",
        evidence=(
            RememberedEvidence(
                evidence=Evidence(
                    path="tests/test_contract.py",
                    line_start=10,
                    line_end=14,
                    description="the public-path contract",
                ),
                snapshot=request.base_commit,
                blob_sha256="0" * 64,
                fresh=False,
            ),
        ),
        assessments=(
            SpecificationAssessment(
                snapshot=request.base_commit,
                status=FindingStatus.VIOLATED,
                patch_assessment="an earlier snapshot omitted the field",
                recommendation="preserve the field",
                confidence=0.9,
            ),
        ),
    )
    executor = ScriptedExecutor(
        [
            _result(json.dumps({"candidates": [_candidate("Copies preserve mode")]})),
            _result(
                json.dumps(
                    {
                        "summary": "The remembered property is now satisfied.",
                        "findings": [
                            {
                                **_finding("Copies preserve mode", "satisfied"),
                                "memory_id": remembered.memory_id,
                                "condition": remembered.condition,
                            }
                        ],
                    }
                )
            ),
        ]
    )
    agent = GuardianAgent(
        GuardianConfig(
            explorer_model="cheap",
            aggregator_model="strong",
            explorer_count=1,
        ),
        executor=executor,
    )

    result = asyncio.run(
        agent.review(replace(request, memory=GuardianMemory((remembered,), ())))
    )

    assert result.assessments[0].memory_id == remembered.memory_id
    assert all(remembered.memory_id in call.instruction for call in executor.requests)
    assert "not authoritative truth" in executor.requests[0].instruction
    assert "hypotheses with provenance" in " ".join(
        executor.requests[1].instruction.split()
    )
    assert '"fresh_in_candidate": false' in executor.requests[0].instruction.lower()
