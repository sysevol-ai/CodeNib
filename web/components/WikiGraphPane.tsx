// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";

import type { WikiAreaLink } from "@/lib/api";
import { neighborhood, type WikiGraph, type WikiGraphEdge, type WikiGraphNode } from "@/lib/wikiGraph";
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
  text: string;
  anchor: "start" | "middle" | "end";
  dx: number;
  dy: number;
}

const short = (title: string) => (title.length > 30 ? `${title.slice(0, 29)}…` : title);
// Geist at the label size averages about this wide per character.
const LABEL_CHAR_PX = 6.1;
const LABEL_HEIGHT_PX = 15;
const LABEL_MARGIN_PX = 8;
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
    const text = short(node.title);
    const width = text.length * LABEL_CHAR_PX;
    const sx = point.x * view.k + view.x;
    const sy = point.y * view.k + view.y;
    const radius = node.radius * view.k;
    const away = point.x - center.x;
    const beside = Math.abs(away) > 40;
    let anchor: PlacedLabel["anchor"] = !beside ? "middle" : away > 0 ? "start" : "end";
    let dx = !beside ? 0 : away > 0 ? radius + 7 : -(radius + 7);
    const dy = beside ? 4 : point.y < center.y ? -(radius + 8) : radius + 15;
    // Near an edge of the pane, the label moves to the node's inner side.
    if (anchor === "end" && sx + dx - width < LABEL_MARGIN_PX) {
      anchor = "start";
      dx = radius + 7;
    } else if (anchor === "start" && sx + dx + width > paneWidth - LABEL_MARGIN_PX) {
      anchor = "end";
      dx = -(radius + 7);
    }
    const left = sx + dx - (anchor === "middle" ? width / 2 : anchor === "end" ? width : 0);
    const box = { left, right: left + width, top: sy + dy - 11, bottom: sy + dy - 11 + LABEL_HEIGHT_PX };
    const hits = (other: typeof box) =>
      box.left < other.right && box.right > other.left && box.top < other.bottom && box.bottom > other.top;
    const clash = taken.some(hits) || marks.some((mark) => mark.id !== node.id && hits(mark));
    const outside = box.left < 0 || box.right > paneWidth;
    if ((clash || outside) && priority(node) < 4) continue;
    taken.push(box);
    placed.push({ id: node.id, text, anchor, dx, dy });
  }
  return placed;
}

/** A curved path between two nodes, so two-way references stay two visible lines. */
function edgePath(a: Point, b: Point, ar: number, br: number, curved: boolean): string {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const length = Math.max(Math.hypot(dx, dy), 1);
  const ux = dx / length;
  const uy = dy / length;
  const x1 = a.x + ux * ar;
  const y1 = a.y + uy * ar;
  const x2 = b.x - ux * (br + (curved ? 3 : 0));
  const y2 = b.y - uy * (br + (curved ? 3 : 0));
  if (!curved) return `M${x1} ${y1}L${x2} ${y2}`;
  const bend = Math.min(length * 0.12, 34);
  return `M${x1} ${y1}Q${(x1 + x2) / 2 - uy * bend} ${(y1 + y2) / 2 + ux * bend} ${x2} ${y2}`;
}

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
}) {
  const paneRef = useRef<HTMLElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  // Null until the reader pans or zooms; until then the view is the fitted one.
  const [movedView, setMovedView] = useState<View | null>(null);
  const [hoverNode, setHoverNode] = useState<string | null>(null);
  const [hoverEdge, setHoverEdge] = useState<WikiGraphEdge | null>(null);
  const drag = useRef<{ x: number; y: number; moved: boolean } | null>(null);

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
    const fitY = (size.height - 170 - (header ? HEADER_PX : 0)) / Math.max(maxY - minY, 1);
    const scale = Math.min(fitX, fitY);
    const stretchX = Math.min(Math.max(fitX, 0.1), scale * 1.45, 2.4);
    const stretchY = Math.min(Math.max(fitY, 0.1), scale * 1.45, 2.4);
    for (const node of graph.nodes) next.set(node.id, { x: node.x * stretchX, y: node.y * stretchY });
    return {
      points: next,
      center: { x: ((minX + maxX) / 2) * stretchX, y: ((minY + maxY) / 2) * stretchY },
    };
  }, [graph, size, header]);

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
      if (node.id === hoverNode || node.id === activeId) return 4;
      if (node.kind === "area") return 3;
      if (near.has(node.id)) return 2;
      return view.k >= 1.7 ? 1 : 0;
    });
  }, [graph, points, view, center, size.width, hoverNode, activeId, near]);

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
    if (node) return { title: node.title, detail: "Click to read this page" };
    return idle ?? null;
  }, [hoverEdge, hoverNode, byId, idle]);

  return (
    <section
      className={`wiki-graph${graph.edges.filter((edge) => edge.kind === "references").length > 30 ? " is-dense" : ""}${
        embedded ? " is-embedded" : ""
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
              const hot = edge === hoverEdge || edge.source === activeId || edge.target === activeId;
              const isReference = edge.kind === "references";
              const d = edgePath(pa, pb, a.radius, b.radius, isReference);
              return (
                <g key={edge.id} className={`wg-edge is-${edge.kind}${hot ? " is-hot" : ""}`}>
                  <path
                    d={d}
                    className="wg-edge-line"
                    strokeWidth={isReference ? referenceWidth(edge.link) : 1}
                    markerEnd={isReference ? `url(#wg-arrow${hot ? "-hot" : ""})` : undefined}
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
            {graph.nodes.map((node) => {
              const active = node.id === activeId;
              const point = points.get(node.id);
              if (!point) return null;
              return (
                <g
                  key={node.id}
                  className={`wg-node is-${node.kind}${active ? " is-active" : ""}${near.has(node.id) ? " is-near" : ""}`}
                  transform={`translate(${point.x} ${point.y})`}
                  role="link"
                  tabIndex={0}
                  aria-label={node.title}
                  aria-current={active ? "page" : undefined}
                  onPointerEnter={() => setHoverNode(node.id)}
                  onPointerLeave={() => setHoverNode(null)}
                  onClick={() => {
                    if (!drag.current?.moved) onPick(node.id);
                  }}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      onPick(node.id);
                    }
                  }}
                >
                  {/* A hit area larger than the mark. */}
                  <circle r={Math.max(node.radius + 6, 12)} className="wg-node-hit" />
                  {active && <circle r={node.radius + 5} className="wg-node-ring" />}
                  <circle r={node.radius} fill={node.color} className="wg-node-mark" />
                </g>
              );
            })}
            {labels.map((label) => {
              const point = points.get(label.id)!;
              return (
                <text
                  key={label.id}
                  x={point.x + label.dx / view.k}
                  y={point.y + label.dy / view.k}
                  textAnchor={label.anchor}
                  fontSize={11.5 / view.k}
                  strokeWidth={3 / view.k}
                  className={`wg-label${near.has(label.id) ? " is-near" : ""}${label.id === activeId ? " is-active" : ""}`}
                >
                  {label.text}
                </text>
              );
            })}
          </g>
        )}
      </svg>

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
        <dl className="wiki-graph-legend">
          {hasReferences && (
            <div>
              <dt className="is-references" aria-hidden="true" />
              <dd>References recorded in the code index</dd>
            </div>
          )}
          <div>
            <dt className="is-contains" aria-hidden="true" />
            <dd>Page outline</dd>
          </div>
        </dl>
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
