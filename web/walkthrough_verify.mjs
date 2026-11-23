// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0
// Build first, then run: node web/walkthrough_verify.mjs [FRONTEND_URL]
// Every API response is a local fixture; no repository, model or database is used.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";
import { chromium } from "playwright";

const base = process.argv[2] || "http://127.0.0.1:4174";
const directory = new URL(".", import.meta.url);
const preview = process.argv[2] ? null : spawn(process.execPath, [
  "node_modules/vite/bin/vite.js", "preview", "--host", "127.0.0.1", "--port", "4174", "--strictPort",
], { cwd: directory, stdio: "ignore" });

const commit = "b".repeat(40);
const repo = { id: "example__walk", repo: "example/walk", base_commit: commit, commit_short: "bbbbbbb",
  language: "python", languages: ["python"], file_count: 3, capabilities: {} };
const tree = { repo: repo.repo, pages: [
  { id: "overview", title: "Overview", children: [] },
  { id: "runtime", title: "Agent Runtime", children: [] },
  { id: "execution", title: "Code Execution", children: [] },
].map((page) => ({ ...page, cache_state: "ready" })) };
const map = { available: true, areas: [
  { id: "runtime", title: "Agent Runtime", symbols: 400, files: 2 },
  { id: "execution", title: "Code Execution", symbols: 120, files: 1 },
], links: [{ source: "runtime", target: "execution", weight: 12, calls: 12,
  example: { source: "agents.py:run()", target: "executor.py:execute()", anchor: null } }] };
const document = (id) => ({ id, title: id, diagram: "", citations: [], generation: { mode: "offline", model: null },
  markdown: `# ${id}\n\nThe repository introduction.\n\n## Runtime\n\nA paragraph about the runtime.` });

const long = "A sentence long enough to wrap across the narration's width several times over. ";
const lines = (from, count, prefix) =>
  Object.fromEntries(Array.from({ length: count }, (_, k) => [String(from + k), `${prefix}${k === 0 ? "def f(self):" : `    value_${k} = self.helper_with_a_long_name(value_${k - 1})`}`]));
const walk = (id, tab) => ({
  id, tab, title: `How <code>agent.run()</code> works (${tab})`, dek: "From <code>run()</code> to <code>execute()</code>.",
  question: `What happens when I call agent.run(task)? (${tab})`, chosen: "question and answer function chosen by hand",
  map: "The path starts in Agent Runtime and crosses into Code Execution at step 3.",
  stops: [
    ["", "agent.run(task)", null, null, true],
    ["Agent", ".run()", "agents.py:10–30", "starts the loop"],
    ["Agent", "._step_with_a_long_private_name()", "agents.py:40–60", "takes one step"],
    ["", "_execute_code_with_a_very_long_name()", "executor.py:5–25", "runs the code"],
  ],
  segs: [["", ""], [":12", "agents.py:12", "", ""], ["not in the index :45", "agents.py:45", "dash", "not in the index", "named"]],
  zones: [["agents.py", "Agent Runtime", [1, 2]], ["executor.py", "Code Execution", [3]]],
  areas: ["runtime", "execution"],
  cross: [["runtime", "execution", 12, 3, "unrecorded"]],
  steps: [
    { title: "Two functions and a hop", lead: "The path in one view.", at: null, runs: [0], table: "stops", what: "a task" },
    { rail: "run()", title: "<code>run()</code> starts", lead: long, body: [long], idea: "One idea.", at: 1, runs: [0], area: "runtime", what: "a task", packet: "task", code: ["src/agents.py", [[10, 30]], [12]] },
    { rail: "_step()", title: "<code>_step_with_a_long_private_name()</code> steps", lead: "Short.", at: 2, runs: [1], area: "runtime", what: "a step", packet: "step", code: ["src/agents.py", [[40, 44], [50, 52]], [45]] },
    { rail: "execute()", title: "<code>_execute_code_with_a_very_long_name()</code> runs it", lead: long + long + long, idea: "Another idea.", at: 3, runs: [2], area: "execution", what: "code", packet: "code", code: ["src/executor.py", [[5, 25]], [6]] },
    { rail: "Recap", title: "Recap", lead: "Done.", at: null, runs: [], recap: true, table: "sites", what: "a result" },
  ],
});
const walkthroughs = {
  version: 1,
  repo: { id: repo.id, slug: repo.repo, commit, model: "deepseek/deepseek-v4-flash" },
  areas: [
    { id: "runtime", title: "Agent Runtime", name: "Agent Runtime", color: "#3987e5", symbols: 400, files: 2 },
    { id: "execution", title: "Code Execution", name: "Code Execution", color: "#d95926", symbols: 120, files: 1 },
  ],
  links: [["runtime", "execution", 12]],
  paths: { "agents.py": "src/agents.py", "executor.py": "src/executor.py" },
  wiki: { pages: { runtime: { title: "Agent Runtime", top: "runtime", depth: 0 }, execution: { title: "Code Execution", top: "execution", depth: 0 } },
    cites: { "Agent.run()": ["runtime"] }, size: { runtime: [400, 2], execution: [120, 1] }, calls: { "runtime>execution": 12 } },
  walks: [walk("run", "Running a task"), walk("tool", "Calling a tool")],
  source: { "src/agents.py": { ...lines(10, 21, "    "), ...lines(40, 21, "    ") }, "src/executor.py": lines(5, 21, "") },
};

