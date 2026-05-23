"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import Header from "@/components/Header";
import { fetchRepos, type RepoInfo } from "@/lib/api";

function repoDescription(r: RepoInfo): string {
  if (r.description) return r.description;
  return `${r.language || "Source"} repository indexed at ${r.commit_short}`;
}

function RepoCard({ r }: { r: RepoInfo }) {
  return (
    <Link className="repo-card" href={`/${r.id}`} aria-label={`Open ${r.repo} wiki`}>
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
        <span className="mono">{r.commit_short}</span>
      </div>
      <span className="repo-card-go" aria-hidden>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M5 12h14M13 6l6 6-6 6" />
        </svg>
      </span>
    </Link>
  );
}

export default function Landing() {
  const router = useRouter();
  const [repos, setRepos] = useState<RepoInfo[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState("");

  useEffect(() => {
    fetchRepos()
      .then(setRepos)
      .catch((e) => setError(String(e)));
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
              if (e.key === "Enter" && filtered.length > 0) router.push(`/${filtered[0].id}`);
            }}
            placeholder="Search repositories (press Enter to open)"
            aria-label="Search repositories"
          />
        </div>
      </section>

      <div className="repo-grid">
        <div className="repo-card add-repo" role="note" aria-label="Add repo (coming soon)">
          <span className="add-plus">+</span>
          <span className="add-label">Add repo</span>
          <span className="repo-card-go" aria-hidden>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M5 12h14M13 6l6 6-6 6" />
            </svg>
          </span>
        </div>

        {error && (
          <div className="empty">
            Backend unavailable — start it with <code>codeminer-web</code> after building an index.
          </div>
        )}
        {!error && repos.length === 0 && <div className="empty">Loading repositories…</div>}
        {filtered.map((r) => (
          <RepoCard key={r.id} r={r} />
        ))}
      </div>
    </div>
  );
}
