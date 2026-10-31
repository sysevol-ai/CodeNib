// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

import {
  layoutAtlas,
  layoutPoster,
  nodeWidth,
  posterMode,
  thumbLayout,
  tracedHops,
  wrapTitle,
} from "./posterLayout";

const areas = [
  { id: "session", title: "Session Orchestration", symbols: 69, files: 2 },
  { id: "lifecycle", title: "Request Lifecycle and Preparation", symbols: 215, files: 5 },
  { id: "response", title: "Response Handling", symbols: 33, files: 2 },
  { id: "utils", title: "Utilities, Compatibility, and Diagnostics", symbols: 88, files: 4 },
  { id: "empty", title: "Integration Workflows", symbols: 0, files: 0 },
];
const links = [
  { source: "session", target: "lifecycle", weight: 105 },
  { source: "lifecycle", target: "session", weight: 12 },
  { source: "lifecycle", target: "utils", weight: 199 },
  { source: "session", target: "response", weight: 20 },
  { source: "response", target: "utils", weight: 3 },
];

describe("wrapTitle", () => {
  it("wraps on words and clips only the last allowed line", () => {
    expect(wrapTitle("Request Lifecycle and Preparation", 18)).toEqual([
      "Request Lifecycle",
      "and Preparation",
    ]);
    expect(wrapTitle("Utilities, Compatibility, and Diagnostics", 12, 2)).toEqual([
      "Utilities,",
      "Compatibili…",
    ]);
  });

  it("keeps the first line short when stop tags share its row", () => {
    expect(wrapTitle("Templating Engine", 23, 3, 10)).toEqual(["Templating", "Engine"]);
    expect(wrapTitle("Templating Engine", 23, 3, 6)).toEqual(["", "Templating Engine"]);
  });
});

describe("layoutPoster", () => {
  const layout = layoutPoster(areas, links, { width: 960, traced: [["lifecycle", "session"]] });

  it("leaves out areas with nothing mapped to them", () => {
    expect(layout.nodes.map((node) => node.id)).not.toContain("empty");
    expect(layout.nodes).toHaveLength(4);
  });

  it("puts a caller left of what it mostly references", () => {
    const x = new Map(layout.nodes.map((node) => [node.id, node.x]));
    expect(x.get("session")!).toBeLessThan(x.get("lifecycle")!);
    expect(x.get("lifecycle")!).toBeLessThan(x.get("utils")!);
  });

  it("keeps every box inside the canvas without overlaps", () => {
    for (const node of layout.nodes) {
      expect(node.x).toBeGreaterThanOrEqual(0);
      expect(node.x + node.w).toBeLessThanOrEqual(layout.width);
      expect(node.y).toBeGreaterThanOrEqual(0);
      expect(node.y + node.h).toBeLessThanOrEqual(layout.height);
    }
    for (const a of layout.nodes) {
      for (const b of layout.nodes) {
        if (a === b || a.column !== b.column) continue;
        expect(a.y + a.h <= b.y || b.y + b.h <= a.y).toBe(true);
      }
    }
  });

  it("draws both directions of a mutual pair and marks traced hops", () => {
    const keys = layout.edges.map((edge) => `${edge.source}>${edge.target}`);
    expect(keys).toContain("session>lifecycle");
    expect(keys).toContain("lifecycle>session");
    const back = layout.edges.find((edge) => edge.source === "lifecycle" && edge.target === "session")!;
    expect(back.traced).toBe(true);
    const forward = layout.edges.find((edge) => edge.source === "session" && edge.target === "lifecycle")!;
    expect(forward.d).not.toBe(back.d);
  });

  it("scales stroke width with weight", () => {
    const heaviest = layout.edges.find((edge) => edge.weight === 199)!;
    const lightest = layout.edges.find((edge) => edge.weight === 3)!;
    expect(heaviest.width).toBeGreaterThan(lightest.width);
  });

  it("keeps the strongest links when it has to drop some, but never a traced hop", () => {
    const trimmed = layoutPoster(areas, links, {
      width: 960,
      maxEdges: 2,
      traced: [["response", "utils"]],
    });
    const keys = trimmed.edges.map((edge) => `${edge.source}>${edge.target}`);
    expect(keys).toEqual(["lifecycle>utils", "session>lifecycle", "response>utils"]);
  });
});

