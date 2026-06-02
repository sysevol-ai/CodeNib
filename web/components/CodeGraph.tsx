"use client";

import { useEffect, useRef, useState } from "react";
import cytoscape, { type Core, type ElementDefinition } from "cytoscape";
import dagre from "cytoscape-dagre";
import fcose from "cytoscape-fcose";
import type { CallSite, CodemapResponse } from "@/lib/api";

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

// Register the layout extensions once (module scope; guard for HMR / double-import).
let layoutsRegistered = false;
function ensureLayouts() {
  if (!layoutsRegistered) {
    cytoscape.use(dagre);
    cytoscape.use(fcose);
    layoutsRegistered = true;
  }
}

// Stable, pleasant per-file accent palette. Files map to a border color so the
// graph reads as "grouped by file" without heavy compound boxes.
const FILE_COLORS = [
  "#2d7ff9", "#e8590c", "#0ca678", "#ae3ec9", "#f08c00",
  "#1098ad", "#e64980", "#5c7cfa", "#37b24d", "#f76707",
];
function colorFor(file: string | null, cache: Map<string, string>): string {
  const key = file || "·";
  let c = cache.get(key);
  if (!c) {
    let h = 0;
    for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) | 0;
    c = FILE_COLORS[Math.abs(h) % FILE_COLORS.length];
    cache.set(key, c);
  }
  return c;
}

interface HoverInfo {
  short: string;
  file: string | null;
  line: number | null;
  kind: string;
}

/**
 * Interactive dependency graph rendered with Cytoscape + dagre.
 * Replaces the old Mermaid auto-layout: directional (left→right) call flow,
 * per-file colour accents, zoom/pan, click-a-node-to-refocus, hover for file:line.
 */
