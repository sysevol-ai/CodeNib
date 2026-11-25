# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""The Walkthrough's words come from the person's own agent, saved by steps."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codenib.web import annotations as routes
from codenib.web import app as web_app
from codenib.web import walk_narration

TOKEN = "walk-session-key"

STEPS = [
    {"name": "Session.request()", "file": "pkg/session.py", "line": 1, "end_line": 3},
    {"name": "Session.send()", "file": "pkg/session.py", "line": 5, "end_line": 6},
]


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "state"))
    path = tmp_path / "repo"
    (path / "pkg").mkdir(parents=True)
    git = ["git", "-C", str(path), "-c", "maintenance.auto=false"]
    subprocess.run([*git, "init", "-q"], check=True)
    (path / "pkg" / "session.py").write_text(
        "def request(self):\n    prep = self.prepare()\n    return self.send(prep)\n\n"
        "def send(self, prep):\n    return adapter.send(prep)\n"
    )
    monkeypatch.setattr(
        routes,
        "_access",
        routes.RepoAccess(
            repo=lambda repo_id: (path, "t/pkg"),
            page=lambda repo_id, page_id: None,
            pages=lambda repo_id: [],
        ),
    )
    monkeypatch.setenv(routes.TOKEN_ENV, TOKEN)
    return path


@pytest.fixture
def client(repo: Path) -> TestClient:
    return TestClient(
        web_app.app,
        base_url="http://127.0.0.1:8000",
        headers={"X-CodeNib-Session": TOKEN},
    )


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> list:
    asked: list = []

    def run(prompt, *, model, system="", json_schema=None, timeout=0):
        asked.append(prompt)
        return {
            "structured": {
                "title": "What happens when a request is sent?",
                "overview": "`Session.request()` prepares, then sends.",
                "steps": [
                    {"text": "Prepares the request.", "lines": [2, 99]},
                    {"text": "Hands it to the adapter.", "lines": [6]},
                ],
                "recap": "Prepare, then send.",
            }
        }

    from codenib.llm import claude_code

    monkeypatch.setattr(claude_code, "available", lambda: True)
    monkeypatch.setattr(claude_code, "run", run)
    return asked


def test_nothing_is_written_until_asked(client: TestClient, agent: list) -> None:
    body = {"page": "sending", "steps": STEPS}
    assert client.post("/api/repos/pkg/walk-narration", json=body).json() == {
        "narration": None
    }
    assert agent == []


def test_the_agent_writes_once_and_the_result_is_kept(
    client: TestClient, agent: list, repo: Path
) -> None:
    body = {"page": "sending", "steps": STEPS, "agent": "claude-code"}

    written = client.post("/api/repos/pkg/walk-narration?write=1", json=body)

    assert written.status_code == 200, written.text
    narration = written.json()["narration"]
    assert narration["title"] == "What happens when a request is sent?"
    assert [s["text"] for s in narration["steps"]] == [
        "Prepares the request.",
        "Hands it to the adapter.",
    ]
    # A line outside the step's code is dropped.
    assert narration["steps"][0]["lines"] == [2]
    # The agent saw each step's code with its line numbers.
    assert "    2     prep = self.prepare()" in agent[0]
    saved = list((repo / ".codenib" / "walkthroughs").glob("sending-*.json"))
    assert len(saved) == 1
    assert json.loads(saved[0].read_text())["recap"] == "Prepare, then send."
    # Asking again reads the saved one; the agent is not asked twice.
    again = client.post("/api/repos/pkg/walk-narration?write=1", json=body).json()
    assert again["narration"] == narration
    assert len(agent) == 1


def test_steps_outside_the_repository_are_refused(
    client: TestClient, agent: list
) -> None:
    steps = [dict(STEPS[0], file="../secret.py"), STEPS[1]]
    response = client.post(
        "/api/repos/pkg/walk-narration?write=1", json={"page": "x", "steps": steps}
    )
    assert response.status_code == 400
    assert agent == []


def test_it_needs_the_session_key(repo: Path, agent: list) -> None:
    bare = TestClient(web_app.app, base_url="http://127.0.0.1:8000")
    response = bare.post(
        "/api/repos/pkg/walk-narration?write=1", json={"page": "x", "steps": STEPS}
    )
    assert response.status_code == 403
    assert agent == []


def test_a_short_answer_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(Exception) as caught:
        walk_narration.shape(
            {"steps": [{"text": "one"}]},
            [walk_narration.StepIn(**s) for s in STEPS],
            "claude-code",
        )
    assert "1 of 2" in str(caught.value.detail)
