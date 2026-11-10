// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { labelLines, type WikiGraphNode } from "@/lib/wikiGraph";

export interface View {
  x: number;
  y: number;
  k: number;
}

export interface Point {
  x: number;
  y: number;
}

export interface PlacedLabel {
  id: string;
  lines: string[];
  anchor: "start" | "middle" | "end";
  dx: number;
  dy: number;
}

/** One straight piece of a drawn line, in layout coordinates. */
export interface Segment {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

interface Box {
  left: number;
  right: number;
  top: number;
  bottom: number;
}

// Geist at the label size averages about this wide per character.
const LABEL_CHAR_PX = 6.1;
// The active label is set heavier, and so wider.
const LABEL_HEAVY = 1.08;
const LABEL_HEIGHT_PX = 15;
export const LABEL_LINE_PX = 14;
const LABEL_MARGIN_PX = 8;
/** A page with this priority or more keeps its name wherever it has to go. */
const ALWAYS_NAMED = 4;
/** An area may touch an earlier label by this share of its own box, no more. */
const AREA_OVERLAP = 0.12;
/** How many crossed lines a later position must save to be chosen. */
const POSITION_COST = 0.5;

/** The control points of the line drawn between two nodes. */
export function edgeCurve(a: Point, b: Point, ar: number, br: number, curved: boolean) {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const length = Math.max(Math.hypot(dx, dy), 1);
  const ux = dx / length;
  const uy = dy / length;
  const x1 = a.x + ux * ar;
  const y1 = a.y + uy * ar;
  const x2 = b.x - ux * (br + (curved ? 3 : 0));
  const y2 = b.y - uy * (br + (curved ? 3 : 0));
  const bend = curved ? Math.min(length * 0.12, 34) : 0;
  return { x1, y1, cx: (x1 + x2) / 2 - uy * bend, cy: (y1 + y2) / 2 + ux * bend, x2, y2, curved };
}

/** A curved path between two nodes, so two-way references stay two visible lines. */
export function edgePath(a: Point, b: Point, ar: number, br: number, curved: boolean): string {
  const c = edgeCurve(a, b, ar, br, curved);
  if (!curved) return `M${c.x1} ${c.y1}L${c.x2} ${c.y2}`;
  return `M${c.x1} ${c.y1}Q${c.cx} ${c.cy} ${c.x2} ${c.y2}`;
}

/** The same line as short straight pieces, for testing what it passes over. */
export function edgeSegments(a: Point, b: Point, ar: number, br: number, curved: boolean): Segment[] {
  const c = edgeCurve(a, b, ar, br, curved);
  if (!curved) return [{ x1: c.x1, y1: c.y1, x2: c.x2, y2: c.y2 }];
  const pieces = 6;
  const at = (t: number) => ({
    x: (1 - t) * (1 - t) * c.x1 + 2 * (1 - t) * t * c.cx + t * t * c.x2,
    y: (1 - t) * (1 - t) * c.y1 + 2 * (1 - t) * t * c.cy + t * t * c.y2,
  });
  const out: Segment[] = [];
  let from = at(0);
  for (let i = 1; i <= pieces; i += 1) {
    const to = at(i / pieces);
    out.push({ x1: from.x, y1: from.y, x2: to.x, y2: to.y });
    from = to;
  }
  return out;
}

const overlaps = (a: Box, b: Box) => a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;

const overlapArea = (a: Box, b: Box) =>
  Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left)) *
  Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top));

/** Whether a straight piece passes through a box (Liang–Barsky clipping). */
function crosses(s: Segment, box: Box): boolean {
  const dx = s.x2 - s.x1;
  const dy = s.y2 - s.y1;
  let t0 = 0;
  let t1 = 1;
  const clip = (p: number, q: number) => {
    if (p === 0) return q >= 0;
    const t = q / p;
    if (p < 0) {
      if (t > t1) return false;
      if (t > t0) t0 = t;
    } else {
      if (t < t0) return false;
      if (t < t1) t1 = t;
    }
    return true;
  };
  return (
    clip(-dx, s.x1 - box.left) && clip(dx, box.right - s.x1) && clip(-dy, s.y1 - box.top) && clip(dy, box.bottom - s.y1)
  );
}

/**
 * Labels hold their on-screen size at any zoom. Each tries the side facing
 * away from the map's centre first, then the other sides and the corners, and
 * takes the free one that the fewest lines pass through. More important pages are
 * placed first. With eight positions an area nearly always keeps its name; a
 * label whose every position lands on an earlier one is left out, unless it is
 * the page being read.
 */
