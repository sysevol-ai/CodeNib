# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for Guardian's multi-round specification search."""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import replace
from pathlib import Path

from codenib.clients.execution import (
    AgentEvent,
    AgentEventKind,
    AgentRunError,
    AgentRunErrorCode,
    AgentRunRequest,
    AgentRunResult,
    RunStatus,
    TokenUsage,
)
from codenib.clients.guardian import (
    ContextFidelity,
    ContextMessage,
    Evidence,
    EvidenceAuthority,
    EvidenceSourceType,
    FindingStatus,
    GuardianAgent,
    GuardianConfig,
    GuardianRequest,
    ProbeOutcome,
    ReviewStatus,
    SpecificationMemory,
    SpecificationProvenance,
    SpecificationRecord,
    SpecificationRelations,
    SpecificationStatus,
    StageUsage,
    TaskContext,
    TaskContextSource,
)
from codenib.clients.guardian.aggregation import (
    _merge_specification_records,
    adjudicate_records,
)
from codenib.clients.guardian.evidence import validate_candidates, validate_evidence
from codenib.clients.guardian.normalization import parse_explorer_output
from codenib.clients.guardian.patch_check import check_patch


def _run(
    message: object, *, success: bool = True, tool: bool = False
) -> AgentRunResult:
    text = message if isinstance(message, str) else json.dumps(message)
    return AgentRunResult(
        status=RunStatus.COMPLETED if success else RunStatus.FAILED,
        final_message=text if success else "",
        trajectory=(
            (
                AgentEvent(
                    sequence=1,
                    kind=AgentEventKind.TOOL,
                    provider_type="command_execution",
                    payload={"command": "rg contract"},
                ),
            )
            if tool
            else ()
        ),
        usage=TokenUsage(input_tokens=100, cached_input_tokens=25, output_tokens=20),
        duration_seconds=1,
        raw_output=text,
        stderr="",
        exit_code=0 if success else 1,
        error=(
            None
            if success
            else AgentRunError(AgentRunErrorCode.CLI_EXIT, "synthetic failure")
        ),
    )


def _run_with_probe(
    message: object,
    *,
    command: str,
    output: str,
    exit_code: int,
    tool_call_id: str = "item_1",
):
    result = _run(message)
    event = AgentEvent(
        sequence=1,
        kind=AgentEventKind.TOOL,
        provider_type="item.completed",
        payload={
            "type": "item.completed",
            "item": {
                "id": tool_call_id,
                "type": "command_execution",
                "command": command,
                "aggregated_output": output,
                "exit_code": exit_code,
                "status": "completed" if exit_code == 0 else "failed",
            },
        },
    )
    return replace(result, trajectory=(event,))


class ScriptedExecutor:
    def __init__(self, results: list[AgentRunResult]) -> None:
        self.results = results
        self.requests: list[AgentRunRequest] = []

    async def run(self, request: AgentRunRequest) -> AgentRunResult:
        self.requests.append(request)
        return self.results.pop(0)


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args),
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _request(tmp_path: Path) -> GuardianRequest:
    repository = tmp_path / "repository"
    repository.mkdir()
    source = repository / "contract.py"
    source.write_text("def copy_state():\n    return {'mode': 'safe'}\n")
    _git(repository, "init", "--quiet")
    _git(repository, "add", ".")
    _git(
        repository,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--quiet",
        "-m",
        "base",
    )
    base = _git(repository, "rev-parse", "HEAD")
    source.write_text("def copy_state():\n    return {}\n")
    _git(repository, "add", ".")
    _git(
        repository,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--quiet",
        "-m",
        "candidate",
    )
    candidate = _git(repository, "rev-parse", "HEAD")
    return GuardianRequest(
        workspace=repository,
        base_commit=base,
        candidate_commit=candidate,
        change_patch="-    return {'mode': 'safe'}\n+    return {}",
        task_context=(
            TaskContext(
                content="Every copied state must preserve its configured mode.",
                source=TaskContextSource.USER_INSTRUCTION,
                fidelity=ContextFidelity.VERBATIM,
                context_id="CTX-task",
            ),
        ),
        context=(ContextMessage("I believe state copies are complete."),),
        artifact_dir=tmp_path / "artifacts",
    )


def _briefs(count: int, *, linked: tuple[str, ...] = ()) -> dict[str, object]:
    return {
        "briefs": [
            {
                "id": f"BRIEF-{index + 1}",
                "focus_question": f"Distinct semantic question {index + 1}?",
                "target_surface": ["contract.py"],
                "evidence_to_seek": ["task and interface evidence"],
                "avoid_duplicating": [],
                "linked_specifications": list(linked),
                "open_ended": index == count - 1 and not linked,
            }
            for index in range(count)
        ]
    }


def _evidence(
    evidence_id: str = "EV-task",
    *,
    source_type: str = "task",
    authority: str = "normative",
    path: str = "CTX-task",
) -> dict[str, object]:
    value = {
        "id": evidence_id,
        "source_type": source_type,
        "authority": authority,
        "path": path,
        "line_start": 1,
        "line_end": 1,
        "observation": "The task requires copied state to preserve mode.",
    }
    if source_type == "task":
        value["quote"] = "Every copied state must preserve its configured mode."
    return value


def _scoped_candidate(
    local: str = "C-1", *, explorer: str = "explorer-1-1", round_number: int = 1
) -> str:
    return f"C-R{round_number}-{explorer}-{local}"


def _scoped_evidence(
    local: str = "EV-task",
    *,
    explorer: str = "explorer-1-1",
    round_number: int = 1,
) -> str:
    return f"EV-R{round_number}-{explorer}-{local}"


def _exploration(
    candidate_id: str = "C-1",
    *,
    evidence: dict[str, object] | None = None,
    suggested_status: str = "supported",
) -> dict[str, object]:
    return {
        "candidate_specifications": [
            {
                "candidate_id": candidate_id,
                "statement": "Copied state preserves configured mode.",
                "condition": "when copy_state is called",
                "scope": ["copy_state", "mode"],
                "supporting_evidence": [evidence or _evidence()],
                "counterevidence": [],
                "suggested_status": suggested_status,
                "unresolved_questions": [],
            }
        ],
        "exploration_trace": {
            "searched_locations": ["contract.py"],
            "actions_and_outcomes": ["inspected copy_state"],
            "unsuccessful_routes": [],
            "remaining_questions": [],
            "suggested_next_actions": [],
        },
    }


def _aggregation(
    *,
    status: str = "supported",
    evidence_ids: tuple[str, ...] | None = None,
    provenance: tuple[tuple[str, str], ...] | None = None,
    conflicts: tuple[str, ...] = (),
) -> dict[str, object]:
    evidence_ids = evidence_ids or (_scoped_evidence(),)
    provenance = provenance or (("explorer-1-1", _scoped_candidate()),)
    return {
        "summary": "Specification memory updated.",
        "specifications": [
            {
                "id": "LS-mode",
                "statement": "Copied state preserves configured mode.",
                "condition": "when copy_state is called",
                "scope": ["copy_state", "mode"],
                "supporting_evidence": list(evidence_ids),
                "counterevidence": [],
                "provenance": [
                    {"explorer": explorer, "round": 1, "candidate_id": candidate}
                    for explorer, candidate in provenance
                ],
                "status": status,
                "status_reason": "model adjudication",
                "unresolved_questions": [],
                "related_entries": {
                    "equivalent": [],
                    "entailed_by": [],
                    "conflicts_with": list(conflicts),
                },
                "created_round": 1,
                "updated_round": 1,
            }
        ],
    }


def _distillation(*, linked: str = "LS-mode") -> dict[str, object]:
    return {
        "experiences": [
            {
                "id": "EXP-relevant",
                "brief_id": "BRIEF-1",
                "searched_locations": ["contract.py"],
                "actions_and_outcomes": ["copy_state inspected"],
                "unsuccessful_routes": [],
                "unresolved_questions": ["check another copy path"],
                "suggested_next_actions": ["inspect callers"],
                "linked_specifications": [linked],
            }
        ]
    }