export default function CodeGraph({
  data,
  onNodeClick,
  onEdgeClick,
}: {
  data: CodemapResponse;
  onNodeClick?: (node: GraphNodeInfo) => void;
  onEdgeClick?: (info: EdgeClickInfo) => void;
}) {
  const boxRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  const [hover, setHover] = useState<HoverInfo | null>(null);
  const [edgeHover, setEdgeHover] = useState<{ count: number } | null>(null);
  // "flow" = directional dagre call-flow; "clusters" = fcose force-directed,
  // which spatially groups the detected communities (best for dense/large maps).
  const [layoutMode, setLayoutMode] = useState<"flow" | "clusters">("flow");

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
    ensureLayouts();
    const box = boxRef.current;
    if (!box) return;

    const dark = document.documentElement.dataset.theme === "dark";
    const colorCache = new Map<string, string>();

    const shortById = new Map(data.nodes.map((n) => [n.id, n.short]));
    // Colour by detected community when the graph splits into clusters; otherwise
    // fall back to per-file colour so a single-cluster graph isn't monochrome.
    const multiComm = new Set(data.nodes.map((n) => n.community ?? 0)).size > 1;
    const elements: ElementDefinition[] = [
      ...data.nodes.map((n) => {
        const hidden = n.hidden_callees || 0;
        const colorKey = multiComm ? `community-${n.community ?? 0}` : n.file;
        return {
          data: {
            id: n.id,
            short: n.short, // clean name (used by the source peek)
            glabel: hidden ? `${n.short}  +${hidden}` : n.short, // node label, with hub badge
            flabel: n.label, // full unified_name, used to refocus
            file: n.file,
            line: n.line,
            kind: n.kind,
            accent: colorFor(colorKey, colorCache),
            importance: n.importance ?? 0,
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
              hasAnchor: anchors.length ? 1 : 0,
              srcShort: shortById.get(e.source) || "",
              tgtShort: shortById.get(e.target) || "",
            },
          };
        }),
    ];

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
          selector: "node.hl",
          style: { "border-width": 4 },
        },
        {
          selector: "node.faded",
          style: { opacity: 0.25 },
        },
        {
          selector: "edge",
          style: {
            width: 1.5,
            "line-color": edgeColor,
            "target-arrow-color": edgeColor,
            "target-arrow-shape": "triangle",
            "arrow-scale": 0.9,
            "curve-style": "bezier",
            opacity: 0.8,
          },
        },
        {
          selector: "edge.hl",
          style: { "line-color": "#2d7ff9", "target-arrow-color": "#2d7ff9", width: 2.5, opacity: 1 },
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

    // Hover: highlight the node + its incident edges/neighbours, show file:line.
    cy.on("mouseover", "node", (evt) => {
      const n = evt.target;
      box.style.cursor = "pointer";
      const nbr = n.closedNeighborhood();
      cy.elements().not(nbr).addClass("faded");
      nbr.removeClass("faded");
      n.addClass("hl");
      n.connectedEdges().addClass("hl");
      setHover({ short: n.data("short"), file: n.data("file"), line: n.data("line"), kind: n.data("kind") });
    });
    cy.on("mouseout", "node", () => {
      box.style.cursor = "";
      cy.elements().removeClass("faded hl");
      setHover(null);
    });
    // Click a node → inspect its definition source (parent opens the peek,
    // which also offers "Focus here" to re-root the map).
    cy.on("tap", "node", (evt) => {
      const d = evt.target.data();
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
      const nbr = e.connectedNodes().union(e);
      cy.elements().not(nbr).addClass("faded");
      nbr.removeClass("faded");
      e.addClass("hl");
      setEdgeHover({ count: (e.data("anchors") || []).length });
    });
    cy.on("mouseout", "edge", () => {
      box.style.cursor = "";
      cy.elements().removeClass("faded hl");
      setEdgeHover(null);
    });
    // Click an edge → open its exact LSP/SCIP call site(s) in source.
    cy.on("tap", "edge", (evt) => {
      const d = evt.target.data();
      if (d.anchors?.length) {
        onEdgeClickRef.current?.({ anchors: d.anchors, srcLabel: d.srcShort, tgtLabel: d.tgtShort });
      }
    });

    return () => {
      cy.destroy();
      cyRef.current = null;
    };
  }, [data]); // build once per graph; callbacks come through refs (see above)

  // Run/re-run the layout when the graph or the view mode changes. Separate from
  // the build effect so a Flow/Clusters toggle re-lays-out the SAME instance.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    const cfg =
      layoutMode === "clusters"
        ? {
            name: "fcose",
            quality: "default",
            animate: false,
            randomize: true,
            nodeSeparation: 80,
            idealEdgeLength: 95,
            nodeRepulsion: 6500,
            packComponents: true,
            fit: true,
            padding: 26,
          }
        : {
            name: "dagre",
            rankDir: "LR",
            nodeSep: 24,
            rankSep: 58,
            edgeSep: 10,
            fit: true,
            padding: 26,
          };
    cy.layout({
      ...cfg,
      stop: () => {
        // Keep nodes legible: clamp the fitted zoom to a readable band.
        const z = cy.zoom();
        if (z < 0.55) cy.zoom(0.55);
        else if (z > 1.3) cy.zoom(1.3);
        cy.center();
      },
    } as any).run();
  }, [data, layoutMode]);

  return (
    <div className="codegraph">
      <div className="codegraph-canvas" ref={boxRef} aria-label="dependency graph" />
      <div className="codegraph-bar mono">
        {edgeHover ? (
          <span>
            <b>
              {edgeHover.count} call site{edgeHover.count === 1 ? "" : "s"}
            </b>
            <span className="muted"> — click the edge to open the exact reference line</span>
          </span>
        ) : hover ? (
          <span>
            <b>{hover.short}</b>
            {hover.file ? ` — ${hover.file}${hover.line ? `:${hover.line}` : ""}` : ""}
            <span className="codegraph-kind">{hover.kind}</span>
          </span>
        ) : (
          <span className="muted">
            <b className="legend-key">size</b> = importance · <b className="legend-key">colour</b> = cluster ·
            click a node → definition, an edge → call site · scroll to zoom
          </span>
        )}
        <div className="codegraph-modes" role="tablist" aria-label="Graph layout">
          <button
            type="button"
            className={layoutMode === "flow" ? "on" : ""}
            onClick={() => setLayoutMode("flow")}
            title="Directional call-flow (dagre)"
          >
            Flow
          </button>
          <button
            type="button"
            className={layoutMode === "clusters" ? "on" : ""}
            onClick={() => setLayoutMode("clusters")}
            title="Group by detected cluster (force-directed)"
          >
            Clusters
          </button>
        </div>
        <button type="button" className="codegraph-fit" onClick={() => cyRef.current?.fit(undefined, 24)}>
          Fit
        </button>
      </div>
    </div>
  );
}
