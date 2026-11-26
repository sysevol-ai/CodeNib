// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

import { freeAreaUrl, freeMapPicture, partNames } from "@/lib/freeMap";
import type { FreeRepositoryMap } from "@/lib/visitorWiki";

const base: FreeRepositoryMap = {
  eligible: true,
  message: "",
  limits: {} as FreeRepositoryMap["limits"],
  map: {
    available: true,
    areas: [
      { id: "src/app/api", title: "src/app/api", symbols: 40, files: 3, path: "src/app/api", kind: "folder" },
      { id: "src/app/core", title: "src/app/core", symbols: 90, files: 6, path: "src/app/core", kind: "folder" },
    ],
    links: [],
  },
  path: {
    title: "From `serve()` to `Router.match()`",
    stops: [
      { symbol: "src/app/api/main.py:serve()", file: "src/app/api/main.py", line: 3, area: "src/app/api" },
      { symbol: "helper()", file: "tools/x.py", line: 1, area: null },
      { symbol: "src/app/core/router.py:Router.match()", file: "src/app/core/router.py", line: 40, area: "src/app/core" },
    ],
  },
};

describe("freeMapPicture", () => {
  it("draws folder areas as pages and the path across them", () => {
    const picture = freeMapPicture(base)!;
    expect(picture.pages.map((page) => page.id)).toEqual(["src/app/api", "src/app/core"]);
    expect(picture.pages.map((page) => page.title)).toEqual(["api/", "core/"]);
    expect(picture.route?.total).toBe(3);
    expect(picture.route?.stops.map((stop) => [stop.index, stop.label, stop.areaId])).toEqual([
      [1, "serve()", "src/app/api"],
      [3, "Router.match()", "src/app/core"],
    ]);
  });

  it("has no route when the path stays in one area", () => {
    const picture = freeMapPicture({ ...base, path: { title: "t", stops: base.path!.stops.slice(0, 1) } });
    expect(picture?.route).toBeNull();
  });

  it("draws nothing without a graph", () => {
    expect(freeMapPicture({ ...base, map: { available: false, areas: [], links: [], reason: "graph_language" } })).toBeNull();
    expect(freeMapPicture(null)).toBeNull();
  });
});

describe("freeAreaUrl", () => {
  const area = (kind: "folder" | "file" | "root" | "other", path: string | null) =>
    ({ id: "a", title: "a", symbols: 1, files: 1, kind, path });
  it("opens folders, files and the root at the commit, and nothing for the rest", () => {
    expect(freeAreaUrl("o/r", "abc", area("folder", "src/app"))).toBe("https://github.com/o/r/tree/abc/src/app");
    expect(freeAreaUrl("o/r", "abc", area("file", "src/app.py"))).toBe("https://github.com/o/r/blob/abc/src/app.py");
    expect(freeAreaUrl("o/r", "abc", area("root", ""))).toBe("https://github.com/o/r/tree/abc");
    expect(freeAreaUrl("o/r", "abc", area("other", null))).toBeNull();
  });
});

describe("partNames", () => {
  const part = (id: string, kind: "folder" | "file" | "root" | "other", path: string | null) =>
    ({ id, title: path ?? id, symbols: 1, files: 1, kind, path });
  it("says the shared folder once and marks folders", () => {
    const names = partNames([
      part("a", "file", "httpx/_client.py"),
      part("b", "folder", "httpx/_transports"),
      part("c", "root", ""),
      part("other", "other", null),
    ]);
    expect([...names.values()]).toEqual(["_client.py", "_transports/", "Top-level files", "other"]);
  });

  it("keeps whole paths when parts share nothing", () => {
    const names = partNames([part("a", "folder", "src"), part("b", "folder", "docs")]);
    expect([...names.values()]).toEqual(["src/", "docs/"]);
  });
});
