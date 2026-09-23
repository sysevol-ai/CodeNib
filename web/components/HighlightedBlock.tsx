"use client";

import { useState } from "react";
import { highlightSource } from "@/lib/highlight";

export default function HighlightedBlock({
  text,
  language,
  highlightLines,
}: {
  text: string;
  language: string;
  /** 1-based lines to mark as the ones the surrounding prose is about. */
  highlightLines?: Set<number>;
}) {
  const [copied, setCopied] = useState(false);
  const marked = highlightLines && highlightLines.size > 0;
  // With marked lines the block is rendered line by line so each line can
  // carry its own background; a short excerpt loses little from per-line
  // highlighting, and the marks are what the reader was sent here to see.
  const lines = marked ? text.replace(/\n$/, "").split("\n") : [];
  const html = marked ? "" : highlightSource(text, language);

  return (
    <div className={`code-block${marked ? " code-block-marked" : ""}`}>
      <div className="code-block-header">
        <span className="code-lang">{language || "code"}</span>
        <button
          className="copy-btn"
          aria-label="Copy code"
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(text);
              setCopied(true);
              setTimeout(() => setCopied(false), 1200);
            } catch {}
          }}
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre>
        {marked ? (
          <code className={`hljs${language ? ` language-${language}` : ""}`}>
            {lines.map((line, index) => (
              <span
                key={index}
                className={`code-line${highlightLines!.has(index + 1) ? " is-marked" : ""}`}
                dangerouslySetInnerHTML={{
                  __html: highlightSource(line, language) + "\n",
                }}
              />
            ))}
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
