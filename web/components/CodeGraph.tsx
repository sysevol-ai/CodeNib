"use client";

import { useEffect, useRef, useState } from "react";
import cytoscape, { type Core, type ElementDefinition, type NodeSingular } from "cytoscape";
import type { CallSite, CodemapResponse } from "@/lib/api";
import { computeFilesPositions, type FilesLayoutItem } from "@/lib/filesLayout";

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
  kind: string;
  external: boolean; // defined outside this repo — no source to open
}

// Fixed, semantic colour per symbol kind — used in the Files layout where
// position already encodes file+line, so colour is freed up for "what kind".
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

// Pure adapter: typed codemap payload -> Cytoscape elements. All visual encoding
// is driven by these node/edge `data` fields plus the stylesheet (mapData mappers
// + selectors) — never by imperative per-node styling. Deterministic (no DOM /
// theme access), so it is unit-testable and the render effect stays a thin shell.
function adaptGraphView(data: CodemapResponse): ElementDefinition[] {
  const shortById = new Map(data.nodes.map((n) => [n.id, n.short]));
  return [
    ...data.nodes.map((n) => {
      const hidden = n.hidden_callees || 0;
      const kindColor = colorForKind(n.kind);
      return {
        data: {
          id: n.id,
          short: n.short, // clean name (used by the source peek)
          glabel: hidden ? `${n.short}  +${hidden}` : n.short, // node label, with hub badge
          flabel: n.label, // full unified_name, used to refocus
          file: n.file,
          line: n.line,
          endLine: n.end_line ?? null,
          kind: n.kind,
          accent: kindColor, // border colour by symbol kind (position encodes file+dependency)
          importance: n.importance ?? 0,
          refCount: n.ref_count ?? 0, // referenced-by count (Phase 3/4 weighting)
          entryScore: n.entry_score ?? 0, // entry-point-ness in [0,1]
          root: n.is_root ? 1 : 0,
          external: n.external ? 1 : 0,
        },
      };
    }),
    ...data.edges
      .filter((e) => e.source && e.target)
      .map((e, i) => {
        const anchors = e.anchors || [];
        return {
          data: {
            id: `e${i}`,
            source: e.source,
            target: e.target,
            anchors,
            weight: e.weight ?? anchors.length, // # call sites -> drives edge width
            hasAnchor: anchors.length ? 1 : 0,
            srcShort: shortById.get(e.source) || "",
            tgtShort: shortById.get(e.target) || "",
          },
        };
      }),
  ];
}

// Files layout: a compound box per file, symbols stacked top→bottom by line
// inside it, boxes packed into balanced columns. Here POSITION encodes the
// symbol's code location (file + line) — the core "graph aligns with code" fix.
// The packing math lives in computeFilesPositions() (pure, unit-tested); this
// just creates the compound boxes and applies the computed positions to `cy`.
function runFilesLayout(cy: Core): void {
  const items: FilesLayoutItem[] = [];
  cy.nodes().forEach((n) => {
    if (n.data("isFileBox")) return;
    items.push({
      id: n.id(),
      file: (n.data("file") as string) || null,
      line: (n.data("line") as number) ?? null,
      external: !!n.data("external"),
      // measured label box → non-overlapping lanes (w) and rows/bands (h)
      width: n.outerWidth() || undefined,
      height: n.outerHeight() || undefined,
    });
  });

  // Symbol→symbol edges drive the dependency layering (caller file above callee
  // file). Read them off cy before the file boxes are added (boxes have none).
  const edges = cy
    .edges()
    .map((e) => ({ source: e.source().id(), target: e.target().id() }));
  const { boxes, positions, parentOf } = computeFilesPositions(items, edges);

  // Create one compound box per file and reparent its symbols into it.
  cy.batch(() => {
    for (const b of boxes) {
      cy.add({
        group: "nodes",
        data: { id: b.id, isFileBox: 1, fileLabel: b.file.split("/").pop() || b.file },
      });
    }
    cy.nodes().forEach((n) => {
      const parent = parentOf[n.id()];
      if (parent) n.move({ parent });
    });
  });

  // Apply the computed grid positions (position = file + line).
  cy.batch(() => {
    cy.nodes().forEach((n) => {
      const pos = positions[n.id()];
      if (pos) n.position(pos);
    });
  });

  cy.layout({ name: "preset", fit: true, padding: 28 } as any).run();
  if (cy.zoom() > 1.2) {
    cy.zoom(1.2);
    cy.center();
  }
}

// Remove any file-box compound structure (used when leaving Files mode).
function clearFileBoxes(cy: Core): void {
  const boxes = cy.nodes("[isFileBox = 1]");
  if (!boxes.length) return;
  cy.batch(() => {
    cy.nodes().forEach((n) => {
      if (!n.data("isFileBox") && n.isChild()) n.move({ parent: null });
    });
    cy.remove(boxes);
  });
}

interface HoverInfo {
  short: string;
  file: string | null;
  line: number | null;
  kind: string;
}

/**
 * Interactive top-down dependency graph (Cytoscape). A single view: file boxes
 * laid out by dependency depth (callers/entry on top → leaves below), symbols by
 * line within each box. Click a node to focus its neighbourhood (no grey-out),
 * hover for file:line, click an edge to open the exact call site.
 */
