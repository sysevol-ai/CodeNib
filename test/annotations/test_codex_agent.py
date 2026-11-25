# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Tasks can run on the person's own Codex, as they run on Claude Code."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from codenib.annotations import agents
from codenib.annotations.agents import Codex, agent_for, detect_agents

FAKE = """
import json, os, sys
from pathlib import Path
argv = sys.argv[1:]
if argv[:2] == ["login", "status"]:
    if os.environ.get("FAKE_CODEX_SIGNED_OUT"):
        print("Not logged in"); sys.exit(1)
    print("Logged in using ChatGPT"); sys.exit(0)
prompt = sys.stdin.read()
schema = json.loads(Path(argv[argv.index("--output-schema") + 1]).read_text())
Path(os.environ["FAKE_CODEX_LOG"]).write_text(json.dumps(
    {"argv": argv, "prompt": prompt, "cwd": os.getcwd(), "schema": schema,
     "gh": os.environ.get("GH_TOKEN"), "edit": os.environ.get("CODENIB_TASK_EDIT")}))
print(json.dumps({"type": "turn.completed",
                  "usage": {"input_tokens": 11, "output_tokens": 5}}))
out = Path(argv[argv.index("--output-last-message") + 1])
out.write_text(json.dumps({"answer": "the session's", "files": None}))
"""


@pytest.fixture
def fake_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "codex.py"
    script.write_text(FAKE)
    launcher = tmp_path / "bin" / "codex"
    launcher.parent.mkdir()
    launcher.write_text(f'#!/bin/sh\nexec {sys.executable} {script} "$@"\n')
    launcher.chmod(0o755)
    log = tmp_path / "call.json"
    monkeypatch.setenv("CODENIB_CODEX_EXECUTABLE", str(launcher))
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    monkeypatch.setenv("GH_TOKEN", "secret")
    monkeypatch.delenv("CODENIB_TASK_AGENT_COMMAND", raising=False)
    agents._sign_in_cache.clear()
    return log


SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "files": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["answer"],
}


def test_agent_for_codex_returns_the_codex_adapter(fake_codex: Path):
    assert isinstance(agent_for("codex"), Codex)
    with pytest.raises(ValueError):
        agent_for("gemini")


def test_a_question_runs_codex_read_only_in_the_repository(
    fake_codex: Path, tmp_path: Path
):
    repo = tmp_path / "repo"
    repo.mkdir()

    run = Codex().run(
        "Which headers win?",
        cwd=repo,
        system="Answer from the code.",
        schema=SCHEMA,
        edit=False,
        log_dir=tmp_path / "log",
    )

    call = json.loads(fake_codex.read_text())
    assert run.error == ""
    # Strict output adds nulls for optional fields; they are dropped again.
    assert run.structured == {"answer": "the session's"}
    assert run.usage == {"input_tokens": 11, "output_tokens": 5}
    argv = call["argv"]
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("--cd") + 1] == str(repo)
    assert "--ephemeral" in argv
    assert call["edit"] == "0"
    assert "Answer from the code." in call["prompt"]
    assert "Which headers win?" in call["prompt"]
    assert call["gh"] is None, "GitHub tokens never reach the agent"
    assert list((tmp_path / "log").glob("prompt-*.txt"))


def test_a_change_may_write_inside_its_worktree(fake_codex: Path, tmp_path: Path):
    run = Codex().run(
        "Clearer error",
        cwd=tmp_path,
        system="",
        schema=SCHEMA,
        edit=True,
        log_dir=tmp_path / "log",
    )

    argv = json.loads(fake_codex.read_text())["argv"]
    assert run.error == ""
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"


def test_agents_list_reports_install_and_sign_in(
    fake_codex: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        agents.shutil, "which", lambda name: name if name.endswith("codex") else None
    )

    listed = {agent["id"]: agent for agent in detect_agents()}

    assert listed["claude-code"]["available"] is False
    assert listed["claude-code"]["signed_in"] is False
    assert listed["codex"]["available"] is True
    assert listed["codex"]["signed_in"] is True


def test_a_signed_out_codex_is_reported(
    fake_codex: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("FAKE_CODEX_SIGNED_OUT", "1")
    monkeypatch.setattr(
        agents.shutil, "which", lambda name: name if name.endswith("codex") else None
    )

    codex = next(agent for agent in detect_agents() if agent["id"] == "codex")

    assert codex["available"] is True
    assert codex["signed_in"] is False


def test_sign_in_checks_are_cached(fake_codex: Path, monkeypatch: pytest.MonkeyPatch):
    calls = []
    real = subprocess.run

    def counting(*args, **kwargs):
        calls.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(agents.subprocess, "run", counting)
    launcher = str(Path(fake_codex.parent / "bin" / "codex"))

    assert agents._signed_in("codex", launcher) is True
    assert agents._signed_in("codex", launcher) is True
    assert len(calls) == 1
