// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import type { Citation } from "@/lib/api";
import { displaySymbol } from "@/lib/symbols";
import type { Journey } from "@/lib/wikiPresentation";

/** One recorded call on the Overview's main path, placed in its map area. */
export interface RouteStop {
  /** The stage's number in the Overview, which counts every stage. */
  index: number;
  symbol: string;
  /** The short name the map shows, such as `Session.send()`. */
  label: string;
  areaId: string;
  file: string | null;
  line: number | null;
}

export interface MapRoute {
  /** The Overview's heading for the path, with its code spans. */
  title: string;
  /** How many stages the Overview lists, placed on the map or not. */
  total: number;
  stops: RouteStop[];
}

/** Only what a route needs from a citation, so build-time captures stay small. */
export type RouteCitation = Pick<Citation, "file" | "start_line">;

/**
 * The Overview's main path as stops on the area map.
 *
 * A stage whose function the Overview places in no area has no point on the
 * map, so it is left out rather than drawn somewhere it does not live. A path
 * that touches fewer than two areas never moves across the map, and is not
 * worth replaying.
 */
export function routeFromJourney(
  journey: Journey | null,
  citations: (RouteCitation | null | undefined)[] | undefined,
  areaIds: Iterable<string>,
): MapRoute | null {
  if (!journey) return null;
  const areas = new Set(areaIds);
  const stops: RouteStop[] = [];
  for (const stage of journey.stages) {
    const areaId = stage.page?.id;
    if (!areaId || !areas.has(areaId)) continue;
    const cited = stage.evidence
      .map((id) => citations?.[Number(id.replace(/^E/, "")) - 1])
      .find((citation) => citation?.file);
    stops.push({
      index: stage.index,
      symbol: stage.symbol,
      label: callLabel(displaySymbol(stage.symbol, { short: true })),
      areaId,
      file: cited?.file ?? null,
      line: cited?.start_line ?? null,
    });
  }
  if (new Set(stops.map((stop) => stop.areaId)).size < 2) return null;
  return { title: journey.title, total: journey.stages.length, stops };
}

/** Every stop is a function the path calls, so it reads as one: `RouterGroup.handle()`. */
export function callLabel(label: string): string {
  return /\(.*\)$/.test(label) ? label : `${label}()`;
}

/** `sessions.py:752`, the place a stop is defined, by file name. */
export function stopPlace(stop: RouteStop): string | null {
  if (!stop.file) return null;
  const name = stop.file.split("/").pop() ?? stop.file;
  return stop.line != null ? `${name}:${stop.line}` : name;
}

/**
 * The route's heading as the map tells it: the Overview's own when every
 * stage is on the map, otherwise from the first to the last stop shown, so
 * the count and the heading describe the same steps.
 */
export function routeHeading(route: MapRoute): string {
  if (route.stops.length === route.total) return route.title;
  const first = route.stops[0];
  const last = route.stops[route.stops.length - 1];
  return `From \`${first.label}\` to \`${last.label}\``;
}

/** The heading without its Markdown code marks. */
export function plainTitle(title: string): string {
  return title.replace(/`([^`]+)`/g, (_, code: string) => displaySymbol(code, { short: true }));
}
