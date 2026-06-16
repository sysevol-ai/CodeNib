"use client";

import hljs from "highlight.js";

const EXT_LANG: Record<string, string> = {
  py: "python",
  pyx: "python",
  go: "go",
  rs: "rust",
  ts: "typescript",
  tsx: "typescript",
  js: "javascript",
  jsx: "javascript",
  mjs: "javascript",
  c: "c",
  h: "cpp",
  cc: "cpp",
  cpp: "cpp",
  hpp: "cpp",
  java: "java",
  rb: "ruby",
  json: "json",
  yml: "yaml",
  yaml: "yaml",
};

/**
 * A syntax-highlighted code fragment with a left line-number gutter. The gutter
 * and the code share one line-height so they stay aligned; `startLine` is the
 * real (1-based) line of the first row so fragments show true file lines.
 */
export default function HighlightedCode({
  code,
  file,
  startLine = 1,
  highlightLine,
  highlightEnd,
}: {
  code: string;
  file?: string | null;
  startLine?: number;
  /** 1-based absolute line to spotlight (e.g. an exact call site, or a span start). */
  highlightLine?: number | null;
  /** 1-based end of the spotlight; defaults to highlightLine (a single line). */
  highlightEnd?: number | null;
}) {
  const ext = (file || "").split(".").pop()?.toLowerCase() || "";
  const lang = EXT_LANG[ext];
  let html = "";
  try {
    html = lang
      ? hljs.highlight(code, { language: lang }).value
      : hljs.highlightAuto(code).value;
  } catch {
    html = code.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]!);
  }
  const lineCount = code ? code.split("\n").length : 0;
  const hlA = highlightLine ?? null;
  const hlB = highlightEnd ?? highlightLine ?? null;
  const bandFrom = hlA != null ? Math.max(0, hlA - startLine) : -1;
  const bandTo = hlB != null ? Math.min(lineCount - 1, hlB - startLine) : -1;
  const hasBand = hlA != null && bandTo >= 0 && bandFrom <= bandTo;
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
        {Array.from({ length: lineCount }, (_, i) => (
          <div key={i} className={hasBand && i >= bandFrom && i <= bandTo ? "hl-gutter-on" : undefined}>
            {startLine + i}
          </div>
        ))}
      </div>
      <pre className="hl-pre">
        <code className="hljs" dangerouslySetInnerHTML={{ __html: html }} />
      </pre>
    </div>
  );
}
