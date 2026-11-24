# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""The local-only annotation routes: the token gate and the whole loop."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codenib.web import annotations as routes
from codenib.web import app as web_app

TOKEN = "test-session-token"
AGENT = textwrap.dedent("""
    import json, os, re, sys
    from pathlib import Path
    prompt = sys.stdin.read()
    ids = re.findall(r"^## Annotation ([0-9A-Z]{26})", prompt, flags=re.M)
    if os.environ.get("CODENIB_TASK_EDIT") == "1":
        Path("calc.py").write_text("def add(a, b):\\n    return a + b\\n")
    print(json.dumps({"type": "result", "subtype": "success",
        "structured_output": {"results": [{"annotation": i, "answer": "It subtracts."}
                                          for i in ids]},
        "usage": {"input_tokens": 1, "output_tokens": 1}, "total_cost_usd": 0.001}))
    """)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "state"))
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
    monkeypatch.setenv("CODENIB_TASK_AGENT_COMMAND", f"{sys.executable} {script}")
    page = {
        "id": "math",
        "title": "Math",
        "markdown": "# Math\n\n## Add\n\nAdds two numbers [E1](#evidence-E1).\n",
        "citations": [{"file": "calc.py", "start_line": 1, "end_line": 2}],
    }
    monkeypatch.setattr(
        routes,
        "_access",
        routes.RepoAccess(
            repo=lambda repo_id: (path, "t/calc"),
            page=lambda repo_id, page_id: page if page_id == "math" else None,
            pages=lambda repo_id: [
                {
                    "id": "math",
                    "title": "Math",
                    "citations": [{"file": "calc.py", "start_line": 1, "end_line": 2}],
                },
                {
                    "id": "far",
                    "title": "Far",
                    "citations": [
                        {"file": "calc.py", "start_line": 40, "end_line": 60}
                    ],
                },
                {
                    "id": "imports",
                    "title": "Imports",
                    "citations": [
                        {
                            "file": "calc.py",
                            "start_line": 1,
                            "end_line": 9,
                            "type": "header",
                        }
                    ],
                },
            ],
        ),
    )
    return path


