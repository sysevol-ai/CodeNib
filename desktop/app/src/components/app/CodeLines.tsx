import { useMemo } from "react";
import { highlightLines } from "@/lib/highlight";
import { cn } from "@/lib/utils";

/** One line of code, coloured for its language. Used where lines arrive one
 *  at a time, such as a diff; whole snippets go through CodeLines. */
export function CodeLine({ text, lang }: { text: string; lang?: string }) {
  const html = useMemo(() => highlightLines(text, lang)[0] ?? "", [text, lang]);
  return <span className="hljs" dangerouslySetInnerHTML={{ __html: html }} />;
}

export function CodeLines({
  text,
  start,
  from,
  to,
  highlight = [],
  className,
  file,
}: {
  text: string;
  start: number;
  from?: number;
  to?: number;
  highlight?: number[];
  className?: string;
  /** The file the code is from, for its language. */
  file?: string;
}) {
  const lines = useMemo(() => highlightLines(text, file), [text, file]);
  const first = from ?? start;
  const last = to ?? start + lines.length - 1;
  return (
    <pre className={cn("code-lines hljs", className)}>
      <code>
        {lines.slice(first - start, last - start + 1).map((line, i) => {
          const n = first + i;
          return (
            <span key={n} className={cn("code-row", highlight.includes(n) && "is-hl")}>
              <span className="code-n" aria-hidden="true">
                {n}
              </span>
              <span className="code-t" dangerouslySetInnerHTML={{ __html: line }} />
            </span>
          );
        })}
      </code>
    </pre>
  );
}