describe("nodeWidth", () => {
  it("shrinks boxes before it squeezes the gaps", () => {
    expect(nodeWidth(960, 4)).toBeLessThan(nodeWidth(960, 3));
    expect(nodeWidth(400, 4)).toBe(150);
    expect(nodeWidth(2000, 2)).toBe(220);
  });
});

describe("tracedHops", () => {
  it("collapses consecutive stages in one area and skips unplaced ones", () => {
    expect(tracedHops(["a", "b", "b", undefined, "a", "a", "c"])).toEqual({
      visits: ["a", "b", "a", "c"],
      hops: [
        ["a", "b"],
        ["b", "a"],
        ["a", "c"],
      ],
    });
  });
});

describe("layoutPoster on a phone-width canvas", () => {
  const layout = layoutPoster(areas, links, { width: 350 });

  it("stacks layers top to bottom, callers above what they reference", () => {
    const y = new Map(layout.nodes.map((node) => [node.id, node.y]));
    expect(y.get("session")!).toBeLessThan(y.get("lifecycle")!);
    expect(y.get("lifecycle")!).toBeLessThan(y.get("utils")!);
    for (const node of layout.nodes) {
      expect(node.x).toBeGreaterThanOrEqual(0);
      expect(node.x + node.w).toBeLessThanOrEqual(350);
    }
  });

  it("never overlaps two boxes", () => {
    for (const a of layout.nodes) {
      for (const b of layout.nodes) {
        if (a === b) continue;
        const apart =
          a.x + a.w <= b.x || b.x + b.w <= a.x || a.y + a.h <= b.y || b.y + b.h <= a.y;
        expect(apart).toBe(true);
      }
    }
  });

  it("puts every box in one column with the links on a rail to its left", () => {
    const xs = new Set(layout.nodes.map((node) => node.x));
    expect(xs.size).toBe(1);
    const left = layout.nodes[0].x;
    for (const edge of layout.edges) {
      const points = routePoints(edge.d);
      expect(points[0].x).toBeCloseTo(left);
      expect(points[points.length - 1].x).toBeCloseTo(left);
      for (const point of points.slice(1, -1)) expect(point.x).toBeLessThan(left);
    }
  });

  it("gives links whose spans overlap separate lanes", () => {
    const lanes = layout.edges.map((edge) => {
      const points = routePoints(edge.d);
      const ys = points.map((point) => point.y);
      return { x: Math.min(...points.map((point) => point.x)), lo: Math.min(...ys), hi: Math.max(...ys) };
    });
    for (const a of lanes) {
      for (const b of lanes) {
        if (a === b || Math.abs(a.x - b.x) > 0.5) continue;
        expect(a.hi <= b.lo + 0.5 || b.hi <= a.lo + 0.5).toBe(true);
      }
    }
  });
});

/** The corner points of a route: every M and L target and each bend's control point. */
function routePoints(d: string): Array<{ x: number; y: number }> {
  const points: Array<{ x: number; y: number }> = [];
  for (const [, command, args] of d.matchAll(/([MLQ])\s*([^MLQ]*)/g)) {
    const n = args.trim().split(/[\s,]+/).map(Number);
    if (command === "Q") points.push({ x: n[0], y: n[1] }, { x: n[2], y: n[3] });
    else points.push({ x: n[0], y: n[1] });
  }
  return points;
}

