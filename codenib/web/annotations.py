# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Wiki annotations over HTTP, for the local Wiki only.

These routes write files into the person's repository and start their coding
agent, so they exist only when ``codenib wiki`` launched this server on the
person's machine: the launcher sets ``CODENIB_LOCAL_SESSION_TOKEN`` and opens
the browser with that token. A request needs the token in the
``X-CodeNib-Session`` header and a loopback ``Host``. A custom header makes a
cross-site request preflight, which CORS refuses, and the ``Host`` check
stops DNS rebinding. The public demo never sets the token, so the routes
answer 404 there.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..annotations import AnnotationError, AnnotationStore, TaskStore
from ..annotations.agents import agent_for, detect_agents
from ..annotations.edits import (
    apply_edit,
    fold_into,
    page_source,
    revert_page,
    save_page,
)
from ..annotations.model import HUMAN_KINDS
from ..annotations.runner import accept, dispatch, reject, review, run_task

log = logging.getLogger(__name__)

TOKEN_ENV = "CODENIB_LOCAL_SESSION_TOKEN"
HEADER = "x-codenib-session"
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}

router = APIRouter()
# Citations of a whole file or its import header, not of code a change edits.
_WHOLE_FILE_TYPES = {"header", "file", "module"}


@dataclass
class RepoAccess:
    """What the routes need from the Wiki app, supplied by ``configure``."""

    #: repo id -> (repository checkout, "owner/name")
    repo: Callable[[str], tuple]
    #: (repo id, page id) -> the page dict, or None
    page: Callable[[str, str], Optional[Dict[str, Any]]]
    #: repo id -> [{"id", "title", "citations": [{"file", "start_line",
    #: "end_line"}]}] for every page
    pages: Callable[[str], List[Dict[str, Any]]]


_access: Optional[RepoAccess] = None
_running: set = set()
_running_lock = threading.Lock()


def configure(access: RepoAccess) -> None:
    global _access
    _access = access


def enabled() -> bool:
    return bool(os.environ.get(TOKEN_ENV, "").strip())


def _authorize(request: Request) -> None:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token or _access is None:
        raise HTTPException(status_code=404, detail="Not Found")
    host = request.headers.get("host", "")
    hostname = (
        host.rsplit(":", 1)[0] if not host.startswith("[") else host[1:].split("]")[0]
    )
    if hostname not in _LOOPBACK:
        raise HTTPException(status_code=403, detail="Annotations are local only")
    supplied = request.headers.get(HEADER, "")
    if not hmac.compare_digest(supplied.encode(), token.encode()):
        raise HTTPException(
            status_code=403,
            detail="Open the Wiki from the link `codenib wiki` printed to annotate it",
        )


def _repo(repo_id: str) -> tuple:
    assert _access is not None
    path, name = _access.repo(repo_id)
    path = Path(path)
    probe = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        raise HTTPException(
            status_code=409,
            detail="Annotations need the repository to be a git checkout",
        )
    return path, str(name or repo_id)


