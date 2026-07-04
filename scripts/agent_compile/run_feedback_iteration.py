#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Run the feedback-probe promotion handoff.

This script is the thin orchestration layer after a small feedback sweep. It
aggregates cells with a named promotion profile, reads ``promotion_decision``,
prints that decision as JSON, writes a next-action handoff plan, and exits with
a stable promote/revise code.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import shlex
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.agent_compile import aggregate_feedback_probe  # noqa: E402

PROMOTE_EXIT = 0
REVISE_EXIT = 2
MISSING_DECISION_EXIT = 3
AGGREGATE_ERROR_EXIT = 4
HANDOFF_NOT_READY_EXIT = 5
HANDOFF_REJECTED_EXIT = 6
HANDOFF_FILENAME = "feedback_iteration_handoff.json"
FOLLOWUP_FILENAME = "feedback_iteration_followup.json"
FOLLOWUP_STDOUT_FILENAME = "feedback_iteration_followup.stdout"
FOLLOWUP_STDERR_FILENAME = "feedback_iteration_followup.stderr"
NEXT_PLAN_FILENAME = "feedback_iteration_next_plan.json"
DERIVED_CONFIG_FILENAME = "feedback_iteration_derived_config.yaml"
NEXT_PLAN_EXECUTION_FILENAME = "feedback_iteration_next_plan_execution.json"
NEXT_PLAN_EXECUTION_STDOUT_FILENAME = "feedback_iteration_next_plan_execution.stdout"
NEXT_PLAN_EXECUTION_STDERR_FILENAME = "feedback_iteration_next_plan_execution.stderr"
NEXT_PLAN_EXECUTION_SERIES_FILENAME = (
    "feedback_iteration_next_plan_execution_series.json"
)
NEXT_PLAN_EXECUTION_SERIES_STDOUT_TEMPLATE = (
    "feedback_iteration_next_plan_execution.command{index}.stdout"
)
NEXT_PLAN_EXECUTION_SERIES_STDERR_TEMPLATE = (
    "feedback_iteration_next_plan_execution.command{index}.stderr"
)
_PLACEHOLDER_RE = re.compile(r"<[a-z-]+>")
_ALLOWED_FOLLOWUP_SCRIPTS = {
    (_PROJECT_ROOT / "scripts/agent_compile/aggregate_feedback_probe.py").resolve(),
    (
        _PROJECT_ROOT / "scripts/agent_compile/aggregate_external_agent_baseline.py"
    ).resolve(),
    (_PROJECT_ROOT / "scripts/agent_compile/plan_feedback_suite.py").resolve(),
    (_PROJECT_ROOT / "scripts/agent_compile/run_external_agent_rerun.py").resolve(),
    (_PROJECT_ROOT / "scripts/agent_compile/run_feedback_iteration.py").resolve(),
    (_PROJECT_ROOT / "scripts/agent_compile/summarize_trace.py").resolve(),
}
_ALLOWED_NEXT_PLAN_SCRIPTS = {
    *_ALLOWED_FOLLOWUP_SCRIPTS,
    (_PROJECT_ROOT / "scripts/agent_compile/run_synthesis_sweep.py").resolve(),
}
_FAILED_ACTION_NEXT_ACTION = {
    "complete_replay_artifacts": "complete_replay_artifacts",
    "repair_answer_evidence": "repair_answer_evidence",
    "restore_answer_recall": "restore_answer_recall",
    "restore_paired_baseline_coverage": "rerun_missing_paired_cells",
    "reduce_tokens_or_turns": "tune_compact_policy",
    "revise_route_lifecycle": "repair_route_lifecycle",
    "repair_tool_envelopes": "repair_tool_envelope_contract",
    "revise_harness_slice": "inspect_profile_blockers",
    "run_with_promotion_profile": "rerun_with_promotion_profile",
    "promote_harness_slice": "inspect_promotion_regression",
}
_ANSWER_DIAGNOSIS_CONTRACTS: Dict[str, Dict[str, str]] = {
    "tighten_answer_ordering": {
        "repair_surface": "answer_ordering",
        "expected_effect": (
            "Move already-found target spans into the strict top-k Locations list."
        ),
    },
    "normalize_repo_relative_paths": {
        "repair_surface": "path_normalization",
        "expected_effect": (
            "Emit strict repo-relative paths instead of basename or "
            "alias-equivalent paths."
        ),
    },
    "strengthen_schema_repair": {
        "repair_surface": "answer_schema",
        "expected_effect": (
            "Recover parseable Files/Symbols/Locations output when the model "
            "answer is empty or malformed."
        ),
    },
    "improve_context_routing": {
        "repair_surface": "context_routing",
        "expected_effect": (
            "Offer stronger read-confirmed context before final answer synthesis."
        ),
    },
    "keep_answer_contract": {
        "repair_surface": "gate_recheck",
        "expected_effect": (
            "The answer diagnosis is already clean; inspect the failing gate "
            "artifact directly."
        ),
    },
    "no_answer_diagnosis": {
        "repair_surface": "diagnosis_collection",
        "expected_effect": "Collect answer diagnosis fields before choosing a repair lane.",
    },
}
_EXTERNAL_DIAGNOSIS_CONTRACTS: Dict[str, Dict[str, str]] = {
    "rank_gap": {
        "action": "tighten_external_answer_ordering",
        "repair_surface": "external_answer_ordering",
        "expected_effect": (
            "Keep correct external-agent spans inside the strict top-k "
            "Locations list."
        ),
    },
    "path_alias_gap": {
        "action": "tighten_external_path_contract",
        "repair_surface": "external_path_normalization",
        "expected_effect": (
            "Require repo-relative external-agent paths instead of basename aliases."
        ),
    },
    "format_gap": {
        "action": "tighten_external_schema_contract",
        "repair_surface": "external_answer_schema",
        "expected_effect": "Require a parseable Files/Symbols/Locations answer contract.",
    },
    "empty_answer": {
        "action": "tighten_external_schema_contract",
        "repair_surface": "external_answer_schema",
        "expected_effect": "Require a non-empty parseable external-agent answer.",
    },
    "content_gap": {
        "action": "compare_external_context_routing",
        "repair_surface": "external_context_routing",
        "expected_effect": (
            "Compare whether the external agent reached equivalent route/read "
            "evidence."
        ),
    },
    "ok": {
        "action": "inspect_external_gate_criteria",
        "repair_surface": "external_gate_policy",
        "expected_effect": "The answer diagnosis is clean; inspect non-answer gate blockers.",
    },
}
_EXTERNAL_BLOCKER_CONTRACTS: Dict[str, Dict[str, str]] = {
    "missing_run": {
        "action": "capture_missing_external_run",
        "repair_surface": "external_run_capture",
        "expected_effect": "Capture the missing external-agent JSONL run before comparing.",
    },
    "rec5_below_gate": {
        "action": "restore_external_answer_recall",
        "repair_surface": "external_answer_recall",
        "expected_effect": (
            "Raise the external run's strict answer_blocks@5 recall to the "
            "configured gate."
        ),
    },
    "mcp_cancelled_above_gate": {
        "action": "fix_external_mcp_policy",
        "repair_surface": "external_mcp_policy",
        "expected_effect": (
            "Run with a policy mode where CodeMiner MCP calls complete instead "
            "of being cancelled."
        ),
    },
    "stderr_internal_errors_above_gate": {
        "action": "inspect_external_mcp_transport_noise",
        "repair_surface": "external_mcp_transport",
        "expected_effect": (
            "Reduce or classify MCP stderr Internal Server Error noise for the "
            "external run."
        ),
    },
    "missing_lsp_route_tool": {
        "action": "surface_lsp_route_to_external_agent",
        "repair_surface": "external_graph_mcp_adoption",
        "expected_effect": (
            "Make the external agent call CodeMiner lsp_route when the "
            "comparison requires it."
        ),
    },
    "missing_native_lsp_surface": {
        "action": "attach_native_ide_lsp_surface",
        "repair_surface": "external_native_lsp_environment",
        "expected_effect": (
            "Run the external agent in an environment exposing native IDE/LSP tools."
        ),
    },
}

