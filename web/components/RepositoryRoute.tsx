// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { lazy, useEffect, useState } from "react";
import Header from "@/components/Header";
import RepositoryEntry, { repositoryDestination } from "@/components/RepositoryEntry";
import { fetchRepos } from "@/lib/api";
import { parseGitHubRepository } from "@/lib/githubPreview";
import { navigate } from "@/lib/router";

/** Resolve a GitHub-shaped URL against the catalog before offering generation. */
const AskPage = lazy(() => import("@/app/[repoId]/ask/page"));

export default function RepositoryRoute({ input, page, askQuery }: { input: string; page?: string; askQuery?: string }) {
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const [legacyAsk, setLegacyAsk] = useState<string | null>(null);
  const repo = parseGitHubRepository(input);
  useEffect(() => {
    if (!repo && askQuery === undefined) return;
    let active = true;
    setError("");
    fetchRepos({ refresh: retry > 0 }).then((repos) => {
      if (!active) return;
      // Bare legacy ids (including exported Wikis) can also have /ask routes.
      const legacyId = input.split("/")[0];
      if (askQuery !== undefined && repos.some(item => item.id === legacyId)) {
        setLegacyAsk(legacyId);
        return;
      }
      if (!repo) return;
      const destination = repositoryDestination(repo.slug, repos)!;
      const chapter = page && !destination.startsWith("/preview/")
        ? `?p=${encodeURIComponent(page)}` : "";
      // Replace the alias so browser Back returns to the visitor's entry page.
      navigate(destination + chapter, true);
    }).catch(() => {
      if (active) setError("We could not check for a ready Wiki. Try again in a moment.");
    });
    return () => { active = false; };
  }, [input, repo?.slug, page, retry, askQuery]);
  if (legacyAsk) return <AskPage repoId={legacyAsk} query={askQuery || ""} />;
  return <><Header /><main className="repository-route">
    <h1>{repo ? `Opening ${repo.slug}` : "Enter a GitHub repository"}</h1>
    {repo && !error && <p role="status">Checking for a ready Wiki…</p>}
    {error && <p role="alert">{error} <button className="btn-outline" onClick={() => setRetry(value => value + 1)}>Retry</button></p>}
    {!repo && <><p>Use a public GitHub URL or owner/repo.</p><RepositoryEntry /></>}
  </main></>;
}
