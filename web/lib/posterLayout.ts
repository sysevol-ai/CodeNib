// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * Geometry for the Overview poster: the wiki's areas as boxes and the
 * recorded references between them as orthogonal routes, drawn like a
 * schematic, in a fixed virtual canvas the SVG scales to fit.
 *
 * Columns come from `layoutAreas`, so an area sits left of the areas it
 * mostly references. Unlike the compact area map, the poster also draws the
 * weaker direction of a mutual pair and edges that run backwards; every drawn
 * route is one recorded link, in its own lane so that neither hides the other.
 */

import { layoutAreas, type AreaLink } from "./areaLayout";

export interface PosterArea {
  id: string;
  title: string;
  symbols: number;
  files: number;
}

export interface PosterLink extends AreaLink {
  calls?: number;
}

export interface PosterNode extends PosterArea {
  x: number;
  y: number;
  w: number;
  h: number;
  column: number;
  /** Title wrapped to at most two lines. */
  lines: string[];
  /** Share of all mapped symbols, 0..1. */
  share: number;
}

export interface PosterEdge {
  source: string;
  target: string;
  weight: number;
  calls: number;
  /** SVG path data. */
  d: string;
  /** Middle of the route's longest run, for a label or hit target. */
  mid: { x: number; y: number };
  width: number;
  /** Part of the traced call path. */
  traced: boolean;
}

export interface PosterLayout {
  width: number;
  height: number;
  nodes: PosterNode[];
  edges: PosterEdge[];
}

export const POSTER_WIDTH = 1000;
const PAD_X = 16;
const PAD_Y = 30;
const GAP_Y = 30;
const MIN_GAP_X = 76;
const LINE_H = 18;
const MIN_HEIGHT = 220;
const MAX_LINES = 3;
// Phone-width canvases stack the boxes in one column with a rail of links.
const VERTICAL_BELOW = 600;
const GAP_Y_STACKED = 12;
const LAYER_GAP_STACKED = 24;
const RAIL_INSET = 12;
const RAIL_LANE = 5;
// Width of one stop tag in a box's top-right corner, gap included.
export const TAG_W = 17;
// Schematic routing: lane spacing in a channel and the bend radius.
const LANE_GAP = 6;
const BEND_R = 5;
// Average advance of Geist at 14px/600, used to wrap titles without layout.
const CHAR_W = 7.6;

/** Box width for `count` columns across `width`, keeping room for curves. */
export function nodeWidth(width: number, count: number): number {
  const room = (width - PAD_X * 2 - MIN_GAP_X * Math.max(0, count - 1)) / Math.max(1, count);
  return Math.max(150, Math.min(220, Math.floor(room)));
}

/**
 * Word-wrap a box title. `firstMax` narrows the first line, which shares the
 * box's top row with the traced path's stop tags.
 */
export function wrapTitle(title: string, max = 23, maxLines = MAX_LINES, firstMax = max): string[] {
  const words = title.split(/\s+/).filter(Boolean);
  const lines: string[] = [];
  let line = "";
  for (const word of words) {
    // A first word too long to sit beside the tags starts on the second line.
    if (!line && lines.length === 0 && firstMax < max && word.length > firstMax) lines.push("");
    const next = line ? `${line} ${word}` : word;
    if (next.length <= (lines.length === 0 ? firstMax : max) || !line) {
      line = next;
    } else {
      lines.push(line);
      line = word;
    }
  }
  if (line) lines.push(line);
  const clip = (l: string, limit = max) => (l.length > limit ? `${l.slice(0, limit - 1).trimEnd()}…` : l);
  const fitted = lines.map((l, i) => (i === 0 ? clip(l, Math.max(firstMax, 1)) : clip(l)));
  if (lines.length <= maxLines) return fitted;
  const last = lines.slice(maxLines - 1).join(" ");
  return [...fitted.slice(0, maxLines - 1), clip(last)];
}

function nodeHeight(lines: number): number {
  // padding + title lines + gap + meta line + padding
  return 14 + lines * LINE_H + 6 + 16 + 16;
}

