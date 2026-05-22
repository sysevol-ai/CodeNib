// SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
// SPDX-License-Identifier: Apache-2.0
//
// Screenshot OUR running app. Captures full page, and (optionally) a clip of a
// single selector for region-level diffing.
//
// Usage:
//   node loop/shoot.mjs <route> <outName> [selector] [width] [height]
//   route is relative to APP_BASE (default http://127.0.0.1:3000)
import { chromium } from "playwright";
import { mkdirSync, writeFileSync } from "node:fs";

const APP_BASE = process.env.APP_BASE ?? "http://127.0.0.1:3000";
const APP_DIR = new URL("./app/", import.meta.url).pathname;
mkdirSync(APP_DIR, { recursive: true });

const route = process.argv[2] ?? "/";
const outName = process.argv[3] ?? "app";
const selector = process.argv[4] ?? null;
const width = parseInt(process.argv[5] ?? "1440", 10);
const height = parseInt(process.argv[6] ?? "900", 10);
const url = APP_BASE + route;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width, height }, ignoreHTTPSErrors: true });
const consoleErrors = [];
const pageErrors = [];
page.on("console", (m) => m.type() === "error" && consoleErrors.push(m.text()));
page.on("pageerror", (e) => pageErrors.push(String(e)));

let httpStatus = null;
try {
  const resp = await page.goto(url, { waitUntil: "networkidle", timeout: 30000 });
  httpStatus = resp ? resp.status() : null;
} catch (e) {
  pageErrors.push("NAV_FAIL: " + e.message);
}
await page.waitForTimeout(1500);

const fullPath = `${APP_DIR}${outName}.png`;
await page.screenshot({ path: fullPath, fullPage: true });

let clipPath = null;
if (selector) {
  const el = await page.$(selector);
  if (el) {
    clipPath = `${APP_DIR}${outName}.clip.png`;
    await el.screenshot({ path: clipPath });
  } else {
    pageErrors.push(`SELECTOR_NOT_FOUND: ${selector}`);
  }
}

const overflow = await page.evaluate(() => {
  const d = document.documentElement;
  return d.scrollWidth > d.clientWidth + 1;
});

const report = { url, outName, httpStatus, selector, fullPath, clipPath, horizontalScroll: overflow, consoleErrors, pageErrors };
writeFileSync(`${APP_DIR}${outName}.json`, JSON.stringify(report, null, 2));
await browser.close();
console.log(JSON.stringify(report, null, 2));
