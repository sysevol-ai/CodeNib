import { ChevronDown, ChevronUp, Copy } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { message } from "@/lib/backend";
import type { Source } from "@/lib/model";
import { repoPath, useStore } from "@/lib/store";
import { cn } from "@/lib/utils";
import { CodeLines } from "./CodeLines";

/** Lines of context shown above and below the cited lines. */
const CONTEXT = 6;
/** Lines added by one "more above" or "more below". */
const MORE = 30;
/** The most cited lines shown at first; the rest are one "more below" away. */
const FIRST_CITED = 120;

/** The file and lines a claim rests on. Opening it shows that code in place,
 *  read from the repository the Wiki was built from: the cited lines marked,
 *  a little context around them, scrolled to the citation, and more of the
 *  file above or below on request. */
export function SourceMark({
  repo,
  file,
  start,
  end,
  label,
  className,
  title,
}: {
  repo: string;
  file: string;
  start: number;
  end?: number;
  label?: string;
  className?: string;
  /** Hover text; defaults to the file and line. */
  title?: string;
}) {
  const { data, call } = useStore();
  const last = end && end >= start ? end : start;
  const [range, setRange] = useState<{ from: number; to: number }>({
    from: Math.max(1, start - CONTEXT),
    to: Math.min(last, start + FIRST_CITED) + CONTEXT,
  });
  const [source, setSource] = useState<Source | null>(null);
  const [error, setError] = useState("");
  const [open, setOpen] = useState(false);
  const body = useRef<HTMLDivElement>(null);
  const scrolled = useRef(false);
  const text = label ?? `${file.split("/").pop()}:${start}`;

  useEffect(() => {
    if (!open) return;
    const path = repoPath(data[repo], `/source?file=${encodeURIComponent(file)}&start=${range.from}&end=${range.to}`);
    if (!path) return;
    let live = true;
    call<Source>(repo, "GET", path)
      .then((s) => {
        if (!live) return;
        setSource(s);
        setError("");
      })
      .catch((e) => live && setError(message(e)));
    return () => {
      live = false;
    };
    // Load when opened and whenever the range grows.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, range.from, range.to, file, repo]);

  // Open on the citation, with two lines of context above it in view.
  useEffect(() => {
    if (!source || scrolled.current || !body.current) return;
    const row = body.current.querySelector<HTMLElement>(".code-row.is-hl");
    if (row) body.current.scrollTop = Math.max(0, row.offsetTop - 2 * row.offsetHeight);
    scrolled.current = true;
  }, [source]);

  const cited = useMemo(() => {
    const lines: number[] = [];
    for (let n = start; n <= last && lines.length < 2000; n++) lines.push(n);
    return lines;
  }, [start, last]);

  const atTop = (source?.start_line ?? range.from) <= 1;
  const atEnd = source ? source.end_line < range.to : false;
  const shownFrom = source ? Math.max(range.from, source.start_line) : range.from;
  const shownTo = source ? Math.min(range.to, source.end_line) : range.to;

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) scrolled.current = false;
      }}
    >
      <PopoverTrigger asChild>
        <button
          type="button"
          className={cn("src-mark", className)}
          aria-label={`Show the code at ${file}:${start}`}
          title={title ?? `${file}:${start}`}
        >
          {text}
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" sideOffset={8} collisionPadding={16} className="src-pop">
        <div className="src-pop-head">
          <span className="mono src-pop-file" title={file}>
            {file}
          </span>
          <span className="src-pop-lines">{start === last ? `line ${start}` : `lines ${start}–${last}`}</span>
          <button
            type="button"
            className="src-pop-icon"
            aria-label="Copy file and line"
            title="Copy file and line"
            onClick={() =>
              void navigator.clipboard
                .writeText(`${file}:${start}${last > start ? `-${last}` : ""}`)
                .then(() => toast("Copied"))
            }
          >
            <Copy />
          </button>
        </div>
        {source && !atTop && (
          <button
            type="button"
            className="src-pop-more"
            onClick={() => setRange((r) => ({ ...r, from: Math.max(1, r.from - MORE) }))}
          >
            <ChevronUp />
            {MORE} more lines above
          </button>
        )}
        <div className={cn("src-pop-body", cited.length > 15 && "is-long-cite")} ref={body}>
          {source ? (
            <CodeLines
              text={source.content}
              start={source.start_line}
              from={shownFrom}
              to={shownTo}
              highlight={cited}
              className="src-pop-code"
              file={file}
            />
          ) : (
            <p className="src-pop-wait">{error || "Reading the code…"}</p>
          )}
        </div>
        {source && !atEnd && (
          <button type="button" className="src-pop-more" onClick={() => setRange((r) => ({ ...r, to: r.to + MORE }))}>
            <ChevronDown />
            {MORE} more lines below
          </button>
        )}
      </PopoverContent>
    </Popover>
  );
}