describe("layoutPoster schematic routes", () => {
  const skipping = [...links, { source: "session", target: "utils", weight: 40 }];
  const layout = layoutPoster(areas, skipping, { width: 960, traced: [["session", "lifecycle"]] });
  const inside = (x: number, y: number) =>
    layout.nodes.some((node) => x > node.x + 1 && x < node.x + node.w - 1 && y > node.y + 1 && y < node.y + node.h - 1);

  it("draws every straight run horizontally or vertically", () => {
    for (const edge of layout.edges) {
      const runs = [...edge.d.matchAll(/L ([\d.-]+) ([\d.-]+)/g)].map((m) => [Number(m[1]), Number(m[2])]);
      const starts = [...edge.d.matchAll(/(?:M|Q [\d.-]+ [\d.-]+) ([\d.-]+) ([\d.-]+)/g)].map((m) => [
        Number(m[1]),
        Number(m[2]),
      ]);
      runs.forEach(([x, y], index) => {
        const [x0, y0] = starts[index];
        expect(Math.abs(x - x0) < 0.2 || Math.abs(y - y0) < 0.2).toBe(true);
      });
    }
  });

  it("never runs a link through a box", () => {
    for (const edge of layout.edges) {
      const points = routePoints(edge.d);
      for (let i = 1; i < points.length; i += 1) {
        const [a, b] = [points[i - 1], points[i]];
        for (let t = 0.05; t < 1; t += 0.05) {
          expect(inside(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t)).toBe(false);
        }
      }
    }
  });

  it("ends each link on its target's facing side, where the arrowhead sits", () => {
    for (const edge of layout.edges) {
      const points = routePoints(edge.d);
      const end = points[points.length - 1];
      const target = layout.nodes.find((node) => node.id === edge.target)!;
      const onSide = Math.abs(end.x - target.x) < 0.2 || Math.abs(end.x - (target.x + target.w)) < 0.2;
      expect(onSide).toBe(true);
      expect(end.y).toBeGreaterThan(target.y);
      expect(end.y).toBeLessThan(target.y + target.h);
    }
  });

  it("carries a link that skips a column through a gap in it", () => {
    const edge = layout.edges.find((e) => e.source === "session" && e.target === "utils")!;
    const from = layout.nodes.find((node) => node.id === "session")!;
    const to = layout.nodes.find((node) => node.id === "utils")!;
    expect(to.column - from.column).toBeGreaterThan(1);
    const points = routePoints(edge.d);
    expect(points[0].x).toBeCloseTo(from.x + from.w);
    expect(points[points.length - 1].x).toBeCloseTo(to.x);
  });

  it("steps stroke widths in three classes", () => {
    expect(new Set(layout.edges.map((edge) => edge.width)).size).toBeLessThanOrEqual(3);
  });

  it("keeps skipping links outside boxes when the gap lanes are exhausted", () => {
    const areas = [..."abcdef"].map((id) => ({ id, title: id, symbols: 10, files: 1 }));
    const links = ["ab", "bc", "cd", "ae", "ef", "fd", "ac", "ad", "bd", "ca", "da", "db"]
      .map(([source, target], index) => ({ source, target, weight: index < 6 ? 100 - index : 16 - index }));
    const dense = layoutPoster(areas, links, { width: 1000 });
    for (const edge of dense.edges) {
      const points = routePoints(edge.d);
      for (let i = 1; i < points.length; i += 1) {
        const [a, b] = [points[i - 1], points[i]];
        for (const node of dense.nodes) {
          // Straight runs and bend control polygons must stay out of every
          // box, including intermediate areas unrelated to this reference.
          const crosses = Math.min(a.x, b.x) < node.x + node.w - 1 &&
            Math.max(a.x, b.x) > node.x + 1 &&
            Math.min(a.y, b.y) < node.y + node.h - 1 &&
            Math.max(a.y, b.y) > node.y + 1;
          expect(crosses, `${edge.source}→${edge.target} crosses ${node.id}`).toBe(false);
        }
      }
    }
  });
});

describe("posterMode", () => {
  it("draws a map only when most areas are linked", () => {
    expect(posterMode(areas, links).mode).toBe("network");
    const lonely = [{ source: "session", target: "lifecycle", weight: 36 }];
    expect(posterMode(areas, lonely).mode).toBe("atlas");
    expect(posterMode(areas, []).mode).toBe("atlas");
  });
});

