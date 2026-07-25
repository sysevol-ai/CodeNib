# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Typed exits for the Guardian L2 cycle loop."""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional


class CycleInterrupt(Exception):  # noqa: B042 - structured typed exit metadata
    """Base class for all intentional L2 loop exits."""

    def __init__(  # noqa: B042 - typed exits carry structured metadata
        self,
        reason: str,
        *,
        decision_log_tail: Optional[Iterable[dict]] = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.decision_log_tail = list(decision_log_tail or [])

    def as_record(self) -> Dict[str, Any]:
        return {
            "event": "exit",
            "exit_type": type(self).__name__,
            "reason": self.reason,
            "decision_log_tail": self.decision_log_tail,
        }


class ReportSubmitted(CycleInterrupt):
    """The agent judged the cycle complete."""

    def __init__(
        self,
        summary: str,
        *,
        decision_log_tail: Optional[Iterable[dict]] = None,
    ) -> None:
        super().__init__(
            summary or "Report submitted.", decision_log_tail=decision_log_tail
        )
        self.summary = summary


class NoProgress(CycleInterrupt):
    """A model turn neither called a tool nor submitted a report."""


class BudgetExceeded(CycleInterrupt):
    """The cycle-wide token ledger reached its hard limit."""


class WallClockExceeded(BudgetExceeded):
    """The cycle-wide wall-clock limit was reached."""


class Degraded(CycleInterrupt):
    """The model was unavailable and a deterministic fallback ran."""


class StateInconsistent(CycleInterrupt):
    """A carried-state invariant was violated; state must not be persisted."""
