# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Stable trace and context-ledger schema for agent runtime observability."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

AGENT_TRACE_SCHEMA_VERSION = 2


@dataclass
class AgentTraceEvent:
    """A replay-oriented event emitted by an agent run.

    The trace is intentionally descriptive, not prescriptive: events explain what
    happened during runtime without encoding benchmark scoring or promotion
    policy.
    """

    kind: str
    turn: int
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "turn": self.turn,
            "data": dict(self.data),
        }


@dataclass
class ContextLedgerEntry:
    """Bounded summary of context produced or consumed during a run."""

    source: str
    state: str
    turn: int
    summary: str = ""
    path: str | None = None
    tool_call_id: str | None = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "source": self.source,
            "state": self.state,
            "turn": self.turn,
            "summary": self.summary,
            "metadata": dict(self.metadata),
        }
        if self.path is not None:
            payload["path"] = self.path
        if self.tool_call_id is not None:
            payload["tool_call_id"] = self.tool_call_id
        return payload


@dataclass
class AgentRunTrace:
    """Durable event log for one ``AgentRunner.run()`` invocation."""

    events: List[AgentTraceEvent] = field(default_factory=list)
    context: List[ContextLedgerEntry] = field(default_factory=list)

    def add(self, kind: str, turn: int, **data: Any) -> AgentTraceEvent:
        event = AgentTraceEvent(kind=kind, turn=turn, data=dict(data))
        self.events.append(event)
        return event

    def add_context(
        self,
        source: str,
        state: str,
        turn: int,
        *,
        summary: str = "",
        path: str | None = None,
        tool_call_id: str | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> ContextLedgerEntry:
        entry = ContextLedgerEntry(
            source=source,
            state=state,
            turn=turn,
            summary=summary,
            path=path,
            tool_call_id=tool_call_id,
            metadata=dict(metadata or {}),
        )
        self.context.append(entry)
        return entry

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": AGENT_TRACE_SCHEMA_VERSION,
            "events": [event.to_dict() for event in self.events],
            "context": [entry.to_dict() for entry in self.context],
        }
