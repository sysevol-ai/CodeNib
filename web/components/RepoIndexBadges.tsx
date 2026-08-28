// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useEffect, useState } from "react";

import {
  fetchIndexStatus,
  type IndexSurfaceState,
  type IndexSurfaceStatus,
  type IndexType,
  type RepoIndexStatus,
} from "@/lib/api";
import { hasCanonicalIndexSurfaces } from "@/lib/indexStatus";

const INDEX_LABELS: Record<IndexType, string> = {
  bm25: "BM25",
  vector: "Embeddings",
  symbol_graph: "Graph",
};

const STATE_LABELS: Record<IndexSurfaceState, string> = {
  built: "Built",
  missing: "Missing",
  stale: "Stale",
  updating: "Updating",
  failed: "Failed",
};

const MODE_LABELS: Record<IndexSurfaceStatus["update_mode"], string> = {
  incremental: "incremental update",
  patch: "patch update",
  rebuild: "rebuild on update",
  unavailable: "updates unavailable",
};

function badgeTitle(surface: IndexSurfaceStatus): string {
  const details = [
    `${INDEX_LABELS[surface.index_type]}: ${STATE_LABELS[surface.state]}`,
    MODE_LABELS[surface.update_mode],
  ];
  if (surface.indexed_commit) {
    details.push(`indexed ${surface.indexed_commit.slice(0, 8)}`);
  }
  if (!surface.updates_enabled && surface.update_reason) {
    details.push(surface.update_reason);
  }
  return details.join(" · ");
}

export function RepoIndexBadgeList({ status }: { status: RepoIndexStatus }) {
  return (
    <div className="repo-index-summary" aria-label="Repository index status">
      <div className="repo-index-badges">
        {status.indexes.map((surface) => (
          <span
            className={`repo-index-badge index-state ${surface.state}`}
            title={badgeTitle(surface)}
            key={surface.index_type}
          >
            {INDEX_LABELS[surface.index_type]} · {STATE_LABELS[surface.state]}
          </span>
        ))}
      </div>
      {status.stale && (
        <span className="repo-index-freshness">Update available</span>
      )}
    </div>
  );
}

export default function RepoIndexBadges({ repoId }: { repoId: string }) {
  const [status, setStatus] = useState<RepoIndexStatus | null>(null);
  const [unavailable, setUnavailable] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setStatus(null);
    setUnavailable(false);

    void fetchIndexStatus(repoId, { signal: controller.signal })
      .then((next) => {
        if (!active) return;
        if (next.repo_id !== repoId || !hasCanonicalIndexSurfaces(next)) {
          throw new Error("Index status response is incomplete");
        }
        setStatus(next);
      })
      .catch(() => {
        if (active && !controller.signal.aborted) setUnavailable(true);
      });

    return () => {
      active = false;
      controller.abort();
    };
  }, [repoId]);

  if (status) return <RepoIndexBadgeList status={status} />;
  return (
    <div
      className={`repo-index-summary ${
        unavailable ? "unavailable" : "loading"
      }`}
    >
      {unavailable ? "Index status unavailable" : "Checking indexes…"}
    </div>
  );
}
