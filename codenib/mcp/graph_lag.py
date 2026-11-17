# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""What a loaded symbol graph does not know about the current source.

An interactive connection may keep answering from a graph that was built
before the latest edits instead of waiting minutes for a rebuild. That is
only honest if every answer names the files the graph is behind on, so the
caller can re-check exactly those in source. This module holds that list
and attaches it to graph results.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable

from ..compiler.source_inventory import SourceDelta

_MAX_REPORTED_FILES = 20
# A diagnostic sentence is capped at 512 characters by the explore tool.
_MAX_NAMED_FILES = 6


@dataclass(frozen=True, slots=True)
class GraphSourceLag:
    """Graph-relevant source files that changed after the graph was built."""

    base_commit: str
    base_source_fingerprint: str
    modified: tuple[str, ...]
    added: tuple[str, ...]
    deleted: tuple[str, ...]

    @property
    def file_count(self) -> int:
        return len(self.modified) + len(self.added) + len(self.deleted)

    def summary(self) -> dict[str, Any]:
        """A bounded description for tool results and status."""

        def bounded(paths: tuple[str, ...]) -> list[str]:
            return list(paths[:_MAX_REPORTED_FILES])

        return {
            "base_commit": self.base_commit,
            "changed_file_count": self.file_count,
            "modified": bounded(self.modified),
            "added": bounded(self.added),
            "deleted": bounded(self.deleted),
            "truncated": any(
                len(paths) > _MAX_REPORTED_FILES
                for paths in (self.modified, self.added, self.deleted)
            ),
        }

    def message(self) -> str:
        """One sentence a caller can act on."""

        if not self.file_count:
            return (
                "The relationship graph was built before the latest commit; "
                "no indexed source file has changed since."
            )
        names = [*self.modified, *self.deleted, *self.added]
        shown = ", ".join(names[:_MAX_NAMED_FILES])
        more = (
            f" and {len(names) - _MAX_NAMED_FILES} more"
            if len(names) > _MAX_NAMED_FILES
            else ""
        )
        return (
            "The relationship graph was built before the latest edits. "
            f"Relationships into or out of these {len(names)} file(s) may be "
            f"missing or out of date; confirm them in source: {shown}{more}."
        )


def graph_source_lag(
    graph: Any,
    *,
    base_commit: str,
    base_source_fingerprint: str,
    delta: SourceDelta,
) -> GraphSourceLag:
    """Keep only the changed files the graph could have described."""

    files = {str(name) for name in getattr(graph, "_file_nodes", None) or ()}
    vertices = getattr(getattr(graph, "graph", None), "vs", None)
    if vertices is not None and "file" in vertices.attributes():
        files.update(name for name in vertices["file"] if isinstance(name, str))
    suffixes = {PurePosixPath(name).suffix for name in files} - {""}

    def relevant(paths: Iterable[str]) -> tuple[str, ...]:
        return tuple(
            path
            for path in paths
            if path in files or PurePosixPath(path).suffix in suffixes
        )

    return GraphSourceLag(
        base_commit=base_commit,
        base_source_fingerprint=base_source_fingerprint,
        modified=relevant(delta.modified),
        added=relevant(delta.added),
        deleted=relevant(delta.deleted),
    )


def annotate_graph_result(result: Any, lag: GraphSourceLag) -> Any:
    """Mark a graph tool result with the files the graph is behind on.

    A mapping gains one ``graph_lag`` entry. A list keeps its shape; each
    row that points into a changed file gains ``stale_source``.
    """

    if isinstance(result, dict):
        if "error" not in result:
            result["graph_lag"] = {**lag.summary(), "detail": lag.message()}
        return result
    if isinstance(result, list):
        changed = {*lag.modified, *lag.deleted}
        for row in result:
            if not isinstance(row, dict):
                continue
            path = row.get("file") or row.get("file_path")
            if isinstance(path, str) and path in changed:
                row["stale_source"] = True
    return result
