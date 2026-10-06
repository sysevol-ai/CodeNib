// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { useLayoutEffect, useRef, useState } from "react";

import RepoPoster from "@/components/RepoPoster";
import type { CapturedMap } from "@/landing-hero/LandingMap";
import type { RepoInfo, WikiAreaLink } from "@/lib/api";
import { highlightSource, languageForFile } from "@/lib/highlight";
import { posterMode } from "@/lib/posterLayout";
import { splitSymbolLabel } from "@/lib/symbols";
import { journeyAreas, traceDuration, traceFrame, traceSteps, type TraceStep } from "@/lib/traceTimeline";
import { extractJourney, splitWikiMarkdown, type Journey } from "@/lib/wikiPresentation";

import "./RepoVideo.css";

export const WIDTH = 1920;
export const HEIGHT = 1080;

/**
 * "short": map, one line of code, address; about ten seconds, one sentence
 * per beat. "story": a hook question and the traced path before the push.
 */
export type Cut = "short" | "story";

/** Source lines around the call site, read at the indexed commit. */
export interface Excerpt {
  start: number;
  lines: string[];
}

/** A recorded reference the film can stop on, with the line the index anchored it to. */
export interface CallSite {
  source: string;
  target: string;
  from: string;
  to: string;
  file: string;
  line: number;
}

type Span = readonly [number, number];

interface Cues {
  hook: Span | null;
  map: Span;
  trace: number | null;
  push: Span;
  card: Span;
  line: Span;
  end: Span;
  total: number;
}

export interface VideoPlan {
  cut: Cut;
  journey: Journey | null;
  /** Candidates in order of preference; the renderer takes the first whose source line checks out. */
  sites: CallSite[];
  steps: TraceStep[];
  cues: Cues;
}

function pushCues(at: number, hold: number): Pick<Cues, "push" | "card" | "line" | "end"> & { total: number } {
  const push = [at, at + 1500] as const;
  const card = [push[1] - 400, push[1] + 400] as const;
  const line = [card[1], card[1] + 600] as const;
  const end = [line[1] + hold, line[1] + hold + 700] as const;
  return { push, card, line, end, total: end[1] + 2300 };
}

function storyCues(traceMs: number): Cues {
  const trace = 3600;
  return { hook: [0, 2900], map: [2400, 3300], trace, ...pushCues(trace + traceMs + 400, 3200) };
}

function shortCues(): Cues {
  return { hook: null, map: [0, 600], trace: null, ...pushCues(2800, 2600) };
}

/** Everything the film shows, chosen from the captured map without a model. */
export function planVideo(captured: CapturedMap, cut: Cut): VideoPlan | null {
  // The camera flies at a drawn edge; an atlas draws none.
  if (posterMode(captured.map.areas, captured.map.links).mode !== "network") return null;
  const journey = extractJourney(splitWikiMarkdown(captured.overview).body).journey;
  if (cut === "story" && !journey) return null;
  const sites: CallSite[] = [];
  const add = (link: WikiAreaLink | undefined) => {
    const anchor = link?.example.anchor;
    if (!link || !anchor?.file || anchor.line == null) return;
    if (sites.some((site) => site.source === link.source && site.target === link.target)) return;
    sites.push({
      source: link.source,
      target: link.target,
      from: splitSymbolLabel(link.example.source).symbol,
      to: splitSymbolLabel(link.example.target).symbol,
      file: anchor.file,
      line: anchor.line,
    });
  };
  // A reference that is itself a hop of the traced path comes first, then
  // the heaviest recorded references.
  const areas = journey ? journeyAreas(journey.stages, captured.pages) : [];
  journey?.stages.slice(1).forEach((stage, index) => {
    const from = journey.stages[index].symbol;
    add(
      captured.map.links.find(
        (link) =>
          link.source === areas[index] &&
          link.target === areas[index + 1] &&
          splitSymbolLabel(link.example.source).symbol === from &&
          splitSymbolLabel(link.example.target).symbol === stage.symbol,
      ),
    );
  });
  [...captured.map.links].sort((a, b) => b.weight - a.weight).forEach(add);
  if (sites.length === 0) return null;
  const steps = cut === "story" ? traceSteps(areas) : [];
  return {
    cut,
    journey: cut === "story" ? journey : null,
    sites,
    steps,
    cues: cut === "story" ? storyCues(traceDuration(steps)) : shortCues(),
  };
}

