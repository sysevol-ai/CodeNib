// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

import { splitSymbolLabel } from "./symbols";

describe("splitSymbolLabel", () => {
  it("separates a file prefix without breaking Rust or C++ paths", () => {
    expect(splitSymbolLabel("context.go:Context.Bind()")).toEqual({
      file: "context.go",
      symbol: "Context.Bind()",
    });
    expect(splitSymbolLabel("src/nb.rs:Notebook::from_reader()")).toEqual({
      file: "src/nb.rs",
      symbol: "Notebook::from_reader()",
    });
    expect(splitSymbolLabel("Notebook::from_reader()")).toEqual({
      file: "",
      symbol: "Notebook::from_reader()",
    });
  });
});
