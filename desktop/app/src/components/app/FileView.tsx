import { BookOpen } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { message } from "@/lib/backend";
import type { Source } from "@/lib/model";
import { repoPath, useStore } from "@/lib/store";
import { CodeLines } from "./CodeLines";

const PAGE = 1000;

/** One file of the repository, as it is on this Mac, with the Wiki pages
 *  that cite it: the code-to-Wiki side of the mapping. */
export function FileView({ repo, file }: { repo: string; file: string }) {
  const { data, call, go, loadFiles } = useStore();
  const d = data[repo];
  const [source, setSource] = useState<Source | null>(null);
  const [error, setError] = useState("");
  const [upTo, setUpTo] = useState(PAGE);

  useEffect(() => {
    if (!d?.files) void loadFiles(repo);
  }, [d?.files, loadFiles, repo]);

  useEffect(() => {
    const path = repoPath(d, `/source?file=${encodeURIComponent(file)}&start=1&end=${upTo}&definition=false`);
    if (!path) return;
    let live = true;
    call<Source>(repo, "GET", path)
      .then((s) => live && (setSource(s), setError("")))
      .catch((e) => live && setError(message(e)));
    return () => {
      live = false;
    };
    // Reload when the file or the amount shown changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repo, file, upTo, d?.info?.id]);

  const citedBy = d?.files?.find((f) => f.path === file)?.cited_by ?? [];
  const marked = useMemo(() => {
    const lines = new Set<number>();
    for (const page of citedBy)
      for (const [start, end] of page.lines) for (let n = start; n <= Math.min(end, start + 2000); n++) lines.add(n);
    return [...lines];
  }, [citedBy]);

  return (
    <div className="view file-view">
      <h1 className="view-title file-title">
        <span className="mono">{file.split("/").pop()}</span>{" "}
        <span className="two-tone mono">{file.split("/").slice(0, -1).join("/")}</span>
      </h1>
      {citedBy.length > 0 ? (
        <div className="file-cited">
          <span className="file-cited-label">Cited by</span>
          {citedBy.map((c) => (
            <button key={c.page} type="button" className="file-cited-page" onClick={() => go({ kind: "page", repo, page: c.page })}>
              <BookOpen />
              {c.title}
              <span className="rail-count">{c.lines.length}</span>
            </button>
          ))}
        </div>
      ) : (
        d?.files && <p className="view-sub">No Wiki page cites this file yet.</p>
      )}
      <div className="code-card file-code">
        {source ? (
          <CodeLines
            text={source.content}
            start={source.start_line}
            from={source.start_line}
            to={source.end_line}
            highlight={marked}
            file={file}
          />
        ) : (
          <p className="src-pop-wait">{error || "Reading the file…"}</p>
        )}
      </div>
      {source && source.end_line >= upTo && (
        <Button variant="outline" size="sm" onClick={() => setUpTo((n) => n + PAGE)}>
          Show the next {PAGE} lines
        </Button>
      )}
    </div>
  );
}