let browser;
try {
  for (let attempt = 0; attempt < 100; attempt++) {
    try { if ((await fetch(base)).ok) break; } catch { /* Preview is starting. */ }
    if (preview?.exitCode !== null && preview?.exitCode !== undefined) throw new Error("Preview exited before becoming ready.");
    if (attempt === 99) throw new Error("Frontend did not become ready.");
    await delay(100);
  }
  browser = await chromium.launch();
  const path = `/api/repos/${repo.id}`;
  for (const width of [1440, 1280, 1024, 390]) {
    for (const scenario of ["late", "missing", "unknown-walk"]) {
      const context = await browser.newContext({ viewport: { width, height: 900 }, reducedMotion: "reduce" });
      context.setDefaultTimeout(10000);
      const page = await context.newPage(), errors = [], unexpected = [];
      page.on("pageerror", (error) => errors.push(error.message));
      let release;
      const held = new Promise((resolve) => { release = resolve; });
      await context.route("**/api/**", async (route) => {
        const url = new URL(route.request().url());
        let data;
        if (url.pathname === "/api/repos") data = [repo];
        else if (url.pathname === `${path}/walkthroughs`) {
          await held;
          if (scenario === "missing") return route.fulfill({ status: 404, json: { detail: "No walkthroughs for this repo" } });
          data = walkthroughs;
        } else if (url.pathname === `${path}/commits`) data = { available: false, commits: [] };
        else if (url.pathname === `${path}/visual-evidence` || url.pathname === `${path}/threads`) return route.fulfill({ status: 404, json: {} });
        else if (url.pathname === `${path}/wiki`) data = tree;
        else if (url.pathname === `${path}/wiki-map`) data = map;
        else if (url.pathname.endsWith("/boundary")) data = { available: false, inbound: [], outbound: [], truncated: false };
        else if (url.pathname.startsWith(`${path}/wiki/`)) data = document(url.pathname.split("/").at(-1));
        else { unexpected.push(url.pathname); data = {}; }
        return route.fulfill({ json: data });
      });

      const walkId = scenario === "unknown-walk" ? "nope" : "run";
      await page.goto(`${base}/${repo.id}?walk=${walkId}&step=2`, { waitUntil: "domcontentloaded" });
      await page.locator(".wiki-main").waitFor();
      // Until the walkthroughs answer, the reader holds the walkthrough's place instead of flashing the Overview.
      await page.waitForTimeout(200);
      assert.equal(await page.locator(".walk-loading").count(), 1, `${scenario}: no loading state while walkthroughs load`);
      assert.equal(await page.locator(".wiki-story").count(), 0, `${scenario}: a page painted where the walkthrough loads`);
      release();

      if (scenario !== "late") {
        // An address naming a walkthrough that does not exist falls back to the page.
        await page.locator('.wiki-main[aria-busy="false"] .wiki-story').waitFor();
        assert.equal(await page.locator(".walk").count(), 0);
        assert.equal(await page.locator(".toc-walks").count(), scenario === "missing" ? 0 : 1);
      } else {
        await page.locator(".walk-route").waitFor();
        await page.evaluate(() => document.fonts.ready);
        await page.waitForTimeout(150);
        assert.match(await page.locator(".walk-pos").innerText(), /^2 of 5$/);
        assert.equal(await page.locator('.toc-walks .toc-link[aria-current="page"]').innerText(), "Running a task");
        const story = page.locator(".walk-story");
        const heights = new Set();
        for (let step = 2; step <= 5; step++) {
          if (step > 2) await page.keyboard.press("ArrowRight");
          await page.waitForTimeout(80);
          assert.match(new URL(page.url()).search, new RegExp(`walk=run&step=${step}$`));
          assert.match(await page.locator(".walk-pos").innerText(), new RegExp(`^${step} of 5$`));
          heights.add(Math.round((await story.boundingBox()).height));
          const clipped = await page.evaluate(() => {
            const prose = document.querySelector(".walk-story > .walk-prose");
            return prose ? prose.scrollHeight - prose.clientHeight : 0;
          });
          if (width >= 1280) assert.ok(clipped <= 2, `step ${step}: the narration clips its prose by ${clipped}px`);
          assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `step ${step}: sideways scroll`);
        }
        // Side by side (from about 1200px), the narration keeps one height for the whole walk;
        // stacked, it grows with each step's text.
        if (width >= 1280) assert.equal(heights.size, 1, `narration heights changed between steps: ${[...heights]}`);
        // The last step goes on to the next walkthrough; a chapter link returns to the Wiki.
        await page.getByRole("button", { name: /^Next/ }).click();
        await page.waitForURL(/walk=tool$/);
        await page.locator(".walk-page-link").first().click();
        await page.locator('.wiki-main[aria-busy="false"] .wiki-story').waitFor();
        assert.match(new URL(page.url()).search, /^\?p=/);
        // The Overview lists the walkthroughs by their questions.
        await page.goto(`${base}/${repo.id}`, { waitUntil: "domcontentloaded" });
        await page.locator(".walk-list button").first().waitFor();
        assert.equal(await page.locator(".walk-list button").count(), 2);
      }
      assert.deepEqual(errors, []);
      assert.deepEqual(unexpected, []);
      await context.close();
      console.log(`Walkthrough: ${width}px ${scenario} passed.`);
    }
  }
} finally {
  await browser?.close();
  preview?.kill();
}
