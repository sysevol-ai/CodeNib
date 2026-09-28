// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

// Link a repo-relative source path to the exact blob on GitHub at the indexed commit.
export function ghFileUrl(
  repo: string | undefined,
  sourceUrl: string | null | undefined,
  commit: string | undefined,
  file: string,
  start?: number | null,
  end?: number | null
): string | null {
  if (!repo) return null;
  const lines = start ? `#L${start}${end && end !== start ? `-L${end}` : ""}` : "";
  const root = (sourceUrl || `https://github.com/${repo}`).replace(/\/+$/, "");
  return `${root}/blob/${commit || "HEAD"}/${file}${lines}`;
}
