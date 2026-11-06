// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0
// Provider-free: node web/landing_loading_verify.mjs LANDING_URL [OUTPUT]
//
// The homepage must read in full before, and without, its one script: the
// case, the call path, the chart and the install commands are static HTML.
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

async function assertReadable(page) {
  await page.locator(".hero h1").waitFor();
  const state = await page.evaluate(() => ({
    overflow: document.documentElement.scrollWidth > innerWidth,
    title: document.querySelector(".hero h1")?.textContent,
    brokenTest: document.querySelector(".hero-panel .r-broken")?.textContent,
    chain: [...document.querySelectorAll(".chain .c-name")].map(node => node.textContent),
    squares: [...document.querySelectorAll(".strip")].map(strip => [
      strip.querySelectorAll(".u").length, strip.querySelectorAll(".u.hit").length]),
    install: document.getElementById("install-command")?.textContent,
    monoDiff: getComputedStyle(document.querySelector(".diff")).whiteSpace,
  }));
  assert.equal(state.overflow, false, "the homepage must not scroll sideways");
  assert.match(state.title, /What did it break\?/);
  assert.match(state.brokenTest, /test_check_homomorphism/);
  assert.deepEqual(state.chain, ["test_check_homomorphism", "homomorphism",
    "_check_homomorphism", "_check_homomorphism._image"]);
  assert.deepEqual(state.squares, [[68, 35], [68, 22]], "the chart is static HTML");
  assert.match(state.install, /codenib guard install --agent claude/);
  assert.equal(state.monoDiff, "pre", "the patch keeps its line structure");
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
      await context.route(/\/assets\/guard\/landing-guard\.js(?:\?.*)?$/, async route => {
        turn.seen();
        await turn.pending;
        return failure ? route.abort("failed") : route.continue();
      });
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", error => errors.push(error.message));
      for (const navigation of ["open", "refresh"]) {
        if (navigation === "open") await page.goto(base, { waitUntil: "commit" });
        else { turn = gate(); await page.reload({ waitUntil: "commit" }); }
        await turn.arrived;
        // The script is held: everything a visitor reads is already there.
        await assertReadable(page);
        if (width !== 320 && !failure) {
          const capture = await context.newCDPSession(page);
          const { data } = await capture.send("Page.captureScreenshot", { format: "png" });
          await fs.writeFile(`${output}/${width}-${navigation}-pending.png`, Buffer.from(data, "base64"));
          await capture.detach();
        }
        turn.release();
        await page.waitForLoadState("load");
        await assertReadable(page);
        if (failure) {
          assert.equal(await page.locator("button.copy").isVisible(), false,
            "a failed script leaves no dead copy button");
        } else if (width !== 320) {
          await page.screenshot({ path: `${output}/${width}-${navigation}-ready.png` });
        }
      }
      assert.deepEqual(errors, []);
      await context.close();
      console.log(`Homepage first paint: ${width}px ${failure ? "failed" : "delayed"} script, open and refresh passed.`);
    }
  }
} finally {
  await browser?.close();
  if (server && server.exitCode === null) server.kill();
}
