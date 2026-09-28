// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * Geometry for the Overview poster: the wiki's areas as boxes and the
 * recorded references between them as curves, in a fixed virtual canvas the
 * SVG scales to fit.
 *
 * Columns come from `layoutAreas`, so an area sits left of the areas it
 * mostly references. Unlike the compact area map, the poster also draws the
 * weaker direction of a mutual pair and edges that run backwards; every drawn
 * curve is one recorded link, bent so that neither hides the other.
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
  /** Share of all mapped symbols, 0..1, for the size meter. */
  share: number;
}

export interface PosterEdge {
  source: string;
  target: string;
  weight: number;
  calls: number;
  /** SVG path data. */
  d: string;
  /** Point halfway along the curve, for a label or hit target. */
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
// Phone-width canvases stack layers top to bottom.
const VERTICAL_BELOW = 600;
const GAP_X_STACKED = 14;
const GAP_Y_STACKED = 16;
const LAYER_GAP_STACKED = 56;
// Average advance of Geist at 14px/600, used to wrap titles without layout.
const CHAR_W = 7.6;

/** Box width for `count` columns across `width`, keeping room for curves. */
export function nodeWidth(width: number, count: number): number {
  const room = (width - PAD_X * 2 - MIN_GAP_X * Math.max(0, count - 1)) / Math.max(1, count);
  return Math.max(150, Math.min(220, Math.floor(room)));
}

export function wrapTitle(title: string, max = 23, maxLines = MAX_LINES): string[] {
  const words = title.split(/\s+/).filter(Boolean);
  const lines: string[] = [];
  let line = "";
  for (const word of words) {
    const next = line ? `${line} ${word}` : word;
    if (next.length <= max || !line) {
      line = next;
    } else {
      lines.push(line);
      line = word;
    }
  }
  if (line) lines.push(line);
  const clip = (l: string) => (l.length > max ? `${l.slice(0, max - 1).trimEnd()}…` : l);
  if (lines.length <= maxLines) return lines.map(clip);
  const last = lines.slice(maxLines - 1).join(" ");
  return [...lines.slice(0, maxLines - 1).map(clip), clip(last)];
}

function nodeHeight(lines: number): number {
  // padding + title lines + meta line + meter
  return 14 + lines * LINE_H + 6 + 16 + 12 + 3 + 12;
}

export function layoutPoster(
  areas: PosterArea[],
  links: PosterLink[],
  {
    traced = [],
    maxEdges = 14,
    maxColumns = 4,
    width = POSTER_WIDTH,
  }: {
    traced?: Array<[string, string]>;
    maxEdges?: number;
    maxColumns?: number;
    width?: number;
  } = {},
): PosterLayout {
  // An area with nothing mapped to it is an outline entry, not a subsystem.
  const shown = areas.filter((area) => area.symbols > 0);
  const ids = shown.map((area) => area.id);
  const known = new Set(ids);
  const valid = links.filter(
    (link) => known.has(link.source) && known.has(link.target) && link.source !== link.target,
  );
  // A phone-width canvas stacks the layers top to bottom instead of squeezing
  // columns side by side; the flow then reads downwards.
  const vertical = width < VERTICAL_BELOW;
  const fit = vertical
    ? maxColumns
    : Math.max(1, Math.floor((width - PAD_X * 2 + MIN_GAP_X) / (150 + MIN_GAP_X)));
  const { columns } = layoutAreas(ids, valid, {
    maxDrawn: maxEdges,
    maxColumns: Math.min(maxColumns, fit),
  });
  const perRow = vertical ? (width >= 340 ? 2 : 1) : 1;
  const NODE_W = vertical
    ? Math.floor((width - PAD_X * 2 - GAP_X_STACKED * (perRow - 1)) / perRow)
    : nodeWidth(width, columns.length);
  const titleMax = Math.floor((NODE_W - 28) / CHAR_W);

  const totalSymbols = shown.reduce((sum, area) => sum + area.symbols, 0) || 1;
  const byId = new Map(shown.map((area) => [area.id, area]));
  const wrapped = new Map(shown.map((area) => [area.id, wrapTitle(area.title, titleMax)]));
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
      for (let start = 0; start < layer.length; start += perRow) {
        const row = layer.slice(start, start + perRow);
        const rowW = row.length * NODE_W + (row.length - 1) * GAP_X_STACKED;
        let x = (width - rowW) / 2;
        for (const id of row) {
          nodes.push(place(id, x, y, index));
          x += NODE_W + GAP_X_STACKED;
        }
        y += Math.max(...row.map(heightOf)) + (start + perRow < layer.length ? GAP_Y_STACKED : 0);
      }
      y += LAYER_GAP_STACKED;
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

  // The strongest links, plus every hop of the traced path whatever its weight.
  const tracedKeys = new Set(traced.map(([s, t]) => `${s}\u0000${t}`));
  const sorted = [...valid].sort(
    (a, b) => b.weight - a.weight || a.source.localeCompare(b.source) || a.target.localeCompare(b.target),
  );
  const chosen = sorted.filter(
    (link, index) => index < maxEdges || tracedKeys.has(`${link.source}\u0000${link.target}`),
  );
  const maxWeight = Math.max(1, ...chosen.map((link) => link.weight));

  // Which side of each box an edge leaves and enters: facing sides between
  // layers, the outer side for a link inside one layer.
  type Side = "l" | "r" | "t" | "b";
  const sideOf = (link: PosterLink): [Side, Side] => {
    const cs = nodeOf.get(link.source)!.column;
    const ct = nodeOf.get(link.target)!.column;
    if (vertical) {
      if (cs < ct) return ["b", "t"];
      if (cs > ct) return ["t", "b"];
      return ["r", "r"];
    }
    if (cs < ct) return ["r", "l"];
    if (cs > ct) return ["l", "r"];
    return ["r", "r"];
  };
  const keyOf = (link: PosterLink) => `${link.source}\u0000${link.target}`;

  // Spread the ends that share a node side so curves leave from distinct
  // ports instead of one point, ordered by where the far end sits.
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
  const port = (node: PosterNode, side: Side, key: string): { x: number; y: number } => {
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

  const edges: PosterEdge[] = chosen.map((link) => {
    const s = nodeOf.get(link.source)!;
    const t = nodeOf.get(link.target)!;
    const [ss, ts] = sideOf(link);
    const key = keyOf(link);
    const { x: x1, y: y1 } = port(s, ss, key);
    const { x: x2, y: y2 } = port(t, ts, key);
    let d: string;
    let mid: { x: number; y: number };
    if (ss === "r" && ts === "r") {
      // Inside one layer: a loop out to the right and back.
      const bulge = 46 + Math.min(60, Math.abs(y2 - y1) * 0.25);
      d = `M ${x1} ${y1} C ${x1 + bulge} ${y1}, ${x2 + bulge} ${y2}, ${x2} ${y2}`;
      mid = { x: Math.max(x1, x2) + bulge * 0.75, y: (y1 + y2) / 2 };
    } else if (vertical) {
      const dy = Math.max(30, Math.abs(y2 - y1) * 0.5);
      const c1 = ss === "b" ? y1 + dy : y1 - dy;
      const c2 = ts === "t" ? y2 - dy : y2 + dy;
      d = `M ${x1} ${y1} C ${x1} ${c1}, ${x2} ${c2}, ${x2} ${y2}`;
      mid = { x: (x1 + x2) / 2, y: (y1 + y2) / 2 };
    } else {
      const dx = Math.max(40, Math.abs(x2 - x1) * 0.5);
      const c1 = ss === "r" ? x1 + dx : x1 - dx;
      const c2 = ts === "l" ? x2 - dx : x2 + dx;
      d = `M ${x1} ${y1} C ${c1} ${y1}, ${c2} ${y2}, ${x2} ${y2}`;
      mid = { x: (x1 + x2) / 2, y: (y1 + y2) / 2 };
    }
    const ratio = Math.log(1 + link.weight) / Math.log(1 + maxWeight);
    return {
      source: link.source,
      target: link.target,
      weight: link.weight,
      calls: link.calls ?? link.weight,
      d,
      mid,
      width: 1.2 + 3.6 * ratio,
      traced: tracedKeys.has(key),
    };
  });

  return { width, height, nodes, edges };
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
export function layoutAtlas(areas: PosterArea[], width: number): AtlasLayout {
  const shown = areas.filter((area) => area.symbols > 0).sort(
    (a, b) => b.symbols - a.symbols || a.id.localeCompare(b.id),
  );
  const height = Math.round(
    width < VERTICAL_BELOW
      ? Math.max(320, Math.min(640, width * 1.15))
      : Math.max(280, Math.min(460, width * 0.42)),
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
    const fits = cell.w >= 72 && cell.h >= 40;
    const maxLines = Math.max(1, Math.min(3, Math.floor((cell.h - 30) / lineHeight)));
    return {
      ...area,
      ...cell,
      lines: fits
        ? wrapTitle(area.title, Math.floor((cell.w - 20) / (fontSize * 0.56)), maxLines)
        : [],
      share: area.symbols / total,
      fontSize,
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
