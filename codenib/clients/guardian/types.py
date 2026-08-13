# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Value types for Guardian's multi-round local-specification search."""

from __future__ import annotations

import re
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Sequence

from ..execution import AgentRunResult, ExecutionIsolation, TokenUsage

_REVISION = re.compile(r"^[0-9a-f]{7,40}$")


class TaskContextSource(str, Enum):
    USER_INSTRUCTION = "user_instruction"
    ISSUE = "issue"
    TICKET = "ticket"
    SOLVER_SUMMARY = "solver_summary"


class ContextFidelity(str, Enum):
    VERBATIM = "verbatim"
    DERIVED = "derived"


@dataclass(frozen=True, slots=True)
class TaskContext:
    """One provenance-preserving task-context item."""

    content: str
    source: TaskContextSource
    fidelity: ContextFidelity
    context_id: str = ""

    def __post_init__(self) -> None:
        content = self.content.strip()
        if not content:
            raise ValueError("task context content must not be empty")
        source = TaskContextSource(self.source)
        fidelity = ContextFidelity(self.fidelity)
        if (
            source is TaskContextSource.SOLVER_SUMMARY
            and fidelity is not ContextFidelity.DERIVED
        ):
            raise ValueError("solver summaries must be marked as derived")
        context_id = self.context_id.strip() or f"CTX-{uuid.uuid4().hex[:12]}"
        object.__setattr__(self, "content", content)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "fidelity", fidelity)
        object.__setattr__(self, "context_id", context_id)


@dataclass(frozen=True, slots=True)
class ContextMessage:
    """Non-authoritative context supplied by a solver or another participant."""

    content: str
    sender: str = "solver"
    scope: tuple[str, ...] = ()


class EvidenceSourceType(str, Enum):
    TASK = "task"
    DOCUMENTATION = "documentation"
    INTERFACE = "interface"
    CALLER = "caller"
    TEST = "test"
    RUNTIME_PROBE = "runtime_probe"
    EXISTING_BEHAVIOR = "existing_behavior"
    SOLVER_MESSAGE = "solver_message"
    CANDIDATE_PATCH = "candidate_patch"


class EvidenceAuthority(str, Enum):
    NORMATIVE = "normative"
    CONVENTIONAL = "conventional"
    BEHAVIORAL = "behavioral"
    DERIVED = "derived"


class ProbeOutcome(str, Enum):
    SATISFIED = "satisfied"
    VIOLATED = "violated"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, slots=True)
class ProbeCheckMetrics:
    """Controller-derived accounting for final-check runtime probes."""

    declared: int = 0
    accepted: int = 0
    satisfied: int = 0
    violated: int = 0
    inconclusive: int = 0
    rejected_by_reason: tuple[tuple[str, int], ...] = ()
    repaired: int = 0
    repair_attempted: bool = False
    repair_accepted: bool = False

    @property
    def rejected(self) -> int:
        return sum(count for _, count in self.rejected_by_reason)

    def to_dict(self) -> dict[str, object]:
        return {
            "declared": self.declared,
            "accepted": self.accepted,
            "satisfied": self.satisfied,
            "violated": self.violated,
            "inconclusive": self.inconclusive,
            "rejected": self.rejected,
            "rejected_by_reason": dict(self.rejected_by_reason),
            "repaired": self.repaired,
            "repair_attempted": self.repair_attempted,
            "repair_accepted": self.repair_accepted,
        }


@dataclass(frozen=True, slots=True)
class Evidence:
    """An inspectable evidence item with independent source and authority axes."""

    path: str
    line_start: int
    line_end: int
    description: str
    source_type: EvidenceSourceType = EvidenceSourceType.DOCUMENTATION
    authority: EvidenceAuthority = EvidenceAuthority.CONVENTIONAL
    evidence_id: str = ""
    supports: tuple[str, ...] = ()
    opposes: tuple[str, ...] = ()
    acquired_by: str = ""
    round: int = 1
    command: str = ""
    output: str = ""
    probe_key: str = ""
    probe_specification_id: str = ""
    probe_outcome: ProbeOutcome | None = None
    exit_code: int | None = None
    quote: str = ""
    snapshot: str = ""
    blob_sha256: str = ""
    fresh: bool = True

    def __post_init__(self) -> None:
        path = self.path.strip()
        description = self.description.strip()
        if not path:
            raise ValueError("evidence locator must be a non-empty string")
        if not description:
            raise ValueError("evidence observation must be a non-empty string")
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
        if self.round < 1:
            raise ValueError("evidence round must be positive")
        evidence_id = self.evidence_id.strip() or f"EV-{uuid.uuid4().hex[:12]}"
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "source_type", EvidenceSourceType(self.source_type))
        object.__setattr__(self, "authority", EvidenceAuthority(self.authority))
        object.__setattr__(self, "evidence_id", evidence_id)
        object.__setattr__(self, "supports", tuple(self.supports))
        object.__setattr__(self, "opposes", tuple(self.opposes))
        object.__setattr__(self, "probe_key", self.probe_key.strip())
        object.__setattr__(
            self, "probe_specification_id", self.probe_specification_id.strip()
        )
        if isinstance(self.exit_code, bool):
            raise ValueError("evidence exit_code must be an integer or None")
        if self.probe_outcome is not None:
            object.__setattr__(self, "probe_outcome", ProbeOutcome(self.probe_outcome))

    @property
    def observation(self) -> str:
        return self.description


