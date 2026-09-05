# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for non-blocking web runtime behavior."""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import codenib.web.app as web_app
import codenib.wiki.media_generation as media_generation
from codenib.repository_source_selection import RepositorySourceSelection
from codenib.source_fingerprint import capture_repository_source
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


def test_web_app_has_no_retained_storage_control_plane() -> None:
    paths = {route.path for route in web_app.app.routes}

    assert not any("index-jobs" in path for path in paths)
    assert not hasattr(web_app, "_configured_local_index_runtime")


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

        def close(self):
            captured["closed"] = True

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
        "closed": True,
    }


def test_lifespan_closes_registry_when_startup_is_cancelled(monkeypatch):
    captured = {}
    config = SimpleNamespace(registry_path="/tmp/qa_registry.json")

    class Registry:
        def __init__(self, _config, **_kwargs):
            pass

        def load_all(self):
            raise asyncio.CancelledError()

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(web_app, "load_config", lambda: config)
    monkeypatch.setattr(web_app, "RepoRegistry", Registry)
    application = SimpleNamespace(state=SimpleNamespace())

    async def run_lifespan():
        async with web_app.lifespan(application):
            raise AssertionError("cancelled startup must not yield")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_lifespan())

    assert captured == {"closed": True}


def test_lifespan_defers_optional_wiki_store_initialization(monkeypatch):
    events = []
    config = SimpleNamespace(
        registry_path="/tmp/qa_registry.json",
        data_dir="/tmp/data",
        wiki_agent=True,
        wiki_generation_model="model",
    )

    class Registry:
        def __init__(self, _config, **_kwargs):
            pass

        def load_all(self):
            events.append("registry-load")

        def list_infos(self):
            return []

        def close(self):
            events.append("registry-close")

    monkeypatch.setattr(web_app, "load_config", lambda: config)
    monkeypatch.setattr(web_app, "RepoRegistry", Registry)
    monkeypatch.setattr(
        web_app,
        "SQLiteWikiStore",
        lambda *_args, **_kwargs: pytest.fail("Wiki store opened during startup"),
    )
    monkeypatch.setattr(
        web_app,
        "_wiki_narrator",
        lambda _config: SimpleNamespace(model="model", enabled=True, cache_dir=None),
    )
    application = SimpleNamespace(state=SimpleNamespace())

    async def run_lifespan():
        async with web_app.lifespan(application):
            events.append("serve")
            assert application.state.wiki_store is None

    asyncio.run(run_lifespan())

    assert events == ["registry-load", "serve", "registry-close"]


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
    source_reader = object()

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
        source_reader=source_reader,
    )

    class Registry:
        def get(self, repo_id):
            return bundle if repo_id == "repo" else None

    def fake_mapping(result, *, repo_path, source_reader: object):
        assert result is runner_result
        assert repo_path == "/served/checkout"
        assert source_reader is bundle.source_reader
        return ChatResponse(answer="answer")

    async def fake_to_thread(func, *args, **kwargs):
        observed = args[0] if func is web_app._capture_thread_outcome else func
        calls.append(observed)
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


def test_chat_pins_one_bundle_generation_through_response_mapping(monkeypatch):
    events = []
    result = object()

    class Runner:
        def run(self, _query, *, chat_history):
            assert chat_history == []
            assert events == ["pin-enter", "runtime"]
            events.append("run")
            return result

    def ensure_runtime():
        assert events == ["pin-enter"]
        events.append("runtime")

    bundle = SimpleNamespace(
        entry=SimpleNamespace(repo_dir="/served/checkout"),
        runner=Runner(),
        ensure_runtime=ensure_runtime,
        source_reader=object(),
    )

    class Registry:
        @contextmanager
        def pin(self, repo_id):
            assert repo_id == "repo"
            events.append("pin-enter")
            try:
                yield bundle
            finally:
                events.append("pin-exit")

    def map_response(observed, **_kwargs):
        assert observed is result
        assert events == ["pin-enter", "runtime", "run"]
        events.append("map")
        return ChatResponse(answer="answer")

    async def inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(web_app.app.state, "registry", Registry(), raising=False)
    monkeypatch.setattr(web_app, "agent_result_to_response", map_response)
    monkeypatch.setattr(web_app.asyncio, "to_thread", inline)

    response = asyncio.run(
        web_app.chat(
            ChatRequest(
                repo_id="repo",
                messages=[{"role": "user", "content": "Where is runtime?"}],
            )
        )
    )

    assert response.answer == "answer"
    assert events == ["pin-enter", "runtime", "run", "map", "pin-exit"]


