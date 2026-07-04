# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for feedback iteration promotion handoff."""

from __future__ import annotations

import json

import yaml

from scripts.agent_compile.run_feedback_iteration import main


def _cell(query_id: str, arm: str, rec: float, tokens: int, *, turns: int = 2):
    return {
        "success": True,
        "query_id": query_id,
        "category": "behavioral",
        "subset_id": arm,
        "format_failed": False,
        "metrics": {
            "answer_blocks": {"5": {"recall": rec}},
            "files": {"5": {"recall": rec}},
        },
        "total_tokens": tokens,
        "total_turns": turns,
        "trace": {
            "events": [
                {"kind": "run_start", "turn": 0, "data": {}},
                {"kind": "run_end", "turn": turns, "data": {}},
            ],
            "context": [],
        },
        "answer_span_evidence": [],
    }


def _with_answer_evidence(cell):
    cell["answer_span_evidence"] = [
        {
            "span": {"file": "src/a.py", "start": 10, "end": 20},
            "trace_evidenced": True,
            "read_confirmed": True,
            "evidence": [{"source": "read", "state": "read"}],
        }
    ]
    return cell


def _with_exact_replay(cell):
    cell["messages"] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "query"},
        {"role": "assistant", "content": "answer"},
    ]
    return cell


def _write_cells(cells_dir, cells):
    cells_dir.mkdir()
    for index, cell in enumerate(cells):
        (cells_dir / f"cell_{index}.json").write_text(
            json.dumps(cell),
            encoding="utf-8",
        )


def test_feedback_iteration_promotes_profile(tmp_path, capsys):
    cells_dir = tmp_path / "cells"
    output_dir = tmp_path / "out"
    _write_cells(
        cells_dir,
        [
            _cell("q1", "grep_only", 1.0, 1000, turns=4),
            _with_exact_replay(
                _with_answer_evidence(
                    _cell("q1", "preinj_eager_compact", 1.0, 800, turns=3)
                )
            ),
        ],
    )

    rc = main(
        [
            "--cells-dir",
            str(cells_dir),
            "--output-dir",
            str(output_dir),
            "--promotion-profile",
            "haiku_compact",
        ]
    )
    captured = capsys.readouterr()
    decision = json.loads(captured.out)

    assert rc == 0
    assert captured.err == ""
    assert decision["decision"] == "promote"
    assert decision["promote"] is True
    assert (output_dir / "promotion_decision.json").exists()
    assert (output_dir / "feedback_iteration_aggregate.stdout").exists()
    assert (output_dir / "feedback_iteration_aggregate.stderr").exists()
    handoff = json.loads((output_dir / "feedback_iteration_handoff.json").read_text())
    assert handoff["decision"] == "promote"
    assert handoff["primary_action"] == "promote_harness_slice"
    assert handoff["next_actions"] == []
    assert handoff["recommended_steps"][0]["action"] == "promote_harness_slice"
    assert "promotion_profile_gate.json" in handoff["recommended_steps"][0]["artifacts"]
    assert handoff["primary_step"] == handoff["recommended_steps"][0]
    assert handoff["next_command"] == handoff["primary_step"]["resolved_commands"][0]
    assert handoff["next_command_ready"] is True
    assert str(output_dir) in handoff["next_command"]
    assert "haiku_compact" in handoff["next_command"]
    assert handoff["primary_step"]["ready_to_run"] is True
    assert handoff["primary_step"]["unresolved_placeholders"] == []


def test_feedback_iteration_revises_profile(tmp_path, capsys):
    cells_dir = tmp_path / "cells"
    output_dir = tmp_path / "out"
    _write_cells(
        cells_dir,
        [
            _cell("q1", "grep_only", 1.0, 1000, turns=3),
            _with_answer_evidence(
                _cell("q1", "preinj_eager_compact", 1.0, 1200, turns=3)
            ),
        ],
    )

    rc = main(
        [
            "--cells-dir",
            str(cells_dir),
            "--output-dir",
            str(output_dir),
            "--promotion-profile",
            "haiku_compact",
        ]
    )
    captured = capsys.readouterr()
    decision = json.loads(captured.out)

    assert rc == 2
    assert captured.err == ""
    assert decision["decision"] == "revise"
    assert decision["action"] == "complete_replay_artifacts"
    assert "complete_replay_artifacts" in decision["next_actions"]
    handoff = json.loads((output_dir / "feedback_iteration_handoff.json").read_text())
    assert handoff["decision"] == "revise"
    assert handoff["primary_action"] == "complete_replay_artifacts"
    assert [step["action"] for step in handoff["recommended_steps"]] == [
        "complete_replay_artifacts",
        "reduce_tokens_or_turns",
    ]
    replay_step = handoff["recommended_steps"][0]
    assert replay_step["priority"] == 1
    assert replay_step["target_arm"] == "preinj_eager_compact"
    assert "replay_readiness_gate.json" in replay_step["artifact_paths"]
    assert str(cells_dir / "cell_1.json") in replay_step["resolved_commands"][0]
    assert handoff["next_command"] == replay_step["resolved_commands"][0]
    assert handoff["next_command_ready"] is True
    assert replay_step["ready_to_run"] is True
    assert replay_step["unresolved_placeholders"] == []
    aggregate_stderr = (output_dir / "feedback_iteration_aggregate.stderr").read_text()
    assert "promotion profile gate failed" in aggregate_stderr


