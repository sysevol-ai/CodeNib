// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
  type RefObject,
} from "react";

import "./ChapterCards.css";
import "./Walkthrough.css";
import {
  areaColor,
  breakName,
  citingPages,
  codeRows,
  curve,
  githubLine,
  miniAreas,
  placeBadge,
  placeLabels,
  routeLayout,
  shortAreaName,
  stepForStop,
  stepStates,
  stopArea,
  stopInfo,
  zoneOf,
  type Box,
  type Walk,
  type WalkCode,
  type WalkthroughSet,
} from "@/lib/walkthrough";

/** Generated step text carries only `<code>`; anything else is shown as text. */
function inline(html: string | undefined): { __html: string } {
  return { __html: (html ?? "").replace(/<(?!\/?code>)/g, "&lt;") };
}

const MONO = '13px "Geist Mono", ui-monospace, monospace';
const SANS = '500 13px "Geist", ui-sans-serif, system-ui, sans-serif';

/** Text widths for the SVG labels, measured with the page's own fonts once they load. */
function useMeasure(): (text: string, font: string) => number {
  const [fontsReady, setFontsReady] = useState(0);
  useEffect(() => {
    let alive = true;
    document.fonts?.ready.then(() => alive && setFontsReady((n) => n + 1));
    return () => {
      alive = false;
    };
  }, []);
  return useMemo(() => {
    const context = typeof document !== "undefined" ? document.createElement("canvas").getContext("2d") : null;
    return (text: string, font: string) => {
      if (!context) return text.length * (font === MONO ? 7.8 : 7);
      context.font = font;
      return context.measureText(text).width;
    };
    // Re-measure when the web fonts arrive.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fontsReady]);
}

function useWidth<T extends HTMLElement>(): [RefObject<T>, number] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const node = ref.current;
    if (!node) return;
    setWidth(Math.round(node.clientWidth));
    const observer = new ResizeObserver(() => setWidth(Math.round(node.clientWidth)));
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  return [ref, width];
}

/**
 * One narration height for the whole walk: the tallest step's heading and prose,
 * plus the tallest excerpt (352 to 520px) and the controls, never under 760px.
 * Nothing moves between steps and no step's text has to scroll. Measured at the
 * story's own width.
 */
function useStoryHeight(walk: Walk) {
  const storyRef = useRef<HTMLElement>(null);
  const measureRef = useRef<HTMLDivElement>(null);
  const codeRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState<{ height: number | null; slot: number | null }>({ height: null, slot: null });
  useLayoutEffect(() => {
    const story = storyRef.current;
    const box = measureRef.current;
    const codes = codeRef.current;
    if (!story || !box || !codes) return;
    // A source index carries its own head; the slot's head is added once below.
    const tallestOf = (parent: HTMLElement) =>
      Math.max(
        0,
        ...(Array.from(parent.children) as HTMLElement[]).map(
          (block) =>
            block.offsetHeight - ((block.querySelector(":scope > .walk-slot-head") as HTMLElement | null)?.offsetHeight ?? 0),
        ),
      );
    const update = () => {
      const style = getComputedStyle(story);
      const padding = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom);
      const gap = parseFloat(style.rowGap) || 16;
      const controls = (story.querySelector(".walk-controls") as HTMLElement | null)?.offsetHeight ?? 56;
      const head = (story.querySelector(".walk-slot-head") as HTMLElement | null)?.offsetHeight ?? 37;
      const slot = Math.min(520, Math.max(352, Math.ceil(tallestOf(codes) + head + 2)));
      const need = padding + tallestOf(box) + gap * 2 + slot + controls + 2;
      setSize({ height: Math.min(1200, Math.max(760, Math.ceil(need))), slot });
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(box);
    document.fonts?.ready.then(update);
    return () => observer.disconnect();
  }, [walk]);
  return { storyRef, measureRef, codeRef, ...size };
}

const prefersReducedMotion = () =>
  typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/**
 * One walkthrough: the route of calls on the left, what the current step says
 * on the right, and where it sits on the Wiki map between them.
 */
export default function Walkthrough({
  set,
  walk,
  step,
  onGo,
  onPage,
  colors,
}: {
  set: WalkthroughSet;
  walk: Walk;
  step: number;
  onGo: (walkId: string, step: number) => void;
  onPage: (pageId: string) => void;
  colors?: Map<string, string>;
}) {
  const index = Math.max(0, Math.min(walk.steps.length - 1, step));
  const current = walk.steps[index];
  const states = stepStates(walk, index);
  const hue = (area: string | null) => areaColor(set, area, colors);
  const areaOf = (stop: number) => stopArea(set, walk, stop);
  const nextWalk = set.walks[(set.walks.findIndex((item) => item.id === walk.id) + 1) % set.walks.length];
  const last = index === walk.steps.length - 1;
  const go = (target: number) => onGo(walk.id, Math.max(0, Math.min(walk.steps.length - 1, target)));
  const [replays, setReplays] = useState(0);
  const storyHeight = useStoryHeight(walk);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (target?.closest("input, textarea, select, [contenteditable='true']")) return;
      if (event.key === "ArrowRight") go(index + 1);
      else if (event.key === "ArrowLeft") go(index - 1);
      else if (event.key === "r" || event.key === "R") setReplays((n) => n + 1);
      else return;
      event.preventDefault();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  });

  const functions = walk.stops.filter((stop) => !stop[4]).length;
  const files = walk.zones.filter((zone) => !zone[3]).length;
  const at = current.at != null ? stopInfo(walk, current.at) : null;
  const nowAt = states.whole || !at ? "the whole path" : at.ln || at.full;

  return (
    <div
      className="walk"
      style={
        {
          "--area-now": hue(current.area ?? null),
          ...(storyHeight.height ? { "--walk-story-h": `${storyHeight.height}px` } : {}),
          ...(storyHeight.slot ? { "--walk-slot-h": `${storyHeight.slot}px` } : {}),
        } as CSSProperties
      }
    >
      <header className="walk-intro">
        <h1 dangerouslySetInnerHTML={inline(walk.title)} />
        <p className="walk-dek" dangerouslySetInnerHTML={inline(walk.dek)} />
        <p className="walk-facts">
          {walk.steps.length} steps · {functions} functions · {files} file{files === 1 ? "" : "s"} · {walk.chosen}
        </p>
      </header>

      <div className="walk-main">
        <section className="walk-sheet walk-figure" aria-label="Call path">
          <dl className="walk-now" aria-live="polite">
            <div>
              <dt>Now at</dt>
              <dd className="mono">{nowAt}</dd>
            </div>
            <div>
              <dt>Carries</dt>
              <dd>{current.what ?? "—"}</dd>
            </div>
          </dl>
          <div className="walk-figure-body">
            <Route set={set} walk={walk} index={index} replays={replays} hue={hue} areaOf={areaOf} onStop={(stop) => go(stepForStop(walk, stop))} />
            <div className="walk-where">
              <MiniMap set={set} walk={walk} index={index} hue={hue} />
              <Here set={set} walk={walk} index={index} hue={hue} areaOf={areaOf} onPage={onPage} />
            </div>
          </div>
        </section>

        <aside className="walk-sheet walk-story" aria-live="polite" ref={storyHeight.storyRef}>
          {/* Every step's heading and prose, unseen, so the narration takes the tallest one's height. */}
          <div className="walk-measure" ref={storyHeight.measureRef} aria-hidden="true">
            {walk.steps.map((item, k) => (
              <div key={k}>
                <h2 dangerouslySetInnerHTML={inline(item.title)} />
                <div className="walk-prose">
                  <p className="lead" dangerouslySetInnerHTML={inline(item.lead)} />
                  {(item.body ?? []).map((paragraph, j) => (
                    <p key={j} dangerouslySetInnerHTML={inline(paragraph)} />
                  ))}
                  {item.idea && <p className="idea" dangerouslySetInnerHTML={inline(item.idea)} />}
                </div>
              </div>
            ))}
          </div>
          {/* inert: the copies of the source index here must not take focus. */}
          <div className="walk-measure walk-measure-code" ref={storyHeight.codeRef} aria-hidden="true" {...{ inert: "" }}>
            {walk.steps.map((item, k) =>
              item.code ? (
                <div className="walk-slot" key={k}>
                  <CodeLines set={set} code={item.code} />
                </div>
              ) : item.table ? (
                <SourceIndex key={k} set={set} walk={walk} kind={item.table} hue={hue} onGo={() => undefined} />
              ) : null,
            )}
          </div>
          <Story key={`${walk.id}-${index}`} set={set} walk={walk} index={index} hue={hue} onGo={go} />
          <div className="walk-controls">
            <span className="walk-pos">
              {index + 1} of {walk.steps.length}
            </span>
            <button type="button" className="walk-btn" disabled={index === 0} onClick={() => go(index - 1)}>
              Back
            </button>
            <button
              type="button"
              className="walk-btn primary"
              disabled={last && set.walks.length < 2}
              onClick={() => (last ? onGo(nextWalk.id, 0) : go(index + 1))}
            >
              Next
              <span className="walk-btn-to">: {last ? nextWalk.tab : walk.steps[index + 1].rail ?? `step ${index + 2}`}</span>
            </button>
          </div>
        </aside>
      </div>

      <p className="walk-foot">
        Paths, files, line numbers and call sites come from CodeNib&rsquo;s index of {set.repo.slug} at{" "}
        <span className="mono">{set.repo.commit.slice(0, 12)}</span>, and the code is that commit&rsquo;s source.{" "}
        {set.repo.model ? `${set.repo.model.split("/").pop()} wrote` : "A model wrote"} the step text; each step was
        checked against these lines. A dashed hop marked &ldquo;not in the index&rdquo; was read from the source.
      </p>
    </div>
  );
}

