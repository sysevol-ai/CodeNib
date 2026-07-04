# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Data types for the agent runner."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..llm.usage import TokenUsage, UsageRecord

TRACE_SCHEMA_VERSION = 2


@dataclass
class ToolResultEnvelope:
    """Uniform metadata envelope for tool results in traces and ledgers."""

    status: str
    result_type: str
    result_chars: int = 0
    duration_ms: float = 0.0
    result_count: Optional[int] = None
    truncated: bool = False
    error_kind: Optional[str] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "status": self.status,
            "result_type": self.result_type,
            "result_chars": self.result_chars,
            "duration_ms": self.duration_ms,
            "truncated": self.truncated,
        }
        if self.result_count is not None:
            payload["result_count"] = self.result_count
        if self.error_kind:
            payload["error_kind"] = self.error_kind
        if self.error:
            payload["error"] = self.error
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload


@dataclass
class ToolCallRecord:
    """Record of a single tool invocation during an agent run."""

    tool_call_id: str
    skill_id: str
    arguments: Dict[str, Any]
    result: Any = None
    duration_ms: float = 0.0
    error: Optional[str] = None
    envelope: Optional[ToolResultEnvelope] = None


@dataclass
class AgentTraceEvent:
    """Replay-oriented event emitted by an agent run."""

    kind: str
    turn: int
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ContextLedgerEntry:
    """Context item offered to, read by, or derived during an agent run."""

    source: str
    state: str
    path: Optional[str] = None
    tool_call_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentRunTrace:
    """Durable run trace for audit and experiment stratification."""

    events: List[AgentTraceEvent] = field(default_factory=list)
    context: List[ContextLedgerEntry] = field(default_factory=list)
    message_replay: List[Dict[str, Any]] = field(default_factory=list)
    tool_schema_replay: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": TRACE_SCHEMA_VERSION,
            "events": [
                {"kind": e.kind, "turn": e.turn, "data": e.data} for e in self.events
            ],
            "context": [
                {
                    "source": c.source,
                    "state": c.state,
                    "path": c.path,
                    "tool_call_id": c.tool_call_id,
                    "metadata": c.metadata,
                }
                for c in self.context
            ],
            "message_replay": self.message_replay,
            "tool_schema_replay": self.tool_schema_replay,
        }


@dataclass
class AgentResult:
    """Outcome of ``AgentRunner.run()``."""

    answer: str
    tool_calls: List[ToolCallRecord] = field(default_factory=list)
    messages: List[Dict[str, Any]] = field(default_factory=list)
    total_turns: int = 0
    total_duration_ms: float = 0.0
    usage: Optional[TokenUsage] = None
    usage_records: List[UsageRecord] = field(default_factory=list)
    trace: Optional[AgentRunTrace] = None
