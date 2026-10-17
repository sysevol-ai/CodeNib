// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * Playback of a poster's traced path: rest on each stage, travel along the
 * hop when the next stage lives in another area. A frame is a pure function
 * of elapsed time, so the poster's own playback and a video renderer that
 * seeks frame by frame draw the same picture.
 */

import type { WikiPageRef } from "./api";
import type { JourneyStage } from "./wikiPresentation";

export const DWELL_MS = 950;
export const TRAVEL_MS = 900;

export type TraceStep =
  | { kind: "dwell"; stage: number; ms: number }
  | { kind: "travel"; stage: number; edge: string; ms: number };

export interface TraceFrame {
  /** Stage being shown; during a hop, the stage the hop leaves. */
  stage: number;
  /** `source\0target` of the hop being travelled, or null while resting. */
  edge: string | null;
  /** Eased position along the hop, 0 to 1. */
  progress: number;
}

export const edgeId = (source: string, target: string) => `${source}\u0000${target}`;

/** Top-level area (page id) of each stage; undefined when it names no page. */
export function journeyAreas(stages: JourneyStage[], pages: WikiPageRef[]): Array<string | undefined> {
  const areaOf = new Map<string, string>();
  const walk = (list: WikiPageRef[], top: string | null) => {
    for (const page of list) {
      const area = top ?? page.id;
      areaOf.set(page.id, area);
      walk(page.children, area);
    }
  };
  walk(pages, null);
  return stages.map((stage) => (stage.page ? areaOf.get(stage.page.id) ?? stage.page.id : undefined));
}

/** One step per stage, plus a hop wherever consecutive stages change area. */
export function traceSteps(stageAreas: Array<string | undefined>): TraceStep[] {
  const steps: TraceStep[] = [];
  stageAreas.forEach((here, index) => {
    steps.push({ kind: "dwell", stage: index, ms: DWELL_MS });
    const next = stageAreas[index + 1];
    if (index + 1 < stageAreas.length && here && next && here !== next) {
      steps.push({ kind: "travel", stage: index, edge: edgeId(here, next), ms: TRAVEL_MS });
    }
  });
  return steps;
}

export function traceDuration(steps: TraceStep[]): number {
  return steps.reduce((sum, step) => sum + step.ms, 0);
}

/** The frame `t` ms into playback; null before it starts and once it ends. */
export function traceFrame(steps: TraceStep[], t: number): TraceFrame | null {
  if (t < 0 || t >= traceDuration(steps)) return null;
  let rest = t;
  for (const step of steps) {
    if (rest > step.ms) {
      rest -= step.ms;
      continue;
    }
    return step.kind === "dwell"
      ? { stage: step.stage, edge: null, progress: 0 }
      : { stage: step.stage, edge: step.edge, progress: 0.5 - Math.cos((Math.PI * rest) / step.ms) / 2 };
  }
  return null;
}
