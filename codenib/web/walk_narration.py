# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Narrate a page's chain of calls with the person's own coding agent.

The Mac app's Walkthrough follows the longest chain of calls a page's graph
records, one function per step. Without words it is a list of functions; this
asks the local agent (Claude Code or Codex, on its own sign-in) to explain
each step from the step's code, plus a title, an overview and a recap. The
agent gets the code as text and no tools. The result is kept beside the
repository's notes, under ``.codenib/walkthroughs/``, keyed by the steps it
describes, so a changed chain is narrated again and an unchanged one opens
at once.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import annotations as local

router = APIRouter()

NARRATION_VERSION = 1
_MAX_STEP_LINES = 90
_PAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,120}")
_TIMEOUT_SECONDS = 300.0

SYSTEM = (
    "You explain code to a developer who is reading it for the first time. "
    "You are given a chain of function calls, one function per step, with "
    "each function's source. Explain only what the code shown does; never "
    "invent behaviour, names or files. Name functions in backticks. Plain, "
    "concrete sentences; no marketing words."
)

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "The question this chain answers, under 70 characters",
        },
        "overview": {
            "type": "string",
            "description": "Two or three sentences: what happens from first to last step",
        },
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": (
                            "Two to four sentences on what this function "
                            "does here and why it calls the next"
                        ),
                    },
                    "lines": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": (
                            "Up to three line numbers in this step's "
                            "code that matter most"
                        ),
                    },
                },
                "required": ["text", "lines"],
            },
        },
        "recap": {
            "type": "string",
            "description": "One or two sentences a reader should remember",
        },
    },
    "required": ["title", "overview", "steps", "recap"],
}


class StepIn(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    file: str = Field(min_length=1, max_length=500)
    line: int = Field(ge=1)
    end_line: Optional[int] = Field(default=None, ge=1)


class NarrateIn(BaseModel):
    page: str = Field(min_length=1, max_length=121)
    steps: List[StepIn] = Field(min_length=2, max_length=8)
    agent: str = "claude-code"


def _key(page: str, steps: List[StepIn]) -> str:
    text = json.dumps(
        [page, [[s.name, s.file, s.line, s.end_line] for s in steps]],
        separators=(",", ":"),
    )
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _store(repo: Path, page: str, key: str) -> Path:
    return repo / ".codenib" / "walkthroughs" / f"{page}-{key}.json"


def _read_step(repo: Path, step: StepIn) -> str:
    """The step's code with line numbers, read from inside the repository."""

    root = repo.resolve()
    path = (root / step.file).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="A step is outside the repository"
        ) from None
    if not path.is_file():
        raise HTTPException(
            status_code=400, detail=f"{step.file} is not in the repository"
        )
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    end = step.end_line if step.end_line and step.end_line >= step.line else step.line
    end = min(end, step.line + _MAX_STEP_LINES - 1, len(lines))
    return "\n".join(f"{n:>5} {lines[n - 1]}" for n in range(step.line, end + 1))


def prompt_for(repo: Path, steps: List[StepIn]) -> str:
    parts = [
        f"A chain of {len(steps)} calls. Step 1 calls step 2, step 2 calls step 3, "
        "and so on. Explain each step in order; return exactly one entry in "
        "`steps` per step.",
    ]
    for index, step in enumerate(steps, start=1):
        parts.append(
            f"## Step {index}: {step.name}\n{step.file}\n```\n{_read_step(repo, step)}\n```"
        )
    return "\n\n".join(parts)


def _complete(agent: str, prompt: str) -> Dict[str, Any]:
    if agent == "codex":
        from ..llm import codex_cli

        if not codex_cli.available():
            raise HTTPException(
                status_code=400, detail="Codex is not installed on this Mac"
            )
        result = codex_cli.run(
            f"{SYSTEM}\n\n{prompt}",
            model=codex_cli.DEFAULT_MODEL,
            json_schema=SCHEMA,
            timeout=_TIMEOUT_SECONDS,
        )
    else:
        from ..llm import claude_code

        if not claude_code.available():
            raise HTTPException(
                status_code=400, detail="Claude Code is not installed on this Mac"
            )
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
    if not isinstance(structured, dict):
        raise HTTPException(status_code=502, detail="The agent returned no walkthrough")
    return structured


def shape(raw: Dict[str, Any], steps: List[StepIn], agent: str) -> Dict[str, Any]:
    """Keep what the window shows, one text per step, and only cited lines
    that fall inside that step's code."""

    written = raw.get("steps") if isinstance(raw.get("steps"), list) else []
    if len(written) != len(steps):
        raise HTTPException(
            status_code=502,
            detail=f"The agent explained {len(written)} of {len(steps)} steps",
        )
    out_steps = []
    for step, entry in zip(steps, written, strict=True):
        entry = entry if isinstance(entry, dict) else {}
        end = (
            step.end_line if step.end_line and step.end_line >= step.line else step.line
        )
        end = min(end, step.line + _MAX_STEP_LINES - 1)
        lines = [
            int(n)
            for n in (entry.get("lines") or [])
            if isinstance(n, int) and step.line <= n <= end
        ][:3]
        out_steps.append({"text": str(entry.get("text") or "").strip(), "lines": lines})
    return {
        "version": NARRATION_VERSION,
        "agent": agent,
        "title": str(raw.get("title") or "").strip()[:120],
        "overview": str(raw.get("overview") or "").strip(),
        "steps": out_steps,
        "recap": str(raw.get("recap") or "").strip(),
    }


def _load(file: Path) -> Optional[dict]:
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return (
        data
        if isinstance(data, dict) and data.get("version") == NARRATION_VERSION
        else None
    )


@router.post("/api/repos/{repo_id}/walk-narration")
async def narration(repo_id: str, body: NarrateIn, request: Request) -> dict:
    """The narration of these steps: the saved one, or ``{"narration": null}``.

    The same body with ``?write=1`` has the agent write it when none is saved.
    """

    space = await local._space(request, repo_id)
    if space.hosted:
        raise HTTPException(
            status_code=403, detail="Walkthroughs are written locally only"
        )
    if not _PAGE.fullmatch(body.page):
        raise HTTPException(status_code=400, detail="Unknown page")
    if body.agent not in ("claude-code", "codex"):
        raise HTTPException(status_code=400, detail=f"No adapter for {body.agent!r}")
    repo = Path(space.repo)
    file = _store(repo, body.page, _key(body.page, body.steps))
    saved = await asyncio.to_thread(_load, file)
    if saved is not None or request.query_params.get("write") != "1":
        return {"narration": saved}
    prompt = await asyncio.to_thread(prompt_for, repo, body.steps)
    raw = await asyncio.to_thread(_complete, body.agent, prompt)
    narrated = shape(raw, body.steps, body.agent)
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_suffix(".tmp")
    temporary.write_text(json.dumps(narrated, indent=2), encoding="utf-8")
    temporary.replace(file)
    return {"narration": narrated}
