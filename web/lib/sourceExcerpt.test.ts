import { describe, expect, it } from "vitest";
import { excerptLayout, revealLines, segmentRows } from "./sourceExcerpt";

describe("widening a source excerpt", () => {
  it("lays out hidden runs above, between and below the lines shown", () => {
    const rows = segmentRows("def prepare_url(\n⋯\n    try:\n        x()", [483, 510]);
    expect(rows?.numbers).toEqual([483, null, 510, 511]);
    expect(excerptLayout([483, 510, 511], { start: 480, end: 563 })).toEqual([
      { kind: "more", where: "above", from: 480, to: 482 },
      { kind: "line", number: 483 },
      { kind: "more", where: "gap", from: 484, to: 509 },
      { kind: "line", number: 510 },
      { kind: "line", number: 511 },
      { kind: "more", where: "below", from: 512, to: 563 },
    ]);
  });

  it("without a known definition keeps only the gaps", () => {
    expect(excerptLayout([12, 10, 11, 20])).toEqual([
      { kind: "line", number: 10 },
      { kind: "line", number: 11 },
      { kind: "line", number: 12 },
      { kind: "more", where: "gap", from: 13, to: 19 },
      { kind: "line", number: 20 },
    ]);
  });

  it("reveals a short run whole and a long one a step at a time", () => {
    const gap = { kind: "more", where: "gap", from: 484, to: 509 } as const;
    // 26 lines is within half a step of 20: one click shows them all.
    expect(revealLines(gap)).toHaveLength(26);
    const long = { kind: "more", where: "gap", from: 100, to: 199 } as const;
    // A gap opens upward from the lines the excerpt is about.
    expect(revealLines(long)).toEqual(Array.from({ length: 20 }, (_, i) => 180 + i));
    const below = { kind: "more", where: "below", from: 512, to: 563 } as const;
    expect(revealLines(below)[0]).toBe(512);
    expect(revealLines(below)).toHaveLength(20);
    const above = { kind: "more", where: "above", from: 1, to: 60 } as const;
    expect(revealLines(above).at(-1)).toBe(60);
  });
});
