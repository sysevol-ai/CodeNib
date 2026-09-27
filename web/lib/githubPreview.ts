// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

/** A bounded, read-only public GitHub preview. No checkout or CodeNib API. */
const API = "https://api.github.com";
const SHA = /^[a-f0-9]{40}$/;
const MAX_FILE_BYTES = 256 * 1024;
type JSONRecord = Record<string, unknown>;

export interface GitHubRepo {
  owner: string;
  name: string;
  slug: string;
}

export interface PreviewEntry {
  path: string;
  sha: string;
  type: "blob" | "tree";
  size: number;
}

export interface RepositoryPreview {
  repository: GitHubRepo;
  description: string;
  language: string;
  branch: string;
  commit: string;
  entries: PreviewEntry[];
  partial: boolean;
}

export interface PreviewFile {
  path: string;
  content: string;
  lineCount: number;
  url: string;
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

function object(value: unknown): JSONRecord {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("GitHub returned invalid repository data.");
  return value as JSONRecord;
}

function boundedText(value: unknown, limit: number): string {
  if (typeof value !== "string" || value.length > limit)
    throw new Error("GitHub returned invalid repository data.");
  return value;
}

class ResponseTooLarge extends Error {}

async function githubJSON(
  path: string,
  signal: AbortSignal,
  maximum = 256 * 1024,
): Promise<JSONRecord> {
  const response = await fetch(`${API}${path}`, {
    signal,
    credentials: "omit",
    redirect: "error",
    referrerPolicy: "no-referrer",
    headers: { Accept: "application/vnd.github+json" },
  }).catch(() => {
    throw new Error(
      signal.aborted
        ? "Repository request cancelled."
        : "Could not reach GitHub. Check your connection and try again.",
    );
  });
  if (!response.ok) {
    await response.body?.cancel();
    if (response.status === 404)
      throw new Error(
        "This public repository was not found. Check the URL; private repositories are not supported in this preview.",
      );
    if (response.status === 403 || response.status === 429)
      throw new Error(
        "GitHub is limiting requests from this connection. Browse a ready Wiki from the home page or return later.",
      );
    if (response.status === 409)
      throw new Error("This repository has no source commit to preview yet.");
    throw new Error(
      `GitHub could not load this repository (HTTP ${response.status}).`,
    );
  }
  if (!response.body) throw new Error("GitHub returned an empty response.");
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    while (true) {
      const next = await reader.read();
      if (next.done) break;
      length += next.value.length;
      if (length > maximum)
        throw new ResponseTooLarge(
          "This GitHub response is too large for the quick preview.",
        );
      chunks.push(next.value);
    }
  } finally {
    await reader.cancel().catch(() => {});
  }
  if (signal.aborted) throw new Error("Repository request cancelled.");
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.length;
  }
  try {
    return object(
      JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)),
    );
  } catch {
    throw new Error("GitHub returned invalid repository data.");
  }
}

function repoAPI(repo: GitHubRepo): string {
  return `/repos/${encodeURIComponent(repo.owner)}/${encodeURIComponent(repo.name)}`;
}

function entriesFrom(
  raw: JSONRecord,
  prefix = "",
): { entries: PreviewEntry[]; partial: boolean } {
  if (!Array.isArray(raw.tree))
    throw new Error("GitHub returned an invalid file tree.");
  const entries: PreviewEntry[] = [];
  for (const item of raw.tree.slice(0, 10000)) {
    const node = object(item);
    if (
      (node.type !== "blob" ||
        !["100644", "100755"].includes(String(node.mode))) &&
      (node.type !== "tree" || node.mode !== "040000")
    )
      continue;
    const path = boundedText(node.path, 2048),
      sha = boundedText(node.sha, 40);
    if (
      !path ||
      /[\\\u0000-\u001f\u007f]/.test(path) ||
      path.split("/").some((p) => !p || p === "." || p === "..") ||
      !SHA.test(sha)
    )
      throw new Error("GitHub returned an invalid source path.");
    const size = node.type === "blob" ? node.size : 0;
    if (typeof size !== "number" || !Number.isSafeInteger(size) || size < 0)
      throw new Error("GitHub returned an invalid file size.");
    entries.push({
      path: prefix ? `${prefix}/${path}` : path,
      sha,
      type: node.type,
      size,
    });
  }
  return {
    entries,
    partial: raw.truncated === true || raw.tree.length > 10000,
  };
}

export async function loadRepositoryPreview(
  input: GitHubRepo,
  signal: AbortSignal,
  onRepository?: (repo: { slug: string; description: string }) => void,
): Promise<RepositoryPreview> {
  const metadata = await githubJSON(repoAPI(input), signal);
  const repository = parseGitHubRepository(
    boundedText(metadata.full_name, 140),
  );
  if (!repository || metadata.private !== false)
    throw new Error("Only public GitHub repositories can be previewed here.");
  const description =
    metadata.description == null ? "" : boundedText(metadata.description, 4096);
  onRepository?.({ slug: repository.slug, description });
  const branch = boundedText(metadata.default_branch, 1024);
  const branchData = await githubJSON(
    `${repoAPI(repository)}/branches/${encodeURIComponent(branch)}`,
    signal,
  );
  const revision = object(branchData.commit);
  const commit = boundedText(revision.sha, 40);
  const tree = object(object(revision.commit).tree);
  const treeSha = boundedText(tree.sha, 40);
  if (!SHA.test(commit) || !SHA.test(treeSha))
    throw new Error("GitHub did not return a pinned source revision.");
  let listing: JSONRecord;
  let partial = false;
  try {
    listing = await githubJSON(
      `${repoAPI(repository)}/git/trees/${treeSha}?recursive=1`,
      signal,
      3 * 1024 * 1024,
    );
  } catch (error) {
    if (!(error instanceof ResponseTooLarge)) throw error;
    // Large repositories still get a useful root immediately. Child folders
    // can be opened on demand using their immutable tree identities.
    listing = await githubJSON(
      `${repoAPI(repository)}/git/trees/${treeSha}`,
      signal,
      1024 * 1024,
    );
    partial = true;
  }
  if (listing.sha !== treeSha)
    throw new Error("GitHub returned a different source tree.");
  const parsed = entriesFrom(listing);
  return {
    repository,
    description,
    branch,
    commit,
    language:
      metadata.language == null ? "" : boundedText(metadata.language, 100),
    entries: parsed.entries,
    partial: partial || parsed.partial,
  };
}

