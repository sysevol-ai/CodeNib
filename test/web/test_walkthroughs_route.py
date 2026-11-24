# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import codenib.web.app as web_app
from codenib.web.walkthroughs import load_walkthroughs, walkthrough_path

REPO_ID = "acme__widget"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _set(**overrides):
    walk = {
        "id": "run",
        "tab": "Running a task",
        "stops": [["", "w.run()", None, None, True], ["Widget", ".run()", "w.py:1–3", ""]],
        "steps": [{"title": "t", "lead": "l", "body": [], "at": 1, "runs": [0]}],
    }
    data = {
        "version": 1,
        "repo": {"id": REPO_ID, "slug": "acme/widget", "commit": COMMIT},
        "areas": [],
        "links": [],
        "paths": {},
        "wiki": {"pages": {}, "cites": {}, "size": {}, "calls": {}},
        "walks": [walk],
        "source": {"w.py": {"1": "def run():"}},
        "log": {"calls": ["kept on disk"]},
    }
    data.update(overrides)
    return data


@pytest.fixture()
def client(monkeypatch, tmp_path):
    bundle = SimpleNamespace(entry=SimpleNamespace(base_commit=COMMIT))

    class Registry:
        @contextmanager
        def pin(self, repo_id):
            yield bundle if repo_id == REPO_ID else None

    monkeypatch.setattr(web_app.app.state, "registry", Registry(), raising=False)
    monkeypatch.setattr(web_app, "load_config", lambda: SimpleNamespace(data_dir=str(tmp_path)))
    return TestClient(web_app.app)


def _write(tmp_path, data):
    target = tmp_path / "walkthroughs" / f"{REPO_ID}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data))


def test_walkthroughs_are_served_without_the_build_log(client, tmp_path):
    data = _set()
    _write(tmp_path, data)

    response = client.get(f"/api/repos/{REPO_ID}/walkthroughs")

    assert response.status_code == 200
    body = response.json()
    assert "log" not in body
    assert body["walks"] == data["walks"]
    assert body["source"] == data["source"]


def test_a_short_commit_matches_the_indexed_one(client, tmp_path):
    _write(tmp_path, _set(repo={"id": REPO_ID, "slug": "acme/widget", "commit": COMMIT[:12]}))
    assert client.get(f"/api/repos/{REPO_ID}/walkthroughs").status_code == 200


@pytest.mark.parametrize(
    "data",
    [
        None,
        _set(version=2),
        _set(repo={"id": "other__repo", "slug": "x/y", "commit": COMMIT}),
        _set(repo={"id": REPO_ID, "slug": "acme/widget", "commit": "f" * 40}),
        _set(walks=[]),
        _set(walks=[{"id": "run", "stops": [], "steps": []}]),
        _set(source=[]),
    ],
)
def test_missing_foreign_stale_or_malformed_walkthroughs_answer_404(client, tmp_path, data):
    if data is not None:
        _write(tmp_path, data)
    assert client.get(f"/api/repos/{REPO_ID}/walkthroughs").status_code == 404


def test_an_unknown_repository_answers_404(client):
    assert client.get("/api/repos/nope/walkthroughs").status_code == 404


def test_a_corrupt_file_reads_as_absent(tmp_path):
    target = tmp_path / "walkthroughs" / f"{REPO_ID}.json"
    target.parent.mkdir(parents=True)
    target.write_text("{not json")
    assert load_walkthroughs(tmp_path, REPO_ID) is None


@pytest.mark.parametrize("repo_id", ["../etc", "a/b", ".hidden", "a..b", ""])
def test_unsafe_repository_ids_have_no_path(tmp_path, repo_id):
    assert walkthrough_path(tmp_path, repo_id) is None