class SpecificationStatus(str, Enum):
    PROPOSED = "proposed"
    SUPPORTED = "supported"
    CONTESTED = "contested"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class SpecificationProvenance:
    explorer: str
    round: int
    candidate_id: str


@dataclass(frozen=True, slots=True)
class SpecificationRelations:
    equivalent: tuple[str, ...] = ()
    entailed_by: tuple[str, ...] = ()
    conflicts_with: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CandidateSpecification:
    candidate_id: str
    statement: str
    condition: str
    scope: tuple[str, ...]
    supporting_evidence: tuple[Evidence, ...]
    counterevidence: tuple[Evidence, ...] = ()
    suggested_status: SpecificationStatus = SpecificationStatus.PROPOSED
    unresolved_questions: tuple[str, ...] = ()
    explorer: str = ""
    round: int = 1
    brief_id: str = ""
    patch_assessment: str = ""

    def __post_init__(self) -> None:
        if not self.statement.strip():
            raise ValueError("candidate specification statement must not be empty")
        object.__setattr__(
            self, "suggested_status", SpecificationStatus(self.suggested_status)
        )
        object.__setattr__(self, "scope", tuple(self.scope))
        object.__setattr__(self, "supporting_evidence", tuple(self.supporting_evidence))
        object.__setattr__(self, "counterevidence", tuple(self.counterevidence))
        object.__setattr__(
            self, "unresolved_questions", tuple(self.unresolved_questions)
        )


# Compatibility name retained for callers that consumed the former explorer type.
LocalSpecification = CandidateSpecification


@dataclass(frozen=True, slots=True)
class SpecificationRecord:
    specification_id: str
    statement: str
    condition: str
    scope: tuple[str, ...]
    supporting_evidence: tuple[str, ...]
    counterevidence: tuple[str, ...]
    provenance: tuple[SpecificationProvenance, ...]
    status: SpecificationStatus
    status_reason: str
    unresolved_questions: tuple[str, ...] = ()
    related_entries: SpecificationRelations = field(
        default_factory=SpecificationRelations
    )
    created_round: int = 1
    updated_round: int = 1

    def __post_init__(self) -> None:
        if not self.specification_id.strip():
            raise ValueError("specification id must not be empty")
        if not self.statement.strip():
            raise ValueError("specification statement must not be empty")
        object.__setattr__(self, "status", SpecificationStatus(self.status))
        object.__setattr__(self, "scope", tuple(self.scope))
        object.__setattr__(self, "supporting_evidence", tuple(self.supporting_evidence))
        object.__setattr__(self, "counterevidence", tuple(self.counterevidence))
        object.__setattr__(self, "provenance", tuple(self.provenance))
        object.__setattr__(
            self, "unresolved_questions", tuple(self.unresolved_questions)
        )


@dataclass(frozen=True, slots=True)
class InvestigationBrief:
    brief_id: str
    focus_question: str
    target_surface: tuple[str, ...]
    evidence_to_seek: tuple[str, ...]
    avoid_duplicating: tuple[str, ...] = ()
    linked_specifications: tuple[str, ...] = ()
    open_ended: bool = False


