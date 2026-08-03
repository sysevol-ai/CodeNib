"use client";

import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import Header from "@/components/Header";
import Markdown from "@/components/Markdown";
import AskBar from "@/components/AskBar";
import { AppLink } from "@/lib/router";
import {
  fetchCommits,
  fetchRepos,
  fetchWikiGraph,
  fetchWikiPage,
  fetchWikiTree,
  repoRelative,
  type CodemapResponse,
  type Citation,
  type CommitRef,
  type RepoInfo,
  type WikiPage,
  type WikiPageRef,
} from "@/lib/api";

const CodePanel = lazy(() => import("@/components/CodePanel"));
const Codemap = lazy(() => import("@/components/Codemap"));
const GraphView = lazy(() => import("@/components/GraphView"));

interface Heading {
  id: string;
  text: string;
  level: number;
}

// The wiki page now leads with an interactive subsystem graph, so the narrator's
// generated mermaid diagrams (and any heading left empty once removed) are
// redundant — strip them before rendering.
/** Drop diagrams unless they came from the validated structured plan.
 *
 *  The WikiBuilder path emits a mechanical fan-out of every module it found,
 *  which Mermaid's auto-layout renders as noise. Free-form model Markdown is
 *  also not trusted to supply executable Mermaid directives. A generated
 *  fact-plan diagram is different: its directed edges were validated against
 *  the page's evidence before the backend rendered the fence. */
