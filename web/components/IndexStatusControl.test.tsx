// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { RepoIndexStatus } from "@/lib/api";
import {
  aggregateIndexState,
  hasCanonicalIndexSurfaces,
  IndexStatusDetails,
} from "./IndexStatusControl";

function statusFixture(): RepoIndexStatus {
  return {
    repo_id: "demo",
    last_indexed_commit: "1234567890abcdef",
    current_head: "fedcba0987654321",
    stale: true,
    indexes: [
      {
        index_type: "bm25",
        state: "stale",
        stale: true,
        indexed_commit: "1234567890abcdef",
        built_at: "2026-08-28T00:00:00Z",
        update_mode: "rebuild",
        updates_enabled: true,
        update_reason: "",
        job_id: null,
        metrics: null,
      },
      {
        index_type: "vector",
        state: "built",
        stale: false,
        indexed_commit: "fedcba0987654321",
        built_at: "2026-08-28T00:01:00Z",
        update_mode: "incremental",
        updates_enabled: true,
        update_reason: "",
        job_id: null,
        metrics: {
          changed_files: 4,
          chunks_reembedded: 12,
          chunks_from_cache: 36,
          cache_hit_rate: 0.75,
          new_commit: "fedcba0987654321",
        },
      },
      {
        index_type: "symbol_graph",
        state: "missing",
        stale: false,
        indexed_commit: null,
        built_at: null,
        update_mode: "unavailable",
        updates_enabled: false,
        update_reason: "No supported graph backend is configured.",
        job_id: null,
        metrics: null,
      },
    ],
  };
}

describe("IndexStatusDetails", () => {
  it("renders exactly the three primary surfaces and honest update modes", () => {
    const html = renderToStaticMarkup(
      <IndexStatusDetails status={statusFixture()} />,
    );

    expect(html.match(/class="index-status-row"/g)).toHaveLength(3);
    expect(html).toContain("BM25");
    expect(html).toContain("Embeddings");
    expect(html).toContain("Symbol graph");
    expect(html).toContain("Rebuild on update");
    expect(html).toContain("Incremental");
    expect(html).toContain("Updates unavailable");
    expect(html).toContain("No supported graph backend is configured.");
  });

  it("shows incremental embedding metrics and both commit positions", () => {
    const html = renderToStaticMarkup(
      <IndexStatusDetails status={statusFixture()} />,
    );

    expect(html).toContain("Changed files");
    expect(html).toContain(">4<");
    expect(html).toContain("Re-embedded");
    expect(html).toContain(">12<");
    expect(html).toContain("From cache");
    expect(html).toContain(">36<");
    expect(html).toContain(">75%<");
    expect(html).toContain("New commit");
    expect(html).toContain("12345678");
    expect(html).toContain("fedcba09");
    expect(html).toContain("Update available");
  });
});

describe("index status invariants", () => {
  it("requires the canonical three-surface order", () => {
    const canonical = statusFixture();
    expect(hasCanonicalIndexSurfaces(canonical)).toBe(true);
    expect(
      hasCanonicalIndexSurfaces({
        ...canonical,
        indexes: canonical.indexes.slice(0, 2),
      }),
    ).toBe(false);
    expect(
      hasCanonicalIndexSurfaces({
        ...canonical,
        indexes: [
          canonical.indexes[1],
          canonical.indexes[0],
          canonical.indexes[2],
        ],
      }),
    ).toBe(false);

    const conflicting = statusFixture();
    conflicting.indexes[0] = {
      ...conflicting.indexes[0],
      state: "updating",
      job_id: "job-1",
    };
    conflicting.indexes[1] = {
      ...conflicting.indexes[1],
      state: "updating",
      job_id: "job-2",
    };
    expect(hasCanonicalIndexSurfaces(conflicting)).toBe(false);
  });

  it("keeps failures visible above in-progress and stale surfaces", () => {
    const status = statusFixture();
    status.indexes[0] = { ...status.indexes[0], state: "updating" };
    status.indexes[2] = { ...status.indexes[2], state: "failed" };

    expect(aggregateIndexState(status)).toBe("failed");
  });

  it("does not hide repository-level staleness behind built surfaces", () => {
    const status = statusFixture();
    status.indexes = status.indexes.map((surface) => ({
      ...surface,
      state: "built",
      stale: false,
    }));

    expect(aggregateIndexState(status)).toBe("stale");
  });
});
