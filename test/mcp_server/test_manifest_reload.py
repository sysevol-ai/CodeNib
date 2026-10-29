# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Recover from a published local rebuild without replacing the MCP connection."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from filelock import FileLock
from mcp import Client

from codenib.compiler.artifact_fingerprints import bm25_artifact_file_fingerprints
from codenib.compiler.manifest import IndexEntry, RepoManifest
from codenib.mcp import server
from codenib.mcp.context import ServerContext
from codenib.repository_source_selection import RepositorySourceSelection
from codenib.source_fingerprint import fingerprint_repository


def _publish(repo: Path, cache: Path, file: str, symbol: str) -> RepoManifest:
    """Write a real authenticated BM25 view for this exact source generation."""

    generation = len(tuple(cache.glob("bm25-*")))
    view = cache / f"bm25-{generation}"
    view.mkdir(parents=True)
    content = (repo / file).read_text()
    (view / "documents.json").write_text(
        json.dumps(
            [
                {
                    "page_content": content,
                    "metadata": {
                        "file": file,
                        "name": symbol,
                        "node_id": f"{file}:{symbol}()",
                        "type": "function",
                        "start_line": 0,
                        "end_line": len(content.splitlines()) - 1,
                    },
                }
            ]
        )
    )
    (view / "bm25_metadata.json").write_text(
        json.dumps({"project_root": str(repo), "max_k": 10, "language": "english"})
    )
    identity = fingerprint_repository(repo)
    selection = RepositorySourceSelection()
    commit = "c" * 40
    manifest = RepoManifest(
        repo_path=str(repo),
        commit=commit,
        last_indexed_commit=commit,
        source_fingerprint=identity.value,
        last_indexed_source_fingerprint=identity.value,
        source_selection=selection,
        last_indexed_source_selection_digest=selection.digest,
        file_count=identity.file_count,
        languages=["python"],
        indexes={
            "bm25": IndexEntry(
                index_type="bm25",
                path=str(view),
                built_at=f"generation-{generation}",
                built_at_epoch=float(generation),
                status="fresh",
                commit=commit,
                source_fingerprint=identity.value,
                source_selection_digest=selection.digest,
                config={
                    "artifact_file_fingerprints": bm25_artifact_file_fingerprints(view)
                },
            )
        },
    )
    manifest.derive_capabilities()
    manifest.save(cache / "repo_manifest.json")
    return manifest


@pytest.fixture
def local_server(tmp_path, monkeypatch):
    repo = tmp_path / "repository"
    repo.mkdir()
    (repo / "sample.py").write_text("def original():\n    return 1\n")
    (repo / "remaining.py").write_text("def remaining():\n    return 0\n")
    cache = tmp_path / "indexes"
    _publish(repo, cache, "sample.py", "original")
    monkeypatch.setattr(server, "_ctx", None)
    server.init_server(cache / "repo_manifest.json")
    initial = server.get_context()
    try:
        yield repo, cache, initial
    finally:
        server.get_context().close()


@pytest.mark.parametrize("change", ("edit", "create", "delete", "rename"))
def test_rebuilt_views_restore_queries_in_the_same_connection(local_server, change):
    repo, cache, initial = local_server
    original_source = initial._source_binding

    async def session():
        async with server._mcp_lifespan(None) as runtime:
            first = await server.explore_context(
                query="original", include_dependencies=False
            )
            assert first["summary"]["session_call"] == 1
            file, symbol = "sample.py", "updated"
            if change == "edit":
                (repo / file).write_text("def updated():\n    return 2\n")
            elif change == "create":
                file = "created.py"
                (repo / file).write_text("def updated():\n    return 2\n")
            elif change == "rename":
                file, symbol = "renamed.py", "original"
                (repo / "sample.py").rename(repo / file)
            else:
                (repo / "sample.py").unlink()
                file, symbol = "remaining.py", "remaining"

            for _ in range(2):
                with pytest.raises(RuntimeError, match="init.*retry") as caught:
                    await server.search_bm25(query="original")
                assert "restart" not in str(caught.value)

            published = _publish(repo, cache, file, symbol)
            result = await server.search_bm25(query=symbol)
            assert len(result) == 1
            assert result[0]["node_id"] == f"{file}:{symbol}()"
            assert result[0]["content"] == (repo / file).read_text()
            source = await server.read_source(file_path=file)
            assert source["content"] == (repo / file).read_text()
            current = server.get_context()
            assert current is not initial
            assert current.manifest.source_fingerprint == published.source_fingerprint
            assert original_source.closed
            assert current.explore_runtime is runtime
            second = await server.explore_context(
                query=symbol, include_dependencies=False
            )
            assert second["summary"]["session_call"] == 2
            assert second["source"]["verified"] is True
        assert server.get_context().explore_runtime is None

    asyncio.run(session())


