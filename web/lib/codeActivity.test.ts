import { describe, expect, it } from "vitest";
import type { CodeActivityItem, CodeActivityOverlay } from "./api";
import {
  activityDate,
  activityHref,
  activityMarks,
  activityReach,
  activitySources,
  activityTitle,
  codeSpans,
  isOpenMark,
  markRadius,
  markSummary,
  pageActivity,
  reachText,
} from "./codeActivity";

function item(overrides: Partial<CodeActivityItem> = {}): CodeActivityItem {
  return {
    number: 27125,
    title: "Stabilize `--add-ignore` (#27125)",
    strength: "symbol",
    symbols: ["add_suppressions_to_path()"],
    files: ["crates/ruff_linter/src/noqa.rs"],
    at: "2026-07-23T13:38:29-04:00",
    url: "https://github.com/astral-sh/ruff/pull/27125",
    ...overrides,
  };
}

function overlay(pages: CodeActivityOverlay["pages"]): CodeActivityOverlay {
  return { version: 1, repo: "astral-sh/ruff", rev: "abc", generated_at: "", pages };
}

describe("pageActivity", () => {
  it("lists each group newest first", () => {
    const [group] = pageActivity(
      overlay({
        p: {
          changes: [
            item({ number: 1, at: "2026-07-01T00:00:00Z" }),
            item({ number: 2, at: "2026-08-01T00:00:00Z" }),
          ],
        },
      }),
      "p",
    );
    expect(group.direct.map((i) => i.number)).toEqual([2, 1]);
  });

  it("groups by kind and splits symbol matches from file matches", () => {
    const groups = pageActivity(
      overlay({
        noqa: {
          changes: [item(), item({ number: 27123, strength: "file", symbols: [] })],
          issues: [item({ number: 9 })],
        },
      }),
      "noqa",
    );
    expect(groups.map((g) => g.kind)).toEqual(["issues", "changes"]);
    expect(groups[1].direct.map((i) => i.number)).toEqual([27125]);
    expect(groups[1].nearby.map((i) => i.number)).toEqual([27123]);
  });

  it("is empty without an overlay, a page entry, or valid items", () => {
    expect(pageActivity(null, "noqa")).toEqual([]);
    expect(pageActivity(overlay({}), "noqa")).toEqual([]);
    const broken = { title: 3 } as unknown as CodeActivityItem;
    expect(pageActivity(overlay({ noqa: { pulls: [broken] } }), "noqa")).toEqual([]);
  });
});

describe("activity text", () => {
  it("drops the pull request number GitHub appends to a merged subject", () => {
    expect(activityTitle(item())).toBe("Stabilize `--add-ignore`");
    expect(activityTitle(item({ title: "(#1)" }))).toBe("(#1)");
  });

  it("says what the item reaches", () => {
    const reach = (i: CodeActivityItem, k: "issues" | "pulls" | "changes") =>
      reachText(activityReach(i, k));
    expect(reach(item(), "changes")).toBe("Changes add_suppressions_to_path()");
    expect(reach(item({ symbols: ["a()", "b()", "c()"] }), "issues")).toBe(
      "Names a() and 2 more",
    );
    expect(reach(item({ strength: "file", files: ["x/noqa.rs", "y/z.rs"] }), "pulls")).toBe(
      "Edits noqa.rs and 1 more file",
    );
    expect(activityReach(item({ strength: "file", files: [] }), "pulls")).toBeNull();
  });

  it("names only the kinds a page has", () => {
    const groups = pageActivity(
      overlay({ p: { changes: [item()], issues: [item({ number: 3 })] } }),
      "p",
    );
    expect(activitySources(groups)).toBe("Open issues and merged changes");
    expect(activitySources(groups.slice(1))).toBe("Merged changes");
  });

  it("splits backticked code out of a title and leaves unbalanced ticks alone", () => {
    expect(codeSpans("Stabilize `--add-ignore` now")).toEqual([
      { text: "Stabilize ", code: false },
      { text: "--add-ignore", code: true },
      { text: " now", code: false },
    ]);
    expect(codeSpans("a ` b")).toEqual([{ text: "a ` b", code: false }]);
  });

  it("formats dates and omits the year only for this year", () => {
    const now = new Date("2026-11-14T00:00:00Z");
    expect(activityDate("2026-07-23T13:38:29-04:00", now)).toBe("Jul 23");
    expect(activityDate("2025-07-23T13:38:29-04:00", now)).toBe("Jul 23, 2025");
    expect(activityDate("not a date", now)).toBe("");
  });

  it("links only to GitHub issues and pull requests", () => {
    expect(activityHref(item())).toBe("https://github.com/astral-sh/ruff/pull/27125");
    expect(activityHref(item({ url: "javascript:alert(1)" }))).toBeUndefined();
    expect(activityHref(item({ url: "https://evil.example/pull/1" }))).toBeUndefined();
  });
});

describe("activityMarks", () => {
  it("marks open items always and merged changes only where several reach symbols", () => {
    const marks = activityMarks(
      overlay({
        quiet: { changes: [item(), item({ number: 2, strength: "file" })] },
        moving: { changes: [item(), item({ number: 2 })] },
        asked: { issues: [item({ number: 3, strength: "file" })] },
      }),
    );
    expect(Object.keys(marks).sort()).toEqual(["asked", "moving"]);
    expect(marks.moving).toEqual({ issues: 0, pulls: 0, changes: 2 });
    expect(isOpenMark(marks.asked)).toBe(true);
    expect(isOpenMark(marks.moving)).toBe(false);
    expect(activityMarks(null)).toEqual({});
  });

  it("leaves closed threads out of the open mark and the open labels", () => {
    const pages = {
      done: { issues: [item({ state: "closed" })] },
      mixed: { pulls: [item({ state: "closed" }), item({ number: 2, state: "open" })] },
    };
    const marks = activityMarks(overlay(pages));
    expect(marks.done).toBeUndefined();
    expect(marks.mixed).toEqual({ issues: 0, pulls: 1, changes: 0 });
    const [group] = pageActivity(overlay(pages), "done");
    expect(group.label).toBe("Issues");
    expect(activitySources([group])).toBe("Issues");
  });

  it("sizes marks to the node, so a small page stays visible under its mark", () => {
    expect(markRadius(4.5)).toBe(2.2);
    expect(markRadius(6)).toBeCloseTo(2.7);
    expect(markRadius(19)).toBe(3.2);
  });

  it("summarizes a mark in words", () => {
    expect(markSummary({ issues: 2, pulls: 1, changes: 3 })).toBe(
      "2 open issues, 1 open pull request and 3 recent changes on its code",
    );
    expect(markSummary({ issues: 0, pulls: 0, changes: 1 })).toBe("1 recent change on its code");
    expect(markSummary({ issues: 0, pulls: 0, changes: 0 })).toBe("");
  });
});
