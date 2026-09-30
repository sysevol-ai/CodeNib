// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useRef, useState } from "react";

import { fetchWikiAreaMap, type WikiAreaMap } from "@/lib/api";
import { thumbLayout } from "@/lib/posterLayout";
import { loadSavedWikiGraphs } from "@/lib/visitorWiki";

const maps = new Map<string, Promise<WikiAreaMap>>();

function loadMap(repoId?: string, wikiId?: string): Promise<WikiAreaMap> {
  const key = wikiId ? `wiki:${wikiId}` : `repo:${repoId}`;
  let pending = maps.get(key);
  if (!pending) {
    pending = wikiId
      ? loadSavedWikiGraphs(wikiId, "overview").then((view) => view.system_map)
      : fetchWikiAreaMap(repoId!);
    pending.catch(() => maps.delete(key));
    // A running Wiki or an operator backfill may acquire its map later.
    pending.then((value) => { if (!value.available) maps.delete(key); }, () => {});
    maps.set(key, pending);
  }
  return pending;
}

const W = 320;
const H = 104;

/** A catalog card's picture: the repository's areas as dots sized by
 *  symbols and its recorded references as lines, from the same index as the
 *  Overview poster. Loaded when the card scrolls into view. */
export default function MapThumb({ repoId, wikiId }: { repoId: string; wikiId?: never } | { repoId?: never; wikiId: string }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [map, setMap] = useState<WikiAreaMap | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    let cancelled = false;
    setMap(null);
    setFailed(false);
    const start = () =>
      loadMap(repoId, wikiId)
        .then((value) => {
          if (!cancelled) setMap(value);
        })
        .catch(() => {
          if (!cancelled) setFailed(true);
        });
    if (typeof IntersectionObserver === "undefined") {
      start();
      return () => {
        cancelled = true;
      };
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          observer.disconnect();
          start();
        }
      },
      { rootMargin: "900px 0px" },
    );
    observer.observe(node);
    return () => {
      cancelled = true;
      observer.disconnect();
    };
  }, [repoId, wikiId]);

  const layout = map ? thumbLayout(map.areas, map.links, W, H) : null;
  const empty = failed || (map != null && (!layout || layout.dots.length === 0));

  return (
    <div className="map-thumb" ref={ref} aria-hidden>
      {layout && layout.dots.length > 0 && (
        <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet">
          {layout.lines.map((line) => (
            <g key={line.key}>
              <path className="map-thumb-line" d={line.d} strokeWidth={line.width} />
              <path className="map-thumb-flow" d={line.d} strokeWidth={Math.max(1, line.width * 0.8)} />
            </g>
          ))}
          {layout.dots.map((dot) => (
            <circle key={dot.id} className="map-thumb-dot" cx={dot.x} cy={dot.y} r={dot.r} />
          ))}
        </svg>
      )}
      {!map && !failed && <span className="map-thumb-loading" />}
      {empty && (
        <span className="map-thumb-empty">
          {map?.reason === "no_cross_area_calls"
            ? "No references between areas in the index"
            : "No code graph in the index yet"}
        </span>
      )}
    </div>
  );
}
