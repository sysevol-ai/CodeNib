# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for edit-run audit artifacts."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from codeminer.agent.runner import AgentRunner
from codeminer.agent.skills.registry import SkillRegistry
from codeminer.llm.litellm_chat import LiteLLMChat
from scripts.agent_compile.lib.edit_audit import (
    AgentEditRunAuditError,
    begin_edit_run_audit,
    run_agent_with_edit_audit,
    run_edit_command_with_audit,
)
from scripts.agent_compile.run_edit_audit import main as edit_audit_main


@pytest.fixture(autouse=True)
def _reset_registry():
    SkillRegistry.reset()
    yield
    SkillRegistry.reset()


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _init_repo(path: Path) -> Path:
    path.mkdir()
    _run(["git", "init"], path)
    tracked = path / "tracked.py"
    tracked.write_text("value = 1\n", encoding="utf-8")
    _run(["git", "add", "tracked.py"], path)
    _run(
        [
            "git",
            "-c",
            "user.email=codeminer@example.test",
            "-c",
            "user.name=CodeMiner Test",
            "commit",
            "-m",
            "init",
        ],
        path,
    )
    return path


def _make_response(content=None, tool_calls=None):
    msg = SimpleNamespace(
        role="assistant",
        content=content,
        tool_calls=tool_calls,
    )
    choice = SimpleNamespace(message=msg)
    return SimpleNamespace(choices=[choice])


def _make_tool_call(tc_id, name, arguments_json):
    return SimpleNamespace(
        id=tc_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments_json),
    )


