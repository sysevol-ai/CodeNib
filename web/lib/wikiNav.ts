// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import type { WikiPageRef } from "./api";

/** The Overview plus five pages, matching the backend's ranking cap. */
const MAX_UNRANKED_PRIMARY = 6;

export interface WikiNavSections {
  primary: WikiPageRef[];
  more: WikiPageRef[];
}

/** Split the top level into what a reader sees first and what waits.
 *
 * A saved Wiki from before page ranking has no tiers, so it keeps its outline
 * order and the same limit.
 */
export function partitionWikiNav(pages: WikiPageRef[]): WikiNavSections {
  if (!pages.some((page) => page.tier)) {
    return {
      primary: pages.slice(0, MAX_UNRANKED_PRIMARY),
      more: pages.slice(MAX_UNRANKED_PRIMARY),
    };
  }
  return {
    primary: pages.filter((page) => page.tier !== "more"),
    more: pages.filter((page) => page.tier === "more"),
  };
}

/** The ids from a top-level page down to `pageId`, or nothing when absent. */
export function pagePath(pages: WikiPageRef[], pageId: string): string[] {
  for (const page of pages) {
    if (page.id === pageId) return [page.id];
    const below = pagePath(page.children, pageId);
    if (below.length) return [page.id, ...below];
  }
  return [];
}

/** The detail pages one level below `pageId`, for its "Go deeper" links. */
export function childPages(pages: WikiPageRef[], pageId: string): WikiPageRef[] {
  for (const page of pages) {
    if (page.id === pageId) return page.children;
    const found = childPages(page.children, pageId);
    if (found.length) return found;
  }
  return [];
}

export interface VisibleChapter {
  page: WikiPageRef;
  depth: number;
}

/** Flatten only what is open: a saved Wiki's chapter list uses this too.
 *
 * A group stays open while it holds the current page, whatever the reader last
 * chose, so the page being read never vanishes from its own navigation.
 */
export function visibleChapters(
  pages: WikiPageRef[],
  activeId: string,
  open: ReadonlySet<string> = new Set(),
): VisibleChapter[] {
  const active = new Set(pagePath(pages, activeId));
  const walk = (items: WikiPageRef[], depth: number): VisibleChapter[] =>
    items.flatMap((page) => [
      { page, depth },
      ...(open.has(page.id) || active.has(page.id) ? walk(page.children, depth + 1) : []),
    ]);
  return walk(pages, 0);
}
