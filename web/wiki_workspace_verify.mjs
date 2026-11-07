// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0
// Build first, then run: node web/wiki_workspace_verify.mjs [FRONTEND_URL]
// Every API response is a local fixture; no repository, model or database is used.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";
import { chromium } from "playwright";

const base = process.argv[2] || "http://127.0.0.1:4175";
const directory = new URL(".", import.meta.url);
const preview = process.argv[2] ? null : spawn(process.execPath, [
  "node_modules/vite/bin/vite.js", "preview", "--host", "127.0.0.1", "--port", "4175", "--strictPort",
], { cwd: directory, stdio: "ignore" });

const repo = { id: "example__map", repo: "example/map", base_commit: "a".repeat(40),
  commit_short: "aaaaaaa", language: "python", languages: ["python"], file_count: 4, capabilities: { codemap: true } };
const leaf = (id, title, tier, children = []) => ({ id, title, tier, cache_state: "ready", children });
const tree = { repo: repo.repo, pages: [
  leaf("overview", "Overview", "primary"),
  leaf("sessions", "Sessions", "primary", [leaf("redirects", "Redirects", "key"), leaf("state", "Session state", "detail")]),
  leaf("adapters", "Adapters", "primary", [leaf("pools", "Connection pools", "detail")]),
] };
const areaMap = { available: true, reason: null,
  areas: [
    { id: "sessions", title: "Sessions", symbols: 60, files: 2 },
    { id: "adapters", title: "Adapters", symbols: 30, files: 1 },
  ],
  links: [{ source: "sessions", target: "adapters", weight: 9, calls: 7,
    example: { source: "a.py:Session.send", target: "b.py:Adapter.send", anchor: { file: "a.py", line: 12 } } }] };
const document = id => ({ id, title: id, diagram: "", citations: [], story: null, journey: null,
  markdown: `# ${id}\n\nThis page explains ${id} in plain words so the reader can follow it.\n\n` +
    Array.from({ length: 30 }, (_, i) => `## Part ${i}\n\nA paragraph about part ${i} of ${id}.`).join("\n\n"),
  generation: { mode: "generated" }, quality: { valid: true } });

