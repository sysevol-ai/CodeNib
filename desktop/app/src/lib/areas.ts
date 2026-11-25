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
