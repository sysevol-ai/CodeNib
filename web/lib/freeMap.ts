// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import type { WikiAreaMap, WikiPageRef } from "@/lib/api";
import { callLabel, type MapRoute } from "@/lib/mapRoute";
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
  const names = partNames(map.areas);
  const pages: WikiPageRef[] = map.areas.map((area) => ({
    id: area.id,
    title: names.get(area.id) ?? area.title,
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
      label: callLabel(displaySymbol(stop.symbol, { short: true })),
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

/**
 * Short names for the map: the folder every part shares is said once (in
 * the page's title), so `httpx/_client.py` reads `_client.py`; folders end
 * in a slash, and the root's own files read "Top-level files".
 */
export function partNames(areas: FreeMapArea[]): Map<string, string> {
  const paths = areas
    .filter((area) => (area.kind === "folder" || area.kind === "file") && area.path)
    .map((area) => area.path as string);
  let prefix = "";
  if (paths.length >= 2) {
    const split = paths.map((path) => path.split("/"));
    const shared: string[] = [];
    for (let depth = 0; ; depth += 1) {
      const part = split[0][depth];
      // Keep the last segment of every path: a part needs a name.
      if (part === undefined || split.some((segments) => segments.length <= depth + 1 || segments[depth] !== part)) break;
      shared.push(part);
    }
    prefix = shared.length ? `${shared.join("/")}/` : "";
  }
  return new Map(
    areas.map((area) => {
      if (area.kind === "root" || area.path === "") return [area.id, "Top-level files"];
      if (area.kind === "other" || area.path == null) return [area.id, area.title];
      const short = area.path.startsWith(prefix) ? area.path.slice(prefix.length) : area.path;
      return [area.id, area.kind === "folder" ? `${short}/` : short];
    }),
  );
}

/** Where a part opens on GitHub, at the commit the map was drawn from. */
export function freeAreaUrl(slug: string, commit: string, area: FreeMapArea | undefined): string | null {
  if (!area || area.path == null) return null;
  if (area.kind === "root" || area.path === "") return `https://github.com/${slug}/tree/${commit}`;
  return `https://github.com/${slug}/${area.kind === "file" ? "blob" : "tree"}/${commit}/${area.path}`;
}
