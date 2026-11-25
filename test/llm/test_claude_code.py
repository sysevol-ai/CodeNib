# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""``claude-code/<model>`` runs the local ``claude`` CLI as a text model."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest
from pydantic import BaseModel

from codenib.llm import claude_code
from codenib.llm.claude_code import (
    ClaudeCodeChat,
    ClaudeCodeError,
    chat_for,
    render_prompt,
)

FAKE = textwrap.dedent("""
    import json, os, sys
    from pathlib import Path
    argv = sys.argv[1:]
    prompt = sys.stdin.read()
    Path(os.environ["FAKE_CLAUDE_LOG"]).write_text(json.dumps(
        {"argv": argv, "prompt": prompt, "cwd": os.getcwd(),
         "gh": os.environ.get("GH_TOKEN")}))
    if os.environ.get("FAKE_CLAUDE_MODE") == "error":
        print(json.dumps({"type": "result", "is_error": True, "result": "Not logged in"}))
        sys.exit(1)
    result = {"type": "result", "subtype": "success", "is_error": False,
              "result": "  the answer  ", "total_cost_usd": 0.01,
              "usage": {"input_tokens": 7, "cache_read_input_tokens": 3,
                        "output_tokens": 2}}
    if "--json-schema" in argv:
        result["structured_output"] = {"name": "get"}
    print(json.dumps(result))
    """)


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "claude.py"
    script.write_text(FAKE)
    launcher = tmp_path / "claude"
    launcher.write_text(f'#!/bin/sh\nexec {sys.executable} {script} "$@"\n')
    launcher.chmod(0o755)
    log = tmp_path / "call.json"
    monkeypatch.setenv("CODENIB_CLAUDE_EXECUTABLE", str(launcher))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("GH_TOKEN", "secret")
    return log


def test_a_completion_runs_claude_with_no_tools_from_an_empty_dir(fake_claude: Path):
    chat = chat_for("claude-code/sonnet", temperature=0.2, max_tokens=99, api_key="x")

    text = chat.complete(
        [
            {"role": "system", "content": "Answer in lowercase."},
            {"role": "user", "content": [{"type": "text", "text": "Which method?"}]},
        ]
    )

    assert isinstance(chat, ClaudeCodeChat)
    assert text == "the answer"
    call = json.loads(fake_claude.read_text())
    argv = call["argv"]
    assert argv[:3] == ["-p", "--output-format", "json"]
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--system-prompt") + 1] == "Answer in lowercase."
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv
    assert call["prompt"] == "Which method?"
    assert "codenib-claude-" in call["cwd"]
    assert call["gh"] is None


def test_structured_output_uses_the_schema(fake_claude: Path):
    class Method(BaseModel):
        name: str

    from codenib.llm.litellm_chat import ChatMessage  # noqa: F401  (interface)

    result = (
        ClaudeCodeChat("claude-code/haiku")
        .with_structured_output(Method)
        .invoke([type("M", (), {"role": "user", "content": "Which method?"})()])
    )

    assert result == Method(name="get")
    argv = json.loads(fake_claude.read_text())["argv"]
    assert json.loads(argv[argv.index("--json-schema") + 1])["required"] == ["name"]


def test_a_failure_names_what_claude_said(
    fake_claude: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "error")
    with pytest.raises(ClaudeCodeError, match="Not logged in"):
        ClaudeCodeChat().complete([{"role": "user", "content": "hi"}])


def test_a_missing_claude_says_how_to_get_it(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CODENIB_CLAUDE_EXECUTABLE", "/nonexistent/claude")
    assert claude_code.available() is False
    with pytest.raises(ClaudeCodeError, match="install Claude Code"):
        ClaudeCodeChat().complete([{"role": "user", "content": "hi"}])


def test_conversations_and_tool_calls():
    system, prompt = render_prompt(
        [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "second"},
        ]
    )
    assert system == "S"
    assert prompt.startswith("User:\nfirst\n\nAssistant:\nreply\n\nUser:\nsecond")
    with pytest.raises(ClaudeCodeError, match="tool calls"):
        render_prompt(
            [{"role": "user", "content": "x"}, {"role": "tool", "content": "y"}]
        )


def test_other_models_keep_litellm():
    pytest.importorskip("litellm")
    from codenib.llm.litellm_chat import LiteLLMChat

    assert isinstance(chat_for("openai/gpt-4.1", api_key="k"), LiteLLMChat)
    assert ClaudeCodeChat("sonnet").model == "claude-code/sonnet"
    assert (
        ClaudeCodeChat("claude-code/a").cache_identity
        != ClaudeCodeChat("claude-code/b").cache_identity
    )