@pytest.mark.parametrize("mode", ("auto", "legacy"))
@pytest.mark.parametrize(
    "tool,arguments",
    (
        ("explore_context", {"query": "original"}),
        ("search_context", {"query": "original"}),
        ("search_semantic", {"query": "original"}),
        ("search_bm25", {"query": "original"}),
        ("search_regex", {"pattern": "original"}),
        ("search_zoekt", {"query": "original"}),
        ("dependency_subgraph", {"symbol": "original"}),
        ("lsp_definition", {"symbol": "original"}),
        ("lsp_references", {"symbol": "original"}),
        ("lsp_route", {"symbols": ["original"]}),
        ("read_source", {"file_path": "sample.py"}),
    ),
)
def test_client_receives_stale_recovery_and_can_retry(
    local_server, mode, tool, arguments
):
    """The agent must receive recovery instructions through actual tools/call."""
    repo, cache, _initial = local_server

    async def session():
        async with Client(server.mcp, mode=mode, cache=None) as client:
            (repo / "sample.py").write_text("def updated():\n    return 2\n")
            rejected = await client.call_tool(tool, arguments)
            assert rejected.is_error is True
            text = "\n".join(part.text for part in rejected.content)
            assert "Indexed source changed" in text
            assert "codenib codegraph init" in text
            assert "retry" in text and "this MCP connection reloads" in text
            assert "restart" not in text
            assert str(repo) not in text

            _publish(repo, cache, "sample.py", "updated")
            retried = await client.call_tool("search_bm25", {"query": "updated"})
            assert retried.is_error is False
            assert "def updated()" in "\n".join(part.text for part in retried.content)

    asyncio.run(session())


@pytest.mark.parametrize("mode", ("auto", "legacy"))
def test_client_discards_mid_query_facts_and_receives_recovery(
    local_server, monkeypatch, mode
):
    repo, _cache, _initial = local_server

    def edit_while_querying(*_args):
        (repo / "sample.py").write_text("def updated():\n    return 2\n")
        return [{"content": "obsolete provider result"}]

    monkeypatch.setattr(server, "search_bm25_impl", edit_while_querying)

    async def session():
        async with Client(server.mcp, mode=mode, cache=None) as client:
            response = await client.call_tool("search_bm25", {"query": "original"})
            assert response.is_error is True
            text = "\n".join(part.text for part in response.content)
            assert "Indexed source changed" in text
            assert "codenib codegraph init" in text
            assert "obsolete provider result" not in text

    asyncio.run(session())


@pytest.mark.parametrize("mode", ("auto", "legacy"))
def test_client_receives_busy_rebuild_retry(local_server, mode):
    repo, cache, initial = local_server
    _publish(repo, cache, "sample.py", "original")

    async def session():
        async with Client(server.mcp, mode=mode, cache=None) as client:
            with FileLock(str(cache / ".index-compiler.lock")):
                rejected = await client.call_tool("search_bm25", {"query": "original"})
                assert rejected.is_error is True
                text = "\n".join(part.text for part in rejected.content)
                assert "being rebuilt; retry the query" in text
                assert server.get_context() is initial
            retried = await client.call_tool("search_bm25", {"query": "original"})
            assert retried.is_error is False

    asyncio.run(session())


def test_status_can_reload_before_the_next_indexed_query(local_server):
    repo, cache, initial = local_server
    (repo / "sample.py").write_text("def updated():\n    return 2\n")
    published = _publish(repo, cache, "sample.py", "updated")

    result = asyncio.run(server.get_manifest())

    assert server.get_context() is not initial
    assert result["repo"]["source_fingerprint"] == published.source_fingerprint
    assert result["runtime"]["source_read"]["verified"] is True


def test_queued_call_follows_replacement_after_active_query_finishes(local_server):
    repo, cache, initial = local_server
    source = initial._source_binding
    entered, release, queued = Event(), Event(), Event()

    def active(ctx):
        entered.set()
        assert release.wait(5)
        assert not source.closed
        return ctx

    def waiting():
        acquired = server._context_lock.acquire(blocking=False)
        if acquired:
            server._context_lock.release()
        assert not acquired, "the active provider must own context publication"
        queued.set()
        return server._indexed_tool_call(lambda ctx: ctx, initial)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(server._indexed_tool_call, active, initial)
        try:
            assert entered.wait(5)
            # Rebuild unchanged source: the active result remains valid, while
            # the next call must use the newly published artifact generation.
            _publish(repo, cache, "sample.py", "original")
            second = pool.submit(waiting)
            assert queued.wait(5)
            status = asyncio.run(server.get_manifest())
            assert status["runtime"]["source_read"]["verified"] is True
            assert server.get_context() is initial
            assert not source.closed
        finally:
            release.set()
        assert first.result(timeout=5) is initial
        assert second.result(timeout=5) is server.get_context()
    assert server.get_context() is not initial
    assert source.closed


