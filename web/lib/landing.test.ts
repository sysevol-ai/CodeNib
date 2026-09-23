// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

import type { RepoInfo } from "./api";
import { groupByLanguage, primaryLanguage } from "./landing";

const repo = (id: string, language: string) => ({ id, language }) as RepoInfo;

describe("landing languages", () => {
  it("names the main language, not every chunker the index used", () => {
    expect(primaryLanguage(repo("jq", "cpp/python/javascript"))).toBe("C/C++");
    expect(primaryLanguage(repo("gin", "go"))).toBe("Go");
    expect(primaryLanguage(repo("x", ""))).toBe("Code");
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
