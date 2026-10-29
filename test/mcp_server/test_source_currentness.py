# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Reject #876's stale local facts at MCP delivery boundaries."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

import codenib.mcp.server as server
from codenib.compiler.manifest import RepoManifest
from codenib.mcp.context import ServerContext
from codenib.source_fingerprint import capture_repository_source


@pytest.fixture
def local_context(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "sample.py").write_text("def original():\n    return 1\n")
    source = capture_repository_source(repo)
    ctx = ServerContext(manifest=RepoManifest(repo_path=str(repo)))
    ctx._install_repository_source(source)
    monkeypatch.setattr(server, "_ctx", ctx)
    try:
        yield ctx, repo
    finally:
        ctx.close()


@pytest.mark.parametrize(
    "tool,implementation,arguments",
    (
        ("explore_context", "explore_context_impl", {"query": "original"}),
        ("search_context", "search_context_impl", {"query": "original"}),
        ("semantic_search", "_search_semantic_impl", {"query": "original"}),
        ("search_bm25", "search_bm25_impl", {"query": "original"}),
        ("search_regex", "search_regex_impl", {"pattern": "original"}),
        ("search_zoekt", "search_zoekt_impl", {"query": "original"}),
        ("dependency_subgraph", "dependency_subgraph_impl", {"symbol": "original"}),
        ("lsp_definition", "lsp_definition_impl", {"symbol": "original"}),
        ("lsp_references", "lsp_references_impl", {"symbol": "original"}),
        ("lsp_route", "lsp_route_impl", {"symbols": ["original"]}),
        ("read_source", "read_source_impl", {"file_path": "sample.py"}),
    ),
)
@pytest.mark.parametrize("change", ("edit", "create", "delete", "rename"))
def test_edited_source_never_reaches_an_indexed_provider(
    local_context, monkeypatch, tool, implementation, arguments, change
):
    _ctx, repo = local_context
    observed = []

    def provider(*args):
        observed.append(args)
        return {"files": [], "summary": {}} if tool == "explore_context" else []

    monkeypatch.setattr(server, implementation, provider)

    async def session():
        await getattr(server, tool)(**arguments)
        assert len(observed) == 1
        path = repo / "sample.py"
        if change == "edit":
            path.write_text("def renamed():\n    return 2\n")
        elif change == "create":
            (repo / "created.py").write_text("def created():\n    return 2\n")
        elif change == "delete":
            path.unlink()
        else:
            path.rename(repo / "renamed.py")
        # The second attempt must remain actionable after the first poisons
        # the old source binding. Rebuilding alone cannot update this context.
        for _attempt in range(2):
            with pytest.raises(RuntimeError, match="init.*restart"):
                await getattr(server, tool)(**arguments)
        assert len(observed) == 1

    asyncio.run(session())


def test_mid_query_edit_discards_completed_provider_result(local_context):
    ctx, repo = local_context

    def query(_ctx):
        (repo / "sample.py").write_text("def renamed():\n    return 2\n")
        return {"nodes": [{"name": "original"}]}

    with pytest.raises(RuntimeError, match="init.*restart"):
        server._indexed_tool_call(query, ctx)
    assert not ctx.source_verified


@pytest.mark.parametrize("failure", (ValueError("bad query"), asyncio.CancelledError()))
def test_provider_failure_leaves_unchanged_source_usable(local_context, failure):
    ctx, _repo = local_context

    def query(_ctx):
        raise failure

    with pytest.raises(type(failure)) as caught:
        server._indexed_tool_call(query, ctx)
    assert caught.value is failure
    assert ctx.source_verified
    assert server._indexed_tool_call(lambda _ctx: "still usable", ctx) == "still usable"


def test_portable_snapshot_query_does_not_follow_working_tree_edits(local_context):
    ctx, repo = local_context
    ctx.artifact = {"id": "admitted-snapshot"}
    (repo / "sample.py").write_text("def renamed():\n    return 2\n")

    assert (
        server._indexed_tool_call(lambda _ctx: "snapshot result", ctx)
        == "snapshot result"
    )


def test_context_close_waits_for_an_active_query(local_context, monkeypatch):
    ctx, _repo = local_context
    entered = Event()
    release = Event()
    closing = Event()
    closed_source = Event()
    source = ctx._source_binding
    source_type = type(source)
    original_close = source_type.close

    def close_source(self):
        if self is source:
            closed_source.set()
        return original_close(self)

    monkeypatch.setattr(source_type, "close", close_source)

    def query(_ctx):
        entered.set()
        assert release.wait(5)
        assert not closed_source.is_set()
        return "accepted"

    def close():
        available = ctx._query_lock.acquire(blocking=False)
        if available:
            ctx._query_lock.release()
        closing.set()
        assert not available, "the active query must retain query ownership"
        ctx.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(server._indexed_tool_call, query, ctx)
        try:
            assert entered.wait(5)
            cleanup = pool.submit(close)
            assert closing.wait(5)
            assert not closed_source.is_set()
        finally:
            release.set()
        assert pending.result(timeout=5) == "accepted"
        cleanup.result(timeout=5)
    assert closed_source.is_set()
