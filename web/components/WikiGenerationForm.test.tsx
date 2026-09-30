// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import WikiGenerationForm from "./WikiGenerationForm";

const attempt = { id: "a".repeat(64), owner: "b".repeat(64), repository: "psf/requests" };

describe("WikiGenerationForm", () => {
  it("groups writing models by provider and explains the default", () => {
    const html = renderToStaticMarkup(
      <WikiGenerationForm attempt={attempt as never} onStarted={() => {}} />,
    );
    expect(html).toContain('<optgroup label="DeepSeek">');
    expect(html).toContain('<optgroup label="Anthropic">');
    expect(html).toContain("DeepSeek V4.1 Flash · $0.30 in / $1.20 out");
    expect(html).toContain("Complete Wikis have been generated with this model.");
  });

  it("offers no model choice when resuming a run", () => {
    const html = renderToStaticMarkup(
      <WikiGenerationForm attempt={attempt as never} onStarted={() => {}} resume />,
    );
    expect(html).not.toContain("<optgroup");
  });

  it("waits for a source check before accepting a key or generation consent", () => {
    const html = renderToStaticMarkup(<WikiGenerationForm attempt={attempt} onStarted={() => {}} />);
    expect(html).toContain("Checking the repository size and source languages");
    expect(html).toMatch(/id="wiki-generation-key"[^>]*disabled/);
    expect(html).toMatch(/type="checkbox"[^>]*disabled/);
    expect(html).toMatch(/type="submit"[^>]*disabled/);
    const resume = renderToStaticMarkup(<WikiGenerationForm attempt={attempt} onStarted={() => {}} resume />);
    expect(resume).not.toContain("Checking the repository size");
    expect(resume).not.toMatch(/id="wiki-generation-key"[^>]*disabled/);
  });
});
