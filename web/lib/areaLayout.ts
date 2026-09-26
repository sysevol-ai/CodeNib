// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * Layered layout for the wiki's area map.
 *
 * Areas that call into others sit left of the areas they call, so every drawn
 * arrow points right. Only the strongest links are drawn; of two areas that
 * call each other, the weaker direction yields, and any remaining cycle loses
 * its weakest edge. Dropped links stay listed on each area's card.
 */

export interface AreaLink {
  source: string;
  target: string;
  weight: number;
}

export interface AreaLayout {
  /** Area ids per column, left to right. */
  columns: string[][];
  /** Links drawn as arrows; each goes from a lower column to a higher one. */
  drawn: AreaLink[];
}

export function layoutAreas(
  areaIds: string[],
  links: AreaLink[],
  { maxDrawn = 8, maxColumns = 3 }: { maxDrawn?: number; maxColumns?: number } = {},
): AreaLayout {
  const known = new Set(areaIds);
  const byPair = new Map<string, AreaLink>();
  for (const link of links) {
    if (!known.has(link.source) || !known.has(link.target) || link.source === link.target) continue;
    byPair.set(`${link.source}\u0000${link.target}`, link);
  }
  // Of a mutual pair keep the stronger direction (ties: declared order).
  const candidates = [...byPair.values()].filter((link) => {
    const back = byPair.get(`${link.target}\u0000${link.source}`);
    if (!back) return true;
    if (back.weight !== link.weight) return link.weight > back.weight;
    return areaIds.indexOf(link.source) < areaIds.indexOf(link.target);
  });
  candidates.sort(
    (a, b) =>
      b.weight - a.weight ||
      areaIds.indexOf(a.source) - areaIds.indexOf(b.source) ||
      areaIds.indexOf(a.target) - areaIds.indexOf(b.target),
  );

  // Add strongest first, skipping any edge that would close a cycle.
  const out = new Map<string, string[]>();
  const reaches = (from: string, to: string): boolean => {
    const stack = [from];
    const seen = new Set<string>();
    while (stack.length) {
      const node = stack.pop()!;
      if (node === to) return true;
      if (seen.has(node)) continue;
      seen.add(node);
      stack.push(...(out.get(node) || []));
    }
    return false;
  };
  const drawn: AreaLink[] = [];
  for (const link of candidates) {
    if (drawn.length >= maxDrawn) break;
    if (reaches(link.target, link.source)) continue;
    drawn.push(link);
    out.set(link.source, [...(out.get(link.source) || []), link.target]);
  }

  // Longest-path layering over the drawn DAG.
  const layer = new Map<string, number>(areaIds.map((id) => [id, 0]));
  for (let pass = 0; pass < areaIds.length; pass += 1) {
    let changed = false;
    for (const link of drawn) {
      const next = (layer.get(link.source) || 0) + 1;
      if (next > (layer.get(link.target) || 0)) {
        layer.set(link.target, next);
        changed = true;
      }
    }
    if (!changed) break;
  }
  const depth = Math.max(0, ...layer.values());
  const columns = Math.max(1, Math.min(maxColumns, depth + 1));
  // Squeeze deep layers into the available columns, keeping order monotone.
  const column = (id: string) =>
    depth === 0 ? 0 : Math.round(((layer.get(id) || 0) * (columns - 1)) / depth);
  const grid: string[][] = Array.from({ length: columns }, () => []);
  const linked = new Set(drawn.flatMap((link) => [link.source, link.target]));
  for (const id of areaIds) {
    if (linked.has(id)) grid[column(id)].push(id);
  }
  // Areas with no drawn link sit at the foot of the last column.
  for (const id of areaIds) {
    if (!linked.has(id)) grid[columns - 1].push(id);
  }
  // Squeezing can put both ends of an edge in one column; those stop being
  // arrows and remain on the cards.
  const keep = drawn.filter((link) => column(link.source) < column(link.target));
  const columnsOut = grid.filter((ids) => ids.length > 0);
  orderColumns(columnsOut, keep, linked);
  return { columns: columnsOut, drawn: keep };
}

/**
 * Reorder each column by the mean position of its neighbours (barycentre
 * sweeps, left to right then right to left) so arrows cross less. Areas with
 * no drawn link keep their place at the foot of their column.
 */
function orderColumns(columns: string[][], drawn: AreaLink[], linked: Set<string>) {
  const position = () => {
    const at = new Map<string, number>();
    for (const ids of columns) ids.forEach((id, index) => at.set(id, index / Math.max(1, ids.length - 1)));
    return at;
  };
  const sweep = (index: number, neighbours: (id: string) => string[]) => {
    const at = position();
    const ids = columns[index];
    const score = new Map<string, number>();
    ids.forEach((id, i) => {
      const near = neighbours(id).filter((n) => at.has(n));
      score.set(
        id,
        near.length ? near.reduce((sum, n) => sum + at.get(n)!, 0) / near.length : at.get(id) ?? i,
      );
    });
    const linkedIds = ids.filter((id) => linked.has(id));
    const rest = ids.filter((id) => !linked.has(id));
    linkedIds.sort((a, b) => score.get(a)! - score.get(b)! || ids.indexOf(a) - ids.indexOf(b));
    columns[index] = [...linkedIds, ...rest];
  };
  const sources = (id: string) => drawn.filter((l) => l.target === id).map((l) => l.source);
  const targets = (id: string) => drawn.filter((l) => l.source === id).map((l) => l.target);
  for (let pass = 0; pass < 2; pass += 1) {
    for (let i = 1; i < columns.length; i += 1) sweep(i, sources);
    for (let i = columns.length - 2; i >= 0; i -= 1) sweep(i, targets);
  }
}
