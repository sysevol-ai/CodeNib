# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for non-blocking web runtime behavior."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import codenib.web.app as web_app
import codenib.wiki.media_generation as media_generation
from codenib.web.schemas import ChatRequest, ChatResponse


def test_request_timing_header_and_slow_log_exclude_query(monkeypatch, caplog):
    ticks = iter((10.0, 12.5))
    monkeypatch.setattr(web_app, "perf_counter", lambda: next(ticks))

    with caplog.at_level(logging.INFO, logger=web_app.logger.name):
        response = TestClient(web_app.app).get("/api/health?secret=query")

    assert response.headers["server-timing"] == "codenib;dur=2500.0"
    app_messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == web_app.logger.name
    ]
    assert "Slow API request: GET /api/health 2500.0 ms" in app_messages
    assert all("secret=query" not in message for message in app_messages)


def test_lifespan_injects_local_native_authority_resolver(monkeypatch):
    captured = {}
    config = SimpleNamespace(
        registry_path="/tmp/qa_registry.json",
        data_dir="/tmp/data",
        wiki_generation_model="model",
        wiki_agent=False,
        wiki_generation_api_base=None,
        wiki_generation_api_key=None,
        wiki_generation_options={},
    )
    resolver = object()

    class Registry:
        def __init__(self, supplied_config, **kwargs):
            captured["config"] = supplied_config
            captured.update(kwargs)

        def load_all(self):
            captured["loaded"] = True

        def list_infos(self):
            return []

    monkeypatch.setattr(web_app, "load_config", lambda: config)
    monkeypatch.setattr(web_app, "RepoRegistry", Registry)
    monkeypatch.setattr(web_app, "authorize_local_manifest_vector", resolver)
    monkeypatch.setattr(
        web_app,
        "_wiki_narrator",
        lambda _config: SimpleNamespace(model="model", enabled=False, cache_dir=None),
    )
    application = SimpleNamespace(state=SimpleNamespace())

    async def run_lifespan():
        async with web_app.lifespan(application):
            assert application.state.registry is not None

    asyncio.run(run_lifespan())

    assert captured == {
        "config": config,
        "native_index_authorization_resolver": resolver,
        "allow_missing_native_index_authorization": True,
        "loaded": True,
    }


def test_oversized_chat_request_is_rejected_before_runtime_lookup(monkeypatch):
    def unexpected_registry_lookup():
        raise AssertionError("invalid chat payload reached the repository runtime")

    monkeypatch.setattr(web_app, "_registry", unexpected_registry_lookup)

    response = TestClient(web_app.app).post(
        "/api/chat",
        json={
            "repo_id": "missing",
            "messages": [{"role": "user", "content": "bounded"} for _ in range(129)],
        },
    )

    assert response.status_code == 422


def test_chat_maps_citations_off_loop_from_the_served_checkout(monkeypatch):
    calls = []
    runner_result = object()

    class Runner:
        def run(self, query, *, chat_history):
            assert query == "Where is runtime?"
            assert chat_history == []
            return runner_result

    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo_dir="/served/checkout",
            base_commit="a" * 40,
        ),
        runner=Runner(),
        ensure_runtime=lambda: None,
    )

    class Registry:
        def get(self, repo_id):
            return bundle if repo_id == "repo" else None

    def fake_mapping(result, *, repo_path, repo_commit):
        assert result is runner_result
        assert repo_path == "/served/checkout"
        assert repo_commit == "a" * 40
        return ChatResponse(answer="answer")

    async def fake_to_thread(func, *args, **kwargs):
        calls.append(func)
        return func(*args, **kwargs)

    monkeypatch.setattr(web_app, "_registry", Registry)
    monkeypatch.setattr(web_app, "agent_result_to_response", fake_mapping)
    monkeypatch.setattr(web_app.asyncio, "to_thread", fake_to_thread)

    response = asyncio.run(
        web_app.chat(
            ChatRequest(
                repo_id="repo",
                messages=[{"role": "user", "content": "Where is runtime?"}],
            )
        )
    )

    assert response.answer == "answer"
    assert calls == [bundle.ensure_runtime, bundle.runner.run, fake_mapping]


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
        "media_slots": [],
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


def test_cached_wiki_tree_does_not_generate_a_missing_outline(monkeypatch):
    calls = []

    class Builder:
        def cached_page_tree(self):
            calls.append("cached_page_tree")
            return None

        def page_tree(self):
            raise AssertionError("cached-only Wiki lookup must not generate")

    monkeypatch.setattr(web_app, "_wiki", lambda _repo_id: Builder())
    monkeypatch.setattr(
        web_app,
        "_bundle",
        lambda _repo_id: SimpleNamespace(entry=SimpleNamespace(repo="org/repo")),
    )

    tree = asyncio.run(web_app.wiki_tree("repo", cached_only=True))

    assert tree == {"repo": "org/repo", "pages": []}
    assert calls == ["cached_page_tree"]


