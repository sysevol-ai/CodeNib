"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import Header from "@/components/Header";
import Markdown from "@/components/Markdown";
import AskBar from "@/components/AskBar";
import Codemap from "@/components/Codemap";
import CitationItem from "@/components/CitationItem";
import {
  fetchRepos,
  fetchWikiPage,
  fetchWikiTree,
  repoRelative,
  type RepoInfo,
  type WikiPage,
  type WikiPageRef,
} from "@/lib/api";

type Mode = "wiki" | "codemap";
interface Heading {
  id: string;
  text: string;
  level: number;
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
  const [mode, setMode] = useState<Mode>("wiki");
  const [headings, setHeadings] = useState<Heading[]>([]);
  const [activeHeading, setActiveHeading] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [pageError, setPageError] = useState<string | null>(null);
  const [tocOpen, setTocOpen] = useState(false);
  const contentRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Deep-link support: ?p=<pageId> selects a page directly.
    const p = new URLSearchParams(window.location.search).get("p");
    if (p) setActiveId(p);
    fetchRepos()
      .then((rs) => setRepo(rs.find((r) => r.id === repoId) ?? null))
      .catch(() => {});
    fetchWikiTree(repoId)
      .then((t) => setPages(t.pages))
      .catch((e) => setError(String(e)));
  }, [repoId]);

  useEffect(() => {
    let cancelled = false;
    setPage(null);
    setPageError(null);
    fetchWikiPage(repoId, activeId)
      .then((p) => !cancelled && setPage(p))
      .catch((e) => !cancelled && setPageError(String(e)));
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
    if (mode !== "wiki") return;
    const t = setTimeout(rescanHeadings, 80);
    return () => clearTimeout(t);
  }, [page, mode, rescanHeadings]);

  // Scroll-spy: highlight the heading currently in view in the right rail.
  useEffect(() => {
    if (mode !== "wiki" || headings.length === 0) return;
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
  }, [headings, mode]);

  function pick(id: string) {
    setActiveId(id);
    setMode("wiki");
    setTocOpen(false);
    const url = `${window.location.pathname}?p=${encodeURIComponent(id)}`;
    window.history.replaceState(null, "", url);
    window.scrollTo({ top: 0 });
  }

  const modes: Mode[] = repo?.capabilities?.codemap ? ["wiki", "codemap"] : ["wiki"];

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
            <span className="mono">{repo ? repo.repo : repoId}</span>
            {page && <span className="crumb-sep">/</span>}
            {page && <span className="crumb-page">{page.title}</span>}
          </nav>
        }
      />

      <div className="wiki-grid">
        {tocOpen && <div className="toc-scrim" onClick={() => setTocOpen(false)} aria-hidden />}
        <aside className={`wiki-toc ${tocOpen ? "open" : ""}`} data-rail="left">
          <div className="rail-title">{repo ? repo.repo : repoId}</div>
          {error && <div className="muted">Failed to load wiki.</div>}
          <TocTree pages={pages} activeId={activeId} onPick={pick} />
        </aside>

        <main className="wiki-main">
          <div className="wiki-modeswitch" role="tablist">
            {modes.map((m) => (
              <button
                key={m}
                role="tab"
                aria-selected={mode === m}
                className={`mode-tab ${mode === m ? "active" : ""}`}
                onClick={() => setMode(m)}
              >
                {m === "wiki" ? "Wiki" : "Graph"}
              </button>
            ))}
          </div>

          {mode === "wiki" && (
            <div className="wiki-content" ref={contentRef}>
              <div className="wiki-meta mono">
                {repo ? `Last indexed ${repo.commit_short}` : ""}
              </div>
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
                          {wikiFiles.map((f) => (
                            <span key={f} className="relevant-file mono">
                              {f}
                            </span>
                          ))}
                        </div>
                      </>
                    );
                  })()}
                </details>
              )}
              {page ? (
                <Markdown>{page.markdown}</Markdown>
              ) : pageError ? (
                <p className="muted">Couldn't load this page. It may not exist — pick a section from the sidebar.</p>
              ) : (
                <p className="muted">Loading…</p>
              )}
              {page && page.citations.length > 0 && (
                <details className="citations" open>
                  <summary>{page.citations.length} code references</summary>
                  {page.citations.map((c, i) => (
                    <CitationItem repoId={repoId} c={c} key={i} />
                  ))}
                </details>
              )}
            </div>
          )}

          {mode === "codemap" && <Codemap repoId={repoId} />}
        </main>

        <aside className="wiki-onthispage" data-rail="right">
          <div className="rail-title">On this page</div>
          {mode === "wiki" && headings.length > 0 ? (
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
            <div className="muted small">
              {mode === "wiki" ? "—" : `${mode} view`}
            </div>
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

      <AskBar repoId={repoId} repo={repo ? repo.repo : repoId} />
    </div>
  );
}
