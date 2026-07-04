import json

from scripts.agent_compile.aggregate_external_agent_baseline import (
    build_comparison_matrix,
    build_external_repair_plan,
    build_external_rerun_specs,
    evaluate_run_gate,
    main,
    render_markdown,
    score_codeminer_cell,
    score_run,
)


def test_external_baseline_scores_claude_jsonl(tmp_path):
    run_path = tmp_path / "claude.jsonl"
    rows = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "mcp__codeminer__lsp_route",
                        "input": {"symbols": ["target"]},
                    }
                ]
            },
        },
        {
            "type": "result",
            "num_turns": 2,
            "total_cost_usd": 0.01,
            "usage": {"output_tokens": 12},
            "result": "Files: a.py, b.py\nLocations: a.py:10-10, b.py:30-40",
        },
    ]
    run_path.write_text("\n".join(json.dumps(row) for row in rows))

    scored = score_run(
        label="claude",
        path=run_path,
        gt_blocks=[
            {"file": "a.py", "start": 10, "end": 20},
            {"file": "b.py", "start": 30, "end": 40},
        ],
    )

    assert scored["format"] == "claude"
    assert scored["turns"] == 2
    assert scored["lsp_route_tool_index"] == 1
    assert scored["native_lsp_available"] is False
    assert scored["native_lsp_tool_index"] is None
    assert scored["answer_span_count"] == 2
    assert scored["answer_diagnosis"] == "ok"
    assert scored["metrics"][5]["recall"] == 1.0
    assert scored["answer_all_metrics"]["recall"] == 1.0


def test_external_baseline_reports_native_lsp_surface(tmp_path):
    run_path = tmp_path / "claude_native.jsonl"
    rows = [
        {
            "type": "system",
            "subtype": "init",
            "tools": ["Read", "ide_definition"],
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "ide_definition",
                        "input": {"symbol": "target"},
                    }
                ]
            },
        },
        {
            "type": "result",
            "num_turns": 1,
            "usage": {"output_tokens": 8},
            "result": "Files: a.py\nLocations: a.py:10-20",
        },
    ]
    run_path.write_text("\n".join(json.dumps(row) for row in rows))

    scored = score_run(
        label="claude_native",
        path=run_path,
        gt_blocks=[{"file": "a.py", "start": 10, "end": 20}],
    )
    report = render_markdown([scored])

    assert scored["native_lsp_available"] is True
    assert scored["native_lsp_tool_index"] == 1
    assert (
        "| claude_native | claude | 1.000 | 1.000 | 1 | 1 | 1 | n/a | yes | 1 |"
        in report
    )


def test_external_baseline_scores_codex_jsonl_and_cancelled_mcp(tmp_path):
    run_path = tmp_path / "codex.jsonl"
    rows = [
        {"type": "thread.started", "thread_id": "t"},
        {
            "type": "item.started",
            "item": {
                "type": "mcp_tool_call",
                "server": "codeminer",
                "tool": "lsp_route",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "codeminer",
                "tool": "lsp_route",
                "error": {"message": "user cancelled MCP tool call"},
            },
        },
        {
            "type": "item.started",
            "item": {"type": "command_execution", "command": "/bin/bash -lc rg"},
        },
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "Files: a.py\nLocations: a.py:10-20",
            },
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 100, "output_tokens": 20},
        },
    ]
    run_path.write_text("\n".join(json.dumps(row) for row in rows))
    run_path.with_suffix(".stderr").write_text(
        "MCP server log message: Internal Server Error\n"
        "unrelated stderr\n"
        "MCP server log message: Internal Server Error\n"
    )

    scored = score_run(
        label="codex",
        path=run_path,
        gt_blocks=[{"file": "a.py", "start": 10, "end": 20}],
    )
    report = render_markdown([scored])

    assert scored["format"] == "codex"
    assert scored["mcp_cancelled"] == 1
    assert scored["mcp_stderr_internal_errors"] == 2
    assert scored["lsp_route_tool_index"] == 1
    assert scored["metrics"][5]["recall"] == 1.0
    assert "| codex | codex | 1.000 | 1.000 | 1 | n/a | 2 | 1 |" in report


