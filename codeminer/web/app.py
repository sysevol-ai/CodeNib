# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""FastAPI service for the DeepWiki-style demo.

Endpoints:
- ``GET  /api/health``  — liveness + loaded repo count.
- ``GET  /api/repos``   — list indexed repos.
- ``POST /api/chat``    — ask a question about a repo, get answer + citations.

Run with::

    codeminer-web                       # uses demo_repos.yaml
    uvicorn codeminer.web.app:app       # for autoreload during dev
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from ..log_utils import get_logger
from ..wiki import WikiBuilder
from ..wiki.narrator import Narrator
from .codemap import build_codemap, build_page_subgraph
from .config import load_config
from .repo_registry import RepoRegistry
from .schemas import (
    ChatRequest,
    ChatResponse,
    EdgeLabelRequest,
    EdgeLabelResponse,
    RepoInfo,
    agent_result_to_response,
)

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    registry = RepoRegistry(config)
    logger.info("Loading QA repos from %s ...", config.registry_path)
    registry.load_all()
    app.state.registry = registry
    app.state.wiki_builders = {}
    app.state.edge_labelers = {}
    app.state.commit_windows = {}
    # Shared LLM narrator for DeepWiki-style prose; cached on disk, fails soft
    # to templated text when no model/creds are available.
    wiki_cache = os.path.join(os.path.abspath(config.data_dir), "wiki_cache")
    app.state.narrator = Narrator(
        model=config.wiki_generation_model, cache_dir=wiki_cache
    )
    logger.info(
        "Wiki narrator: model=%s enabled=%s cache=%s",
        app.state.narrator.model,
        app.state.narrator.enabled,
        wiki_cache,
    )
    logger.info("Ready: %d repo(s) available", len(registry.list_infos()))
    yield


