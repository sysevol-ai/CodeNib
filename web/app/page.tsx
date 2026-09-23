"use client";

import { useEffect, useMemo, useState, type ReactNode } from "react";
import Header from "@/components/Header";
import { fetchRepos, fetchWikiAreaMap, repoRelative, type RepoInfo } from "@/lib/api";
import { groupByLanguage, primaryLanguage } from "@/lib/landing";
import { splitSymbolLabel } from "@/lib/symbols";
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

interface Proof {
  repoId: string;
  repo: string;
  commit: string;
  from: string;
  to: string;
  file: string;
  line: number | null;
}

/** One real recorded call, shown as the reason to trust the pages. */
function HeroProof({ proof }: { proof: Proof }) {
  return (
    <AppLink className="hero-proof" href={`/${proof.repoId}`}>
      <span className="hero-proof-label">A recorded call</span>
      <span className="hero-proof-body">
        <code>{proof.from}</code> calls <code>{proof.to}</code>
        {proof.file && (
          <span className="hero-proof-site mono">
            {proof.file}
            {proof.line != null ? `:${proof.line}` : ""}
          </span>
        )}
      </span>
      <span className="hero-proof-repo">
        in {proof.repo} at <span className="mono">{proof.commit}</span> →
      </span>
    </AppLink>
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
  const staticRuntime = isStaticRuntime();
  const [repos, setRepos] = useState<RepoInfo[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");
  const [proof, setProof] = useState<Proof | null>(null);

  const loadRepos = () => {
    setError(null);
    setLoading(true);
    let active = true;

    const run = async () => {
      let lastError: unknown = null;
      const delays = staticRuntime ? [0] : repoRetryDelays;
      for (let attempt = 0; attempt < delays.length; attempt += 1) {
        const delay = delays[attempt];
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

  // The strongest recorded call between two areas of a small, familiar repo.
  useEffect(() => {
    const pickRepo = repos.find((r) => r.id === "psf__requests") ?? repos[0];
    if (!pickRepo) return;
    let active = true;
    fetchWikiAreaMap(pickRepo.id)
      .then((map) => {
        const link = map.links[0];
        if (!active || !link?.example) return;
        const anchor = link.example.anchor;
        const file = anchor ? repoRelative(anchor.file) ?? anchor.file : "";
        setProof({
          repoId: pickRepo.id,
          repo: pickRepo.repo,
          commit: pickRepo.commit_short,
          from: splitSymbolLabel(link.example.source).symbol,
          to: splitSymbolLabel(link.example.target).symbol,
          file: file.split("/").pop() || file,
          line: anchor?.line ?? null,
        });
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [repos]);

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return repos;
    return repos.filter(
      (r) =>
        r.repo.toLowerCase().includes(needle) ||
        r.id.toLowerCase().includes(needle) ||
        (r.language || "").toLowerCase().includes(needle) ||
        (r.summary || "").toLowerCase().includes(needle)
    );
  }, [repos, q]);

  return (
    <div className="landing">
      <Header />
      <section className="hero">
        <h1>Read a codebase through its call graph</h1>
        <p className="hero-sub">
          Every wiki here stands on a compiler-precise index of the repository.
          Its structure comes from recorded calls, and its prose cites the lines
          it explains.
        </p>
        {proof && <HeroProof proof={proof} />}
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

      {!staticRuntime && (
        <div className="landing-actions">
          <AppLink className="add-repo-link" href="/add-repo" aria-label="Index your own repository">
            + Index your own repository
          </AppLink>
        </div>
      )}

      {error && (
        <div className="repo-grid">
          <div className="empty">
            <p>
              {staticRuntime ? (
                "Static Wiki data is unavailable."
              ) : (
                <>Backend unavailable — start it with <code>codenib-web</code> after building an index.</>
              )}
            </p>
            <p className="small muted">Request failed: {error}</p>
            <button type="button" className="codegraph-fit" onClick={loadRepos}>
              Retry
            </button>
          </div>
        </div>
      )}
      {!error && loading && repos.length === 0 && (
        <div className="repo-grid"><div className="empty">Loading repositories…</div></div>
      )}
      {!error && !loading && repos.length === 0 && (
        <div className="repo-grid"><div className="empty">No repositories found.</div></div>
      )}
      {q.trim() ? (
        <div className="repo-grid">
          {filtered.map((r) => (
            <RepoCard key={r.id} r={r} />
          ))}
        </div>
      ) : (
        groupByLanguage(repos).map((group) => (
          <section className="repo-group" key={group.language} aria-label={`${group.language} repositories`}>
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
    </div>
  );
}
