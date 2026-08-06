// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { CodemapResponse } from "@/lib/api";
import HierarchyMap, {
  hierarchyNodeToInfo,
  toggleHierarchyOpen,
} from "./HierarchyMap";

function hierarchyData(scopeOpen: boolean): CodemapResponse {
  return {
    available: true,
    root: "src/model.py:Widget.run()",
    root_label: "src/model.py:Widget.run()",
    nodes: [
      {
        id: "n0",
        name: "src/model.py:Widget.run()",
        label: "src/model.py:Widget.run()",
        short: "model.py:Widget.run()",
        file: "src/model.py",
        line: 12,
        end_line: 18,
        kind: "method",
        depth: 0,
        is_root: true,
      },
      {
        id: "n1",
        name: "src/helper.py:helper()",
        label: "src/helper.py:helper()",
        short: "helper.py:helper()",
        file: "src/helper.py",
        line: 4,
        end_line: 7,
        kind: "function",
        depth: 1,
        is_root: false,
      },
    ],
    edges: [
      {
        source: "n0",
        target: "n1",
        anchors: [{ file: "src/model.py", line: 15 }],
        source_hierarchy: "hier::symbol::n0",
        target_hierarchy: "hier::symbol::n1",
      },
    ],
    hierarchy: {
      root: "hier::root",
      nodes: [
        {
          id: "hier::root",
          parent: null,
          kind: "root",
          label: "root",
          depth: 0,
          child_count: 2,
          symbol_count: 3,
          doi: 1,
          open_by_default: true,
        },
        {
          id: "hier::file::src/model.py",
          parent: "hier::root",
          kind: "file",
          label: "model.py",
          file: "src/model.py",
          depth: 1,
          child_count: 1,
          symbol_count: 2,
          doi: 1,
          open_by_default: true,
        },
        {
          id: "hier::symbol::src/model.py:Widget",
          parent: "hier::file::src/model.py",
          kind: "symbol",
          label: "Widget",
          path: "src/model.py:Widget",
          file: "src/model.py",
          node_id: "src/model.py:Widget",
          line: 8,
          end_line: 24,
          source: {
            file: "src/model.py",
            start_line: 8,
            end_line: 24,
            content: "class Widget:\n    pass",
          },
          depth: 2,
          child_count: 1,
          symbol_count: 2,
          doi: 1,
          open_by_default: scopeOpen,
        },
        {
          id: "hier::symbol::n0",
          parent: "hier::symbol::src/model.py:Widget",
          kind: "symbol",
          label: "Widget.run()",
          path: "src/model.py:Widget.run()",
          file: "src/model.py",
          node_id: "n0",
          line: 12,
          end_line: 18,
          depth: 3,
          child_count: 0,
          symbol_count: 1,
          doi: 1,
        },
        {
          id: "hier::file::src/helper.py",
          parent: "hier::root",
          kind: "file",
          label: "helper.py",
          file: "src/helper.py",
          depth: 1,
          child_count: 1,
          symbol_count: 1,
          doi: 0.5,
          open_by_default: true,
        },
        {
          id: "hier::symbol::n1",
          parent: "hier::file::src/helper.py",
          kind: "symbol",
          label: "helper()",
          path: "src/helper.py:helper()",
          file: "src/helper.py",
          node_id: "n1",
          line: 4,
          end_line: 7,
          depth: 2,
          child_count: 0,
          symbol_count: 1,
          doi: 0.5,
        },
      ],
    },
    mermaid: "",
  };
}

describe("HierarchyMap symbol scopes", () => {
  it("opens and closes a scope without mutating the previous state", () => {
    const closed = new Set(["hier::root"]);
    const opened = toggleHierarchyOpen(
      closed,
      "hier::symbol::src/model.py:Widget",
    );

    expect(closed).toEqual(new Set(["hier::root"]));
    expect(opened).toContain("hier::symbol::src/model.py:Widget");
    expect(
      toggleHierarchyOpen(opened, "hier::symbol::src/model.py:Widget"),
    ).not.toContain("hier::symbol::src/model.py:Widget");
  });

  it("renders an open parent scope, its child symbol, and an enabled source action", () => {
    const data = hierarchyData(true);
    const html = renderToStaticMarkup(
      <HierarchyMap
        data={data}
        onNodeClick={vi.fn()}
        onSourceClick={vi.fn()}
      />,
    );

    expect(html).toContain('class="hier-scope-toggle" aria-expanded="true"');
    expect(html).toContain("Widget.run()");
    const sourceButton = html.match(
      /<button[^>]*class="hier-scope-source"[^>]*aria-label="View source for Widget"[^>]*>/,
    )?.[0];
    expect(sourceButton).toBeDefined();
    expect(sourceButton).not.toContain("disabled");
    expect(sourceButton).toContain("src/model.py:Widget");

    const parentScope = data.hierarchy?.nodes.find(
      (node) => node.id === "hier::symbol::src/model.py:Widget",
    );
    expect(parentScope && hierarchyNodeToInfo(parentScope)?.source?.content).toBe(
      "class Widget:\n    pass",
    );
  });

  it("does not label a focus-only consumer action as source", () => {
    const html = renderToStaticMarkup(
      <HierarchyMap data={hierarchyData(true)} onNodeClick={vi.fn()} />,
    );

    expect(html).not.toContain('class="hier-scope-source"');
  });

  it("rolls a child edge up to its nearest closed symbol scope", () => {
    const html = renderToStaticMarkup(
      <HierarchyMap
        data={hierarchyData(false)}
        onNodeClick={vi.fn()}
        onEdgeClick={vi.fn()}
      />,
    );

    expect(html).toContain('title="Widget -&gt; helper()"');
    expect(html).not.toContain('title="Widget.run() -&gt; helper()"');
  });
});
