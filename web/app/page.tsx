"use client";

import { useEffect, useMemo, useState } from "react";
import Header from "@/components/Header";
import { fetchRepos, type RepoInfo } from "@/lib/api";
import { AppLink, navigate } from "@/lib/router";

function repoDescription(r: RepoInfo): string {
  if (r.description) return r.description;
  return `${r.language || "Source"} repository indexed at ${r.commit_short}`;
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
          {r.language || "code"}
        </span>
        {r.file_count > 0 && (
          <span className="repo-metric" title="indexed files">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
              <path d="M13 2v7h7" />
            </svg>
            {r.file_count.toLocaleString()} files
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
const sleep = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms));

export default function Landing() {
  const [repos, setRepos] = useState<RepoInfo[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");

  const loadRepos = () => {
    setError(null);
    setLoading(true);
    let active = true;

    const run = async () => {
      let lastError: unknown = null;
      for (let attempt = 0; attempt < repoRetryDelays.length; attempt += 1) {
        const delay = repoRetryDelays[attempt];
        if (delay > 0) {
          if (active) setError("Connecting to backend; retrying repository list...");
          await sleep(delay);
        }
        if (!active) return;
        try {
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
      setError(lastError instanceof Error ? lastError.message : String(lastError));
    };

    run()
      .finally(() => {
        if (!active) return;
        setLoading(false);
      });
    return () => {
      active = false;
    };
  };

  useEffect(() => {
    return loadRepos();
  }, []);

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return repos;
    return repos.filter(
      (r) =>
        r.repo.toLowerCase().includes(needle) ||
        r.id.toLowerCase().includes(needle) ||
        (r.language || "").toLowerCase().includes(needle)
    );
  }, [repos, q]);

  return (
    <div className="landing">
      <Header />
      <section className="hero">
        <h1>Which repo would you like to understand?</h1>
        <div className="search-box">
          <span className="search-icon" aria-hidden>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
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
            placeholder="Search repositories (press Enter to open)"
            aria-label="Search repositories"
          />
        </div>
      </section>

      <div className="repo-grid">
        <button
          type="button"
          className="repo-card add-repo"
          aria-label="Index your own repository"
          onClick={() => navigate("/add-repo")}
        >
          <span className="add-plus">+</span>
          <span className="add-label">Add repo</span>
          <span className="repo-card-go" aria-hidden>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M5 12h14M13 6l6 6-6 6" />
            </svg>
          </span>
        </button>

        {error && (
          <div className="empty">
            <p>
              Backend unavailable — start it with <code>codenib-web</code> after building an index.
            </p>
            <p className="small muted">Request failed: {error}</p>
            <button type="button" className="codegraph-fit" onClick={loadRepos}>
              Retry
            </button>
          </div>
        )}
        {!error && loading && repos.length === 0 && <div className="empty">Loading repositories…</div>}
        {!error && !loading && repos.length === 0 && <div className="empty">No repositories found.</div>}
        {filtered.map((r) => (
          <RepoCard key={r.id} r={r} />
        ))}
      </div>
    </div>
  );
}
