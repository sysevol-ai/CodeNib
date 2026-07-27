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
    assert page == {
        "id": "overview",
        "generation": {
            "mode": "offline",
            "model": None,
            "repaired": False,
        },
        "grounding": {
            "valid": True,
            "citation_coverage": 1.0,
            "cited_evidence": 0,
            "evidence_count": 0,
            "relation_count": 0,
        },
    }
    assert calls == [("page_tree", ()), ("page", ("overview",))]


def test_template_wiki_disables_narrator(tmp_path):
    config = SimpleNamespace(
        data_dir=str(tmp_path),
        wiki_generation_model="gpt-4o",
        wiki_agent=False,
    )

    narrator = web_app._wiki_narrator(config)

    assert narrator.enabled is False
    assert narrator.cache_dir is None


def test_wiki_llm_receives_provider_configuration(monkeypatch):
    captured = {}

    def fake_chat(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        "codenib.llm.litellm_chat.LiteLLMChat",
        fake_chat,
    )
    config = SimpleNamespace(
        wiki_generation_model="openai/local-model",
        wiki_generation_api_base="http://localhost:4000/v1",
        wiki_generation_api_key="secret",
        wiki_generation_options={
            "api_version": "2025-01-01",
            "extra_body": {"reasoning": {"enabled": False}},
        },
    )

    web_app._wiki_llm(config, max_tokens=123)

    assert captured == {
        "model": "openai/local-model",
        "temperature": 0.2,
        "max_tokens": 123,
        "api_base": "http://localhost:4000/v1",
        "api_key": "secret",
        "extra_kwargs": {
            "api_version": "2025-01-01",
            "extra_body": {"reasoning": {"enabled": False}},
        },
    }


def test_unavailable_codemap_returns_repository_setup_report(monkeypatch):
    class Window:
        available = False

    class Setup:
        def to_dict(self):
            return {
                "ready": False,
                "languages": [
                    {
                        "language": "python",
                        "backend": "scip",
                        "missing": ["scip-python"],
                    }
                ],
            }

    bundle = SimpleNamespace(
        entry=SimpleNamespace(base_commit="abc123"),
        code_graph=lambda: None,
        graph_unavailable_note=lambda: "Dependency graph is not built.",
        graph_setup=lambda: Setup(),
    )
    monkeypatch.setattr(web_app, "_bundle", lambda _repo_id: bundle)
    monkeypatch.setattr(web_app, "_commit_window", lambda _repo_id: Window())

    result = asyncio.run(web_app.codemap("repo"))

    assert result["available"] is False
    assert result["setup"]["languages"][0]["missing"] == ["scip-python"]
