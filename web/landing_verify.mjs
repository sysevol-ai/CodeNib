// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0
// Provider-free: node web/landing_verify.mjs LANDING_URL [OUTPUT]
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import { chromium } from "playwright";
import evidence from "../landing/assets/explore/requests.js";

const base = process.argv[2] || "http://127.0.0.1:7894";
const output = process.argv[3] || "/tmp/codenib-question-first";
await fs.mkdir(output, { recursive: true });
const browser = await chromium.launch();
const reports = [];

async function revealPage(page) {
  // Trigger the existing section reveals before taking a full-page image.
  for (const section of await page.locator("main > section, .cta-block").all()) {
    await section.scrollIntoViewIfNeeded();
  }
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
  await page.locator(".reveal").last().waitFor({ state: "visible" });
  await page.waitForTimeout(700);
}

try {
  for (const width of [1440, 1024, 860, 768, 390, 320]) {
    const context = await browser.newContext({
      viewport: { width, height: 900 }, reducedMotion: "reduce",
    });
    context.setDefaultTimeout(10000);
    const page = await context.newPage(), errors = [], remote = [], failed = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("request", request => {
      if (new URL(request.url()).origin !== new URL(base).origin) remote.push(request.url());
    });
    page.on("response", response => { if (response.status() >= 400) failed.push(response.url()); });
    await page.goto(base, { waitUntil: "networkidle" });
    const explorer = page.locator("codenib-explorer");
    await explorer.locator(".node").first().waitFor();
    await page.evaluate(() => document.fonts.ready);
    assert.equal(await page.title(), "CodeNib: Understand Unfamiliar Code with Evidence.");
    assert.equal(await page.locator(".stage-ctas .btn-primary").count(), 1);
    assert.equal(await page.locator(".stage input").count(), 0);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    const card = await explorer.boundingBox(), form = await page.locator(".repo-entry").boundingBox();
    assert.ok(card.y < form.y, "the example must precede the own-repository form");
    if (width > 860) {
      const source = await explorer.locator(".source").boundingBox();
      assert.ok(source.y + source.height <= 900, "source must be visible in the desktop hero");
      for (const selector of [".repo-entry", ".agent-handoff .frame"]) {
        const box = await page.locator(selector).boundingBox();
        assert.ok(Math.abs(box.x - card.x) < 1, "right columns must share an edge");
      }
    } else {
      assert.ok(card.y < 550, "the example should follow a compact mobile introduction");
      const questions = await page.locator(".example-tasks").boundingBox();
      assert.ok(questions.y >= card.y + card.height, "mobile questions follow the first example");
    }
    const order = await explorer.evaluate(host => [".example-question", ".answer", ".flow", ".source"]
      .map(selector => host.shadowRoot.querySelector(selector).getBoundingClientRect().y));
    assert.ok(order.every((value, index) => !index || value > order[index - 1]),
      "the example must read question, explanation, call path, source");
    assert.match(await explorer.locator(".syntax").innerText(), /del headers\["Authorization"\]/);
    await page.locator('.stage-ctas a[href="#example-source"]').click();
    assert.equal(new URL(page.url()).hash, "#example-source");
    assert.equal(await explorer.locator('[data-edge="2"]').evaluate(element =>
      element.getRootNode().activeElement === element), true, "Try an example must focus a source control");

    for (const [task, text] of [
      ["request", /authentication path within a request/],
      ["impact", /callers on this selected path/],
      ["auth", /removes the Authorization header/],
    ]) {
      const button = page.locator(`[data-example-task="${task}"]`);
      await button.focus();
      await page.keyboard.press("Enter");
      assert.equal(await button.getAttribute("aria-pressed"), "true");
      assert.equal(await page.locator('.example-tasks [aria-pressed="true"]').count(), 1);
      assert.match(await explorer.locator(".answer").innerText(), text);
      if (task === "request") assert.match(await explorer.locator(".syntax").innerText(), /def send\(/);
      if (task === "impact") assert.match(await explorer.locator(".syntax").innerText(), /self.rebuild_auth\(prepared_request, resp\)/);
    }

    // Capture page geometry after the explicit task switch. Source selection
    // must leave the card and page fixed, even when excerpts have different lengths.
    await explorer.locator(".flow").evaluate(element => window.scrollTo({
      top: scrollY + element.getBoundingClientRect().top - 100, behavior: "instant",
    }));
    const geometry = () => explorer.evaluate(host => ({
      y: host.getBoundingClientRect().y, height: host.getBoundingClientRect().height, scroll: scrollY,
    }));
    const before = await geometry();
    for (const [index, node] of evidence.nodes.entries()) {
      await explorer.locator(`[data-node="${index}"]`).click();
      assert.deepEqual(await geometry(), before);
      assert.ok((await explorer.locator(".syntax").innerText()).includes(`def ${node.symbol.split(".").at(-1).slice(0, -2)}(`));
      const href = await explorer.locator(".source-link").getAttribute("href");
      assert.ok(href.includes(`/blob/${evidence.commit}/${node.file}#L${node.start_line}-L`));
    }
    for (const [index, edge] of evidence.edges.entries()) {
      const control = explorer.locator(`[data-edge="${index}"]`);
      await control.focus();
      await page.keyboard.press("Enter");
      assert.deepEqual(await geometry(), before);
      assert.equal(await control.getAttribute("aria-pressed"), "true");
      assert.ok((await explorer.locator(".source-link").getAttribute("href"))
        .endsWith(`#L${edge.anchor.line}-L${edge.anchor.line}`));
      const band = await explorer.locator(".line-highlight").boundingBox();
      const line = await explorer.locator(".code-line.selected").boundingBox();
      assert.ok(Math.abs(band.y - line.y) < 1, "call highlight must align with the source line");
    }
    await page.evaluate(() => Object.defineProperty(navigator, "clipboard", {
      configurable: true, value: { writeText: async value => { window.copiedExample = value; } },
    }));
    await explorer.getByRole("button", { name: "Copy example link" }).click();
    assert.equal(await page.evaluate(() => window.copiedExample), `${base}/#requests-auth`);
    await page.goto(`${base}/#requests-auth`, { waitUntil: "networkidle" });
    assert.ok((await explorer.boundingBox()).y < 150, "shared links must open the example");
    await page.goto(`${base}/#example-source`, { waitUntil: "networkidle" });
    assert.ok((await explorer.boundingBox()).y < 150, "the existing example link must still work");

    const formText = await page.locator(".repo-entry").innerText();
    for (const value of ["OpenRouter", "$0.25–$5", "spending limit", "20 MiB", "40 MiB", "4,000", "50,000", "4 MiB"]) {
      assert.ok(formText.includes(value), `${value} must be visible at the repository entry`);
    }
    let submitted;
    await page.route("https://demo.codenib.ai/**", route => {
      submitted = route.request().url();
      return route.fulfill({ contentType: "text/html", body: "<p>Wiki handoff</p>" });
    });
    await page.getByRole("textbox", { name: "Public GitHub repository" }).fill("https://github.com/psf/requests");
    await page.getByRole("button", { name: "Open Wiki" }).click();
    await page.waitForURL("https://demo.codenib.ai/**");
    assert.equal(new URL(submitted).searchParams.get("repo"), "https://github.com/psf/requests");
    assert.deepEqual(remote, [submitted]); // Only the explicit Wiki handoff is remote.
    remote.length = 0;
    await page.goto(base, { waitUntil: "networkidle" });
    await revealPage(page);
    await page.screenshot({ path: `${output}/landing-${width}.png`, fullPage: true });
    assert.deepEqual(errors, []);
    assert.deepEqual(remote, []);
    assert.deepEqual(failed, []);
    reports.push({ width, source: true, tasks: 3, keyboard: true, stableSelection: true, links: true, aligned: true, overflow: false });
    console.log(`Verified landing at ${width}px.`);
    await context.close();
  }

  const noJs = await browser.newContext({ javaScriptEnabled: false, viewport: { width: 390, height: 844 } });
  noJs.setDefaultTimeout(10000);
  const fallback = await noJs.newPage();
  await fallback.goto(base);
  assert.ok(await fallback.getByRole("link", { name: "Read the exact source on GitHub" }).isVisible());
  assert.ok(await fallback.getByRole("textbox", { name: "Public GitHub repository" }).isVisible());
  assert.equal(await fallback.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await noJs.close();

  // Existing events measure evidence inspection and the next meaningful step.
  // Merely viewing or switching a question must not count as opening source.
  for (const privacy of ["off", "dnt", "gpc"]) {
    const context = await browser.newContext();
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
    await page.locator('[data-example-task="impact"]').click();
    assert.equal(events.filter(event => event.event === "example_source_open").length, 0);
    await page.locator('codenib-explorer [data-node="2"]').click();
    await page.locator('codenib-explorer [data-node="3"]').click();
    await page.locator('.stage-ctas a[href="#get-started"]').click();
    await page.getByRole("textbox", { name: "Public GitHub repository" }).fill("private-example/do-not-log");
    await page.route("https://demo.codenib.ai/**", route => route.fulfill({ body: "Wiki handoff" }));
    await page.getByRole("button", { name: "Open Wiki" }).click();
    await page.waitForURL("https://demo.codenib.ai/**");
    if (privacy !== "off") assert.deepEqual(events, []);
    else {
      for (const event of ["example_source_open", "repository_submit", "agent_setup_open"]) {
        assert.equal(events.filter(item => item.event === event).length, 1);
      }
      for (const payload of events) {
        assert.deepEqual(Object.keys(payload).sort(), ["event", "surface", "visit"]);
        assert.equal(payload.surface, "landing");
        assert.ok(!JSON.stringify(payload).includes("do-not-log"));
      }
    }
    await context.close();
  }
  await fs.writeFile(`${output}/acceptance.json`, JSON.stringify(reports, null, 2));
  console.log(JSON.stringify({ reports, noJavaScript: true, events: "source inspection, own repository, agent setup, deduplication, DNT and GPC pass" }, null, 2));
} finally {
  await browser.close();
}
