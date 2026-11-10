// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

import type { WikiAreaMap } from "@/lib/api";
import { THUMB_HEIGHT, THUMB_WIDTH, mapThumb } from "@/lib/mapThumb";
import { buildWikiGraph } from "@/lib/wikiGraph";

const areas = [
  { id: "lifecycle", title: "Lifecycle", symbols: 180, files: 5 },
  { id: "session", title: "Session", symbols: 90, files: 3 },
  { id: "response", title: "Response", symbols: 60, files: 2 },
  { id: "utils", title: "Utilities", symbols: 20, files: 4 },
];
const links = [
  { source: "session", target: "lifecycle", weight: 120 },
  { source: "lifecycle", target: "response", weight: 30 },
  { source: "utils", target: "lifecycle", weight: 2 },
] as WikiAreaMap["links"];
const map: WikiAreaMap = { available: true, areas, links };

describe("mapThumb", () => {
  it("keeps every area inside the card", () => {
    const thumb = mapThumb(map)!;
    expect(thumb.dots.map((dot) => dot.id).sort()).toEqual(["lifecycle", "response", "session", "utils"]);
    for (const dot of thumb.dots) {
      expect(dot.x - dot.r).toBeGreaterThanOrEqual(0);
      expect(dot.x + dot.r).toBeLessThanOrEqual(THUMB_WIDTH);
      expect(dot.y - dot.r).toBeGreaterThanOrEqual(0);
      expect(dot.y + dot.r).toBeLessThanOrEqual(THUMB_HEIGHT);
    }
  });

  it("uses the full map's colours and relative positions", () => {
    const thumb = mapThumb(map)!;
    const graph = buildWikiGraph(areas.map((area) => ({ id: area.id, title: area.title, children: [] })), map);
    const dot = new Map(thumb.dots.map((item) => [item.id, item]));
    for (const node of graph.nodes) expect(dot.get(node.id)!.color).toBe(node.color);
    const [a, b] = [graph.nodes[0], graph.nodes[1]];
    expect(Math.sign(dot.get(a.id)!.x - dot.get(b.id)!.x)).toBe(Math.sign(a.x - b.x));
    expect(Math.sign(dot.get(a.id)!.y - dot.get(b.id)!.y)).toBe(Math.sign(a.y - b.y));
  });

  it("draws one line per recorded reference and marks the heavy ones", () => {
    const thumb = mapThumb(map)!;
    expect(thumb.lines).toHaveLength(links.length);
    expect(thumb.lines.find((line) => line.key === "session~lifecycle")!.strong).toBe(true);
    expect(thumb.lines.find((line) => line.key === "utils~lifecycle")!.strong).toBe(false);
  });

  it("draws areas without references as dots, and nothing for no areas", () => {
    const sparse = mapThumb({ available: false, areas, links: [], reason: "no_cross_area_calls" })!;
    expect(sparse.dots).toHaveLength(4);
    expect(sparse.lines).toEqual([]);
    expect(mapThumb({ available: false, areas: [], links: [] })).toBeNull();
  });
});
