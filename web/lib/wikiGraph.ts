// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import type { WikiAreaLink, WikiAreaMap, WikiPageRef } from "@/lib/api";

/** Fixed order, validated on the map's dark surface; never cycled. */
export const AREA_COLORS = [
  "#3987e5",
  "#d95926",
  "#199e70",
  "#c98500",
  "#d55181",
  "#008300",
  "#9085e9",
  "#e66767",
] as const;
/** The Overview's dot in the page list, and every area past the eighth. */
export const NEUTRAL_COLOR = "#80868b";

export interface WikiGraphNode {
  id: string;
  title: string;
  kind: "area" | "page";
  /** The top-level page this one belongs to. */
  areaId: string;
  color: string;
  radius: number;
  x: number;
  y: number;
}

export interface WikiGraphEdge {
  id: string;
  source: string;
  target: string;
  /** `references` comes from the code index; `contains` is only the page outline. */
  kind: "references" | "contains";
  link?: WikiAreaLink;
}

export interface WikiGraph {
  nodes: WikiGraphNode[];
  edges: WikiGraphEdge[];
  colors: Map<string, string>;
}

const PAGE_RADIUS: Record<string, number> = { primary: 6.5, key: 6, more: 4.5, detail: 4.5 };

/**
 * Pages become nodes; references recorded between areas become the heavy edges.
 * The index records references, so these include calls, field reads and type uses.
 * The Overview is not a node: the whole map already is the overview.
 */
export function buildWikiGraph(pages: WikiPageRef[], map: WikiAreaMap | null): WikiGraph {
  const symbols = new Map((map?.available ? map.areas : []).map((area) => [area.id, area.symbols]));
  const mostSymbols = Math.max(1, ...symbols.values());
  const nodes: WikiGraphNode[] = [];
  const edges: WikiGraphEdge[] = [];
  const colors = new Map<string, string>();
  let colorSlot = 0;

  const addChildren = (parent: WikiPageRef, areaId: string, color: string) => {
    for (const page of parent.children) {
      colors.set(page.id, color);
      nodes.push({
        id: page.id,
        title: page.title,
        kind: "page",
        areaId,
        color,
        radius: PAGE_RADIUS[page.tier ?? "detail"] ?? 4.5,
        x: 0,
        y: 0,
      });
      edges.push({ id: `${parent.id}>${page.id}`, source: parent.id, target: page.id, kind: "contains" });
      addChildren(page, areaId, color);
    }
  };

  for (const page of pages) {
    if (page.id === "overview") {
      colors.set(page.id, NEUTRAL_COLOR);
      continue;
    }
    const color = AREA_COLORS[colorSlot++] ?? NEUTRAL_COLOR;
    colors.set(page.id, color);
    const share = Math.sqrt((symbols.get(page.id) ?? mostSymbols / 3) / mostSymbols);
    nodes.push({
      id: page.id,
      title: page.title,
      kind: "area",
      areaId: page.id,
      color,
      radius: 9 + 10 * share,
      x: 0,
      y: 0,
    });
    addChildren(page, page.id, color);
  }

  const known = new Set(nodes.map((node) => node.id));
  if (map?.available) {
    for (const link of map.links) {
      if (!known.has(link.source) || !known.has(link.target) || link.source === link.target) continue;
      edges.push({
        id: `${link.source}~${link.target}`,
        source: link.source,
        target: link.target,
        kind: "references",
        link,
      });
    }
  }

  layoutWikiGraph(nodes, edges);
  return { nodes, edges, colors };
}

/**
 * A small force layout with a fixed starting arrangement and no randomness, so
 * a repository draws the same shape on every visit and in every test.
 */
export function layoutWikiGraph(nodes: WikiGraphNode[], edges: WikiGraphEdge[], iterations = 360): void {
  const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));
  const position = new Map<string, WikiGraphNode>();
  const areas = nodes.filter((node) => node.kind === "area");
  for (const node of nodes) {
    position.set(node.id, node);
    if (node.kind === "area") {
      const angle = areas.indexOf(node) * GOLDEN_ANGLE;
      node.x = 220 * Math.cos(angle);
      node.y = 220 * Math.sin(angle);
    }
  }
  nodes.forEach((node, index) => {
    if (node.kind !== "page") return;
    const parent = position.get(node.areaId);
    const angle = index * GOLDEN_ANGLE;
    node.x = (parent?.x ?? 0) * 1.4 + 60 * Math.cos(angle);
    node.y = (parent?.y ?? 0) * 1.4 + 60 * Math.sin(angle);
  });

  const springs = edges.flatMap((edge) => {
    const a = position.get(edge.source);
    const b = position.get(edge.target);
    if (!a || !b) return [];
    return edge.kind === "references"
      ? [{ a, b, length: 190, strength: 0.012 }]
      : [{ a, b, length: 58, strength: 0.09 }];
  });

  for (let step = 0; step < iterations; step += 1) {
    const heat = 1 - step / iterations;
    const force = new Map(nodes.map((node) => [node.id, { x: 0, y: 0 }]));
    for (let i = 0; i < nodes.length; i += 1) {
      const a = nodes[i];
      const fa = force.get(a.id)!;
      fa.x -= a.x * 0.006;
      fa.y -= a.y * 0.006;
      for (let j = i + 1; j < nodes.length; j += 1) {
        const b = nodes[j];
        const dx = a.x - b.x || 0.01;
        const dy = a.y - b.y || 0.01;
        const distance2 = Math.max(dx * dx + dy * dy, 25);
        const push = 2600 / distance2;
        const distance = Math.sqrt(distance2);
        const fb = force.get(b.id)!;
        fa.x += (dx / distance) * push;
        fa.y += (dy / distance) * push;
        fb.x -= (dx / distance) * push;
        fb.y -= (dy / distance) * push;
      }
    }
    for (const { a, b, length, strength } of springs) {
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const distance = Math.max(Math.hypot(dx, dy), 1);
      const pull = (distance - length) * strength;
      const fa = force.get(a.id)!;
      const fb = force.get(b.id)!;
      fa.x += (dx / distance) * pull;
      fa.y += (dy / distance) * pull;
      fb.x -= (dx / distance) * pull;
      fb.y -= (dy / distance) * pull;
    }
    for (const node of nodes) {
      const f = force.get(node.id)!;
      node.x += Math.max(-14, Math.min(14, f.x)) * (0.25 + 0.75 * heat);
      node.y += Math.max(-14, Math.min(14, f.y)) * (0.25 + 0.75 * heat);
    }
  }
}

/** The active page plus everything one edge away from it. */
export function neighborhood(graph: WikiGraph, id: string | null): Set<string> {
  const near = new Set<string>();
  if (!id) return near;
  near.add(id);
  for (const edge of graph.edges) {
    if (edge.source === id) near.add(edge.target);
    if (edge.target === id) near.add(edge.source);
  }
  return near;
}