@dataclass(frozen=True, slots=True)
class ExplorationTrace:
    searched_locations: tuple[str, ...] = ()
    actions_and_outcomes: tuple[str, ...] = ()
    unsuccessful_routes: tuple[str, ...] = ()
    remaining_questions: tuple[str, ...] = ()
    suggested_next_actions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExplorationExperience:
    experience_id: str
    round: int
    brief_id: str
    searched_locations: tuple[str, ...] = ()
    actions_and_outcomes: tuple[str, ...] = ()
    unsuccessful_routes: tuple[str, ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    suggested_next_actions: tuple[str, ...] = ()
    linked_specifications: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExplorerOutput:
    explorer: str
    round: int
    brief_id: str
    candidates: tuple[CandidateSpecification, ...]
    trace: ExplorationTrace
    error: str = ""


@dataclass(frozen=True, slots=True)
class StageUsage:
    stage: str
    round: int
    model: str
    usage: TokenUsage
    tool_calls: int = 0


@dataclass(frozen=True, slots=True)
class RoundRecord:
    round: int
    briefs: tuple[InvestigationBrief, ...]
    explorer_outputs: tuple[ExplorerOutput, ...]
    aggregation_summary: str
    frontier: tuple[str, ...]
    new_information: bool
    degraded: bool = False
    errors: tuple[str, ...] = ()
    specifications: tuple[SpecificationRecord, ...] = ()
    experiences: tuple[ExplorationExperience, ...] = ()


@dataclass(frozen=True, slots=True)
class SpecificationMemory:
    """Task-scoped structured specification memory."""

    session_id: str = ""
    specifications: tuple[SpecificationRecord, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    experiences: tuple[ExplorationExperience, ...] = ()
    rounds: tuple[RoundRecord, ...] = ()
    stage_usage: tuple[StageUsage, ...] = ()
    observed_snapshots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "session_id", self.session_id or f"GS-{uuid.uuid4().hex[:16]}"
        )


GuardianMemory = SpecificationMemory


class FindingStatus(str, Enum):
    VIOLATED = "violated"
    UNCERTAIN = "uncertain"
    SATISFIED = "satisfied"


@dataclass(frozen=True, slots=True)
class GuardianFinding:
    specification_id: str
    statement: str
    status: FindingStatus
    evidence: tuple[Evidence, ...]
    patch_assessment: str
    recommendation: str
    condition: str = ""
    patch_locations: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", FindingStatus(self.status))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "patch_locations", tuple(self.patch_locations))
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))


@dataclass(frozen=True, slots=True)
class GuardianRequest:
    workspace: Path
    base_commit: str
    candidate_commit: str
    change_patch: str
    task_context: tuple[TaskContext, ...] = ()
    context: tuple[ContextMessage, ...] = ()
    memory: SpecificationMemory = field(default_factory=SpecificationMemory)
    previous_session_id: str = ""
    artifact_dir: Path | None = None
    # Backward-compatible entry point; normalized into task_context.
    task_description: str = ""

    def __post_init__(self) -> None:
        workspace = Path(self.workspace).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError(f"workspace must be an existing directory: {workspace}")
        for name in ("base_commit", "candidate_commit"):
            revision = getattr(self, name).lower()
            if not _REVISION.fullmatch(revision):
                raise ValueError(f"{name} must be a 7-40 character lowercase Git SHA")
            object.__setattr__(self, name, revision)
        if not isinstance(self.change_patch, str):
            raise TypeError("change_patch must be a string")
        if len(self.change_patch.encode("utf-8")) > 2 * 1024**2:
            raise ValueError("change_patch exceeds the 2 MiB request limit")
        task_context = tuple(self.task_context)
        if not isinstance(self.task_description, str):
            raise TypeError("task_description must be a string")
        if self.task_description.strip():
            if len(self.task_description.encode("utf-8")) > 256 * 1024:
                raise ValueError("task_description exceeds the 256 KiB request limit")
            if not any(
                item.content == self.task_description.strip()
                and item.source is TaskContextSource.USER_INSTRUCTION
                for item in task_context
            ):
                task_context += (
                    TaskContext(
                        content=self.task_description,
                        source=TaskContextSource.USER_INSTRUCTION,
                        fidelity=ContextFidelity.VERBATIM,
                        context_id="CTX-original-task",
                    ),
                )
        for message in self.context:
            if not any(
                item.content == message.content.strip()
                and item.source is TaskContextSource.SOLVER_SUMMARY
                for item in task_context
            ):
                task_context += (
                    TaskContext(
                        content=message.content,
                        source=TaskContextSource.SOLVER_SUMMARY,
                        fidelity=ContextFidelity.DERIVED,
                        context_id=f"CTX-solver-{uuid.uuid4().hex[:8]}",
                    ),
                )
        if sum(len(item.content.encode("utf-8")) for item in task_context) > 512 * 1024:
            raise ValueError("task_context exceeds the 512 KiB request limit")
        if not isinstance(self.memory, SpecificationMemory):
            raise TypeError("memory must be SpecificationMemory")
        artifact_dir = self.artifact_dir
        if artifact_dir is None:
            artifact_dir = Path(tempfile.mkdtemp(prefix="guardian-review-artifacts-"))
        artifact_dir = Path(artifact_dir).expanduser().resolve()
        artifact_dir.mkdir(parents=True, exist_ok=True)
        object.__setattr__(self, "workspace", workspace)
        object.__setattr__(self, "task_context", task_context)
        object.__setattr__(self, "context", tuple(self.context))
        object.__setattr__(self, "artifact_dir", artifact_dir)


