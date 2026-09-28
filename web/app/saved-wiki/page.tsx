// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useRef, useState } from "react";
import Header from "@/components/Header";
import Markdown from "@/components/Markdown";
import WikiGenerationForm from "@/components/WikiGenerationForm";
import WikiRunActivity from "@/components/WikiRunActivity";
import {
  shouldWithholdWikiPage,
  type Citation,
  type WikiPage,
  type WikiPageRef,
} from "@/lib/api";
import { AppLink } from "@/lib/router";
import { withBasePath } from "@/lib/runtime";
import {
  allWikiPages,
  loadSavedWiki,
  loadSavedWikiPage,
  recentWikis,
  stopWiki,
  wikiStages,
  type SavedWiki,
} from "@/lib/visitorWiki";

function chapterRows(
  pages: WikiPageRef[],
  depth = 0,
): Array<WikiPageRef & { depth: number }> {
  return pages.flatMap((page) => [
    { ...page, depth },
    ...chapterRows(page.children, depth + 1),
  ]);
}

export default function SavedWikiPage({ id }: { id: string }) {
  const [wiki, setWiki] = useState<SavedWiki | null>(null);
  const [error, setError] = useState("");
  const [active, setActive] = useState(
    () => new URLSearchParams(window.location.search).get("p") || "overview",
  );
  const [page, setPage] = useState<WikiPage | null>(null);
  const [pageError, setPageError] = useState("");
  const [citation, setCitation] = useState<Citation | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [copyMessage, setCopyMessage] = useState("");
  const [stopping, setStopping] = useState(false);
  const [showResume, setShowResume] = useState(false);
  const [checkedAt, setCheckedAt] = useState(0);
  const [statusConnected, setStatusConnected] = useState(false);
  const [, tick] = useState(0);
  const sourcePanel = useRef<HTMLDivElement>(null);
  const owner = recentWikis().find((item) => item.id === id);

  useEffect(() => {
    const robots = document.createElement("meta");
    robots.name = "robots";
    robots.content = "noindex, nofollow";
    document.head.append(robots);
    return () => robots.remove();
  }, []);

  useEffect(() => {
    let cancelled = false;
    let settled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const value = await loadSavedWiki(id);
        settled = value.status === "complete" || value.status === "partial";
        if (!cancelled) {
          setWiki(value);
          setError("");
          setCheckedAt(Date.now() / 1000);
          setStatusConnected(true);
        }
      } catch (reason) {
        if (!cancelled) {
          setStatusConnected(false);
          setError(
            reason instanceof Error
              ? reason.message
              : "Could not load the saved Wiki.",
          );
        }
      } finally {
        if (!cancelled && !settled) timer = setTimeout(poll, 2000);
      }
    };
    void poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [id, refresh]);
  const ready = wiki?.page_states[active] === "ready";
  useEffect(() => {
    let cancelled = false;
    setPage(null);
    setPageError("");
    setCitation(null);
    if (ready)
      loadSavedWikiPage(id, active)
        .then((value) => {
          if (!cancelled) setPage(value);
        })
        .catch((reason) => {
          if (!cancelled)
            setPageError(
              reason instanceof Error
                ? reason.message
                : "Could not load this chapter.",
            );
        });
    return () => {
      cancelled = true;
    };
  }, [id, active, ready, refresh]);
  const running = wiki?.status === "running" || wiki?.status === "queued";
  useEffect(() => {
    if (!running) {
      setStopping(false);
      return;
    }
    const timer = setInterval(() => tick((value) => value + 1), 1000);
    return () => clearInterval(timer);
  }, [running]);
  const pages = allWikiPages(wiki?.pages || []);
  const completed = pages.filter(
    (item) => wiki?.page_states[item.id] === "ready",
  ).length;
  const selected = pages.find((item) => item.id === active);
  function pick(pageId: string) {
    setActive(pageId);
    history.replaceState(
      null,
      "",
      `${withBasePath(`/wiki/${id}`)}?p=${encodeURIComponent(pageId)}`,
    );
  }
  function cite(index: number) {
    setCitation(page?.citations[index] || null);
    setTimeout(
      () =>
        sourcePanel.current?.scrollIntoView({
          behavior: "smooth",
          block: "nearest",
        }),
      0,
    );
  }
  return (
    <>
      <Header />
      <div className="saved-wiki">
        <div className="saved-wiki-heading">
          <div>
            <AppLink href="/">← All repositories</AppLink>
            <h1>
              {wiki?.repository || owner?.repository || "Saved Repo Wiki"}
            </h1>
            {wiki?.commit && (
              <a
                className="small mono muted"
                href={`https://github.com/${wiki.repository}/tree/${wiki.commit}`}
                target="_blank"
                rel="noopener noreferrer"
              >
                {wiki.commit.slice(0, 8)} ↗
              </a>
            )}
          </div>
          <button
            type="button"
            className="btn-outline"
            onClick={async () => {
              try {
                await navigator.clipboard.writeText(
                  new URL(withBasePath(`/wiki/${id}`), location.origin).href,
                );
                setCopyMessage("Link copied");
              } catch {
                setCopyMessage(
                  "Copy the address from your browser to share this Wiki.",
                );
              }
            }}
          >
            Share Wiki ↗
          </button>
        </div>
        {copyMessage && (
          <p role="status" className="small">
            {copyMessage}
          </p>
        )}
        {error && (
          <p role="alert" className="trial-error">
            {error}
          </p>
        )}
        {!wiki && !error && <p role="status">Opening your saved Wiki…</p>}
        {wiki && (
          <section
            className={`wiki-progress ${running ? "is-running" : wiki.status === "partial" ? "is-paused" : ""}`}
            aria-label="Wiki generation progress"
          >
            <div className="wiki-progress-heading">
              <div>
                <strong role="status">
                  {wiki.status === "complete"
                    ? "Your Wiki is ready"
                    : wiki.status === "partial"
                      ? completed === 0
                        ? "Generation stopped"
                        : "Generation paused"
                      : wikiStages[wiki.stage] || "Generating Wiki"}
                </strong>
              </div>
              {completed > 0 ? (
                <a className="small" href="#saved-wiki-chapters">
                  Read {completed} / {pages.length} ready chapters ↓
                </a>
              ) : (
                <span className="small">
                  0{pages.length ? ` / ${pages.length}` : ""} chapters ready
                </span>
              )}
            </div>
            {wiki.status === "partial" && (
              <div className="wiki-stopped-message" role="alert">
                <strong>{wiki.message || "This run stopped before finishing."}</strong>
                <p>
                  {completed === 0
                    ? "No chapters were saved. Generation is no longer running."
                    : "Ready chapters are saved. Generation is no longer running."}
                  {wiki.calls === 0 && !wiki.unreported_call_cost &&
                    " No model calls were made."}
                </p>
                <p>
                  {owner
                    ? "Resolve the issue above, then retry using your OpenRouter key."
                    : "The owner can retry from the browser that started this Wiki."}
                </p>
                <button className="btn-ghost" onClick={() => setRefresh((value) => value + 1)}>
                  Refresh status
                </button>
              </div>
            )}
            {pages.length > 0 && (
              <progress
                value={completed}
                max={pages.length}
                aria-label="Saved Wiki chapters"
              />
            )}
            <div className="wiki-progress-foot">
              <span>
                {wiki.model === "deepseek/deepseek-v4.1-flash" ? "DeepSeek V4.1 Flash" : "Claude Sonnet 4.6"} · ${wiki.reported_cost_usd.toFixed(4)}{" "}
                reported · {wiki.calls} model requests sent
              </span>
              {owner && running && !stopping && (
                <button
                  className="btn-ghost"
                  onClick={async () => {
                    try {
                      await stopWiki(owner);
                      setStopping(true);
                    } catch {
                      setError("Could not stop generation. Please try again.");
                    }
                  }}
                >
                  Stop generation
                </button>
              )}
              {stopping && (
                <span role="status">Stopping after the current request…</span>
              )}
            </div>
            {wiki.message && wiki.status !== "partial" && <p role="status">{wiki.message}</p>}
            <details>
              <summary>Generation activity{wiki.skipped_files?.length ? ` · ${wiki.skipped_files.length} file(s) skipped` : ""}</summary>
            {!!wiki.skipped_files?.length && (
              <details className="wiki-skipped-files">
                <summary>
                  Skipped {wiki.skipped_files.length} {wiki.skipped_files.length === 1 ? "file" : "files"} larger than 4 MiB
                </summary>
                <p>These files are excluded from this Wiki. The remaining source is used for generation.</p>
                <ul>
                  {wiki.skipped_files.map((file) => (
                    <li key={file.path}>
                      <code>{file.path}</code> · {(file.size_bytes / 1024 / 1024).toFixed(1)} MiB
                    </li>
                  ))}
                </ul>
              </details>
            )}
            {wiki.unreported_call_cost && !running && (
              <p className="small">
                The reported total excludes an in-flight or unreported request.
                Check OpenRouter usage before resuming.
              </p>
            )}
            <p className="small muted">
              Saved as you go. Return with this link; it stays off the homepage.
            </p>

              <ol className="wiki-activity">
                {wiki.history.slice(-12).map((event, index) => (
                  <li key={`${event.at}-${index}`}>
                    <time>
                      {new Date(event.at * 1000).toLocaleTimeString()}
                    </time>{" "}
                    {wikiStages[event.stage] || event.stage}
                    {event.page &&
                      ` · ${pages.find((item) => item.id === event.page)?.title || event.page}`}
                  </li>
                ))}
              </ol>
            </details>
          </section>
        )}
        {owner &&
          (wiki?.status === "partial" || wiki?.stalled || (!wiki && error)) && (
            <div className="wiki-resume">
              {!showResume ? (
                <button
                  className="btn-primary"
                  onClick={() => setShowResume(true)}
                >
                  {completed === 0 ? "Retry generation" : "Continue unfinished chapters"}
                </button>
              ) : (
                <WikiGenerationForm
                  attempt={owner}
                  resume
                  onStarted={() => {
                    setShowResume(false);
                    setRefresh((value) => value + 1);
                  }}
                />
              )}
            </div>
          )}
        {wiki && (
          <div className="saved-wiki-layout" id="saved-wiki-chapters">
            <nav className="saved-wiki-nav" aria-label="Wiki chapters">
              <h2>Chapters</h2>
              {pages.length === 0 ? (
                <p className="small muted">
                  {running
                    ? "The chapter list appears as soon as planning finishes."
                    : "No chapters were generated."}
                </p>
              ) : (
                chapterRows(wiki.pages).map((item) => {
                  const state = wiki.page_states[item.id] || "pending";
                  return (
                    <button
                      key={item.id}
                      className={`saved-wiki-chapter ${active === item.id ? "active" : ""}`}
                      style={{ paddingLeft: 12 + Math.min(item.depth, 3) * 16 }}
                      onClick={() => pick(item.id)}
                      aria-current={active === item.id ? "page" : undefined}
                    >
                      <span>{item.title}</span>
                      <span className={`chapter-state ${state}`}>
                        {state === "ready"
                          ? "Ready"
                          : state === "running"
                            ? "Generating"
                            : state === "needs_review"
                              ? "Needs review"
                              : "Pending"}
                      </span>
                    </button>
                  );
                })
              )}
            </nav>
            <main className="saved-wiki-content">
            {running && (
              <WikiRunActivity
                wiki={wiki}
                compact={!!page}
                now={Date.now() / 1000}
                checkedAt={checkedAt}
                connected={statusConnected}
              />
            )}

              {pageError && (
                <p role="alert" className="trial-error">
                  {pageError}{" "}
                  <button onClick={() => setRefresh((value) => value + 1)}>
                    Retry loading
                  </button>
                </p>
              )}
              {page && !shouldWithholdWikiPage(page) ? (
                <>
                  <Markdown
                    citations={page.citations}
                    relations={page.evidence?.relations}
                    onCite={cite}
                    onPageLink={pick}
                    allowRemoteImages={false}
                    pageBasePath={`/wiki/${id}`}
                  >
                    {page.markdown}
                  </Markdown>
                  <div className="page-provenance source-checked">
                    <span>Evidence-linked Wiki chapter</span>
                    <span>{page.citations.length} source references</span>
                  </div>
                  <details className="saved-wiki-sources">
                    <summary>Sources for this chapter</summary>
                    {page.citations.map((item, index) => (
                      <button key={index} onClick={() => cite(index)}>
                        {item.file}:{item.start_line}–{item.end_line}
                      </button>
                    ))}
                  </details>
                </>
              ) : (
                !pageError && (
                  <div className="wiki-chapter-pending" role="status" aria-busy={running || ready}>
                    {(running || ready) && <div className={`wiki-document-skeleton ${running && !wiki.stalled && statusConnected ? "is-animated" : ""}`} aria-hidden="true"><span /><span /><span /><span /></div>}
                    <h2>{selected?.title || (running ? "Your Wiki is taking shape" : "No chapters saved yet")}</h2>
                    <p>
                      {ready
                        ? "Loading this chapter…"
                        : wiki.page_states[active] === "needs_review"
                          ? "This chapter did not pass source checks. Its draft is withheld; the owner can resume to retry it."
                          : running
                            ? completed === 0
                              ? "The first chapter appears after writing and source checks finish. Keep this page open to watch it arrive."
                              : "This chapter is still being prepared. You can read any ready chapter in the sidebar while generation continues."
                            : "This chapter has not been completed. The owner can continue generation without rebuilding ready chapters."}
                    </p>
                  </div>
                )
              )}
              {citation && (
                <div className="saved-wiki-source" ref={sourcePanel}>
                  <div>
                    <strong>
                      {citation.file}:{citation.start_line}–{citation.end_line}
                    </strong>
                    <button
                      className="btn-ghost"
                      aria-label="Close source"
                      onClick={() => setCitation(null)}
                    >
                      ×
                    </button>
                  </div>
                  <a
                    href={`https://github.com/${wiki.repository}/blob/${wiki.commit}/${(citation.file || "").split("/").map(encodeURIComponent).join("/")}${citation.start_line ? `#L${citation.start_line}${citation.end_line ? `-L${citation.end_line}` : ""}` : ""}`}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    Open pinned source on GitHub ↗
                  </a>
                  {citation.content && (
                    <pre>
                      <code>{citation.content}</code>
                    </pre>
                  )}
                </div>
              )}
            </main>
          </div>
        )}
      </div>
    </>
  );
}
