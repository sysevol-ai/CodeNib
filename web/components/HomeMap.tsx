// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from "react";

import RepoPoster from "@/components/RepoPoster";
import "./HomeMap.css";
import {
  fetchWikiAreaMap,
  fetchWikiPage,
  fetchWikiTree,
  type RepoInfo,
  type WikiAreaMap,
  type WikiPageRef,
} from "@/lib/api";
import { extractJourney, splitWikiMarkdown, type Journey } from "@/lib/wikiPresentation";

/** Repositories whose traced path lands in a named area at every step, one
 *  per language, so switching shows a different kind of system each time. */
export const HOME_MAP_REPOS: Array<{ id: string; label: string; language: string }> = [
  { id: "psf__requests", label: "requests", language: "Python" },
  { id: "gin-gonic__gin", label: "gin", language: "Go" },
  { id: "nushell__nushell", label: "nushell", language: "Rust" },
  { id: "fmtlib__fmt", label: "fmt", language: "C++" },
];

export interface Loaded {
  id: string;
  map: WikiAreaMap;
  pages: WikiPageRef[];
  journey: Journey | null;
}

const cache = new Map<string, Promise<Loaded>>();

/** Map, page tree and traced path of one prepared repository, shared. */
export function loadRepoMap(id: string): Promise<Loaded> {
  let pending = cache.get(id);
  if (!pending) {
    pending = Promise.all([
      fetchWikiAreaMap(id),
      fetchWikiTree(id, { cachedOnly: true }),
      fetchWikiPage(id, "overview", { materializeMedia: false }).catch(() => null),
    ]).then(([map, tree, overview]) => ({
      id,
      map,
      pages: tree.pages,
      journey: overview
        ? extractJourney(splitWikiMarkdown(overview.markdown || "").body).journey
        : null,
    }));
    pending.catch(() => cache.delete(id));
    cache.set(id, pending);
  }
  return pending;
}

/** The home page's live map: one indexed repository at a time, with its
 *  traced path played when it appears. */
export default function HomeMap({ repos }: { repos: RepoInfo[] }) {
  const available = HOME_MAP_REPOS.filter(
    (item) => repos.length === 0 || repos.some((repo) => repo.id === item.id),
  );
  const [selected, setSelected] = useState(available[0]?.id ?? HOME_MAP_REPOS[0].id);
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setFailed(false);
    loadRepoMap(selected)
      .then((value) => {
        if (!cancelled) setLoaded(value);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [selected]);

  // Warm the other maps after the first one is on screen.
  useEffect(() => {
    if (!loaded) return;
    const timer = window.setTimeout(() => {
      for (const item of available) if (item.id !== selected) void loadRepoMap(item.id).catch(() => {});
    }, 1500);
    return () => window.clearTimeout(timer);
  }, [loaded?.id]);

  const repo = repos.find((item) => item.id === selected) ?? null;
  const showing = loaded && loaded.id === selected ? loaded : null;

  return (
    <div className="home-map">
      <div className="home-map-tabs" role="tablist" aria-label="Choose a repository">
        {available.map((item) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            aria-selected={item.id === selected}
            className={`home-map-tab${item.id === selected ? " is-active" : ""}`}
            onClick={() => setSelected(item.id)}
          >
            {item.label}
            <span>{item.language}</span>
          </button>
        ))}
      </div>
      {showing && showing.map.areas.some((area) => area.symbols > 0) ? (
        <RepoPoster
          key={showing.id}
          variant="hero"
          repoId={showing.id}
          repo={repo}
          map={showing.map}
          pages={showing.pages}
          journey={showing.journey}
        />
      ) : (
        <div className="repo-poster is-hero home-map-placeholder" role="status">
          <div className="poster-grid-bg" aria-hidden />
          <span>{failed ? "This map could not be loaded. Pick another repository." : "Loading the map…"}</span>
        </div>
      )}
    </div>
  );
}