describe("layoutAtlas", () => {
  const atlas = layoutAtlas(areas, 900);

  it("fills the canvas with one cell per non-empty area, sized by symbols", () => {
    expect(atlas.cells.map((cell) => cell.id)).toEqual(["lifecycle", "utils", "session", "response"]);
    const areaOf = (id: string) => {
      const cell = atlas.cells.find((c) => c.id === id)!;
      return (cell.w + 6) * (cell.h + 6);
    };
    const total = atlas.cells.reduce((sum, cell) => sum + areaOf(cell.id), 0);
    expect(areaOf("lifecycle") / total).toBeCloseTo(215 / 405, 2);
    expect(areaOf("response") / total).toBeCloseTo(33 / 405, 2);
  });

  it("keeps cells inside the canvas and apart from each other", () => {
    for (const a of atlas.cells) {
      expect(a.x).toBeGreaterThanOrEqual(0);
      expect(a.y).toBeGreaterThanOrEqual(0);
      expect(a.x + a.w).toBeLessThanOrEqual(atlas.width + 0.001);
      expect(a.y + a.h).toBeLessThanOrEqual(atlas.height + 0.001);
      for (const b of atlas.cells) {
        if (a === b) continue;
        const apart =
          a.x + a.w <= b.x + 0.001 || b.x + b.w <= a.x + 0.001 ||
          a.y + a.h <= b.y + 0.001 || b.y + b.h <= a.y + 0.001;
        expect(apart).toBe(true);
      }
    }
  });

  it("reserves a title row below journey tags in sparse phone maps", () => {
    const area = { id: "request", title: "Request Lifecycle Handling", symbols: 10, files: 1 };
    const plain = layoutAtlas([area], 350).cells[0];
    const tagged = layoutAtlas([area], 350, new Map([[area.id, 6]])).cells[0];
    expect(plain.titleOffset).toBe(0);
    expect(tagged.lines.join(" ")).toBe(area.title);
    // Tags occupy y=7..21; title glyphs start at 10 + titleOffset.
    expect(10 + tagged.titleOffset).toBeGreaterThan(21);
    expect(tagged.h).toBe(plain.h);
  });
});

describe("thumbLayout", () => {
  it("places every non-empty area inside the thumbnail", () => {
    const thumb = thumbLayout(areas, links, 320, 104);
    expect(thumb.dots.map((dot) => dot.id).sort()).toEqual(["lifecycle", "response", "session", "utils"]);
    for (const dot of thumb.dots) {
      expect(dot.x - dot.r).toBeGreaterThanOrEqual(0);
      expect(dot.x + dot.r).toBeLessThanOrEqual(320);
      expect(dot.y - dot.r).toBeGreaterThanOrEqual(0);
      expect(dot.y + dot.r).toBeLessThanOrEqual(104);
    }
    expect(thumb.lines.length).toBe(links.length);
  });

  it("lays a sparse index out as one row of dots, biggest first", () => {
    const thumb = thumbLayout(areas, [], 320, 104);
    expect(new Set(thumb.dots.map((dot) => dot.y)).size).toBe(1);
    const leftmost = [...thumb.dots].sort((a, b) => a.x - b.x)[0];
    expect(leftmost.id).toBe("lifecycle");
    expect(thumb.lines).toEqual([]);
  });
});

describe("layoutAtlas height", () => {
  it("gives a few areas a shorter canvas than many", () => {
    const three = layoutAtlas(areas.slice(0, 3), 900);
    const many = layoutAtlas(
      Array.from({ length: 10 }, (_, i) => ({ id: `a${i}`, title: `Area ${i}`, symbols: 10 + i, files: 1 })),
      900,
    );
    expect(three.height).toBeLessThan(many.height);
    expect(three.height).toBeLessThanOrEqual(252);
  });
});
