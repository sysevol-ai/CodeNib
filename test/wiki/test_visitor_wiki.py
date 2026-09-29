# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
import io
import json
import stat
import threading
import zipfile
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from codenib.storage import SQLiteWikiStore
from codenib.web.visitor_wikis import router
from codenib.wiki import visitor_graph, visitor_provider, visitor_source
from codenib.wiki.builder import WikiBuilder
from codenib.wiki.store import WikiGenerationBusyError
from codenib.wiki.visitor_provider import VisitorProvider, WikiRunStopped
from codenib.wiki.visitor_source import extract_source
from codenib.wiki.visitor_wiki import VisitorWikiError, VisitorWikis

ATTEMPT, OWNER, COMMIT = "a" * 64, "b" * 64, "c" * 40
KEY = "sk-or-test-do-not-save-this"
PAGES = [
    {"id": "overview", "title": "Overview", "children": []},
    {"id": "pipeline", "title": "Pipeline", "children": []},
]


class FakeProvider:
    def __init__(self, key, budget, check, changed, **_kwargs):
        self.key, self.check = key, check

    def close(self):
        self.key = ""


class FakeWiki:
    produced = []
    before_page = None

    def __init__(self, bundle, model, *, store, **_kwargs):
        self.store = store

    def outline(self):
        return {"pages": PAGES}

    def page_tree(self):
        return PAGES

    def cached_page(self, page):
        found = self.store.read(f"cached:{page}")
        return copy.deepcopy(found.envelope["data"]) if found else None

    def page(self, page, **_kwargs):
        if type(self).before_page:
            type(self).before_page(page)
        self.produced.append(page)
        result = {
            "id": page,
            "title": page,
            "markdown": "# A real chapter\n\nSource text [E1](#evidence-E1).",
            "citations": [{"file": "main.py", "start_line": 1, "end_line": 2}],
            "generation": {"mode": "generated"},
            "grounding": {"valid": True},
            "quality": {"valid": True},
        }
        self.store.publish(
            entry_id=f"cached:{page}", repository_id=ATTEMPT, envelope={"data": result}
        )
        return result

    def source(self, *_args):
        return {"content": "def main():\n    return 1"}


@contextmanager
def prepare(*_args):
    yield SimpleNamespace(), None


@pytest.fixture(autouse=True)
def local_index_only(monkeypatch):
    """Source preparation tests must not start an installed compiler tool."""
    monkeypatch.setattr(visitor_graph, "resolve_command", lambda _: None)


@pytest.fixture
def manager(tmp_path, monkeypatch):
    FakeWiki.produced, FakeWiki.before_page = [], None
    monkeypatch.setattr(VisitorWikis, "_dispatch", VisitorWikis._run)
    return VisitorWikis(
        SQLiteWikiStore(tmp_path / "wiki.sqlite3"),
        prepare=prepare,
        resolve=lambda *_: COMMIT,
        provider=FakeProvider,
        verify=lambda _: None,
        wiki_factory=FakeWiki,
        clock=lambda: 1000,
    )


def submit(manager, client="test"):
    return manager.submit(ATTEMPT, "owner/repo", OWNER, KEY, 2, client)


def test_full_wiki_saved_and_reopened_without_source_key_or_generation(manager):
    state = submit(manager)
    assert state["status"] == "complete"
    assert state["page_states"] == {"overview": "ready", "pipeline": "ready"}
    assert FakeWiki.produced == ["overview", "pipeline"]

    reopened = VisitorWikis(manager.store)
    assert [p["id"] for p in reopened.status(ATTEMPT)["pages"]] == [
        "overview",
        "pipeline",
    ]
    assert all(p["cache_state"] == "ready" for p in reopened.status(ATTEMPT)["pages"])
    assert reopened.page(ATTEMPT, "pipeline")["citations"][0]["content"].startswith(
        "def main"
    )
    encoded = json.dumps([entry.envelope for entry in manager.store.scan()])
    assert KEY not in encoded and OWNER not in encoded
    assert "owner_hash" not in state
    assert submit(manager, "another-client")["status"] == "complete"
    assert FakeWiki.produced == ["overview", "pipeline"]


