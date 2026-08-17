import { describe, expect, it } from "vitest";
import { sourceIndexAtThreshold } from "./sourceNavigation";

describe("sourceIndexAtThreshold", () => {
  it("advances after each fragment crosses the reading threshold", () => {
    expect(sourceIndexAtThreshold([20, 240, 520], 100)).toBe(0);
    expect(sourceIndexAtThreshold([-220, 80, 360], 100)).toBe(1);
    expect(sourceIndexAtThreshold([-520, -220, 70], 100)).toBe(2);
  });

  it("does not skip ahead to a final fragment below the threshold", () => {
    expect(sourceIndexAtThreshold([-500, 80, 420], 100)).toBe(1);
  });

  it("selects the final renderable fragment at the real scroll boundary", () => {
    expect(
      sourceIndexAtThreshold([-500, 80, 420, Number.POSITIVE_INFINITY], 100, true),
    ).toBe(2);
  });

  it("reports no selection without source fragments", () => {
    expect(sourceIndexAtThreshold([], 100)).toBe(-1);
  });
});
