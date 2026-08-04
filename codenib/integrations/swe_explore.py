# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""SWE-Explore's ranked-region contract over CodeNib repository views.

The official benchmark asks an explorer for ranked, repository-relative,
1-based inclusive source regions.  CodeNib stores chunk ranges as 0-based
inclusive locations.  This module owns that conversion and keeps benchmark
serving separate from index construction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._repository import RepositoryAdapter, RepositoryPathError

_RUNTIME_VIEWS = frozenset({"bm25"})


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
    """Serve ranked CodeNib BM25 chunks under SWE-Explore's protocol.

    The explorer loads only the BM25 view declared by the manifest.  It does
    not build or update indexes; callers must materialize the view explicitly
    before constructing the explorer.
    """

    def __init__(self, context: Any, *, include_snippets: bool = False) -> None:
        self.repository = RepositoryAdapter(context, require_graph=False)
        self.bm25 = self.repository.require_view("bm25", "bm25")
        self.include_snippets = bool(include_snippets)
        self._source_lines: dict[str, list[str]] = {}

    @classmethod
    def from_manifest(
        cls,
        manifest_path: str | Path,
        *,
        include_snippets: bool = False,
    ) -> "CodeNibSWEExploreExplorer":
        """Load only the BM25 artifact from an existing CodeNib manifest."""

        from ..mcp.context import ServerContext

        context = ServerContext.load(manifest_path, views=_RUNTIME_VIEWS)
        return cls(context, include_snippets=include_snippets)

    @classmethod
    def from_repository(
        cls,
        repo_path: str | Path,
        *,
        manifest_path: str | Path | None = None,
        include_snippets: bool = False,
    ) -> "CodeNibSWEExploreExplorer":
        """Resolve and validate the manifest bound to one checkout."""

        from ..clients._manifest import select_checkout_manifest

        selected = select_checkout_manifest(
            repo_path,
            integration_name="SWE-Explore",
            manifest_path=manifest_path,
        )
        return cls.from_manifest(selected, include_snippets=include_snippets)

    @property
    def context(self) -> Any:
        return self.repository.context

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

        if isinstance(top_k, bool) or not isinstance(top_k, int):
            raise TypeError("top_k must be an integer")
        if top_k < 0:
            raise ValueError("top_k must be non-negative")
        if top_k == 0 or not str(query or "").strip():
            return []

        max_candidates = max(top_k, top_k * 4)
        index_limit = getattr(self.bm25, "max_k", max_candidates)
        if isinstance(index_limit, int) and index_limit > 0:
            max_candidates = min(max_candidates, index_limit)
        candidates = self.bm25.search(
            query=str(query),
            top_k=max_candidates,
            return_code_content=False,
        )

        results: list[SWEExploreResult] = []
        seen: set[tuple[str, int, int]] = set()
        for candidate in candidates:
            region = self._region_from_candidate(candidate)
            if region is None:
                continue
            key = (region.path, region.start, region.end)
            if key in seen:
                continue
            seen.add(key)
            results.append(
                SWEExploreResult(
                    instance_id=str(instance_id),
                    score=_finite_score(getattr(candidate, "score", 0.0)),
                    regions=(region,),
                )
            )
            if len(results) >= top_k:
                break
        return results

    def _region_from_candidate(self, candidate: Any) -> SWEExploreContextRegion | None:
        raw_path = str(getattr(candidate, "file", "") or "").strip()
        if not raw_path:
            return None
        try:
            path = self._relative_path(raw_path)
            lines = self._lines(path)
        except (OSError, RepositoryPathError, UnicodeError):
            return None
        if not lines:
            return None

        start = _line_number(getattr(candidate, "start_line", None))
        end = _line_number(getattr(candidate, "end_line", None))
        if start is None or end is None or end < start or start >= len(lines):
            return None
        end = min(end, len(lines) - 1)
        snippet = "\n".join(lines[start : end + 1]) if self.include_snippets else None
        return SWEExploreContextRegion(
            path=path,
            start=start + 1,
            end=end + 1,
            snippet=snippet,
        )

    def _relative_path(self, value: str) -> str:
        raw = Path(value).expanduser()
        if raw.is_absolute():
            try:
                value = raw.resolve().relative_to(self.repository.repo_root).as_posix()
            except ValueError as exc:
                raise RepositoryPathError(
                    f"retrieval result is outside the manifest repository: {raw}"
                ) from exc
        return self.repository.normalize_path(value)

    def _lines(self, path: str) -> list[str]:
        lines = self._source_lines.get(path)
        if lines is None:
            lines = self.repository.source_lines(path)
            self._source_lines[path] = lines
        return lines


def _line_number(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _finite_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return score if math.isfinite(score) else 0.0


__all__ = [
    "CodeNibSWEExploreExplorer",
    "SWEExploreContextRegion",
    "SWEExploreResult",
]