@dataclass(frozen=True, slots=True)
class GuardianConfig:
    explorer_model: str
    aggregator_model: str
    explorer_count: int = 4
    targeted_explorer_count: int = 2
    max_rounds: int = 2
    explorer_reasoning_effort: str = "medium"
    aggregator_reasoning_effort: str = "medium"
    rollout_timeout_seconds: float = 600.0
    max_findings: int = 10
    max_specifications_per_explorer: int = 5
    execution_isolation: ExecutionIsolation = ExecutionIsolation.NATIVE

    def __post_init__(self) -> None:
        for name in (
            "explorer_count",
            "targeted_explorer_count",
            "max_rounds",
            "max_findings",
            "max_specifications_per_explorer",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")
        object.__setattr__(
            self, "execution_isolation", ExecutionIsolation(self.execution_isolation)
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
    return TokenUsage(
        **{
            field: sum(getattr(rollout.usage, field) for rollout in rollouts)
            for field in fields
        }
    )


@dataclass(frozen=True, slots=True)
class GuardianResult:
    base_commit: str
    candidate_commit: str
    status: ReviewStatus
    memory: SpecificationMemory = field(default_factory=SpecificationMemory)
    candidates: tuple[CandidateSpecification, ...] = ()
    findings: tuple[GuardianFinding, ...] = ()
    uncertain_specifications: tuple[GuardianFinding, ...] = ()
    summary: str = ""
    rollouts: tuple[AgentRunResult, ...] = field(default=(), repr=False)
    rollout_stages: tuple[str, ...] = field(default=(), repr=False)
    rounds: tuple[RoundRecord, ...] = ()
    artifact_dir: Path | None = None
    errors: tuple[str, ...] = ()
    probe_metrics: ProbeCheckMetrics = field(default_factory=ProbeCheckMetrics)

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", ReviewStatus(self.status))

    @property
    def usage(self) -> TokenUsage:
        return _usage_total(self.rollouts)

    @property
    def assessments(self) -> tuple[GuardianFinding, ...]:
        return self.findings + self.uncertain_specifications

    def to_dict(self) -> dict[str, object]:
        def evidence(value: Evidence) -> dict[str, object]:
            return {
                "id": value.evidence_id,
                "source_type": value.source_type.value,
                "authority": value.authority.value,
                "locator": {
                    "path": value.path,
                    "line_start": value.line_start,
                    "line_end": value.line_end,
                },
                "observation": value.description,
                "supports": list(value.supports),
                "opposes": list(value.opposes),
                "acquired_by": value.acquired_by,
                "round": value.round,
            }

        def finding(value: GuardianFinding) -> dict[str, object]:
            return {
                "specification_id": value.specification_id,
                "statement": value.statement,
                "condition": value.condition,
                "status": value.status.value,
                "patch_locations": list(value.patch_locations),
                "evidence_ids": list(value.evidence_ids),
                "evidence": [evidence(item) for item in value.evidence],
                "assessment": value.patch_assessment,
                "recommendation": value.recommendation,
            }

        return {
            "base_commit": self.base_commit,
            "candidate_commit": self.candidate_commit,
            "status": self.status.value,
            "summary": self.summary,
            "findings": [finding(value) for value in self.findings],
            "uncertain_specifications": [
                finding(value) for value in self.uncertain_specifications
            ],
            "candidate_count": len(self.candidates),
            "memory_specifications": len(self.memory.specifications),
            "rounds": len(self.rounds),
            "artifact_dir": str(self.artifact_dir or ""),
            "errors": list(self.errors),
            "probe_metrics": self.probe_metrics.to_dict(),
            "usage": {**asdict(self.usage), "total_tokens": self.usage.total_tokens},
        }


__all__ = [
    "CandidateSpecification",
    "ContextFidelity",
    "ContextMessage",
    "Evidence",
    "EvidenceAuthority",
    "EvidenceSourceType",
    "ExplorationExperience",
    "ExplorationTrace",
    "ExplorerOutput",
    "FindingStatus",
    "GuardianConfig",
    "GuardianFinding",
    "GuardianMemory",
    "GuardianRequest",
    "GuardianResult",
    "InvestigationBrief",
    "LocalSpecification",
    "ProbeCheckMetrics",
    "ProbeOutcome",
    "ReviewStatus",
    "RoundRecord",
    "SpecificationMemory",
    "SpecificationProvenance",
    "SpecificationRecord",
    "SpecificationRelations",
    "SpecificationStatus",
    "StageUsage",
    "TaskContext",
    "TaskContextSource",
]