def _patch_check() -> dict[str, object]:
    return {
        "summary": "One supported specification is violated.",
        "findings": [
            {
                "specification_id": "LS-mode",
                "status": "violated",
                "patch_locations": ["contract.py:2"],
                "evidence_ids": [_scoped_evidence()],
                "assessment": "The patch removes mode from the returned state.",
                "recommendation": "Preserve configured mode on every copy path.",
            }
        ],
        "backlog": [],
    }


def _one_round_executor(*, tool: bool = False) -> ScriptedExecutor:
    return ScriptedExecutor(
        [
            _run(_briefs(1), tool=tool),
            _run(_exploration(), tool=tool),
            _run(_aggregation(), tool=tool),
            _run(_distillation(), tool=tool),
            _run(_patch_check(), tool=tool),
        ]
    )


def _one_round_config(**kwargs) -> GuardianConfig:
    explorer_count = kwargs.pop("explorer_count", 1)
    return GuardianConfig(
        explorer_model="cheap",
        aggregator_model="strong",
        explorer_count=explorer_count,
        max_rounds=1,
        **kwargs,
    )


def test_verbatim_task_context_retains_provenance(tmp_path: Path) -> None:
    request = _request(tmp_path)
    executor = _one_round_executor()
    result = asyncio.run(
        GuardianAgent(_one_round_config(), executor=executor).review(request)
    )

    persisted = json.loads((result.artifact_dir / "request.json").read_text())
    assert persisted["task_context"][0] == {
        "content": "Every copied state must preserve its configured mode.",
        "fidelity": "verbatim",
        "id": "CTX-task",
        "source": "user_instruction",
    }
    assert all("untrusted" in item.instruction.lower() for item in executor.requests)
    explorer_instruction = executor.requests[1].instruction.lower()
    normalized_explorer_instruction = " ".join(explorer_instruction.split())
    assert "code mode and the code mode host are unavailable" in explorer_instruction
    assert "direct shell command-execution tool" in explorer_instruction
    assert (
        "discover at most 5 falsifiable local specifications"
        in normalized_explorer_instruction
    )
    assert "hard output limit" in normalized_explorer_instruction


def _record(
    status: SpecificationStatus = SpecificationStatus.SUPPORTED,
) -> SpecificationRecord:
    return SpecificationRecord(
        specification_id="LS-mode",
        statement="Copied state preserves configured mode.",
        condition="when copied",
        scope=("copy",),
        supporting_evidence=("EV-1",),
        counterevidence=(),
        provenance=(SpecificationProvenance("explorer", 1, "C-1"),),
        status=status,
        status_reason="suggested",
    )


def _typed_evidence(
    source: EvidenceSourceType, authority: EvidenceAuthority
) -> Evidence:
    return Evidence(
        path="CTX-any",
        line_start=1,
        line_end=1,
        description="an observation",
        source_type=source,
        authority=authority,
        evidence_id="EV-1",
    )


def test_solver_summary_cannot_independently_support_specification() -> None:
    adjudicated = adjudicate_records(
        (_record(),),
        (
            _typed_evidence(
                EvidenceSourceType.SOLVER_MESSAGE, EvidenceAuthority.DERIVED
            ),
        ),
    )
    assert adjudicated[0].status is SpecificationStatus.REJECTED


def test_candidate_patch_cannot_independently_support_specification() -> None:
    adjudicated = adjudicate_records(
        (_record(),),
        (
            _typed_evidence(
                EvidenceSourceType.CANDIDATE_PATCH, EvidenceAuthority.DERIVED
            ),
        ),
    )
    assert adjudicated[0].status is SpecificationStatus.REJECTED


def test_behavioral_only_evidence_cannot_promote_specification() -> None:
    adjudicated = adjudicate_records(
        (_record(),),
        (
            _typed_evidence(
                EvidenceSourceType.RUNTIME_PROBE, EvidenceAuthority.BEHAVIORAL
            ),
        ),
    )
    assert adjudicated[0].status is SpecificationStatus.PROPOSED


def test_rollout_local_candidate_and_evidence_ids_are_namespaced() -> None:
    first = parse_explorer_output(
        json.dumps(_exploration("C-1", evidence=_evidence("EV-1"))),
        explorer="explorer-1-1",
        round_number=1,
        brief_id="BRIEF-1",
    )
    second = parse_explorer_output(
        json.dumps(_exploration("C-1", evidence=_evidence("EV-1"))),
        explorer="explorer-1-2",
        round_number=1,
        brief_id="BRIEF-2",
    )

    assert first.candidates[0].candidate_id != second.candidates[0].candidate_id
    assert (
        first.candidates[0].supporting_evidence[0].evidence_id
        != second.candidates[0].supporting_evidence[0].evidence_id
    )
    assert first.candidates[0].supporting_evidence[0].supports == (
        first.candidates[0].candidate_id,
    )


def test_invalid_evidence_is_dropped_without_losing_supported_candidate(
    tmp_path: Path,
) -> None:
    exploration = _exploration("C-kept")
    invalid = _evidence("EV-invalid", path="CTX-missing")
    exploration["candidate_specifications"][0]["supporting_evidence"].append(invalid)
    exploration["candidate_specifications"].append(
        {
            **exploration["candidate_specifications"][0],
            "candidate_id": "C-dropped",
            "supporting_evidence": [invalid],
        }
    )
    parsed = parse_explorer_output(
        json.dumps(exploration),
        explorer="explorer-1-1",
        round_number=1,
        brief_id="BRIEF-1",
    )

    candidates, errors = validate_candidates(_request(tmp_path), parsed.candidates)

    assert [item.candidate_id for item in candidates] == [_scoped_candidate("C-kept")]
    assert len(candidates[0].supporting_evidence) == 1
    assert len(errors) == 2


def test_invalid_candidate_evidence_does_not_degrade_whole_review(
    tmp_path: Path,
) -> None:
    executor = _one_round_executor()
    exploration = json.loads(executor.results[1].final_message)
    exploration["candidate_specifications"][0]["supporting_evidence"].append(
        _evidence("EV-bad", path="missing.py")
    )
    executor.results[1] = _run(exploration)

    result = asyncio.run(
        GuardianAgent(_one_round_config(), executor=executor).review(_request(tmp_path))
    )

    assert result.status is ReviewStatus.COMPLETE
    assert any("missing.py" in error for error in result.errors)


def test_file_evidence_locator_and_whitespace_are_normalized(tmp_path: Path) -> None:
    request = _request(tmp_path)
    evidence = Evidence(
        path="`contract.py:1-2`",
        line_start=1,
        line_end=1,
        description="copy_state returns the candidate state",
        source_type=EvidenceSourceType.INTERFACE,
        quote="def copy_state():\nreturn {}",
    )

    checked, error = validate_evidence(request, evidence)

    assert error == ""
    assert checked is not None
    assert checked.path == "contract.py"
    assert (checked.line_start, checked.line_end) == (1, 2)
    assert checked.quote == "def copy_state():\n    return {}"


