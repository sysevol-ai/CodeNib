# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Explicit, visitor-funded Wiki creation and read-only saved Wiki routes."""

from __future__ import annotations

import asyncio
import os
import re
import threading
from importlib.util import find_spec
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ..storage import SQLiteWikiStore
from ..wiki.store import WikiGenerationBusyError
from ..wiki.visitor_wiki import VisitorWikiError, VisitorWikis
from .config import load_config

router = APIRouter(prefix="/api/visitor-wikis")
_manager_lock = threading.Lock()
_TOKEN = re.compile(r"[a-f0-9]{64}")


class GenerateWiki(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    repository: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$"
    )
    budget_usd: float = Field(ge=0.25, le=5)
    model: Literal["anthropic/claude-sonnet-4.6", "deepseek/deepseek-v4.1-flash"] = (
        "anthropic/claude-sonnet-4.6"
    )


class WikiPublication(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    published: bool


def _enabled():
    if os.environ.get("CODENIB_VISITOR_WIKI", "").lower() not in {"1", "true", "yes"}:
        return False
    from ..agent.runtime.grep_jev import resolve_ripgrep

    return find_spec("requests") is not None and resolve_ripgrep() is not None


def _manager(request, *, for_generation=False):
    if for_generation and not _enabled():
        raise HTTPException(
            503, "Visitor Wiki generation is not enabled on this server."
        )
    with _manager_lock:
        manager = getattr(request.app.state, "visitor_wikis", None)
        if manager is None:
            path = (
                Path(load_config().data_dir).absolute()
                / "wiki_cache"
                / "visitor_wiki.sqlite3"
            )
            if not for_generation and not path.is_file():
                raise HTTPException(404, "This saved Wiki was not found.")
            manager = VisitorWikis(SQLiteWikiStore(path))
            request.app.state.visitor_wikis = manager
        return manager


def _token(value):
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise HTTPException(400, "Invalid saved Wiki identifier or owner token.")
    return value


def _owner(request):
    return _token(request.headers.get("X-Wiki-Owner", ""))


@router.get("")
def capabilities(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return {"enabled": _enabled(), "persistence": "unlisted", "max_budget_usd": 5}


@router.post("/{attempt}", status_code=202)
async def generate(
    attempt: str, payload: GenerateWiki, request: Request, response: Response
):
    manager = _manager(request, for_generation=True)
    _token(attempt)
    owner = _owner(request)
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer ") or not re.fullmatch(
        r"[\x21-\x7e]{10,1024}", header[7:]
    ):
        raise HTTPException(400, "An OpenRouter inference key is required.")
    try:
        state = await asyncio.to_thread(
            manager.submit,
            attempt,
            payload.repository,
            owner,
            header[7:],
            payload.budget_usd,
            request.client.host if request.client else "unknown",
            payload.model,
        )
    except (VisitorWikiError, WikiGenerationBusyError) as exc:
        raise HTTPException(409, str(exc)) from None
    response.headers["Cache-Control"] = "no-store"
    return state


@router.get("/public")
def public_wikis(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        return _manager(request).public_wikis()
    except HTTPException as exc:
        if exc.status_code == 404:
            return []
        raise


@router.post("/{attempt}/publication")
def publish_wiki(
    attempt: str, payload: WikiPublication, request: Request, response: Response
):
    response.headers["Cache-Control"] = "no-store"
    try:
        return _manager(request).publish_wiki(
            _token(attempt), _owner(request), payload.published
        )
    except (VisitorWikiError, WikiGenerationBusyError) as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/{attempt}")
def status(attempt: str, request: Request, response: Response):
    try:
        result = _manager(request).status(_token(attempt))
    except VisitorWikiError as exc:
        raise HTTPException(404, str(exc)) from None
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return result


@router.get("/{attempt}/pages/{page_id}")
def page(attempt: str, page_id: str, request: Request, response: Response):
    if not re.fullmatch(r"[a-z0-9_-]{1,160}", page_id):
        raise HTTPException(400, "Invalid Wiki page.")
    try:
        result = _manager(request).page(_token(attempt), page_id)
    except VisitorWikiError as exc:
        raise HTTPException(404, str(exc)) from None
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return result


@router.post("/{attempt}/stop")
def stop(attempt: str, request: Request):
    try:
        _manager(request).cancel(_token(attempt), _owner(request))
    except VisitorWikiError as exc:
        raise HTTPException(403, str(exc)) from None
    return {"stopping": True}
