// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import type { WikiAreaMap, WikiPageRef } from "@/lib/api";
import type { MapRoute } from "@/lib/mapRoute";
import { displaySymbol } from "@/lib/symbols";
import type { FreeMapArea, FreeRepositoryMap } from "@/lib/visitorWiki";

/**
 * What the home map draws, from a visitor repository's free map: its folder
 * areas as pages, the recorded references between them, and the main path.
 * Null until the index has drawn at least two areas.
 */
export function freeMapPicture(
  result: FreeRepositoryMap | null,
): { pages: WikiPageRef[]; map: WikiAreaMap; route: MapRoute | null } | null {
  const map = result?.map;
  if (!map?.available || map.areas.length < 2) return null;
  const pages: WikiPageRef[] = map.areas.map((area) => ({
    id: area.id,
    title: area.title,
    tier: "primary",
    children: [],
  }));
  const areaIds = new Set(map.areas.map((area) => area.id));
  const all = result?.path?.stops ?? [];
  const stops = all
    .map((stop, index) => ({ stop, index }))
    .filter(({ stop }) => stop.area && areaIds.has(stop.area))
    .map(({ stop, index }) => ({
      index: index + 1,
      symbol: stop.symbol,
      label: displaySymbol(stop.symbol, { short: true }),
      areaId: stop.area as string,
      file: stop.file,
      line: stop.line,
    }));
  const route: MapRoute | null =
    result?.path && new Set(stops.map((stop) => stop.areaId)).size >= 2
      ? { title: result.path.title, total: all.length, stops }
      : null;
  return {
    pages,
    map: {
      available: true,
      areas: map.areas.map(({ id, title, symbols, files }) => ({ id, title, symbols, files })),
      links: map.links,
      reason: null,
    },
    route,
  };
}

/** Where a part opens on GitHub, at the commit the map was drawn from. */
export function freeAreaUrl(slug: string, commit: string, area: FreeMapArea | undefined): string | null {
  if (!area || area.path == null) return null;
  if (area.kind === "root" || area.path === "") return `https://github.com/${slug}/tree/${commit}`;
  return `https://github.com/${slug}/${area.kind === "file" ? "blob" : "tree"}/${commit}/${area.path}`;
}
