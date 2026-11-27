// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import MainPath from "@/components/MainPath";
import type { Citation } from "@/lib/api";

const citations = [
  { file: "README.md", start_line: 1, end_line: 40 },
  { file: "tsdb/head.go", start_line: 10, end_line: 30 },
  { file: "cmd/main.go", start_line: 5, end_line: 9 },
] as unknown as Citation[];

const render = (roles: boolean) =>
  renderToStaticMarkup(
    <MainPath
      roles={roles}
      journey={{
        title: "How it runs",
        stages: [
          { index: 1, symbol: "Targets", text: "Scraped.", evidence: ["E1"] },
          { index: 2, symbol: "Storage", text: "Kept.", evidence: ["E2"], page: { id: "tsdb", title: "TSDB" } },
          { index: 3, symbol: "Entry", text: "Starts.", evidence: ["E3"] },
        ],
      }}
      onPick={() => {}}
      renderText={(text) => text}
      colors={new Map([["tsdb", "#3987e5"]])}
      citations={citations}
    />,
  );

describe("MainPath", () => {
  it("says a part grounded only in the README is described there", () => {
    const html = render(true);
    expect(html).toContain("Described in the README");
    expect(html).toContain("Outside the map&#x27;s areas");
    expect(html).toContain('class="main-path-role"');
  });

  it("keeps calls outside the map labelled as outside", () => {
    expect(render(false)).not.toContain("Described in the README");
  });
});
