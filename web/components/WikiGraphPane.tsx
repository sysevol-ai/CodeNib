// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";

import type { WikiAreaLink } from "@/lib/api";
import { type ActivityMark, isOpenMark, markRadius, markSummary } from "@/lib/codeActivity";
import type { RouteStop } from "@/lib/mapRoute";
import type { WikiPageState } from "@/lib/visitorWiki";
import { labelLines, neighborhood, type WikiGraph, type WikiGraphEdge, type WikiGraphNode } from "@/lib/wikiGraph";
import "./WikiGraphPane.css";

interface View {
  x: number;
  y: number;
  k: number;
}

interface Point {
  x: number;
  y: number;
}

interface PlacedLabel {
  id: string;
  lines: string[];
  anchor: "start" | "middle" | "end";
  dx: number;
  dy: number;
  /** Where the label sits on screen, for anything placed after it. */
  box: { left: number; right: number; top: number; bottom: number };
}

// Geist at the label size averages about this wide per character.
const LABEL_CHAR_PX = 6.1;
const LABEL_HEIGHT_PX = 15;
const LABEL_LINE_PX = 14;
const LABEL_MARGIN_PX = 8;
const number = new Intl.NumberFormat("en-US");
// Room for the repository name above an embedded map.
const HEADER_PX = 44;

/**
 * Labels hold their on-screen size at any zoom. Each sits on the side facing
 * away from the map's centre, and a label that would land on an earlier one is
 * left out; more important pages are placed first.
 */
function placeLabels(
  nodes: WikiGraphNode[],
  points: Map<string, Point>,
  view: View,
  center: Point,
  paneWidth: number,
  priority: (node: WikiGraphNode) => number,
): PlacedLabel[] {
  const taken: { left: number; right: number; top: number; bottom: number }[] = [];
  const placed: PlacedLabel[] = [];
  // Labels also keep off the large marks, so a name never hides a node.
  const marks = nodes
    .filter((node) => node.kind === "area")
    .map((node) => {
      const point = points.get(node.id)!;
      const sx = point.x * view.k + view.x;
      const sy = point.y * view.k + view.y;
      const r = node.radius * view.k;
      return { id: node.id, left: sx - r, right: sx + r, top: sy - r, bottom: sy + r };
    });
  const ordered = nodes
    .filter((node) => priority(node) > 0)
    .sort((a, b) => priority(b) - priority(a) || b.radius - a.radius);
  for (const node of ordered) {
    const point = points.get(node.id)!;
    const lines = labelLines(node.title);
    const extra = (lines.length - 1) * LABEL_LINE_PX;
    const width = Math.max(...lines.map((line) => line.length)) * LABEL_CHAR_PX;
    const sx = point.x * view.k + view.x;
    const sy = point.y * view.k + view.y;
    const radius = node.radius * view.k;
    const away = point.x - center.x;
    const beside = Math.abs(away) > 40;
    let anchor: PlacedLabel["anchor"] = !beside ? "middle" : away > 0 ? "start" : "end";
    let dx = !beside ? 0 : away > 0 ? radius + 7 : -(radius + 7);
    const dy = beside ? 4 - extra / 2 : point.y < center.y ? -(radius + 8) - extra : radius + 15;
    // Near an edge of the pane, the label moves to the node's inner side.
    if (anchor === "end" && sx + dx - width < LABEL_MARGIN_PX) {
      anchor = "start";
      dx = radius + 7;
    } else if (anchor === "start" && sx + dx + width > paneWidth - LABEL_MARGIN_PX) {
      anchor = "end";
      dx = -(radius + 7);
    }
    const left = sx + dx - (anchor === "middle" ? width / 2 : anchor === "end" ? width : 0);
    const box = { left, right: left + width, top: sy + dy - 11, bottom: sy + dy - 11 + LABEL_HEIGHT_PX + extra };
    const hits = (other: typeof box) =>
      box.left < other.right && box.right > other.left && box.top < other.bottom && box.bottom > other.top;
    const clash = taken.some(hits) || marks.some((mark) => mark.id !== node.id && hits(mark));
    const outside = box.left < 0 || box.right > paneWidth;
    if ((clash || outside) && priority(node) < 4) continue;
    taken.push(box);
    placed.push({ id: node.id, lines, anchor, dx, dy, box });
  }
  return placed;
}

interface Curve {
  start: Point;
  control: Point;
  end: Point;
}

