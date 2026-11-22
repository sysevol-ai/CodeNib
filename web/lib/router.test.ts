import { afterEach, describe, expect, it, vi } from "vitest";
import { walkFromLocation, walkLocation, wikiPageFromLocation, wikiPageLocation, wikiRoute } from "./router";

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

describe("walkthrough addresses", () => {
  it("names the walkthrough and counts its steps from one", () => {
    expect(walkLocation("/acme__agent", "run", 0)).toBe("/acme__agent?walk=run");
    expect(walkLocation("/acme__agent", "run", 2)).toBe("/acme__agent?walk=run&step=3");
    expect(walkFromLocation("?walk=run&step=3")).toEqual({ id: "run", step: 2 });
    expect(walkFromLocation("?walk=run")).toEqual({ id: "run", step: 0 });
  });

  it("reads a page address, or a broken step, as no walkthrough step", () => {
    expect(walkFromLocation("?p=overview")).toBeNull();
    expect(walkFromLocation("?walk=run&step=zero")).toEqual({ id: "run", step: 0 });
  });

  it("leaves the walkthrough when a page is picked", () => {
    expect(wikiPageLocation("/acme__agent", "tools", false)).toBe("/acme__agent?p=tools");
  });
});
