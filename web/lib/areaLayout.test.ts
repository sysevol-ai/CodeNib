// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

import { layoutAreas } from "./areaLayout";

describe("layoutAreas", () => {
  it("puts callers left of the areas they call", () => {
    const layout = layoutAreas(
      ["prep", "session", "auth"],
      [
        { source: "session", target: "prep", weight: 12 },
        { source: "prep", target: "auth", weight: 6 },
        { source: "prep", target: "session", weight: 4 },
        { source: "session", target: "auth", weight: 3 },
      ],
    );
    expect(layout.columns).toEqual([["session"], ["prep"], ["auth"]]);
    // The weaker direction of the mutual pair is not drawn.
    expect(layout.drawn.map((l) => `${l.source}>${l.target}`)).toEqual([
      "session>prep",
      "prep>auth",
      "session>auth",
    ]);
  });

  it("breaks a longer cycle at its weakest edge and parks unlinked areas last", () => {
    const layout = layoutAreas(
      ["a", "b", "c", "lonely"],
      [
        { source: "a", target: "b", weight: 5 },
        { source: "b", target: "c", weight: 4 },
        { source: "c", target: "a", weight: 1 },
      ],
    );
    expect(layout.columns).toEqual([["a"], ["b"], ["c", "lonely"]]);
    expect(layout.drawn).toHaveLength(2);
  });

  it("squeezes deep chains into the column budget without backward arrows", () => {
    const ids = ["a", "b", "c", "d", "e"];
    const links = ids.slice(1).map((id, i) => ({ source: ids[i], target: id, weight: 5 - i }));
    const layout = layoutAreas(ids, links, { maxColumns: 3 });
    expect(layout.columns.length).toBe(3);
    const col = (id: string) => layout.columns.findIndex((c) => c.includes(id));
    for (const link of layout.drawn) expect(col(link.source)).toBeLessThan(col(link.target));
  });
});
