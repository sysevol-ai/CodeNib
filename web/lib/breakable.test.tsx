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

  it("breaks only a name too long for a phone's line between its words", () => {
    expect(html("TestTargetSetTargetGroupsPresentOnConfigChange")).toBe(
      "<code>Test<wbr/>Target<wbr/>SetTarget<wbr/>Groups<wbr/>Present<wbr/>OnConfig<wbr/>Change</code>",
    );
    // Shorter names move to the next line whole.
    expect(html("RequestsJSONDecodeError")).toBe("<code>RequestsJSONDecodeError</code>");
    expect(html("GaussianProcessRegressor.predict()")).toBe(
      "<code>GaussianProcessRegressor.<wbr/>predict()</code>",
    );
    // A call with its arguments is never split inside a name.
    expect(html('ValueError("CRT failed for these moduli")')).not.toContain("Value<wbr/>");
  });

  it("keeps a tiny head with what follows it", () => {
    expect(html("(n_samples_X, n_samples_Y)")).toBe("<code>(n_samples_<wbr/>X, n_<wbr/>samples_Y)</code>");
  });

  it("may break before a private name after a dot", () => {
    expect(html("OneHotEncoder._compute_drop_idx()")).toBe(
      "<code>OneHotEncoder.<wbr/>_compute_<wbr/>drop_<wbr/>idx()</code>",
    );
  });

  it("leaves short names and leading separators alone", () => {
    expect(html("request()")).toBe("<code>request()</code>");
    expect(html("__init__")).toBe("<code>__init__</code>");
    expect(html("n.insertChild")).toBe("<code>n.insertChild</code>");
    expect(html("group.engine.addRoute")).toBe("<code>group.engine.addRoute</code>");
    // An extension stays with its name.
    expect(html("./src/requests/adapters.py")).toBe("<code>./src/<wbr/>requests/<wbr/>adapters.py</code>");
  });
});
