// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

import type { WikiPageRef } from "./api";
import { childPages, pagePath, partitionWikiNav, visibleChapters } from "./wikiNav";

const page = (
  id: string,
  tier?: WikiPageRef["tier"],
  children: WikiPageRef[] = [],
): WikiPageRef => ({ id, title: id, tier, children });

const tree = [
  page("overview", "primary"),
  page("sessions", "primary", [
    page("state", "key"),
    page("redirects", "detail", [page("deep", "detail")]),
  ]),
  page("compat", "more", [page("aliases", "detail")]),
];

describe("partitionWikiNav", () => {
  it("keeps lower-ranked topics out of the first view", () => {
    const { primary, more } = partitionWikiNav(tree);
    expect(primary.map((item) => item.id)).toEqual(["overview", "sessions"]);
    expect(more.map((item) => item.id)).toEqual(["compat"]);
  });

  it("applies the same limit to a Wiki saved before ranking", () => {
    const old = Array.from({ length: 9 }, (_, index) => page(`p${index}`));
    const { primary, more } = partitionWikiNav(old);
    expect(primary).toHaveLength(6);
    expect(more.map((item) => item.id)).toEqual(["p6", "p7", "p8"]);
  });
});

describe("pagePath", () => {
  it("names every ancestor of a nested page", () => {
    expect(pagePath(tree, "deep")).toEqual(["sessions", "redirects", "deep"]);
  });

  it("is empty for an unknown page", () => {
    expect(pagePath(tree, "missing")).toEqual([]);
  });
});

describe("childPages", () => {
  it("lists only the next level", () => {
    expect(childPages(tree, "sessions").map((item) => item.id)).toEqual([
      "state",
      "redirects",
    ]);
  });

  it("is empty for a page with no detail pages", () => {
    expect(childPages(tree, "overview")).toEqual([]);
    expect(childPages(tree, "missing")).toEqual([]);
  });
});

describe("visibleChapters", () => {
  const ids = (active: string, open?: Set<string>) =>
    visibleChapters(tree, active, open).map((item) => item.page.id);

  it("shows only top-level pages at first", () => {
    expect(ids("overview")).toEqual(["overview", "sessions", "compat"]);
  });

  it("opens the groups around the current page", () => {
    expect(ids("deep")).toEqual([
      "overview",
      "sessions",
      "state",
      "redirects",
      "deep",
      "compat",
    ]);
  });

  it("opens a group the reader asked for, one level at a time", () => {
    expect(ids("overview", new Set(["sessions"]))).toEqual([
      "overview",
      "sessions",
      "state",
      "redirects",
      "compat",
    ]);
  });

  it("reports depth for indentation", () => {
    const rows = visibleChapters(tree, "state");
    expect(rows.find((row) => row.page.id === "state")?.depth).toBe(1);
  });
});
