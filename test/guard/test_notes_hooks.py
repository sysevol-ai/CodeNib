# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for anchored notes and the agent hooks of ``codenib guard``."""

from __future__ import annotations

import json
import shlex
import textwrap
from types import SimpleNamespace

import pytest

from codenib.guard import hooks
from codenib.guard.impact import Impact
from codenib.guard.impact import TestAtRisk as AtRisk
from codenib.guard.notes import (
    NoteError,
    add_note,
    anchor_states,
    confirm_note,
    definition_hash,
    load_notes,
)
from codenib.guard.report import GuardReport, working_tree_diff, working_tree_digest

SOURCE = textwrap.dedent(
    """\
    class Loader:
        @staticmethod
        def load(path):
            return open(path).read()


    def parse(text):
        return text.split()
    """
)


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


def test_hook_command_prefers_the_script_beside_the_interpreter(tmp_path, monkeypatch):
    bin_dir = tmp_path / "my env" / "bin"
    bin_dir.mkdir(parents=True)
    interpreter = bin_dir / "python"
    interpreter.write_text("")
    for platform, name in (("win32", "codenib.exe"), ("linux", "codenib")):
        monkeypatch.setattr(hooks.sys, "platform", platform)
        script = bin_dir / name
        script.write_text("")
        script.chmod(0o755)
        command = hooks.hook_command("claude", "stop", str(interpreter))
        assert command == (
            f"{shlex.quote(str(script))} guard hook --agent claude --event stop"
        )
        script.unlink()


def test_hook_command_keeps_an_explicit_python_as_given(monkeypatch):
    for platform in ("win32", "linux"):
        monkeypatch.setattr(hooks.sys, "platform", platform)
        command = hooks.hook_command("codex", "stop", "C:/py dir/python.exe")
        assert command.startswith("'C:/py dir/python.exe' -m codenib guard hook")


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


def test_stop_skips_repositories_without_python(git_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "home"))
    git_repo.commit({"app.js": "console.log(1)\n"}, "init")
    (git_repo.root / "app.js").write_text("console.log(2)\n")
    payload = {"session_id": "s", "cwd": str(git_repo.root), "stop_hook_active": False}
    assert hooks.stop("claude", payload) == ("", "skipped: no Python files")


def _reported_session(git_repo, tmp_path, monkeypatch):
    """A session whose base is committed and whose current change was reported."""

    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "home"))
    base = git_repo.commit({"shop.py": "def total():\n    return 1\n"}, "init")
    (git_repo.root / "shop.py").write_text("def total():\n    return 2\n")
    (git_repo.root / "new_helper.py").write_text("X = 1\n")
    repo = hooks._repo_root(str(git_repo.root))
    hooks._update_session(
        repo, "s", base=base, reported=working_tree_digest(repo, base)
    )
    return {"session_id": "s", "cwd": str(git_repo.root), "stop_hook_active": False}


def test_stop_skips_a_reported_change_before_any_graph_work(
    git_repo, tmp_path, monkeypatch
):
    payload = _reported_session(git_repo, tmp_path, monkeypatch)

    def no_graph_work(*args, **kwargs):
        raise AssertionError("a reported change needs no graph")

    monkeypatch.setattr(hooks, "_wait_for_graph", no_graph_work)
    monkeypatch.setattr(hooks, "run_guard", no_graph_work)
    assert hooks.stop("claude", payload) == ("", "already reported this change")


@pytest.mark.parametrize(
    "edit",
    [
        lambda root: (root / "shop.py").write_text("def total():\n    return 3\n"),
        lambda root: (root / "another_new.py").write_text("Y = 2\n"),
    ],
    ids=["edited-file", "new-untracked-file"],
)
def test_stop_runs_the_guard_for_a_change_not_yet_reported(
    git_repo, tmp_path, monkeypatch, edit
):
    payload = _reported_session(git_repo, tmp_path, monkeypatch)
    edit(git_repo.root)
    waited = []
    monkeypatch.setattr(
        hooks, "_wait_for_graph", lambda repo, commit: waited.append(commit) or False
    )
    monkeypatch.setattr(hooks, "failed_snapshot", lambda repo, commit: None)
    out, reason = hooks.stop("claude", payload)
    assert out == "" and reason.endswith("still building")
    assert len(waited) == 1


