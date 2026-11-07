// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

import type { WikiAreaMap, WikiPageRef } from "@/lib/api";
import { AREA_COLORS, buildWikiGraph, NEUTRAL_COLOR, neighborhood } from "@/lib/wikiGraph";

const page = (id: string, children: WikiPageRef[] = []): WikiPageRef => ({
  id,
  title: id,
  tier: "primary",
  children,
});
const pages = [page("overview"), page("a", [page("a1"), page("a2", [page("a21")])]), page("b")];
const link = (source: string, target: string) => ({
  source,
  target,
  weight: 3,
  calls: 2,
  example: { source: "x", target: "y", anchor: { file: "x.py", line: 4 } },
});
const map: WikiAreaMap = {
  available: true,
  areas: [
    { id: "a", title: "A", symbols: 100, files: 3 },
    { id: "b", title: "B", symbols: 25, files: 1 },
  ],
  links: [link("a", "b"), link("a", "gone")],
};

describe("buildWikiGraph", () => {
  it("makes every page a node, at any depth, in its area's colour", () => {
    const graph = buildWikiGraph(pages, map);
    expect(graph.nodes.map((node) => node.id).sort()).toEqual(["a", "a1", "a2", "a21", "b"]);
    expect(graph.colors.get("a21")).toBe(graph.colors.get("a"));
    expect(graph.colors.get("overview")).toBe(NEUTRAL_COLOR);
    expect(graph.colors.get("a")).toBe(AREA_COLORS[0]);
    expect(graph.colors.get("b")).toBe(AREA_COLORS[1]);
  });

  it("draws recorded calls only between pages that exist", () => {
    const calls = buildWikiGraph(pages, map).edges.filter((edge) => edge.kind === "calls");
    expect(calls.map((edge) => edge.id)).toEqual(["a~b"]);
    expect(calls[0].link?.example.anchor?.file).toBe("x.py");
  });

  it("claims no calls when the repository has no code graph", () => {
    const graph = buildWikiGraph(pages, { available: false, areas: [], links: [], reason: "graph_unavailable" });
    expect(graph.edges.every((edge) => edge.kind === "contains")).toBe(true);
    expect(graph.nodes).toHaveLength(5);
    expect(buildWikiGraph(pages, null).edges.every((edge) => edge.kind === "contains")).toBe(true);
  });

  it("sizes areas by their symbols", () => {
    const { nodes } = buildWikiGraph(pages, map);
    const radius = (id: string) => nodes.find((node) => node.id === id)!.radius;
    expect(radius("a")).toBeGreaterThan(radius("b"));
    expect(radius("b")).toBeGreaterThan(radius("a1"));
  });

  it("draws the same shape every time, with finite coordinates", () => {
    const first = buildWikiGraph(pages, map).nodes.map(({ id, x, y }) => [id, x, y]);
    expect(buildWikiGraph(pages, map).nodes.map(({ id, x, y }) => [id, x, y])).toEqual(first);
    for (const [, x, y] of first) {
      expect(Number.isFinite(x)).toBe(true);
      expect(Number.isFinite(y)).toBe(true);
    }
  });

  it("gives no colour slot past the eighth area and never reuses one", () => {
    const many = [page("overview"), ...Array.from({ length: 10 }, (_, index) => page(`p${index}`))];
    const { colors } = buildWikiGraph(many, null);
    const used = Array.from({ length: 8 }, (_, index) => colors.get(`p${index}`));
    expect(new Set(used).size).toBe(8);
    expect(colors.get("p8")).toBe(NEUTRAL_COLOR);
    expect(colors.get("p9")).toBe(NEUTRAL_COLOR);
  });
});

describe("neighborhood", () => {
  it("is the page and what one edge reaches", () => {
    const graph = buildWikiGraph(pages, map);
    expect([...neighborhood(graph, "a")].sort()).toEqual(["a", "a1", "a2", "b"]);
    expect(neighborhood(graph, null).size).toBe(0);
  });
});
