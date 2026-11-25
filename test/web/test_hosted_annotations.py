# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Hosted annotations: one shared checkout, a folder per visitor, their key."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codenib.annotations.agents import CommandAgent
from codenib.web import annotations as routes
from codenib.web import app as web_app

VISITOR_A = "a" * 43
VISITOR_B = "b" * 43
KEY = "sk-or-v1-" + "0" * 40
AGENT = textwrap.dedent("""
    import json, os, re, sys
    from pathlib import Path
    prompt = sys.stdin.read()
    ids = re.findall(r"^## Annotation ([0-9A-Z]{26})", prompt, flags=re.M)
    if os.environ.get("CODENIB_TASK_EDIT") == "1":
        Path("calc.py").write_text("def add(a, b):\\n    return a + b\\n")
        Path("notes.txt").write_text("why\\n")
    print(json.dumps({"type": "result", "subtype": "success",
        "structured_output": {"results": [{"annotation": i, "answer": "It subtracts."}
                                          for i in ids]},
        "usage": {"input_tokens": 1, "output_tokens": 1}, "total_cost_usd": 0.001}))
    """)


class FakeSandbox(CommandAgent):
    """Stands in for the container: same run, plus the key it must forget."""

    made: list = []

    def __init__(self, key: str, *, provider: str):
        super().__init__(FakeSandbox.argv)
        self.key, self.provider = key, provider
        FakeSandbox.made.append(self)

    def forget_key(self) -> None:
        self.key = ""


