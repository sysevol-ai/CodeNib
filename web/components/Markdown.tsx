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
import { AppLink } from "@/lib/router";
import { matchCitation, lineLabel } from "@/lib/citations";
import { callPaths, parseFlowchart } from "@/lib/flowchart";
import CallChain from "./CallChain";
import {
  repoRelative,
  type Citation,
  type WikiRelationItem,
} from "@/lib/api";

// Mermaid (~1MB) is only needed when a diagram actually appears; load it on
// demand so it never weighs down pages that have none (wiki strips diagrams).
const Mermaid = lazy(() => import("./Mermaid"));
const HighlightedBlock = lazy(() => import("./HighlightedBlock"));

/** Parse a fence meta such as `hl=2,5-7` into 1-based line numbers. */
function parseHighlightLines(meta: string): Set<number> {
  const lines = new Set<number>();
  const match = /(?:^|\s)hl=([\d,-]+)/.exec(meta || "");
  if (!match) return lines;
  for (const part of match[1].split(",")) {
    const range = part.split("-").map((value) => Number.parseInt(value, 10));
    if (range.some((value) => Number.isNaN(value))) continue;
    const [start, end = start] = range;
    for (let line = start; line <= end && line - start < 200; line += 1) {
      if (line > 0) lines.add(line);
    }
  }
  return lines;
}

/** Is this rendered child one of our citation references? */
function isCitationElement(child: ReactNode): boolean {
  return (
    isValidElement<{ href?: string }>(child) &&
    /^#evidence-[ER]\d+$/.test(String(child.props.href || ""))
  );
}

/** Move the trailing run of citations into one group so they read as a
 *  reference list at the end of the block rather than as words in it. */
function groupTrailingCitations(children: ReactNode): ReactNode {
  const items = Children.toArray(children);
  let end = items.length;
  while (end > 0) {
    const item = items[end - 1];
    if (isCitationElement(item) || (typeof item === "string" && !item.trim())) {
      end -= 1;
    } else {
      break;
    }
  }
  const tail = items.slice(end).filter(isCitationElement);
  if (tail.length === 0) return children;
  const body = items.slice(0, end);
  return (
    <>
      {body}
      <span className="cite-group">{tail}</span>
    </>
  );
}

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

/** A wiki page link the generator writes as `?p=<page-id>`. */
const WIKI_PAGE_LINK_RE = /^\?p=([^&#]+)$/;

export default function Markdown({
  children,
  citations,
  relations,
  onCite,
  repoId,
  onPageLink,
}: {
  children: string;
  /** When provided (with onCite), inline code naming a citation becomes a clickable chip. */
  citations?: Citation[];
  /** Static relations, so an R# marker can name the call site it stands for. */
  relations?: WikiRelationItem[];
  onCite?: (index: number) => void;
  /** Repository the `?p=<page>` links belong to. The document carries a
   *  `<base href>`, so a relative `?p=` link would otherwise resolve against
   *  the site root and land on the landing page instead of the wiki page. */
  repoId?: string;
  /** Switch wiki pages in place instead of routing through the app shell. */
  onPageLink?: (pageId: string) => void;
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
            const pageMatch = WIKI_PAGE_LINK_RE.exec(String(href || ""));
            if (pageMatch && repoId) {
              let pageId = pageMatch[1];
              try {
                pageId = decodeURIComponent(pageId);
              } catch {
                // Keep the raw id; the page lookup will report it as missing.
              }
              const target = `/${encodeURIComponent(repoId)}?p=${encodeURIComponent(pageId)}`;
              return (
                <AppLink
                  href={target}
                  onClick={
                    onPageLink
                      ? (event) => {
                          if (
                            event.button !== 0 ||
                            event.metaKey ||
                            event.ctrlKey ||
                            event.shiftKey ||
                            event.altKey
                          ) {
                            return;
                          }
                          event.preventDefault();
                          onPageLink(pageId);
                        }
                      : undefined
                  }
                  {...rest}
                >
                  {children}
                </AppLink>
              );
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
          p({ children }) {
            return <p>{groupTrailingCitations(children)}</p>;
          },
          li({ children }) {
            return <li>{groupTrailingCitations(children)}</li>;
          },
          pre({ children }) {
            const codeEl = (Array.isArray(children) ? children[0] : children) as
              | ReactElement<{ className?: string; children?: ReactNode }>
              | undefined;
            const className = codeEl?.props?.className || "";
            const text = nodeText(codeEl?.props?.children);
            if (/language-mermaid/.test(className)) {
              // A straight call chain reads better as a list than as a row of
              // boxes; only a flow that branches is drawn.
              const flow = parseFlowchart(text);
              const paths = flow ? callPaths(flow) : null;
              if (paths) {
                return (
                  <CallChain
                    paths={paths}
                    relations={relations}
                    renderSymbol={(label) => {
                      const m = citations && onCite ? matchCitation(label, citations) : null;
                      return m != null && onCite ? (
                        <CiteChip
                          text={label}
                          c={citations![m.index]}
                          showLoc={false}
                          onClick={() => onCite(m.index)}
                        />
                      ) : (
                        <code>{label}</code>
                      );
                    }}
                  />
                );
              }
              return (
                <Suspense fallback={<div className="mermaid-loading">Loading diagram…</div>}>
                  <Mermaid chart={text} />
                </Suspense>
              );
            }
            const lang = (className.match(/language-(\w+)/) || [])[1] || "";
            // The fence info string (`python hl=3,5-6`) names the lines the
            // surrounding prose is about; remark keeps it as the node's meta.
            const meta = String(
              (codeEl?.props as { node?: { data?: { meta?: string } } } | undefined)
                ?.node?.data?.meta || "",
            );
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
                <HighlightedBlock
                  text={text}
                  language={lang}
                  highlightLines={parseHighlightLines(meta)}
                />
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