def test_external_baseline_separates_top_k_from_all_parsed_recall(tmp_path):
    run_path = tmp_path / "codex_many_spans.jsonl"
    rows = [
        {"type": "thread.started", "thread_id": "t"},
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": (
                    "Files: a.py, b.py, c.py\n"
                    "Locations: a.py:10-20, h.py:1-2, h.py:3-4, "
                    "h.py:5-6, h.py:7-8, b.py:30-40, c.py:50-60"
                ),
            },
        },
    ]
    run_path.write_text("\n".join(json.dumps(row) for row in rows))

    scored = score_run(
        label="codex_many",
        path=run_path,
        gt_blocks=[
            {"file": "a.py", "start": 10, "end": 20},
            {"file": "b.py", "start": 30, "end": 40},
            {"file": "c.py", "start": 50, "end": 60},
        ],
    )

    assert scored["metrics"][5]["recall"] == 1 / 3
    assert scored["answer_all_metrics"]["recall"] == 1.0
    assert scored["answer_span_count"] == 7
    assert scored["answer_diagnosis"] == "rank_gap"


def test_external_baseline_diagnoses_path_alias_gap(tmp_path):
    run_path = tmp_path / "codex_alias.jsonl"
    rows = [
        {"type": "thread.started", "thread_id": "t"},
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "Files: pkg/target.py\nLocations: target.py:10-20",
            },
        },
    ]
    run_path.write_text("\n".join(json.dumps(row) for row in rows))

    scored = score_run(
        label="codex_alias",
        path=run_path,
        gt_blocks=[{"file": "pkg/target.py", "start": 10, "end": 20}],
    )
    report = render_markdown([scored])

    assert scored["metrics"][5]["recall"] == 0.0
    assert scored["answer_all_metrics"]["recall"] == 0.0
    assert scored["answer_path_alias_metrics"]["recall"] == 1.0
    assert scored["answer_path_alias_metrics"]["alias_hits"] == 1
    assert scored["answer_diagnosis"] == "path_alias_gap"
    assert "path_alias_gap" in report


def test_external_baseline_diagnoses_format_gap(tmp_path):
    run_path = tmp_path / "codex_format.jsonl"
    rows = [
        {"type": "thread.started", "thread_id": "t"},
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "The target is pkg/target.py around the setup code.",
            },
        },
    ]
    run_path.write_text("\n".join(json.dumps(row) for row in rows))

    scored = score_run(
        label="codex_format",
        path=run_path,
        gt_blocks=[{"file": "pkg/target.py", "start": 10, "end": 20}],
    )

    assert scored["answer_span_count"] == 0
    assert scored["answer_diagnosis"] == "format_gap"