def test_pinned_bundle_preserves_get_only_registry_fallback(monkeypatch):
    bundle = object()
    calls = []

    class Registry:
        def get(self, repo_id):
            calls.append(repo_id)
            return bundle if repo_id == "repo" else None

    monkeypatch.setattr(web_app.app.state, "registry", Registry(), raising=False)

    with web_app._pinned_bundle("repo") as observed:
        assert observed is bundle

    with pytest.raises(web_app.HTTPException) as error:
        with web_app._pinned_bundle("missing"):
            raise AssertionError("missing bundle context yielded")

    assert error.value.status_code == 404
    assert calls == ["repo", "missing"]


def test_pinned_bundle_releases_on_exact_base_exception(monkeypatch):
    stop = SystemExit("request stopped")
    events = []

    class Registry:
        @contextmanager
        def pin(self, _repo_id):
            events.append("pin-enter")
            try:
                yield object()
            finally:
                events.append("pin-exit")

    monkeypatch.setattr(web_app.app.state, "registry", Registry(), raising=False)

    with pytest.raises(SystemExit) as observed:
        with web_app._pinned_bundle("repo"):
            raise stop

    assert observed.value is stop
    assert events == ["pin-enter", "pin-exit"]


def test_chat_cancellation_keeps_bundle_pinned_until_worker_settles(monkeypatch):
    events = []
    worker_started = threading.Event()
    release_worker = threading.Event()

    class Runner:
        def run(self, _query, *, chat_history):
            assert chat_history == []
            events.append("worker-enter")
            worker_started.set()
            assert release_worker.wait(timeout=5)
            events.append("worker-exit")
            return object()

    bundle = SimpleNamespace(
        entry=SimpleNamespace(repo_dir="/served/checkout"),
        runner=Runner(),
        ensure_runtime=lambda: None,
        source_reader=object(),
    )

    class Registry:
        @contextmanager
        def pin(self, repo_id):
            assert repo_id == "repo"
            events.append("pin-enter")
            try:
                yield bundle
            finally:
                events.append("pin-exit")

    monkeypatch.setattr(web_app.app.state, "registry", Registry(), raising=False)

    async def cancel_while_worker_is_blocked():
        request = asyncio.create_task(
            web_app.chat(
                ChatRequest(
                    repo_id="repo",
                    messages=[{"role": "user", "content": "Where is runtime?"}],
                )
            )
        )
        started = await asyncio.to_thread(worker_started.wait, 5)
        assert started
        request.cancel()
        for _ in range(3):
            await asyncio.sleep(0)
        assert not request.done()
        assert events == ["pin-enter", "worker-enter"]
        release_worker.set()
        with pytest.raises(asyncio.CancelledError):
            await request

    asyncio.run(cancel_while_worker_is_blocked())

    assert events == ["pin-enter", "worker-enter", "worker-exit", "pin-exit"]


def test_pinned_thread_preserves_cancelled_error_identity():
    failure = asyncio.CancelledError("thread cancelled")

    def fail():
        raise failure

    async def observe():
        try:
            await web_app._run_pinned_thread(fail)
        except BaseException as observed:  # noqa: B036 - assert exact carrier
            return observed
        raise AssertionError("thread failure did not propagate")

    assert asyncio.run(observe()) is failure


def test_pinned_thread_does_not_read_hostile_exception_class():
    touched = []

    class HostileFailure(BaseException):
        @property
        def __class__(self):
            touched.append("class")
            raise SystemExit("spoofed worker failure")

    failure = HostileFailure()

    def fail():
        raise failure

    async def observe():
        try:
            await web_app._run_pinned_thread(fail)
        except BaseException as observed:  # noqa: B036 - assert exact carrier
            return observed
        raise AssertionError("thread failure did not propagate")

    assert asyncio.run(observe()) is failure
    assert touched == []


def test_pinned_thread_maps_stop_iteration_without_losing_exact_cause():
    failure = StopIteration("thread stop")

    def fail():
        raise failure

    async def observe():
        with pytest.raises(RuntimeError) as observed:
            await web_app._run_pinned_thread(fail)
        return observed.value

    observed = asyncio.run(observe())

    assert str(observed) == "thread worker raised an iteration sentinel"
    assert observed.__cause__ is failure