_ACTION_STEPS: Dict[str, Dict[str, Any]] = {
    "complete_replay_artifacts": {
        "reason": (
            "Replay readiness failed; complete persisted messages, typed tool "
            "results, and LLM-call indexes before judging harness behavior."
        ),
        "artifacts": [
            "replay_readiness_gate.json",
            "feedback_iteration_aggregate.stderr",
        ],
        "commands": [
            "python scripts/agent_compile/summarize_trace.py "
            "<cell-json> --format replay-markdown --require-exact-replay"
        ],
    },
    "repair_answer_evidence": {
        "reason": (
            "Committed answer spans are not fully backed by trace or read "
            "evidence, so the answer may be right for the wrong reason."
        ),
        "artifacts": [
            "answer_evidence_gate.json",
            "answer_diagnosis_plan.json",
        ],
        "commands": [
            "python scripts/agent_compile/aggregate_feedback_probe.py "
            "--cells-dir <cells-dir> --output-dir <output-dir> "
            "--require-answer-evidence <arm>"
        ],
    },
    "restore_answer_recall": {
        "reason": (
            "The promoted arm regressed strict answer recall beyond the "
            "feedback gate tolerance."
        ),
        "artifacts": [
            "feedback_iteration_gate.json",
            "answer_diagnosis_plan.json",
        ],
        "commands": [
            "python scripts/agent_compile/aggregate_feedback_probe.py "
            "--cells-dir <cells-dir> --output-dir <output-dir> "
            "--require-feedback-gate <arm>"
        ],
    },
    "restore_paired_baseline_coverage": {
        "reason": (
            "The gate is missing paired baseline/profile rows, so deltas are "
            "not comparable yet."
        ),
        "artifacts": [
            "feedback_iteration_gate.json",
            "feedback_iteration_aggregate.stderr",
        ],
        "commands": [
            "python scripts/agent_compile/run_feedback_iteration.py "
            "--output-dir <output-dir> --promotion-profile <profile>"
        ],
    },
    "reduce_tokens_or_turns": {
        "reason": (
            "Recall held, but the compact arm did not save tokens or turns "
            "against the paired baseline."
        ),
        "artifacts": [
            "feedback_iteration_gate.json",
            "compact_policy_plan.json",
        ],
        "commands": [
            "python scripts/agent_compile/aggregate_feedback_probe.py "
            "--cells-dir <cells-dir> --output-dir <output-dir> "
            "--require-feedback-gate <arm> --feedback-gate-require-savings"
        ],
    },
    "revise_route_lifecycle": {
        "reason": (
            "The scheduled route lifecycle is incomplete or stale, so LSP/graph "
            "routing cannot be promoted yet."
        ),
        "artifacts": [
            "route_lifecycle_gate.json",
            "route_lifecycle_preflight.json",
        ],
        "commands": [
            "python scripts/agent_compile/plan_feedback_suite.py "
            "--suite-file "
            "scripts/agent_compile/feedback_suites/haiku_static_lsp_route_q2.yaml "
            "--output-dir <output-dir> --write-plan",
            "python scripts/agent_compile/aggregate_feedback_probe.py "
            "--cells-dir <cells-dir> --output-dir <output-dir> "
            "--promotion-profile route_lifecycle",
        ],
    },
    "repair_tool_envelopes": {
        "reason": (
            "Tool calls are missing schema-v2 envelopes or have typed errors, "
            "skips, or truncation."
        ),
        "artifacts": [
            "tool_envelope_plan.json",
            "feedback_iteration_aggregate.stderr",
        ],
        "commands": [
            "python scripts/agent_compile/aggregate_feedback_probe.py "
            "--cells-dir <cells-dir> --output-dir <output-dir> "
            "--require-tool-envelope-health"
        ],
    },
    "revise_harness_slice": {
        "reason": "The profile failed with blockers that do not map to a narrow repair.",
        "artifacts": [
            "promotion_profile_gate.json",
            "promotion_decision.json",
        ],
        "commands": [
            "python scripts/agent_compile/run_feedback_iteration.py "
            "--output-dir <output-dir> --promotion-profile <profile>"
        ],
    },
    "run_with_promotion_profile": {
        "reason": "No promotion profile was requested, so no promote/revise gate ran.",
        "artifacts": ["promotion_decision.json"],
        "commands": [
            "python scripts/agent_compile/run_feedback_iteration.py "
            "--output-dir <output-dir> --promotion-profile haiku_compact"
        ],
    },
    "promote_harness_slice": {
        "reason": "All requested promotion profiles passed.",
        "artifacts": [
            "promotion_decision.json",
            "promotion_profile_gate.json",
            "feedback_iteration_gate.json",
            "answer_evidence_gate.json",
            "replay_readiness_gate.json",
        ],
        "commands": [
            "python scripts/agent_compile/run_feedback_iteration.py "
            "--output-dir <output-dir> --promotion-profile <profile>"
        ],
    },
}


def _load_decision(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"promotion decision missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"promotion decision is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"promotion decision must be a JSON object: {path}")
    return payload


def _load_json_object(path: Path, *, label: str) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"{label} missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} must be a JSON object: {path}")
    return payload


def _load_yaml_mapping(path: Path, *, label: str) -> Dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise RuntimeError(f"{label} missing: {path}") from exc
    except yaml.YAMLError as exc:
        raise RuntimeError(f"{label} is not valid YAML: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} must be a YAML mapping: {path}")
    return payload


def _exit_for_decision(decision: Dict[str, Any]) -> int:
    if decision.get("decision") == "promote" and decision.get("promote") is True:
        return PROMOTE_EXIT
    return REVISE_EXIT


def _artifact_paths(
    artifacts: Sequence[str], artifact_dir: Optional[Path]
) -> Dict[str, str]:
    if artifact_dir is None:
        return {name: name for name in artifacts}
    return {name: str(artifact_dir / name) for name in artifacts}


def _profiles_from_decision(decision: Dict[str, Any]) -> List[str]:
    profiles = [str(profile) for profile in decision.get("profiles") or []]
    return profiles or ["haiku_compact"]


def _target_arm_for_action(decision: Dict[str, Any], action: str) -> Optional[str]:
    profiles = _profiles_from_decision(decision)
    arm_fields: Dict[str, Sequence[str]] = {
        "complete_replay_artifacts": ("replay_arms",),
        "repair_answer_evidence": ("answer_evidence_arms",),
        "restore_answer_recall": ("feedback_arms",),
        "reduce_tokens_or_turns": ("feedback_arms",),
    }
    if action == "revise_route_lifecycle":
        return str(aggregate_feedback_probe.ROUTE_LIFECYCLE_ARM)
    for profile_name in profiles:
        profile = aggregate_feedback_probe.PROMOTION_PROFILES.get(profile_name) or {}
        for field in arm_fields.get(action, ()):
            arms = profile.get(field) or []
            if arms:
                return str(arms[0])
    return None


