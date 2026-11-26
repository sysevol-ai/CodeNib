// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

import { rankByTitle, titleScore } from "@/lib/jumpSearch";

describe("titleScore", () => {
  it("prefers a title that starts with the query, then a word that does", () => {
    expect(titleScore("Cookies and Hooks", "cook")).toBe(0);
    expect(titleScore("Cookie Jar and Conversion", "jar")).toBe(1);
    expect(titleScore("Request Lifecycle", "cycle")).toBe(2);
    expect(titleScore("Cookie Jar and Conversion", "jar cookie")).toBe(3);
    expect(titleScore("Cookie Jar", "redirect")).toBeNull();
  });

  it("treats a query with regex characters as text", () => {
    expect(titleScore("How get() sends a request", "get()")).toBe(1);
  });
});

describe("rankByTitle", () => {
  const pages = ["Session Orchestration", "Cookies and Hooks", "Connection Pooling", "Cookie Jar"];

  it("keeps the outline order when nothing is typed", () => {
    expect(rankByTitle(pages, (page) => page, "", 3)).toEqual(pages.slice(0, 3));
  });

  it("puts better matches first and drops the rest", () => {
    expect(rankByTitle(pages, (page) => page, "coo", 5)).toEqual(["Cookies and Hooks", "Cookie Jar"]);
  });
});
