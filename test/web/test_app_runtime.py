# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for non-blocking web runtime behavior."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import codenib.web.app as web_app


def test_wiki_generation_runs_off_event_loop(monkeypatch):
    calls = []

    class Builder:
        def page_tree(self):
            return [{"id": "overview"}]

        def page(self, page_id):
            return {"id": page_id}

    async def fake_to_thread(func, *args, **kwargs):
        calls.append((func.__name__, args))
        return func(*args, **kwargs)

    builder = Builder()
    monkeypatch.setattr(web_app, "_wiki", lambda _repo_id: builder)
    monkeypatch.setattr(
        web_app,
        "_bundle",
        lambda _repo_id: SimpleNamespace(entry=SimpleNamespace(repo="org/repo")),
    )
    monkeypatch.setattr(web_app.asyncio, "to_thread", fake_to_thread)

    tree = asyncio.run(web_app.wiki_tree("repo"))
    page = asyncio.run(web_app.wiki_page("repo", "overview"))

    assert tree == {"repo": "org/repo", "pages": [{"id": "overview"}]}
    assert page == {"id": "overview"}
    assert calls == [("page_tree", ()), ("page", ("overview",))]
