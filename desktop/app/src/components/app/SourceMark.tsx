import { useState } from "react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { message } from "@/lib/backend";
import type { Source } from "@/lib/model";
import { repoPath, useStore } from "@/lib/store";
import { cn } from "@/lib/utils";
import { CodeLines } from "./CodeLines";

/** The file and lines a claim rests on. Opening it shows that code in place,
 *  read from the repository the Wiki was built from. */
export function SourceMark({
  repo,
  file,
  start,
  end,
  label,
  className,
}: {
  repo: string;
  file: string;
  start: number;
  end?: number;
  label?: string;
  className?: string;
}) {
  const { data, call } = useStore();
  const [source, setSource] = useState<Source | null>(null);
  const [error, setError] = useState("");
  const last = end && end >= start ? end : start;
  const shownEnd = Math.min(last, start + 60);
  const text = label ?? `${file.split("/").pop()}:${start}`;

  const load = (open: boolean) => {
    if (!open || source) return;
    const path = repoPath(
      data[repo],
      `/source?file=${encodeURIComponent(file)}&start=${start}&end=${shownEnd}`,
    );
    if (!path) return;
    call<Source>(repo, "GET", path)
      .then(setSource)
      .catch((e) => setError(message(e)));
  };

  return (
    <Popover onOpenChange={load}>
      <PopoverTrigger asChild>
        <button type="button" className={cn("src-mark", className)} aria-label={`Show the code at ${file}:${start}`}>
          {text}
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" sideOffset={8} className="src-pop">
        <div className="src-pop-head">
          <span className="mono">{file}</span>
          <span className="src-pop-lines">
            {start === shownEnd ? `line ${start}` : `lines ${start}–${shownEnd}`}
          </span>
        </div>
        {source ? (
          <CodeLines
            text={source.content}
            start={source.start_line}
            from={Math.max(start, source.start_line)}
            to={Math.min(shownEnd, source.end_line)}
            className="src-pop-code"
          />
        ) : (
          <p className="src-pop-wait">{error || "Reading the code…"}</p>
        )}
      </PopoverContent>
    </Popover>
  );
}
