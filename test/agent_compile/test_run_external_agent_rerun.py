# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for external-agent rerun spec execution."""

from __future__ import annotations

import json
import shlex
import sys

from scripts.agent_compile.run_external_agent_rerun import main


def _write_specs(path, *, launcher_status="ready", requirement="capture_jsonl"):
    path.write_text(
        json.dumps(
            {
                "rerun_specs_version": 1,
                "status": "ready",
                "specs": [
                    {
                        "index": 1,
                        "label": "codex_alias",
                        "gap_category": "answer_contract_gap",
                        "repair_action": "tighten_external_answer_contract",
                        "repair_surface": "external_prompt_schema",
                        "launcher_status": launcher_status,
                        "requirement": requirement,
                        "prompt_delta": "Require repo-relative Locations.",
                        "output_paths": {
                            "jsonl": str(path.parent / "codex_alias.jsonl"),
                            "stderr": str(path.parent / "codex_alias.stderr"),
                        },
                        "validation_command": "python validate.py",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_external_agent_rerun_dry_run_writes_prompt_and_execution(tmp_path, capsys):
    spec_path = tmp_path / "external_agent_rerun_specs.json"
    execution_path = tmp_path / "execution.json"
    _write_specs(spec_path)

    rc = main(
        [
            "--spec-file",
            str(spec_path),
            "--execution-file",
            str(execution_path),
            "--command-template",
            f"{sys.executable} -c 'print(1)'",
        ]
    )
    captured = capsys.readouterr()
    execution = json.loads(execution_path.read_text())
    prompt = (tmp_path / "external_agent_rerun_prompt.1.md").read_text()

    assert rc == 0
    assert captured.out == ""
    assert captured.err == ""
    assert execution["status"] == "dry_run"
    assert execution["ready_to_execute"] is True
    assert execution["validation_command"] == "python validate.py"
    assert "Require repo-relative Locations." in prompt
    assert "Final Answer Contract" in prompt


def test_external_agent_rerun_materializes_provider_profile(tmp_path, capsys):
    spec_path = tmp_path / "external_agent_rerun_specs.json"
    execution_path = tmp_path / "execution.json"
    provider_path = tmp_path / "external_agent_provider_profile.1.json"
    _write_specs(spec_path)

    rc = main(
        [
            "--spec-file",
            str(spec_path),
            "--execution-file",
            str(execution_path),
            "--provider-profile",
            "codex_mcp_bypass",
        ]
    )
    captured = capsys.readouterr()
    execution = json.loads(execution_path.read_text())
    provider = json.loads(provider_path.read_text())

    assert rc == 0
    assert captured.out == ""
    assert captured.err == ""
    assert execution["status"] == "dry_run"
    assert execution["provider_profile_file"] == str(provider_path)
    assert execution["provider_profile"]["name"] == "codex_mcp_bypass"
    assert provider["name"] == "codex_mcp_bypass"
    assert "dangerously-bypass" in execution["command_template"]
    assert execution["argv"][0] == "codex"
    assert execution["required_allowances"] == ["allow_codeminer_mcp_calls"]
    assert execution["requirements_allowed"] is False
    assert execution["ready_to_execute"] is False


def test_external_agent_rerun_dry_run_does_not_block_policy_requirement(
    tmp_path, capsys
):
    spec_path = tmp_path / "external_agent_rerun_specs.json"
    execution_path = tmp_path / "execution.json"
    _write_specs(
        spec_path,
        launcher_status="needs_policy",
        requirement="allow_codeminer_mcp_calls",
    )

    rc = main(
        [
            "--spec-file",
            str(spec_path),
            "--execution-file",
            str(execution_path),
            "--command-template",
            f"{sys.executable} -c 'print(1)'",
        ]
    )
    captured = capsys.readouterr()
    execution = json.loads(execution_path.read_text())

    assert rc == 0
    assert captured.out == ""
    assert captured.err == ""
    assert execution["status"] == "dry_run"
    assert execution["required_allowances"] == ["allow_codeminer_mcp_calls"]
    assert execution["requirements_allowed"] is False
    assert execution["ready_to_execute"] is False


def test_external_agent_rerun_executes_template_and_captures_outputs(tmp_path, capsys):
    spec_path = tmp_path / "external_agent_rerun_specs.json"
    execution_path = tmp_path / "execution.json"
    _write_specs(spec_path)
    code = (
        "import sys; "
        'print(\'{"type":"thread.started"}\'); '
        "print('warning', file=sys.stderr)"
    )

    rc = main(
        [
            "--spec-file",
            str(spec_path),
            "--execution-file",
            str(execution_path),
            "--command-template",
            f"{sys.executable} -c {shlex.quote(code)}",
            "--execute",
        ]
    )
    captured = capsys.readouterr()
    execution = json.loads(execution_path.read_text())

    assert rc == 0
    assert captured.out == ""
    assert captured.err == ""
    assert execution["status"] == "pass"
    assert execution["returncode"] == 0
    assert "thread.started" in (tmp_path / "codex_alias.jsonl").read_text()
    assert (tmp_path / "codex_alias.stderr").read_text() == "warning\n"


def test_external_agent_rerun_blocks_policy_requirement_without_allowance(
    tmp_path, capsys
):
    spec_path = tmp_path / "external_agent_rerun_specs.json"
    execution_path = tmp_path / "execution.json"
    _write_specs(
        spec_path,
        launcher_status="needs_policy",
        requirement="allow_codeminer_mcp_calls",
    )

    rc = main(
        [
            "--spec-file",
            str(spec_path),
            "--execution-file",
            str(execution_path),
            "--command-template",
            f"{sys.executable} -c 'print(1)'",
            "--execute",
        ]
    )
    captured = capsys.readouterr()
    execution = json.loads(execution_path.read_text())

    assert rc == 5
    assert captured.out == ""
    assert captured.err == ""
    assert execution["status"] == "blocked"
    assert execution["required"] == ["allow_codeminer_mcp_calls"]


def test_external_agent_rerun_blocks_provider_requirement_without_allowance(
    tmp_path, capsys
):
    spec_path = tmp_path / "external_agent_rerun_specs.json"
    execution_path = tmp_path / "execution.json"
    _write_specs(spec_path)

    rc = main(
        [
            "--spec-file",
            str(spec_path),
            "--execution-file",
            str(execution_path),
            "--provider-profile",
            "codex_mcp_bypass",
            "--execute",
        ]
    )
    captured = capsys.readouterr()
    execution = json.loads(execution_path.read_text())

    assert rc == 5
    assert captured.out == ""
    assert captured.err == ""
    assert execution["status"] == "blocked"
    assert execution["required"] == ["allow_codeminer_mcp_calls"]


def test_external_agent_rerun_executes_policy_requirement_with_allowance(
    tmp_path, capsys
):
    spec_path = tmp_path / "external_agent_rerun_specs.json"
    execution_path = tmp_path / "execution.json"
    _write_specs(
        spec_path,
        launcher_status="needs_policy",
        requirement="allow_codeminer_mcp_calls",
    )

    rc = main(
        [
            "--spec-file",
            str(spec_path),
            "--execution-file",
            str(execution_path),
            "--command-template",
            f"{sys.executable} -c 'print(1)'",
            "--allow-requirement",
            "allow_codeminer_mcp_calls",
            "--execute",
        ]
    )
    captured = capsys.readouterr()
    execution = json.loads(execution_path.read_text())

    assert rc == 0
    assert captured.out == ""
    assert captured.err == ""
    assert execution["status"] == "pass"