type Pt = { x: number; y: number };
type Side = "l" | "r" | "t" | "b";

/** A polyline with every bend rounded by a quarter turn of radius `r`. */
function roundedPath(points: Pt[], r = BEND_R): string {
  const pts = points.filter(
    (p, i) => i === 0 || Math.abs(p.x - points[i - 1].x) > 0.01 || Math.abs(p.y - points[i - 1].y) > 0.01,
  );
  const f = (n: number) => Math.round(n * 10) / 10;
  let d = `M ${f(pts[0].x)} ${f(pts[0].y)}`;
  for (let i = 1; i < pts.length - 1; i += 1) {
    const [a, p, b] = [pts[i - 1], pts[i], pts[i + 1]];
    const la = Math.hypot(p.x - a.x, p.y - a.y);
    const lb = Math.hypot(b.x - p.x, b.y - p.y);
    const rr = Math.min(r, la / 2, lb / 2);
    const s = { x: p.x + ((a.x - p.x) * rr) / la, y: p.y + ((a.y - p.y) * rr) / la };
    const e = { x: p.x + ((b.x - p.x) * rr) / lb, y: p.y + ((b.y - p.y) * rr) / lb };
    d += ` L ${f(s.x)} ${f(s.y)} Q ${f(p.x)} ${f(p.y)} ${f(e.x)} ${f(e.y)}`;
  }
  const last = pts[pts.length - 1];
  return `${d} L ${f(last.x)} ${f(last.y)}`;
}

/** Crossings between the horizontal and vertical runs of different routes. */
function crossings(routes: Pt[][]): number {
  type Run = { route: number; x1: number; y1: number; x2: number; y2: number };
  const flat: Run[] = [];
  const upright: Run[] = [];
  routes.forEach((pts, route) => {
    for (let i = 1; i < pts.length; i += 1) {
      const [a, b] = [pts[i - 1], pts[i]];
      const run = {
        route,
        x1: Math.min(a.x, b.x),
        x2: Math.max(a.x, b.x),
        y1: Math.min(a.y, b.y),
        y2: Math.max(a.y, b.y),
      };
      if (Math.abs(a.y - b.y) < 0.01) flat.push(run);
      else if (Math.abs(a.x - b.x) < 0.01) upright.push(run);
    }
  });
  let count = 0;
  for (const h of flat) {
    for (const v of upright) {
      if (h.route === v.route) continue;
      if (v.x1 > h.x1 + 0.5 && v.x1 < h.x2 - 0.5 && h.y1 > v.y1 + 0.5 && h.y1 < v.y2 - 0.5) count += 1;
    }
  }
  return count;
}

/** Stroke width in three steps, so a heavier link reads as a class, not a pixel. */
function strokeStep(weight: number, maxWeight: number): number {
  const ratio = Math.log(1 + weight) / Math.log(1 + maxWeight);
  return ratio < 1 / 3 ? 1 : ratio < 2 / 3 ? 1.75 : 2.5;
}

/** Midpoint of the longest run, where a label has the most room. */
function longestRunMid(points: Pt[]): Pt {
  let best = -1;
  let mid = points[0];
  for (let i = 1; i < points.length; i += 1) {
    const [a, b] = [points[i - 1], points[i]];
    const length = Math.hypot(b.x - a.x, b.y - a.y);
    if (length > best) {
      best = length;
      mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
    }
  }
  return mid;
}

