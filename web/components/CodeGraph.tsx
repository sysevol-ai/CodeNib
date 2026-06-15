"use client";

import { useEffect, useRef, useState } from "react";
import cytoscape, { type Core, type ElementDefinition, type NodeSingular } from "cytoscape";
import fcose from "cytoscape-fcose";
import type { CallSite, CodemapResponse } from "@/lib/api";

// Register the compound-aware force layout once (idempotent across HMR reloads).
const _g = globalThis as unknown as { __cmFcose?: boolean };
if (!_g.__cmFcose) {
  cytoscape.use(fcose);
  _g.__cmFcose = true;
}

export interface EdgeClickInfo {
  anchors: CallSite[];
  srcLabel: string;
  tgtLabel: string;
}

export interface GraphNodeInfo {
  label: string; // full unified_name (for refocus)
  short: string;
  file: string | null;
  line: number | null;
  endLine: number | null; // 1-based end of the definition, so the peek shows just this symbol
  kind: string;
  external: boolean; // defined outside this repo — no source to open
}

type CMNode = CodemapResponse["nodes"][number];

// Fixed, semantic colour per symbol kind — used in the expanded symbol view where
// the file box already groups by file, so colour is freed up for "what kind".
const KIND_COLORS: Record<string, string> = {
  function: "#2d7ff9",
  method: "#1098ad",
  class: "#ae3ec9",
  interface: "#0ca678",
  trait: "#3b5bdb",
  struct: "#e8590c",
  enum: "#9c36b5",
  type: "#0ca678",
  field: "#f08c00",
  var: "#f08c00",
  const: "#e8590c",
  module: "#868e96",
  file: "#868e96",
};
function colorForKind(kind: string): string {
  return KIND_COLORS[kind] || "#868e96";
}

// Files-overview model: the map opens with one "pill" per file (a readable
// overview), and a file expands into its real symbols on click. Ids are namespaced
// so a pill, its compound box, and its symbols never collide.
const META = "filemeta::"; // collapsed file pill
const BOX = "filebox::"; //   expanded file compound box
const fileKey = (n: CMNode) => (n.external ? "· external" : n.file || "· external");
const baseName = (f: string) => (f.startsWith("·") ? f : f.split("/").pop() || f);

