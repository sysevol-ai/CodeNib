// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

// The Wiki installs as an app: Chromium finds no installability errors, the
// manifest parses, the service worker registers, and an offline page load gets
// the offline page. On a phone the chrome stays clear of the status bar and
// the home indicator: WebKit at 390px with the safe-area insets an iPhone
// reports (simulated through the --safe-* properties they feed).
//
//   npm run build && node pwa_verify.mjs [base-url] [screenshot-dir]
//   PWA_ENGINES=chromium node pwa_verify.mjs   # without WebKit installed

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdirSync } from "node:fs";
import { setTimeout as delay } from "node:timers/promises";
import { chromium, webkit } from "playwright";

const base = process.argv[2] || "http://127.0.0.1:4183";
const shots = process.argv[3] || "";
// CI installs Chromium only (PWA_ENGINES=chromium); locally WebKit runs too.
const ENGINES = { chromium, webkit };
const engines = (process.env.PWA_ENGINES || "chromium,webkit").split(",").map((name) => name.trim());
const phoneEngine = engines.includes("webkit") ? "webkit" : "chromium";
const directory = new URL(".", import.meta.url);
const preview = process.argv[2] ? null : spawn(process.execPath, [
  "node_modules/vite/bin/vite.js", "preview", "--host", "127.0.0.1", "--port", "4183", "--strictPort",
], { cwd: directory, stdio: "ignore" });

const KEY = "local-session-key";
const repo = { id: "example__app", repo: "example/app", base_commit: "a".repeat(40), commit_short: "aaaaaaa",
  language: "python", languages: ["python"], file_count: 3, capabilities: { annotations: true } };
const tree = { repo: repo.repo, pages: [
  { id: "overview", title: "Overview", children: [] },
  { id: "runtime", title: "Agent Runtime", children: [] },
].map((page) => ({ ...page, cache_state: "ready" })) };
const document = (id) => ({ id, title: id === "runtime" ? "Agent Runtime" : "Overview", diagram: "", citations: [],
  generation: { mode: "offline", model: null },
  markdown: `# Agent Runtime\n\nThe runtime starts a loop.\n\n` +
    Array.from({ length: 30 }, (_, i) => `Paragraph ${i}. The agent plans, acts and reads the result.`).join("\n\n") });
const INSETS = { top: 47, bottom: 34 };

async function mockApi(context) {
  const path = `/api/repos/${repo.id}`;
  await context.route("**/api/**", (route) => {
    const url = new URL(route.request().url());
    let data = {};
    if (url.pathname === "/api/repos") data = [repo];
    else if (url.pathname === `${path}/commits`) data = { available: false, commits: [] };
    else if ([`${path}/visual-evidence`, `${path}/threads`, `${path}/walkthroughs`].includes(url.pathname))
      return route.fulfill({ status: 404, json: {} });
    else if (url.pathname === `${path}/wiki`) data = tree;
    else if (url.pathname === `${path}/wiki-map`) data = { available: false, reason: "graph_unavailable", areas: [], links: [] };
    else if (url.pathname.endsWith("/boundary")) data = { available: false, inbound: [], outbound: [], truncated: false };
    else if (url.pathname === `${path}/annotations`) data = { annotations: [], tasks: [], kinds: ["question", "change", "note"],
      agents: [{ id: "claude-code", name: "Claude Code", available: true, install: "" }] };
    else if (url.pathname.startsWith(`${path}/wiki/`)) data = document(url.pathname.split("/").at(-1));
    else if (url.pathname.startsWith("/api/")) data = [];
    return route.fulfill({ json: data });
  });
}

async function installable() {
  const browser = await chromium.launch();
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    await mockApi(context);
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`${base}/${repo.id}?p=runtime`, { waitUntil: "load" });
    const scope = await page.evaluate(async () => (await navigator.serviceWorker.ready).scope);
    assert.equal(scope, `${base}/`, "the service worker covers the whole app");

    const cdp = await context.newCDPSession(page);
    const manifest = await cdp.send("Page.getAppManifest");
    assert.deepEqual(manifest.errors, [], "the manifest parses without errors");
    assert.ok(manifest.url.endsWith("/manifest.webmanifest"));
    const parsed = JSON.parse(manifest.data);
    assert.equal(parsed.name, "CodeNib Wiki");
    assert.equal(parsed.display, "standalone");
    const { installabilityErrors } = await cdp.send("Page.getInstallabilityErrors");
    assert.deepEqual(installabilityErrors, [], "Chromium can install the app");
    const color = await page.locator('meta[name="theme-color"]').getAttribute("content");
    assert.equal(color, "#121315", "the title bar takes the dark page colour");

    // Online, a page load under the worker is the server's own page.
    await page.goto(`${base}/${repo.id}?p=overview`, { waitUntil: "domcontentloaded" });
    await page.locator(".wiki-main").waitFor();
    assert.equal(await page.evaluate(() => !!navigator.serviceWorker.controller), true);

    assert.deepEqual(errors, []);
    console.log(`Installability: no errors; manifest "${parsed.name}" parsed; service worker scope ${scope}.`);
  } finally {
    await browser.close();
  }
}

