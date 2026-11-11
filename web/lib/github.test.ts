// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { ghFileUrl } from "./github";

describe("ghFileUrl", () => {
  it("links a file in an owner/name repository at the indexed commit", () => {
    expect(ghFileUrl("owner/demo", null, "abc123", "src/a.py", 4, 9)).toBe(
      "https://github.com/owner/demo/blob/abc123/src/a.py#L4-L9",
    );
  });

  it("prefers the recorded source address", () => {
    expect(ghFileUrl("demo", "https://github.com/owner/demo/", "abc123", "a.py")).toBe(
      "https://github.com/owner/demo/blob/abc123/a.py",
    );
  });

  it("makes no link when only a directory name is known", () => {
    expect(ghFileUrl("demo", null, "abc123", "a.py")).toBeNull();
  });
});
