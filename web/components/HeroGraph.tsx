// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useMemo, useState } from "react";

import WikiGraphPane from "@/components/WikiGraphPane";
import type { WikiAreaMap, WikiPageRef } from "@/lib/api";
import { buildWikiGraph } from "@/lib/wikiGraph";

const TOUR_MS = 2800;
const number = new Intl.NumberFormat("en-US");

/**
 * A repository's Wiki as a picture, for the home pages. It walks through the
 * areas on its own, lighting up what each one references, until the reader
 * points at it. Readers who ask for reduced motion get a still map.
 */
export default function HeroGraph({
  repoName,
  pages,
  map,
  wikiHref,
  onPick,
}: {
  repoName: string;
  pages: WikiPageRef[];
  map: WikiAreaMap;
  wikiHref: string;
  onPick: (id: string) => void;
}) {
  const graph = useMemo(() => buildWikiGraph(pages, map), [pages, map]);
  // The largest areas first; they have the most to show.
  const tour = useMemo(
    () =>
      graph.nodes
        .filter((node) => node.kind === "area")
        .sort((a, b) => b.radius - a.radius)
        .slice(0, 6)
        .map((node) => node.id),
    [graph],
  );
  const [step, setStep] = useState(0);
  const [paused, setPaused] = useState(false);

  useEffect(() => {
    setStep(0);
  }, [graph]);

  useEffect(() => {
    if (paused || tour.length < 2) return undefined;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return undefined;
    const timer = window.setInterval(() => setStep((current) => current + 1), TOUR_MS);
    return () => window.clearInterval(timer);
  }, [paused, tour]);

  const activeId = tour[step % Math.max(tour.length, 1)] ?? "";
  const title = graph.nodes.find((node) => node.id === activeId)?.title;
  const reach = map.links.filter((link) => link.source === activeId || link.target === activeId);
  const references = reach.reduce((sum, link) => sum + link.weight, 0);
  const symbols = map.areas.reduce((sum, area) => sum + area.symbols, 0);
  const total = map.links.reduce((sum, link) => sum + link.weight, 0);

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
        idle={
          title
            ? { title, detail: `${number.format(references)} recorded references with other areas` }
            : undefined
        }
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