def test_external_baseline_scores_codeminer_cell_reference(tmp_path):
    cell_path = tmp_path / "codeminer_cell.json"
    cell_path.write_text(
        json.dumps(
            {
                "query_id": "q1",
                "subset_id": "preinj_graph_scheduled",
                "total_turns": 4,
                "total_tokens": 1200,
                "metrics": {"answer_blocks": {"5": {"recall": 1.0}}},
                "answer_all_metrics": {"recall": 1.0},
                "answer_path_alias_metrics": {"recall": 1.0},
                "answer_diagnosis": "ok",
                "answer_spans": [{"file": "pkg/a.py", "start": 10, "end": 20}],
                "tool_calls": [{"skill_id": "read"}, {"skill_id": "lsp_route"}],
                "trace": {
                    "events": [
                        {
                            "kind": "scheduled_context",
                            "turn": 2,
                            "data": {"operation": "lsp_route"},
                        },
                        {
                            "kind": "scheduled_context",
                            "turn": 3,
                            "data": {"operation": "lsp_route_finalization_guard"},
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    scored = score_codeminer_cell(label="codeminer_route", path=cell_path)
    matrix = build_comparison_matrix(
        external_rows=[
            {
                "label": "codex_alias",
                "format": "codex",
                "metrics": {5: {"recall": 0.0}},
                "answer_all_metrics": {"recall": 0.0},
                "answer_diagnosis": "path_alias_gap",
                "tool_count": 9,
                "lsp_route_tool_index": 6,
                "native_lsp_available": False,
                "mcp_cancelled": 0,
                "mcp_stderr_internal_errors": 0,
            }
        ],
        codeminer_rows=[scored],
    )

    assert scored["format"] == "codeminer_cell"
    assert scored["lsp_route_tool_index"] == 2
    assert scored["scheduled_lsp_route"] is True
    assert scored["scheduled_lsp_route_turn"] == 2
    assert scored["scheduled_final_guard"] is True
    assert matrix["status"] == "ready"
    assert matrix["reference_label"] == "codeminer_route"
    assert matrix["rows"][0]["gap_category"] == "answer_contract_gap"
    assert matrix["rows"][0]["delta_rec5"] == -1.0
    repair_plan = build_external_repair_plan(
        matrix=matrix,
        gate=None,
        command_manifest={"suggested_command": "python rerun.py"},
    )
    assert repair_plan["status"] == "ready"
    assert repair_plan["rerun_command"] == "python rerun.py"
    assert repair_plan["actions"][0]["repair_action"] == (
        "tighten_external_answer_contract"
    )
    assert repair_plan["actions"][0]["repair_surface"] == "external_prompt_schema"
    rerun_specs = build_external_rerun_specs(
        repair_plan=repair_plan,
        command_manifest={
            "output_dir": str(tmp_path / "out"),
            "suggested_command": "python rerun.py",
        },
    )
    assert rerun_specs["status"] == "ready"
    assert rerun_specs["specs"][0]["launcher_status"] == "ready"
    assert rerun_specs["specs"][0]["requirement"] == "capture_jsonl"
    assert rerun_specs["specs"][0]["output_paths"]["jsonl"].endswith(
        "codex_alias.tighten_external_answer_contract.jsonl"
    )
    assert rerun_specs["specs"][0]["validation_command"] == "python rerun.py"


def test_external_baseline_writes_comparison_matrix(tmp_path, capsys):
    gt_path = tmp_path / "gt.json"
    run_path = tmp_path / "codex_alias.jsonl"
    cell_path = tmp_path / "codeminer_cell.json"
    output_dir = tmp_path / "out"
    gt_path.write_text(
        json.dumps({"gt_code_blocks": [{"file": "pkg/a.py", "start": 10, "end": 20}]}),
        encoding="utf-8",
    )
    run_path.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"type": "thread.started", "thread_id": "t"},
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "Files: pkg/a.py\nLocations: a.py:10-20",
                    },
                },
            ]
        ),
        encoding="utf-8",
    )
    cell_path.write_text(
        json.dumps(
            {
                "query_id": "q1",
                "subset_id": "preinj_graph_scheduled",
                "total_turns": 4,
                "metrics": {"answer_blocks": {"5": {"recall": 1.0}}},
                "answer_all_metrics": {"recall": 1.0},
                "answer_path_alias_metrics": {"recall": 1.0},
                "answer_diagnosis": "ok",
                "trace": {
                    "events": [
                        {
                            "kind": "scheduled_context",
                            "turn": 2,
                            "data": {"operation": "lsp_route"},
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "--gt-cell",
            str(gt_path),
            "--run",
            f"codex_alias={run_path}",
            "--codeminer-cell",
            f"codeminer_route={cell_path}",
            "--reference-label",
            "codeminer_route",
            "--output-dir",
            str(output_dir),
        ]
    )
    captured = capsys.readouterr()
    matrix = json.loads(
        (output_dir / "external_agent_comparison_matrix.json").read_text()
    )
    command = json.loads((output_dir / "external_agent_command.json").read_text())
    repair_plan = json.loads(
        (output_dir / "external_agent_repair_plan.json").read_text()
    )
    rerun_specs = json.loads(
        (output_dir / "external_agent_rerun_specs.json").read_text()
    )

    assert rc == 0
    assert captured.err == ""
    assert "External Agent Comparison Matrix" in captured.out
    assert "External Agent Repair Plan" in captured.out
    assert "External Agent Rerun Specs" in captured.out
    assert matrix["rows"][0]["gap_category"] == "answer_contract_gap"
    assert repair_plan["actions"][0]["repair_action"] == (
        "tighten_external_answer_contract"
    )
    assert repair_plan["actions"][0]["gap_category"] == "answer_contract_gap"
    assert rerun_specs["specs"][0]["launcher_status"] == "ready"
    assert rerun_specs["specs"][0]["prompt_delta"]
    assert command["codeminer_cells"] == [
        {"label": "codeminer_route", "path": str(cell_path)}
    ]
    assert command["reference_label"] == "codeminer_route"
    assert "--codeminer-cell" in command["suggested_command"]


def test_external_baseline_marks_mcp_policy_rerun_requirement(tmp_path):
    repair_plan = {
        "status": "ready",
        "rerun_command": "python rerun.py",
        "actions": [
            {
                "label": "codex_cancelled",
                "gap_category": "mcp_policy_gap",
                "repair_action": "rerun_external_with_mcp_policy",
                "repair_surface": "external_mcp_policy",
                "suggested_prompt_delta": "Allow CodeMiner MCP calls.",
                "evidence": {"mcp_cancelled": 4},
            }
        ],
    }

    rerun_specs = build_external_rerun_specs(
        repair_plan=repair_plan,
        command_manifest={"output_dir": str(tmp_path / "out")},
    )

    assert rerun_specs["specs"][0]["launcher_status"] == "needs_policy"
    assert rerun_specs["specs"][0]["requirement"] == "allow_codeminer_mcp_calls"
    assert rerun_specs["specs"][0]["output_paths"]["stderr"].endswith(
        "codex_cancelled.rerun_external_with_mcp_policy.stderr"
    )


def test_external_baseline_run_gate_passes_named_schema_run(tmp_path, capsys):
    gt_path = tmp_path / "gt.json"
    run_path = tmp_path / "codex_schema.jsonl"
    output_dir = tmp_path / "out"
    gt_path.write_text(
        json.dumps({"gt_code_blocks": [{"file": "a.py", "start": 10, "end": 20}]}),
        encoding="utf-8",
    )
    run_path.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"type": "thread.started", "thread_id": "t"},
                {
                    "type": "item.started",
                    "item": {
                        "type": "mcp_tool_call",
                        "server": "codeminer",
                        "tool": "lsp_route",
                    },
                },
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "Files: a.py\nLocations: a.py:10-20",
                    },
                },
            ]
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "--gt-cell",
            str(gt_path),
            "--run",
            f"codex_schema={run_path}",
            "--output-dir",
            str(output_dir),
            "--require-run-gate",
            "codex_schema",
            "--gate-require-lsp-route",
        ]
    )
    captured = capsys.readouterr()
    gate = json.loads((output_dir / "external_agent_gate.json").read_text())
    command = json.loads((output_dir / "external_agent_command.json").read_text())

    assert rc == 0
    assert captured.err == ""
    assert gate["status"] == "pass"
    assert gate["promote"] is True
    assert gate["runs"][0]["evidence"]["lsp_route_tool_index"] == 1
    assert command["command_manifest_version"] == 1
    assert command["gt_cell"] == str(gt_path)
    assert command["runs"] == [{"label": "codex_schema", "path": str(run_path)}]
    assert command["gate"]["labels"] == ["codex_schema"]
    assert command["gate"]["require_lsp_route"] is True
    assert "--require-run-gate codex_schema" in command["suggested_command"]
    assert "--gate-require-lsp-route" in command["suggested_command"]


