// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

import { DWELL_MS, TRAVEL_MS, edgeId, traceDuration, traceFrame, traceSteps } from "./traceTimeline";

describe("traceSteps", () => {
  it("adds a hop only where consecutive stages change area", () => {
    const steps = traceSteps(["a", "b", "b", undefined, "a"]);
    expect(steps.map((step) => step.kind)).toEqual(["dwell", "travel", "dwell", "dwell", "dwell", "dwell"]);
    expect(steps[1]).toMatchObject({ stage: 0, edge: edgeId("a", "b") });
    expect(traceDuration(steps)).toBe(5 * DWELL_MS + TRAVEL_MS);
  });
});

describe("traceFrame", () => {
  const steps = traceSteps(["a", "b"]);
  const total = traceDuration(steps);

  it("is empty before playback starts and once it ends", () => {
    expect(traceFrame(steps, -1)).toBeNull();
    expect(traceFrame(steps, total)).toBeNull();
  });

  it("rests on a stage, then eases along the hop from that stage", () => {
    expect(traceFrame(steps, 0)).toEqual({ stage: 0, edge: null, progress: 0 });
    expect(traceFrame(steps, DWELL_MS)).toEqual({ stage: 0, edge: null, progress: 0 });
    const mid = traceFrame(steps, DWELL_MS + TRAVEL_MS / 2);
    expect(mid?.stage).toBe(0);
    expect(mid?.edge).toBe(edgeId("a", "b"));
    expect(mid?.progress).toBeCloseTo(0.5);
    expect(traceFrame(steps, DWELL_MS + TRAVEL_MS + 1)).toEqual({ stage: 1, edge: null, progress: 0 });
  });

  it("returns the same frame for the same time", () => {
    expect(traceFrame(steps, 1234)).toEqual(traceFrame(steps, 1234));
  });
});