def test_graph_get_survives_restart_without_generation_or_publication(
    manager, monkeypatch
):
    submit(manager)
    before = {
        entry.entry_id: entry.envelope
        for entry in manager.store.scan()
        if not entry.entry_id.endswith(":graphs")
    }
    summary = {
        "coverage": {"available": True, "note": "fixture"},
        "system_map": {"available": False, "areas": [], "links": []},
    }
    view = {"available": True, "nodes": [{"id": "n0"}], "edges": [], "mermaid": ""}
    monkeypatch.setattr(
        visitor_graph, "wiki_graph_views", lambda *_: (summary, {"overview": view})
    )
    with manager.store.generation_guard("visitor-wiki-generation-v1"):
        manager.save_graph_views(
            manager._read(ATTEMPT), SimpleNamespace(), lambda: None
        )
    for entry_id, envelope in before.items():
        assert manager.store.read(entry_id).envelope == envelope
    app = FastAPI()
    app.include_router(router)
    app.state.visitor_wikis = VisitorWikis(manager.store)
    client = TestClient(app)
    first = client.get(f"/api/visitor-wikis/{ATTEMPT}/graphs/overview")
    assert first.status_code == 200
    assert first.json()["code_graph"] == view
    assert first.json()["commit"] == COMMIT
    assert first.headers["Cache-Control"] == "no-store"
    assert client.get(f"/api/visitor-wikis/{ATTEMPT}/graphs/missing").status_code == 404
    assert manager.status(ATTEMPT)["published"] is False
    assert FakeWiki.produced == ["overview", "pipeline"]


def test_backfill_rebuilds_only_missing_code_maps(manager, monkeypatch):
    failed = {
        "coverage": {"available": False, "reason": "index_failed"},
        "system_map": {"available": False, "areas": [], "links": []},
    }
    monkeypatch.setattr(visitor_graph, "wiki_graph_views", lambda *_: (failed, {}))
    submit(manager)
    assert manager.missing_graph_views() == [
        {"id": ATTEMPT, "repository": "owner/repo", "reason": "index_failed"}
    ]
    before = {
        entry.entry_id: entry.envelope
        for entry in manager.store.scan()
        if ":graph" not in entry.entry_id
    }
    prepared = []

    @contextmanager
    def pinned(repository, commit, *_args):
        prepared.append((repository, commit))
        yield SimpleNamespace(), None

    summary = {
        "coverage": {"available": True, "files": 3, "nodes": 9, "edges": 4},
        "system_map": {"available": True, "areas": [], "links": []},
    }
    view = {"available": True, "nodes": [{"id": "n0"}], "edges": [], "mermaid": ""}
    monkeypatch.setattr(
        visitor_graph, "wiki_graph_views", lambda *_: (summary, {"overview": view})
    )
    manager.prepare = pinned
    coverage = manager.backfill_graph_views(ATTEMPT)

    assert coverage["available"] and coverage["edges"] == 4
    assert prepared == [("owner/repo", COMMIT)]
    assert manager.graphs(ATTEMPT, "overview")["code_graph"] == view
    assert manager.missing_graph_views() == []
    for entry_id, envelope in before.items():
        assert manager.store.read(entry_id).envelope == envelope
    assert FakeWiki.produced == ["overview", "pipeline"]


def test_interrupted_run_reuses_ready_page_after_service_restart(manager):
    def stop_at_second(page):
        if page == "pipeline":
            raise WikiRunStopped("Stopped by the owner.")

    FakeWiki.before_page = stop_at_second
    first = submit(manager)
    assert first["status"] == "partial"
    assert first["page_states"]["overview"] == "ready"
    FakeWiki.before_page = None
    resumed = VisitorWikis(
        manager.store,
        prepare=prepare,
        resolve=lambda *_: pytest.fail("Must reuse commit"),
        provider=FakeProvider,
        verify=lambda _: None,
        wiki_factory=FakeWiki,
    )
    assert submit(resumed)["status"] == "complete"
    assert FakeWiki.produced == ["overview", "pipeline"]


def test_new_scope_and_model_are_pinned_across_explicit_resume(manager):
    FakeWiki.before_page = lambda _: (_ for _ in ()).throw(WikiRunStopped("pause"))
    first = manager.submit(
        ATTEMPT,
        "owner/repo",
        OWNER,
        KEY,
        2,
        "first",
        model="deepseek/deepseek-v4.1-flash",
    )
    assert first["scope"] == "focused"
    assert first["model"] == "deepseek/deepseek-v4.1-flash"
    resumed = manager.submit(ATTEMPT, "owner/repo", OWNER, KEY, 2, "second")
    assert resumed["model"] == first["model"]
    assert resumed["scope"] == first["scope"]


