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
  // Parts the main path stops in keep their own place, so the path never
  // lands on the grey "Other".
  const onPath = new Set(
    (result?.path?.stops ?? []).map((stop) => stop.area).filter((area): area is string => !!area),
  );
  const folded = foldParts(result?.map, onPath);
  const map = folded;
  if (!map?.available || map.areas.length < 2) return null;
  const names = partNames(map.areas);
  const pages: WikiPageRef[] = map.areas.map((area) => ({
    id: area.id,
    title: names.get(area.id) ?? area.title,
    tier: "primary",
    children: [],
  }));
  const areaIds = new Set(map.areas.map((area) => area.id));
  const into = folded?.into ?? new Map<string, string>();
  const all = (result?.path?.stops ?? []).map((stop) => ({
    ...stop,
    area: stop.area ? (into.get(stop.area) ?? stop.area) : null,
  }));
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

/** The map has eight area colours; a ninth part would be grey like "Other". */
export const MAX_PARTS = 8;

/**
 * At most eight parts: seven keep their place and the rest join "Other",
 * with their references summed and references inside Other dropped. Parts in
 * `keep` (where the main path stops) are kept first, then the largest.
 */
export function foldParts(
  map: FreeRepositoryMap["map"] | undefined,
  keep: Set<string> = new Set(),
): (NonNullable<FreeRepositoryMap["map"]> & { into: Map<string, string> }) | undefined {
  if (!map) return undefined;
  const into = new Map<string, string>();
  if (map.areas.length <= MAX_PARTS) return { ...map, into };
  const own = map.areas.filter((area) => area.kind !== "other").sort((a, b) => b.symbols - a.symbols);
  const kept = [
    ...own.filter((area) => keep.has(area.id)),
    ...own.filter((area) => !keep.has(area.id)),
  ].slice(0, MAX_PARTS - 1);
  const keptIds = new Set(kept.map((area) => area.id));
  const rest = map.areas.filter((area) => !keptIds.has(area.id));
  for (const area of rest) into.set(area.id, "other");
  const other: FreeMapArea = {
    id: "other",
    title: "Other",
    symbols: rest.reduce((sum, area) => sum + area.symbols, 0),
    files: rest.reduce((sum, area) => sum + area.files, 0),
    path: null,
    kind: "other",
  };
  const links = new Map<string, (typeof map.links)[number]>();
  for (const link of map.links) {
    const source = into.get(link.source) ?? link.source;
    const target = into.get(link.target) ?? link.target;
    if (source === target) continue;
    const key = `${source}>${target}`;
    const found = links.get(key);
    if (!found) links.set(key, { ...link, source, target });
    else {
      const heavier = link.weight > found.weight ? link.example : found.example;
      links.set(key, { ...found, weight: found.weight + link.weight, calls: found.calls + link.calls, example: heavier });
    }
  }
  return {
    ...map,
    areas: [...map.areas.filter((area) => keptIds.has(area.id)), other],
    links: [...links.values()],
    into,
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