/** The bend between two nodes, so two-way references stay two visible lines. */
function edgeCurve(a: Point, b: Point, ar: number, br: number, curved: boolean): Curve {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const length = Math.max(Math.hypot(dx, dy), 1);
  const ux = dx / length;
  const uy = dy / length;
  const start = { x: a.x + ux * ar, y: a.y + uy * ar };
  const end = { x: b.x - ux * (br + (curved ? 3 : 0)), y: b.y - uy * (br + (curved ? 3 : 0)) };
  const bend = curved ? Math.min(length * 0.12, 34) : 0;
  const control = { x: (start.x + end.x) / 2 - uy * bend, y: (start.y + end.y) / 2 + ux * bend };
  return { start, control, end };
}

/** A curved path between two nodes, so two-way references stay two visible lines. */
function edgePath(a: Point, b: Point, ar: number, br: number, curved: boolean): string {
  const { start, control, end } = edgeCurve(a, b, ar, br, curved);
  if (!curved) return `M${start.x} ${start.y}L${end.x} ${end.y}`;
  return `M${start.x} ${start.y}Q${control.x} ${control.y} ${end.x} ${end.y}`;
}

function curvePoint({ start, control, end }: Curve, t: number): Point {
  const u = 1 - t;
  return {
    x: u * u * start.x + 2 * u * t * control.x + t * t * end.x,
    y: u * u * start.y + 2 * u * t * control.y + t * t * end.y,
  };
}

/** The route's areas in the order it visits them, as one legend swatch. */
function routeSwatch(stops: RouteStop[], byId: Map<string, WikiGraphNode>): string {
  const colors = [...new Set(stops.map((stop) => byId.get(stop.areaId)?.color).filter(Boolean))];
  if (colors.length < 2) return colors[0] ?? "transparent";
  const step = 100 / colors.length;
  return `linear-gradient(90deg, ${colors
    .map((color, index) => `${color} ${(index * step).toFixed(1)}% ${((index + 1) * step).toFixed(1)}%`)
    .join(", ")})`;
}

/** How long the token takes to cross from one area to the next. */
export const ROUTE_TRAVEL_MS = 720;
// Geist Mono at 13 px is about this wide per character.
const ROUTE_CHAR_PX = 7.9;

interface Box {
  left: number;
  right: number;
  top: number;
  bottom: number;
}

const overlaps = (a: Box, b: Box) =>
  a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;

const referenceWidth = (link?: WikiAreaLink) => 0.8 + Math.log10(Math.max(link?.weight ?? 1, 1)) * 0.9;

/**
 * The Wiki as one picture: pages are nodes, and heavy lines are references the
 * code index recorded between areas (calls, field reads, type uses). Choosing a
 * page never moves the camera, so the reader keeps their place in the map.
 */