def test_legacy_resume_keeps_full_scope_and_original_model(manager):
    FakeWiki.before_page = lambda _: (_ for _ in ()).throw(WikiRunStopped("pause"))
    submit(manager)
    legacy = manager._read(ATTEMPT)
    legacy.pop("model")
    legacy.pop("scope")
    legacy.pop("retrieval_identity")
    manager._save(legacy)
    observed = []

    def factory(bundle, model, **options):
        observed.append((model, options["concise"]))
        return FakeWiki(bundle, model, **options)

    manager.wiki_factory = factory
    FakeWiki.before_page = None
    resumed = manager.submit(
        ATTEMPT,
        "owner/repo",
        OWNER,
        KEY,
        2,
        "resume",
        model="deepseek/deepseek-v4.1-flash",
    )
    assert resumed["status"] == "complete"
    assert observed == [("openrouter/anthropic/claude-sonnet-4.6", False)]


@pytest.mark.parametrize("legacy", [False, True])
def test_resume_pins_wiki_retrieval_and_reader_review(manager, legacy):
    FakeWiki.before_page = lambda _: (_ for _ in ()).throw(WikiRunStopped("pause"))
    submit(manager)
    state = manager._read(ATTEMPT)
    if legacy:
        state.pop("retrieval_identity")
        manager._save(state)
    observed = []

    class Provider(FakeProvider):
        def retrieve(self, source, query, limit, **options):
            observed.append(options)
            return []

    def factory(bundle, model, **options):
        observed.append((options["retrieval_identity"], options["story_review"]))
        options["source_retriever"]("chapter", 12)
        return FakeWiki(bundle, model, **options)

    manager.provider = Provider
    manager.wiki_factory = factory
    FakeWiki.before_page = None
    resumed = submit(manager, "resume")
    assert resumed["status"] == "complete"
    expected = "grep_jev_v1" if legacy else visitor_provider.WIKI_RETRIEVAL_IDENTITY
    assert observed == [(expected, not legacy), {"wiki_context": not legacy}]


@pytest.mark.parametrize("wiki_context", [False, True])
def test_wiki_search_can_retrieve_test_contracts_without_changing_legacy_route(
    tmp_path, monkeypatch, wiki_context
):
    from codenib.source_fingerprint import capture_repository_source

    (tmp_path / "tests").mkdir()
    (tmp_path / "runtime.py").write_text("def run():\n    return True\n")
    (tmp_path / "tests" / "test_runtime.py").write_text(
        "def test_reject_invalid_input():\n    assert not False\n"
    )
    sent = []

    def complete(messages, **_):
        sent.extend(messages)
        return json.dumps(
            {
                "actions": [
                    {
                        "pattern": "test_reject_invalid_input",
                        "glob": "tests/*.py",
                        "case_sensitive": True,
                    }
                ]
            }
        )

    monkeypatch.setattr(
        VisitorProvider, "complete", lambda _, *a, **k: complete(*a, **k)
    )
    monkeypatch.setattr(visitor_provider, "OpenRouterDecisions", lambda **_: None)
    monkeypatch.setattr(
        visitor_provider,
        "decide_code_relevance",
        lambda _, query, batch: SimpleNamespace(
            usage={"cost": 0.01},
            answers={f"node_{index}": SimpleNamespace(score=3) for index, _ in batch},
        ),
    )
    provider = VisitorProvider(KEY, 1, lambda: None, lambda _: None)
    with capture_repository_source(tmp_path) as source:
        nodes = provider.retrieve(
            source, "Validation and tests", 12, wiki_context=wiki_context
        )
    payload = json.loads(sent[1]["content"])
    if wiki_context:
        assert payload["chapter"] == "Validation and tests" and "issue" not in payload
        assert sent[0]["content"] == visitor_provider.WIKI_PLANNER_SYSTEM
        assert [node.file for node in nodes] == ["tests/test_runtime.py"]
        assert "test_reject_invalid_input" in nodes[0].content
        assert provider.cost == 0.01
    else:
        assert payload["issue"] == "" and payload["query"] == "Validation and tests"
        assert sent[0]["content"] == visitor_provider.PLANNER_SYSTEM
        assert nodes == [] and provider.calls == 0


