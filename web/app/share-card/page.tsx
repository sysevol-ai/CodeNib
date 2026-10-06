// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useRef, useState, type ReactNode } from "react";

import { loadRepoMap, type Loaded } from "@/components/HomeMap";
import RepoPoster from "@/components/RepoPoster";
import { fetchRepos, type RepoInfo } from "@/lib/api";
import { assetUrl } from "@/lib/runtime";

const W = 1200;
const H = 630;
const SITE_MAP = "psf__requests";

/** A summary's `code` spans as code, not as literal backticks. */
function summaryNodes(text: string): ReactNode[] {
  return text.split(/(`[^`]+`)/).map((part, index) =>
    part.length > 2 && part.startsWith("`") && part.endsWith("`") ? (
      <code key={index}>{part.slice(1, -1)}</code>
    ) : (
      part
    ),
  );
}

/**
 * The 1200x630 image a shared link shows. `web/scripts/render-share-cards.mjs`
 * screenshots this route once `data-share-ready` is set. Without a repository
 * id it draws the site card.
 */
export default function ShareCardPage({ repoId }: { repoId?: string }) {
  const site = !repoId;
  const id = repoId || SITE_MAP;
  const [repo, setRepo] = useState<RepoInfo | null>(null);
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [count, setCount] = useState(0);
  const [done, setDone] = useState(false);
  const [scale, setScale] = useState(1);
  const [ready, setReady] = useState(false);
  const posterRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    let active = true;
    Promise.all([fetchRepos(), loadRepoMap(id).catch(() => null)]).then(([repos, map]) => {
      if (!active) return;
      setRepo(repos.find((item) => item.id === id) ?? null);
      setCount(repos.length);
      setLoaded(map);
      setDone(true);
    });
    return () => {
      active = false;
    };
  }, [id]);

  // A tall map is scaled to fit the image rather than cut off. The poster
  // lays itself out at its measured width, so the scale follows its size
  // until it settles, and the image is taken only after that.
  useEffect(() => {
    const node = posterRef.current;
    if (!node || !done || typeof ResizeObserver === "undefined") return;
    let settle = 0;
    const measure = () => {
      const room = node.parentElement?.clientHeight ?? H;
      const natural = node.scrollHeight;
      if (natural > 0) setScale(Math.min(1, room / natural));
      window.clearTimeout(settle);
      settle = window.setTimeout(() => {
        void document.fonts.ready.then(() =>
          requestAnimationFrame(() => requestAnimationFrame(() => setReady(true))),
        );
      }, 400);
    };
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    measure();
    return () => {
      observer.disconnect();
      window.clearTimeout(settle);
    };
  }, [done, loaded]);

  const hasMap = loaded != null && loaded.map.areas.some((area) => area.symbols > 0);
  const host = new URLSearchParams(window.location.search).get("host") || "demo.codenib.ai";
  return (
    <div
      className={`share-card${site ? " is-site" : ""}`}
      style={{ width: W, height: H }}
      data-share-ready={ready ? "true" : undefined}
    >
      {site && (
        <div className="share-card-intro">
          <span className="share-card-brand">
            <img src={assetUrl("/codenib-icon.svg")} alt="" /> CodeNib Wiki
          </span>
          <h1>
            Understand a repo.
            <br />
            Follow the code.
          </h1>
          <p>
            {count > 0 ? `${count} open-source repositories` : "Open-source repositories"},
            mapped from their code index. Follow any line to the source.
          </p>
          <span className="share-card-url">demo.codenib.ai</span>
        </div>
      )}
      <div className="share-card-map">
        <div
          ref={posterRef}
          // Scaling by transform leaves the laid-out size alone, so the
          // measurement above never feeds back into itself.
          style={{ transform: `scale(${scale})`, transformOrigin: "top center" }}
        >
          {hasMap ? (
            <RepoPoster
              variant="card"
              brandHost={host}
              repoId={id}
              repo={repo}
              map={loaded.map}
              pages={loaded.pages}
              journey={loaded.journey}
            />
          ) : (
            <div className="share-card-plain">
              <h1>{repo?.repo || id}</h1>
              <p>{summaryNodes(repo?.summary || repo?.description || "A source-linked Wiki.")}</p>
              <span className="share-card-url">
                {host}/{id}
              </span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
