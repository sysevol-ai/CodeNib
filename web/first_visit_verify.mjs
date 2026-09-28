// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0
// Provider-free: node web/first_visit_verify.mjs WIKI_URL LANDING_URL [OUTPUT]
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import { chromium } from "playwright";
import evidence from "../landing/assets/explore/requests.js";

const wiki = process.argv[2] || "http://127.0.0.1:3012";
const landing = process.argv[3] || "http://127.0.0.1:7882";
const output = process.argv[4] || "/tmp/codenib-first-visit";
await fs.mkdir(output, { recursive: true });
const browser = await chromium.launch();
const repo = { id: "psf__requests", repo: "psf/requests", language: "python", languages: ["python"], base_commit: evidence.commit, commit_short: evidence.commit.slice(0, 8), file_count: 22, capabilities: {} };
const reports = [];

async function selectionLayout(page) {
  return page.evaluate(() => {
    const host = document.querySelector("codenib-explorer");
    const root = host.shadowRoot;
    const bounds = element => {
      const rect = element.getBoundingClientRect();
      return [rect.x, rect.y, rect.width, rect.height];
    };
    return [scrollX, scrollY, ...[host, root.querySelector(".flow"), root.querySelector(".source"),
      root.querySelector("footer"), document.querySelector("h1")].flatMap(bounds)];
  });
}

async function assertStableSelection(page, explorer) {
  // Keep every flow control visible, including on a scrolled mobile page.
  await explorer.locator(".flow").evaluate(element => window.scrollTo({
    top: scrollY + element.getBoundingClientRect().top - 150, behavior: "instant",
  }));
  const before = await selectionLayout(page);
  for (const index of [3, 0, 1, 2]) {
    await explorer.locator(`[data-node="${index}"]`).click();
    const after = await selectionLayout(page);
    assert.ok(after.every((value, i) => Math.abs(value - before[i]) < 1), `node ${index} moved the page or resized the example`);
    const lines = evidence.nodes[index].content.trimEnd().split("\n");
    const indent = Math.min(...lines.filter(line => line.trim()).map(line => line.match(/^ */)[0].length));
    assert.equal(await explorer.locator(".syntax").textContent(), lines.map(line => line.slice(indent)).join("\n"));
  }
  const plainColor = await explorer.locator(".syntax").evaluate(element => getComputedStyle(element).color);
  for (const token of ["keyword", "string", "comment"]) {
    const color = await explorer.locator(`.hljs-${token}`).first().evaluate(element => getComputedStyle(element).color);
    assert.notEqual(color, plainColor, `${token} must have visible syntax color`);
  }
  for (const index of [0, 1, 2]) {
    await explorer.locator(`[data-edge="${index}"]`).click();
    const after = await selectionLayout(page);
    assert.ok(after.every((value, i) => Math.abs(value - before[i]) < 1), `edge ${index} moved the page or resized the example`);
    const band = await explorer.locator(".line-highlight").boundingBox();
    const line = await explorer.locator(".code-line.selected").boundingBox();
    assert.ok(Math.abs(band.y - line.y) < 1, "call highlight must align with its line number");
  }
  await explorer.locator(".source-scroll").evaluate(element => element.scrollTo({ top: 100, left: 100 }));
  await explorer.locator('[data-node="0"]').click();
  assert.deepEqual(await explorer.locator(".source-scroll").evaluate(element => [element.scrollTop, element.scrollLeft]), [0, 0]);
  await explorer.locator('[data-edge="2"]').click();
}