function stripGeneratedDiagrams(
  md: string,
  keepValidatedPlan: boolean,
): string {
  if (keepValidatedPlan) return md;
  return md
    .replace(/\n#{1,6}[^\n]*\n+```mermaid[\s\S]*?```/g, "") // a heading + its diagram
    .replace(/```mermaid[\s\S]*?```/g, "") // any stray diagram
    .trimEnd();
}

// Link a repo-relative source path to the exact blob on GitHub at the indexed commit.
function ghFileUrl(
  repo: string | undefined,
  commit: string | undefined,
  file: string,
  start?: number | null,
  end?: number | null
): string | null {
  if (!repo) return null;
  const lines = start ? `#L${start}${end && end !== start ? `-L${end}` : ""}` : "";
  return `https://github.com/${repo}/blob/${commit || "HEAD"}/${file}${lines}`;
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

// Timed cold graph-build or warm patch section for the selected snapshot. This
// excludes LSP startup and transition overhead and does not imply equality with
// a fresh rebuild.
function commitEvidence(commits: CommitRef[], selected?: string): string | null {
  if (!commits.length) return null;
  const i = commits.findIndex((c) => c.sha === selected);
  const c = i >= 0 ? commits[i] : commits[0];
  if (!c) return null;

  if (c.method === "cold") {
    return c.build_seconds != null ? `cold build · ${c.build_seconds.toFixed(1)}s` : null;
  }

  const parts: string[] = [];
  if (c.build_seconds != null) parts.push(`warm patch · ${c.build_seconds.toFixed(2)}s`);
  if (c.changed_files != null) {
    parts.push(`${c.changed_files} file${c.changed_files === 1 ? "" : "s"} changed`);
  }
  // commits[] is newest-first, so this commit's predecessor is the next entry.
  const prev = i >= 0 ? commits[i + 1] : commits[1];
  if (prev && c.node_count != null && prev.node_count != null) {
    const d = c.node_count - prev.node_count;
    parts.push(d === 0 ? "±0 nodes" : `${d > 0 ? "+" : "−"}${Math.abs(d)} nodes`);
  }
  return parts.length ? parts.join(" · ") : null;
}

export default function WikiPageView({ repoId }: { repoId: string }) {

  const [repo, setRepo] = useState<RepoInfo | null>(null);
  const [pages, setPages] = useState<WikiPageRef[]>([]);
  const [activeId, setActiveId] = useState<string>("overview");
  const [page, setPage] = useState<WikiPage | null>(null);
  const [pageGraph, setPageGraph] = useState<CodemapResponse | null>(null);
  const [pageGraphOpen, setPageGraphOpen] = useState(false);
  const [pageGraphLoading, setPageGraphLoading] = useState(false);
  const [pageGraphError, setPageGraphError] = useState(false);
  // The graph explorer opens as a full-screen modal, optionally seeded on a
  // symbol when launched via "Focus here" from a wiki subsystem map.
  const [graphSeed, setGraphSeed] = useState<string | undefined>(undefined);
  const [graphOpen, setGraphOpen] = useState(false);
  const [sourceCitation, setSourceCitation] = useState<Citation | null>(null);
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
  const commitCost = commitEvidence(commits, selectedCommit);
  const contentRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    setCommits([]);
    setSelectedCommit(undefined);

    // Deep-link support: ?p=<pageId> opens that wiki page directly.
    const p = new URLSearchParams(window.location.search).get("p");
    if (p) setActiveId(p);
    fetchRepos()
      .then((rs) => {
        if (!cancelled) setRepo(rs.find((x) => x.id === repoId) ?? null);
      })
      .catch(() => {});
    // Optional feature: repos without a prebuilt window just keep the static
    // commit label, so a failure here is not surfaced as a page error.
    fetchCommits(repoId)
      .then((w) => {
        if (cancelled || !w.available) return;
        setCommits(w.commits);
        setSelectedCommit(w.selected ?? undefined);
      })
      .catch(() => {});
    setTocLoading(true);
    setError(null);
    fetchWikiTree(repoId)
      .then((t) => {
        if (!cancelled) setPages(t.pages);
      })
      .catch((e) => {
        if (!cancelled) setError(String(e));
      })
      .finally(() => {
        if (!cancelled) setTocLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [repoId]);

  useEffect(() => {
    let cancelled = false;
    setPage(null);
    setPageError(null);
    setPageGraph(null);
    setPageGraphOpen(false);
    setPageGraphLoading(false);
    setPageGraphError(false);
    setSourceCitation(null);
    fetchWikiPage(repoId, activeId)
      .then((p) => !cancelled && setPage(p))
      .catch((e) => !cancelled && setPageError(String(e)));
    return () => {
      cancelled = true;
    };
  }, [repoId, activeId]);

  useEffect(() => {
    if (!pageGraphOpen || pageGraph) return;
    let cancelled = false;
    setPageGraphLoading(true);
    setPageGraphError(false);
    fetchWikiGraph(repoId, activeId)
      .then((graph) => {
        if (!cancelled) setPageGraph(graph);
      })
      .catch(() => {
        if (!cancelled) setPageGraphError(true);
      })
      .finally(() => {
        if (!cancelled) setPageGraphLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [repoId, activeId, pageGraphOpen, pageGraph]);

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

  useEffect(() => {
    if (!sourceCitation) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSourceCitation(null);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [sourceCitation]);

  const hasGraph = !!repo?.capabilities?.codemap;
  const generationMode = page?.generation?.mode ?? "offline";
  // "Source checked" is the strict claim: every substantial block is cited
  // and every referenced source identifier resolves. Generated pages that only
  // clear the looser grounding floor stay visible, but retain the review badge.
  const sourceChecked =
    generationMode === "generated" && page?.grounding?.valid === true;
  const evidenceRoutes = [
    ...new Set(page?.evidence?.items.flatMap((item) => item.routes) ?? []),
  ];
  const openGraph = (seed?: string) => {
    setGraphSeed(seed);
    setGraphOpen(true);
  };

  return (
    <div className="wiki wiki-reading">
      <Header
        actions={
          repo?.capabilities?.chat ? (
            <AppLink
              className="icon-btn header-ask"
              href={`/${encodeURIComponent(repoId)}/ask`}
              aria-label={`Ask a question about ${repo.repo}`}
              title="Ask this repository"
            >
              <span aria-hidden>✦</span>
            </AppLink>
          ) : null
        }
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
            <button
              className={`codegraph-launch ${hasGraph ? "" : "unavailable"}`}
              onClick={() => openGraph()}
              title={
                hasGraph
                  ? "Open the interactive code dependency graph"
                  : "Dependency graph is not indexed for this repository"
              }
              aria-label={
                hasGraph
                  ? "Open dependency map"
                  : "Set up dependency map"
              }
            >
              <svg
                width="13"
                height="13"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                aria-hidden
              >
                <circle cx="5" cy="6" r="2" />
                <circle cx="19" cy="7" r="2" />
                <circle cx="12" cy="19" r="2" />
                <path d="m7 6.2 10 .6M6.5 8l4.4 9.2m6.5-8.3-4.3 8.4" />
              </svg>
              <span>Dependency Map</span>
            </button>
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
                Graph snapshot
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
              {commitCost && <div className="commit-evidence">{commitCost}</div>}
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
              {page && (
                <div
                  className={`page-provenance ${
                    sourceChecked ? "source-checked" : "needs-review"
                  }`}
                  role="status"
                >
                  <span className="provenance-state">
                    {generationMode === "offline"
                      ? "Index-derived page"
                      : sourceChecked
                        ? "Evidence-linked generation"
                        : page.generation?.fallback
                          ? "Index-derived fallback"
                          : "Generated, evidence review needed"}
                  </span>
                  {page.grounding && (
                    <span className="provenance-detail">
                      {page.grounding.evidence_count} source symbols
                      {page.grounding.relation_count > 0 &&
                        ` · ${page.grounding.relation_count} static relations`}
                      {/* State how much of the page carries its source rather
                          than only flagging a shortfall: the coverage number is
                          the claim this product makes. */}
                      {page.grounding.citation_coverage < 1 &&
                        ` · ${Math.round(page.grounding.citation_coverage * 100)}% of blocks sourced`}
                      {evidenceRoutes.length > 0 && ` · ${evidenceRoutes.join(" + ")}`}
                    </span>
                  )}
                  {page.generation?.model && (
                    <span className="provenance-model mono">{page.generation.model}</span>
                  )}
                </div>
              )}
              {hasGraph && (
                <details
                  className="subsystem-map"
                  open={pageGraphOpen}
                  onToggle={(event) =>
                    setPageGraphOpen(event.currentTarget.open)
                  }
                >
                  <summary className="subsystem-summary">
                    <span className="subsystem-title">Subsystem map</span>
                    <span className="subsystem-count">
                      {pageGraph?.available && pageGraph.nodes.length > 0
                        ? `${pageGraph.nodes.length} symbols`
                        : "Indexed graph"}
                    </span>
                    {repo?.commit_short && (
                      <span className="subsystem-index mono">Last indexed {repo.commit_short}</span>
                    )}
                  </summary>
                  {pageGraphOpen &&
                    (pageGraphLoading ? (
                      <div className="subsystem-loading">Loading subsystem map…</div>
                    ) : pageGraphError ||
                      !pageGraph?.available ||
                      pageGraph.nodes.length === 0 ? (
                      <div className="subsystem-loading">
                        No source-linked graph is available for this page.
                      </div>
                    ) : (
                      <Suspense
                        fallback={
                          <div className="subsystem-loading">Loading graph view…</div>
                        }
                      >
                        <GraphView
                          repoId={repoId}
                          data={pageGraph}
                          variant="wiki"
                          onFocus={(label) => openGraph(label)}
                          repoFullName={repo?.repo}
                          commit={repo?.base_commit}
                        />
                      </Suspense>
                    ))}
                </details>
              )}
              {page?.evidence && page.evidence.items.length > 0 ? (
                <details className="evidence-ledger">
                  <summary>
                    Source evidence ({page.evidence.items.length})
                  </summary>
                  <div className="evidence-list">
                    {page.evidence.items.map((item) => {
                      const url = ghFileUrl(
                        repo?.repo,
                        repo?.base_commit,
                        item.file,
                        item.start_line,
                        item.end_line
                      );
                      return (
                        <a
                          id={`evidence-${item.id}`}
                          key={item.id}
                          className="evidence-row"
                          href={url ?? undefined}
                          target={url ? "_blank" : undefined}
                          rel={url ? "noreferrer" : undefined}
                        >
                          <span className="evidence-id mono">{item.id}</span>
                          <span className="evidence-symbol mono">{item.symbol}</span>
                          <span className="evidence-location mono">
                            {item.file}
                            {item.start_line ? `:${item.start_line}` : ""}
                          </span>
                          {item.routes.length > 0 && (
                            <span className="evidence-route">{item.routes.join(" + ")}</span>
                          )}
                        </a>
                      );
                    })}
                    {page.evidence.relations.map((item) => (
                      <div
                        id={`evidence-${item.id}`}
                        key={item.id}
                        className="evidence-row relation"
                      >
                        <span className="evidence-id mono">{item.id}</span>
                        <span className="evidence-symbol mono">
                          {item.source} → {item.target}
                        </span>
                        <span className="evidence-location mono">
                          {item.anchors[0] ?? "static reference"}
                        </span>
                      </div>
                    ))}
                  </div>
                </details>
              ) : page && page.citations.length > 0 ? (
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
              ) : null}
              {page ? (
                <Markdown
                  citations={page.citations}
                  relations={page.evidence?.relations}
                  onCite={(index) =>
                    setSourceCitation(page.citations[index] ?? null)
                  }
                >
                  {stripGeneratedDiagrams(
                    page.markdown,
                    generationMode === "generated" &&
                      page.generation?.renderer === "fact_plan",
                  )}
                </Markdown>
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
          aria-label="Repository dependency map"
        >
          <div className="graph-modal" onClick={(e) => e.stopPropagation()}>
            <div className="graph-modal-head">
              <b>Dependency Map</b>
              <span aria-hidden>·</span>
              <span className="graph-modal-repo mono">
                {repo ? repo.repo : repoId}
              </span>
              <span className="graph-modal-hint muted small">
                Symbol references at the indexed commit · Esc to close
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
              <Suspense
                fallback={
                  <div className="codemap-loading">Loading dependency map…</div>
                }
              >
                <Codemap
                  repoId={repoId}
                  initialSymbol={graphSeed}
                  commit={selectedCommit}
                  coverage={repo?.graph_coverage}
                />
              </Suspense>
            </div>
          </div>
        </div>
      )}

      {sourceCitation && (
        <div
          className="source-modal-scrim"
          onClick={() => setSourceCitation(null)}
          role="dialog"
          aria-modal="true"
          aria-label="Source definition"
        >
          <div
            className="source-modal"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="source-modal-head">
              <div>
                <span className="source-modal-kicker">Indexed definition</span>
                <div className="source-modal-title mono">
                  {sourceCitation.node_name ||
                    repoRelative(sourceCitation.file)}
                </div>
                <div className="source-modal-location mono">
                  {repoRelative(sourceCitation.file)}
                  {sourceCitation.start_line != null
                    ? `:${sourceCitation.start_line}-${
                        sourceCitation.end_line ??
                        sourceCitation.start_line
                      }`
                    : ""}
                </div>
              </div>
              <button
                className="source-modal-close"
                onClick={() => setSourceCitation(null)}
                aria-label="Close source"
              >
                ×
              </button>
            </div>
            <div className="source-modal-body">
              <Suspense
                fallback={
                  <div className="codemap-loading">Loading source…</div>
                }
              >
                <CodePanel
                  repoId={repoId}
                  citations={[sourceCitation]}
                  repo={repo?.repo}
                  commit={repo?.base_commit}
                />
              </Suspense>
            </div>
          </div>
        </div>
      )}

      {repo?.capabilities?.chat && (
        <AskBar repoId={repoId} repo={repo.repo} collapsible />
      )}
    </div>
  );
}
