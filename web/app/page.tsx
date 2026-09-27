"use client";

import { useEffect, useMemo, useState, type ReactNode } from "react";
import Header from "@/components/Header";
import { AGENT_SETUP_URL } from "@/components/AgentSetup";
import { fetchRepos, type RepoInfo } from "@/lib/api";
import { groupByLanguage, primaryLanguage } from "@/lib/landing";
import RepositoryEntry from "@/components/RepositoryEntry";
import { AppLink, navigate } from "@/lib/router";
import { isStaticRuntime } from "@/lib/runtime";

function repoDescription(r: RepoInfo): ReactNode {
  // `summary` is chosen server-side to be a statement of purpose; the raw
  // README blurb is often a chat invitation or a pager note, so it is not a
  // fallback here.
  const text = r.summary || `${primaryLanguage(r)} repository indexed at ${r.commit_short}.`;
  return text.split(/(`[^`]+`)/).map((part, index) =>
    part.startsWith("`") && part.endsWith("`") ? (
      <code key={index}>{part.slice(1, -1)}</code>
    ) : (
      <span key={index}>{part}</span>
    ),
  );
}

// Cold graph-build time divided by mean warm patch time. The measurement
// excludes LSP startup and transition overhead, and is not end-to-end re-index
// latency or a fresh-rebuild equality claim.
function incrementalNote(r: RepoInfo): string | null {
  const s = r.incremental;
  if (!s || s.commit_count < 1) return null;
  const commits = `${s.commit_count} commit${s.commit_count === 1 ? "" : "s"}`;
  // No speedup is derivable (single-commit window, no cold anchor, zero
  // denominator) — say only what we can stand behind.
  if (s.speedup == null) return commits;
  return `${commits} · ${s.speedup}× warm-patch speedup`;
}

function RepoCard({ r }: { r: RepoInfo }) {
  const incremental = incrementalNote(r);
  return (
    <AppLink className="repo-card" href={`/${r.id}`} aria-label={`Open ${r.repo} wiki`}>
      <div className="repo-card-title">{r.repo}</div>
      <div className="repo-card-desc">{repoDescription(r)}</div>
      <div className="repo-card-footer">
        <span className={`lang lang-${(r.language || "").toLowerCase().split("/")[0]}`}>
          {primaryLanguage(r)}
        </span>
        {r.file_count > 0 && (
          <span className="repo-metric" title="indexed files">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
              <path d="M13 2v7h7" />
            </svg>
            {r.file_count.toLocaleString()} {r.file_count === 1 ? "file" : "files"}
          </span>
        )}
        {incremental ? (
          <span className="repo-metric repo-incremental" title="Cold graph-build time divided by mean warm patch time; excludes LSP startup and transition overhead">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M13 2 3 14h9l-1 8 10-12h-9z" />
            </svg>
            {incremental}
          </span>
        ) : (
          <span className="mono">{r.commit_short}</span>
        )}
      </div>
      <span className="repo-card-go" aria-hidden>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M5 12h14M13 6l6 6-6 6" />
        </svg>
      </span>
    </AppLink>
  );
}

const repoRetryDelays = [0, 1000, 2000, 4000, 8000];
const sleep = (ms: number) =>
  new Promise((resolve) => window.setTimeout(resolve, ms));

export default function Landing() {
  const staticRuntime = isStaticRuntime();
  const [repos, setRepos] = useState<RepoInfo[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");

  const [repoAttempt, setRepoAttempt] = useState(0);

  useEffect(() => {
    setError(null);
    setLoading(true);
    let active = true;

    const run = async () => {
      let lastError: unknown = null;
      const delays = staticRuntime ? [0] : repoRetryDelays;
      for (let attempt = 0; attempt < delays.length; attempt += 1) {
        const delay = delays[attempt];
        if (delay > 0) {
          if (active)
            setError("Connecting to backend; retrying repository list...");
          await sleep(delay);
        }
        if (!active) return;
        try {
          // Keep the shared catalog cache introduced with the cold-load fix.
          const rs = await fetchRepos();
          if (!active) return;
          setRepos(rs);
          setError(null);
          return;
        } catch (e) {
          lastError = e;
        }
      }
      if (!active) return;
      setError(
        lastError instanceof Error ? lastError.message : String(lastError),
      );
    };

    run().finally(() => {
      if (!active) return;
      setLoading(false);
    });
    return () => {
      active = false;
    };
  }, [staticRuntime, repoAttempt]);

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return repos;
    return repos.filter(
      (r) =>
        r.repo.toLowerCase().includes(needle) ||
        r.id.toLowerCase().includes(needle) ||
        (r.language || "").toLowerCase().includes(needle) ||
        (r.summary || "").toLowerCase().includes(needle),
    );
  }, [repos, q]);

  return (
    <div className="landing">
      <Header />
      <section className="hero">
        <h1>Understand a repo. Start with its URL.</h1>
        <p className="hero-sub">
          Open a ready Wiki or explore any public repository’s files and README.
          Get an AI explanation with links to the code when you need it.
        </p>
        <RepositoryEntry repos={repos} />
      </section>
      <section className="landing-catalog" aria-label="Ready repository Wikis">
        <div className="landing-catalog-heading">
          <div>
            <h2>Go deeper with a ready Wiki</h2>
            <p className="small muted">
              Source-linked explanations and recorded code relationships.
            </p>
          </div>
          <div className="search-box">
            <span className="search-icon" aria-hidden>
              <svg
                width="16"
                height="16"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
              >
                <circle cx="11" cy="11" r="7" />
                <path d="M21 21l-4.3-4.3" />
              </svg>
            </span>
            <input
              type="search"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && filtered.length > 0) {
                  navigate(`/${filtered[0].id}`);
                }
              }}
              placeholder="Filter ready Wikis…"
              aria-label="Filter ready Wikis"
            />
          </div>
        </div>
      </section>

      {error && (
        <div className="repo-grid">
          <div className="empty">
            <p>
              Ready Wikis are temporarily unavailable. You can still paste a
              public GitHub URL above.
            </p>
            <button type="button" className="codegraph-fit" onClick={() => setRepoAttempt(value => value + 1)}>
              Retry
            </button>
          </div>
        </div>
      )}
      {!error && loading && repos.length === 0 && (
        <div className="repo-grid">
          <div className="empty">Loading repositories…</div>
        </div>
      )}
      {!error && !loading && repos.length === 0 && (
        <div className="repo-grid">
          <div className="empty">No repositories found.</div>
        </div>
      )}
      {q.trim() ? (
        <div className="repo-grid">
          {filtered.length === 0 && (
            <p className="empty">
              No matching repositories. Try a project name or language.
            </p>
          )}
          {filtered.map((r) => (
            <RepoCard key={r.id} r={r} />
          ))}
        </div>
      ) : (
        groupByLanguage(repos).map((group) => (
          <section
            className="repo-group"
            key={group.language}
            aria-label={`${group.language} repositories`}
          >
            <h2 className="repo-group-title">
              {group.language} <span>{group.repos.length}</span>
            </h2>
            <div className="repo-grid">
              {group.repos.map((r) => (
                <RepoCard key={r.id} r={r} />
              ))}
            </div>
          </section>
        ))
      )}
      <section className="landing-agent">
        <h2>Bring source context to your coding agent</h2>
        <p>
          <strong>71.4% code-block Recall@5</strong> in our 100-issue grep + Jev
          experiment, up from 58.6% before reranking.{" "}
          <a
            href="https://codenib.ai/blogs/jev-model-grep-reranking/"
            target="_blank"
            rel="noreferrer"
          >
            Read the experiment
          </a>
        </p>
        <a href={AGENT_SETUP_URL} target="_blank" rel="noreferrer">
          Use CodeNib with your agent →
        </a>
      </section>
      <footer className="preview-footer">
        <a href="https://codenib.ai/" target="_blank" rel="noreferrer">
          Generated by CodeNib
        </a>
        {staticRuntime && (
          <span>
            Precomputed Wiki · Source and model provenance in the export
          </span>
        )}
      </footer>
    </div>
  );
}
