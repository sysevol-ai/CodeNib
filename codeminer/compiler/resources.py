# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Index resource management for the skill compiler.

Skills declare index dependencies via ``IndexRequirement``.  The
``ResourceResolver`` checks current index state and produces a
``ResourcePlan`` that tells the scheduler what to build, update, or use.

Three index states:
  MISSING  → block, synchronous build (first use)
  STALE    → allow degraded execution + async incremental update
  FRESH    → use directly
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


class IndexState(str, Enum):
    """Lifecycle state of an index."""

    MISSING = "missing"
    STALE = "stale"
    FRESH = "fresh"


@dataclass(slots=True)
class IndexRequirement:
    """Declaration of an index dependency from a skill config."""

    index_type: str  # e.g. "bm25", "vector", "code_graph"
    max_age_seconds: float = 3600.0  # freshness threshold
    scope: str = "current_repo"
    required: bool = True  # if False, skill can degrade without it

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index_type": self.index_type,
            "max_age_seconds": self.max_age_seconds,
            "scope": self.scope,
            "required": self.required,
        }


@dataclass(slots=True)
class IndexStatus:
    """Current state of an index at resolution time."""

    index_type: str
    state: IndexState
    last_built: Optional[float] = None  # epoch timestamp
    age_seconds: Optional[float] = None
    scope: str = "current_repo"
    path: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index_type": self.index_type,
            "state": self.state.value,
            "last_built": self.last_built,
            "age_seconds": self.age_seconds,
            "scope": self.scope,
            "path": self.path,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class ResourceDecision:
    """Outcome of resource resolution for a single index."""

    index_type: str
    state: IndexState
    action: str  # "use" | "rebuild" | "incremental_update" | "skip"
    warning: Optional[str] = None
    blocking: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index_type": self.index_type,
            "state": self.state.value,
            "action": self.action,
            "warning": self.warning,
            "blocking": self.blocking,
        }


@dataclass(slots=True)
class ResourcePlan:
    """Aggregate resource decisions for all indexes needed by a plan."""

    decisions: List[ResourceDecision] = field(default_factory=list)
    can_execute: bool = True
    blocking_builds: List[str] = field(default_factory=list)
    async_updates: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decisions": [d.to_dict() for d in self.decisions],
            "can_execute": self.can_execute,
            "blocking_builds": list(self.blocking_builds),
            "async_updates": list(self.async_updates),
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# Index state stores
# ---------------------------------------------------------------------------


@runtime_checkable
class IndexStateStore(Protocol):
    """Protocol for persisting index state."""

    def get_status(self, index_type: str, scope: str) -> Optional[IndexStatus]: ...
    def set_status(self, status: IndexStatus) -> None: ...


class InMemoryIndexStateStore:
    """In-memory store for testing."""

    def __init__(self) -> None:
        self._store: Dict[str, IndexStatus] = {}

    def get_status(self, index_type: str, scope: str) -> Optional[IndexStatus]:
        return self._store.get(f"{index_type}:{scope}")

    def set_status(self, status: IndexStatus) -> None:
        self._store[f"{status.index_type}:{status.scope}"] = status


class FileSystemIndexStateStore:
    """Track index state via JSON metadata files alongside indexes."""

    META_FILENAME = ".index_meta.json"

    def __init__(self, cache_dir: str) -> None:
        self._cache_dir = cache_dir

    def get_status(self, index_type: str, scope: str) -> Optional[IndexStatus]:
        meta_path = os.path.join(self._cache_dir, index_type, self.META_FILENAME)
        if not os.path.exists(meta_path):
            return None

        with open(meta_path) as f:
            data = json.load(f)

        last_built = data.get("last_built")
        age = (time.time() - last_built) if last_built else None

        return IndexStatus(
            index_type=index_type,
            state=IndexState.FRESH,  # caller will re-evaluate
            last_built=last_built,
            age_seconds=age,
            scope=scope,
            path=os.path.join(self._cache_dir, index_type),
            metadata=data.get("metadata", {}),
        )

    def set_status(self, status: IndexStatus) -> None:
        index_dir = os.path.join(self._cache_dir, status.index_type)
        os.makedirs(index_dir, exist_ok=True)
        meta_path = os.path.join(index_dir, self.META_FILENAME)

        data = {
            "index_type": status.index_type,
            "last_built": status.last_built or time.time(),
            "scope": status.scope,
            "metadata": status.metadata,
        }
        with open(meta_path, "w") as f:
            json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Resource resolver
# ---------------------------------------------------------------------------


class ResourceResolver:
    """Resolves index requirements against current index state."""

    def __init__(self, state_store: IndexStateStore) -> None:
        self._state_store = state_store

    def resolve(self, requirements: List[IndexRequirement]) -> ResourcePlan:
        plan = ResourcePlan()

        for req in requirements:
            status = self._state_store.get_status(req.index_type, req.scope)
            decision = self._decide(req, status)
            plan.decisions.append(decision)

            if decision.action == "rebuild":
                plan.blocking_builds.append(req.index_type)
                plan.can_execute = False

            elif decision.action == "skip":
                plan.warnings.append(
                    f"Index {req.index_type!r} is missing (non-required), skipping"
                )

            elif decision.action == "incremental_update":
                plan.async_updates.append(req.index_type)
                if decision.warning:
                    plan.warnings.append(decision.warning)

        return plan

    def _decide(
        self, req: IndexRequirement, status: Optional[IndexStatus]
    ) -> ResourceDecision:
        if status is None:
            return ResourceDecision(
                index_type=req.index_type,
                state=IndexState.MISSING,
                action="rebuild" if req.required else "skip",
                blocking=req.required,
            )

        age = status.age_seconds
        if age is None:
            return ResourceDecision(
                index_type=req.index_type,
                state=IndexState.MISSING,
                action="rebuild" if req.required else "skip",
                blocking=req.required,
            )

        if age <= req.max_age_seconds:
            return ResourceDecision(
                index_type=req.index_type,
                state=IndexState.FRESH,
                action="use",
            )

        age_human = _format_duration(age)
        return ResourceDecision(
            index_type=req.index_type,
            state=IndexState.STALE,
            action="incremental_update",
            warning=f"{req.index_type} is {age_human} old, results may miss recent changes",
        )


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    hours = seconds / 3600
    if hours < 24:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"
