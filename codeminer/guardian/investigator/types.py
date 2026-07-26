# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Typed contracts for one Repository Guardian investigation run."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from ...agent.agent_types import ToolCallRecord
from ...llm.usage import TokenUsage


class ProcessStatus(str, Enum):
    """How a probe subprocess terminated."""

    EXITED = "exited"
    SIGNALED = "signaled"
    TIMED_OUT = "timed_out"
    SPAWN_FAILED = "spawn_failed"
    SANDBOX_FAILED = "sandbox_failed"


class TestStatus(str, Enum):
    """Per-test outcome parsed from a machine-readable test report."""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class SourceExcerpt:
    """A bounded source span already known to L2 or read by L3."""

    path: str
    start_line: int
    end_line: int
    content: str


@dataclass(frozen=True)
class CommandResult:
    """Structured result of executing a command in a probe sandbox."""

    status: ProcessStatus
    exit_code: Optional[int]
    signal: Optional[int]
    command: List[str]
    cwd: str
    commit: str
    tests: Dict[str, TestStatus] = field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""

    def to_dict(self) -> dict:
        result = asdict(self)
        result["status"] = self.status.value
        result["tests"] = {key: value.value for key, value in self.tests.items()}
        return result


@dataclass(frozen=True)
class ProbeRunResult:
    """One model-designed probe executed in current and/or prior snapshots."""

    current: Optional[CommandResult] = None
    prior: Optional[CommandResult] = None

    def to_dict(self) -> dict:
        return {
            "current": self.current.to_dict() if self.current is not None else None,
            "prior": self.prior.to_dict() if self.prior is not None else None,
        }


@dataclass(frozen=True)
class ToolResult:
    """Bounded result returned to the model for one tool call."""

    content: str
    is_error: bool = False
    structured_content: Any = None

    def to_dict(self) -> dict:
        structured = self.structured_content
        if isinstance(structured, CommandResult):
            structured = structured.to_dict()
        elif hasattr(structured, "to_dict"):
            structured = structured.to_dict()
        return {
            "content": self.content,
            "is_error": self.is_error,
            "structured_content": structured,
        }


@dataclass(frozen=True)
class InvestigationTask:
    """Everything L2 gives L3 to answer one falsifiable obligation."""

    hypothesis: Any
    obligation: str
    excerpts: List[SourceExcerpt]
    commit_diff: str
    prior_attempts: List[dict]
    grant_tokens: int
    deadline_s: float


@dataclass
class BudgetLedger:
    """Reservation ledger for model-token spending."""

    granted: int
    reserved: int = 0
    actual: int = 0
    overshoot: int = 0

    def reserve(self, estimate: int) -> bool:
        estimate = max(0, int(estimate))
        if self.actual + self.reserved + estimate > self.granted:
            return False
        self.reserved += estimate
        return True

    def reconcile(self, estimate: int, actual: int) -> None:
        self.reserved = max(0, self.reserved - max(0, int(estimate)))
        self.actual += max(0, int(actual))
        self.overshoot = max(0, self.actual - self.granted)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class InvestigationRunResult:
    """Complete, auditable outcome of one L3 run."""

    verdict: str
    exit_status: str
    evidence_status: str
    reasoning: str
    tool_calls: List[ToolCallRecord] = field(default_factory=list)
    cites: List[str] = field(default_factory=list)
    evidence_test: str = ""
    evidence_diff: str = ""
    source_spans: List[SourceExcerpt] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    budget: BudgetLedger = field(default_factory=lambda: BudgetLedger(0))
    capabilities: Dict[str, bool] = field(default_factory=dict)
    capability_warning: str = ""
    degraded: bool = False

    @property
    def tokens_used(self) -> int:
        """Compatibility view for the L2 accounting code."""

        return self.budget.actual

    @property
    def probe_trace(self) -> List[ToolCallRecord]:
        """Compatibility view; new code should use ``tool_calls``."""

        return self.tool_calls

    def to_dict(self) -> dict:
        calls = []
        for call in self.tool_calls:
            result = call.result
            if isinstance(result, ToolResult):
                result = result.to_dict()
            calls.append(
                {
                    "tool_call_id": call.tool_call_id,
                    "skill_id": call.skill_id,
                    "arguments": call.arguments,
                    "resolved_arguments": call.resolved_arguments,
                    "result": result,
                    "duration_ms": call.duration_ms,
                    "error": call.error,
                    "parent_tool_call_id": call.parent_tool_call_id,
                }
            )
        return {
            "verdict": self.verdict,
            "exit_status": self.exit_status,
            "evidence_status": self.evidence_status,
            "reasoning": self.reasoning,
            "tool_calls": calls,
            "cites": list(self.cites),
            "evidence_test": self.evidence_test,
            "evidence_diff": self.evidence_diff,
            "source_spans": [asdict(item) for item in self.source_spans],
            "usage": self.usage.to_dict(),
            "budget": self.budget.to_dict(),
            "tokens_used": self.tokens_used,
            "capabilities": dict(self.capabilities),
            "capability_warning": self.capability_warning,
            "degraded": self.degraded,
        }


# Temporary source compatibility for callers that imported the old name.
InvestigatorResult = InvestigationRunResult
