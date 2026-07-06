# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Context ledger contract for agent runtime observability."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Sequence


@dataclass
class ContextLedgerEntry:
    """Bounded summary of context produced, retained, or consumed by a run."""

    source: str
    state: str
    turn: int
    summary: str = ""
    path: str | None = None
    tool_call_id: str | None = None
    entry_id: str | None = None
    provenance: Dict[str, Any] = field(default_factory=dict)
    freshness: str | None = None
    token_estimate: int | None = None
    cost_estimate: float | None = None
    consumed_by: List[str] = field(default_factory=list)
    consumed_turn: int | None = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.turn < 0:
            raise ValueError("turn must be non-negative")
        if self.consumed_turn is not None and self.consumed_turn < 0:
            raise ValueError("consumed_turn must be non-negative when set")
        if self.token_estimate is not None and self.token_estimate < 0:
            raise ValueError("token_estimate must be non-negative when set")
        if self.cost_estimate is not None and self.cost_estimate < 0:
            raise ValueError("cost_estimate must be non-negative when set")
        self.provenance = dict(self.provenance or {})
        self.metadata = dict(self.metadata or {})
        self.consumed_by = list(self.consumed_by or [])

    def mark_consumed(self, *, turn: int, consumer: str) -> None:
        """Record that this context entry was consumed by a later runtime step."""

        if turn < 0:
            raise ValueError("turn must be non-negative")
        self.state = "consumed"
        self.consumed_turn = turn
        if consumer and consumer not in self.consumed_by:
            self.consumed_by.append(consumer)

    def mark_expired(self, *, turn: int, reason: str | None = None) -> None:
        """Record that this context entry left the active working set."""

        if turn < 0:
            raise ValueError("turn must be non-negative")
        self.state = "expired"
        self.consumed_turn = turn
        if reason:
            self.metadata["expiration_reason"] = reason

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
        if self.entry_id is not None:
            payload["entry_id"] = self.entry_id
        if self.provenance:
            payload["provenance"] = dict(self.provenance)
        if self.freshness is not None:
            payload["freshness"] = self.freshness
        if self.token_estimate is not None:
            payload["token_estimate"] = self.token_estimate
        if self.cost_estimate is not None:
            payload["cost_estimate"] = self.cost_estimate
        if self.consumed_by:
            payload["consumed_by"] = list(self.consumed_by)
        if self.consumed_turn is not None:
            payload["consumed_turn"] = self.consumed_turn
        return payload


class ContextLedger:
    """Append-only collection of context ledger entries for one agent run."""

    def __init__(self, entries: Sequence[ContextLedgerEntry] | None = None) -> None:
        self._entries: List[ContextLedgerEntry] = list(entries or [])

    def __iter__(self) -> Iterator[ContextLedgerEntry]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __getitem__(self, index: int) -> ContextLedgerEntry:
        return self._entries[index]

    @property
    def entries(self) -> tuple[ContextLedgerEntry, ...]:
        return tuple(self._entries)

    def add(
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
        consumed_by: Sequence[str] | None = None,
        consumed_turn: int | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> ContextLedgerEntry:
        entry = ContextLedgerEntry(
            source=source,
            state=state,
            turn=turn,
            summary=summary,
            path=path,
            tool_call_id=tool_call_id,
            entry_id=entry_id or f"context_{len(self._entries) + 1}",
            provenance=dict(provenance or {}),
            freshness=freshness,
            token_estimate=token_estimate,
            cost_estimate=cost_estimate,
            consumed_by=list(consumed_by or []),
            consumed_turn=consumed_turn,
            metadata=dict(metadata or {}),
        )
        self._entries.append(entry)
        return entry

    def by_state(self, state: str) -> List[ContextLedgerEntry]:
        return [entry for entry in self._entries if entry.state == state]

    def find(self, entry_id: str) -> ContextLedgerEntry | None:
        for entry in self._entries:
            if entry.entry_id == entry_id:
                return entry
        return None

    def mark_consumed(
        self,
        entry_id: str,
        *,
        turn: int,
        consumer: str,
    ) -> ContextLedgerEntry:
        entry = self.find(entry_id)
        if entry is None:
            raise KeyError(f"Unknown context ledger entry: {entry_id}")
        entry.mark_consumed(turn=turn, consumer=consumer)
        return entry

    def mark_expired(
        self,
        entry_id: str,
        *,
        turn: int,
        reason: str | None = None,
    ) -> ContextLedgerEntry:
        entry = self.find(entry_id)
        if entry is None:
            raise KeyError(f"Unknown context ledger entry: {entry_id}")
        entry.mark_expired(turn=turn, reason=reason)
        return entry

    def to_list(self) -> List[Dict[str, Any]]:
        return [entry.to_dict() for entry in self._entries]


__all__ = ["ContextLedger", "ContextLedgerEntry"]
