// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import AskPage from "@/app/[repoId]/ask/page";
import Markdown from "./Markdown";

afterEach(() => vi.unstubAllGlobals());

describe("static activation", () => {
  it("offers direct authorization only for an explicitly configured public trial", () => {
    vi.stubGlobal("window", { __CODENIB_RUNTIME__: { mode: "static", trialApiBase: "https://source.example" } });
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const html = renderToStaticMarkup(<AskPage repoId="repo" query="Where is retry handled?" />);
    expect(html).toContain("Connect OpenRouter");
    expect(html).toContain("sent only to OpenRouter");
    expect(html).toContain("Use with your local agent");
    expect(html).not.toContain('type="password"');
    expect(html).not.toContain("api-key");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("does not enable a trial from an insecure or credential-bearing endpoint", () => {
    vi.stubGlobal("window", { __CODENIB_RUNTIME__: { mode: "static", trialApiBase: "https://key@source.example" } });
    const html = renderToStaticMarkup(<AskPage repoId="repo" query="retry" />);
    expect(html).toContain("Ask on your own repository");
    expect(html).not.toContain("Connect OpenRouter");
  });

  it("replaces direct Ask links with local setup and escapes the question", () => {
    vi.stubGlobal("window", { __CODENIB_RUNTIME__: { mode: "static", basePath: "/demo" } });
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const html = renderToStaticMarkup(<AskPage repoId="repo" query={'<img src="/api/spend">'} />);
    expect(html).toContain("Ask on your own repository");
    expect(html).toContain("&lt;img");
    expect(html).not.toContain('src="/api/spend"');
    expect(html).not.toContain("askbar-input");
    expect(html).toContain('href="/demo/repo"');
    expect(fetch).not.toHaveBeenCalled();
  });

  it("does not load Markdown images from live or remote endpoints", () => {
    vi.stubGlobal("window", { __CODENIB_RUNTIME__: { mode: "static" } });
    const html = renderToStaticMarkup(<Markdown>{"![External](https://example.com/image.png)\n![Live](/api/generate)"}</Markdown>);
    expect(html).not.toContain("<img");
    expect(html).not.toContain("https://example.com");
    expect(html).toContain("not included in this export");
  });
});