/* ---------- the route ---------- */

function Route({
  set,
  walk,
  index,
  replays,
  hue,
  areaOf,
  onStop,
}: {
  set: WalkthroughSet;
  walk: Walk;
  index: number;
  replays: number;
  hue: (area: string | null) => string;
  areaOf: (stop: number) => string | null;
  onStop: (stop: number) => void;
}) {
  const [boardRef, width] = useWidth<HTMLDivElement>();
  const measure = useMeasure();
  const layout = useMemo(() => routeLayout(walk, Math.max(width, 200)), [walk, width]);
  const states = stepStates(walk, index);
  const step = walk.steps[index];
  const tokenRef = useRef<SVGGElement>(null);
  const W = layout.width;
  // Text stays clear of the file zone's right edge (W - 10) by 8px.
  const fits = (x: number, text: string, font = MONO) => x + measure(text, font) <= W - 18;
  const clip = (text: string) => {
    if (fits(64, text)) return text;
    let keep = text.length - 1;
    while (keep > 4 && !fits(64, `${text.slice(0, keep)}…`)) keep -= 1;
    return `${text.slice(0, keep)}…`;
  };

  // The token runs along the step's segments, in the colour of the area it enters.
  useEffect(() => {
    const token = tokenRef.current;
    if (!token) return;
    token.style.opacity = "0";
    if (!step.runs.length || prefersReducedMotion() || width === 0) return;
    let frame = 0;
    let alive = true;
    const reverse = !!step.reverse;
    const per = reverse ? 360 : 820;
    const runs = step.runs.filter((k) => k >= 0 && k < walk.stops.length - 1);
    if (!runs.length) return;
    const colourAt = (k: number) => hue(areaOf(reverse ? runs[k] : runs[k] + 1));
    let k = 0;
    let start: number | null = null;
    const place = (seg: number, f: number) => {
      const a = layout.ys[seg];
      const b = layout.ys[seg + 1];
      token.setAttribute("transform", `translate(${layout.x},${a + (b - a) * f})`);
    };
    token.style.setProperty("--tok", colourAt(0));
    place(runs[0], reverse ? 1 : 0);
    token.style.opacity = "1";
    const tick = (time: number) => {
      if (!alive) return;
      if (start === null) start = time;
      const f = Math.min(1, (time - start) / per);
      const eased = 1 - Math.pow(1 - f, 3);
      place(runs[k], reverse ? 1 - eased : eased);
      if (f >= 1) {
        k += 1;
        start = null;
        if (k >= runs.length) {
          window.setTimeout(() => alive && (token.style.opacity = "0"), 500);
          return;
        }
        token.style.setProperty("--tok", colourAt(k));
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => {
      alive = false;
      cancelAnimationFrame(frame);
    };
    // Replays and layout changes restart the run; colours follow the walk.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [walk, index, replays, layout]);

  const zones: ReactNode[] = walk.zones.map(([file, chapter, stops, outside], zi) => {
    const top = layout.ys[stops[0]] - 46;
    const bottom = layout.ys[stops[stops.length - 1]] + 24;
    const name = shortAreaName(chapter);
    const chapterX = 64 + measure(file, `600 ${MONO}`) + 12;
    const area = outside ? null : areaOf(stops[0]);
    return (
      <g
        key={`zone-${zi}`}
        className={`zone${outside ? " ext" : ""}${zi === states.zone ? " is-here" : ""}`}
        style={{ "--area": hue(area) } as CSSProperties}
      >
        <rect x={10} y={top} width={W - 20} height={bottom - top} rx={6} />
        <text x={64} y={top + 21} className="file">
          {file}
        </text>
        {fits(chapterX, name, SANS) && (
          <text x={chapterX} y={top + 21} className="chapter">
            {name}
          </text>
        )}
      </g>
    );
  });

  const segs: ReactNode[] = walk.segs.map(([short, site, kind, dashLabel], i) => {
    if (i >= walk.stops.length - 1) return null;
    const a = layout.ys[i];
    const b = layout.ys[i + 1];
    const dash = kind === "dash";
    const crossing = zoneOf(walk, i) !== zoneOf(walk, i + 1);
    const y = crossing ? a + (zoneOf(walk, i) >= 0 ? 42 : 30) : (a + b) / 2 + 4;
    const full = site + (dash ? `  ${dashLabel || "subclass"}` : "");
    const label = fits(64, full) ? full : short.trim();
    return (
      <g key={`seg-${i}`}>
        <path
          d={`M${layout.x},${a} L${layout.x},${b}`}
          className={`seg${dash ? " dash" : ""} ${states.segs[i]}`}
          style={{ "--area": hue(areaOf(i + 1)) } as CSSProperties}
        />
        {site && (
          <text x={64} y={y} className={`site${states.segs[i] === "is-active" ? " is-active" : ""}`}>
            {label}
          </text>
        )}
      </g>
    );
  });

  const stops: ReactNode[] = walk.stops.map((_, i) => {
    const info = stopInfo(walk, i);
    const y = layout.ys[i];
    const target = walk.steps.findIndex((item) => item.at === i && !item.recap);
    let lines: string[] = [info.full];
    if (!fits(64, info.full)) {
      if (info.owner) lines = [info.owner, info.name];
      else lines = breakName(info.full) ?? lines;
      // A piece still too wide ends in an ellipsis; the stop's label keeps the name.
      lines = lines.map((line) => clip(line));
    }
    const area = areaOf(i);
    return (
      <g
        key={`stop-${i}`}
        className={`stop ${states.stops[i]}${target < 0 ? " is-inert" : ""}`}
        style={area ? ({ "--area": hue(area) } as CSSProperties) : undefined}
        role={target >= 0 ? "button" : undefined}
        tabIndex={target >= 0 ? 0 : undefined}
        aria-label={target >= 0 ? `Go to ${info.full}` : undefined}
        aria-current={states.stops[i] === "is-active" ? "step" : undefined}
        onClick={target >= 0 ? () => onStop(i) : undefined}
        onKeyDown={
          target >= 0
            ? (event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  onStop(i);
                }
              }
            : undefined
        }
      >
        {lines.some((line) => line.endsWith("…")) && <title>{info.full}</title>}
        <circle cx={layout.x} cy={y} r={16} className="ring" />
        {info.end ? (
          <rect x={layout.x - 8} y={y - 8} width={16} height={16} rx={3} className="dot" />
        ) : (
          <circle cx={layout.x} cy={y} r={8.5} className="dot" />
        )}
        {lines.length === 1 ? (
          <text x={64} y={y + 5} className="name">
            {lines[0]}
          </text>
        ) : (
          <>
            <text x={64} y={y - 2} className="name">
              {lines[0]}
            </text>
            <text x={64} y={y + 15} className="name">
              {lines[1]}
            </text>
          </>
        )}
      </g>
    );
  });

  return (
    <div className="walk-board" ref={boardRef}>
      {width > 0 && (
        <svg
          className="walk-route"
          viewBox={`0 0 ${W} ${layout.height}`}
          width={W}
          height={layout.height}
          role="group"
          aria-label={`${walk.tab}, step ${index + 1} of ${walk.steps.length}: ${walk.steps[index].rail ?? ""}`}
        >
          {zones}
          {segs}
          {stops}
          <g className="token" ref={tokenRef} style={{ opacity: 0 }} aria-hidden="true">
            <rect x={-30} y={-11} width={60} height={22} rx={11} />
            <text x={0} y={4.5} textAnchor="middle">
              {step.packet ?? ""}
            </text>
          </g>
        </svg>
      )}
      <span className="sr-only">{set.repo.slug}</span>
    </div>
  );
}

/* ---------- the Wiki map, small ---------- */

function MiniMap({
  set,
  walk,
  index,
  hue,
}: {
  set: WalkthroughSet;
  walk: Walk;
  index: number;
  hue: (area: string | null) => string;
}) {
  const measure = useMeasure();
  const areas = useMemo(() => miniAreas(set), [set]);
  const labels = useMemo(() => placeLabels(areas, (text) => measure(text, SANS)), [areas, measure]);
  const step = walk.steps[index];
  const byId = new Map(areas.map((area) => [area.id, area]));
  const pair = (a: string, b: string) => [a, b].sort().join("|");
  const crossed = new Set(walk.cross.map(([s, t]) => pair(s, t)));
  const taken: Box[] = [
    ...labels.map((label) => label.box),
    ...areas.map((area) => ({ x: area.x - area.r, y: area.y - area.r, width: 2 * area.r, height: 2 * area.r })),
  ];
  const crossings = walk.cross
    .map(([s, t, n, atStep], k) => {
      const a = byId.get(s);
      const b = byId.get(t);
      if (!a || !b) return null;
      const twins = walk.cross.filter(([s2, t2]) => pair(s2, t2) === pair(s, t)).length;
      const line = curve(a, b, twins > 1 ? -24 : 0.001);
      const [x, y] = placeBadge(line.at, taken);
      taken.push({ x: x - 11, y: y - 11, width: 22, height: 22 });
      return { k, s, t, n, atStep, d: line.d, x, y };
    })
    .filter((item): item is NonNullable<typeof item> => item !== null);

  return (
    <svg className="walk-minimap" viewBox="0 0 300 258" role="img" aria-label={`Wiki map of ${set.repo.slug}`}>
      {set.links
        .filter(([s, t]) => byId.has(s) && byId.has(t) && !crossed.has(pair(s, t)))
        .map(([s, t, n]) => {
          const a = byId.get(s)!;
          const b = byId.get(t)!;
          return (
            <line
              key={`${s}-${t}`}
              x1={a.x}
              y1={a.y}
              x2={b.x}
              y2={b.y}
              className="link"
              strokeWidth={Math.min(3, 1 + Math.log2(Math.max(1, n)) / 3)}
            />
          );
        })}
      {crossings.map((item) => (
        <path
          key={`cross-${item.k}`}
          d={item.d}
          className={`link ${index === item.atStep ? "on" : "lit"}`}
          strokeWidth={3}
          style={{ "--area": hue(item.t) } as CSSProperties}
        />
      ))}
      {areas.map((area) => (
        <circle
          key={area.id}
          cx={area.x}
          cy={area.y}
          r={area.r}
          className={`area${area.id === step.area ? " on" : walk.areas.includes(area.id) ? " lit" : ""}`}
          style={{ "--area": hue(area.id) } as CSSProperties}
        />
      ))}
      {labels.map((label) => (
        <g key={`label-${label.id}`}>
          <rect
            x={label.box.x - 3}
            y={label.box.y - 1}
            width={label.box.width + 6}
            height={label.box.height + 2}
            rx={3}
            className="bg"
          />
          <text x={label.x} y={label.y} textAnchor={label.anchor}>
            {label.text}
          </text>
        </g>
      ))}
      {crossings.map((item) => (
        <g key={`badge-${item.k}`}>
          <circle cx={item.x} cy={item.y} r={11} className="bg" />
          <text x={item.x} y={item.y + 4.5} textAnchor="middle" className="num">
            {item.n}
          </text>
        </g>
      ))}
    </svg>
  );
}

/* ---------- where the step sits on the Wiki map ---------- */

function Here({
  set,
  walk,
  index,
  hue,
  areaOf,
  onPage,
}: {
  set: WalkthroughSet;
  walk: Walk;
  index: number;
  hue: (area: string | null) => string;
  areaOf: (stop: number) => string | null;
  onPage: (pageId: string) => void;
}) {
  const step = walk.steps[index];
  const pages = set.wiki.pages;
  const areaName = (id: string) => shortAreaName(set.areas.find((area) => area.id === id)?.name ?? id);
  const pageLink = (id: string) => (
    <button type="button" key={id} className="walk-page-link" onClick={() => onPage(id)}>
      {pages[id].title}
    </button>
  );

  if (step.table === "stops" || step.recap || step.at == null) {
    const chapters = walk.areas.filter((id) => pages[id]);
    return (
      <div className="walk-here">
        <h2>On the Wiki map</h2>
        <p>{walk.map}</p>
        {chapters.length > 0 && (
          <div className="walk-read">
            <span>{chapters.length === 1 ? "The area's chapter" : "The areas' chapters"}</span>
            {chapters.map(pageLink)}
          </div>
        )}
      </div>
    );
  }

  const at = stopInfo(walk, step.at);
  const area = areaOf(step.at);
  if (!area) {
    const outside = walk.zones[zoneOf(walk, step.at)]?.[3];
    return (
      <div className="walk-here">
        <h2>
          <span className="walk-swatch" aria-hidden="true" />
          {outside ? "Outside the map" : "Not in an area"}
        </h2>
        <p>
          <code>{at.full}</code>
          {outside
            ? ` is not part of ${set.repo.slug}, so the Wiki map does not show it.`
            : ` is in ${set.repo.slug}, but its file is not assigned to any area of the Wiki map.`}
        </p>
      </div>
    );
  }

  const [symbols, files] = set.wiki.size[area] ?? [0, 0];
  const seg = step.runs.length ? step.runs[step.runs.length - 1] : null;
  const from = seg != null ? areaOf(seg) : null;
  const crossedIn = !!from && from !== area;
  const segment = seg != null ? walk.segs[seg] : null;
  const dash = segment && segment[2] === "dash" ? segment[3] || "subclass" : null;
  const named = segment?.[4] === "named";
  const calls = crossedIn ? set.wiki.calls[`${from}>${area}`] ?? 0 : 0;
  let hop: ReactNode = null;
  if (dash === "subclass") {
    hop = `The source calls the base method; the index follows it to this subclass.${crossedIn ? ` It crosses in from ${areaName(from!)}.` : ""}`;
  } else if (dash) {
    hop =
      (named
        ? "The index does not record this call: the source calls a method by this name on a value whose type it does not know, and the walk follows the implementation most callers get."
        : "The index does not record this call. The hop is read from the source, where the attribute's type names this method.") +
      (crossedIn ? ` It crosses in from ${areaName(from!)}${calls ? `; the map records ${calls} other calls in that direction` : ""}.` : "");
  } else if (crossedIn) {
    hop = `This hop crossed in from ${areaName(from!)}.${calls ? ` The map records ${calls} calls in that direction; this is one of them.` : ""}`;
  }
  const cited = citingPages(set, at.full, area);
  return (
    <div className="walk-here" style={{ "--area": hue(area) } as CSSProperties}>
      <h2>
        <span className="walk-swatch" aria-hidden="true" />
        {areaName(area)}
        <small>
          {symbols} symbols in {files} files
        </small>
      </h2>
      {hop && <p>{hop}</p>}
      {cited.ids.length > 0 && (
        <div className="walk-read">
          <span>
            {cited.cited ? (
              <>
                <code>{at.full}</code> in the Wiki
              </>
            ) : (
              <>
                The Wiki does not cite <code>{at.full}</code>. Its area&rsquo;s chapter:
              </>
            )}
          </span>
          {cited.ids.map(pageLink)}
        </div>
      )}
    </div>
  );
}

/* ---------- the narration ---------- */

function Story({
  set,
  walk,
  index,
  hue,
  onGo,
}: {
  set: WalkthroughSet;
  walk: Walk;
  index: number;
  hue: (area: string | null) => string;
  onGo: (step: number) => void;
}) {
  const step = walk.steps[index];
  const proseRef = useRef<HTMLDivElement>(null);
  const [clipped, setClipped] = useState(false);
  const mark = () => {
    const prose = proseRef.current;
    if (prose) setClipped(prose.scrollHeight > prose.clientHeight + 2 && prose.scrollTop + prose.clientHeight < prose.scrollHeight - 2);
  };
  useLayoutEffect(() => {
    mark();
    const prose = proseRef.current;
    if (!prose) return;
    const observer = new ResizeObserver(mark);
    observer.observe(prose);
    return () => observer.disconnect();
  }, []);

  return (
    <>
      <h2 dangerouslySetInnerHTML={inline(step.title)} />
      <div className={`walk-prose${clipped ? " is-clipped" : ""}`} ref={proseRef} onScroll={mark}>
        <p className="lead" dangerouslySetInnerHTML={inline(step.lead)} />
        {(step.body ?? []).map((paragraph, k) => (
          <p key={k} dangerouslySetInnerHTML={inline(paragraph)} />
        ))}
        {step.idea && <p className="idea" dangerouslySetInnerHTML={inline(step.idea)} />}
      </div>
      {step.code ? <CodeSlot set={set} walk={walk} index={index} /> : <SourceIndex set={set} walk={walk} kind={step.table ?? "stops"} hue={hue} onGo={onGo} />}
    </>
  );
}

function CodeLines({ set, code }: { set: WalkthroughSet; code: WalkCode }) {
  const rows = useMemo(() => codeRows(set, code), [set, code]);
  return (
    <pre>
      {rows.map((row, k) =>
        row.kind === "gap" ? (
          <span className="ln gap" key={`gap-${k}`}>
            <span className="no" />
            <span className="c">
              {row.hidden} line{row.hidden === 1 ? "" : "s"} not shown
            </span>
          </span>
        ) : (
          <span className={`ln${row.marked ? " m" : ""}`} key={row.number}>
            <span className="no">{row.number}</span>
            <span className="c" style={{ "--hang": `${row.hang}ch` } as CSSProperties} dangerouslySetInnerHTML={{ __html: row.html ?? "" }} />
          </span>
        ),
      )}
    </pre>
  );
}

function CodeSlot({ set, walk, index }: { set: WalkthroughSet; walk: Walk; index: number }) {
  const code = walk.steps[index].code!;
  const [file, show] = code;
  const first = show[0][0];
  const lastLine = show[show.length - 1][1];
  return (
    <div className="walk-slot">
      <div className="walk-slot-head">
        <span className="mono">
          {file.split("/").pop()} {first}–{lastLine}
        </span>
        <a href={githubLine(set, file, first, lastLine)} target="_blank" rel="noopener noreferrer">
          View on GitHub
        </a>
      </div>
      <div className="walk-slot-scroll">
        <CodeLines set={set} code={code} />
      </div>
    </div>
  );
}

/** The path's functions, or its call sites, grouped by file. Each row opens its step; its lines open GitHub. */
function SourceIndex({
  set,
  walk,
  kind,
  hue,
  onGo,
}: {
  set: WalkthroughSet;
  walk: Walk;
  kind: "stops" | "sites";
  hue: (area: string | null) => string;
  onGo: (step: number) => void;
}) {
  type Row = { file: string; range: string; step: number; label: string; what: ReactNode };
  let head: string;
  let rows: Row[];
  if (kind === "stops") {
    const fns = walk.stops.map((_, i) => [i, stopInfo(walk, i)] as const).filter(([, info]) => !info.end && info.ln);
    const fileCount = new Set(fns.map(([, info]) => info.ln!.split(":")[0])).size;
    head = `${fns.length} functions in ${fileCount} file${fileCount === 1 ? "" : "s"}`;
    rows = fns.map(([i, info]) => {
      const [file, range] = info.ln!.split(":");
      return {
        file,
        range,
        step: stepForStop(walk, i),
        label: info.full,
        what: (
          <>
            <code>{info.full}</code>
            {info.role}
          </>
        ),
      };
    });
  } else {
    const hops = walk.segs.map((seg, i) => [i, seg] as const).filter(([i, seg]) => seg[1] && i < walk.stops.length - 1);
    head = `${hops.length} call sites`;
    rows = hops.map(([i, seg]) => {
      const [file, line] = seg[1].split(":");
      const via = seg[2] === "dash" ? ((seg[3] || "subclass") === "subclass" ? ", subclass" : ", not in the index") : "";
      return {
        file,
        range: line,
        step: stepForStop(walk, i + 1),
        label: `${stopInfo(walk, i).full} to ${stopInfo(walk, i + 1).full}`,
        what: (
          <>
            <code>{stopInfo(walk, i + 1).full}</code>from <code>{stopInfo(walk, i).full}</code>
            {via}
          </>
        ),
      };
    });
  }
  const out: ReactNode[] = [];
  let file: string | null = null;
  rows.forEach((row, k) => {
    if (row.file !== file) {
      file = row.file;
      const zone = walk.zones.find((item) => item[0] === row.file);
      const area = zone && !zone[3] ? stopArea(set, walk, zone[2][0]) : null;
      out.push(
        <li className="file" key={`file-${k}`} style={{ "--area": hue(area) } as CSSProperties}>
          <span className="walk-swatch" aria-hidden="true" />
          <span className="mono">{row.file}</span>
          <span className="area">{zone ? shortAreaName(zone[1]) : ""}</span>
        </li>,
      );
    }
    const [a, b] = row.range.split("–");
    out.push(
      <li className="row" key={`row-${k}`}>
        <button type="button" onClick={() => onGo(row.step)} aria-label={`Go to step ${row.step + 1}: ${row.label}`}>
          <span className="n">{k + 1}</span>
          <span className="what">{row.what}</span>
        </button>
        <a
          className="lines"
          href={githubLine(set, set.paths[row.file] ?? row.file, a, b)}
          target="_blank"
          rel="noopener noreferrer"
          title={`Open ${row.file} at ${row.range} on GitHub`}
        >
          {row.range}
        </a>
      </li>,
    );
  });
  return (
    <div className="walk-slot">
      <div className="walk-slot-head">
        <span>{head}</span>
        <span>
          {set.repo.slug} @ <span className="mono">{set.repo.commit.slice(0, 7)}</span>
        </span>
      </div>
      <div className="walk-slot-scroll">
        <ul className="walk-srcs">{out}</ul>
      </div>
    </div>
  );
}

/** The walkthroughs on a Wiki's Overview: one card each, opening it. */
export function WalkthroughList({
  set,
  colors,
  onOpen,
  hrefFor,
}: {
  set: WalkthroughSet;
  colors?: Map<string, string>;
  onOpen: (walkId: string) => void;
  /** The walkthrough's own address, so a card opens in a new tab as a link does. */
  hrefFor?: (walkId: string) => string;
}) {
  return (
    <nav className="walk-cards" aria-label="Walkthroughs">
      <h2>Walkthroughs</h2>
      <ul>
        {set.walks.map((walk) => {
          const first = walk.areas[0] ?? null;
          const functions = walk.stops.filter((stop) => !stop[4]).length;
          // The areas the walk passes through, in order, drawn as its route
          // crosses the map: one dot per area, joined.
          const route = walk.areas.slice(0, 6);
          return (
            <li key={walk.id}>
              <a
                href={hrefFor?.(walk.id) ?? `?walk=${encodeURIComponent(walk.id)}`}
                className="chapter-card walk-card"
                onClick={(event) => {
                  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                  event.preventDefault();
                  onOpen(walk.id);
                }}
                style={{ "--area": areaColor(set, first, colors) } as CSSProperties}
              >
                {route.length > 0 && (
                  <svg
                    className="walk-route"
                    width={14 + (route.length - 1) * 22}
                    height="14"
                    viewBox={`0 0 ${14 + (route.length - 1) * 22} 14`}
                    aria-hidden="true"
                  >
                    {route.slice(1).map((_, index) => (
                      <line key={`l${index}`} x1={7 + index * 22} y1="7" x2={7 + (index + 1) * 22} y2="7" />
                    ))}
                    {route.map((area, index) => (
                      <circle key={`c${index}`} cx={7 + index * 22} cy="7" r="5" fill={areaColor(set, area, colors)} />
                    ))}
                  </svg>
                )}
                <span className="chapter-title">{walk.question}</span>
                <span className="chapter-meta">
                  <span>
                    {walk.steps.length} steps through {functions} functions
                  </span>
                  <svg
                    className="chapter-arrow"
                    width="16"
                    height="16"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.8"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    aria-hidden="true"
                  >
                    <path d="M5 12h14M13 6l6 6-6 6" />
                  </svg>
                </span>
              </a>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
