import { describe, expect, it } from "vitest";
import type { UserConfig } from "vite";
import { readFileSync } from "node:fs";

import viteConfig from "./vite.config";

describe("public demo host policy", () => {
  it("allows the production hostname without disabling host checks", () => {
    const config = viteConfig as UserConfig;

    expect(config.server?.allowedHosts).toEqual(["demo.codenib.ai"]);
    expect(config.preview?.allowedHosts).toEqual(["demo.codenib.ai"]);
    expect(config.server?.allowedHosts).not.toBe(true);
    expect(config.server?.proxy?.["/api"]).toMatchObject({
      target: "http://127.0.0.1:8000",
      changeOrigin: true,
    });
    expect(config.preview?.proxy).toEqual(config.server?.proxy);
  });

  it("resolves relative build assets from the live deployment root", () => {
    const index = readFileSync(new URL("./index.html", import.meta.url), "utf8");

    expect(viteConfig).toMatchObject({ base: "./" });
    expect(index).toContain('<base href="/" />');
  });
});
