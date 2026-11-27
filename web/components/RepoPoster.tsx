// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type CSSProperties } from "react";
import { createPortal } from "react-dom";

import "./RepoPoster.css";

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
import { NEUTRAL_COLOR } from "@/lib/wikiGraph";
import {
  ATLAS_MORE,
  layoutAtlas,
  layoutPoster,
  posterMode,
  tracedHops,
  TAG_W,
  type PosterEdge,
} from "@/lib/posterLayout";
import { AppLink, wikiRoute } from "@/lib/router";
import { isStaticRuntime } from "@/lib/runtime";
import { excerptRows, GAP } from "@/lib/sourceExcerpt";
import { displaySymbol } from "@/lib/symbols";
import { journeyAreas, traceFrame, traceSteps, type TraceFrame } from "@/lib/traceTimeline";
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

/** A computed colour as plain rgba(): an exported image's canvas reads
 *  color-mix() and color(srgb …) as black, so each is painted once on a
 *  pixel and read back. */
const plainColor = (() => {
  let context: CanvasRenderingContext2D | null = null;
  const cache = new Map<string, string>();
  return (value: string): string => {
    const text = value.trim();
    if (!text || text === "none" || /^(rgba?\(|#|transparent$|currentcolor$|url\()/i.test(text)) return value;
    const known = cache.get(text);
    if (known) return known;
    context ??= document.createElement("canvas").getContext("2d", { willReadFrequently: true });
    if (!context) return value;
    // The page resolves color-mix() first; the canvas then reads what the
    // page computed (Chromium's canvas does not parse color-mix itself).
    const probe = document.createElement("span");
    probe.style.color = text;
    probe.style.display = "none";
    document.body.appendChild(probe);
    const resolved = getComputedStyle(probe).color || text;
    probe.remove();
    context.clearRect(0, 0, 1, 1);
    context.fillStyle = "#000";
    context.fillStyle = resolved;
    context.fillRect(0, 0, 1, 1);
    const [r, g, b, a] = context.getImageData(0, 0, 1, 1).data;
    const plain = `rgba(${r}, ${g}, ${b}, ${Math.round((a / 255) * 1000) / 1000})`;
    cache.set(text, plain);
    return plain;
  };
})();
const COLOR_PROPS = new Set(["fill", "stroke", "color", "stop-color", "background-color", "border-color"]);

function inlineSvgStyles(root: Element): () => void {
  // Every value is read before any is written: writing first would drop a
  // group's --area before its boxes are read, and they would export black.
  // Written values are appended, so custom properties stay in place.
  const writes: Array<[Element, string]> = [];
  for (const el of root.querySelectorAll("svg *")) {
    const computed = getComputedStyle(el);
    const inline = SVG_STYLE_PROPS.map((prop) => {
      const value = computed.getPropertyValue(prop);
      return `${prop}:${COLOR_PROPS.has(prop) ? plainColor(value) : value}`;
    }).join(";");
    writes.push([el, inline]);
  }
  // The poster's own boxes and text outside the drawing, the same way.
  for (const el of [root, ...root.querySelectorAll(":not(svg *)")]) {
    if (!(el instanceof HTMLElement)) continue;
    const computed = getComputedStyle(el);
    const colors = ["color", "background-color", "border-top-color", "border-bottom-color", "border-left-color", "border-right-color"]
      .map((prop) => `${prop}:${plainColor(computed.getPropertyValue(prop))}`)
      .join(";");
    writes.push([el, colors]);
  }
  const saved: Array<[Element, string | null]> = [];
  for (const [el, inline] of writes) {
    const original = el.getAttribute("style");
    saved.push([el, original]);
    el.setAttribute("style", original ? `${original};${inline}` : inline);
  }
  return () => {
    for (const [el, style] of saved) {
      if (style == null) el.removeAttribute("style");
      else el.setAttribute("style", style);
    }
  };
}
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
  repoId: string | null;
  repo: RepoInfo | null;
  onClose: () => void;
}) {
  const anchor = link?.example.anchor ?? null;
  const [slice, setSlice] = useState<SourceSlice | null>(null);
  // The highlighter is loaded with the first source excerpt, so pages that
  // never show one (the codenib.ai hero) do not carry it.
  const [highlight, setHighlight] = useState<((code: string, language?: string) => string) | null>(null);
  useEffect(() => {
    setSlice(null);
    if (!repoId || !anchor?.file || anchor.line == null || isStaticRuntime()) return;
    let cancelled = false;
    void import("@/lib/highlight").then((module) => {
      if (!cancelled) setHighlight(() => module.highlightSource);
    });
    fetchSource(repoId, anchor.file, Math.max(1, anchor.line - 2), anchor.line + 3, repo?.base_commit)
      .then((value) => {
        if (!cancelled) setSlice(value);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [repoId, anchor?.file, anchor?.line, repo?.base_commit]);

  const from = link ? displaySymbol(link.example.source, { short: true }) : "";
  const to = link ? displaySymbol(link.example.target, { short: true }) : "";
  const url =
    anchor?.file && repo
      ? ghFileUrl(repo.repo, repo.source_url, repo.base_commit, anchor.file, anchor.line)
      : null;
  const rows = slice
    ? excerptRows(slice.content, slice.start_line, slice.definition)
    : { lines: [], numbers: [] };
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
        {plural(edge.weight, "recorded reference")}
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
          {rows.lines.length > 0 && (
            <pre>
              {rows.lines.map((line, index) => {
                const at = rows.numbers[index];
                return (
                  <span
                    key={index}
                    className={`poster-code-line${at === anchor.line ? " is-anchor" : ""}${at == null ? " is-gap" : ""}`}
                  >
                    <span className="poster-code-no">{at ?? GAP}</span>
                    {highlight ? (
                      <span dangerouslySetInnerHTML={{ __html: highlight(line, language) || " " }} />
                    ) : (
                      <span>{line || " "}</span>
                    )}
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
const counted = new Intl.NumberFormat("en-US");
/** "1 file", "12 files": one wording for every count the poster prints. */
function plural(count: number, noun: string): string {
  return `${counted.format(count)} ${noun}${count === 1 ? "" : "s"}`;
}

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
  brandHost,
  brandLabel,
  sourceRepoId,
  wikiHref,
  at,
  titleAs: Title = "h2",
  colors,
  pathLabel = "Traced call path",
  roles = false,
  capture,
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
  variant?: "page" | "hero" | "card";
  /** Host named in the image watermark; the current host by default. */
  brandHost?: string;
  /** Replaces "host/repo id" in the watermark (a saved Wiki's repository). */
  brandLabel?: string;
  /** Prepared repository whose /source serves call-site lines; null skips it. */
  sourceRepoId?: string | null;
  /** Absolute Wiki address for the hero link, when the poster is shown on
   *  another site (codenib.ai); the app's own route otherwise. */
  wikiHref?: string;
  /** Milliseconds into the traced path. Draws that frame and turns the
   *  poster's own playback off, so a video renderer can seek it. */
  at?: number;
  /** "h1" where the poster opens a page and nothing above it names the page. */
  titleAs?: "h1" | "h2";
  /** Each area's colour on the Wiki map, so the poster keys the Overview's
   *  coloured path; without it the poster keeps its own palette. */
  colors?: Map<string, string>;
  /** What the marked path is: traced calls, or the architecture's parts. */
  pathLabel?: string;
  /** The path's stops are named parts in words, not calls. */
  roles?: boolean;
  /** Internal: this is the offscreen copy a saved image is drawn from; it
   *  hands its root over once laid out. */
  capture?: (node: HTMLElement) => void;
}) {
  // "card": the static 1200x630 link-preview image; no motion, no controls.
  const card = variant === "card";
  const hero = variant === "hero" || card;
  const stages = journey?.stages ?? [];
  // How many Wiki pages an area holds, itself included.
  const pagesIn = (areaId: string): number => {
    const count = (list: WikiPageRef[]): number =>
      list.reduce((sum, page) => sum + 1 + count(page.children ?? []), 0);
    const area = pages.find((page) => page.id === areaId);
    return area ? count([area]) : 0;
  };
  // The stop numbers of an area listed outside the drawing, as its box's
  // tags would show them.
  const stopBadges = (areaId: string) => {
    const staged = stagedIn(areaId);
    if (!staged.length) return null;
    return (
      <span className="poster-row-stops" aria-label={`Stops ${staged.map(({ index }) => index + 1).join(", ")}`}>
        {staged.map(({ index }) => (
          <span key={index} className={`poster-step-no${stageNow === index ? " is-now" : ""}`}>{index + 1}</span>
        ))}
      </span>
    );
  };
  // The area a treemap cell opens: its own, or the one area a combined
  // cell names; none for "+3".
  const target = (cell: { id: string; lines: string[] }): string | null => {
    if (cell.id !== ATLAS_MORE) return cell.id;
    const ids = (cell as { moreIds?: string[] }).moreIds ?? [];
    return ids.length === 1 && !cell.lines[0]?.startsWith("+") ? ids[0] : null;
  };
  // An area's map colour as --area, for its box, its path and its tags.
  const areaStyle = (areaId: string): CSSProperties | undefined => {
    const color = colors?.get(areaId);
    return color ? ({ "--area": color } as CSSProperties) : undefined;
  };
  // Kept while its content holds: the page re-renders (and refetches its
  // tree every 15 s) with new arrays, which would restart the trace.
  const stageAreas = useStableValue(journeyAreas(stages, pages), (areas) => areas.map((area) => area ?? "").join("\u0000"));
  const { hops } = useMemo(() => tracedHops(stageAreas), [stageAreas]);
  const stopTags = useMemo(() => {
    const counts = new Map<string, number>();
    for (const area of stageAreas) if (area) counts.set(area, (counts.get(area) ?? 0) + 1);
    return counts;
  }, [stageAreas]);
  // Lay out at the canvas's real width so text is drawn at its CSS size
  // instead of being scaled with the drawing.
  const canvasRef = useRef<HTMLDivElement | null>(null);
  const [canvasWidth, setCanvasWidth] = useState(960);
  useLayoutEffect(() => {
    const node = canvasRef.current;
    if (!node) return;
    // Measure the content box before the first paint. The 960px fallback
    // otherwise paints a short, scaled map before narrow columns reflow it.
    const style = getComputedStyle(node);
    const width = node.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    setCanvasWidth(Math.round(Math.max(280, width)));
    if (typeof ResizeObserver === "undefined") return;
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
  // An area the path enters is shown even with no symbols mapped to it, so
  // no stop number goes missing from the poster.
  const shownAreas = useMemo(
    () => map.areas.filter((area) => area.symbols > 0 || stopTags.has(area.id)),
    [map, stopTags],
  );
  const isolated = mode === "network" ? shownAreas.filter((area) => !linked.has(area.id)) : [];
  const layout = useMemo(
    () =>
      mode === "network"
        ? layoutPoster(
            map.areas.filter((area) => linked.has(area.id)),
            map.links,
            { traced: hops, width: canvasWidth, tags: stopTags },
          )
        : null,
    [map, mode, linked, hops, canvasWidth, stopTags],
  );
  // Areas with a hue of their own on the map; in the Overview, the others
  // wait under the picture so grey there means only "listed below".
  const huedAreas = useMemo(
    () =>
      colors
        ? new Set([...colors.entries()].filter(([, color]) => color !== NEUTRAL_COLOR).map(([id]) => id))
        : undefined,
    [colors],
  );
  const atlas = useMemo(
    () => (mode === "atlas" ? layoutAtlas(map.areas, canvasWidth, stopTags, huedAreas) : null),
    [map, mode, canvasWidth, stopTags, huedAreas],
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
      // Every area the Wiki has, as its map draws them, code or not.
      areas: map.areas.length,
      symbols: shownAreas.reduce((sum, area) => sum + area.symbols, 0),
      // The repository's own count only: a sum over areas would show first
      // and then change under the reader.
      files: repo?.file_count ?? null,
      references: map.links.reduce((sum, link) => sum + link.weight, 0),
    }),
    [shownAreas, map, repo?.file_count],
  );

  const [hoverNode, setHoverNode] = useState<string | null>(null);
  const [hoverEdge, setHoverEdge] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [activeStage, setActiveStage] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);
  // The copy is drawn in its saved form; the live poster never changes.
  const exporting = capture != null;
  const [saving, setSaving] = useState<number | null>(null);
  const pathRefs = useRef(new Map<string, SVGPathElement>());
  const pulseRef = useRef<SVGCircleElement | null>(null);
  const rootRef = useRef<HTMLElement | null>(null);
  const played = useRef(false);
  const autoplayRef = useRef<number | null>(null);

  // One timeline: rest on each stage, travel along the hop when the next
  // stage lives in another area.
  const timeline = useMemo(() => traceSteps(stageAreas), [stageAreas]);
  const seeking = at != null;
  const seekFrame = seeking ? traceFrame(timeline, at) : null;

  // The pulse moves outside React so playback does not re-render the map.
  const placePulse = (frame: TraceFrame | null) => {
    const pulse = pulseRef.current;
    if (!pulse) return;
    const path = frame?.edge ? pathRefs.current.get(frame.edge) : undefined;
    if (!frame || !path) {
      pulse.setAttribute("opacity", "0");
      return;
    }
    const point = path.getPointAtLength(path.getTotalLength() * frame.progress);
    pulse.setAttribute("cx", String(point.x));
    pulse.setAttribute("cy", String(point.y));
    pulse.setAttribute("opacity", "1");
  };

  useEffect(() => {
    if (!playing || seeking || timeline.length === 0) return;
    let handle = 0;
    // The clock starts at the first frame's own time: performance.now()
    // can run ahead of it, and a negative time draws nothing.
    let started: number | null = null;
    const tick = (now: number) => {
      started ??= now;
      const frame = traceFrame(timeline, now - started);
      placePulse(frame);
      if (!frame) {
        setPlaying(false);
        setActiveStage(null);
        return;
      }
      setActiveStage((current) => (current === frame.stage ? current : frame.stage));
      handle = requestAnimationFrame(tick);
    };
    handle = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(handle);
  }, [playing, seeking, timeline]);

  useLayoutEffect(() => {
    if (seeking) placePulse(seekFrame);
  });

  // Play once, the first time the poster is on screen, unless the reader
  // asked for less motion.
  useEffect(() => {
    const node = rootRef.current;
    if (!node || played.current || timeline.length === 0 || card || seeking || exporting) return;
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting) && !played.current) {
        played.current = true;
        autoplayRef.current = window.setTimeout(() => setPlaying(true), 500);
        observer.disconnect();
      }
    }, { threshold: 0.4 });
    observer.observe(node);
    return () => observer.disconnect();
  }, [timeline.length]);

  // Save the poster as an image a reader can post. The copy carries the
  // page's address; controls that only work on the page are left out.
  // The image is drawn from an offscreen copy at the poster's width, at
  // rest and in its saved form, so the page itself never changes: no
  // flash, no hover, and a trace in progress plays on.
  const exportImage = () => {
    const node = rootRef.current;
    if (!node || saving != null) return;
    setSaving(Math.round(node.getBoundingClientRect().width));
  };
  const captureCopy = async (node: HTMLElement) => {
    try {
      const { toPng } = await import("html-to-image");
      const restore = inlineSvgStyles(node);
      let url: string;
      try {
        url = await toPng(node, {
          pixelRatio: 2,
          // The poster's own margin would shift the copy inside its frame.
          style: { margin: "0" },
          // The theme's own ground: a light page exports a light poster.
          backgroundColor: plainColor(getComputedStyle(rootRef.current ?? node).backgroundColor) || "#070b14",
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
      setSaving(null);
    }
  };

  // The copy hands itself over once laid out and its fonts are ready.
  useEffect(() => {
    if (!capture) return;
    let cancelled = false;
    void (async () => {
      await document.fonts?.ready;
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      if (!cancelled && rootRef.current) capture(rootRef.current);
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const stageNow = seeking ? seekFrame?.stage ?? null : activeStage;
  const focusArea = stageNow != null ? stageAreas[stageNow] ?? null : null;
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
  // The traced path's stop numbers, as small tags inside the box's top-right
  // corner rather than badges hanging off it.
  const pins = (areaId: string, right: number) => {
    const staged = stagedIn(areaId);
    if (staged.length === 0) return null;
    return (
      <g className="poster-node-stages">
        {staged.map(({ index }, i) => (
          <g key={index} transform={`translate(${right - 8 - (staged.length - i) * TAG_W} 7)`}>
            <rect width="14" height="14" rx="2" className={stageNow === index ? "is-now" : ""} />
            <text x="7" y="10.4" textAnchor="middle">{index + 1}</text>
          </g>
        ))}
      </g>
    );
  };
  const hasTrace = stages.length > 1;
  // The snapshot's commit, its room held until the repository's record
  // arrives so the footer never rewraps under the reader.
  const commitAt = repo ? (
    repo.commit_short ? ` at ${repo.commit_short}` : ""
  ) : (
    <span aria-hidden="true" style={{ visibility: "hidden" }}> at 0000000</span>
  );
  // The address names the repository ("psf__requests"), so the title is
  // right before the repository's record arrives.
  const slug = repo?.repo || repoId.replace("__", "/");
  const [owner, name] = slug.includes("/")
    ? [slug.split("/")[0] + "/", slug.split("/").slice(1).join("/")]
    : ["", slug];

  const copy =
    saving != null && typeof document !== "undefined"
      ? createPortal(
          <div className="poster-export-stage" aria-hidden="true" style={{ width: saving }}>
            <RepoPoster
              repoId={repoId}
              repo={repo}
              map={map}
              pages={pages}
              journey={journey}
              lead={lead}
              variant={variant}
              brandHost={brandHost}
              brandLabel={brandLabel}
              titleAs={Title}
              colors={colors}
              pathLabel={pathLabel}
              roles={roles}
              capture={captureCopy}
            />
          </div>,
          rootRef.current?.parentElement ?? document.body,
        )
      : null;

  return (
    <>
    {copy}
    <section
      className={`repo-poster${hero ? " is-hero" : ""}${card ? " is-card" : ""}${exporting ? " is-exporting" : ""}${colors ? " has-colours" : ""}`}
      ref={rootRef}
      aria-label={`System map of ${repo?.repo || repoId}`}
    >
      <header className="poster-head">
        <Title className="poster-title">
          <span className="poster-owner">{owner}</span>
          <wbr />
          {name}
        </Title>
        {lead && !hero && <p className="poster-lead">{leadNodes(lead)}</p>}
        <dl className="poster-stats">
          <div><dt>areas</dt><dd>{number.format(totals.areas)}</dd></div>
          <div><dt>symbols</dt><dd>{number.format(totals.symbols)}</dd></div>
          {/* Until the repository's count arrives its place is held, so the
              line never rewraps under the reader. */}
          <div
            aria-hidden={totals.files === null ? true : undefined}
            style={totals.files === null ? { visibility: "hidden" } : undefined}
          >
            <dt>files</dt>
            <dd>{number.format(totals.files ?? shownAreas.reduce((sum, area) => sum + area.files, 0))}</dd>
          </div>
          {totals.references > 0 && (
            <div><dt>recorded references</dt><dd>{number.format(totals.references)}</dd></div>
          )}
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
            {atlas.cells.map((cell) => {
              const lineHeight = Math.round(cell.fontSize * 1.25);
              const lit = hoverNode === cell.id || focusArea === cell.id;
              return (
                <g
                  key={cell.id}
                  className={`poster-node poster-cell${lit ? " is-lit" : ""}${
                    focusArea === cell.id ? " is-active" : ""
                  }${colors?.get(target(cell) ?? "") ? " has-area" : ""}${cell.id === ATLAS_MORE && !target(cell) ? " is-more" : ""}`}
                  style={areaStyle(target(cell) ?? cell.id)}
                  transform={`translate(${cell.x} ${cell.y})`}
                  onMouseEnter={() => setHoverNode(cell.id)}
                  onMouseLeave={() => setHoverNode(null)}
                  // The combined cell stands for the rows listed below it; one
                  // area named in it is that area, and opens it.
                  onClick={target(cell) ? () => onPick?.(target(cell)!) : undefined}
                  role={target(cell) ? "link" : undefined}
                  tabIndex={target(cell) ? 0 : undefined}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && target(cell)) onPick?.(target(cell)!);
                  }}
                >
                  <title>
                    {cell.id === ATLAS_MORE && !target(cell)
                      ? `${plural((cell as { moreCount?: number }).moreCount ?? 1, "smaller area")}, listed below: ${plural(cell.symbols, "symbol")}`
                      : `${cell.title}: ${plural(cell.symbols, "symbol")} in ${plural(cell.files, "file")}`}
                  </title>
                  <rect
                    className="poster-node-box"
                    width={cell.w}
                    height={cell.h}
                    rx="4"
                    style={{ fillOpacity: 0.55 + Math.min(0.45, cell.share * 2.2) }}
                  />
                  {cell.vertical
                    ? cell.lines.slice(0, 2).map((line, index, shown) => (
                        <text
                          key={`vertical-${index}`}
                          className="poster-node-title"
                          transform={`translate(${Math.round(
                            cell.w / 2 + cell.fontSize * 0.36 + (index - (shown.length - 1) / 2) * lineHeight,
                          )} ${cell.h - 12}) rotate(-90)`}
                          style={{ fontSize: cell.fontSize }}
                        >
                          {line}
                        </text>
                      ))
                    : cell.lines.map((line, index) => (
                        <text
                          key={index}
                          className="poster-node-title"
                          x="12"
                          y={10 + cell.titleOffset + cell.fontSize + index * lineHeight}
                          style={{ fontSize: cell.fontSize }}
                        >
                          {line}
                        </text>
                      ))}
                  {!cell.vertical && cell.lines.length > 0 && cell.w >= 150 && cell.h >= cell.titleOffset + cell.lines.length * lineHeight + 44 && (
                    <text
                      className="poster-node-meta"
                      x="12"
                      y={10 + cell.titleOffset + cell.fontSize + (cell.lines.length - 1) * lineHeight + 22}
                    >
                      {number.format(cell.symbols)} symbol{cell.symbols === 1 ? "" : "s"} · {cell.files} file{cell.files === 1 ? "" : "s"}
                    </text>
                  )}
                  <text className="poster-cell-share" x={cell.w - 12} y={cell.h - 12} textAnchor="end">
                    {!cell.vertical && !cell.noShare && cell.w >= 64 && cell.h >= 40 ? `${Math.round(cell.share * 100)}%` : ""}
                  </text>
                  {pins(cell.id, cell.w)}
                </g>
              );
            })}
          </svg>
        )}
        {atlas?.rest && atlas.rest.length > 0 && (
          // Areas too small to name inside the picture, each on its own line.
          <ul className="poster-rest" aria-label="Smaller areas">
            {atlas.rest.map((area) => (
              <li key={area.id}>
                <button type="button" style={areaStyle(area.id)} onClick={() => onPick?.(area.id)}>
                  <span className="poster-rest-name">
                    {area.title}
                    {stopBadges(area.id)}
                  </span>
                  <span className="poster-rest-share">
                    {area.symbols > 0 ? `${Math.max(1, Math.round(area.share * 100))}%` : plural(pagesIn(area.id), "page")}
                  </span>
                </button>
              </li>
            ))}
          </ul>
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
            <marker
              id="poster-arrow"
              viewBox="0 0 8 8"
              refX="7.5"
              refY="4"
              markerWidth="7"
              markerHeight="7"
              markerUnits="userSpaceOnUse"
              orient="auto"
            >
              <path className="poster-arrow" d="M0 0.6 L8 4 L0 7.4 z" />
            </marker>
            <marker
              id="poster-arrow-traced"
              viewBox="0 0 8 8"
              refX="7.5"
              refY="4"
              markerWidth="8"
              markerHeight="8"
              markerUnits="userSpaceOnUse"
              orient="auto"
            >
              <path className="poster-arrow is-traced" d="M0 0.6 L8 4 L0 7.4 z" />
            </marker>
          </defs>
          <g className="poster-edges">
            {/* Traced links last, so the path is never under another line. */}
            {[...layout.edges].sort((a, b) => Number(a.traced) - Number(b.traced)).map((edge) => {
              const key = edgeKey(edge);
              const dim =
                (litNode != null && !touches(edge, litNode)) ||
                (hoverEdge != null && hoverEdge !== key);
              const hot = hoverEdge === key || selected === key;
              return (
                <g
                  key={key}
                  data-source={edge.source}
                  data-target={edge.target}
                  className={`poster-edge${edge.traced ? " is-traced" : ""}${dim ? " is-dim" : ""}${hot ? " is-hot" : ""}${colors?.get(edge.target) ? " has-area" : ""}`}
                  style={areaStyle(edge.target)}
                >
                  <path
                    className="poster-edge-line"
                    d={edge.d}
                    strokeWidth={edge.traced ? Math.max(2, edge.width) : edge.width}
                    markerEnd={`url(#poster-arrow${edge.traced ? "-traced" : ""})`}
                    ref={(node) => {
                      if (node) pathRefs.current.set(key, node);
                      else pathRefs.current.delete(key);
                    }}
                  />
                  <path
                    className="poster-edge-hit"
                    d={edge.d}
                    onMouseEnter={() => setHoverEdge(key)}
                    onMouseLeave={() => setHoverEdge(null)}
                    onClick={() => setSelected((current) => (current === key ? null : key))}
                  >
                    <title>
                      {`${titles.get(edge.source)} → ${titles.get(edge.target)}: ${plural(edge.weight, "reference")}`}
                    </title>
                  </path>
                </g>
              );
            })}
          </g>

          <circle ref={pulseRef} className="poster-pulse" r="5" opacity="0" />

          <g className="poster-nodes">
            {layout.nodes.map((node) => {
              const lit = litNode === node.id || focusArea === node.id;
              const dim = litNode != null && !lit && !layout.edges.some(
                (edge) => touches(edge, litNode) && touches(edge, node.id),
              );
              return (
                <g
                  key={node.id}
                  data-area={node.id}
                  className={`poster-node${lit ? " is-lit" : ""}${dim ? " is-dim" : ""}${
                    focusArea === node.id ? " is-active" : ""
                  }${colors?.get(node.id) ? " has-area" : ""}`}
                  style={areaStyle(node.id)}
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
                  <rect className="poster-node-box" width={node.w} height={node.h} rx="4" />
                  {node.lines.map((line, index) => (
                    <text key={index} className="poster-node-title" x="14" y={30 + index * 18}>
                      {line}
                    </text>
                  ))}
                  <text className="poster-node-meta" x="14" y={30 + node.lines.length * 18 + 6}>
                    {number.format(node.symbols)} symbol{node.symbols === 1 ? "" : "s"} · {node.files} file{node.files === 1 ? "" : "s"}
                  </text>
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
                <rect x={-(label.length * 3.6 + 10)} y="-11" width={label.length * 7.2 + 20} height="22" rx="3" />
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
            repoId={sourceRepoId === undefined ? repoId : sourceRepoId}
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
                className={`poster-chip is-area${focusArea === area.id ? " is-now" : ""}`}
                style={areaStyle(area.id)}
                onClick={() => onPick?.(area.id)}
              >
                <span className="poster-chip-name">
                  {area.title}
                  {stopBadges(area.id)}
                </span>
                {/* An area with no symbols mapped still has its pages. */}
                <span>{area.symbols > 0 ? plural(area.symbols, "symbol") : plural(pagesIn(area.id), "page")}</span>
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
            <span className="poster-trace-label">{pathLabel}</span>
            <span className="poster-trace-title">
              {leadNodes(
                journey!.title.replace(/`([^`]+)`/g, (_match, symbol: string) =>
                  `\`${displaySymbol(symbol, { short: true })}\``,
                ),
              )}
            </span>
            <button
              type="button"
              className="poster-play poster-no-export"
              onClick={() => {
                setSelected(null);
                setPlaying(false);
                window.requestAnimationFrame(() => setPlaying(true));
              }}
            >
              {playing ? (
                "Playing…"
              ) : (
                <>
                  <svg viewBox="0 0 10 10" aria-hidden>
                    <path d="M2 1.2v7.6L8.6 5z" />
                  </svg>
                  Play
                </>
              )}
            </button>
          </div>
          {/* One line of numbered stops, the same numbers the map shows. */}
          <ol className="poster-steps">
            {stages.map((stage, index) => (
              <li key={stage.index}>
                {(() => {
                  const className = `poster-step${stageNow === index ? " is-now" : ""}${
                    stageAreas[index] && colors?.get(stageAreas[index]!) ? " has-area" : ""
                  }`;
                  const style = stageAreas[index] ? areaStyle(stageAreas[index]!) : undefined;
                  const body = (
                    <>
                      <span className="poster-step-no">{index + 1}</span>
                      {roles ? (
                        <span className="poster-step-role">{stage.symbol}</span>
                      ) : (
                        <code title={stage.symbol}>
                          {/* Saved, a name stays whole: the copy's break
                              points split "run_/with_error_handler()". */}
                          {exporting
                            ? displaySymbol(stage.symbol, { short: true })
                            : breakableCode(displaySymbol(stage.symbol, { short: true }))}
                        </code>
                      )}
                    </>
                  );
                  // The copy is drawn with its own font metrics, so a saved
                  // image gets text, not a box sized on the page.
                  if (exporting) return <span className={className} style={style}>{body}</span>;
                  return (
                    <button
                      type="button"
                      className={className}
                      style={style}
                      onMouseEnter={() => !playing && setActiveStage(index)}
                      onMouseLeave={() => !playing && setActiveStage(null)}
                      onClick={() => stage.page && onPick?.(stage.page.id)}
                      title={stage.page ? `Open ${stage.page.title}` : undefined}
                    >
                      {body}
                    </button>
                  );
                })()}
              </li>
            ))}
          </ol>
        </div>
      )}

      <footer className="poster-foot">
        {mode === "network" ? (
          <span>
            Every line is a reference recorded in the code index{commitAt}.
            No model drew this map.<span className="poster-no-export"> Click a line to see one.</span>
          </span>
        ) : (
          <span>
            Area sizes are the symbols the code index{commitAt} assigns
            to each area. No model drew this map.
          </span>
        )}
        {hero ? (
          <span className="poster-actions">
            {wikiHref ? (
              <a className="poster-link" href={wikiHref}>
                Open the {slug} Wiki
              </a>
            ) : (
              <AppLink className="poster-link" href={wikiRoute(repoId)}>
                Open the {slug} Wiki
              </AppLink>
            )}
          </span>
        ) : (
        <span className="poster-actions poster-no-export">
          <button type="button" className="poster-link" onClick={exportImage} disabled={saving != null}>
            {saving != null ? "Saving…" : "Download image"}
          </button>
          {onOpenGraph && (
            <button type="button" className="poster-link" onClick={onOpenGraph}>
              Open the symbol-level map
              <svg className="poster-link-arrow" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M5 12h14M13 6l6 6-6 6" />
              </svg>
            </button>
          )}
        </span>
        )}
        <span className="poster-brand" aria-hidden>
          <b>CodeNib</b> ·{" "}
          {brandLabel ?? (
            <>
              {/* The address may break after its host, never leaving
                  "CodeNib ·" alone on a line. */}
              {brandHost ?? (typeof window !== "undefined" ? window.location.host : "demo.codenib.ai")}/
              <wbr />
              {repoId}
            </>
          )}
        </span>
      </footer>
    </section>
    </>
  );
}

/** The same value while its key holds, so a re-render with equal content
 *  keeps every memo and effect built on it. */
function useStableValue<T>(value: T, keyOf: (value: T) => string): T {
  const key = keyOf(value);
  const ref = useRef({ key, value });
  if (ref.current.key !== key) ref.current = { key, value };
  return ref.current.value;
}
