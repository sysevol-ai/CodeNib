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
from .codemap import build_codemap
from .config import load_config
from .repo_registry import RepoRegistry
from .schemas import ChatRequest, ChatResponse, RepoInfo, agent_result_to_response

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    registry = RepoRegistry(config)
    logger.info("Loading QA repos from %s ...", config.registry_path)
    registry.load_all()
    app.state.registry = registry
    app.state.wiki_builders = {}
    # Shared LLM narrator for DeepWiki-style prose; cached on disk, fails soft
    # to templated text when no model/creds are available.
    wiki_cache = os.path.join(os.path.abspath(config.data_dir), "wiki_cache")
    app.state.narrator = Narrator(model=config.model, cache_dir=wiki_cache)
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


def _wiki(repo_id: str) -> WikiBuilder:
    """Lazily build + cache a WikiBuilder per repo."""
    cache = app.state.wiki_builders
    if repo_id not in cache:
        cache[repo_id] = WikiBuilder(
            _bundle(repo_id), narrator=getattr(app.state, "narrator", None)
        )
    return cache[repo_id]


@app.get("/api/health")
async def health() -> dict:
    registry = getattr(app.state, "registry", None)
    return {
        "status": "ok",
        "repos": len(registry.list_infos()) if registry else 0,
    }


@app.get("/api/repos", response_model=list[RepoInfo])
async def list_repos() -> list[RepoInfo]:
    return _registry().list_infos()


@app.get("/api/repos/{repo_id}/wiki")
async def wiki_tree(repo_id: str) -> dict:
    builder = _wiki(repo_id)
    return {"repo": _bundle(repo_id).entry.repo, "pages": builder.page_tree()}


@app.get("/api/repos/{repo_id}/wiki/{page_id}")
async def wiki_page(repo_id: str, page_id: str) -> dict:
    page = _wiki(repo_id).page(page_id)
    if page is None:
        raise HTTPException(status_code=404, detail=f"Unknown wiki page: {page_id!r}")
    return page


@app.get("/api/repos/{repo_id}/source")
async def source(
    repo_id: str, file: str, start: int | None = None, end: int | None = None
) -> dict:
    result = _wiki(repo_id).source(file, start, end)
    if result is None:
        raise HTTPException(status_code=404, detail=f"File not found: {file!r}")
    return result


@app.get("/api/repos/{repo_id}/codemap")
async def codemap(
    repo_id: str,
    symbol: str | None = None,
    direction: str = "both",
    depth: int = 2,
    max_nodes: int = 40,
) -> dict:
    """Dependency subgraph ("codemap") around *symbol* (or a central default).

    Returns ``{available, root, nodes, edges, mermaid, ...}``; the ``mermaid``
    field renders directly in the frontend's existing diagram component.
    """
    bundle = _bundle(repo_id)
    graph = await asyncio.to_thread(bundle.code_graph)
    if graph is None:
        return {
            "available": False,
            "nodes": [],
            "edges": [],
            "mermaid": "",
            "note": "This repo has no symbol graph.",
        }
    return await asyncio.to_thread(
        build_codemap, graph, symbol, direction, depth, max_nodes
    )


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    query = req.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query must not be empty")

    bundle = _registry().get(req.repo_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail=f"Unknown repo: {req.repo_id!r}")

    try:
        result = await asyncio.to_thread(bundle.runner.run, query)
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