// Build Cytoscape elements for the current expand state. A collapsed file is a
// single pill meta-node; an expanded file is a compound box holding its symbols.
// Symbol→symbol edges are aggregated onto whichever endpoints are visible (a
// symbol when its file is expanded, else the file pill); edges inside one
// collapsed file collapse away. Solid edges are exact references; dashed edges
// (`meta = 1`) are file-level aggregates that you drill into by expanding.
function buildElements(data: CodemapResponse, expanded: Set<string>): ElementDefinition[] {
  const byFile = new Map<string, CMNode[]>();
  const fileOfNode = new Map<string, string>();
  for (const n of data.nodes) {
    const f = fileKey(n);
    fileOfNode.set(n.id, f);
    let arr = byFile.get(f);
    if (!arr) byFile.set(f, (arr = []));
    arr.push(n);
  }

  const els: ElementDefinition[] = [];
  for (const [f, nodes] of byFile) {
    if (expanded.has(f)) {
      els.push({ data: { id: BOX + f, isFileBox: 1, file: f, fileLabel: `${baseName(f)}  ▾` } });
      for (const n of nodes) {
        const hidden = n.hidden_callees || 0;
        els.push({
          data: {
            id: n.id,
            parent: BOX + f,
            short: n.short,
            glabel: hidden ? `${n.short}  +${hidden}` : n.short, // node label, with hub badge
            flabel: n.label, // full unified_name, used to refocus
            file: n.file,
            line: n.line,
            endLine: n.end_line ?? null,
            kind: n.kind,
            accent: colorForKind(n.kind),
            importance: n.importance ?? 0,
            refCount: n.ref_count ?? 0,
            entryScore: n.entry_score ?? 0,
            root: n.is_root ? 1 : 0,
            external: n.external ? 1 : 0,
          },
        });
      }
    } else {
      let imp = 0;
      for (const n of nodes) imp = Math.max(imp, n.importance ?? 0);
      els.push({
        data: {
          id: META + f,
          isFileMeta: 1,
          file: f,
          short: baseName(f),
          glabel: `${baseName(f)}  ·${nodes.length}`,
          kind: "file",
          accent: colorForKind("file"),
          importance: imp,
          count: nodes.length,
          pill: Math.min(1, nodes.length / 20), // clamped size cue: bigger file = bigger pill
          root: nodes.some((n) => n.is_root) ? 1 : 0,
          external: f === "· external" ? 1 : 0,
        },
      });
    }
  }

  // Map a symbol id to its currently-visible node id (itself, or its file pill).
  const visId = (id: string): string => {
    const f = fileOfNode.get(id);
    if (f === undefined) return id;
    return expanded.has(f) ? id : META + f;
  };
  const shortById = new Map(data.nodes.map((n) => [n.id, n.short]));

  type Agg = { weight: number; anchors: CallSite[]; symbolEdge: boolean; srcShort: string; tgtShort: string };
  const agg = new Map<string, Agg>();
  for (const e of data.edges) {
    if (!e.source || !e.target) continue;
    const vs = visId(e.source);
    const vt = visId(e.target);
    if (vs === vt) continue; // self-loop, or both inside one collapsed file
    const symbolEdge = !vs.startsWith(META) && !vt.startsWith(META);
    const key = `${vs}\u0000${vt}`;
    let a = agg.get(key);
    if (!a) agg.set(key, (a = { weight: 0, anchors: [], symbolEdge, srcShort: "", tgtShort: "" }));
    a.weight += e.weight ?? e.anchors?.length ?? 0;
    if (symbolEdge && e.anchors) a.anchors.push(...e.anchors);
    if (symbolEdge) {
      a.srcShort = shortById.get(e.source) || "";
      a.tgtShort = shortById.get(e.target) || "";
    }
  }
  let i = 0;
  for (const [key, a] of agg) {
    const [source, target] = key.split("\u0000");
    els.push({
      data: {
        id: `e${i++}`,
        source,
        target,
        anchors: a.symbolEdge ? a.anchors : [],
        weight: a.weight || 1, // # call sites -> drives edge width
        hasAnchor: a.symbolEdge && a.anchors.length ? 1 : 0,
        meta: a.symbolEdge ? 0 : 1, // file-level aggregate (dashed) vs exact reference
        srcShort: a.srcShort,
        tgtShort: a.tgtShort,
      },
    });
  }
  return els;
}

// The subsystem "spine": the few highest-importance files that actually take part
// in a cross-file edge. We auto-expand these on load so the default view shows real
// symbol-level calls through the core, while the long tail of singletons stays as
// pills — a level-of-detail middle ground between Files and Symbols.
function spineFiles(data: CodemapResponse, maxFiles = 4, maxSymbols = 16): Set<string> {
  const fileOf = new Map<string, string>();
  const score = new Map<string, number>();
  const size = new Map<string, number>();
  for (const n of data.nodes) {
    const f = fileKey(n);
    if (f === "· external") continue;
    fileOf.set(n.id, f);
    score.set(f, Math.max(score.get(f) ?? 0, n.is_root ? 1 : n.importance ?? 0));
    size.set(f, (size.get(f) ?? 0) + 1);
  }
  // Only files with a cross-file edge — expanding an isolated singleton reveals nothing.
  const connected = new Set<string>();
  for (const e of data.edges) {
    const sf = fileOf.get(e.source);
    const tf = fileOf.get(e.target);
    if (sf && tf && sf !== tf) {
      connected.add(sf);
      connected.add(tf);
    }
  }
  const ranked = [...connected].sort((a, b) => (score.get(b) ?? 0) - (score.get(a) ?? 0));
  const out = new Set<string>();
  let symbols = 0;
  for (const f of ranked) {
    if (out.size >= maxFiles || symbols >= maxSymbols) break;
    out.add(f);
    symbols += size.get(f) ?? 0;
  }
  return out;
}

