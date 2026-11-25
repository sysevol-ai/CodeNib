# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Wiki annotations over HTTP: on the person's machine, or hosted.

Local: these routes write files into the person's repository and start their
coding agent, so they exist only when ``codenib wiki`` launched this server
on the person's machine: the launcher sets ``CODENIB_LOCAL_SESSION_TOKEN`` and
opens the browser with that token. A request needs the token in the
``X-CodeNib-Session`` header and a loopback ``Host``. A custom header makes a
cross-site request preflight, which CORS refuses, and the ``Host`` check
stops DNS rebinding.

Hosted (``CODENIB_HOSTED_ANNOTATIONS=1``, the public demo): the served
checkout is shared and read-only. Each browser holds a random visitor key
(``X-CodeNib-Visitor``) and its annotations and tasks live in a folder of
their own on the server. Questions and changes run Claude Code in a container
on the visitor's own OpenRouter or Anthropic key (``Authorization: Bearer``),
sent with each task and never stored; a change is taken away as a patch.
Page edits stay local-only for now.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..annotations import AnnotationError, AnnotationStore, TaskStore
from ..annotations.agents import AGENTS, agent_for, detect_agents
from ..annotations.anchoring import context, follow, head_commit, locate
from ..annotations.edits import (apply_edit, fold_into, page_source,
                                 revert_page, save_page)
from ..annotations.model import HUMAN_KINDS
from ..annotations.runner import (accept, dispatch, kept_patch, reject, review,
                                  run_task)
from ..annotations.sandbox import DEFAULT_BUDGET_USD, SandboxedClaude
from ..annotations.sandbox import available as sandbox_available
from ..annotations.sandbox import check_key, provider_of

log = logging.getLogger(__name__)

TOKEN_ENV = "CODENIB_LOCAL_SESSION_TOKEN"
HEADER = "x-codenib-session"
HOSTED_ENV = "CODENIB_HOSTED_ANNOTATIONS"
VISITOR_HEADER = "x-codenib-visitor"
_VISITOR = re.compile(r"\A[A-Za-z0-9_-]{32,128}\Z")
#: Hosted runs at once on this server, and per hour; each visitor runs one.
HOSTED_MAX_RUNNING = int(os.environ.get("CODENIB_HOSTED_MAX_TASKS", "2") or 2)
HOSTED_MAX_PER_HOUR = int(os.environ.get("CODENIB_HOSTED_TASKS_PER_HOUR", "60") or 60)
HOSTED_MAX_ANNOTATIONS = 500
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
    #: Where hosted visitors' folders live (the server's data dir).
    state_root: Optional[Callable[[], Path]] = None


@dataclass(frozen=True)
class Space:
    """The code annotations sit on, and where they are kept."""

    repo_id: str
    repo: Path
    name: str
    #: None: in the repository (local); else a hosted visitor's folder.
    state: Optional[Path] = None

    @property
    def hosted(self) -> bool:
        return self.state is not None


_access: Optional[RepoAccess] = None
_running: set = set()
_running_lock = threading.Lock()
#: Annotations already located against unchanged files (see anchoring.follow).
_anchor_cache: Dict[str, tuple] = {}
#: The local Wiki's working-tree follower, when there is one.
_worktree: Any = None


def set_worktree(follower: Any) -> None:
    global _worktree
    _worktree = follower


def configure(access: RepoAccess) -> None:
    global _access
    _access = access


def enabled() -> bool:
    return bool(os.environ.get(TOKEN_ENV, "").strip())


def hosted_enabled() -> bool:
    """The public Wiki lets visitors annotate, on their own keys."""

    if enabled():
        return False
    return os.environ.get(HOSTED_ENV, "").strip().lower() in {"1", "true", "yes"}


_hosted_runs: List[float] = []


def _visitor_folder(repo_id: str, visitor: str) -> Path:
    assert _access is not None and _access.state_root is not None
    digest = hashlib.sha256(visitor.encode()).hexdigest()[:32]
    safe_repo = re.sub(r"[^A-Za-z0-9_.-]", "_", repo_id)[:120]
    return Path(_access.state_root()) / "hosted-annotations" / safe_repo / digest


async def _space(request: Request, repo_id: str) -> Space:
    """Who may annotate this repository, and where their annotations go."""

    if hosted_enabled() and _access is not None and _access.state_root:
        visitor = request.headers.get(VISITOR_HEADER, "")
        if not _VISITOR.match(visitor):
            raise HTTPException(
                status_code=401, detail="This browser has no visitor key yet"
            )
        repo, name = await asyncio.to_thread(_repo, repo_id)
        return Space(repo_id, repo, name, _visitor_folder(repo_id, visitor))
    _authorize(request)
    repo, name = await asyncio.to_thread(_repo, repo_id)
    return Space(repo_id, repo, name)


