// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import type { WikiAreaMap } from "@/lib/api";
import { buildWikiGraph } from "@/lib/wikiGraph";

export const THUMB_WIDTH = 320;
export const THUMB_HEIGHT = 132;
const W = THUMB_WIDTH;
const H = THUMB_HEIGHT;
const PAD_X = 30;
const PAD_Y = 22;

export interface MapThumbLayout {
  dots: Array<{ id: string; x: number; y: number; r: number; color: string }>;
  lines: Array<{ key: string; d: string; width: number; strong: boolean }>;
}

/** The full map's own layout, fitted to the card: the same positions, colours
 *  and relative sizes the reader will find after opening the Wiki. */
export function mapThumb(map: WikiAreaMap): MapThumbLayout | null {
  if (map.areas.length === 0) return null;
  // Areas with no references between them still draw, as dots without lines.
  const graph = buildWikiGraph(
    map.areas.map((area) => ({ id: area.id, title: area.title, children: [] })),
    { ...map, available: true },
  );
  const xs = graph.nodes.map((node) => node.x);
  const ys = graph.nodes.map((node) => node.y);
  const [minX, maxX, minY, maxY] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const fitX = (W - 2 * PAD_X) / Math.max(maxX - minX, 1);
  const fitY = (H - 2 * PAD_Y) / Math.max(maxY - minY, 1);
  const scale = Math.min(fitX, fitY);
  // Stretched a little toward the card's shape, as the full map is toward its pane.
  const sx = Math.min(fitX, scale * 1.9);
  const sy = Math.min(fitY, scale * 1.9);
  const size = Math.min(1, Math.max(0.42, scale * 1.1));
  const at = new Map(graph.nodes.map((node) => [node.id, {
    x: W / 2 + (node.x - (minX + maxX) / 2) * sx,
    y: H / 2 + (node.y - (minY + maxY) / 2) * sy,
    r: Math.max(3.2, node.radius * size),
  }]));
  const references = graph.edges.filter((edge) => edge.kind === "references");
  const heaviest = Math.max(1, ...references.map((edge) => edge.link?.weight ?? 1));
  const lines = references.map((edge) => {
    const a = at.get(edge.source)!;
    const b = at.get(edge.target)!;
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const length = Math.max(Math.hypot(dx, dy), 1);
    const [ux, uy] = [dx / length, dy / length];
    const [x1, y1, x2, y2] = [a.x + ux * a.r, a.y + uy * a.r, b.x - ux * b.r, b.y - uy * b.r];
    const bend = Math.min(length * 0.12, 14);
    const share = Math.log10(1 + (edge.link?.weight ?? 1)) / Math.log10(1 + heaviest);
    return {
      key: edge.id,
      d: `M${x1} ${y1}Q${(x1 + x2) / 2 - uy * bend} ${(y1 + y2) / 2 + ux * bend} ${x2} ${y2}`,
      width: 0.6 + share * 1.3,
      strong: share > 0.55,
    };
  });
  return {
    dots: graph.nodes.map((node) => ({ id: node.id, color: node.color, ...at.get(node.id)! })),
    lines,
  };
}
