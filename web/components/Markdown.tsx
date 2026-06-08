"use client";

import { useState, type ReactElement, type ReactNode } from "react";
import dynamic from "next/dynamic";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeSlug from "rehype-slug";
import rehypeHighlight from "rehype-highlight";
import "highlight.js/styles/github.css";
import { matchCitation, lineLabel } from "@/lib/citations";
import type { Citation } from "@/lib/api";

// Mermaid (~1MB) is only needed when a diagram actually appears; load it on
// demand so it never weighs down pages that have none (wiki strips diagrams).
const Mermaid = dynamic(() => import("./Mermaid"), { ssr: false });

// Recursively collect plain text from React children (to recover raw code).
function nodeText(n: ReactNode): string {
  if (n == null || n === false) return "";
  if (typeof n === "string" || typeof n === "number") return String(n);
  if (Array.isArray(n)) return n.map(nodeText).join("");
  // @ts-expect-error - runtime prop access on element
  if (n.props?.children) return nodeText(n.props.children);
  return "";
}

function CodeBlock({ text, lang, children }: { text: string; lang: string; children: ReactNode }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="code-block">
      <div className="code-block-header">
        <span className="code-lang">{lang || "code"}</span>
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
      <pre>{children}</pre>
    </div>
  );
}

/** Inline `code` span that names a cited symbol/file: click to jump the code pane. */
function CiteChip({
  text,
  c,
  showLoc,
  onClick,
}: {
  text: string;
  c: Citation;
  /** Only symbol matches carry a meaningful range; a file has many symbols. */
  showLoc: boolean;
  onClick: () => void;
}) {
  const loc = showLoc ? lineLabel(c) : "";
  return (
    <button
      type="button"
      className="cite-chip"
      onClick={onClick}
      title={
        loc
          ? `Jump to ${c.node_name || text} (lines ${loc})`
          : `Jump to ${text} in the code panel`
      }
    >
      <code>{text}</code>
      {loc && <span className="cite-chip-loc">:{loc}</span>}
    </button>
  );
}

export default function Markdown({
  children,
  citations,
  onCite,
}: {
  children: string;
  /** When provided (with onCite), inline code naming a citation becomes a clickable chip. */
  citations?: Citation[];
  onCite?: (index: number) => void;
}) {
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeSlug, [rehypeHighlight, { ignoreMissing: true }]]}
        components={{
          pre({ children }) {
            const codeEl = (Array.isArray(children) ? children[0] : children) as
              | ReactElement<{ className?: string; children?: ReactNode }>
              | undefined;
            const className = codeEl?.props?.className || "";
            const text = nodeText(codeEl?.props?.children);
            if (/language-mermaid/.test(className)) {
              return <Mermaid chart={text} />;
            }
            const lang = (className.match(/language-(\w+)/) || [])[1] || "";
            return <CodeBlock text={text} lang={lang}>{children}</CodeBlock>;
          },
          code({ className, children }) {
            // Inline code only; block code has a language-* class or newlines.
            const text = nodeText(children);
            const inline = !/language-/.test(className || "") && !text.includes("\n");
            if (inline && citations && onCite) {
              const m = matchCitation(text, citations);
              if (m != null) {
                return (
                  <CiteChip
                    text={text}
                    c={citations[m.index]}
                    showLoc={m.kind === "symbol"}
                    onClick={() => onCite(m.index)}
                  />
                );
              }
            }
            return <code className={className}>{children}</code>;
          },
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
