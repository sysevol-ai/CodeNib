// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { CodemapResponse } from "@/lib/api";
import SystemMap from "./SystemMap";

function systemData(source: null | undefined): CodemapResponse {
  return {
    available: true,
    root: "src/api.py:start()",
    root_label: "src/api.py:start()",
    nodes: [
      {
        id: "n0",
        name: "src/api.py:start()",
        label: "src/api.py:start()",
        short: "api.py:start()",
        file: "src/api.py",
        line: 1,
        end_line: 3,
        kind: "function",
        depth: 0,
        is_root: true,
        ...(source === null ? { source } : {}),
      },
      {
        id: "n1",
        name: "src/store.py:save()",
        label: "src/store.py:save()",
        short: "store.py:save()",
        file: "src/store.py",
        line: 4,
        end_line: 6,
        kind: "function",
        depth: 1,
        is_root: false,
        ...(source === null ? { source } : {}),
      },
    ],
    edges: [
      {
        source: "n0",
        target: "n1",
        anchors: [
          {
            file: "src/api.py",
            line: 2,
            ...(source === null ? { source } : {}),
          },
        ],
      },
    ],
    mermaid: "",
  };
}

describe("SystemMap static source actions", () => {
  it("disables nodes and relationships with explicitly unavailable previews", () => {
    const html = renderToStaticMarkup(
      <SystemMap
        data={systemData(null)}
        onNodeClick={vi.fn()}
        onEdgeClick={vi.fn()}
      />,
    );

    const symbols =
      html.match(/<button[^>]*class="system-symbol[^>]*>/g) ?? [];
    expect(symbols).toHaveLength(2);
    expect(symbols.every((button) => button.includes("disabled"))).toBe(true);
    const relationship = html.match(
      /<button[^>]*class="system-link"[^>]*>/,
    )?.[0];
    expect(relationship).toContain("disabled");
  });

  it("keeps live payloads without embedded source previews actionable", () => {
    const html = renderToStaticMarkup(
      <SystemMap
        data={systemData(undefined)}
        onNodeClick={vi.fn()}
        onEdgeClick={vi.fn()}
      />,
    );

    const symbols =
      html.match(/<button[^>]*class="system-symbol[^>]*>/g) ?? [];
    expect(symbols).toHaveLength(2);
    expect(symbols.every((button) => !button.includes("disabled"))).toBe(true);
    const relationship = html.match(
      /<button[^>]*class="system-link"[^>]*>/,
    )?.[0];
    expect(relationship).not.toContain("disabled");
  });
});
