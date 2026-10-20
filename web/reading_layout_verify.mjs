// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0
// Build first, then run: node web/reading_layout_verify.mjs [FRONTEND_URL]
// Every API response is a local fixture; no repository, model or database is used.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";
import { chromium } from "playwright";

const base = process.argv[2] || "http://127.0.0.1:4173";
const directory = new URL(".", import.meta.url);
const preview = process.argv[2] ? null : spawn(process.execPath, [
  "node_modules/vite/bin/vite.js", "preview", "--host", "127.0.0.1", "--port", "4173", "--strictPort",
], { cwd: directory, stdio: "ignore" });
const repo = { id: "example__reading", repo: "example/reading", base_commit: "a".repeat(40),
  commit_short: "aaaaaaa", language: "python", languages: ["python"], file_count: 4, capabilities: {} };
const areas = ["Runtime", "Preparation", "Session", "Utilities"].map((title, index) => ({
  id: index ? title.toLowerCase() : "runtime", title, symbols: 20 + index, files: 1,
}));
const map = { available: true, areas, links: areas.slice(0, -1).map((area, index) => ({
  source: area.id, target: areas[index + 1].id, weight: 10, calls: 1,
  example: { source: "example.py:caller()", target: "example.py:callee()", anchor: null },
})) };
const tree = { repo: repo.repo, pages: [{ id: "overview", title: "Overview", children: [] },
  ...areas.map(area => ({ id: area.id, title: area.title, children: [] }))]
  .map(page => ({ ...page, cache_state: "ready" })) };
const document = id => ({ id, title: id, diagram: "", citations: [],
  generation: { mode: "offline", model: null },
  markdown: `# ${id}\n\nThe repository introduction.\n\n## Runtime\n\n` +
    Array.from({ length: 24 }, (_, index) => `Stable paragraph ${index}. The source-linked explanation stays in its reading column while the surrounding index data loads.`).join("\n\n"),
});
const boundary = { available: true, inbound: [], outbound: [], truncated: false };

function gate() {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  return { pending, release };
}

