# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Request/response models for the demo API.

Also holds the mapping from an ``AgentResult`` (codenib's agent output) to the
flat, UI-friendly ``ChatResponse`` the Wiki frontend renders.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Any, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from codenib.agent.boundary import to_agent_repr

from ..repository_filters import repository_path_is_visible
from ..repository_source_selection import RepositorySourceSelection
from .repository_files import (
    bound_source_slice,
    git_source_slice,
    has_git_metadata,
    live_source_slice,
)

if TYPE_CHECKING:
    from ..source_fingerprint import RepositorySourceReader

_MAX_REPO_ID_CHARS = 512
_MAX_PATH_CHARS = 4096
_MAX_EDGE_LABEL_CHARS = 1024
_MAX_EDGE_ANCHORS = 32
_MAX_CHAT_MESSAGES = 128
_MAX_CHAT_MESSAGE_CHARS = 64 * 1024
_MAX_CHAT_TOTAL_CHARS = 256 * 1024
_MAX_CITATION_CONTENT_CHARS = 16 * 1024


class WindowStats(BaseModel):
    """Cold-vs-patched cost for a repo's commit window.

    Derived once, in ``commit_window.window_stats``. ``speedup`` is the cold
    graph-build time divided by mean warm patch time, not an end-to-end re-index
    ratio. It is ``None`` when no defensible ratio exists (no cold anchor, no
    patched transitions, or a zero denominator) -- surfaces must then make no
    claim rather than substituting a default.
    """

    commit_count: int = 0
    patched_count: int = 0
    cold_seconds: float | None = None
    mean_patch_seconds: float | None = None
    speedup: float | None = None


class GraphCoverage(BaseModel):
    """Language providers represented by the current symbol graph artifact."""

    available_languages: List[str] = Field(default_factory=list)
    unavailable_languages: List[str] = Field(default_factory=list)
    partial: bool = False


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
    graph_coverage: GraphCoverage | None = None
    # Present only for repos with a prebuilt commit window; absent otherwise, so
    # the landing page keeps its single-commit label.
    incremental: WindowStats | None = None


class IndexUpdateMetrics(BaseModel):
    """Bounded metrics retained from the most recent index generation."""

    changed_files: Optional[int] = Field(default=None, ge=0)
    chunks_reembedded: Optional[int] = Field(default=None, ge=0)
    chunks_from_cache: Optional[int] = Field(default=None, ge=0)
    cache_hit_rate: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    new_commit: Optional[str] = Field(default=None, max_length=128)


class IndexSurfaceStatus(BaseModel):
    """Reader-facing state for one primary repository index surface."""

    index_type: Literal["bm25", "vector", "symbol_graph"]
    state: Literal["built", "missing", "stale", "updating", "failed"]
    stale: bool = False
    indexed_commit: Optional[str] = Field(default=None, max_length=128)
    built_at: Optional[str] = Field(default=None, max_length=128)
    update_mode: Literal["incremental", "patch", "rebuild", "unavailable"]
    updates_enabled: bool = False
    update_reason: str = Field(default="", max_length=512)
    job_id: Optional[str] = Field(default=None, max_length=256)
    metrics: Optional[IndexUpdateMetrics] = None


class RepoIndexStatus(BaseModel):
    """Exactly three primary index surfaces for one repository generation."""

    repo_id: str = Field(min_length=1, max_length=_MAX_REPO_ID_CHARS)
    last_indexed_commit: Optional[str] = Field(default=None, max_length=128)
    current_head: Optional[str] = Field(default=None, max_length=128)
    stale: bool = False
    indexes: List[IndexSurfaceStatus] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def _require_primary_surfaces(self) -> "RepoIndexStatus":
        observed = tuple(index.index_type for index in self.indexes)
        expected = ("bm25", "vector", "symbol_graph")
        if observed != expected:
            raise ValueError("index status must contain the three primary surfaces")
        return self


class IndexJobSurface(BaseModel):
    """One primary index requested by a durable job."""

    index_type: Literal["bm25", "vector", "symbol_graph"]
    requested_mode: Literal["auto", "full", "incremental"]
    required: bool


class IndexJobCreateRequest(BaseModel):
    """Bounded user intent for one durable repository index update."""

    indexes: List[Literal["bm25", "vector", "symbol_graph"]] = Field(
        min_length=1,
        max_length=3,
    )
    mode: Literal["full", "incremental"]
    force: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def _require_unique_indexes(self) -> "IndexJobCreateRequest":
        if len(set(self.indexes)) != len(self.indexes):
            raise ValueError("index job request surfaces must be unique")
        return self


class IndexJobEvent(BaseModel):
    """Bounded progress with a Web-owned key and no worker/fencing authority."""

    sequence: int = Field(ge=1)
    attempt_count: int = Field(ge=1, le=1_000)
    event_key: str = Field(min_length=1, max_length=128)
    kind: Literal["progress", "view_result"]
    index_type: Optional[Literal["bm25", "vector", "symbol_graph"]] = None
    effective_mode: Optional[
        Literal["full", "incremental", "rebuild_fallback", "unavailable"]
    ] = None
    outcome: Optional[Literal["succeeded", "failed", "skipped"]] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _require_event_shape(self) -> "IndexJobEvent":
        result_fields = (self.effective_mode, self.outcome)
        if self.kind == "progress" and any(
            value is not None for value in result_fields
        ):
            raise ValueError("progress events cannot carry a view result")
        if self.kind == "view_result" and any(
            value is None
            for value in (self.index_type, self.effective_mode, self.outcome)
        ):
            raise ValueError("view-result events require view, mode, and outcome")
        return self


class IndexJobStatusResponse(BaseModel):
    """Detached, reader-facing durable index-job state."""

    job_id: str = Field(min_length=1, max_length=80)
    repo_id: str = Field(min_length=1, max_length=_MAX_REPO_ID_CHARS)
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    cancel_requested: bool
    attempt_count: int = Field(ge=0, le=1_000)
    max_attempts: int = Field(ge=1, le=1_000)
    indexes: List[IndexJobSurface] = Field(min_length=1, max_length=3)
    result_snapshot_id: Optional[str] = Field(default=None, max_length=96)
    error_code: Optional[str] = Field(default=None, max_length=128)
    error_message: Optional[str] = Field(default=None, max_length=512)
    created_at_ms: int = Field(ge=0)
    updated_at_ms: int = Field(ge=0)
    started_at_ms: Optional[int] = Field(default=None, ge=0)
    finished_at_ms: Optional[int] = Field(default=None, ge=0)
    events: List[IndexJobEvent] = Field(default_factory=list, max_length=64)
    next_event_sequence: int = Field(ge=0)

    @model_validator(mode="after")
    def _require_canonical_job_status(self) -> "IndexJobStatusResponse":
        order = {"bm25": 0, "vector": 1, "symbol_graph": 2}
        observed = [surface.index_type for surface in self.indexes]
        if len(set(observed)) != len(observed) or observed != sorted(
            observed,
            key=order.__getitem__,
        ):
            raise ValueError("index job surfaces must be unique and canonical")
        sequences = [event.sequence for event in self.events]
        if sequences != sorted(set(sequences)):
            raise ValueError("index job events must have increasing unique sequences")
        if sequences and self.next_event_sequence != sequences[-1]:
            raise ValueError("index job event cursor must match the final event")
        return self


class CallSite(BaseModel):
    """An exact call site (1-based line), mirroring the frontend ``CallSite``."""

    file: str = Field(default="", max_length=_MAX_PATH_CHARS)
    line: Optional[int] = Field(default=None, ge=1)


class EdgeEndpoint(BaseModel):
    """One end of a graph edge, identified by source location.

    The frontend has each node's ``file`` + 1-based ``line``/``end_line`` (from
    the codemap payload) but NOT the graph's internal symbol identity, so an edge
    is addressed by (file, line span) rather than a symbol name.
    """

    file: str = Field(max_length=_MAX_PATH_CHARS)
    line: Optional[int] = Field(default=None, ge=1)  # 1-based start
    end_line: Optional[int] = Field(default=None, ge=1)  # 1-based end
    label: str = Field(default="", max_length=_MAX_EDGE_LABEL_CHARS)  # display only


class EdgeLabelRequest(BaseModel):
    """Ask for a short dependency phrase describing how ``source`` uses ``target``."""

    source: EdgeEndpoint
    target: EdgeEndpoint
    commit: Optional[str] = Field(default=None, max_length=128)
    # Exact call sites where source references target (1-based), if known.
    anchors: List[CallSite] = Field(
        default_factory=list,
        max_length=_MAX_EDGE_ANCHORS,
    )


class EdgeLabelResponse(BaseModel):
    label: str = ""  # e.g. "validates user input"; "" when unavailable/disabled
    cached: bool = False
    disabled: bool = False


class ChatMessage(BaseModel):
    """One conversation message (text only — citations stay client-side)."""

    role: Literal["user", "assistant"]
    content: str = Field(max_length=_MAX_CHAT_MESSAGE_CHARS)


class ChatRequest(BaseModel):
    repo_id: str = Field(min_length=1, max_length=_MAX_REPO_ID_CHARS)
    # Full conversation, oldest first; the last message is the current question
    # and must be from the user (OpenAI/DeepWiki-style). Earlier messages give
    # the agent context for follow-ups.
    messages: List[ChatMessage] = Field(
        min_length=1,
        max_length=_MAX_CHAT_MESSAGES,
    )

    @model_validator(mode="after")
    def _bound_conversation(self) -> "ChatRequest":
        if (
            sum(len(message.content) for message in self.messages)
            > _MAX_CHAT_TOTAL_CHARS
        ):
            raise ValueError("conversation exceeds the CodeNib chat context limit")
        return self


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
    (``/workspace/repo/...``, ``~/.codenib/<repo>/...``). The robust
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


def _node_to_citation(
    node: Any,
    repo_path: str = "",
    *,
    source_reader: "RepositorySourceReader | None" = None,
) -> Optional[Citation]:
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
    file = data.get("file")
    if source_reader is not None and file:
        file = source_reader.captured_relative_path(file)
        if file is None:
            return None
    else:
        file = _repo_relative(file, repo_path)
    return Citation(
        file=file,
        start_line=data.get("start_line"),
        end_line=data.get("end_line"),
        node_name=data.get("node_name") or data.get("name") or "",
        type=data.get("type") or "",
        score=data.get("score"),
        content=content,
    )


def _symbol_labels(node_name: str) -> set[str]:
    """Return answer-facing symbol spellings from an indexed node identity."""

    symbol = (node_name or "").rsplit(":", 1)[-1].strip()
    if not symbol:
        return set()
    without_args = re.sub(r"\([^)]*\)$", "", symbol)
    labels = {symbol, without_args}
    leaf = without_args.rsplit(".", 1)[-1]
    if leaf:
        labels.add(leaf)
        labels.add(f"{leaf}()")
    return {label for label in labels if len(label) >= 3}


def _answer_names_symbol(answer: str, node_name: str) -> bool:
    folded = answer.casefold()
    for label in _symbol_labels(node_name):
        candidate = label.casefold()
        if f"`{candidate}`" in folded:
            return True
        plain_identifier = re.sub(r"\(\)$", "", label)
        distinctive = (
            "_" in plain_identifier
            or "." in plain_identifier
            or "::" in plain_identifier
            or any(character.isupper() for character in label)
        )
        if not distinctive:
            continue
        if re.search(
            rf"(?<![\w.]){re.escape(candidate)}(?![\w.])",
            folded,
        ):
            return True
    return False


def _answer_names_file(answer: str, file: str) -> bool:
    candidate = (file or "").casefold()
    folded = (answer or "").casefold()
    if not candidate:
        return False
    if f"`{candidate}`" in folded:
        return True
    return bool(
        re.search(
            rf"(?<![\w/.-]){re.escape(candidate)}(?![\w/.-])",
            folded,
        )
    )


def _select_answer_citations(
    answer: str,
    citations: List[Citation],
    *,
    limit: int = 5,
    repo_path: str = "",
    repo_commit: str = "",
    source_reader: "RepositorySourceReader | None" = None,
    source_selection: RepositorySourceSelection | None = None,
) -> List[Citation]:
    """Keep strong answer citations that resolve to exact repository source."""

    symbol_matches = [
        citation
        for citation in citations
        if _answer_names_symbol(answer, citation.node_name)
    ]
    selected = list(symbol_matches)
    selected_files = {citation.file for citation in selected if citation.file}

    for citation in citations:
        file = citation.file
        if not file or file in selected_files or not _answer_names_file(answer, file):
            continue
        selected.append(citation)
        selected_files.add(file)

    if not selected:
        selected = citations

    renderable: List[Citation] = []
    for citation in selected:
        if source_reader is not None:
            if not citation.file or citation.start_line is None:
                continue
            source = bound_source_slice(
                source_reader,
                citation.file,
                citation.start_line,
                citation.end_line or citation.start_line,
            )
            if not source or not str(source.get("content") or "").strip():
                continue
            content = str(source["content"])
            if len(content) > _MAX_CITATION_CONTENT_CHARS:
                content = content[:_MAX_CITATION_CONTENT_CHARS] + "\n... (truncated)"
            citation = citation.model_copy(
                update={
                    "file": source.get("file") or citation.file,
                    "start_line": source.get("start_line") or citation.start_line,
                    "end_line": source.get("end_line") or citation.end_line,
                    "content": content,
                }
            )
        elif repo_path:
            if not citation.file or citation.start_line is None:
                continue
            if repo_commit:
                historical_selection = (
                    source_selection
                    if type(source_selection) is RepositorySourceSelection
                    else RepositorySourceSelection()
                )
                if not repository_path_is_visible(
                    citation.file,
                    selection=historical_selection,
                ):
                    continue
                source = git_source_slice(
                    repo_path,
                    repo_commit,
                    citation.file,
                    citation.start_line,
                    citation.end_line or citation.start_line,
                )
                if source is None:
                    # Read-only prebuilt layouts carry an indexed commit but do
                    # not necessarily include Git metadata.  In that case the
                    # retrieval result is the authenticated snapshot payload;
                    # retain it instead of reading the mutable live path or
                    # dropping the citation altogether.
                    if has_git_metadata(repo_path):
                        continue
                    if (
                        not isinstance(citation.content, str)
                        or not citation.content.strip()
                    ):
                        continue
                    renderable.append(citation)
                    if len(renderable) >= limit:
                        break
                    continue
            else:
                source = live_source_slice(
                    repo_path,
                    citation.file,
                    citation.start_line,
                    citation.end_line or citation.start_line,
                )
            if not source or not str(source.get("content") or "").strip():
                continue
            content = str(source["content"])
            if len(content) > _MAX_CITATION_CONTENT_CHARS:
                content = content[:_MAX_CITATION_CONTENT_CHARS] + "\n... (truncated)"
            citation = citation.model_copy(
                update={
                    "file": source.get("file") or citation.file,
                    "start_line": source.get("start_line") or citation.start_line,
                    "end_line": source.get("end_line") or citation.end_line,
                    "content": content,
                }
            )
        renderable.append(citation)
        if len(renderable) >= limit:
            break
    return renderable


def agent_result_to_response(
    result: Any,
    repo_path: str = "",
    repo_commit: str = "",
    *,
    source_reader: "RepositorySourceReader | None" = None,
    source_selection: RepositorySourceSelection | None = None,
) -> ChatResponse:
    """Flatten an ``AgentResult`` into the API response.

    Retrieved locations are de-duplicated and narrowed to the strongest files
    or symbols named in the final answer. ``repo_path`` (when given) makes
    citation file paths repo-relative.
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
            cit = _node_to_citation(
                node,
                repo_path,
                source_reader=source_reader,
            )
            if cit is None:
                continue
            key = (cit.file, cit.start_line, cit.end_line)
            if key in seen:
                continue
            seen.add(key)
            citations.append(cit)

    return ChatResponse(
        answer=result.answer or "",
        citations=_select_answer_citations(
            result.answer or "",
            citations,
            repo_path=repo_path,
            repo_commit=repo_commit,
            source_reader=source_reader,
            source_selection=source_selection,
        ),
        tool_calls=tool_calls,
        total_turns=result.total_turns,
        total_duration_ms=result.total_duration_ms,
    )