def _fail(exc: AnnotationError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _recover(repo: Path) -> None:
    """A task this process is not running was cut off by a restart."""

    tasks = TaskStore(repo)
    store = AnnotationStore(repo)
    for task in tasks.list():
        if task.status not in ("queued", "running"):
            continue
        with _running_lock:
            alive = task.id in _running
        if alive:
            continue
        tasks.update(task.id, status="failed", error="interrupted: the server stopped")
        for annotation_id in task.inputs:
            try:
                if store.get(annotation_id).status == "dispatched":
                    store.update(annotation_id, status="failed")
            except AnnotationError:
                pass


class AnnotationIn(BaseModel):
    kind: Literal["question", "change", "note"]
    text: str = Field(min_length=1, max_length=20_000)
    on: Dict[str, Any] = Field(default_factory=dict)
    reply_to: Optional[str] = None


class AnnotationPatch(BaseModel):
    text: Optional[str] = Field(default=None, min_length=1, max_length=20_000)
    kind: Optional[Literal["question", "change", "note"]] = None
    status: Optional[Literal["draft", "open", "resolved"]] = None


class TaskIn(BaseModel):
    annotations: List[str] = Field(min_length=1, max_length=50)
    agent: str = "claude-code"


def _state(repo: Path, page: Optional[str]) -> Dict[str, Any]:
    _recover(repo)
    store, tasks = AnnotationStore(repo), TaskStore(repo)
    annotations = store.list(page)
    task_ids = {a.task for a in annotations if a.task}
    recent = tasks.list()
    shown = [t for t in recent if page is None or t.id in task_ids][-20:]
    active = tasks.active()
    if active is not None and active not in shown:
        shown.append(active)
    return {
        "annotations": [a.to_json() for a in annotations],
        "tasks": [t.to_json() for t in shown],
        "agents": detect_agents(),
        "kinds": list(HUMAN_KINDS),
    }


@router.get("/api/repos/{repo_id}/annotations")
async def list_annotations(
    repo_id: str, request: Request, page: Optional[str] = None
) -> dict:
    _authorize(request)
    repo, _ = await asyncio.to_thread(_repo, repo_id)
    return await asyncio.to_thread(_state, repo, page)


@router.post("/api/repos/{repo_id}/annotations")
async def create_annotation(repo_id: str, body: AnnotationIn, request: Request) -> dict:
    _authorize(request)
    repo, _ = await asyncio.to_thread(_repo, repo_id)
    store = AnnotationStore(repo)
    try:
        annotation = await asyncio.to_thread(
            lambda: store.create(
                kind=body.kind,
                text=body.text,
                on=body.on,
                reply_to=body.reply_to or "",
            )
        )
    except AnnotationError as exc:
        raise _fail(exc) from exc
    return annotation.to_json()


@router.patch("/api/repos/{repo_id}/annotations/{annotation_id}")
async def update_annotation(
    repo_id: str, annotation_id: str, body: AnnotationPatch, request: Request
) -> dict:
    _authorize(request)
    repo, _ = await asyncio.to_thread(_repo, repo_id)
    store = AnnotationStore(repo)
    changes = body.model_dump(exclude_none=True)

    def apply() -> dict:
        current = store.get(annotation_id)
        if current.author != "human" and set(changes) - {"status"}:
            raise AnnotationError("an agent's annotation can only be resolved")
        if ("text" in changes or "kind" in changes) and current.status == "dispatched":
            raise AnnotationError("this annotation is in a running task")
        return store.update(annotation_id, **changes).to_json()

    try:
        return await asyncio.to_thread(apply)
    except AnnotationError as exc:
        raise _fail(exc) from exc


@router.delete("/api/repos/{repo_id}/annotations/{annotation_id}")
async def delete_annotation(repo_id: str, annotation_id: str, request: Request) -> dict:
    _authorize(request)
    repo, _ = await asyncio.to_thread(_repo, repo_id)
    store = AnnotationStore(repo)

    def remove() -> None:
        current = store.get(annotation_id)
        if current.author != "human" or current.status not in (
            "draft",
            "open",
            "failed",
        ):
            raise AnnotationError("only your own unsent annotations can be deleted")
        if any(a.reply_to == annotation_id for a in store.list()):
            raise AnnotationError("this annotation has replies; resolve it instead")
        store.delete(annotation_id)

    try:
        await asyncio.to_thread(remove)
    except AnnotationError as exc:
        raise _fail(exc) from exc
    return {"deleted": annotation_id}


def _pages_citing(
    repo_id: str,
) -> Callable[[Dict[str, List[tuple]]], List[Dict[str, Any]]]:
    """Pages whose citations reach the changed lines, not just the file.

    A file such as ``sessions.py`` is cited by most pages of a Wiki; a note on
    every one of them would be noise. A citation counts when its line range
    contains a changed line. A file's import header does not count: a page
    that cites it is not about the code that changed.
    """

    def find(changed: Dict[str, List[tuple]]) -> List[Dict[str, Any]]:
        assert _access is not None
        found = []
        for page in _access.pages(repo_id):
            hits = set()
            for citation in page.get("citations") or []:
                if citation.get("type") in _WHOLE_FILE_TYPES:
                    continue
                spans = changed.get(citation.get("file") or "")
                if not spans:
                    continue
                start = citation.get("start_line")
                end = citation.get("end_line") or start
                if not start:
                    continue
                if any(start <= b and a <= end for a, b in spans):
                    hits.add(citation["file"])
            if hits:
                found.append(
                    {
                        "id": page["id"],
                        "title": page.get("title"),
                        "files": sorted(hits),
                    }
                )
        return found

    return find


def _start(repo_id: str, repo: Path, name: str, task_id: str, agent_name: str) -> None:
    assert _access is not None
    access = _access
    agent = agent_for(agent_name)

    def work() -> None:
        try:
            run_task(
                repo,
                task_id,
                agent=agent,
                page_lookup=lambda page_id: apply_edit(
                    repo, page_id, access.page(repo_id, page_id)
                ),
                pages_citing=_pages_citing(repo_id),
                repo_name=name,
            )
        finally:
            with _running_lock:
                _running.discard(task_id)

    with _running_lock:
        _running.add(task_id)
    threading.Thread(target=work, name=f"codenib-task-{task_id}", daemon=True).start()


@router.post("/api/repos/{repo_id}/tasks")
async def create_task(repo_id: str, body: TaskIn, request: Request) -> dict:
    _authorize(request)
    repo, name = await asyncio.to_thread(_repo, repo_id)
    if body.agent != "claude-code" and not os.environ.get("CODENIB_TASK_AGENT_COMMAND"):
        raise HTTPException(
            status_code=400, detail=f"No adapter for {body.agent!r} yet"
        )

    def begin() -> dict:
        _recover(repo)
        task = dispatch(repo, body.annotations, agent=body.agent)
        _start(repo_id, repo, name, task.id, body.agent)
        return task.to_json()

    try:
        return await asyncio.to_thread(begin)
    except AnnotationError as exc:
        raise _fail(exc) from exc


@router.get("/api/repos/{repo_id}/tasks/{task_id}")
async def get_task(repo_id: str, task_id: str, request: Request) -> dict:
    _authorize(request)
    repo, _ = await asyncio.to_thread(_repo, repo_id)
    try:
        return (await asyncio.to_thread(TaskStore(repo).get, task_id)).to_json()
    except AnnotationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/repos/{repo_id}/tasks/{task_id}/diff")
async def get_task_diff(repo_id: str, task_id: str, request: Request) -> dict:
    """The diff, and each hunk with whether it was accepted or discarded."""

    _authorize(request)
    repo, _ = await asyncio.to_thread(_repo, repo_id)
    try:
        return await asyncio.to_thread(review, repo, task_id)
    except AnnotationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class HunksIn(BaseModel):
    hunks: List[str] = Field(default_factory=list, max_length=500)


@router.post("/api/repos/{repo_id}/tasks/{task_id}/{decision}")
async def decide_task(
    repo_id: str,
    task_id: str,
    decision: Literal["accept", "reject"],
    request: Request,
    body: Optional[HunksIn] = None,
) -> dict:
    """Accept or discard the hunks named, or every undecided hunk."""

    _authorize(request)
    repo, _ = await asyncio.to_thread(_repo, repo_id)
    action = accept if decision == "accept" else reject
    hunks = (body.hunks if body else None) or None
    try:
        return (await asyncio.to_thread(action, repo, task_id, hunks)).to_json()
    except AnnotationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# --- People write the Wiki -------------------------------------------------


class MarkdownIn(BaseModel):
    markdown: str = Field(min_length=1, max_length=400_000)
    base: str = Field(default="", max_length=64)


class FoldIn(BaseModel):
    annotation: str = Field(min_length=26, max_length=26)


def _generated(repo_id: str, page_id: str) -> Optional[Dict[str, Any]]:
    assert _access is not None
    return _access.page(repo_id, page_id)


@router.get("/api/repos/{repo_id}/pages/{page_id}/markdown")
async def get_page_markdown(repo_id: str, page_id: str, request: Request) -> dict:
    _authorize(request)
    repo, _ = await asyncio.to_thread(_repo, repo_id)
    try:
        page = await asyncio.to_thread(_generated, repo_id, page_id)
        return await asyncio.to_thread(page_source, repo, page_id, page)
    except (AnnotationError, OSError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/api/repos/{repo_id}/pages/{page_id}/markdown")
async def put_page_markdown(
    repo_id: str, page_id: str, body: MarkdownIn, request: Request
) -> dict:
    _authorize(request)
    repo, _ = await asyncio.to_thread(_repo, repo_id)
    try:
        page = await asyncio.to_thread(_generated, repo_id, page_id)
        return await asyncio.to_thread(
            save_page, repo, page_id, page, body.markdown, body.base
        )
    except AnnotationError as exc:
        status = 409 if "changed since" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.delete("/api/repos/{repo_id}/pages/{page_id}/markdown")
async def delete_page_markdown(repo_id: str, page_id: str, request: Request) -> dict:
    _authorize(request)
    repo, _ = await asyncio.to_thread(_repo, repo_id)
    try:
        reverted = await asyncio.to_thread(revert_page, repo, page_id)
    except AnnotationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"reverted": reverted}


@router.post("/api/repos/{repo_id}/pages/{page_id}/fold")
async def fold_answer(
    repo_id: str, page_id: str, body: FoldIn, request: Request
) -> dict:
    """Add an agent's answer to the page, under the question it answers."""

    _authorize(request)
    repo, _ = await asyncio.to_thread(_repo, repo_id)
    store = AnnotationStore(repo)

    def fold() -> dict:
        answer = store.get(body.annotation)
        if answer.author != "agent" or answer.kind != "answer":
            raise AnnotationError("only an agent's answer can be added to a page")
        question = store.get(answer.reply_to) if answer.reply_to else None
        page = _generated(repo_id, page_id)
        current = page_source(repo, page_id, page)
        markdown = fold_into(
            current["markdown"],
            section=str(answer.on.get("section") or ""),
            quote=str(answer.on.get("quote") or ""),
            question=question.text if question else "Answer",
            answer=answer.text,
        )
        saved = save_page(repo, page_id, page, markdown, current["base"])
        store.update(answer.id, status="applied")
        return saved

    try:
        return await asyncio.to_thread(fold)
    except AnnotationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
