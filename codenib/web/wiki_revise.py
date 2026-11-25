# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Improve a Wiki passage from a comment, with the person's own agent.

A reader comments on words in a page and asks for the passage to be better.
The agent (Claude Code or Codex, on its own sign-in, with no tools) reads the
paragraph, the comment and the code the paragraph cites, and proposes a new
paragraph. The proposal waits in the Inbox; accepting it saves the page as a
person's edit (``.codenib/wiki/<page>.md``), rejecting it drops it. Nothing
changes in the page until the person accepts.

Proposals are files under ``.codenib/revisions/``, so they survive a restart
and travel with the repository's notes.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..annotations.edits import page_source, save_page
from ..annotations.model import AnnotationError
from . import annotations as local

router = APIRouter()

_ID = re.compile(r"\A[0-9a-f]{20}\Z")
_PAGE = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,150}\Z")
_CITE = re.compile(r"\[([ER]\d+)\]\(#evidence-\1\)")
_MAX_CODE_LINES = 60
_TIMEOUT_SECONDS = 600.0

SYSTEM = (
    "You improve one paragraph of a code Wiki for a reader. Keep it accurate "
    "to the code you are shown; never invent behaviour, names or files. Keep "
    "every citation link such as [E1](#evidence-E1) that still supports a "
    "sentence, and do not add citation ids that are not in the paragraph. "
    "Plain, concrete sentences; Markdown inline code for names."
)

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "replacement": {
            "type": "string",
            "description": "The new paragraph, in Markdown, replacing the old one",
        },
        "summary": {
            "type": "string",
            "description": "One sentence on what changed and why",
        },
    },
    "required": ["replacement", "summary"],
}


class ReviseIn(BaseModel):
    page: str = Field(min_length=1, max_length=151)
    quote: str = Field(min_length=2, max_length=600)
    comment: str = Field(min_length=1, max_length=4000)
    agent: str = "claude-code"


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _plain(markdown: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", markdown)
    return re.sub(r"[`*_>#]", "", text)


def paragraph_with(markdown: str, quote: str) -> Optional[str]:
    """The paragraph (blank-line separated block) whose text holds the quote."""

    want = _squash(quote)
    for block in re.split(r"\n\s*\n", markdown):
        if block.strip().startswith("```"):
            continue
        if want and want in _squash(_plain(block)):
            return block.strip("\n")
    return None


def _store(repo: Path) -> Path:
    return repo / ".codenib" / "revisions"


def _save(repo: Path, proposal: Dict[str, Any]) -> None:
    folder = _store(repo)
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / f"{proposal['id']}.tmp"
    temporary.write_text(json.dumps(proposal, indent=2), encoding="utf-8")
    temporary.replace(folder / f"{proposal['id']}.json")


def _load(repo: Path, revision_id: str) -> Dict[str, Any]:
    if not _ID.fullmatch(revision_id):
        raise HTTPException(status_code=404, detail="Unknown proposal")
    file = _store(repo) / f"{revision_id}.json"
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="Unknown proposal") from None


def _cited_code(repo: Path, page: Dict[str, Any], paragraph: str) -> str:
    """The lines the paragraph's citations point at, numbered."""

    items = {i.get("id"): i for i in (page.get("evidence") or {}).get("items", [])}
    parts: List[str] = []
    root = repo.resolve()
    for cite in dict.fromkeys(_CITE.findall(paragraph)):
        item = items.get(cite)
        if not item or not item.get("file"):
            continue
        path = (root / str(item["file"])).resolve()
        try:
            path.relative_to(root)
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except (ValueError, OSError):
            continue
        start = max(1, int(item.get("start_line") or 1))
        end = min(
            len(lines), int(item.get("end_line") or start), start + _MAX_CODE_LINES - 1
        )
        body = "\n".join(f"{n:>5} {lines[n - 1]}" for n in range(start, end + 1))
        parts.append(f"[{cite}] {item['file']}\n```\n{body}\n```")
        if len(parts) >= 5:
            break
    return "\n\n".join(parts)


