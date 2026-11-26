// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useMemo, useState } from "react";
import Header from "@/components/Header";
import HeroGraph from "@/components/HeroGraph";
import WikiGenerationForm from "@/components/WikiGenerationForm";
import WikiGraphPane from "@/components/WikiGraphPane";
import WikiStateLegend from "@/components/WikiStateLegend";
import { parseGitHubRepository } from "@/lib/githubPreview";
import { AppLink, navigate } from "@/lib/router";
import { isStaticRuntime } from "@/lib/runtime";
import { freeAreaUrl, freeMapPicture } from "@/lib/freeMap";
import { buildSourceGraph, describeSourceNode, SOURCE_HUB } from "@/lib/sourceMap";
import {
  loadFreeRepositoryMap,
  newWikiAttempt,
  recentWikis,
  wikiGenerationAvailable,
  type FreeRepositoryMap,
  WikiRequestError,
  type WikiAttempt,
  type WikiRepositoryCheck,
} from "@/lib/visitorWiki";
import "@/components/WikiRun.css";

/** The free map's catch-all group is grey: it is not a part of its own. */
const OTHER_PART: ReadonlySet<string> = new Set(["other"]);

export default function GenerateWikiPage({
  owner,
  name,
}: {
  owner: string;
  name: string;
}) {
  const repo = parseGitHubRepository(`${owner}/${name}`);
  const [attempt, setAttempt] = useState<WikiAttempt | null>(null);
  const [error, setError] = useState("");
  const [enabled, setEnabled] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [check, setCheck] = useState<WikiRepositoryCheck | null>(null);
  // The map the code index draws without a model: offered when the server
  // allows it, before any key is asked for.
  const [freeMapOffered, setFreeMapOffered] = useState(false);
  const [freeMap, setFreeMap] = useState<FreeRepositoryMap | null>(null);
  const [freeMapFailed, setFreeMapFailed] = useState(false);
  const [checkFailed, setCheckFailed] = useState("");
  /** Seconds until a refused free map is asked for again; 0 when not waiting. */
  const [freeMapWait, setFreeMapWait] = useState(0);
  /** The map was refused twice; the outline says so instead of falling silent. */
  const [freeMapRefused, setFreeMapRefused] = useState(false);
  useEffect(() => {
    let ignored = false;
    if (isStaticRuntime()) {
      setLoaded(true);
      return;
    }
    wikiGenerationAvailable()
      .then((result) => {
        if (!ignored) {
          setEnabled(result.enabled);
          setFreeMapOffered(!!result.free_map);
          setLoaded(true);
        }
      })
      .catch(() => {
        if (!ignored) {
          setLoaded(true);
          setError(
            "The Wiki generation service is unavailable. Please try again shortly.",
          );
        }
      });
    return () => {
      ignored = true;
    };
  }, []);
  useEffect(() => {
    if (!repo || !enabled) return;
    try {
      setAttempt(newWikiAttempt(repo.slug));
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Could not save attempt access.",
      );
    }
  }, [enabled, repo?.slug]);
  useEffect(() => {
    if (!repo || !enabled || !freeMapOffered) return;
    let ignored = false;
    setFreeMap(null);
    setFreeMapFailed(false);
    let timer = 0;
    // A refused map (the server is busy, or this network asked a moment ago)
    // is asked for once more when the server says it may be.
    const ask = (retried: boolean) =>
      loadFreeRepositoryMap(repo.slug)
        .then((result) => {
          if (!ignored) {
            setFreeMap(result);
            setFreeMapWait(0);
          }
        })
        .catch((reason: unknown) => {
          if (ignored) return;
          if (!retried && reason instanceof WikiRequestError && reason.status === 429) {
            const wait = Math.min(120, Math.max(1, Math.ceil(reason.retryAfter ?? 60)));
            setFreeMapWait(wait);
            timer = window.setTimeout(() => void ask(true), wait * 1000);
            return;
          }
          setFreeMapWait(0);
          setFreeMapRefused(reason instanceof WikiRequestError && reason.status === 429);
          setFreeMapFailed(true);
        });
    void ask(false);
    return () => {
      ignored = true;
      window.clearTimeout(timer);
    };
  }, [enabled, freeMapOffered, repo?.slug]);
  const picture = useMemo(() => freeMapPicture(freeMap), [freeMap]);
  const previous = recentWikis().filter(
    (item) =>
      item.repository.toLowerCase() === repo?.slug.toLowerCase() &&
      item.id !== attempt?.id,
  );
  const slug = repo?.slug ?? `${owner}/${name}`;
  // The free map carries the same outline, so either answer draws it.
  const tree = check?.eligible
    ? check.source_tree
    : freeMap?.eligible
      ? freeMap.source_tree
      : undefined;
  const source = useMemo(() => buildSourceGraph(slug, tree), [slug, tree]);
  const folders = tree ? tree.folders.length + tree.more.folders : 0;
  const snapshot = check?.commit ?? freeMap?.commit ?? null;
  let idle: { title: string; detail: string };
  if (tree)
    idle = {
      title: `${tree.files.toLocaleString()} code files in ${folders} ${folders === 1 ? "folder" : "folders"}`,
      // Either answer names the snapshot; a refused check leaves the map's.
      detail: [
        snapshot ? `at ${snapshot.slice(0, 8)}` : "",
        freeMapOffered && !freeMap && !freeMapFailed
          ? freeMapWait
            ? "map refused (server busy); asking again shortly"
            : "indexing the code for its map"
          : freeMapRefused
            ? "server busy, map not drawn; reload in a minute"
            : checkFailed
              ? "check refused just now; see the form"
              : "a Wiki's chapters appear here as they are planned",
      ]
        .filter(Boolean)
        .join("\u00a0·\u00a0"),
    };
  else if (check && !check.eligible)
    idle = { title: "This snapshot cannot be generated here", detail: "The reason is with the form." };
  else if (checkFailed && freeMapOffered && !freeMap && !freeMapFailed)
    idle = {
      title: "Check refused just now",
      detail: freeMapWait ? "The map was refused too; asking again shortly." : "The map is still being drawn.",
    };
  else if (checkFailed && freeMapRefused)
    idle = { title: "The server is busy just now", detail: "Neither the check nor the map ran; reload in a minute." };
  else if (checkFailed)
    idle = { title: "The snapshot could not be checked just now", detail: "See the form; try again shortly." };
  else if (loaded && !enabled)
    idle = { title: slug, detail: "The folder map appears once generation is available." };
  else idle = { title: "Checking the snapshot", detail: "Its folders appear when the check finishes. No key is needed." };
  const [ownerName, repoName] = slug.split("/");

  return (
    <>
      <Header />
      <main className="own-start">
        <div className="own-start-copy">
          <AppLink className="own-back" href="/browse">← Browse Wikis</AppLink>
          <h1 className="own-title">
            {repo ? (
              <>
                <span>{ownerName}/</span>
                {repoName}
              </>
            ) : (
              "Invalid repository URL"
            )}
          </h1>
          {/* One lead for every state of the free map, so the form below it
              never moves when the map arrives. */}
          {freeMapOffered && enabled ? (
            <p className="own-lead">
              CodeNib reads this snapshot and draws what its code index finds:
              for Python code, its parts, the references recorded between them
              and the path a call takes. That needs no key; a key is needed only
              to write the Wiki's chapters.
            </p>
          ) : (
            <p className="own-lead">
              CodeNib reads this snapshot, plans the chapters on a map, and writes
              each one from cited source. Ready chapters open while the rest are
              written.
            </p>
          )}

        </div>
        <section className="own-start-map" aria-label="The snapshot as a map">
          {picture ? (
            <HeroGraph
              repoName={slug}
              pages={picture.pages}
              map={picture.map}
              route={picture.route}
              unit="parts"
              retryAreaLabels
              neutral={OTHER_PART}
              wikiHref={`https://github.com/${slug}`}
              onPick={(id) => {
                // A part opens on GitHub at the commit the map was drawn from.
                const url = freeMap?.commit
                  ? freeAreaUrl(slug, freeMap.commit, freeMap.map?.areas.find((area) => area.id === id))
                  : null;
                if (url) window.open(url, "_blank", "noopener");
              }}
            />
          ) : (
          <WikiGraphPane
            graph={source.graph}
            activeId={SOURCE_HUB}
            hasReferences={false}
            onPick={() => {}}
            onOpenReference={() => {}}
            embedded
            readonly
            live
            // The repository turns while its snapshot is being checked.
            states={
              enabled &&
              ((!check && !checkFailed) || (freeMapOffered && !freeMap && !freeMapFailed))
                ? { [SOURCE_HUB]: "running" }
                : undefined
            }
            idle={idle}
            describe={(node) => describeSourceNode(node, source.facts)}
            // Before a run there are no chapters yet when the free map is
            // on its way: the legend explains only what is drawn.
            legend={freeMapOffered ? <WikiStateLegend folders chapters={false} /> : <WikiStateLegend folders />}
            note={
              freeMap?.map && !freeMap.map.available && freeMap.map.reason === "graph_language"
                ? "The free map traces Python code for now; this is the repository's folder outline. The written Wiki covers every supported language."
                : null
            }
          />
          )}
        </section>
        <div className="own-start-form">
          {!loaded && <p className="own-wait" role="status">Connecting to the Wiki service…</p>}
          {loaded && !enabled && (
            <div className="generation-notice">
              {isStaticRuntime() ? (
                <p>
                  Generate and save a Wiki on the{" "}
                  <a
                    href={`https://demo.codenib.ai/preview/${encodeURIComponent(owner)}/${encodeURIComponent(name)}`}
                  >
                    live CodeNib demo ↗
                  </a>
                  .
                </p>
              ) : (
                <p>
                  This server has not enabled visitor Wiki generation yet. You can
                  still read the <AppLink href="/wikis">ready Wikis</AppLink>.
                </p>
              )}
            </div>
          )}
          {error && (
            <p role="alert" className="trial-error">
              {error}
            </p>
          )}
          {attempt && enabled && (
            <WikiGenerationForm
              attempt={attempt}
              onChecked={(result) => {
                setCheck(result);
                if (result) setCheckFailed("");
              }}
              onCheckFailed={setCheckFailed}
              onStarted={() => navigate(`/wiki/${attempt.id}`)}
            />
          )}
          <p className="own-aside">
            Built from a snapshot of the default branch. Your Wiki is saved in My
            Wikis; share its link when it is ready, or publish it to Browse so
            others can discover it.
            {previous.length > 0 && (
              <>
                {" "}
                <AppLink href="/browse?tab=mine">Open your existing Wikis →</AppLink>
              </>
            )}
          </p>
        </div>
      </main>
    </>
  );
}
