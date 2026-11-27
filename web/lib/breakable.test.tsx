// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { breakableCode } from "./breakable";

const html = (text: string) => renderToStaticMarkup(<code>{breakableCode(text)}</code>);

describe("breakableCode", () => {
  it("offers breaks only after name separators", () => {
    expect(html("SessionRedirectMixin.should_strip_auth()")).toBe(
      "<code>SessionRedirectMixin.<wbr/>should_<wbr/>strip_<wbr/>auth()</code>",
    );
    expect(html("tokio::runtime::Builder")).toBe(
      "<code>tokio::<wbr/>runtime::<wbr/>Builder</code>",
    );
  });

  it("breaks a long camelCase name between its words only", () => {
    expect(html("TestTargetSetGroupsOnConfigChange")).toBe(
      "<code>Test<wbr/>Target<wbr/>Set<wbr/>Groups<wbr/>On<wbr/>Config<wbr/>Change</code>",
    );
    // A short segment keeps its humps whole.
    expect(html("SessionRedirectMixin.should_strip_auth()")).toContain("SessionRedirectMixin.");
  });

  it("leaves short names and leading separators alone", () => {
    expect(html("request()")).toBe("<code>request()</code>");
    expect(html("__init__")).toBe("<code>__init__</code>");
    expect(html("n.insertChild")).toBe("<code>n.insertChild</code>");
    expect(html("group.engine.addRoute")).toBe("<code>group.engine.addRoute</code>");
    expect(html("./src/requests/adapters.py")).toBe("<code>./src/<wbr/>requests/<wbr/>adapters.<wbr/>py</code>");
  });
});
