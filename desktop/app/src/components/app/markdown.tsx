import { Children, isValidElement, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { backend } from "@/lib/backend";
import { CodeLine } from "./CodeLines";

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

/** A fenced code block, set in ink alone like every code card in the app. */
function CodeBlock({ code }: { code: string }) {
  const lines = code.replace(/\n$/, "").split("\n");
  return (
    <div className="code-card md-code">
      <pre className="code-lines">
        <code>
          {lines.map((line, i) => (
            <span key={i} className="code-row">
              <span className="code-t">
                <CodeLine text={line} />
              </span>
            </span>
          ))}
        </code>
      </pre>
    </div>
  );
}

export function Markdown({
  text,
  onPage,
}: {
  text: string;
  /** Open another page of the same Wiki. */
  onPage?: (page: string) => void;
}) {
  const components: Components = {
    h1: ({ children }) => <h2>{children}</h2>,
    a: ({ href, children }) => {
      const target = href ?? "";
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
      return <CodeBlock code={textOf(code ?? children)} />;
    },
    table: ({ children }) => (
      <div className="md-table">
        <table>{children}</table>
      </div>
    ),
  };
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
      {text}
    </ReactMarkdown>
  );
}