def test_external_baseline_run_gate_fails_with_blockers(tmp_path, capsys):
    gt_path = tmp_path / "gt.json"
    run_path = tmp_path / "codex_bad.jsonl"
    output_dir = tmp_path / "out"
    gt_path.write_text(
        json.dumps({"gt_code_blocks": [{"file": "pkg/a.py", "start": 10, "end": 20}]}),
        encoding="utf-8",
    )
    run_path.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"type": "thread.started", "thread_id": "t"},
                {
                    "type": "item.started",
                    "item": {
                        "type": "mcp_tool_call",
                        "server": "codeminer",
                        "tool": "lsp_route",
                    },
                },
                {
                    "type": "item.completed",
                    "item": {
                        "type": "mcp_tool_call",
                        "server": "codeminer",
                        "tool": "lsp_route",
                        "error": {"message": "cancelled"},
                    },
                },
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "Files: pkg/a.py\nLocations: a.py:10-20",
                    },
                },
            ]
        ),
        encoding="utf-8",
    )
    run_path.with_suffix(".stderr").write_text("Internal Server Error\n")

    rc = main(
        [
            "--gt-cell",
            str(gt_path),
            "--run",
            f"codex_bad={run_path}",
            "--output-dir",
            str(output_dir),
            "--require-run-gate",
            "codex_bad",
            "--gate-require-lsp-route",
        ]
    )
    captured = capsys.readouterr()
    gate = json.loads((output_dir / "external_agent_gate.json").read_text())

    assert rc == 2
    assert "external agent gate failed" in captured.err
    assert "codex_bad:rec5_below_gate" in captured.err
    assert "codex_bad:answer_diagnosis_mismatch" in captured.err
    assert "codex_bad:mcp_cancelled_above_gate" in captured.err
    assert "codex_bad:stderr_internal_errors_above_gate" in captured.err
    assert gate["status"] == "fail"
    assert gate["promote"] is False


def test_external_baseline_run_gate_requires_native_lsp():
    gate = evaluate_run_gate(
        [
            {
                "label": "claude",
                "metrics": {5: {"recall": 1.0}},
                "answer_diagnosis": "ok",
                "mcp_cancelled": 0,
                "mcp_stderr_internal_errors": 0,
                "native_lsp_available": False,
            }
        ],
        labels=["claude"],
        require_native_lsp=True,
    )

    assert gate["status"] == "fail"
    assert gate["blockers"] == ["claude:missing_native_lsp_surface"]
