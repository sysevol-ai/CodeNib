"use client";

import { useState } from "react";
import { highlightSource } from "@/lib/highlight";
import { GAP, segmentRows } from "@/lib/sourceExcerpt";

export default function HighlightedBlock({
  text,
  language,
  highlightLines,
  segmentStarts,
}: {
  text: string;
  language: string;
  /** Lines to mark as the ones the surrounding prose is about: 1-based within
   * the block, or real file lines when `segmentStarts` numbers the block. */
  highlightLines?: Set<number>;
  /** First file line of each segment of a source excerpt (fence `at=`). */
  segmentStarts?: number[];
}) {
  const [copied, setCopied] = useState(false);
  // A source excerpt shows real file lines: its definition's signature, a gap
  // row, then the lines the section is about.
  const rows = segmentStarts?.length ? segmentRows(text, segmentStarts) : null;
  const lineCount = text.replace(/\n$/, "").split("\n").length;
  const markOf = (index: number) =>
    rows ? rows.numbers[index] != null && highlightLines!.has(rows.numbers[index]!) : highlightLines!.has(index + 1);
  // A mark is only a mark when most lines are not marked; an excerpt with
  // nearly every line tinted reads as a selection, not as a pointer.
  const marked =
    highlightLines != null &&
    highlightLines.size > 0 &&
    highlightLines.size <= Math.max(1, Math.floor(lineCount / 2));
  // With marked lines or line numbers the block is rendered line by line so
  // each line can carry its own background and number; a short excerpt loses
  // little from per-line highlighting.
  const perLine = marked || rows != null;
  const lines = perLine ? text.replace(/\n$/, "").split("\n") : [];
  const html = perLine ? "" : highlightSource(text, language);
  const copyText = rows
    ? rows.lines.filter((_, index) => rows.numbers[index] != null).join("\n")
    : text;

  return (
    <div className={`code-block${marked ? " code-block-marked" : ""}${rows ? " code-block-numbered" : ""}`}>
      <div className="code-block-header">
        <span className="code-lang">{language || "code"}</span>
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
      </div>
      <pre>
        {perLine ? (
          <code className={`hljs${language ? ` language-${language}` : ""}`}>
            {lines.map((line, index) => {
              const number = rows ? rows.numbers[index] : undefined;
              const gap = rows != null && number == null;
              return (
                <span
                  key={index}
                  className={`code-line${marked && markOf(index) ? " is-marked" : ""}${gap ? " is-gap" : ""}`}
                >
                  {rows && (
                    <span className="code-ln" aria-hidden>
                      {gap ? "" : number}
                    </span>
                  )}
                  <span
                    dangerouslySetInnerHTML={{
                      __html: gap ? GAP : highlightSource(line, language),
                    }}
                  />
                  {"\n"}
                </span>
              );
            })}
          </code>
        ) : (
          <code
            className={`hljs${language ? ` language-${language}` : ""}`}
            dangerouslySetInnerHTML={{ __html: html }}
          />
        )}
      </pre>
    </div>
  );
}
