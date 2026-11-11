// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

import {
  cleanTemplateArguments,
  displaySymbol,
  splitSymbolLabel,
  tidyInteractionRows,
} from "./symbols";

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

describe("cleanTemplateArguments", () => {
  it("names clang's template parameters and folds long argument lists", () => {
    expect(
      cleanTemplateArguments(
        "fmt.dynamic_format_arg_store.operator basic_format_args<type-parameter-0-0>",
      ),
    ).toBe("fmt.dynamic_format_arg_store.operator basic_format_args<T>");
    expect(
      cleanTemplateArguments(
        "fmt.formatter<type-parameter-0-0, type-parameter-0-1, typename enable_if<fmt.is_tuple_like<Tuple>.value && fmt.is_tuple_formattable<Tuple, Char>.value, void>.type>.parse()",
      ),
    ).toBe("fmt.formatter<…>.parse()");
  });

  it("keeps short arguments, operators and comparisons", () => {
    expect(cleanTemplateArguments("cache_accessor<double>.get_cached_power()")).toBe(
      "cache_accessor<double>.get_cached_power()",
    );
    expect(cleanTemplateArguments("basic_ostream.operator<<()")).toBe("basic_ostream.operator<<()");
    expect(cleanTemplateArguments("if a < b")).toBe("if a < b");
    expect(cleanTemplateArguments("std::vector<int")).toBe("std::vector<int");
    expect(cleanTemplateArguments("Session.request()")).toBe("Session.request()");
  });
});

describe("displaySymbol", () => {
  it("drops the file prefix and cleans templates", () => {
    expect(
      displaySymbol(
        "include/fmt/args.h:fmt.dynamic_format_arg_store.operator basic_format_args<type-parameter-0-0>",
      ),
    ).toBe("fmt.dynamic_format_arg_store.operator basic_format_args<T>");
  });

  it("keeps the owner and member when asked for a short name", () => {
    expect(displaySymbol("include/fmt/base.h:fmt.detail.parse_arg_id()", { short: true })).toBe(
      "detail.parse_arg_id()",
    );
    expect(displaySymbol("src/nb.rs:crate::nb::Notebook::from_reader()", { short: true })).toBe(
      "Notebook::from_reader()",
    );
    expect(
      displaySymbol("ranges.h:fmt.formatter<type-parameter-0-0, typename enable_if<x.y>::type>.parse()", {
        short: true,
      }),
    ).toBe("formatter<…>.parse()");
    expect(displaySymbol("printf()", { short: true })).toBe("printf()");
    expect(displaySymbol("requests.get(url.path)", { short: true })).toBe("requests.get(url.path)");
  });
});

describe("tidyInteractionRows", () => {
  it("shortens both ends of rows under the Interactions label only", () => {
    const markdown = [
      "Text with `fmt.detail.parse_arg_id()` stays.",
      "",
      "**Interactions**",
      "- `fmt.detail.parse_replacement_field()` → `fmt.detail.parse_arg_id()` [R3](#evidence-R3)",
      "- `fmt.formatter<type-parameter-0-0, typename enable_if<a>::type>.parse()` → `fmt.parse_context`: reads specs [R4](#evidence-R4)",
      "",
      "- `a.b.c()` → `d.e.f()` outside",
    ].join("\n");
    expect(tidyInteractionRows(markdown).split("\n")).toEqual([
      "Text with `fmt.detail.parse_arg_id()` stays.",
      "",
      "**Interactions**",
      "- `detail.parse_replacement_field()` → `detail.parse_arg_id()` [R3](#evidence-R3)",
      "- `formatter<…>.parse()` → `fmt.parse_context`: reads specs [R4](#evidence-R4)",
      "",
      "- `a.b.c()` → `d.e.f()` outside",
    ]);
  });
});
