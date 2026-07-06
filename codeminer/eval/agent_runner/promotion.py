# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Promotion evidence contracts for agent-runner behavior changes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence, Tuple

FAILURE_CATEGORIES = frozenset(
    {
        "content",
        "rank",
        "path_alias",
        "format",
        "context_runtime",
        "infrastructure",
    }
)


@dataclass(frozen=True)
class EvidenceSlice:
    """One evaluated slice used to support a promotion decision."""

    name: str
    role: str
    instance_count: int
    report_path: Optional[str] = None
    seed: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("evidence slice name must be non-empty")
        if self.role not in {"smoke", "holdout"}:
            raise ValueError("evidence slice role must be 'smoke' or 'holdout'")
        if self.instance_count < 0:
            raise ValueError("evidence slice instance_count must be non-negative")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "instance_count": self.instance_count,
            "report_path": self.report_path,
            "seed": self.seed,
        }


@dataclass(frozen=True)
class PromotionEvidence:
    """Evidence required before experiment behavior becomes a runtime default."""

    runtime_contract: str
    raw_behavior_delta: str
    failure_category: str
    slices: Tuple[EvidenceSlice, ...] = ()
    compared_models: Tuple[str, ...] = ()
    compared_agents: Tuple[str, ...] = ()
    model_specific: bool = False
    scorer_dependencies: Tuple[str, ...] = ()
    benchmark_dependencies: Tuple[str, ...] = ()
    notes: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "runtime_contract", self.runtime_contract.strip())
        object.__setattr__(
            self,
            "raw_behavior_delta",
            self.raw_behavior_delta.strip(),
        )
        object.__setattr__(self, "failure_category", self.failure_category.strip())
        object.__setattr__(self, "slices", tuple(self.slices))
        object.__setattr__(
            self,
            "compared_models",
            _normalized_tuple(self.compared_models),
        )
        object.__setattr__(
            self,
            "compared_agents",
            _normalized_tuple(self.compared_agents),
        )
        object.__setattr__(
            self,
            "scorer_dependencies",
            _normalized_tuple(self.scorer_dependencies),
        )
        object.__setattr__(
            self,
            "benchmark_dependencies",
            _normalized_tuple(self.benchmark_dependencies),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "runtime_contract": self.runtime_contract,
            "raw_behavior_delta": self.raw_behavior_delta,
            "failure_category": self.failure_category,
            "slices": [item.to_dict() for item in self.slices],
            "compared_models": list(self.compared_models),
            "compared_agents": list(self.compared_agents),
            "model_specific": self.model_specific,
            "scorer_dependencies": list(self.scorer_dependencies),
            "benchmark_dependencies": list(self.benchmark_dependencies),
            "notes": self.notes,
        }


@dataclass(frozen=True)
class PromotionGateResult:
    """Validation result for a promotion evidence record."""

    ready: bool
    failures: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ready": self.ready,
            "failures": list(self.failures),
        }


def evaluate_promotion_evidence(
    evidence: PromotionEvidence,
) -> PromotionGateResult:
    """Validate promotion evidence without reading benchmark-specific context."""

    failures: list[str] = []
    if not evidence.runtime_contract:
        failures.append("runtime_contract is required")
    if not evidence.raw_behavior_delta:
        failures.append("raw_behavior_delta is required")
    if evidence.failure_category not in FAILURE_CATEGORIES:
        failures.append("failure_category must be one of the allowed categories")

    smoke = [item for item in evidence.slices if item.role == "smoke"]
    holdout = [item for item in evidence.slices if item.role == "holdout"]
    if not _has_non_empty_slice(smoke):
        failures.append("at least one non-empty smoke slice is required")
    if not _has_non_empty_slice(holdout):
        failures.append("at least one non-empty holdout slice is required")

    compared_systems = len(set(evidence.compared_models + evidence.compared_agents))
    if evidence.model_specific and compared_systems < 2:
        failures.append("model_specific changes require at least two compared systems")

    if evidence.scorer_dependencies:
        failures.append("promotion must not depend on scorer-specific fields")
    if evidence.benchmark_dependencies:
        failures.append("promotion must not depend on named benchmark instances")

    return PromotionGateResult(ready=not failures, failures=tuple(failures))


def _has_non_empty_slice(slices: Sequence[EvidenceSlice]) -> bool:
    return any(item.instance_count > 0 for item in slices)


def _normalized_tuple(values: Sequence[str]) -> Tuple[str, ...]:
    return tuple(str(value).strip() for value in values if str(value).strip())


__all__ = [
    "FAILURE_CATEGORIES",
    "EvidenceSlice",
    "PromotionEvidence",
    "PromotionGateResult",
    "evaluate_promotion_evidence",
]
