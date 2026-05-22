# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Request/response models for the demo API.

Also holds the mapping from an ``AgentResult`` (codeminer's agent output) to the
flat, UI-friendly ``ChatResponse`` the Next.js frontend renders.
"""

from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, Field


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
    problem_statement: str = ""
    languages: List[str] = Field(default_factory=list)
    file_count: int = 0
    capabilities: dict[str, bool] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    repo_id: str
    query: str


class Citation(BaseModel):
    """A code reference backing the answer, rendered as a card in the UI."""

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


def _node_to_citation(node: Any) -> Optional[Citation]:
    """Coerce a single retrieval result (QueriedNode / dict) into a Citation."""
    if hasattr(node, "model_dump"):
        data = node.model_dump()
    elif isinstance(node, dict):
        data = node
    else:
        return None
    if not (data.get("file") or data.get("node_name")):
        return None
    content = data.get("content")
    if isinstance(content, str) and len(content) > 2000:
        content = content[:2000] + "\n... (truncated)"
    return Citation(
        file=data.get("file"),
        start_line=data.get("start_line"),
        end_line=data.get("end_line"),
        node_name=data.get("node_name") or data.get("name") or "",
        type=data.get("type") or "",
        score=data.get("score"),
        content=content,
    )


def agent_result_to_response(result: Any) -> ChatResponse:
    """Flatten an ``AgentResult`` into the API response.

    Citations are de-duplicated across all tool calls by (file, start_line,
    end_line) so a node retrieved by several searches appears once.
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
            cit = _node_to_citation(node)
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
