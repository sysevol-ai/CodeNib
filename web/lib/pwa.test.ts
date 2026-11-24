import { describe, expect, it } from "vitest";
import { serviceWorkerAllowed } from "./pwa";

describe("serviceWorkerAllowed", () => {
  const at = (protocol: string, hostname: string) => ({ protocol, hostname });

  it("registers on https and on this machine, in production builds", () => {
    expect(serviceWorkerAllowed(at("https:", "demo.codenib.ai"), true, true)).toBe(true);
    expect(serviceWorkerAllowed(at("http:", "127.0.0.1"), true, true)).toBe(true);
    expect(serviceWorkerAllowed(at("http:", "localhost"), true, true)).toBe(true);
  });

  it("stays off on plain http elsewhere, in development, and without support", () => {
    expect(serviceWorkerAllowed(at("http:", "192.168.1.20"), true, true)).toBe(false);
    expect(serviceWorkerAllowed(at("https:", "demo.codenib.ai"), false, true)).toBe(false);
    expect(serviceWorkerAllowed(at("https:", "demo.codenib.ai"), true, false)).toBe(false);
  });
});
