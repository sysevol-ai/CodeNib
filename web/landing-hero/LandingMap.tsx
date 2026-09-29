// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { useMemo, useState } from "react";

import RepoPoster from "@/components/RepoPoster";
import "@/components/HomeMap.css";
import type { RepoInfo, WikiAreaMap, WikiPageRef } from "@/lib/api";
import { extractJourney, splitWikiMarkdown } from "@/lib/wikiPresentation";

export interface CapturedMap {
  id: string;
  label: string;
  language: string;
  repo: Pick<RepoInfo, "repo" | "commit_short" | "base_commit" | "source_url" | "file_count">;
  map: WikiAreaMap;
  pages: WikiPageRef[];
  /** The Overview body; its traced path is read the same way the Wiki reads it. */
  overview: string;
}

/**
 * The codenib.ai hero: the same live map as the demo home, drawn from maps
 * captured at build time, so the marketing page makes no request to the demo.
 * Areas and the Wiki link open the repository on the demo.
 */
export default function LandingMap({ maps, demo }: { maps: CapturedMap[]; demo: string }) {
  const [selected, setSelected] = useState(maps[0]?.id);
  const current = maps.find((item) => item.id === selected) ?? maps[0];
  const journey = useMemo(
    () => (current ? extractJourney(splitWikiMarkdown(current.overview).body).journey : null),
    [current],
  );
  if (!current) return null;
  const wiki = `${demo}/${encodeURIComponent(current.id)}`;
  return (
    <div className="home-map">
      <div className="home-map-tabs" role="tablist" aria-label="Choose a repository">
        {maps.map((item) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            aria-selected={item.id === current.id}
            className={`home-map-tab${item.id === current.id ? " is-active" : ""}`}
            onClick={() => setSelected(item.id)}
          >
            {item.label}
            <span>{item.language}</span>
          </button>
        ))}
      </div>
      <RepoPoster
        key={current.id}
        variant="hero"
        repoId={current.id}
        repo={{ id: current.id, ...current.repo } as RepoInfo}
        map={current.map}
        pages={current.pages}
        journey={journey}
        sourceRepoId={null}
        wikiHref={wiki}
        onPick={(page) => {
          window.location.href = `${wiki}?p=${encodeURIComponent(page)}`;
        }}
      />
    </div>
  );
}
