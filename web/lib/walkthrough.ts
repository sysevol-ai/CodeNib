// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * Code walkthroughs: one question followed through the code, a function per step.
 *
 * `scripts/walkthrough/` builds them offline from the index and the source; the
 * backend serves a repository's set only for the commit it was built from. This
 * module holds the shapes and the layout arithmetic, so the view stays drawing.
 */

import { highlightRows, languageForFile } from "./highlight";
import { NEUTRAL_COLOR } from "./wikiGraph";

/** [owner, name, "file:a–b", role, isEntryCall?]; stop 0 is the user's own call. */
export type WalkStop = [string, string, string | null, string | null, boolean?];
/**
 * Segment i joins stop i and stop i + 1; segment 0 leaves the user's own call.
 * [short label, call site "file:line", kind ("dash", "cross" or ""), dash label,
 * how an unrecorded hop was found ("attr" or "named")].
 */
export type WalkSeg = [string, string, string?, string?, string?];
/** [file, area name, stop indexes, outside the repository?] */
export type WalkZone = [string, string, number[], boolean?];
/** [from area, to area, recorded calls that way, step, hop kind] */
export type WalkCrossing = [string, string, number, number, string];
/** [file, shown line ranges, marked lines] */
export type WalkCode = [string, [number, number][], number[]];

export interface WalkStep {
  rail?: string;
  title: string;
  lead: string;
  body?: string[];
  idea?: string;
  at: number | null;
  reach?: number;
  area?: string | null;
  what?: string;
  runs: number[];
  reverse?: boolean;
  packet?: string;
  recap?: boolean;
  code?: WalkCode;
  table?: "stops" | "sites";
}

export interface Walk {
  id: string;
  tab: string;
  title: string;
  dek: string;
  question: string;
  chosen: string;
  map: string;
  stops: WalkStop[];
  segs: WalkSeg[];
  zones: WalkZone[];
  areas: string[];
  cross: WalkCrossing[];
  steps: WalkStep[];
}

export interface WalkArea {
  id: string;
  title: string;
  name: string;
  color: string;
  symbols: number;
  files: number;
}

export interface WalkthroughSet {
  version: number;
  repo: { id: string; slug: string; commit: string; model?: string };
  areas: WalkArea[];
  /** [from area, to area, recorded references] */
  links: [string, string, number][];
  /** Base file name → repository path, for files the walks show. */
  paths: Record<string, string>;
  wiki: {
    pages: Record<string, { title: string; top: string; depth: number }>;
    /** Function name → Wiki pages whose citations include it. */
    cites: Record<string, string[]>;
    /** Area → [symbols, files]. */
    size: Record<string, [number, number]>;
    /** "from>to" → recorded calls. */
    calls: Record<string, number>;
  };
  walks: Walk[];
  /** Repository path → line number → text, for every line a step shows. */
  source: Record<string, Record<string, string>>;
}

export interface StopInfo {
  owner: string;
  name: string;
  full: string;
  ln: string | null;
  role: string;
  end: boolean;
}

export function stopInfo(walk: Walk, index: number): StopInfo {
  const [owner, name, ln, role, end] = walk.stops[index];
  return { owner, name, full: owner + name, ln, role: role ?? "", end: !!end };
}

/** Area names read better without a trailing "Layer" or "System". */
export function shortAreaName(name: string): string {
  return name.replace(/\s+(Layer|System|Subsystem|Module)$/, "");
}

export function zoneOf(walk: Walk, stop: number): number {
  return walk.zones.findIndex((zone) => zone[2].includes(stop));
}

/** The Wiki area a stop's file belongs to, or null outside the map. */
export function stopArea(set: WalkthroughSet, walk: Walk, stop: number): string | null {
  const zone = walk.zones[zoneOf(walk, stop)];
  if (!zone || zone[3]) return null;
  const name = zone[1];
  return set.areas.find((area) => area.name === name || shortAreaName(area.name) === name)?.id ?? null;
}

/** The step that tells a stop's story; the opening step when none does. */
export function stepForStop(walk: Walk, stop: number): number {
  const index = walk.steps.findIndex((step) => step.at === stop && !step.recap);
  return index < 0 ? 0 : index;
}

export type MarkState = "is-active" | "is-done" | "is-future";

/** How far along the route a step is: each stop and segment active, done or still ahead. */
export function stepStates(walk: Walk, index: number): {
  stops: MarkState[];
  segs: MarkState[];
  zone: number;
  whole: boolean;
} {
  const step = walk.steps[index];
  const whole = step.table === "stops" || !!step.recap;
  const last = walk.stops.length - 1;
  const reached = whole ? last : (step.reach ?? step.at ?? 0);
  const stops = walk.stops.map((_, k): MarkState =>
    !whole && k === step.at ? "is-active" : k <= reached ? "is-done" : "is-future",
  );
  const firstRun = step.runs[0] ?? step.at ?? 0;
  const segs = walk.segs.map((_, k): MarkState =>
    !whole && step.runs.includes(k) ? "is-active" : whole || k < firstRun ? "is-done" : "is-future",
  );
  const zone = !whole && step.at != null ? zoneOf(walk, step.at) : -1;
  return { stops, segs, zone, whole };
}

