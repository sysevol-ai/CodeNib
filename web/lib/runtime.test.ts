import { afterEach, describe, expect, it, vi } from "vitest";
import {
  homeRepository,
  mediaAssetUrl,
  normalizeRuntimeBasePath,
  moveLegacyWikiRoute,
  restoreStaticRoute,
  staticDataUrl,
  stripBasePath,
  withBasePath,
} from "./runtime";

afterEach(() => {
  vi.unstubAllGlobals();
});

function staticWindow(overrides: Record<string, unknown> = {}) {
  const replaceState = vi.fn();
  vi.stubGlobal("window", {
    __CODENIB_RUNTIME__: {
      mode: "static",
      basePath: "/project",
      dataBase: "/project/data",
    },
    location: {
      href: "https://example.test/project/",
      origin: "https://example.test",
    },
    history: { replaceState },
    ...overrides,
  });
  return replaceState;
}

describe("runtime base paths", () => {
  it("normalizes trusted mount paths and rejects traversal", () => {
    expect(normalizeRuntimeBasePath("/project/")).toBe("/project");
    expect(normalizeRuntimeBasePath("/")).toBe("/");
    expect(normalizeRuntimeBasePath("project")).toBe("/");
    expect(normalizeRuntimeBasePath("/project/../other")).toBe("/");
  });

  it("adds and removes the configured Pages prefix", () => {
    staticWindow();

    expect(withBasePath("/demo")).toBe("/project/demo");
    // Route paths are app-relative, even when the first segment matches the mount.
    expect(withBasePath("/project")).toBe("/project/project");
    expect(stripBasePath("/project/demo")).toBe("/demo");
    expect(staticDataUrl("repos", "demo", "pages", "overview.json")).toBe(
      "/project/data/repos/demo/pages/overview.json",
    );
  });

  it("loads only exported media in a static preview", () => {
    staticWindow();

    expect(mediaAssetUrl("data/repos/demo/wiki-media/page/asset.svg")).toBe(
      "/project/data/repos/demo/wiki-media/page/asset.svg",
    );
    expect(mediaAssetUrl("api/repos/demo/wiki-media/page/asset.svg")).toBeNull();
    expect(mediaAssetUrl("https://media.example/asset.png")).toBeNull();
    expect(mediaAssetUrl("data/repos/%2e%2e/private.png")).toBeNull();
    expect(mediaAssetUrl("data/repos/demo/%5cprivate.png")).toBeNull();
    expect(mediaAssetUrl("javascript:alert(1)")).toBeNull();
    expect(mediaAssetUrl("https://user:secret@media.example/asset.png")).toBeNull();
    expect(mediaAssetUrl("../secret.svg")).toBeNull();
    expect(mediaAssetUrl("//media.example/asset.png")).toBeNull();
  });

  it("keeps supported local-runtime media endpoints", () => {
    vi.stubGlobal("window", { __CODENIB_RUNTIME__: { mode: "api" } });
    expect(mediaAssetUrl("api/repos/demo/wiki-media/page/asset.svg")).toBe("/api/repos/demo/wiki-media/page/asset.svg");
    expect(mediaAssetUrl("https://media.example/asset.png")).toBe("https://media.example/asset.png");
  });

  it("restores a route redirected through the static 404 page", () => {
    const route = "/project/demo?p=architecture#runtime";
    const replaceState = staticWindow({
      location: {
        href: `https://example.test/project/?__codenib_route=${encodeURIComponent(route)}`,
        origin: "https://example.test",
      },
    });

    restoreStaticRoute();

    expect(replaceState).toHaveBeenCalledWith(null, "", route);
  });
});

describe("published Wiki home", () => {
  it("opens a static export on the repository it publishes", () => {
    staticWindow({
      __CODENIB_RUNTIME__: { mode: "static", basePath: "/", homeRepository: "orcaloca" },
    });
    expect(homeRepository()).toBe("orcaloca");
  });

  it("keeps the landing page outside a static export or without a repository", () => {
    staticWindow();
    expect(homeRepository()).toBeNull();
    staticWindow({ __CODENIB_RUNTIME__: { mode: "api", homeRepository: "orcaloca" } });
    expect(homeRepository()).toBeNull();
    staticWindow({ __CODENIB_RUNTIME__: { mode: "static", homeRepository: " " } });
    expect(homeRepository()).toBeNull();
  });

  it("moves an earlier export's /<repo>/ addresses to the site root", () => {
    const runtime = { mode: "static", basePath: "/project", homeRepository: "orcaloca" };
    for (const [pathname, search, hash, expected] of [
      ["/project/orcaloca/", "?p=search-loop", "#retry", "/project/?p=search-loop#retry"],
      ["/project/orcaloca", "", "", "/project/"],
      ["/project/orcaloca/page/search-loop/", "", "#retry", "/project/page/search-loop/#retry"],
    ]) {
      const replaceState = staticWindow({
        __CODENIB_RUNTIME__: runtime,
        location: { pathname, search, hash },
      });
      moveLegacyWikiRoute();
      expect(replaceState).toHaveBeenCalledWith(null, "", expected);
    }

    for (const pathname of [
      "/project/",
      "/project/page/search-loop/",
      "/project/orcaloca/ask",
      "/project/other/",
    ]) {
      const untouched = staticWindow({
        __CODENIB_RUNTIME__: runtime,
        location: { pathname, search: "", hash: "" },
      });
      moveLegacyWikiRoute();
      expect(untouched).not.toHaveBeenCalled();
    }
  });
});
