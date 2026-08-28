// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import {
  createIndexJob,
  fetchIndexJob,
  type IndexJobCreateRequest,
  type IndexJobEvent,
  type IndexJobStatusResponse,
  type IndexSurfaceStatus,
  type IndexType,
  type RepoIndexStatus,
} from "@/lib/api";

const INDEX_LABELS: Record<IndexType, string> = {
  bm25: "BM25",
  vector: "Embeddings",
  symbol_graph: "Symbol graph",
};
const ACTIVE_JOB_STATES = new Set<IndexJobStatusResponse["status"]>([
  "queued",
  "running",
]);
const JOB_STATE_LABELS: Record<IndexJobStatusResponse["status"], string> = {
  queued: "Queued",
  running: "Running",
  succeeded: "Succeeded",
  failed: "Failed",
  cancelled: "Cancelled",
};
const EFFECTIVE_MODE_LABELS: Record<
  NonNullable<IndexJobEvent["effective_mode"]>,
  string
> = {
  full: "Full rebuild",
  incremental: "Incremental",
  rebuild_fallback: "Rebuild fallback",
  unavailable: "Unavailable",
};
const EVENT_PAGE_SIZE = 64;
const MAX_RETAINED_EVENTS = 64;

export interface PendingIndexJobCreate {
  signature: string;
  idempotencyKey: string;
}

export function activeIndexJobId(status: RepoIndexStatus): string | null {
  const jobIds = [
    ...new Set(
      status.indexes
        .map((surface) => surface.job_id)
        .filter((jobId): jobId is string => Boolean(jobId)),
    ),
  ];
  if (jobIds.length > 1) {
    throw new Error("Index status names conflicting active jobs");
  }
  return jobIds[0] ?? null;
}

export function mergeIndexJobEvents(
  previous: IndexJobStatusResponse | null,
  next: IndexJobStatusResponse,
): IndexJobStatusResponse {
  if (!previous || previous.job_id !== next.job_id) return next;
  const events = new Map<number, IndexJobEvent>();
  for (const event of [...previous.events, ...next.events]) {
    events.set(event.sequence, event);
  }
  return {
    ...next,
    events: [...events.values()]
      .sort((left, right) => left.sequence - right.sequence)
      .slice(-MAX_RETAINED_EVENTS),
  };
}

function createIdempotencyKey(): string {
  const randomUUID = globalThis.crypto?.randomUUID;
  if (typeof randomUUID !== "function") {
    throw new Error("Secure index update IDs are unavailable in this browser");
  }
  return `web-index-${randomUUID.call(globalThis.crypto)}`;
}

export function prepareIndexJobCreate(
  current: PendingIndexJobCreate | null,
  signature: string,
  keyFactory: () => string = createIdempotencyKey,
): PendingIndexJobCreate {
  if (current?.signature === signature) return current;
  return { signature, idempotencyKey: keyFactory() };
}

export function indexJobRequestForSurface(
  surface: IndexSurfaceStatus,
): IndexJobCreateRequest {
  if (!surface.updates_enabled || surface.update_mode === "unavailable") {
    throw new Error("The selected index surface is not writable");
  }
  return {
    indexes: [surface.index_type],
    mode: surface.update_mode === "rebuild" ? "full" : "incremental",
    force: false,
  };
}

function eventMetricSummary(event: IndexJobEvent): string | null {
  const parts: string[] = [];
  const changedFiles = event.payload.changed_files;
  if (typeof changedFiles === "number") {
    parts.push(`${changedFiles} changed file${changedFiles === 1 ? "" : "s"}`);
  }
  const reembedded = event.payload.chunks_reembedded;
  if (typeof reembedded === "number") parts.push(`${reembedded} re-embedded`);
  const fromCache = event.payload.chunks_from_cache;
  if (typeof fromCache === "number") parts.push(`${fromCache} from cache`);
  const cacheHitRate = event.payload.cache_hit_rate;
  if (typeof cacheHitRate === "number") {
    parts.push(`${Math.round(cacheHitRate * 100)}% cache hits`);
  }
  return parts.length ? parts.join(" · ") : null;
}

