// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import Markdown from "./Markdown";

describe("Markdown tables", () => {
  it("renders both a semantic table and labeled mobile rows", () => {
    const html = renderToStaticMarkup(
      <Markdown>
        {
          "| Area | Files | Definition |\n" +
          "|---|---:|---|\n" +
          "| Runtime | 3 | `serve` |"
        }
      </Markdown>,
    );

    expect(html).toContain('class="table-scroll"');
    expect(html).toContain('class="table-cards"');
    expect(html).toContain('class="table-card-label">Definition</div>');
    expect(html).toContain('class="table-card-value"><code>serve</code>');
  });
});