def test_feedback_iteration_reads_existing_decision(tmp_path, capsys):
    decision_path = tmp_path / "promotion_decision.json"
    decision_path.write_text(
        json.dumps(
            {
                "decision": "revise",
                "promote": False,
                "action": "reduce_tokens_or_turns",
                "next_actions": ["reduce_tokens_or_turns"],
            }
        ),
        encoding="utf-8",
    )

    handoff_path = tmp_path / "handoff.json"
    rc = main(
        ["--decision-file", str(decision_path), "--handoff-file", str(handoff_path)]
    )
    captured = capsys.readouterr()
    decision = json.loads(captured.out)

    assert rc == 2
    assert captured.err == ""
    assert decision["action"] == "reduce_tokens_or_turns"
    handoff = json.loads(handoff_path.read_text())
    assert handoff["primary_action"] == "reduce_tokens_or_turns"
    assert handoff["recommended_steps"][0]["action"] == "reduce_tokens_or_turns"
    assert handoff["recommended_steps"][0]["target_arm"] == "preinj_eager_compact"
    assert "compact_policy_plan.json" in handoff["recommended_steps"][0]["artifacts"]
    assert handoff["next_command"] == handoff["primary_step"]["resolved_commands"][0]
    assert handoff["next_command_ready"] is True
    assert "preinj_eager_compact" in handoff["next_command"]
    assert str(tmp_path) in handoff["next_command"]
    assert handoff["primary_step"]["ready_to_run"] is True
    assert handoff["primary_step"]["unresolved_placeholders"] == []


def test_feedback_iteration_route_lifecycle_revise_uses_suite_plan(tmp_path, capsys):
    decision_path = tmp_path / "promotion_decision.json"
    output_dir = tmp_path / "out"
    handoff_path = tmp_path / "handoff.json"
    decision_path.write_text(
        json.dumps(
            {
                "decision": "revise",
                "promote": False,
                "action": "revise_route_lifecycle",
                "profiles": ["route_lifecycle"],
                "next_actions": ["revise_route_lifecycle"],
            }
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "--decision-file",
            str(decision_path),
            "--output-dir",
            str(output_dir),
            "--handoff-file",
            str(handoff_path),
        ]
    )
    captured = capsys.readouterr()
    decision = json.loads(captured.out)
    handoff = json.loads(handoff_path.read_text())
    step = handoff["recommended_steps"][0]

    assert rc == 2
    assert captured.err == ""
    assert decision["action"] == "revise_route_lifecycle"
    assert handoff["primary_action"] == "revise_route_lifecycle"
    assert step["action"] == "revise_route_lifecycle"
    assert step["target_arm"] == "preinj_graph_scheduled"
    assert step["ready_to_run"] is True
    assert step["unresolved_placeholders"] == []
    assert "plan_feedback_suite.py" in step["resolved_commands"][0]
    assert "haiku_static_lsp_route_q2.yaml" in step["resolved_commands"][0]
    assert str(output_dir) in step["resolved_commands"][0]
    assert "aggregate_feedback_probe.py" in step["resolved_commands"][1]


def test_feedback_iteration_marks_handoff_not_ready_when_input_is_missing(
    tmp_path, capsys
):
    decision_path = tmp_path / "promotion_decision.json"
    decision_path.write_text(
        json.dumps(
            {
                "decision": "revise",
                "promote": False,
                "action": "complete_replay_artifacts",
                "profiles": ["haiku_compact"],
                "next_actions": ["complete_replay_artifacts"],
            }
        ),
        encoding="utf-8",
    )
    handoff_path = tmp_path / "handoff.json"

    rc = main(
        ["--decision-file", str(decision_path), "--handoff-file", str(handoff_path)]
    )
    captured = capsys.readouterr()
    handoff = json.loads(handoff_path.read_text())

    assert rc == 2
    assert captured.err == ""
    assert handoff["next_command_ready"] is False
    assert handoff["primary_step"]["ready_to_run"] is False
    assert handoff["primary_step"]["unresolved_placeholders"] == ["<cell-json>"]
    assert "<cell-json>" in handoff["next_command"]


def test_feedback_iteration_executes_ready_handoff_next_command(tmp_path, capsys):
    decision_path = tmp_path / "promotion_decision.json"
    decision_path.write_text(
        json.dumps(
            {
                "decision": "promote",
                "promote": True,
                "action": "promote_harness_slice",
                "next_actions": [],
            }
        ),
        encoding="utf-8",
    )
    handoff_path = tmp_path / "feedback_iteration_handoff.json"
    followup_path = tmp_path / "feedback_iteration_followup.json"
    handoff_path.write_text(
        json.dumps(
            {
                "decision": "promote",
                "primary_action": "promote_harness_slice",
                "next_command": (
                    "python scripts/agent_compile/run_feedback_iteration.py "
                    f"--decision-file {decision_path}"
                ),
                "next_command_ready": True,
                "primary_step": {
                    "action": "promote_harness_slice",
                    "ready_to_run": True,
                    "unresolved_placeholders": [],
                },
            }
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "--execute-handoff",
            str(handoff_path),
            "--followup-file",
            str(followup_path),
        ]
    )
    captured = capsys.readouterr()
    followup = json.loads(followup_path.read_text())

    assert rc == 0
    assert captured.out == ""
    assert captured.err == ""
    assert followup["status"] == "pass"
    assert followup["returncode"] == 0
    assert followup["next_command_ready"] is True
    assert "run_feedback_iteration.py" in followup["argv"][1]
    assert followup["assessment"]["state"] == "complete"
    assert followup["assessment"]["next_action"] == "record_promoted_slice"
    assert (
        json.loads((tmp_path / "feedback_iteration_followup.stdout").read_text())[
            "decision"
        ]
        == "promote"
    )
    assert (tmp_path / "feedback_iteration_followup.stderr").read_text() == ""


