// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import type { IndexSurfaceState, RepoIndexStatus } from "./api";

const INDEX_TYPES = ["bm25", "vector", "symbol_graph"] as const;

const STATE_PRIORITY: Record<IndexSurfaceState, number> = {
  built: 0,
  missing: 1,
  stale: 2,
  updating: 3,
  failed: 4,
};

export function hasCanonicalIndexSurfaces(status: RepoIndexStatus): boolean {
  const activeJobIds = new Set(
    status.indexes
      .map((surface) => surface.job_id)
      .filter((jobId): jobId is string => Boolean(jobId)),
  );
  return (
    status.indexes.length === INDEX_TYPES.length &&
    activeJobIds.size <= 1 &&
    status.indexes.every(
      (surface, index) =>
        surface.index_type === INDEX_TYPES[index] &&
        (surface.state === "updating") === Boolean(surface.job_id),
    )
  );
}

export function aggregateIndexState(
  status: RepoIndexStatus,
): IndexSurfaceState {
  return status.indexes.reduce<IndexSurfaceState>(
    (current, surface) =>
      STATE_PRIORITY[surface.state] > STATE_PRIORITY[current]
        ? surface.state
        : current,
    status.stale ? "stale" : "built",
  );
}
