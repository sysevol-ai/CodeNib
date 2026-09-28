"use client";

import { useEffect, useMemo, useState, type ReactNode } from "react";
import Header from "@/components/Header";
import { fetchRepos, type RepoInfo } from "@/lib/api";
import { primaryLanguage } from "@/lib/landing";
import RepositoryEntry from "@/components/RepositoryEntry";
import FirstVisitExplorer from "@/components/FirstVisitExplorer";
import { AppLink, useBrowserLocation } from "@/lib/router";
import { isStaticRuntime } from "@/lib/runtime";
import {
  recentWikis,
  loadPublicWikis,
  loadSavedWiki,
  type PublicWiki,
  type SavedWiki,
} from "@/lib/visitorWiki";

function repoDescription(r: RepoInfo): ReactNode {
  // `summary` is chosen server-side to be a statement of purpose; the raw
  // README blurb is often a chat invitation or a pager note, so it is not a
  // fallback here.
  const text =
    r.summary ||
    `${primaryLanguage(r)} repository indexed at ${r.commit_short}.`;
  return text
    .split(/(`[^`]+`)/)
    .map((part, index) =>
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
    <AppLink
      className="repo-card"
      href={`/${r.id}`}
      aria-label={`Open ${r.repo} wiki`}
    >
      <div className="repo-card-title">{r.repo}</div>
      <div className="repo-card-desc">{repoDescription(r)}</div>
      <div className="repo-card-footer">
        <span
          className={`lang lang-${(r.language || "").toLowerCase().split("/")[0]}`}
        >
          {primaryLanguage(r)}
        </span>
        {r.file_count > 0 && (
          <span className="repo-metric" title="indexed files">
            <svg
              width="12"
              height="12"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
              <path d="M13 2v7h7" />
            </svg>
            {r.file_count.toLocaleString()}{" "}
            {r.file_count === 1 ? "file" : "files"}
          </span>
        )}
        {incremental ? (
          <span
            className="repo-metric repo-incremental"
            title="Cold graph-build time divided by mean warm patch time; excludes LSP startup and transition overhead"
          >
            <svg
              width="12"
              height="12"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d="M13 2 3 14h9l-1 8 10-12h-9z" />
            </svg>
            {incremental}
          </span>
        ) : (
          <span className="mono">{r.commit_short}</span>
        )}
      </div>
      <span className="repo-card-go" aria-hidden>
        <svg
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M5 12h14M13 6l6 6-6 6" />
        </svg>
      </span>
    </AppLink>
  );
}

type LibraryWiki = { id: string; repository: string; wiki?: SavedWiki };

function CommunityCard({ wiki }: { wiki: PublicWiki }) {
  return (
    <AppLink
      className="repo-card"
      href={`/wiki/${wiki.id}`}
      aria-label={`Open ${wiki.repository} wiki`}
    >
      <div className="repo-card-title">{wiki.repository}</div>
      <div className="repo-card-desc">
        {wiki.summary ||
          "Explore this repository through its source-linked Wiki."}
      </div>
      <div className="repo-card-footer">
        <span className="wiki-community-label">Community</span>
        <span>
          {wiki.chapters} {wiki.chapters === 1 ? "chapter" : "chapters"}
        </span>
        {wiki.languages[0] && <span>{wiki.languages[0]}</span>}
      </div>
      <span className="repo-card-go" aria-hidden>
        ↗
      </span>
    </AppLink>
  );
}

function LibraryCard({ item }: { item: LibraryWiki }) {
  const wiki = item.wiki;
  const total = Object.keys(wiki?.page_states || {}).length;
  const ready = Object.values(wiki?.page_states || {}).filter(
    (value) => value === "ready",
  ).length;
  const label = !wiki
    ? "Open Wiki"
    : wiki.status === "complete"
      ? "Ready to read"
      : wiki.status === "partial"
        ? "Paused · continue when ready"
        : "Generating";
  return (
    <AppLink
      className="repo-card"
      href={`/wiki/${item.id}`}
      aria-label={`Open ${item.repository} wiki`}
    >
      <div className="repo-card-title">{item.repository}</div>
      <div className="repo-card-desc">{label}</div>
      <div className="repo-card-footer">
        <span>
          {total
            ? `${ready} / ${total} chapters ready`
            : "Open to check progress"}
        </span>
        {wiki?.published && (
          <span className="wiki-community-label">In Community</span>
        )}
        {wiki?.status === "complete" && !wiki.published && (
          <span className="wiki-community-label">Unlisted</span>
        )}
      </div>
      {wiki?.status === "complete" && !wiki.published && (
        <span className="library-publish-hint">
          Open to publish to Community →
        </span>
      )}
      <span className="repo-card-go" aria-hidden>
        →
      </span>
    </AppLink>
  );
}

export default function Landing({ browse = false }: { browse?: boolean }) {
  const staticRuntime = isStaticRuntime();
  const location = useBrowserLocation();
  const selected = new URLSearchParams(location.search).get("tab") || "all";
  const tab =
    browse && ["featured", "community", "mine"].includes(selected)
      ? selected
      : "all";
  const [repos, setRepos] = useState<RepoInfo[]>([]);
  const [community, setCommunity] = useState<PublicWiki[]>([]);
  const [mine, setMine] = useState<LibraryWiki[]>([]);
  const [error, setError] = useState("");
  const [communityError, setCommunityError] = useState("");
  const [loading, setLoading] = useState(true);
  const [communityLoading, setCommunityLoading] = useState(!staticRuntime);
  const [mineLoading, setMineLoading] = useState(!staticRuntime);
  const [q, setQ] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    fetchRepos()
      .then((value) => {
        if (active) setRepos(value);
      })
      .catch(() => {
        if (active) setError("Featured Wikis could not be loaded.");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    if (!staticRuntime) {
      setCommunityLoading(true);
      setMineLoading(true);
      setCommunityError("");
      loadPublicWikis()
        .then((value) => {
          if (active) setCommunity(value);
        })
        .catch(() => {
          if (active) setCommunityError("Community Wikis could not be loaded.");
        })
        .finally(() => {
          if (active) setCommunityLoading(false);
        });
      const saved = recentWikis()
        .slice(0, 24)
        .map(({ id, repository }) => ({ id, repository }));
      setMine(saved);
      Promise.all(
        saved.map(async (item) => {
          try {
            return { ...item, wiki: await loadSavedWiki(item.id) };
          } catch {
            return item;
          }
        }),
      ).then((value) => {
        if (active) {
          setMine(value);
          setMineLoading(false);
        }
      });
    }
    return () => {
      active = false;
    };
  }, [staticRuntime, retry]);
  const needle = q.trim().toLowerCase();
  const featured = useMemo(
    () =>
      repos.filter((r) =>
        `${r.repo} ${r.language} ${r.summary}`.toLowerCase().includes(needle),
      ),
    [repos, needle],
  );
  const published = community.filter((w) =>
    `${w.repository} ${w.summary} ${w.languages.join(" ")}`
      .toLowerCase()
      .includes(needle),
  );
  const personal = mine.filter((w) =>
    w.repository.toLowerCase().includes(needle),
  );
  const showFeatured = tab === "all" || tab === "featured";
  const showCommunity =
    !staticRuntime && (tab === "all" || tab === "community");
  const visibleCount =
    (showFeatured ? featured.length : 0) +
    (showCommunity ? published.length : 0) +
    (tab === "mine" ? personal.length : 0);
  const catalogLoading =
    (showFeatured && loading) ||
    (showCommunity && communityLoading) ||
    (tab === "mine" && mineLoading);
  const catalogError =
    (showFeatured ? error : "") || (showCommunity ? communityError : "");
  return (
    <div className="landing">
      <Header />
      {browse ? (
        <section className="browse-intro landing-catalog">
          <span className="browse-eyebrow">CODE, EXPLAINED</span>
          <h1>Browse Wikis</h1>
          <p>
            Explore how real projects work. Read a featured guide or discover
            one shared by the community.
          </p>
          <AppLink href="/" className="btn-outline">
            Create a Wiki →
          </AppLink>
        </section>
      ) : (
        <section className={`hero ${!staticRuntime ? "first-visit-hero" : ""}`}>
          <div className="first-visit-intro">
            <p className="first-visit-eyebrow">FROM THE BIG PICTURE TO THE SOURCE</p>
            <h1>Understand a repo.<br />Follow the code.</h1>
            <p className="hero-sub">
              Explore how a repository works, see the code behind each explanation,
              and keep going with a complete Wiki.
            </p>
            {!staticRuntime && <p className="first-visit-invitation">Try the example. No account or API key needed.</p>}
          </div>
          {!staticRuntime && <FirstVisitExplorer />}
          <div className="first-visit-entry">
            <RepositoryEntry repos={repos} />
            <AppLink className="browse-entry" href="/browse">Explore ready Wikis →</AppLink>
          </div>
        </section>
      )}
      {!browse && mine.length > 0 && (
        <section
          className="landing-catalog wiki-continue"
          aria-label="Continue reading"
        >
          <div className="landing-catalog-heading">
            <h2>Continue reading</h2>
            <AppLink href="/browse?tab=mine">My Wikis →</AppLink>
          </div>
          <div className="repo-grid">
            {mine.slice(0, 3).map((item) => (
              <LibraryCard key={item.id} item={item} />
            ))}
          </div>
        </section>
      )}
      <section className="landing-catalog" aria-label="Wiki collection">
        <div className="landing-catalog-heading">
          <div>
            {browse ? (
              <nav className="browse-tabs" aria-label="Wiki collections">
                {[
                  ["all", "All Wikis"],
                  ["featured", "Featured"],
                  ...(!staticRuntime
                    ? [
                        ["community", "Community"],
                        ["mine", "My Wikis"],
                      ]
                    : []),
                ].map(([value, label]) => (
                  <AppLink
                    key={value}
                    href={`/browse?tab=${value}`}
                    aria-current={tab === value ? "page" : undefined}
                  >
                    {label}
                  </AppLink>
                ))}
              </nav>
            ) : (
              <>
                <h2>Explore a Wiki</h2>
                <p className="small muted">
                  Architecture, explanations, and the source behind them.
                </p>
              </>
            )}
          </div>
          <div className="search-box">
            <input
              type="search"
              value={q}
              onChange={(event) => setQ(event.target.value)}
              placeholder="Search repositories or topics…"
              aria-label="Search Wikis"
            />
          </div>
        </div>
        {tab === "mine" && (
          <p className="small muted">
            Pick up where you left off, or publish a finished Wiki for others to
            discover.
          </p>
        )}
        {catalogError && (
          <p role="alert" className="browse-error">
            {catalogError}{" "}
            <button
              className="btn-ghost"
              onClick={() => setRetry((value) => value + 1)}
            >
              Retry
            </button>
          </p>
        )}
        {catalogLoading && visibleCount === 0 && (
          <div
            className="repo-grid"
            aria-label="Loading Wikis"
            aria-busy="true"
          >
            {[0, 1, 2].map((i) => (
              <div key={i} className="repo-card wiki-catalog-skeleton">
                <span />
                <span />
                <span />
              </div>
            ))}
          </div>
        )}
        {!catalogLoading && !catalogError && visibleCount === 0 && (
          <div className="browse-empty">
            <h2>
              {needle
                ? "No matching Wikis"
                : tab === "mine"
                  ? "Your next discovery starts with a repository"
                  : tab === "community"
                    ? "Share the first community Wiki"
                    : "No Wikis here yet"}
            </h2>
            <p>
              {needle
                ? "Try a repository name, language, or topic."
                : tab === "community"
                  ? "Open a completed Wiki in My Wikis and choose Publish to Community. Other readers will be able to discover it here."
                  : "Create a Wiki from a public GitHub repository, or explore a featured project."}
            </p>
            {!needle && (
              <AppLink
                href={tab === "mine" ? "/browse?tab=featured" : "/"}
                className="btn-outline"
              >
                {tab === "mine" ? "Explore featured Wikis" : "Create a Wiki"} →
              </AppLink>
            )}
          </div>
        )}
        <div className="repo-grid">
          {tab === "mine" &&
            personal.map((item) => <LibraryCard key={item.id} item={item} />)}
          {showCommunity &&
            published
              .slice(0, browse ? undefined : 3)
              .map((wiki) => <CommunityCard key={wiki.id} wiki={wiki} />)}
          {showFeatured &&
            featured
              .slice(0, browse || needle ? undefined : 6)
              .map((r) => <RepoCard key={r.id} r={r} />)}
        </div>
        {!browse && (
          <AppLink className="browse-more" href="/browse">
            Browse all Wikis →
          </AppLink>
        )}
      </section>
      <footer className="preview-footer">
        <a href="https://codenib.ai/" target="_blank" rel="noreferrer">
          CodeNib
        </a>
        <a href="https://docs.codenib.ai/" target="_blank" rel="noreferrer">
          Use with your agent ↗
        </a>
      </footer>
    </div>
  );
}