export default function CodeGraph({
  data,
  variant = "explore",
  onNodeClick,
  onEdgeClick,
}: {
  data: CodemapResponse;
  // "wiki" = the focused top-down dependency map embedded in a wiki page: a
  // single dependency layout, no mode toggle, tuned for reading. "explore" =
  // the standalone Graph view: layout toggles + richer interaction.
  variant?: "wiki" | "explore";
  onNodeClick?: (node: GraphNodeInfo) => void;
  onEdgeClick?: (info: EdgeClickInfo) => void;
}) {
  const boxRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  // Id of the node held in persistent focus (click-to-focus). While set, hover
  // previews don't clobber the focus highlight; Esc / background-tap clears it.
  const focusedRef = useRef<string | null>(null);
  const [hover, setHover] = useState<HoverInfo | null>(null);
  const [edgeHover, setEdgeHover] = useState<{ count: number } | null>(null);

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

    // Build Cytoscape elements from the typed codemap payload (pure adapter).
    const elements = adaptGraphView(data);

    const nodeText = dark ? "#e6e6e6" : "#1f2937";
    const nodeBg = dark ? "#1b1d21" : "#ffffff";
    const edgeColor = dark ? "#4b5159" : "#c9ccd1";

    const cy = cytoscape({
      container: box,
      elements,
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
          // File container (Files layout): a titled box wrapping its symbols.
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
        // Neighbours of the focused node — kept fully legible (no dim).
        {
          selector: "node.hl",
          style: { "border-width": 3, opacity: 1 },
        },
        // The focused node itself: a filled accent so it POPS out of context,
        // instead of the old effect where focusing just greyed everything out.
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
            // weight 0 (no resolved anchors) clamps to the thinnest width.
            width: "mapData(weight, 1, 12, 1.2, 4.5)",
            "line-color": edgeColor,
            "target-arrow-color": edgeColor,
            "target-arrow-shape": "triangle",
            "arrow-scale": 0.9,
            // Gentle arcs (not straight) so the many edges around a hub fan out
            // and stop overlapping/clashing.
            "curve-style": "unbundled-bezier",
            "control-point-distances": [30],
            "control-point-weights": [0.5],
            opacity: 0.72,
          },
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

    // Layout runs in a dedicated effect below (so toggling Flow/Clusters re-lays
    // out without rebuilding the graph and losing zoom/pan).

    // A fresh graph build invalidates any prior focus.
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

    // Hover: a transient preview of the node + its neighbours. Suppressed while a
    // node is in persistent focus so it doesn't fight the focus highlight.
    cy.on("mouseover", "node", (evt) => {
      const n = evt.target;
      if (n.data("isFileBox")) return; // file containers aren't interactive
      box.style.cursor = "pointer";
      setHover({ short: n.data("short"), file: n.data("file"), line: n.data("line"), kind: n.data("kind") });
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
    // Click a node → focus it (persistent degree-of-interest) AND open its source
    // peek. Esc or a background click clears the focus.
    cy.on("tap", "node", (evt) => {
      const n = evt.target;
      const d = n.data();
      if (d.isFileBox) return; // file containers aren't clickable
      focusedRef.current = d.id;
      focusNode(n);
      onNodeClickRef.current?.({
        label: d.flabel,
        short: d.short,
        file: d.file ?? null,
        line: d.line ?? null,
        kind: d.kind,
        external: d.external === 1,
      });
    });

    // Edge hover: spotlight the call relationship + show its call-site count.
    cy.on("mouseover", "edge", (evt) => {
      const e = evt.target;
      box.style.cursor = e.data("hasAnchor") ? "pointer" : "default";
      setEdgeHover({ count: (e.data("anchors") || []).length });
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
    // Click an edge → open its exact LSP/SCIP call site(s) in source.
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

    return () => {
      document.removeEventListener("keydown", onKey);
      cy.destroy();
      cyRef.current = null;
    };
  }, [data]); // build once per graph; callbacks come through refs (see above)

  // Lay out the single top-down dependency view. (The redundant Flow/Clusters
  // modes were removed — one view, one meaning.) Re-runs when the graph changes;
  // clearFileBoxes keeps it idempotent across dev/HMR double-invokes.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    clearFileBoxes(cy);
    runFilesLayout(cy);
  }, [data]);

  return (
    <div className="codegraph" data-variant={variant}>
      <div className="codegraph-canvas" ref={boxRef} aria-label="dependency graph" />
      <div className="codegraph-bar mono">
        {edgeHover ? (
          <span className="cg-info">
            <b>
              {edgeHover.count} call site{edgeHover.count === 1 ? "" : "s"}
            </b>
            <span className="muted"> — click the edge to open the exact reference line</span>
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
              <span className="lk-grow" />
              bigger = more referenced
            </span>
            <span>
              <span className="lk-swatch" />
              colour = kind
            </span>
            <span>
              <span className="lk-ext" />
              external
            </span>
          </span>
        )}
        <span
          className="codegraph-depnote"
          title="Files ordered top→bottom by dependency: callers/entry points on top, their dependencies below"
        >
          ↓ top-down dependencies
        </span>
        <button type="button" className="codegraph-fit" onClick={() => cyRef.current?.fit(undefined, 24)}>
          Fit
        </button>
      </div>
    </div>
  );
}
