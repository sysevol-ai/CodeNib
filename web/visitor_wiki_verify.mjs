// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0
// Provider-free acceptance: node web/visitor_wiki_verify.mjs URL [artifact-directory]
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import { chromium } from "playwright";

const base = process.argv[2] || "http://127.0.0.1:4179";
const output = process.argv[3] || "/tmp/codenib-visitor-wiki-browser";
await fs.mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const reports = [];
try {
  for (const mobile of [false, true]) {
    const context = await browser.newContext({
      viewport: mobile
        ? { width: 390, height: 844 }
        : { width: 1440, height: 1000 },
    });
    let state = null;
    let posts = 0;
    let stops = 0;
    const errors = [],
      remote = [];
    let statusReads = 0;
    const pages = [
      { id: "overview", title: "Overview", children: [] },
      { id: "pipeline", title: "Request pipeline", children: [] },
    ];
    await context.route("**/*", async (route) => {
      const url = new URL(route.request().url());
      if (url.origin !== new URL(base).origin) {
        remote.push(url.origin);
        return route.abort();
      }
      if (url.pathname === "/api/repos") return route.fulfill({ json: [] });
      if (!url.pathname.startsWith("/api/visitor-wikis"))
        return route.continue();
      const request = route.request();
      if (url.pathname === "/api/visitor-wikis")
        return route.fulfill({ json: { enabled: true } });
      if (url.pathname.endsWith("/stop")) {
        assert.ok(request.headers()["x-wiki-owner"]);
        assert.equal(request.headers().authorization, undefined);
        stops++;
        state = {
          ...state,
          status: "partial",
          message: "Stopped. Completed pages are saved.",
        };
        return route.fulfill({ json: { stopping: true } });
      }
      if (request.method() === "POST") {
        posts++;
        assert.equal(
          request.headers().authorization,
          "Bearer sk-or-fixture-value",
        );
        assert.equal(request.headers()["x-wiki-owner"].length, 64);
        const body = request.postDataJSON();
        assert.equal(body.repository, "owner/repo");
        assert.equal(body.budget_usd, 2);
        const id = url.pathname.split("/").at(-1);
        state = {
          id,
          repository: "owner/repo",
          commit: "c".repeat(40),
          status: "running",
          stage: "analyzing",
          active_page: "",
          pages: [],
          page_states: {},
          message: "",
          created_at: Date.now() / 1000,
          updated_at: Date.now() / 1000,
          reported_cost_usd: 0,
          calls: 0,
          budget_usd: 2,
          unreported_call_cost: false,
          stalled: false,
          history: [{ stage: "downloading", page: "", at: Date.now() / 1000 }],
        };
        return route.fulfill({ status: 202, json: state });
      }
      assert.equal(request.headers().authorization, undefined);
      assert.equal(request.headers()["x-wiki-owner"], undefined);
      if (url.pathname.includes("/pages/")) {
        const id = url.pathname.split("/").at(-1);
        return route.fulfill({
          json: {
            id,
            title: id,
            markdown: `# ${id === "overview" ? "Repository overview" : "Request pipeline"}\n\nThe application receives a request through \`run\`. [E1](#evidence-E1)\n\n## How it works\n\nThe handler validates input before producing a response. [E1](#evidence-E1)\n\n[Read the next chapter](?p=pipeline)\n\n![tracking image](https://tracking.invalid/pixel.png)`,
            citations: [
              {
                file: "src/main.py",
                start_line: 1,
                end_line: 2,
                content: 'def run():\n    return "response"',
                node_name: "run",
                type: "function",
              },
            ],
            diagram: "",
            generation: { mode: "generated" },
            quality: { valid: true },
            grounding: {
              valid: true,
              citation_coverage: 1,
              evidence_count: 1,
              relation_count: 0,
            },
          },
        });
      }
      statusReads++;
      return state
        ? route.fulfill({ json: state })
        : route.fulfill({ status: 404, json: { detail: "Wiki not found" } });
    });
    const page = await context.newPage();
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(base);
    await page
      .getByLabel("Public GitHub repository")
      .fill("https://github.com/owner/repo");
    await page.getByRole("button", { name: "Open Wiki" }).click();
    await page.getByLabel("OpenRouter inference key").waitFor();
    assert.equal(posts, 0);
    await page
      .getByLabel("OpenRouter inference key")
      .fill("sk-or-fixture-value");
    await page.getByRole("checkbox").check();
    await page
      .getByRole("button", { name: "Generate Wiki", exact: true })
      .click();
    await page.getByText("Analyzing source files", { exact: true }).waitFor();
    assert.equal(posts, 1);
    const savedURL = page.url();
    state = {
      ...state,
      stage: "planning_page",
      active_page: "pipeline",
      pages,
      calls: 4,
      reported_cost_usd: 0.023,
      page_states: { overview: "ready", pipeline: "running" },
      history: [
        ...state.history,
        { stage: "outline", page: "", at: Date.now() / 1000 },
        { stage: "planning_page", page: "pipeline", at: Date.now() / 1000 },
      ],
    };
    await page
      .getByRole("heading", { name: "Repository overview", exact: true })
      .waitFor();
    await page.screenshot({
      path: `${output}/visitor-wiki-${mobile ? "mobile" : "desktop"}-progress.png`,
      fullPage: true,
    });
    await page.reload();
    await page
      .getByRole("heading", { name: "Repository overview", exact: true })
      .waitFor();
    assert.equal(posts, 1, "refresh must not start another funded run");
    const storage = await page.evaluate(() =>
      JSON.stringify({ ...localStorage, ...sessionStorage }),
    );
    assert.ok(!storage.includes("sk-or-fixture-value"));
    await page.getByRole("button", { name: "Stop generation" }).click();
    await page.getByText("Generation paused", { exact: true }).waitFor();
    assert.equal(stops, 1);
    await page
      .getByRole("button", { name: "Continue unfinished chapters" })
      .click();
    await page
      .getByLabel("OpenRouter inference key")
      .fill("sk-or-fixture-value");
    await page.getByRole("checkbox").check();
    await page
      .getByRole("button", { name: "Resume unfinished chapters" })
      .click();
    assert.equal(posts, 2);
    state = {
      ...state,
      stage: "complete",
      status: "complete",
      pages,
      page_states: { overview: "ready", pipeline: "ready" },
      active_page: "",
      reported_cost_usd: 0.046,
    };
    await page.getByText("Your Wiki is ready", { exact: true }).waitFor();
    const completedReads = statusReads;
    await page.waitForTimeout(2300);
    assert.equal(statusReads, completedReads, "completed Wikis stop polling");
    assert.ok(
      (
        await page
          .getByRole("link", { name: "Read the next chapter" })
          .getAttribute("href")
      ).startsWith(`/wiki/${state.id}?p=`),
    );
    await page.getByRole("link", { name: "Read the next chapter" }).click();
    await page
      .getByRole("heading", { name: "Request pipeline", exact: true })
      .waitFor();
    await page.getByText("Sources for this chapter").click();
    await page
      .getByRole("button", { name: "src/main.py:1–2", exact: true })
      .click();
    assert.ok(
      (
        await page
          .getByRole("link", { name: "Open pinned source on GitHub" })
          .getAttribute("href")
      ).includes(`/blob/${"c".repeat(40)}/src/main.py#L1-L2`),
    );
    // Remove only owner recovery access to model a recipient opening the read link.
    await page.evaluate(() =>
      localStorage.removeItem("codenib-wiki-attempts-v1"),
    );
    state.status = "partial";
    await page.goto(savedURL);
    await page.getByText("Generation paused", { exact: true }).waitFor();
    assert.equal(
      await page
        .getByRole("button", { name: "Continue unfinished chapters" })
        .count(),
      0,
    );
    assert.equal(await page.locator("input[type=password]").count(), 0);
    assert.equal(posts, 2);
    assert.deepEqual(remote, []);
    assert.deepEqual(errors, []);
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth,
    );
    assert.equal(overflow, false);
    reports.push({
      mobile,
      posts,
      stops,
      refreshDoesNotGenerate: true,
      completedWikiStopsPolling: true,
      keysAbsentFromStorage: true,
      readLinkHasNoOwnerAccess: true,
      externalRequests: remote,
      pageErrors: errors,
      overflow,
    });
    await context.close();
  }
  await fs.writeFile(
    `${output}/visitor-wiki-browser.json`,
    JSON.stringify(reports, null, 2),
  );
  console.log(JSON.stringify(reports));
} finally {
  await browser.close();
}
