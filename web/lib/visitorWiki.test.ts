// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  loadSavedWiki,
  checkWikiRepository,
  newWikiAttempt,
  recentWikis,
  startWiki,
  stopWiki,
} from "./visitorWiki";

let stored: Map<string, string>;
beforeEach(() => {
  stored = new Map();
  vi.stubGlobal("window", { location: { origin: "https://demo.example" } });
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => stored.get(key) || null,
    setItem: (key: string, value: string) => stored.set(key, value),
  });
});
afterEach(() => vi.unstubAllGlobals());

it("persists recovery access before generation but never the provider key", async () => {
  const attempt = newWikiAttempt("owner/repo");
  expect(recentWikis()).toEqual([]);
  const key = "sk-or-private-test-value";
  const fetch = vi.fn(async (url: URL, init: RequestInit) => {
    expect(recentWikis()).toEqual([attempt]);
    expect(url.href).toBe(
      `https://demo.example/api/visitor-wikis/${attempt.id}`,
    );
    expect(init.headers).toMatchObject({
      Authorization: `Bearer ${key}`,
      "X-Wiki-Owner": attempt.owner,
    });
    expect(init.redirect).toBe("error");
    expect(init.credentials).toBe("omit");
    return new Response(JSON.stringify({ id: attempt.id }));
  });
  vi.stubGlobal("fetch", fetch);
  await startWiki(attempt, key, 2);
  expect([...stored.values()].join()).not.toContain(key);
  expect(fetch).toHaveBeenCalledTimes(1);
});

it("opening or refreshing a saved Wiki never sends a credential or starts work", async () => {
  const fetch = vi.fn(async (_: unknown, init: RequestInit) => {
    expect(init.method).toBeUndefined();
    expect(init.headers).toBeUndefined();
    return new Response(JSON.stringify({ status: "complete" }));
  });
  vi.stubGlobal("fetch", fetch);
  await loadSavedWiki("a".repeat(64));
  await loadSavedWiki("a".repeat(64));
  expect(fetch).toHaveBeenCalledTimes(2);
});

it("checking a repository sends no credential and stores no attempt", async () => {
  const fetch = vi.fn(async (url: URL, init: RequestInit) => {
    expect(url.searchParams.get("repository")).toBe("owner/repo");
    expect(url.pathname).toBe("/api/visitor-wikis/check");
    expect(init.headers).toBeUndefined();
    expect(init.method).toBeUndefined();
    return new Response(JSON.stringify({ eligible: false, message: "Archive exceeds 20 MiB." }));
  });
  vi.stubGlobal("fetch", fetch);
  expect((await checkWikiRepository("owner/repo")).eligible).toBe(false);
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(stored.size).toBe(0);
});

it("concurrent source checks share one download and a later check is fresh", async () => {
  const fetch = vi.fn(async () => new Response(JSON.stringify({ eligible: true })));
  vi.stubGlobal("fetch", fetch);
  await Promise.all([checkWikiRepository("owner/repo"), checkWikiRepository("OWNER/repo")]);
  expect(fetch).toHaveBeenCalledTimes(1);
  await checkWikiRepository("owner/repo");
  expect(fetch).toHaveBeenCalledTimes(2);
});

it("storage failure prevents a funded request and network errors do not retry", async () => {
  const attempt = newWikiAttempt("owner/repo");
  const fetch = vi.fn().mockRejectedValue(new Error("offline"));
  vi.stubGlobal("fetch", fetch);
  await expect(startWiki(attempt, "key", 2)).rejects.toThrow("offline");
  expect(fetch).toHaveBeenCalledTimes(1);
  vi.stubGlobal("localStorage", {
    setItem: () => {
      throw new Error("disabled");
    },
    getItem: () => null,
  });
  expect(() => startWiki(attempt, "key", 2)).toThrow("browser storage");
  expect(fetch).toHaveBeenCalledTimes(1);
});

it("stop uses only the owner's recovery token and static exports never send it", async () => {
  const attempt = newWikiAttempt("owner/repo");
  const fetch = vi.fn(async (_: unknown, init: RequestInit) => {
    expect(init.headers).toEqual({ "X-Wiki-Owner": attempt.owner });
    return new Response("{}");
  });
  vi.stubGlobal("fetch", fetch);
  await stopWiki(attempt);
  vi.stubGlobal("window", { __CODENIB_RUNTIME__: { mode: "static" } });
  await expect(stopWiki(attempt)).rejects.toThrow("live demo");
  expect(fetch).toHaveBeenCalledTimes(1);
});