export interface RouteLayout {
  width: number;
  height: number;
  x: number;
  ys: number[];
}

/**
 * A vertical trace at the board's own pixel width, so 13px text is 13px on
 * screen. A stop that opens a file leaves room for the file's heading; a short
 * trace spreads out, up to 36px a hop, toward the narration's height.
 */
export function routeLayout(walk: Walk, width: number, target = 560): RouteLayout {
  const ys: number[] = [];
  let y = 22;
  let previous = -2;
  walk.stops.forEach((_, index) => {
    const zone = zoneOf(walk, index);
    if (index === 0) y += zone >= 0 ? 46 : 12;
    else if (zone !== previous) y += zone >= 0 ? 104 : 70;
    else y += 60;
    ys.push(y);
    previous = zone;
  });
  const extra = Math.max(0, Math.min(36, (target - (y + 40)) / Math.max(1, ys.length - 1)));
  const spread = ys.map((value, index) => value + extra * index);
  return { width, height: spread[spread.length - 1] + 40, x: 34, ys: spread };
}

/** A long name with no owner breaks at the underscore nearest its middle; null when it has none. */
export function breakName(name: string): [string, string] | null {
  const cuts = [...name.matchAll(/_/g)].map((match) => match.index ?? 0).filter((index) => index > 0);
  if (cuts.length === 0) return null;
  const cut = cuts.sort((a, b) => Math.abs(a - name.length / 2) - Math.abs(b - name.length / 2))[0];
  return [name.slice(0, cut), name.slice(cut)];
}

export interface CodeRow {
  kind: "line" | "gap";
  number?: number;
  html?: string;
  /** Columns a wrapped line hangs by: its own indentation plus four. */
  hang?: number;
  marked?: boolean;
  hidden?: number;
}

/**
 * The excerpt's rows, highlighted as one piece. The indentation every shown
 * line shares is dropped so a method's body starts at the left edge.
 */
export function codeRows(set: WalkthroughSet, code: WalkCode): CodeRow[] {
  const [file, show, mark] = code;
  const lines = set.source[file] ?? {};
  const marks = new Set(mark);
  const shown = show.flatMap(([a, b]) =>
    Array.from({ length: b - a + 1 }, (_, k) => lines[String(a + k)] ?? ""),
  );
  const indents = shown.filter((text) => text.trim()).map((text) => text.match(/^ */)![0].length);
  const cut = indents.length ? Math.min(...indents) : 0;
  const language = languageForFile(file);
  const rows: CodeRow[] = [];
  show.forEach(([a, b], index) => {
    if (index > 0) {
      const hidden = a - show[index - 1][1] - 1;
      if (hidden > 0) rows.push({ kind: "gap", hidden });
    }
    const text: string[] = [];
    for (let n = a; n <= b; n += 1) text.push((lines[String(n)] ?? "").slice(cut));
    highlightRows(text.join("\n"), language).forEach((html, k) => {
      rows.push({
        kind: "line",
        number: a + k,
        html,
        hang: text[k].match(/^ */)![0].length + 4,
        marked: marks.has(a + k),
      });
    });
  });
  return rows;
}

export function githubLine(set: WalkthroughSet, path: string, first: number | string, last?: number | string): string {
  const range = last != null && String(last) !== String(first) ? `#L${first}-L${last}` : `#L${first}`;
  return `https://github.com/${set.repo.slug}/blob/${set.repo.commit}/${path}${range}`;
}

/** Wiki pages that cite a function, its own area's first and the narrowest first. */
export function citingPages(set: WalkthroughSet, fn: string, area: string | null): { cited: boolean; ids: string[] } {
  const pages = set.wiki.pages;
  const ids = (set.wiki.cites[fn] ?? []).filter((id) => pages[id]);
  const rank = (id: string) => (pages[id].top === area ? 0 : 2) - pages[id].depth;
  ids.sort((a, b) => rank(a) - rank(b));
  if (ids.length) return { cited: true, ids: ids.slice(0, 2) };
  return { cited: false, ids: area && pages[area] ? [area] : [] };
}

export function areaColor(set: WalkthroughSet, area: string | null, colors?: Map<string, string>): string {
  if (!area) return NEUTRAL_COLOR;
  return colors?.get(area) ?? set.areas.find((item) => item.id === area)?.color ?? NEUTRAL_COLOR;
}