def _complete(agent: str, prompt: str) -> Dict[str, Any]:
    if agent == "codex":
        from ..llm import codex_cli

        if not codex_cli.available():
            raise RuntimeError("Codex is not installed on this Mac")
        result = codex_cli.run(
            f"{SYSTEM}\n\n{prompt}",
            model=codex_cli.DEFAULT_MODEL,
            json_schema=SCHEMA,
            timeout=_TIMEOUT_SECONDS,
        )
    else:
        from ..llm import claude_code

        if not claude_code.available():
            raise RuntimeError("Claude Code is not installed on this Mac")
        result = claude_code.run(
            prompt,
            model=claude_code.DEFAULT_MODEL,
            system=SYSTEM,
            json_schema=SCHEMA,
            timeout=_TIMEOUT_SECONDS,
        )
    structured = result.get("structured")
    if not isinstance(structured, dict):
        try:
            structured = json.loads(result.get("text") or "")
        except ValueError:
            structured = None
    if (
        not isinstance(structured, dict)
        or not str(structured.get("replacement") or "").strip()
    ):
        raise RuntimeError("The agent returned no new paragraph")
    return structured


def _work(repo: Path, page: Dict[str, Any], proposal: Dict[str, Any]) -> None:
    code = _cited_code(repo, page, proposal["original"]) or "(none)"
    title = page.get("title") or proposal["page"]
    prompt = (
        f"Page: {title}\n\n"
        f"Paragraph:\n{proposal['original']}\n\n"
        f"The reader commented on {proposal['quote']!r}:\n{proposal['comment']}\n\n"
        f"Code the paragraph cites:\n{code}\n\n"
        "Rewrite the paragraph to answer the comment."
    )
    try:
        answer = _complete(proposal["agent"], prompt)
        proposal.update(
            status="needs_review",
            replacement=str(answer["replacement"]).strip(),
            summary=str(answer.get("summary") or "").strip(),
        )
    except Exception as exc:  # noqa: BLE001 - the proposal says why
        proposal.update(status="failed", error=str(exc)[:500])
    proposal["finished"] = time.time()
    _save(repo, proposal)


@router.post("/api/repos/{repo_id}/revisions")
async def propose(repo_id: str, body: ReviseIn, request: Request) -> dict:
    """Ask the agent for a better paragraph; it is written in the background."""

    space = await local._space(request, repo_id)
    local._local_only(space)
    if not _PAGE.fullmatch(body.page) or body.agent not in ("claude-code", "codex"):
        raise HTTPException(status_code=400, detail="Unknown page or agent")
    page = local._generated(repo_id, body.page)
    if page is None:
        raise HTTPException(status_code=404, detail="Unknown page")
    repo = Path(space.repo)
    current = page_source(repo, body.page, page)
    original = paragraph_with(str(current["markdown"]), body.quote)
    if original is None:
        raise HTTPException(
            status_code=409, detail="Those words are no longer in the page"
        )
    proposal = {
        "id": uuid.uuid4().hex[:20],
        "page": body.page,
        "page_title": page.get("title") or body.page,
        "quote": body.quote,
        "comment": body.comment,
        "agent": body.agent,
        "original": original,
        "status": "working",
        "created": time.time(),
    }
    _save(repo, proposal)
    threading.Thread(
        target=_work, args=(repo, page, dict(proposal)), daemon=True
    ).start()
    return proposal


@router.get("/api/repos/{repo_id}/revisions")
async def proposals(repo_id: str, request: Request) -> dict:
    space = await local._space(request, repo_id)
    local._local_only(space)
    folder = _store(Path(space.repo))
    found = []
    for file in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            found.append(json.loads(file.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    found.sort(key=lambda p: p.get("created", 0), reverse=True)
    return {"revisions": found}


@router.post("/api/repos/{repo_id}/revisions/{revision_id}/{decision}")
async def decide(
    repo_id: str, revision_id: str, decision: str, request: Request
) -> dict:
    """Accept (save the page with the new paragraph) or reject a proposal."""

    space = await local._space(request, repo_id)
    local._local_only(space)
    repo = Path(space.repo)
    proposal = _load(repo, revision_id)
    if decision not in ("accept", "reject"):
        raise HTTPException(status_code=404, detail="Unknown decision")
    if proposal.get("status") != "needs_review":
        raise HTTPException(
            status_code=409, detail="This proposal is not waiting for review"
        )
    if decision == "reject":
        proposal["status"] = "rejected"
        _save(repo, proposal)
        return proposal
    page = local._generated(repo_id, proposal["page"])
    current = page_source(repo, proposal["page"], page)
    markdown = str(current["markdown"])
    if proposal["original"] not in markdown:
        raise HTTPException(
            status_code=409,
            detail="The paragraph changed since this was proposed; ask again",
        )
    updated = markdown.replace(proposal["original"], proposal["replacement"], 1)
    try:
        save_page(repo, proposal["page"], page, updated, current["base"])
    except AnnotationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    proposal["status"] = "applied"
    _save(repo, proposal)
    return proposal
