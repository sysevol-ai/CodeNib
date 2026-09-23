// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * Reading the section flows the Wiki generator writes as Mermaid.
 *
 * Most flows are a straight call chain (A calls B calls C). Drawn by Mermaid
 * that becomes a thousand-pixel row of grey boxes the reader has to scroll
 * sideways; as an ordered list it reads top to bottom and keeps the call
 * sites. Only a flow that branches earns a drawn diagram.
 */

import type { WikiRelationItem } from "./api";

export interface FlowNode {
  id: string;
  label: string;
}

export interface FlowEdge {
  from: string;
  to: string;
  label: string;
}

export interface Flowchart {
  nodes: FlowNode[];
  edges: FlowEdge[];
}

const HEADER = /^\s*(?:flowchart|graph)\s+(?:LR|RL|TD|TB|BT)\s*$/;
const NODE = /^\s*([A-Za-z_][\w-]*)\s*\[\s*"([^"]*)"\s*\]\s*$/;
const EDGE = /^\s*([A-Za-z_][\w-]*)\s*-->\s*(?:\|([^|]*)\|\s*)?([A-Za-z_][\w-]*)\s*$/;

/** Parse the generator's flowchart subset; anything else returns null. */
export function parseFlowchart(text: string): Flowchart | null {
  const lines = text.split("\n").filter((line) => line.trim());
  if (!lines.length || !HEADER.test(lines[0])) return null;
  const nodes = new Map<string, FlowNode>();
  const edges: FlowEdge[] = [];
  for (const line of lines.slice(1)) {
    const node = NODE.exec(line);
    if (node) {
      nodes.set(node[1], { id: node[1], label: node[2].trim() });
      continue;
    }
    const edge = EDGE.exec(line);
    if (edge) {
      edges.push({ from: edge[1], to: edge[3], label: (edge[2] || "").trim() });
      continue;
    }
    return null; // styling, subgraphs, other shapes: leave it to Mermaid
  }
  if (!edges.length) return null;
  if (edges.some((e) => !nodes.has(e.from) || !nodes.has(e.to))) return null;
  return { nodes: [...nodes.values()], edges };
}

export interface ChainStep {
  node: FlowNode;
  /** The call leading out of this node; absent on the last step. */
  next?: FlowEdge;
}

/**
 * Order a flow as one or more straight paths, or return null when any part
 * branches or loops. Disconnected pieces are kept apart: the generator draws
 * two pairs when it spells one symbol two ways, and those read as two paths.
 */
export function callPaths(flow: Flowchart): ChainStep[][] | null {
  const out = new Map<string, FlowEdge>();
  const indegree = new Map<string, number>();
  for (const edge of flow.edges) {
    if (out.has(edge.from)) return null;
    out.set(edge.from, edge);
    indegree.set(edge.to, (indegree.get(edge.to) || 0) + 1);
  }
  if ([...indegree.values()].some((count) => count > 1)) return null;
  const used = new Set([...out.keys(), ...indegree.keys()]);
  const byId = new Map(flow.nodes.map((n) => [n.id, n]));
  const seen = new Set<string>();
  const paths: ChainStep[][] = [];
  // Keep the order the flow declares its nodes in.
  for (const node of flow.nodes) {
    if (!used.has(node.id) || indegree.has(node.id)) continue;
    const steps: ChainStep[] = [];
    let cursor: string | undefined = node.id;
    while (cursor && !seen.has(cursor)) {
      seen.add(cursor);
      const next = out.get(cursor);
      steps.push({ node: byId.get(cursor)!, next });
      cursor = next?.to;
    }
    paths.push(steps);
  }
  // Nodes never reached from a start sit on a cycle.
  return seen.size === used.size && paths.length ? paths : null;
}

/** The single path of a flow, or null when it branches, loops, or splits. */
export function straightChain(flow: Flowchart): ChainStep[] | null {
  const paths = callPaths(flow);
  return paths && paths.length === 1 ? paths[0] : null;
}

function symbolMatches(qualified: string, label: string): boolean {
  const bare = label.replace(/\(\)$/, "");
  const name = qualified.replace(/\(\)$/, "");
  return name === bare || name.endsWith(":" + bare);
}

/** The recorded call site for an arrow, when the page's relations hold it. */
export function edgeCallSite(
  from: string,
  to: string,
  relations: WikiRelationItem[] | undefined,
): string | null {
  const rel = relations?.find(
    (r) => symbolMatches(r.source, from) && symbolMatches(r.target, to),
  );
  return rel?.anchors?.[0] || null;
}