export function layoutPoster(
  areas: PosterArea[],
  links: PosterLink[],
  {
    traced = [],
    maxEdges = 14,
    maxColumns = 4,
    width = POSTER_WIDTH,
    tags = new Map(),
  }: {
    traced?: Array<[string, string]>;
    maxEdges?: number;
    maxColumns?: number;
    width?: number;
    /** Stop tags each area carries in its top-right corner. */
    tags?: Map<string, number>;
  } = {},
): PosterLayout {
  // An area with nothing mapped to it is an outline entry, not a subsystem.
  const shown = areas.filter((area) => area.symbols > 0);
  const ids = shown.map((area) => area.id);
  const known = new Set(ids);
  const valid = links.filter(
    (link) => known.has(link.source) && known.has(link.target) && link.source !== link.target,
  );
  // A phone-width canvas puts every box in one column with the links on a
  // rail to its left; the flow then reads downwards.
  const vertical = width < VERTICAL_BELOW;
  const fit = vertical
    ? maxColumns
    : Math.max(1, Math.floor((width - PAD_X * 2 + MIN_GAP_X) / (150 + MIN_GAP_X)));
  const { columns } = layoutAreas(ids, valid, {
    maxDrawn: maxEdges,
    maxColumns: Math.min(maxColumns, fit),
  });

  // The strongest links, plus every hop of the traced path whatever its weight.
  const tracedKeys = new Set(traced.map(([s, t]) => `${s}\u0000${t}`));
  const keyOf = (link: PosterLink) => `${link.source}\u0000${link.target}`;
  const sorted = [...valid].sort(
    (a, b) => b.weight - a.weight || a.source.localeCompare(b.source) || a.target.localeCompare(b.target),
  );
  const chosen = sorted.filter((link, index) => index < maxEdges || tracedKeys.has(keyOf(link)));
  const maxWeight = Math.max(1, ...chosen.map((link) => link.weight));

  // On the rail, each link holds one lane over the boxes it spans; a short
  // link takes an inner lane, so longer ones wrap around it without crossing.
  const order = columns.flat();
  const rank = new Map(order.map((id, index) => [id, index]));
  const railLane = new Map<string, number>();
  let railLanes = 0;
  if (vertical) {
    const lanes: Array<Array<[number, number]>> = [];
    const spans = chosen
      .map((link) => {
        const a = rank.get(link.source)!;
        const b = rank.get(link.target)!;
        return { key: keyOf(link), lo: Math.min(a, b), hi: Math.max(a, b) };
      })
      .sort((a, b) => a.hi - a.lo - (b.hi - b.lo) || a.lo - b.lo || a.key.localeCompare(b.key));
    for (const span of spans) {
      let lane = lanes.findIndex((taken) => taken.every(([lo, hi]) => span.hi <= lo || hi <= span.lo));
      if (lane < 0) lane = lanes.push([]) - 1;
      lanes[lane].push([span.lo, span.hi]);
      railLane.set(span.key, lane);
    }
    railLanes = lanes.length;
  }
  const railWidth = railLanes ? RAIL_INSET + RAIL_LANE * railLanes : 0;

  const NODE_W = vertical ? width - PAD_X * 2 - railWidth : nodeWidth(width, columns.length);
  const titleMax = Math.floor((NODE_W - 28) / CHAR_W);

  const totalSymbols = shown.reduce((sum, area) => sum + area.symbols, 0) || 1;
  const byId = new Map(shown.map((area) => [area.id, area]));
  const firstMax = (id: string) =>
    tags.get(id) ? Math.floor((NODE_W - 28 - (tags.get(id)! * TAG_W + 4)) / CHAR_W) : titleMax;
  const wrapped = new Map(
    shown.map((area) => [area.id, wrapTitle(area.title, titleMax, MAX_LINES, firstMax(area.id))]),
  );
  const heightOf = (id: string) => nodeHeight(wrapped.get(id)!.length);
  const place = (id: string, x: number, y: number, column: number): PosterNode => {
    const area = byId.get(id)!;
    return {
      ...area,
      x,
      y,
      w: NODE_W,
      h: heightOf(id),
      column,
      lines: wrapped.get(id)!,
      share: area.symbols / totalSymbols,
    };
  };

  const nodes: PosterNode[] = [];
  let height: number;
  if (vertical) {
    let y = PAD_Y;
    columns.forEach((layer, index) => {
      for (const id of layer) {
        nodes.push(place(id, PAD_X + railWidth, y, index));
        y += heightOf(id) + GAP_Y_STACKED;
      }
      y += LAYER_GAP_STACKED - GAP_Y_STACKED;
    });
    height = y - LAYER_GAP_STACKED + PAD_Y;
  } else {
    const blockHeight = (column: string[]) =>
      column.reduce((sum, id) => sum + heightOf(id), 0) + GAP_Y * Math.max(0, column.length - 1);
    height = Math.max(MIN_HEIGHT, Math.max(0, ...columns.map(blockHeight)) + PAD_Y * 2);
    const count = columns.length;
    const span = width - PAD_X * 2 - NODE_W;
    columns.forEach((column, index) => {
      const x = count === 1 ? (width - NODE_W) / 2 : PAD_X + (span * index) / (count - 1);
      let y = (height - blockHeight(column)) / 2;
      for (const id of column) {
        nodes.push(place(id, x, y, index));
        y += heightOf(id) + GAP_Y;
      }
    });
  }
  const nodeOf = new Map(nodes.map((node) => [node.id, node]));
  const lastColumn = columns.length - 1;

  // Which side of each box a link leaves and enters: facing sides between
  // columns, and for a link inside one column the side with a channel (the
  // last column has only the one to its left). On the rail, always the left.
  const sideOf = (link: PosterLink): [Side, Side] => {
    if (vertical) return ["l", "l"];
    const cs = nodeOf.get(link.source)!.column;
    const ct = nodeOf.get(link.target)!.column;
    if (cs < ct) return ["r", "l"];
    if (cs > ct) return ["l", "r"];
    return cs === lastColumn && lastColumn > 0 ? ["l", "l"] : ["r", "r"];
  };

  // Spread the ends that share a box side so links leave from distinct ports,
  // ordered by where the far end sits.
  const ports = new Map<string, Array<{ key: string; at: number }>>();
  for (const link of chosen) {
    const [ss, ts] = sideOf(link);
    const s = nodeOf.get(link.source)!;
    const t = nodeOf.get(link.target)!;
    const push = (node: PosterNode, side: Side, far: PosterNode) => {
      const slot = `${node.id}\u0000${side}`;
      const at = side === "t" || side === "b" ? far.x + far.w / 2 : far.y + far.h / 2;
      ports.set(slot, [...(ports.get(slot) || []), { key: keyOf(link), at }]);
    };
    push(s, ss, t);
    push(t, ts, s);
  }
  const port = (node: PosterNode, side: Side, key: string): Pt => {
    const list = [...(ports.get(`${node.id}\u0000${side}`) || [])].sort(
      (a, b) => a.at - b.at || a.key.localeCompare(b.key),
    );
    const index = list.findIndex((entry) => entry.key === key);
    const along = (start: number, length: number) =>
      list.length <= 1
        ? start + length / 2
        : start + 14 + ((length - 28) * index) / (list.length - 1);
    switch (side) {
      case "l":
        return { x: node.x, y: along(node.y, node.h) };
      case "r":
        return { x: node.x + node.w, y: along(node.y, node.h) };
      case "t":
        return { x: along(node.x, node.w), y: node.y };
      default:
        return { x: along(node.x, node.w), y: node.y + node.h };
    }
  };

  const ends = chosen.map((link) => {
    const [ss, ts] = sideOf(link);
    return {
      link,
      key: keyOf(link),
      p1: port(nodeOf.get(link.source)!, ss, keyOf(link)),
      p2: port(nodeOf.get(link.target)!, ts, keyOf(link)),
      ss,
    };
  });

  let routes: Pt[][];
  if (vertical) {
    const laneX = (lane: number) => PAD_X + railWidth - RAIL_INSET + 2 - lane * RAIL_LANE;
    routes = ends.map(({ key, p1, p2 }) => {
      const x = laneX(railLane.get(key)!);
      return [p1, { x, y: p1.y }, { x, y: p2.y }, p2];
    });
  } else {
    routes = routeColumns(ends, nodes, nodeOf, columns.length, width, height);
  }

  const edges: PosterEdge[] = ends.map(({ link, key }, index) => ({
    source: link.source,
    target: link.target,
    weight: link.weight,
    calls: link.calls ?? link.weight,
    d: roundedPath(routes[index]),
    mid: longestRunMid(routes[index]),
    width: strokeStep(link.weight, maxWeight),
    traced: tracedKeys.has(key),
  }));

  return { width, height, nodes, edges };
}