def _local_only(space: Space) -> None:
    if space.hosted:
        raise HTTPException(
            status_code=403,
            detail="Editing Wiki pages needs the Wiki on your machine for now",
        )


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


def _recover(repo: Path, state: Optional[Path] = None) -> None:
    """A task this process is not running was cut off by a restart."""

    tasks = TaskStore(repo, state)
    store = AnnotationStore(repo, state)
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


def _hosted_agents() -> List[Dict[str, Any]]:
    return [
        {
            "id": "claude-code",
            "name": "Claude Code",
            "available": sandbox_available(),
            "hosted": True,
            "budget_usd": DEFAULT_BUDGET_USD,
        }
    ]


def _state(space: Space, page: Optional[str]) -> Dict[str, Any]:
    repo = space.repo
    _recover(repo, space.state)
    store, tasks = AnnotationStore(repo, space.state), TaskStore(repo, space.state)
    # Code moves under its annotations; follow it before showing them.
    annotations = follow(store, store.list(page), _anchor_cache)
    task_ids = {a.task for a in annotations if a.task}
    recent = tasks.list()
    shown = [t for t in recent if page is None or t.id in task_ids][-20:]
    active = tasks.active()
    if active is not None and active not in shown:
        shown.append(active)
    return {
        "annotations": [a.to_json() for a in annotations],
        "tasks": [t.to_json() for t in shown],
        "agents": _hosted_agents() if space.hosted else detect_agents(),
        "kinds": list(HUMAN_KINDS),
        "mode": "hosted" if space.hosted else "local",
        "worktree": (
            _worktree.status() if _worktree is not None and not space.hosted else None
        ),
    }


@router.get("/api/repos/{repo_id}/annotations")
async def list_annotations(
    repo_id: str, request: Request, page: Optional[str] = None
) -> dict:
    space = await _space(request, repo_id)
    return await asyncio.to_thread(_state, space, page)


@router.post("/api/repos/{repo_id}/annotations")
async def create_annotation(repo_id: str, body: AnnotationIn, request: Request) -> dict:
    space = await _space(request, repo_id)
    repo = space.repo
    store = AnnotationStore(repo, space.state)

    def create() -> Any:
        if space.hosted and len(store.list()) >= HOSTED_MAX_ANNOTATIONS:
            raise AnnotationError(
                f"This browser has {HOSTED_MAX_ANNOTATIONS} annotations on this "
                "Wiki; resolve or delete some first"
            )
        on = dict(body.on)
        state = ""
        if on.get("path"):
            # The panel shows the indexed code; anchor to the working tree.
            on["commit"] = head_commit(repo)
            found = locate(repo, on)
            if found is not None and found.state != "detached":
                on.update(
                    path=found.path,
                    start_line=found.start_line,
                    end_line=found.end_line,
                )
                # Remember the neighbours, to tell this code from copies of it.
                source = (repo / found.path).read_text(
                    encoding="utf-8", errors="replace"
                )
                on["before"], on["after"] = context(
                    source.splitlines(), found.start_line, found.end_line
                )
                state = "current"
            elif found is not None:
                state = "detached"
        annotation = store.create(
            kind=body.kind, text=body.text, on=on, reply_to=body.reply_to or ""
        )
        if state:
            annotation = store.update(annotation.id, anchor_state=state)
        return annotation

    try:
        annotation = await asyncio.to_thread(create)
    except AnnotationError as exc:
        raise _fail(exc) from exc
    return annotation.to_json()


@router.patch("/api/repos/{repo_id}/annotations/{annotation_id}")
async def update_annotation(
    repo_id: str, annotation_id: str, body: AnnotationPatch, request: Request
) -> dict:
    space = await _space(request, repo_id)
    store = AnnotationStore(space.repo, space.state)
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
    space = await _space(request, repo_id)
    store = AnnotationStore(space.repo, space.state)

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


def _start(space: Space, task_id: str, agent: Any) -> None:
    assert _access is not None
    access = _access
    repo, repo_id = space.repo, space.repo_id

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
                repo_name=space.name,
                state=space.state,
                shared=space.hosted,
            )
        finally:
            forget = getattr(agent, "forget_key", None)
            if callable(forget):
                forget()
            with _running_lock:
                _running.discard(task_id)

    with _running_lock:
        _running.add(task_id)
    threading.Thread(target=work, name=f"codenib-task-{task_id}", daemon=True).start()


