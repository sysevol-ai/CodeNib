# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Value types for evidence-backed local-specification review."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Sequence

from ..execution import AgentRunResult, ExecutionIsolation, TokenUsage

_REVISION = re.compile(r"^[0-9a-f]{7,40}$")


class EvidenceAuthority(str, Enum):
    TASK = "task"
    REPOSITORY = "repository"
    TEST = "test"
    RUNTIME = "runtime"
    SOLVER = "solver"


@dataclass(frozen=True, slots=True)
class ContextMessage:
    """Non-authoritative context supplied by a solver or another participant."""

    content: str
    sender: str = "solver"
    scope: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Evidence:
    path: str
    line_start: int
    line_end: int
    description: str
    authority: EvidenceAuthority = EvidenceAuthority.REPOSITORY

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path.strip():
            raise ValueError("evidence path must be a non-empty string")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("evidence description must be a non-empty string")
        if (
            isinstance(self.line_start, bool)
            or not isinstance(self.line_start, int)
            or self.line_start < 1
        ):
            raise ValueError("evidence line_start must be a positive integer")
        if (
            isinstance(self.line_end, bool)
            or not isinstance(self.line_end, int)
            or self.line_end < self.line_start
        ):
            raise ValueError("evidence line_end must not precede line_start")
        object.__setattr__(self, "path", self.path.strip())
        object.__setattr__(self, "description", self.description.strip())
        object.__setattr__(self, "authority", EvidenceAuthority(self.authority))


@dataclass(frozen=True, slots=True)
class LocalSpecification:
    statement: str
    condition: str
    evidence: tuple[Evidence, ...]
    patch_assessment: str
    confidence: float
    uncertainty: str = ""
    explorer: str = ""
    memory_id: str = ""


class FindingStatus(str, Enum):
    VIOLATED = "violated"
    UNCERTAIN = "uncertain"
    SATISFIED = "satisfied"
    RETRACTED = "retracted"


@dataclass(frozen=True, slots=True)
class RememberedEvidence:
    """Repository evidence as it was observed in an immutable snapshot."""

    evidence: Evidence
    snapshot: str
    blob_sha256: str
    fresh: bool = False


@dataclass(frozen=True, slots=True)
class SpecificationAssessment:
    """A local specification's assessment for one repository snapshot."""

    snapshot: str
    status: FindingStatus
    patch_assessment: str
    recommendation: str
    confidence: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", FindingStatus(self.status))


@dataclass(frozen=True, slots=True)
class RememberedSpecification:
    """A specification whose evidence and assessments persist across reviews."""

    memory_id: str
    statement: str
    condition: str
    evidence: tuple[RememberedEvidence, ...] = ()
    assessments: tuple[SpecificationAssessment, ...] = ()


@dataclass(frozen=True, slots=True)
class GuardianMemory:
    """Controller-owned, read-only memory supplied to one Guardian review."""

    specifications: tuple[RememberedSpecification, ...] = ()
    observed_snapshots: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GuardianFinding:
    statement: str
    status: FindingStatus
    evidence: tuple[Evidence, ...]
    patch_assessment: str
    recommendation: str
    confidence: float
    condition: str = ""
    memory_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", FindingStatus(self.status))


@dataclass(frozen=True, slots=True)
class GuardianRequest:
    workspace: Path
    base_commit: str
    candidate_commit: str
    context: tuple[ContextMessage, ...] = ()
    change_patch: str = ""
    memory: GuardianMemory = field(default_factory=GuardianMemory)

    def __post_init__(self) -> None:
        workspace = Path(self.workspace).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError(f"workspace must be an existing directory: {workspace}")
        for name in ("base_commit", "candidate_commit"):
            revision = getattr(self, name).lower()
            if not _REVISION.fullmatch(revision):
                raise ValueError(f"{name} must be a 7-40 character lowercase Git SHA")
            object.__setattr__(self, name, revision)
        object.__setattr__(self, "workspace", workspace)
        object.__setattr__(self, "context", tuple(self.context))
        if not isinstance(self.memory, GuardianMemory):
            raise TypeError("memory must be GuardianMemory")
        if not isinstance(self.change_patch, str):
            raise TypeError("change_patch must be a string")
        if len(self.change_patch.encode("utf-8")) > 2 * 1024**2:
            raise ValueError("change_patch exceeds the 2 MiB request limit")