def _status(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(routes.TOKEN_ENV, raising=False)
    monkeypatch.setenv(routes.HOSTED_ENV, "1")
    path = tmp_path / "repo"
    path.mkdir()
    git = ["git", "-C", str(path), "-c", "maintenance.auto=false"]
    subprocess.run([*git, "init", "-q"], check=True)
    (path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run(
        [*git, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
        check=True,
    )
    script = tmp_path / "agent.py"
    script.write_text(AGENT)
    FakeSandbox.argv = [sys.executable, str(script)]
    FakeSandbox.made = []
    monkeypatch.setattr(routes, "SandboxedClaude", FakeSandbox)
    monkeypatch.setattr(routes, "check_key", lambda key: "openrouter")
    monkeypatch.setattr(routes, "sandbox_available", lambda: True)
    monkeypatch.setattr(routes, "_hosted_runs", [])
    page = {
        "id": "math",
        "title": "Math",
        "markdown": "# Math\n\n## Add\n\nAdds two numbers.\n",
        "citations": [{"file": "calc.py", "start_line": 1, "end_line": 2}],
    }
    monkeypatch.setattr(
        routes,
        "_access",
        routes.RepoAccess(
            repo=lambda repo_id: (path, "t/calc"),
            page=lambda repo_id, page_id: page if page_id == "math" else None,
            pages=lambda repo_id: [page],
            state_root=lambda: tmp_path / "data",
        ),
    )
    return path


def _client(visitor: str) -> TestClient:
    return TestClient(
        web_app.app,
        base_url="https://demo.example",
        headers={"X-CodeNib-Visitor": visitor},
    )


def _wait(client: TestClient, task_id: str) -> dict:
    deadline = time.time() + 30
    while time.time() < deadline:
        task = client.get(f"/api/repos/calc/tasks/{task_id}").json()
        if task["status"] not in ("queued", "running"):
            return task
        time.sleep(0.05)
    raise AssertionError("the task did not finish")


def _ask(client: TestClient, kind: str, text: str) -> dict:
    created = client.post(
        "/api/repos/calc/annotations",
        json={"kind": kind, "text": text, "on": {"page": "math", "quote": "Adds"}},
    )
    assert created.status_code == 200, created.text
    return created.json()


def test_a_visitor_key_is_needed_and_visitors_see_only_their_own(repo: Path) -> None:
    anonymous = TestClient(web_app.app, base_url="https://demo.example")
    assert anonymous.get("/api/repos/calc/annotations").status_code == 401

    a, b = _client(VISITOR_A), _client(VISITOR_B)
    note = _ask(a, "note", "Remember this.")
    state = a.get("/api/repos/calc/annotations?page=math").json()
    assert state["mode"] == "hosted"
    assert [x["id"] for x in state["annotations"]] == [note["id"]]
    assert b.get("/api/repos/calc/annotations?page=math").json()["annotations"] == []
    assert b.delete(f"/api/repos/calc/annotations/{note['id']}").status_code == 400
    # The shared checkout is never written.
    assert _status(repo) == "" and not (repo / ".codenib").exists()


def test_a_question_runs_on_the_visitors_key_and_forgets_it(repo: Path) -> None:
    a = _client(VISITOR_A)
    question = _ask(a, "question", "What does add do?")
    refused = a.post("/api/repos/calc/tasks", json={"annotations": [question["id"]]})
    assert refused.status_code == 402
    bad = a.post(
        "/api/repos/calc/tasks",
        json={"annotations": [question["id"]]},
        headers={"Authorization": "Bearer not-a-key-at-all-but-long"},
    )
    assert bad.status_code == 400

    task = a.post(
        "/api/repos/calc/tasks",
        json={"annotations": [question["id"]]},
        headers={"Authorization": f"Bearer {KEY}"},
    ).json()
    done = _wait(a, task["id"])
    assert done["status"] == "done", done["error"]
    answers = [
        x
        for x in a.get("/api/repos/calc/annotations").json()["annotations"]
        if x["author"] == "agent"
    ]
    assert [x["text"] for x in answers] == ["It subtracts."]
    assert FakeSandbox.made and FakeSandbox.made[-1].key == ""
    assert _status(repo) == ""


def test_a_change_becomes_a_patch_and_never_touches_the_checkout(repo: Path) -> None:
    a = _client(VISITOR_A)
    change = _ask(a, "change", "Make add add.")
    task = a.post(
        "/api/repos/calc/tasks",
        json={"annotations": [change["id"]]},
        headers={"Authorization": f"Bearer {KEY}"},
    ).json()
    done = _wait(a, task["id"])
    assert done["status"] == "needs_review", done["error"]
    assert done["diff"]["branch"] == ""
    hunks = a.get(f"/api/repos/calc/tasks/{done['id']}/diff").json()["hunks"]
    assert {h["path"] for h in hunks} == {"calc.py", "notes.txt"}
    notes = next(h["id"] for h in hunks if h["path"] == "notes.txt")
    assert (
        a.post(
            f"/api/repos/calc/tasks/{done['id']}/reject", json={"hunks": [notes]}
        ).status_code
        == 200
    )
    patch = a.get(f"/api/repos/calc/tasks/{done['id']}/patch")
    assert patch.status_code == 200
    assert "attachment" in patch.headers["content-disposition"]
    assert "+    return a + b" in patch.text and "notes.txt" not in patch.text
    closed = a.post(f"/api/repos/calc/tasks/{done['id']}/accept").json()
    assert closed["status"] == "done"

    # The served checkout: no edit, no worktree, no branch, nothing untracked.
    assert "a - b" in (repo / "calc.py").read_text()
    assert _status(repo) == ""
    worktrees = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert len(worktrees) == 1
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "codenib/*"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert branches == ""
    # The private clone is gone once its diff was saved.
    runs = list((repo.parent / "data").rglob("worktree"))
    assert runs == []


def test_page_edits_stay_local(repo: Path) -> None:
    a = _client(VISITOR_A)
    assert a.get("/api/repos/calc/pages/math/markdown").status_code == 403
    assert (
        a.put(
            "/api/repos/calc/pages/math/markdown", json={"markdown": "# Mine\n"}
        ).status_code
        == 403
    )


def test_a_busy_server_asks_to_wait(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _client(VISITOR_A)
    question = _ask(a, "question", "What does add do?")
    monkeypatch.setattr(routes, "_running", {"X", "Y"})
    busy = a.post(
        "/api/repos/calc/tasks",
        json={"annotations": [question["id"]]},
        headers={"Authorization": f"Bearer {KEY}"},
    )
    assert busy.status_code == 429 and busy.headers["retry-after"] == "60"


def test_the_local_session_wins_over_hosted_mode(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(routes.TOKEN_ENV, "local-token")
    assert not routes.hosted_enabled()
    response = _client(VISITOR_A).get("/api/repos/calc/annotations")
    assert response.status_code == 403  # a foreign host, no session