def test_pinned_thread_outer_cancellation_precedes_worker_failure():
    worker_started = threading.Event()
    release_worker = threading.Event()
    worker_finished = threading.Event()

    def fail_after_release():
        worker_started.set()
        assert release_worker.wait(5)
        worker_finished.set()
        raise StopIteration("secondary worker stop")

    async def cancel_worker():
        request = asyncio.create_task(web_app._run_pinned_thread(fail_after_release))
        assert await asyncio.to_thread(worker_started.wait, 5)
        request.cancel()
        await asyncio.sleep(0)
        assert not request.done()
        release_worker.set()
        with pytest.raises(asyncio.CancelledError):
            await request

    asyncio.run(cancel_worker())

    assert worker_finished.is_set()


def test_wiki_cache_rebuilds_when_bundle_generation_changes(monkeypatch):
    created = []
    config = SimpleNamespace(wiki_agent=False)

    def build(bundle, narrator=None):
        builder = SimpleNamespace(bundle=bundle, narrator=narrator)
        created.append(builder)
        return builder

    old = object()
    new = object()
    monkeypatch.setattr(web_app, "load_config", lambda: config)
    monkeypatch.setattr(web_app, "WikiBuilder", build)
    monkeypatch.setattr(web_app.app.state, "wiki_builders", {}, raising=False)
    monkeypatch.setattr(web_app.app.state, "narrator", object(), raising=False)

    first = web_app._wiki("repo", old)
    repeated = web_app._wiki("repo", old)
    refreshed = web_app._wiki("repo", new)

    assert repeated is first
    assert refreshed is not first
    assert [builder.bundle for builder in created] == [old, new]
    assert web_app.app.state.wiki_builders["repo"] == ("repo", new, refreshed)