async function fixtures(context, { metrics = false, unavailable = false } = {}) {
  const writes = [];
  await context.route("**/runtime-config.js", route => route.fulfill({
    contentType: "text/javascript",
    body: `window.__CODENIB_RUNTIME__ = { mode: 'api', experienceEvents: ${metrics} };`,
  }));
  await context.route("**/api/**", async route => {
    const request = route.request(), url = new URL(request.url());
    if (request.method() !== "GET") {
      writes.push({ path: url.pathname, body: request.postDataJSON() });
      return route.fulfill({ status: url.pathname === "/api/experience-events" ? 204 : 403 });
    }
    if (url.pathname === "/api/repos") return route.fulfill({ status: unavailable ? 503 : 200, json: unavailable ? {} : [repo] });
    if (url.pathname === "/api/visitor-wikis") return route.fulfill({ json: { enabled: true } });
    if (url.pathname === "/api/visitor-wikis/public") return route.fulfill({ json: [] });
    if (url.pathname.endsWith("/wiki")) return route.fulfill({ json: { repo: repo.repo, pages: [{ id: "redirects", title: "Redirects", children: [], cache_state: "ready" }] } });
    if (url.pathname.endsWith("/wiki/redirects")) return route.fulfill({ json: { id: "redirects", title: "Redirects", markdown: "## Redirects\n\nRequests rechecks credentials when a request is redirected.", citations: [], diagram: "", grounding: { valid: true }, quality: { valid: true } } });
    if (url.pathname.endsWith("/commits")) return route.fulfill({ json: { available: false, commits: [] } });
    return route.fulfill({ json: { available: false, nodes: [], edges: [], areas: [], links: [] } });
  });
  return writes;
}

