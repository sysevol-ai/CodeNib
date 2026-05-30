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
}: {
  code: string;
  file?: string | null;
  startLine?: number;
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

  return (
    <div className="hl-code">
      <div className="hl-gutter" aria-hidden>
        {Array.from({ length: lineCount }, (_, i) => (
          <div key={i}>{startLine + i}</div>
        ))}
      </div>
      <pre className="hl-pre">
        <code className="hljs" dangerouslySetInnerHTML={{ __html: html }} />
      </pre>
    </div>
  );
}
