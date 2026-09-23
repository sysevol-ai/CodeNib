// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import { useLayoutEffect, useMemo, useRef, useState } from "react";

import { repoRelative, type WikiAreaLink, type WikiAreaMap } from "@/lib/api";
import { layoutAreas } from "@/lib/areaLayout";
import { splitSymbolLabel } from "@/lib/symbols";

interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

const pairKey = (link: { source: string; target: string }) => `${link.source}>${link.target}`;

function Example({ link, titles }: { link: WikiAreaLink; titles: Map<string, string> }) {
  const from = splitSymbolLabel(link.example.source).symbol;
  const to = splitSymbolLabel(link.example.target).symbol;
  const anchor = link.example.anchor;
  const file = anchor ? repoRelative(anchor.file) ?? anchor.file : "";
  return (
    <p className="area-map-caption" aria-live="polite">
      <strong>{titles.get(link.source)}</strong> calls into{" "}
      <strong>{titles.get(link.target)}</strong> {link.weight}
      {link.weight === 1 ? " time" : " times"}, for example <code>{from}</code> calls{" "}
      <code>{to}</code>
      {anchor && (
        <span className="cite-src" title={`${file}${anchor.line ? `:${anchor.line}` : ""}`}>
          {" "}
          {file.split("/").pop() || file}
          {anchor.line != null && <span className="cite-src-loc">:{anchor.line}</span>}
        </span>
      )}
      .
    </p>
  );
}

