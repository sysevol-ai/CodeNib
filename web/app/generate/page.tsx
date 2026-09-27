// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from "react";
import Header from "@/components/Header";
import WikiGenerationForm from "@/components/WikiGenerationForm";
import { parseGitHubRepository } from "@/lib/githubPreview";
import { AppLink, navigate } from "@/lib/router";
import { isStaticRuntime } from "@/lib/runtime";
import {
  newWikiAttempt,
  recentWikis,
  wikiGenerationAvailable,
  type WikiAttempt,
} from "@/lib/visitorWiki";

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
  return (
    <>
      <Header />
      <main className="generate-wiki-page">
        <AppLink href="/browse">← Browse Wikis</AppLink>
        <p className="hero-eyebrow">YOUR REPOSITORY, YOUR WIKI</p>
        <h1>{repo?.slug || "Invalid repository URL"}</h1>
        <p className="generate-lead">
          A source-linked guide to the architecture and ideas behind this repository.
        </p>
        <ol className="wiki-generation-steps" aria-label="Generation stages">
          <li>
            <span>1</span> Read source
          </li>
          <li>
            <span>2</span> Plan chapters
          </li>
          <li>
            <span>3</span> Generate & verify
          </li>
          <li>
            <span>4</span> Save & share
          </li>
        </ol>
        {!loaded && <p role="status">Connecting to the Wiki service…</p>}
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
                still read the <AppLink href="/">ready Wikis</AppLink>.
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
            onStarted={() => navigate(`/wiki/${attempt.id}`)}
          />
        )}
        <p className="small muted">
          Built from a snapshot of the default branch. Share the link when it is
          ready, or publish to Browse so others can discover it.
        </p>
        {previous.length > 0 && (
          <AppLink href="/browse?tab=mine">Open your existing Wikis →</AppLink>
        )}
      </main>
    </>
  );
}