export async function loadPreviewFolder(
  snapshot: RepositoryPreview,
  entry: PreviewEntry,
  signal: AbortSignal,
): Promise<PreviewEntry[]> {
  if (
    entry.type !== "tree" ||
    !snapshot.entries.some((e) => e.path === entry.path && e.sha === entry.sha)
  )
    throw new Error("This folder is outside the loaded source tree.");
  const data = await githubJSON(
    `${repoAPI(snapshot.repository)}/git/trees/${entry.sha}`,
    signal,
    1024 * 1024,
  );
  if (data.sha !== entry.sha)
    throw new Error("GitHub returned a different source tree.");
  return entriesFrom(data, entry.path).entries;
}

export function sourceURL(
  snapshot: RepositoryPreview,
  path: string,
  line?: number,
): string {
  const encoded = path.split("/").map(encodeURIComponent).join("/");
  return `https://github.com/${snapshot.repository.slug}/blob/${snapshot.commit}/${encoded}${line ? `#L${line}` : ""}`;
}

export async function readPreviewFile(
  snapshot: RepositoryPreview,
  entry: PreviewEntry,
  signal: AbortSignal,
): Promise<PreviewFile> {
  if (
    entry.type !== "blob" ||
    !snapshot.entries.some((e) => e.path === entry.path && e.sha === entry.sha)
  )
    throw new Error("This file is outside the loaded source tree.");
  if (entry.size > MAX_FILE_BYTES)
    throw new Error(
      "This file is too large for the quick preview. Open its pinned source on GitHub.",
    );
  const data = await githubJSON(
    `${repoAPI(snapshot.repository)}/git/blobs/${entry.sha}`,
    signal,
    MAX_FILE_BYTES * 2,
  );
  if (
    data.sha !== entry.sha ||
    data.encoding !== "base64" ||
    data.size !== entry.size
  )
    throw new Error("GitHub returned a different source file.");
  const encoded = boundedText(data.content, MAX_FILE_BYTES * 2).replace(
    /\s/g,
    "",
  );
  let bytes: Uint8Array;
  try {
    bytes = Uint8Array.from(atob(encoded), (c) => c.charCodeAt(0));
  } catch {
    throw new Error("GitHub returned invalid source encoding.");
  }
  if (bytes.length !== entry.size)
    throw new Error("GitHub returned incomplete source bytes.");
  const header = new TextEncoder().encode(`blob ${bytes.length}\0`);
  const blob = new Uint8Array(header.length + bytes.length);
  blob.set(header);
  blob.set(bytes, header.length);
  const sha = [...new Uint8Array(await crypto.subtle.digest("SHA-1", blob))]
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
  if (sha !== entry.sha)
    throw new Error("Source integrity verification failed.");
  let content: string;
  try {
    content = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new Error("This binary file cannot be shown as text.");
  }
  if (content.includes("\0"))
    throw new Error("This binary file cannot be shown as text.");
  return {
    path: entry.path,
    content,
    lineCount: content.split("\n").length,
    url: sourceURL(snapshot, entry.path),
  };
}

export function previewReadme(
  entries: PreviewEntry[],
): PreviewEntry | undefined {
  return entries.find(
    (e) =>
      e.type === "blob" &&
      /^readme(?:\.(?:md|rst|txt|markdown))?$/i.test(e.path),
  );
}

export function previewSources(entries: PreviewEntry[]): PreviewEntry[] {
  const candidates = entries.filter(
    (e) =>
      e.type === "blob" &&
      e.size <= MAX_FILE_BYTES &&
      /\.(?:py|ts|tsx|js|jsx|go|rs|c|h|cpp|java|rb|php|kt|scala|swift|lua)$/.test(
        e.path,
      ) &&
      !/(?:^|\/)(?:tests?|__tests__|vendor|node_modules|third_party|dist|build|\.github)(?:\/|$)/.test(
        e.path,
      ) &&
      !/\.(?:min|test|spec)\./.test(e.path),
  );
  const priority = (e: PreviewEntry) =>
    /\/(?:main|index|lib|app|server)\./.test("/" + e.path)
      ? 0
      : /^(?:src|lib|app)\//.test(e.path)
        ? 1
        : 2;
  return candidates
    .sort(
      (a, b) =>
        priority(a) - priority(b) ||
        a.path.split("/").length - b.path.split("/").length ||
        a.path.localeCompare(b.path),
    )
    .slice(0, 4);
}
