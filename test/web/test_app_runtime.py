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
        # No customization store on app.state in this test, so the page passes
        # through _apply_customization untouched but flagged.
        "customized": False,
    }
    # page_tree, then page generation, then the customization pass — all off the
    # event loop. The customization arg is the pre-flag page dict, so assert the
    # call sequence by name rather than by dict identity.
    assert [name for name, _ in calls] == [
        "page_tree",
        "page",
        "_apply_customization",
    ]


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


def test_unavailable_modulemap_returns_repository_setup_report(monkeypatch):
    class Window:
        available = False

    class Setup:
        def to_dict(self):
            return {"ready": False, "languages": []}

    bundle = SimpleNamespace(
        entry=SimpleNamespace(base_commit="abc123"),
        code_graph=lambda: None,
        graph_unavailable_note=lambda: "Dependency graph is not built.",
        graph_setup=lambda: Setup(),
    )
    monkeypatch.setattr(web_app, "_bundle", lambda _repo_id: bundle)
    monkeypatch.setattr(web_app, "_commit_window", lambda _repo_id: Window())

    result = asyncio.run(web_app.modulemap("repo"))

    assert result["available"] is False
    assert result["setup"] == {"ready": False, "languages": []}
    assert result["nodes"] == []
    assert result["granularity"] == "file"


def test_modulemap_endpoint_projects_the_graph_and_stamps_the_commit(monkeypatch):
    from codenib.graph.code_graph import CodeGraph

    class Window:
        available = False

    graph = CodeGraph()
    for name, file in (
        ("src/a.py:alpha()", "src/a.py"),
        ("src/b.py:beta()", "src/b.py"),
    ):
        graph._add_vertex(
            name,
            {
                "type": "function",
                "file": file,
                "start_line": 0,
                "end_line": 2,
                "unified_name": name,
            },
        )
    graph._add_edge(
        "src/a.py:alpha()",
        "src/b.py:beta()",
        "reference",
        anchor_file="src/a.py",
        anchor_line=1,
    )

    bundle = SimpleNamespace(
        entry=SimpleNamespace(base_commit="abc123", repo_dir=None),
        code_graph=lambda: graph,
        graph_unavailable_note=lambda: "",
        graph_setup=lambda: None,
    )
    monkeypatch.setattr(web_app, "_bundle", lambda _repo_id: bundle)
    monkeypatch.setattr(web_app, "_commit_window", lambda _repo_id: Window())

    result = asyncio.run(web_app.modulemap("repo", granularity="file"))

    assert result["available"] is True
    assert result["commit"] == "abc123"
    assert {node["path"] for node in result["nodes"]} == {"src/a.py", "src/b.py"}
    assert len(result["edges"]) == 1


def test_commit_window_maps_use_only_selected_snapshot_metadata(monkeypatch):
    import codenib.web.codemap as codemap_builder
    import codenib.web.modulemap as modulemap_builder

    selected = "abcdef1234567890"
    graph = object()
    captured = {}

    class Window:
        available = True

        def resolve(self, commit):
            assert commit == selected
            return {"sha": selected}

        def graph_for(self, commit):
            assert commit == selected
            return graph

    def no_current_hierarchy():
        raise AssertionError("historical graph must not use the current hierarchy")

    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            base_commit="1111111111111111", repo_dir="/tmp/repository"
        ),
        code_graph=lambda: None,
        hierarchical_graph=no_current_hierarchy,
    )

    def fake_codemap(*args, **kwargs):
        captured["codemap"] = (args, kwargs)
        return {"available": True, "nodes": [], "edges": []}

    def fake_modulemap(*args, **kwargs):
        captured["modulemap"] = (args, kwargs)
        return {
            "available": True,
            "granularity": "file",
            "nodes": [],
            "edges": [],
        }

    monkeypatch.setattr(web_app, "_bundle", lambda _repo_id: bundle)
    monkeypatch.setattr(web_app, "_commit_window", lambda _repo_id: Window())
    monkeypatch.setattr(codemap_builder, "build_codemap", fake_codemap)
    monkeypatch.setattr(modulemap_builder, "build_modulemap", fake_modulemap)

    codemap_result = asyncio.run(web_app.codemap("repo", commit=selected))
    modulemap_result = asyncio.run(web_app.modulemap("repo", commit=selected))

    assert codemap_result["commit"] == selected
    assert modulemap_result["commit"] == selected
    assert captured["codemap"][1]["repo_commit"] == selected
    assert captured["codemap"][1]["hierarchy_graph"] is None
    assert captured["modulemap"][1]["repo_commit"] == selected


def test_source_endpoint_reads_the_requested_window_commit(monkeypatch):
    selected = "abcdef1234567890"
    calls = []

    class Window:
        available = True

        def resolve(self, commit):
            return {"sha": selected} if commit == selected else None

        def source_for(self, commit, file, start, end):
            calls.append((commit, file, start, end))
            return {
                "file": file,
                "start_line": start,
                "end_line": end,
                "content": "historical\n",
            }

    bundle = SimpleNamespace(entry=SimpleNamespace(base_commit="1" * 40))
    monkeypatch.setattr(web_app, "_bundle", lambda _repo_id: bundle)
    monkeypatch.setattr(web_app, "_commit_window", lambda _repo_id: Window())
    monkeypatch.setattr(
        web_app,
        "_wiki",
        lambda _repo_id: (_ for _ in ()).throw(
            AssertionError("historical source must not read the checkout")
        ),
    )

    result = asyncio.run(
        web_app.source("repo", "src/runtime.py", 4, 8, commit=selected)
    )

    assert result["content"] == "historical\n"
    assert calls == [(selected, "src/runtime.py", 4, 8)]


def test_edge_label_uses_the_graph_payload_commit(monkeypatch):
    from codenib.web.schemas import EdgeEndpoint, EdgeLabelRequest

    selected = "abcdef1234567890"
    calls = []

    class Labeler:
        def label(self, *args):
            return "calls", False

    def labeler(repo_id, commit):
        calls.append((repo_id, commit))
        return Labeler()

    monkeypatch.setattr(
        web_app, "load_config", lambda: SimpleNamespace(edge_labels=True)
    )
    monkeypatch.setattr(web_app, "_bundle", lambda _repo_id: object())
    monkeypatch.setattr(web_app, "_edge_labeler", labeler)

    request = EdgeLabelRequest(
        source=EdgeEndpoint(file="src/a.py", line=1),
        target=EdgeEndpoint(file="src/b.py", line=2),
        commit=selected,
    )
    result = asyncio.run(web_app.edge_label("repo", request))

    assert result.label == "calls"
    assert calls == [("repo", selected)]


def test_source_endpoint_reads_the_live_checkout_without_building_the_wiki(
    tmp_path, monkeypatch
):
    source = tmp_path / "runtime.py"
    source.write_text("first\nsecond\nthird\n")

    class Window:
        available = False

    bundle = SimpleNamespace(
        entry=SimpleNamespace(base_commit="1" * 40, repo_dir=str(tmp_path))
    )
    monkeypatch.setattr(web_app, "_bundle", lambda _repo_id: bundle)
    monkeypatch.setattr(web_app, "_commit_window", lambda _repo_id: Window())
    monkeypatch.setattr(
        web_app,
        "_wiki",
        lambda _repo_id: (_ for _ in ()).throw(
            AssertionError("source serving must not initialize wiki generation")
        ),
    )

    result = asyncio.run(web_app.source("repo", "runtime.py", 2, 3))

    assert result == {
        "file": "runtime.py",
        "start_line": 2,
        "end_line": 3,
        "content": "second\nthird\n",
    }
