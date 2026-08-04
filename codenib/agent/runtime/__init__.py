# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Runtime support types for agent execution."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..._lazy import exported_dir, load_export
from .context import ContextLedger, ContextLedgerEntry
from .trace import AGENT_TRACE_SCHEMA_VERSION, AgentRunTrace, AgentTraceEvent

if TYPE_CHECKING:  # pragma: no cover - imported only by static analyzers
    from .explorer import (
        REPOSITORY_EXPLORER_POLICIES,
        RepositoryContextExplorer,
        RepositoryEvidence,
        RepositoryExplorePreparation,
        RepositoryExplorerCapabilityError,
        RepositoryExploreResult,
        RepositoryExploreTrace,
        normalize_repository_explorer_policy,
        repository_explorer_build_views,
    )

_EXPORTS = {
    "REPOSITORY_EXPLORER_POLICIES": (
        "codenib.agent.runtime.explorer",
        "REPOSITORY_EXPLORER_POLICIES",
    ),
    "RepositoryContextExplorer": (
        "codenib.agent.runtime.explorer",
        "RepositoryContextExplorer",
    ),
    "RepositoryEvidence": (
        "codenib.agent.runtime.explorer",
        "RepositoryEvidence",
    ),
    "RepositoryExplorePreparation": (
        "codenib.agent.runtime.explorer",
        "RepositoryExplorePreparation",
    ),
    "RepositoryExploreResult": (
        "codenib.agent.runtime.explorer",
        "RepositoryExploreResult",
    ),
    "RepositoryExploreTrace": (
        "codenib.agent.runtime.explorer",
        "RepositoryExploreTrace",
    ),
    "RepositoryExplorerCapabilityError": (
        "codenib.agent.runtime.explorer",
        "RepositoryExplorerCapabilityError",
    ),
    "normalize_repository_explorer_policy": (
        "codenib.agent.runtime.explorer",
        "normalize_repository_explorer_policy",
    ),
    "repository_explorer_build_views": (
        "codenib.agent.runtime.explorer",
        "repository_explorer_build_views",
    ),
}

__all__ = [
    "AGENT_TRACE_SCHEMA_VERSION",
    "AgentRunTrace",
    "AgentTraceEvent",
    "ContextLedger",
    "ContextLedgerEntry",
    "REPOSITORY_EXPLORER_POLICIES",
    "RepositoryContextExplorer",
    "RepositoryEvidence",
    "RepositoryExplorePreparation",
    "RepositoryExploreResult",
    "RepositoryExploreTrace",
    "RepositoryExplorerCapabilityError",
    "normalize_repository_explorer_policy",
    "repository_explorer_build_views",
]


def __getattr__(name: str) -> Any:
    return load_export(globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return exported_dir(globals(), _EXPORTS)
