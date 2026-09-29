"use client";

import { highlightSource, languageForFile } from "@/lib/highlight";
import { excerptRows, type SourceDefinition } from "@/lib/sourceExcerpt";

/**
 * A syntax-highlighted code fragment with a left line-number gutter. The gutter
 * and the code share one line-height so they stay aligned; `startLine` is the
 * real (1-based) line of the first row so fragments show true file lines.
 * When the fragment starts inside a function, `definition` is that function's
 * signature, drawn first with a gap row for the lines in between.
 */
export default function HighlightedCode({
  code,
  file,
  startLine = 1,
  definition,
  highlightLine,
  highlightEnd,
}: {
  code: string;
  file?: string | null;
  startLine?: number;
  definition?: SourceDefinition | null;
  /** 1-based absolute line to spotlight (e.g. an exact call site, or a span start). */
  highlightLine?: number | null;
  /** 1-based end of the spotlight; defaults to highlightLine (a single line). */
  highlightEnd?: number | null;
}) {
  const rows = code ? excerptRows(code, startLine, definition) : { lines: [], numbers: [] };
  const html = highlightSource(rows.lines.join("\n"), languageForFile(file));
  const hlA = highlightLine ?? null;
  const hlB = highlightEnd ?? highlightLine ?? null;
  // The spotlight covers slice rows only, never the signature drawn above.
  const spotlit = rows.numbers.map(
    (line) => line != null && line >= startLine && hlA != null && hlB != null && line >= hlA && line <= hlB,
  );
  const bandFrom = spotlit.indexOf(true);
  const bandTo = spotlit.lastIndexOf(true);
  const hasBand = bandFrom >= 0;
  const LINE_H = 19.375; // 12.5px × 1.55 — matches the gutter & code line-height

  return (
    <div className="hl-code">
      {hasBand && (
        <div
          className="hl-band"
          style={{
            top: `${10 + bandFrom * LINE_H}px`,
            height: `${(bandTo - bandFrom + 1) * LINE_H}px`,
          }}
          aria-hidden
        />
      )}
      <div className="hl-gutter" aria-hidden>
        {rows.numbers.map((line, i) => (
          <div
            key={i}
            className={
              line == null ? "hl-gutter-gap" : spotlit[i] ? "hl-gutter-on" : undefined
            }
          >
            {line ?? "⋯"}
          </div>
        ))}
      </div>
      <pre className="hl-pre">
        <code className="hljs" dangerouslySetInnerHTML={{ __html: html }} />
      </pre>
    </div>
  );
}
