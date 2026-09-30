# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Hosted source admission uses generation's limits without funded work."""

from __future__ import annotations

import io
import threading
import zipfile
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from codenib.storage import SQLiteWikiStore
from codenib.web.visitor_wikis import router
from codenib.wiki import visitor_graph, visitor_source
from codenib.wiki.visitor_provider import WikiRunStopped
from codenib.wiki.visitor_wiki import VisitorWikiError, VisitorWikis

COMMIT = "c" * 40


def source_archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(f"repo/{path}", content)
    return output.getvalue()


@pytest.fixture
def source(monkeypatch):
    files = {"src/pkg/main.py": "def run():\n    return 1\n"}
    monkeypatch.setattr(visitor_source, "repository_revision", lambda *_: COMMIT)
    monkeypatch.setattr(
        visitor_source, "github_bytes", lambda *_: source_archive(files)
    )
    monkeypatch.setattr(
        visitor_graph,
        "build_visitor_graph",
        lambda *_: pytest.fail("Check must not index"),
    )
    return files


def test_check_uses_real_source_and_reports_skips_without_leaving_a_checkout(
    source, monkeypatch
):
    roots = []
    original_capture = visitor_source.capture_repository_source

    @contextmanager
    def capture(root, **kwargs):
        roots.append(root)
        with original_capture(root, **kwargs) as binding:
            yield binding

    monkeypatch.setattr(visitor_source, "capture_repository_source", capture)
    source["large.py"] = b"x" * (visitor_source.MAX_FILE_BYTES + 1)
    result = visitor_source.check_repository("owner/repo", lambda: None)
    assert result["eligible"] is True
    assert result["commit"] == COMMIT
    assert result["source_files"] == 1
    assert result["languages"] == ["python"]
    assert result["skipped_files"] == [
        {"path": "large.py", "size_bytes": visitor_source.MAX_FILE_BYTES + 1}
    ]
    assert result["limits"] == visitor_source.generation_limits()
    assert roots and all(not root.exists() for root in roots)


@pytest.mark.parametrize(
    "limit,value,message",
    [
        ("MAX_FILES", 0, "file hosted limit"),
        ("MAX_SOURCE_BYTES", 1, "Retained files"),
        ("MAX_CHUNKS", 0, "code-section hosted limit"),
    ],
)
def test_source_check_rejects_the_same_analysis_limits_as_generation(
    source, monkeypatch, limit, value, message
):
    monkeypatch.setattr(visitor_source, limit, value)
    result = visitor_source.check_repository("owner/repo", lambda: None)
    assert result["eligible"] is False
    assert message in result["message"] or "archive has" in result["message"]
    with pytest.raises(WikiRunStopped):
        with visitor_source.visitor_source(
            "owner/repo", COMMIT, "attempt", lambda: None, lambda *_: None
        ):
            pytest.fail("Generation must reject the same source")


def test_check_rejects_unsupported_source_without_creating_a_wiki(source):
    source.clear()
    source["README.md"] = "No code here"
    result = visitor_source.check_repository("owner/repo", lambda: None)
    assert result["eligible"] is False
    assert "No supported source language" in result["message"]


def test_public_check_and_rules_need_no_key_and_allocate_no_attempt(
    source, tmp_path, monkeypatch
):
    monkeypatch.setenv("CODENIB_VISITOR_WIKI", "1")
    store = SQLiteWikiStore(tmp_path / "wiki.sqlite3")
    app = FastAPI()
    app.include_router(router)
    app.state.visitor_wikis = VisitorWikis(store)
    client = TestClient(app)
    rules = client.get("/api/visitor-wikis").json()["limits"]
    assert rules == visitor_source.generation_limits()
    result = client.get("/api/visitor-wikis/check", params={"repository": "owner/repo"})
    assert result.status_code == 200
    assert result.json()["eligible"] is True
    assert result.json()["limits"] == rules
    assert result.headers["Cache-Control"] == "no-store"
    assert not store.scan()
    invalid = client.get("/api/visitor-wikis/check", params={"repository": "../repo"})
    assert invalid.status_code == 422
    assert not store.scan()


