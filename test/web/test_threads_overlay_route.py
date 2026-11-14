# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import codenib.web.app as web_app
from codenib.paths import CODENIB_HOME_ENV

REPO = "acme/widget"


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv(CODENIB_HOME_ENV, str(tmp_path))
    bundle = SimpleNamespace(entry=SimpleNamespace(repo=REPO))

    class Registry:
        @contextmanager
        def pin(self, repo_id):
            yield bundle if repo_id == "acme__widget" else None

    monkeypatch.setattr(web_app.app.state, "registry", Registry(), raising=False)
    return TestClient(web_app.app)


def _write(tmp_path, overlay):
    target = tmp_path / "threads" / REPO / "wiki_overlay.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(overlay))


def test_threads_overlay_is_served_from_the_thread_store(client, tmp_path):
    overlay = {"version": 1, "repo": REPO, "rev": "abc", "pages": {"p": {}}}
    _write(tmp_path, overlay)

    response = client.get("/api/repos/acme__widget/threads")
    assert response.status_code == 200
    assert response.json() == overlay


@pytest.mark.parametrize(
    "overlay",
    [
        None,
        {"version": 2, "repo": REPO, "pages": {}},
        {"version": 1, "repo": "other/repo", "pages": {}},
        {"version": 1, "repo": REPO, "pages": []},
    ],
)
def test_a_missing_or_foreign_overlay_answers_404(client, tmp_path, overlay):
    if overlay is not None:
        _write(tmp_path, overlay)
    assert client.get("/api/repos/acme__widget/threads").status_code == 404


def test_an_unknown_repository_answers_404(client):
    assert client.get("/api/repos/nope/threads").status_code == 404