/* ---------- the Wiki map's areas, small ---------- */

export interface MiniArea {
  id: string;
  name: string;
  symbols: number;
  x: number;
  y: number;
  r: number;
  pos: "above" | "below";
  anchor: "start" | "middle" | "end";
}

const SLOTS: Pick<MiniArea, "x" | "y" | "pos" | "anchor">[] = [
  { x: 132, y: 136, pos: "below", anchor: "middle" },
  { x: 48, y: 60, pos: "above", anchor: "start" },
  { x: 230, y: 68, pos: "above", anchor: "end" },
  { x: 236, y: 178, pos: "below", anchor: "end" },
  { x: 52, y: 214, pos: "below", anchor: "start" },
];

/** Five areas: every area a walk passes through, then the largest of the rest, largest in the middle. */
export function miniAreas(set: WalkthroughSet): MiniArea[] {
  const most = Math.max(1, ...set.areas.map((area) => area.symbols || 1));
  const onWalks = new Set(set.walks.flatMap((walk) => walk.areas));
  const rest = set.areas.filter((area) => !onWalks.has(area.id)).sort((a, b) => (b.symbols || 0) - (a.symbols || 0));
  return [...set.areas.filter((area) => onWalks.has(area.id)), ...rest]
    .slice(0, SLOTS.length)
    .sort((a, b) => (b.symbols || 0) - (a.symbols || 0))
    .map((area, k) => ({
      id: area.id,
      name: shortAreaName(area.name),
      symbols: area.symbols || 1,
      r: 10 + Math.sqrt((area.symbols || 1) / most) * 20,
      ...SLOTS[k],
    }));
}

export type Point = [number, number];

/** A quadratic link between two areas; `at(t)` walks along it. */
export function curve(a: { x: number; y: number }, b: { x: number; y: number }, bend: number) {
  const mx = (a.x + b.x) / 2;
  const my = (a.y + b.y) / 2;
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const length = Math.hypot(dx, dy) || 1;
  const cx = mx - (dy / length) * bend;
  const cy = my + (dx / length) * bend;
  const at = (t: number): Point => [
    (1 - t) ** 2 * a.x + 2 * (1 - t) * t * cx + t ** 2 * b.x,
    (1 - t) ** 2 * a.y + 2 * (1 - t) * t * cy + t ** 2 * b.y,
  ];
  return { d: `M${a.x},${a.y} Q${cx},${cy} ${b.x},${b.y}`, at };
}

export interface Box {
  x: number;
  y: number;
  width: number;
  height: number;
}

function overlap(p: Box, q: Box): number {
  return (
    Math.max(0, Math.min(p.x + p.width, q.x + q.width) - Math.max(p.x, q.x)) *
    Math.max(0, Math.min(p.y + p.height, q.y + q.height) - Math.max(p.y, q.y))
  );
}

export interface MiniLabel {
  id: string;
  text: string;
  x: number;
  y: number;
  anchor: MiniArea["anchor"];
  box: Box;
}

/** Area labels beside their circles; one that would sit on another goes to the other side. */
export function placeLabels(areas: MiniArea[], measure: (text: string) => number): MiniLabel[] {
  const labels: MiniLabel[] = [];
  const boxAt = (area: MiniArea, x: number, y: number, width: number): Box => ({
    x: area.anchor === "start" ? x : area.anchor === "end" ? x - width : x - width / 2,
    y: y - 13,
    width,
    height: 17,
  });
  for (const area of areas) {
    const width = measure(area.name);
    const x = area.anchor === "start" ? area.x - area.r : area.anchor === "end" ? area.x + area.r : area.x;
    const above = area.y - area.r - 8;
    const below = area.y + area.r + 18;
    let y = area.pos === "above" ? above : below;
    let box = boxAt(area, x, y, width);
    if (labels.some((other) => overlap(box, other.box) > 0)) {
      y = area.pos === "above" ? below : above;
      box = boxAt(area, x, y, width);
    }
    labels.push({ id: area.id, text: area.name, x, y, anchor: area.anchor, box });
  }
  return labels;
}

/** Where a count sits on its link: covering no label and no area, near the middle. */
export function placeBadge(at: (t: number) => Point, taken: Box[]): Point {
  let best: [number, number, number] | null = null;
  for (let t = 0.15; t <= 0.851; t += 0.05) {
    const [x, y] = at(t);
    const box = { x: x - 11, y: y - 11, width: 22, height: 22 };
    const cost = taken.reduce((sum, other) => sum + overlap(box, other), 0) + Math.abs(t - 0.5) * 4;
    if (!best || cost < best[0]) best = [cost, x, y];
  }
  return [best![1], best![2]];
}
