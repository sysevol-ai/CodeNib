// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0
// Rows the explorer draws for one step: the function's signature, a gap row
// for the lines skipped, then the excerpt, dedented together so nesting stays
// visible. Shared by the page and scripts/highlight_first_visit_example.mjs.

export const GAP = "⋯";

export function excerptRows(node) {
  const body = node.content.trimEnd().split("\n");
  const head = node.definition ? node.definition.content.trimEnd().split("\n") : [];
  const all = [...head, ...body];
  const indent = Math.min(...all.filter((line) => line.trim()).map((line) => line.match(/^ */)[0].length));
  const dedent = (line) => line.slice(indent);
  const numbers = body.map((_, offset) => node.start_line + offset);
  if (!head.length) return { lines: body.map(dedent), numbers };
  return {
    lines: [...head.map(dedent), GAP, ...body.map(dedent)],
    numbers: [...head.map((_, offset) => node.definition.start_line + offset), null, ...numbers],
  };
}
