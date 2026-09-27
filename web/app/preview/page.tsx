// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import Header from "@/components/Header";
import RepositoryEntry from "@/components/RepositoryEntry";
import RepositoryExplanation from "@/components/RepositoryExplanation";
import HighlightedCode from "@/components/HighlightedCode";
import { AGENT_SETUP_URL } from "@/components/AgentSetup";
import { AppLink } from "@/lib/router";
import {
  loadRepositoryPreview,
  loadPreviewFolder,
  parseGitHubRepository,
  previewReadme,
  readPreviewFile,
  sourceURL,
  type PreviewEntry,
  type PreviewFile,
  type RepositoryPreview,
} from "@/lib/githubPreview";

export default function PreviewPage({
  owner,
  name,
}: {
  owner: string;
  name: string;
}) {
  const [snapshot, setSnapshot] = useState<RepositoryPreview | null>(null);
  const [metadata, setMetadata] = useState<{
    slug: string;
    description: string;
  } | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);
  const [folder, setFolder] = useState("");
  const [folderBusy, setFolderBusy] = useState(false);
  const [folderError, setFolderError] = useState("");
  const [file, setFile] = useState<PreviewFile | null>(null);
  const [selection, setSelection] = useState<PreviewEntry | null>(null);
  const [fileError, setFileError] = useState("");
  const [fileBusy, setFileBusy] = useState(false);
  const [raw, setRaw] = useState(false);
  const [filter, setFilter] = useState("");
  const rootRequest = useRef<AbortController | null>(null);
  const fileRequest = useRef<AbortController | null>(null);
  const folderRequest = useRef<AbortController | null>(null);

  async function openFile(repo: RepositoryPreview, entry: PreviewEntry) {
    fileRequest.current?.abort();
    const abort = new AbortController();
    fileRequest.current = abort;
    const timeout = setTimeout(() => abort.abort(), 30000);
    setSelection(entry);
    setFile(null);
    setFileError("");
    setFileBusy(true);
    setRaw(false);
    try {
      const value = await readPreviewFile(repo, entry, abort.signal);
      if (!abort.signal.aborted) setFile(value);
    } catch (reason) {
      if (fileRequest.current === abort)
        setFileError(
          abort.signal.aborted
            ? "Reading this file timed out. Open its pinned source on GitHub or try again."
            : reason instanceof Error
              ? reason.message
              : "Could not read this file.",
        );
    } finally {
      clearTimeout(timeout);
      if (fileRequest.current === abort) {
        fileRequest.current = null;
        setFileBusy(false);
      }
    }
  }

  useEffect(() => {
    const abort = new AbortController();
    rootRequest.current = abort;
    let mounted = true;
    const timeout = setTimeout(() => abort.abort(), 30000);
    setLoading(true);
    setError("");
    setMetadata(null);
    setSnapshot(null);
    setFolder("");
    setFile(null);
    setSelection(null);
    setFilter("");
    setFolderError("");
    const repo = parseGitHubRepository(`${owner}/${name}`);
    (async () => {
      try {
        if (!repo)
          throw new Error("Enter a valid public GitHub repository URL.");
        const loaded = await loadRepositoryPreview(
          repo,
          abort.signal,
          (value) => {
            if (mounted && !abort.signal.aborted) setMetadata(value);
          },
        );
        if (!mounted || abort.signal.aborted) return;
        setSnapshot(loaded);
        const readme = previewReadme(loaded.entries);
        if (readme) void openFile(loaded, readme);
      } catch (reason) {
        if (mounted)
          setError(
            abort.signal.aborted
              ? "Repository loading stopped. Retry when you’re ready."
              : reason instanceof Error
                ? reason.message
                : "Could not open this repository.",
          );
      } finally {
        clearTimeout(timeout);
        if (mounted) setLoading(false);
      }
    })();
    return () => {
      mounted = false;
      clearTimeout(timeout);
      abort.abort();
      fileRequest.current?.abort();
      fileRequest.current = null;
      folderRequest.current?.abort();
      folderRequest.current = null;
    };
  }, [owner, name, attempt]);

  async function openFolder(path: string) {
    folderRequest.current?.abort();
    setFolder(path);
    setFilter("");
    setFolderError("");
    setFolderBusy(false);
    if (!snapshot?.partial || !path) return;
    const entry = snapshot.entries.find(
      (item) => item.path === path && item.type === "tree",
    );
    if (!entry) return;
    const abort = new AbortController();
    folderRequest.current = abort;
    const timeout = setTimeout(() => abort.abort(), 30000);
    setFolderBusy(true);
    try {
      const children = await loadPreviewFolder(snapshot, entry, abort.signal);
      if (abort.signal.aborted) return;
      setSnapshot(
        (previous) =>
          previous && {
            ...previous,
            entries: [
              ...new Map(
                [...previous.entries, ...children].map((item) => [
                  item.path,
                  item,
                ]),
              ).values(),
            ],
          },
      );
    } catch (reason) {
      if (folderRequest.current === abort)
        setFolderError(
          reason instanceof Error
            ? reason.message
            : "Could not load this folder.",
        );
    } finally {
      clearTimeout(timeout);
      if (folderRequest.current === abort) {
        folderRequest.current = null;
        setFolderBusy(false);
      }
    }
  }

  const visible = useMemo(() => {
    if (!snapshot) return [];
    const needle = filter.trim().toLowerCase();
    const prefix = folder ? `${folder}/` : "";
    return snapshot.entries
      .filter((entry) =>
        needle
          ? entry.path.toLowerCase().includes(needle)
          : entry.path.startsWith(prefix) &&
            !entry.path.slice(prefix.length).includes("/"),
      )
      .sort(
        (a, b) =>
          Number(b.type === "tree") - Number(a.type === "tree") ||
          a.path.localeCompare(b.path),
      );
  }, [snapshot, folder, filter]);
  const markdown =
    file && /(?:^|\/)readme(?:\.(?:md|markdown))?$/i.test(file.path) && !raw;
  const excerpt =
    file?.content.split("\n").slice(0, 800).join("\n").slice(0, 40000) ?? "";
  function readmeLink(href: string | undefined): string | undefined {
    if (!href || !file) return undefined;
    try {
      const resolved = new URL(href, file.url);
      return ["https:", "http:"].includes(resolved.protocol)
        ? resolved.href
        : undefined;
    } catch {
      return undefined;
    }
  }

  return (
    <div className="repository-preview">
      <Header />
      <main>
        <div className="preview-breadcrumb">
          <AppLink href="/">← Repositories</AppLink>
          <span>Quick preview</span>
        </div>
        <header className="preview-heading">
          <h1>{metadata?.slug ?? `${owner}/${name}`}</h1>
          {metadata?.description && <p>{metadata.description}</p>}
          {snapshot && (
            <p className="small muted">
              {snapshot.language && `${snapshot.language} · `}
              {snapshot.branch} ·{" "}
              <a
                href={`https://github.com/${snapshot.repository.slug}/tree/${snapshot.commit}`}
                target="_blank"
                rel="noopener noreferrer"
              >
                {snapshot.commit.slice(0, 8)}
              </a>{" "}
              · Default branch snapshot
            </p>
          )}
        </header>
        {loading && (
          <div className="preview-loading" role="status">
            <p>
              {metadata
                ? "Opening the file tree…"
                : "Opening public repository…"}
            </p>
            <button type="button" onClick={() => rootRequest.current?.abort()}>
              Cancel
            </button>
          </div>
        )}
        {error && (
          <section className="preview-failure">
            <p className="trial-error" role="alert">
              {error}
            </p>
            <button
              type="button"
              onClick={() => setAttempt((value) => value + 1)}
            >
              Retry repository
            </button>
            <p>
              <AppLink href="/">Browse ready Wikis →</AppLink>
            </p>
            <RepositoryEntry />
          </section>
        )}
        {snapshot && (
          <>
            <section
              className="preview-folders"
              aria-label="Repository folders"
            >
              {snapshot.entries
                .filter(
                  (entry) => entry.type === "tree" && !entry.path.includes("/"),
                )
                .slice(0, 8)
                .map((entry) => {
                  const count = snapshot.entries.filter(
                    (file) =>
                      file.type === "blob" &&
                      file.path.startsWith(`${entry.path}/`),
                  ).length;
                  return (
                    <button
                      type="button"
                      key={entry.path}
                      aria-pressed={folder === entry.path}
                      onClick={() => void openFolder(entry.path)}
                    >
                      <strong>{entry.path}/</strong>
                      <span>
                        {snapshot.partial
                          ? "Open folder"
                          : `${count} ${count === 1 ? "file" : "files"}`}{" "}
                        →
                      </span>
                    </button>
                  );
                })}
            </section>
            <RepositoryExplanation snapshot={snapshot} />
          <section className="preview-browser" aria-label="Repository source">
              <aside className="preview-files">
                <h2>Explore the code</h2>
                <label className="sr-only" htmlFor="preview-filter">
                  Find a file or folder
                </label>
                <input
                  id="preview-filter"
                  type="search"
                  value={filter}
                  maxLength={256}
                  onChange={(event) => setFilter(event.target.value)}
                  placeholder="Find a file or folder…"
                />
                <nav aria-label="Folder path" className="folder-breadcrumb">
                  <button type="button" onClick={() => void openFolder("")}>
                    root
                  </button>
                  {folder
                    .split("/")
                    .filter(Boolean)
                    .map((part, i) => (
                      <button
                        type="button"
                        key={i}
                        onClick={() =>
                          void openFolder(
                            folder
                              .split("/")
                              .slice(0, i + 1)
                              .join("/"),
                          )
                        }
                      >
                        / {part}
                      </button>
                    ))}
                </nav>
                {folderBusy && <p role="status">Opening folder…</p>}
                {folderError && (
                  <p role="alert" className="trial-error">
                    {folderError}
                  </p>
                )}
                <ul className="preview-file-list">
                  {visible.slice(0, 200).map((entry) => (
                    <li key={entry.path}>
                      <button
                        type="button"
                        aria-pressed={selection?.path === entry.path}
                        title={entry.path}
                        onClick={() =>
                          entry.type === "tree"
                            ? void openFolder(entry.path)
                            : void openFile(snapshot, entry)
                        }
                      >
                        <span className="file-kind" aria-hidden>
                          {entry.type === "tree" ? "▸" : "·"}
                        </span>
                        <span>
                          {filter.trim()
                            ? entry.path
                            : entry.path.split("/").pop()}
                          {entry.type === "tree" ? "/" : ""}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
                {!visible.length && !folderBusy && (
                  <p className="small muted">
                    {filter
                      ? "No matching paths in the loaded tree."
                      : "No files in this folder."}
                  </p>
                )}
                {visible.length > 200 && (
                  <p className="small muted">
                    Showing 200 of {visible.length} entries. Narrow the file
                    search.
                  </p>
                )}
                {snapshot.partial && (
                  <p className="small muted">
                    Large repository: showing a partial tree. Open a folder to
                    load its entries.
                  </p>
                )}
              </aside>
              <article className="preview-source">
                <div className="preview-source-heading">
                  <h2>{selection?.path ?? "Choose a file"}</h2>
                  {selection && (
                    <a
                      href={sourceURL(snapshot, selection.path)}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      View on GitHub ↗
                    </a>
                  )}
                </div>
                {fileBusy && <p role="status">Reading source…</p>}
                {fileError && (
                  <p className="trial-error" role="alert">
                    {fileError}
                  </p>
                )}
                {file && (
                  <>
                    {/readme/i.test(file.path) && (
                      <button
                        className="preview-source-toggle"
                        type="button"
                        onClick={() => setRaw((value) => !value)}
                      >
                        {raw ? "Read document" : "View source lines"}
                      </button>
                    )}
                    {markdown ? (
                      <div className="preview-readme">
                        <ReactMarkdown
                          skipHtml
                          components={{
                            img: ({ alt }) => (
                              <span className="small muted">
                                {alt ? `[Image: ${alt}]` : "[Image]"}
                              </span>
                            ),
                            a: ({ href, children }) => (
                              <a
                                href={readmeLink(href)}
                                target="_blank"
                                rel="noopener noreferrer"
                              >
                                {children}
                              </a>
                            ),
                          }}
                        >
                          {excerpt}
                        </ReactMarkdown>
                      </div>
                    ) : (
                      <div
                        className="preview-code"
                        tabIndex={0}
                        aria-label={`Source from ${file.path}`}
                      >
                        <HighlightedCode file={file.path} code={excerpt} />
                      </div>
                    )}
                    {excerpt.length < file.content.length && (
                      <p className="small muted">
                        Showing the beginning of this file.{" "}
                        <a
                          href={file.url}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          Read the complete source on GitHub
                        </a>
                        .
                      </p>
                    )}
                  </>
                )}
                {!selection && (
                  <p className="muted">
                    Open a folder or file to start exploring. Files are linked
                    to this commit on GitHub.
                  </p>
                )}
              </article>
            </section>
              <p className="preview-local">
              Want source search inside your coding agent?{" "}
              <a
                href={AGENT_SETUP_URL}
                target="_blank"
                rel="noopener noreferrer"
              >
                Set up CodeNib locally →
              </a>
            </p>
          </>
        )}
      </main>
    </div>
  );
}