/**
 * Orthogonal routes between columns, like a schematic: out of the port, along
 * a lane in the channel between two columns, and into the far port. A link
 * that skips columns crosses them through a gap between their boxes. Lanes in
 * each channel are then reordered while that removes crossings.
 */
function routeColumns(
  ends: Array<{ link: PosterLink; key: string; p1: Pt; p2: Pt; ss: Side }>,
  nodes: PosterNode[],
  nodeOf: Map<string, PosterNode>,
  count: number,
  width: number,
  height: number,
): Pt[][] {
  const left: number[] = [];
  const right: number[] = [];
  for (let c = 0; c < count; c += 1) {
    const inColumn = nodes.filter((node) => node.column === c);
    left[c] = Math.min(...inColumn.map((node) => node.x));
    right[c] = Math.max(...inColumn.map((node) => node.x + node.w));
  }
  // Channel k runs between column k and k + 1; -1 and `count - 1` are the
  // margins outside the first and last columns.
  const bounds = (k: number): [number, number] =>
    k < 0 ? [2, left[0]] : k >= count - 1 ? [right[count - 1], width - 2] : [right[k], left[k + 1]];

  // The gaps a skipping link can cross: free in every column it passes.
  const gapsBetween = (from: number, to: number): Array<[number, number]> => {
    let free: Array<[number, number]> = [[6, height - 6]];
    for (let c = from + 1; c < to; c += 1) {
      for (const node of nodes.filter((n) => n.column === c)) {
        const [a, b] = [node.y - 14, node.y + node.h + 14];
        free = free.flatMap(([lo, hi]): Array<[number, number]> =>
          b <= lo || a >= hi ? [[lo, hi]] : [[lo, Math.min(hi, a)], [Math.max(lo, b), hi]],
        ).filter(([lo, hi]) => hi - lo >= 4);
      }
    }
    return free;
  };

  type Plan = { channels: number[]; pass?: number };
  const plans: Plan[] = ends.map(({ link, ss }) => {
    const cs = nodeOf.get(link.source)!.column;
    const ct = nodeOf.get(link.target)!.column;
    if (cs === ct) return { channels: [ss === "l" ? cs - 1 : cs] };
    if (Math.abs(cs - ct) === 1) return { channels: [Math.min(cs, ct)] };
    return { channels: cs < ct ? [cs, ct - 1] : [cs - 1, ct] };
  });

  // Pick each skipping link's crossing height: the free gap nearest the
  // straight line between its ends, a lane apart from links already there.
  const taken: number[] = [];
  ends.forEach(({ link, p1, p2 }, index) => {
    if (plans[index].channels.length < 2) return;
    const cs = nodeOf.get(link.source)!.column;
    const ct = nodeOf.get(link.target)!.column;
    const want = (p1.y + p2.y) / 2;
    const gaps = gapsBetween(Math.min(cs, ct), Math.max(cs, ct));
    let best = want;
    let cost = Infinity;
    for (const [lo, hi] of gaps) {
      for (let y = Math.max(lo, Math.min(hi, want)), step = 0; step < 8; step += 1) {
        const tries = [y + step * LANE_GAP, y - step * LANE_GAP].filter((v) => v >= lo && v <= hi);
        for (const v of tries) {
          if (taken.some((t) => Math.abs(t - v) < LANE_GAP - 0.5)) continue;
          const c = Math.abs(v - want);
          if (c < cost) {
            cost = c;
            best = v;
          }
        }
      }
    }
    if (!Number.isFinite(cost)) {
      // A busy gap may need shared lanes. Keep the route in a valid gap
      // rather than falling back to the midpoint through an unrelated box.
      best = gaps.map(([lo, hi]) => Math.max(lo, Math.min(hi, want)))
        .sort((a, b) => Math.abs(a - want) - Math.abs(b - want))[0];
    }
    taken.push(best);
    plans[index].pass = best;
  });

  // Lanes: the routes using a channel, ordered left to right.
  const users = new Map<number, number[]>();
  plans.forEach((plan, index) => {
    for (const k of plan.channels) users.set(k, [...(users.get(k) || []), index]);
  });
  const along = (index: number, k: number) => {
    const { p1, p2 } = ends[index];
    const plan = plans[index];
    if (plan.pass === undefined) return (p1.y + p2.y) / 2;
    return k === plan.channels[0] ? (p1.y + plan.pass) / 2 : (plan.pass + p2.y) / 2;
  };
  const orderOf = new Map<number, number[]>();
  for (const [k, list] of users) {
    orderOf.set(
      k,
      [...list].sort((a, b) => along(a, k) - along(b, k) || ends[a].key.localeCompare(ends[b].key)),
    );
  }
  const laneX = (k: number, slot: number, size: number): number => {
    const [start, end] = bounds(k);
    if (k < 0) return end - 10 - slot * LANE_GAP;
    if (k >= count - 1) return start + 10 + slot * LANE_GAP;
    const gap = size > 1 ? Math.min(LANE_GAP, (end - start - 28) / (size - 1)) : 0;
    return (start + end) / 2 + (slot - (size - 1) / 2) * gap;
  };
  const build = (): Pt[][] => {
    const x = new Map<string, number>();
    for (const [k, list] of orderOf) {
      list.forEach((index, slot) => x.set(`${index}\u0000${k}`, laneX(k, slot, list.length)));
    }
    return ends.map(({ p1, p2 }, index) => {
      const plan = plans[index];
      const xa = x.get(`${index}\u0000${plan.channels[0]}`)!;
      if (plan.pass === undefined) return [p1, { x: xa, y: p1.y }, { x: xa, y: p2.y }, p2];
      const xb = x.get(`${index}\u0000${plan.channels[1]}`)!;
      return [p1, { x: xa, y: p1.y }, { x: xa, y: plan.pass }, { x: xb, y: plan.pass }, { x: xb, y: p2.y }, p2];
    });
  };

  let routes = build();
  let best = crossings(routes);
  for (let round = 0; round < 24 && best > 0; round += 1) {
    let improved = false;
    for (const list of orderOf.values()) {
      for (let i = 0; i + 1 < list.length; i += 1) {
        [list[i], list[i + 1]] = [list[i + 1], list[i]];
        const next = build();
        const score = crossings(next);
        if (score < best) {
          best = score;
          routes = next;
          improved = true;
        } else {
          [list[i], list[i + 1]] = [list[i + 1], list[i]];
        }
      }
    }
    if (!improved) break;
  }
  return routes;
}

