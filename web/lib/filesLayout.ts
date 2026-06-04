// Pure, deterministic "Files" dependency layout — extracted from CodeGraph so it
// can be unit-tested without a live Cytoscape/DOM instance.
//
// POSITION encodes meaning two ways at once:
//   • vertical axis = DEPENDENCY DEPTH — callers/entry points sit at the top,
//     their dependencies flow downward, leaves land at the bottom (top-down
//     dependency reading). Files that touch nothing else sink to a bottom band
//     so they don't crowd the entry row.
//   • each file is a compound box; symbols stack top→bottom by line inside it.
//
// Both axes are spaced by each node's MEASURED size (item.width / item.height,
// from Cytoscape), so neither wide labels (horizontal) nor wrapped multi-line
// labels (vertical) cause overlap — between lanes OR between dependency bands.
// When there are no cross-file edges, it falls back to size-packed columns.

export interface FilesLayoutItem {
  id: string;
  file: string | null;
  line: number | null;
  external: boolean; // external/locationless symbols share one "· external" box
  width?: number; // measured node outer width (px)  → non-overlapping lanes
  height?: number; // measured node outer height (px) → non-overlapping rows/bands
}

export interface FilesLayoutEdge {
  source: string; // symbol id (caller)
  target: string; // symbol id (callee / dependency)
}

export interface FilesLayoutConfig {
  rowGap?: number; // vertical gap between stacked symbols in a box
  colW?: number; // (fallback only) horizontal pitch between columns
  bandGap?: number; // vertical gap between dependency bands
  titlePad?: number; // headroom inside a box for its title
  laneGap?: number; // horizontal gap between boxes in the same band
  boxPad?: number; // box chrome added around the widest child
  bottomPad?: number; // box chrome below the last child
  defaultW?: number; // assumed child width when none was measured
  defaultH?: number; // assumed child height when none was measured
  maxColH?: number; // (fallback only) a box won't be placed in a column past this
}

export interface FilesLayoutResult {
  // compound parents to create, in placement order; `layer` is the dependency
  // depth (0 = top/entry), or null in the size-packed fallback.
  boxes: { id: string; file: string; layer: number | null }[];
  positions: Record<string, { x: number; y: number }>; // symbol id -> centre
  parentOf: Record<string, string>; // symbol id -> its file-box id
}

const DEFAULTS = {
  rowGap: 14,
  colW: 260,
  bandGap: 74,
  titlePad: 28,
  laneGap: 64,
  boxPad: 26,
  bottomPad: 16,
  defaultW: 190,
  defaultH: 32,
  maxColH: 1500,
};
const boxIdFor = (file: string) => `filebox::${file}`;
const fileKey = (it: FilesLayoutItem) =>
  it.external ? "· external" : it.file || "· external";

