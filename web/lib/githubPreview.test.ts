// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { createHash } from "node:crypto";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  loadPreviewFolder,
  loadRepositoryPreview,
  parseGitHubRepository,
  previewPath,
  previewReadme,
  readPreviewFile,
  sourceURL,
  type RepositoryPreview,
} from "./githubPreview";
import { repositoryDestination } from "../components/RepositoryEntry";

const COMMIT = "a".repeat(40),
  TREE = "b".repeat(40),
  FOLDER = "c".repeat(40);
const CONTENT = "# Example\n\nA small public repository.\n";
const BLOB = createHash("sha1")
  .update(`blob ${Buffer.byteLength(CONTENT)}\0${CONTENT}`)
  .digest("hex");
const entry = {
  path: "README.md",
  type: "blob" as const,
  sha: BLOB,
  size: Buffer.byteLength(CONTENT),
};
const snapshot: RepositoryPreview = {
  repository: parseGitHubRepository("owner/repo")!,
  description: "Example",
  language: "Python",
  branch: "main",
  commit: COMMIT,
  entries: [entry],
  partial: false,
};
const reply = (data: unknown) => new Response(JSON.stringify(data));
afterEach(() => vi.unstubAllGlobals());

function mockGitHub(options: { large?: boolean; corrupt?: boolean } = {}) {
  const fetch = vi.fn(async (url: string, init: RequestInit) => {
    expect(new URL(url).origin).toBe("https://api.github.com");
    expect(init.credentials).toBe("omit");
    expect(init.redirect).toBe("error");
    expect(init.headers).not.toHaveProperty("Authorization");
    if (url.endsWith("/repos/owner/repo"))
      return reply({
        full_name: "owner/repo",
        private: false,
        default_branch: "main",
        description: "Example",
        language: "Python",
      });
    if (url.endsWith("/branches/main"))
      return reply({
        commit: { sha: COMMIT, commit: { tree: { sha: TREE } } },
      });
    if (url.includes("/git/trees/")) {
      if (options.large && url.endsWith("?recursive=1"))
        return new Response(" ".repeat(3 * 1024 * 1024 + 1));
      return reply({
        sha: url.includes(FOLDER) ? FOLDER : TREE,
        truncated: false,
        tree: [
          { ...entry, mode: "100644" },
          { path: "src", type: "tree", sha: FOLDER, mode: "040000" },
          {
            path: "linked-secret",
            type: "blob",
            mode: "120000",
            sha: BLOB,
            size: 99,
          },
        ],
      });
    }
    if (url.endsWith(`/git/blobs/${BLOB}`))
      return reply({
        sha: BLOB,
        encoding: "base64",
        size: entry.size,
        content: Buffer.from(
          options.corrupt ? CONTENT.replace("small", "large") : CONTENT,
        ).toString("base64"),
      });
    throw new Error(`Unexpected GitHub request: ${url}`);
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

describe("repository URL entry", () => {
  it.each([
    "owner/repo",
    "https://github.com/owner/repo",
    " github.com/owner/repo.git/ ",
    "https://www.github.com/owner/repo/tree/v1/src",
    "https://github.com/owner/repo/blob/main/app.py#L1",
  ])("normalizes %s", (value) => {
    expect(parseGitHubRepository(value)?.slug).toBe("owner/repo");
  });
  it.each([
    "",
    "owner",
    "https://github.com.evil.test/owner/repo",
    "https://user:secret@github.com/owner/repo",
    "https://github.com:444/owner/repo",
    "file:///owner/repo",
    "owner/repo\\bad",
    "owner/..",
    "owner/repo\n/secret",
    "owner/repo/not-a-github-route",
    "owner/repo?token=secret",
  ])("rejects %s", (value) => {
    expect(parseGitHubRepository(value)).toBeNull();
  });
  it("opens a prepared Wiki by identity and previews repositories absent from the catalog", () => {
    expect(
      repositoryDestination("https://github.com/OWNER/repo.git", [
        { repo: "owner/repo", id: "pinned-version" },
      ]),
    ).toBe("/pinned-version");
    expect(repositoryDestination("owner/new", [])).toBe("/preview/owner/new");
    expect(repositoryDestination("invalid", [])).toBeNull();
    expect(previewPath(snapshot.repository)).toBe("/preview/owner/repo");
  });
});

describe("public source preview", () => {
  it("pins one default-branch revision, skips symlinks and verifies Git blob bytes", async () => {
    const fetch = mockGitHub();
    const progress = vi.fn();
    const loaded = await loadRepositoryPreview(
      snapshot.repository,
      new AbortController().signal,
      progress,
    );
    expect(progress).toHaveBeenCalledWith({
      slug: "owner/repo",
      description: "Example",
    });
    expect(loaded.commit).toBe(COMMIT);
    expect(loaded.entries.map((item) => item.path)).toEqual([
      "README.md",
      "src",
    ]);
    const file = await readPreviewFile(
      loaded,
      previewReadme(loaded.entries)!,
      new AbortController().signal,
    );
    expect(file.content).toBe(CONTENT);
    expect(file.url).toContain(`/blob/${COMMIT}/README.md`);
    expect(fetch).toHaveBeenCalledTimes(4);
    expect(sourceURL(loaded, "src/space name.py", 2)).toContain(
      "space%20name.py#L2",
    );
  });
  it("falls back to a bounded root tree and expands only a known folder", async () => {
    const fetch = mockGitHub({ large: true });
    const loaded = await loadRepositoryPreview(
      snapshot.repository,
      new AbortController().signal,
    );
    expect(loaded.partial).toBe(true);
    const children = await loadPreviewFolder(
      loaded,
      loaded.entries[1],
      new AbortController().signal,
    );
    expect(children[0].path).toBe("src/README.md");
    expect(fetch).toHaveBeenCalledTimes(5);
    await expect(
      loadPreviewFolder(
        loaded,
        { ...loaded.entries[1], path: "unknown" },
        new AbortController().signal,
      ),
    ).rejects.toThrow("outside");
    expect(fetch).toHaveBeenCalledTimes(5);
  });
  it("rejects altered bytes even if the API repeats the expected blob identity", async () => {
    mockGitHub({ corrupt: true });
    await expect(
      readPreviewFile(snapshot, entry, new AbortController().signal),
    ).rejects.toThrow("integrity");
  });
  it("does not request unknown or oversized files", async () => {
    const fetch = mockGitHub();
    await expect(
      readPreviewFile(
        snapshot,
        { ...entry, path: "elsewhere" },
        new AbortController().signal,
      ),
    ).rejects.toThrow("outside");
    await expect(
      readPreviewFile(
        snapshot,
        { ...entry, size: 300000 },
        new AbortController().signal,
      ),
    ).rejects.toThrow("too large");
    expect(fetch).not.toHaveBeenCalled();
  });
  it.each([
    [404, "not found"],
    [403, "limiting"],
    [409, "no source commit"],
  ])(
    "handles HTTP %s without displaying upstream content",
    async (status, message) => {
      vi.stubGlobal(
        "fetch",
        vi.fn(
          async () =>
            new Response("upstream-secret", { status: Number(status) }),
        ),
      );
      await expect(
        loadRepositoryPreview(
          snapshot.repository,
          new AbortController().signal,
        ),
      ).rejects.toThrow(String(message));
    },
  );
  it("refuses a tree belonging to a different revision", async () => {
    const fetch = mockGitHub();
    const normal = fetch.getMockImplementation()!;
    fetch.mockImplementation(async (url, init) =>
      url.includes("/git/trees/")
        ? reply({ sha: COMMIT, tree: [] })
        : normal(url, init),
    );
    await expect(
      loadRepositoryPreview(snapshot.repository, new AbortController().signal),
    ).rejects.toThrow("different source tree");
  });
  it("does not publish repository data after cancellation", async () => {
    const abort = new AbortController();
    const fetch = mockGitHub();
    const normal = fetch.getMockImplementation()!;
    fetch.mockImplementation(async (url, init) => {
      const response = await normal(url, init);
      abort.abort();
      return response;
    });
    const progress = vi.fn();
    await expect(
      loadRepositoryPreview(snapshot.repository, abort.signal, progress),
    ).rejects.toThrow("cancelled");
    expect(progress).not.toHaveBeenCalled();
  });
});
