// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0
// Provider-free publication, discovery and shared architecture acceptance.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import { chromium } from "playwright";

const base = process.argv[2] || "http://127.0.0.1:3011";
const output = process.argv[3] || "/tmp/codenib-community-browser";
await fs.mkdir(output, { recursive: true });
const browser = await chromium.launch();
const id = "a".repeat(64),
  owner = "b".repeat(64),
  commit = "c".repeat(40);
const reports = [];
try {
  for (const mobile of [false, true]) {
    let published = false,
      mutations = 0;
    const errors = [],
      outbound = [];
    const context = await browser.newContext({
      viewport: mobile
        ? { width: 390, height: 844 }
        : { width: 1440, height: 1000 },
    });
    const fixture = async (route) => {
      const request = route.request(),
        url = new URL(request.url());
      if (url.origin !== new URL(base).origin) {
        outbound.push(url.origin);
        return route.abort();
      }
      const card = {
        id,
        repository: "owner/fastqueue",
        commit,
        summary: "Routes requests into a persistent queue and worker pool.",
        chapters: 1,
        languages: ["python"],
        published_at: 1000,
        published: true,
      };
      if (url.pathname === "/api/repos")
        return route.fulfill({
          json: [
            {
              id: "psf__requests",
              repo: "psf/requests",
              name: "requests",
              language: "python",
              languages: ["python"],
              summary: "HTTP for humans.",
              commit_short: "abc123",
              base_commit: commit,
              file_count: 34,
              capabilities: {},
            },
          ],
        });
      if (url.pathname === "/api/visitor-wikis/public")
        return route.fulfill({ json: published ? [card] : [] });
      if (url.pathname === "/api/visitor-wikis")
        return route.fulfill({ json: { enabled: true } });
      if (url.pathname.endsWith("/publication")) {
        assert.equal(request.headers()["x-wiki-owner"], owner);
        assert.equal(request.headers().authorization, undefined);
        mutations++;
        published = request.postDataJSON().published;
        return route.fulfill({ json: { published } });
      }
      if (url.pathname === `/api/visitor-wikis/${id}`)
        return route.fulfill({
          json: {
            id,
            repository: card.repository,
            commit,
            status: "complete",
            stage: "complete",
            active_page: "",
            pages: [{ id: "overview", title: "Overview", children: [] }],
            page_states: { overview: "ready" },
            created_at: 1000,
            updated_at: 1045,
            reported_cost_usd: 0.025,
            calls: 22,
            budget_usd: 2,
            unreported_call_cost: false,
            stalled: false,
            history: [],
            published,
          },
        });
      if (url.pathname === `/api/visitor-wikis/${id}/pages/overview`)
        return route.fulfill({
          json: {
            id: "overview",
            title: "Overview",
            markdown:
              "# Fastqueue\n\nRoutes each request to a persistent queue and a worker. [E1](#evidence-E1)\n\n## Durable delivery\n\nThe worker acknowledges a request after its result is saved. [E1](#evidence-E1)",
            citations: [
              {
                file: "queue.py",
                start_line: 1,
                end_line: 3,
                content:
                  "def handle(request):\n    queue.put(request)\n    return request.id",
                node_name: "handle",
                type: "function",
              },
            ],
            grounding: { valid: true },
            quality: { valid: true },
            generation: { mode: "generated", renderer: "fact_plan" },
            media_slots: [
              {
                id: "architecture",
                kind: "diagram",
                placement: "aside",
                title: "From request to durable result",
                source_citations: ["queue.py"],
                render_contract: {
                  schema_version: 1,
                  adapter: "architecture",
                  provenance: "architecture-plan",
                  evidence: ["E1"],
                  data: {
                    nodes: [
                      {
                        id: "request",
                        label: "Request",
                        detail: "A caller submits work.",
                        layer: "external",
                        evidence: ["E1"],
                      },
                      {
                        id: "router",
                        label: "Router",
                        detail: "Selects the queue.",
                        layer: "interface",
                        evidence: ["E1"],
                      },
                      {
                        id: "worker",
                        label: "Worker",
                        detail: "Processes queued work.",
                        layer: "execution",
                        evidence: ["E1"],
                      },
                      {
                        id: "result",
                        label: "Result store",
                        detail: "Persists the result.",
                        layer: "data",
                        evidence: ["E1"],
                      },
                    ],
                    edges: [
                      {
                        source: "request",
                        target: "router",
                        label: "submits work",
                        evidence: ["E1"],
                      },
                      {
                        source: "router",
                        target: "worker",
                        label: "queues work",
                        evidence: ["E1"],
                      },
                      {
                        source: "worker",
                        target: "result",
                        label: "stores result",
                        evidence: ["E1"],
                      },
                    ],
                    primary_path: ["request", "router", "worker", "result"],
                    boundaries: [],
                  },
                },
              },
            ],
          },
        });
      if (url.pathname.startsWith("/api/"))
        throw new Error(
          `Unexpected API request ${request.method()} ${url.pathname}`,
        );
      return route.continue();
    };
    await context.route("**/*", fixture);
    await context.addInitScript(
      ({ id, owner }) =>
        localStorage.setItem(
          "codenib-wiki-attempts-v1",
          JSON.stringify([{ id, owner, repository: "owner/fastqueue" }]),
        ),
      { id, owner },
    );
    const page = await context.newPage();
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(base);
    await page.getByRole("heading", { name: "Continue reading" }).waitFor();
    assert.equal(
      await page.getByText("Your saved Wikis", { exact: true }).count(),
      0,
    );
    await page.getByRole("link", { name: "Browse", exact: true }).click();
    await page
      .getByRole("link", { name: "Open psf/requests wiki", exact: true })
      .waitFor();
    assert.equal(
      await page
        .getByRole("link", { name: "Open owner/fastqueue wiki", exact: true })
        .count(),
      0,
    );
    await page.getByRole("link", { name: "Community", exact: true }).click();
    await page
      .getByRole("heading", { name: "Share the first community Wiki" })
      .waitFor();
    await page.getByRole("link", { name: "My Wikis", exact: true }).click();
    await page.getByRole("link", { name: "Open owner/fastqueue wiki" }).click();
    await page
      .getByRole("heading", { name: "From request to durable result" })
      .waitFor();
    assert.equal(await page.locator(".wiki-system-path-step").count(), 4);
    assert.equal(
      await page.locator(".wiki-progress").getAttribute("open"),
      null,
    );
    await page
      .getByRole("button", { name: "View source", exact: false })
      .first()
      .click();
    await page
      .getByRole("link", { name: "Open pinned source on GitHub" })
      .waitFor();
    assert.ok(
      (
        await page
          .getByRole("link", { name: "Open pinned source on GitHub" })
          .getAttribute("href")
      ).endsWith(`/blob/${commit}/queue.py#L1-L3`),
    );
    await page.getByRole("button", { name: "Close source" }).click();
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({
      path: `${output}/${mobile ? "mobile" : "desktop"}-reading.png`,
      fullPage: true,
    });
    assert.equal(mutations, 0, "opening a Wiki must not publish it");
    await page
      .getByRole("button", { name: "Publish to Browse", exact: true })
      .click();
    await page
      .getByText("Published — everyone can now find this Wiki in Browse.", {
        exact: true,
      })
      .waitFor();
    assert.equal(mutations, 1);
    await page.getByRole("link", { name: "View in Browse" }).click();
    await page
      .getByRole("link", { name: "Open owner/fastqueue wiki" })
      .waitFor();
    await page.getByLabel("Search Wikis").fill("persistent");
    assert.equal(
      await page
        .getByRole("link", { name: "Open owner/fastqueue wiki" })
        .count(),
      1,
    );
    await page.getByLabel("Search Wikis").fill("no-such-topic");
    await page.getByRole("heading", { name: "No matching Wikis" }).waitFor();
    await page.getByLabel("Search Wikis").fill("");
    await page.screenshot({
      path: `${output}/${mobile ? "mobile" : "desktop"}-browse.png`,
      fullPage: true,
    });
    await page.getByRole("button", { name: "Toggle color theme" }).click();
    await page.screenshot({
      path: `${output}/${mobile ? "mobile" : "desktop"}-browse-dark.png`,
      fullPage: true,
    });
    const recipient = await browser.newContext({
      viewport: mobile
        ? { width: 390, height: 844 }
        : { width: 1440, height: 1000 },
    });
    await recipient.route("**/*", fixture);
    const reader = await recipient.newPage();
    await reader.goto(`${base}/browse?tab=community`);
    await reader
      .getByRole("link", { name: "Open owner/fastqueue wiki" })
      .click();
    await reader
      .getByRole("heading", { name: "From request to durable result" })
      .waitFor();
    assert.equal(
      await reader.getByRole("button", { name: "Remove from Browse" }).count(),
      0,
    );
    assert.equal(await reader.locator('input[type="password"]').count(), 0);
    assert.equal(
      mutations,
      1,
      "public discovery/read must not mutate or generate",
    );
    await page.goto(`${base}/wiki/${id}`);
    await page.getByRole("button", { name: "Remove from Browse" }).click();
    await page
      .getByText("Removed from Browse. Your share link still works.", {
        exact: true,
      })
      .waitFor();
    await reader.goto(`${base}/browse?tab=community`);
    await reader
      .getByRole("heading", { name: "Share the first community Wiki" })
      .waitFor();
    await reader.goto(`${base}/wiki/${id}`);
    await reader
      .getByRole("heading", { name: "From request to durable result" })
      .waitFor();
    const overflow = await reader.evaluate(
      () => document.documentElement.scrollWidth > innerWidth,
    );
    assert.equal(overflow, false);
    assert.deepEqual(errors, []);
    assert.deepEqual(outbound, []);
    reports.push({
      mobile,
      sharedArchitecture: true,
      sourcesClickable: true,
      publicationRequiresOwnerAction: true,
      crossBrowserDiscovery: true,
      unpublishPreservesReadLink: true,
      modelRequests: 0,
      mutations,
      overflow,
      errors,
    });
    await context.close();
    await recipient.close();
  }
  await fs.writeFile(`${output}/report.json`, JSON.stringify(reports, null, 2));
  console.log(JSON.stringify(reports));
} finally {
  await browser.close();
}
