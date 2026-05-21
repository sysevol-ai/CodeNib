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
from .config import load_config
from .repo_registry import RepoRegistry
from .schemas import ChatRequest, ChatResponse, RepoInfo, agent_result_to_response

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    registry = RepoRegistry(config)
    logger.info("Loading %d demo repo(s)...", len(config.repos))
    registry.load_all()
    app.state.registry = registry
    logger.info("Ready: %d repo(s) available", len(registry.list_infos()))
    yield


app = FastAPI(title="CodeMiner DeepWiki Demo", lifespan=lifespan)

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

    return agent_result_to_response(result)


def main() -> None:
    """Console-script entry point: ``codeminer-web``."""
    import uvicorn

    host = os.environ.get("CODEMINER_DEMO_HOST", "127.0.0.1")
    port = int(os.environ.get("CODEMINER_DEMO_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