@pytest.fixture
def client(repo: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv(routes.TOKEN_ENV, TOKEN)
    return TestClient(
        web_app.app,
        base_url="http://127.0.0.1:8000",
        headers={"X-CodeNib-Session": TOKEN},
    )


def _wait(client: TestClient, task_id: str) -> dict:
    deadline = time.time() + 30
    while time.time() < deadline:
        task = client.get(f"/api/repos/calc/tasks/{task_id}").json()
        if task["status"] not in ("queued", "running"):
            return task
        time.sleep(0.05)
    raise AssertionError("the task did not finish")


def test_routes_do_not_exist_without_a_session(repo: Path, monkeypatch) -> None:
    monkeypatch.delenv(routes.TOKEN_ENV, raising=False)
    client = TestClient(web_app.app, base_url="http://127.0.0.1:8000")
    assert client.get("/api/repos/calc/annotations").status_code == 404
    assert routes.enabled() is False


def test_a_wrong_token_or_a_foreign_host_is_refused(repo: Path, monkeypatch) -> None:
    monkeypatch.setenv(routes.TOKEN_ENV, TOKEN)
    wrong = TestClient(
        web_app.app,
        base_url="http://127.0.0.1:8000",
        headers={"X-CodeNib-Session": "x"},
    )
    assert wrong.get("/api/repos/calc/annotations").status_code == 403
    rebound = TestClient(
        web_app.app,
        base_url="http://attacker.example:8000",
        headers={"X-CodeNib-Session": TOKEN},
    )
    assert rebound.get("/api/repos/calc/annotations").status_code == 403
    assert not (repo / ".codenib").exists()


def test_question_round_trip(client: TestClient, repo: Path) -> None:
    created = client.post(
        "/api/repos/calc/annotations",
        json={
            "kind": "question",
            "text": "Does it add?",
            "on": {"page": "math", "quote": "two numbers"},
        },
    )
    assert created.status_code == 200, created.text
    question = created.json()
    assert question["status"] == "draft"

    task = client.post("/api/repos/calc/tasks", json={"annotations": [question["id"]]})
    assert task.status_code == 200, task.text
    done = _wait(client, task.json()["id"])
    assert done["status"] == "done", done["error"]

    state = client.get("/api/repos/calc/annotations", params={"page": "math"}).json()
    by_id = {a["id"]: a for a in state["annotations"]}
    assert by_id[question["id"]]["status"] == "answered"
    answer = by_id[done["results"][0]]
    assert answer["author"] == "agent" and answer["reply_to"] == question["id"]
    assert [t["id"] for t in state["tasks"]] == [done["id"]]
    assert state["agents"][0]["id"] == "claude-code"

    # An agent's answer can be resolved, never rewritten.
    edit = client.patch(
        f"/api/repos/calc/annotations/{answer['id']}", json={"text": "x"}
    )
    assert edit.status_code == 400
    closed = client.patch(
        f"/api/repos/calc/annotations/{answer['id']}", json={"status": "resolved"}
    )
    assert closed.json()["status"] == "resolved"
    assert (
        client.delete(f"/api/repos/calc/annotations/{question['id']}").status_code
        == 400
    )


def test_change_round_trip(client: TestClient, repo: Path) -> None:
    change = client.post(
        "/api/repos/calc/annotations",
        json={"kind": "change", "text": "Make add add.", "on": {"page": "math"}},
    ).json()
    done = _wait(
        client,
        client.post(
            "/api/repos/calc/tasks", json={"annotations": [change["id"]]}
        ).json()["id"],
    )
    assert done["status"] == "needs_review", done["error"]
    assert [p["id"] for p in done["diff"]["pages"]] == [
        "math"
    ]  # "far" cites other lines; "imports" cites only the header
    patch = client.get(f"/api/repos/calc/tasks/{done['id']}/diff").json()["patch"]
    assert "+    return a + b" in patch

    accepted = client.post(f"/api/repos/calc/tasks/{done['id']}/accept")
    assert accepted.status_code == 200, accepted.text
    assert "a + b" in (repo / "calc.py").read_text()
    again = client.post(f"/api/repos/calc/tasks/{done['id']}/accept")
    assert again.status_code == 409


def test_drafts_can_be_edited_and_deleted(client: TestClient, repo: Path) -> None:
    draft = client.post(
        "/api/repos/calc/annotations",
        json={"kind": "question", "text": "Why?", "on": {"page": "math"}},
    ).json()
    edited = client.patch(
        f"/api/repos/calc/annotations/{draft['id']}",
        json={"text": "Why so?", "kind": "change"},
    )
    assert edited.json()["text"] == "Why so?" and edited.json()["kind"] == "change"
    assert client.delete(f"/api/repos/calc/annotations/{draft['id']}").json() == {
        "deleted": draft["id"]
    }
    bad = client.post(
        "/api/repos/calc/annotations", json={"kind": "answer", "text": "x"}
    )
    assert bad.status_code == 422


def test_an_interrupted_task_is_marked_failed(client: TestClient, repo: Path) -> None:
    from codenib.annotations import AnnotationStore
    from codenib.annotations.runner import dispatch

    question = AnnotationStore(repo).create(
        kind="question", text="Why?", on={"page": "math"}
    )
    task = dispatch(repo, [question.id])  # dispatched, never started: a restart
    state = client.get("/api/repos/calc/annotations").json()
    (stale,) = [t for t in state["tasks"] if t["id"] == task.id]
    assert stale["status"] == "failed" and "interrupted" in stale["error"]
    assert json.loads(json.dumps(state))["annotations"][0]["status"] == "failed"


def test_an_answer_is_added_to_its_page_and_edits_save(
    client: TestClient, repo: Path
) -> None:
    question = client.post(
        "/api/repos/calc/annotations",
        json={
            "kind": "question",
            "text": "Does it add?",
            "on": {"page": "math", "section": "Add", "quote": "two numbers"},
        },
    ).json()
    done = _wait(
        client,
        client.post(
            "/api/repos/calc/tasks", json={"annotations": [question["id"]]}
        ).json()["id"],
    )
    answer_id = done["results"][0]

    folded = client.post(
        "/api/repos/calc/pages/math/fold", json={"annotation": answer_id}
    )

    assert folded.status_code == 200, folded.text
    assert "**Does it add?**\n\nIt subtracts." in folded.json()["markdown"]
    state = client.get("/api/repos/calc/annotations", params={"page": "math"}).json()
    assert {a["id"]: a for a in state["annotations"]}[answer_id]["status"] == "applied"
    assert (repo / ".codenib" / "wiki" / "math.md").exists()

    source = client.get("/api/repos/calc/pages/math/markdown").json()
    assert source["edited"] is True
    stale = client.put(
        "/api/repos/calc/pages/math/markdown",
        json={"markdown": "# Math", "base": "old"},
    )
    assert stale.status_code == 409
    saved = client.put(
        "/api/repos/calc/pages/math/markdown",
        json={"markdown": "# Math\n\nMine.", "base": source["base"]},
    )
    assert saved.json()["markdown"] == "# Math\n\nMine."
    assert client.delete("/api/repos/calc/pages/math/markdown").json() == {
        "reverted": True
    }
    question_fold = client.post(
        "/api/repos/calc/pages/math/fold", json={"annotation": question["id"]}
    )
    assert question_fold.status_code == 409


def test_an_annotation_on_code_is_anchored_to_the_working_tree(
    client: TestClient, repo: Path
) -> None:
    (repo / "calc.py").write_text("# added above\ndef add(a, b):\n    return a - b\n")
    created = client.post(
        "/api/repos/calc/annotations",
        json={
            "kind": "question",
            "text": "Why?",
            "on": {
                "page": "math",
                "path": "calc.py",
                "start_line": 2,
                "end_line": 2,
                "quote": "return a - b",
            },
        },
    ).json()
    assert created["on"]["start_line"] == 3  # where the code is now
    assert created["anchor_state"] == "current"
    assert len(created["on"]["commit"]) == 40

    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    listed = client.get("/api/repos/calc/annotations").json()["annotations"]
    (mine,) = [a for a in listed if a["id"] == created["id"]]
    assert (mine["anchor_state"], mine["on"]["start_line"]) == ("moved", 2)
