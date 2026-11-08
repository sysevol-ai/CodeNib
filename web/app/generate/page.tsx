// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useMemo, useState } from "react";
import Header from "@/components/Header";
import WikiGenerationForm from "@/components/WikiGenerationForm";
import WikiGraphPane from "@/components/WikiGraphPane";
import WikiStateLegend from "@/components/WikiStateLegend";
import { parseGitHubRepository } from "@/lib/githubPreview";
import { AppLink, navigate } from "@/lib/router";
import { isStaticRuntime } from "@/lib/runtime";
import { buildSourceGraph, describeSourceNode, SOURCE_HUB } from "@/lib/sourceMap";
import {
  newWikiAttempt,
  recentWikis,
  wikiGenerationAvailable,
  type WikiAttempt,
  type WikiRepositoryCheck,
} from "@/lib/visitorWiki";
import "@/components/WikiRun.css";

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
  const previous = recentWikis().filter(
    (item) =>
      item.repository.toLowerCase() === repo?.slug.toLowerCase() &&
      item.id !== attempt?.id,
  );
  const slug = repo?.slug ?? `${owner}/${name}`;
  const tree = check?.eligible ? check.source_tree : undefined;
  const source = useMemo(() => buildSourceGraph(slug, tree), [slug, tree]);
  const folders = tree ? tree.folders.length + tree.more.folders : 0;
  let idle: { title: string; detail: string };
  if (tree)
    idle = {
      title: `${tree.files.toLocaleString()} code files in ${folders} ${folders === 1 ? "folder" : "folders"}`,
      detail: `at ${check?.commit?.slice(0, 8)} · chapters take their place once planned`,
    };
  else if (check && !check.eligible)
    idle = { title: "This snapshot cannot be generated here", detail: "The reason is beside the map." };
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
          <p className="own-lead">
            CodeNib reads this snapshot, plans the chapters on a map, and writes
            each one from cited source. Ready chapters open while the rest are
            written.
          </p>
        </div>
        <section className="own-start-map" aria-label="The snapshot as a map">
          <WikiGraphPane
            graph={source.graph}
            activeId={SOURCE_HUB}
            hasReferences={false}
            note={null}
            onPick={() => {}}
            onOpenReference={() => {}}
            embedded
            readonly
            live
            // The repository turns while its snapshot is being checked.
            states={enabled && !check ? { [SOURCE_HUB]: "running" } : undefined}
            idle={idle}
            describe={(node) => describeSourceNode(node, source.facts)}
            legend={<WikiStateLegend folders />}
          />
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
              onChecked={setCheck}
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