def test_wiki_cache_serializes_concurrent_lazy_construction(tmp_path, monkeypatch):
    import codenib.wiki.agent_wiki as agent_wiki

    first_entered = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    second_entered = threading.Event()
    call_guard = threading.Lock()
    store = object()
    bundle = object()
    call_count = 0
    config = SimpleNamespace(
        data_dir=str(tmp_path),
        wiki_agent=True,
        wiki_generation_model="wiki-model",
        wiki_generation_api_base=None,
        wiki_generation_api_key=None,
    )

    def open_store(_path):
        nonlocal call_count
        with call_guard:
            call_count += 1
            ordinal = call_count
        if ordinal == 1:
            first_entered.set()
            assert release_first.wait(timeout=5)
        else:
            second_entered.set()
        return store

    def open_second():
        second_started.set()
        return web_app._wiki("repo", bundle)

    monkeypatch.setattr(web_app, "_WIKI_BUILD_LOCK", threading.Lock())
    monkeypatch.setattr(web_app, "load_config", lambda: config)
    monkeypatch.setattr(web_app, "_wiki_llm", lambda _config: object())
    monkeypatch.setattr(web_app, "SQLiteWikiStore", open_store)
    monkeypatch.setattr(agent_wiki, "AgentWiki", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(web_app.app.state, "wiki_builders", {}, raising=False)
    monkeypatch.setattr(web_app.app.state, "wiki_store", None, raising=False)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_result = executor.submit(web_app._wiki, "repo", bundle)
        try:
            assert first_entered.wait(timeout=5)
            second_result = executor.submit(open_second)
            assert second_started.wait(timeout=5)
            assert not second_entered.wait(timeout=0.2)
        finally:
            release_first.set()
        first = first_result.result(timeout=5)
        second = second_result.result(timeout=5)

    assert call_count == 1
    assert second is first
    assert web_app.app.state.wiki_store is store


def test_generation_cache_drops_helpers_bound_to_retired_bundle():
    old = SimpleNamespace(entry=SimpleNamespace(instance_id="repo"))
    new = SimpleNamespace(entry=SimpleNamespace(instance_id="repo"))
    cache = {
        "repo@old": ("repo", old, object()),
        "other@commit": (
            "other",
            SimpleNamespace(entry=SimpleNamespace(instance_id="other")),
            object(),
        ),
    }

    current = web_app._generation_cached(
        cache,
        "repo@new",
        "repo",
        new,
        object,
    )

    assert cache["repo@new"] == ("repo", new, current)
    assert "repo@old" not in cache
    assert "other@commit" in cache


def test_generation_cache_does_not_read_hostile_bundle_repo_id():
    touched = []

    class HostileId(str):
        def __eq__(self, other):
            touched.append(other)
            return str.__eq__(self, other)

    old = SimpleNamespace(entry=SimpleNamespace(instance_id="repo"))
    new = SimpleNamespace(entry=SimpleNamespace(instance_id=HostileId("repo")))
    cache = {"repo@old": ("repo", old, object())}

    current = web_app._generation_cached(
        cache,
        "repo@new",
        "repo",
        new,
        object,
    )

    assert touched == []
    assert cache == {"repo@new": ("repo", new, current)}


def test_retired_generation_caches_are_pruned_after_removal(monkeypatch):
    removed = SimpleNamespace(entry=SimpleNamespace(instance_id="removed"))
    current = SimpleNamespace(entry=SimpleNamespace(instance_id="current"))
    current_helper = object()
    cache = {
        "removed@commit": ("removed", removed, object()),
        "current@commit": ("current", current, current_helper),
    }
    registry = SimpleNamespace(
        get=lambda repo_id: current if repo_id == "current" else None
    )
    monkeypatch.setattr(web_app.app.state, "edge_labelers", cache, raising=False)
    monkeypatch.setattr(web_app.app.state, "wiki_builders", {}, raising=False)
    monkeypatch.setattr(web_app.app.state, "commit_windows", {}, raising=False)

    web_app._prune_retired_generation_caches(registry)

    assert cache == {"current@commit": ("current", current, current_helper)}


def test_list_repos_keeps_info_and_window_stats_on_one_generation(monkeypatch):
    events = []
    info = SimpleNamespace(
        id="repo",
        base_commit="old",
        capabilities={},
        incremental=None,
    )
    old = SimpleNamespace(
        entry=SimpleNamespace(instance_id="repo", base_commit="old"),
        info=lambda: info,
    )
    new = SimpleNamespace(entry=SimpleNamespace(instance_id="repo", base_commit="new"))

    class Registry:
        @contextmanager
        def pin_all(self):
            events.append("pin-enter")
            try:
                yield (old,)
            finally:
                events.append("pin-exit")

        def get(self, _repo_id):
            return new

        def list_infos(self):
            raise AssertionError("production listing must use the pinned snapshot")

    def window_stats(repo_id, bundle):
        assert repo_id == "repo"
        events.append(f"stats-{bundle.entry.base_commit}")
        return bundle.entry.base_commit

    monkeypatch.setattr(web_app.app.state, "registry", Registry(), raising=False)
    monkeypatch.setattr(
        web_app, "load_config", lambda: SimpleNamespace(edge_labels=False)
    )
    monkeypatch.setattr(web_app, "_window_stats_for_bundle", window_stats)

    observed = asyncio.run(web_app.list_repos())

    assert observed[0].base_commit == "old"
    assert observed[0].incremental == "old"
    assert events == ["pin-enter", "stats-old", "pin-exit"]


def test_chat_fails_closed_without_authenticated_source_reader(monkeypatch):
    class Runner:
        def run(self, _query, *, chat_history):
            assert chat_history == []
            return object()

    bundle = SimpleNamespace(
        entry=SimpleNamespace(repo_dir="/served/checkout"),
        runner=Runner(),
        ensure_runtime=lambda: None,
        source_reader=None,
    )
    monkeypatch.setattr(
        web_app,
        "_registry",
        lambda: SimpleNamespace(get=lambda _repo_id: bundle),
    )
    monkeypatch.setattr(
        web_app,
        "agent_result_to_response",
        lambda *_args, **_kwargs: pytest.fail(
            "missing reader must not hydrate citations"
        ),
    )

    with pytest.raises(web_app.HTTPException) as error:
        asyncio.run(
            web_app.chat(
                ChatRequest(
                    repo_id="repo",
                    messages=[{"role": "user", "content": "Where is runtime?"}],
                )
            )
        )

    assert error.value.status_code == 503


def test_wiki_generation_runs_off_event_loop(monkeypatch):
    calls = []

    class Builder:
        def page_tree(self):
            return [{"id": "overview"}]

        def page(self, page_id):
            return {"id": page_id}

        def page_citations(self, _page_id):
            return []

    async def fake_to_thread(func, *args, **kwargs):
        if func is web_app._capture_thread_outcome:
            observed = args[0]
            observed_args = args[1]
        else:
            observed = func
            observed_args = args
        calls.append((observed.__name__, observed_args))
        return func(*args, **kwargs)

    builder = Builder()

    def code_graph():
        return None

    bundle = SimpleNamespace(
        entry=SimpleNamespace(repo="org/repo"),
        code_graph=code_graph,
        graph_unavailable_note=lambda: "graph unavailable",
    )

    def build_wiki(_repo_id, _bundle=None):
        return builder

    monkeypatch.setattr(web_app, "_wiki", build_wiki)
    monkeypatch.setattr(web_app, "_bundle", lambda _repo_id: bundle)
    monkeypatch.setattr(web_app.asyncio, "to_thread", fake_to_thread)

    tree = asyncio.run(web_app.wiki_tree("repo"))
    page = asyncio.run(web_app.wiki_page("repo", "overview"))
    graph = asyncio.run(web_app.wiki_page_graph("repo", "overview"))

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
    assert graph == {
        "available": False,
        "nodes": [],
        "edges": [],
        "mermaid": "",
        "note": "graph unavailable",
    }
    assert calls == [
        ("build_wiki", ("repo", bundle)),
        ("page_tree", ()),
        ("build_wiki", ("repo", bundle)),
        ("page", ("overview",)),
        ("build_wiki", ("repo", bundle)),
        ("page_citations", ("overview",)),
        ("code_graph", ()),
    ]


def test_cached_wiki_tree_does_not_generate_a_missing_outline(monkeypatch):
    calls = []

    class Builder:
        def cached_page_tree(self):
            calls.append("cached_page_tree")
            return None

        def page_tree(self):
            raise AssertionError("cached-only Wiki lookup must not generate")

    monkeypatch.setattr(web_app, "_wiki", lambda _repo_id, _bundle=None: Builder())
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
    monkeypatch.setattr(web_app, "_wiki", lambda _repo_id, _bundle=None: Builder())
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
    assert asset["metadata"]["evidence_pack_sha256"]
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


def test_wiki_media_materialization_builds_page_evidence(tmp_path, monkeypatch):
    captured = {}

    class SourceReader:
        def captured_relative_path(self, path):
            return path if path == "src/runtime.py" else None

        def read_line_range(self, relative, *, start_line, end_line, max_bytes):
            captured["source_read"] = (
                relative,
                start_line,
                end_line,
                max_bytes,
            )
            return b"def run(): return 'grounded'\n"

    class Generator:
        def generate(self, slot, **_kwargs):
            captured["generation_slot"] = slot
            return {"slot_id": slot["id"], "provider": "capture"}

    config = SimpleNamespace(data_dir=str(tmp_path))
    monkeypatch.setattr(web_app, "load_config", lambda: config)
    monkeypatch.setattr(
        web_app, "image_generator_from_config", lambda _config: Generator()
    )
    monkeypatch.setattr(
        web_app,
        "_bundle",
        lambda _repo_id: SimpleNamespace(source_reader=SourceReader()),
    )
    page = {
        "id": "overview",
        "title": "Overview",
        "markdown": "# Overview\n\nRuntime flow.",
        "citations": [
            {
                "file": "src/runtime.py",
                "start_line": 7,
                "end_line": 9,
                "node_name": "run",
                "content": None,
            }
        ],
        "evidence": {
            "relations": [{"source": "run", "target": "render", "relation": "calls"}]
        },
        "media_slots": [
            {
                "id": "overview-image",
                "kind": "image",
                "prompt": "Draw the runtime.",
                "source_citations": ["src/runtime.py"],
            }
        ],
    }

    materialized = web_app._materialize_wiki_media("demo", "overview", page)

    evidence_pack = captured["generation_slot"]["evidence_pack"]
    assert evidence_pack["page"]["summary"] == "Runtime flow."
    assert evidence_pack["sources"] == [
        {
            "file": "src/runtime.py",
            "symbol": "run",
            "start_line": 7,
            "end_line": 9,
            "snippet": "def run(): return 'grounded'",
        }
    ]
    assert evidence_pack["relations"] == [
        {"source": "run", "target": "render", "relation": "calls"}
    ]
    assert captured["source_read"][:3] == ("src/runtime.py", 7, 9)
    assert "evidence_pack" not in materialized["media_slots"][0]


def test_wiki_page_can_skip_media_materialization_for_preload(monkeypatch):
    slot = {
        "id": "overview-concept-illustration",
        "kind": "image",
        "prompt": "Draw the runtime.",
        "evidence_pack": {"snippet": "server-only preload evidence"},
    }

    class Builder:
        def page(self, page_id):
            return {
                "id": page_id,
                "title": "Overview",
                "markdown": "# Overview",
                "citations": [],
                "diagram": "",
                "media_slots": [slot],
            }

    monkeypatch.setattr(web_app, "_wiki", lambda _repo_id, _bundle=None: Builder())
    monkeypatch.setattr(
        web_app,
        "_bundle",
        lambda _repo_id: SimpleNamespace(entry=SimpleNamespace(repo="org/repo")),
    )
    monkeypatch.setattr(
        web_app,
        "_materialize_wiki_media",
        lambda *_args: pytest.fail("read-only preload must not generate media"),
    )

    page = asyncio.run(web_app.wiki_page("demo", "overview", materialize_media=False))

    assert page["media_slots"] == [
        {
            "id": "overview-concept-illustration",
            "kind": "image",
            "prompt": "Draw the runtime.",
        }
    ]
    assert "asset" not in page["media_slots"][0]
    assert "server-only preload evidence" not in str(page)
    assert slot["evidence_pack"]["snippet"] == "server-only preload evidence"


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
    monkeypatch.setattr(
        web_app, "_bundle", lambda _repo_id: SimpleNamespace(source_reader=None)
    )

    def fail_materialization(*_args, **_kwargs):
        raise MemoryError("out of memory")

    monkeypatch.setattr(web_app, "materialize_media_slots", fail_materialization)

    with pytest.raises(MemoryError, match="out of memory"):
        web_app._materialize_wiki_media(
            "demo", "overview", {"media_slots": [{"id": "asset"}]}
        )


def test_wiki_media_redacts_evidence_when_generation_is_disabled(tmp_path, monkeypatch):
    config = SimpleNamespace(data_dir=str(tmp_path))
    monkeypatch.setattr(web_app, "load_config", lambda: config)
    monkeypatch.setattr(web_app, "image_generator_from_config", lambda _config: None)
    page = {
        "media_slots": [
            {"id": "asset", "evidence_pack": {"snippet": "server-only secret"}}
        ]
    }

    public_page = web_app._materialize_wiki_media("demo", "overview", page)

    assert "evidence_pack" not in public_page["media_slots"][0]
    assert page["media_slots"][0]["evidence_pack"]["snippet"] == "server-only secret"


def test_wiki_media_redacts_evidence_after_generation_failure(tmp_path, monkeypatch):
    config = SimpleNamespace(data_dir=str(tmp_path))
    monkeypatch.setattr(web_app, "load_config", lambda: config)
    monkeypatch.setattr(
        web_app, "image_generator_from_config", lambda _config: object()
    )
    monkeypatch.setattr(
        web_app, "_bundle", lambda _repo_id: SimpleNamespace(source_reader=None)
    )
    monkeypatch.setattr(
        web_app,
        "materialize_media_slots",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("failed")),
    )
    page = {
        "media_slots": [
            {"id": "asset", "evidence_pack": {"snippet": "server-only secret"}}
        ]
    }

    public_page = web_app._materialize_wiki_media("demo", "overview", page)

    assert "evidence_pack" not in public_page["media_slots"][0]
    assert "server-only secret" not in str(public_page)


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
    monkeypatch.setattr(web_app, "_wiki", lambda _repo_id, _bundle=None: Builder())
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