def test_reloading_never_waits_for_the_compiler_lock(local_server):
    repo, cache, initial = local_server
    with FileLock(str(cache / ".index-compiler.lock")):
        _publish(repo, cache, "sample.py", "original")
        with pytest.raises(RuntimeError, match="being rebuilt.*retry"):
            asyncio.run(server.search_bm25(query="original"))
        assert server.get_context() is initial
        assert initial.source_verified

    assert asyncio.run(server.search_bm25(query="original"))
    assert server.get_context() is not initial


def test_explore_session_lookup_during_reload_keeps_event_loop_responsive(
    local_server, monkeypatch
):
    repo, cache, initial = local_server
    source = initial._source_binding
    closing, waiting, release = Event(), Event(), Event()
    original_close = type(source).close
    original_session = ServerContext.ensure_explore_session

    def close(self):
        if self is source:
            closing.set()
            assert release.wait(5), "the event loop must release reload cleanup"
        return original_close(self)

    def session(self, **kwargs):
        if self is initial and closing.is_set():
            waiting.set()
        return original_session(self, **kwargs)

    monkeypatch.setattr(type(source), "close", close)
    monkeypatch.setattr(ServerContext, "ensure_explore_session", session)

    async def calls():
        async with server._mcp_lifespan(None) as runtime:
            first = await server.explore_context(
                query="original", include_dependencies=False
            )
            assert first["summary"]["session_call"] == 1
            _publish(repo, cache, "sample.py", "original")
            rebuilding = asyncio.create_task(server.search_bm25(query="original"))
            exploring = None
            try:
                assert await asyncio.to_thread(closing.wait, 5)
                exploring = asyncio.create_task(
                    server.explore_context(query="original", include_dependencies=False)
                )
                assert await asyncio.to_thread(waiting.wait, 5)
            finally:
                release.set()
                await rebuilding
            result = await exploring
            assert result["summary"]["session_call"] == 2
            assert server.get_context().explore_runtime is runtime

    asyncio.run(calls())


def test_mismatched_rebuild_releases_new_source_and_remains_retryable(
    local_server, monkeypatch
):
    import codenib.source_fingerprint as source_module

    repo, cache, initial = local_server
    original_capture = source_module.capture_repository_source
    captured = []

    def capture(*args, **kwargs):
        source = original_capture(*args, **kwargs)
        captured.append(source)
        return source

    monkeypatch.setattr(source_module, "capture_repository_source", capture)
    (repo / "sample.py").write_text("def updated():\n    return 2\n")
    _publish(repo, cache, "sample.py", "updated")
    (repo / "sample.py").write_text("def latest():\n    return 3\n")

    with pytest.raises(RuntimeError, match="do not match current source"):
        asyncio.run(server.search_bm25(query="updated"))
    assert server.get_context() is initial
    assert len(captured) == 1 and captured[0].closed

    _publish(repo, cache, "sample.py", "latest")
    result = asyncio.run(server.search_bm25(query="latest"))
    assert result[0]["node_id"] == "sample.py:latest()"
    assert server.get_context() is not initial


def test_rebuild_cannot_redirect_an_existing_connection(local_server, tmp_path):
    _repo, cache, initial = local_server
    other = tmp_path / "other-repository"
    other.mkdir()
    (other / "sample.py").write_text("def unrelated():\n    return 9\n")
    _publish(other, cache, "sample.py", "unrelated")

    with pytest.raises(RuntimeError, match="different repository"):
        asyncio.run(server.search_bm25(query="unrelated"))
    assert server.get_context() is initial
    assert initial.source_verified

    initial.manifest.save(cache / "repo_manifest.json")
    assert asyncio.run(server.search_bm25(query="original"))


def test_old_cleanup_failure_keeps_its_owner_and_closes_unpublished_source(
    local_server, monkeypatch
):
    import codenib.source_fingerprint as source_module

    repo, cache, initial = local_server
    source = initial._source_binding
    original_close = type(source).close
    original_capture = source_module.capture_repository_source
    captured = []
    failed = False

    def close(self):
        nonlocal failed
        if self is source and not failed:
            failed = True
            raise RuntimeError("source cleanup boundary failed")
        return original_close(self)

    def capture(*args, **kwargs):
        binding = original_capture(*args, **kwargs)
        captured.append(binding)
        return binding

    monkeypatch.setattr(type(source), "close", close)
    monkeypatch.setattr(source_module, "capture_repository_source", capture)
    _publish(repo, cache, "sample.py", "original")
    with pytest.raises(RuntimeError, match="cleanup boundary failed") as caught:
        asyncio.run(server.search_bm25(query="original"))
    assert caught.value.context_cleanup_owner is initial
    assert server.get_context() is initial
    assert not source.closed
    assert len(captured) == 1 and captured[0].closed

    assert asyncio.run(server.search_bm25(query="original"))
    assert source.closed
    assert server.get_context() is not initial