def _no_graph_work(*args, **kwargs):
    raise AssertionError("this stop needs no graph")


def test_stop_returns_at_once_for_an_unchanged_tree(git_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "home"))
    git_repo.commit({"shop.py": "def total():\n    return 1\n"}, "init")
    monkeypatch.setattr(hooks, "_wait_for_graph", _no_graph_work)
    monkeypatch.setattr(hooks, "run_guard", _no_graph_work)
    payload = {"session_id": "s", "cwd": str(git_repo.root), "stop_hook_active": False}
    assert hooks.stop("claude", payload) == ("", "no changes since the session base")


def test_stop_still_checks_notes_on_an_unchanged_tree(git_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "home"))
    git_repo.commit(
        {"shop.py": "def total():\n    return 1\n", ".codenib/notes/n.md": "note\n"},
        "init",
    )
    monkeypatch.setattr(hooks, "_wait_for_graph", lambda repo, commit: False)
    monkeypatch.setattr(hooks, "failed_snapshot", lambda repo, commit: None)
    payload = {"session_id": "s", "cwd": str(git_repo.root), "stop_hook_active": False}
    assert hooks.stop("claude", payload)[1].endswith("still building")


def _report(test_ids, repo, base):
    """A report with fixed findings for the working tree's current change."""

    diff, untracked = working_tree_diff(repo, base)
    report = GuardReport(
        repository=str(repo),
        base=base,
        base_commit=base,
        impact=Impact(tests=[AtRisk(t, t, 1, ()) for t in test_ids]),
        diff_sha256=hooks.change_digest(diff, untracked),
    )
    report.tracked_diff_sha256 = hooks.change_digest(diff, [])
    return report


def test_same_findings_for_the_same_edits_are_reported_once(
    git_repo, tmp_path, monkeypatch
):
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "home"))
    git_repo.commit({"shop.py": "def total():\n    return 1\n"}, "init")
    monkeypatch.setattr(hooks, "_wait_for_graph", lambda repo, commit: True)
    monkeypatch.setattr(
        hooks, "run_guard", lambda repo, base: _report(["t::a", "t::b"], repo, base)
    )
    monkeypatch.setattr(hooks, "render_for_agent", lambda report: "tests at risk")
    monkeypatch.setattr(hooks, "_report_first_run", lambda: None)
    payload = {"session_id": "s", "cwd": str(git_repo.root), "stop_hook_active": False}

    (git_repo.root / "shop.py").write_text("def total():\n    return 2\n")
    assert hooks.stop("claude", payload)[0]  # first report
    (git_repo.root / "notes.txt").write_text("a new file, not in the graph\n")
    assert hooks.stop("claude", payload) == (
        "",
        "already reported these findings for the same edits",
    )
    assert hooks.stop("claude", payload) == ("", "already reported this change")
    (git_repo.root / "shop.py").write_text("def total():\n    return 3\n")
    assert hooks.stop("claude", payload)[0]  # new edits: the same tests are repeated


def test_findings_digest_ignores_order():
    def report(ids):
        tests = [AtRisk(t, t, 1, ()) for t in ids]
        return GuardReport("r", "HEAD", "c", Impact(tests=tests))

    assert report(["a", "b"]).findings_sha256 == report(["b", "a"]).findings_sha256
    assert report(["a"]).findings_sha256 != report(["a", "b"]).findings_sha256


def test_a_failed_build_is_recorded_and_retried_later(git_repo, tmp_path, monkeypatch):
    import os

    from codenib.guard import snapshot

    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "home"))
    commit = git_repo.commit({"app.js": "console.log(1)\n"}, "init")
    with pytest.raises(snapshot.SnapshotError, match="no Python files"):
        snapshot.build_snapshot(git_repo.root, commit)
    assert "no Python files" in snapshot.failed_snapshot(git_repo.root, commit)
    assert hooks._wait_for_graph(git_repo.root, commit) is False
    marker = snapshot.guard_dir(git_repo.root) / "snapshots" / commit / "failed.json"
    old = marker.stat().st_mtime - snapshot.FAILURE_TTL - 1
    os.utime(marker, (old, old))
    assert snapshot.failed_snapshot(git_repo.root, commit) is None
