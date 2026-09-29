// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { lazy, Suspense, useEffect, useState } from "react";
import AreaMap from "./AreaMap";
import { loadSavedWikiGraphs, type SavedWikiGraphs } from "@/lib/visitorWiki";

const GraphView = lazy(() => import("./GraphView"));

export default function SavedWikiMaps({
  id,
  pageId,
  repository,
  complete,
  onPick,
  hideSystemMap = false,
}: {
  id: string;
  pageId: string;
  repository: string;
  complete: boolean;
  onPick: (page: string) => void;
  /** The page already opens on the poster drawn from the same map. */
  hideSystemMap?: boolean;
}) {
  const [maps, setMaps] = useState<SavedWikiGraphs | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    let cancelled = false;
    setMaps(null);
    setError("");
    if (complete)
      loadSavedWikiGraphs(id, pageId)
        .then((value) => {
          if (!cancelled) setMaps(value);
        })
        .catch(() => {
          if (!cancelled) setError("The saved code maps could not be loaded.");
        });
    return () => {
      cancelled = true;
    };
  }, [id, pageId, complete, attempt]);
  return (
    <section className="saved-wiki-maps" aria-label="Code maps">
      {pageId === "overview" &&
        !hideSystemMap &&
        maps?.coverage.available &&
        maps.system_map.areas.length > 0 && (
          <AreaMap
            map={maps.system_map}
            commit={maps.commit.slice(0, 8)}
            onPick={onPick}
          />
        )}
      <div className="saved-wiki-map-heading">
        <h2>
          {pageId === "overview"
            ? hideSystemMap
              ? "CodeGraph"
              : "System Map & CodeGraph"
            : "Chapter CodeGraph"}
        </h2>
        {maps?.code_graph.available && (
          <button
            className="btn-outline"
            aria-expanded={open}
            onClick={() => setOpen(!open)}
          >
            {open ? "Hide CodeGraph" : "Explore CodeGraph"}
          </button>
        )}
      </div>
      {!complete ? (
        <p role="status">
          Code maps will appear when generation finishes. Ready chapters are
          available now.
        </p>
      ) : error ? (
        <p role="alert">
          {error}{" "}
          <button onClick={() => setAttempt(attempt + 1)}>Retry maps</button>
        </p>
      ) : !maps ? (
        <p role="status">Loading saved code maps…</p>
      ) : (
        <>
          <p className="small muted">{maps.coverage.note}</p>
          {maps.coverage.available &&
            pageId === "overview" &&
            !maps.system_map.available && (
              <p className="small muted">
                No indexed connections were found between these chapters.
                Explore the CodeGraph to inspect the recorded source
                relationships.
              </p>
            )}
          {maps.coverage.available && !maps.code_graph.available && (
            <p className="small muted">
              No connected indexed symbols were found for this chapter's
              citations.
            </p>
          )}
          {open && maps.code_graph.available && (
            <div className="saved-wiki-codegraph">
              <Suspense fallback={<p role="status">Opening CodeGraph…</p>}>
                <GraphView
                  repoId=""
                  data={maps.code_graph}
                  variant="explore"
                  repoFullName={repository}
                  commit={maps.commit}
                  localOnly
                />
              </Suspense>
            </div>
          )}
        </>
      )}
    </section>
  );
}