def _hosted_agent(request: Request) -> SandboxedClaude:
    """The visitor's key, checked, in an agent that forgets it after the run."""

    header = request.headers.get("authorization", "")
    key = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not key:
        raise HTTPException(
            status_code=402,
            detail="Add your OpenRouter or Anthropic key to send this to Claude Code",
        )
    try:
        provider_of(key)
    except AnnotationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not sandbox_available():
        raise HTTPException(
            status_code=503, detail="This server cannot run Claude Code right now"
        )
    with _running_lock:
        now = time.monotonic()
        _hosted_runs[:] = [t for t in _hosted_runs if now - t < 3600]
        if len(_hosted_runs) >= HOSTED_MAX_PER_HOUR:
            raise HTTPException(
                status_code=429,
                detail="The hosted Wiki is busy; try again in a few minutes",
                headers={"Retry-After": "300"},
            )
        if len(_running) >= HOSTED_MAX_RUNNING:
            raise HTTPException(
                status_code=429,
                detail="Claude Code is answering other visitors; try again in a minute",
                headers={"Retry-After": "60"},
            )
    try:
        provider = check_key(key)
    except AnnotationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SandboxedClaude(key, provider=provider)


@router.post("/api/repos/{repo_id}/tasks")
async def create_task(repo_id: str, body: TaskIn, request: Request) -> dict:
    space = await _space(request, repo_id)
    repo = space.repo
    local_agent = body.agent in AGENTS and not space.hosted
    if (
        body.agent != "claude-code"
        and not local_agent
        and (space.hosted or not os.environ.get("CODENIB_TASK_AGENT_COMMAND"))
    ):
        raise HTTPException(
            status_code=400, detail=f"No adapter for {body.agent!r} yet"
        )
    agent: Any = None
    if space.hosted:
        agent = await asyncio.to_thread(_hosted_agent, request)

    def begin() -> dict:
        _recover(repo, space.state)
        task = dispatch(repo, body.annotations, agent=body.agent, state=space.state)
        if space.hosted:
            with _running_lock:
                _hosted_runs.append(time.monotonic())
        _start(space, task.id, agent or agent_for(body.agent))
        return task.to_json()

    try:
        return await asyncio.to_thread(begin)
    except AnnotationError as exc:
        if agent is not None:
            agent.forget_key()
        raise _fail(exc) from exc


@router.get("/api/repos/{repo_id}/tasks/{task_id}")
async def get_task(repo_id: str, task_id: str, request: Request) -> dict:
    space = await _space(request, repo_id)
    try:
        return (
            await asyncio.to_thread(TaskStore(space.repo, space.state).get, task_id)
        ).to_json()
    except AnnotationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/repos/{repo_id}/tasks/{task_id}/diff")
async def get_task_diff(repo_id: str, task_id: str, request: Request) -> dict:
    """The diff, and each hunk with whether it was accepted or discarded."""

    space = await _space(request, repo_id)
    try:
        return await asyncio.to_thread(review, space.repo, task_id, space.state)
    except AnnotationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/repos/{repo_id}/tasks/{task_id}/patch")
async def get_task_patch(repo_id: str, task_id: str, request: Request) -> Response:
    """The change, without discarded hunks, as a file for ``git apply``."""

    space = await _space(request, repo_id)
    try:
        patch = await asyncio.to_thread(kept_patch, space.repo, task_id, space.state)
    except AnnotationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not patch:
        raise HTTPException(status_code=404, detail="This task changed no code")
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", space.repo_id)[:60]
    return Response(
        patch,
        media_type="text/x-diff; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="codenib-{name}-{task_id.lower()}.patch"'
            ),
            "Cache-Control": "no-store",
        },
    )


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

    space = await _space(request, repo_id)
    action = accept if decision == "accept" else reject
    hunks = (body.hunks if body else None) or None
    try:
        return (
            await asyncio.to_thread(
                action, space.repo, task_id, hunks, space.state, space.hosted
            )
        ).to_json()
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
    space = await _space(request, repo_id)
    _local_only(space)
    repo = space.repo
    try:
        page = await asyncio.to_thread(_generated, repo_id, page_id)
        return await asyncio.to_thread(page_source, repo, page_id, page)
    except (AnnotationError, OSError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/api/repos/{repo_id}/pages/{page_id}/markdown")
async def put_page_markdown(
    repo_id: str, page_id: str, body: MarkdownIn, request: Request
) -> dict:
    space = await _space(request, repo_id)
    _local_only(space)
    repo = space.repo
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
    space = await _space(request, repo_id)
    _local_only(space)
    repo = space.repo
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

    space = await _space(request, repo_id)
    _local_only(space)
    repo = space.repo
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
