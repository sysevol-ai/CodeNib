// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import {
  fetchSource,
  type RepoInfo,
  type SourceSlice,
  type WikiAreaLink,
  type WikiAreaMap,
  type WikiPageRef,
} from "@/lib/api";
import { breakableCode } from "@/lib/breakable";
import { ghFileUrl } from "@/lib/github";
import { highlightSource } from "@/lib/highlight";
import {
  layoutAtlas,
  layoutPoster,
  posterMode,
  tracedHops,
  type PosterEdge,
} from "@/lib/posterLayout";
import { AppLink } from "@/lib/router";
import { isStaticRuntime } from "@/lib/runtime";
import { splitSymbolLabel } from "@/lib/symbols";
import type { Journey } from "@/lib/wikiPresentation";

/** Plain text of a lead sentence: citation handles dropped, code kept. */
function leadNodes(markdown: string): ReactNode[] {
  const text = markdown
    .replace(/\[[ER]\d+\]\(#evidence-[ER]\d+\)/g, "")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
  return text.split(/(`[^`]+`)/).map((part, index) =>
    part.startsWith("`") && part.endsWith("`") ? (
      <code key={index}>{breakableCode(part.slice(1, -1))}</code>
    ) : (
      <span key={index}>{part}</span>
    ),
  );
}

/** Top-level area (page id) for every page in the tree. */
function areaOfPage(pages: WikiPageRef[]): Map<string, string> {
  const out = new Map<string, string>();
  const walk = (list: WikiPageRef[], top: string | null) => {
    for (const page of list) {
      const area = top ?? page.id;
      out.set(page.id, area);
      walk(page.children, area);
    }
  };
  walk(pages, null);
  return out;
}

const number = new Intl.NumberFormat("en-US");

// SVG properties the stylesheet sets; the image capture copies inline styles
// only, so these are written onto each element for the duration of a capture.
const SVG_STYLE_PROPS = [
  "fill",
  "stroke",
  "stroke-width",
  "stroke-dasharray",
  "stroke-dashoffset",
  "stroke-linecap",
  "opacity",
  "filter",
  "font-family",
  "font-size",
  "font-weight",
  "font-variant-numeric",
];

function inlineSvgStyles(root: Element): () => void {
  const saved: Array<[Element, string | null]> = [];
  for (const el of root.querySelectorAll("svg *")) {
    const computed = getComputedStyle(el);
    saved.push([el, el.getAttribute("style")]);
    const inline = SVG_STYLE_PROPS.map((prop) => `${prop}:${computed.getPropertyValue(prop)}`).join(";");
    el.setAttribute("style", inline);
  }
  return () => {
    for (const [el, style] of saved) {
      if (style == null) el.removeAttribute("style");
      else el.setAttribute("style", style);
    }
  };
}
const DWELL_MS = 950;
const TRAVEL_MS = 900;

type Step =
  | { kind: "dwell"; stage: number; ms: number }
  | { kind: "travel"; stage: number; edge: string; ms: number };

function EdgeCallout({
  edge,
  link,
  titles,
  repoId,
  repo,
  onClose,
}: {
  edge: PosterEdge;
  link: WikiAreaLink | undefined;
  titles: Map<string, string>;
  repoId: string;
  repo: RepoInfo | null;
  onClose: () => void;
}) {
  const anchor = link?.example.anchor ?? null;
  const [slice, setSlice] = useState<SourceSlice | null>(null);
  useEffect(() => {
    setSlice(null);
    if (!anchor?.file || anchor.line == null || isStaticRuntime()) return;
    let cancelled = false;
    fetchSource(repoId, anchor.file, Math.max(1, anchor.line - 2), anchor.line + 3, repo?.base_commit)
      .then((value) => {
        if (!cancelled) setSlice(value);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [repoId, anchor?.file, anchor?.line, repo?.base_commit]);

  const from = link ? splitSymbolLabel(link.example.source).symbol : "";
  const to = link ? splitSymbolLabel(link.example.target).symbol : "";
  const url =
    anchor?.file && repo
      ? ghFileUrl(repo.repo, repo.source_url, repo.base_commit, anchor.file, anchor.line)
      : null;
  const lines = slice ? slice.content.replace(/\n$/, "").split("\n") : [];
  const language = anchor?.file?.split(".").pop() || "";

  return (
    <div className="poster-callout" role="dialog" aria-label="Recorded reference">
      <div className="poster-callout-head">
        <span>
          <b>{titles.get(edge.source)}</b>
          <span className="poster-callout-arrow" aria-hidden>→</span>
          <b>{titles.get(edge.target)}</b>
        </span>
        <button type="button" className="poster-callout-close" onClick={onClose} aria-label="Close">
          ×
        </button>
      </div>
      <div className="poster-callout-meta">
        {number.format(edge.weight)} recorded references
        {edge.calls !== edge.weight && <> · {number.format(edge.calls)} distinct pairs</>}
      </div>
      {link && (
        <div className="poster-callout-example">
          <span className="poster-callout-label">For example</span>
          <code>{breakableCode(from)}</code>
          <span aria-hidden> → </span>
          <code>{breakableCode(to)}</code>
        </div>
      )}
      {anchor?.file && (
        <div className="poster-code">
          <div className="poster-code-head">
            <span>
              {anchor.file.split("/").pop()}
              {anchor.line != null && `:${anchor.line}`}
            </span>
            {url && (
              <a href={url} target="_blank" rel="noreferrer">
                Open on GitHub ↗
              </a>
            )}
          </div>
          {lines.length > 0 && (
            <pre>
              {lines.map((line, index) => {
                const at = slice!.start_line + index;
                return (
                  <span key={at} className={`poster-code-line${at === anchor.line ? " is-anchor" : ""}`}>
                    <span className="poster-code-no">{at}</span>
                    <span dangerouslySetInnerHTML={{ __html: highlightSource(line, language) || " " }} />
                  </span>
                );
              })}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * The Overview's first screen: the wiki's areas and the references recorded
 * between them, with the traced entry path played along it. Everything drawn
 * comes from the code index; the model wrote none of it.
 */
export default function RepoPoster({
  repoId,
  repo,
  map,
  pages,
  journey,
  lead,
  onPick,
  onOpenGraph,
  variant = "page",
}: {
  repoId: string;
  repo: RepoInfo | null;
  map: WikiAreaMap;
  pages: WikiPageRef[];
  journey: Journey | null;
  lead?: string;
  onPick?: (pageId: string) => void;
  onOpenGraph?: () => void;
  /** "hero": the home page's live map, with a compact heading and a link
   *  into the Wiki instead of the page's export controls. */
  variant?: "page" | "hero";
}) {
  const hero = variant === "hero";
  const pageArea = useMemo(() => areaOfPage(pages), [pages]);
  const stages = journey?.stages ?? [];
  const stageAreas = useMemo(
    () => stages.map((stage) => (stage.page ? pageArea.get(stage.page.id) ?? stage.page.id : undefined)),
    [stages, pageArea],
  );
  const { hops } = useMemo(() => tracedHops(stageAreas), [stageAreas]);
  // Lay out at the canvas's real width so text is drawn at its CSS size
  // instead of being scaled with the drawing.
  const canvasRef = useRef<HTMLDivElement | null>(null);
  const [canvasWidth, setCanvasWidth] = useState(960);
  useEffect(() => {
    const node = canvasRef.current;
    if (!node || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      const next = Math.round(Math.max(280, entry.contentRect.width));
      setCanvasWidth((current) => (Math.abs(current - next) > 4 ? next : current));
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  // Too few recorded references to draw a map: show how the code divides
  // by size instead, and say how many references there were.
  const { mode, linked } = useMemo(() => posterMode(map.areas, map.links), [map]);
  const shownAreas = useMemo(() => map.areas.filter((area) => area.symbols > 0), [map]);
  const isolated = mode === "network" ? shownAreas.filter((area) => !linked.has(area.id)) : [];
  const layout = useMemo(
    () =>
      mode === "network"
        ? layoutPoster(
            map.areas.filter((area) => linked.has(area.id)),
            map.links,
            { traced: hops, width: canvasWidth },
          )
        : null,
    [map, mode, linked, hops, canvasWidth],
  );
  const atlas = useMemo(
    () => (mode === "atlas" ? layoutAtlas(map.areas, canvasWidth) : null),
    [map, mode, canvasWidth],
  );
  const edges = layout?.edges ?? [];
  // In the atlas the recorded links are listed, not drawn; they still open
  // the same call-site card.
  const listedEdges = useMemo<PosterEdge[]>(
    () =>
      mode === "atlas"
        ? map.links.map((link) => ({
            source: link.source,
            target: link.target,
            weight: link.weight,
            calls: link.calls,
            d: "",
            mid: { x: 0, y: 0 },
            width: 1,
            traced: false,
          }))
        : [],
    [map, mode],
  );
  const titles = useMemo(() => new Map(map.areas.map((area) => [area.id, area.title])), [map]);
  const linkOf = useMemo(
    () => new Map(map.links.map((link) => [`${link.source}\u0000${link.target}`, link])),
    [map],
  );
  const edgeKey = (edge: { source: string; target: string }) => `${edge.source}\u0000${edge.target}`;

  const totals = useMemo(
    () => ({
      areas: shownAreas.length,
      symbols: shownAreas.reduce((sum, area) => sum + area.symbols, 0),
      files: repo?.file_count || shownAreas.reduce((sum, area) => sum + area.files, 0),
      references: map.links.reduce((sum, link) => sum + link.weight, 0),
    }),
    [shownAreas, map, repo?.file_count],
  );

  const [hoverNode, setHoverNode] = useState<string | null>(null);
  const [hoverEdge, setHoverEdge] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [activeStage, setActiveStage] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);
  const [exporting, setExporting] = useState(false);
  const pathRefs = useRef(new Map<string, SVGPathElement>());
  const pulseRef = useRef<SVGCircleElement | null>(null);
  const rootRef = useRef<HTMLElement | null>(null);
  const played = useRef(false);

  // One timeline: rest on each stage, travel along the hop when the next
  // stage lives in another area.
  const timeline = useMemo<Step[]>(() => {
    const steps: Step[] = [];
    stages.forEach((_stage, index) => {
      steps.push({ kind: "dwell", stage: index, ms: DWELL_MS });
      const here = stageAreas[index];
      const next = stageAreas[index + 1];
      if (index + 1 < stages.length && here && next && here !== next) {
        steps.push({ kind: "travel", stage: index, edge: `${here}\u0000${next}`, ms: TRAVEL_MS });
      }
    });
    return steps;
  }, [stages, stageAreas]);

  useEffect(() => {
    if (!playing || timeline.length === 0) return;
    let frame = 0;
    const started = performance.now();
    const total = timeline.reduce((sum, step) => sum + step.ms, 0);
    const tick = (now: number) => {
      let t = now - started;
      if (t >= total) {
        setPlaying(false);
        setActiveStage(null);
        pulseRef.current?.setAttribute("opacity", "0");
        return;
      }
      for (const step of timeline) {
        if (t > step.ms) {
          t -= step.ms;
          continue;
        }
        const pulse = pulseRef.current;
        if (step.kind === "dwell") {
          setActiveStage((current) => (current === step.stage ? current : step.stage));
          pulse?.setAttribute("opacity", "0");
        } else {
          const path = pathRefs.current.get(step.edge);
          if (path && pulse) {
            const eased = 0.5 - Math.cos((Math.PI * t) / step.ms) / 2;
            const point = path.getPointAtLength(path.getTotalLength() * eased);
            pulse.setAttribute("cx", String(point.x));
            pulse.setAttribute("cy", String(point.y));
            pulse.setAttribute("opacity", "1");
          }
        }
        break;
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [playing, timeline]);

  // Play once, the first time the poster is on screen, unless the reader
  // asked for less motion.
  useEffect(() => {
    const node = rootRef.current;
    if (!node || played.current || timeline.length === 0) return;
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting) && !played.current) {
        played.current = true;
        window.setTimeout(() => setPlaying(true), 500);
        observer.disconnect();
      }
    }, { threshold: 0.4 });
    observer.observe(node);
    return () => observer.disconnect();
  }, [timeline.length]);

  // Save the poster as an image a reader can post. The copy carries the
  // page's address; controls that only work on the page are left out.
  const exportImage = async () => {
    const node = rootRef.current;
    if (!node || exporting) return;
    setSelected(null);
    setPlaying(false);
    setActiveStage(null);
    setExporting(true);
    try {
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      const { toPng } = await import("html-to-image");
      const restore = inlineSvgStyles(node);
      let url: string;
      try {
        url = await toPng(node, {
          pixelRatio: 2,
          backgroundColor: "#070b14",
          filter: (child) =>
            !(child instanceof Element && child.classList.contains("poster-no-export")),
        });
      } finally {
        restore();
      }
      const link = document.createElement("a");
      link.href = url;
      link.download = `${(repo?.repo || repoId).replace(/[^\w.-]+/g, "-")}-system-map.png`;
      link.click();
    } catch {
      // A failed capture leaves the page as it was; nothing to undo.
    } finally {
      setExporting(false);
    }
  };

  const focusArea = activeStage != null ? stageAreas[activeStage] ?? null : null;
  const litNode = hoverNode ?? focusArea;
  const touches = (edge: PosterEdge, id: string | null) =>
    id != null && (edge.source === id || edge.target === id);
  const selectedEdge = selected
    ? [...edges, ...listedEdges].find((edge) => edgeKey(edge) === selected)
    : undefined;
  const stagedIn = (areaId: string) =>
    stages
      .map((stage, index) => ({ stage, index }))
      .filter(({ index }) => stageAreas[index] === areaId);
  const pins = (areaId: string, right: number) => {
    const staged = stagedIn(areaId);
    if (staged.length === 0) return null;
    return (
      <g className="poster-node-stages">
        {staged.map(({ index }, i) => (
          <g key={index} transform={`translate(${right - 16 - (staged.length - 1 - i) * 22} 0)`}>
            <circle r="9" className={activeStage === index ? "is-now" : ""} />
            <text textAnchor="middle" dy="3.5">{index + 1}</text>
          </g>
        ))}
      </g>
    );
  };
  const hasTrace = stages.length > 1;
  const [owner, name] = (repo?.repo || repoId).includes("/")
    ? [(repo?.repo || repoId).split("/")[0] + "/", (repo?.repo || repoId).split("/").slice(1).join("/")]
    : ["", repo?.repo || repoId];

  return (
    <section
      className={`repo-poster${hero ? " is-hero" : ""}${exporting ? " is-exporting" : ""}`}
      ref={rootRef}
      aria-label={`System map of ${repo?.repo || repoId}`}
    >
      <div className="poster-grid-bg" aria-hidden />
      <header className="poster-head">
        <div className="poster-eyebrow">
          <span className="poster-live" aria-hidden />
          System map
          {repo?.commit_short && (
            <>
              <span className="poster-sep">·</span>indexed at <span className="mono">{repo.commit_short}</span>
            </>
          )}
        </div>
        <h2 className="poster-title">
          <span className="poster-owner">{owner}</span>
          {name}
        </h2>
        {lead && !hero && <p className="poster-lead">{leadNodes(lead)}</p>}
        <dl className="poster-stats">
          <div><dt>areas</dt><dd>{number.format(totals.areas)}</dd></div>
          <div><dt>symbols</dt><dd>{number.format(totals.symbols)}</dd></div>
          <div><dt>files</dt><dd>{number.format(totals.files)}</dd></div>
          <div><dt>recorded references</dt><dd>{number.format(totals.references)}</dd></div>
        </dl>
      </header>

      <div className="poster-canvas" ref={canvasRef}>
        {atlas && (
          <svg
            viewBox={`0 0 ${atlas.width} ${atlas.height}`}
            role="img"
            aria-label="Areas of the repository, sized by the symbols the index assigns them"
            onMouseLeave={() => setHoverNode(null)}
          >
            <defs>
              <pattern id="poster-dots" width="9" height="9" patternUnits="userSpaceOnUse">
                <circle cx="1.5" cy="1.5" r="0.9" fill="rgba(148, 163, 184, 0.2)" />
              </pattern>
            </defs>
            {atlas.cells.map((cell) => {
              const lineHeight = Math.round(cell.fontSize * 1.25);
              const lit = hoverNode === cell.id || focusArea === cell.id;
              return (
                <g
                  key={cell.id}
                  className={`poster-node poster-cell${lit ? " is-lit" : ""}${
                    focusArea === cell.id ? " is-active" : ""
                  }`}
                  transform={`translate(${cell.x} ${cell.y})`}
                  onMouseEnter={() => setHoverNode(cell.id)}
                  onMouseLeave={() => setHoverNode(null)}
                  onClick={() => onPick?.(cell.id)}
                  role="link"
                  tabIndex={0}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") onPick?.(cell.id);
                  }}
                >
                  <title>{`${cell.title}: ${number.format(cell.symbols)} symbols in ${cell.files} files`}</title>
                  <rect
                    className="poster-node-box"
                    width={cell.w}
                    height={cell.h}
                    rx="10"
                    style={{ fillOpacity: 0.55 + Math.min(0.45, cell.share * 2.2) }}
                  />
                  <rect className="poster-cell-dots" width={cell.w} height={cell.h} rx="10" fill="url(#poster-dots)" />
                  {cell.lines.map((line, index) => (
                    <text
                      key={index}
                      className="poster-node-title"
                      x="12"
                      y={10 + cell.fontSize + index * lineHeight}
                      style={{ fontSize: cell.fontSize }}
                    >
                      {line}
                    </text>
                  ))}
                  {cell.lines.length > 0 && cell.w >= 150 && cell.h >= cell.lines.length * lineHeight + 44 && (
                    <text
                      className="poster-node-meta"
                      x="12"
                      y={10 + cell.fontSize + (cell.lines.length - 1) * lineHeight + 22}
                    >
                      {number.format(cell.symbols)} symbols · {cell.files} file{cell.files === 1 ? "" : "s"}
                    </text>
                  )}
                  <text className="poster-cell-share" x={cell.w - 12} y={cell.h - 12} textAnchor="end">
                    {cell.w >= 64 && cell.h >= 40 ? `${Math.round(cell.share * 100)}%` : ""}
                  </text>
                  {pins(cell.id, cell.w)}
                </g>
              );
            })}
          </svg>
        )}
        {layout && (
        <svg
          viewBox={`0 0 ${layout.width} ${layout.height}`}
          role="img"
          aria-label="Areas of the repository and the references recorded between them"
          onMouseLeave={() => {
            setHoverNode(null);
            setHoverEdge(null);
          }}
        >
          <defs>
            <filter id="poster-glow" x="-50%" y="-50%" width="200%" height="200%">
              <feGaussianBlur stdDeviation="4" result="blur" />
              <feMerge>
                <feMergeNode in="blur" />
                <feMergeNode in="SourceGraphic" />
              </feMerge>
            </filter>
            <linearGradient id="poster-meter" x1="0" x2="1">
              <stop offset="0" stopColor="#3b82f6" />
              <stop offset="1" stopColor="#22d3ee" />
            </linearGradient>
          </defs>

          <g className="poster-edges">
            {layout.edges.map((edge) => {
              const key = edgeKey(edge);
              const dim =
                (litNode != null && !touches(edge, litNode)) ||
                (hoverEdge != null && hoverEdge !== key);
              const hot = hoverEdge === key || selected === key;
              return (
                <g
                  key={key}
                  className={`poster-edge${edge.traced ? " is-traced" : ""}${dim ? " is-dim" : ""}${hot ? " is-hot" : ""}`}
                >
                  <path
                    className="poster-edge-line"
                    d={edge.d}
                    strokeWidth={edge.width}
                    ref={(node) => {
                      if (node) pathRefs.current.set(key, node);
                      else pathRefs.current.delete(key);
                    }}
                  />
                  <path className="poster-edge-flow" d={edge.d} strokeWidth={Math.max(1.4, edge.width * 0.8)} />
                  <path
                    className="poster-edge-hit"
                    d={edge.d}
                    onMouseEnter={() => setHoverEdge(key)}
                    onMouseLeave={() => setHoverEdge(null)}
                    onClick={() => setSelected((current) => (current === key ? null : key))}
                  >
                    <title>
                      {`${titles.get(edge.source)} → ${titles.get(edge.target)}: ${number.format(edge.weight)} references`}
                    </title>
                  </path>
                </g>
              );
            })}
          </g>

          <circle ref={pulseRef} className="poster-pulse" r="6" opacity="0" filter="url(#poster-glow)" />

          <g className="poster-nodes">
            {layout.nodes.map((node) => {
              const lit = litNode === node.id || focusArea === node.id;
              const dim = litNode != null && !lit && !layout.edges.some(
                (edge) => touches(edge, litNode) && touches(edge, node.id),
              );
              return (
                <g
                  key={node.id}
                  className={`poster-node${lit ? " is-lit" : ""}${dim ? " is-dim" : ""}${
                    focusArea === node.id ? " is-active" : ""
                  }`}
                  transform={`translate(${node.x} ${node.y})`}
                  onMouseEnter={() => setHoverNode(node.id)}
                  onMouseLeave={() => setHoverNode(null)}
                  onClick={() => onPick?.(node.id)}
                  role="link"
                  tabIndex={0}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") onPick?.(node.id);
                  }}
                >
                  <title>{`${node.title}: open this part of the wiki`}</title>
                  <rect className="poster-node-box" width={node.w} height={node.h} rx="12" />
                  {node.lines.map((line, index) => (
                    <text key={index} className="poster-node-title" x="14" y={30 + index * 18}>
                      {line}
                    </text>
                  ))}
                  <text className="poster-node-meta" x="14" y={30 + node.lines.length * 18 + 6}>
                    {number.format(node.symbols)} symbols · {node.files} file{node.files === 1 ? "" : "s"}
                  </text>
                  <rect className="poster-node-track" x="14" y={node.h - 15} width={node.w - 28} height="3" rx="1.5" />
                  <rect
                    className="poster-node-meter"
                    x="14"
                    y={node.h - 15}
                    width={Math.max(6, (node.w - 28) * Math.min(1, node.share * 2))}
                    height="3"
                    rx="1.5"
                  />
                  {pins(node.id, node.w)}
                </g>
              );
            })}
          </g>

          {hoverEdge && !selected && (() => {
            const edge = layout.edges.find((candidate) => edgeKey(candidate) === hoverEdge);
            if (!edge) return null;
            const label = `${number.format(edge.weight)} refs`;
            return (
              <g className="poster-edge-label" transform={`translate(${edge.mid.x} ${edge.mid.y})`}>
                <rect x={-(label.length * 3.6 + 10)} y="-11" width={label.length * 7.2 + 20} height="22" rx="11" />
                <text textAnchor="middle" dy="4">{label}</text>
              </g>
            );
          })()}
        </svg>
        )}

        {selectedEdge && !exporting && (
          <EdgeCallout
            edge={selectedEdge}
            link={linkOf.get(edgeKey(selectedEdge))}
            titles={titles}
            repoId={repoId}
            repo={repo}
            onClose={() => setSelected(null)}
          />
        )}
      </div>

      {isolated.length > 0 && (
        <div className="poster-isolated">
          <span className="poster-isolated-label">No recorded references to the areas above</span>
          <div className="poster-chips">
            {isolated.map((area) => (
              <button
                key={area.id}
                type="button"
                className={`poster-chip${focusArea === area.id ? " is-now" : ""}`}
                onClick={() => onPick?.(area.id)}
              >
                {area.title}
                <span>{number.format(area.symbols)}</span>
              </button>
            ))}
          </div>
        </div>
      )}
      {mode === "atlas" && (
        <div className="poster-isolated">
          <span className="poster-isolated-label">
            {map.links.length === 0
              ? "The index recorded no references between these areas, so they are sized by symbols instead of drawn as a map."
              : `The index recorded references between only ${linked.size} of these ${shownAreas.length} areas, too few to draw as a map, so they are sized by symbols.`}
          </span>
          {listedEdges.length > 0 && (
            <div className="poster-chips">
              {listedEdges.slice(0, 4).map((edge) => (
                <button
                  key={edgeKey(edge)}
                  type="button"
                  className={`poster-chip${selected === edgeKey(edge) ? " is-now" : ""}`}
                  onClick={() => setSelected((current) => (current === edgeKey(edge) ? null : edgeKey(edge)))}
                >
                  {titles.get(edge.source)} → {titles.get(edge.target)}
                  <span>{number.format(edge.weight)} refs</span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      {hasTrace && (
        <div className="poster-trace">
          <div className="poster-trace-head">
            <span className="poster-trace-kicker">Traced call path</span>
            <span className="poster-trace-title">{leadNodes(journey!.title)}</span>
            <button
              type="button"
              className="poster-play poster-no-export"
              onClick={() => {
                setSelected(null);
                setPlaying(false);
                window.requestAnimationFrame(() => setPlaying(true));
              }}
            >
              {playing ? "Playing…" : "▶ Play"}
            </button>
          </div>
          <ol className="poster-steps">
            {stages.map((stage, index) => (
              <li key={stage.index}>
                <button
                  type="button"
                  className={`poster-step${activeStage === index ? " is-now" : ""}`}
                  onMouseEnter={() => !playing && setActiveStage(index)}
                  onMouseLeave={() => !playing && setActiveStage(null)}
                  onClick={() => stage.page && onPick?.(stage.page.id)}
                  title={stage.page ? `Open ${stage.page.title}` : undefined}
                >
                  <span className="poster-step-no">{String(index + 1).padStart(2, "0")}</span>
                  <code>{breakableCode(stage.symbol)}</code>
                  {stage.page && <span className="poster-step-area">{stage.page.title}</span>}
                </button>
              </li>
            ))}
          </ol>
        </div>
      )}

      <footer className="poster-foot">
        {mode === "network" ? (
          <span>
            Every line is a reference recorded in the code index{repo?.commit_short ? ` at ${repo.commit_short}` : ""}.
            No model drew this map.<span className="poster-no-export"> Click a line to see one.</span>
          </span>
        ) : (
          <span>
            Area sizes are the symbols the code index{repo?.commit_short ? ` at ${repo.commit_short}` : ""} assigns
            to each area. No model drew this map.
          </span>
        )}
        {hero ? (
          <span className="poster-actions">
            <AppLink className="poster-link" href={`/${encodeURIComponent(repoId)}`}>
              Open the {repo?.repo || repoId} Wiki →
            </AppLink>
          </span>
        ) : (
        <span className="poster-actions poster-no-export">
          <button type="button" className="poster-link" onClick={exportImage} disabled={exporting}>
            {exporting ? "Saving…" : "Download image"}
          </button>
          {onOpenGraph && (
            <button type="button" className="poster-link" onClick={onOpenGraph}>
              Open the symbol-level map →
            </button>
          )}
        </span>
        )}
        <span className="poster-brand" aria-hidden>
          <b>CodeNib</b> · {typeof window !== "undefined" ? window.location.host : "demo.codenib.ai"}/{repoId}
        </span>
      </footer>
    </section>
  );
}
