import { Children, isValidElement, lazy, Suspense, useMemo, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { backend } from "@/lib/backend";
import { highlightLines } from "@/lib/highlight";
import type { Page } from "@/lib/model";
import { SourceMark } from "./SourceMark";

// Mermaid is large; it loads the first time a page draws a diagram.
const Mermaid = lazy(() => import("./Mermaid"));

/** One top-level block of a page: a heading, a paragraph, a list, a table or
 *  a code block. Each one gets its own handle for Ask, Change and Note. */
export interface MdBlock {
  text: string;
  heading: number;
  title: string;
}

export function splitBlocks(markdown: string): MdBlock[] {
  const blocks: MdBlock[] = [];
  let current: string[] = [];
  let fence = "";
  const flush = () => {
    const text = current.join("\n").trim();
    current = [];
    if (!text) return;
    const heading = /^(#{1,6})\s+(.*)$/.exec(text.split("\n")[0]);
    blocks.push({
      text,
      heading: heading && !text.includes("\n") ? heading[1].length : 0,
      title: heading ? heading[2].replace(/[`*_]/g, "").trim() : "",
    });
  };
  for (const line of markdown.split("\n")) {
    const trimmed = line.trim();
    const marker = /^(```|~~~)/.exec(trimmed)?.[1];
    if (fence) {
      current.push(line);
      if (marker === fence) {
        fence = "";
        flush();
      }
      continue;
    }
    if (marker) {
      flush();
      fence = marker;
      current.push(line);
      continue;
    }
    if (!trimmed) {
      flush();
      continue;
    }
    if (/^#{1,6}\s/.test(trimmed)) {
      flush();
      current.push(line);
      flush();
      continue;
    }
    current.push(line);
  }
  flush();
  return blocks;
}

function textOf(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) return textOf(node.props.children);
  return "";
}

/** A fenced code block, coloured for the language its fence names. */
function CodeBlock({ code, lang }: { code: string; lang?: string }) {
  const lines = useMemo(() => highlightLines(code.replace(/\n$/, ""), lang), [code, lang]);
  return (
    <div className="code-card md-code">
      <pre className="code-lines hljs">
        <code>
          {lines.map((line, i) => (
            <span key={i} className="code-row">
              <span className="code-t" dangerouslySetInnerHTML={{ __html: line }} />
            </span>
          ))}
        </code>
      </pre>
    </div>
  );
}

/** The language a fenced block names: ```python gives "python". */
function fenceLanguage(node: ReactNode): string {
  if (!isValidElement<{ className?: string }>(node)) return "";
  return /language-([\w#+.-]+)/.exec(node.props.className ?? "")?.[1]?.toLowerCase() ?? "";
}

/** A symbol as a reader names it: `pkg/mod.py:Store.get()` -> `Store.get`. */
function shortSymbol(symbol: string): string {
  const name = (symbol.split(":").pop() ?? symbol).replace(/\(\)$/, "").trim();
  const parts = name.split(".");
  return parts.length > 2 ? parts.slice(-2).join(".") : name;
}

/** Keep a chip short enough to sit inside a sentence. */
function clip(text: string, max = 32): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

/**
 * The code an [E1](#evidence-E1) or [R2](#evidence-R2) citation names, with
 * a chip label that says what it is: the symbol for code evidence, "a → b"
 * for a recorded call.
 */
function citedCode(
  evidence: Page["evidence"],
  id: string,
): { file: string; start: number; end: number; what: string; label: string } | null {
  const item = evidence?.items?.find((e) => e.id === id);
  if (item?.file && item.start_line) {
    const name = item.symbol ? shortSymbol(item.symbol) : "";
    const base = item.file.split("/").pop() ?? item.file;
    return { file: item.file, start: item.start_line, end: item.end_line, what: name || item.file, label: clip(name || base) };
  }
  const relation = evidence?.relations?.find((r) => r.id === id);
  const anchor = relation?.anchors?.[0];
  const at = anchor ? /^(.*):(\d+)$/.exec(anchor) : null;
  if (relation && at) {
    const from = shortSymbol(relation.source);
    const to = shortSymbol(relation.target);
    return {
      file: at[1],
      start: Number(at[2]),
      end: Number(at[2]),
      what: `${from} calls ${to}`,
      label: `${clip(from, 18)} → ${clip(to, 18)}`,
    };
  }
  return null;
}

export function Markdown({
  text,
  onPage,
  repo,
  evidence,
}: {
  text: string;
  /** Open another page of the same Wiki. */
  onPage?: (page: string) => void;
  /** With the page's evidence, citations open the code they name. */
  repo?: string;
  evidence?: Page["evidence"];
}) {
  // One map for the life of these props: a new map each render would make
  // react-markdown remount every link, closing a citation's open code.
  const components: Components = useMemo(() => ({
    h1: ({ children }) => <h2>{children}</h2>,
    a: ({ href, children }) => {
      const target = href ?? "";
      const cited = /^#evidence-([ER]\d+)$/.exec(target)?.[1];
      if (cited) {
        const code = repo ? citedCode(evidence, cited) : null;
        if (!code) return <span className="cite is-plain">{cited}</span>;
        return (
          <SourceMark
            repo={repo!}
            file={code.file}
            start={code.start}
            end={code.end}
            label={code.label}
            className="cite"
            title={`${cited} · ${code.what} · ${code.file.split("/").pop()}:${code.start}`}
          />
        );
      }
      const page = /^\?p=([^&#]+)/.exec(target)?.[1];
      if (page && onPage) {
        return (
          <a
            href={target}
            className="md-link"
            onClick={(e) => {
              e.preventDefault();
              onPage(decodeURIComponent(page));
            }}
          >
            {children}
          </a>
        );
      }
      if (/^https?:\/\//.test(target)) {
        return (
          <a
            href={target}
            className="md-link"
            onClick={(e) => {
              e.preventDefault();
              void backend.openExternal(target);
            }}
          >
            {children}
          </a>
        );
      }
      return <span className="md-link-dead">{children}</span>;
    },
    // Remote images would leave the machine; the Wiki shows their words.
    img: ({ alt }) => (alt ? <span className="md-img-alt">{alt}</span> : null),
    pre: ({ children }) => {
      const code = Children.toArray(children).find(isValidElement);
      const lang = fenceLanguage(code);
      const text = textOf(code ?? children);
      if (lang === "mermaid") {
        return (
          <Suspense fallback={<div className="mermaid-diagram is-loading">Drawing the diagram…</div>}>
            <Mermaid chart={text.replace(/\n$/, "")} />
          </Suspense>
        );
      }
      return <CodeBlock code={text} lang={lang} />;
    },
    table: ({ children }) => (
      <div className="md-table">
        <table>{children}</table>
      </div>
    ),
  }), [onPage, repo, evidence]);
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
      {text}
    </ReactMarkdown>
  );
}
