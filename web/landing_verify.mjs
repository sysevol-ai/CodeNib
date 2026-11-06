// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0
// Provider-free: node web/landing_verify.mjs LANDING_URL [OUTPUT]
//
// Acceptance for the guard homepage: layout at each width, the page without
// JavaScript, copying the install commands, the chart's case names, and the
// opt-in funnel events under Do Not Track and Global Privacy Control.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import { chromium } from "playwright";

const base = process.argv[2] || "http://127.0.0.1:7894";
const output = process.argv[3] || "/tmp/codenib-guard-home";
await fs.mkdir(output, { recursive: true });
const browser = await chromium.launch();
const reports = [];

try {
  for (const width of [1440, 1024, 860, 768, 390, 320]) {
    const context = await browser.newContext({ viewport: { width, height: 900 }, reducedMotion: "reduce" });
    context.setDefaultTimeout(10000);
    const page = await context.newPage(), errors = [], remote = [], failed = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("request", request => {
      if (new URL(request.url()).origin !== new URL(base).origin) remote.push(request.url());
    });
    page.on("response", response => { if (response.status() >= 400) failed.push(response.url()); });
    await page.goto(base, { waitUntil: "networkidle" });
    await page.evaluate(() => document.fonts.ready);
    assert.equal(await page.title(), "CodeNib: What Did Your Coding Agent Break?");
    assert.equal(await page.locator(".hero-ctas .btn-primary").getAttribute("href"), "#install");
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    const hero = await page.locator(".hero h1").boundingBox();
    const panel = await page.locator(".hero-panel").boundingBox();
    if (width > 1080) {
      assert.ok(panel.x > hero.x + hero.width - 1, "the agent message sits beside the headline");
      assert.ok(panel.y + panel.height <= 900, "the agent message is in the first desktop view");
    } else {
      assert.ok(panel.y > hero.y, "the agent message follows the headline on narrow screens");
    }
    // Every heading, panel and chart row stays inside the viewport.
    const outside = await page.evaluate(() => [...document.querySelectorAll(
      "main h1, main h2, .panel, .units-table, .cmd-block, .more-row a")]
      .filter(node => { const box = node.getBoundingClientRect(); return box.left < 0 || box.right > innerWidth + 0.5; })
      .map(node => node.className || node.tagName));
    assert.deepEqual(outside, []);
    assert.deepEqual(errors, []);
    assert.deepEqual(remote, [], "the homepage loads nothing from other origins");
    assert.deepEqual(failed, []);
    await page.screenshot({ path: `${output}/home-${width}.png`, fullPage: true });
    reports.push({ width, heroBottom: Math.round(hero.y + hero.height), panelTop: Math.round(panel.y) });
    await context.close();
    console.log(`Verified homepage at ${width}px.`);
  }

  // Without JavaScript the case, chart and commands still read in full.
  const noJs = await browser.newContext({ javaScriptEnabled: false, viewport: { width: 390, height: 844 } });
  const fallback = await noJs.newPage();
  await fallback.goto(base);
  assert.equal(await fallback.locator(".strip .u.hit").count(), 57);
  assert.ok(await fallback.locator("#install-command").isVisible());
  assert.equal(await fallback.locator("button.copy").isVisible(), false, "no dead button without JavaScript");
  await noJs.close();

  // Copy puts the two commands on the clipboard without their prompts, and
  // the chart names the case under the pointer.
  const copyContext = await browser.newContext({ permissions: ["clipboard-read", "clipboard-write"] });
  const copyPage = await copyContext.newPage();
  await copyPage.goto(base, { waitUntil: "networkidle" });
  await copyPage.locator("button.copy").click();
  assert.equal(await copyPage.evaluate(() => navigator.clipboard.readText()),
    'python -m pip install "codenib[graph]"\ncodenib guard install --agent claude');
  await copyPage.locator(".strip .u").first().hover();
  assert.match(await copyPage.locator(".unit-tip").textContent(), /django\/django-11149 · CodeStory: flagged/);
  await copyContext.close();

  // Funnel events are opt-in, fixed-shape and suppressed by privacy signals.
  for (const privacy of ["off", "dnt", "gpc"]) {
    const context = await browser.newContext({ permissions: ["clipboard-read", "clipboard-write"] });
    context.setDefaultTimeout(10000);
    if (privacy !== "off") await context.addInitScript(name => Object.defineProperty(navigator, name, {
      value: name === "doNotTrack" ? "1" : true,
    }), privacy === "dnt" ? "doNotTrack" : "globalPrivacyControl");
    const events = [];
    await context.route(`${base}/`, async route => {
      const response = await route.fetch();
      return route.fulfill({ response, body: (await response.text()).replace("</head>",
        '<meta name="codenib-experience-endpoint" content="/api/experience-events"></head>') });
    });
    await context.route("**/api/experience-events", route => {
      events.push(route.request().postDataJSON());
      return route.fulfill({ status: 204 });
    });
    const page = await context.newPage();
    await page.goto(base, { waitUntil: "networkidle" });
    await page.locator('.hero-ctas a[href="#real-case"]').click();
    await page.waitForTimeout(400);
    await page.locator("button.copy").click();
    await page.locator("button.copy").click();
    await page.waitForTimeout(200);
    if (privacy !== "off") assert.deepEqual(events, []);
    else {
      for (const event of ["page_view", "guard_case_view", "install_command_copy"]) {
        assert.equal(events.filter(item => item.event === event).length, 1, event);
      }
      for (const payload of events) {
        assert.deepEqual(Object.keys(payload).sort(), ["event", "surface", "visit"]);
        assert.equal(payload.surface, "landing");
      }
    }
    await context.close();
  }
  await fs.writeFile(`${output}/acceptance.json`, JSON.stringify(reports, null, 2));
  console.log(JSON.stringify({ reports, noJavaScript: true, events: "case view, install copy, deduplication, DNT and GPC pass" }, null, 2));
} finally {
  await browser.close();
}