def test_edit_audit_records_diff_verification_and_revert_for_clean_repo(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    artifact_dir = tmp_path / "audit"

    session = begin_edit_run_audit(repo, artifact_dir, metadata={"cell_id": "unit"})
    (repo / "tracked.py").write_text("value = 2\n", encoding="utf-8")
    (repo / "created.py").write_text("created = True\n", encoding="utf-8")
    audit = session.finalize(
        verification_command=[sys.executable, "-c", "print('verified')"]
    )

    assert audit.dirty_at_start is False
    assert audit.changed_paths == ["created.py", "tracked.py"]
    assert audit.metadata["cell_id"] == "unit"
    assert audit.verification is not None
    assert audit.verification.exit_code == 0
    assert "verified" in audit.verification.stdout
    assert Path(audit.before_diff_path).read_text(encoding="utf-8") == ""
    assert "value = 2" in Path(audit.after_diff_path).read_text(encoding="utf-8")

    payload = json.loads(Path(audit.audit_json_path).read_text(encoding="utf-8"))
    assert payload["verification"]["command"][0] == sys.executable
    assert payload["revert_script_path"] == audit.revert_script_path

    _run([sys.executable, audit.revert_script_path], repo)

    assert (repo / "tracked.py").read_text(encoding="utf-8") == "value = 1\n"
    assert not (repo / "created.py").exists()
    assert _run(["git", "status", "--porcelain"], repo).stdout == ""


def test_edit_audit_revert_restores_dirty_start_state(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    artifact_dir = tmp_path / "audit"
    tracked = repo / "tracked.py"
    tracked.write_text("value = 'user dirty'\n", encoding="utf-8")

    session = begin_edit_run_audit(repo, artifact_dir)
    tracked.write_text("value = 'agent edit'\n", encoding="utf-8")
    audit = session.finalize()

    assert audit.dirty_at_start is True
    assert audit.changed_paths == ["tracked.py"]
    manifest = json.loads(Path(audit.revert_manifest_path).read_text(encoding="utf-8"))
    assert manifest["actions"][0]["kind"] == "restore_preimage"

    _run([sys.executable, audit.revert_script_path], repo)

    assert tracked.read_text(encoding="utf-8") == "value = 'user dirty'\n"
    assert _run(["git", "status", "--porcelain"], repo).stdout == " M tracked.py\n"


def test_run_edit_command_with_audit_records_edit_command(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    artifact_dir = tmp_path / "audit"
    edit_code = (
        "from pathlib import Path; "
        "Path('tracked.py').write_text('value = 3\\n', encoding='utf-8')"
    )

    audit = run_edit_command_with_audit(
        repo,
        [sys.executable, "-c", edit_code],
        artifact_dir=artifact_dir,
        verification_command=[sys.executable, "-c", "print('verified')"],
        metadata={"surface": "unit"},
    )

    assert audit.edit_command is not None
    assert audit.edit_command.exit_code == 0
    assert audit.edit_command.command[:2] == [sys.executable, "-c"]
    assert audit.verification is not None
    assert audit.verification.exit_code == 0
    assert audit.metadata["surface"] == "unit"
    payload = json.loads(Path(audit.audit_json_path).read_text(encoding="utf-8"))
    assert payload["edit_command"]["exit_code"] == 0
    assert payload["verification"]["exit_code"] == 0
    assert "value = 3" in Path(audit.after_diff_path).read_text(encoding="utf-8")


def test_run_edit_audit_cli_invokes_edit_command(tmp_path, capsys):
    repo = _init_repo(tmp_path / "repo")
    artifact_dir = tmp_path / "audit"
    edit_code = (
        "from pathlib import Path; "
        "Path('tracked.py').write_text('value = 4\\n', encoding='utf-8')"
    )

    code = edit_audit_main(
        [
            "--repo",
            str(repo),
            "--artifact-dir",
            str(artifact_dir),
            "--verify",
            f"{sys.executable} -c \"print('verified')\"",
            "--metadata",
            "surface=cli",
            "--",
            sys.executable,
            "-c",
            edit_code,
        ]
    )

    assert code == 0
    audit_json_path = Path(capsys.readouterr().out.strip())
    payload = json.loads(audit_json_path.read_text(encoding="utf-8"))
    assert payload["metadata"]["surface"] == "cli"
    assert payload["edit_command"]["exit_code"] == 0
    assert payload["verification"]["exit_code"] == 0
    assert payload["changed_paths"] == ["tracked.py"]


def test_run_agent_with_edit_audit_records_agent_result_and_revert(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    artifact_dir = tmp_path / "audit"
    edit_script = tmp_path / "edit_script.py"
    edit_script.write_text(
        "from pathlib import Path\n"
        "Path('tracked.py').write_text('value = 5\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(edit_script))}"

    llm = MagicMock(spec=LiteLLMChat)
    llm._call_raw.side_effect = [
        _make_response(
            tool_calls=[
                _make_tool_call(
                    "call_1",
                    "bash",
                    json.dumps({"command": command, "cwd": str(repo)}),
                )
            ]
        ),
        _make_response(content="Done."),
    ]
    runner = AgentRunner(
        llm,
        SkillRegistry(),
        force_localization_contract=False,
    )

    wrapped = run_agent_with_edit_audit(
        runner,
        "Edit tracked.py",
        repo,
        artifact_dir=artifact_dir,
        verification_command=[sys.executable, "-c", "print('verified')"],
        metadata={"cell_id": "agent-unit"},
    )

    audit = wrapped.audit
    assert wrapped.result.answer == "Done."
    assert audit.edit_command is None
    assert audit.verification is not None
    assert audit.verification.exit_code == 0
    assert audit.metadata["surface"] == "agent_runner"
    assert audit.metadata["cell_id"] == "agent-unit"
    assert audit.metadata["agent_result_json_path"] == wrapped.result_json_path
    assert audit.changed_paths == ["tracked.py"]
    assert "value = 5" in Path(audit.after_diff_path).read_text(encoding="utf-8")

    result_payload = json.loads(Path(wrapped.result_json_path).read_text())
    assert result_payload["answer"] == "Done."
    assert result_payload["total_turns"] == 2
    assert result_payload["tool_calls"][0]["skill_id"] == "bash"
    assert result_payload["tool_calls"][0]["envelope"]["status"] == "ok"
    envelope_metadata = result_payload["tool_calls"][0]["envelope"]["metadata"]
    assert envelope_metadata["permission_boundary"] == "process_exec_permissions"
    assert envelope_metadata["side_effects_possible"] is True

    audit_payload = json.loads(Path(audit.audit_json_path).read_text())
    assert audit_payload["metadata"]["agent_tool_call_count"] == 1
    assert audit_payload["metadata"]["agent_total_turns"] == 2

    _run([sys.executable, audit.revert_script_path], repo)

    assert (repo / "tracked.py").read_text(encoding="utf-8") == "value = 1\n"
    assert _run(["git", "status", "--porcelain"], repo).stdout == ""


def test_run_agent_with_edit_audit_finalizes_after_agent_exception(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    artifact_dir = tmp_path / "audit"

    class FailingRunner:
        def run(self, query, **kwargs):
            assert query == "Edit then fail"
            assert kwargs == {"max_turns": 2}
            (repo / "tracked.py").write_text("value = 9\n", encoding="utf-8")
            raise RuntimeError("agent crashed")

    with pytest.raises(AgentEditRunAuditError) as excinfo:
        run_agent_with_edit_audit(
            FailingRunner(),
            "Edit then fail",
            repo,
            artifact_dir=artifact_dir,
            max_turns=2,
            verification_command=[sys.executable, "-c", "print('verified')"],
        )

    err = excinfo.value
    audit = err.audit
    assert isinstance(err.original_error, RuntimeError)
    assert Path(err.error_json_path).exists()
    error_payload = json.loads(Path(err.error_json_path).read_text())
    assert error_payload["error_type"] == "RuntimeError"
    assert error_payload["message"] == "agent crashed"
    assert audit.changed_paths == ["tracked.py"]
    assert audit.verification is not None
    assert audit.verification.exit_code == 0
    assert audit.metadata["agent_failed"] is True
    assert audit.metadata["agent_error_type"] == "RuntimeError"
    assert audit.metadata["agent_error_json_path"] == err.error_json_path
    assert "value = 9" in Path(audit.after_diff_path).read_text(encoding="utf-8")

    _run([sys.executable, audit.revert_script_path], repo)

    assert (repo / "tracked.py").read_text(encoding="utf-8") == "value = 1\n"
    assert _run(["git", "status", "--porcelain"], repo).stdout == ""
