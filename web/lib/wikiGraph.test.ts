// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

import type { WikiAreaMap, WikiPageRef } from "@/lib/api";
import { AREA_COLORS, buildWikiGraph, labelLines, minorReferences, NEUTRAL_COLOR, neighborhood } from "@/lib/wikiGraph";

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

  it("draws recorded references only between pages that exist", () => {
    const references = buildWikiGraph(pages, map).edges.filter((edge) => edge.kind === "references");
    expect(references.map((edge) => edge.id)).toEqual(["a~b"]);
    expect(references[0].link?.example.anchor?.file).toBe("x.py");
  });

  it("claims no references when the repository has no code graph", () => {
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

describe("labelLines", () => {
  it("keeps a short title on one line", () => {
    expect(labelLines("Session Orchestration")).toEqual(["Session Orchestration"]);
  });

  it("breaks a long title at its most balanced word and loses nothing", () => {
    const title = "Framework Integration and Benchmark Workflows";
    const lines = labelLines(title);
    expect(lines).toEqual(["Framework Integration", "and Benchmark Workflows"]);
    expect(lines.join(" ")).toBe(title);
  });

  it("leaves one unbreakable word whole", () => {
    const word = "x".repeat(40);
    expect(labelLines(word)).toEqual([word]);
  });
});

describe("minorReferences", () => {
  const ids = ["a", "b", "c", "d", "e", "f", "g"];
  const many = ids.map((id) => page(id));
  const weighted = (source: string, target: string, weight: number) => ({ ...link(source, target), weight });
  const dense: WikiAreaMap = {
    available: true,
    areas: ids.map((id) => ({ id, title: id, symbols: 10, files: 1 })),
    links: ids.flatMap((source, i) => ids.filter((target) => target !== source).map((target, j) => weighted(source, target, 1 + ((i * 7 + j * 3) % 11)))),
  };

  it("leaves a map with few references alone", () => {
    expect(minorReferences(buildWikiGraph(pages, map)).size).toBe(0);
  });

  it("keeps each area's heaviest references and marks the rest", () => {
    const graph = buildWikiGraph(many, dense);
    const minor = minorReferences(graph);
    const references = graph.edges.filter((edge) => edge.kind === "references");
    expect(references.length).toBe(42);
    expect(minor.size).toBeGreaterThan(0);
    expect(minor.size).toBeLessThan(references.length);
    for (const id of ids) {
      const mine = references.filter((edge) => edge.source === id || edge.target === id);
      const heaviest = mine.sort((x, y) => (y.link?.weight ?? 0) - (x.link?.weight ?? 0) || x.id.localeCompare(y.id))[0];
      expect(minor.has(heaviest.id)).toBe(false);
    }
  });
});
