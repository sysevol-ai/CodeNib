// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

export interface GitHubRepo {
  owner: string;
  name: string;
  slug: string;
}

export function parseGitHubRepository(input: string): GitHubRepo | null {
  let value = input.trim();
  if (!value || value.length > 2048 || /[\\\u0000-\u001f\u007f]/.test(value))
    return null;
  if (/^(?:www\.)?github\.com\//i.test(value)) value = `https://${value}`;
  if (/^https?:\/\//i.test(value)) {
    try {
      const url = new URL(value);
      if (
        !["github.com", "www.github.com"].includes(
          url.hostname.toLowerCase(),
        ) ||
        url.username ||
        url.password ||
        url.port
      )
        return null;
      value = url.pathname.replace(/^\//, "");
    } catch {
      return null;
    }
  } else if (value.includes(":") || value.includes("?") || value.includes("#"))
    return null;
  const parts = value.replace(/\/+$/, "").split("/");
  if (parts.length < 2) return null;
  // A file/tree link still identifies a repository; the preview explicitly
  // shows its default branch and pinned commit, never claims that link's ref.
  if (
    parts.length > 2 &&
    !["tree", "blob", "issues", "pull", "pulls"].includes(parts[2])
  )
    return null;
  const owner = parts[0],
    name = parts[1].replace(/\.git$/i, "");
  if (
    !/^[A-Za-z0-9][A-Za-z0-9-]{0,38}$/.test(owner) ||
    !/^[A-Za-z0-9_.-]{1,100}$/.test(name) ||
    name === "." ||
    name === ".."
  )
    return null;
  return { owner, name, slug: `${owner}/${name}` };
}

export function previewPath(repo: GitHubRepo): string {
  return `/preview/${encodeURIComponent(repo.owner)}/${encodeURIComponent(repo.name)}`;
}