// Lay out the current elements with fcose (compound-aware spring embedder), then
// fit. packComponents keeps disconnected pieces from sprawling across the canvas.
// `stable` warm-starts from current positions and skips the refit so expand/
// collapse stays anchored.
function runLayout(cy: Core, opts: { stable?: boolean } = {}): void {
  if (cy.nodes().length === 0) return;
  cy.layout({
    name: "fcose",
    quality: "proof",
    randomize: !opts.stable,
    animate: false,
    fit: !opts.stable,
    padding: 28,
    packComponents: true,
    nodeSeparation: 78,
    idealEdgeLength: 95,
    nodeRepulsion: 6500,
    nestingFactor: 0.1,
    gravity: 0.3,
    gravityCompound: 1.2,
    tile: true,
  } as any).run();
  if (!opts.stable && cy.zoom() > 1.2) {
    cy.zoom(1.2);
    cy.center();
  }
}

interface HoverInfo {
  short: string;
  file: string | null;
  line: number | null;
  kind: string;
}

/**
 * Interactive dependency graph (Cytoscape). Opens as a files overview — one pill
 * per file — and a file expands into its real symbols on click; click again (or
 * the file box) to collapse. Click a symbol to focus + open its source; click an
 * edge to open the exact SCIP/LSP call site.
 */