const json = body => ({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
async function fixtures(context) {
  await context.route(/\/api\//, route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path.endsWith("/api/repos")) return route.fulfill(json([repo]));
    if (path.endsWith("/wiki-map")) return route.fulfill(json(areaMap));
    if (path.endsWith("/wiki")) return route.fulfill(json(tree));
    if (path.endsWith("/commits")) return route.fulfill(json({ commits: [] }));
    if (path.endsWith("/source")) {
      return route.fulfill(json({ file: "a.py", start_line: 10, end_line: 14, content: "line\nline\nline\nline\nline", language: "python" }));
    }
    const page = path.match(/\/wiki\/([^/]+)$/);
    if (page) return route.fulfill(json(document(decodeURIComponent(page[1]))));
    return route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
  });
}

const browser = await chromium.launch({ args: ["--no-sandbox"] });
try {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    try { if ((await fetch(base)).ok) break; } catch { /* still starting */ }
    await delay(150);
  }
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await fixtures(context);
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(`${base}/example__map?p=sessions`);
  await page.waitForSelector(".wiki-graph .wg-node");

  // Layout: the map and reader fill the window, and every page but Overview is a node.
  assert.equal(await page.locator(".wiki-grid.is-workspace").count(), 1);
  assert.equal(await page.locator(".wg-node").count(), 5);
  assert.equal(await page.locator(".wg-edge.is-calls").count(), 1);
  assert.equal(await page.locator(".wiki-onthispage").isVisible(), false);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
  assert.ok(overflow <= 0, `page overflows by ${overflow}px`);

  // Reading never moves the map: it stays in view, and choosing a page keeps the camera.
  await page.evaluate(() => scrollTo(0, 900));
  await delay(150);
  const box = await page.locator(".wiki-graph").boundingBox();
  assert.equal(Math.round(box.y), 64, "the map stays pinned under the header");
  await page.evaluate(() => scrollTo(0, 0));
  const camera = () => page.locator(".wiki-graph svg > g").getAttribute("transform");
  const before = await camera();
  await page.locator('.wg-node[aria-label="Adapters"]').click();
  await page.waitForFunction(() => location.search.includes("p=adapters"));
  assert.equal(await camera(), before, "choosing a page moved the map");
  assert.equal(await page.locator('.wg-node[aria-current="page"]').getAttribute("aria-label"), "Adapters");

  // A recorded call says what it is and opens its source.
  // The middle of the curve: a curved path's bounding box centre is not on its line.
  const middle = await page.locator(".wg-edge.is-calls .wg-edge-hit").evaluate(path => {
    const point = path.getPointAtLength(path.getTotalLength() / 2).matrixTransform(path.getScreenCTM());
    return { x: point.x, y: point.y };
  });
  await page.mouse.move(middle.x, middle.y);
  await page.waitForFunction(() => document.querySelector(".wiki-graph-foot")?.textContent.includes("7 recorded calls"));
  await page.mouse.click(middle.x, middle.y);
  await page.waitForFunction(() => document.body.textContent.includes("a.py:12") || document.querySelector(".source-panel, .source-drawer, [class*=source]")?.textContent.includes("a.py"));

  // The keyboard reaches a page, and the list carries each page's map colour.
  await page.keyboard.press("Escape");
  await page.locator('.wg-node[aria-label="Sessions"]').focus();
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => location.search.includes("p=sessions"));
  assert.ok(await page.locator(".toc-link .toc-dot").count() >= 3);

  // The splitter resizes the reader, and the size survives a reload.
  const readerWidth = () => page.locator(".wiki-main").evaluate(element => element.getBoundingClientRect().width);
  const start = await readerWidth();
  await page.locator(".wiki-graph-splitter").focus();
  for (let index = 0; index < 3; index += 1) await page.keyboard.press("ArrowLeft");
  await page.waitForFunction(width => document.querySelector(".wiki-main").getBoundingClientRect().width > width + 60, start);
  const resized = await readerWidth();
  await page.reload();
  await page.waitForSelector(".wiki-graph .wg-node");
  assert.equal(Math.round(await readerWidth()), Math.round(resized));

  // Hiding the map returns the plain document; the choice is remembered.
  await page.getByRole("button", { name: "Hide map" }).click();
  assert.equal(await page.locator(".wiki-graph").count(), 0);
  assert.equal(await page.locator(".wiki-grid.is-workspace").count(), 0);
  await page.reload();
  await page.waitForSelector(".wiki-map-show");
  assert.equal(await page.locator(".wiki-graph").count(), 0);
  await page.locator(".wiki-map-show").click();
  await page.waitForSelector(".wiki-graph .wg-node");
  await context.close();

  // Below 1200 px the page stays a document, with no map to squeeze the reader.
  const narrow = await browser.newContext({ viewport: { width: 1100, height: 800 } });
  await fixtures(narrow);
  const small = await narrow.newPage();
  await small.goto(`${base}/example__map?p=sessions`);
  await small.waitForSelector(".wiki-main h1, .wiki-content");
  assert.equal(await small.locator(".wiki-graph").count(), 0);
  await narrow.close();

  // No code graph: the map shows the outline and says so, and claims no calls.
  const plain = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await fixtures(plain);
  await plain.route(/\/wiki-map$/, route => route.fulfill(json({ available: false, areas: [], links: [], reason: "graph_unavailable" })));
  const outline = await plain.newPage();
  await outline.goto(`${base}/example__map?p=sessions`);
  await outline.waitForSelector(".wiki-graph .wg-node");
  await outline.waitForFunction(() => document.querySelector(".wiki-graph-note"));
  assert.equal(await outline.locator(".wg-edge.is-calls").count(), 0);
  await plain.close();

  assert.deepEqual(errors, []);
  console.log("wiki workspace verified");
} finally {
  await browser.close();
  preview?.kill();
}
