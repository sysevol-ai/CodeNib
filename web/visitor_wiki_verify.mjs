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
    let statusOffline = false;
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
      if (statusOffline)
        return route.fulfill({ status: 503, json: { detail: "Status temporarily unavailable" } });
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
      skipped_files: [{ path: "assets/diagram.png", size_bytes: 7513866 }],
      history: [
        ...state.history,
        { stage: "outline", page: "", at: Date.now() / 1000 },
        { stage: "planning_page", page: "pipeline", at: Date.now() / 1000 },
      ],
    };
    await page
      .getByRole("heading", { name: "Repository overview", exact: true })
      .waitFor();
    const activity = page.getByLabel("Current agent activity");
    await activity.getByText("Working on: Request pipeline", { exact: true }).waitFor();
    assert.ok((await activity.innerText()).includes("Asking the model to compose"));
    assert.ok(await activity.getByText("Next", { exact: true }).isVisible());
    assert.equal(await activity.locator(".wiki-run-recent li").count(), 3);
    assert.equal(await activity.locator(".is-animated").count(), 1);
    await page.emulateMedia({ reducedMotion: "reduce" });
    assert.equal(await activity.locator(".wiki-run-pulse i").first().evaluate(
      (element) => getComputedStyle(element).animationName,
    ), "none");
    await page.emulateMedia({ reducedMotion: "no-preference" });
    await page.getByText("Skipped 1 file larger than 4 MiB", { exact: true }).click();
    assert.ok(await page.getByText("assets/diagram.png", { exact: true }).isVisible());
    assert.equal(await page.getByRole("link", { name: "Read 1 / 2 ready chapters" }).getAttribute("href"), "#saved-wiki-chapters");
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({
      path: `${output}/visitor-wiki-${mobile ? "mobile" : "desktop"}-progress.png`,
      fullPage: true,
    });
    state = { ...state, updated_at: Date.now() / 1000 - 50 };
    await activity.getByText("Waiting for the next update", { exact: true }).waitFor();
    assert.ok((await activity.innerText()).includes("operation has not reported a new result"));
    // Successful status polls must not reset worker progress age.
    state = { ...state, updated_at: Date.now() / 1000 - 185 };
    await activity.getByText("No recent progress reported", { exact: true }).waitFor();
    assert.equal(await activity.locator(".is-animated").count(), 0);
    assert.ok((await activity.innerText()).includes("does not confirm that it has stopped"));
    assert.ok((await activity.innerText()).includes("Checked"));
    await page.screenshot({
      path: `${output}/visitor-wiki-${mobile ? "mobile" : "desktop"}-delayed.png`,
      fullPage: true,
    });
    await page.getByRole("button", { name: "Toggle color theme" }).click();
    const contrast = await activity.evaluate((element) => {
      const luminance = (color) => {
        const channels = color.match(/[\d.]+/g).slice(0, 3).map(Number).map((value) => {
          const channel = value / 255;
          return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
        });
        return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
      };
      const text = luminance(getComputedStyle(element.querySelector(".wiki-run-status")).color);
      const background = luminance(getComputedStyle(element).backgroundColor);
      return (Math.max(text, background) + 0.05) / (Math.min(text, background) + 0.05);
    });
    assert.ok(contrast >= 4.5, "delayed status must remain legible in dark mode");
    await page.screenshot({
      path: `${output}/visitor-wiki-${mobile ? "mobile" : "desktop"}-dark-delayed.png`,
      fullPage: true,
    });
    await page.getByRole("button", { name: "Toggle color theme" }).click();
    statusOffline = true;
    await activity.getByText("Connection interrupted", { exact: true }).waitFor();
    assert.equal(await activity.locator(".is-animated").count(), 0);
    assert.ok((await activity.innerText()).includes("Reconnecting"));
    statusOffline = false;
    state = { ...state, updated_at: Date.now() / 1000, calls: 5 };
    await activity.getByText("Generation in progress", { exact: true }).waitFor();
    assert.equal(await activity.locator(".is-animated").count(), 1);
    assert.equal(posts, 1, "status reconnect must not submit another funded run");
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
    assert.equal(await page.getByLabel("Current agent activity").count(), 0);
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
    // A source rejection before the first chapter must not look like a live run.
    state = {
      ...state,
      status: "partial",
      stage: "downloading",
      pages: [],
      page_states: {},
      skipped_files: [],
      calls: 0,
      reported_cost_usd: 0,
      message: "The repository archive could not be read safely.",
    };
    await page.reload();
    await page.getByText("Generation stopped", { exact: true }).waitFor();
    const failure = page.getByRole("alert");
    assert.ok((await failure.innerText()).includes(state.message));
    assert.ok((await failure.innerText()).includes("No model calls were made."));
    assert.equal(await page.locator('[aria-current="step"]').count(), 0);
    assert.equal(await page.getByText("Your Wiki is taking shape", { exact: true }).count(), 0);
    const pausedReads = statusReads;
    await page.waitForTimeout(2300);
    assert.equal(statusReads, pausedReads, "stopped Wikis stop polling");
    const refreshed = page.waitForResponse((response) =>
      response.url().endsWith(`/api/visitor-wikis/${state.id}`),
    );
    await page.getByRole("button", { name: "Refresh status" }).click();
    await refreshed;
    assert.equal(statusReads, pausedReads + 1);
    await page.getByText("Generation stopped", { exact: true }).waitFor();
    await page.screenshot({
      path: `${output}/visitor-wiki-${mobile ? "mobile" : "desktop"}-stopped.png`,
      fullPage: true,
    });
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
      stoppedWikiShowsReason: true,
      stoppedWikiStopsPolling: true,
      liveActivityExplainsWork: true,
      staleProgressIsNotServerConnectivity: true,
      reconnectDoesNotGenerate: true,
      reducedMotionRespected: true,
      skippedFilesVisible: true,
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
