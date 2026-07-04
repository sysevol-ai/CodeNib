# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Stable trace event schema for agent runtime observability."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

AGENT_TRACE_SCHEMA_VERSION = 1


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
class AgentRunTrace:
    """Durable event log for one ``AgentRunner.run()`` invocation."""

    events: List[AgentTraceEvent] = field(default_factory=list)

    def add(self, kind: str, turn: int, **data: Any) -> AgentTraceEvent:
        event = AgentTraceEvent(kind=kind, turn=turn, data=dict(data))
        self.events.append(event)
        return event

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": AGENT_TRACE_SCHEMA_VERSION,
            "events": [event.to_dict() for event in self.events],
        }