def _first_cell_for_arm(
    cells_dir: Optional[Path], arm: Optional[str]
) -> Optional[Path]:
    if cells_dir is None or arm is None or not cells_dir.exists():
        return None
    for path in sorted(cells_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(payload.get("subset_id") or "") == arm:
            return path
    return None


def _shell_arg(value: str) -> str:
    return shlex.quote(value)


def _resolve_command(
    command: str,
    *,
    decision: Dict[str, Any],
    action: str,
    artifact_dir: Optional[Path],
    cells_dir: Optional[Path],
) -> str:
    profiles = _profiles_from_decision(decision)
    profile = profiles[0] if profiles else "haiku_compact"
    arm = _target_arm_for_action(decision, action)
    cell = _first_cell_for_arm(cells_dir, arm)
    replacements = {
        "<profile>": _shell_arg(profile),
        "<arm>": _shell_arg(arm) if arm else "<arm>",
        "<output-dir>": (
            _shell_arg(str(artifact_dir)) if artifact_dir else "<output-dir>"
        ),
        "<cells-dir>": _shell_arg(str(cells_dir)) if cells_dir else "<cells-dir>",
        "<cell-json>": _shell_arg(str(cell)) if cell else "<cell-json>",
    }
    resolved = command
    for placeholder, value in replacements.items():
        resolved = resolved.replace(placeholder, value)
    return resolved


def _unresolved_placeholders(commands: Sequence[str]) -> List[str]:
    unresolved = []
    for command in commands:
        unresolved.extend(_PLACEHOLDER_RE.findall(command))
    return sorted(set(unresolved))


def _recommended_step(
    action: str,
    *,
    decision: Dict[str, Any],
    priority: int,
    artifact_dir: Optional[Path],
    cells_dir: Optional[Path],
) -> Dict[str, Any]:
    template = _ACTION_STEPS.get(action, _ACTION_STEPS["revise_harness_slice"])
    artifacts = [str(item) for item in template.get("artifacts") or []]
    commands = [str(command) for command in template.get("commands") or []]
    resolved_commands = [
        _resolve_command(
            command,
            decision=decision,
            action=action,
            artifact_dir=artifact_dir,
            cells_dir=cells_dir,
        )
        for command in commands
    ]
    unresolved = _unresolved_placeholders(resolved_commands)
    return {
        "priority": priority,
        "action": action,
        "target_arm": _target_arm_for_action(decision, action),
        "reason": str(template.get("reason") or ""),
        "artifacts": artifacts,
        "artifact_paths": _artifact_paths(artifacts, artifact_dir),
        "commands": commands,
        "resolved_commands": resolved_commands,
        "unresolved_placeholders": unresolved,
        "ready_to_run": bool(resolved_commands) and not unresolved,
    }


def _handoff_plan(
    decision: Dict[str, Any],
    *,
    artifact_dir: Optional[Path],
    cells_dir: Optional[Path],
) -> Dict[str, Any]:
    decision_action = str(decision.get("action") or "revise_harness_slice")
    next_actions = [str(action) for action in decision.get("next_actions") or []]
    actions = next_actions or [decision_action]
    recommended_steps = [
        _recommended_step(
            action,
            decision=decision,
            priority=index + 1,
            artifact_dir=artifact_dir,
            cells_dir=cells_dir,
        )
        for index, action in enumerate(actions)
    ]
    primary_step = recommended_steps[0] if recommended_steps else None
    next_command = None
    if primary_step:
        resolved_commands = primary_step.get("resolved_commands") or []
        next_command = resolved_commands[0] if resolved_commands else None
    decision_artifact = ["promotion_decision.json"]
    return {
        "handoff_version": 1,
        "decision": decision.get("decision"),
        "promote": decision.get("promote") is True,
        "primary_action": decision_action,
        "primary_step": primary_step,
        "next_command": next_command,
        "next_command_ready": bool(primary_step and primary_step.get("ready_to_run")),
        "next_actions": next_actions,
        "recommended_steps": recommended_steps,
        "blockers": [str(blocker) for blocker in decision.get("blockers") or []],
        "profiles": _profiles_from_decision(decision),
        "artifact_dir": str(artifact_dir) if artifact_dir is not None else None,
        "cells_dir": str(cells_dir) if cells_dir is not None else None,
        "artifact_paths": _artifact_paths(decision_artifact, artifact_dir),
    }


def _write_handoff(
    *,
    decision: Dict[str, Any],
    handoff_file: Optional[Path],
    artifact_dir: Optional[Path],
    cells_dir: Optional[Path],
) -> None:
    if handoff_file is None:
        return
    handoff_file.parent.mkdir(parents=True, exist_ok=True)
    plan = _handoff_plan(decision, artifact_dir=artifact_dir, cells_dir=cells_dir)
    handoff_file.write_text(
        json.dumps(plan, indent=2, sort_keys=True), encoding="utf-8"
    )


def _safe_python_script_argv(
    command: str,
    *,
    allowed_scripts: Sequence[Path],
) -> List[str]:
    if _unresolved_placeholders([command]):
        raise RuntimeError("next command still has unresolved placeholders")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise RuntimeError(f"next command is not shell-parseable: {exc}") from exc
    if len(argv) < 2:
        raise RuntimeError("next command must invoke an allowlisted Python script")
    executable = Path(argv[0]).name
    if executable not in {"python", "python3", Path(sys.executable).name}:
        raise RuntimeError("next command executable is not an allowlisted Python")
    script = Path(argv[1])
    if not script.is_absolute():
        script = (_PROJECT_ROOT / script).resolve()
    else:
        script = script.resolve()
    if script not in set(allowed_scripts):
        raise RuntimeError(f"next command script is not allowlisted: {script}")
    return [sys.executable, str(script), *argv[2:]]


def _safe_followup_argv(command: str) -> List[str]:
    return _safe_python_script_argv(
        command,
        allowed_scripts=tuple(_ALLOWED_FOLLOWUP_SCRIPTS),
    )


def _safe_next_plan_argv(command: str) -> List[str]:
    return _safe_python_script_argv(
        command,
        allowed_scripts=tuple(_ALLOWED_NEXT_PLAN_SCRIPTS),
    )


def _default_followup_file(handoff: Dict[str, Any], handoff_file: Path) -> Path:
    artifact_dir = handoff.get("artifact_dir")
    if artifact_dir:
        return Path(str(artifact_dir)) / FOLLOWUP_FILENAME
    return handoff_file.parent / FOLLOWUP_FILENAME


def _write_followup(
    *,
    followup_file: Path,
    payload: Dict[str, Any],
    stdout: str = "",
    stderr: str = "",
) -> None:
    followup_file.parent.mkdir(parents=True, exist_ok=True)
    stdout_file = followup_file.with_name(FOLLOWUP_STDOUT_FILENAME)
    stderr_file = followup_file.with_name(FOLLOWUP_STDERR_FILENAME)
    stdout_file.write_text(stdout, encoding="utf-8")
    stderr_file.write_text(stderr, encoding="utf-8")
    output_payload = {
        **payload,
        "stdout_file": str(stdout_file),
        "stderr_file": str(stderr_file),
    }
    output_payload["assessment"] = _followup_assessment(output_payload)
    followup_file.write_text(
        json.dumps(output_payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _followup_assessment(payload: Dict[str, Any]) -> Dict[str, Any]:
    status = str(payload.get("status") or "")
    primary_action = str(payload.get("primary_action") or "revise_harness_slice")
    recommended_steps = [
        step
        for step in payload.get("recommended_steps") or []
        if isinstance(step, dict)
    ]
    primary_step = payload.get("primary_step") or {}
    remaining_steps = recommended_steps[1:] if recommended_steps else []

    if status == "not_ready":
        return {
            "state": "blocked",
            "next_action": "provide_missing_followup_input",
            "reason": str(payload.get("reason") or "follow-up command is not ready"),
            "unresolved_placeholders": payload.get("unresolved_placeholders")
            or primary_step.get("unresolved_placeholders")
            or [],
            "inspect": [str(payload.get("handoff_file") or "")],
        }
    if status == "rejected":
        return {
            "state": "blocked",
            "next_action": "fix_handoff_command_contract",
            "reason": str(payload.get("reason") or "follow-up command was rejected"),
            "inspect": [str(payload.get("handoff_file") or "")],
        }
    if status == "pass":
        if remaining_steps:
            next_step = remaining_steps[0]
            resolved = next_step.get("resolved_commands") or []
            return {
                "state": "continue",
                "next_action": "run_next_handoff_step",
                "reason": (
                    "The primary follow-up passed; continue with the next "
                    "recommended step."
                ),
                "next_step": next_step,
                "next_command": resolved[0] if resolved else None,
                "next_command_ready": bool(next_step.get("ready_to_run")),
            }
        if payload.get("decision") == "promote":
            return {
                "state": "complete",
                "next_action": "record_promoted_slice",
                "reason": "The promotion follow-up passed.",
                "inspect": [str(payload.get("stdout_file") or "")],
            }
        return {
            "state": "recheck",
            "next_action": "rerun_promotion_gate",
            "reason": (
                "The diagnostic follow-up passed; rerun the promotion gate "
                "with updated artifacts."
            ),
            "inspect": [str(payload.get("stdout_file") or "")],
        }
    if status == "fail":
        return {
            "state": "needs_repair",
            "next_action": _FAILED_ACTION_NEXT_ACTION.get(
                primary_action, "inspect_profile_blockers"
            ),
            "reason": f"The {primary_action} follow-up command failed.",
            "returncode": payload.get("returncode"),
            "inspect": [
                str(payload.get("stdout_file") or ""),
                str(payload.get("stderr_file") or ""),
            ],
        }
    return {
        "state": "unknown",
        "next_action": "inspect_followup_artifact",
        "reason": f"Unknown follow-up status: {status or 'missing'}",
        "inspect": [str(payload.get("handoff_file") or "")],
    }


def _step_artifact_path(step: Dict[str, Any], artifact_name: str) -> Optional[Path]:
    artifact_paths = step.get("artifact_paths") or {}
    if isinstance(artifact_paths, dict) and artifact_paths.get(artifact_name):
        return Path(str(artifact_paths[artifact_name]))
    return None


def _default_next_plan_file(followup_file: Path) -> Path:
    return followup_file.parent / NEXT_PLAN_FILENAME


def _compact_policy_next_plan(
    *,
    followup: Dict[str, Any],
    followup_file: Path,
) -> Dict[str, Any]:
    primary_step = followup.get("primary_step") or {}
    compact_plan_path = _step_artifact_path(primary_step, "compact_policy_plan.json")
    if compact_plan_path is None:
        compact_plan_path = followup_file.parent / "compact_policy_plan.json"

    base = {
        "next_plan_version": 1,
        "source_followup_file": str(followup_file),
        "assessment": followup.get("assessment") or {},
        "next_action": "tune_compact_policy",
        "target_arm": primary_step.get("target_arm") or "preinj_eager_compact",
        "inspect": [str(compact_plan_path)],
    }

    if not compact_plan_path.exists():
        return {
            **base,
            "status": "blocked",
            "reason": "compact_policy_plan.json is missing",
            "missing_artifacts": [str(compact_plan_path)],
        }

    compact_plans = _load_json_object(compact_plan_path, label="compact policy plan")
    target_arm = str(base["target_arm"])
    arm_plan = compact_plans.get(target_arm)
    if not isinstance(arm_plan, dict):
        return {
            **base,
            "status": "blocked",
            "reason": f"compact policy plan has no entry for {target_arm}",
            "available_arms": sorted(str(arm) for arm in compact_plans.keys()),
        }

    preload_patch = arm_plan.get("preload_patch") or {}
    if not preload_patch:
        return {
            **base,
            "status": "manual",
            "reason": "compact policy plan did not propose an executable preload patch",
            "compact_policy": arm_plan,
            "suggested_commands": [
                "python scripts/agent_compile/run_feedback_iteration.py "
                "--output-dir <output-dir> --promotion-profile haiku_compact"
            ],
        }

    output_dir = followup_file.parent / f"haiku_feedback_probe_{target_arm}_policy"
    return {
        **base,
        "status": "ready",
        "reason": str(arm_plan.get("reason") or "compact policy patch is available"),
        "compact_policy": arm_plan,
        "config_overlay": {
            "base": "scripts/agent_compile/configs/haiku_feedback_probe.yaml",
            "sweep_id_suffix": f"{target_arm}_policy",
            "preload": {target_arm: preload_patch},
        },
        "suggested_commands": [
            "python scripts/agent_compile/run_synthesis_sweep.py "
            "--config <derived-config-yaml> "
            f"--output-dir {_shell_arg(str(output_dir))} "
            "--synthesis-configs Python,Go,Rust "
            "--categories behavioral,symbol_hint,traversal "
            "--max-queries-per-category 1",
            "python scripts/agent_compile/run_feedback_iteration.py "
            f"--output-dir {_shell_arg(str(output_dir))} "
            "--promotion-profile haiku_compact",
        ],
    }


def _first_string(values: Sequence[Any], default: str) -> str:
    for value in values:
        if value:
            return str(value)
    return default


def _command_option_value(command: str, option: str) -> Optional[str]:
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    for index, arg in enumerate(argv):
        if arg == option and index + 1 < len(argv):
            return argv[index + 1]
        prefix = f"{option}="
        if arg.startswith(prefix):
            return arg[len(prefix) :]
    return None


def _first_resolved_command(step: Dict[str, Any]) -> Optional[str]:
    commands = step.get("resolved_commands") or []
    for command in commands:
        if command:
            return str(command)
    return None


def _followup_command_option(
    followup: Dict[str, Any],
    option: str,
) -> Optional[str]:
    primary_step = followup.get("primary_step") or {}
    command = _first_resolved_command(primary_step)
    if command:
        value = _command_option_value(command, option)
        if value:
            return value
    command = str(followup.get("next_command") or "")
    if command:
        return _command_option_value(command, option)
    return None


def _answer_quality_contract(
    *,
    next_action: str,
    arm_plan: Dict[str, Any],
) -> Dict[str, Any]:
    diagnosis_action = str(arm_plan.get("action") or "inspect_answer_diagnosis")
    diagnosis_contract = _ANSWER_DIAGNOSIS_CONTRACTS.get(
        diagnosis_action,
        {
            "repair_surface": "answer_quality",
            "expected_effect": (
                "Inspect the answer diagnosis plan before choosing a repair lane."
            ),
        },
    )
    if next_action == "repair_answer_evidence":
        primary = {
            "repair_surface": "answer_evidence_provenance",
            "expected_effect": (
                "Every committed answer span should be backed by trace evidence "
                "or read-confirmed context."
            ),
        }
    else:
        primary = diagnosis_contract
    return {
        "repair_lane": next_action,
        "diagnosis_action": diagnosis_action,
        "diagnosis_dominant": arm_plan.get("dominant"),
        "diagnosis_rate": arm_plan.get("rate"),
        "repair_surface": primary["repair_surface"],
        "expected_effect": primary["expected_effect"],
        "diagnosis_surface": diagnosis_contract["repair_surface"],
        "diagnosis_expected_effect": diagnosis_contract["expected_effect"],
    }


def _answer_quality_next_plan(
    *,
    followup: Dict[str, Any],
    followup_file: Path,
    next_action: str,
) -> Dict[str, Any]:
    primary_step = followup.get("primary_step") or {}
    answer_plan_path = _step_artifact_path(
        primary_step,
        "answer_diagnosis_plan.json",
    )
    if answer_plan_path is None:
        answer_plan_path = followup_file.parent / "answer_diagnosis_plan.json"

    target_arm = str(primary_step.get("target_arm") or "preinj_eager_compact")
    base = {
        "next_plan_version": 1,
        "source_followup_file": str(followup_file),
        "assessment": followup.get("assessment") or {},
        "next_action": next_action,
        "target_arm": target_arm,
        "inspect": [str(answer_plan_path)],
    }

    if not answer_plan_path.exists():
        return {
            **base,
            "status": "blocked",
            "reason": "answer_diagnosis_plan.json is missing",
            "missing_artifacts": [str(answer_plan_path)],
        }

    answer_plans = _load_json_object(answer_plan_path, label="answer diagnosis plan")
    arm_plan = answer_plans.get(target_arm)
    if not isinstance(arm_plan, dict):
        return {
            **base,
            "status": "blocked",
            "reason": f"answer diagnosis plan has no entry for {target_arm}",
            "available_arms": sorted(str(arm) for arm in answer_plans.keys()),
        }

    output_dir = _first_string(
        [
            followup.get("artifact_dir"),
            _followup_command_option(followup, "--output-dir"),
            answer_plan_path.parent,
        ],
        str(followup_file.parent),
    )
    cells_dir = _first_string(
        [
            followup.get("cells_dir"),
            _followup_command_option(followup, "--cells-dir"),
            Path(output_dir) / "cells",
        ],
        "<cells-dir>",
    )
    if _unresolved_placeholders([cells_dir, output_dir]):
        return {
            **base,
            "status": "blocked",
            "reason": "answer-quality follow-up is missing cells or output directory",
            "unresolved_placeholders": _unresolved_placeholders(
                [cells_dir, output_dir]
            ),
            "answer_diagnosis": arm_plan,
        }

    profiles = [
        str(profile)
        for profile in followup.get("profiles") or []
        if str(profile).strip()
    ]
    promotion_profile = profiles[0] if profiles else "haiku_compact"
    gate_option = (
        "--require-answer-evidence"
        if next_action == "repair_answer_evidence"
        else "--require-feedback-gate"
    )
    contract = _answer_quality_contract(next_action=next_action, arm_plan=arm_plan)
    return {
        **base,
        "status": "ready",
        "reason": str(arm_plan.get("reason") or contract["expected_effect"]),
        "answer_diagnosis": arm_plan,
        "repair_contract": contract,
        "suggested_commands": [
            "python scripts/agent_compile/aggregate_feedback_probe.py "
            f"--cells-dir {_shell_arg(cells_dir)} "
            f"--output-dir {_shell_arg(output_dir)} "
            f"{gate_option} {_shell_arg(target_arm)}",
            "python scripts/agent_compile/run_feedback_iteration.py "
            f"--output-dir {_shell_arg(output_dir)} "
            f"--promotion-profile {_shell_arg(promotion_profile)}",
        ],
    }


def _external_artifact_path(
    step: Dict[str, Any],
    followup_file: Path,
    artifact_name: str,
) -> Path:
    return _step_artifact_path(step, artifact_name) or (
        followup_file.parent / artifact_name
    )


def _external_run_rows_by_label(baseline: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    rows = baseline.get("runs") or []
    return {
        str(row.get("label")): row
        for row in rows
        if isinstance(row, dict) and row.get("label")
    }


def _external_run_evidence(
    *,
    gate_run: Dict[str, Any],
    baseline_rows: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    evidence = dict(gate_run.get("evidence") or {})
    row = baseline_rows.get(str(gate_run.get("label") or "")) or {}
    for key in (
        "answer_diagnosis",
        "answer_span_count",
        "lsp_route_tool_index",
        "native_lsp_available",
        "native_lsp_tool_index",
        "mcp_cancelled",
        "mcp_stderr_internal_errors",
        "turns",
        "tool_count",
        "output_tokens",
    ):
        if evidence.get(key) is None and row.get(key) is not None:
            evidence[key] = row.get(key)
    if "rec5" not in evidence:
        metric = (row.get("metrics") or {}).get(5) or {}
        if not metric:
            metric = (row.get("metrics") or {}).get("5") or {}
        if isinstance(metric, dict):
            evidence["rec5"] = metric.get("recall")
    if "answer_rec_all" not in evidence:
        all_metric = row.get("answer_all_metrics") or {}
        if isinstance(all_metric, dict):
            evidence["answer_rec_all"] = all_metric.get("recall")
    return evidence


def _external_blocker_contract(
    *,
    blocker: str,
    evidence: Dict[str, Any],
) -> Dict[str, str]:
    diagnosis = str(evidence.get("answer_diagnosis") or "")
    if blocker == "answer_diagnosis_mismatch":
        return _EXTERNAL_DIAGNOSIS_CONTRACTS.get(
            diagnosis,
            {
                "action": "inspect_external_answer_diagnosis",
                "repair_surface": "external_answer_quality",
                "expected_effect": (
                    "Inspect the external answer diagnosis before choosing a "
                    "repair lane."
                ),
            },
        )
    if blocker == "rec5_below_gate" and diagnosis in _EXTERNAL_DIAGNOSIS_CONTRACTS:
        return _EXTERNAL_DIAGNOSIS_CONTRACTS[diagnosis]
    return _EXTERNAL_BLOCKER_CONTRACTS.get(
        blocker,
        {
            "action": "inspect_external_gate_blocker",
            "repair_surface": "external_gate_policy",
            "expected_effect": "Inspect the external baseline gate blocker.",
        },
    )


def _external_baseline_contract(
    *,
    gate: Dict[str, Any],
    baseline: Dict[str, Any],
    comparison_matrix: Optional[Dict[str, Any]] = None,
    repair_plan: Optional[Dict[str, Any]] = None,
    rerun_specs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    baseline_rows = _external_run_rows_by_label(baseline)
    run_actions: List[Dict[str, Any]] = []
    for run in gate.get("runs") or []:
        if not isinstance(run, dict):
            continue
        label = str(run.get("label") or "")
        evidence = _external_run_evidence(
            gate_run=run,
            baseline_rows=baseline_rows,
        )
        for blocker in run.get("blockers") or []:
            blocker_text = str(blocker)
            contract = _external_blocker_contract(
                blocker=blocker_text,
                evidence=evidence,
            )
            run_actions.append(
                {
                    "label": label,
                    "blocker": blocker_text,
                    "action": contract["action"],
                    "repair_surface": contract["repair_surface"],
                    "expected_effect": contract["expected_effect"],
                    "evidence": evidence,
                }
            )
    if not run_actions:
        for blocker in gate.get("blockers") or []:
            blocker_text = str(blocker)
            label, _, blocker_name = blocker_text.partition(":")
            contract = _external_blocker_contract(
                blocker=blocker_name or blocker_text,
                evidence={},
            )
            run_actions.append(
                {
                    "label": label or None,
                    "blocker": blocker_name or blocker_text,
                    "action": contract["action"],
                    "repair_surface": contract["repair_surface"],
                    "expected_effect": contract["expected_effect"],
                    "evidence": {},
                }
            )
    contract = {
        "gate": gate.get("gate") or "external_agent_run_gate",
        "status": gate.get("status"),
        "promote": gate.get("promote") is True,
        "criteria": gate.get("criteria") or {},
        "blockers": [str(blocker) for blocker in gate.get("blockers") or []],
        "run_actions": run_actions,
    }
    if comparison_matrix:
        gap_counts: Dict[str, int] = {}
        rows = [
            row for row in comparison_matrix.get("rows") or [] if isinstance(row, dict)
        ]
        for row in rows:
            gap = str(row.get("gap_category") or "unknown")
            gap_counts[gap] = gap_counts.get(gap, 0) + 1
        contract["comparison_matrix"] = {
            "status": comparison_matrix.get("status"),
            "reference_label": comparison_matrix.get("reference_label"),
            "gap_counts": gap_counts,
            "rows": rows,
        }
    if repair_plan:
        actions = [
            action
            for action in repair_plan.get("actions") or []
            if isinstance(action, dict)
        ]
        contract["repair_plan"] = {
            "status": repair_plan.get("status"),
            "action_count": len(actions),
            "actions": actions,
            "rerun_command": repair_plan.get("rerun_command"),
        }
    if rerun_specs:
        specs = [
            spec for spec in rerun_specs.get("specs") or [] if isinstance(spec, dict)
        ]
        contract["rerun_specs"] = {
            "status": rerun_specs.get("status"),
            "spec_count": len(specs),
            "specs": specs,
        }
    return contract


def _external_baseline_next_plan(
    *,
    followup: Dict[str, Any],
    followup_file: Path,
) -> Dict[str, Any]:
    primary_step = followup.get("primary_step") or {}
    gate_path = _external_artifact_path(
        primary_step,
        followup_file,
        "external_agent_gate.json",
    )
    baseline_path = _external_artifact_path(
        primary_step,
        followup_file,
        "external_agent_baseline.json",
    )
    command_path = _external_artifact_path(
        primary_step,
        followup_file,
        "external_agent_command.json",
    )
    matrix_path = _external_artifact_path(
        primary_step,
        followup_file,
        "external_agent_comparison_matrix.json",
    )
    repair_plan_path = _external_artifact_path(
        primary_step,
        followup_file,
        "external_agent_repair_plan.json",
    )
    rerun_specs_path = _external_artifact_path(
        primary_step,
        followup_file,
        "external_agent_rerun_specs.json",
    )
    base = {
        "next_plan_version": 1,
        "source_followup_file": str(followup_file),
        "assessment": followup.get("assessment") or {},
        "next_action": "compare_external_agent_baseline",
        "inspect": [
            str(gate_path),
            str(baseline_path),
            str(command_path),
            str(matrix_path),
            str(repair_plan_path),
            str(rerun_specs_path),
        ],
    }
    if not gate_path.exists():
        return {
            **base,
            "status": "blocked",
            "reason": "external_agent_gate.json is missing",
            "missing_artifacts": [str(gate_path)],
        }

    gate = _load_json_object(gate_path, label="external agent gate")
    baseline = (
        _load_json_object(baseline_path, label="external agent baseline")
        if baseline_path.exists()
        else {}
    )
    command_manifest = (
        _load_json_object(command_path, label="external agent command")
        if command_path.exists()
        else {}
    )
    comparison_matrix = (
        _load_json_object(matrix_path, label="external agent comparison matrix")
        if matrix_path.exists()
        else None
    )
    repair_plan = (
        _load_json_object(repair_plan_path, label="external agent repair plan")
        if repair_plan_path.exists()
        else None
    )
    rerun_specs = (
        _load_json_object(rerun_specs_path, label="external agent rerun specs")
        if rerun_specs_path.exists()
        else None
    )
    contract = _external_baseline_contract(
        gate=gate,
        baseline=baseline,
        comparison_matrix=comparison_matrix,
        repair_plan=repair_plan,
        rerun_specs=rerun_specs,
    )
    suggested_command = str(command_manifest.get("suggested_command") or "")
    suggested_commands: List[str] = []
    if rerun_specs is not None:
        suggested_commands.append(
            "python scripts/agent_compile/run_external_agent_rerun.py "
            f"--spec-file {_shell_arg(str(rerun_specs_path))} "
            "--spec-index 1 "
            f"--artifact-dir {_shell_arg(str(rerun_specs_path.parent))}"
        )
    if suggested_command:
        suggested_commands.append(suggested_command)
    if gate.get("promote") is True:
        return {
            **base,
            "status": "complete",
            "reason": "external agent gate passed",
            "external_baseline_contract": contract,
            "suggested_commands": [],
        }
    if suggested_commands:
        return {
            **base,
            "status": "ready",
            "reason": "external agent gate failed; rerun after applying the repair contract",
            "external_baseline_contract": contract,
            "external_command": command_manifest,
            "suggested_commands": suggested_commands,
        }
    return {
        **base,
        "status": "manual",
        "reason": "external agent gate failed but external_agent_command.json is missing",
        "external_baseline_contract": contract,
        "missing_artifacts": [str(command_path)],
        "suggested_commands": [],
    }


def _generic_next_plan(
    *,
    followup: Dict[str, Any],
    followup_file: Path,
) -> Dict[str, Any]:
    assessment = followup.get("assessment") or {}
    next_action = str(assessment.get("next_action") or "inspect_followup_artifact")
    state = str(assessment.get("state") or "unknown")
    status = "manual"
    if state in {"complete", "recheck"}:
        status = state
    elif state == "blocked":
        status = "blocked"
    return {
        "next_plan_version": 1,
        "source_followup_file": str(followup_file),
        "status": status,
        "next_action": next_action,
        "reason": str(assessment.get("reason") or "inspect follow-up assessment"),
        "assessment": assessment,
        "inspect": assessment.get("inspect") or [str(followup_file)],
        "suggested_commands": [],
    }


def _base_config_path(base_ref: str) -> Path:
    base_path = Path(base_ref)
    if base_path.is_absolute():
        return base_path.resolve()
    return (_PROJECT_ROOT / base_path).resolve()


def _materialize_config_overlay(
    *,
    plan: Dict[str, Any],
    next_plan_file: Path,
) -> Dict[str, Any]:
    overlay = plan.get("config_overlay")
    if plan.get("status") != "ready" or not isinstance(overlay, dict):
        return plan

    base_ref = overlay.get("base")
    if not base_ref:
        return {
            **plan,
            "status": "blocked",
            "reason": "config_overlay.base is missing",
        }

    base_path = _base_config_path(str(base_ref))
    base_payload = _load_yaml_mapping(base_path, label="base sweep config")
    base_preload = base_payload.get("preload") or {}
    if not isinstance(base_preload, dict):
        return {
            **plan,
            "status": "blocked",
            "reason": f"base sweep config preload must be a mapping: {base_path}",
        }

    overlay_preload = overlay.get("preload") or {}
    if not isinstance(overlay_preload, dict):
        return {
            **plan,
            "status": "blocked",
            "reason": "config_overlay.preload must be a mapping",
        }

    merged_preload = deepcopy(base_preload)
    for arm, patch in overlay_preload.items():
        current = merged_preload.get(arm) or {}
        if isinstance(current, dict) and isinstance(patch, dict):
            merged = deepcopy(current)
            merged.update(patch)
            merged_preload[arm] = merged
        else:
            merged_preload[arm] = deepcopy(patch)

    base_sweep_id = str(base_payload.get("sweep_id") or base_path.stem)
    suffix = str(overlay.get("sweep_id_suffix") or "feedback_iteration")
    derived_payload = {
        "base": str(base_path),
        "sweep_id": f"{base_sweep_id}_{suffix}",
        "preload": merged_preload,
    }
    derived_config_file = next_plan_file.with_name(DERIVED_CONFIG_FILENAME)
    derived_config_file.write_text(
        yaml.safe_dump(derived_payload, sort_keys=False),
        encoding="utf-8",
    )
    derived_config_arg = _shell_arg(str(derived_config_file))
    suggested_commands = [
        str(command).replace("<derived-config-yaml>", derived_config_arg)
        for command in plan.get("suggested_commands") or []
    ]
    return {
        **plan,
        "derived_config_file": str(derived_config_file),
        "suggested_commands": suggested_commands,
    }


def _next_plan_from_followup(
    *,
    followup_file: Path,
    next_plan_file: Optional[Path],
) -> int:
    followup = _load_json_object(followup_file, label="feedback follow-up")
    assessment = followup.get("assessment") or {}
    next_action = str(assessment.get("next_action") or "")
    if next_action == "tune_compact_policy":
        plan = _compact_policy_next_plan(followup=followup, followup_file=followup_file)
    elif next_action in {"repair_answer_evidence", "restore_answer_recall"}:
        plan = _answer_quality_next_plan(
            followup=followup,
            followup_file=followup_file,
            next_action=next_action,
        )
    elif next_action == "compare_external_agent_baseline":
        plan = _external_baseline_next_plan(
            followup=followup,
            followup_file=followup_file,
        )
    else:
        plan = _generic_next_plan(followup=followup, followup_file=followup_file)

    output_file = next_plan_file or _default_next_plan_file(followup_file)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    plan = _materialize_config_overlay(plan=plan, next_plan_file=output_file)
    output_file.write_text(json.dumps(plan, indent=2, sort_keys=True), encoding="utf-8")
    return HANDOFF_NOT_READY_EXIT if plan.get("status") == "blocked" else PROMOTE_EXIT


def _default_next_plan_execution_file(next_plan_file: Path) -> Path:
    return next_plan_file.parent / NEXT_PLAN_EXECUTION_FILENAME


def _default_next_plan_execution_series_file(next_plan_file: Path) -> Path:
    return next_plan_file.parent / NEXT_PLAN_EXECUTION_SERIES_FILENAME


def _write_next_plan_execution(
    *,
    execution_file: Path,
    payload: Dict[str, Any],
    stdout: str = "",
    stderr: str = "",
) -> None:
    execution_file.parent.mkdir(parents=True, exist_ok=True)
    stdout_file = execution_file.with_name(NEXT_PLAN_EXECUTION_STDOUT_FILENAME)
    stderr_file = execution_file.with_name(NEXT_PLAN_EXECUTION_STDERR_FILENAME)
    stdout_file.write_text(stdout, encoding="utf-8")
    stderr_file.write_text(stderr, encoding="utf-8")
    output_payload = {
        **payload,
        "stdout_file": str(stdout_file),
        "stderr_file": str(stderr_file),
    }
    output_payload["assessment"] = _next_plan_execution_assessment(output_payload)
    execution_file.write_text(
        json.dumps(output_payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _next_plan_execution_assessment(payload: Dict[str, Any]) -> Dict[str, Any]:
    status = str(payload.get("status") or "")
    commands = payload.get("suggested_commands") or []
    command_index = int(payload.get("command_index") or 0)
    next_index = command_index + 1
    if status == "not_ready":
        return {
            "state": "blocked",
            "next_action": "fix_next_plan_command",
            "reason": str(payload.get("reason") or "next-plan command is not ready"),
            "inspect": [str(payload.get("next_plan_file") or "")],
        }
    if status == "rejected":
        return {
            "state": "blocked",
            "next_action": "fix_next_plan_command_contract",
            "reason": str(payload.get("reason") or "next-plan command was rejected"),
            "inspect": [str(payload.get("next_plan_file") or "")],
        }
    if status == "pass":
        if next_index < len(commands):
            return {
                "state": "continue",
                "next_action": "execute_next_plan_command",
                "reason": (
                    "The current next-plan command passed; execute the next "
                    "suggested command."
                ),
                "next_command_index": next_index,
                "next_command": commands[next_index],
            }
        return {
            "state": "complete",
            "next_action": "inspect_next_plan_outputs",
            "reason": "All executed next-plan commands passed.",
            "inspect": [str(payload.get("stdout_file") or "")],
        }
    if status == "fail":
        return {
            "state": "needs_repair",
            "next_action": "inspect_next_plan_execution",
            "reason": "The next-plan command failed.",
            "returncode": payload.get("returncode"),
            "inspect": [
                str(payload.get("stdout_file") or ""),
                str(payload.get("stderr_file") or ""),
            ],
        }
    return {
        "state": "unknown",
        "next_action": "inspect_next_plan_execution",
        "reason": f"Unknown next-plan execution status: {status or 'missing'}",
        "inspect": [str(payload.get("next_plan_file") or "")],
    }


def _next_plan_execution_series_assessment(payload: Dict[str, Any]) -> Dict[str, Any]:
    status = str(payload.get("status") or "")
    steps = [step for step in payload.get("steps") or [] if isinstance(step, dict)]
    if status == "pass":
        decision_steps = [
            step for step in steps if isinstance(step.get("promotion_decision"), dict)
        ]
        if decision_steps:
            decision_step = decision_steps[-1]
            decision = decision_step.get("promotion_decision") or {}
            if (
                decision.get("decision") == "promote"
                and decision.get("promote") is True
            ):
                return {
                    "state": "complete",
                    "next_action": "record_promoted_slice",
                    "reason": (
                        "The next-plan series passed and the promotion gate "
                        "promoted the slice."
                    ),
                    "decision": decision,
                    "inspect": [str(decision_step.get("stdout_file") or "")],
                }
            handoff_file = decision_step.get("handoff_file")
            next_command = None
            if handoff_file:
                next_command = (
                    "python scripts/agent_compile/run_feedback_iteration.py "
                    f"--execute-handoff {_shell_arg(str(handoff_file))}"
                )
            return {
                "state": "continue",
                "next_action": "execute_handoff",
                "reason": (
                    "The next-plan series passed, but the promotion gate still "
                    "requests revision."
                ),
                "decision": decision,
                "handoff_file": handoff_file,
                "next_command": next_command,
                "next_command_ready": bool(next_command),
                "inspect": [str(decision_step.get("stdout_file") or "")],
            }
        return {
            "state": "complete",
            "next_action": "inspect_next_plan_outputs",
            "reason": "All suggested next-plan commands passed.",
            "inspect": [
                str(step.get("stdout_file") or "")
                for step in steps
                if step.get("stdout_file")
            ],
        }
    if status == "not_ready":
        step = steps[-1] if steps else {}
        return {
            "state": "blocked",
            "next_action": "fix_next_plan_command",
            "reason": str(payload.get("reason") or "next-plan command is not ready"),
            "command_index": step.get("command_index"),
            "inspect": [str(payload.get("next_plan_file") or "")],
        }
    if status == "rejected":
        step = steps[-1] if steps else {}
        return {
            "state": "blocked",
            "next_action": "fix_next_plan_command_contract",
            "reason": str(payload.get("reason") or "next-plan command was rejected"),
            "command_index": step.get("command_index"),
            "inspect": [str(payload.get("next_plan_file") or "")],
        }
    if status == "fail":
        failed = steps[-1] if steps else {}
        return {
            "state": "needs_repair",
            "next_action": "inspect_next_plan_execution",
            "reason": "A suggested next-plan command failed.",
            "command_index": failed.get("command_index"),
            "returncode": failed.get("returncode"),
            "inspect": [
                str(failed.get("stdout_file") or ""),
                str(failed.get("stderr_file") or ""),
            ],
        }
    return {
        "state": "unknown",
        "next_action": "inspect_next_plan_execution",
        "reason": f"Unknown next-plan execution series status: {status or 'missing'}",
        "inspect": [str(payload.get("next_plan_file") or "")],
    }


def _write_next_plan_execution_series(
    *,
    series_file: Path,
    payload: Dict[str, Any],
) -> None:
    series_file.parent.mkdir(parents=True, exist_ok=True)
    output_payload = dict(payload)
    output_payload["assessment"] = _next_plan_execution_series_assessment(
        output_payload
    )
    series_file.write_text(
        json.dumps(output_payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _json_stdout_object(stdout: str) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _promotion_decision_step_metadata(
    *,
    command: str,
    stdout: str,
) -> Dict[str, Any]:
    payload = _json_stdout_object(stdout)
    if not payload or "decision" not in payload:
        return {}

    handoff_file = _command_option_value(command, "--handoff-file")
    output_dir = _command_option_value(command, "--output-dir")
    if not handoff_file and output_dir:
        handoff_file = str(Path(output_dir) / HANDOFF_FILENAME)
    return {
        "promotion_decision": payload,
        "handoff_file": handoff_file,
    }


def _execute_next_plan(
    *,
    next_plan_file: Path,
    command_index: int,
    execution_file: Optional[Path],
    show_output: bool,
) -> int:
    next_plan = _load_json_object(next_plan_file, label="feedback next plan")
    resolved_execution_file = execution_file or _default_next_plan_execution_file(
        next_plan_file
    )
    suggested_commands = [str(cmd) for cmd in next_plan.get("suggested_commands") or []]
    base_payload = {
        "execution_version": 1,
        "next_plan_file": str(next_plan_file),
        "next_plan_status": next_plan.get("status"),
        "next_action": next_plan.get("next_action"),
        "command_index": command_index,
        "suggested_commands": suggested_commands,
    }
    if command_index < 0 or command_index >= len(suggested_commands):
        _write_next_plan_execution(
            execution_file=resolved_execution_file,
            payload={
                **base_payload,
                "status": "not_ready",
                "returncode": HANDOFF_NOT_READY_EXIT,
                "reason": "suggested command index is not available",
            },
        )
        return HANDOFF_NOT_READY_EXIT

    command = suggested_commands[command_index]
    base_payload["command"] = command
    if _unresolved_placeholders([command]):
        _write_next_plan_execution(
            execution_file=resolved_execution_file,
            payload={
                **base_payload,
                "status": "not_ready",
                "returncode": HANDOFF_NOT_READY_EXIT,
                "reason": "suggested command still has unresolved placeholders",
                "unresolved_placeholders": _unresolved_placeholders([command]),
            },
        )
        return HANDOFF_NOT_READY_EXIT

    try:
        argv = _safe_next_plan_argv(command)
    except RuntimeError as exc:
        _write_next_plan_execution(
            execution_file=resolved_execution_file,
            payload={
                **base_payload,
                "status": "rejected",
                "returncode": HANDOFF_REJECTED_EXIT,
                "reason": str(exc),
            },
        )
        return HANDOFF_REJECTED_EXIT

    started = time.monotonic()
    completed = subprocess.run(argv, check=False, capture_output=True, text=True)
    duration_ms = int((time.monotonic() - started) * 1000)
    if show_output:
        print(completed.stdout, end="")
        print(completed.stderr, end="", file=sys.stderr)
    _write_next_plan_execution(
        execution_file=resolved_execution_file,
        payload={
            **base_payload,
            "status": "pass" if completed.returncode == 0 else "fail",
            "returncode": completed.returncode,
            "argv": argv,
            "duration_ms": duration_ms,
        },
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
    return int(completed.returncode)


def _execute_next_plan_series(
    *,
    next_plan_file: Path,
    series_file: Optional[Path],
    show_output: bool,
) -> int:
    next_plan = _load_json_object(next_plan_file, label="feedback next plan")
    resolved_series_file = series_file or _default_next_plan_execution_series_file(
        next_plan_file
    )
    suggested_commands = [str(cmd) for cmd in next_plan.get("suggested_commands") or []]
    base_payload = {
        "execution_series_version": 1,
        "next_plan_file": str(next_plan_file),
        "next_plan_status": next_plan.get("status"),
        "next_action": next_plan.get("next_action"),
        "suggested_command_count": len(suggested_commands),
        "steps": [],
    }
    if not suggested_commands:
        _write_next_plan_execution_series(
            series_file=resolved_series_file,
            payload={
                **base_payload,
                "status": "not_ready",
                "returncode": HANDOFF_NOT_READY_EXIT,
                "reason": "next plan has no suggested commands",
                "completed_command_count": 0,
            },
        )
        return HANDOFF_NOT_READY_EXIT

    steps: List[Dict[str, Any]] = []
    for command_index, command in enumerate(suggested_commands):
        step_payload: Dict[str, Any] = {
            "command_index": command_index,
            "command": command,
        }
        unresolved = _unresolved_placeholders([command])
        if unresolved:
            steps.append(
                {
                    **step_payload,
                    "status": "not_ready",
                    "returncode": HANDOFF_NOT_READY_EXIT,
                    "reason": "suggested command still has unresolved placeholders",
                    "unresolved_placeholders": unresolved,
                }
            )
            _write_next_plan_execution_series(
                series_file=resolved_series_file,
                payload={
                    **base_payload,
                    "status": "not_ready",
                    "returncode": HANDOFF_NOT_READY_EXIT,
                    "reason": "suggested command still has unresolved placeholders",
                    "completed_command_count": max(0, len(steps) - 1),
                    "steps": steps,
                },
            )
            return HANDOFF_NOT_READY_EXIT

        try:
            argv = _safe_next_plan_argv(command)
        except RuntimeError as exc:
            steps.append(
                {
                    **step_payload,
                    "status": "rejected",
                    "returncode": HANDOFF_REJECTED_EXIT,
                    "reason": str(exc),
                }
            )
            _write_next_plan_execution_series(
                series_file=resolved_series_file,
                payload={
                    **base_payload,
                    "status": "rejected",
                    "returncode": HANDOFF_REJECTED_EXIT,
                    "reason": str(exc),
                    "completed_command_count": max(0, len(steps) - 1),
                    "steps": steps,
                },
            )
            return HANDOFF_REJECTED_EXIT

        stdout_file = resolved_series_file.with_name(
            NEXT_PLAN_EXECUTION_SERIES_STDOUT_TEMPLATE.format(index=command_index)
        )
        stderr_file = resolved_series_file.with_name(
            NEXT_PLAN_EXECUTION_SERIES_STDERR_TEMPLATE.format(index=command_index)
        )
        started = time.monotonic()
        completed = subprocess.run(argv, check=False, capture_output=True, text=True)
        duration_ms = int((time.monotonic() - started) * 1000)
        stdout_file.write_text(completed.stdout, encoding="utf-8")
        stderr_file.write_text(completed.stderr, encoding="utf-8")
        if show_output:
            print(completed.stdout, end="")
            print(completed.stderr, end="", file=sys.stderr)

        step_metadata = _promotion_decision_step_metadata(
            command=command,
            stdout=completed.stdout,
        )
        decision_returncode = (
            "promotion_decision" in step_metadata
            and completed.returncode in {PROMOTE_EXIT, REVISE_EXIT}
        )
        status = "pass" if completed.returncode == 0 or decision_returncode else "fail"
        steps.append(
            {
                **step_payload,
                "status": status,
                "returncode": completed.returncode,
                "argv": argv,
                "duration_ms": duration_ms,
                "stdout_file": str(stdout_file),
                "stderr_file": str(stderr_file),
                **step_metadata,
            }
        )
        if status != "pass":
            _write_next_plan_execution_series(
                series_file=resolved_series_file,
                payload={
                    **base_payload,
                    "status": "fail",
                    "returncode": completed.returncode,
                    "failed_command_index": command_index,
                    "completed_command_count": len(steps) - 1,
                    "steps": steps,
                },
            )
            return int(completed.returncode)

    _write_next_plan_execution_series(
        series_file=resolved_series_file,
        payload={
            **base_payload,
            "status": "pass",
            "returncode": PROMOTE_EXIT,
            "completed_command_count": len(steps),
            "steps": steps,
        },
    )
    return PROMOTE_EXIT


def _execute_handoff(
    *,
    handoff_file: Path,
    followup_file: Optional[Path],
    show_output: bool,
) -> int:
    handoff = _load_json_object(handoff_file, label="feedback handoff")
    resolved_followup_file = followup_file or _default_followup_file(
        handoff, handoff_file
    )
    next_command = str(handoff.get("next_command") or "")
    primary_step = handoff.get("primary_step") or {}
    base_payload = {
        "followup_version": 1,
        "handoff_file": str(handoff_file),
        "decision": handoff.get("decision"),
        "primary_action": handoff.get("primary_action"),
        "artifact_dir": handoff.get("artifact_dir"),
        "cells_dir": handoff.get("cells_dir"),
        "profiles": handoff.get("profiles") or [],
        "next_command": next_command,
        "next_command_ready": bool(handoff.get("next_command_ready")),
        "next_actions": handoff.get("next_actions") or [],
        "primary_step": primary_step,
        "recommended_steps": handoff.get("recommended_steps") or [],
    }
    if not handoff.get("next_command_ready"):
        _write_followup(
            followup_file=resolved_followup_file,
            payload={
                **base_payload,
                "status": "not_ready",
                "returncode": HANDOFF_NOT_READY_EXIT,
                "reason": "handoff next_command_ready is false",
                "unresolved_placeholders": primary_step.get(
                    "unresolved_placeholders", []
                ),
            },
        )
        return HANDOFF_NOT_READY_EXIT

    try:
        argv = _safe_followup_argv(next_command)
    except RuntimeError as exc:
        _write_followup(
            followup_file=resolved_followup_file,
            payload={
                **base_payload,
                "status": "rejected",
                "returncode": HANDOFF_REJECTED_EXIT,
                "reason": str(exc),
            },
        )
        return HANDOFF_REJECTED_EXIT

    started = time.monotonic()
    completed = subprocess.run(argv, check=False, capture_output=True, text=True)
    duration_ms = int((time.monotonic() - started) * 1000)
    if show_output:
        print(completed.stdout, end="")
        print(completed.stderr, end="", file=sys.stderr)
    _write_followup(
        followup_file=resolved_followup_file,
        payload={
            **base_payload,
            "status": "pass" if completed.returncode == 0 else "fail",
            "returncode": completed.returncode,
            "argv": argv,
            "duration_ms": duration_ms,
        },
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
    return int(completed.returncode)


def _run_aggregate(
    *,
    cells_dir: Path,
    output_dir: Path,
    profiles: Sequence[str],
    show_aggregate_output: bool,
) -> int:
    argv = [
        "--cells-dir",
        str(cells_dir),
        "--output-dir",
        str(output_dir),
    ]
    for profile in profiles:
        argv.extend(["--promotion-profile", profile])

    if show_aggregate_output:
        return aggregate_feedback_probe.main(argv)

    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc = aggregate_feedback_probe.main(argv)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "feedback_iteration_aggregate.stdout").write_text(
        stdout.getvalue(), encoding="utf-8"
    )
    (output_dir / "feedback_iteration_aggregate.stderr").write_text(
        stderr.getvalue(), encoding="utf-8"
    )
    return rc


def _run_sweep(args: argparse.Namespace) -> None:
    from scripts.agent_compile.run_synthesis_sweep import main as sweep_main

    argv = [
        "--config",
        str(args.run_sweep_config),
        "--output-dir",
        str(args.output_dir),
        "--synthesis-configs",
        args.synthesis_configs,
    ]
    if args.categories:
        argv.extend(["--categories", args.categories])
    if args.max_queries is not None:
        argv.extend(["--max-queries", str(args.max_queries)])
    if args.max_queries_per_category is not None:
        argv.extend(["--max-queries-per-category", str(args.max_queries_per_category)])
    if args.reps is not None:
        argv.extend(["--reps", str(args.reps)])
    if args.no_resume:
        argv.append("--no-resume")
    if args.model:
        argv.extend(["--model", args.model])

    rc = sweep_main(argv)
    if rc:
        raise RuntimeError(f"feedback sweep failed with exit code {rc}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--cells-dir", type=Path)
    parser.add_argument(
        "--execute-handoff",
        type=Path,
        help=(
            "Execute a ready feedback_iteration_handoff.json next_command through "
            "the allowlisted follow-up runner."
        ),
    )
    parser.add_argument(
        "--followup-file",
        type=Path,
        help=(
            "Optional path for feedback_iteration_followup.json when using "
            "--execute-handoff."
        ),
    )
    parser.add_argument(
        "--show-followup-output",
        action="store_true",
        help="Echo executed follow-up stdout/stderr in addition to writing files.",
    )
    parser.add_argument(
        "--plan-from-followup",
        type=Path,
        help=(
            "Create feedback_iteration_next_plan.json from a "
            "feedback_iteration_followup.json assessment."
        ),
    )
    parser.add_argument(
        "--next-plan-file",
        type=Path,
        help="Optional path for feedback_iteration_next_plan.json.",
    )
    parser.add_argument(
        "--execute-next-plan",
        type=Path,
        help=(
            "Execute one suggested command from feedback_iteration_next_plan.json "
            "through the allowlisted next-plan runner."
        ),
    )
    parser.add_argument(
        "--execute-next-plan-all",
        type=Path,
        help=(
            "Execute all suggested commands from feedback_iteration_next_plan.json "
            "sequentially through the allowlisted next-plan runner."
        ),
    )
    parser.add_argument(
        "--next-plan-command-index",
        type=int,
        default=0,
        help="Suggested command index to execute from --execute-next-plan.",
    )
    parser.add_argument(
        "--plan-execution-file",
        type=Path,
        help="Optional path for feedback_iteration_next_plan_execution.json.",
    )
    parser.add_argument(
        "--plan-execution-series-file",
        type=Path,
        help=("Optional path for feedback_iteration_next_plan_execution_series.json."),
    )
    parser.add_argument(
        "--show-plan-output",
        action="store_true",
        help="Echo executed next-plan stdout/stderr in addition to writing files.",
    )
    parser.add_argument(
        "--promotion-profile",
        action="append",
        default=["haiku_compact"],
        help="Promotion profile to aggregate. Repeat for multiple profiles.",
    )
    parser.add_argument(
        "--decision-file",
        type=Path,
        help="Evaluate an existing promotion_decision.json instead of aggregating.",
    )
    parser.add_argument(
        "--handoff-file",
        type=Path,
        help=(
            "Optional path for feedback_iteration_handoff.json. Defaults to "
            "output-dir/feedback_iteration_handoff.json after aggregation."
        ),
    )
    parser.add_argument(
        "--show-aggregate-output",
        action="store_true",
        help="Do not capture aggregate markdown/stderr into output-dir files.",
    )
    parser.add_argument(
        "--run-sweep-config",
        type=Path,
        help="Optional synthesis sweep config to run before aggregating.",
    )
    parser.add_argument(
        "--synthesis-configs",
        default="Python,Go,Rust",
        help="Comma-separated synthesis configs for --run-sweep-config.",
    )
    parser.add_argument("--categories")
    parser.add_argument("--max-queries", type=int)
    parser.add_argument("--max-queries-per-category", type=int)
    parser.add_argument("--reps", type=int)
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--model")
    args = parser.parse_args(argv)

    if args.execute_handoff:
        try:
            return _execute_handoff(
                handoff_file=args.execute_handoff,
                followup_file=args.followup_file,
                show_output=args.show_followup_output,
            )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return MISSING_DECISION_EXIT

    if args.plan_from_followup:
        try:
            return _next_plan_from_followup(
                followup_file=args.plan_from_followup,
                next_plan_file=args.next_plan_file,
            )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return MISSING_DECISION_EXIT

    if args.execute_next_plan:
        try:
            return _execute_next_plan(
                next_plan_file=args.execute_next_plan,
                command_index=args.next_plan_command_index,
                execution_file=args.plan_execution_file,
                show_output=args.show_plan_output,
            )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return MISSING_DECISION_EXIT

    if args.execute_next_plan_all:
        try:
            return _execute_next_plan_series(
                next_plan_file=args.execute_next_plan_all,
                series_file=args.plan_execution_series_file,
                show_output=args.show_plan_output,
            )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return MISSING_DECISION_EXIT

    if args.decision_file and not args.cells_dir and not args.run_sweep_config:
        try:
            decision = _load_decision(args.decision_file)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return MISSING_DECISION_EXIT
        artifact_dir = args.output_dir or args.decision_file.parent
        cells_dir = args.cells_dir or (artifact_dir / "cells")
        _write_handoff(
            decision=decision,
            handoff_file=args.handoff_file,
            artifact_dir=artifact_dir,
            cells_dir=cells_dir,
        )
        print(json.dumps(decision, indent=2, sort_keys=True))
        return _exit_for_decision(decision)

    if not args.output_dir:
        parser.error("--output-dir is required unless --decision-file is used alone")

    if args.run_sweep_config:
        try:
            _run_sweep(args)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return AGGREGATE_ERROR_EXIT

    cells_dir = args.cells_dir or (args.output_dir / "cells")
    rc = _run_aggregate(
        cells_dir=cells_dir,
        output_dir=args.output_dir,
        profiles=args.promotion_profile,
        show_aggregate_output=args.show_aggregate_output,
    )
    decision_path = args.output_dir / "promotion_decision.json"
    try:
        decision = _load_decision(decision_path)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return MISSING_DECISION_EXIT if rc in {PROMOTE_EXIT, REVISE_EXIT} else rc

    handoff_file = args.handoff_file or (args.output_dir / HANDOFF_FILENAME)
    _write_handoff(
        decision=decision,
        handoff_file=handoff_file,
        artifact_dir=args.output_dir,
        cells_dir=cells_dir,
    )
    print(json.dumps(decision, indent=2, sort_keys=True))
    return _exit_for_decision(decision)


if __name__ == "__main__":
    raise SystemExit(main())
