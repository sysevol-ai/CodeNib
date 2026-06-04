import { describe, it, expect } from "vitest";
import {
  computeFilesPositions,
  type FilesLayoutItem,
  type FilesLayoutEdge,
} from "./filesLayout";

describe("computeFilesPositions — dependency layering", () => {
  it("stacks a file's symbols top→bottom by line at a fixed row pitch", () => {
    const items: FilesLayoutItem[] = [
      { id: "a", file: "m.ts", line: 30, external: false },
      { id: "b", file: "m.ts", line: 10, external: false },
      { id: "c", file: "m.ts", line: 20, external: false },
    ];
    // single file → no cross-file deps → fallback, but line-ordering still holds.
    const { positions, parentOf, boxes } = computeFilesPositions(items, []);
    expect(positions.a.x).toBe(positions.b.x); // same file → same lane
    expect(positions.b.y).toBeLessThan(positions.c.y); // line 10 above line 20
    expect(positions.c.y).toBeLessThan(positions.a.y); // line 20 above line 30
    expect(positions.c.y - positions.b.y).toBe(46); // default row pitch
    expect(parentOf.a).toBe("filebox::m.ts");
    expect(boxes).toEqual([{ id: "filebox::m.ts", file: "m.ts", layer: null }]);
  });

  it("orders files top→bottom by dependency depth (caller above callee)", () => {
    // A (a.ts) calls B (b.ts) calls C (c.ts): a linear dependency chain.
    const items: FilesLayoutItem[] = [
      { id: "a", file: "a.ts", line: 1, external: false },
      { id: "b", file: "b.ts", line: 1, external: false },
      { id: "c", file: "c.ts", line: 1, external: false },
    ];
    const edges: FilesLayoutEdge[] = [
      { source: "a", target: "b" },
      { source: "b", target: "c" },
    ];
    const { positions, boxes } = computeFilesPositions(items, edges);
    // entry (a) at the top, dependency (c) at the bottom.
    expect(positions.a.y).toBeLessThan(positions.b.y);
    expect(positions.b.y).toBeLessThan(positions.c.y);
    // a single chain → one lane (same x).
    expect(positions.a.x).toBe(positions.b.x);
    expect(positions.b.x).toBe(positions.c.x);
    // boxes carry their dependency layer.
    expect(boxes.map((b) => [b.file, b.layer])).toEqual([
      ["a.ts", 0],
      ["b.ts", 1],
      ["c.ts", 2],
    ]);
  });

  it("puts sibling dependencies in the same band, different lanes", () => {
    // A calls both B and C → B and C are at the same depth.
    const items: FilesLayoutItem[] = [
      { id: "a", file: "a.ts", line: 1, external: false },
      { id: "b", file: "b.ts", line: 1, external: false },
      { id: "c", file: "c.ts", line: 1, external: false },
    ];
    const edges: FilesLayoutEdge[] = [
      { source: "a", target: "b" },
      { source: "a", target: "c" },
    ];
    const { positions } = computeFilesPositions(items, edges);
    expect(positions.a.y).toBeLessThan(positions.b.y);
    expect(positions.b.y).toBe(positions.c.y); // same band
    expect(positions.b.x).not.toBe(positions.c.x); // different lanes
  });

  it("does not loop forever on a dependency cycle", () => {
    const items: FilesLayoutItem[] = [
      { id: "a", file: "a.ts", line: 1, external: false },
      { id: "b", file: "b.ts", line: 1, external: false },
    ];
    const edges: FilesLayoutEdge[] = [
      { source: "a", target: "b" },
      { source: "b", target: "a" }, // cycle
    ];
    const { positions } = computeFilesPositions(items, edges);
    expect(positions.a).toBeDefined();
    expect(positions.b).toBeDefined();
  });

  it("groups external/locationless symbols into one '· external' box", () => {
    const items: FilesLayoutItem[] = [
      { id: "x", file: "a.ts", line: 1, external: true },
      { id: "y", file: null, line: null, external: false },
      { id: "z", file: "a.ts", line: 5, external: false },
    ];
    const { parentOf } = computeFilesPositions(items, []);
    expect(parentOf.x).toBe("filebox::· external");
    expect(parentOf.y).toBe("filebox::· external");
    expect(parentOf.z).toBe("filebox::a.ts");
  });

  it("spaces lanes by measured box width so wide boxes don't overlap", () => {
    const base: FilesLayoutItem[] = [
      { id: "a", file: "a.ts", line: 1, external: false },
      { id: "b", file: "b.ts", line: 1, external: false },
      { id: "c", file: "c.ts", line: 1, external: false },
    ];
    const edges: FilesLayoutEdge[] = [
      { source: "a", target: "b" },
      { source: "a", target: "c" }, // b and c are siblings in the same band
    ];
    const narrow = computeFilesPositions(
      base.map((x) => ({ ...x, width: 80 })),
      edges
    );
    const wide = computeFilesPositions(
      base.map((x) => (x.id === "b" ? { ...x, width: 400 } : { ...x, width: 80 })),
      edges
    );
    const gapNarrow = narrow.positions.c.x - narrow.positions.b.x;
    const gapWide = wide.positions.c.x - wide.positions.b.x;
    expect(gapWide).toBeGreaterThan(gapNarrow); // a wide box pushes its neighbour further right
  });

  it("sinks files with no dependency edges to a bottom band", () => {
    const items: FilesLayoutItem[] = [
      { id: "a", file: "a.ts", line: 1, external: false },
      { id: "b", file: "b.ts", line: 1, external: false },
      { id: "iso", file: "iso.ts", line: 1, external: false }, // touches nothing
    ];
    const edges: FilesLayoutEdge[] = [{ source: "a", target: "b" }];
    const { positions, boxes } = computeFilesPositions(items, edges);
    expect(positions.a.y).toBeLessThan(positions.b.y); // caller above callee
    expect(positions.iso.y).toBeGreaterThan(positions.b.y); // isolated sinks below
    const isoLayer = boxes.find((x) => x.file === "iso.ts")!.layer!;
    const bLayer = boxes.find((x) => x.file === "b.ts")!.layer!;
    expect(isoLayer).toBeGreaterThan(bLayer);
  });

  it("is deterministic — identical input yields identical output", () => {
    const items: FilesLayoutItem[] = [
      { id: "a", file: "a.ts", line: 2, external: false },
      { id: "b", file: "b.ts", line: 1, external: false },
      { id: "c", file: "a.ts", line: 1, external: false },
    ];
    const edges: FilesLayoutEdge[] = [{ source: "a", target: "b" }];
    expect(computeFilesPositions(items, edges)).toEqual(
      computeFilesPositions(items, edges)
    );
  });
});
