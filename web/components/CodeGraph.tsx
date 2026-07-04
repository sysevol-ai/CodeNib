"use client";

import { useEffect, useRef, useState } from "react";
import cytoscape, { type Core, type ElementDefinition, type NodeSingular } from "cytoscape";
import { fetchEdgeLabel, type CallSite, type CodemapResponse } from "@/lib/api";

export interface EdgeClickInfo {
  anchors: CallSite[];
  srcLabel: string;
  tgtLabel: string;
  srcFile?: string;
  tgtFile?: string;
  // 1-based definition spans of the two endpoints, so an on-demand edge label
  // can fetch both symbols' code. Absent for aggregate (collapsed) edges.
  srcLine?: number | null;
  srcEnd?: number | null;
  tgtLine?: number | null;
  tgtEnd?: number | null;
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
type HierarchyNode = NonNullable<CodemapResponse["hierarchy"]>["nodes"][number];

// Keep graph color semantic and restrained: most symbols are neutral, roots are
// the primary focus color, and type/model-like symbols get one muted secondary.
function toneForKind(kind: string): "model" | "default" {
  return ["class", "interface", "trait", "struct", "enum", "type"].includes(kind)
    ? "model"
    : "default";
}

// Files-overview model: the map opens with one "pill" per file (a readable
// overview). Symbol detail is shown outside the layout so selecting a file does
// not mutate the main tree.
const META = "filemeta::"; // collapsed file pill
const BOX = "filebox::"; //   expanded file compound box
const DIR = "dir::"; // directory/package compound box
const SCOPE = "scopebox::"; // symbol/class compound scope inside an expanded file
const fileKey = (n: CMNode) => (n.external ? "· external" : n.file || "· external");
const baseName = (f: string) => (f.startsWith("·") ? f : f.split("/").pop() || f);
const compactFileName = (f: string) => {
  if (f.startsWith("·")) return f;
  const parts = f.split("/");
  return parts.length > 1 ? parts.slice(-2).join("/") : parts[0] || f;
};
const graphFileName = (f: string) => {
  const name = baseName(f);
  if (name.length <= 14) return name;
  const cut = name.lastIndexOf("_");
  if (cut >= 5 && cut <= name.length - 5) return `${name.slice(0, cut + 1)}\n${name.slice(cut + 1)}`;
  return name;
};
const pathSuffix = (f: string, partCount: number) => fileParts(f).slice(-partCount).join("/");
const wrapFileLabel = (label: string) => {
  if (label.startsWith("·")) return label;
  const parts = label.split("/");
  if (parts.length > 1) return `${parts.slice(0, -1).join("/")}/\n${parts[parts.length - 1]}`;
  return graphFileName(label);
};
const fileDisplayLabels = (files: string[], compactLabels: boolean): Map<string, string> => {
  const labels = new Map<string, string>();
  const byBase = new Map<string, string[]>();

  for (const file of files) {
    const base = baseName(file);
    let group = byBase.get(base);
    if (!group) byBase.set(base, (group = []));
    group.push(file);
  }

  for (const [base, group] of byBase) {
    if (group.length === 1) {
      const file = group[0];
      labels.set(file, compactLabels ? wrapFileLabel(compactFileName(file)) : graphFileName(file));
      continue;
    }

    for (const file of group) {
      const parts = fileParts(file);
      let label = base;
      for (let count = Math.min(2, parts.length); count <= parts.length; count += 1) {
        const candidate = pathSuffix(file, count);
        const unique = group.every((other) => other === file || pathSuffix(other, count) !== candidate);
        if (unique) {
          label = candidate;
          break;
        }
      }
      labels.set(file, wrapFileLabel(label));
    }
  }

  return labels;
};
const plainGraphLabel = (label: string) => label.replace(/\s*·\d+$/, "").replace(/\n/g, "");
const symbolName = (s: string) => s.split(":").pop() || s;
const scopedSymbolName = (s: string, scope?: string) => {
  const name = symbolName(s);
  return scope && name.startsWith(`${scope}.`) ? name.slice(scope.length + 1) : name;
};
const fileParts = (f: string) => (f.startsWith("·") ? [f] : f.split("/").filter(Boolean));
const commonSourceRoot = (files: string[]) => {
  const concrete = files.map(fileParts).filter((p) => p.length > 1 && !p[0].startsWith("·"));
  if (!concrete.length) return null;
  const first = concrete[0][0];
  return concrete.every((p) => p[0] === first) ? first : null;
};
const hierarchyPath = (f: string, root: string | null) => {
  const parts = fileParts(f);
  return root && parts[0] === root ? parts.slice(1) : parts;
};
const dirKeyForFile = (f: string, root: string | null) => {
  if (f.startsWith("·")) return "· external";
  const parts = hierarchyPath(f, root);
  return parts.length > 1 ? parts.slice(0, -1).join("/") : "· root";
};
const dirLabel = (d: string) => (d === "· root" ? "root" : d === "· external" ? "external" : `${d}/`);
const stableHash = (s: string) => {
  let h = 2166136261;
  for (let i = 0; i < s.length; i += 1) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
};
const stableOffset = (id: string, radius = 40) => {
  const h = stableHash(id);
  const angle = ((h % 360) / 360) * Math.PI * 2;
  const r = radius * (0.35 + ((h >>> 9) % 1000) / 2000);
  return { x: Math.cos(angle) * r, y: Math.sin(angle) * r };
};

type Point = { x: number; y: number };

type DirInfo = { label: string; count: number; external: boolean; parent?: string };
type HierarchyProjection = { dirs: Map<string, DirInfo>; fileDir: Map<string, string> };

function fallbackHierarchyProjection(
  files: string[],
  compactLabels: boolean,
  hierarchyMode: "files" | "symbols"
): HierarchyProjection {
  const root = commonSourceRoot(files);
  const byDir = new Map<string, string[]>();
  for (const f of files) {
    const d = dirKeyForFile(f, root);
    let arr = byDir.get(d);
    if (!arr) byDir.set(d, (arr = []));
    arr.push(f);
  }

  const dirs = new Map<string, DirInfo>();
  const fileDir = new Map<string, string>();
  for (const [d, dirFiles] of byDir) {
    if ((hierarchyMode === "files" && !compactLabels) || dirFiles.length > 1) {
      dirs.set(d, {
        label: dirLabel(d),
        count: dirFiles.length,
        external: d === "· external",
      });
    }
  }
  for (const [d, dirFiles] of byDir) {
    if (!dirs.has(d)) continue;
    for (const f of dirFiles) fileDir.set(f, d);
  }
  return { dirs, fileDir };
}

function backendHierarchyProjection(
  data: CodemapResponse,
  files: string[],
  compactLabels: boolean,
  hierarchyMode: "files" | "symbols"
): HierarchyProjection | null {
  const hnodes = data.hierarchy?.nodes;
  if (!hnodes?.length) return null;

  const fileSet = new Set(files);
  const byId = new Map(hnodes.map((n) => [n.id, n]));
  const fileNodes = hnodes.filter((n) => n.kind === "file" && n.file && fileSet.has(n.file));
  if (!fileNodes.length) return null;

  const fileCountByDir = new Map<string, number>();
  for (const fnode of fileNodes) {
    const parent = fnode.parent ? byId.get(fnode.parent) : null;
    if (parent?.kind !== "directory") continue;
    fileCountByDir.set(parent.id, (fileCountByDir.get(parent.id) ?? 0) + 1);
  }

  const dirs = new Map<string, DirInfo>();
  for (const [id, count] of fileCountByDir) {
    const node = byId.get(id);
    if (!node) continue;
    if ((hierarchyMode === "files" && !compactLabels) || count > 1) {
      const parent = node.parent && byId.get(node.parent)?.kind === "directory" ? node.parent : undefined;
      dirs.set(id, {
        label: node.label,
        count,
        external: !!node.external,
        parent,
      });
    }
  }

  const nearestVisibleDir = (id: string | null | undefined): string | undefined => {
    let cur = id;
    while (cur) {
      if (dirs.has(cur)) return cur;
      cur = byId.get(cur)?.parent ?? undefined;
    }
    return undefined;
  };
  const fileDir = new Map<string, string>();
  for (const fnode of fileNodes) {
    const visible = nearestVisibleDir(fnode.parent);
    if (visible && fnode.file) fileDir.set(fnode.file, visible);
  }

  return { dirs, fileDir };
}

// Build Cytoscape elements for the current expand state. A collapsed file is a
// single pill meta-node; an expanded file is a compound box holding its symbols.
// Symbol→symbol edges are aggregated onto whichever endpoints are visible (a
// symbol when its file is expanded, else the file pill); edges inside one
// collapsed file collapse away. Solid edges are exact references; dashed edges
// (`meta = 1`) are file-level aggregates that you drill into by expanding.
function buildElements(
  data: CodemapResponse,
  expanded: Set<string>,
  compactLabels = false,
  hierarchyMode: "files" | "symbols" = "files"
): ElementDefinition[] {
  const byFile = new Map<string, CMNode[]>();
  const fileOfNode = new Map<string, string>();
  for (const n of data.nodes) {
    const f = fileKey(n);
    fileOfNode.set(n.id, f);
    let arr = byFile.get(f);
    if (!arr) byFile.set(f, (arr = []));
    arr.push(n);
  }

  const files = [...byFile.keys()];
  const displayLabels = fileDisplayLabels(files, compactLabels);
  const els: ElementDefinition[] = [];
  const hierarchy =
    backendHierarchyProjection(data, files, compactLabels, hierarchyMode) ??
    fallbackHierarchyProjection(files, compactLabels, hierarchyMode);
  const hierarchyNodes = data.hierarchy?.nodes ?? [];
  const hierarchyById = new Map(hierarchyNodes.map((n) => [n.id, n]));
  const hierarchySymbolByNodeId = new Map<string, HierarchyNode>();
  for (const hnode of hierarchyNodes) {
    if (hnode.kind === "symbol" && hnode.node_id) hierarchySymbolByNodeId.set(hnode.node_id, hnode);
  }
  const scopeBoxIds = new Set<string>();
  for (const n of data.nodes) {
    let cur = hierarchySymbolByNodeId.get(n.id)?.parent ?? null;
    while (cur) {
      const hnode = hierarchyById.get(cur);
      if (!hnode) break;
      if (hnode.kind === "symbol") scopeBoxIds.add(hnode.id);
      cur = hnode.parent ?? null;
    }
  }

  const scopeParentFor = (parentId: string | null | undefined, file: string): string => {
    let cur = parentId ?? null;
    while (cur) {
      const hnode = hierarchyById.get(cur);
      if (!hnode) break;
      if (hnode.kind === "symbol" && scopeBoxIds.has(hnode.id)) return SCOPE + hnode.id;
      if (hnode.kind === "file") return BOX + (hnode.file || file);
      cur = hnode.parent ?? null;
    }
    return BOX + file;
  };

  for (const [d, info] of hierarchy.dirs) {
    const parent = info.parent && hierarchy.dirs.has(info.parent) ? DIR + info.parent : undefined;
    els.push({
      data: {
        id: DIR + d,
        ...(parent ? { parent } : {}),
        isDirBox: 1,
        dir: d,
        dirLabel: info.label,
        count: info.count,
        external: info.external ? 1 : 0,
      },
    });
  }

  for (const [f, nodes] of byFile) {
    const dirKey = hierarchy.fileDir.get(f);
    const dirParent = dirKey ? DIR + dirKey : undefined;
    const fileDepth = nodes.some((n) => n.is_root)
      ? 0
      : Math.min(...nodes.map((n) => (Number.isFinite(n.depth) ? n.depth : 0)));
    if (expanded.has(f)) {
      els.push({
        data: {
          id: BOX + f,
          ...(dirParent ? { parent: dirParent } : {}),
          isFileBox: 1,
          file: f,
          short: baseName(f),
          count: nodes.length,
          fileLabel: displayLabels.get(f) ?? baseName(f),
          treeDepth: fileDepth,
        },
      });
      const scopeNodes = hierarchyNodes
        .filter((hnode) => hnode.kind === "symbol" && hnode.file === f && scopeBoxIds.has(hnode.id))
        .sort((a, b) => (a.depth ?? 0) - (b.depth ?? 0) || a.id.localeCompare(b.id));
      for (const hnode of scopeNodes) {
        const short = symbolName(hnode.label || hnode.path || hnode.id);
        els.push({
          data: {
            id: SCOPE + hnode.id,
            parent: scopeParentFor(hnode.parent, f),
            isSymbolBox: 1,
            hierarchyId: hnode.id,
            file: f,
            short,
            scopeLabel: short,
            count: hnode.symbol_count ?? 0,
            importance: hnode.importance ?? hnode.doi ?? 0,
            external: hnode.external ? 1 : 0,
            treeDepth: hnode.depth ?? fileDepth,
          },
        });
      }
      for (const n of nodes) {
        const hidden = n.hidden_callees || 0;
        const hnode = hierarchySymbolByNodeId.get(n.id);
        const parentHnode = hnode?.parent ? hierarchyById.get(hnode.parent) : undefined;
        const parentScope =
          parentHnode?.kind === "symbol" ? symbolName(parentHnode.label || parentHnode.path || parentHnode.id) : undefined;
        const short = scopedSymbolName(n.short, parentScope);
        const parent =
          hnode && scopeBoxIds.has(hnode.id)
            ? SCOPE + hnode.id
            : hnode
              ? scopeParentFor(hnode.parent, f)
              : BOX + f;
        els.push({
          data: {
            id: n.id,
            parent,
            short,
            glabel: hidden ? `${short}  +${hidden}` : short, // node label, with hub badge
            flabel: n.label, // full unified_name, used to refocus
            file: n.file,
            line: n.line,
            endLine: n.end_line ?? null,
            kind: n.kind,
            tone: toneForKind(n.kind),
            isConcreteSymbol: 1,
            importance: n.importance ?? 0,
            refCount: n.ref_count ?? 0,
            entryScore: n.entry_score ?? 0,
            root: n.is_root ? 1 : 0,
            external: n.external ? 1 : 0,
            treeDepth: n.depth ?? fileDepth,
          },
        });
      }
    } else {
      let imp = 0;
      for (const n of nodes) imp = Math.max(imp, n.importance ?? 0);
      els.push({
        data: {
          id: META + f,
          ...(dirParent ? { parent: dirParent } : {}),
          isFileMeta: 1,
          file: f,
          short: baseName(f),
          glabel: `${displayLabels.get(f) ?? baseName(f)}  ·${nodes.length}`,
          kind: "file",
          tone: "file",
          importance: imp,
          count: nodes.length,
          pill: Math.min(1, nodes.length / 20), // clamped size cue: bigger file = bigger pill
          root: nodes.some((n) => n.is_root) ? 1 : 0,
          external: f === "· external" ? 1 : 0,
          treeDepth: fileDepth,
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
  const fileLabel = (file: string | undefined) => (file ? plainGraphLabel(displayLabels.get(file) ?? compactFileName(file)) : "");

  type Agg = {
    weight: number;
    anchors: CallSite[];
    symbolEdge: boolean;
    srcShort: string;
    tgtShort: string;
    srcFile: string;
    tgtFile: string;
    srcDisplay: string;
    tgtDisplay: string;
    crossFile: boolean;
  };
  const agg = new Map<string, Agg>();
  for (const e of data.edges) {
    if (!e.source || !e.target) continue;
    const vs = visId(e.source);
    const vt = visId(e.target);
    if (vs === vt) continue; // self-loop, or both inside one collapsed file
    const symbolEdge = !vs.startsWith(META) && !vt.startsWith(META);
    const key = `${vs}\u0000${vt}`;
    let a = agg.get(key);
    if (!a) {
      agg.set(
        key,
        (a = {
          weight: 0,
          anchors: [],
          symbolEdge,
          srcShort: "",
          tgtShort: "",
          srcFile: fileOfNode.get(e.source) || "",
          tgtFile: fileOfNode.get(e.target) || "",
          srcDisplay: "",
          tgtDisplay: "",
          crossFile: false,
        })
      );
    }
    a.weight += e.weight ?? e.anchors?.length ?? 0;
    // Collect anchors for ALL edges (incl. file-level aggregates) so the on-hover
    // label has call-site evidence. The click-peek still only uses them for
    // symbol edges (see the `anchors` field below).
    if (e.anchors) a.anchors.push(...e.anchors);
    a.crossFile = a.crossFile || !!e.cross_file;
    if (symbolEdge) {
      a.srcShort = shortById.get(e.source) || "";
      a.tgtShort = shortById.get(e.target) || "";
    }
    a.srcFile ||= fileOfNode.get(e.source) || "";
    a.tgtFile ||= fileOfNode.get(e.target) || "";
    a.srcDisplay ||= symbolEdge ? a.srcShort : fileLabel(a.srcFile);
    a.tgtDisplay ||= symbolEdge ? a.tgtShort : fileLabel(a.tgtFile);
  }
  let i = 0;
  for (const [key, a] of agg) {
    const [source, target] = key.split("\u0000");
    els.push({
      data: {
        id: `e${i++}`,
        source,
        target,
        anchors: a.symbolEdge ? a.anchors : [], // click-peek: exact edges only
        labelAnchors: a.anchors, // on-hover LLM label: all edges, incl. file-level
        weight: a.weight || 1, // # call sites -> drives edge width
        hasAnchor: a.symbolEdge && a.anchors.length ? 1 : 0,
        meta: a.symbolEdge ? 0 : 1, // file-level aggregate (dashed) vs exact reference
        srcShort: a.srcShort,
        tgtShort: a.tgtShort,
        srcFile: a.srcFile,
        tgtFile: a.tgtFile,
        srcDisplay: a.srcDisplay || a.srcShort || fileLabel(a.srcFile),
        tgtDisplay: a.tgtDisplay || a.tgtShort || fileLabel(a.tgtFile),
        crossFile: a.crossFile ? 1 : 0,
        scopeRoute: a.symbolEdge && !a.crossFile ? 1 : 0,
      },
    });
  }
  return els;
}

// Legacy compact placement helpers kept for file/symbol internals. The main
// graph layout below is a breadth-first tree, not a force-directed embedder.
function seedInitialPositions(cy: Core, compact: boolean, structural: boolean): void {
  const leaves = cy
    .nodes()
    .filter((n) => !n.isParent())
    .sort((a, b) => {
      const ar = a.data("root") ? 0 : 1;
      const br = b.data("root") ? 0 : 1;
      if (ar !== br) return ar - br;
      return a.id().localeCompare(b.id());
    });
  const count = leaves.length;
  if (count === 0) return;

  const grouped = new Map<string, NodeSingular[]>();
  leaves.forEach((n) => {
    const parent = n.parent();
    const group = parent.nonempty() ? parent.first().id() : "ungrouped";
    let arr = grouped.get(group);
    if (!arr) grouped.set(group, (arr = []));
    arr.push(n);
  });
  const groups = [...grouped.entries()].sort((a, b) => {
    const ar = a[1].some((n) => n.data("root")) ? 0 : 1;
    const br = b[1].some((n) => n.data("root")) ? 0 : 1;
    if (ar !== br) return ar - br;
    if (b[1].length !== a[1].length) return b[1].length - a[1].length;
    return a[0].localeCompare(b[0]);
  });
  const groupCols = compact ? Math.min(2, Math.ceil(Math.sqrt(groups.length))) : Math.ceil(Math.sqrt(groups.length));
  const groupGapX = compact ? 138 : structural ? 190 : 220;
  const groupGapY = compact ? 110 : structural ? 140 : 160;
  const leafGapX = compact ? 72 : structural ? 86 : 104;
  const leafGapY = compact ? 52 : structural ? 64 : 76;
  groups.forEach(([group, nodes], gi) => {
    const groupCol = gi % groupCols;
    const groupRow = Math.floor(gi / groupCols);
    const groupX = (groupCol - (groupCols - 1) / 2) * groupGapX;
    const groupY = (groupRow - (Math.ceil(groups.length / groupCols) - 1) / 2) * groupGapY;
    const localCols = Math.min(compact ? 2 : 3, Math.ceil(Math.sqrt(nodes.length)));
    const localRows = Math.ceil(nodes.length / localCols);
    nodes
      .sort((a, b) => a.id().localeCompare(b.id()))
      .forEach((n, i) => {
        const col = i % localCols;
        const row = Math.floor(i / localCols);
        const jitter = stableOffset(`${group}/${n.id()}`, compact ? 7 : 10);
        n.position({
          x: groupX + (col - (localCols - 1) / 2) * leafGapX + jitter.x,
          y: groupY + (row - (localRows - 1) / 2) * leafGapY + jitter.y,
        });
      });
  });
}

function runCompactFileOverview(cy: Core): boolean {
  const container = cy.container();
  const compact = (container?.clientWidth ?? 0) < 500;
  const graphMode = container?.closest(".codegraph")?.getAttribute("data-mode");
  if (!compact || graphMode !== "files" || cy.nodes("[isFileBox = 1]").length > 0) return false;

  const leaves = cy.nodes("[isFileMeta = 1]");
  if (leaves.length === 0) return false;

  const grouped = new Map<string, NodeSingular[]>();
  leaves.forEach((n) => {
    const parent = n.parent();
    const group = parent.nonempty() ? parent.first().id() : n.id();
    let arr = grouped.get(group);
    if (!arr) grouped.set(group, (arr = []));
    arr.push(n);
  });
  const groups = [...grouped.entries()].sort((a, b) => {
    const ar = a[1].some((n) => n.data("root")) ? 0 : a[1].some((n) => n.data("external")) ? 2 : 1;
    const br = b[1].some((n) => n.data("root")) ? 0 : b[1].some((n) => n.data("external")) ? 2 : 1;
    if (ar !== br) return ar - br;
    if (b[1].length !== a[1].length) return b[1].length - a[1].length;
    return a[0].localeCompare(b[0]);
  });

  const cols = 2;
  const cellX = 118;
  const cellY = 52;
  const rows: NodeSingular[][] = [];
  let rowBuf: NodeSingular[] = [];
  const flushRow = () => {
    if (rowBuf.length) rows.push(rowBuf);
    rowBuf = [];
  };

  groups.forEach(([, nodes]) => {
    const sorted = nodes.sort((a, b) => {
      const ar = a.data("root") ? 0 : 1;
      const br = b.data("root") ? 0 : 1;
      if (ar !== br) return ar - br;
      return a.id().localeCompare(b.id());
    });
    if (sorted.length > 1) {
      flushRow();
      for (let i = 0; i < sorted.length; i += cols) rows.push(sorted.slice(i, i + cols));
      return;
    }
    rowBuf.push(sorted[0]);
    if (rowBuf.length === cols) flushRow();
  });
  flushRow();

  rows.forEach((rowNodes, row) => {
    const inRow = rowNodes.length;
    rowNodes.forEach((n, col) => {
      n.position({
        x: (col - (inRow - 1) / 2) * cellX,
        y: (row - (rows.length - 1) / 2) * cellY,
      });
    });
  });

  cy.layout({ name: "preset", animate: false, fit: true, padding: 10 } as any).run();
  if (cy.zoom() > 1.05) {
    cy.zoom(1.05);
    cy.center();
  }
  ensureGraphVisible(cy, 10);
  return true;
}

function runCompactSymbolSpine(cy: Core): boolean {
  const container = cy.container();
  const compact = (container?.clientWidth ?? 0) < 500;
  const graphMode = container?.closest(".codegraph")?.getAttribute("data-mode");
  if (!compact || graphMode !== "symbols") return false;

  const groups: Array<{ id: string; nodes: NodeSingular[]; rank: number }> = [];
  cy.nodes("[isFileBox = 1]").forEach((box) => {
    const symbols = box
      .children()
      .filter((n) => !n.isParent())
      .sort((a, b) => {
        const ar = a.data("root") ? 0 : 1;
        const br = b.data("root") ? 0 : 1;
        if (ar !== br) return ar - br;
        return a.id().localeCompare(b.id());
      });
    const symbolNodes: NodeSingular[] = [];
    symbols.forEach((n) => {
      symbolNodes.push(n);
    });
    if (symbolNodes.length) groups.push({ id: box.id(), nodes: symbolNodes, rank: 0 });
  });

  const metaGroups = new Map<string, NodeSingular[]>();
  cy.nodes("[isFileMeta = 1]").forEach((n) => {
    const parent = n.parent();
    const group = parent.nonempty() && parent.first().data("isDirBox") ? parent.first().id() : n.id();
    let arr = metaGroups.get(group);
    if (!arr) metaGroups.set(group, (arr = []));
    arr.push(n);
  });
  metaGroups.forEach((nodes, id) => {
    const rank = nodes.some((n) => n.data("root")) ? 1 : nodes.some((n) => n.data("external")) ? 3 : 2;
    groups.push({ id, nodes, rank });
  });

  if (groups.length === 0) return false;
  groups.sort((a, b) => {
    if (a.rank !== b.rank) return a.rank - b.rank;
    if (b.nodes.length !== a.nodes.length) return b.nodes.length - a.nodes.length;
    return a.id.localeCompare(b.id);
  });

  const cols = 2;
  const cellX = 118;
  const cellY = 52;
  const rows: NodeSingular[][] = [];
  let rowBuf: NodeSingular[] = [];
  const flushRow = () => {
    if (rowBuf.length) rows.push(rowBuf);
    rowBuf = [];
  };

  groups.forEach((group) => {
    const sorted = group.nodes.sort((a, b) => {
      const ar = a.data("root") ? 0 : 1;
      const br = b.data("root") ? 0 : 1;
      if (ar !== br) return ar - br;
      return a.id().localeCompare(b.id());
    });
    if (group.rank === 0 || sorted.length > 1) {
      flushRow();
      for (let i = 0; i < sorted.length; i += cols) rows.push(sorted.slice(i, i + cols));
      return;
    }
    rowBuf.push(sorted[0]);
    if (rowBuf.length === cols) flushRow();
  });
  flushRow();

  rows.forEach((rowNodes, row) => {
    const inRow = rowNodes.length;
    rowNodes.forEach((n, col) => {
      n.position({
        x: (col - (inRow - 1) / 2) * cellX,
        y: (row - (rows.length - 1) / 2) * cellY,
      });
    });
  });

  cy.layout({ name: "preset", animate: false, fit: true, padding: 10 } as any).run();
  if (cy.zoom() > 1.05) {
    cy.zoom(1.05);
    cy.center();
  }
  ensureGraphVisible(cy, 10);
  return true;
}

function runLayout(cy: Core, opts: { stable?: boolean } = {}): void {
  if (cy.nodes().length === 0) return;
  const compact = (cy.container()?.clientWidth ?? 0) < 500;
  const graphMode = cy.container()?.closest(".codegraph")?.getAttribute("data-mode");
  const structural = graphMode === "symbols" || cy.nodes("[isFileBox = 1]").length > 0;
  const containerW = cy.container()?.clientWidth ?? 640;
  const targetCellW = compact ? 108 : structural ? 136 : 120;
  const maxCols = Math.max(1, Math.min(compact ? 2 : structural ? 4 : 4, Math.floor((containerW - 72) / targetCellW)));
  const colGap = compact ? 14 : structural ? 18 : 16;
  const rowGap = compact ? 12 : structural ? 16 : 14;
  const depthGap = compact ? 10 : 14;

  if (structural) {
    cy.nodes("[isFileBox = 1]").forEach((box) => {
      const file = box.data("file");
      if (typeof file === "string") layoutExpandedFileInternals(cy, file, compact);
    });
  }

  const rows = new Map<number, NodeSingular[]>();
  topLevelLayoutItems(cy).forEach((node) => {
    const rawDepth = Number(node.data("treeDepth"));
    const depth = Number.isFinite(rawDepth) ? Math.max(0, Math.round(rawDepth)) : 0;
    const row = rows.get(depth) ?? [];
    row.push(node);
    rows.set(depth, row);
  });

  const visualRows: Array<{ depth: number; nodes: NodeSingular[] }> = [];
  [...rows.entries()]
    .sort((a, b) => a[0] - b[0])
    .forEach(([depth, nodes]) => {
      nodes.sort(compareLayoutItems);
      for (let i = 0; i < nodes.length; i += maxCols) {
        visualRows.push({ depth, nodes: nodes.slice(i, i + maxCols) });
      }
    });
  if (visualRows.length === 0) return;

  const rowBoxes = visualRows.map(({ nodes }) => nodes.map(layoutItemBox));
  const rowHeights = rowBoxes.map((boxes) => Math.max(...boxes.map((box) => box.h), 1));
  const rowY: number[] = [];
  let cursorY = rowHeights[0] / 2;
  for (let i = 0; i < visualRows.length; i += 1) {
    if (i > 0) {
      cursorY += rowHeights[i - 1] / 2 + rowHeights[i] / 2 + rowGap;
      if (visualRows[i].depth !== visualRows[i - 1].depth) cursorY += depthGap;
    }
    rowY[i] = cursorY;
  }
  const totalHeight = cursorY + rowHeights[visualRows.length - 1] / 2;
  const yOffset = totalHeight / 2;

  cy.batch(() => {
    visualRows.forEach(({ nodes }, rowIndex) => {
      const boxes = rowBoxes[rowIndex];
      const width = boxes.reduce((sum, box) => sum + box.w, 0) + Math.max(0, nodes.length - 1) * colGap;
      let x = -width / 2;
      const y = rowY[rowIndex] - yOffset;
      nodes.forEach((node, index) => {
        const box = boxes[index];
        node.position({ x: x + box.w / 2, y });
        x += box.w + colGap;
      });
    });
  });

  cy.layout({
    name: "preset",
    animate: false,
    fit: !opts.stable,
    padding: compact ? 10 : structural ? 16 : 20,
  } as any).run();
  if (!opts.stable) {
    const minCompactZoom = graphMode === "files" ? 0 : 0.54;
    if (compact && minCompactZoom > 0 && cy.zoom() < minCompactZoom) {
      cy.zoom(minCompactZoom);
      cy.center();
    } else if (cy.zoom() > 1.2) {
      cy.zoom(1.2);
      cy.center();
    }
  }
}

function ensureGraphVisible(cy: Core, padding = 24): void {
  const container = cy.container();
  if (!container || cy.elements().empty()) return;
  const width = container.clientWidth;
  const height = container.clientHeight;
  const bb = cy.elements().renderedBoundingBox({ includeLabels: true });
  const usableW = Math.max(80, width - padding * 2);
  const usableH = Math.max(80, height - padding * 2);
  const needsZoom = bb.w > usableW || bb.h > usableH;
  const offscreen = bb.x1 < padding || bb.y1 < padding || bb.x2 > width - padding || bb.y2 > height - padding;
  if (!needsZoom && !offscreen) return;

  const scale = Math.min(1, usableW / Math.max(bb.w, 1), usableH / Math.max(bb.h, 1));
  if (needsZoom && scale < 1) {
    cy.zoom({
      level: Math.max(cy.minZoom(), cy.zoom() * scale),
      renderedPosition: { x: width / 2, y: height / 2 },
    });
  }
  cy.fit(cy.elements(), padding);
}

function collectionNodes(nodes: cytoscape.NodeCollection): NodeSingular[] {
  const out: NodeSingular[] = [];
  nodes.forEach((n) => {
    out.push(n);
  });
  return out;
}

function topLevelLayoutItems(cy: Core): NodeSingular[] {
  const items: NodeSingular[] = [];
  cy.nodes("[isFileBox = 1]").forEach((node) => {
    items.push(node);
  });
  cy.nodes("[isFileMeta = 1]").forEach((node) => {
    items.push(node);
  });
  if (items.length) return items;

  return collectionNodes(cy.nodes().filter((n) => !n.isParent()));
}

function subtreeCenter(node: NodeSingular): Point {
  const box = node.descendants().length
    ? node.descendants().boundingBox({ includeLabels: true })
    : node.boundingBox({ includeLabels: true });
  return { x: (box.x1 + box.x2) / 2, y: (box.y1 + box.y2) / 2 };
}

function shiftSubtree(node: NodeSingular, dx: number, dy: number): void {
  node.descendants().forEach((child) => {
    const p = child.position();
    child.position({ x: p.x + dx, y: p.y + dy });
  });
}

function placeLayoutItem(item: NodeSingular, x: number, y: number): void {
  if (item.isParent()) {
    const c = subtreeCenter(item);
    shiftSubtree(item, x - c.x, y - c.y);
    return;
  }
  item.position({ x, y });
}

function layoutRank(item: NodeSingular): number {
  return (item.data("root") ? 100 : 0) + Number(item.data("importance") || 0) * 10 + (item.data("isSymbolBox") ? 1 : 0);
}

function compareLayoutItems(a: NodeSingular, b: NodeSingular): number {
  const diff = layoutRank(b) - layoutRank(a);
  return Math.abs(diff) > 0.001 ? diff : a.id().localeCompare(b.id());
}

function layoutItemBox(item: NodeSingular): { w: number; h: number } {
  const box = item.isParent()
    ? item.union(item.descendants()).boundingBox({ includeLabels: true })
    : item.boundingBox({ includeLabels: true });
  return { w: Math.max(1, box.w), h: Math.max(1, box.h) };
}

function arrangeLayoutGrid(items: NodeSingular[], cols: number, center: Point, gapX: number, gapY: number): void {
  const rows = Math.ceil(items.length / cols);
  const boxes = items.map(layoutItemBox);
  const colWidths = Array.from({ length: cols }, () => 0);
  const rowHeights = Array.from({ length: rows }, () => 0);
  boxes.forEach((box, i) => {
    const col = i % cols;
    const row = Math.floor(i / cols);
    colWidths[col] = Math.max(colWidths[col], box.w + gapX);
    rowHeights[row] = Math.max(rowHeights[row], box.h + gapY);
  });

  const totalW = colWidths.reduce((sum, w) => sum + w, 0);
  const totalH = rowHeights.reduce((sum, h) => sum + h, 0);
  const colStarts: number[] = [];
  const rowStarts: number[] = [];
  colWidths.reduce((x, w, i) => {
    colStarts[i] = x;
    return x + w;
  }, center.x - totalW / 2);
  rowHeights.reduce((y, h, i) => {
    rowStarts[i] = y;
    return y + h;
  }, center.y - totalH / 2);

  items.forEach((item, i) => {
    const col = i % cols;
    const row = Math.floor(i / cols);
    placeLayoutItem(item, colStarts[col] + colWidths[col] / 2, rowStarts[row] + rowHeights[row] / 2);
  });
}

function layoutSymbolScope(scope: NodeSingular, compact: boolean): void {
  const childScopes = collectionNodes(scope.children("[isSymbolBox = 1]"));
  childScopes.forEach((child) => layoutSymbolScope(child, compact));
  const leaves = collectionNodes(scope.children().filter((n) => !n.isParent()));
  const items = [...childScopes, ...leaves].sort(compareLayoutItems);
  if (!items.length) return;

  const cols = Math.min(2, Math.ceil(Math.sqrt(items.length)));
  const center = subtreeCenter(scope);
  arrangeLayoutGrid(items, cols, center, compact ? 22 : 30, compact ? 14 : 20);
}

function layoutExpandedFileInternals(cy: Core, file: string, compact: boolean): void {
  const fileBox = cy.getElementById(BOX + file);
  if (fileBox.empty()) return;

  const scopes = collectionNodes(fileBox.children("[isSymbolBox = 1]"));
  scopes.forEach((scope) => layoutSymbolScope(scope, compact));
  const leaves = collectionNodes(fileBox.children().filter((n) => !n.isParent()));
  const items = [...scopes, ...leaves].sort(compareLayoutItems);
  if (!items.length) return;

  const cols = Math.min(compact ? 1 : 2, Math.ceil(Math.sqrt(items.length)));
  const center = subtreeCenter(fileBox);
  arrangeLayoutGrid(items, cols, center, compact ? 36 : 54, compact ? 28 : 40);
}

function layoutExpandedFileContext(cy: Core, file: string, compact: boolean): void {
  const fileBox = cy.getElementById(BOX + file);
  if (fileBox.empty()) return;
  const focus = fileBox.union(fileBox.descendants());
  const focusIds = new Set<string>();
  focus.nodes().forEach((n) => {
    focusIds.add(n.id());
  });
  const context = collectionNodes(
    focus
      .connectedEdges()
      .connectedNodes()
      .filter((n) => !n.isParent() && !focusIds.has(n.id()))
  ).sort((a, b) => {
    const ar = a.data("external") ? 1 : 0;
    const br = b.data("external") ? 1 : 0;
    if (ar !== br) return ar - br;
    return a.id().localeCompare(b.id());
  });
  if (!context.length) return;

  const box = fileBox.boundingBox({ includeLabels: true });
  const x = box.x1 - (compact ? 110 : 155);
  const gapY = compact ? 48 : 58;
  const startY = (box.y1 + box.y2) / 2 - ((context.length - 1) * gapY) / 2;
  context.forEach((node, i) => {
    node.position({ x, y: startY + i * gapY });
  });
}

function applySemanticZoom(cy: Core): void {
  const z = cy.zoom();
  const level = z < 0.48 ? "zoom-low" : z < 0.82 ? "zoom-mid" : "zoom-high";
  const classes = "zoom-low zoom-mid zoom-high";
  cy.batch(() => {
    cy.nodes("[isConcreteSymbol = 1]").removeClass(classes).addClass(level);
    cy.edges().removeClass(classes).addClass(level);
  });
}

interface HoverInfo {
  short: string;
  file: string | null;
  line: number | null;
  kind: string;
}

interface SelectedFileInfo {
  file: string;
  short: string;
  symbols: CMNode[];
  inbound: number;
  outbound: number;
}

type GraphMode = "files";
type EdgeHoverInfo = {
  count: number;
  src: string;
  tgt: string;
  aggregate: boolean;
  // LLM dependency phrase: undefined = not fetched, "…" = loading, else the phrase.
  label?: string;
  // Viewport cursor position, for the floating tooltip next to the edge.
  x?: number;
  y?: number;
};

function fileInfo(data: CodemapResponse, file: string | null): SelectedFileInfo | null {
  if (!file) return null;
  const symbols = data.nodes.filter((n) => fileKey(n) === file);
  if (!symbols.length) return null;

  const symbolIds = new Set(symbols.map((n) => n.id));
  let inbound = 0;
  let outbound = 0;
  for (const edge of data.edges) {
    const sourceInFile = symbolIds.has(edge.source);
    const targetInFile = symbolIds.has(edge.target);
    if (sourceInFile && !targetInFile) outbound += edge.weight ?? edge.anchors?.length ?? 1;
    if (!sourceInFile && targetInFile) inbound += edge.weight ?? edge.anchors?.length ?? 1;
  }

  return {
    file,
    short: baseName(file),
    inbound,
    outbound,
    symbols: [...symbols].sort(compareSymbolsForPanel),
  };
}

function compareSymbolsForPanel(a: CMNode, b: CMNode): number {
  const ar = a.is_root ? 0 : 1;
  const br = b.is_root ? 0 : 1;
  if (ar !== br) return ar - br;
  const imp = (b.importance ?? 0) - (a.importance ?? 0);
  if (Math.abs(imp) > 0.001) return imp;
  const refs = (b.ref_count ?? 0) - (a.ref_count ?? 0);
  if (refs !== 0) return refs;
  return (a.line ?? Number.MAX_SAFE_INTEGER) - (b.line ?? Number.MAX_SAFE_INTEGER) || a.short.localeCompare(b.short);
}

function graphFileCount(data: CodemapResponse): number {
  return new Set(data.nodes.map(fileKey)).size;
}

/**
 * Interactive dependency graph (Cytoscape). Opens as a files overview — one pill
 * per file. Selecting a file keeps the tree stable and opens a separate symbol
 * drilldown; click a symbol there to open its source. Click an edge to open the
 * exact SCIP/LSP call site.
 */
export default function CodeGraph({
  data,
  variant = "explore",
  repoId,
  onNodeClick,
  onEdgeClick,
}: {
  data: CodemapResponse;
  // "wiki" = the focused map embedded in a wiki page, tuned for reading.
  // "explore" = the standalone Graph view with richer interaction.
  variant?: "wiki" | "explore";
  repoId?: string; // needed to fetch on-hover LLM edge labels
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
  const [edgeHover, setEdgeHover] = useState<EdgeHoverInfo | null>(null);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const mode: GraphMode = "files";
  const selectedInfo = fileInfo(data, selectedFile);
  const totalFiles = graphFileCount(data);

  // Keep the click callbacks in refs so the cytoscape instance is built once
  // per `data` and is NOT torn down/rebuilt when a parent re-render hands us new
  // handler identities (e.g. opening the source peek) — that would reset zoom/pan.
  const onNodeClickRef = useRef(onNodeClick);
  const onEdgeClickRef = useRef(onEdgeClick);
  // On-hover LLM edge-label plumbing: keep repoId current for the cy closures,
  // cache phrases per edge id (fetch once), debounce so mousing over the graph
  // doesn't spam the LLM, and track the hovered edge so a late result only
  // applies if we're still on that edge.
  const repoIdRef = useRef(repoId);
  const edgeLabelCacheRef = useRef<Map<string, string>>(new Map());
  const hoverTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const hoverEdgeIdRef = useRef<string | null>(null);
  useEffect(() => {
    repoIdRef.current = repoId;
    onNodeClickRef.current = onNodeClick;
    onEdgeClickRef.current = onEdgeClick;
  });

  useEffect(() => {
    const box = boxRef.current;
    if (!box) return;

    const dark = document.documentElement.dataset.theme === "dark";
    const compactGraph = box.clientWidth < 500;

    const initExpanded = new Set<string>();
    expandedRef.current = initExpanded;
    setSelectedFile(null);

    const palette = dark
      ? {
          text: "#E2E8F0",
          textMuted: "#94A3B8",
          nodeBg: "#111827",
          nodeBorder: "#334155",
          fileBg: "#0F172A",
          fileBorder: "#334155",
          modelBg: "#1E1B4B",
          modelBorder: "#6366F1",
          modelText: "#C7D2FE",
          focusBg: "#2563EB",
          focusBorder: "#60A5FA",
          selectedBg: "#1E1B4B",
          selectedBorder: "#A5B4FC",
          selectedText: "#E0E7FF",
          externalBg: "#111827",
          externalBorder: "#475569",
          externalText: "#94A3B8",
          edge: "#475569",
          edgeFocus: "#60A5FA",
        }
      : {
          text: "#1F2937",
          textMuted: "#52637A",
          nodeBg: "#F8FAFC",
          nodeBorder: "#B9C6D8",
          fileBg: "#F8FAFC",
          fileBorder: "#B9C6D8",
          modelBg: "#F5F3FF",
          modelBorder: "#C4B5FD",
          modelText: "#5B21B6",
          focusBg: "#2563EB",
          focusBorder: "#1D4ED8",
          selectedBg: "#EEF2FF",
          selectedBorder: "#A5B4FC",
          selectedText: "#3730A3",
          externalBg: "#F9FAFB",
          externalBorder: "#D1D5DB",
          externalText: "#6B7280",
          edge: "#9AA8BA",
          edgeFocus: "#2563EB",
        };

    const cy = cytoscape({
      container: box,
      elements: buildElements(data, expandedRef.current, compactGraph, mode),
      wheelSensitivity: 0.2,
      minZoom: 0.2,
      maxZoom: 2.5,
      style: [
        {
          selector: "node",
          style: {
            shape: "round-rectangle",
            "background-color": palette.nodeBg,
            "border-width": 1.5,
            "border-color": palette.nodeBorder,
            label: "data(glabel)",
            color: palette.text,
            // Size by importance (PageRank percentile): core symbols read bigger.
            "font-size": "mapData(importance, 0, 1, 13, 17)",
            "font-family": "system-ui, -apple-system, Segoe UI, sans-serif",
            "text-valign": "center",
            "text-halign": "center",
            "text-wrap": "wrap",
            "text-max-width": "160px",
            width: "label",
            height: "label",
            padding: "mapData(importance, 0, 1, 10, 19)",
            "transition-property": "border-width, background-color",
            "transition-duration": 120,
          },
        },
        {
          selector: "node[tone = 'model']",
          style: {
            "background-color": palette.modelBg,
            "border-color": palette.modelBorder,
            color: palette.modelText,
          },
        },
        {
          selector: "node[root = 1]",
          style: {
            "background-color": palette.focusBg,
            "border-color": palette.focusBorder,
            color: "#ffffff",
            "font-weight": 600,
          },
        },
        {
          selector: "node[external = 1]",
          style: {
            "border-style": "dashed",
            "background-color": palette.externalBg,
            "border-color": palette.externalBorder,
            color: palette.externalText,
            "font-style": "italic",
          },
        },
        {
          // Directory/package containers exist for grouping, but stay visual-only
          // invisible in the map so the user reads one structure: tree rows plus
          // dependency edges.
          selector: "node[isDirBox = 1]",
          style: {
            shape: "round-rectangle",
            "background-opacity": 0,
            "border-width": 0,
            "border-style": "solid",
            label: "",
            "events": "no",
            "text-valign": "top",
            "text-halign": "left",
            "text-background-opacity": 0,
            padding: "12px",
            "z-index": 0,
          },
        },
        {
          selector: "node[isDirBox = 1][external = 1]",
          style: {
            "border-style": "dashed",
            "background-opacity": 0,
          },
        },
        {
          // Expanded file container: a titled box wrapping its symbols.
          selector: "node[isFileBox = 1]",
          style: {
            shape: "round-rectangle",
            "background-color": dark ? "#0B1220" : "#F8FAFC",
            "background-opacity": 1,
            "border-width": 1,
            "border-color": dark ? "#1E293B" : "#E2E8F0",
            label: "",
            color: palette.text,
            "font-size": 13,
            "font-weight": 600,
            "font-style": "normal",
            "text-valign": "top",
            "text-halign": "left",
            "text-margin-x": 8,
            "text-margin-y": 7,
            "text-wrap": "wrap",
            "text-max-width": "120px",
            "text-background-color": dark ? "#111827" : "#FFFFFF",
            "text-background-opacity": 0.92,
            "text-background-shape": "roundrectangle",
            "text-background-padding": "4px",
            padding: "20px",
            "z-index": 0,
          },
        },
        {
          // Symbol/class scope container inside an expanded file. This is the
          // semantic-zoom layer between file and concrete symbol nodes.
          selector: "node[isSymbolBox = 1]",
          style: {
            shape: "round-rectangle",
            "background-color": dark ? "#111827" : "#FFFFFF",
            "background-opacity": dark ? 0.45 : 0.7,
            "border-width": 1,
            "border-style": "dotted",
            "border-color": dark ? "#334155" : "#CBD5E1",
            label: "data(scopeLabel)",
            color: palette.textMuted,
            "font-size": 11,
            "font-weight": 600,
            "text-valign": "top",
            "text-halign": "left",
            "text-margin-x": 7,
            "text-margin-y": 5,
            "text-wrap": "wrap",
            "text-max-width": "150px",
            padding: "16px",
            "z-index": 1,
          },
        },
        {
          // Collapsed file pill (the overview): a soft card standing in for a
          // file. Sized a touch by symbol count (a light "weight" cue).
          selector: "node[isFileMeta = 1]",
          style: {
            shape: "round-rectangle",
            "corner-radius": "8px",
            "background-color": palette.fileBg,
            "background-opacity": 1,
            "border-width": 1,
            "border-style": "solid",
            "border-color": palette.fileBorder,
            label: "data(glabel)",
            color: palette.text,
            "font-size": 14,
            "font-weight": 600,
            "font-style": "normal",
            "text-valign": "center",
            "text-halign": "center",
            "text-wrap": "wrap",
            "text-max-width": "210px",
            width: "label",
            height: "label",
            padding: "mapData(pill, 0, 1, 13, 20)",
          },
        },
        {
          // Semantic zoom: when the user is looking at module/file scale, concrete
          // symbols become small landmarks while file/scope labels keep the map readable.
          selector: "node[isConcreteSymbol = 1].zoom-low",
          style: {
            label: "",
            width: 12,
            height: 12,
            padding: "4px",
            opacity: 0.78,
          },
        },
        {
          selector: "node[isConcreteSymbol = 1].zoom-mid",
          style: {
            "font-size": 12,
            "text-max-width": "116px",
            padding: "9px",
            opacity: 0.96,
          },
        },
        {
          selector:
            "node[isConcreteSymbol = 1].zoom-low[root = 1], node[isConcreteSymbol = 1].zoom-low.reveal-label, node[isConcreteSymbol = 1].zoom-low.focus, node[isConcreteSymbol = 1].zoom-low.hl",
          style: {
            label: "data(glabel)",
            width: "label",
            height: "label",
            padding: "10px",
            opacity: 1,
          },
        },
        // Neighbours of the focused node — kept fully legible (no dim).
        {
          selector: "node.hl",
          style: { "border-width": 3, opacity: 1 },
        },
        // The focused node itself: quiet selected-symbol treatment, not another
        // competing primary-blue surface.
        {
          selector: "node.focus",
          style: {
            "border-width": 3,
            "border-color": palette.selectedBorder,
            "background-color": palette.selectedBg,
            color: palette.selectedText,
            "font-weight": 700,
            "z-index": 30,
          },
        },
        {
          selector: "edge",
          style: {
            // Width by call-site count (weight): a heavily-used call reads thicker.
            width: "mapData(weight, 1, 12, 1.2, 4.5)",
            "line-color": palette.edge,
            "target-arrow-color": palette.edge,
            "target-arrow-shape": "triangle",
            "arrow-scale": 0.9,
            "curve-style": "straight",
            opacity: 0.72,
          },
        },
        {
          // File-level aggregate edge (an expand reveals the exact references).
          selector: "edge[meta = 1]",
          style: { "line-style": "dashed", opacity: 0.24 },
        },
        {
          // Same-file references stay visible as plain edges.
          selector: "edge[scopeRoute = 1]",
          style: {
            opacity: 0.48,
            width: "mapData(weight, 1, 12, 0.8, 2.6)",
          },
        },
        {
          selector: "edge.zoom-low",
          style: {
            opacity: 0.16,
            width: "mapData(weight, 1, 12, 0.8, 2.4)",
          },
        },
        {
          selector: "edge.zoom-mid",
          style: {
            opacity: 0.42,
            width: "mapData(weight, 1, 12, 1, 3.2)",
          },
        },
        {
          selector: "edge[scopeRoute = 1].zoom-low",
          style: {
            opacity: 0.1,
            width: "mapData(weight, 1, 12, 0.65, 2)",
          },
        },
        {
          selector: "edge[scopeRoute = 1].zoom-mid",
          style: {
            opacity: 0.3,
            width: "mapData(weight, 1, 12, 0.8, 2.5)",
          },
        },
        {
          selector: "edge[meta = 1].zoom-low",
          style: { opacity: 0.12 },
        },
        {
          selector: "edge.hl",
          style: {
            "line-color": palette.edgeFocus,
            "target-arrow-color": palette.edgeFocus,
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
    let semanticRaf = 0;
    const scheduleSemanticZoom = () => {
      if (semanticRaf) return;
      semanticRaf = requestAnimationFrame(() => {
        semanticRaf = 0;
        applySemanticZoom(cy);
      });
    };
    cy.on("zoom resize", scheduleSemanticZoom);
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
        cy.elements().removeClass("focus hl faded reveal-label");
        cy.edges().not(nbr.edges()).addClass("faded");
        n.addClass("focus reveal-label");
        nbr.nodes().not(n).addClass("hl reveal-label");
        n.connectedEdges().addClass("hl");
      });
    };
    const clearFocus = () => {
      focusedRef.current = null;
      setSelectedFile(null);
      cy.elements().removeClass("focus hl faded reveal-label");
    };

    // Hover: a transient preview of the node + its neighbours. Suppressed while a
    // node is in persistent focus so it doesn't fight the focus highlight.
    cy.on("mouseover", "node", (evt) => {
      const n = evt.target;
      const d = n.data();
      box.style.cursor = d.isDirBox || d.isSymbolBox ? "default" : "pointer";
      if (d.isFileBox) {
        setHover({
          short: d.short,
          file: d.file,
          line: null,
          kind: `${d.count} symbols`,
        });
      } else if (d.isDirBox) {
        setHover({
          short: d.dirLabel,
          file: null,
          line: null,
          kind: `${d.count} files`,
        });
      } else if (d.isSymbolBox) {
        setHover({
          short: d.short,
          file: d.file,
          line: null,
          kind: `${d.count} contained symbols`,
        });
      } else if (d.isFileMeta) {
        setHover({ short: d.short, file: d.file, line: null, kind: `${d.count} symbols` });
      } else {
        setHover({ short: d.short, file: d.file, line: d.line, kind: d.kind });
      }
      if (focusedRef.current) return;
      const nbr = n.closedNeighborhood();
      cy.edges().not(nbr.edges()).addClass("faded"); // dim only unrelated edges
      n.addClass("hl reveal-label");
      nbr.nodes().not(n).addClass("hl reveal-label");
      n.connectedEdges().addClass("hl");
    });
    cy.on("mouseout", "node", () => {
      box.style.cursor = "";
      setHover(null);
      if (focusedRef.current) return;
      cy.elements().removeClass("faded hl reveal-label");
    });
    // Click a file pill / box → expand or collapse it. Click a symbol → focus it
    // (persistent degree-of-interest) AND open its source peek.
    cy.on("tap", "node", (evt) => {
      const d = evt.target.data();
      if (d.isDirBox || d.isSymbolBox) return;
      if (d.isFileMeta || d.isFileBox) {
        focusedRef.current = d.id;
        setSelectedFile(d.file);
        focusNode(evt.target);
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

    // Edge hover: spotlight the relationship, show its call-site count, and
    // (debounced) fetch a short LLM phrase describing the dependency.
    cy.on("mouseover", "edge", (evt) => {
      const e = evt.target;
      box.style.cursor = e.data("hasAnchor") ? "pointer" : "default";
      const eid = e.id();
      hoverEdgeIdRef.current = eid;
      const cachedLabel = edgeLabelCacheRef.current.get(eid);
      const oe = evt.originalEvent as MouseEvent | undefined;
      setEdgeHover({
        count: e.data("weight") || 0,
        src: e.data("srcDisplay") || e.data("srcShort") || "source",
        tgt: e.data("tgtDisplay") || e.data("tgtShort") || "target",
        aggregate: e.data("meta") === 1,
        label: cachedLabel,
        x: oe?.clientX,
        y: oe?.clientY,
      });
      if (!focusedRef.current) {
        cy.edges().not(e).addClass("faded"); // dim other edges; leave nodes legible
        e.addClass("hl");
        e.connectedNodes().addClass("hl reveal-label");
      }
      // Lazily fetch the dependency phrase (once per edge), debounced.
      if (cachedLabel === undefined && repoIdRef.current) {
        if (hoverTimerRef.current) clearTimeout(hoverTimerRef.current);
        hoverTimerRef.current = setTimeout(() => {
          if (hoverEdgeIdRef.current !== eid) return; // moved on before debounce fired
          const s = e.source().data();
          const t = e.target().data();
          const body = {
            source: {
              file: e.data("srcFile") || s.file || "",
              line: (s.line ?? null) as number | null,
              end_line: (s.endLine ?? null) as number | null,
              label: e.data("srcShort") || e.data("srcDisplay") || "",
            },
            target: {
              file: e.data("tgtFile") || t.file || "",
              line: (t.line ?? null) as number | null,
              end_line: (t.endLine ?? null) as number | null,
              label: e.data("tgtShort") || e.data("tgtDisplay") || "",
            },
            anchors: (e.data("labelAnchors") || e.data("anchors") || []) as CallSite[],
          };
          setEdgeHover((h) =>
            h && hoverEdgeIdRef.current === eid ? { ...h, label: "…" } : h
          );
          fetchEdgeLabel(repoIdRef.current as string, body)
            .then((r) => {
              const lbl = r.label || "";
              edgeLabelCacheRef.current.set(eid, lbl);
              setEdgeHover((h) =>
                h && hoverEdgeIdRef.current === eid ? { ...h, label: lbl } : h
              );
            })
            .catch(() => {
              setEdgeHover((h) =>
                h && hoverEdgeIdRef.current === eid ? { ...h, label: "" } : h
              );
            });
        }, 450);
      }
    });
    cy.on("mouseout", "edge", () => {
      box.style.cursor = "";
      hoverEdgeIdRef.current = null;
      if (hoverTimerRef.current) clearTimeout(hoverTimerRef.current);
      setEdgeHover(null);
      if (focusedRef.current) return;
      cy.elements().removeClass("faded hl reveal-label");
    });
    // Click an exact edge → open its LSP/SCIP call site(s). Aggregate (dashed)
    // edges carry no single site — expand the files to reach them.
    cy.on("tap", "edge", (evt) => {
      const edge = evt.target;
      const d = edge.data();
      if (d.anchors?.length) {
        // Pull the two endpoints' definition spans from the connected nodes so
        // an edge label can read both symbols' code. Concrete-symbol nodes carry
        // line/endLine; file pills (aggregate edges) don't → left undefined.
        const s = edge.source().data();
        const t = edge.target().data();
        onEdgeClickRef.current?.({
          anchors: d.anchors,
          srcLabel: d.srcShort || d.srcDisplay || "",
          tgtLabel: d.tgtShort || d.tgtDisplay || "",
          srcFile: d.srcFile,
          tgtFile: d.tgtFile,
          srcLine: s.line ?? null,
          srcEnd: s.endLine ?? null,
          tgtLine: t.line ?? null,
          tgtEnd: t.endLine ?? null,
        });
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
    if (expandedRef.current.size > 0) {
      for (const file of expandedRef.current) layoutExpandedFileInternals(cy, file, compactGraph);
      ensureGraphVisible(cy, compactGraph ? 10 : 20);
    }
    applySemanticZoom(cy);

    return () => {
      document.removeEventListener("keydown", onKey);
      cy.off("zoom resize", scheduleSemanticZoom);
      if (semanticRaf) cancelAnimationFrame(semanticRaf);
      cy.destroy();
      cyRef.current = null;
    };
  }, [data, mode]); // rebuild on a new graph; refs carry callbacks

  const closeSelectedFile = () => {
    focusedRef.current = null;
    setSelectedFile(null);
    cyRef.current?.elements().removeClass("focus hl faded reveal-label");
  };

  const openSymbol = (node: CMNode) => {
    onNodeClickRef.current?.({
      label: node.label,
      short: node.short,
      file: node.file ?? null,
      line: node.line ?? null,
      endLine: node.end_line ?? null,
      kind: node.kind,
      external: node.external === true,
    });
  };

  return (
    <div className="codegraph" data-variant={variant} data-mode={mode}>
      <div className="codegraph-stage">
        <div className="codegraph-canvas" ref={boxRef} aria-label="dependency graph" />
      </div>
      {selectedInfo && (
        <div className="codegraph-drilldown">
          <div className="codegraph-drilldown-head">
            <div className="codegraph-drilldown-title">
              <b>{selectedInfo.short}</b>
              <span className="mono">{selectedInfo.file}</span>
            </div>
            <div className="codegraph-drilldown-metrics">
              <span>{selectedInfo.symbols.length} symbols</span>
              <span>{selectedInfo.inbound} in</span>
              <span>{selectedInfo.outbound} out</span>
            </div>
            <button type="button" className="codegraph-drilldown-close" onClick={closeSelectedFile} aria-label="Close">
              ×
            </button>
          </div>
          <div className="codegraph-symbol-list">
            {selectedInfo.symbols.slice(0, 8).map((node) => (
              <button
                key={node.id}
                type="button"
                className={`codegraph-symbol-chip ${node.is_root ? "root" : ""}`}
                onClick={() => openSymbol(node)}
                title={`${node.label}${node.file ? ` (${node.file}:${node.line ?? "?"})` : ""}`}
              >
                <span className="codegraph-symbol-kind">{node.kind}</span>
                <span className="codegraph-symbol-name">{symbolName(node.short)}</span>
                {node.line != null && <span className="codegraph-symbol-line mono">:{node.line}</span>}
              </button>
            ))}
            {selectedInfo.symbols.length > 8 && (
              <span className="codegraph-symbol-more">+{selectedInfo.symbols.length - 8}</span>
            )}
          </div>
        </div>
      )}
      <div className="codegraph-bar">
        {edgeHover ? (
          <span className="cg-info">
            <b>{edgeHover.src} -&gt; {edgeHover.tgt}</b>
            <span className="muted">
              {" "}
              · {edgeHover.count} ref{edgeHover.count === 1 ? "" : "s"}
              {edgeHover.aggregate ? " · file-level" : " · exact"}
            </span>
            {edgeHover.label === "…" ? (
              <span className="muted"> · describing…</span>
            ) : edgeHover.label ? (
              <span className="cg-edgelabel"> · {edgeHover.label}</span>
            ) : null}
          </span>
        ) : hover ? (
          <span className="cg-info">
            <b>
              {hover.short}
            </b>
            {hover.file ? ` — ${hover.file}${hover.line ? `:${hover.line}` : ""}` : ""}
            <span className="codegraph-kind">{hover.kind}</span>
          </span>
        ) : (
          <span className="codegraph-legend" aria-label="Graph legend">
            <span className="codegraph-legend-title">Legend</span>
            <span className="codegraph-legend-item">
              <span className="codegraph-legend-line exact" aria-hidden="true" />
              <span>Exact symbol ref</span>
            </span>
            <span className="codegraph-legend-item">
              <span className="codegraph-legend-line aggregate" aria-hidden="true" />
              <span>File-level refs</span>
            </span>
            <span className="codegraph-legend-item">
              <span className="codegraph-legend-line weight" aria-hidden="true" />
              <span>Arrow = references</span>
            </span>
          </span>
        )}
        <span
          className="codegraph-depnote"
          title="SCIP/LSP references"
        >
          {selectedInfo ? `${selectedInfo.short} selected` : `${totalFiles} files`}
        </span>
        <button type="button" className="codegraph-fit" onClick={() => cyRef.current?.fit(undefined, 24)}>
          Fit
        </button>
      </div>
      {edgeHover && edgeHover.x != null && edgeHover.y != null && (
        <div
          className="cg-edge-tip"
          style={{ left: Math.min(edgeHover.x + 14, (typeof window !== "undefined" ? window.innerWidth : 9999) - 260), top: edgeHover.y + 14 }}
        >
          <div className="cg-edge-tip-head">
            {edgeHover.src} &rarr; {edgeHover.tgt}
          </div>
          <div className="cg-edge-tip-sub">
            {edgeHover.count} ref{edgeHover.count === 1 ? "" : "s"} ·{" "}
            {edgeHover.aggregate ? "file-level" : "exact"}
          </div>
          {edgeHover.label === "…" ? (
            <div className="cg-edge-tip-label muted">describing dependency…</div>
          ) : edgeHover.label ? (
            <div className="cg-edge-tip-label">{edgeHover.label}</div>
          ) : null}
        </div>
      )}
    </div>
  );
}
