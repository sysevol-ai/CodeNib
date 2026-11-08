// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import type { SavedWiki } from "./visitorWiki";
import { clock, runLead, runStarted, runSteps, runTitle } from "./wikiRun";

const tree = { files: 239, root: "xarray", folders: [{ path: "xarray/core", files: 36 }], more: { folders: 0, files: 0 } };
const pages = [
  { id: "overview", title: "Overview", children: [] },
  { id: "model", title: "Data model", children: [{ id: "lazy", title: "Lazy data", children: [] }] },
  { id: "io", title: "File I/O", children: [] },
];
const base: SavedWiki = {
  id: "a".repeat(64), repository: "pydata/xarray", commit: "8f2c1d0a".padEnd(40, "0"),
  status: "running", stage: "downloading", active_page: "", pages: [], page_states: {},
  message: "", created_at: 1, updated_at: 100, reported_cost_usd: 0, calls: 0,
  unreported_call_cost: false, budget_usd: 2, stalled: false,
  history: [
    { stage: "connecting", page: "", at: 10 },
    { stage: "downloading", page: "", at: 12 },
  ],
};
const brief = (wiki: SavedWiki, now = 100) =>
  runSteps(wiki, now).map((step) => `${step.state} ${step.label}${step.detail ? ` (${step.detail})` : ""}`);

describe("runSteps", () => {
  it("lists what is done, what runs now and what comes next before any chapter exists", () => {
    expect(brief(base)).toEqual([
      "done Connected to your model account",
      "current Downloading the snapshot (8f2c1d0a)",
      "next Reading the source files",
      "next Planning the chapters",
      "next Chapters (Appear here and on the map once planned)",
      "next Save the maps and finish",
    ]);
    expect(runSteps(base, 100)[1].seconds).toBe(88);
  });

  it("reports the snapshot facts the server saved, and the index only when it ran", () => {
    const wiki: SavedWiki = {
      ...base, stage: "outline", source_tree: tree, languages: ["python"],
      skipped_files: [{ path: "big.nc", size_bytes: 5e6 }],
      code_index: { available: true, files: 199, nodes: 6120, edges: 15873 },
      history: [...base.history, { stage: "analyzing", page: "", at: 20 }, { stage: "indexing", page: "", at: 30 }, { stage: "outline", page: "", at: 70 }],
    };
    expect(brief(wiki).slice(2, 5)).toEqual([
      "done Read 239 code files (python · 1 file over 4 MiB skipped)",
      "done Indexed 199 Python files (6,120 graph nodes · 15,873 edges)",
      "current Planning the chapters",
    ]);
    const noGraph = { ...wiki, code_index: { available: false, reason: "unsupported_language" }, history: wiki.history.filter((e) => e.stage !== "indexing") };
    expect(brief(noGraph)[2]).toContain("no code graph: hosted graphs cover Python only");
    expect(brief(noGraph).some((line) => line.includes("Index"))).toBe(false);
  });

  it("turns each chapter into a step with its state and writing time", () => {
    const wiki: SavedWiki = {
      ...base, stage: "editing", active_page: "lazy", pages,
      page_states: { overview: "ready", model: "ready", lazy: "running" },
      history: [
        ...base.history,
        { stage: "outline", page: "", at: 40 },
        { stage: "retrieving", page: "overview", at: 50 },
        { stage: "checking", page: "overview", at: 95 },
        { stage: "retrieving", page: "model", at: 100 },
        { stage: "checking", page: "model", at: 160 },
        { stage: "retrieving", page: "lazy", at: 170 },
        { stage: "editing", page: "lazy", at: 200 },
      ],
    };
    const steps = runSteps(wiki, 230);
    const chapters = steps.filter((step) => step.pageId);
    expect(chapters.map((step) => [step.pageId, step.state, step.seconds])).toEqual([
      ["overview", "done", 50],
      ["model", "done", 70],
      ["lazy", "current", 60],
      ["io", "next", undefined],
    ]);
    expect(chapters[2].detail).toBe("Reviewing the explanation");
    expect(steps.at(-1)?.label).toBe("Save the maps and finish");
  });

  it("describes only the latest run after a resume, and offers no next steps once stopped", () => {
    const wiki: SavedWiki = {
      ...base, status: "partial", stage: "paused", pages,
      page_states: { overview: "ready", model: "needs_review", lazy: "pending", io: "pending" },
      history: [
        { stage: "connecting", page: "", at: 1 },
        { stage: "retrieving", page: "overview", at: 5 },
        { stage: "connecting", page: "", at: 50 },
        { stage: "downloading", page: "", at: 52 },
        { stage: "retrieving", page: "model", at: 60 },
        { stage: "checking", page: "model", at: 90 },
      ],
    };
    const steps = runSteps(wiki, 120);
    expect(steps[0].at).toBe(50);
    expect(steps.find((step) => step.pageId === "overview")?.at).toBeUndefined();
    expect(steps.find((step) => step.pageId === "model")?.state).toBe("withheld");
    expect(steps.some((step) => step.state === "current")).toBe(false);
    expect(steps.some((step) => step.id === "saving_graphs")).toBe(false);
    expect(runStarted(wiki)).toBe(50);
  });
});

describe("run headline", () => {
  it("names the phase from the reader's side", () => {
    expect(runTitle(base, 0)).toBe("Reading xarray");
    expect(runTitle({ ...base, stage: "outline" }, 0)).toBe("Planning the chapters");
    expect(runTitle({ ...base, stage: "writing" }, 0)).toBe("Writing the first chapter");
    expect(runTitle({ ...base, stage: "writing" }, 2)).toBe("Writing the Wiki");
    expect(runTitle({ ...base, status: "partial" }, 0)).toBe("Generation stopped");
    expect(runTitle({ ...base, status: "partial" }, 1)).toBe("Generation paused");
    expect(runTitle({ ...base, status: "complete" }, 3)).toBe("Your Wiki is ready");
    expect(runLead({ ...base, stage: "writing" }, 0)).toContain("fills in on the map");
  });

  it("formats elapsed time as a reader says it", () => {
    expect(clock(-3)).toBe("0 s");
    expect(clock(42.9)).toBe("42 s");
    expect(clock(187)).toBe("3:07");
    expect(clock(3729)).toBe("1:02:09");
  });
});
