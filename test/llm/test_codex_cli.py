# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""``codex/<model>`` runs the local ``codex exec`` as a text model."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest
from pydantic import BaseModel

from codenib.llm import codex_cli
from codenib.llm.claude_code import chat_for
from codenib.llm.codex_cli import CodexChat, CodexError, strict_schema

FAKE = textwrap.dedent("""
    import json, os, sys
    from pathlib import Path
    argv = sys.argv[1:]
    prompt = sys.stdin.read()
    schema = None
    if "--output-schema" in argv:
        schema = json.loads(Path(argv[argv.index("--output-schema") + 1]).read_text())
    Path(os.environ["FAKE_CODEX_LOG"]).write_text(json.dumps(
        {"argv": argv, "prompt": prompt, "cwd": os.getcwd(), "schema": schema,
         "gh": os.environ.get("GH_TOKEN")}))
    mode = os.environ.get("FAKE_CODEX_MODE", "")
    if mode == "error":
        print(json.dumps({"type": "error", "message": "Not signed in. Run codex login."}))
        sys.exit(1)
    print(json.dumps({"type": "thread.started", "thread_id": "t"}))
    print(json.dumps({"type": "turn.completed",
                      "usage": {"input_tokens": 9, "cached_input_tokens": 4,
                                "output_tokens": 3}}))
    if mode == "silent":
        sys.exit(0)
    out = Path(argv[argv.index("--output-last-message") + 1])
    if schema is not None:
        out.write_text(json.dumps({"name": "get", "note": None}))
    else:
        out.write_text("  the answer  ")
    """)


@pytest.fixture
def fake_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "codex.py"
    script.write_text(FAKE)
    launcher = tmp_path / "codex"
    launcher.write_text(f'#!/bin/sh\nexec {sys.executable} {script} "$@"\n')
    launcher.chmod(0o755)
    log = tmp_path / "call.json"
    monkeypatch.setenv("CODENIB_CODEX_EXECUTABLE", str(launcher))
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    monkeypatch.setenv("GH_TOKEN", "secret")
    return log


def test_a_completion_runs_codex_read_only_from_an_empty_dir(fake_codex: Path):
    chat = chat_for("codex/gpt-5.5", temperature=0.2, max_tokens=99, api_key="x")

    text = chat.complete(
        [
            {"role": "system", "content": "Answer in lowercase."},
            {"role": "user", "content": [{"type": "text", "text": "Which method?"}]},
        ]
    )

    assert isinstance(chat, CodexChat)
    assert text == "the answer"
    call = json.loads(fake_codex.read_text())
    argv = call["argv"]
    assert argv[0] == "exec"
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("--model") + 1] == "gpt-5.5"
    assert "--ephemeral" in argv and "--skip-git-repo-check" in argv
    assert argv[-1] == "-"
    assert "Answer in lowercase." in call["prompt"]
    assert "Which method?" in call["prompt"]
    assert "codenib-codex-empty-" in call["cwd"]
    assert call["gh"] is None


def test_the_default_model_passes_no_model_flag(fake_codex: Path):
    assert codex_cli.run("hi", model="codex/default")["text"] == "the answer"
    assert "--model" not in json.loads(fake_codex.read_text())["argv"]


def test_structured_output_uses_a_strict_schema_and_drops_nulls(fake_codex: Path):
    class Pick(BaseModel):
        name: str
        note: str | None = None

    picked = (
        chat_for("codex/default")
        .with_structured_output(Pick)
        .invoke([type("M", (), {"role": "user", "content": "pick"})()])
    )

    assert picked == Pick(name="get")
    schema = json.loads(fake_codex.read_text())["schema"]
    assert schema["additionalProperties"] is False
    assert sorted(schema["required"]) == ["name", "note"]


def test_usage_comes_from_the_events(fake_codex: Path):
    usage = codex_cli.run("hi", model="codex/default")["usage"]
    assert usage["prompt_tokens"] == 9 and usage["completion_tokens"] == 3


def test_a_failure_names_what_codex_said(
    fake_codex: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("FAKE_CODEX_MODE", "error")
    with pytest.raises(CodexError, match="Not signed in"):
        chat_for("codex/default").complete([{"role": "user", "content": "x"}])


def test_no_final_message_is_an_error(
    fake_codex: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("FAKE_CODEX_MODE", "silent")
    with pytest.raises(CodexError, match="no answer"):
        codex_cli.run("x", model="codex/default")


def test_a_missing_codex_says_how_to_get_it(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CODENIB_CODEX_EXECUTABLE", "/nonexistent/codex")
    assert not codex_cli.available()
    with pytest.raises(CodexError, match="install Codex"):
        codex_cli.run("x", model="codex/default")


def test_strict_schema_makes_optional_properties_nullable():
    schema = strict_schema(
        {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "line": {"type": "integer"},
                        },
                        "required": ["path"],
                    },
                }
            },
            "required": ["items"],
        }
    )
    item = schema["properties"]["items"]["items"]
    assert item["required"] == ["path", "line"]
    assert item["properties"]["line"]["type"] == ["integer", "null"]
    assert item["properties"]["path"]["type"] == "string"
    assert (
        item["additionalProperties"] is False
        and schema["additionalProperties"] is False
    )


def test_model_names():
    assert codex_cli.is_codex_model("codex/default")
    assert not codex_cli.is_codex_model("claude-code/sonnet")
    assert not codex_cli.is_codex_model(None)