export default function CodeGraph({
  data,
  variant = "explore",
  onNodeClick,
  onEdgeClick,
}: {
  data: CodemapResponse;
  // "wiki" = the focused map embedded in a wiki page, tuned for reading.
  // "explore" = the standalone Graph view with richer interaction.
  variant?: "wiki" | "explore";
  onNodeClick?: (node: GraphNodeInfo) => void;
  onEdgeClick?: (info: EdgeClickInfo) => void;
}) {
  const boxRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  // Which files are expanded to their symbols (default: all collapsed = overview).
  const expandedRef = useRef<Set<string>>(new Set());
  // Id of the node held in persistent focus (click-to-focus). While set, hover
  // previews don't clobber the focus highlight; Esc / background-tap clears it.
  const focusedRef = useRef<string | null>(null);
  const [hover, setHover] = useState<HoverInfo | null>(null);
  const [edgeHover, setEdgeHover] = useState<{ count: number; openable: boolean } | null>(null);
  // View mode: "files" = collapsed files overview (expand on click); "symbols"
  // = every symbol shown at once (every file expanded), same fcose layout.
  const [mode, setMode] = useState<"files" | "symbols">("files");

  // Keep the click callbacks in refs so the cytoscape instance is built once
  // per `data` and is NOT torn down/rebuilt when a parent re-render hands us new
  // handler identities (e.g. opening the source peek) — that would reset zoom/pan.
  const onNodeClickRef = useRef(onNodeClick);
  const onEdgeClickRef = useRef(onEdgeClick);
  useEffect(() => {
    onNodeClickRef.current = onNodeClick;
    onEdgeClickRef.current = onEdgeClick;
  });

  useEffect(() => {
    const box = boxRef.current;
    if (!box) return;

    const dark = document.documentElement.dataset.theme === "dark";

    // "files" opens as the hybrid spine (key files expanded, tail as pills);
    // "symbols" expands every file.
    const initExpanded = new Set<string>();
    if (mode === "symbols") for (const n of data.nodes) initExpanded.add(fileKey(n));
    else for (const f of spineFiles(data)) initExpanded.add(f);
    expandedRef.current = initExpanded;

    const nodeText = dark ? "#e6e6e6" : "#1f2937";
    const nodeBg = dark ? "#1b1d21" : "#ffffff";
    const edgeColor = dark ? "#4b5159" : "#c9ccd1";

    const cy = cytoscape({
      container: box,
      elements: buildElements(data, expandedRef.current),
      wheelSensitivity: 0.2,
      minZoom: 0.2,
      maxZoom: 2.5,
      style: [
        {
          selector: "node",
          style: {
            shape: "round-rectangle",
            "background-color": nodeBg,
            "border-width": 2,
            "border-color": "data(accent)",
            label: "data(glabel)",
            color: nodeText,
            // Size by importance (PageRank percentile): core symbols read bigger.
            "font-size": "mapData(importance, 0, 1, 11, 15)",
            "font-family": "var(--font-geist-sans), ui-sans-serif, system-ui, sans-serif",
            "text-valign": "center",
            "text-halign": "center",
            "text-wrap": "wrap",
            "text-max-width": "180px",
            width: "label",
            height: "label",
            padding: "mapData(importance, 0, 1, 7, 20)",
            "transition-property": "border-width, background-color",
            "transition-duration": 120,
          },
        },
        {
          selector: "node[root = 1]",
          style: {
            "background-color": "#2d7ff9",
            "border-color": "#1b5fd0",
            color: "#ffffff",
            "font-weight": 600,
          },
        },
        {
          selector: "node[external = 1]",
          style: {
            "border-style": "dashed",
            "border-color": dark ? "#4b5159" : "#b3b8c0",
            color: dark ? "#8b9099" : "#9099a3",
            "font-style": "italic",
          },
        },
        {
          // Expanded file container: a titled box wrapping its symbols.
          selector: "node[isFileBox = 1]",
          style: {
            shape: "round-rectangle",
            "background-color": dark ? "#13161b" : "#faf9f7",
            "background-opacity": 1,
            "border-width": 1,
            "border-color": dark ? "#2a3038" : "#e3e1dc",
            label: "data(fileLabel)",
            color: dark ? "#9198a1" : "#8a8880",
            "font-size": 11,
            "font-weight": 600,
            "font-style": "normal",
            "text-valign": "top",
            "text-halign": "center",
            "text-margin-y": 4,
            padding: "12px",
            "z-index": 0,
          },
        },
        {
          // Collapsed file pill (the overview): a soft card standing in for a
          // file. Sized a touch by symbol count (a light "weight" cue).
          selector: "node[isFileMeta = 1]",
          style: {
            shape: "round-rectangle",
            "corner-radius": "11px",
            "background-color": dark ? "#262b33" : "#f1f5fa",
            "background-opacity": 1,
            "background-fill": "linear-gradient",
            "background-gradient-stop-colors": dark ? ["#2a313a", "#20242b"] : ["#f7fafd", "#e8eef5"],
            "background-gradient-direction": "to-bottom",
            "border-width": 1.5,
            "border-style": "solid",
            "border-color": dark ? "#3d4654" : "#c3cedd",
            label: "data(glabel)",
            color: dark ? "#dfe4ea" : "#2c3540",
            "font-size": 13,
            "font-weight": 600,
            "font-style": "normal",
            "text-valign": "center",
            "text-halign": "center",
            "text-wrap": "wrap",
            "text-max-width": "210px",
            width: "label",
            height: "label",
            padding: "mapData(pill, 0, 1, 13, 23)",
          },
        },
        // Neighbours of the focused node — kept fully legible (no dim).
        {
          selector: "node.hl",
          style: { "border-width": 3, opacity: 1 },
        },
        // The focused node itself: a filled accent so it POPS out of context.
        {
          selector: "node.focus",
          style: {
            "border-width": 4,
            "border-color": "#2563eb",
            "background-color": dark ? "#1e3a8a" : "#dbeafe",
            color: dark ? "#ffffff" : "#13367a",
            "font-weight": 700,
            "z-index": 30,
          },
        },
        {
          selector: "edge",
          style: {
            // Width by call-site count (weight): a heavily-used call reads thicker.
            width: "mapData(weight, 1, 12, 1.2, 4.5)",
            "line-color": edgeColor,
            "target-arrow-color": edgeColor,
            "target-arrow-shape": "triangle",
            "arrow-scale": 0.9,
            // Gentle arcs (not straight) so edges around a hub fan out cleanly.
            "curve-style": "unbundled-bezier",
            "control-point-distances": [30],
            "control-point-weights": [0.5],
            opacity: 0.72,
          },
        },
        {
          // File-level aggregate edge (an expand reveals the exact references).
          selector: "edge[meta = 1]",
          style: { "line-style": "dashed", opacity: 0.55 },
        },
        {
          selector: "edge.hl",
          style: {
            "line-color": "#2563eb",
            "target-arrow-color": "#2563eb",
            width: "mapData(weight, 1, 12, 2, 5.5)",
            opacity: 1,
            "z-index": 30,
          },
        },
        {
          selector: "edge.faded",
          style: { opacity: 0.12 },
        },
      ],
    });
    cyRef.current = cy;
    // Test/e2e seam (dev only): expose the instance on the container so
    // screenshot scripts can drive interactions.
    if (process.env.NODE_ENV !== "production") {
      (box as unknown as { __cy?: Core }).__cy = cy;
    }

    focusedRef.current = null;

    // Focus = the clicked node + its neighbourhood POP via accent. We never dim
    // the *nodes* (every node's content stays fully legible — no grey cover);
    // only the unrelated *edges* fade so the focused path reads clearly.
    const focusNode = (n: NodeSingular) => {
      const nbr = n.closedNeighborhood();
      cy.batch(() => {
        cy.elements().removeClass("focus hl faded");
        cy.edges().not(nbr.edges()).addClass("faded");
        n.addClass("focus");
        nbr.nodes().not(n).addClass("hl");
        n.connectedEdges().addClass("hl");
      });
    };
    const clearFocus = () => {
      focusedRef.current = null;
      cy.elements().removeClass("focus hl faded");
    };

    // Expand/collapse a file: rebuild the element set for the new state and re-lay
    // it out. Cheap (<=120 nodes) and keeps meta-edge aggregation correct.
    const toggleFile = (file: string) => {
      const s = expandedRef.current;
      const willExpand = !s.has(file);

      // Anchor on the clicked node (pill when expanding, box when collapsing).
      const anchor = cy.getElementById(willExpand ? META + file : BOX + file);
      const anchorScreen = anchor.nonempty() ? { ...anchor.renderedPosition() } : null;
      const anchorModel = anchor.nonempty() ? { ...anchor.position() } : null;

      if (willExpand) s.add(file);
      else s.delete(file);
      focusedRef.current = null;
      setHover(null);

      // Keep surviving nodes where they are; seed the new box + symbols at the
      // clicked file so they grow out from there instead of the origin.
      const prev = new Map<string, { x: number; y: number }>();
      cy.nodes().forEach((n) => {
        prev.set(n.id(), { ...n.position() });
      });
      cy.batch(() => {
        cy.elements().remove();
        cy.add(buildElements(data, s));
        cy.nodes().forEach((n) => {
          const p = prev.get(n.id());
          if (p) n.position(p);
          else if (anchorModel)
            n.position({
              x: anchorModel.x + (Math.random() - 0.5) * 40,
              y: anchorModel.y + (Math.random() - 0.5) * 40,
            });
        });
      });
      runLayout(cy, { stable: true });

      // Pin the clicked file back where it was clicked so it expands in place.
      if (anchorScreen) {
        const after = cy.getElementById(willExpand ? BOX + file : META + file);
        if (after.nonempty()) {
          const now = after.renderedPosition();
          cy.panBy({ x: anchorScreen.x - now.x, y: anchorScreen.y - now.y });
        }
      }
    };

    // Hover: a transient preview of the node + its neighbours. Suppressed while a
    // node is in persistent focus so it doesn't fight the focus highlight.
    cy.on("mouseover", "node", (evt) => {
      const n = evt.target;
      const d = n.data();
      if (d.isFileBox) return; // the box title is for collapsing, handled on tap
      box.style.cursor = "pointer";
      if (d.isFileMeta) {
        setHover({ short: d.short, file: d.file, line: null, kind: `${d.count} symbols · click to open` });
      } else {
        setHover({ short: d.short, file: d.file, line: d.line, kind: d.kind });
      }
      if (focusedRef.current) return;
      const nbr = n.closedNeighborhood();
      cy.edges().not(nbr.edges()).addClass("faded"); // dim only unrelated edges
      n.addClass("hl");
      nbr.nodes().not(n).addClass("hl");
      n.connectedEdges().addClass("hl");
    });
    cy.on("mouseout", "node", () => {
      box.style.cursor = "";
      setHover(null);
      if (focusedRef.current) return;
      cy.elements().removeClass("faded hl");
    });
    // Click a file pill / box → expand or collapse it. Click a symbol → focus it
    // (persistent degree-of-interest) AND open its source peek.
    cy.on("tap", "node", (evt) => {
      const d = evt.target.data();
      if (d.isFileMeta || d.isFileBox) {
        toggleFile(d.file);
        return;
      }
      focusedRef.current = d.id;
      focusNode(evt.target);
      onNodeClickRef.current?.({
        label: d.flabel,
        short: d.short,
        file: d.file ?? null,
        line: d.line ?? null,
        endLine: d.endLine ?? null,
        kind: d.kind,
        external: d.external === 1,
      });
    });

    // Edge hover: spotlight the relationship + show its call-site count.
    cy.on("mouseover", "edge", (evt) => {
      const e = evt.target;
      box.style.cursor = e.data("hasAnchor") ? "pointer" : "default";
      setEdgeHover({ count: e.data("weight") || 0, openable: !!e.data("hasAnchor") });
      if (focusedRef.current) return;
      cy.edges().not(e).addClass("faded"); // dim other edges; leave all nodes legible
      e.addClass("hl");
      e.connectedNodes().addClass("hl");
    });
    cy.on("mouseout", "edge", () => {
      box.style.cursor = "";
      setEdgeHover(null);
      if (focusedRef.current) return;
      cy.elements().removeClass("faded hl");
    });
    // Click an exact edge → open its LSP/SCIP call site(s). Aggregate (dashed)
    // edges carry no single site — expand the files to reach them.
    cy.on("tap", "edge", (evt) => {
      const d = evt.target.data();
      if (d.anchors?.length) {
        onEdgeClickRef.current?.({ anchors: d.anchors, srcLabel: d.srcShort, tgtLabel: d.tgtShort });
      }
    });
    // Click empty canvas → clear focus.
    cy.on("tap", (evt) => {
      if (evt.target === cy) clearFocus();
    });
    // Esc → clear focus from anywhere.
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") clearFocus();
    };
    document.addEventListener("keydown", onKey);

    runLayout(cy); // initial layout

    return () => {
      document.removeEventListener("keydown", onKey);
      cy.destroy();
      cyRef.current = null;
    };
  }, [data, mode]); // rebuild on a new graph or a view-mode switch; refs carry callbacks

  return (
    <div className="codegraph" data-variant={variant}>
      <div className="codegraph-canvas" ref={boxRef} aria-label="dependency graph" />
      <div className="codegraph-bar mono">
        {edgeHover ? (
          <span className="cg-info">
            <b>
              {edgeHover.count} call site{edgeHover.count === 1 ? "" : "s"}
            </b>
            {edgeHover.openable ? (
              <span className="muted"> — click the edge to open the exact reference line</span>
            ) : (
              <span className="muted"> — expand the files to reach the call sites</span>
            )}
          </span>
        ) : hover ? (
          <span className="cg-info">
            <b>{hover.short}</b>
            {hover.file ? ` — ${hover.file}${hover.line ? `:${hover.line}` : ""}` : ""}
            <span className="codegraph-kind">{hover.kind}</span>
          </span>
        ) : (
          <span className="codegraph-legend">
            <span>
              <span className="lk-swatch" />
              each pill = a file
            </span>
            <span>
              <span className="lk-grow" />
              bigger = more referenced
            </span>
            <span>
              <span className="lk-ext" />
              external
            </span>
          </span>
        )}
        <div className="codegraph-modes" role="group" aria-label="View mode">
          <button
            type="button"
            className={mode === "files" ? "on" : ""}
            onClick={() => setMode("files")}
            title="Overview: key files expanded to symbols, the rest as pills you can expand"
          >
            Overview
          </button>
          <button
            type="button"
            className={mode === "symbols" ? "on" : ""}
            onClick={() => setMode("symbols")}
            title="All symbols: every file expanded at once"
          >
            All symbols
          </button>
        </div>
        <span
          className="codegraph-depnote"
          title="Solid edges are exact SCIP/LSP references; dashed edges are file-level aggregates you expand to reach."
        >
          click a file to expand · its box to collapse
        </span>
        <button type="button" className="codegraph-fit" onClick={() => cyRef.current?.fit(undefined, 24)}>
          Fit
        </button>
      </div>
    </div>
  );
}
