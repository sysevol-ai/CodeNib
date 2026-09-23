// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import type { RepoInfo } from "./api";

const LANGUAGE_NAMES: Record<string, string> = {
  python: "Python",
  cpp: "C/C++",
  c: "C",
  go: "Go",
  rust: "Rust",
  typescript: "TypeScript",
  javascript: "JavaScript",
  java: "Java",
};

/** The repository's main language, named the way people write it. The
 *  index lists every chunker it used, which made C projects read as
 *  "cpp/python/javascript". */
export function primaryLanguage(r: Pick<RepoInfo, "language">): string {
  const first = (r.language || "").split("/")[0].trim().toLowerCase();
  return LANGUAGE_NAMES[first] || (first ? first[0].toUpperCase() + first.slice(1) : "Code");
}

/** Group repositories by main language, largest group first. */
export function groupByLanguage(repos: RepoInfo[]): { language: string; repos: RepoInfo[] }[] {
  const groups = new Map<string, RepoInfo[]>();
  for (const repo of repos) {
    const language = primaryLanguage(repo);
    groups.set(language, [...(groups.get(language) || []), repo]);
  }
  return [...groups.entries()]
    .map(([language, items]) => ({ language, repos: items }))
    .sort((a, b) => b.repos.length - a.repos.length || a.language.localeCompare(b.language));
}
