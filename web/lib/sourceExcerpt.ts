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