@dataclass(frozen=True, slots=True)
class GuardianConfig:
    explorer_model: str
    aggregator_model: str
    explorer_count: int = 2
    explorer_reasoning_effort: str = "medium"
    aggregator_reasoning_effort: str = "medium"
    rollout_timeout_seconds: float = 600.0
    max_findings: int = 5
    execution_isolation: ExecutionIsolation = ExecutionIsolation.NATIVE

    def __post_init__(self) -> None:
        if self.explorer_count < 1:
            raise ValueError("explorer_count must be positive")
        if self.max_findings < 1:
            raise ValueError("max_findings must be positive")
        object.__setattr__(
            self,
            "execution_isolation",
            ExecutionIsolation(self.execution_isolation),
        )


class ReviewStatus(str, Enum):
    COMPLETE = "complete"
    DEGRADED = "degraded"
    FAILED = "failed"


def _usage_total(rollouts: Sequence[AgentRunResult]) -> TokenUsage:
    fields = (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    )
    totals = {
        field: sum(getattr(rollout.usage, field) for rollout in rollouts)
        for field in fields
    }
    return TokenUsage(**totals)


@dataclass(frozen=True, slots=True)
class GuardianResult:
    base_commit: str
    candidate_commit: str
    status: ReviewStatus
    candidates: tuple[LocalSpecification, ...] = ()
    findings: tuple[GuardianFinding, ...] = ()
    backlog: tuple[GuardianFinding, ...] = ()
    assessments: tuple[GuardianFinding, ...] = field(default=(), repr=False)
    summary: str = ""
    rollouts: tuple[AgentRunResult, ...] = field(default=(), repr=False)
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", ReviewStatus(self.status))

    @property
    def usage(self) -> TokenUsage:
        return _usage_total(self.rollouts)

    def to_dict(self) -> dict[str, object]:
        def specification(value: LocalSpecification) -> dict[str, object]:
            row = asdict(value)
            for evidence in row["evidence"]:
                evidence["authority"] = evidence["authority"].value
            return row

        def finding(value: GuardianFinding) -> dict[str, object]:
            row = asdict(value)
            row["status"] = value.status.value
            for evidence in row["evidence"]:
                evidence["authority"] = evidence["authority"].value
            return row

        return {
            "base_commit": self.base_commit,
            "candidate_commit": self.candidate_commit,
            "status": self.status.value,
            "summary": self.summary,
            "candidates": [specification(value) for value in self.candidates],
            "findings": [finding(value) for value in self.findings],
            "backlog": [finding(value) for value in self.backlog],
            "assessments": [finding(value) for value in self.assessments],
            "candidate_count": len(self.candidates),
            "errors": list(self.errors),
            "usage": {
                "input_tokens": self.usage.input_tokens,
                "cached_input_tokens": self.usage.cached_input_tokens,
                "cache_write_input_tokens": self.usage.cache_write_input_tokens,
                "output_tokens": self.usage.output_tokens,
                "reasoning_output_tokens": self.usage.reasoning_output_tokens,
                "total_tokens": self.usage.total_tokens,
            },
        }


__all__ = [
    "ContextMessage",
    "Evidence",
    "EvidenceAuthority",
    "FindingStatus",
    "GuardianConfig",
    "GuardianFinding",
    "GuardianMemory",
    "GuardianRequest",
    "GuardianResult",
    "LocalSpecification",
    "RememberedEvidence",
    "RememberedSpecification",
    "ReviewStatus",
    "SpecificationAssessment",
]
