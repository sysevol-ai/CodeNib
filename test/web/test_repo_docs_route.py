# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""A local Wiki lists and serves the repository's own documents."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import codenib.web.app as web_app


@pytest.fixture()
def client(monkeypatch, tmp_path):
    (tmp_path / "README.md").write_text("# Widget\n\nHello.\n")
    bundle = SimpleNamespace(
        entry=SimpleNamespace(repo="acme/widget", repo_dir=str(tmp_path))
    )

    class Registry:
        @contextmanager
        def pin(self, repo_id):
            yield bundle if repo_id == "widget" else None

    builder = SimpleNamespace(
        page_tree=lambda: [{"id": "overview", "title": "Overview", "children": []}],
        page=lambda page_id: None,
    )
    monkeypatch.setattr(web_app.app.state, "registry", Registry(), raising=False)
    monkeypatch.setattr(web_app, "_wiki", lambda repo_id, bundle=None: builder)
    return TestClient(web_app.app)


@pytest.mark.parametrize("enabled", [True, False])
def test_documents_only_on_a_local_wiki(client, monkeypatch, enabled):
    monkeypatch.setattr(web_app, "_repo_docs_enabled", lambda: enabled)
    tree = client.get("/api/repos/widget/wiki").json()
    page = client.get("/api/repos/widget/wiki/doc--readme")
    if enabled:
        assert tree["docs"] == [
            {"id": "doc--readme", "title": "Widget", "path": "README.md"}
        ]
        assert page.status_code == 200
        assert page.json()["generation"]["mode"] == "document"
        assert page.json()["markdown"].startswith("# Widget")
    else:
        assert "docs" not in tree
        assert page.status_code == 404
