// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { githubPathInput, parseGitHubRepository, previewPath } from "./githubPreview";
import { repositoryDestination } from "../components/RepositoryEntry";

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
  it("opens a prepared Wiki by identity and starts Wikis absent from the catalog", () => {
    expect(
      repositoryDestination("https://github.com/OWNER/repo.git", [
        { repo: "owner/repo", id: "pinned-version" },
      ]),
    ).toBe("/pinned-version");
    expect(repositoryDestination("owner/new", [])).toBe("/preview/owner/new");
    expect(repositoryDestination("invalid", [])).toBeNull();
    expect(previewPath(parseGitHubRepository("owner/repo")!)).toBe("/preview/owner/repo");
  });
});

describe("githubPathInput", () => {
  it("reads GitHub addresses pasted after the domain", () => {
    expect(githubPathInput(["github.com", "psf", "requests"])).toBe("github.com/psf/requests");
    expect(githubPathInput(["https:", "github.com", "psf", "requests"])).toBe(
      "https://github.com/psf/requests",
    );
    expect(githubPathInput(["psf", "requests", "tree", "main", "src"])).toBe(
      "psf/requests/tree/main/src",
    );
    expect(parseGitHubRepository(githubPathInput(["https:", "github.com", "psf", "requests", "blob", "main", "x.py"])!)?.slug).toBe(
      "psf/requests",
    );
  });

  it("leaves the site's own routes alone", () => {
    expect(githubPathInput(["psf", "requests"])).toBeNull();
    expect(githubPathInput(["psf__requests", "ask"])).toBeNull();
    expect(githubPathInput(["preview", "pallets", "click"])).toBeNull();
    expect(githubPathInput(["psf__requests", "p", "tree"])).toBeNull();
  });
});
