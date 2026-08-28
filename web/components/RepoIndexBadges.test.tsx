// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { RepoIndexStatus } from "@/lib/api";
import RepoIndexBadges, { RepoIndexBadgeList } from "./RepoIndexBadges";

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
        update_reason: "No graph updater is configured.",
        job_id: null,
        metrics: null,
      },
    ],
  };
}

describe("RepoIndexBadgeList", () => {
  it("shows exactly the three canonical surfaces and repository freshness", () => {
    const html = renderToStaticMarkup(
      <RepoIndexBadgeList status={statusFixture()} />,
    );

    expect(html.match(/class="repo-index-badge index-state/g)).toHaveLength(3);
    expect(html).toContain("BM25 · Stale");
    expect(html).toContain("Embeddings · Built");
    expect(html).toContain("Graph · Missing");
    expect(html).toContain("Update available");
  });

  it("keeps update mode, commit, and disabled reason in each badge title", () => {
    const html = renderToStaticMarkup(
      <RepoIndexBadgeList status={statusFixture()} />,
    );

    expect(html).toContain("rebuild on update · indexed 12345678");
    expect(html).toContain("incremental update · indexed fedcba09");
    expect(html).toContain(
      "updates unavailable · No graph updater is configured.",
    );
  });

  it("reserves a visible loading state before the runtime response", () => {
    const html = renderToStaticMarkup(<RepoIndexBadges repoId="demo" />);

    expect(html).toContain("Checking indexes…");
    expect(html).toContain('class="repo-index-summary loading"');
  });
});
