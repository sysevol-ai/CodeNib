// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import WikiToc from "./WikiToc";

const pages = [
  { id: "overview", title: "Overview", children: [] },
  { id: "model", title: "Data model", children: [] },
  { id: "io", title: "File I/O", children: [] },
];

describe("WikiToc", () => {
  it("shows each chapter's state as its dot does on the map", () => {
    const html = renderToStaticMarkup(
      <WikiToc
        pages={pages}
        activeId="overview"
        onPick={() => {}}
        colors={new Map([["model", "#3987e5"]])}
        states={{ overview: "ready", model: "running", io: "pending" }}
      />,
    );
    expect(html).toContain('class="toc-dot is-ready"');
    expect(html).toContain('class="toc-dot is-running" style="--dot:#3987e5"');
    expect(html).toContain('class="sr-only">, Being written</span>');
    expect(html).toContain('class="sr-only">, Planned</span>');
    expect(html).not.toContain("toc-cache-state");
  });
});
