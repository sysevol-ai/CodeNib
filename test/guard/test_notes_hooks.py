# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for anchored notes and the agent hooks of ``codenib guard``."""

from __future__ import annotations

import json
import textwrap
from types import SimpleNamespace

import pytest

from codenib.guard import hooks
from codenib.guard.notes import (
    NoteError,
    add_note,
    anchor_states,
    confirm_note,
    definition_hash,
    load_notes,
)

SOURCE = textwrap.dedent("""\
    class Loader:
        @staticmethod
        def load(path):
            return open(path).read()


    def parse(text):
        return text.split()
    """)


def test_note_round_trip_and_staleness(tmp_path, write_files):
    write_files(tmp_path, {"pkg/loader.py": SOURCE})
    note = add_note(
        tmp_path,
        "Loader reads the whole file.\nCallers must not pass sockets.",
        ["pkg/loader.py:Loader.load", "pkg/loader.py:parse"],
        kind="invariant",
        source="https://example.com/pr/1",
    )
    assert (
        note.path.as_posix() == ".codenib/notes/loader-reads-the-whole-file-callers.md"
    )
    (loaded,), errors = load_notes(tmp_path)
    assert errors == []
    assert loaded.kind == "invariant" and loaded.title == "Loader reads the whole file."
    assert {s.state for s in anchor_states(tmp_path, loaded)} == {"current"}

    write_files(
        tmp_path,
        {
            "pkg/loader.py": SOURCE.replace("text.split()", "text.split(',')").replace(
                "class Loader:", "class Reader:"
            )
        },
    )
    states = {s.anchor.symbol: s.state for s in anchor_states(tmp_path, loaded)}
    assert states == {
        "pkg/loader.py:Loader.load": "missing",
        "pkg/loader.py:parse": "changed",
    }
    with pytest.raises(NoteError):
        confirm_note(tmp_path, loaded)


def test_hash_ignores_indentation_and_blank_lines(tmp_path, write_files):
    write_files(
        tmp_path,
        {
            "a.py": "def f():\n    return 1\n",
            "b.py": "class K:\n\n    def f():\n        return 1\n",
        },
    )
    assert definition_hash(tmp_path, "a.py:f") == definition_hash(tmp_path, "b.py:K.f")
    assert definition_hash(tmp_path, "a.py:missing") is None
    with pytest.raises(NoteError):
        definition_hash(tmp_path, "a.py")


def test_confirm_records_the_current_code(tmp_path, write_files):
    write_files(tmp_path, {"pkg/loader.py": SOURCE})
    note = add_note(tmp_path, "Parse splits on whitespace.", ["pkg/loader.py:parse"])
    write_files(
        tmp_path, {"pkg/loader.py": SOURCE.replace("return text", "return  text")}
    )
    (loaded,), _ = load_notes(tmp_path)
    assert anchor_states(tmp_path, loaded)[0].state == "changed"
    confirm_note(tmp_path, loaded)
    (again,), _ = load_notes(tmp_path)
    assert anchor_states(tmp_path, again)[0].state == "current"
    assert again.path == note.path


def test_install_keeps_other_hooks_and_is_idempotent(tmp_path):
    settings = tmp_path / ".claude" / "settings.local.json"
    settings.parent.mkdir()
    other = {"type": "command", "command": "make lint"}
    settings.write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [other]}]}, "model": "x"})
    )
    for _ in range(2):
        hooks.install(tmp_path, "claude", python="/usr/bin/python3")
    data = json.loads(settings.read_text())
    assert data["model"] == "x"
    stop = [h for group in data["hooks"]["Stop"] for h in group["hooks"]]
    assert stop[0] == other
    assert [h["command"] for h in stop[1:]] == [
        "/usr/bin/python3 -m codenib guard hook --agent claude --event stop"
    ]
    assert len(data["hooks"]["SessionStart"]) == 1

    hooks.uninstall(tmp_path, "claude")
    data = json.loads(settings.read_text())
    assert data["hooks"] == {"Stop": [{"hooks": [other]}]}


def test_codex_install_writes_project_hooks(tmp_path):
    path = hooks.install(tmp_path, "codex", python="python3")
    assert path == tmp_path / ".codex" / "hooks.json"
    data = json.loads(path.read_text())
    assert set(data["hooks"]) == {"SessionStart", "Stop"}
    assert hooks.uninstall(tmp_path, "codex") == path
    assert json.loads(path.read_text()) == {}


def test_output_shapes_for_each_agent():
    claude = json.loads(hooks._output("claude", "text"))
    assert claude == {
        "hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": "text"}
    }
    assert json.loads(hooks._output("codex", "text")) == {
        "decision": "block",
        "reason": "text",
    }


def test_stop_never_continues_a_continued_turn():
    out, reason = hooks.stop(
        "claude", {"stop_hook_active": True, "cwd": "/nonexistent"}
    )
    assert out == "" and "already continued" in reason


def test_first_run_event_is_off_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "home"))
    sent = []
    monkeypatch.setattr(
        hooks.urllib.request,
        "urlopen",
        lambda *a, **k: sent.append(a) or SimpleNamespace(close=lambda: None),
    )
    hooks._report_first_run()
    assert sent == []
    hooks.set_first_run_sharing(True)
    hooks._report_first_run()
    hooks._report_first_run()
    assert len(sent) == 1
    body = json.loads(sent[0][0].data)
    assert body["event"] == "guard_first_run" and body["surface"] == "cli"
    assert set(body) == {"event", "surface", "visit"}
    assert sent[0][0].full_url.endswith("/api/experience-events")


def test_hook_errors_never_fail_the_turn(monkeypatch, capsys):
    def boom(agent, payload):
        raise RuntimeError("graph broke")

    monkeypatch.setattr(hooks, "stop", boom)
    monkeypatch.setattr(hooks, "_repo_root", lambda cwd: None)
    assert hooks.run_hook("claude", "stop", "{}") == 0
    assert capsys.readouterr().out == ""
