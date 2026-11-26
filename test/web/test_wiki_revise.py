# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""A comment can ask the agent for a better paragraph, reviewed before saving."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codenib.web import annotations as routes
from codenib.web import app as web_app
from codenib.web import wiki_revise

TOKEN = "revise-session-key"
MARKDOWN = (
    "# Sending\n\n"
    "Sessions send requests. [E1](#evidence-E1)\n\n"
    "The adapter is chosen by prefix and does the work. [E1](#evidence-E1)\n"
)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "state"))
    path = tmp_path / "repo"
    path.mkdir()
    git = ["git", "-C", str(path), "-c", "maintenance.auto=false"]
    subprocess.run([*git, "init", "-q"], check=True)
    (path / "session.py").write_text("def get_adapter(url):\n    return pick(url)\n")
    page = {
        "id": "sending",
        "title": "Sending",
        "markdown": MARKDOWN,
        "evidence": {
            "items": [
                {"id": "E1", "file": "session.py", "start_line": 1, "end_line": 2}
            ]
        },
    }
    monkeypatch.setattr(
        routes,
        "_access",
        routes.RepoAccess(
            repo=lambda repo_id: (path, "t/x"),
            page=lambda repo_id, page_id: page if page_id == "sending" else None,
            pages=lambda repo_id: [page],
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
    prompts: list = []

    def complete(agent_id: str, prompt: str) -> dict:
        prompts.append(prompt)
        return {
            "replacement": (
                "`get_adapter()` picks the adapter whose prefix matches the URL."
                " [E1](#evidence-E1)"
            ),
            "summary": "Named the function and what it matches on.",
        }

    monkeypatch.setattr(wiki_revise, "_complete", complete)
    return prompts


def _wait(client: TestClient, revision_id: str) -> dict:
    deadline = time.time() + 10
    while time.time() < deadline:
        found = client.get("/api/repos/x/revisions").json()["revisions"]
        mine = next(p for p in found if p["id"] == revision_id)
        if mine["status"] != "working":
            return mine
        time.sleep(0.05)
    raise AssertionError("the proposal never finished")


def test_a_comment_proposes_a_paragraph_that_is_saved_only_when_accepted(
    client: TestClient, agent: list, repo: Path
) -> None:
    started = client.post(
        "/api/repos/x/revisions",
        json={
            "page": "sending",
            "quote": "chosen by prefix",
            "comment": "Which function?",
        },
    )
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "working"
    assert started.json()["original"].startswith("The adapter is chosen by prefix")

    ready = _wait(client, started.json()["id"])
    assert ready["status"] == "needs_review"
    assert "get_adapter()" in ready["replacement"]
    # The agent saw the paragraph, the comment and the cited code.
    assert "Which function?" in agent[0] and "def get_adapter(url):" in agent[0]
    # Nothing changes before the person accepts.
    assert not (repo / ".codenib" / "wiki" / "sending.md").exists()

    applied = client.post(f"/api/repos/x/revisions/{ready['id']}/accept")
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "applied"
    saved = (repo / ".codenib" / "wiki" / "sending.md").read_text()
    assert "picks the adapter whose prefix matches" in saved
    assert "Sessions send requests." in saved
    assert "chosen by prefix" not in saved


def test_a_rejected_proposal_changes_nothing(
    client: TestClient, agent: list, repo: Path
) -> None:
    started = client.post(
        "/api/repos/x/revisions",
        json={"page": "sending", "quote": "Sessions send", "comment": "Say more"},
    ).json()
    _wait(client, started["id"])
    rejected = client.post(f"/api/repos/x/revisions/{started['id']}/reject").json()
    assert rejected["status"] == "rejected"
    assert not (repo / ".codenib" / "wiki" / "sending.md").exists()
    again = client.post(f"/api/repos/x/revisions/{started['id']}/accept")
    assert again.status_code == 409


def test_words_no_longer_on_the_page_are_refused(
    client: TestClient, agent: list
) -> None:
    response = client.post(
        "/api/repos/x/revisions",
        json={"page": "sending", "quote": "not on the page", "comment": "?"},
    )
    assert response.status_code == 409
    assert agent == []


def test_it_needs_the_session_key(repo: Path, agent: list) -> None:
    bare = TestClient(web_app.app, base_url="http://127.0.0.1:8000")
    response = bare.post(
        "/api/repos/x/revisions",
        json={"page": "sending", "quote": "Sessions send", "comment": "?"},
    )
    assert response.status_code == 403
    assert agent == []


def test_finds_the_paragraph_through_markup() -> None:
    assert (
        wiki_revise.paragraph_with(MARKDOWN, "the adapter is CHOSEN by prefix")
        == "The adapter is chosen by prefix and does the work. [E1](#evidence-E1)"
    )
    assert wiki_revise.paragraph_with(MARKDOWN, "missing words") is None


def test_a_paragraph_without_citations_gets_the_pages_code(repo: Path) -> None:
    page = {
        "evidence": {
            "items": [
                {"id": "E1", "file": "session.py", "start_line": 1, "end_line": 2}
            ]
        }
    }
    code = wiki_revise._cited_code(repo, page, "Sessions send requests.")
    assert "def get_adapter(url):" in code
    bare = {"citations": [{"file": "session.py", "start_line": 1, "end_line": 1}]}
    assert "def get_adapter(url):" in wiki_revise._cited_code(
        repo, bare, "No cites here."
    )
