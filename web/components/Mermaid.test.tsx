// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

import { mermaidConfig } from "./Mermaid";

describe("mermaidConfig", () => {
  it("keeps node labels out of foreignObject in both themes", () => {
    for (const dark of [false, true]) {
      const config = mermaidConfig(dark);
      // The sanitizer strips <foreignObject>, so HTML labels would render
      // as empty boxes. Mermaid 11 only honours the top-level flag.
      expect(config.htmlLabels).toBe(false);
      expect(config.flowchart.htmlLabels).toBe(false);
      expect(config.securityLevel).toBe("strict");
    }
  });
});
