// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { buildSourceGraph, describeSourceNode, folderLabel, SOURCE_HUB } from "./sourceMap";

const outline = {
  files: 239,
  root: "xarray",
  folders: [
    { path: "xarray/tests", files: 76 },
    { path: "xarray/core", files: 36 },
    { path: "xarray", files: 8, direct: true },
  ],
  more: { folders: 8, files: 46 },
};

describe("buildSourceGraph", () => {
  it("draws the opened folder in the middle and its folders around it", () => {
    const { graph } = buildSourceGraph("pydata/xarray", outline);
    expect(graph.nodes.map((node) => node.title)).toEqual([
      "xarray/",
      "tests/",
      "core/",
      "files in xarray/",
      "8 more folders",
    ]);
    expect(graph.edges.every((edge) => edge.source === SOURCE_HUB && edge.kind === "contains")).toBe(true);
    const radius = (title: string) => graph.nodes.find((node) => node.title === title)!.radius;
    expect(radius("tests/")).toBeGreaterThan(radius("core/"));
    expect(radius("core/")).toBeGreaterThan(radius("files in xarray/"));
  });

  it("draws the same picture every time", () => {
    const first = buildSourceGraph("pydata/xarray", outline).graph.nodes.map((node) => [node.x, node.y]);
    const again = buildSourceGraph("pydata/xarray", outline).graph.nodes.map((node) => [node.x, node.y]);
    expect(again).toEqual(first);
  });

  it("shows only the repository until the snapshot is read", () => {
    const { graph, facts } = buildSourceGraph("psf/requests", undefined);
    expect(graph.nodes.map((node) => node.title)).toEqual(["requests/"]);
    expect(describeSourceNode(graph.nodes[0], facts)?.detail).toBe("0 code files in this snapshot");
  });

  it("names folders relative to the opened one and says what a node holds", () => {
    expect(folderLabel("src/requests", "")).toBe("src/requests/");
    expect(folderLabel("", "", true)).toBe("top-level files");
    const { graph, facts } = buildSourceGraph("pydata/xarray", outline);
    expect(describeSourceNode(graph.nodes[2], facts)).toEqual({ title: "xarray/core", detail: "36 code files" });
    expect(describeSourceNode(graph.nodes[4], facts)?.detail).toBe("46 code files in smaller folders");
  });
});
