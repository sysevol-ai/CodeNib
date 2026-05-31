# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Core data structures for skill definitions.

All types here are pure data — no behaviour, no side effects.
This makes them trivially serializable for the future Rust compiler handoff.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from ...compiler.resources import IndexRequirement  # noqa: F401


class SkillType(str, Enum):
    """Execution characteristic of a skill."""

    RETRIEVAL = "retrieval"
    TRANSFORM = "transform"
    RERANK = "rerank"
    AGGREGATE = "aggregate"
    EXPAND = "expand"
    CUSTOM = "custom"


class Cost(str, Enum):
    """Relative cost hint for the policy engine."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# Numeric mapping so the policy engine can do arithmetic.
COST_WEIGHTS: Dict[Cost, float] = {
    Cost.LOW: 1.0,
    Cost.MEDIUM: 2.0,
    Cost.HIGH: 4.0,
}


@dataclass(slots=True)
class SkillInputSpec:
    """Type specification for a single skill input."""

    name: str
    type_hint: str  # e.g. "str", "List[QueriedNode]"
    required: bool = True
    default: Any = None
    description: str = ""
    # Optional closed set of allowed values. When set, emitted as a JSON-Schema
    # ``enum`` constraint in the OpenAI tool schema so the model sees the valid
    # choices in-band (e.g. ``output_mode`` on the ``grep`` default tool) rather
    # than only in prose. Not part of the Python↔Rust ``to_dict`` contract.
    enum: Optional[List[str]] = None


@dataclass(slots=True)
class SkillOutputSpec:
    """Type specification for skill output."""

    type_hint: str  # e.g. "List[QueriedNode]"
    description: str = ""


@dataclass(slots=True)
class SkillMetadata:
    """
    Pure-data descriptor for a registered skill.

    The ``executor_fn`` is the only non-serializable field — the Rust compiler
    will never touch it; the Python scheduler calls it at execution time.
    """

    skill_id: str
    skill_type: SkillType
    inputs: List[SkillInputSpec] = field(default_factory=list)
    outputs: SkillOutputSpec = field(
        default_factory=lambda: SkillOutputSpec(type_hint="Any")
    )

    # Execution
    executor_fn: Optional[Callable[..., Any]] = field(default=None, repr=False)
    async_capable: bool = False
    cacheable: bool = True
    cost: Cost = Cost.MEDIUM

    # Compilation hints
    dependencies: List[str] = field(default_factory=list)
    resources: List[str] = field(default_factory=list)
    operator: Optional[str] = None  # PhysicalOperator value for bridge layer

    # Skill package fields
    defaults: Dict[str, Any] = field(default_factory=dict)
    index_requirements: List[Any] = field(
        default_factory=list
    )  # List[IndexRequirement]
    skill_doc: str = ""  # agent-readable skill.md content

    description: str = ""

    def __post_init__(self) -> None:
        if not self.skill_id:
            raise ValueError("skill_id is required")

    # ---- serialisation (Python↔Rust contract) ----

    def to_dict(self) -> Dict[str, Any]:
        """JSON-friendly dict (excludes ``executor_fn``)."""
        return {
            "skill_id": self.skill_id,
            "skill_type": self.skill_type.value,
            "inputs": [
                {
                    "name": i.name,
                    "type_hint": i.type_hint,
                    "required": i.required,
                    "default": i.default,
                    "description": i.description,
                }
                for i in self.inputs
            ],
            "outputs": {
                "type_hint": self.outputs.type_hint,
                "description": self.outputs.description,
            },
            "async_capable": self.async_capable,
            "cacheable": self.cacheable,
            "cost": self.cost.value,
            "dependencies": list(self.dependencies),
            "resources": list(self.resources),
            "operator": self.operator,
            "defaults": dict(self.defaults),
            "index_requirements": [
                r.to_dict() if hasattr(r, "to_dict") else r
                for r in self.index_requirements
            ],
            "skill_doc": self.skill_doc,
            "description": self.description,
        }