def test_cancel_does_not_overwrite_saved_pages(manager):
    FakeWiki.before_page = lambda page: (
        manager.cancel(ATTEMPT, OWNER) if page == "pipeline" else None
    )
    state = submit(manager)
    assert state["status"] == "partial"
    assert state["page_states"]["overview"] == "ready"
    assert "stopped" in state["message"]
    with pytest.raises(VisitorWikiError):
        manager.cancel(ATTEMPT, "wrong-owner")
    FakeWiki.before_page = None
    assert submit(manager, "resume-client")["status"] == "complete"
    # The second page completed its model call before cancellation; the saved
    # AgentWiki entry is recovered without repeating that call.
    assert FakeWiki.produced == ["overview", "pipeline"]


def test_invalid_credentials_allocate_no_saved_attempt(manager):
    def reject(_):
        raise WikiRunStopped("Invalid inference key.")

    manager.verify = reject
    with pytest.raises(VisitorWikiError, match="Invalid"):
        submit(manager)
    assert manager.store.scan() == ()


def test_source_failure_is_persisted_before_any_model_call(manager):
    @contextmanager
    def failed_source(*args):
        args[-1]("downloading", "")
        raise WikiRunStopped("The repository archive could not be read safely.")
        yield  # pragma: no cover

    manager.prepare = failed_source
    state = submit(manager)
    assert state["status"] == "partial"
    assert state["message"] == "The repository archive could not be read safely."
    assert state["calls"] == 0 and state["reported_cost_usd"] == 0
    assert state["pages"] == []
    assert VisitorWikis(manager.store).status(ATTEMPT)["message"] == state["message"]


def test_skipped_files_remain_visible_in_saved_wiki(manager):
    skipped = [{"path": "assets/diagram.png", "size_bytes": 7513866}]

    @contextmanager
    def source(*_args):
        yield SimpleNamespace(skipped_files=skipped), None

    manager.prepare = source
    assert submit(manager)["status"] == "complete"
    assert VisitorWikis(manager.store).status(ATTEMPT)["skipped_files"] == skipped


@pytest.mark.parametrize("status", [403, 404])
def test_invalid_repository_does_not_consume_saved_wiki_capacity(manager, status):
    def reject(*_):
        raise WikiRunStopped(f"GitHub returned HTTP {status}.")

    manager.resolve = reject
    with pytest.raises(VisitorWikiError, match=f"HTTP {status}"):
        submit(manager)
    assert manager.store.scan() == ()
    assert not FakeWiki.produced


def test_stop_cancels_dispatched_stalled_resume_before_worker_starts(
    manager, monkeypatch
):
    def interrupt(page):
        if page == "pipeline":
            raise WikiRunStopped("Interrupted.")

    FakeWiki.before_page = interrupt
    submit(manager)
    # A dead process can leave its last persisted state marked running.
    state = manager._read(ATTEMPT)
    state.update(status="running", stage="writing")
    manager._save(state)
    FakeWiki.before_page = None
    queued, downloads = [], []

    @contextmanager
    def observed_prepare(*args):
        downloads.append(1)
        with prepare(*args) as source:
            yield source

    resumed = VisitorWikis(
        manager.store,
        prepare=observed_prepare,
        resolve=lambda *_: pytest.fail("Must reuse pinned commit"),
        provider=FakeProvider,
        verify=lambda _: None,
        wiki_factory=FakeWiki,
        clock=lambda: 2000,
    )
    assert resumed.status(ATTEMPT)["stalled"]
    monkeypatch.setattr(
        VisitorWikis,
        "_dispatch",
        lambda _, state, key, budget: queued.append((state, key, budget)),
    )
    assert submit(resumed)["status"] == "queued"
    resumed.cancel(ATTEMPT, OWNER)
    resumed._run(*queued.pop())
    assert not downloads
    assert FakeWiki.produced == ["overview"]
    assert "stopped" in resumed.status(ATTEMPT)["message"]

    # A later explicit resume gets a fresh identity, not the stopped one's flag.
    submit(resumed, "later-client")
    resumed._run(*queued.pop())
    assert resumed.status(ATTEMPT)["status"] == "complete"
    assert downloads == [1]
    assert FakeWiki.produced == ["overview", "pipeline"]


def test_quality_rejected_chapter_is_not_published_as_ready(manager, monkeypatch):
    original_page = FakeWiki.page

    def rejected(wiki, page, **kwargs):
        result = original_page(wiki, page, **kwargs)
        if page == "pipeline":
            result["quality"]["valid"] = False
        return result

    monkeypatch.setattr(FakeWiki, "page", rejected)
    state = submit(manager)
    assert state["status"] == "partial"
    assert state["page_states"] == {"overview": "ready", "pipeline": "needs_review"}
    assert manager.page(ATTEMPT, "overview")["markdown"]
    with pytest.raises(VisitorWikiError, match="not ready"):
        manager.page(ATTEMPT, "pipeline")


