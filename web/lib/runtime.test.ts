import { afterEach, describe, expect, it, vi } from "vitest";
import {
  normalizeRuntimeBasePath,
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