export default function WikiGraphPane({
  graph,
  activeId,
  hasReferences,
  note,
  onRetry,
  onPick,
  onOpenReference,
  onHide,
  onResizeReader,
  embedded = false,
  header,
  idle,
  states,
  arrived,
  describe,
  readonly = false,
  live = false,
  still = false,
  surveying = false,
  legend,
  activity,
  route,
  onRoute,
}: {
  graph: WikiGraph;
  activeId: string;
  hasReferences: boolean;
  /** Why the map shows no recorded references, in the reader's terms. */
  note: string | null;
  onRetry?: () => void;
  onPick: (id: string) => void;
  onOpenReference: (link: WikiAreaLink) => void;
  onHide?: () => void;
  onResizeReader?: (delta: number) => void;
  /** Inside a page rather than a workspace: the page keeps the wheel and touch. */
  embedded?: boolean;
  header?: ReactNode;
  /** What the status line says when nothing is under the pointer. */
  idle?: { title: string; detail: string };
  /** A Wiki being written: each page is planned, being written, ready or withheld. */
  states?: Record<string, WikiPageState>;
  /** Pages that became ready while this map was open; each blooms once. */
  arrived?: ReadonlySet<string>;
  /** What the status line says about a node under the pointer. */
  describe?: (node: WikiGraphNode) => { title: string; detail: string } | null;
  /** A picture of the source: nothing to open, so nodes are not links. */
  readonly?: boolean;
  /** Nodes fade in as they first appear, and references draw in once. */
  live?: boolean;
  /** The run has gone quiet: running marks stop moving. */
  still?: boolean;
  /** A page that is not a node (the Overview) is being written about all of them. */
  surveying?: boolean;
  /** Replaces the line-style legend. */
  legend?: ReactNode;
  /** Pages with open issues, open pull requests or several recent changes on their code. */
  activity?: Record<string, ActivityMark>;
  /**
   * The Overview's main path, drawn up to stop `at`. With `animate`, a token
   * carries each new call across the map; without it the route is drawn whole.
   */
  route?: { stops: RouteStop[]; at: number; animate: boolean };
  /** Opens the page that tells the path in words. */
  onRoute?: () => void;
}) {
  const paneRef = useRef<HTMLElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  // Null until the reader pans or zooms; until then the view is the fitted one.
  const [movedView, setMovedView] = useState<View | null>(null);
  const [hoverNode, setHoverNode] = useState<string | null>(null);
  const [hoverEdge, setHoverEdge] = useState<WikiGraphEdge | null>(null);
  const drag = useRef<{ x: number; y: number; moved: boolean } | null>(null);
  const tokenRef = useRef<SVGGElement>(null);
  const newestHopRef = useRef<SVGPathElement>(null);

  const byId = useMemo(() => new Map(graph.nodes.map((node) => [node.id, node])), [graph]);
  const near = useMemo(() => neighborhood(graph, activeId), [graph, activeId]);

  // The layout is stretched to the pane's shape, so the map fills it at any
  // width; nodes stay round because only their positions are stretched.
  const { points, center } = useMemo(() => {
    const next = new Map<string, Point>();
    const xs = graph.nodes.map((node) => node.x);
    const ys = graph.nodes.map((node) => node.y);
    if (!xs.length || !size.width || !size.height) return { points: next, center: { x: 0, y: 0 } };
    const [minX, maxX, minY, maxY] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
    // A phone has no room for labels at the sides; they move inward instead.
    const fitX = (size.width - (size.width < 520 ? 56 : 190)) / Math.max(maxX - minX, 1);
    // An embedded map has no toolbar, so its nodes can use more of the pane.
    const fitY = (size.height - (embedded && size.width >= 520 ? 124 : 170) - (header ? HEADER_PX : 0)) / Math.max(maxY - minY, 1);
    const scale = Math.min(fitX, fitY);
    const stretchX = Math.min(Math.max(fitX, 0.1), scale * 1.45, 2.4);
    const stretchY = Math.min(Math.max(fitY, 0.1), scale * 1.45, 2.4);
    for (const node of graph.nodes) next.set(node.id, { x: node.x * stretchX, y: node.y * stretchY });
    return {
      points: next,
      center: { x: ((minX + maxX) / 2) * stretchX, y: ((minY + maxY) / 2) * stretchY },
    };
  }, [graph, size, header, embedded]);

  const fitted = useMemo((): View | null => {
    if (!size.width || !size.height || !points.size) return null;
    const top = header ? HEADER_PX : 0;
    return { k: 1, x: size.width / 2 - center.x, y: (size.height - 40 + top) / 2 - center.y };
  }, [points, center, size, header]);
  const view = movedView ?? fitted;
  const viewRef = useRef(view);
  viewRef.current = view;

  // Measured before paint, so the map never shows a blank first frame.
  useLayoutEffect(() => {
    const pane = paneRef.current;
    if (!pane) return undefined;
    setSize({ width: pane.clientWidth, height: pane.clientHeight });
    const observer = new ResizeObserver(() => {
      setSize({ width: pane.clientWidth, height: pane.clientHeight });
    });
    observer.observe(pane);
    return () => observer.disconnect();
  }, []);

  // A non-passive listener: the wheel zooms the map, not the page behind it.
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg || embedded) return undefined;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const box = svg.getBoundingClientRect();
      const px = event.clientX - box.left;
      const py = event.clientY - box.top;
      const current = viewRef.current;
      if (!current) return;
      const k = Math.min(4, Math.max(0.3, current.k * Math.exp(-event.deltaY * 0.0012)));
      const ratio = k / current.k;
      setMovedView({ k, x: px - (px - current.x) * ratio, y: py - (py - current.y) * ratio });
    };
    svg.addEventListener("wheel", onWheel, { passive: false });
    return () => svg.removeEventListener("wheel", onWheel);
  }, [embedded]);

  const labels = useMemo(() => {
    if (!view || !points.size) return [];
    return placeLabels(graph.nodes, points, view, center, size.width, (node) => {
      // The chapter being written keeps its name, so the reader can follow it.
      if (node.id === hoverNode || node.id === activeId || states?.[node.id] === "running") return 4;
      if (node.kind === "area") return 3;
      if (near.has(node.id)) return 2;
      return view.k >= 1.7 ? 1 : 0;
    });
  }, [graph, points, view, center, size.width, hoverNode, activeId, near, states]);

  // The main path up to the current stop: the hops that crossed areas, where
  // the token travels last, and a free place for the current function's name.
  const routeView = useMemo(() => {
    if (!route || !view || !points.size) return null;
    const stops = route.stops.filter((stop) => byId.get(stop.areaId)?.kind === "area" && points.has(stop.areaId));
    const at = Math.min(route.at, stops.length - 1);
    if (at < 0) return null;
    const hops: { id: string; to: string; curve: Curve; d: string; color: string; newest: boolean }[] = [];
    for (let index = 1; index <= at; index += 1) {
      const from = stops[index - 1].areaId;
      const to = stops[index].areaId;
      if (from === to) continue;
      const curve = edgeCurve(points.get(from)!, points.get(to)!, byId.get(from)!.radius + 2, byId.get(to)!.radius + 5, true);
      hops.push({
        id: `${index}:${from}>${to}`,
        to,
        curve,
        d: `M${curve.start.x} ${curve.start.y}Q${curve.control.x} ${curve.control.y} ${curve.end.x} ${curve.end.y}`,
        color: byId.get(to)!.color,
        newest: index === at,
      });
    }
    const current = stops[at];
    const crossed = at > 0 && stops[at - 1].areaId !== current.areaId;
    const node = byId.get(current.areaId)!;
    const point = points.get(current.areaId)!;
    const sx = point.x * view.k + view.x;
    const sy = point.y * view.k + view.y;
    const r = node.radius * view.k;
    const width = current.label.length * ROUTE_CHAR_PX;
    // Area labels are placed from an estimate of their width, so keep a margin.
    const taken: Box[] = [
      ...labels.map(({ box }) => ({ left: box.left - 5, right: box.right + 5, top: box.top - 2, bottom: box.bottom + 2 })),
      ...graph.nodes
        .filter((other) => other.kind === "area" && other.id !== node.id)
        .map((other) => {
          const p = points.get(other.id)!;
          const ox = p.x * view.k + view.x;
          const oy = p.y * view.k + view.y;
          const or = other.radius * view.k + 3;
          return { left: ox - or, right: ox + or, top: oy - or, bottom: oy + or };
        }),
    ];
    // Nearest first: straight above or below the mark, then its corners, then
    // a step further out. A name with no free place stays in the status line.
    const candidates: { anchor: "start" | "middle" | "end"; dx: number; dy: number }[] = [];
    for (const out of [0, 14, 30]) {
      candidates.push(
        { anchor: "middle", dx: 0, dy: -(r + 13 + out) },
        { anchor: "middle", dx: 0, dy: r + 23 + out },
        { anchor: "start", dx: r + 9 + out, dy: -(r + 3 + out) },
        { anchor: "end", dx: -(r + 9 + out), dy: -(r + 3 + out) },
        { anchor: "start", dx: r + 9 + out, dy: r + 15 + out },
        { anchor: "end", dx: -(r + 9 + out), dy: r + 15 + out },
        { anchor: "start", dx: r + 10 + out, dy: 5 },
        { anchor: "end", dx: -(r + 10 + out), dy: 5 },
      );
    }
    const top = header ? HEADER_PX + 6 : 6;
    const bottom = size.height - 58;
    const boxOf = (spot: (typeof candidates)[number]) => {
      const left = sx + spot.dx - (spot.anchor === "middle" ? width / 2 : spot.anchor === "end" ? width : 0);
      return { left: left - 3, right: left + width + 3, top: sy + spot.dy - 13, bottom: sy + spot.dy + 5 };
    };
    const inside = (box: Box) => box.left >= 8 && box.right <= size.width - 8 && box.top >= top && box.bottom <= bottom;
    const spot = candidates.find((option) => {
      const box = boxOf(option);
      return inside(box) && !taken.some((other) => overlaps(box, other));
    });
    return {
      at,
      hops,
      current,
      crossed,
      visited: new Set(stops.slice(0, at + 1).map((stop) => stop.areaId)),
      label: spot ? { ...spot, x: point.x + spot.dx / view.k, y: point.y + spot.dy / view.k } : null,
    };
  }, [route, view, points, byId, labels, graph, header, size]);
  const newestHop = routeView?.hops.find((hop) => hop.newest) ?? null;
  const animateRoute = !!route?.animate;

  // The token runs outside React: one frame per paint, while the newest hop
  // draws itself in behind it.
  useLayoutEffect(() => {
    const token = tokenRef.current;
    const line = newestHopRef.current;
    if (!newestHop || !token || !line) return undefined;
    if (!animateRoute) {
      token.style.opacity = "0";
      line.style.strokeDashoffset = "0";
      return undefined;
    }
    const curve = newestHop.curve;
    let frame = 0;
    const started = performance.now();
    const place = (t: number) => {
      const p = curvePoint(curve, t);
      token.setAttribute("transform", `translate(${p.x} ${p.y})`);
      line.style.strokeDashoffset = String(1 - t);
    };
    place(0);
    token.style.opacity = "1";
    const tick = (now: number) => {
      const raw = Math.min(1, (now - started) / ROUTE_TRAVEL_MS);
      place(raw < 0.5 ? 2 * raw * raw : 1 - (-2 * raw + 2) ** 2 / 2);
      if (raw < 1) frame = requestAnimationFrame(tick);
      else token.style.opacity = "0";
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [newestHop?.id, newestHop?.d, animateRoute]);

  // What an area is, for a reader pointing at it: its size and the areas it
  // shares the most recorded references with. The status line keeps the action.
  const card = useMemo(() => {
    const node = hoverNode ? byId.get(hoverNode) : null;
    if (!node || node.kind !== "area" || !view || drag.current?.moved) return null;
    const point = points.get(node.id);
    if (!point) return null;
    const strongest = (side: "source" | "target") =>
      graph.edges
        .filter((edge) => edge.kind === "references" && edge.link && edge[side] === node.id)
        .sort((a, b) => b.link!.weight - a.link!.weight)
        .slice(0, 2)
        .map((edge) => {
          const other = byId.get(side === "source" ? edge.target : edge.source)!;
          return { id: other.id, title: other.title, color: other.color, weight: edge.link!.weight };
        });
    return {
      node,
      to: strongest("source"),
      from: strongest("target"),
      at: { x: point.x * view.k + view.x, y: point.y * view.k + view.y, r: node.radius * view.k },
    };
  }, [hoverNode, byId, view, points, graph]);
  const cardRef = useRef<HTMLDivElement>(null);

  // Placed once its size is known: beside the mark where there is room, else
  // under or over it, and always inside the map.
  useLayoutEffect(() => {
    const element = cardRef.current;
    if (!card || !element) return;
    const { x, y, r } = card.at;
    const w = element.offsetWidth;
    const h = element.offsetHeight;
    const gap = r + 12;
    const fits = (left: number, top: number) =>
      left >= 8 && top >= 8 && left + w <= size.width - 8 && top + h <= size.height - 8;
    const clampY = (top: number) => Math.max(8, Math.min(top, size.height - h - 8));
    const clampX = (left: number) => Math.max(8, Math.min(left, size.width - w - 8));
    const options = [
      { left: x + gap, top: clampY(y - 28) },
      { left: x - gap - w, top: clampY(y - 28) },
      { left: clampX(x - w / 2), top: y + gap },
      { left: clampX(x - w / 2), top: y - gap - h },
    ];
    const chosen = options.find((option) => fits(option.left, option.top)) ?? {
      left: clampX(x + gap),
      top: clampY(y + gap),
    };
    element.style.left = `${chosen.left}px`;
    element.style.top = `${chosen.top}px`;
    element.style.visibility = "visible";
  }, [card, size]);

  const activityLegend = useMemo(() => {
    const marks = Object.entries(activity ?? {}).filter(([id]) => byId.has(id));
    return {
      open: marks.some(([, mark]) => isOpenMark(mark)),
      changed: marks.some(([, mark]) => !isOpenMark(mark)),
    };
  }, [activity, byId]);

  const status = useMemo(() => {
    if (hoverEdge?.link) {
      const { link } = hoverEdge;
      const anchor = link.example.anchor;
      return {
        title: `${byId.get(link.source)?.title} → ${byId.get(link.target)?.title}`,
        detail: `${link.weight} recorded ${link.weight === 1 ? "reference" : "references"}${
          link.calls !== link.weight ? ` · ${link.calls} distinct pairs` : ""
        }${anchor?.file ? ` · ${anchor.file.split("/").pop()}:${anchor.line} · click to read it` : ""}`,
      };
    }
    const node = hoverNode ? byId.get(hoverNode) : null;
    if (node) {
      const summary = activity?.[node.id] ? markSummary(activity[node.id]) : "";
      return (
        describe?.(node) ?? {
          title: node.title,
          detail: summary ? `${summary} · Click to read this page` : "Click to read this page",
        }
      );
    }
    return idle ?? null;
  }, [hoverEdge, hoverNode, byId, idle, describe, activity]);

  return (
    <section
      className={`wiki-graph${graph.edges.filter((edge) => edge.kind === "references").length > 30 ? " is-dense" : ""}${
        embedded ? " is-embedded" : ""
      }${live ? " is-live" : ""}${readonly ? " is-readonly" : ""}${still ? " is-still" : ""}${surveying ? " is-surveying" : ""}${
        routeView ? " is-routing" : ""
      }`}
      ref={paneRef}
      aria-label="Map of this Wiki"
    >
      <svg
        ref={svgRef}
        width={size.width}
        height={size.height}
        onPointerDown={(event) => {
          if (!embedded) drag.current = { x: event.clientX, y: event.clientY, moved: false };
        }}
        onPointerMove={(event) => {
          const start = drag.current;
          if (!start || !(event.buttons & 1)) return;
          const dx = event.clientX - start.x;
          const dy = event.clientY - start.y;
          if (!start.moved && Math.hypot(dx, dy) < 4) return;
          start.moved = true;
          start.x = event.clientX;
          start.y = event.clientY;
          const current = viewRef.current;
          if (current) setMovedView({ ...current, x: current.x + dx, y: current.y + dy });
        }}
        onPointerUp={() => {
          // Let the click that ends a drag be ignored by nodes and edges.
          window.setTimeout(() => {
            drag.current = null;
          }, 0);
        }}
      >
        <defs>
          <marker id="wg-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="8" markerHeight="8" markerUnits="userSpaceOnUse" orient="auto-start-reverse">
            <path d="M0 0L8 4L0 8z" className="wg-arrow" />
          </marker>
          <marker id="wg-arrow-hot" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="8" markerHeight="8" markerUnits="userSpaceOnUse" orient="auto-start-reverse">
            <path d="M0 0L8 4L0 8z" className="wg-arrow is-hot" />
          </marker>
        </defs>
        {view && (
          <g transform={`translate(${view.x} ${view.y}) scale(${view.k})`}>
            {graph.edges.map((edge) => {
              const a = byId.get(edge.source);
              const b = byId.get(edge.target);
              const pa = points.get(edge.source);
              const pb = points.get(edge.target);
              if (!a || !b || !pa || !pb) return null;
              const hot = edge === hoverEdge || (!readonly && (edge.source === activeId || edge.target === activeId));
              const isReference = edge.kind === "references";
              const d = edgePath(pa, pb, a.radius, b.radius, isReference);
              return (
                <g key={edge.id} className={`wg-edge is-${edge.kind}${hot ? " is-hot" : ""}`}>
                  <path
                    d={d}
                    className="wg-edge-line"
                    strokeWidth={isReference ? referenceWidth(edge.link) : 1}
                    markerEnd={isReference ? `url(#wg-arrow${hot ? "-hot" : ""})` : undefined}
                    pathLength={live && isReference ? 1 : undefined}
                  />
                  {isReference && (
                    <path
                      d={d}
                      className="wg-edge-hit"
                      onPointerEnter={() => setHoverEdge(edge)}
                      onPointerLeave={() => setHoverEdge(null)}
                      onClick={() => {
                        if (!drag.current?.moved && edge.link) onOpenReference(edge.link);
                      }}
                    />
                  )}
                </g>
              );
            })}
            {routeView && (
              <g className="wg-route" aria-hidden="true">
                {routeView.hops.map((hop) => (
                  <path
                    key={hop.id}
                    ref={hop.newest ? newestHopRef : undefined}
                    d={hop.d}
                    className={`wg-route-line${hop.newest ? " is-newest" : ""}`}
                    stroke={hop.color}
                    pathLength={hop.newest ? 1 : undefined}
                  />
                ))}
                {onRoute &&
                  !readonly &&
                  routeView.hops.map((hop) => (
                    <path
                      key={`hit:${hop.id}`}
                      d={hop.d}
                      className="wg-route-hit"
                      onClick={() => {
                        if (!drag.current?.moved) onRoute();
                      }}
                    />
                  ))}
              </g>
            )}
            {graph.nodes.map((node, index) => {
              const active = !readonly && node.id === activeId;
              const point = points.get(node.id);
              if (!point) return null;
              const state = states?.[node.id];
              const mark = activity?.[node.id];
              const style = { "--node": node.color, "--i": index } as CSSProperties;
              return (
                <g
                  key={node.id}
                  className={`wg-node is-${node.kind}${active ? " is-active" : ""}${near.has(node.id) ? " is-near" : ""}${
                    state ? ` is-${state}` : ""
                  }${arrived?.has(node.id) ? " is-arrived" : ""}`}
                  style={style}
                  transform={`translate(${point.x} ${point.y})`}
                  role={readonly ? undefined : "link"}
                  tabIndex={readonly ? undefined : 0}
                  aria-label={
                    readonly ? undefined : mark ? `${node.title}, ${markSummary(mark)}` : node.title
                  }
                  aria-current={active ? "page" : undefined}
                  onPointerEnter={() => setHoverNode(node.id)}
                  onPointerLeave={() => setHoverNode(null)}
                  onFocus={() => setHoverNode(node.id)}
                  onBlur={() => setHoverNode(null)}
                  onClick={() => {
                    if (!readonly && !drag.current?.moved) onPick(node.id);
                  }}
                  onKeyDown={(event) => {
                    if (!readonly && (event.key === "Enter" || event.key === " ")) {
                      event.preventDefault();
                      onPick(node.id);
                    }
                  }}
                >
                  {/* A hit area larger than the mark. */}
                  <circle r={Math.max(node.radius + 6, 12)} className="wg-node-hit" />
                  {state === "running" && (
                    <>
                      <circle r={node.radius + 11} className="wg-node-halo" />
                      <circle r={node.radius + 5} className="wg-node-orbit" pathLength={100} />
                    </>
                  )}
                  {active && <circle r={node.radius + 5} className="wg-node-ring" />}
                  {routeView?.current.areaId === node.id && (
                    <g
                      key={`route-${routeView.at}`}
                      className={`wg-route-stop${animateRoute ? " is-moving" : ""}`}
                      style={{ "--arrive": `${routeView.crossed && animateRoute ? ROUTE_TRAVEL_MS : 0}ms` } as CSSProperties}
                    >
                      {animateRoute && <circle r={node.radius + 4} className="wg-route-ripple" />}
                      <circle r={node.radius + 5} className="wg-route-ring" />
                    </g>
                  )}
                  <circle r={node.radius} fill={node.color} className="wg-node-mark" />
                  {mark && (
                    <circle
                      cx={node.radius * 0.72}
                      cy={-node.radius * 0.72}
                      r={markRadius(node.radius)}
                      className={`wg-node-activity ${isOpenMark(mark) ? "is-open" : "is-changed"}`}
                    />
                  )}
                </g>
              );
            })}
            {/* The halo is its own layer under every label. Safari paints a stroked
                glyph over the one before it, so a halo on the label itself eats
                the letters. */}
            {(["halo", "text"] as const).map((layer) =>
              labels.map((label) => {
                const point = points.get(label.id)!;
                const x = point.x + label.dx / view.k;
                const state = `${label.id === activeId && !readonly ? " is-active" : ""}${
                  states?.[label.id] ? ` is-${states[label.id]}` : ""
                }`;
                return (
                  <text
                    key={`${layer}:${label.id}`}
                    x={x}
                    y={point.y + label.dy / view.k}
                    textAnchor={label.anchor}
                    fontSize={11.5 / view.k}
                    strokeWidth={layer === "halo" ? 4 / view.k : undefined}
                    aria-hidden={layer === "halo" ? true : undefined}
                    className={layer === "halo" ? `wg-label-halo${state}` : `wg-label${near.has(label.id) ? " is-near" : ""}${state}`}
                  >
                    {label.lines.map((line, index) => (
                      <tspan key={index} x={x} dy={index ? LABEL_LINE_PX / view.k : 0}>
                        {line}
                      </tspan>
                    ))}
                  </text>
                );
              }),
            )}
            {routeView && (
              <>
                <g ref={tokenRef} className="wg-route-token" style={{ opacity: 0 }} aria-hidden="true">
                  <circle r={6.5 / view.k} fill={newestHop?.color ?? "transparent"} />
                </g>
                {routeView.label && (
                <g
                  key={`route-label-${routeView.at}`}
                  className={`wg-route-label${animateRoute ? " is-moving" : ""}${onRoute && !readonly ? " is-link" : ""}`}
                  style={{ "--arrive": `${routeView.crossed && animateRoute ? ROUTE_TRAVEL_MS : 0}ms` } as CSSProperties}
                  onClick={() => {
                    if (onRoute && !readonly && !drag.current?.moved) onRoute();
                  }}
                >
                  {(["halo", "text"] as const).map((layer) => (
                    <text
                      key={layer}
                      x={routeView.label!.x}
                      y={routeView.label!.y}
                      textAnchor={routeView.label!.anchor}
                      fontSize={13 / view.k}
                      strokeWidth={layer === "halo" ? 5 / view.k : undefined}
                      aria-hidden={layer === "halo" ? true : undefined}
                      className={layer === "halo" ? "wg-route-name-halo" : "wg-route-name"}
                    >
                      {routeView.current.label}
                    </text>
                  ))}
                </g>
                )}
              </>
            )}
          </g>
        )}
      </svg>

      {card && (
        <div className="wg-card" ref={cardRef} style={{ visibility: "hidden" }} aria-hidden="true">
          <p className="wg-card-title">
            <span className="wg-card-dot" style={{ background: card.node.color }} />
            {card.node.title}
          </p>
          {card.node.symbols != null && (
            <p className="wg-card-size">
              {number.format(card.node.symbols)} {card.node.symbols === 1 ? "symbol" : "symbols"}
              {card.node.files != null && ` · ${number.format(card.node.files)} ${card.node.files === 1 ? "file" : "files"}`}
            </p>
          )}
          {(card.to.length > 0 || card.from.length > 0) && (
            <dl className="wg-card-links">
              {[
                ["To", card.to],
                ["From", card.from],
              ].flatMap(([word, rows]) =>
                (rows as typeof card.to).map((row) => (
                  <div key={`${word}:${row.id}`}>
                    <dt>
                      <span className="wg-card-word">{word as string}</span>
                      <span className="wg-card-dot" style={{ background: row.color }} />
                      <span className="wg-card-name">{row.title}</span>
                    </dt>
                    <dd>{number.format(row.weight)}</dd>
                  </div>
                )),
              )}
            </dl>
          )}
          <p className="wg-card-hint">Recorded references · click to read this area</p>
        </div>
      )}
      {header && <div className="wiki-graph-head">{header}</div>}
      {!embedded && (
        <div className="wiki-graph-controls">
          <button type="button" onClick={() => setMovedView(null)}>
            Fit
          </button>
          <button type="button" onClick={onHide}>
            Hide map
          </button>
        </div>
      )}

      <div className="wiki-graph-foot" role="status">
        {status ? (
          <p>
            <b>{status.title}</b> <span>{status.detail}</span>
          </p>
        ) : (
          <p>
            <span>{embedded ? "Click a page to read it" : "Drag to pan · Scroll to zoom · Click a page to read it"}</span>
          </p>
        )}
        {legend ?? (
        <dl className="wiki-graph-legend">
          {hasReferences && (
            <div>
              <dt className="is-references" aria-hidden="true" />
              <dd>References recorded in the code index</dd>
            </div>
          )}
          {routeView && (
            <div>
              <dt
                className="is-route"
                aria-hidden="true"
                style={{ background: routeSwatch(route?.stops ?? [], byId) }}
              />
              <dd>Main path</dd>
            </div>
          )}
          {graph.edges.some((edge) => edge.kind === "contains") && (
            <div>
              <dt className="is-contains" aria-hidden="true" />
              <dd>Page outline</dd>
            </div>
          )}
          {activityLegend.open && (
            <div>
              <dt className="is-activity-open" aria-hidden="true" />
              <dd>Open issues or pull requests on its code</dd>
            </div>
          )}
          {activityLegend.changed && (
            <div>
              <dt className="is-activity-changed" aria-hidden="true" />
              <dd>Several recent changes to its code</dd>
            </div>
          )}
        </dl>
        )}
        {note && (
          <p className="wiki-graph-note">
            {note}{" "}
            {onRetry && (
              <button type="button" onClick={onRetry}>
                Retry map
              </button>
            )}
          </p>
        )}
      </div>

      {onResizeReader && (
      <div
        className="wiki-graph-splitter"
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize the map and the reader"
        tabIndex={0}
        onKeyDown={(event) => {
          if (event.key === "ArrowLeft") onResizeReader(32);
          if (event.key === "ArrowRight") onResizeReader(-32);
        }}
        onPointerDown={(event) => {
          event.currentTarget.setPointerCapture(event.pointerId);
          let last = event.clientX;
          const target = event.currentTarget;
          const move = (next: PointerEvent) => {
            onResizeReader(last - next.clientX);
            last = next.clientX;
          };
          const up = () => {
            target.removeEventListener("pointermove", move);
            target.removeEventListener("pointerup", up);
          };
          target.addEventListener("pointermove", move);
          target.addEventListener("pointerup", up);
        }}
      />
      )}
    </section>
  );
}
