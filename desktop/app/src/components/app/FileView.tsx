import { BookOpen, ChevronRight } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { message } from "@/lib/backend";
import type { Source } from "@/lib/model";
import { repoPath, useStore } from "@/lib/store";
import { cn } from "@/lib/utils";
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

interface Folder {
  name: string;
  path: string;
  folders: Map<string, Folder>;
  files: { name: string; path: string; cites: number }[];
}

function buildTree(files: { path: string; cited_by: unknown[] }[]): Folder {
  const root: Folder = { name: "", path: "", folders: new Map(), files: [] };
  for (const file of files) {
    const parts = file.path.split("/");
    let at = root;
    for (const part of parts.slice(0, -1)) {
      const next = at.folders.get(part) ?? {
        name: part,
        path: at.path ? `${at.path}/${part}` : part,
        folders: new Map(),
        files: [],
      };
      at.folders.set(part, next);
      at = next;
    }
    at.files.push({ name: parts[parts.length - 1], path: file.path, cites: file.cited_by.length });
  }
  return root;
}

/** The folders that hold a file, so opening it shows where it is. */
function parentsOf(file?: string): string[] {
  if (!file) return [];
  const parts = file.split("/").slice(0, -1);
  return parts.map((_, i) => parts.slice(0, i + 1).join("/"));
}

/** The Files mode: the repository's folders beside the chosen file. A file
 *  the Wiki cites carries how many pages cite it. */
export function FilesView({ repo, page, file }: { repo: string; page: string; file?: string }) {
  const { data, loadFiles, go } = useStore();
  const files = data[repo]?.files;
  const [open, setOpen] = useState<Set<string>>(() => new Set(parentsOf(file)));
  const tree = useMemo(() => (files ? buildTree(files) : null), [files]);
  useEffect(() => {
    if (!files) void loadFiles(repo);
  }, [files, loadFiles, repo]);
  const toggle = (path: string) =>
    setOpen((all) => {
      const next = new Set(all);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  const render = (folder: Folder, depth: number): React.ReactNode[] => [
    ...[...folder.folders.values()]
      .sort((a, b) => a.name.localeCompare(b.name))
      .flatMap((f) => [
        <li key={`d:${f.path}`}>
          <button type="button" className="ft-row ft-folder" style={{ paddingLeft: 8 + depth * 12 }} onClick={() => toggle(f.path)}>
            <ChevronRight className="sb-chev" data-open={open.has(f.path) ? "yes" : "no"} />
            <span>{f.name}</span>
          </button>
        </li>,
        ...(open.has(f.path) ? render(f, depth + 1) : []),
      ]),
    ...[...folder.files]
      .sort((a, b) => a.name.localeCompare(b.name))
      .map((f) => (
        <li key={`f:${f.path}`}>
          <button
            type="button"
            className={cn("ft-row", file === f.path && "is-on")}
            style={{ paddingLeft: 22 + depth * 12 }}
            title={f.path}
            aria-current={file === f.path ? "page" : undefined}
            onClick={() => go({ kind: "files", repo, page, file: f.path })}
          >
            <span className="sb-file">{f.name}</span>
            {f.cites > 0 && <span className="sb-file-cites">{f.cites}</span>}
          </button>
        </li>
      )),
  ];
  return (
    <div className="files-split">
      <ScrollArea className="files-tree">
        <ul aria-label="Files">{tree ? render(tree, 0) : <li className="sb-note">Listing files…</li>}</ul>
      </ScrollArea>
      <ScrollArea className="main-scroll" key={file ?? ""}>
        {file ? (
          <FileView repo={repo} file={file} />
        ) : (
          <div className="view">
            <h1 className="view-title">Files.</h1>
            <p className="view-sub">
              Pick a file to read it. A number beside a file counts the Wiki pages that cite it; the file opens with those
              pages linked and the cited lines marked.
            </p>
          </div>
        )}
      </ScrollArea>
    </div>
  );
}