def test_stale_queued_run_cannot_repeat_generation(manager):
    submit(manager)
    current = manager._read(ATTEMPT)
    stale = {**current, "revision": current["revision"] - 1}
    manager._run(stale, KEY, 2)
    assert FakeWiki.produced == ["overview", "pipeline"]
    assert manager.status(ATTEMPT)["status"] == "complete"


@pytest.mark.parametrize("model", [None, "deepseek/deepseek-v4.1-flash"])
def test_endpoints_expose_saved_pages_but_never_owner_access(
    manager, monkeypatch, model
):
    monkeypatch.setenv("CODENIB_VISITOR_WIKI", "1")
    app = FastAPI()
    app.include_router(router)
    app.state.visitor_wikis = manager
    client = TestClient(app)
    assert client.get("/api/visitor-wikis").json()["enabled"]
    endpoint = f"/api/visitor-wikis/{ATTEMPT}"
    headers = {"Authorization": f"Bearer {KEY}", "X-Wiki-Owner": OWNER}
    assert (
        client.post(
            endpoint,
            json={
                "repository": "owner/repo",
                "budget_usd": 2.0,
                "model": "unsupported",
            },
            headers=headers,
        ).status_code
        == 422
    )
    assert not FakeWiki.produced
    response = client.post(
        endpoint,
        json={
            "repository": "owner/repo",
            "budget_usd": 2.0,
            **({"model": model} if model else {}),
        },
        headers=headers,
    )
    assert response.status_code == 202
    status = client.get(endpoint)
    assert status.json()["status"] == "complete"
    assert status.json()["model"] == (model or "anthropic/claude-sonnet-4.6")
    assert status.headers["Cache-Control"] == "no-store"
    assert status.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert (
        OWNER not in status.text
        and KEY not in status.text
        and "owner_hash" not in status.text
    )
    assert client.get(endpoint + "/pages/pipeline").status_code == 200
    assert (
        client.post(endpoint + "/stop", headers={"X-Wiki-Owner": "d" * 64}).status_code
        == 403
    )
    monkeypatch.delenv("CODENIB_VISITOR_WIKI")
    assert client.get(endpoint).status_code == 200
    assert client.get(endpoint + "/pages/pipeline").status_code == 200
    assert (
        client.post(
            endpoint, json={"repository": "owner/repo", "budget_usd": 2.0}
        ).status_code
        == 503
    )


def test_unknown_cost_or_cancellation_cannot_trigger_pipeline_fallback(monkeypatch):
    calls = []
    monkeypatch.setattr(
        visitor_provider,
        "provider_stream",
        lambda *_: calls.append(1) or {"usage": {}, "choices": []},
    )
    provider = VisitorProvider(KEY, 1, lambda: None, lambda _: None)
    with pytest.raises(WikiRunStopped, match="charge"):
        provider.complete([])
    with pytest.raises(WikiRunStopped):
        provider.complete([])
    assert len(calls) == 1
    assert not isinstance(WikiRunStopped(), Exception)


def test_reader_review_does_not_swallow_paid_run_stop():
    from codenib.wiki.agent_wiki import AgentWiki

    class Stopped:
        def complete(self, *_args, **_kwargs):
            raise WikiRunStopped("No further funded calls")

    wiki = AgentWiki(
        SimpleNamespace(entry=SimpleNamespace(repo="owner/repo", language="python")),
        model="fake-model",
        llm=Stopped(),
        story_review=True,
    )
    with pytest.raises(WikiRunStopped, match="No further funded calls"):
        wiki._review_story("Source-checked prose awaiting its reading review.")


def test_reported_budget_blocks_following_call(monkeypatch):
    calls, updates = [], []
    monkeypatch.setattr(
        visitor_provider,
        "provider_stream",
        lambda *_: calls.append(1)
        or {
            "usage": {"cost": 0.3},
            "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
        },
    )
    provider = VisitorProvider(KEY, 0.25, lambda: None, updates.append)
    assert provider.complete([]) == "ok"
    with pytest.raises(WikiRunStopped, match="budget"):
        provider.complete([])
    assert len(calls) == 1 and updates[-1]["reported_cost_usd"] == 0.3
    provider.close()
    assert not provider._key


