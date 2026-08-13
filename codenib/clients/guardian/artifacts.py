# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Machine-readable artifacts and concise human-readable Guardian reports."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .memory import evidence_to_dict, memory_to_dict, specification_to_dict
from .types import (
    CandidateSpecification,
    ExplorerOutput,
    GuardianConfig,
    GuardianFinding,
    GuardianRequest,
    GuardianResult,
    InvestigationBrief,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _brief(value: InvestigationBrief) -> dict[str, object]:
    return {
        "id": value.brief_id,
        "focus_question": value.focus_question,
        "target_surface": list(value.target_surface),
        "evidence_to_seek": list(value.evidence_to_seek),
        "avoid_duplicating": list(value.avoid_duplicating),
        "linked_specifications": list(value.linked_specifications),
        "open_ended": value.open_ended,
    }


def _candidate(value: CandidateSpecification) -> dict[str, object]:
    return {
        "candidate_id": value.candidate_id,
        "statement": value.statement,
        "condition": value.condition,
        "scope": list(value.scope),
        "supporting_evidence": [
            evidence_to_dict(item) for item in value.supporting_evidence
        ],
        "counterevidence": [evidence_to_dict(item) for item in value.counterevidence],
        "suggested_status": value.suggested_status.value,
        "unresolved_questions": list(value.unresolved_questions),
        "explorer": value.explorer,
        "round": value.round,
        "brief_id": value.brief_id,
    }


def _explorer(value: ExplorerOutput) -> dict[str, object]:
    return {
        "explorer": value.explorer,
        "round": value.round,
        "brief_id": value.brief_id,
        "candidate_specifications": [_candidate(item) for item in value.candidates],
        "exploration_trace": {
            "searched_locations": list(value.trace.searched_locations),
            "actions_and_outcomes": list(value.trace.actions_and_outcomes),
            "unsuccessful_routes": list(value.trace.unsuccessful_routes),
            "remaining_questions": list(value.trace.remaining_questions),
            "suggested_next_actions": list(value.trace.suggested_next_actions),
        },
        "error": value.error,
    }


def _finding(value: GuardianFinding) -> dict[str, object]:
    return {
        "specification_id": value.specification_id,
        "status": value.status.value,
        "statement": value.statement,
        "condition": value.condition,
        "patch_locations": list(value.patch_locations),
        "evidence_ids": list(value.evidence_ids),
        "assessment": value.patch_assessment,
        "recommendation": value.recommendation,
    }


def render_markdown(result: GuardianResult) -> str:
    """Render only findings, concise uncertainty, usage, and artifact identifiers."""

    lines = [
        "# Repository Guardian Report",
        "",
        f"Candidate: `{result.candidate_commit}`",
        f"Analysis status: `{result.status.value}`",
        "",
    ]
    if result.findings:
        for index, finding in enumerate(result.findings, 1):
            lines.extend(
                (
                    f"## {index}. {finding.statement}",
                    "",
                    f"Specification: `{finding.specification_id}`",
                    "",
                    finding.patch_assessment,
                    "",
                    f"Recommendation: {finding.recommendation}",
                    "",
                    "Evidence: "
                    + ", ".join(f"`{item}`" for item in finding.evidence_ids),
                    "",
                )
            )
    else:
        lines.extend(("No definite evidence-backed patch violation was found.", ""))
    if result.uncertain_specifications:
        lines.extend(("## Uncertainty / backlog", ""))
        for item in result.uncertain_specifications:
            lines.extend(
                (
                    f"- `{item.specification_id}` — {item.patch_assessment} "
                    f"Next verification: {item.recommendation}",
                    "",
                )
            )
    lines.extend(
        (
            "## Inference usage",
            "",
            f"Total: {result.usage.total_tokens} tokens "
            f"({result.usage.input_tokens} input, {result.usage.output_tokens} output, "
            f"{result.usage.cached_input_tokens} cached input).",
            "",
            f"Supporting artifacts: `{result.artifact_dir}`",
        )
    )
    return "\n".join(lines).rstrip() + "\n"


def persist_artifacts(
    request: GuardianRequest, config: GuardianConfig, result: GuardianResult
) -> None:
    root = Path(result.artifact_dir or request.artifact_dir)
    current_stage_usage = (
        result.memory.stage_usage[-len(result.rollouts) :] if result.rollouts else ()
    )
    _write_json(
        root / "request.json",
        {
            "workspace": str(request.workspace),
            "base_revision": request.base_commit,
            "candidate_revision": request.candidate_commit,
            "candidate_patch": request.change_patch,
            "task_context": [
                {
                    "id": item.context_id,
                    "content": item.content,
                    "source": item.source.value,
                    "fidelity": item.fidelity.value,
                }
                for item in request.task_context
            ],
            "previous_guardian_session_identifier": request.previous_session_id,
        },
    )
    _write_json(
        root / "config.json",
        {
            "explorer_model": config.explorer_model,
            "aggregator_model": config.aggregator_model,
            "maximum_rounds": config.max_rounds,
            "round_1_explorers": config.explorer_count,
            "later_round_explorers": config.targeted_explorer_count,
            "maximum_findings": config.max_findings,
            "maximum_specifications_per_explorer": config.max_specifications_per_explorer,
        },
    )
    _write_json(root / "memory.json", memory_to_dict(result.memory))
    for round_record in result.rounds:
        directory = root / f"round-{round_record.round:02d}"
        _write_json(
            directory / "briefs.json",
            {"briefs": [_brief(item) for item in round_record.briefs]},
        )
        for index, output in enumerate(round_record.explorer_outputs, 1):
            _write_json(directory / f"explorer-{index:02d}.json", _explorer(output))
        _write_json(
            directory / "aggregation.json",
            {
                "summary": round_record.aggregation_summary,
                "specifications": [
                    specification_to_dict(item) for item in round_record.specifications
                ],
                "errors": list(round_record.errors),
            },
        )
        _write_json(
            directory / "distillation.json",
            {
                "experiences": [
                    {
                        "id": item.experience_id,
                        "brief_id": item.brief_id,
                        "searched_locations": list(item.searched_locations),
                        "actions_and_outcomes": list(item.actions_and_outcomes),
                        "unsuccessful_routes": list(item.unsuccessful_routes),
                        "unresolved_questions": list(item.unresolved_questions),
                        "suggested_next_actions": list(item.suggested_next_actions),
                        "linked_specifications": list(item.linked_specifications),
                    }
                    for item in round_record.experiences
                ]
            },
        )
        _write_json(
            directory / "frontier.json",
            {
                "specification_frontier": list(round_record.frontier),
                "new_information": round_record.new_information,
            },
        )
    _write_json(
        root / "patch-check.json",
        {
            "summary": result.summary,
            "findings": [_finding(item) for item in result.findings],
            "backlog": [_finding(item) for item in result.uncertain_specifications],
            "probe_metrics": result.probe_metrics.to_dict(),
        },
    )
    _write_json(root / "final-report.json", result.to_dict())
    _write_json(
        root / "usage.json",
        {
            "stages": [
                {
                    "stage": item.stage,
                    "round": item.round,
                    "model": item.model,
                    "input_tokens": item.usage.input_tokens,
                    "cached_input_tokens": item.usage.cached_input_tokens,
                    "cache_write_input_tokens": item.usage.cache_write_input_tokens,
                    "output_tokens": item.usage.output_tokens,
                    "reasoning_output_tokens": item.usage.reasoning_output_tokens,
                    "total_tokens": item.usage.total_tokens,
                    "tool_calls": item.tool_calls,
                }
                for item in current_stage_usage
            ],
            "total": result.to_dict()["usage"],
        },
    )
    (root / "final-report.md").write_text(render_markdown(result), encoding="utf-8")


__all__ = ["persist_artifacts", "render_markdown"]