def test_feedback_iteration_does_not_execute_not_ready_handoff(tmp_path, capsys):
    handoff_path = tmp_path / "feedback_iteration_handoff.json"
    followup_path = tmp_path / "feedback_iteration_followup.json"
    handoff_path.write_text(
        json.dumps(
            {
                "decision": "revise",
                "primary_action": "complete_replay_artifacts",
                "next_command": "python scripts/agent_compile/summarize_trace.py <cell-json>",
                "next_command_ready": False,
                "primary_step": {
                    "action": "complete_replay_artifacts",
                    "ready_to_run": False,
                    "unresolved_placeholders": ["<cell-json>"],
                },
            }
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "--execute-handoff",
            str(handoff_path),
            "--followup-file",
            str(followup_path),
        ]
    )
    captured = capsys.readouterr()
    followup = json.loads(followup_path.read_text())

    assert rc == 5
    assert captured.out == ""
    assert captured.err == ""
    assert followup["status"] == "not_ready"
    assert followup["unresolved_placeholders"] == ["<cell-json>"]
    assert followup["assessment"]["state"] == "blocked"
    assert followup["assessment"]["next_action"] == "provide_missing_followup_input"
    assert followup["assessment"]["unresolved_placeholders"] == ["<cell-json>"]


def test_feedback_iteration_rejects_non_allowlisted_handoff_command(tmp_path, capsys):
    handoff_path = tmp_path / "feedback_iteration_handoff.json"
    followup_path = tmp_path / "feedback_iteration_followup.json"
    handoff_path.write_text(
        json.dumps(
            {
                "decision": "revise",
                "primary_action": "revise_harness_slice",
                "next_command": "python -c 'print(1)'",
                "next_command_ready": True,
                "primary_step": {
                    "action": "revise_harness_slice",
                    "ready_to_run": True,
                    "unresolved_placeholders": [],
                },
            }
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "--execute-handoff",
            str(handoff_path),
            "--followup-file",
            str(followup_path),
        ]
    )
    captured = capsys.readouterr()
    followup = json.loads(followup_path.read_text())

    assert rc == 6
    assert captured.out == ""
    assert captured.err == ""
    assert followup["status"] == "rejected"
    assert "not allowlisted" in followup["reason"]
    assert followup["assessment"]["state"] == "blocked"
    assert followup["assessment"]["next_action"] == "fix_handoff_command_contract"
    assert (tmp_path / "feedback_iteration_followup.stdout").read_text() == ""


def test_feedback_iteration_assesses_failed_followup_as_repair_needed(tmp_path, capsys):
    handoff_path = tmp_path / "feedback_iteration_handoff.json"
    followup_path = tmp_path / "feedback_iteration_followup.json"
    missing_decision = tmp_path / "missing_decision.json"
    handoff_path.write_text(
        json.dumps(
            {
                "decision": "revise",
                "primary_action": "restore_answer_recall",
                "next_command": (
                    "python scripts/agent_compile/run_feedback_iteration.py "
                    f"--decision-file {missing_decision}"
                ),
                "next_command_ready": True,
                "primary_step": {
                    "action": "restore_answer_recall",
                    "ready_to_run": True,
                    "unresolved_placeholders": [],
                },
            }
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "--execute-handoff",
            str(handoff_path),
            "--followup-file",
            str(followup_path),
        ]
    )
    captured = capsys.readouterr()
    followup = json.loads(followup_path.read_text())

    assert rc == 3
    assert captured.out == ""
    assert captured.err == ""
    assert followup["status"] == "fail"
    assert followup["assessment"]["state"] == "needs_repair"
    assert followup["assessment"]["next_action"] == "restore_answer_recall"
    assert followup["assessment"]["returncode"] == 3
    assert (
        "promotion decision missing"
        in (tmp_path / "feedback_iteration_followup.stderr").read_text()
    )