/**
 * The areas a traced path passes through, in order, with consecutive repeats
 * collapsed, and the hops between them.
 */
export function tracedHops(areaIds: Array<string | undefined>): {
  visits: string[];
  hops: Array<[string, string]>;
} {
  const visits: string[] = [];
  for (const id of areaIds) {
    if (id && visits[visits.length - 1] !== id) visits.push(id);
  }
  const hops: Array<[string, string]> = [];
  for (let i = 1; i < visits.length; i += 1) hops.push([visits[i - 1], visits[i]]);
  return { visits, hops };
}

export interface AtlasCell extends PosterArea {
  x: number;
  y: number;
  w: number;
  h: number;
  /** Title wrapped to the cell; empty when the cell is too small for text. */
  lines: string[];
  share: number;
  /** Title size: bigger areas get bigger names. */
  fontSize: number;
  /** Space above the title for inset journey tags. */
  titleOffset: number;
}

export interface AtlasLayout {
  width: number;
  height: number;
  cells: AtlasCell[];
}

const ATLAS_GAP = 6;

/**
 * Areas as a squarified treemap, sized by the symbols the index assigns them.
 * Used when the index recorded too few references between areas to draw
 * them as a map: size is still a fact about the code, a lone curve is not a
 * picture of it.
 */
export function layoutAtlas(
  areas: PosterArea[],
  width: number,
  tags: Map<string, number> = new Map(),
): AtlasLayout {
  const shown = areas.filter((area) => area.symbols > 0).sort(
    (a, b) => b.symbols - a.symbols || a.id.localeCompare(b.id),
  );
  // A handful of areas needs less room than a dozen; tall empty cells read
  // as a missing picture.
  const few = shown.length <= 4 ? 180 + 24 * shown.length : Infinity;
  const height = Math.round(
    width < VERTICAL_BELOW
      ? Math.max(Math.min(320, few), Math.min(640, width * 1.15, few * 1.6))
      : Math.max(Math.min(280, few), Math.min(460, width * 0.42, few)),
  );
  const total = shown.reduce((sum, area) => sum + area.symbols, 0) || 1;
  // Inset like the map's boxes so the cells line up with the heading above.
  const inner = { x: PAD_X - ATLAS_GAP / 2, w: width - (PAD_X - ATLAS_GAP / 2) * 2 };
  const scale = (inner.w * height) / total;
  const values = shown.map((area) => area.symbols * scale);

  const rects: Array<{ x: number; y: number; w: number; h: number }> = [];
  let box = { x: inner.x, y: 0, w: inner.w, h: height };
  const worst = (row: number[], side: number) => {
    const sum = row.reduce((a, b) => a + b, 0);
    const max = Math.max(...row);
    const min = Math.min(...row);
    return Math.max((side * side * max) / (sum * sum), (sum * sum) / (side * side * min));
  };
  const place = (row: number[]) => {
    const sum = row.reduce((a, b) => a + b, 0);
    if (box.w >= box.h) {
      const w = sum / box.h;
      let y = box.y;
      for (const value of row) {
        const h = value / w;
        rects.push({ x: box.x, y, w, h });
        y += h;
      }
      box = { x: box.x + w, y: box.y, w: box.w - w, h: box.h };
    } else {
      const h = sum / box.w;
      let x = box.x;
      for (const value of row) {
        const w = value / h;
        rects.push({ x, y: box.y, w, h });
        x += w;
      }
      box = { x: box.x, y: box.y + h, w: box.w, h: box.h - h };
    }
  };
  let row: number[] = [];
  for (const value of values) {
    const side = Math.min(box.w, box.h);
    if (row.length === 0 || worst([...row, value], side) <= worst(row, side)) {
      row.push(value);
    } else {
      place(row);
      row = [value];
    }
  }
  if (row.length) place(row);

  const cells = shown.map((area, index) => {
    const r = rects[index];
    const cell = {
      x: r.x + ATLAS_GAP / 2,
      y: r.y + ATLAS_GAP / 2,
      w: Math.max(0, r.w - ATLAS_GAP),
      h: Math.max(0, r.h - ATLAS_GAP),
    };
    const narrow = cell.w < 120;
    const fontSize = narrow
      ? 12
      : Math.round(Math.max(13, Math.min(22, Math.sqrt(cell.w * cell.h) / 16)));
    const lineHeight = Math.round(fontSize * 1.25);
    const titleOffset = tags.get(area.id) ? 18 : 0;
    const fits = cell.w >= 72 && cell.h >= 40 + titleOffset;
    const maxLines = Math.max(1, Math.min(3, Math.floor((cell.h - 30 - titleOffset) / lineHeight)));
    return {
      ...area,
      ...cell,
      lines: fits
        ? wrapTitle(area.title, Math.floor((cell.w - 20) / (fontSize * 0.56)), maxLines)
        : [],
      share: area.symbols / total,
      fontSize,
      titleOffset,
    };
  });
  return { width, height, cells };
}

