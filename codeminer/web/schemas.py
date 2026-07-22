# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Request/response models for the demo API.

Also holds the mapping from an ``AgentResult`` (codeminer's agent output) to the
flat, UI-friendly ``ChatResponse`` the Next.js frontend renders.
"""

from __future__ import annotations

import os
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field

from codeminer.agent.boundary import to_agent_repr


class RepoInfo(BaseModel):
    """A repository the demo can answer questions about.

    ``id`` is the dataset ``instance_id``; ``repo`` @ ``commit_short`` identifies
    the exact snapshot that was indexed.
    """

    id: str
    name: str
    repo: str = ""
    base_commit: str = ""
    commit_short: str = ""
    language: str = ""
    description: str = ""  # repo purpose (README-derived), not the issue text
    problem_statement: str = ""
    languages: List[str] = Field(default_factory=list)
    file_count: int = 0
    capabilities: dict[str, bool] = Field(default_factory=dict)


class CallSite(BaseModel):
    """An exact call site (1-based line), mirroring the frontend ``CallSite``."""

    file: str = ""
    line: Optional[int] = None


class EdgeEndpoint(BaseModel):
    """One end of a graph edge, identified by source location.

    The frontend has each node's ``file`` + 1-based ``line``/``end_line`` (from
    the codemap payload) but NOT the graph's internal symbol identity, so an edge
    is addressed by (file, line span) rather than a symbol name.
    """

    file: str
    line: Optional[int] = None  # 1-based start
    end_line: Optional[int] = None  # 1-based end
    label: str = ""  # display name, for the prompt only (not identity)


class EdgeLabelRequest(BaseModel):
    """Ask for a short dependency phrase describing how ``source`` uses ``target``."""

    source: EdgeEndpoint
    target: EdgeEndpoint
    # Exact call sites where source references target (1-based), if known.
    anchors: List[CallSite] = Field(default_factory=list)


class EdgeLabelResponse(BaseModel):
    label: str = ""  # e.g. "validates user input"; "" when unavailable/disabled
    cached: bool = False
    disabled: bool = False


class ChatMessage(BaseModel):
    """One conversation message (text only — citations stay client-side)."""

    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    repo_id: str
    # Full conversation, oldest first; the last message is the current question
    # and must be from the user (OpenAI/DeepWiki-style). Earlier messages give
    # the agent context for follow-ups.
    messages: List[ChatMessage]


class Citation(BaseModel):
    """A code reference backing the answer, rendered as a card in the UI.

    Line numbers are 1-based at this API boundary, matching the agent-facing
    representation and the ``/source`` endpoint.
    """

    file: Optional[str] = None
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    node_name: str = ""
    type: str = ""
    score: Optional[float] = None
    content: Optional[str] = None


class ToolCallInfo(BaseModel):
    skill_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result_count: int = 0
    duration_ms: float = 0.0
    error: Optional[str] = None


class ChatResponse(BaseModel):
    answer: str
    citations: List[Citation] = Field(default_factory=list)
    tool_calls: List[ToolCallInfo] = Field(default_factory=list)
    total_turns: int = 0
    total_duration_ms: float = 0.0


def _repo_relative(path: Optional[str], repo_path: str = "") -> Optional[str]:
    """Make a source path repo-relative for display + ``/source`` lookup.

    Indexes store build-machine-absolute paths under arbitrary roots
    (``/mnt/.../repo/...``, ``/home/.../.codeminer/<repo>/...``). The robust
    rule (mirrors ``WikiBuilder._index_root``): the repo-relative path is the
    longest suffix of *path* that actually exists under *repo_path*. Falls back
    to stripping the repo root / a ``/repo/`` ancestor / a leading slash.
    """
    if not path:
        return path
    p = path.replace("\\", "/")
    if repo_path:
        root = repo_path.replace("\\", "/").rstrip("/") + "/"
        if p.startswith(root):
            return p[len(root) :]
        parts = [x for x in p.split("/") if x]
        for i in range(len(parts)):
            rel = "/".join(parts[i:])
            if os.path.exists(os.path.join(repo_path, rel)):
                return rel
    marker = "/repo/"
    idx = p.rfind(marker)
    if idx != -1:
        return p[idx + len(marker) :]
    return p.lstrip("/")


def _node_to_citation(node: Any, repo_path: str = "") -> Optional[Citation]:
    """Coerce a single retrieval result (QueriedNode / dict) into a Citation."""
    if not (
        hasattr(node, "model_dump")
        or isinstance(node, dict)
        or hasattr(node, "__dict__")
    ):
        return None

    # Retrieval/tool results keep internal 0-based lines; API responses are an
    # agent-facing boundary and expose 1-based locations.
    data = to_agent_repr(node)
    if not (data.get("file") or data.get("node_name")):
        return None
    content = data.get("content")
    if isinstance(content, str) and len(content) > 2000:
        content = content[:2000] + "\n... (truncated)"
    return Citation(
        file=_repo_relative(data.get("file"), repo_path),
        start_line=data.get("start_line"),
        end_line=data.get("end_line"),
        node_name=data.get("node_name") or data.get("name") or "",
        type=data.get("type") or "",
        score=data.get("score"),
        content=content,
    )


def agent_result_to_response(result: Any, repo_path: str = "") -> ChatResponse:
    """Flatten an ``AgentResult`` into the API response.

    Citations are de-duplicated across all tool calls by (file, start_line,
    end_line) so a node retrieved by several searches appears once. ``repo_path``
    (when given) makes citation file paths repo-relative.
    """
    tool_calls: List[ToolCallInfo] = []
    citations: List[Citation] = []
    seen: set[tuple] = set()

    for tc in result.tool_calls:
        nodes = tc.result if isinstance(tc.result, (list, tuple)) else []
        tool_calls.append(
            ToolCallInfo(
                skill_id=tc.skill_id,
                arguments=tc.arguments or {},
                result_count=len(nodes),
                duration_ms=tc.duration_ms,
                error=tc.error,
            )
        )
        for node in nodes:
            cit = _node_to_citation(node, repo_path)
            if cit is None:
                continue
            key = (cit.file, cit.start_line, cit.end_line)
            if key in seen:
                continue
            seen.add(key)
            citations.append(cit)

    return ChatResponse(
        answer=result.answer or "",
        citations=citations,
        tool_calls=tool_calls,
        total_turns=result.total_turns,
        total_duration_ms=result.total_duration_ms,
    )


class FeedbackRequest(BaseModel):
    """Anonymous visitor feedback. No identifying fields by design."""

    message: str
    # UI category; unknown values are stored as "other" rather than rejected.
    category: str = "other"
    # Optional context so a report can be located: which repo/page it came from.
    repo: Optional[str] = None
    page: Optional[str] = None


class FeedbackResponse(BaseModel):
    ok: bool = True
    id: str = ""
    error: str = ""
