"use client";

import { useState } from "react";
import { highlightSource } from "@/lib/highlight";

export default function HighlightedBlock({
  text,
  language,
}: {
  text: string;
  language: string;
}) {
  const [copied, setCopied] = useState(false);
  const html = highlightSource(text, language);

  return (
    <div className="code-block">
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
        <code
          className={`hljs${language ? ` language-${language}` : ""}`}
          dangerouslySetInnerHTML={{ __html: html }}
        />
      </pre>
    </div>
  );
}
