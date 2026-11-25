"use client";

import { useMemo, useState } from "react";
import { highlightRows, highlightSource } from "@/lib/highlight";
import {
  GAP,
  excerptLayout,
  revealLines,
  segmentRows,
  type ExcerptLayoutRow,
  type ExcerptSource,
} from "@/lib/sourceExcerpt";

type MoreRow = Extract<ExcerptLayoutRow, { kind: "more" }>;

function moreLabel(row: MoreRow): string {
  const count = row.to - row.from + 1;
  const lines = `${count} ${count === 1 ? "line" : "lines"}`;
  if (row.where === "above") return `${lines} above`;
  if (row.where === "below") return `${lines} below`;
  return `${lines} hidden`;
}

const MORE_ICON = { above: "↑", gap: "↕", below: "↓" } as const;

export default function HighlightedBlock({
  text,
  language,
  highlightLines,
  segmentStarts,
  source,
}: {
  text: string;
  language: string;
  /** Lines to mark as the ones the surrounding prose is about: 1-based within
   * the block, or real file lines when `segmentStarts` numbers the block. */
  highlightLines?: Set<number>;
  /** First file line of each segment of a source excerpt (fence `at=`). */
  segmentStarts?: number[];
  /** The definition the excerpt was cut from (fence `cite=`), so a reader can
   * show the lines around it. */
  source?: ExcerptSource | null;
}) {
  const [copied, setCopied] = useState(false);
  const [revealed, setRevealed] = useState<number[]>([]);
  const [full, setFull] = useState<string[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const startsKey = (segmentStarts || []).join(",");

  // A source excerpt shows real file lines: its definition's signature, a gap
  // row, then the lines the section is about.
  const rows = useMemo(
    () => (startsKey ? segmentRows(text, startsKey.split(",").map(Number)) : null),
    [text, startsKey],
  );
  // Each file line the excerpt itself shows, with its text.
  const own = useMemo(() => {
    const lines = new Map<number, string>();
    rows?.numbers.forEach((number, index) => {
      if (number != null) lines.set(number, rows.lines[index]);
    });
    return lines;
  }, [rows]);
  // Highlight whole runs, never single lines, so a docstring stays a string.
  const markup = useMemo(() => {
    const html = new Map<number, string>();
    const first = source?.start;
    if (full && first != null) {
      highlightRows(full.join("\n"), language).forEach((row, index) =>
        html.set(first + index, row),
      );
      return html;
    }
    let run: number[] = [];
    const flush = () => {
      if (!run.length) return;
      const lines = highlightRows(run.map((n) => own.get(n)!).join("\n"), language);
      run.forEach((number, index) => html.set(number, lines[index]));
      run = [];
    };
    for (const number of own.keys()) {
      if (run.length && number !== run[run.length - 1] + 1) flush();
      run.push(number);
    }
    flush();
    return html;
  }, [full, source?.start, own, language]);

  const lineCount = text.replace(/\n$/, "").split("\n").length;
  // A mark is only a mark when most lines are not marked; an excerpt with
  // nearly every line tinted reads as a selection, not as a pointer.
  const marked =
    highlightLines != null &&
    highlightLines.size > 0 &&
    highlightLines.size <= Math.max(1, Math.floor(lineCount / 2));
  const expandable = rows != null && source != null && !unavailable;
  const layout = rows
    ? excerptLayout([...own.keys(), ...revealed], expandable ? source : null)
    : null;
  const textOf = (number: number) =>
    full && source ? full[number - source.start] ?? "" : own.get(number) ?? "";

  async function reveal(row: MoreRow) {
    if (!source) return;
    let lines = full;
    if (!lines) {
      setLoading(true);
      try {
        lines = await source.load();
      } catch {
        lines = null;
      }
      setLoading(false);
      // Widen only around the very lines shown; another revision of the file
      // would put the wrong code next to them.
      const loaded = lines;
      const matches =
        loaded != null &&
        [...own].every(
          ([number, line]) => loaded[number - source.start]?.trim() === line.trim(),
        );
      if (!matches) {
        setUnavailable(true);
        return;
      }
      setFull(loaded);
    }
    setRevealed((previous) => [...previous, ...revealLines(row)]);
  }

  // Unnumbered blocks with marks still need one row per line.
  const plainLines =
    !rows && marked ? highlightRows(text.replace(/\n$/, ""), language) : null;
  const copyText = layout
    ? layout
        .filter((row): row is { kind: "line"; number: number } => row.kind === "line")
        .map((row) => textOf(row.number))
        .join("\n")
    : text;

  return (
    <div
      className={`code-block${marked ? " code-block-marked" : ""}${rows ? " code-block-numbered" : ""}`}
    >
      <div className="code-block-header">
        <span className="code-lang">{language || "code"}</span>
        <span className="code-block-actions">
          {revealed.length > 0 && (
            <button className="copy-btn" onClick={() => setRevealed([])}>
              Show less
            </button>
          )}
          <button
            className="copy-btn"
            aria-label="Copy code"
            onClick={async () => {
              try {
                await navigator.clipboard.writeText(copyText);
                setCopied(true);
                setTimeout(() => setCopied(false), 1200);
              } catch {}
            }}
          >
            {copied ? "Copied" : "Copy"}
          </button>
        </span>
      </div>
      <pre>
        {layout ? (
          <code className={`hljs${language ? ` language-${language}` : ""}`}>
            {layout.map((row) => {
              if (row.kind === "line") {
                const isMarked = marked && highlightLines!.has(row.number);
                return (
                  <span
                    key={row.number}
                    className={`code-line${isMarked ? " is-marked" : ""}`}
                  >
                    <span className="code-ln" aria-hidden>
                      {row.number}
                    </span>
                    <span dangerouslySetInnerHTML={{ __html: markup.get(row.number) ?? "" }} />
                    {"\n"}
                  </span>
                );
              }
              if (!expandable) {
                // Nothing to load: keep the gap row, with its size on hover.
                return row.where === "gap" ? (
                  <span key={`${row.where}-${row.from}`} className="code-line is-gap">
                    <span className="code-ln" aria-hidden />
                    <span title={moreLabel(row)}>{GAP}</span>
                    {"\n"}
                  </span>
                ) : null;
              }
              return (
                <span key={`${row.where}-${row.from}`} className="code-line is-more">
                  <button
                    type="button"
                    className="code-more"
                    disabled={loading}
                    onClick={() => void reveal(row)}
                    aria-label={`Show ${moreLabel(row)}`}
                  >
                    <span className="code-ln" aria-hidden>
                      {MORE_ICON[row.where]}
                    </span>
                    <span>{loading ? "Loading…" : moreLabel(row)}</span>
                  </button>
                </span>
              );
            })}
          </code>
        ) : plainLines ? (
          <code className={`hljs${language ? ` language-${language}` : ""}`}>
            {plainLines.map((line, index) => (
              <span
                key={index}
                className={`code-line${highlightLines!.has(index + 1) ? " is-marked" : ""}`}
              >
                <span dangerouslySetInnerHTML={{ __html: line }} />
                {"\n"}
              </span>
            ))}
          </code>
        ) : (
          <code
            className={`hljs${language ? ` language-${language}` : ""}`}
            dangerouslySetInnerHTML={{ __html: highlightSource(text, language) }}
          />
        )}
      </pre>
    </div>
  );
}
