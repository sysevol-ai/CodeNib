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

  it("leaves short names and leading separators alone", () => {
    expect(html("request()")).toBe("<code>request()</code>");
    expect(html("__init__")).toBe("<code>__init__</code>");
    expect(html("./src/api.py")).toBe("<code>./src/<wbr/>api.<wbr/>py</code>");
  });
});