/** The excerpt as shown: up to `before` lines of lead-in, blank edges
 *  trimmed and the shared indent removed. */
function shownRows(excerpt: Excerpt, line: number, before: number): Array<{ no: number; text: string }> {
  const rows = excerpt.lines
    .map((text, index) => ({ no: excerpt.start + index, text: text.replace(/\t/g, "    ") }))
    .filter((row) => row.no >= line - before);
  while (rows.length && !rows[0].text.trim() && rows[0].no !== line) rows.shift();
  while (rows.length && !rows[rows.length - 1].text.trim() && rows[rows.length - 1].no !== line) rows.pop();
  const lead = (text: string) => text.length - text.trimStart().length;
  const indent = Math.min(...rows.filter((row) => row.text.trim()).map((row) => lead(row.text)));
  return rows.map((row) => ({ ...row, text: row.text.slice(Math.min(indent, lead(row.text))) }));
}

const clamp01 = (x: number) => Math.min(1, Math.max(0, x));
const span = (t: number, [a, b]: Span) => clamp01((t - a) / (b - a));
const ease = (x: number) => (x < 0.5 ? 4 * x * x * x : 1 - (-2 * x + 2) ** 3 / 2);
const lerp = (a: number, b: number, p: number) => a + (b - a) * p;

interface Geometry {
  poster: { x: number; y: number; w: number; h: number };
  mid: { x: number; y: number };
}

// Where the call site's edge comes to rest once the camera has pushed in.
const FOCUS = { x: 600, y: 540, scale: 2.3 };
const CARD_LEFT = 860;

/**
 * A system-map film as a pure function of `t`: the map, a push into one
 * recorded reference and the source line it was recorded at, then the
 * address. A renderer seeks it frame by frame.
 */