export function computeFilesPositions(
  items: FilesLayoutItem[],
  edges: FilesLayoutEdge[] = [],
  cfg: FilesLayoutConfig = {}
): FilesLayoutResult {
  const ROW_GAP = cfg.rowGap ?? DEFAULTS.rowGap;
  const BAND_GAP = cfg.bandGap ?? DEFAULTS.bandGap;
  const TITLE_PAD = cfg.titlePad ?? DEFAULTS.titlePad;
  const LANE_GAP = cfg.laneGap ?? DEFAULTS.laneGap;
  const BOX_PAD = cfg.boxPad ?? DEFAULTS.boxPad;
  const BOTTOM_PAD = cfg.bottomPad ?? DEFAULTS.bottomPad;
  const DEFAULT_W = cfg.defaultW ?? DEFAULTS.defaultW;
  const DEFAULT_H = cfg.defaultH ?? DEFAULTS.defaultH;

  // Group symbols by file (each file's symbols sorted by line).
  const byFile = new Map<string, FilesLayoutItem[]>();
  const fileOf: Record<string, string> = {};
  for (const it of items) {
    const f = fileKey(it);
    if (!byFile.has(f)) byFile.set(f, []);
    byFile.get(f)!.push(it);
    fileOf[it.id] = f;
  }
  for (const ns of byFile.values())
    ns.sort((a, b) => (a.line || 0) - (b.line || 0));

  const nodeH = (n: FilesLayoutItem) => n.height ?? DEFAULT_H;
  const nodeW = (n: FilesLayoutItem) => n.width ?? DEFAULT_W;
  // Box height = stacked child heights + gaps + title/bottom chrome.
  const boxHeight = (file: string) => {
    const ns = byFile.get(file)!;
    const content =
      ns.reduce((s, n) => s + nodeH(n), 0) + ROW_GAP * Math.max(0, ns.length - 1);
    return TITLE_PAD + content + BOTTOM_PAD;
  };
  const boxWidth = (file: string) =>
    Math.max(...byFile.get(file)!.map(nodeW)) + BOX_PAD * 2;

  // Place a file's symbols inside a box whose top-left content origin is (cx, top):
  // children share the lane centre x = cx, stacked by their measured heights.
  const placeBox = (
    file: string,
    cx: number,
    bandTop: number,
    positions: Record<string, { x: number; y: number }>,
    parentOf: Record<string, string>
  ) => {
    let yc = bandTop + TITLE_PAD;
    for (const n of byFile.get(file)!) {
      const h = nodeH(n);
      positions[n.id] = { x: cx, y: yc + h / 2 };
      parentOf[n.id] = boxIdFor(file);
      yc += h + ROW_GAP;
    }
  };

  // File-level dependency graph (caller file -> callee file).
  const adj = new Map<string, Set<string>>();
  const hasOut = new Set<string>();
  const hasIn = new Set<string>();
  for (const f of byFile.keys()) adj.set(f, new Set());
  for (const e of edges) {
    const fs = fileOf[e.source];
    const ft = fileOf[e.target];
    if (fs && ft && fs !== ft && !adj.get(fs)!.has(ft)) {
      adj.get(fs)!.add(ft);
      hasOut.add(fs);
      hasIn.add(ft);
    }
  }

  // Longest-path layering = dependency depth (bounded relaxation → cycle-safe).
  const layer = new Map<string, number>();
  for (const f of byFile.keys()) layer.set(f, 0);
  for (let iter = 0; iter < byFile.size; iter++) {
    let changed = false;
    for (const [u, outs] of adj) {
      for (const v of outs) {
        if (layer.get(v)! < layer.get(u)! + 1) {
          layer.set(v, layer.get(u)! + 1);
          changed = true;
        }
      }
    }
    if (!changed) break;
  }
  const depthMax = Math.max(0, ...layer.values());

  if (depthMax === 0) return sizePacked(byFile, boxHeight, boxWidth, placeBox, cfg);

  // Files touching nothing else sink to a dedicated bottom band.
  const bottom = depthMax + 1;
  let hasIsolated = false;
  for (const f of byFile.keys()) {
    if (!hasOut.has(f) && !hasIn.has(f)) {
      layer.set(f, bottom);
      hasIsolated = true;
    }
  }
  const maxLayer = hasIsolated ? bottom : depthMax;

  const layers: string[][] = Array.from({ length: maxLayer + 1 }, () => []);
  for (const f of [...byFile.keys()].sort()) layers[layer.get(f)!].push(f);

  const positions: Record<string, { x: number; y: number }> = {};
  const parentOf: Record<string, string> = {};
  const boxes: { id: string; file: string; layer: number | null }[] = [];

  let bandY = 0;
  for (let L = 0; L < layers.length; L++) {
    const filesInBand = layers[L];
    if (!filesInBand.length) continue;
    let cursor = 0;
    let bandHeight = 0;
    for (const file of filesInBand) {
      boxes.push({ id: boxIdFor(file), file, layer: L });
      const w = boxWidth(file);
      placeBox(file, cursor + w / 2, bandY, positions, parentOf);
      cursor += w + LANE_GAP;
      bandHeight = Math.max(bandHeight, boxHeight(file));
    }
    bandY += bandHeight + BAND_GAP; // next band clears the tallest box in this one
  }

  return { boxes, positions, parentOf };
}

// Fallback: largest files first, packed into the currently-shortest fitting
// column (shortest-column-first). Used when there are no cross-file edges.
function sizePacked(
  byFile: Map<string, FilesLayoutItem[]>,
  boxHeight: (file: string) => number,
  boxWidth: (file: string) => number,
  placeBox: (
    file: string,
    cx: number,
    bandTop: number,
    positions: Record<string, { x: number; y: number }>,
    parentOf: Record<string, string>
  ) => void,
  cfg: FilesLayoutConfig
): FilesLayoutResult {
  const COL_W = cfg.colW ?? DEFAULTS.colW;
  const BAND_GAP = cfg.bandGap ?? DEFAULTS.bandGap;
  const MAX_COL_H = cfg.maxColH ?? DEFAULTS.maxColH;

  const positions: Record<string, { x: number; y: number }> = {};
  const parentOf: Record<string, string> = {};
  const boxes: { id: string; file: string; layer: number | null }[] = [];

  const files = [...byFile.keys()].sort(
    (a, b) => byFile.get(b)!.length - byFile.get(a)!.length
  );
  const colY: number[] = [];
  const colX: number[] = [];
  for (const file of files) {
    boxes.push({ id: boxIdFor(file), file, layer: null });
    const boxH = boxHeight(file);
    let col = -1;
    let bestY = Infinity;
    colY.forEach((y, i) => {
      if (y + boxH <= MAX_COL_H && y < bestY) {
        col = i;
        bestY = y;
      }
    });
    if (col === -1) {
      col = colY.length;
      colY.push(0);
      colX.push(col === 0 ? boxWidth(file) / 2 : colX[col - 1] + COL_W);
    }
    placeBox(file, colX[col], colY[col], positions, parentOf);
    colY[col] += boxH + BAND_GAP;
  }

  return { boxes, positions, parentOf };
}