def test_feedback_iteration_creates_compact_policy_next_plan(tmp_path, capsys):
    compact_plan_path = tmp_path / "compact_policy_plan.json"
    followup_path = tmp_path / "feedback_iteration_followup.json"
    next_plan_path = tmp_path / "feedback_iteration_next_plan.json"
    compact_plan_path.write_text(
        json.dumps(
            {
                "preinj_eager_compact": {
                    "policy": "trim_tail",
                    "action": "set_compact_tail_chars",
                    "preload_patch": {"compact_tail_chars": 250},
                    "reason": "protected tail dominates the compact seed",
                    "evidence": {"compact_tail_share": 0.5},
                }
            }
        ),
        encoding="utf-8",
    )
    followup_path.write_text(
        json.dumps(
            {
                "status": "fail",
                "primary_action": "reduce_tokens_or_turns",
                "primary_step": {
                    "target_arm": "preinj_eager_compact",
                    "artifact_paths": {
                        "compact_policy_plan.json": str(compact_plan_path)
                    },
                },
                "assessment": {
                    "state": "needs_repair",
                    "next_action": "tune_compact_policy",
                    "reason": "compact arm did not save tokens or turns",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "--plan-from-followup",
            str(followup_path),
            "--next-plan-file",
            str(next_plan_path),
        ]
    )
    captured = capsys.readouterr()
    plan = json.loads(next_plan_path.read_text())

    assert rc == 0
    assert captured.out == ""
    assert captured.err == ""
    assert plan["status"] == "ready"
    assert plan["next_action"] == "tune_compact_policy"
    assert plan["target_arm"] == "preinj_eager_compact"
    assert plan["config_overlay"]["preload"]["preinj_eager_compact"] == {
        "compact_tail_chars": 250
    }
    assert "derived-config-yaml" not in plan["suggested_commands"][0]
    assert "--promotion-profile haiku_compact" in plan["suggested_commands"][1]
    derived_config = yaml.safe_load(
        (tmp_path / "feedback_iteration_derived_config.yaml").read_text()
    )
    assert plan["derived_config_file"] == str(
        tmp_path / "feedback_iteration_derived_config.yaml"
    )
    assert derived_config["sweep_id"] == (
        "haiku_feedback_probe_preinj_eager_compact_policy"
    )
    assert "preinj_eager" in derived_config["preload"]
    assert derived_config["preload"]["preinj_eager_compact"] == {
        "retrievers": ["embedding"],
        "top_k": 10,
        "level": "l2",
        "mode": "eager_compact",
        "compact_keep_reads": 0,
        "compact_tail_chars": 250,
    }
    assert str(tmp_path / "feedback_iteration_derived_config.yaml") in (
        plan["suggested_commands"][0]
    )


def test_feedback_iteration_blocks_next_plan_when_compact_plan_is_missing(
    tmp_path, capsys
):
    followup_path = tmp_path / "feedback_iteration_followup.json"
    next_plan_path = tmp_path / "feedback_iteration_next_plan.json"
    missing_plan = tmp_path / "missing_compact_policy_plan.json"
    followup_path.write_text(
        json.dumps(
            {
                "status": "fail",
                "primary_action": "reduce_tokens_or_turns",
                "primary_step": {
                    "target_arm": "preinj_eager_compact",
                    "artifact_paths": {"compact_policy_plan.json": str(missing_plan)},
                },
                "assessment": {
                    "state": "needs_repair",
                    "next_action": "tune_compact_policy",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "--plan-from-followup",
            str(followup_path),
            "--next-plan-file",
            str(next_plan_path),
        ]
    )
    captured = capsys.readouterr()
    plan = json.loads(next_plan_path.read_text())

    assert rc == 5
    assert captured.out == ""
    assert captured.err == ""
    assert plan["status"] == "blocked"
    assert plan["missing_artifacts"] == [str(missing_plan)]


def test_feedback_iteration_creates_answer_recall_next_plan(tmp_path, capsys):
    cells_dir = tmp_path / "cells"
    output_dir = tmp_path / "out"
    cells_dir.mkdir()
    output_dir.mkdir()
    answer_plan_path = output_dir / "answer_diagnosis_plan.json"
    followup_path = tmp_path / "feedback_iteration_followup.json"
    next_plan_path = tmp_path / "feedback_iteration_next_plan.json"
    answer_plan_path.write_text(
        json.dumps(
            {
                "preinj_eager_compact": {
                    "dominant": "rank_gap",
                    "rate": 1.0,
                    "action": "tighten_answer_ordering",
                    "reason": "target spans fell outside top-k",
                    "evidence": {"delta_rec5": -0.25},
                }
            }
        ),
        encoding="utf-8",
    )
    followup_path.write_text(
        json.dumps(
            {
                "status": "fail",
                "artifact_dir": str(output_dir),
                "cells_dir": str(cells_dir),
                "profiles": ["haiku_compact"],
                "primary_action": "restore_answer_recall",
                "primary_step": {
                    "target_arm": "preinj_eager_compact",
                    "artifact_paths": {
                        "answer_diagnosis_plan.json": str(answer_plan_path)
                    },
                },
                "assessment": {
                    "state": "needs_repair",
                    "next_action": "restore_answer_recall",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "--plan-from-followup",
            str(followup_path),
            "--next-plan-file",
            str(next_plan_path),
        ]
    )
    captured = capsys.readouterr()
    plan = json.loads(next_plan_path.read_text())

    assert rc == 0
    assert captured.out == ""
    assert captured.err == ""
    assert plan["status"] == "ready"
    assert plan["next_action"] == "restore_answer_recall"
    assert plan["target_arm"] == "preinj_eager_compact"
    assert plan["repair_contract"]["repair_surface"] == "answer_ordering"
    assert plan["repair_contract"]["diagnosis_action"] == "tighten_answer_ordering"
    assert plan["answer_diagnosis"]["dominant"] == "rank_gap"
    assert "--require-feedback-gate preinj_eager_compact" in (
        plan["suggested_commands"][0]
    )
    assert f"--cells-dir {cells_dir}" in plan["suggested_commands"][0]
    assert f"--output-dir {output_dir}" in plan["suggested_commands"][0]
    assert "--promotion-profile haiku_compact" in plan["suggested_commands"][1]


def test_feedback_iteration_creates_answer_evidence_next_plan(tmp_path, capsys):
    output_dir = tmp_path / "out"
    cells_dir = output_dir / "cells"
    cells_dir.mkdir(parents=True)
    answer_plan_path = output_dir / "answer_diagnosis_plan.json"
    followup_path = tmp_path / "feedback_iteration_followup.json"
    next_plan_path = tmp_path / "feedback_iteration_next_plan.json"
    answer_plan_path.write_text(
        json.dumps(
            {
                "preinj_eager_compact": {
                    "dominant": "ok",
                    "rate": 1.0,
                    "action": "keep_answer_contract",
                    "reason": "strict spans are correct but not evidenced",
                    "evidence": {"rec5": 1.0},
                }
            }
        ),
        encoding="utf-8",
    )
    followup_path.write_text(
        json.dumps(
            {
                "status": "fail",
                "primary_action": "repair_answer_evidence",
                "primary_step": {
                    "target_arm": "preinj_eager_compact",
                    "artifact_paths": {
                        "answer_diagnosis_plan.json": str(answer_plan_path)
                    },
                    "resolved_commands": [
                        (
                            "python scripts/agent_compile/aggregate_feedback_probe.py "
                            f"--cells-dir {cells_dir} --output-dir {output_dir} "
                            "--require-answer-evidence preinj_eager_compact"
                        )
                    ],
                },
                "assessment": {
                    "state": "needs_repair",
                    "next_action": "repair_answer_evidence",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "--plan-from-followup",
            str(followup_path),
            "--next-plan-file",
            str(next_plan_path),
        ]
    )
    captured = capsys.readouterr()
    plan = json.loads(next_plan_path.read_text())

    assert rc == 0
    assert captured.out == ""
    assert captured.err == ""
    assert plan["status"] == "ready"
    assert plan["next_action"] == "repair_answer_evidence"
    assert plan["repair_contract"]["repair_surface"] == "answer_evidence_provenance"
    assert plan["repair_contract"]["diagnosis_surface"] == "gate_recheck"
    assert "--require-answer-evidence preinj_eager_compact" in (
        plan["suggested_commands"][0]
    )
    assert "--promotion-profile haiku_compact" in plan["suggested_commands"][1]


def test_feedback_iteration_blocks_answer_quality_plan_when_diagnosis_is_missing(
    tmp_path, capsys
):
    followup_path = tmp_path / "feedback_iteration_followup.json"
    next_plan_path = tmp_path / "feedback_iteration_next_plan.json"
    missing_plan = tmp_path / "missing_answer_diagnosis_plan.json"
    followup_path.write_text(
        json.dumps(
            {
                "status": "fail",
                "primary_action": "restore_answer_recall",
                "primary_step": {
                    "target_arm": "preinj_eager_compact",
                    "artifact_paths": {"answer_diagnosis_plan.json": str(missing_plan)},
                },
                "assessment": {
                    "state": "needs_repair",
                    "next_action": "restore_answer_recall",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "--plan-from-followup",
            str(followup_path),
            "--next-plan-file",
            str(next_plan_path),
        ]
    )
    captured = capsys.readouterr()
    plan = json.loads(next_plan_path.read_text())

    assert rc == 5
    assert captured.out == ""
    assert captured.err == ""
    assert plan["status"] == "blocked"
    assert plan["missing_artifacts"] == [str(missing_plan)]


def test_feedback_iteration_creates_external_baseline_next_plan(tmp_path, capsys):
    gate_path = tmp_path / "external_agent_gate.json"
    baseline_path = tmp_path / "external_agent_baseline.json"
    command_path = tmp_path / "external_agent_command.json"
    matrix_path = tmp_path / "external_agent_comparison_matrix.json"
    repair_plan_path = tmp_path / "external_agent_repair_plan.json"
    rerun_specs_path = tmp_path / "external_agent_rerun_specs.json"
    followup_path = tmp_path / "feedback_iteration_followup.json"
    next_plan_path = tmp_path / "feedback_iteration_next_plan.json"
    gate_path.write_text(
        json.dumps(
            {
                "gate": "external_agent_run_gate",
                "status": "fail",
                "promote": False,
                "blockers": [
                    "codex_bad:rec5_below_gate",
                    "codex_bad:answer_diagnosis_mismatch",
                    "codex_bad:mcp_cancelled_above_gate",
                ],
                "criteria": {
                    "labels": ["codex_bad"],
                    "min_rec5": 1.0,
                    "require_diagnosis": "ok",
                },
                "runs": [
                    {
                        "label": "codex_bad",
                        "status": "fail",
                        "blockers": [
                            "rec5_below_gate",
                            "answer_diagnosis_mismatch",
                            "mcp_cancelled_above_gate",
                        ],
                        "evidence": {
                            "rec5": 0.0,
                            "answer_diagnosis": "path_alias_gap",
                            "mcp_cancelled": 1,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    baseline_path.write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "label": "codex_bad",
                        "answer_diagnosis": "path_alias_gap",
                        "answer_span_count": 1,
                        "answer_path_alias_metrics": {"recall": 1.0},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    command_path.write_text(
        json.dumps(
            {
                "command_manifest_version": 1,
                "suggested_command": (
                    "python scripts/agent_compile/aggregate_external_agent_baseline.py --help"
                ),
            }
        ),
        encoding="utf-8",
    )
    matrix_path.write_text(
        json.dumps(
            {
                "comparison_matrix_version": 1,
                "status": "ready",
                "reference_label": "codeminer_route",
                "rows": [
                    {
                        "label": "codex_bad",
                        "gap_category": "answer_contract_gap",
                        "delta_rec5": -1.0,
                    },
                    {
                        "label": "codex_cancelled",
                        "gap_category": "mcp_policy_gap",
                        "delta_rec5": -1.0,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    repair_plan_path.write_text(
        json.dumps(
            {
                "repair_plan_version": 1,
                "status": "ready",
                "rerun_command": (
                    "python scripts/agent_compile/aggregate_external_agent_baseline.py --help"
                ),
                "actions": [
                    {
                        "label": "codex_bad",
                        "gap_category": "answer_contract_gap",
                        "repair_action": "tighten_external_answer_contract",
                        "repair_surface": "external_prompt_schema",
                        "suggested_prompt_delta": "Require repo-relative Locations.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    rerun_specs_path.write_text(
        json.dumps(
            {
                "rerun_specs_version": 1,
                "status": "ready",
                "specs": [
                    {
                        "index": 1,
                        "label": "codex_bad",
                        "gap_category": "answer_contract_gap",
                        "repair_action": "tighten_external_answer_contract",
                        "launcher_status": "ready",
                        "requirement": "capture_jsonl",
                        "prompt_delta": "Require repo-relative Locations.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    followup_path.write_text(
        json.dumps(
            {
                "status": "fail",
                "primary_action": "compare_external_agent_baseline",
                "primary_step": {
                    "artifact_paths": {
                        "external_agent_gate.json": str(gate_path),
                        "external_agent_baseline.json": str(baseline_path),
                        "external_agent_command.json": str(command_path),
                        "external_agent_comparison_matrix.json": str(matrix_path),
                        "external_agent_repair_plan.json": str(repair_plan_path),
                        "external_agent_rerun_specs.json": str(rerun_specs_path),
                    }
                },
                "assessment": {
                    "state": "needs_repair",
                    "next_action": "compare_external_agent_baseline",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "--plan-from-followup",
            str(followup_path),
            "--next-plan-file",
            str(next_plan_path),
        ]
    )
    captured = capsys.readouterr()
    plan = json.loads(next_plan_path.read_text())
    actions = plan["external_baseline_contract"]["run_actions"]

    assert rc == 0
    assert captured.out == ""
    assert captured.err == ""
    assert plan["status"] == "ready"
    assert plan["next_action"] == "compare_external_agent_baseline"
    assert len(plan["suggested_commands"]) == 2
    assert "run_external_agent_rerun.py" in plan["suggested_commands"][0]
    assert str(rerun_specs_path) in plan["suggested_commands"][0]
    assert plan["suggested_commands"][1] == (
        "python scripts/agent_compile/aggregate_external_agent_baseline.py --help"
    )
    assert plan["external_baseline_contract"]["status"] == "fail"
    assert str(matrix_path) in plan["inspect"]
    assert (
        plan["external_baseline_contract"]["comparison_matrix"]["reference_label"]
        == "codeminer_route"
    )
    assert plan["external_baseline_contract"]["comparison_matrix"]["gap_counts"] == {
        "answer_contract_gap": 1,
        "mcp_policy_gap": 1,
    }
    assert plan["external_baseline_contract"]["repair_plan"]["action_count"] == 1
    assert (
        plan["external_baseline_contract"]["repair_plan"]["actions"][0]["repair_action"]
        == "tighten_external_answer_contract"
    )
    assert plan["external_baseline_contract"]["repair_plan"]["rerun_command"] == (
        "python scripts/agent_compile/aggregate_external_agent_baseline.py --help"
    )
    assert str(rerun_specs_path) in plan["inspect"]
    assert plan["external_baseline_contract"]["rerun_specs"]["spec_count"] == 1
    assert (
        plan["external_baseline_contract"]["rerun_specs"]["specs"][0]["launcher_status"]
        == "ready"
    )
    assert {action["action"] for action in actions} == {
        "tighten_external_path_contract",
        "fix_external_mcp_policy",
    }
    assert {action["repair_surface"] for action in actions} >= {
        "external_path_normalization",
        "external_mcp_policy",
    }
    assert all(action["label"] == "codex_bad" for action in actions)


def test_feedback_iteration_completes_external_baseline_next_plan(tmp_path, capsys):
    gate_path = tmp_path / "external_agent_gate.json"
    followup_path = tmp_path / "feedback_iteration_followup.json"
    next_plan_path = tmp_path / "feedback_iteration_next_plan.json"
    gate_path.write_text(
        json.dumps(
            {
                "gate": "external_agent_run_gate",
                "status": "pass",
                "promote": True,
                "blockers": [],
                "criteria": {"labels": ["codex_schema"]},
                "runs": [
                    {
                        "label": "codex_schema",
                        "status": "pass",
                        "blockers": [],
                        "evidence": {
                            "rec5": 1.0,
                            "answer_diagnosis": "ok",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    followup_path.write_text(
        json.dumps(
            {
                "status": "pass",
                "primary_action": "compare_external_agent_baseline",
                "primary_step": {
                    "artifact_paths": {"external_agent_gate.json": str(gate_path)}
                },
                "assessment": {
                    "state": "recheck",
                    "next_action": "compare_external_agent_baseline",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "--plan-from-followup",
            str(followup_path),
            "--next-plan-file",
            str(next_plan_path),
        ]
    )
    captured = capsys.readouterr()
    plan = json.loads(next_plan_path.read_text())

    assert rc == 0
    assert captured.out == ""
    assert captured.err == ""
    assert plan["status"] == "complete"
    assert plan["external_baseline_contract"]["promote"] is True
    assert plan["suggested_commands"] == []


def test_feedback_iteration_blocks_external_baseline_plan_when_gate_is_missing(
    tmp_path, capsys
):
    missing_gate = tmp_path / "external_agent_gate.json"
    followup_path = tmp_path / "feedback_iteration_followup.json"
    next_plan_path = tmp_path / "feedback_iteration_next_plan.json"
    followup_path.write_text(
        json.dumps(
            {
                "status": "fail",
                "primary_action": "compare_external_agent_baseline",
                "primary_step": {
                    "artifact_paths": {"external_agent_gate.json": str(missing_gate)}
                },
                "assessment": {
                    "state": "needs_repair",
                    "next_action": "compare_external_agent_baseline",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "--plan-from-followup",
            str(followup_path),
            "--next-plan-file",
            str(next_plan_path),
        ]
    )
    captured = capsys.readouterr()
    plan = json.loads(next_plan_path.read_text())

    assert rc == 5
    assert captured.out == ""
    assert captured.err == ""
    assert plan["status"] == "blocked"
    assert plan["missing_artifacts"] == [str(missing_gate)]


def test_feedback_iteration_creates_generic_next_plan(tmp_path, capsys):
    followup_path = tmp_path / "feedback_iteration_followup.json"
    next_plan_path = tmp_path / "feedback_iteration_next_plan.json"
    followup_path.write_text(
        json.dumps(
            {
                "status": "pass",
                "assessment": {
                    "state": "recheck",
                    "next_action": "rerun_promotion_gate",
                    "reason": "diagnostic follow-up passed",
                    "inspect": ["feedback_iteration_followup.stdout"],
                },
            }
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "--plan-from-followup",
            str(followup_path),
            "--next-plan-file",
            str(next_plan_path),
        ]
    )
    captured = capsys.readouterr()
    plan = json.loads(next_plan_path.read_text())

    assert rc == 0
    assert captured.out == ""
    assert captured.err == ""
    assert plan["status"] == "recheck"
    assert plan["next_action"] == "rerun_promotion_gate"
    assert plan["inspect"] == ["feedback_iteration_followup.stdout"]


def test_feedback_iteration_executes_next_plan_command(tmp_path, capsys):
    decision_path = tmp_path / "promotion_decision.json"
    next_plan_path = tmp_path / "feedback_iteration_next_plan.json"
    execution_path = tmp_path / "feedback_iteration_next_plan_execution.json"
    decision_path.write_text(
        json.dumps(
            {
                "decision": "promote",
                "promote": True,
                "action": "promote_harness_slice",
                "next_actions": [],
            }
        ),
        encoding="utf-8",
    )
    next_plan_path.write_text(
        json.dumps(
            {
                "status": "ready",
                "next_action": "run_next_handoff_step",
                "suggested_commands": [
                    (
                        "python scripts/agent_compile/run_feedback_iteration.py "
                        f"--decision-file {decision_path}"
                    ),
                    "python scripts/agent_compile/run_feedback_iteration.py --help",
                ],
            }
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "--execute-next-plan",
            str(next_plan_path),
            "--plan-execution-file",
            str(execution_path),
        ]
    )
    captured = capsys.readouterr()
    execution = json.loads(execution_path.read_text())

    assert rc == 0
    assert captured.out == ""
    assert captured.err == ""
    assert execution["status"] == "pass"
    assert execution["command_index"] == 0
    assert "run_feedback_iteration.py" in execution["argv"][1]
    assert execution["assessment"]["state"] == "continue"
    assert execution["assessment"]["next_command_index"] == 1
    assert (
        json.loads(
            (tmp_path / "feedback_iteration_next_plan_execution.stdout").read_text()
        )["decision"]
        == "promote"
    )
    assert (
        tmp_path / "feedback_iteration_next_plan_execution.stderr"
    ).read_text() == ""


def test_feedback_iteration_marks_missing_next_plan_command_not_ready(tmp_path, capsys):
    next_plan_path = tmp_path / "feedback_iteration_next_plan.json"
    execution_path = tmp_path / "feedback_iteration_next_plan_execution.json"
    next_plan_path.write_text(
        json.dumps({"status": "ready", "suggested_commands": []}),
        encoding="utf-8",
    )

    rc = main(
        [
            "--execute-next-plan",
            str(next_plan_path),
            "--plan-execution-file",
            str(execution_path),
        ]
    )
    captured = capsys.readouterr()
    execution = json.loads(execution_path.read_text())

    assert rc == 5
    assert captured.out == ""
    assert captured.err == ""
    assert execution["status"] == "not_ready"
    assert execution["assessment"]["state"] == "blocked"
    assert execution["assessment"]["next_action"] == "fix_next_plan_command"


def test_feedback_iteration_rejects_non_allowlisted_next_plan_command(tmp_path, capsys):
    next_plan_path = tmp_path / "feedback_iteration_next_plan.json"
    execution_path = tmp_path / "feedback_iteration_next_plan_execution.json"
    next_plan_path.write_text(
        json.dumps(
            {
                "status": "ready",
                "suggested_commands": ["python -c 'print(1)'"],
            }
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "--execute-next-plan",
            str(next_plan_path),
            "--plan-execution-file",
            str(execution_path),
        ]
    )
    captured = capsys.readouterr()
    execution = json.loads(execution_path.read_text())

    assert rc == 6
    assert captured.out == ""
    assert captured.err == ""
    assert execution["status"] == "rejected"
    assert execution["assessment"]["state"] == "blocked"
    assert execution["assessment"]["next_action"] == "fix_next_plan_command_contract"
    assert (
        tmp_path / "feedback_iteration_next_plan_execution.stdout"
    ).read_text() == ""


def test_feedback_iteration_executes_next_plan_series(tmp_path, capsys):
    decision_path = tmp_path / "promotion_decision.json"
    next_plan_path = tmp_path / "feedback_iteration_next_plan.json"
    series_path = tmp_path / "feedback_iteration_next_plan_execution_series.json"
    decision_path.write_text(
        json.dumps(
            {
                "decision": "promote",
                "promote": True,
                "action": "promote_harness_slice",
                "next_actions": [],
            }
        ),
        encoding="utf-8",
    )
    next_plan_path.write_text(
        json.dumps(
            {
                "status": "ready",
                "next_action": "tune_compact_policy",
                "suggested_commands": [
                    (
                        "python scripts/agent_compile/run_feedback_iteration.py "
                        f"--decision-file {decision_path}"
                    ),
                    "python scripts/agent_compile/run_feedback_iteration.py --help",
                ],
            }
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "--execute-next-plan-all",
            str(next_plan_path),
            "--plan-execution-series-file",
            str(series_path),
        ]
    )
    captured = capsys.readouterr()
    series = json.loads(series_path.read_text())

    assert rc == 0
    assert captured.out == ""
    assert captured.err == ""
    assert series["status"] == "pass"
    assert series["completed_command_count"] == 2
    assert series["suggested_command_count"] == 2
    assert [step["status"] for step in series["steps"]] == ["pass", "pass"]
    assert series["assessment"]["state"] == "complete"
    assert series["assessment"]["next_action"] == "record_promoted_slice"
    assert series["assessment"]["decision"]["decision"] == "promote"
    assert series["steps"][0]["promotion_decision"]["decision"] == "promote"
    assert (
        json.loads(
            (
                tmp_path / "feedback_iteration_next_plan_execution.command0.stdout"
            ).read_text()
        )["decision"]
        == "promote"
    )
    assert (
        "usage:"
        in (
            tmp_path / "feedback_iteration_next_plan_execution.command1.stdout"
        ).read_text()
    )
    assert (
        tmp_path / "feedback_iteration_next_plan_execution.command1.stderr"
    ).read_text() == ""


def test_feedback_iteration_stops_next_plan_series_on_failed_command(tmp_path, capsys):
    decision_path = tmp_path / "promotion_decision.json"
    missing_decision = tmp_path / "missing_decision.json"
    next_plan_path = tmp_path / "feedback_iteration_next_plan.json"
    series_path = tmp_path / "feedback_iteration_next_plan_execution_series.json"
    decision_path.write_text(
        json.dumps(
            {
                "decision": "promote",
                "promote": True,
                "action": "promote_harness_slice",
                "next_actions": [],
            }
        ),
        encoding="utf-8",
    )
    next_plan_path.write_text(
        json.dumps(
            {
                "status": "ready",
                "next_action": "tune_compact_policy",
                "suggested_commands": [
                    (
                        "python scripts/agent_compile/run_feedback_iteration.py "
                        f"--decision-file {decision_path}"
                    ),
                    (
                        "python scripts/agent_compile/run_feedback_iteration.py "
                        f"--decision-file {missing_decision}"
                    ),
                    "python scripts/agent_compile/run_feedback_iteration.py --help",
                ],
            }
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "--execute-next-plan-all",
            str(next_plan_path),
            "--plan-execution-series-file",
            str(series_path),
        ]
    )
    captured = capsys.readouterr()
    series = json.loads(series_path.read_text())

    assert rc == 3
    assert captured.out == ""
    assert captured.err == ""
    assert series["status"] == "fail"
    assert series["failed_command_index"] == 1
    assert series["completed_command_count"] == 1
    assert [step["status"] for step in series["steps"]] == ["pass", "fail"]
    assert series["assessment"]["state"] == "needs_repair"
    assert series["assessment"]["command_index"] == 1
    assert series["assessment"]["returncode"] == 3
    assert (
        "promotion decision missing"
        in (
            tmp_path / "feedback_iteration_next_plan_execution.command1.stderr"
        ).read_text()
    )
    assert not (
        tmp_path / "feedback_iteration_next_plan_execution.command2.stdout"
    ).exists()


def test_feedback_iteration_series_lifts_revise_decision_to_handoff(tmp_path, capsys):
    decision_path = tmp_path / "promotion_decision.json"
    handoff_path = tmp_path / "feedback_iteration_handoff.json"
    next_plan_path = tmp_path / "feedback_iteration_next_plan.json"
    series_path = tmp_path / "feedback_iteration_next_plan_execution_series.json"
    decision_path.write_text(
        json.dumps(
            {
                "decision": "revise",
                "promote": False,
                "action": "restore_answer_recall",
                "next_actions": ["restore_answer_recall"],
            }
        ),
        encoding="utf-8",
    )
    next_plan_path.write_text(
        json.dumps(
            {
                "status": "ready",
                "next_action": "restore_answer_recall",
                "suggested_commands": [
                    (
                        "python scripts/agent_compile/run_feedback_iteration.py "
                        f"--decision-file {decision_path} "
                        f"--handoff-file {handoff_path}"
                    )
                ],
            }
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "--execute-next-plan-all",
            str(next_plan_path),
            "--plan-execution-series-file",
            str(series_path),
        ]
    )
    captured = capsys.readouterr()
    series = json.loads(series_path.read_text())

    assert rc == 0
    assert captured.out == ""
    assert captured.err == ""
    assert series["status"] == "pass"
    assert series["steps"][0]["promotion_decision"]["decision"] == "revise"
    assert series["steps"][0]["handoff_file"] == str(handoff_path)
    assert series["assessment"]["state"] == "continue"
    assert series["assessment"]["next_action"] == "execute_handoff"
    assert series["assessment"]["handoff_file"] == str(handoff_path)
    assert series["assessment"]["next_command_ready"] is True
    assert "--execute-handoff" in series["assessment"]["next_command"]


def test_feedback_iteration_missing_decision_returns_handoff_error(tmp_path, capsys):
    rc = main(["--decision-file", str(tmp_path / "missing.json")])
    captured = capsys.readouterr()

    assert rc == 3
    assert captured.out == ""
    assert "promotion decision missing" in captured.err
