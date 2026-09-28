// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import type { WikiPage, WikiPageRef } from "./api";
import { apiBase, isStaticRuntime } from "./runtime";

export interface SavedWiki {
  id: string;
  repository: string;
  commit: string;
  status: "queued" | "running" | "partial" | "complete";
  stage: string;
  active_page: string;
  pages: WikiPageRef[];
  page_states: Record<string, "running" | "pending" | "ready" | "needs_review">;
  skipped_files?: Array<{ path: string; size_bytes: number }>;
  message: string;
  created_at: number;
  updated_at: number;
  reported_cost_usd: number;
  calls: number;
  unreported_call_cost: boolean;
  budget_usd: number;
  stalled: boolean;
  history: Array<{ stage: string; page: string; at: number }>;
  model?: string;
  scope?: "concise" | "focused";
  published?: boolean;
  languages?: string[];
  source_files?: number;
  request_active?: boolean;
  response_chars?: number;
  request_started_at?: number;
}

export interface WikiAttempt {
  id: string;
  owner: string;
  repository: string;
}

export interface PublicWiki {
  id: string;
  repository: string;
  commit: string;
  summary: string;
  chapters: number;
  languages: string[];
  published_at: number;
  published: boolean;
}

const STORAGE = "codenib-wiki-attempts-v1";
const token = /^[a-f0-9]{64}$/;

export function recentWikis(): WikiAttempt[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(STORAGE) || "[]");
    return Array.isArray(value)
      ? value
          .filter(
            (item): item is WikiAttempt =>
              item &&
              token.test(item.id) &&
              token.test(item.owner) &&
              typeof item.repository === "string" &&
              /^[A-Za-z0-9-]+\/[A-Za-z0-9_.-]+$/.test(item.repository),
          )
          .slice(0, 20)
      : [];
  } catch {
    return [];
  }
}

export function newWikiAttempt(repository: string): WikiAttempt {
  const random = () =>
    Array.from(crypto.getRandomValues(new Uint8Array(32)), (byte) =>
      byte.toString(16).padStart(2, "0"),
    ).join("");
  const attempt = { id: random(), owner: random(), repository };
  return attempt;
}

export function rememberWikiAttempt(attempt: WikiAttempt): void {
  // Save the recovery capability before submitting a key. If this fails no
  // funded request is sent; the user can enable storage and try again.
  try {
    localStorage.setItem(
      STORAGE,
      JSON.stringify(
        [
          attempt,
          ...recentWikis().filter((item) => item.id !== attempt.id),
        ].slice(0, 20),
      ),
    );
  } catch {
    throw new Error(
      "Allow browser storage to save your Wiki recovery access, then try again.",
    );
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  if (isStaticRuntime())
    throw new Error("Open the live demo to generate a saved Wiki.");
  const url = new URL(
    `${apiBase()}/api/visitor-wikis${path}`,
    window.location.origin,
  );
  if (
    url.protocol !== "https:" &&
    !["localhost", "127.0.0.1"].includes(url.hostname)
  )
    throw new Error("Wiki generation requires an HTTPS connection.");
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 35000);
  try {
    const response = await fetch(url, {
      ...init,
      signal: controller.signal,
      credentials: "omit",
      redirect: "error",
      cache: "no-store",
      referrerPolicy: "no-referrer",
    });
    const body = await response.json();
    if (!response.ok)
      throw new Error(
        typeof body.detail === "string"
          ? body.detail
          : "The saved Wiki request failed.",
      );
    return body as T;
  } catch (error) {
    if (controller.signal.aborted)
      throw new Error(
        "The server did not respond in time. Your saved Wiki link can recover an accepted attempt.",
      );
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

export const wikiGenerationAvailable = () => request<{ enabled: boolean }>("");
export const loadPublicWikis = () => request<PublicWiki[]>("/public");
export const publishWiki = (attempt: WikiAttempt, published: boolean) =>
  request<{ published: boolean }>(`/${attempt.id}/publication`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Wiki-Owner": attempt.owner,
    },
    body: JSON.stringify({ published }),
  });
export const loadSavedWiki = (id: string) =>
  request<SavedWiki>(`/${encodeURIComponent(id)}`);
export const loadSavedWikiPage = (id: string, page: string) =>
  request<WikiPage>(
    `/${encodeURIComponent(id)}/pages/${encodeURIComponent(page)}`,
  );
export const startWiki = (
  attempt: WikiAttempt,
  key: string,
  budget: number,
  model = "anthropic/claude-sonnet-4.6",
) => {
  rememberWikiAttempt(attempt);
  return request<SavedWiki>(`/${attempt.id}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${key}`,
      "X-Wiki-Owner": attempt.owner,
    },
    body: JSON.stringify({
      repository: attempt.repository,
      budget_usd: budget,
      model,
    }),
  });
};
export const stopWiki = (attempt: WikiAttempt) =>
  request(`/${attempt.id}/stop`, {
    method: "POST",
    headers: { "X-Wiki-Owner": attempt.owner },
  });

export const wikiStages: Record<string, string> = {
  queued: "Waiting to start",
  connecting: "Connecting to your model account",
  downloading: "Downloading the repository",
  analyzing: "Analyzing source files",
  outline: "Planning the Wiki chapters",
  retrieving: "Finding relevant source",
  planning_page: "Drafting and checking this chapter",
  writing: "Assembling this chapter",
  editing: "Reviewing the explanation",
  checking: "Checking citations and saving",
  complete: "Generation finished",
  paused: "Generation paused",
};

export function allWikiPages(pages: WikiPageRef[]): WikiPageRef[] {
  return pages.flatMap((page) => [page, ...allWikiPages(page.children)]);
}