def test_file_evidence_quote_is_reanchored_outside_cited_range(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    evidence = Evidence(
        path="contract.py:99-101",
        line_start=1,
        line_end=1,
        description="copy_state preserves safe mode",
        source_type=EvidenceSourceType.INTERFACE,
        quote="return {}",
    )

    checked, error = validate_evidence(request, evidence)

    assert error == ""
    assert checked is not None
    assert checked.path == "contract.py"
    assert (checked.line_start, checked.line_end) == (2, 2)


def test_verbatim_task_authority_survives_locator_suffix_and_paraphrase(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    evidence = Evidence(
        path="CTX-task#copy-mode",
        line_start=1,
        line_end=1,
        description="copy operations retain the selected mode",
        source_type=EvidenceSourceType.TASK,
        quote="Copies have to keep their mode.",
    )

    checked, error = validate_evidence(request, evidence)

    assert error == ""
    assert checked is not None
    assert checked.path == "CTX-task"
    assert checked.authority is EvidenceAuthority.NORMATIVE
    assert checked.quote == request.task_context[0].content


def test_derived_context_still_requires_an_exact_quote(tmp_path: Path) -> None:
    request = _request(tmp_path)
    solver_context = next(
        item
        for item in request.task_context
        if item.source is TaskContextSource.SOLVER_SUMMARY
    )
    evidence = Evidence(
        path=f"{solver_context.context_id}#summary",
        line_start=1,
        line_end=1,
        description="solver believes state copies are complete",
        source_type=EvidenceSourceType.SOLVER_MESSAGE,
        quote="The solver says all copies are correct.",
    )

    checked, error = validate_evidence(request, evidence)

    assert checked is None
    assert error == "quoted content does not occur in the task message"


def test_silent_runtime_probe_is_valid_when_exit_code_is_retained(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    evidence = Evidence(
        path="runtime-probe:item_1",
        line_start=1,
        line_end=1,
        description="The assertion completed without printing output.",
        source_type=EvidenceSourceType.RUNTIME_PROBE,
        command="python /tmp/assertion.py",
        output="",
        exit_code=0,
    )

    checked, error = validate_evidence(request, evidence)

    assert error == ""
    assert checked is not None
    assert checked.authority is EvidenceAuthority.BEHAVIORAL


def test_guardian_defaults_to_five_specifications_per_explorer() -> None:
    config = GuardianConfig(explorer_model="cheap", aggregator_model="strong")

    assert config.max_specifications_per_explorer == 5


def test_equivalent_candidates_merge_without_losing_provenance(tmp_path: Path) -> None:
    request = _request(tmp_path)
    executor = ScriptedExecutor(
        [
            _run(_briefs(2)),
            _run(_exploration("C-1", evidence=_evidence("EV-1"))),
            _run(_exploration("C-2", evidence=_evidence("EV-2"))),
            _run(
                _aggregation(
                    evidence_ids=(
                        _scoped_evidence("EV-1", explorer="explorer-1-1"),
                        _scoped_evidence("EV-2", explorer="explorer-1-2"),
                    ),
                    provenance=(
                        (
                            "explorer-1-1",
                            _scoped_candidate("C-1", explorer="explorer-1-1"),
                        ),
                        (
                            "explorer-1-2",
                            _scoped_candidate("C-2", explorer="explorer-1-2"),
                        ),
                    ),
                )
            ),
            _run(_distillation()),
            _run(_patch_check()),
        ]
    )
    result = asyncio.run(
        GuardianAgent(_one_round_config(explorer_count=2), executor=executor).review(
            request
        )
    )
    spec = result.memory.specifications[0]
    assert spec.supporting_evidence == (
        _scoped_evidence("EV-1", explorer="explorer-1-1"),
        _scoped_evidence("EV-2", explorer="explorer-1-2"),
    )
    assert {item.candidate_id for item in spec.provenance} == {
        _scoped_candidate("C-1", explorer="explorer-1-1"),
        _scoped_candidate("C-2", explorer="explorer-1-2"),
    }


def test_aggregation_cannot_attach_unrelated_evidence_by_identifier(
    tmp_path: Path,
) -> None:
    first_candidate = _scoped_candidate("C-1", explorer="explorer-1-1")
    second_candidate = _scoped_candidate("C-2", explorer="explorer-1-2")
    first_evidence = _scoped_evidence("EV-1", explorer="explorer-1-1")
    second_evidence = _scoped_evidence("EV-2", explorer="explorer-1-2")
    aggregation = _aggregation(
        evidence_ids=(second_evidence,),
        provenance=(("explorer-1-1", first_candidate),),
    )
    aggregation["specifications"].append(
        {
            **aggregation["specifications"][0],
            "id": "LS-other",
            "supporting_evidence": [first_evidence],
            "provenance": [
                {
                    "explorer": "explorer-1-2",
                    "round": 1,
                    "candidate_id": second_candidate,
                }
            ],
        }
    )
    executor = ScriptedExecutor(
        [
            _run(_briefs(2)),
            _run(_exploration("C-1", evidence=_evidence("EV-1"))),
            _run(_exploration("C-2", evidence=_evidence("EV-2"))),
            _run(aggregation),
            _run(_distillation()),
        ]
    )

    result = asyncio.run(
        GuardianAgent(_one_round_config(explorer_count=2), executor=executor).review(
            _request(tmp_path)
        )
    )

    assert result.status is ReviewStatus.COMPLETE
    assert all(
        item.status is SpecificationStatus.REJECTED
        for item in result.memory.specifications
    )
    assert not result.findings


def test_aggregation_cannot_rewrite_existing_specification_through_its_id() -> None:
    original = _record()
    rewritten = replace(
        original,
        statement="Copied state preserves configured output labels.",
        condition="when target preprocessing expands columns",
    )

    merged = _merge_specification_records((original,), (rewritten,))

    assert len(merged) == 2
    persisted = next(
        item for item in merged if item.specification_id == original.specification_id
    )
    assert persisted.statement == original.statement
    assert persisted.condition == original.condition
    added = next(item for item in merged if item is not persisted)
    assert added.statement == rewritten.statement
    assert added.condition == rewritten.condition
    assert added.specification_id != original.specification_id


def test_aggregation_keeps_equal_statements_under_distinct_conditions() -> None:
    original = _record()
    distinct = replace(
        original,
        specification_id="LS-mode-on-reset",
        condition="when copied state is reset",
    )

    merged = _merge_specification_records((original,), (distinct,))

    assert len(merged) == 2
    assert {item.condition for item in merged} == {
        original.condition,
        distinct.condition,
    }
    assert len({item.specification_id for item in merged}) == 2


def test_contradictory_candidates_remain_contested() -> None:
    record = replace(
        _record(),
        related_entries=SpecificationRelations(conflicts_with=("LS-opposite",)),
    )
    opposite = replace(
        _record(),
        specification_id="LS-opposite",
        statement="Copied state omits configured mode.",
        supporting_evidence=("EV-2",),
        related_entries=SpecificationRelations(conflicts_with=("LS-mode",)),
    )
    evidence = _typed_evidence(EvidenceSourceType.TASK, EvidenceAuthority.NORMATIVE)
    opposing_evidence = replace(evidence, evidence_id="EV-2")
    adjudicated = adjudicate_records((record, opposite), (evidence, opposing_evidence))
    assert all(item.status is SpecificationStatus.CONTESTED for item in adjudicated)


def test_rejected_conflict_does_not_contest_supported_specification() -> None:
    supported = replace(
        _record(),
        related_entries=SpecificationRelations(conflicts_with=("LS-rejected",)),
    )
    rejected = replace(
        _record(),
        specification_id="LS-rejected",
        supporting_evidence=(),
        related_entries=SpecificationRelations(conflicts_with=("LS-mode",)),
    )
    evidence = _typed_evidence(EvidenceSourceType.TASK, EvidenceAuthority.NORMATIVE)

    adjudicated = adjudicate_records((supported, rejected), (evidence,))

    assert adjudicated[0].status is SpecificationStatus.SUPPORTED
    assert adjudicated[1].status is SpecificationStatus.REJECTED


def test_explorer_suggested_supported_is_advisory(tmp_path: Path) -> None:
    request = _request(tmp_path)
    behavioral = _evidence(
        source_type="existing_behavior",
        authority="behavioral",
        path="contract.py",
    )
    executor = ScriptedExecutor(
        [
            _run(_briefs(1)),
            _run(_exploration(evidence=behavioral, suggested_status="supported")),
            _run(_aggregation(status="supported")),
            _run(_distillation()),
            _run({"summary": "", "findings": [], "backlog": []}),
        ]
    )
    result = asyncio.run(
        GuardianAgent(_one_round_config(), executor=executor).review(request)
    )
    assert result.memory.specifications[0].status is SpecificationStatus.PROPOSED
    assert not result.findings


def test_distillation_cannot_create_or_promote_specification(tmp_path: Path) -> None:
    request = _request(tmp_path)
    executor = ScriptedExecutor(
        [
            _run(_briefs(1)),
            _run(_exploration()),
            _run(_aggregation(status="proposed")),
            _run(
                {
                    "experiences": [
                        {
                            "id": "EXP-1",
                            "brief_id": "BRIEF-1",
                            "searched_locations": [],
                            "actions_and_outcomes": ["LS-mode is definitely supported"],
                            "unsuccessful_routes": [],
                            "unresolved_questions": [],
                            "suggested_next_actions": [],
                            "linked_specifications": ["LS-mode"],
                            "status": "supported",
                        }
                    ]
                }
            ),
            _run(_patch_check()),
        ]
    )
    result = asyncio.run(
        GuardianAgent(_one_round_config(), executor=executor).review(request)
    )
    assert result.memory.specifications[0].status is SpecificationStatus.SUPPORTED
    # Direct task evidence, not the trajectory claim, is what promoted the entry.
    assert result.memory.specifications[0].status_reason.startswith("direct normative")


def test_only_supported_specifications_produce_definite_violations(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    executor = ScriptedExecutor(
        [
            _run(_briefs(1)),
            _run(
                _exploration(
                    evidence=_evidence(
                        source_type="runtime_probe",
                        authority="behavioral",
                        path="probe-1",
                    )
                )
            ),
            _run(_aggregation(status="supported")),
            _run(_distillation()),
            _run({"summary": "", "findings": [], "backlog": []}),
        ]
    )
    # Make runtime evidence valid.
    exploration = json.loads(executor.results[1].final_message)
    ev = exploration["candidate_specifications"][0]["supporting_evidence"][0]
    ev["command"] = "python probe.py"
    ev["output"] = "mode missing"
    executor.results[1] = _run(exploration)
    result = asyncio.run(
        GuardianAgent(_one_round_config(), executor=executor).review(request)
    )
    assert not result.findings
    assert result.uncertain_specifications


def test_one_failed_explorer_yields_degraded_usable_review(tmp_path: Path) -> None:
    request = _request(tmp_path)
    executor = ScriptedExecutor(
        [
            _run(_briefs(2)),
            _run("", success=False),
            _run(_exploration()),
            _run(
                _aggregation(
                    evidence_ids=(_scoped_evidence(explorer="explorer-1-2"),),
                    provenance=(
                        (
                            "explorer-1-2",
                            _scoped_candidate(explorer="explorer-1-2"),
                        ),
                    ),
                )
            ),
            _run(_distillation()),
            _run(
                {
                    **_patch_check(),
                    "findings": [
                        {
                            **_patch_check()["findings"][0],
                            "evidence_ids": [_scoped_evidence(explorer="explorer-1-2")],
                        }
                    ],
                }
            ),
        ]
    )
    result = asyncio.run(
        GuardianAgent(_one_round_config(explorer_count=2), executor=executor).review(
            request
        )
    )
    assert result.status is ReviewStatus.DEGRADED
    assert result.findings[0].status is FindingStatus.VIOLATED


def test_zero_supported_specifications_is_valid_empty_review(tmp_path: Path) -> None:
    request = _request(tmp_path)
    behavioral = _evidence(
        source_type="existing_behavior", authority="behavioral", path="contract.py"
    )
    executor = ScriptedExecutor(
        [
            _run(_briefs(1)),
            _run(_exploration(evidence=behavioral)),
            _run(_aggregation()),
            _run(_distillation()),
            _run({"summary": "", "findings": [], "backlog": []}),
        ]
    )
    result = asyncio.run(
        GuardianAgent(_one_round_config(), executor=executor).review(request)
    )
    assert result.status is ReviewStatus.COMPLETE
    assert not result.findings
    assert result.uncertain_specifications


def test_later_round_receives_only_relevant_experience(tmp_path: Path) -> None:
    request = _request(tmp_path)
    round_one_aggregation = _aggregation(
        provenance=tuple(
            (
                f"explorer-1-{index}",
                _scoped_candidate(f"C-{index}", explorer=f"explorer-1-{index}"),
            )
            for index in range(1, 5)
        )
    )
    round_one_aggregation["specifications"].append(
        {
            **round_one_aggregation["specifications"][0],
            "id": "LS-other",
            "statement": "An unrelated property.",
            "supporting_evidence": [],
            "status": "rejected",
            "status_reason": "irrelevant",
        }
    )
    distillation = _distillation()
    distillation["experiences"].append(
        {
            **distillation["experiences"][0],
            "id": "EXP-irrelevant",
            "linked_specifications": ["LS-other"],
        }
    )
    executor = ScriptedExecutor(
        [
            _run(_briefs(4)),
            *[_run(_exploration(f"C-{index}")) for index in range(1, 5)],
            _run(round_one_aggregation),
            _run(distillation),
            _run({"frontier": ["LS-mode"], "reason": "high impact"}),
            _run(_briefs(2, linked=("LS-mode",))),
            _run({"candidate_specifications": [], "exploration_trace": {}}),
            _run({"candidate_specifications": [], "exploration_trace": {}}),
            _run({"summary": "no changes", "specifications": []}),
            _run({"experiences": []}),
            _run(_patch_check()),
        ]
    )
    result = asyncio.run(
        GuardianAgent(
            GuardianConfig(explorer_model="cheap", aggregator_model="strong"),
            executor=executor,
        ).review(request)
    )
    assert len(result.rounds) == 2
    assert len(result.rounds[0].explorer_outputs) == 4
    assert len(result.rounds[1].explorer_outputs) == 2
    assert len(result.rollouts) == 14
    round_two_prompts = [
        item.instruction
        for item in executor.requests
        if item.metadata.get("guardian_stage") == "investigation"
        and item.metadata.get("round") == "2"
    ]
    assert round_two_prompts
    assert all("EXP-relevant" in prompt for prompt in round_two_prompts)
    assert all("EXP-irrelevant" not in prompt for prompt in round_two_prompts)
    root_artifacts = {
        "request.json",
        "config.json",
        "memory.json",
        "patch-check.json",
        "final-report.json",
        "usage.json",
    }
    assert root_artifacts.issubset(
        {path.name for path in result.artifact_dir.iterdir()}
    )
    assert len(list((result.artifact_dir / "round-01").glob("explorer-*.json"))) == 4
    assert len(list((result.artifact_dir / "round-02").glob("explorer-*.json"))) == 2
    round_one = json.loads(
        (result.artifact_dir / "round-01" / "aggregation.json").read_text()
    )
    assert round_one["specifications"]
    assert all(item["updated_round"] == 1 for item in round_one["specifications"])
    patch_check = json.loads((result.artifact_dir / "patch-check.json").read_text())
    assert patch_check["probe_metrics"]["declared"] == 0
    final_report = json.loads((result.artifact_dir / "final-report.json").read_text())
    assert final_report["probe_metrics"] == patch_check["probe_metrics"]


def test_later_round_planning_failure_falls_back_to_patch_check(
    tmp_path: Path,
) -> None:
    executor = ScriptedExecutor(
        [
            _run(_briefs(1)),
            _run(_exploration()),
            _run(_aggregation()),
            _run(_distillation()),
            _run({"frontier": ["LS-mode"], "reason": "high impact"}),
            _run(_briefs(1, linked=("LS-not-on-frontier",))),
            _run(_patch_check()),
        ]
    )

    result = asyncio.run(
        GuardianAgent(
            GuardianConfig(
                explorer_model="cheap",
                aggregator_model="strong",
                explorer_count=1,
                targeted_explorer_count=1,
                max_rounds=2,
            ),
            executor=executor,
        ).review(_request(tmp_path))
    )

    assert result.status is ReviewStatus.DEGRADED
    assert result.findings
    assert len(result.rounds) == 1
    assert any("selected frontier" in error for error in result.errors)


def test_all_explorer_failures_fail_with_inspectable_artifacts(tmp_path: Path) -> None:
    executor = ScriptedExecutor(
        [_run(_briefs(2)), _run("", success=False), _run("", success=False)]
    )

    result = asyncio.run(
        GuardianAgent(_one_round_config(explorer_count=2), executor=executor).review(
            _request(tmp_path)
        )
    )

    assert result.status is ReviewStatus.FAILED
    assert len(result.rounds) == 1
    artifacts = list((result.artifact_dir / "round-01").glob("explorer-*.json"))
    assert len(artifacts) == 2
    assert all(json.loads(path.read_text())["error"] for path in artifacts)


def test_aggregation_failure_preserves_completed_explorer_artifacts(
    tmp_path: Path,
) -> None:
    executor = ScriptedExecutor(
        [_run(_briefs(1)), _run(_exploration()), _run("", success=False)]
    )

    result = asyncio.run(
        GuardianAgent(_one_round_config(), executor=executor).review(_request(tmp_path))
    )

    assert result.status is ReviewStatus.FAILED
    explorer_artifact = result.artifact_dir / "round-01" / "explorer-01.json"
    assert explorer_artifact.is_file()
    assert json.loads(explorer_artifact.read_text())["candidate_specifications"]
    aggregation = json.loads(
        (result.artifact_dir / "round-01" / "aggregation.json").read_text()
    )
    assert aggregation["errors"]


def test_distillation_failure_preserves_memory_and_continues(tmp_path: Path) -> None:
    executor = ScriptedExecutor(
        [
            _run(_briefs(1)),
            _run(_exploration()),
            _run(_aggregation()),
            _run("", success=False),
            _run(_patch_check()),
        ]
    )

    result = asyncio.run(
        GuardianAgent(_one_round_config(), executor=executor).review(_request(tmp_path))
    )

    assert result.status is ReviewStatus.DEGRADED
    assert result.memory.specifications
    assert result.findings
    assert any("distill" in error for error in result.errors)


def test_patch_check_failure_preserves_completed_memory(tmp_path: Path) -> None:
    executor = ScriptedExecutor(
        [
            _run(_briefs(1)),
            _run(_exploration()),
            _run(_aggregation()),
            _run(_distillation()),
            _run("", success=False),
        ]
    )

    result = asyncio.run(
        GuardianAgent(_one_round_config(), executor=executor).review(_request(tmp_path))
    )

    assert result.status is ReviewStatus.FAILED
    assert result.memory.specifications
    assert (result.artifact_dir / "memory.json").is_file()
    assert any("patch checker" in error for error in result.errors)


def test_patch_check_exposes_omitted_supported_specification(tmp_path: Path) -> None:
    request = _request(tmp_path)
    evidence = replace(
        _typed_evidence(EvidenceSourceType.TASK, EvidenceAuthority.NORMATIVE),
        evidence_id=_scoped_evidence(),
    )
    first = replace(_record(), supporting_evidence=(_scoped_evidence(),))
    second = replace(
        first,
        specification_id="LS-second",
        statement="Every copy path preserves configured mode.",
    )
    memory = SpecificationMemory(specifications=(first, second), evidence=(evidence,))
    executor = ScriptedExecutor([_run(_patch_check())])

    _, findings, uncertainty, _, _, error = asyncio.run(
        check_patch(request, _one_round_config(), executor, memory)
    )

    assert error is None
    assert [item.specification_id for item in findings] == ["LS-mode"]
    assert [item.specification_id for item in uncertainty] == ["LS-second"]
    assert "omitted" in uncertainty[0].patch_assessment
    instruction = executor.requests[0].instruction.lower()
    normalized_instruction = " ".join(instruction.split())
    assert "assess every supported" in normalized_instruction
    assert "exactly once in the findings array" in normalized_instruction
    assert "review contract-first" in normalized_instruction
    assert "original task supplied to the coding system" in normalized_instruction
    assert (
        "every copied state must preserve its configured mode" in normalized_instruction
    )
    assert instruction.index("direct verbatim task contract:") < instruction.index(
        "\nsupported specifications:"
    )


def test_verbatim_task_can_promote_proposed_patch_violation(tmp_path: Path) -> None:
    request = _request(tmp_path)
    proposed = replace(
        _record(SpecificationStatus.PROPOSED),
        supporting_evidence=(),
        status_reason="explorer citation was malformed",
    )
    memory = SpecificationMemory(specifications=(proposed,))
    response = {
        "summary": "The explicit task requirement is violated.",
        "findings": [
            {
                "specification_id": "LS-mode",
                "status": "violated",
                "patch_locations": ["contract.py:1"],
                "evidence_ids": ["EV-TASK-CTX-task"],
                "assessment": "The copy drops the configured mode.",
                "recommendation": "Preserve the mode.",
            }
        ],
        "backlog": [],
    }
    executor = ScriptedExecutor([_run(response)])

    _, findings, uncertainty, _, _, error = asyncio.run(
        check_patch(request, _one_round_config(), executor, memory)
    )

    assert error is None
    assert [item.specification_id for item in findings] == ["LS-mode"]
    assert findings[0].evidence[0].authority is EvidenceAuthority.NORMATIVE
    assert not uncertainty


def test_supported_violation_accepts_linked_runtime_counterevidence(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    task_evidence = replace(
        _typed_evidence(EvidenceSourceType.TASK, EvidenceAuthority.NORMATIVE),
        evidence_id="EV-task",
        supports=("LS-mode",),
    )
    memory = SpecificationMemory(
        specifications=(replace(_record(), supporting_evidence=("EV-task",)),),
        evidence=(task_evidence,),
    )
    response = {
        "summary": "The focused contract probe fails.",
        "runtime_evidence": [
            {
                "id": "EV-PROBE-mode",
                "probe_key": "mode-copy-preserved",
                "tool_call_id": "item_1",
                "specification_id": "LS-mode",
                "observation": "A copied state omitted its configured mode.",
            }
        ],
        "findings": [
            {
                "specification_id": "LS-mode",
                "status": "satisfied",
                "patch_locations": ["contract.py:2"],
                "evidence_ids": ["EV-PROBE-mode"],
                "assessment": "The model incorrectly labels the failed probe satisfied.",
                "recommendation": "Preserve mode on the copy path.",
            }
        ],
        "backlog": [],
    }

    _, findings, uncertainty, _, _, error = asyncio.run(
        check_patch(
            request,
            _one_round_config(),
            ScriptedExecutor(
                [
                    _run_with_probe(
                        response,
                        command="python /tmp/probe_mode.py",
                        output="mode key missing; exit=1",
                        exit_code=10,
                    )
                ]
            ),
            memory,
        )
    )

    assert error is None
    assert [item.specification_id for item in findings] == ["LS-mode"]
    assert findings[0].evidence[0].source_type is EvidenceSourceType.RUNTIME_PROBE
    assert findings[0].evidence[0].evidence_id.startswith("EV-PATCH-")
    assert findings[0].evidence_ids == (findings[0].evidence[0].evidence_id,)
    assert findings[0].evidence[0].opposes == ("LS-mode",)
    assert findings[0].evidence[0].snapshot == request.candidate_commit
    assert findings[0].evidence[0].probe_key == "mode-copy-preserved"
    assert findings[0].evidence[0].probe_outcome is ProbeOutcome.VIOLATED
    assert findings[0].evidence[0].exit_code == 10
    assert findings[0].evidence[0].command == "python /tmp/probe_mode.py"
    assert findings[0].evidence[0].output == "mode key missing; exit=1"
    assert not uncertainty


def test_unexecuted_runtime_claim_cannot_promote_violation(tmp_path: Path) -> None:
    request = _request(tmp_path)
    task_evidence = replace(
        _typed_evidence(EvidenceSourceType.TASK, EvidenceAuthority.NORMATIVE),
        evidence_id="EV-task",
        supports=("LS-mode",),
    )
    memory = SpecificationMemory(
        specifications=(replace(_record(), supporting_evidence=("EV-task",)),),
        evidence=(task_evidence,),
    )
    response = {
        "summary": "A probe was claimed but not executed.",
        "runtime_evidence": [
            {
                "id": "EV-PROBE-invented",
                "probe_key": "mode-copy-preserved",
                "tool_call_id": "item-missing",
                "specification_id": "LS-mode",
                "observation": "This output is absent from the trajectory.",
            }
        ],
        "findings": [
            {
                "specification_id": "LS-mode",
                "status": "violated",
                "evidence_ids": ["EV-PROBE-invented"],
                "assessment": "The copy path allegedly drops mode.",
                "recommendation": "Preserve mode.",
            }
        ],
        "backlog": [],
    }

    _, findings, uncertainty, _, execution, error = asyncio.run(
        check_patch(
            request,
            _one_round_config(),
            ScriptedExecutor(
                [
                    _run(response),
                    _run(
                        {
                            **response,
                            "runtime_evidence": [],
                            "findings": [
                                {
                                    **response["findings"][0],
                                    "status": "uncertain",
                                    "evidence_ids": [],
                                }
                            ],
                        }
                    ),
                ]
            ),
            memory,
        )
    )

    assert error is None
    assert not findings
    assert [item.specification_id for item in uncertainty] == ["LS-mode"]
    assert not uncertainty[0].evidence_ids
    assert execution.metrics.declared == 1
    assert execution.metrics.accepted == 0
    assert execution.metrics.repaired == 0
    assert execution.metrics.rejected_by_reason == (("removed_by_repair", 1),)


def test_probe_marker_recovers_misreported_tool_call_id(tmp_path: Path) -> None:
    request = _request(tmp_path)
    memory = SpecificationMemory(specifications=(_record(),))
    response = {
        "summary": "The assertion falsified the specification.",
        "runtime_evidence": [
            {
                "id": "EV-PROBE-mode",
                "probe_key": "mode-copy-preserved",
                "tool_call_id": "item_probe_symbolic",
                "specification_id": "LS-mode",
                "observation": "The focused copy assertion failed.",
            }
        ],
        "findings": [
            {
                "specification_id": "LS-mode",
                "status": "violated",
                "evidence_ids": ["EV-PROBE-mode"],
                "assessment": "The copy drops mode.",
                "recommendation": "Preserve mode.",
            }
        ],
        "backlog": [],
    }
    command = (
        "GUARDIAN_PROBE_KEY=mode-copy-preserved "
        "GUARDIAN_SPECIFICATION_ID=LS-mode python /tmp/probe.py"
    )

    _, findings, uncertainty, probes, _, error = asyncio.run(
        check_patch(
            request,
            _one_round_config(),
            ScriptedExecutor(
                [
                    _run_with_probe(
                        response,
                        command=command,
                        output="mode missing",
                        exit_code=10,
                        tool_call_id="item_13",
                    )
                ]
            ),
            memory,
        )
    )

    assert error is None
    assert [item.specification_id for item in findings] == ["LS-mode"]
    assert not uncertainty
    assert probes[0].command == command
    assert probes[0].probe_outcome is ProbeOutcome.VIOLATED


def test_patch_check_repairs_probe_reference_without_rerunning_probe(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    memory = SpecificationMemory(specifications=(_record(),))
    response = {
        "summary": "The assertion falsified the specification.",
        "runtime_evidence": [
            {
                "id": "EV-PROBE-mode",
                "probe_key": "mode-copy-preserved",
                "tool_call_id": "item_symbolic",
                "specification_id": "LS-mode",
                "observation": "The focused copy assertion failed.",
            }
        ],
        "findings": [
            {
                "specification_id": "LS-mode",
                "status": "violated",
                "evidence_ids": ["EV-PROBE-mode"],
                "assessment": "The copy drops mode.",
                "recommendation": "Preserve mode.",
            }
        ],
        "backlog": [],
    }
    repaired = {
        **response,
        "runtime_evidence": [
            {**response["runtime_evidence"][0], "tool_call_id": "item_13"}
        ],
    }
    executor = ScriptedExecutor(
        [
            _run_with_probe(
                response,
                command="python /tmp/probe.py",
                output="mode missing",
                exit_code=10,
                tool_call_id="item_13",
            ),
            _run(repaired),
        ]
    )

    _, findings, uncertainty, probes, execution, error = asyncio.run(
        check_patch(request, _one_round_config(), executor, memory)
    )

    assert error is None
    assert [item.specification_id for item in findings] == ["LS-mode"]
    assert not uncertainty
    assert probes[0].probe_outcome is ProbeOutcome.VIOLATED
    assert len(execution.rollouts) == 2
    assert execution.metrics.to_dict() == {
        "declared": 1,
        "accepted": 1,
        "satisfied": 0,
        "violated": 1,
        "inconclusive": 0,
        "rejected": 0,
        "rejected_by_reason": {},
        "repaired": 1,
        "repair_attempted": True,
        "repair_accepted": True,
    }
    repair_request = executor.requests[1]
    assert repair_request.metadata["guardian_stage"] == "patch_check_repair"
    assert "Do not inspect the repository, run commands, call tools" in (
        repair_request.instruction
    )
    assert '"tool_call_id": "item_13"' in repair_request.instruction


def test_patch_check_discards_repair_that_uses_tools(tmp_path: Path) -> None:
    request = _request(tmp_path)
    memory = SpecificationMemory(specifications=(_record(),))
    response = {
        "summary": "An unlinked probe was claimed.",
        "runtime_evidence": [
            {
                "id": "EV-PROBE-mode",
                "probe_key": "mode-copy-preserved",
                "tool_call_id": "item_missing",
                "specification_id": "LS-mode",
                "observation": "No matching command exists.",
            }
        ],
        "findings": [
            {
                "specification_id": "LS-mode",
                "status": "violated",
                "evidence_ids": ["EV-PROBE-mode"],
                "assessment": "The copy allegedly drops mode.",
                "recommendation": "Preserve mode.",
            }
        ],
        "backlog": [],
    }
    executor = ScriptedExecutor([_run(response), _run(response, tool=True)])

    _, findings, uncertainty, probes, execution, error = asyncio.run(
        check_patch(request, _one_round_config(), executor, memory)
    )

    assert error is None
    assert not findings
    assert [item.specification_id for item in uncertainty] == ["LS-mode"]
    assert not probes
    assert execution.metrics.repair_attempted is True
    assert execution.metrics.repair_accepted is False
    assert execution.metrics.rejected_by_reason == (("unlinked_execution", 1),)


def test_probe_exit_code_overrides_model_violation_claim(tmp_path: Path) -> None:
    request = _request(tmp_path)
    task_evidence = replace(
        _typed_evidence(EvidenceSourceType.TASK, EvidenceAuthority.NORMATIVE),
        evidence_id="EV-task",
        supports=("LS-mode",),
    )
    memory = SpecificationMemory(
        specifications=(replace(_record(), supporting_evidence=("EV-task",)),),
        evidence=(task_evidence,),
    )
    response = {
        "summary": "The model incorrectly calls a passing probe a violation.",
        "runtime_evidence": [
            {
                "id": "EV-PROBE-mode",
                "probe_key": "mode-copy-preserved",
                "tool_call_id": "item_1",
                "specification_id": "LS-mode",
                "observation": "The focused copy assertion completed.",
            }
        ],
        "findings": [
            {
                "specification_id": "LS-mode",
                "status": "violated",
                "evidence_ids": ["EV-task", "EV-PROBE-mode"],
                "assessment": "The model misread the result.",
                "recommendation": "Change the copy path.",
            }
        ],
        "backlog": [],
    }

    _, findings, uncertainty, probes, _, error = asyncio.run(
        check_patch(
            request,
            _one_round_config(),
            ScriptedExecutor(
                [
                    _run_with_probe(
                        response,
                        command="python /tmp/probe_mode.py",
                        output="mode preserved",
                        exit_code=0,
                    )
                ]
            ),
            memory,
        )
    )

    assert error is None
    assert not findings
    assert not uncertainty
    assert probes[0].probe_outcome is ProbeOutcome.SATISFIED
    assert probes[0].supports == ("LS-mode",)


def test_non_protocol_probe_exit_is_inconclusive(tmp_path: Path) -> None:
    request = _request(tmp_path)
    memory = SpecificationMemory(specifications=(_record(),))
    response = {
        "summary": "The probe crashed operationally.",
        "runtime_evidence": [
            {
                "id": "EV-PROBE-mode",
                "probe_key": "mode-copy-preserved",
                "tool_call_id": "item_1",
                "specification_id": "LS-mode",
                "observation": "The focused copy assertion could not run.",
            }
        ],
        "findings": [
            {
                "specification_id": "LS-mode",
                "status": "violated",
                "evidence_ids": ["EV-PROBE-mode"],
                "assessment": "The probe failed to import a dependency.",
                "recommendation": "Investigate the environment.",
            }
        ],
        "backlog": [],
    }

    _, findings, uncertainty, probes, _, error = asyncio.run(
        check_patch(
            request,
            _one_round_config(),
            ScriptedExecutor(
                [
                    _run_with_probe(
                        response,
                        command="python /tmp/probe_mode.py",
                        output="ImportError: missing dependency",
                        exit_code=1,
                    )
                ]
            ),
            memory,
        )
    )

    assert error is None
    assert not findings
    assert [item.specification_id for item in uncertainty] == ["LS-mode"]
    assert probes[0].probe_outcome is ProbeOutcome.INCONCLUSIVE
    assert not probes[0].supports
    assert not probes[0].opposes


def test_reused_tool_call_invalidates_all_linked_probes(tmp_path: Path) -> None:
    request = _request(tmp_path)
    memory = SpecificationMemory(specifications=(_record(),))
    response = {
        "summary": "One compound command was incorrectly reported twice.",
        "runtime_evidence": [
            {
                "id": "EV-PROBE-mode-a",
                "probe_key": "mode-copy-preserved",
                "tool_call_id": "item_1",
                "specification_id": "LS-mode",
                "observation": "First interpretation.",
            },
            {
                "id": "EV-PROBE-mode-b",
                "probe_key": "mode-copy-reloaded",
                "tool_call_id": "item_1",
                "specification_id": "LS-mode",
                "observation": "Second interpretation.",
            },
        ],
        "findings": [
            {
                "specification_id": "LS-mode",
                "status": "violated",
                "evidence_ids": ["EV-PROBE-mode-a"],
                "assessment": "The compound command printed a failure.",
                "recommendation": "Preserve mode.",
            }
        ],
        "backlog": [],
    }

    _, findings, uncertainty, probes, _, error = asyncio.run(
        check_patch(
            request,
            _one_round_config(),
            ScriptedExecutor(
                [
                    _run_with_probe(
                        response,
                        command="python /tmp/compound_probe.py",
                        output="mixed results",
                        exit_code=10,
                    ),
                    _run(
                        {
                            **response,
                            "runtime_evidence": [],
                            "findings": [
                                {
                                    **response["findings"][0],
                                    "status": "uncertain",
                                    "evidence_ids": [],
                                }
                            ],
                        }
                    ),
                ]
            ),
            memory,
        )
    )

    assert error is None
    assert not findings
    assert [item.specification_id for item in uncertainty] == ["LS-mode"]
    assert not probes


def test_patch_checker_requires_fresh_directory_persistence_probe(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    memory = SpecificationMemory(
        specifications=(_record(),),
        evidence=(
            replace(
                _typed_evidence(EvidenceSourceType.TASK, EvidenceAuthority.NORMATIVE),
                supports=("LS-mode",),
            ),
        ),
    )
    executor = ScriptedExecutor([_run(_patch_check())])

    asyncio.run(check_patch(request, _one_round_config(), executor, memory))

    instruction = " ".join(executor.requests[0].instruction.lower().split())
    assert "smallest focused runtime probe" in instruction
    assert "fresh temporary working directory" in instruction
    assert "verify that the expected artifact is actually created" in instruction
    assert "return `uncertain`" in instruction
    assert "exit 0 only when that specification is satisfied" in instruction
    assert "exit 10 only when the specification is behaviorally violated" in instruction
    assert "do not reuse a command execution or probe key" in instruction
    assert "do not report an outcome, command, or output" in instruction
    assert "guardian_probe_key=<probe_key>" in instruction
    assert "guardian_specification_id=<specification_id>" in instruction


def test_patch_checker_probe_is_retained_in_cross_cycle_memory(tmp_path: Path) -> None:
    response = {
        "summary": "The focused contract probe fails.",
        "runtime_evidence": [
            {
                "id": "EV-PROBE-mode",
                "probe_key": "mode-copy-preserved",
                "tool_call_id": "item_1",
                "specification_id": "LS-mode",
                "observation": "A copied state omitted its configured mode.",
            }
        ],
        "findings": [
            {
                "specification_id": "LS-mode",
                "status": "violated",
                "patch_locations": ["contract.py:2"],
                "evidence_ids": ["EV-PROBE-mode"],
                "assessment": "The executable copy path drops mode.",
                "recommendation": "Preserve mode on the copy path.",
            }
        ],
        "backlog": [],
    }
    executor = ScriptedExecutor(
        [
            _run(_briefs(1)),
            _run(_exploration()),
            _run(_aggregation()),
            _run(_distillation()),
            _run_with_probe(
                response,
                command="python /tmp/probe_mode.py",
                output="mode key missing; exit=1",
                exit_code=10,
            ),
        ]
    )

    result = asyncio.run(
        GuardianAgent(_one_round_config(), executor=executor).review(_request(tmp_path))
    )

    probe = next(
        item
        for item in result.memory.evidence
        if item.source_type is EvidenceSourceType.RUNTIME_PROBE
    )
    record = next(
        item
        for item in result.memory.specifications
        if item.specification_id == "LS-mode"
    )
    assert probe.evidence_id in record.counterevidence
    assert probe.snapshot == result.candidate_commit
    assert probe.probe_key == "mode-copy-preserved"
    assert probe.probe_outcome is ProbeOutcome.VIOLATED
    assert probe.exit_code == 10
    persisted = json.loads((result.artifact_dir / "memory.json").read_text())
    persisted_probe = next(
        item for item in persisted["evidence"] if item["source_type"] == "runtime_probe"
    )
    assert persisted_probe["command"] == "python /tmp/probe_mode.py"
    assert persisted_probe["output"] == "mode key missing; exit=1"
    assert persisted_probe["probe_key"] == "mode-copy-preserved"
    assert persisted_probe["probe_specification_id"] == "LS-mode"
    assert persisted_probe["probe_outcome"] == "violated"
    assert persisted_probe["exit_code"] == 10


def test_current_failed_probe_survives_satisfied_patch_checker_verdict(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    task_evidence = replace(
        _typed_evidence(EvidenceSourceType.TASK, EvidenceAuthority.NORMATIVE),
        evidence_id="EV-task",
        supports=("LS-mode",),
    )
    failed_probe = Evidence(
        evidence_id="EV-failed-probe",
        path="contract.py",
        line_start=1,
        line_end=2,
        description="The focused mode-preservation test failed.",
        source_type=EvidenceSourceType.RUNTIME_PROBE,
        authority=EvidenceAuthority.BEHAVIORAL,
        probe_key="mode-copy-preserved",
        probe_specification_id="LS-mode",
        probe_outcome=ProbeOutcome.VIOLATED,
        exit_code=10,
        command="pytest -q test_mode.py",
        output="1 failed",
        opposes=("LS-mode",),
        snapshot=request.candidate_commit,
    )
    record = replace(
        _record(),
        supporting_evidence=("EV-task",),
        counterevidence=("EV-failed-probe",),
    )
    memory = SpecificationMemory(
        specifications=(record,), evidence=(task_evidence, failed_probe)
    )
    response = {
        "summary": "The patch looks correct.",
        "findings": [
            {
                "specification_id": "LS-mode",
                "status": "satisfied",
                "evidence_ids": ["EV-task"],
                "assessment": "Code inspection appears correct.",
                "recommendation": "",
            }
        ],
        "backlog": [],
    }

    _, findings, _, _, _, error = asyncio.run(
        check_patch(
            request,
            _one_round_config(),
            ScriptedExecutor([_run(response)]),
            memory,
        )
    )

    assert error is None
    assert [item.specification_id for item in findings] == ["LS-mode"]
    assert findings[0].evidence_ids == ("EV-failed-probe",)
    assert "no later successful run" in findings[0].patch_assessment


def test_later_successful_run_of_same_probe_clears_failed_probe(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    task_evidence = replace(
        _typed_evidence(EvidenceSourceType.TASK, EvidenceAuthority.NORMATIVE),
        evidence_id="EV-task",
        supports=("LS-mode",),
    )
    failed_probe = Evidence(
        evidence_id="EV-failed-probe",
        path="contract.py",
        line_start=1,
        line_end=2,
        description="The focused mode-preservation test failed.",
        source_type=EvidenceSourceType.RUNTIME_PROBE,
        authority=EvidenceAuthority.BEHAVIORAL,
        probe_key="mode-copy-preserved",
        probe_specification_id="LS-mode",
        probe_outcome=ProbeOutcome.VIOLATED,
        exit_code=10,
        command="pytest -q test_mode.py",
        output="1 failed",
        opposes=("LS-mode",),
        snapshot=request.candidate_commit,
        round=1,
    )
    successful_probe = replace(
        failed_probe,
        evidence_id="EV-successful-probe",
        description="The focused mode-preservation test passed.",
        output="1 passed",
        supports=("LS-mode",),
        opposes=(),
        probe_outcome=ProbeOutcome.SATISFIED,
        exit_code=0,
        round=2,
    )
    record = replace(
        _record(),
        supporting_evidence=("EV-task", "EV-successful-probe"),
        counterevidence=("EV-failed-probe",),
    )
    memory = SpecificationMemory(
        specifications=(record,),
        evidence=(task_evidence, failed_probe, successful_probe),
    )
    response = {
        "summary": "The corrected patch satisfies the requirement.",
        "findings": [
            {
                "specification_id": "LS-mode",
                "status": "satisfied",
                "evidence_ids": ["EV-task", "EV-successful-probe"],
                "assessment": "The matching probe now passes.",
                "recommendation": "",
            }
        ],
        "backlog": [],
    }

    _, findings, _, _, _, error = asyncio.run(
        check_patch(
            request,
            _one_round_config(),
            ScriptedExecutor([_run(response)]),
            memory,
        )
    )

    assert error is None
    assert not findings


def test_current_successful_rerun_clears_retained_failed_probe(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    failed_probe = Evidence(
        evidence_id="EV-failed-probe",
        path="runtime-probe:item-old",
        line_start=1,
        line_end=1,
        description="The focused mode-preservation test failed.",
        source_type=EvidenceSourceType.RUNTIME_PROBE,
        authority=EvidenceAuthority.BEHAVIORAL,
        command="python /tmp/probe_mode.py",
        output="mode missing",
        probe_key="mode-copy-preserved",
        probe_specification_id="LS-mode",
        probe_outcome=ProbeOutcome.VIOLATED,
        exit_code=10,
        opposes=("LS-mode",),
        snapshot=request.candidate_commit,
        round=1,
    )
    record = replace(
        _record(),
        supporting_evidence=(),
        counterevidence=("EV-failed-probe",),
    )
    memory = SpecificationMemory(specifications=(record,), evidence=(failed_probe,))
    response = {
        "summary": "The corrected path now preserves mode.",
        "runtime_evidence": [
            {
                "id": "EV-PROBE-mode",
                "probe_key": "mode-copy-preserved",
                "tool_call_id": "item_1",
                "specification_id": "LS-mode",
                "observation": "The same logical assertion now passes.",
            }
        ],
        "findings": [
            {
                "specification_id": "LS-mode",
                "status": "satisfied",
                "evidence_ids": ["EV-PROBE-mode"],
                "assessment": "The focused rerun passes.",
                "recommendation": "",
            }
        ],
        "backlog": [],
    }

    _, findings, uncertainty, probes, _, error = asyncio.run(
        check_patch(
            request,
            _one_round_config(),
            ScriptedExecutor(
                [
                    _run_with_probe(
                        response,
                        command="python /tmp/probe_mode.py",
                        output="mode preserved",
                        exit_code=0,
                    )
                ]
            ),
            memory,
        )
    )

    assert error is None
    assert not findings
    assert not uncertainty
    assert probes[0].round == 2
    assert probes[0].probe_outcome is ProbeOutcome.SATISFIED


def test_all_model_and_tool_usage_is_accounted(tmp_path: Path) -> None:
    result = asyncio.run(
        GuardianAgent(
            _one_round_config(), executor=_one_round_executor(tool=True)
        ).review(_request(tmp_path))
    )
    assert result.usage.input_tokens == 500
    assert result.usage.output_tokens == 100
    usage = json.loads((result.artifact_dir / "usage.json").read_text())
    assert sum(item["tool_calls"] for item in usage["stages"]) == 5
    assert {item["stage"] for item in usage["stages"]} == {
        "planning",
        "exploration",
        "aggregation",
        "distillation",
        "patch_check",
    }


def test_usage_artifact_excludes_prior_review_stage_usage(tmp_path: Path) -> None:
    request = _request(tmp_path)
    previous = StageUsage(
        stage="prior_review",
        round=1,
        model="old",
        usage=TokenUsage(input_tokens=999, output_tokens=1),
    )
    request = replace(
        request,
        memory=SpecificationMemory(stage_usage=(previous,)),
        artifact_dir=tmp_path / "second-artifacts",
    )

    result = asyncio.run(
        GuardianAgent(
            _one_round_config(), executor=_one_round_executor(tool=True)
        ).review(request)
    )

    usage = json.loads((result.artifact_dir / "usage.json").read_text())
    assert len(usage["stages"]) == 5
    assert "prior_review" not in {item["stage"] for item in usage["stages"]}
    assert usage["total"]["input_tokens"] == 500


def test_canonical_repository_remains_unchanged(tmp_path: Path) -> None:
    request = _request(tmp_path)
    before = _git(request.workspace, "status", "--porcelain=v1")
    result = asyncio.run(
        GuardianAgent(_one_round_config(), executor=_one_round_executor()).review(
            request
        )
    )
    assert result.status is ReviewStatus.COMPLETE
    assert _git(request.workspace, "status", "--porcelain=v1") == before
    assert _git(request.workspace, "rev-parse", "HEAD") == request.candidate_commit