def test_agent_wiki_receives_the_shared_wiki_store(tmp_path, monkeypatch):
    import codenib.wiki.agent_wiki as agent_wiki

    store = object()
    bundle = object()
    captured = {}
    config = SimpleNamespace(
        data_dir=str(tmp_path),
        wiki_agent=True,
        wiki_generation_model="wiki-model",
        wiki_generation_api_base=None,
        wiki_generation_api_key=None,
    )

    def build(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(web_app, "load_config", lambda: config)
    monkeypatch.setattr(web_app, "_bundle", lambda _repo_id: bundle)
    monkeypatch.setattr(web_app, "_wiki_llm", lambda _config: "llm")
    monkeypatch.setattr(agent_wiki, "AgentWiki", build)
    monkeypatch.setattr(web_app.app.state, "wiki_builders", {}, raising=False)
    monkeypatch.setattr(web_app.app.state, "wiki_store", store, raising=False)

    created = web_app._wiki("repo")

    assert web_app.app.state.wiki_builders["repo"] == ("repo", bundle, created)
    assert captured["args"] == (bundle, "wiki-model")
    assert captured["kwargs"]["store"] is store
    assert "cache_dir" not in captured["kwargs"]
    assert captured["kwargs"]["llm"] == "llm"


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
    monkeypatch.setattr(
        web_app,
        "_commit_window",
        lambda _repo_id, _bundle=None: Window(),
    )

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
    monkeypatch.setattr(
        web_app,
        "_commit_window",
        lambda _repo_id, _bundle=None: Window(),
    )

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
    monkeypatch.setattr(
        web_app,
        "_commit_window",
        lambda _repo_id, _bundle=None: Window(),
    )

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
        manifest=SimpleNamespace(
            source_selection=RepositorySourceSelection(("private",))
        ),
        code_graph=lambda: None,
        hierarchical_graph=no_current_hierarchy,
        source_reader=object(),
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
    monkeypatch.setattr(
        web_app,
        "_commit_window",
        lambda _repo_id, _bundle=None: Window(),
    )
    monkeypatch.setattr(codemap_builder, "build_codemap", fake_codemap)
    monkeypatch.setattr(modulemap_builder, "build_modulemap", fake_modulemap)

    codemap_result = asyncio.run(web_app.codemap("repo", commit=selected))
    modulemap_result = asyncio.run(web_app.modulemap("repo", commit=selected))

    assert codemap_result["commit"] == selected
    assert modulemap_result["commit"] == selected
    assert captured["codemap"][1]["repo_commit"] == selected
    assert captured["codemap"][1]["hierarchy_graph"] is None
    assert captured["codemap"][1]["source_reader"] is None
    assert captured["codemap"][1]["source_selection"] == RepositorySourceSelection(
        ("private",)
    )
    assert captured["modulemap"][1]["repo_commit"] == selected
    assert captured["modulemap"][1]["source_reader"] is None
    assert captured["modulemap"][1]["source_selection"] == (
        RepositorySourceSelection(("private",))
    )


def test_current_codemap_uses_the_authenticated_source_reader(monkeypatch):
    import codenib.web.codemap as codemap_builder

    graph = object()
    reader = object()
    captured = {}

    class Window:
        available = False

    bundle = SimpleNamespace(
        entry=SimpleNamespace(base_commit="abc123", repo_dir="/tmp/repository"),
        source_reader=reader,
        code_graph=lambda: graph,
        hierarchical_graph=lambda: None,
    )

    def fake_codemap(*args, **kwargs):
        captured.update(kwargs)
        return {"available": True, "nodes": [], "edges": []}

    monkeypatch.setattr(web_app, "_bundle", lambda _repo_id: bundle)
    monkeypatch.setattr(
        web_app,
        "_commit_window",
        lambda _repo_id, _bundle=None: Window(),
    )
    monkeypatch.setattr(codemap_builder, "build_codemap", fake_codemap)

    result = asyncio.run(web_app.codemap("repo"))

    assert result["commit"] == "abc123"
    assert captured["source_reader"] is reader
    assert captured["repo_commit"] is None


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

    bundle = SimpleNamespace(
        entry=SimpleNamespace(base_commit="1" * 40),
        manifest=SimpleNamespace(
            source_selection=RepositorySourceSelection(("private",))
        ),
    )
    monkeypatch.setattr(web_app, "_bundle", lambda _repo_id: bundle)
    monkeypatch.setattr(
        web_app,
        "_commit_window",
        lambda _repo_id, _bundle=None: Window(),
    )
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


def test_source_endpoint_rejects_excluded_historical_source(monkeypatch):
    selected = "abcdef1234567890"

    class Window:
        available = True

        def resolve(self, commit):
            return {"sha": selected} if commit == selected else None

        def source_for(self, *_args):
            raise AssertionError("excluded historical source must not reach Git")

    bundle = SimpleNamespace(
        entry=SimpleNamespace(base_commit="1" * 40),
        manifest=SimpleNamespace(
            source_selection=RepositorySourceSelection(("private",))
        ),
    )
    monkeypatch.setattr(web_app, "_bundle", lambda _repo_id: bundle)
    monkeypatch.setattr(
        web_app,
        "_commit_window",
        lambda _repo_id, _bundle=None: Window(),
    )

    with pytest.raises(web_app.HTTPException) as error:
        asyncio.run(
            web_app.source(
                "repo",
                "private/secret.py",
                commit=selected,
            )
        )

    assert error.value.status_code == 404


def test_edge_label_uses_the_graph_payload_commit(monkeypatch):
    from codenib.web.schemas import EdgeEndpoint, EdgeLabelRequest

    selected = "abcdef1234567890"
    calls = []

    class Labeler:
        def label(self, *args):
            return "calls", False

    def labeler(repo_id, commit, _bundle=None):
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


def test_historical_edge_labeler_gates_source_with_manifest_selection(monkeypatch):
    import codenib.web.edge_label as edge_label_module

    selected = "abcdef1234567890"
    source_calls = []
    captured = {}

    class Window:
        available = True

        def resolve(self, commit):
            return {"sha": selected} if commit == selected else None

        def source_for(self, commit, file, start, end):
            source_calls.append((commit, file, start, end))
            return {"file": file, "content": "historical\n"}

    class Labeler:
        def __init__(self, *, source_fn, **_kwargs):
            captured["source_fn"] = source_fn

    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            base_commit="1" * 40,
            instance_id="owner__repo-1",
        ),
        manifest=SimpleNamespace(
            source_selection=RepositorySourceSelection(("private",))
        ),
    )
    config = SimpleNamespace(
        data_dir="/tmp/data",
        edge_label_model=None,
        wiki_generation_model="fake-model",
        wiki_generation_api_base=None,
        wiki_generation_api_key=None,
    )
    monkeypatch.setattr(web_app, "_bundle", lambda _repo_id: bundle)
    monkeypatch.setattr(
        web_app,
        "_commit_window",
        lambda _repo_id, _bundle=None: Window(),
    )
    monkeypatch.setattr(web_app, "load_config", lambda: config)
    monkeypatch.setattr(web_app, "_wiki_llm", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(edge_label_module, "EdgeLabeler", Labeler)
    monkeypatch.setattr(web_app.app.state, "edge_labelers", {}, raising=False)

    web_app._edge_labeler("repo", selected)
    source_fn = captured["source_fn"]

    assert source_fn("private/secret.py", 1, 2) is None
    assert source_calls == []
    assert source_fn("src/runtime.py", 3, 4)["content"] == "historical\n"
    assert source_calls == [(selected, "src/runtime.py", 3, 4)]


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
    binding = capture_repository_source(tmp_path)
    bundle.source_reader = binding.borrow_reader()
    monkeypatch.setattr(web_app, "_bundle", lambda _repo_id: bundle)
    monkeypatch.setattr(
        web_app,
        "_commit_window",
        lambda _repo_id, _bundle=None: Window(),
    )
    monkeypatch.setattr(
        web_app,
        "_wiki",
        lambda _repo_id: (_ for _ in ()).throw(
            AssertionError("source serving must not initialize wiki generation")
        ),
    )

    try:
        result = asyncio.run(web_app.source("repo", "runtime.py", 2, 3))
    finally:
        binding.close()

    assert result == {
        "file": "runtime.py",
        "start_line": 2,
        "end_line": 3,
        "content": "second\nthird\n",
    }


def test_source_endpoint_returns_404_for_excluded_current_source(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "runtime.py").write_text("visible\n")
    (tmp_path / "excluded").mkdir()
    (tmp_path / "excluded" / "secret.py").write_text("secret\n")
    binding = capture_repository_source(
        tmp_path,
        selection=RepositorySourceSelection(("excluded",)),
    )
    bundle = SimpleNamespace(
        entry=SimpleNamespace(base_commit="1" * 40, repo_dir=str(tmp_path)),
        source_reader=binding.borrow_reader(),
    )
    monkeypatch.setattr(web_app, "_bundle", lambda _repo_id: bundle)

    try:
        with pytest.raises(web_app.HTTPException) as error:
            asyncio.run(web_app.source("repo", "excluded/secret.py"))
    finally:
        binding.close()

    assert error.value.status_code == 404