try {
  for (const [site, base] of [["landing", landing], ["wiki", wiki]]) {
    for (const width of [1440, 390, 320]) {
      const context = await browser.newContext({ viewport: { width, height: 960 }, reducedMotion: "no-preference" });
      const writes = await fixtures(context);
      const page = await context.newPage(), errors = [], remote = [];
      page.on("pageerror", error => errors.push(error.message));
      page.on("request", request => { if (new URL(request.url()).origin !== new URL(base).origin) remote.push(request.url()); });
      await page.goto(base, { waitUntil: "networkidle" });
      const explorer = page.locator("codenib-explorer");
      await explorer.locator(".node").first().waitFor();
      await page.evaluate(() => document.fonts.ready);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      assert.ok((await explorer.boundingBox()).y < (width === 1440 ? 200 : 450), "example must precede the mobile repository form");
      for (let index = 0; index < 3; index++) {
        const edge = explorer.locator(`[data-edge="${index}"]`);
        await edge.focus();
        await page.keyboard.press("Enter");
        assert.equal(await edge.getAttribute("aria-pressed"), "true");
        const line = evidence.edges[index].anchor.line;
        assert.match(await explorer.locator(".code-line.selected").innerText(), new RegExp(String(line)));
        assert.ok((await explorer.locator(".source-link").getAttribute("href")).endsWith(`#L${line}-L${line}`));
      }
      await explorer.locator('[data-node="3"]').click();
      assert.match(await explorer.locator(".source-code").innerText(), /old_parsed.hostname != new_parsed.hostname/);
      await explorer.locator('[data-edge="2"]').click();
      assert.match(await explorer.locator(".source-code").innerText(), /del headers\["Authorization"\]/);
      await assertStableSelection(page, explorer);
      if (width === 390) {
        await page.emulateMedia({ reducedMotion: "reduce" });
        await assertStableSelection(page, explorer);
      }
      await page.evaluate(() => Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async value => { window.copiedExample = value; } } }));
      await explorer.getByRole("button", { name: "Copy example link" }).click();
      assert.equal(await page.evaluate(() => window.copiedExample), `${base}/#requests-auth`);
      assert.match(await explorer.locator(".share-status").innerText(), /copied/);
      await page.evaluate(() => window.scrollTo({ top: 0, left: 0, behavior: "instant" }));
      await page.screenshot({ path: `${output}/${site}-${width}.png` });
      if (site === "wiki" && width === 390) {
        await page.getByRole("button", { name: "Toggle color theme" }).click();
        await page.screenshot({ path: `${output}/wiki-dark-390.png` });
      }
      assert.deepEqual(errors, []);
      assert.deepEqual(remote, []);
      assert.deepEqual(writes, []);
      reports.push({ site, width, sourceLinks: 3, keyboard: true, stableSelection: true, syntaxColors: true, overflow: false, errors });
      await context.close();
    }
  }

  const context = await browser.newContext();
  await fixtures(context);
  const page = await context.newPage();
  await page.goto(`${wiki}/`);
  await page.goto(`${wiki}/psf/requests?p=redirects`);
  await page.waitForURL(`${wiki}/psf__requests?p=redirects`);
  await page.reload();
  await page.getByRole("heading", { name: "Redirects", exact: true }).waitFor();
  await page.goBack();
  assert.equal(new URL(page.url()).pathname, "/");
  await page.goto(`${wiki}/?repo=https%3A%2F%2Fgithub.com%2Fpsf%2Frequests`);
  await page.waitForURL(`${wiki}/psf__requests`);
  await page.goto(`${wiki}/fresh-owner/ask`);
  await page.waitForURL(`${wiki}/preview/fresh-owner/ask`);
  await page.getByRole("heading", { name: "Generate your Repo Wiki" }).waitFor();
  await page.goto(`${wiki}/?repo=invalid%25value`);
  await page.getByRole("heading", { name: "Enter a GitHub repository" }).waitFor();
  await context.close();

  const offline = await browser.newContext();
  const offlineWrites = await fixtures(offline, { unavailable: true });
  const offlinePage = await offline.newPage();
  await offlinePage.goto(wiki);
  await offlinePage.locator('codenib-explorer [data-node="3"]').click();
  assert.match(await offlinePage.locator(".source-code").innerText(), /return True/);
  await offlinePage.goto(`${wiki}/psf/requests`);
  await offlinePage.getByRole("button", { name: "Retry", exact: true }).waitFor();
  assert.equal(new URL(offlinePage.url()).pathname, "/psf/requests");
  assert.deepEqual(offlineWrites, []);
  await offline.close();

  const delayed = await browser.newContext();
  const delayedWrites = await fixtures(delayed);
  let releaseCatalog;
  const catalogReady = new Promise(resolve => { releaseCatalog = resolve; });
  await delayed.route("**/api/repos", async route => {
    await catalogReady;
    return route.fulfill({ json: [repo] });
  });
  const early = await delayed.newPage();
  await early.goto(wiki);
  await early.getByRole("textbox", { name: "Explore your own repository" }).fill("psf/requests");
  await early.getByRole("button", { name: "Open Wiki" }).click();
  await early.getByText("Checking for a ready Wiki…", { exact: true }).waitFor();
  assert.equal(new URL(early.url()).pathname, "/psf/requests");
  assert.equal(await early.getByLabel("OpenRouter inference key").count(), 0);
  releaseCatalog();
  await early.waitForURL(`${wiki}/psf__requests`);
  assert.deepEqual(delayedWrites, []);
  await delayed.close();

  for (const privacy of [false, true]) {
    const measured = await browser.newContext();
    if (privacy) await measured.addInitScript(() => Object.defineProperty(navigator, "doNotTrack", { value: "1" }));
    const events = await fixtures(measured, { metrics: true });
    const p = await measured.newPage();
    await p.goto(wiki, { waitUntil: "networkidle" });
    await p.locator('codenib-explorer [data-node="3"]').click();
    await p.locator('codenib-explorer [data-node="2"]').click();
    await p.getByRole("textbox", { name: "Explore your own repository" }).fill("https://github.com/private-example/do-not-log");
    await p.getByRole("button", { name: "Open Wiki" }).click();
    await p.getByRole("heading", { name: "Generate your Repo Wiki" }).waitFor();
    if (privacy) assert.deepEqual(events, []);
    else {
      assert.equal(events.filter(item => item.body.event === "example_source_open").length, 1);
      assert.ok(events.some(item => item.body.event === "generation_form_view"));
      for (const item of events) {
        assert.equal(item.path, "/api/experience-events");
        assert.deepEqual(Object.keys(item.body).sort(), ["event", "surface", "visit"]);
        assert.match(item.body.visit, /^[a-f0-9]{32}$/);
        assert.ok(!JSON.stringify(item).includes("do-not-log"));
      }
    }
    await measured.close();
  }
  await fs.writeFile(`${output}/acceptance.json`, JSON.stringify(reports, null, 2));
  console.log(JSON.stringify({ reports, routes: "aliases, refresh, Back, invalid input and unavailable catalog pass", metrics: "bounded payload, deduplication and DNT pass" }, null, 2));
} finally { await browser.close(); }
