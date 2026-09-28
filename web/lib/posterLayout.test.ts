// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

import {
  layoutAtlas,
  layoutPoster,
  nodeWidth,
  posterMode,
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

  it("runs forward edges from a box's bottom to the next layer's top", () => {
    const edge = layout.edges.find((e) => e.source === "lifecycle" && e.target === "utils")!;
    const from = layout.nodes.find((n) => n.id === "lifecycle")!;
    const to = layout.nodes.find((n) => n.id === "utils")!;
    const [, x1, y1] = /^M ([\d.]+) ([\d.]+)/.exec(edge.d)!.map(Number);
    expect(y1).toBeCloseTo(from.y + from.h);
    expect(x1).toBeGreaterThan(from.x);
    expect(edge.d.endsWith(`${to.y}`)).toBe(true);
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
});
