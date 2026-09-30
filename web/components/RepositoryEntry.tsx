// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { useId, useState } from "react";
import type { RepoInfo } from "@/lib/api";
import { parseGitHubRepository, previewPath } from "@/lib/githubPreview";
import { AppLink, navigate } from "@/lib/router";
import { recordExperience } from "@/lib/experience";
import WikiRepositoryRules from "./WikiRepositoryRules";

export function repositoryDestination(
  input: string,
  repos: Pick<RepoInfo, "repo" | "id">[],
): string | null {
  const repo = parseGitHubRepository(input);
  if (!repo) return null;
  const ready = repos.find(
    (item) => item.repo.toLowerCase() === repo.slug.toLowerCase(),
  );
  return ready ? `/${encodeURIComponent(ready.id)}` : previewPath(repo);
}

export default function RepositoryEntry({
  repos = [],
}: {
  repos?: RepoInfo[];
}) {
  const id = useId();
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  return (
    <div className="repository-entry">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          const repo = parseGitHubRepository(value);
          if (!repo) {
            setError("Paste a public GitHub URL or enter owner/repo.");
            return;
          }
          setError("");
          recordExperience("repository_submit");
          // A still-loading catalog is not evidence that a Wiki is missing.
          // The alias resolver shares the in-flight catalog request first.
          navigate(`/${encodeURIComponent(repo.owner)}/${encodeURIComponent(repo.name)}`);
        }}
      >
        <label htmlFor={id}>Explore your own repository</label>
        <div className="repository-entry-row">
          <input
            id={id}
            type="text"
            inputMode="url"
            autoComplete="off"
            spellCheck={false}
            maxLength={2048}
            value={value}
            onChange={(event) => {
              setValue(event.target.value);
              setError("");
            }}
            placeholder="https://github.com/owner/repo"
            aria-invalid={Boolean(error)}
            aria-describedby={`${id}-hint`}
            required
          />
          <button className="btn-primary" type="submit">
            Open Wiki <span aria-hidden>→</span>
          </button>
        </div>
        <p
          id={`${id}-hint`}
          className={error ? "trial-error" : "small muted"}
          role={error ? "alert" : undefined}
        >
          {error ||
            "Ready Wikis open instantly. A new Wiki uses your OpenRouter account; you review the cost limit before starting."}
        </p>
      </form>
      <WikiRepositoryRules />
      <div className="repository-examples">
        <span className="small muted">
          {repos.length ? "Ready Wikis:" : "Try a repository:"}
        </span>
        {repos.length
          ? repos.slice(0, 3).map((repo) => (
              <AppLink key={repo.id} href={`/${encodeURIComponent(repo.id)}`}>
                {repo.repo}
              </AppLink>
            ))
          : ["psf/requests", "pallets/flask", "fastapi/fastapi"].map((slug) => (
              <AppLink
                key={slug}
                href={previewPath(parseGitHubRepository(slug)!)}
              >
                {slug}
              </AppLink>
            ))}
      </div>
    </div>
  );
}
