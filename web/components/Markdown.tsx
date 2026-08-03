"use client";

import {
  Children,
  isValidElement,
  lazy,
  Suspense,
  type ReactElement,
  type ReactNode,
} from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeSlug from "rehype-slug";
import { matchCitation, lineLabel } from "@/lib/citations";
import {
  repoRelative,
  type Citation,
  type WikiRelationItem,
} from "@/lib/api";

// Mermaid (~1MB) is only needed when a diagram actually appears; load it on
// demand so it never weighs down pages that have none (wiki strips diagrams).
const Mermaid = lazy(() => import("./Mermaid"));
const HighlightedBlock = lazy(() => import("./HighlightedBlock"));

type ChildElement = ReactElement<{ children?: ReactNode }>;

function childElements(children: ReactNode): ChildElement[] {
  return Children.toArray(children).filter(
    (child): child is ChildElement =>
      isValidElement<{ children?: ReactNode }>(child),
  );
}

// Recursively collect plain text from React children (to recover raw code).
function nodeText(n: ReactNode): string {
  if (n == null || n === false) return "";
  if (typeof n === "string" || typeof n === "number") return String(n);
  if (Array.isArray(n)) return n.map(nodeText).join("");
  // @ts-expect-error - runtime prop access on element
  if (n.props?.children) return nodeText(n.props.children);
  return "";
}

function ResponsiveTable({ children }: { children: ReactNode }) {
  const sections = childElements(children);
  const head = sections.find((section) => section.type === "thead");
  const body = sections.find((section) => section.type === "tbody");
  const headerRow = head
    ? childElements(head.props.children).find((row) => row.type === "tr")
    : undefined;
  const headers = headerRow
    ? childElements(headerRow.props.children).map((cell) =>
        nodeText(cell.props.children),
      )
    : [];
  const rows = body
    ? childElements(body.props.children)
        .filter((row) => row.type === "tr")
        .map((row) => childElements(row.props.children))
    : [];

  return (
    <>
      <div className="table-scroll">
        <table>{children}</table>
      </div>
      {headers.length > 0 && rows.length > 0 && (
        <div className="table-cards" role="list">
          {rows.map((cells, rowIndex) => (
            <div className="table-card" role="listitem" key={rowIndex}>
              {cells.map((cell, cellIndex) => (
                <div className="table-card-field" key={cellIndex}>
                  <div className="table-card-label">
                    {headers[cellIndex] || `Column ${cellIndex + 1}`}
                  </div>
                  <div className="table-card-value">{cell.props.children}</div>
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </>
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
  // The line range belongs with the source reference at the end of the block,
  // not wedged into the middle of a sentence.
  const loc = "";
  void showLoc;
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
  relations,
  onCite,
}: {
  children: string;
  /** When provided (with onCite), inline code naming a citation becomes a clickable chip. */
  citations?: Citation[];
  /** Static relations, so an R# marker can name the call site it stands for. */
  relations?: WikiRelationItem[];
  onCite?: (index: number) => void;
}) {
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeSlug]}
        components={{
          a({ href, children, ...rest }) {
            // `[E3]` is an internal handle; the reader wants the source it
            // stands for. Resolve it to file and line range, the way a
            // reference in a technical document reads.
            const m = /^#evidence-([ER]\d+)$/.exec(String(href || ""));
            const marker = m ? m[1] : "";
            const index = marker.startsWith("E") ? Number(marker.slice(1)) - 1 : -1;
            const cite =
              index >= 0 && citations ? citations[index] : undefined;
            if (!cite && marker.startsWith("R") && relations) {
              // A relation stands for a call site; name that, not the handle.
              const rel = relations.find((r) => r.id === marker);
              const anchor = rel?.anchors?.[0];
              if (anchor) {
                const at = anchor.lastIndexOf(":");
                const file = at > 0 ? anchor.slice(0, at) : anchor;
                const line = at > 0 ? anchor.slice(at + 1) : "";
                const shown = repoRelative(file) ?? file;
                return (
                  <span className="cite-src" title={`${shown}${line ? `:${line}` : ""}`}>
                    {shown.split("/").pop() || shown}
                    {line && <span className="cite-src-loc">:{line}</span>}
                  </span>
                );
              }
            }
            if (cite) {
              const rel = repoRelative(cite.file) ?? cite.file;
              const loc = lineLabel(cite);
              return (
                <button
                  type="button"
                  className="cite-src"
                  title={`${rel}${loc ? `:${loc}` : ""}`}
                  onClick={() => onCite?.(index)}
                >
                  {rel.split("/").pop() || rel}
                  {loc && <span className="cite-src-loc">:{loc}</span>}
                </button>
              );
            }
            return (
              <a href={href} {...rest}>
                {children}
              </a>
            );
          },
          table({ children }) {
            return <ResponsiveTable>{children}</ResponsiveTable>;
          },
          pre({ children }) {
            const codeEl = (Array.isArray(children) ? children[0] : children) as
              | ReactElement<{ className?: string; children?: ReactNode }>
              | undefined;
            const className = codeEl?.props?.className || "";
            const text = nodeText(codeEl?.props?.children);
            if (/language-mermaid/.test(className)) {
              return (
                <Suspense fallback={<div className="mermaid-loading">Loading diagram…</div>}>
                  <Mermaid chart={text} />
                </Suspense>
              );
            }
            const lang = (className.match(/language-(\w+)/) || [])[1] || "";
            return (
              <Suspense
                fallback={
                  <div className="code-block">
                    <pre>
                      <code>{text}</code>
                    </pre>
                  </div>
                }
              >
                <HighlightedBlock text={text} language={lang} />
              </Suspense>
            );
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
