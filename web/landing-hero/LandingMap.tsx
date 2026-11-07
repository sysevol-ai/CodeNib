// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { useState } from "react";

import HeroGraph from "@/components/HeroGraph";
import "@/components/HomeMap.css";
import type { RepoInfo, WikiAreaMap, WikiPageRef } from "@/lib/api";

export interface CapturedMap {
  id: string;
  label: string;
  language: string;
  repo: Pick<RepoInfo, "repo" | "commit_short" | "base_commit" | "source_url" | "file_count">;
  map: WikiAreaMap;
  pages: WikiPageRef[];
  /** The Overview's traced path, which the repository video reads. */
  overview: string;
}

/**
 * The codenib.ai hero: the same map as the demo home, drawn from maps captured
 * at build time, so the marketing page makes no request to the demo. Pages and
 * the repository name open the Wiki on the demo.
 */
export default function LandingMap({ maps, demo }: { maps: CapturedMap[]; demo: string }) {
  const [selected, setSelected] = useState(maps[0]?.id);
  const current = maps.find((item) => item.id === selected) ?? maps[0];
  if (!current) return null;
  const wiki = `${demo}/${encodeURIComponent(current.id)}`;
  return (
    <div className="home-map">
      <div className="home-map-bar">
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
        <a className="home-map-all" href={`${demo}/browse`}>
          All Wikis <span aria-hidden="true">→</span>
        </a>
      </div>
      <HeroGraph
        key={current.id}
        repoName={current.repo.repo}
        pages={current.pages}
        map={current.map}
        wikiHref={wiki}
        onPick={(page) => {
          window.location.href = `${wiki}?p=${encodeURIComponent(page)}`;
        }}
      />
    </div>
  );
}
