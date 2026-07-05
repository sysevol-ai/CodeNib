# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Feedback-plan selection for small agent-runner iterations."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class FeedbackPlanSpec:
    """Deterministic small-sample plan for iterative agent evaluation.

    ``seed`` is the rotation handle: keep it fixed for the stable smoke gate;
    change it for a new holdout slice. Selection is stratified by
    ``group_field`` so a small plan still covers the configured language or
    dataset groups without hand-picking instances.
    """

    seed: str = "codeminer-feedback-v1"
    group_field: str = "language_group"
    smoke_per_group: int = 1
    holdout_per_group: int = 2
    exclude_instances: Sequence[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.smoke_per_group < 0:
            raise ValueError("smoke_per_group must be non-negative")
        if self.holdout_per_group < 0:
            raise ValueError("holdout_per_group must be non-negative")
        if not self.group_field:
            raise ValueError("group_field must be non-empty")
        object.__setattr__(
            self,
            "exclude_instances",
            tuple(str(instance_id) for instance_id in self.exclude_instances),
        )


@dataclass(frozen=True)
class FeedbackGroup:
    """Selected smoke and holdout instances for one group."""

    smoke: tuple[str, ...]
    holdout: tuple[str, ...]


@dataclass(frozen=True)
class FeedbackPlan:
    """Selected feedback instances plus enough metadata for audit logs."""

    seed: str
    group_field: str
    groups: Mapping[str, FeedbackGroup]

    def __post_init__(self) -> None:
        object.__setattr__(self, "groups", MappingProxyType(dict(self.groups)))

    @property
    def smoke_instances(self) -> list[str]:
        return [iid for group in self.groups.values() for iid in group.smoke]

    @property
    def holdout_instances(self) -> list[str]:
        return [iid for group in self.groups.values() for iid in group.holdout]

    @property
    def instances(self) -> list[str]:
        return self.smoke_instances + self.holdout_instances

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "group_field": self.group_field,
            "smoke_instances": self.smoke_instances,
            "holdout_instances": self.holdout_instances,
            "instances": self.instances,
            "groups": {
                group: {
                    "smoke": list(selected.smoke),
                    "holdout": list(selected.holdout),
                }
                for group, selected in self.groups.items()
            },
        }


def build_feedback_plan(
    rows_by_id: Mapping[str, Mapping[str, Any]],
    spec: FeedbackPlanSpec | None = None,
) -> FeedbackPlan:
    """Select deterministic smoke and holdout instances from dataset rows."""
    spec = spec or FeedbackPlanSpec()
    excluded = set(spec.exclude_instances or ())
    grouped: dict[str, list[str]] = {}
    for instance_id, row in rows_by_id.items():
        iid = str(row.get("instance_id") or instance_id)
        if iid in excluded:
            continue
        group = str(row.get(spec.group_field) or "unknown")
        grouped.setdefault(group, []).append(iid)

    groups: dict[str, FeedbackGroup] = {}
    for group in sorted(grouped):
        instance_ids = sorted(set(grouped[group]))
        smoke = tuple(
            _stable_order(instance_ids, seed=spec.seed, bucket=f"smoke:{group}")[
                : spec.smoke_per_group
            ]
        )
        smoke_set = set(smoke)
        remaining = [iid for iid in instance_ids if iid not in smoke_set]
        holdout = tuple(
            _stable_order(remaining, seed=spec.seed, bucket=f"holdout:{group}")[
                : spec.holdout_per_group
            ]
        )
        groups[group] = FeedbackGroup(smoke=smoke, holdout=holdout)

    return FeedbackPlan(seed=spec.seed, group_field=spec.group_field, groups=groups)


def _stable_order(instance_ids: Sequence[str], *, seed: str, bucket: str) -> list[str]:
    return sorted(
        instance_ids,
        key=lambda iid: (
            hashlib.sha256(f"{bucket}\0{seed}\0{iid}".encode("utf-8")).hexdigest(),
            iid,
        ),
    )