def archive(path, text=b"hello", mode=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as handle:
        item = zipfile.ZipInfo(path)
        if mode is not None:
            item.external_attr = mode << 16
        handle.writestr(item, text)
    return output.getvalue()


@pytest.mark.parametrize(
    "path,mode",
    [
        ("repo/../../secret", None),
        ("/tmp/source.py", None),
        ("repo/.git/config", None),
        ("repo/source.py", stat.S_IFLNK | 0o777),
        ("repo/a\\b.py", None),
    ],
)
def test_archive_rejects_escape_links_and_git(path, mode, tmp_path):
    with pytest.raises(WikiRunStopped):
        extract_source(archive(path, mode=mode), tmp_path, lambda: None)
    assert not list(tmp_path.iterdir())


def test_source_skips_large_files_without_reading_or_losing_small_source(monkeypatch):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("repo/assets/diagram.png", b"x" * 7513866)
        archive.writestr("repo/large.py", b"x" * (visitor_source.MAX_FILE_BYTES + 1))
        archive.writestr("repo/main.py", b"def run():\n    return 1\n")
    monkeypatch.setattr(visitor_source, "github_bytes", lambda *_: payload.getvalue())
    original_read = zipfile.ZipFile.read

    def small_only(self, name, *args, **kwargs):
        assert name.filename == "repo/main.py", "Large members must not be inflated"
        return original_read(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "read", small_only)
    with visitor_source.visitor_source(
        "owner/repo", COMMIT, ATTEMPT, lambda: None, lambda *_: None
    ) as (bundle, source):
        assert bundle.skipped_files == [
            {"path": "assets/diagram.png", "size_bytes": 7513866},
            {"path": "large.py", "size_bytes": visitor_source.MAX_FILE_BYTES + 1},
        ]
        assert list(source.borrow_reader().file_paths) == ["main.py"]
        assert WikiBuilder(bundle)._symbols()[0].file == "main.py"


@pytest.mark.parametrize(
    "path,mode", [("repo/../bad.png", None), ("repo/image.png", stat.S_IFLNK | 0o777)]
)
def test_skipped_large_members_still_require_safe_paths(
    tmp_path, monkeypatch, path, mode
):
    monkeypatch.setattr(visitor_source, "MAX_FILE_BYTES", 1)
    with pytest.raises(WikiRunStopped, match="unsupported path"):
        extract_source(archive(path, b"large", mode), tmp_path, lambda: None)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("limit", ["bytes", "files", "duplicates"])
def test_skipping_large_members_preserves_archive_bounds(tmp_path, monkeypatch, limit):
    monkeypatch.setattr(visitor_source, "MAX_FILE_BYTES", 8)
    monkeypatch.setattr(
        visitor_source, "MAX_SOURCE_BYTES", 6 if limit == "bytes" else 100
    )
    monkeypatch.setattr(visitor_source, "MAX_FILES", 1 if limit == "files" else 10)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as zipped:
        zipped.writestr("repo/large.png", b"x" * 20)
        zipped.writestr("repo/main.py", b"x" * 7)
        if limit == "duplicates":
            zipped.writestr("repo/LARGE.png", b"x")
    with pytest.raises(WikiRunStopped):
        extract_source(payload.getvalue(), tmp_path, lambda: None)
    assert not list(tmp_path.iterdir())


def test_real_source_preparation_enumerates_symbols_without_bm25_or_embeddings(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(visitor_graph, "resolve_command", lambda _: None)
    monkeypatch.setattr(
        visitor_source,
        "github_bytes",
        lambda *_: archive("repo/main.py", b"def run():\n    return 1\n"),
    )
    stages = []
    with visitor_source.visitor_source(
        "owner/repo",
        COMMIT,
        ATTEMPT,
        lambda: None,
        lambda stage, _: stages.append(stage),
    ) as (bundle, source):
        assert bundle.vector_store is None and bundle.bm25 is None
        assert WikiBuilder(bundle)._symbols()[0].file == "main.py"
        assert source.read_bytes("main.py", max_bytes=1024).startswith(b"def run")
    assert stages == ["downloading", "analyzing"]


def test_concurrent_stale_owner_cannot_repeat_a_funded_run(manager, monkeypatch):
    submitted = []
    monkeypatch.setattr(
        VisitorWikis,
        "_dispatch",
        lambda _, state, key, budget: submitted.append((state, key, budget)),
    )
    submit(manager)
    other = VisitorWikis(
        manager.store,
        prepare=prepare,
        resolve=lambda *_: COMMIT,
        provider=FakeProvider,
        verify=lambda _: None,
        wiki_factory=FakeWiki,
    )
    entered, release, other_waiting = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    failures = []

    def pause(page):
        if page == "overview":
            entered.set()
            assert release.wait(5)

    FakeWiki.before_page = pause
    original_guard = manager.store.generation_guard

    @contextmanager
    def observed_guard(identity):
        if (
            threading.current_thread().name == "contender"
            and identity == "visitor-wiki-generation-v1"
        ):
            other_waiting.set()
        with original_guard(identity):
            yield

    monkeypatch.setattr(manager.store, "generation_guard", observed_guard)

    def run(owner, inputs):
        try:
            owner._run(*inputs)
        except BaseException as exc:  # noqa: B036 - report worker failure below
            failures.append(exc)

    first = threading.Thread(target=run, args=(manager, submitted[0]), name="first")
    first.start()
    assert entered.wait(5)
    submit(other, "contender")
    during_run = submitted[1]
    second = threading.Thread(target=run, args=(other, during_run), name="contender")
    second.start()
    assert other_waiting.wait(5)
    release.set()
    first.join(5)
    second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert not failures
    assert FakeWiki.produced == ["overview", "pipeline"]
    assert manager.status(ATTEMPT)["status"] == "complete"


def test_busy_contender_cannot_overwrite_active_owner_progress(manager, monkeypatch):
    queued = []
    monkeypatch.setattr(
        VisitorWikis,
        "_dispatch",
        lambda _, state, key, budget: queued.append((state, key, budget)),
    )
    submit(manager)
    entered, release = threading.Event(), threading.Event()

    def pause(page):
        if page == "overview":
            entered.set()
            assert release.wait(5)

    FakeWiki.before_page = pause
    first = threading.Thread(target=manager._run, args=queued[0])
    first.start()
    assert entered.wait(5)
    try:
        other = VisitorWikis(
            manager.store,
            prepare=prepare,
            resolve=lambda *_: COMMIT,
            provider=FakeProvider,
            verify=lambda _: None,
            wiki_factory=FakeWiki,
        )
        submit(other, "contender")
        before = manager._read(ATTEMPT)
        original_guard = manager.store.generation_guard

        @contextmanager
        def busy_guard(identity):
            if identity == "visitor-wiki-generation-v1":
                raise WikiGenerationBusyError("Existing owner is still active")
            with original_guard(identity):
                yield

        monkeypatch.setattr(manager.store, "generation_guard", busy_guard)
        other._run(*queued[1])
        assert manager._read(ATTEMPT) == before
        assert manager.status(ATTEMPT)["status"] == "running"
    finally:
        release.set()
        first.join(5)
    assert not first.is_alive()
    assert manager.status(ATTEMPT)["status"] == "complete"
    assert FakeWiki.produced == ["overview", "pipeline"]


def test_real_agent_wiki_cache_survives_fresh_source_download(tmp_path, monkeypatch):
    from codenib.wiki.agent_wiki import AgentWiki

    payload = archive("repo/main.py", b"def run():\n    return 1\n")
    monkeypatch.setattr(visitor_source, "github_bytes", lambda *_: payload)
    store = SQLiteWikiStore(tmp_path / "saved.sqlite3")
    outline = {"pages": [{"id": "overview", "title": "Overview", "children": []}]}
    page = {
        "id": "overview",
        "title": "Overview",
        "markdown": "# Overview\nSource-grounded text.",
        "generation": {"mode": "generated"},
        "quality": {"valid": True},
        "grounding": {"valid": True},
    }
    identities = []
    for iteration in range(2):
        with visitor_source.visitor_source(
            "owner/repo", COMMIT, ATTEMPT, lambda: None, lambda *_: None
        ) as (bundle, _):
            wiki = AgentWiki(
                bundle,
                "unused",
                store=store,
                source_retriever=lambda *_: pytest.fail(
                    "Cached pages must not retrieve"
                ),
                retrieval_identity="grep_jev_v1",
            )
            identities.append(wiki._cache_identity("outline"))
            concise = AgentWiki(
                bundle,
                "unused",
                store=store,
                concise=True,
                source_retriever=lambda *_: [],
                retrieval_identity="grep_jev_v1",
            )
            assert concise._cache_identity("outline") != wiki._cache_identity("outline")
            focused = AgentWiki(
                bundle,
                "unused",
                store=store,
                focused=True,
                source_retriever=lambda *_: [],
                retrieval_identity="grep_jev_v1",
            )
            assert (
                len(
                    {
                        focused._cache_identity("outline"),
                        concise._cache_identity("outline"),
                        wiki._cache_identity("outline"),
                    }
                )
                == 3
            )
            assert concise.cached_outline() is None
            if iteration == 0:
                wiki._write_cache("outline", outline)
                meta = wiki._overview_page_meta(outline["pages"][0], [])
                wiki._write_cache(wiki._page_cache_suffix(meta), page)
            else:
                assert wiki.cached_page("overview") == page
    assert identities[0] == identities[1]


def test_publication_is_explicit_owner_only_and_survives_restart(manager):
    submit(manager)
    assert manager.public_wikis() == []
    assert manager.status(ATTEMPT)["published"] is False
    with pytest.raises(VisitorWikiError):
        manager.publish_wiki(ATTEMPT, "d" * 64, True)
    assert manager.public_wikis() == []
    before = manager._read(ATTEMPT)
    assert manager.publish_wiki(ATTEMPT, OWNER, True) == {"published": True}
    reopened = VisitorWikis(manager.store)
    cards = reopened.public_wikis()
    assert len(cards) == 1
    assert cards[0]["repository"] == "owner/repo"
    assert cards[0]["chapters"] == 2
    assert set(cards[0]) == {
        "id",
        "published",
        "repository",
        "commit",
        "summary",
        "chapters",
        "languages",
        "published_at",
    }
    assert KEY not in json.dumps(cards) and OWNER not in json.dumps(cards)
    assert reopened.status(ATTEMPT)["published"] is True
    reopened.publish_wiki(ATTEMPT, OWNER, False)
    # A late progress-envelope write cannot resurrect public visibility.
    manager._save(before)
    assert manager.public_wikis() == []
    assert manager.status(ATTEMPT)["published"] is False
    assert manager.page(ATTEMPT, "overview")["markdown"]
    assert FakeWiki.produced == ["overview", "pipeline"]


def test_publication_rejects_partial_and_quality_rejected_pages(manager):
    FakeWiki.before_page = lambda _: (_ for _ in ()).throw(WikiRunStopped("pause"))
    submit(manager)
    with pytest.raises(VisitorWikiError, match="Finish"):
        manager.publish_wiki(ATTEMPT, OWNER, True)
    FakeWiki.before_page = None
    submit(manager, "resume")
    page = manager.page(ATTEMPT, "pipeline")
    page["quality"]["valid"] = False
    manager.store.publish(
        entry_id=f"visitor:{ATTEMPT}:page:pipeline",
        repository_id=ATTEMPT,
        envelope={"data": page},
    )
    with pytest.raises(VisitorWikiError, match="source checks"):
        manager.publish_wiki(ATTEMPT, OWNER, True)
    assert manager.public_wikis() == []


def test_public_catalog_endpoint_needs_no_key_and_never_lists_unpublished(manager):
    app = FastAPI()
    app.include_router(router)
    app.state.visitor_wikis = manager
    client = TestClient(app)
    submit(manager)
    endpoint = f"/api/visitor-wikis/{ATTEMPT}/publication"
    assert client.get("/api/visitor-wikis/public").json() == []
    assert client.post(endpoint, json={"published": True}).status_code == 400
    assert (
        client.post(
            endpoint, json={"published": True}, headers={"X-Wiki-Owner": "d" * 64}
        ).status_code
        == 409
    )
    assert (
        client.post(
            endpoint, json={"published": "true"}, headers={"X-Wiki-Owner": OWNER}
        ).status_code
        == 422
    )
    assert (
        client.post(
            endpoint, json={"published": True}, headers={"X-Wiki-Owner": OWNER}
        ).status_code
        == 200
    )
    result = client.get("/api/visitor-wikis/public")
    assert result.status_code == 200 and result.json()[0]["id"] == ATTEMPT
    assert result.headers["cache-control"] == "no-store"
    assert (
        client.post(
            endpoint, json={"published": False}, headers={"X-Wiki-Owner": OWNER}
        ).status_code
        == 200
    )
    assert client.get("/api/visitor-wikis/public").json() == []
    assert client.get(f"/api/visitor-wikis/{ATTEMPT}/pages/overview").status_code == 200