export default function RepoVideo({
  captured,
  plan,
  site,
  t,
  excerpt,
  host,
}: {
  captured: CapturedMap;
  plan: VideoPlan;
  site: CallSite;
  t: number;
  excerpt: Excerpt | null;
  host: string;
}) {
  const { cut, journey, cues } = plan;
  const story = cut === "story";
  const layerRef = useRef<HTMLDivElement | null>(null);
  const [geo, setGeo] = useState<Geometry | null>(null);

  // Measured in the camera layer's own units, so it holds at any zoom.
  useLayoutEffect(() => {
    const layer = layerRef.current;
    const poster = layer?.querySelector<HTMLElement>(".repo-poster");
    const path = layer?.querySelector<SVGPathElement>(
      `.poster-edge[data-source="${site.source}"][data-target="${site.target}"] .poster-edge-line`,
    );
    if (!layer || !poster || !path) return;
    const base = layer.getBoundingClientRect();
    const scale = base.width / layer.offsetWidth || 1;
    const box = poster.getBoundingClientRect();
    const point = path.getPointAtLength(path.getTotalLength() / 2);
    const mid = new DOMPoint(point.x, point.y).matrixTransform(path.getScreenCTM() ?? undefined);
    const next: Geometry = {
      poster: {
        x: (box.left - base.left) / scale,
        y: (box.top - base.top) / scale,
        w: box.width / scale,
        h: box.height / scale,
      },
      mid: { x: (mid.x - base.left) / scale, y: (mid.y - base.top) / scale },
    };
    const moved =
      !geo ||
      Math.abs(geo.poster.w - next.poster.w) > 0.5 ||
      Math.abs(geo.poster.h - next.poster.h) > 0.5 ||
      Math.abs(geo.mid.x - next.mid.x) > 0.5 ||
      Math.abs(geo.mid.y - next.mid.y) > 0.5;
    if (moved) setGeo(next);
  });

  const push = ease(span(t, cues.push));
  const mapIn = ease(span(t, cues.map));
  let camera = "none";
  let anchor = { x: FOCUS.x, y: FOCUS.y };
  if (geo) {
    const { poster, mid } = geo;
    // Leave a bottom band for the caption or the stage readout.
    const fit = Math.min(1.2, (HEIGHT - 300) / poster.h, (WIDTH - 160) / poster.w);
    const tx0 = WIDTH / 2 - (poster.x + poster.w / 2) * fit;
    const ty0 = (HEIGHT - 170) / 2 - (poster.y + poster.h / 2) * fit + 24 * (1 - mapIn);
    const scale = lerp(fit, FOCUS.scale, push);
    // Move the call site's screen position, not the layer origin, so the
    // camera flies straight at it while zooming.
    anchor = {
      x: lerp(mid.x * fit + tx0, FOCUS.x, push),
      y: lerp(mid.y * fit + ty0, FOCUS.y, push),
    };
    camera = `translate(${anchor.x - mid.x * scale}px, ${anchor.y - mid.y * scale}px) scale(${scale})`;
  }

  const hook = cues.hook;
  const hookIn = (delay: number) => ease(span(t, [delay, delay + 600]));
  const hookOut = hook ? 1 - ease(span(t, [hook[1] - 500, hook[1]])) : 0;
  const card = ease(span(t, cues.card));
  const lineLit = ease(span(t, cues.line));
  const end = ease(span(t, cues.end));
  const focused = t >= cues.push[0];
  const tracing = cues.trace != null && t >= cues.trace;
  const now = cues.trace != null ? traceFrame(plan.steps, t - cues.trace) : null;
  const stageIndex = now?.stage ?? (tracing && journey ? journey.stages.length - 1 : 0);
  const stage = journey?.stages[stageIndex];
  const repoName = captured.repo.repo;
  const commit = captured.repo.commit_short;
  const fileName = site.file.split("/").pop();
  const language = languageForFile(site.file);
  const mapCaption = 1 - ease(span(t, [cues.push[0], cues.push[0] + 400]));
  const lineCaption = ease(span(t, [cues.push[0] + 600, cues.push[1]])) * (1 - end);

  return (
    <div
      className={`rv${focused ? " is-focus" : ""}${story ? "" : " is-short"}`}
      style={
        {
          width: WIDTH,
          height: HEIGHT,
          "--rv-clock": t,
          "--rv-dim": lerp(1, 0.06, push),
        } as React.CSSProperties
      }
    >
      <style>{`
        .rv.is-focus .poster-edge[data-source="${site.source}"][data-target="${site.target}"],
        .rv.is-focus .poster-node[data-area="${site.source}"],
        .rv.is-focus .poster-node[data-area="${site.target}"] { opacity: 1; }
        .rv.is-focus .poster-edge[data-source="${site.source}"][data-target="${site.target}"] .poster-edge-line {
          stroke: #a5f3fc;
        }
      `}</style>

      <div className="rv-camera" ref={layerRef} style={{ transform: camera, opacity: mapIn * (1 - end) }}>
        <RepoPoster
          variant="hero"
          repoId={captured.id}
          repo={{ id: captured.id, ...captured.repo } as RepoInfo}
          map={captured.map}
          pages={captured.pages}
          journey={journey}
          sourceRepoId={null}
          brandHost={host}
          at={cues.trace != null ? t - cues.trace : -1}
        />
      </div>

      {hook && hookOut > 0 && (
        <div className="rv-hook" style={{ opacity: hookOut }}>
          <div className="rv-eyebrow" style={{ opacity: hookIn(0), transform: `translateY(${12 * (1 - hookIn(0))}px)` }}>
            {repoName}
            {commit && (
              <>
                <span className="rv-sep">·</span>indexed at <span className="rv-mono">{commit}</span>
              </>
            )}
          </div>
          <h1 style={{ opacity: hookIn(250), transform: `translateY(${18 * (1 - hookIn(250))}px)` }}>
            What runs when you call
          </h1>
          <h1 style={{ opacity: hookIn(550), transform: `translateY(${18 * (1 - hookIn(550))}px)` }}>
            <code>{journey?.stages[0]?.symbol}</code>?
          </h1>
        </div>
      )}

      {tracing && t < cues.push[0] + 500 && stage && (
        <div className="rv-stage" style={{ opacity: 1 - span(t, [cues.push[0], cues.push[0] + 500]) }}>
          <span className="rv-stage-no">{String(stageIndex + 1).padStart(2, "0")}</span>
          <code>{stage.symbol}</code>
          {stage.page && <span className="rv-stage-area">{stage.page.title}</span>}
        </div>
      )}

      {!story && mapCaption > 0 && (
        <div className="rv-caption" style={{ opacity: ease(span(t, [200, 800])) * mapCaption }}>
          Mapped from the code index. No model drew it.
        </div>
      )}

      {card > 0 && (
        <svg className="rv-wire" width={WIDTH} height={HEIGHT} style={{ opacity: 1 - end }}>
          <circle cx={anchor.x} cy={anchor.y} r={7 + 4 * card} className="rv-wire-dot" />
          <line
            x1={anchor.x}
            y1={anchor.y}
            x2={lerp(anchor.x, CARD_LEFT, card)}
            y2={anchor.y}
            className="rv-wire-line"
          />
        </svg>
      )}

      {card > 0 && (
        <div
          className="rv-code"
          style={{ opacity: card * (1 - end), transform: `translate(${CARD_LEFT + 36 * (1 - card)}px, -50%)` }}
        >
          <div className="rv-code-head">
            <span className="rv-mono">
              {site.file.slice(0, site.file.length - (fileName?.length ?? 0))}
              <b>{fileName}</b>
              <span className="rv-code-line-no">:{site.line}</span>
            </span>
            {story && <span className="rv-code-kind">recorded reference</span>}
          </div>
          <div className="rv-code-pair">
            <code>{site.from}</code>
            <span aria-hidden>→</span>
            <code>{site.to}</code>
          </div>
          <pre>
            {(excerpt ? shownRows(excerpt, site.line, story ? 5 : 2) : [{ no: 0, text: "(source not loaded)" }]).map(({ no, text }, index) => {
              const hit = no === site.line;
              return (
                <span key={index} className={`rv-code-row${hit ? " is-anchor" : ""}`}>
                  {hit && <span className="rv-code-mark" style={{ transform: `scaleX(${lineLit})` }} />}
                  <span className="rv-code-no">{no || ""}</span>
                  <span
                    className="rv-code-text"
                    dangerouslySetInnerHTML={{ __html: (excerpt && highlightSource(text, language)) || text || " " }}
                  />
                </span>
              );
            })}
          </pre>
          {story && (
            <div className="rv-code-foot">
              Read from {repoName}
              {commit && (
                <>
                  {" "}at <span className="rv-mono">{commit}</span>
                </>
              )}
              , the commit the index was built from.
            </div>
          )}
        </div>
      )}

      {focused && lineCaption > 0 && (
        <div className="rv-caption" style={{ opacity: lineCaption }}>
          {story
            ? "Each line on the map is a reference the code index recorded. Here is one."
            : "Click a line on the map, get the line of code."}
        </div>
      )}

      {end > 0 &&
        (story ? (
          <div className="rv-end" style={{ opacity: end }}>
            <p className="rv-end-claim">
              Every line on that map is a reference recorded in the code index{commit ? ` at ${commit}` : ""}.
            </p>
            <p className="rv-end-accent">No model drew it.</p>
            <div className="rv-end-url rv-mono">
              {host}/{captured.id}
            </div>
            <div className="rv-end-brand">CodeNib</div>
          </div>
        ) : (
          <div className="rv-end is-short" style={{ opacity: end }}>
            <div className="rv-end-mark">
              <img src="/codenib-icon.svg" alt="" />
              CodeNib
            </div>
            <div className="rv-end-url rv-mono">
              {host}/{captured.id}
            </div>
          </div>
        ))}
    </div>
  );
}