def test_two_source_slots_are_owned_until_checks_finish(tmp_path):
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()
    inspected, failures = [], []

    def inspect(repository, check):
        check()
        inspected.append(repository)
        entered[int(repository[-1])].set()
        assert release.wait(5)
        check()
        return {"eligible": True}

    manager = VisitorWikis(
        SQLiteWikiStore(tmp_path / "wiki.sqlite3"), inspect_repository=inspect
    )

    def run(index):
        try:
            manager.check_repository(f"owner/repo{index}", f"client{index}")
        except Exception as exc:
            failures.append(exc)

    threads = [threading.Thread(target=run, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    try:
        assert all(event.wait(5) for event in entered)
        with pytest.raises(VisitorWikiError, match="busy"):
            manager.check_repository("owner/third", "third")
        with pytest.raises(VisitorWikiError, match="busy"):
            manager.submit("a" * 64, "owner/repo", "b" * 64, "key", 2, "generation")
        assert len(inspected) == 2
        assert not manager.store.scan()
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
    assert not failures
    assert not any(thread.is_alive() for thread in threads)
    assert manager._active == set()


def test_failed_check_releases_its_slot_and_remains_rate_limited(tmp_path):
    def failed(*_):
        raise RuntimeError("source check failed")

    manager = VisitorWikis(
        SQLiteWikiStore(tmp_path / "wiki.sqlite3"), inspect_repository=failed
    )
    with pytest.raises(RuntimeError):
        manager.check_repository("owner/repo", "client")
    assert not manager._active
    with pytest.raises(VisitorWikiError, match="one minute"):
        manager.check_repository("owner/repo", "client")
    assert not manager.store.scan()


def test_check_permits_an_immediate_funded_submission(tmp_path, monkeypatch):
    verified, dispatched = [], []
    manager = VisitorWikis(
        SQLiteWikiStore(tmp_path / "wiki.sqlite3"),
        inspect_repository=lambda *_: {"eligible": True},
        verify=verified.append,
        resolve=lambda *_: COMMIT,
    )
    monkeypatch.setattr(manager, "_dispatch", lambda *_: dispatched.append(1))
    manager.check_repository("owner/repo", "client")
    assert not manager.store.scan()
    state = manager.submit("a" * 64, "owner/repo", "b" * 64, "key", 2, "client")
    assert state["status"] == "queued"
    assert verified == ["key"] and dispatched == [1]


def test_generation_rechecks_source_before_any_model_call(
    source, tmp_path, monkeypatch
):
    monkeypatch.setattr(visitor_source, "MAX_SOURCE_BYTES", 1)

    class Provider:
        def __init__(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

    manager = VisitorWikis(
        SQLiteWikiStore(tmp_path / "wiki.sqlite3"),
        verify=lambda _: None,
        resolve=lambda *_: COMMIT,
        provider=Provider,
        wiki_factory=lambda *_args, **_kwargs: pytest.fail(
            "Invalid source must not reach a model"
        ),
    )
    monkeypatch.setattr(manager, "_dispatch", manager._run)
    # Even a client bypassing the browser check cannot spend on rejected source.
    state = manager.submit("a" * 64, "owner/repo", "b" * 64, "key", 2, "client")
    assert state["status"] == "partial"
    assert "Retained files exceed" in state["message"]
    assert state["calls"] == 0 and state["reported_cost_usd"] == 0


def test_archive_content_length_rejects_before_downloading(monkeypatch):
    import requests

    class Response:
        status_code = 200
        headers = {"Content-Length": str(visitor_source.MAX_ARCHIVE_BYTES + 1)}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def iter_content(self, *_):
            pytest.fail("Oversized archive must not be downloaded")

    class Session(Response):
        def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(requests, "Session", Session)
    with pytest.raises(WikiRunStopped, match="20 MiB hosted archive limit"):
        visitor_source.github_bytes(
            "https://codeload.github.com/owner/repo/zip/sha",
            visitor_source.MAX_ARCHIVE_BYTES,
            lambda: None,
        )
