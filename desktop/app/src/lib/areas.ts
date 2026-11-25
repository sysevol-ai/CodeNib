// Code areas and their hues. With a written outline, the areas are the
// Wiki map's (one per top-level page, in outline order); a Wiki built from
// the index alone has none, so its module pages stand in. Five hues cycle
// (tokens.css --area-1..5); everything else is the neutral grey. Blue never
// marks an area: it means only "needs you".

import type { PageRef } from "./model";

export interface MapArea {
  id: string;
  title: string;
  symbols?: number;
  files?: number;
}

export function areaPages(pages: PageRef[], mapAreas: MapArea[] | null): string[] {
  if (mapAreas && mapAreas.length) return mapAreas.map((a) => a.id);
  const modules: string[] = [];
  const walk = (list: PageRef[]) => {
    for (const p of list) {
      if (p.id.startsWith("mod__")) modules.push(p.id);
      walk(p.children ?? []);
    }
  };
  walk(pages);
  return modules;
}

export function hueFor(pageId: string, areas: string[]): string {
  const index = areas.indexOf(pageId);
  return index < 0 ? "var(--area-none)" : `var(--area-${(index % 5) + 1})`;
}

// ---- Areas a Wiki without an outline gets from its own file graph ----------

export interface FileNode {
  id: string;
  file: string;
  kind?: string;
  external?: boolean;
  symbol_count?: number;
  importance?: number;
  ref_count?: number;
}

export interface FileEdge {
  source: string;
  target: string;
  call_sites?: number;
  weight?: number;
}

export interface FileArea {
  id: string;
  title: string;
  files: string[];
  symbols: number;
  /** 1–5 for the five hues, 0 for the neutral grey. */
  slot: number;
}

export interface FileAreas {
  areas: FileArea[];
  byFile: Record<string, string>;
}

export function slotHue(slot: number): string {
  return slot > 0 ? `var(--area-${slot})` : "var(--area-none)";
}

const folderOf = (file: string) => file.split("/").slice(0, -1).join("/");
const baseOf = (file: string) => file.split("/").pop() ?? file;

/** Greedy modularity merging with fixed order and tie-breaks: the same
 *  graph always gives the same groups. */
function communities(nodes: FileNode[], edges: FileEdge[]): number[][] {
  const index = new Map(nodes.map((n, i) => [n.id, i]));
  const n = nodes.length;
  const weight: number[][] = Array.from({ length: n }, () => new Array<number>(n).fill(0));
  for (const e of edges) {
    const a = index.get(e.source);
    const b = index.get(e.target);
    if (a === undefined || b === undefined || a === b) continue;
    const w = e.call_sites ?? e.weight ?? 1;
    weight[a][b] += w;
    weight[b][a] += w;
  }
  const total = weight.reduce((sum, row) => sum + row.reduce((s, w) => s + w, 0), 0);
  let groups = nodes.map((_, i) => [i]);
  if (total === 0) return groups;
  const degree = (g: number[]) => g.reduce((s, i) => s + weight[i].reduce((t, w) => t + w, 0), 0);
  const between = (a: number[], b: number[]) => a.reduce((s, i) => s + b.reduce((t, j) => t + weight[i][j], 0), 0);
  for (;;) {
    let best = { gain: 1e-12, x: -1, y: -1 };
    for (let x = 0; x < groups.length; x++) {
      for (let y = x + 1; y < groups.length; y++) {
        const e = between(groups[x], groups[y]);
        if (e <= 0) continue;
        const gain = 2 * (e / total - (degree(groups[x]) * degree(groups[y])) / (total * total));
        if (gain > best.gain + 1e-12) best = { gain, x, y };
      }
    }
    if (best.x < 0) break;
    const merged = [...groups[best.x], ...groups[best.y]].sort((a, b) => a - b);
    groups = groups.filter((_, i) => i !== best.x && i !== best.y);
    groups.push(merged);
    groups.sort((a, b) => a[0] - b[0]);
  }
  return groups;
}

/** Group a repository's files into areas: by subpackage when the folders
 *  split them, otherwise by who calls whom. Each area is named after its
 *  folder or its most central file; the five largest get the five hues. */
export function deriveFileAreas(allNodes: FileNode[], edges: FileEdge[]): FileAreas {
  const nodes = allNodes
    .filter((n) => !n.external && (n.kind ?? "file") === "file")
    .sort((a, b) => (a.file < b.file ? -1 : a.file > b.file ? 1 : 0));
  if (!nodes.length) return { areas: [], byFile: {} };
  const byFolder = new Map<string, FileNode[]>();
  for (const n of nodes) byFolder.set(folderOf(n.file), [...(byFolder.get(folderOf(n.file)) ?? []), n]);
  const folders = [...byFolder.values()];
  const useFolders =
    folders.filter((g) => g.length >= 2).length >= 2 && Math.max(...folders.map((g) => g.length)) <= 0.7 * nodes.length;
  const groups: { nodes: FileNode[]; title: string }[] = useFolders
    ? [...byFolder.entries()].map(([folder, members]) => ({
        nodes: members,
        title: folder.split("/").slice(-2).join("/") || "top level",
      }))
    : communities(nodes, edges).map((members) => {
        const group = members.map((i) => nodes[i]);
        const central = [...group].sort(
          (a, b) =>
            (b.importance ?? 0) - (a.importance ?? 0) ||
            (b.ref_count ?? 0) - (a.ref_count ?? 0) ||
            (b.symbol_count ?? 0) - (a.symbol_count ?? 0) ||
            (a.file < b.file ? -1 : 1),
        )[0];
        return { nodes: group, title: baseOf(central.file) };
      });
  const sized = groups
    .map((g) => ({ ...g, symbols: g.nodes.reduce((s, n) => s + (n.symbol_count ?? 0), 0) }))
    .sort((a, b) => b.symbols - a.symbols || (a.title < b.title ? -1 : 1));
  const areas: FileArea[] = [];
  const byFile: Record<string, string> = {};
  sized.forEach((g, i) => {
    // Five hues; any further groups share one neutral area.
    const slot = i < 5 ? i + 1 : 0;
    const id = slot ? `area-${slot}` : "area-other";
    let area = areas.find((a) => a.id === id);
    if (!area) {
      area = { id, title: slot ? g.title : "Other files", files: [], symbols: 0, slot };
      areas.push(area);
    }
    area.files.push(...g.nodes.map((n) => n.file));
    area.symbols += g.symbols;
    for (const n of g.nodes) byFile[n.file] = id;
  });
  return { areas, byFile };
}
