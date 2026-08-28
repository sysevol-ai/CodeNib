// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { RepoIndexStatus } from "@/lib/api";
import AskIndexFreshness, { askIndexAttention } from "./AskIndexFreshness";

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
        state: "missing",
        stale: false,
        indexed_commit: null,
        built_at: null,
        update_mode: "unavailable",
        updates_enabled: false,
        update_reason: "No vector writer is configured.",
        job_id: null,
        metrics: null,
      },
      {
        index_type: "symbol_graph",
        state: "failed",
        stale: false,
        indexed_commit: null,
        built_at: null,
        update_mode: "patch",
        updates_enabled: true,
        update_reason: "",
        job_id: null,
        metrics: null,
      },
    ],
  };
}

describe("askIndexAttention", () => {
  it("tracks Ask retrieval surfaces without treating graph state as retrieval", () => {
    const attention = askIndexAttention(statusFixture());

    expect(attention).toEqual({
      needsAttention: true,
      state: "stale",
      indexes: ["bm25"],
      writable: true,
    });
  });

  it("prioritizes failed and updating retrieval states", () => {
    const failed = statusFixture();
    failed.indexes[0] = { ...failed.indexes[0], state: "failed" };
    expect(askIndexAttention(failed).state).toBe("failed");

    const updating = statusFixture();
    updating.indexes[0] = {
      ...updating.indexes[0],
      state: "updating",
      job_id: "job-1",
    };
    expect(askIndexAttention(updating).state).toBe("updating");
  });

  it("accepts a current built retrieval snapshot", () => {
    const current = statusFixture();
    current.stale = false;
    current.current_head = current.last_indexed_commit;
    current.indexes[0] = {
      ...current.indexes[0],
      state: "built",
      stale: false,
    };

    expect(askIndexAttention(current).needsAttention).toBe(false);
  });
});

describe("AskIndexFreshness", () => {
  it("offers an explicit update-or-current choice for a pending question", () => {
    const html = renderToStaticMarkup(
      <AskIndexFreshness
        status={statusFixture()}
        hasPendingQuestion
        showUpdateControl={false}
        onShowUpdate={vi.fn()}
        onAskCurrent={vi.fn()}
        onRefresh={vi.fn()}
      />,
    );

    expect(html).toContain("Newer source changes are available");
    expect(html).toContain("BM25 may not match the repository HEAD");
    expect(html).toContain("Update indexes first");
    expect(html).toContain("Ask with current index");
    expect(html).toContain("12345678");
    expect(html).toContain("fedcba09");
  });

  it("limits the embedded update control to retrieval surfaces", () => {
    const html = renderToStaticMarkup(
      <AskIndexFreshness
        status={statusFixture()}
        hasPendingQuestion
        showUpdateControl
        onShowUpdate={vi.fn()}
        onAskCurrent={vi.fn()}
        onRefresh={vi.fn()}
      />,
    );

    expect(html).toContain("Rebuild BM25");
    expect(html).toContain(">Symbol graph<");
    expect(html.match(/type="radio"[^>]*disabled/g)).toHaveLength(2);
    expect(html).toContain("Ask with current index");
  });
});