app = FastAPI(title="CodeMiner Code QA", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=load_config().cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _registry() -> RepoRegistry:
    registry = getattr(app.state, "registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="Server still starting up")
    return registry


def _bundle(repo_id: str):
    bundle = _registry().get(repo_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail=f"Unknown repo: {repo_id!r}")
    return bundle


def _wiki(repo_id: str):
    """Lazily build + cache a wiki per repo (conceptual agent wiki by default)."""
    cache = app.state.wiki_builders
    if repo_id not in cache:
        config = load_config()
        if getattr(config, "wiki_agent", True):
            from ..wiki.agent_wiki import AgentWiki

            wiki_cache = os.path.join(os.path.abspath(config.data_dir), "wiki_cache")
            cache[repo_id] = AgentWiki(
                _bundle(repo_id),
                config.wiki_generation_model,
                cache_dir=wiki_cache,
            )
        else:
            cache[repo_id] = WikiBuilder(
                _bundle(repo_id), narrator=getattr(app.state, "narrator", None)
            )
    return cache[repo_id]


def _edge_labeler(repo_id: str):
    """Lazily build + cache a per-repo edge labeler (reuses the wiki's source reader)."""
    cache = app.state.edge_labelers
    if repo_id not in cache:
        from .edge_label import EdgeLabeler

        config = load_config()
        bundle = _bundle(repo_id)
        wiki_cache = os.path.join(os.path.abspath(config.data_dir), "wiki_cache")
        namespace = f"{bundle.entry.instance_id}@{bundle.entry.commit_short}"
        cache[repo_id] = EdgeLabeler(
            source_fn=_wiki(repo_id).source,
            model=config.edge_label_model or config.wiki_generation_model,
            cache_dir=wiki_cache,
            cache_namespace=namespace,
        )
    return cache[repo_id]


def _commit_window(repo_id: str):
    """Lazily build + cache this repo's per-commit graph window."""
    cache = app.state.commit_windows
    if repo_id not in cache:
        from .commit_window import CommitWindow

        cache[repo_id] = CommitWindow(_bundle(repo_id).entry.repo_dir)
    return cache[repo_id]


def _window_stats_for(repo_id: str):
    """Commit-window cost figures for *repo_id*, or None when it has no window."""
    from .schemas import WindowStats

    window = _commit_window(repo_id)
    if not window.available:
        return None
    stats = window.summary().get("stats")
    return WindowStats(**stats) if stats else None


@app.get("/api/health")
async def health() -> dict:
    registry = getattr(app.state, "registry", None)
    return {
        "status": "ok",
        "repos": len(registry.list_infos()) if registry else 0,
    }


@app.get("/api/repos", response_model=list[RepoInfo])
async def list_repos() -> list[RepoInfo]:
    infos = _registry().list_infos()
    # Surface the (global) edge-label toggle per repo so the UI can gate the
    # feature. repo_registry.py is skip-worktree, so inject it here instead.
    edge_on = bool(getattr(load_config(), "edge_labels", False))
    for info in infos:
        info.capabilities = {**info.capabilities, "edge_labels": edge_on}
        # Attach commit-window cost figures so the landing page can say what
        # incremental maintenance bought, rather than only naming a commit.
        # CommitWindow is lazy and cached per repo, and _bundle() is a registry
        # lookup with no graph loading, so this stays one cheap request.
        try:
            info.incremental = await asyncio.to_thread(_window_stats_for, info.id)
        except HTTPException:
            # Registry and bundle can disagree transiently; a missing bundle
            # just means no window figures for that card.
            info.incremental = None
    return infos


@app.get("/api/repos/{repo_id}/wiki")
async def wiki_tree(repo_id: str) -> dict:
    builder = _wiki(repo_id)
    pages = await asyncio.to_thread(builder.page_tree)
    return {"repo": _bundle(repo_id).entry.repo, "pages": pages}


@app.get("/api/repos/{repo_id}/wiki/{page_id}")
async def wiki_page(repo_id: str, page_id: str) -> dict:
    page = await asyncio.to_thread(_wiki(repo_id).page, page_id)
    if page is None:
        raise HTTPException(status_code=404, detail=f"Unknown wiki page: {page_id!r}")
    return page


@app.get("/api/repos/{repo_id}/wiki/{page_id}/graph")
async def wiki_page_graph(repo_id: str, page_id: str) -> dict:
    """Induced dependency subgraph over a wiki page's cited symbols.

    Lets a wiki page render as a *view over the graph* (subsystem symbols + how
    they connect), using the same ``{nodes, edges}`` payload as ``/codemap``.
    """
    page = await asyncio.to_thread(_wiki(repo_id).page, page_id)
    if page is None:
        raise HTTPException(status_code=404, detail=f"Unknown wiki page: {page_id!r}")
    bundle = _bundle(repo_id)
    graph = await asyncio.to_thread(bundle.code_graph)
    if graph is None:
        return {"available": False, "nodes": [], "edges": [], "mermaid": ""}
    hierarchy_graph = await asyncio.to_thread(bundle.hierarchical_graph)
    citations = page.get("citations", []) if isinstance(page, dict) else []
    return await asyncio.to_thread(
        build_page_subgraph,
        graph,
        citations,
        repo_dir=bundle.entry.repo_dir,
        hierarchy_graph=hierarchy_graph,
    )


@app.get("/api/repos/{repo_id}/source")
async def source(
    repo_id: str, file: str, start: int | None = None, end: int | None = None
) -> dict:
    result = _wiki(repo_id).source(file, start, end)
    if result is None:
        raise HTTPException(status_code=404, detail=f"File not found: {file!r}")
    return result


@app.get("/api/repos/{repo_id}/commits")
async def commits(repo_id: str) -> dict:
    """Commits selectable in the graph view, newest first.

    Backed by ``scripts/build_commit_window.py``. Repos without a prebuilt
    window return ``available=False`` and the UI keeps its single-commit label.
    """
    _bundle(repo_id)  # 404 on unknown repo
    window = _commit_window(repo_id)
    return await asyncio.to_thread(window.summary)


@app.get("/api/repos/{repo_id}/codemap")
async def codemap(
    repo_id: str,
    symbol: str | None = None,
    direction: str = "both",
    depth: int = 2,
    max_nodes: int = 40,
    commit: str | None = None,
) -> dict:
    """Dependency subgraph ("codemap") around *symbol* (or a central default).

    Returns ``{available, root, nodes, edges, mermaid, ...}``; the ``mermaid``
    field renders directly in the frontend's existing diagram component.
    """
    bundle = _bundle(repo_id)
    # Prefer a commit-window snapshot when one exists: an explicit ``commit``
    # selects that point in history, and an absent one still defaults to the
    # window's newest commit so the graph matches the selector's default.
    window = _commit_window(repo_id)
    graph = None
    selected_commit = None
    fell_back = False
    if window.available:
        entry = window.resolve(commit)
        if entry is None and commit:
            raise HTTPException(
                status_code=404, detail=f"Unknown commit for this repo: {commit!r}"
            )
        graph = await asyncio.to_thread(window.graph_for, commit)
        if entry is not None and graph is not None:
            selected_commit = entry.get("sha")
    if graph is None:
        # The snapshot was absent or unloadable (e.g. a graph.pkl written under
        # an older schema_version). Serving the repo's default graph is fine;
        # reporting it as the requested commit is not -- the client would render
        # one commit's graph under another commit's label.
        graph = await asyncio.to_thread(bundle.code_graph)
        selected_commit = bundle.entry.base_commit
        fell_back = window.available
    if graph is None:
        return {
            "available": False,
            "nodes": [],
            "edges": [],
            "hierarchy": {"root": "hier::root", "nodes": [], "open_files": []},
            "mermaid": "",
            "note": "This repo has no symbol graph.",
        }
    result = await asyncio.to_thread(
        build_codemap,
        graph,
        symbol,
        direction,
        depth,
        max_nodes,
        repo_dir=bundle.entry.repo_dir,
        hierarchy_graph=await asyncio.to_thread(bundle.hierarchical_graph),
    )
    # Let the client confirm which snapshot it is looking at. ``fell_back``
    # marks the case where a window exists but its snapshot could not be served,
    # so the UI can say so instead of implying the selection took effect.
    if isinstance(result, dict):
        result["commit"] = selected_commit
        if fell_back:
            result["fell_back"] = True
    return result


@app.post("/api/repos/{repo_id}/edge-label", response_model=EdgeLabelResponse)
async def edge_label(repo_id: str, req: EdgeLabelRequest) -> EdgeLabelResponse:
    """Short LLM phrase describing how the source symbol uses the target.

    On-demand + cached; gated by ``edge_labels`` config. Returns ``label=""``
    (with ``disabled``/empty) whenever the feature is off or nothing could be
    generated — the UI falls back to its ref-count display.
    """
    if not bool(getattr(load_config(), "edge_labels", False)):
        return EdgeLabelResponse(label="", disabled=True)
    _bundle(repo_id)  # 404 on unknown repo
    labeler = _edge_labeler(repo_id)
    anchors = [a.model_dump() for a in req.anchors]
    label, cached = await asyncio.to_thread(
        labeler.label,
        req.source.file,
        req.source.line,
        req.source.end_line,
        req.source.label,
        req.target.file,
        req.target.line,
        req.target.end_line,
        req.target.label,
        anchors,
    )
    return EdgeLabelResponse(label=label, cached=cached)


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    if not req.messages or req.messages[-1].role != "user":
        raise HTTPException(
            status_code=400, detail="last message must be from the user"
        )
    query = req.messages[-1].content.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query must not be empty")

    bundle = _registry().get(req.repo_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail=f"Unknown repo: {req.repo_id!r}")
    await asyncio.to_thread(bundle.ensure_runtime)
    if bundle.runner is None:
        raise HTTPException(status_code=503, detail="repo runtime is unavailable")

    # Earlier messages give the agent context so it can resolve follow-ups
    # like "what calls it?".
    chat_history = [{"role": m.role, "content": m.content} for m in req.messages[:-1]]

    try:
        result = await asyncio.to_thread(
            bundle.runner.run, query, chat_history=chat_history
        )
    except Exception as exc:  # noqa: BLE001 - surface a clean 500 to the client
        logger.error("agent run failed for %r: %s", req.repo_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail="agent run failed") from exc

    return agent_result_to_response(
        result, repo_path=getattr(bundle.manifest, "repo_path", "")
    )


def main() -> None:
    """Console-script entry point: ``codeminer-web``."""
    import uvicorn

    host = os.environ.get("CODEMINER_DEMO_HOST", "127.0.0.1")
    port = int(os.environ.get("CODEMINER_DEMO_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
