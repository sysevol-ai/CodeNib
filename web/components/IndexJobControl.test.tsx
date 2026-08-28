// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type {
  IndexJobStatusResponse,
  RepoIndexStatus,
} from "@/lib/api";
import IndexJobControl, {
  activeIndexJobId,
  indexJobRequestForSurface,
  IndexJobProgress,
  mergeIndexJobEvents,
  prepareIndexJobCreate,
} from "./IndexJobControl";

function statusFixture(): RepoIndexStatus {
  return {
    repo_id: "demo",
    last_indexed_commit: "a".repeat(40),
    current_head: "b".repeat(40),
    stale: true,
    indexes: [
      {
        index_type: "bm25",
        state: "stale",
        stale: true,
        indexed_commit: "a".repeat(40),
        built_at: null,
        update_mode: "rebuild",
        updates_enabled: true,
        update_reason: "",
        job_id: null,
        metrics: null,
      },
      {
        index_type: "vector",
        state: "stale",
        stale: true,
        indexed_commit: "a".repeat(40),
        built_at: null,
        update_mode: "incremental",
        updates_enabled: true,
        update_reason: "",
        job_id: null,
        metrics: null,
      },
      {
        index_type: "symbol_graph",
        state: "missing",
        stale: false,
        indexed_commit: null,
        built_at: null,
        update_mode: "unavailable",
        updates_enabled: false,
        update_reason: "Graph updates are unavailable.",
        job_id: null,
        metrics: null,
      },
    ],
  };
}

function jobFixture(
  overrides: Partial<IndexJobStatusResponse> = {},
): IndexJobStatusResponse {
  return {
    job_id: "job-1",
    repo_id: "demo",
    status: "running",
    cancel_requested: false,
    attempt_count: 1,
    max_attempts: 2,
    indexes: [
      { index_type: "bm25", requested_mode: "incremental", required: true },
    ],
    result_snapshot_id: null,
    error_code: null,
    error_message: null,
    created_at_ms: 1,
    updated_at_ms: 2,
    started_at_ms: 2,
    finished_at_ms: null,
    events: [],
    next_event_sequence: 0,
    ...overrides,
  };
}

describe("IndexJobControl", () => {
  it("selects one writable surface and keeps unavailable ones disabled", () => {
    const html = renderToStaticMarkup(
      <IndexJobControl status={statusFixture()} onRefresh={vi.fn()} />,
    );

    expect(html.match(/type="radio"/g)).toHaveLength(3);
    expect(html.match(/type="radio"[^>]*checked/g)).toHaveLength(1);
    expect(html.match(/type="radio"[^>]*disabled/g)).toHaveLength(1);
    expect(html).toContain("Rebuild BM25");
    expect(html).toContain("BM25 updates require a full rebuild.");
  });

  it("maps advertised capabilities onto honest job requests", () => {
    const status = statusFixture();

    expect(indexJobRequestForSurface(status.indexes[0])).toEqual({
      indexes: ["bm25"],
      mode: "full",
      force: false,
    });
    expect(indexJobRequestForSurface(status.indexes[1])).toEqual({
      indexes: ["vector"],
      mode: "incremental",
      force: false,
    });
    expect(() => indexJobRequestForSurface(status.indexes[2])).toThrow(
      "not writable",
    );
  });

  it("renders fallback results and safe failure messages", () => {
    const job = jobFixture({
      status: "failed",
      error_code: "worker_executor_failed",
      error_message: "The index worker failed while preparing artifacts.",
      finished_at_ms: 4,
      events: [
        {
          sequence: 1,
          attempt_count: 1,
          event_key: "view-result-1",
          kind: "view_result",
          index_type: "bm25",
          effective_mode: "rebuild_fallback",
          outcome: "failed",
          payload: { changed_files: 3 },
          created_at_ms: 3,
        },
      ],
      next_event_sequence: 1,
    });
    const html = renderToStaticMarkup(<IndexJobProgress job={job} />);

    expect(html).toContain("Rebuild fallback");
    expect(html).toContain("3 changed files");
    expect(html).toContain(
      "The index worker failed while preparing artifacts.",
    );
  });
});

describe("index job polling invariants", () => {
  it("deduplicates event pages and retains the newest bounded window", () => {
    const first = jobFixture({
      events: [
        {
          sequence: 1,
          attempt_count: 1,
          event_key: "progress-1",
          kind: "progress",
          index_type: null,
          effective_mode: null,
          outcome: null,
          payload: {},
          created_at_ms: 1,
        },
      ],
      next_event_sequence: 1,
    });
    const second = jobFixture({
      events: [
        first.events[0],
        { ...first.events[0], sequence: 2, event_key: "progress-2" },
      ],
      next_event_sequence: 2,
    });

    expect(
      mergeIndexJobEvents(first, second).events.map((event) => event.sequence),
    ).toEqual([1, 2]);
  });

  it("accepts one active job and rejects cross-job overlays", () => {
    const status = statusFixture();
    status.indexes[0] = { ...status.indexes[0], job_id: "job-1" };
    status.indexes[1] = { ...status.indexes[1], job_id: "job-1" };
    expect(activeIndexJobId(status)).toBe("job-1");

    status.indexes[1] = { ...status.indexes[1], job_id: "job-2" };
    expect(() => activeIndexJobId(status)).toThrow(
      "Index status names conflicting active jobs",
    );
  });

  it("reuses an idempotency key only for the same canonical intent", () => {
    const keyFactory = vi
      .fn()
      .mockReturnValueOnce("key-1")
      .mockReturnValueOnce("key-2");
    const first = prepareIndexJobCreate(null, "intent-a", keyFactory);
    const retry = prepareIndexJobCreate(first, "intent-a", keyFactory);
    const changed = prepareIndexJobCreate(retry, "intent-b", keyFactory);

    expect(retry).toBe(first);
    expect(changed.idempotencyKey).toBe("key-2");
    expect(keyFactory).toHaveBeenCalledTimes(2);
  });
});
