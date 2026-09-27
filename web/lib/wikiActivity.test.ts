// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { expect, it } from "vitest";
import type { SavedWiki } from "./visitorWiki";
import { formatWait, wikiActivity } from "./wikiActivity";

const wiki: SavedWiki = {
  id: "a".repeat(64), repository: "owner/repo", commit: "c".repeat(40),
  status: "running", stage: "planning_page", active_page: "pipeline",
  pages: [{ id: "overview", title: "Overview", children: [
    { id: "pipeline", title: "Request pipeline", children: [] },
  ] }],
  page_states: { overview: "ready", pipeline: "running" },
  message: "", created_at: 1, updated_at: 190, reported_cost_usd: 0.02,
  calls: 4, unreported_call_cost: true, budget_usd: 2, stalled: false,
  history: [
    { stage: "planning_page", page: "pipeline", at: 20 },
    { stage: "connecting", page: "", at: 100 },
    { stage: "retrieving", page: "pipeline", at: 110 },
    { stage: "retrieving", page: "pipeline", at: 111 },
    { stage: "planning_page", page: "pipeline", at: 120 },
  ],
};

it("keeps elapsed step time separate from accounting and polling updates", () => {
  const activity = wikiActivity(wiki, 200);
  expect(activity.stageAge).toBe(80);
  expect(activity.progressAge).toBe(10);
  expect(activity.chapter).toBe("Request pipeline");
  expect(activity.animate).toBe(true);
  expect(wikiActivity({ ...wiki, updated_at: 200, calls: 5 }, 201).stageAge).toBe(81);
  expect(activity.recent.map((event) => event.stage)).toEqual([
    "connecting", "retrieving", "planning_page",
  ]);
});

it("distinguishes a long operation from a worker with no recent progress", () => {
  expect(wikiActivity(wiki, 234).label).toBe("Generation in progress");
  expect(wikiActivity(wiki, 235).label).toBe("Waiting for the next update");
  expect(wikiActivity(wiki, 369).animate).toBe(true);
  const delayed = wikiActivity(wiki, 370);
  expect(delayed.label).toBe("No recent progress reported");
  expect(delayed.explanation).toContain("does not confirm that it has stopped");
  expect(delayed.animate).toBe(false);
  expect(wikiActivity({ ...wiki, stalled: true }, 200).delayed).toBe(true);
});

it("reports lost status connectivity without claiming generation stopped", () => {
  const offline = wikiActivity(wiki, 200, false);
  expect(offline.label).toBe("Connection interrupted");
  expect(offline.explanation).toContain("server may still be working");
  expect(offline.animate).toBe(false);
});

it("does not invent a stage start or animate completed and paused runs", () => {
  expect(wikiActivity({ ...wiki, stage: "future_stage" }, 200).stageAge).toBeNull();
  for (const status of ["complete", "partial"] as const) {
    const settled = wikiActivity({ ...wiki, status }, 500);
    expect(settled.running).toBe(false);
    expect(settled.animate).toBe(false);
  }
  expect(wikiActivity(wiki, 10).stageAge).toBe(0);
  expect(wikiActivity(wiki, 10).progressAge).toBe(0);
  expect(formatWait(-10)).toBe("0s");
  expect(formatWait(125.9)).toBe("2m 5s");
});
