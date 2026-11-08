// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

import type { RepoInfo } from "./api";
import { groupByLanguage, plainSummary, primaryLanguage } from "./landing";

const repo = (id: string, language: string) => ({ id, language }) as RepoInfo;

describe("landing languages", () => {
  it("names the main language, not every chunker the index used", () => {
    expect(primaryLanguage(repo("jq", "cpp/python/javascript"))).toBe("C/C++");
    expect(primaryLanguage(repo("gin", "go"))).toBe("Go");
    expect(primaryLanguage(repo("x", ""))).toBe("Code");
  });

  it("drops tagline emoji from card summaries and keeps the words", () => {
    expect(plainSummary("AMA-Bench is an 🧠 AMA-Bench: evaluating memory.")).toBe(
      "AMA-Bench is an AMA-Bench: evaluating memory.",
    );
    expect(plainSummary("✨ Simplicity: the logic fits in ~1,000 lines")).toBe(
      "Simplicity: the logic fits in ~1,000 lines",
    );
    expect(plainSummary("Fast ⚡️ and 👩‍💻 friendly © 2026")).toBe("Fast and friendly © 2026");
  });

  it("groups repositories largest language first", () => {
    const groups = groupByLanguage([
      repo("a", "go"),
      repo("b", "python"),
      repo("c", "python/cpp"),
    ]);
    expect(groups.map((g) => [g.language, g.repos.length])).toEqual([
      ["Python", 2],
      ["Go", 1],
    ]);
  });
});
