// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  fetchIndexStatus,
  type IndexSurfaceState,
  type IndexSurfaceStatus,
  type IndexType,
  type RepoIndexStatus,
} from "@/lib/api";
import {
  aggregateIndexState,
  hasCanonicalIndexSurfaces,
} from "@/lib/indexStatus";
import IndexJobControl from "./IndexJobControl";

export { aggregateIndexState, hasCanonicalIndexSurfaces } from "@/lib/indexStatus";

const INDEX_LABELS: Record<IndexType, string> = {
  bm25: "BM25",
  vector: "Embeddings",
  symbol_graph: "Symbol graph",
};

const STATE_LABELS: Record<IndexSurfaceState, string> = {
  built: "Built",
  missing: "Missing",
  stale: "Stale",
  updating: "Updating",
  failed: "Failed",
};

const MODE_LABELS: Record<IndexSurfaceStatus["update_mode"], string> = {
  incremental: "Incremental",
  patch: "Patch",
  rebuild: "Rebuild on update",
  unavailable: "Updates unavailable",
};

function shortCommit(commit: string | null): string {
  return commit ? commit.slice(0, 8) : "—";
}

function MetricList({ surface }: { surface: IndexSurfaceStatus }) {
  const metrics = surface.metrics;
  if (!metrics) return null;
  const values = [
    metrics.changed_files == null
      ? null
      : ["Changed files", String(metrics.changed_files)],
    metrics.chunks_reembedded == null
      ? null
      : ["Re-embedded", String(metrics.chunks_reembedded)],
    metrics.chunks_from_cache == null
      ? null
      : ["From cache", String(metrics.chunks_from_cache)],
    metrics.cache_hit_rate == null
      ? null
      : ["Cache hits", `${Math.round(metrics.cache_hit_rate * 100)}%`],
    metrics.new_commit == null
      ? null
      : ["New commit", shortCommit(metrics.new_commit)],
  ].filter((item): item is string[] => item !== null);
  if (!values.length) return null;
  return (
    <dl className="index-status-metrics">
      {values.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd className="mono">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function IndexStatusDetails({ status }: { status: RepoIndexStatus }) {
  return (
    <div className="index-status-details">
      <ul className="index-status-list">
        {status.indexes.map((surface) => (
          <li className="index-status-row" key={surface.index_type}>
            <div className="index-status-row-head">
              <span className="index-status-name">
                {INDEX_LABELS[surface.index_type]}
              </span>
              <span className={`index-state ${surface.state}`}>
                {STATE_LABELS[surface.state]}
              </span>
            </div>
            <div className="index-status-mode">
              {MODE_LABELS[surface.update_mode]}
              {surface.indexed_commit && (
                <span className="mono">
                  {shortCommit(surface.indexed_commit)}
                </span>
              )}
            </div>
            {!surface.updates_enabled && surface.update_reason && (
              <p className="index-status-reason">{surface.update_reason}</p>
            )}
            <MetricList surface={surface} />
          </li>
        ))}
      </ul>
      <div className="index-status-commits">
        <span>
          Indexed <b className="mono">{shortCommit(status.last_indexed_commit)}</b>
        </span>
        <span>
          HEAD <b className="mono">{shortCommit(status.current_head)}</b>
        </span>
        {status.stale && <span className="index-status-stale">Update available</span>}
      </div>
    </div>
  );
}

export default function IndexStatusControl({ repoId }: { repoId: string }) {
  const [status, setStatus] = useState<RepoIndexStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Supersede and abort old reads so repo navigation cannot publish a status
  // snapshot for the previous route after a faster request has completed.
  const requestId = useRef(0);
  const activeRequest = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    activeRequest.current?.abort();
    const controller = new AbortController();
    activeRequest.current = controller;
    const currentRequest = ++requestId.current;
    setLoading(true);
    setError(null);
    try {
      const next = await fetchIndexStatus(repoId, {
        signal: controller.signal,
      });
      if (next.repo_id !== repoId || !hasCanonicalIndexSurfaces(next)) {
        throw new Error("Index status response is incomplete");
      }
      if (requestId.current === currentRequest) setStatus(next);
    } catch (failure) {
      if (
        controller.signal.aborted ||
        requestId.current !== currentRequest
      ) {
        return;
      }
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      if (requestId.current === currentRequest) {
        activeRequest.current = null;
        setLoading(false);
      }
    }
  }, [repoId]);

  useEffect(() => {
    setStatus(null);
    void load();
    return () => {
      requestId.current += 1;
      activeRequest.current?.abort();
      activeRequest.current = null;
    };
  }, [load]);

  const visibleStatus = status?.repo_id === repoId ? status : null;
  const aggregate = visibleStatus ? aggregateIndexState(visibleStatus) : null;
  return (
    <details className="index-status-control">
      <summary>
        <span>Indexes</span>
        {aggregate ? (
          <span className={`index-state ${aggregate}`}>
            {STATE_LABELS[aggregate]}
          </span>
        ) : (
          <span className="index-status-summary-note">
            {loading ? "Checking" : "Unavailable"}
          </span>
        )}
      </summary>
      <div className="index-status-panel">
        <div className="index-status-panel-head">
          <div>
            <strong>Repository indexes</strong>
            <p>Current derived artifacts and their safe update modes.</p>
          </div>
          <button type="button" onClick={() => void load()} disabled={loading}>
            {loading ? "Checking…" : "Refresh"}
          </button>
        </div>
        {error ? (
          <div className="index-status-error" role="alert">
            {error}
          </div>
        ) : visibleStatus ? (
          <>
            <IndexStatusDetails status={visibleStatus} />
            <IndexJobControl
              key={visibleStatus.repo_id}
              status={visibleStatus}
              onRefresh={load}
            />
          </>
        ) : (
          <div className="index-status-loading" role="status">
            Loading index status…
          </div>
        )}
      </div>
    </details>
  );
}
