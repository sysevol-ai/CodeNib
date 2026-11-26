// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useMemo, useState } from "react";

import WikiGraphPane, { type MapStatus } from "@/components/WikiGraphPane";
import type { WikiAreaMap, WikiPageRef } from "@/lib/api";
import { routeHeading, stopPlace, type MapRoute } from "@/lib/mapRoute";
import { buildWikiGraph } from "@/lib/wikiGraph";

const TOUR_MS = 2800;
/** Each recorded call on the main path holds this long, its crossing included. */
const STOP_MS = 1500;
/** The finished route stays up this long before the area tour starts. */
const HOLD_MS = 3600;
const number = new Intl.NumberFormat("en-US");

type Phase = { kind: "route"; at: number } | { kind: "hold" } | { kind: "areas"; step: number };

/**
 * A repository's Wiki as a picture, for the home pages. It first replays the
 * Overview's main path, one recorded call at a time across the areas, then
 * walks through the areas on its own, lighting up what each one references,
 * until the reader points at it. Readers who ask for reduced motion get a
 * still map with the whole path drawn.
 */
export default function HeroGraph({
  repoName,
  pages,
  map,
  route = null,
  wikiHref,
  onPick,
}: {
  repoName: string;
  pages: WikiPageRef[];
  map: WikiAreaMap;
  /** The Overview's main path; without one the map only tours its areas. */
  route?: MapRoute | null;
  wikiHref: string;
  onPick: (id: string) => void;
}) {
  const graph = useMemo(() => buildWikiGraph(pages, map), [pages, map]);
  // The largest areas first; they have the most to show.
  const tour = useMemo(
    () =>
      graph.nodes
        // A catch-all group ("Other") has nothing of its own to show.
        .filter((node) => node.kind === "area" && node.id !== "other")
        .sort((a, b) => b.radius - a.radius)
        .slice(0, 6)
        .map((node) => node.id),
    [graph],
  );
  const stops = useMemo(
    () => (route ? route.stops.filter((stop) => graph.nodes.some((node) => node.id === stop.areaId)) : []),
    [route, graph],
  );
  const hasRoute = stops.length > 1;
  const [reduced] = useState(
    () => typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  );
  const [phase, setPhase] = useState<Phase>(() =>
    hasRoute ? (reduced ? { kind: "hold" } : { kind: "route", at: 0 }) : { kind: "areas", step: 0 },
  );
  const [paused, setPaused] = useState(false);

  useEffect(() => {
    setPhase(hasRoute ? (reduced ? { kind: "hold" } : { kind: "route", at: 0 }) : { kind: "areas", step: 0 });
  }, [graph, hasRoute, reduced]);

  useEffect(() => {
    if (paused || reduced) return undefined;
    if (phase.kind === "areas" && !hasRoute && tour.length < 2) return undefined;
    const wait = phase.kind === "route" ? STOP_MS : phase.kind === "hold" ? HOLD_MS : TOUR_MS;
    const timer = window.setTimeout(() => {
      setPhase((current) => {
        if (current.kind === "route") {
          return current.at + 1 < stops.length ? { kind: "route", at: current.at + 1 } : { kind: "hold" };
        }
        if (current.kind === "hold") return tour.length ? { kind: "areas", step: 0 } : { kind: "route", at: 0 };
        if (current.step + 1 < tour.length) return { kind: "areas", step: current.step + 1 };
        return hasRoute ? { kind: "route", at: 0 } : { kind: "areas", step: 0 };
      });
    }, wait);
    return () => window.clearTimeout(timer);
  }, [phase, paused, reduced, hasRoute, stops.length, tour.length]);

  const activeId = phase.kind === "areas" ? (tour[phase.step % Math.max(tour.length, 1)] ?? "") : "";
  const title = graph.nodes.find((node) => node.id === activeId)?.title;
  const reach = map.links.filter((link) => link.source === activeId || link.target === activeId);
  const references = reach.reduce((sum, link) => sum + link.weight, 0);
  const symbols = map.areas.reduce((sum, area) => sum + area.symbols, 0);
  const total = map.links.reduce((sum, link) => sum + link.weight, 0);

  let idle: MapStatus | undefined;
  if (route && hasRoute && phase.kind === "route") {
    // The ring on the map already names the area; the line keeps to the step
    // and the place in the file, so it stays one line on a phone.
    const at = Math.min(phase.at, stops.length - 1);
    const stop = stops[at];
    const place = stopPlace(stop);
    idle = {
      title: stop.label,
      code: true,
      detail: (
        <>
          {`Step\u00a0${at + 1}\u00a0of\u00a0${stops.length}`}
          {place && (
            <>
              {" · "}
              <span className="is-code">{place}</span>
            </>
          )}
        </>
      ),
    };
  } else if (route && hasRoute && phase.kind === "hold") {
    // Code names in the heading stay in the code face, as in the steps.
    idle = {
      title: routeHeading({ ...route, stops }).split(/`([^`]+)`/).map((part, index) =>
        index % 2 ? (
          <span key={index} className="is-code">
            {part}
          </span>
        ) : (
          part
        ),
      ),
      detail: `${stops.length}\u00a0steps traced in the code index`,
    };
  } else if (title) {
    idle = { title, detail: `${number.format(references)} recorded references with other areas` };
  }

  return (
    <div
      className="hero-graph"
      onPointerEnter={() => setPaused(true)}
      onPointerLeave={() => setPaused(false)}
      onFocusCapture={() => setPaused(true)}
      onBlurCapture={() => setPaused(false)}
    >
      <WikiGraphPane
        embedded
        graph={graph}
        activeId={activeId}
        hasReferences={map.links.length > 0}
        note={null}
        onPick={onPick}
        onOpenReference={(link) => onPick(link.source)}
        idle={idle}
        route={
          hasRoute && phase.kind !== "areas"
            ? { stops, at: phase.kind === "route" ? phase.at : stops.length - 1, animate: !reduced }
            : undefined
        }
        routeLegend={hasRoute ? stops : undefined}
        onRoute={() => onPick("overview")}
        header={
          <>
            <a className="wiki-graph-title" href={wikiHref}>
              {repoName} <span aria-hidden="true">→</span>
            </a>
            <p className="wiki-graph-stats">
              {map.areas.length} areas · {number.format(symbols)} symbols · {number.format(total)} recorded references
            </p>
          </>
        }
      />
    </div>
  );
}