def test_wiki_page_materializes_local_svg_media(tmp_path, monkeypatch):
    class Builder:
        def page(self, page_id):
            return {
                "id": page_id,
                "title": "Overview",
                "markdown": "# Overview",
                "citations": [],
                "diagram": "",
                "media_slots": [
                    {
                        "id": "overview-concept-illustration",
                        "kind": "image",
                        "placement": "section",
                        "title": "Overview concept illustration",
                        "purpose": "Show the source-grounded flow.",
                        "source_citations": ["src/runtime.py"],
                        "prompt": "Draw the runtime.",
                        "human_prior": {"editable": True, "notes": []},
                    }
                ],
            }

    config = SimpleNamespace(
        data_dir=str(tmp_path),
        wiki_media_generation_enabled=True,
        wiki_media_model="local/svg",
        wiki_media_api_base=None,
        wiki_media_api_key=None,
        wiki_media_options={},
    )
    monkeypatch.setattr(web_app, "_wiki", lambda _repo_id: Builder())
    monkeypatch.setattr(web_app, "load_config", lambda: config)
    monkeypatch.setattr(
        web_app,
        "_bundle",
        lambda _repo_id: SimpleNamespace(entry=SimpleNamespace(repo="org/repo")),
    )

    page = asyncio.run(web_app.wiki_page("demo", "overview"))

    asset = page["media_slots"][0]["asset"]
    assert asset["uri"].endswith(
        "/api/repos/demo/wiki-media/overview/overview-concept-illustration.svg"
    ) or asset["uri"].endswith(
        "api/repos/demo/wiki-media/overview/overview-concept-illustration.svg"
    )
    asset_response = asyncio.run(
        web_app.wiki_media_asset(
            "demo",
            "overview",
            "overview-concept-illustration.svg",
        )
    )
    assert asset_response.body.startswith(b"<svg")
    assert asset_response.media_type == "image/svg+xml"
    assert asset_response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in asset_response.headers["content-security-policy"]


def test_wiki_media_storage_keys_cannot_traverse_data_root(tmp_path):
    config = SimpleNamespace(data_dir=str(tmp_path))
    root = tmp_path / "wiki_media"

    target = web_app._wiki_media_dir(config, "..", "../secret")

    assert target.is_relative_to(root)
    assert target != root
    assert ".." not in target.parts

    with pytest.raises(web_app.HTTPException):
        web_app._safe_media_filename("asset\x00.svg")


def test_wiki_media_asset_rejects_links_and_oversized_files(tmp_path, monkeypatch):
    config = SimpleNamespace(data_dir=str(tmp_path))
    monkeypatch.setattr(web_app, "load_config", lambda: config)
    monkeypatch.setattr(web_app, "_bundle", lambda _repo_id: object())
    media_dir = web_app._wiki_media_dir(config, "demo", "overview")
    media_dir.mkdir(parents=True)
    secret = tmp_path / "secret.svg"
    secret.write_text("<svg>secret</svg>", encoding="utf-8")
    linked = media_dir / "linked.svg"
    try:
        linked.symlink_to(secret)
    except (NotImplementedError, OSError):
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(web_app.HTTPException) as linked_error:
        asyncio.run(web_app.wiki_media_asset("demo", "overview", "linked.svg"))
    assert linked_error.value.status_code == 404

    monkeypatch.setattr(media_generation, "_MAX_IMAGE_BYTES", 4)
    (media_dir / "large.png").write_bytes(b"12345")
    with pytest.raises(web_app.HTTPException) as large_error:
        asyncio.run(web_app.wiki_media_asset("demo", "overview", "large.png"))
    assert large_error.value.status_code == 404


def test_wiki_media_materialization_does_not_swallow_memory_error(
    tmp_path, monkeypatch
):
    config = SimpleNamespace(data_dir=str(tmp_path))
    monkeypatch.setattr(web_app, "load_config", lambda: config)
    monkeypatch.setattr(
        web_app, "image_generator_from_config", lambda _config: object()
    )

    def fail_materialization(*_args, **_kwargs):
        raise MemoryError("out of memory")

    monkeypatch.setattr(web_app, "materialize_media_slots", fail_materialization)

    with pytest.raises(MemoryError, match="out of memory"):
        web_app._materialize_wiki_media(
            "demo", "overview", {"media_slots": [{"id": "asset"}]}
        )


def test_wiki_page_graph_reports_why_the_graph_is_unavailable(monkeypatch):
    class Builder:
        def page_citations(self, page_id):
            return []

        def page(self, page_id):
            raise AssertionError("page graph must not generate prose")

    bundle = SimpleNamespace(
        code_graph=lambda: None,
        graph_unavailable_note=lambda: (
            "Dependency graph uses schema 4, but this server requires schema 5."
        ),
    )
    monkeypatch.setattr(web_app, "_wiki", lambda _repo_id: Builder())
    monkeypatch.setattr(web_app, "_bundle", lambda _repo_id: bundle)

    result = asyncio.run(web_app.wiki_page_graph("repo", "overview"))

    assert result == {
        "available": False,
        "nodes": [],
        "edges": [],
        "mermaid": "",
        "note": "Dependency graph uses schema 4, but this server requires schema 5.",
    }


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
