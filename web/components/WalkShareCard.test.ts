// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

import { sentenceCase } from "@/components/WalkShareCard";

describe("sentenceCase", () => {
  it("lowers a Title Case title but keeps its code", () => {
    expect(sentenceCase('How r.GET("/ping", handler) Builds the Tree')).toBe('How r.GET("/ping", handler) builds the tree');
  });

  it("leaves a sentence-case title alone", () => {
    const title = "How requests.get() sends and returns a response";
    expect(sentenceCase(title)).toBe(title);
    expect(sentenceCase("How gin finds the handler for GET /ping")).toBe("How gin finds the handler for GET /ping");
  });
});

describe("code in titles", () => {
  it("treats an HTTP route as code when setting case", () => {
    expect(sentenceCase("How Gin Finds The Handler For GET /ping")).toBe("How gin finds the handler for GET /ping");
  });
});
