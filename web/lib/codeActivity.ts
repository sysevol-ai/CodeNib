// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import type { CodeActivityItem, CodeActivityOverlay } from "./api";

export type ActivityKind = "issues" | "pulls" | "changes";

export interface ActivityGroup {
  kind: ActivityKind;
  label: string;
  /** Items that reach a symbol the page cites. */
  direct: CodeActivityItem[];
  /** Items that only reach a file the page cites. */
  nearby: CodeActivityItem[];
  /** Some issue or pull request here is closed, so the group is not "open". */
  anyClosed: boolean;
}

const LABELS: Record<ActivityKind, string> = {
  issues: "Open issues",
  pulls: "Open pull requests",
  changes: "Recent changes",
};

/** The page's activity, grouped by kind; empty when there is none. */
export function pageActivity(
  overlay: CodeActivityOverlay | null | undefined,
  pageId: string,
): ActivityGroup[] {
  const page = overlay?.pages?.[pageId];
  if (!page) return [];
  const groups: ActivityGroup[] = [];
  for (const kind of ["issues", "pulls", "changes"] as ActivityKind[]) {
    const items = (page[kind] ?? []).filter(
      (item) => item && typeof item.title === "string" && typeof item.number === "number",
    );
    if (!items.length) continue;
    // The overlay ranks items to choose which fit; a reader scans by time.
    const newest = [...items].sort((a, b) => timeOf(b.at) - timeOf(a.at));
    const anyClosed = kind !== "changes" && items.some((item) => !isOpenItem(item));
    groups.push({
      kind,
      label: (anyClosed && WITH_CLOSED[kind]) || LABELS[kind],
      anyClosed,
      direct: newest.filter((item) => item.strength === "symbol"),
      nearby: newest.filter((item) => item.strength !== "symbol"),
    });
  }
  return groups;
}

/** An overlay made with --include-closed carries closed threads too. */
export function isOpenItem(item: CodeActivityItem): boolean {
  return item.state == null || item.state === "open";
}

const WITH_CLOSED: Partial<Record<ActivityKind, string>> = {
  issues: "Issues",
  pulls: "Pull requests",
};

function timeOf(at: string): number {
  const time = new Date(at).getTime();
  return Number.isNaN(time) ? 0 : time;
}

const SOURCES: Record<ActivityKind, string> = {
  issues: "open issues",
  pulls: "open pull requests",
  changes: "merged changes",
};

/** "Open issues and merged changes", naming only the kinds the page has. */
export function activitySources(groups: ActivityGroup[]): string {
  const names = groups.map((group) =>
    group.anyClosed ? SOURCES[group.kind].replace(/^open /, "") : SOURCES[group.kind],
  );
  const text =
    names.length > 1 ? `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}` : names[0] ?? "";
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** Text split at backticks: odd parts were written as inline code. */
export function codeSpans(text: string): { text: string; code: boolean }[] {
  const parts = text.split("`");
  if (parts.length % 2 === 0) return [{ text, code: false }]; // unbalanced
  return parts
    .map((part, index) => ({ text: part, code: index % 2 === 1 }))
    .filter((part) => part.text);
}

/** A merged change's subject without the "(#123)" GitHub appends. */
export function activityTitle(item: CodeActivityItem): string {
  return item.title.replace(/\s*\(#\d+\)\s*$/, "").trim() || item.title;
}

/** What an item reaches on this page, in a few words. */
export interface ActivityReach {
  verb: string;
  /** A symbol or file name, shown as code. */
  name: string;
  more: string;
}

/** What an item reaches on this page; null when it names nothing. */
export function activityReach(
  item: CodeActivityItem,
  kind: ActivityKind,
): ActivityReach | null {
  if (item.strength === "symbol" && item.symbols.length) {
    const rest = item.symbols.length - 1;
    return {
      verb: kind === "issues" ? "Names" : "Changes",
      name: item.symbols[0],
      more: rest > 0 ? `and ${rest} more` : "",
    };
  }
  const file = item.files[0];
  if (!file) return null;
  const rest = item.files.length - 1;
  return {
    verb: kind === "issues" ? "Points at" : "Edits",
    name: file.split("/").pop() || file,
    more: rest > 0 ? `and ${rest} more ${rest === 1 ? "file" : "files"}` : "",
  };
}

export function reachText(reach: ActivityReach | null): string {
  return reach ? [reach.verb, reach.name, reach.more].filter(Boolean).join(" ") : "";
}

/** "Jul 23" this year, "Jul 23, 2025" otherwise; "" for an unreadable time. */
export function activityDate(at: string, now: Date = new Date()): string {
  const when = new Date(at);
  if (Number.isNaN(when.getTime())) return "";
  return when.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    ...(when.getFullYear() === now.getFullYear() ? {} : { year: "numeric" }),
  });
}

/** A GitHub link only; anything else in the overlay is not followed. */
export function activityHref(item: CodeActivityItem): string | undefined {
  return item.url && /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/(issues|pull)\/\d+$/.test(item.url)
    ? item.url
    : undefined;
}

// --- Marks on the Wiki map ---------------------------------------------------

/** A page is marked for merged changes only when this many reach its symbols. */
export const MIN_CHANGES_TO_MARK = 2;

export interface ActivityMark {
  issues: number;
  pulls: number;
  /** Merged changes that reach a symbol the page cites. */
  changes: number;
}

/**
 * Pages worth marking on the map. Any open issue or pull request is marked;
 * merged changes are marked only where several reach the page's symbols, so
 * the map shows where the code is moving rather than every page.
 */
export function activityMarks(
  overlay: CodeActivityOverlay | null | undefined,
): Record<string, ActivityMark> {
  const marks: Record<string, ActivityMark> = {};
  for (const [pageId, page] of Object.entries(overlay?.pages ?? {})) {
    const count = (items: CodeActivityItem[] | undefined, symbolOnly = false) =>
      (items ?? []).filter((item) => item && (!symbolOnly || item.strength === "symbol")).length;
    // The mark says "open", so closed threads do not count toward it.
    const open = (items: CodeActivityItem[] | undefined) => count(items?.filter(isOpenItem));
    const mark = {
      issues: open(page?.issues),
      pulls: open(page?.pulls),
      changes: count(page?.changes, true),
    };
    if (mark.issues || mark.pulls || mark.changes >= MIN_CHANGES_TO_MARK) marks[pageId] = mark;
  }
  return marks;
}

export function isOpenMark(mark: ActivityMark): boolean {
  return mark.issues + mark.pulls > 0;
}

/**
 * The mark's radius in map units, from the node it sits on. A mark sized by
 * activity covered small page nodes; how much activity is in the status line.
 */
export function markRadius(nodeRadius: number): number {
  return Math.min(3.2, Math.max(2.2, nodeRadius * 0.45));
}

/** "2 open issues, 1 open pull request and 3 recent changes on its code". */
export function markSummary(mark: ActivityMark): string {
  const parts = [
    mark.issues ? `${mark.issues} open ${mark.issues === 1 ? "issue" : "issues"}` : "",
    mark.pulls ? `${mark.pulls} open ${mark.pulls === 1 ? "pull request" : "pull requests"}` : "",
    mark.changes ? `${mark.changes} recent ${mark.changes === 1 ? "change" : "changes"}` : "",
  ].filter(Boolean);
  if (!parts.length) return "";
  const text =
    parts.length > 1 ? `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}` : parts[0];
  return `${text} on its code`;
}