export function placeLabels(
  nodes: WikiGraphNode[],
  points: Map<string, Point>,
  view: View,
  center: Point,
  paneWidth: number,
  priority: (node: WikiGraphNode) => number,
  lines: Segment[] = [],
): PlacedLabel[] {
  const taken: Box[] = [];
  const placed: PlacedLabel[] = [];
  const screen = lines.map((s) => ({
    x1: s.x1 * view.k + view.x,
    y1: s.y1 * view.k + view.y,
    x2: s.x2 * view.k + view.x,
    y2: s.y2 * view.k + view.y,
  }));
  // Labels also keep off the large marks, so a name never hides a node.
  const marks = nodes
    .filter((node) => node.kind === "area")
    .map((node) => {
      const point = points.get(node.id)!;
      const sx = point.x * view.k + view.x;
      const sy = point.y * view.k + view.y;
      const r = node.radius * view.k;
      return { id: node.id, left: sx - r, right: sx + r, top: sy - r, bottom: sy + r };
    });
  const ordered = nodes
    .filter((node) => priority(node) > 0)
    .sort((a, b) => priority(b) - priority(a) || b.radius - a.radius);
  for (const node of ordered) {
    const point = points.get(node.id)!;
    const rank = priority(node);
    const text = labelLines(node.title);
    const extra = (text.length - 1) * LABEL_LINE_PX;
    const width = Math.max(...text.map((line) => line.length)) * LABEL_CHAR_PX * (rank >= 4 ? LABEL_HEAVY : 1);
    const sx = point.x * view.k + view.x;
    const sy = point.y * view.k + view.y;
    const radius = node.radius * view.k;
    const away = point.x - center.x;
    const beside = Math.abs(away) > 40;

    const right = { anchor: "start" as const, dx: radius + 7, dy: 4 - extra / 2 };
    const left = { anchor: "end" as const, dx: -(radius + 7), dy: 4 - extra / 2 };
    const above = { anchor: "middle" as const, dx: 0, dy: -(radius + 8) - extra };
    const below = { anchor: "middle" as const, dx: 0, dy: radius + 15 };
    // The corners, for a node whose four sides are all taken.
    const reach = radius * 0.7 + 6;
    const corners = [
      { anchor: "start" as const, dx: reach, dy: -reach - extra },
      { anchor: "start" as const, dx: reach, dy: reach + 9 },
      { anchor: "end" as const, dx: -reach, dy: -reach - extra },
      { anchor: "end" as const, dx: -reach, dy: reach + 9 },
    ];
    if (away <= 0) corners.reverse();
    const [outer, inner] = away > 0 ? [right, left] : [left, right];
    const [nearV, farV] = point.y < center.y ? [above, below] : [below, above];
    const options = [...(beside ? [outer, nearV, farV, inner] : [nearV, outer, inner, farV]), ...corners];

    type Choice = { option: (typeof options)[number]; box: Box; cost: number };
    let best: Choice | null = null;
    let fallback: Choice | null = null;
    const others = [...taken, ...marks.filter((mark) => mark.id !== node.id)];
    for (const [index, option] of options.entries()) {
      const x = sx + option.dx - (option.anchor === "middle" ? width / 2 : option.anchor === "end" ? width : 0);
      const box = { left: x, right: x + width, top: sy + option.dy - 11, bottom: sy + option.dy - 11 + LABEL_HEIGHT_PX + extra };
      const outside = box.left < LABEL_MARGIN_PX || box.right > paneWidth - LABEL_MARGIN_PX;
      const blocked = outside || others.some((other) => overlaps(box, other));
      if (!blocked) {
        const cost = screen.filter((s) => crosses(s, box)).length + index * POSITION_COST;
        if (!best || cost < best.cost) best = { option, box, cost };
        continue;
      }
      // Kept only for a page that must be named: the position that covers least.
      const covered = others.reduce((sum, other) => sum + overlapArea(box, other), 0);
      const cost = covered + (outside ? 1e6 : 0) + index;
      const slight = !outside && covered <= AREA_OVERLAP * width * (LABEL_HEIGHT_PX + extra);
      if (rank < ALWAYS_NAMED && !(node.kind === "area" && slight)) continue;
      if (!fallback || cost < fallback.cost) fallback = { option, box, cost };
    }
    const chosen: Choice | null = best ?? fallback;
    if (!chosen) continue;
    taken.push(chosen.box);
    placed.push({ id: node.id, lines: text, anchor: chosen.option.anchor, dx: chosen.option.dx, dy: chosen.option.dy });
  }
  return placed;
}
