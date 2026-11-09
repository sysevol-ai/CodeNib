import { afterEach, describe, expect, it, vi } from "vitest";
import { wikiPageFromLocation, wikiPageLocation, wikiRoute } from "./router";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Wiki addresses", () => {
  it("serves a published Wiki at the site root", () => {
    vi.stubGlobal("window", {
      __CODENIB_RUNTIME__: { mode: "static", basePath: "/", homeRepository: "orcaloca" },
    });
    expect(wikiRoute("orcaloca")).toBe("/");
    expect(wikiRoute("other repo")).toBe("/other%20repo");
  });

  it("keeps each repository's own address outside a published Wiki", () => {
    vi.stubGlobal("window", { __CODENIB_RUNTIME__: { mode: "api" } });
    expect(wikiRoute("orcaloca")).toBe("/orcaloca");
  });

  it("reads the page an address names", () => {
    expect(wikiPageFromLocation("/project/", "")).toBe("overview");
    expect(wikiPageFromLocation("/project/page/search%20loop/", "")).toBe("search loop");
    expect(wikiPageFromLocation("/project/page/a/", "?p=b")).toBe("b");
    expect(wikiPageFromLocation("/project/orcaloca", "?p=graph")).toBe("graph");
  });

  it("moves between root pages by path", () => {
    expect(wikiPageLocation("/project/", "search-loop", true)).toBe(
      "/project/page/search-loop/",
    );
    expect(wikiPageLocation("/project", "search-loop", true)).toBe(
      "/project/page/search-loop/",
    );
    expect(wikiPageLocation("/project/page/search-loop/", "overview", true)).toBe(
      "/project/",
    );
    expect(wikiPageLocation("/project/page/search-loop/", "graph", true)).toBe(
      "/project/page/graph/",
    );
  });
});
