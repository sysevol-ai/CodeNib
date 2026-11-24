// Rows of a source excerpt: the enclosing definition's signature, a gap row
// for the lines skipped, then the excerpt itself, each with its real file line.
// Every code view draws through here so a cropped window never loses the
// function it belongs to (see codenib/wiki/source_excerpt.py).

/** The row standing in for skipped lines; also the fence segment separator. */
export const GAP = "⋯";

export interface SourceDefinition {
  start_line: number;
  end_line?: number;
  content: string;
}

export interface ExcerptRows {
  lines: string[];
  /** Real 1-based file line of each row; null for a gap row. */
  numbers: (number | null)[];
}

function splitLines(text: string): string[] {
  return text.replace(/\n$/, "").split("\n");
}

/** Rows for a fetched slice and the definition the server says it starts in. */
export function excerptRows(
  code: string,
  startLine: number,
  definition?: SourceDefinition | null,
): ExcerptRows {
  const body = splitLines(code);
  const numbers: (number | null)[] = body.map((_, index) => startLine + index);
  if (!definition || !definition.content || definition.start_line >= startLine) {
    return { lines: body, numbers };
  }
  const head = splitLines(definition.content);
  return {
    lines: [...head, GAP, ...body],
    numbers: [...head.map((_, index) => definition.start_line + index), null, ...numbers],
  };
}

/** Parse a fence meta `at=288,300` into the first file line of each segment. */
export function parseSegmentStarts(meta: string): number[] {
  const match = /(?:^|\s)at=([\d,]+)/.exec(meta || "");
  if (!match) return [];
  return match[1]
    .split(",")
    .map((value) => Number.parseInt(value, 10))
    .filter((value) => Number.isFinite(value) && value > 0);
}

/**
 * Rows for an anchored fence body: segments separated by a GAP line, the
 * n-th starting at `starts[n]`. Returns null when the body and `at=` disagree,
 * so the caller falls back to an unnumbered block.
 */
export function segmentRows(text: string, starts: number[]): ExcerptRows | null {
  if (!starts.length) return null;
  const lines = splitLines(text);
  const numbers: (number | null)[] = [];
  let segment = 0;
  let line = starts[0];
  for (const row of lines) {
    if (row.trim() === GAP) {
      segment += 1;
      if (segment >= starts.length) return null;
      line = starts[segment];
      numbers.push(null);
      continue;
    }
    numbers.push(line);
    line += 1;
  }
  return segment === starts.length - 1 ? { lines, numbers } : null;
}

/** The definition an excerpt was cut from, loadable on demand. */
export interface ExcerptSource {
  /** First and last file line of the definition (1-based, inclusive). */
  start: number;
  end: number;
  /** The definition's lines, from `start`; null when unavailable. */
  load: () => Promise<string[] | null>;
}

/** A row of an excerpt being widened: a source line, or lines still hidden. */
export type ExcerptLayoutRow =
  | { kind: "line"; number: number }
  | { kind: "more"; where: "above" | "gap" | "below"; from: number; to: number };

/**
 * Rows for the excerpt's own lines plus those a reader revealed. Each run of
 * hidden lines inside the definition becomes one "more" row: above the first
 * line shown, between two shown lines, or below the last. Without a known
 * definition only the gaps between shown lines are rows.
 */
export function excerptLayout(
  shown: Iterable<number>,
  range?: { start: number; end: number } | null,
): ExcerptLayoutRow[] {
  const lines = [...new Set(shown)].sort((a, b) => a - b);
  const rows: ExcerptLayoutRow[] = [];
  if (!lines.length) return rows;
  if (range && lines[0] > range.start) {
    rows.push({ kind: "more", where: "above", from: range.start, to: lines[0] - 1 });
  }
  lines.forEach((number, index) => {
    const previous = lines[index - 1];
    if (previous != null && number > previous + 1) {
      rows.push({ kind: "more", where: "gap", from: previous + 1, to: number - 1 });
    }
    rows.push({ kind: "line", number });
  });
  const last = lines[lines.length - 1];
  if (range && last < range.end) {
    rows.push({ kind: "more", where: "below", from: last + 1, to: range.end });
  }
  return rows;
}

/** Lines revealed at once; a remainder this close to it is revealed whole. */
export const REVEAL_STEP = 20;

/**
 * The lines one click on a "more" row reveals: all of them when few remain,
 * otherwise the next REVEAL_STEP next to the code already shown. A gap opens
 * from its lower end, the context just above the lines the excerpt is about.
 */
export function revealLines(
  row: Extract<ExcerptLayoutRow, { kind: "more" }>,
  step = REVEAL_STEP,
): number[] {
  let { from, to } = row;
  if (to - from + 1 > step + step / 2) {
    if (row.where === "below") to = from + step - 1;
    else from = to - step + 1;
  }
  return Array.from({ length: to - from + 1 }, (_, index) => from + index);
}
