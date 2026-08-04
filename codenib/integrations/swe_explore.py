# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""SWE-Explore's ranked-region contract over CodeNib repository exploration.

The official benchmark asks an explorer for ranked, repository-relative,
1-based inclusive source regions.  CodeNib stores chunk ranges as 0-based
inclusive locations. This module owns only that protocol conversion; planning,
retrieval, graph expansion, reranking, and source validation stay in CodeNib's
native repository-context runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..agent.runtime.explorer import (
    RepositoryContextExplorer,
    RepositoryExplorePreparation,
    RepositoryExploreTrace,
)
from ..model.retrieval_planner import BudgetInput


@dataclass(frozen=True, slots=True)
class SWEExploreContextRegion:
    """One source region in SWE-Explore's external coordinate system."""

    path: str
    start: int
    end: int
    snippet: str | None = None

    def to_record(self) -> dict[str, str | int | None]:
        """Return the official JSON-compatible region representation."""

        return {
            "path": self.path,
            "start": self.start,
            "end": self.end,
            "snippet": self.snippet,
        }


@dataclass(frozen=True, slots=True)
class SWEExploreResult:
    """One ranked result compatible with SWE-Explore's explorer protocol."""

    instance_id: str
    score: float
    regions: tuple[SWEExploreContextRegion, ...]

    def to_record(self) -> dict[str, Any]:
        """Return a stable JSON-ready result."""

        return {
            "instance_id": self.instance_id,
            "score": self.score,
            "regions": [region.to_record() for region in self.regions],
        }


class CodeNibSWEExploreExplorer:
    """Adapt CodeNib's native explorer to SWE-Explore's region protocol."""

    def __init__(
        self,
        explorer: RepositoryContextExplorer,
        *,
        include_snippets: bool = False,
    ) -> None:
        self.runtime = explorer
        self.include_snippets = bool(include_snippets)
        self.last_trace: RepositoryExploreTrace | None = None

    @classmethod
    def from_manifest(
        cls,
        manifest_path: str | Path,
        *,
        include_snippets: bool = False,
        policy: str = "auto",
        budget: BudgetInput = "balanced",
        level: str = "l2",
    ) -> "CodeNibSWEExploreExplorer":
        """Bind the protocol adapter to an existing CodeNib manifest."""

        runtime = RepositoryContextExplorer.from_manifest(
            manifest_path,
            policy=policy,
            budget=budget,
            level=level,
        )
        return cls(runtime, include_snippets=include_snippets)

    @classmethod
    def from_repository(
        cls,
        repo_path: str | Path,
        *,
        manifest_path: str | Path | None = None,
        include_snippets: bool = False,
        policy: str = "auto",
        budget: BudgetInput = "balanced",
        level: str = "l2",
    ) -> "CodeNibSWEExploreExplorer":
        """Resolve and validate the manifest bound to one checkout."""

        runtime = RepositoryContextExplorer.from_repository(
            repo_path,
            manifest_path=manifest_path,
            policy=policy,
            budget=budget,
            level=level,
        )
        return cls(
            runtime,
            include_snippets=include_snippets,
        )

    @property
    def context(self) -> Any:
        return self.runtime.context

    def close(self) -> None:
        self.runtime.close()

    def prepare(self, query: str) -> RepositoryExplorePreparation:
        """Load the native views selected for a benchmark query."""

        return self.runtime.prepare(query)

    def __enter__(self) -> "CodeNibSWEExploreExplorer":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def explore(
        self,
        *,
        instance_id: str,
        query: str,
        top_k: int = 5,
    ) -> list[SWEExploreResult]:
        """Return at most ``top_k`` ranked, valid source regions.

        ``top_k`` is a region-count cutoff.  SWE-Explore's Recall@100/300/500
        metrics apply a separate cumulative-line budget in the scorer.
        """

        result = self.runtime.explore(
            str(query or ""),
            top_k=top_k,
            include_content=self.include_snippets,
        )
        self.last_trace = result.trace
        return [
            SWEExploreResult(
                instance_id=str(instance_id),
                score=item.score,
                regions=(
                    SWEExploreContextRegion(
                        path=item.path,
                        start=item.start_line + 1,
                        end=item.end_line + 1,
                        snippet=item.content,
                    ),
                ),
            )
            for item in result.evidence
        ]


__all__ = [
    "CodeNibSWEExploreExplorer",
    "SWEExploreContextRegion",
    "SWEExploreResult",
]