let browser;
try {
  for (let attempt = 0; attempt < 100; attempt++) {
    try { if ((await fetch(base)).ok) break; } catch { /* Preview is starting. */ }
    if (preview?.exitCode !== null && preview?.exitCode !== undefined) throw new Error("Preview exited before becoming ready.");
    if (attempt === 99) throw new Error("Frontend did not become ready.");
    await delay(100);
  }
  browser = await chromium.launch();
  for (const width of [1440, 1024, 768, 390]) {
    for (const scenario of ["map-late", "page-late", "boundary-late", "map-error", "boundary-error", "map-unavailable", "map-stalled", "boundary-stalled", "tree-stalled"]) {
      const stalled = scenario.endsWith("stalled");
      const overview = scenario.startsWith("map") || scenario.startsWith("tree") || scenario === "page-late";
      const chapter = overview ? "overview" : "runtime";
      const path = `/api/repos/${repo.id}`;
      const held = scenario === "tree-stalled" ? `${path}/wiki`
        : scenario === "page-late" ? `${path}/wiki/overview`
        : overview ? `${path}/wiki-map` : `${path}/wiki/runtime/boundary`;
      const context = await browser.newContext({ viewport: { width, height: 900 }, reducedMotion: "reduce" });
      context.setDefaultTimeout(10000);
      const page = await context.newPage(), errors = [], unexpected = [];
      if (stalled) {
        // Keep real-time ticks from racing a manual jump, including after reload
        // when Playwright replays the clock command history in the new document.
        await page.clock.install({ time: new Date("2026-01-01T00:00:00Z") });
        await page.clock.pauseAt(new Date("2026-01-01T01:00:00Z"));
      }
      page.on("pageerror", error => errors.push(error.message));
      let turn = gate();
      await context.route("**/api/**", async route => {
        const url = new URL(route.request().url());
        if (url.pathname === held) await turn.pending;
        if (url.pathname === held && scenario.endsWith("error")) return route.fulfill({ status: 500, body: "Unavailable fixture" });
        let data;
        if (url.pathname === "/api/repos") data = [repo];
        else if (url.pathname === `${path}/commits`) data = { available: false, commits: [] };
        else if (url.pathname === `${path}/visual-evidence`) return route.fulfill({ status: 404, json: { detail: "No visual evidence in this layout fixture" } });
        else if (url.pathname === `${path}/wiki`) data = tree;
        else if (url.pathname === `${path}/wiki-map`) data = scenario === "map-unavailable"
          ? { available: false, reason: "graph_unavailable", areas: [], links: [] } : map;
        else if (url.pathname.endsWith("/boundary")) data = boundary;
        else if (url.pathname.startsWith(`${path}/wiki/`)) data = document(url.pathname.split("/").at(-1));
        else { unexpected.push(url.pathname); data = {}; }
        return route.fulfill({ json: data });
      });
      await page.addInitScript(() => {
        window.readingFrames = [];
        const sample = () => {
          const main = document.querySelector(".wiki-main");
          if (main) {
            const box = main.getBoundingClientRect();
            const poster = document.querySelector(".repo-poster");
            window.readingFrames.push({ x: box.x, width: box.width, busy: main.getAttribute("aria-busy"),
              posterHeight: poster?.getBoundingClientRect().height,
              paragraph: document.querySelector(".wiki-story .markdown p")?.textContent,
            });
          }
          window.readingFrame = requestAnimationFrame(sample);
        };
        requestAnimationFrame(sample);
      });

      for (const navigation of ["open", "refresh"]) {
        const heldStarted = page.waitForRequest(request => new URL(request.url()).pathname === held);
        const proseLoaded = scenario === "page-late" ? null : page.waitForResponse(
          response => new URL(response.url()).pathname === `${path}/wiki/${chapter}`,
        ).then(response => response.finished());
        if (navigation === "open") await page.goto(`${base}/${repo.id}?p=${chapter}`, { waitUntil: "domcontentloaded" });
        else { turn = gate(); await page.reload({ waitUntil: "domcontentloaded" }); }
        await heldStarted;
        if (proseLoaded) await proseLoaded;
        await page.locator(".wiki-main").waitFor();
        await page.evaluate(() => document.fonts.ready);
        // Let the unheld page/visual response arrive while the other stays pending.
        if (stalled) await page.clock.runFor(180);
        else await page.waitForTimeout(180);
        assert.equal(await page.locator(".wiki-story").count(), 0, `${scenario}: prose painted before its leading visual settled`);
        const initial = await page.locator(".wiki-main").boundingBox();
        if (stalled) await page.clock.fastForward(6000);
        else turn.release();
        try { await page.locator('.wiki-main[aria-busy="false"] .wiki-story').waitFor(); }
        catch (error) {
          console.error("LAYOUT_FAILURE", { width, scenario, navigation, errors, unexpected,
            body: (await page.locator("body").innerText()).slice(0, 2400),
            frames: (await page.evaluate(() => window.readingFrames)).slice(-3),
            now: await page.evaluate(() => Date.now()),
          });
          throw error;
        }
        if (stalled) {
          assert.equal(await page.locator(".repo-poster").count(), 0);
          const paragraph = page.locator(".wiki-story .markdown p").first();
          const before = await paragraph.boundingBox();
          turn.release();
          await page.clock.runFor(250);
          const after = await paragraph.boundingBox();
          assert.ok(Math.abs(before.y - after.y) < 1, `${scenario}: a late visual displaced readable prose`);
          if (overview) {
            await page.getByRole("button", { name: "Retry map" }).click();
            await page.locator(".repo-poster").waitFor();
          }
        }
        if (stalled) await page.clock.runFor(250);
        else await page.waitForTimeout(250);
        const final = await page.locator(".wiki-main").boundingBox();
        assert.ok(Math.abs(initial.width - final.width) < 1 && Math.abs(initial.x - final.x) < 1,
          `${scenario}: opening the page changed its reading column`);
        const frames = await page.evaluate(() => window.readingFrames);
        assert.ok(frames.every(frame => Math.abs(frame.width - final.width) < 1), "reading width changed between painted frames");
        const heights = frames.filter(frame => frame.posterHeight !== undefined).map(frame => frame.posterHeight);
        if (heights.length) assert.ok(Math.max(...heights) - Math.min(...heights) < 1, "the map changed height after its first paint");
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
        if (navigation === "open") await page.evaluate(() => scrollTo(0, 1000));
      }
      assert.deepEqual(errors, []);
      assert.deepEqual(unexpected, []);
      await context.close();
      console.log(`Reading layout: ${width}px ${scenario}, open and refresh passed.`);
    }
  }
} finally {
  await browser?.close();
  if (preview && preview.exitCode === null) preview.kill();
}