// Playwright's offline switch does not reach a service worker's own fetches,
// so the server goes away instead: the worker's fetch fails as it would with
// no connection.
async function offline() {
  const port = 4184;
  const origin = `http://127.0.0.1:${port}`;
  const start = () => spawn(process.execPath, [
    "node_modules/vite/bin/vite.js", "preview", "--host", "127.0.0.1", "--port", String(port), "--strictPort",
  ], { cwd: directory, stdio: "ignore" });
  const ready = async (up) => {
    for (let attempt = 0; attempt < 100; attempt++) {
      let ok = false;
      try { ok = (await fetch(origin)).ok; } catch { /* not listening */ }
      if (ok === up) return;
      await delay(100);
    }
    throw new Error(`preview did not ${up ? "start" : "stop"}`);
  };
  let server = start();
  await ready(true);
  const passed = [];
  try {
    // Safari is where most phones install it; check the worker there too.
    for (const [name, engine] of Object.entries(ENGINES).filter(([name]) => engines.includes(name))) {
      const browser = await engine.launch();
      try {
        const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
        await mockApi(context);
        const page = await context.newPage();
        await page.goto(`${origin}/${repo.id}?p=runtime`, { waitUntil: "load" });
        await page.evaluate(async () => { await navigator.serviceWorker.ready; });
        await page.reload({ waitUntil: "load" });
        assert.equal(await page.evaluate(() => !!navigator.serviceWorker.controller), true, `${name}: controlled`);

        server.kill();
        await ready(false);
        await page.goto(`${origin}/${repo.id}?p=overview`, { waitUntil: "domcontentloaded" });
        assert.match(await page.locator("h1").innerText(), /You are offline/, `${name}: offline page`);
        if (shots) await page.screenshot({ path: `${shots}/${name}-offline.png` });

        server = start();
        await ready(true);
        await page.getByRole("button", { name: "Try again" }).click();
        await page.locator("#root").waitFor({ state: "attached" });
        assert.notEqual(await page.title(), "CodeNib Wiki · Offline", `${name}: Try again reloads the app`);
        passed.push(name);
      } finally {
        await browser.close();
      }
    }
    console.log(`Offline: ${passed.join(", ")}: a page load without the server shows the offline page; Try again recovers.`);
  } finally {
    server.kill();
  }
}

async function measure(page) {
  return page.evaluate(() => {
    const box = (selector) => document.querySelector(selector)?.getBoundingClientRect();
    const header = box(".site-header");
    const brand = box(".site-header .brand");
    const askbar = document.querySelector(".askbar");
    const toc = document.querySelector(".wiki-toc");
    return {
      header: header && { top: header.top, height: header.height },
      brandTop: brand?.top,
      tocTop: toc?.getBoundingClientRect().top,
      askbarPadding: askbar ? parseFloat(getComputedStyle(askbar).paddingBottom) : null,
      overflow: document.documentElement.scrollWidth - window.innerWidth,
    };
  });
}

async function phone() {
  const browser = await ENGINES[phoneEngine].launch();
  try {
    for (const insets of [null, INSETS]) {
      const label = insets ? "insets" : "plain";
      // Playwright's routes do not see requests that pass a service worker
      // (WebKit), so the mocked API needs the worker off; installed() covers it.
      const context = await browser.newContext({
        viewport: { width: 390, height: 844 }, reducedMotion: "reduce", serviceWorkers: "block",
      });
      await mockApi(context);
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${base}/${repo.id}?p=runtime&session=${KEY}`, { waitUntil: "domcontentloaded" });
      await page.locator('.wiki-main[aria-busy="false"] .wiki-story').waitFor();
      if (insets) {
        // An installed app on an iPhone reports these through env(); the CSS
        // reads them only through these properties.
        await page.addStyleTag({
          content: `html:root { --safe-top: ${insets.top}px; --safe-bottom: ${insets.bottom}px; }`,
        });
      }
      await page.evaluate(() => document.fonts.ready);
      const top = insets?.top ?? 0;
      const bottom = insets?.bottom ?? 0;
      const read = await measure(page);
      assert.equal(read.header.height, 64 + top, `${label}: the header grows by the status bar`);
      assert.ok(read.brandTop >= top, `${label}: the brand sits below the status bar`);
      assert.equal(read.askbarPadding === null || read.askbarPadding === 16 + bottom, true,
        `${label}: the ask bar clears the home indicator`);
      assert.ok(read.overflow <= 0, `${label}: no sideways scroll`);
      if (shots) await page.screenshot({ path: `${shots}/${phoneEngine}-390-${label}.png` });

      // The table of contents slides in below the header.
      await page.locator(".toc-toggle").click();
      await page.waitForTimeout(250);
      const tocTop = (await measure(page)).tocTop;
      assert.equal(tocTop, 64 + top, `${label}: the contents open below the header`);
      await page.locator(".toc-toggle").click();
      await page.waitForTimeout(250);

      // The Notes bottom sheet keeps its height above the home indicator.
      await page.getByRole("button", { name: /^Notes/ }).click();
      const drawer = page.locator(".cn-drawer");
      await drawer.waitFor();
      const sheet = await drawer.evaluate((node) => ({
        bottom: node.getBoundingClientRect().bottom,
        padding: parseFloat(getComputedStyle(node).paddingBottom),
      }));
      assert.equal(sheet.bottom, 844, `${label}: the sheet reaches the bottom edge`);
      assert.equal(sheet.padding, bottom, `${label}: the sheet's content stops above the home indicator`);
      if (shots) await page.screenshot({ path: `${shots}/${phoneEngine}-390-${label}-notes.png` });
      assert.deepEqual(errors, []);
      await context.close();
    }
    console.log(`Phone: ${phoneEngine} 390px passed, with and without iPhone safe-area insets.`);
  } finally {
    await browser.close();
  }
}

try {
  for (let attempt = 0; attempt < 100; attempt++) {
    try { if ((await fetch(base)).ok) break; } catch { /* Preview is starting. */ }
    if (preview?.exitCode !== null && preview?.exitCode !== undefined) throw new Error("Preview exited before becoming ready.");
    if (attempt === 99) throw new Error("Frontend did not become ready.");
    await delay(100);
  }
  if (shots) mkdirSync(shots, { recursive: true });
  await installable();
  await offline();
  await phone();
} finally {
  preview?.kill();
}
