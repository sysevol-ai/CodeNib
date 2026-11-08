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
const tree = { files: 65, root: "", folders: [{ path: "src", files: 40 }, { path: "tests", files: 24 }, { path: "", files: 1, direct: true }], more: { folders: 0, files: 0 } };
const limits = {
  max_archive_bytes: 20 * 1024 * 1024, max_source_bytes: 40 * 1024 * 1024,
  max_file_bytes: 4 * 1024 * 1024, max_files: 4000, max_archive_entries: 8000,
  source_languages: ["python", "typescript"], graph_languages: ["python"],
};
let lastPage;
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
    let checks = 0;
    // Refusals answered before any run is accepted; each is used once.
    const refusals = [];
    let refused = 0;
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
        return route.fulfill({ json: { enabled: true, limits } });
      if (url.pathname === "/api/visitor-wikis/check") {
        checks++;
        assert.equal(request.headers().authorization, undefined);
        assert.equal(request.headers()["x-wiki-owner"], undefined);
        const rejected = url.searchParams.get("repository") === "owner/large";
        await new Promise((resolve) => setTimeout(resolve, 400));
        return route.fulfill({ json: {
          eligible: !rejected, limits,
          commit: "c".repeat(40), source_files: 65, languages: ["python"], source_tree: tree,
          message: rejected
            ? "The repository download exceeds the 20 MiB hosted archive limit. Run CodeNib locally for this repository."
            : "This snapshot fits the hosted source limits. Generation checks them again before any model call.",
        } });
      }
      if (url.pathname === "/api/visitor-wikis/public")
        return route.fulfill({ json: [] });
      if (url.pathname === "/api/visitor-wikis/models")
        return route.fulfill({ status: 503, json: { detail: "Fixture uses recorded model prices." } });
      // Saved graphs are fetched when a run stops/completes. The status
      // envelope is not a graph response; keep this fixture on the #808 API.
      if (url.pathname.includes("/graphs/"))
        return route.fulfill({ json: {
          commit: "c".repeat(40),
          coverage: { available: false, note: "No index in this provider-free fixture." },
          system_map: { available: false, areas: [], links: [] },
          code_graph: { available: false, nodes: [], edges: [] },
        } });
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
      if (request.method() === "POST" && refusals.length) {
        refused++;
        const refusal = refusals.shift();
        return route.fulfill({
          status: refusal.status,
          headers: refusal.wait ? { "Retry-After": String(refusal.wait) } : {},
          json: { detail: refusal.detail },
        });
      }
      if (request.method() === "POST") {
        await new Promise((resolve) => setTimeout(resolve, 400));
        posts++;
        assert.equal(
          request.headers().authorization,
          "Bearer sk-or-fixture-value",
        );
        assert.equal(request.headers()["x-wiki-owner"].length, 64);
        const body = request.postDataJSON();
        assert.equal(body.repository, "owner/repo");
        assert.equal(body.budget_usd, 2);
        assert.equal(body.model, "deepseek/deepseek-v4.1-flash");
        const id = url.pathname.split("/").at(-1);
        state = {
          id,
          repository: "owner/repo",
          model: body.model,
          scope: "concise",
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
          source_tree: tree,
          history: [
            { stage: "connecting", page: "", at: Date.now() / 1000 },
            { stage: "downloading", page: "", at: Date.now() / 1000 },
            { stage: "analyzing", page: "", at: Date.now() / 1000 },
          ],
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
    lastPage = page;
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`${base}/preview/owner/large`);
    await page.getByText("The repository download exceeds the 20 MiB hosted archive limit. Run CodeNib locally for this repository.", { exact: true }).waitFor();
    assert.ok(await page.getByLabel("OpenRouter inference key").isDisabled());
    assert.ok(await page.getByRole("button", { name: "Generate Wiki", exact: true }).isDisabled());
    assert.equal(posts, 0);
    assert.equal(await page.evaluate(() => localStorage.getItem("codenib-wiki-attempts-v1")), null);
    await page.screenshot({ path: `${output}/visitor-wiki-${mobile ? "mobile" : "desktop"}-rejected.png`, fullPage: true });
    await page.goto(`${base}/wikis`);
    await page
      .getByLabel("Explore your own repository")
      .fill("https://github.com/owner/repo");
    await page.getByRole("button", { name: "Open Wiki" }).click();
    await page.getByLabel("OpenRouter inference key").waitFor();
    await page.getByText("This snapshot fits the hosted source limits. Generation checks them again before any model call.", { exact: true }).waitFor();
    assert.equal(checks, 2);
    const snapshot = page.getByLabel("The snapshot as a map");
    await snapshot.locator(".wg-node").nth(3).waitFor();
    assert.equal(await snapshot.locator(".wg-node").count(), 4, "the opened folder and its three groups");
    assert.ok((await snapshot.innerText()).includes("65 code files in 3 folders"));
    assert.equal(await snapshot.locator(".wg-edge.is-hot").count(), 0, "a picture of the source highlights nothing");
    assert.equal(posts, 0);
    // A refused key says what to fix; nothing was accepted, so there is no
    // saved attempt to check, and an immediate retry is allowed.
    const keyRefusal = "OpenRouter did not accept this key. Check that you copied the whole inference key. No model call was made.";
    refusals.push(
      { status: 409, detail: keyRefusal },
      { status: 429, wait: 2, detail: "Please wait 2 seconds before trying again." },
    );
    const generate = page.getByRole("button", { name: "Generate Wiki", exact: true });
    await page.getByLabel("OpenRouter inference key").fill("sk-or-fixture-typo");
    await page.getByRole("checkbox").check();
    await generate.click();
    await page.getByText(keyRefusal, { exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "Check saved attempt" }).count(), 0);
    assert.ok(await generate.isEnabled(), "a refused key must not block the retry");
    // A rate limit counts down, and the button waits with it.
    await page.getByLabel("OpenRouter inference key").fill("sk-or-fixture-value");
    await generate.click();
    await page.getByText("Please wait 2 seconds before trying again.", { exact: true }).waitFor();
    await page.getByText(/You can try again in [12] seconds?\./).waitFor();
    assert.ok(await generate.isDisabled());
    assert.equal(await page.getByRole("button", { name: "Check saved attempt" }).count(), 0);
    await page.getByText(/You can try again in/).waitFor({ state: "detached", timeout: 5000 });
    assert.ok(await generate.isEnabled());
    assert.equal(refused, 2);
    assert.equal(posts, 0);
    await page
      .getByLabel("OpenRouter inference key")
      .fill("sk-or-fixture-value");
    await generate.click();
    await page.getByText("Checking your account and repository…", { exact: true }).waitFor();
    // Before the plan, the map is the snapshot and the reader is the run.
    await page.getByRole("heading", { level: 1, name: "Reading repo" }).waitFor();
    assert.equal(posts, 1);
    const savedURL = page.url();
    const run = page.getByLabel("Wiki generation");
    assert.ok((await run.innerText()).includes("Reading the source files"));
    assert.ok((await run.innerText()).includes("Downloaded the snapshot"));
    assert.equal(await run.locator('[aria-current="step"]').count(), 1);
    const map = page.getByLabel("Map of this Wiki");
    await map.locator(".wg-node").nth(3).waitFor();
    assert.equal(await map.locator(".wg-node.is-running").count(), 1, "the opened folder turns while reading");
    await page.screenshot({
      path: `${output}/visitor-wiki-${mobile ? "mobile" : "desktop"}-reading.png`,
      fullPage: true,
    });
    state = {
      ...state,
      stage: "planning_page",
      active_page: "pipeline",
      pages,
      calls: 4,
      reported_cost_usd: 0.023,
      page_states: { overview: "ready", pipeline: "running" },
      scope: "concise",
      source_files: 65,
      request_active: true,
      response_chars: 0,
      skipped_files: [{ path: "assets/diagram.png", size_bytes: 7513866 }],
      history: [
        ...state.history,
        { stage: "outline", page: "", at: Date.now() / 1000 },
        { stage: "retrieving", page: "overview", at: Date.now() / 1000 },
        { stage: "planning_page", page: "pipeline", at: Date.now() / 1000 },
      ],
    };
    // The first ready chapter opens by itself; the run folds into one line.
    await page
      .getByRole("heading", { name: "Repository overview", exact: true })
      .waitFor();
    const strip = page.locator(".run-strip");
    await strip.getByText("Writing “Request pipeline”", { exact: true }).waitFor();
    assert.equal(await strip.getAttribute("open"), null);
    assert.equal(await page.locator(".toc-dot.is-ready").count(), 1);
    assert.equal(await page.locator(".toc-dot.is-running").count(), 1);
    if (!mobile) {
      assert.equal(await map.locator(".wg-node.is-running").count(), 1, "the chapter being written turns on the map");
      assert.equal(await map.locator(".wg-node-orbit").count(), 1);
    }
    await strip.locator(":scope > summary").click();
    await strip.getByText("Waiting for the model to respond", { exact: true }).waitFor();
    state = { ...state, response_chars: 512, updated_at: Date.now() / 1000 };
    await strip.getByText("Receiving the model’s response · 512 characters", { exact: true }).waitFor();
    assert.ok((await strip.innerText()).includes("Drafting and checking this chapter"));
    assert.equal(await strip.locator('[aria-current="step"]').count(), 1);
    assert.equal(await strip.locator(".run-strip-mark").evaluate((element) => getComputedStyle(element).animationName), "run-spin");
    await page.emulateMedia({ reducedMotion: "reduce" });
    assert.equal(await strip.locator(".run-strip-mark").evaluate((element) => getComputedStyle(element).animationName), "none");
    if (!mobile)
      assert.equal(await map.locator(".wg-node-orbit").evaluate((element) => getComputedStyle(element).animationName), "none");
    await page.emulateMedia({ reducedMotion: "no-preference" });
    await strip.getByText("1 file over 4 MiB left out", { exact: true }).click();
    assert.ok(await page.getByText("assets/diagram.png", { exact: true }).isVisible());
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({
      path: `${output}/visitor-wiki-${mobile ? "mobile" : "desktop"}-progress.png`,
      fullPage: true,
    });
    state = { ...state, updated_at: Date.now() / 1000 - 50 };
    const quiet = strip.locator(".run-strip-wait");
    await strip.getByText("Waiting for the next update", { exact: true }).waitFor();
    assert.ok((await strip.innerText()).includes("operation has not reported a new result"));
    // Successful status polls must not reset worker progress age.
    state = { ...state, updated_at: Date.now() / 1000 - 185 };
    await strip.getByText("No recent progress reported", { exact: true }).waitFor();
    assert.equal(await strip.locator(".run-strip-mark").evaluate((element) => getComputedStyle(element).animationName), "none");
    if (!mobile) assert.equal(await map.locator(".wg-node-orbit").evaluate((element) => getComputedStyle(element).animationName), "none");
    assert.ok((await strip.innerText()).includes("does not confirm that it has stopped"));
    await page.screenshot({
      path: `${output}/visitor-wiki-${mobile ? "mobile" : "desktop"}-delayed.png`,
      fullPage: true,
    });
    const contrastOf = () => strip.locator(".run-notice strong").evaluate((element) => {
      const luminance = (color) => {
        const channels = color.match(/[\d.]+/g).slice(0, 3).map(Number).map((value) => {
          const channel = value / 255;
          return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
        });
        return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
      };
      const text = luminance(getComputedStyle(element).color);
      const background = luminance(getComputedStyle(document.body).backgroundColor);
      return (Math.max(text, background) + 0.05) / (Math.min(text, background) + 0.05);
    });
    assert.ok((await contrastOf()) >= 4.5, "delayed status must remain legible");
    await page.getByRole("button", { name: "Toggle color theme" }).click();
    assert.ok((await contrastOf()) >= 4.5, "delayed status must remain legible in the other theme");
    await page.screenshot({
      path: `${output}/visitor-wiki-${mobile ? "mobile" : "desktop"}-other-theme-delayed.png`,
      fullPage: true,
    });
    await page.getByRole("button", { name: "Toggle color theme" }).click();
    statusOffline = true;
    await strip.getByText("Connection interrupted", { exact: true }).waitFor();
    assert.equal(await strip.locator(".run-strip-mark").evaluate((element) => getComputedStyle(element).animationName), "none");
    assert.ok((await strip.innerText()).includes("server may still be working"));
    statusOffline = false;
    state = { ...state, updated_at: Date.now() / 1000, calls: 5 };
    await quiet.waitFor({ state: "detached" });
    assert.equal(await strip.locator(".run-strip-mark").evaluate((element) => getComputedStyle(element).animationName), "run-spin");
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
    await strip.locator(":scope > summary").click();
    await strip.getByRole("button", { name: "Stop generation" }).click();
    await strip.locator(":scope > summary").getByText("Generation paused", { exact: true }).waitFor();
    assert.equal(stops, 1);
    await strip
      .getByRole("button", { name: "Continue unfinished chapters" })
      .click();
    await strip
      .getByLabel("OpenRouter inference key")
      .fill("sk-or-fixture-value");
    await strip.getByRole("checkbox").check();
    await strip
      .getByRole("button", { name: "Resume unfinished chapters" })
      .click();
    await page.getByRole("heading", { level: 1, name: "Reading repo" }).waitFor();
    assert.equal(posts, 2);
    state = {
      ...state,
      stage: "complete",
      status: "complete",
      pages,
      page_states: { overview: "ready", pipeline: "ready" },
      active_page: "",
      reported_cost_usd: 0.046,
      history: [...state.history, { stage: "saving_graphs", page: "", at: Date.now() / 1000 }],
    };
    await strip.locator(":scope > summary").getByText("2 chapters, all source-checked", { exact: true }).waitFor();
    assert.equal(await page.locator(".toc-dot.is-running, .toc-dot.is-pending").count(), 0);
    await page.getByRole("heading", { name: "Your Wiki is ready to share" }).waitFor();
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
    await page.locator(".run-strip summary").getByText("Generation paused", { exact: true }).waitFor();
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
    await page.getByRole("heading", { level: 1, name: "Generation stopped" }).waitFor();
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
    await page.getByRole("heading", { level: 1, name: "Generation stopped" }).waitFor();
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
      repositoryChecks: checks,
      oversizedRepositoryRejectedBeforeKey: true,
      posts,
      stops,
      refreshDoesNotGenerate: true,
      completedWikiStopsPolling: true,
      stoppedWikiShowsReason: true,
      stoppedWikiStopsPolling: true,
      liveActivityExplainsWork: true,
      mapShowsSnapshotThenChapters: true,
      refusalsAreExplainedAndRetryable: true,
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
} catch (error) {
  if (lastPage && !lastPage.isClosed()) {
    await lastPage.screenshot({ path: `${output}/failure.png`, fullPage: true });
    await fs.writeFile(`${output}/failure.txt`, await lastPage.locator("body").innerText());
  }
  throw error;
} finally {
  await browser.close();
}
