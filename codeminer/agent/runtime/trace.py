# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Stable trace schema for agent runtime observability."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List

from .context import ContextLedger, ContextLedgerEntry

AGENT_TRACE_SCHEMA_VERSION = 4


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
    timestamp_ms: float | None = None

    def to_dict(self) -> Dict[str, Any]:
        out = {
            "kind": self.kind,
            "turn": self.turn,
            "data": dict(self.data),
        }
        if self.timestamp_ms is not None:
            out["timestamp_ms"] = self.timestamp_ms
        return out


@dataclass
class AgentRunTrace:
    """Durable event log for one ``AgentRunner.run()`` invocation."""

    events: List[AgentTraceEvent] = field(default_factory=list)
    context: ContextLedger = field(default_factory=ContextLedger)
    start_monotonic: float = field(default_factory=time.monotonic, repr=False)

    def add(self, kind: str, turn: int, **data: Any) -> AgentTraceEvent:
        event = AgentTraceEvent(
            kind=kind,
            turn=turn,
            data=dict(data),
            timestamp_ms=(time.monotonic() - self.start_monotonic) * 1000,
        )
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
        entry_id: str | None = None,
        provenance: Dict[str, Any] | None = None,
        freshness: str | None = None,
        token_estimate: int | None = None,
        cost_estimate: float | None = None,
        consumed_by: List[str] | None = None,
        consumed_turn: int | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> ContextLedgerEntry:
        return self.context.add(
            source=source,
            state=state,
            turn=turn,
            summary=summary,
            path=path,
            tool_call_id=tool_call_id,
            entry_id=entry_id,
            provenance=provenance,
            freshness=freshness,
            token_estimate=token_estimate,
            cost_estimate=cost_estimate,
            consumed_by=consumed_by,
            consumed_turn=consumed_turn,
            metadata=metadata,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": AGENT_TRACE_SCHEMA_VERSION,
            "events": [event.to_dict() for event in self.events],
            "context": self.context.to_list(),
        }
