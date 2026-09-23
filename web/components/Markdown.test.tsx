// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import Markdown from "./Markdown";

describe("Markdown tables", () => {
  it("renders both a semantic table and labeled mobile rows", () => {
    const html = renderToStaticMarkup(
      <Markdown>
        {
          "| Area | Files | Definition |\n" +
          "|---|---:|---|\n" +
          "| Runtime | 3 | `serve` |"
        }
      </Markdown>,
    );

    expect(html).toContain('class="table-scroll"');
    expect(html).toContain('class="table-cards"');
    expect(html).toContain('class="table-card-label">Definition</div>');
    expect(html).toContain('class="table-card-value"><code>serve</code>');
  });
});

describe("Markdown story rendering", () => {
  it("groups trailing citations at the end of a paragraph", () => {
    const html = renderToStaticMarkup(
      <Markdown
        citations={[
          {
            file: "src/requests/sessions.py",
            start_line: 309,
            end_line: 332,
            node_name: "src/requests/sessions.py:SessionRedirectMixin.rebuild_auth()",
            type: "method",
            score: null,
            content: null,
          },
        ]}
      >
        {"`rebuild_auth()` strips the header. [E1](#evidence-E1)"}
      </Markdown>,
    );

    expect(html).toContain('class="cite-group"');
    expect(html).toContain("sessions.py");
    // The citation is the last thing in the paragraph, after the prose.
    expect(html.indexOf("strips the header")).toBeLessThan(html.indexOf("cite-group"));
  });

  it("renders relation rows as a plain list after the Interactions label", () => {
    const html = renderToStaticMarkup(
      <Markdown
        relations={[
          {
            id: "R1",
            source: "src/requests/sessions.py:Session.send()",
            target: "src/requests/adapters.py:HTTPAdapter.send()",
            anchors: ["src/requests/sessions.py:703"],
          },
        ]}
      >
        {"**Interactions**\n- `Session.send()` → `HTTPAdapter.send()`: hands the request to the transport [R1](#evidence-R1)"}
      </Markdown>,
    );

    expect(html).toContain("<strong>Interactions</strong>");
    expect(html).toContain("<li>");
    expect(html).toContain("sessions.py");
    expect(html).toContain(":703");
  });
});

describe("Markdown wiki page links", () => {
  it("scopes ?p= links to the repository route", () => {
    const html = renderToStaticMarkup(
      <Markdown repoId="psf__requests">
        {"- [Authentication Mechanisms](?p=authentication)"}
      </Markdown>,
    );

    // A relative `?p=` would resolve against the document `<base href>` and
    // leave the wiki for the landing page.
    expect(html).toContain('href="/psf__requests?p=authentication"');
    expect(html).not.toContain('href="?p=');
  });

  it("leaves external links alone", () => {
    const html = renderToStaticMarkup(
      <Markdown repoId="psf__requests">
        {"[docs](https://example.com/?p=1)"}
      </Markdown>,
    );

    expect(html).toContain('href="https://example.com/?p=1"');
  });
});

describe("Markdown section flows", () => {
  const chain =
    "```mermaid\nflowchart LR\n" +
    '  n0["Context.MustBindWith()"]\n  n1["Context.AbortWithError()"]\n' +
    "  n0 -->|binding failure triggers abort| n1\n```\n";

  it("renders a straight chain as an ordered call path with its call site", () => {
    const html = renderToStaticMarkup(
      <Markdown
        relations={[
          {
            id: "R3",
            source: "context.go:Context.MustBindWith()",
            target: "context.go:Context.AbortWithError()",
            anchors: ["context.go:843"],
          },
        ]}
      >
        {chain}
      </Markdown>,
    );
    expect(html).toContain('class="call-chain"');
    expect(html).toContain("<code>Context.MustBindWith()</code>");
    expect(html).toContain("binding failure triggers abort");
    expect(html).toContain('<span class="cite-src-loc">:843</span>');
    expect(html).not.toContain("mermaid");
  });

  it("still hands a branching flow to Mermaid", () => {
    const fan = chain.replace("```\n", '  n2["x()"]\n  n0 --> n2\n```\n');
    const html = renderToStaticMarkup(<Markdown>{fan}</Markdown>);
    expect(html).not.toContain('class="call-chain"');
  });
});