/** The wiki's areas laid out by who calls whom, straight from the index. */
export default function AreaMap({
  map,
  commit,
  onPick,
}: {
  map: WikiAreaMap;
  commit?: string | null;
  onPick: (pageId: string) => void;
}) {
  const titles = useMemo(() => new Map(map.areas.map((a) => [a.id, a.title])), [map]);
  const layout = useMemo(
    () => layoutAreas(map.areas.map((a) => a.id), map.links),
    [map],
  );
  const linkByPair = useMemo(() => new Map(map.links.map((l) => [pairKey(l), l])), [map]);
  const maxWeight = Math.max(1, ...layout.drawn.map((l) => l.weight));
  const columnOf = useMemo(() => {
    const at = new Map<string, number>();
    layout.columns.forEach((ids, index) => ids.forEach((id) => at.set(id, index)));
    return at;
  }, [layout]);
  // An arrow that skips a column would run behind the cards between; it
  // arcs over the top instead, so the canvas keeps headroom for it.
  const skips = (link: { source: string; target: string }) =>
    (columnOf.get(link.target) ?? 0) - (columnOf.get(link.source) ?? 0) > 1;
  const hasSkip = layout.drawn.some(skips);
  const [focus, setFocus] = useState<string | null>(null);
  const [activePair, setActivePair] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const cardRefs = useRef(new Map<string, HTMLElement>());
  const [boxes, setBoxes] = useState<Map<string, Box>>(new Map());
  const [size, setSize] = useState({ w: 0, h: 0 });

  useLayoutEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const measure = () => {
      const base = root.getBoundingClientRect();
      const next = new Map<string, Box>();
      cardRefs.current.forEach((el, id) => {
        const r = el.getBoundingClientRect();
        next.set(id, { x: r.left - base.left, y: r.top - base.top, w: r.width, h: r.height });
      });
      setBoxes(next);
      setSize({ w: base.width, h: base.height });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(root);
    return () => observer.disconnect();
  }, [layout]);

  const outgoing = (id: string) =>
    map.links.filter((l) => l.source === id).sort((a, b) => b.weight - a.weight);
  const strongest = layout.drawn[0] ? linkByPair.get(pairKey(layout.drawn[0])) : map.links[0];
  const focusLink = focus ? outgoing(focus)[0] : undefined;
  const caption =
    (activePair && linkByPair.get(activePair)) || focusLink || strongest || null;
  // Columns sit side by side only when the arrows have room to run between them.
  const sideBySide = layout.columns.length > 1;

  return (
    <figure className="area-map" aria-label="How the areas of this repository call each other">
      <figcaption className="area-map-head">
        <span className="boundary-kicker">System map</span>
        <span className="boundary-meta">
          Recorded calls between the areas of this wiki, from the index
          {commit ? ` at ${commit}` : ""}
        </span>
      </figcaption>
      <div
        ref={rootRef}
        className={`area-map-canvas ${sideBySide ? "is-layered" : ""} ${hasSkip ? "has-arcs" : ""}`}
        style={{ ["--area-columns" as string]: String(layout.columns.length) }}
        onMouseLeave={() => {
          setFocus(null);
          setActivePair(null);
        }}
      >
        {size.w > 0 && (
          <svg
            className="area-map-links"
            width={size.w}
            height={size.h}
            viewBox={`0 0 ${size.w} ${size.h}`}
            aria-hidden
          >
            <defs>
              <marker
                id="area-arrow"
                viewBox="0 0 8 8"
                refX="7"
                refY="4"
                markerWidth="8"
                markerHeight="8"
                markerUnits="userSpaceOnUse"
                orient="auto"
              >
                <path d="M0,0 L8,4 L0,8 z" className="area-map-arrowhead" />
              </marker>
            </defs>
            {layout.drawn.map((link) => {
              const a = boxes.get(link.source);
              const b = boxes.get(link.target);
              if (!a || !b) return null;
              // Spread a card's arrows down its side, ordered by where the
              // other end sits, so they leave and arrive without piling up.
              const port = (box: Box, id: string, side: "out" | "in") => {
                const peers = layout.drawn
                  .filter((l) => (side === "out" ? l.source === id : l.target === id))
                  .map((l) => (side === "out" ? l.target : l.source))
                  .sort((p, q) => (boxes.get(p)?.y ?? 0) - (boxes.get(q)?.y ?? 0));
                const other = side === "out" ? link.target : link.source;
                const index = peers.indexOf(other);
                const span = box.h * 0.5;
                return peers.length < 2
                  ? box.y + box.h / 2
                  : box.y + box.h * 0.25 + (span * index) / (peers.length - 1);
              };
              const x1 = a.x + a.w;
              const y1 = port(a, link.source, "out");
              const x2 = b.x - 2;
              const y2 = port(b, link.target, "in");
              const bend = Math.max(24, (x2 - x1) / 2);
              const top = Math.min(...[...boxes.values()].map((box) => box.y));
              const d = skips(link)
                ? `M${a.x + a.w * 0.62},${a.y} C${a.x + a.w * 0.62},${top - 34} ${b.x + b.w * 0.38},${top - 34} ${b.x + b.w * 0.38},${b.y - 2}`
                : `M${x1},${y1} C${x1 + bend},${y1} ${x2 - bend},${y2} ${x2},${y2}`;
              const key = pairKey(link);
              const lit =
                activePair === key ||
                (focus != null && (link.source === focus || link.target === focus));
              const dim = (focus != null || activePair != null) && !lit;
              return (
                <path
                  key={key}
                  d={d}
                  className={`area-map-link ${lit ? "is-lit" : ""} ${dim ? "is-dim" : ""}`}
                  strokeWidth={1.25 + (2.5 * link.weight) / maxWeight}
                  markerEnd="url(#area-arrow)"
                  onMouseEnter={() => setActivePair(key)}
                />
              );
            })}
          </svg>
        )}
        {layout.columns.map((ids, index) => (
          <div className="area-map-column" key={index}>
            {ids.map((id) => {
              const area = map.areas.find((a) => a.id === id)!;
              const calls = outgoing(id);
              const touched =
                focus === id ||
                (focus != null &&
                  map.links.some(
                    (l) =>
                      (l.source === focus && l.target === id) ||
                      (l.target === focus && l.source === id),
                  ));
              return (
                <div
                  key={id}
                  ref={(el) => {
                    if (el) cardRefs.current.set(id, el);
                    else cardRefs.current.delete(id);
                  }}
                  className={`area-card ${focus && !touched ? "is-dim" : ""}`}
                  onMouseEnter={() => {
                    setFocus(id);
                    setActivePair(null);
                  }}
                >
                  <button type="button" className="area-card-title" onClick={() => onPick(id)}>
                    {area.title}
                  </button>
                  <span className="area-card-meta">
                    {area.symbols} symbols · {area.files} {area.files === 1 ? "file" : "files"}
                  </span>
                  {calls.length > 0 && (
                    <ul className="area-card-calls" aria-label={`${area.title} calls`}>
                      {calls.slice(0, 3).map((l) => (
                        <li key={l.target}>
                          <span aria-hidden>→</span> {titles.get(l.target)}{" "}
                          <span className="area-card-count">{l.weight}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              );
            })}
          </div>
        ))}
      </div>
      {caption && <Example link={caption} titles={titles} />}
    </figure>
  );
}
