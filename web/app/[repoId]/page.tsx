"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import Header from "@/components/Header";
import Markdown from "@/components/Markdown";
import AskBar from "@/components/AskBar";
import Codemap from "@/components/Codemap";
import GraphView from "@/components/GraphView";
import {
  fetchCommits,
  fetchRepos,
  fetchWikiGraph,
  fetchWikiPage,
  fetchWikiTree,
  repoRelative,
  type CodemapResponse,
  type CommitRef,
  type RepoInfo,
  type WikiPage,
  type WikiPageRef,
} from "@/lib/api";

interface Heading {
  id: string;
  text: string;
  level: number;
}

// The wiki page now leads with an interactive subsystem graph, so the narrator's
// generated mermaid diagrams (and any heading left empty once removed) are
// redundant — strip them before rendering.
function stripGeneratedDiagrams(md: string): string {
  return md
    .replace(/\n#{1,6}[^\n]*\n+```mermaid[\s\S]*?```/g, "") // a heading + its diagram
    .replace(/```mermaid[\s\S]*?```/g, "") // any stray diagram
    .trimEnd();
}

// Link a repo-relative source path to the exact blob on GitHub at the indexed commit.
function ghFileUrl(repo: string | undefined, commit: string | undefined, file: string): string | null {
  if (!repo) return null;
  return `https://github.com/${repo}/blob/${commit || "HEAD"}/${file}`;
}

function TocTree({
  pages,
  activeId,
  onPick,
}: {
  pages: WikiPageRef[];
  activeId: string;
  onPick: (id: string) => void;
}) {
  return (
    <ul className="toc-tree">
      {pages.map((p) => (
        <li key={p.id}>
          <button
            className={`toc-link ${p.id === activeId ? "active" : ""}`}
            aria-current={p.id === activeId ? "page" : undefined}
            title={p.title}
            onClick={() => onPick(p.id)}
          >
            {p.title}
          </button>
          {p.children.length > 0 && (
            <TocTree pages={p.children} activeId={activeId} onPick={onPick} />
          )}
        </li>
      ))}
    </ul>
  );
}

export default function WikiPageView() {
  const params = useParams<{ repoId: string }>();
  const repoId = decodeURIComponent(params.repoId);

  const [repo, setRepo] = useState<RepoInfo | null>(null);
  const [pages, setPages] = useState<WikiPageRef[]>([]);
  const [activeId, setActiveId] = useState<string>("overview");
  const [page, setPage] = useState<WikiPage | null>(null);
  const [pageGraph, setPageGraph] = useState<CodemapResponse | null>(null);
  // The graph explorer opens as a full-screen modal, optionally seeded on a
  // symbol when launched via "Focus here" from a wiki subsystem map.
  const [graphSeed, setGraphSeed] = useState<string | undefined>(undefined);
  const [graphOpen, setGraphOpen] = useState(false);
  const [headings, setHeadings] = useState<Heading[]>([]);
  const [activeHeading, setActiveHeading] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [pageError, setPageError] = useState<string | null>(null);
  const [tocLoading, setTocLoading] = useState(true);
  const [tocOpen, setTocOpen] = useState(false);
  // Commit window: empty when this repo has no prebuilt snapshots, in which
  // case the rail keeps its static "Last indexed" label.
  const [commits, setCommits] = useState<CommitRef[]>([]);
  const [selectedCommit, setSelectedCommit] = useState<string | undefined>(undefined);
  const contentRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Deep-link support: ?p=<pageId> opens that wiki page directly.
    const p = new URLSearchParams(window.location.search).get("p");
    if (p) setActiveId(p);
    fetchRepos()
      .then((rs) => {
        setRepo(rs.find((x) => x.id === repoId) ?? null);
      })
      .catch(() => {});
    // Optional feature: repos without a prebuilt window just keep the static
    // commit label, so a failure here is not surfaced as a page error.
    fetchCommits(repoId)
      .then((w) => {
        if (!w.available) return;
        setCommits(w.commits);
        setSelectedCommit(w.selected ?? undefined);
      })
      .catch(() => {});
    setTocLoading(true);
    setError(null);
    fetchWikiTree(repoId)
      .then((t) => setPages(t.pages))
      .catch((e) => setError(String(e)))
      .finally(() => setTocLoading(false));
  }, [repoId]);

  useEffect(() => {
    let cancelled = false;
    setPage(null);
    setPageError(null);
    setPageGraph(null);
    fetchWikiPage(repoId, activeId)
      .then((p) => !cancelled && setPage(p))
      .catch((e) => !cancelled && setPageError(String(e)));
    // The page's symbols as a view over the graph (rendered atop the narrative).
    fetchWikiGraph(repoId, activeId)
      .then((g) => !cancelled && setPageGraph(g))
      .catch(() => !cancelled && setPageGraph(null));
    return () => {
      cancelled = true;
    };
  }, [repoId, activeId]);

  // Build "On this page" from the actually-rendered heading ids (matches rehype-slug).
  const rescanHeadings = useCallback(() => {
    const root = contentRef.current;
    if (!root) return setHeadings([]);
    const hs = Array.from(root.querySelectorAll<HTMLElement>("h1, h2, h3"))
      .filter((h) => h.id)
      .map((h) => ({ id: h.id, text: h.textContent || "", level: Number(h.tagName[1]) }));
    setHeadings(hs);
  }, []);

  useEffect(() => {
    const t = setTimeout(rescanHeadings, 80);
    return () => clearTimeout(t);
  }, [page, rescanHeadings]);

  // Scroll-spy: highlight the heading currently in view in the right rail.
  useEffect(() => {
    if (headings.length === 0) return;
    const els = headings
      .map((h) => document.getElementById(h.id))
      .filter((e): e is HTMLElement => !!e);
    if (els.length === 0) return;
    const obs = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((e) => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (visible[0]) setActiveHeading(visible[0].target.id);
      },
      { rootMargin: "-64px 0px -70% 0px", threshold: 0 }
    );
    els.forEach((e) => obs.observe(e));
    setActiveHeading((cur) => cur || headings[0].id);
    return () => obs.disconnect();
  }, [headings]);

  function pick(id: string) {
    setActiveId(id);
    setTocOpen(false);
    const url = `${window.location.pathname}?p=${encodeURIComponent(id)}`;
    window.history.replaceState(null, "", url);
    window.scrollTo({ top: 0 });
  }

  // Esc closes the graph explorer modal.
  useEffect(() => {
    if (!graphOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setGraphOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [graphOpen]);

  const hasGraph = !!repo?.capabilities?.codemap;
  const openGraph = (seed?: string) => {
    setGraphSeed(seed);
    setGraphOpen(true);
  };

  return (
    <div className="wiki">
      <Header
        center={
          <nav className="breadcrumb" aria-label="Breadcrumb">
            <button
              className="toc-toggle"
              aria-label="Toggle section list"
              onClick={() => setTocOpen((o) => !o)}
            >
              ☰
            </button>
            <span className="crumb-sep">/</span>
            <span className="crumb-repo mono">{repo ? repo.repo : repoId}</span>
            {hasGraph && (
              <button
                className="codegraph-launch"
                onClick={() => openGraph()}
                title="Open the interactive code dependency graph"
              >
                ⌗ CodeGraph
              </button>
            )}
            {page && <span className="crumb-sep">/</span>}
            {page && <span className="crumb-page">{page.title}</span>}
          </nav>
        }
      />

      <div className="wiki-grid">
        {tocOpen && <div className="toc-scrim" onClick={() => setTocOpen(false)} aria-hidden />}
        <aside className={`wiki-toc ${tocOpen ? "open" : ""}`} data-rail="left">
          <div className="rail-title">{repo ? repo.repo : repoId}</div>
          {commits.length > 0 ? (
            <div className="rail-sub commit-picker">
              <label className="commit-picker-label" htmlFor="commit-select">
                Viewing commit
              </label>
              <select
                id="commit-select"
                className="commit-select mono"
                value={selectedCommit ?? ""}
                onChange={(e) => setSelectedCommit(e.target.value || undefined)}
                title="Show the symbol graph as of this commit"
              >
                {commits.map((c) => (
                  <option key={c.sha} value={c.sha}>
                    {c.short} · {c.date} · {c.subject.slice(0, 40)}
                  </option>
                ))}
              </select>
            </div>
          ) : (
            repo?.commit_short && (
              <div className="rail-sub mono">Last indexed {repo.commit_short}</div>
            )
          )}
          {error ? (
            <div className="muted">Failed to load wiki.</div>
          ) : tocLoading ? (
            <div className="toc-skeleton" aria-hidden>
              {Array.from({ length: 7 }).map((_, i) => (
                <div key={i} className="toc-skeleton-row" />
              ))}
            </div>
          ) : pages.length === 0 ? (
            <div className="muted small">No pages in this wiki yet.</div>
          ) : (
            <TocTree pages={pages} activeId={activeId} onPick={pick} />
          )}
        </aside>

        <main className="wiki-main">
          <div className="wiki-content" ref={contentRef}>
              {pageGraph && pageGraph.available && pageGraph.nodes.length > 0 && (
                <details className="subsystem-map" open>
                  <summary className="subsystem-summary">
                    <span className="subsystem-title">Subsystem map</span>
                    <span className="subsystem-count">{pageGraph.nodes.length} symbols</span>
                    {repo?.commit_short && (
                      <span className="subsystem-index mono">Last indexed {repo.commit_short}</span>
                    )}
                  </summary>
                  <GraphView
                    repoId={repoId}
                    data={pageGraph}
                    variant="wiki"
                    onFocus={(label) => openGraph(label)}
                    repoFullName={repo?.repo}
                    commit={repo?.base_commit}
                  />
                </details>
              )}
              {page && page.citations.length > 0 && (
                <details className="relevant-files-wiki">
                  {(() => {
                    const wikiFiles = [
                      ...new Set(
                        page.citations.map((c) => repoRelative(c.file)).filter(Boolean)
                      ),
                    ];
                    return (
                      <>
                        <summary>Relevant source files ({wikiFiles.length})</summary>
                        <div className="relevant-files-list">
                          {wikiFiles.map((f) => {
                            const url = ghFileUrl(repo?.repo, repo?.base_commit, f);
                            return url ? (
                              <a
                                key={f}
                                className="relevant-file mono"
                                href={url}
                                target="_blank"
                                rel="noreferrer"
                                title={`Open ${f} on GitHub`}
                              >
                                {f} ↗
                              </a>
                            ) : (
                              <span key={f} className="relevant-file mono">
                                {f}
                              </span>
                            );
                          })}
                        </div>
                      </>
                    );
                  })()}
                </details>
              )}
              {page ? (
                <Markdown>{stripGeneratedDiagrams(page.markdown)}</Markdown>
              ) : pageError ? (
                <p className="muted">Couldn't load this page. It may not exist — pick a section from the sidebar.</p>
              ) : (
                <p className="muted">Loading…</p>
              )}
          </div>
        </main>

        <aside className="wiki-onthispage" data-rail="right">
          <div className="rail-title">On this page</div>
          {headings.length > 0 ? (
            <ul className="onthispage-list">
              {headings.map((h) => (
                <li key={h.id} className={`lvl-${h.level}`}>
                  <a href={`#${h.id}`} className={h.id === activeHeading ? "active" : ""}>
                    {h.text}
                  </a>
                </li>
              ))}
            </ul>
          ) : (
            <div className="muted small">—</div>
          )}
          {repo && (
            <button
              className="refresh-wiki"
              title="Re-fetch this wiki page"
              onClick={() => {
                fetchWikiTree(repoId).then((t) => setPages(t.pages)).catch(() => {});
                fetchWikiPage(repoId, activeId).then(setPage).catch(() => {});
              }}
            >
              Refresh this wiki
            </button>
          )}
        </aside>
      </div>

      {graphOpen && (
        <div
          className="graph-modal-scrim"
          onClick={() => setGraphOpen(false)}
          role="dialog"
          aria-modal="true"
          aria-label="Code dependency graph"
        >
          <div className="graph-modal" onClick={(e) => e.stopPropagation()}>
            <div className="graph-modal-head">
              <span className="mono">
                <b>CodeGraph</b> · {repo ? repo.repo : repoId}
              </span>
              <span className="graph-modal-hint muted small">
                top-down dependency graph — every edge is a real LSP/SCIP reference. Esc to close.
              </span>
              <button
                className="graph-modal-close"
                onClick={() => setGraphOpen(false)}
                aria-label="Close graph"
              >
                ×
              </button>
            </div>
            <div className="graph-modal-body">
              <Codemap repoId={repoId} initialSymbol={graphSeed} commit={selectedCommit} />
            </div>
          </div>
        </div>
      )}

      <AskBar repoId={repoId} repo={repo ? repo.repo : repoId} />
    </div>
  );
}