export function IndexJobProgress({ job }: { job: IndexJobStatusResponse }) {
  const resultEvents = job.events.filter(
    (
      event,
    ): event is IndexJobEvent & {
      index_type: IndexType;
      effective_mode: NonNullable<IndexJobEvent["effective_mode"]>;
      outcome: NonNullable<IndexJobEvent["outcome"]>;
    } =>
      event.kind === "view_result" &&
      event.index_type !== null &&
      event.effective_mode !== null &&
      event.outcome !== null,
  );
  return (
    <div className={`index-job-progress ${job.status}`} role="status">
      <div className="index-job-progress-head">
        <span
          className={`index-state ${
            job.status === "succeeded" ? "built" : job.status
          }`}
        >
          {JOB_STATE_LABELS[job.status]}
        </span>
        <span className="mono">
          attempt {job.attempt_count}/{job.max_attempts}
        </span>
      </div>
      {job.error_message && (
        <p className="index-job-error" role="alert">
          {job.error_message}
        </p>
      )}
      {resultEvents.length > 0 && (
        <ul className="index-job-results">
          {resultEvents.map((event) => {
            const metrics = eventMetricSummary(event);
            return (
              <li key={event.event_key}>
                <div>
                  <span>{INDEX_LABELS[event.index_type]}</span>
                  <span className={`index-job-outcome ${event.outcome}`}>
                    {event.outcome}
                  </span>
                </div>
                <span>{EFFECTIVE_MODE_LABELS[event.effective_mode]}</span>
                {metrics && <small>{metrics}</small>}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

export default function IndexJobControl({
  status,
  onRefresh,
}: {
  status: RepoIndexStatus;
  onRefresh: () => Promise<void>;
}) {
  const enabledTypes = useMemo(
    () =>
      status.indexes
        .filter((surface) => surface.updates_enabled)
        .map((surface) => surface.index_type),
    [status.indexes],
  );
  const [selected, setSelected] = useState<IndexType | null>(
    () => enabledTypes[0] ?? null,
  );
  const [job, setJob] = useState<IndexJobStatusResponse | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pollRevision, setPollRevision] = useState(0);
  const pendingCreate = useRef<PendingIndexJobCreate | null>(null);
  const statusJobId = activeIndexJobId(status);
  const jobIsActive = Boolean(job && ACTIVE_JOB_STATES.has(job.status));
  const pollingJobId = jobIsActive ? job!.job_id : job ? null : statusJobId;
  const busy = submitting || jobIsActive || Boolean(statusJobId && !job);

  const enabledSignature = enabledTypes.join("\u0000");
  useEffect(() => {
    const enabled = new Set<IndexType>(
      enabledSignature
        ? (enabledSignature.split("\u0000") as IndexType[])
        : [],
    );
    setSelected((current) =>
      current && enabled.has(current)
        ? current
        : (enabled.values().next().value ?? null),
    );
  }, [enabledSignature]);

  useEffect(() => {
    if (!pollingJobId) return;
    let stopped = false;
    let timer: number | undefined;
    let cursor =
      job?.job_id === pollingJobId ? job.next_event_sequence : 0;

    const poll = async () => {
      try {
        const next = await fetchIndexJob(pollingJobId, {
          afterSequence: cursor,
          eventLimit: EVENT_PAGE_SIZE,
        });
        if (stopped) return;
        if (next.job_id !== pollingJobId || next.repo_id !== status.repo_id) {
          throw new Error("Index job status escaped its repository");
        }
        cursor = next.next_event_sequence;
        setJob((current) => mergeIndexJobEvents(current, next));
        setError(null);
        const terminal = !ACTIVE_JOB_STATES.has(next.status);
        const pageMayContinue = next.events.length === EVENT_PAGE_SIZE;
        if (terminal) {
          await onRefresh();
          return;
        }
        timer = window.setTimeout(poll, pageMayContinue ? 0 : 1_000);
      } catch (failure) {
        if (!stopped) {
          setError(failure instanceof Error ? failure.message : String(failure));
        }
      }
    };

    timer = window.setTimeout(poll, job ? 750 : 0);
    return () => {
      stopped = true;
      if (timer != null) window.clearTimeout(timer);
    };
  }, [
    job?.job_id,
    job?.status,
    onRefresh,
    pollRevision,
    pollingJobId,
    status.repo_id,
  ]);

  const selectedSurface =
    status.indexes.find(
      (surface) =>
        surface.index_type === selected && surface.updates_enabled,
    ) ?? null;

  const submit = async () => {
    if (!selectedSurface || busy) return;
    const request = indexJobRequestForSurface(selectedSurface);
    const signature = JSON.stringify({
      repoId: status.repo_id,
      ...request,
    });
    const prepared = prepareIndexJobCreate(pendingCreate.current, signature);
    pendingCreate.current = prepared;
    setSubmitting(true);
    setError(null);
    setJob(null);
    try {
      const created = await createIndexJob(
        status.repo_id,
        request,
        { idempotencyKey: prepared.idempotencyKey },
      );
      if (created.repo_id !== status.repo_id) {
        throw new Error("Created index job escaped its repository");
      }
      pendingCreate.current = null;
      setJob(created);
      setPollRevision((revision) => revision + 1);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setSubmitting(false);
    }
  };

  if (!enabledTypes.length) {
    return (
      <div className="index-job-unavailable">
        This server has no writable index surfaces for this repository.
      </div>
    );
  }

  const requiresRebuild = selectedSurface?.update_mode === "rebuild";
  return (
    <div className="index-job-control">
      <div className="index-job-control-head">
        <strong>Update indexes</strong>
        <span>Select one writable surface.</span>
      </div>
      <div
        className="index-job-selection"
        role="radiogroup"
        aria-label="Writable index surface"
      >
        {status.indexes.map((surface) => (
          <label key={surface.index_type}>
            <input
              type="radio"
              name="index-job-surface"
              checked={selected === surface.index_type}
              disabled={!surface.updates_enabled || busy}
              onChange={() => setSelected(surface.index_type)}
            />
            <span>{INDEX_LABELS[surface.index_type]}</span>
          </label>
        ))}
      </div>
      {requiresRebuild && selectedSurface && (
        <p className="index-job-fallback-note">
          {INDEX_LABELS[selectedSurface.index_type]} updates require a full
          rebuild.
        </p>
      )}
      <div className="index-job-actions">
        <button
          type="button"
          disabled={!selectedSurface || busy}
          onClick={() => void submit()}
        >
          {selectedSurface
            ? `${requiresRebuild ? "Rebuild" : "Update"} ${
                INDEX_LABELS[selectedSurface.index_type]
              }`
            : "Select an index"}
        </button>
      </div>
      {submitting && <div className="index-job-submitting">Creating update…</div>}
      {error && (
        <div className="index-job-request-error" role="alert">
          <span>{error}</span>
          {pollingJobId && (
            <button
              type="button"
              onClick={() => setPollRevision((revision) => revision + 1)}
            >
              Retry status
            </button>
          )}
        </div>
      )}
      {job && <IndexJobProgress job={job} />}
    </div>
  );
}
