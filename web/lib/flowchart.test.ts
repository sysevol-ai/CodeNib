// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

import { callPaths, edgeCallSite, parseFlowchart, straightChain } from "./flowchart";

const CHAIN = [
  "flowchart LR",
  '  n0["Context.MustBindWith()"]',
  '  n1["Context.AbortWithError()"]',
  '  n2["Context.Error()"]',
  "  n1 -->|records the error| n2",
  "  n0 -->|binding failure triggers abort| n1",
].join("\n");

describe("straightChain", () => {
  it("orders a straight chain from its only start", () => {
    const steps = straightChain(parseFlowchart(CHAIN)!)!;
    expect(steps.map((s) => s.node.label)).toEqual([
      "Context.MustBindWith()",
      "Context.AbortWithError()",
      "Context.Error()",
    ]);
    expect(steps[0].next?.label).toBe("binding failure triggers abort");
    expect(steps[2].next).toBeUndefined();
  });

  it("leaves a branching flow to the diagram", () => {
    const fan = CHAIN + "\n  n0 -->|also| n2";
    expect(straightChain(parseFlowchart(fan)!)).toBeNull();
  });

  it("rejects loops and syntax outside the generator's subset", () => {
    expect(straightChain(parseFlowchart(CHAIN + "\n  n2 --> n0")!)).toBeNull();
    expect(parseFlowchart(CHAIN + "\n  classDef hot fill:#f00")).toBeNull();
    expect(parseFlowchart("sequenceDiagram\n  A->>B: hi")).toBeNull();
  });
});

describe("callPaths", () => {
  it("keeps disconnected pairs as separate paths in declared order", () => {
    const split = [
      "flowchart LR",
      '  n0["doCompileStyle()"]',
      '  n1["scopedPlugin(longId)"]',
      '  n2["scopedPlugin"]',
      '  n3["processRule()"]',
      "  n0 -->|adds plugin| n1",
      "  n2 -->|invokes rule processing| n3",
    ].join("\n");
    const paths = callPaths(parseFlowchart(split)!)!;
    expect(paths.map((p) => p.map((s) => s.node.label))).toEqual([
      ["doCompileStyle()", "scopedPlugin(longId)"],
      ["scopedPlugin", "processRule()"],
    ]);
    expect(straightChain(parseFlowchart(split)!)).toBeNull();
  });
});

describe("edgeCallSite", () => {
  it("matches file-qualified relation names, including Rust paths", () => {
    const relations = [
      {
        id: "R3",
        source: "context.go:Context.MustBindWith()",
        target: "context.go:Context.AbortWithError()",
        anchors: ["context.go:843"],
      },
      {
        id: "R4",
        source: "src/nb.rs:Notebook::from_reader()",
        target: "src/nb.rs:Notebook::from_raw()",
        anchors: ["src/nb.rs:40"],
      },
    ];
    expect(
      edgeCallSite("Context.MustBindWith()", "Context.AbortWithError()", relations),
    ).toBe("context.go:843");
    expect(edgeCallSite("Notebook::from_reader()", "Notebook::from_raw()", relations)).toBe(
      "src/nb.rs:40",
    );
    expect(edgeCallSite("a()", "b()", relations)).toBeNull();
  });
});
