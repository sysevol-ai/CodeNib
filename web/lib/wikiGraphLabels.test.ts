// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

import type { WikiGraphNode } from "@/lib/wikiGraph";
import { edgeSegments, placeLabels, type Point } from "@/lib/wikiGraphLabels";

const node = (id: string, x: number, y: number, kind: "area" | "page" = "area", title = id): WikiGraphNode => ({
  id,
  title,
  kind,
  areaId: id,
  color: "#fff",
  radius: kind === "area" ? 14 : 5,
  x,
  y,
});
const view = { x: 400, y: 300, k: 1 };
const center = { x: 0, y: 0 };
const pointsOf = (nodes: WikiGraphNode[]) => new Map<string, Point>(nodes.map((n) => [n.id, { x: n.x, y: n.y }]));
const areaFirst = (n: WikiGraphNode) => (n.kind === "area" ? 3 : 2);

describe("placeLabels", () => {
  it("puts a label on the side facing away from the centre when nothing is in the way", () => {
    const nodes = [node("left", -200, 0), node("right", 200, 0)];
    const placed = placeLabels(nodes, pointsOf(nodes), view, center, 800, areaFirst);
    expect(placed.find((l) => l.id === "left")?.anchor).toBe("end");
    expect(placed.find((l) => l.id === "right")?.anchor).toBe("start");
  });

  it("names every area, even where the first position is taken", () => {
    // Three areas stacked so close that their side labels would collide.
    const nodes = [
      node("a", 120, 0, "area", "Core Formatting Engine"),
      node("b", 120, 12, "area", "Range and Container Formatting"),
      node("c", 120, 24, "area", "Chrono and Time Formatting"),
    ];
    const placed = placeLabels(nodes, pointsOf(nodes), view, center, 800, areaFirst);
    expect(placed.map((l) => l.id).sort()).toEqual(["a", "b", "c"]);
  });

  it("still leaves out a page whose every position is taken", () => {
    const nodes = [node("area", 100, 0, "area", "An area with a long name"), node("page", 100, 0, "page", "A page")];
    const crowded = [...nodes];
    // Marks all around the page leave it no free side.
    for (const [x, y] of [[150, 0], [50, 0], [100, 40], [100, -40]]) crowded.push(node(`m${x}${y}`, x, y, "area", ""));
    const placed = placeLabels(crowded, pointsOf(crowded), view, center, 800, (n) => (n.id === "page" ? 2 : 0));
    expect(placed).toEqual([]);
  });

  it("moves a label off a line that would pass through it", () => {
    const nodes = [node("a", 100, 0, "area", "Core Formatting Engine"), node("b", 320, 0)];
    const points = pointsOf(nodes);
    const through = edgeSegments(points.get("a")!, points.get("b")!, 14, 14, false);
    const only = (n: WikiGraphNode) => (n.id === "a" ? 3 : 0);
    expect(placeLabels(nodes, points, view, center, 1200, only)[0].anchor).toBe("start");
    expect(placeLabels(nodes, points, view, center, 1200, only, through)[0].anchor).not.toBe("start");
  });

  it("keeps a label inside the pane", () => {
    const nodes = [node("edge", 380, 0, "area", "A name too long for the right margin")];
    const placed = placeLabels(nodes, pointsOf(nodes), view, center, 800, areaFirst);
    expect(placed[0].anchor).not.toBe("start");
  });
});
