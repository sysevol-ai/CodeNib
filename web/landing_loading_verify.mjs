// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0
// Provider-free: node web/landing_loading_verify.mjs LANDING_URL [OUTPUT]
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import { spawn } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";
import { chromium } from "playwright";

const base = process.argv[2] || "http://127.0.0.1:7894";
const output = process.argv[3] || "/tmp/codenib-landing-loading";
await fs.mkdir(output, { recursive: true });
const server = process.argv[2] ? null : spawn("python3", ["-m", "http.server", "7894",
  "--bind", "127.0.0.1", "--directory", new URL("../landing", import.meta.url).pathname], { stdio: "ignore" });
let browser;

function gate() {
  let release, seen;
  return { pending: new Promise(resolve => { release = resolve; }),
    arrived: new Promise(resolve => { seen = resolve; }),
    release: () => release(), seen: () => seen() };
}

try {
  if (server) {
    for (let attempt = 0; ; attempt++) {
      if (server.exitCode !== null) throw new Error("Homepage preview exited before becoming ready");
      try { if ((await fetch(base)).ok) break; } catch { /* Preview is starting. */ }
      if (attempt === 99) throw new Error("Homepage preview did not become ready");
      await delay(100);
    }
  }
  browser = await chromium.launch();
  for (const width of [1440, 390, 320]) {
    for (const failure of [false, true]) {
      const context = await browser.newContext({ viewport: { width, height: 900 } });
      context.setDefaultTimeout(10000);
      let turn = gate();
      // The former shadow CSS and its replacement module share this gate, so
      // the regression also fails against the original published homepage.
      await context.route(/\/repo-explorer(?:-styles\.js|\.css)(?:\?.*)?$/, async route => {
        turn.seen();
        await turn.pending;
        return failure ? route.abort("failed") : route.continue();
      });
      const page = await context.newPage();
      await page.addInitScript(() => {
        window.exampleFrames = [];
        const sample = () => {
          const root = document.querySelector("codenib-explorer")?.shadowRoot;
          const row = root?.querySelector(".code-line");
          if (row) {
            const notes = [...root.querySelectorAll("[data-note]")].filter(node =>
              getComputedStyle(node).visibility === "visible" && getComputedStyle(node).display !== "none");
            window.exampleFrames.push({ rowsStyled: getComputedStyle(row).display === "block", visibleNotes: notes.length });
          }
          requestAnimationFrame(sample);
        };
        requestAnimationFrame(sample);
      });
      for (const navigation of ["open", "refresh"]) {
        if (navigation === "open") await page.goto(base, { waitUntil: "commit" });
        else { turn = gate(); await page.reload({ waitUntil: "commit" }); }
        await turn.arrived;
        await page.locator(".stage h1").waitFor();
        await page.waitForTimeout(150);
        const frames = await page.evaluate(() => window.exampleFrames);
        assert.ok(frames.every(frame => frame.rowsStyled && frame.visibleNotes === 1),
          "the homepage painted raw source, concatenated line numbers or hidden notes before styles loaded");
        const fallback = page.locator("codenib-explorer > a");
        assert.equal(await fallback.isVisible(), true, "keep a readable fallback while the example loads");
        if (width !== 320 && !failure) await page.screenshot({ path: `${output}/${width}-${navigation}-pending.png` });
        turn.release();
        await page.waitForLoadState("domcontentloaded");
        if (failure) {
          assert.equal(await fallback.isVisible(), true, "a failed style dependency must preserve the Wiki link");
          assert.match(await fallback.getAttribute("href"), /psf__requests\?p=redirects$/);
        } else {
          await page.locator("codenib-explorer .source-scroll").waitFor();
          await page.waitForTimeout(150);
          const painted = await page.evaluate(() => window.exampleFrames);
          assert.ok(painted.length > 0);
          assert.ok(painted.every(frame => frame.rowsStyled && frame.visibleNotes === 1),
            "the example must be styled on its first painted frame");
          await page.locator('codenib-explorer [data-node="3"]').click();
          assert.match(await page.locator("codenib-explorer .syntax").innerText(), /old_parsed.hostname != new_parsed.hostname/);
          if (width !== 320) await page.screenshot({ path: `${output}/${width}-${navigation}-ready.png` });
        }
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      }
      await context.close();
      console.log(`Homepage first paint: ${width}px ${failure ? "failed" : "delayed"} styles, open and refresh passed.`);
    }
  }
} finally {
  await browser?.close();
  if (server && server.exitCode === null) server.kill();
}
