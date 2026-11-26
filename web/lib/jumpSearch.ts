// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

/** Lower is better: a title that starts with the words beats one that only contains them. */
export function titleScore(title: string, query: string): number | null {
  const text = title.toLowerCase();
  const needle = query.trim().toLowerCase();
  if (!needle) return 0;
  if (text.startsWith(needle)) return 0;
  if (new RegExp(`(^|[\\s(/._-])${needle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`).test(text)) return 1;
  if (text.includes(needle)) return 2;
  const words = needle.split(/\s+/).filter(Boolean);
  return words.length > 1 && words.every((word) => text.includes(word)) ? 3 : null;
}

/** The items whose title matches, best first, in their original order on ties. */
export function rankByTitle<T>(items: T[], title: (item: T) => string, query: string, limit: number): T[] {
  return items
    .map((item, order) => ({ item, order, score: titleScore(title(item), query) }))
    .filter((entry): entry is { item: T; order: number; score: number } => entry.score !== null)
    .sort((a, b) => a.score - b.score || a.order - b.order)
    .slice(0, limit)
    .map((entry) => entry.item);
}