/**
 * Whether the recorded references are dense enough to draw as a map. A map
 * needs at least three linked areas, and more of them linked than not.
 */
export function posterMode(
  areas: PosterArea[],
  links: PosterLink[],
): { mode: "network" | "atlas"; linked: Set<string> } {
  const shown = new Set(areas.filter((area) => area.symbols > 0).map((area) => area.id));
  const linked = new Set<string>();
  for (const link of links) {
    if (shown.has(link.source) && shown.has(link.target) && link.source !== link.target) {
      linked.add(link.source);
      linked.add(link.target);
    }
  }
  const isolated = shown.size - linked.size;
  return { mode: linked.size >= 3 && isolated * 2 <= linked.size ? "network" : "atlas", linked };
}

export interface ThumbLayout {
  dots: Array<{ id: string; x: number; y: number; r: number }>;
  lines: Array<{ key: string; d: string; width: number }>;
}

/**
 * A catalog card's small map: areas as dots sized by symbols, the strongest
 * recorded references as curves. Sparse indexes get a row of dots, the
 * thumbnail's version of the atlas.
 */
export function thumbLayout(
  areas: PosterArea[],
  links: PosterLink[],
  width: number,
  height: number,
): ThumbLayout {
  const shown = areas.filter((area) => area.symbols > 0);
  if (shown.length === 0) return { dots: [], lines: [] };
  const ids = shown.map((area) => area.id);
  const known = new Set(ids);
  const valid = links.filter(
    (link) => known.has(link.source) && known.has(link.target) && link.source !== link.target,
  );
  const maxSymbols = Math.max(...shown.map((area) => area.symbols));
  const symbols = new Map(shown.map((area) => [area.id, area.symbols]));
  const padX = 22;
  const padY = 16;
  const at = new Map<string, { x: number; y: number }>();
  let maxR = 9;

  if (posterMode(areas, links).mode === "atlas") {
    const row = [...shown].sort((a, b) => b.symbols - a.symbols);
    const step = row.length > 1 ? (width - padX * 2) / (row.length - 1) : 0;
    maxR = Math.min(maxR, row.length > 1 ? step / 2 - 1 : maxR);
    row.forEach((area, index) =>
      at.set(area.id, { x: row.length > 1 ? padX + step * index : width / 2, y: height / 2 }),
    );
  } else {
    const { columns } = layoutAreas(ids, valid, { maxDrawn: 12, maxColumns: 5 });
    const tallest = Math.max(...columns.map((column) => column.length));
    if (tallest > 1) maxR = Math.min(maxR, (height - padY * 2) / (tallest - 1) / 2 - 1);
    columns.forEach((column, i) => {
      const x = columns.length === 1 ? width / 2 : padX + ((width - padX * 2) * i) / (columns.length - 1);
      // A long column zig-zags so its dots and curves don't read as a comb.
      const stagger = column.length > 4 ? Math.min(12, (width - padX * 2) / columns.length / 4) : 0;
      column.forEach((id, j) => {
        const y = column.length === 1 ? height / 2 : padY + ((height - padY * 2) * j) / (column.length - 1);
        const nudge = i === 0 ? stagger * (j % 2) : i === columns.length - 1 ? -stagger * (j % 2) : stagger * (j % 2 ? 1 : -1);
        at.set(id, { x: x + nudge, y });
      });
    });
  }

  const dots = ids.map((id) => ({
    id,
    ...at.get(id)!,
    r: Math.max(2, Math.min(maxR, 2 + (maxR - 2) * Math.sqrt(symbols.get(id)! / maxSymbols))),
  }));
  const chosen = [...valid].sort((a, b) => b.weight - a.weight).slice(0, 12);
  const heaviest = Math.max(1, ...chosen.map((link) => link.weight));
  const lines = chosen.map((link) => {
    const a = at.get(link.source)!;
    const b = at.get(link.target)!;
    const dx = Math.max(12, Math.abs(b.x - a.x) * 0.5);
    const d =
      Math.abs(a.x - b.x) < 1
        ? `M ${a.x} ${a.y} C ${a.x + 28} ${a.y}, ${b.x + 28} ${b.y}, ${b.x} ${b.y}`
        : `M ${a.x} ${a.y} C ${a.x + Math.sign(b.x - a.x) * dx} ${a.y}, ${b.x - Math.sign(b.x - a.x) * dx} ${b.y}, ${b.x} ${b.y}`;
    return {
      key: `${link.source}\u0000${link.target}`,
      d,
      width: 0.6 + 1.8 * (Math.log(1 + link.weight) / Math.log(1 + heaviest)),
    };
  });
  return { dots, lines };
}
