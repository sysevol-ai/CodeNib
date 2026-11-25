import { Fragment } from "react";
import { cn } from "@/lib/utils";

const KEYWORDS = new Set([
  "def", "return", "if", "elif", "else", "is", "not", "None", "and", "or", "in", "for",
  "class", "raise", "with", "as", "assert", "True", "False", "import", "from", "self",
]);

/** Colour code with ink alone: keywords stand forward, strings and comments
 *  step back. The accent stays free for what a person or the agent wrote. */
function tokens(line: string) {
  const parts: { text: string; kind: "kw" | "str" | "com" | "plain" }[] = [];
  const re = /(#.*$)|("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|"""[^]*?""")|([A-Za-z_][A-Za-z0-9_]*)|(\s+|[^A-Za-z_\s"'#]+)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(line))) {
    if (m[1]) parts.push({ text: m[1], kind: "com" });
    else if (m[2]) parts.push({ text: m[2], kind: "str" });
    else if (m[3]) parts.push({ text: m[3], kind: KEYWORDS.has(m[3]) ? "kw" : "plain" });
    else parts.push({ text: m[0], kind: "plain" });
  }
  return parts;
}

export function CodeLine({ text }: { text: string }) {
  return (
    <>
      {tokens(text).map((t, i) => (
        <Fragment key={i}>
          {t.kind === "plain" ? t.text : <span className={`tok-${t.kind}`}>{t.text}</span>}
        </Fragment>
      ))}
    </>
  );
}

export function CodeLines({
  text,
  start,
  from,
  to,
  highlight = [],
  className,
}: {
  text: string;
  start: number;
  from?: number;
  to?: number;
  highlight?: number[];
  className?: string;
}) {
  const lines = text.split("\n");
  const first = from ?? start;
  const last = to ?? start + lines.length - 1;
  return (
    <pre className={cn("code-lines", className)}>
      <code>
        {lines.slice(first - start, last - start + 1).map((line, i) => {
          const n = first + i;
          return (
            <span key={n} className={cn("code-row", highlight.includes(n) && "is-hl")}>
              <span className="code-n" aria-hidden="true">{n}</span>
              <span className="code-t">
                <CodeLine text={line} />
              </span>
            </span>
          );
        })}
      </code>
    </pre>
  );
}
