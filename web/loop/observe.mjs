// SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
// SPDX-License-Identifier: Apache-2.0
//
// Observe the DeepWiki reference: capture screenshots + DOM/style dumps.
//
// Usage:
//   node loop/observe.mjs --selfcheck
//   node loop/observe.mjs <url> <outName> [width] [height]
//
// Writes reference/<outName>.png (full page) and reference/<outName>.json
// (http status, console/page errors, and computed styles for major regions).
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";
import { writeFileSync } from "node:fs";

const REF_DIR = new URL("./reference/", import.meta.url).pathname;
mkdirSync(REF_DIR, { recursive: true });

const args = process.argv.slice(2);
const selfcheck = args.includes("--selfcheck");
const url = selfcheck ? "https://deepwiki.com/" : args[0] ?? "https://deepwiki.com/";
const outName = args[1] ?? "deepwiki-landing";
const width = parseInt(args[2] ?? "1440", 10);
const height = parseInt(args[3] ?? "900", 10);

const browser = await chromium.launch();
const ctx = await browser.newContext({
  viewport: { width, height },
  ignoreHTTPSErrors: true,
  userAgent:
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) " +
    "Chrome/120.0 Safari/537.36",
});
const page = await ctx.newPage();

const consoleErrors = [];
const pageErrors = [];
page.on("console", (m) => m.type() === "error" && consoleErrors.push(m.text()));
page.on("pageerror", (e) => pageErrors.push(String(e)));

let httpStatus = null;
let title = null;
try {
  const resp = await page.goto(url, { waitUntil: "networkidle", timeout: 45000 });
  httpStatus = resp ? resp.status() : null;
  title = await page.title();
} catch (e) {
  pageErrors.push("NAV_FAIL: " + e.message);
}
await page.waitForTimeout(2000);

// Computed-style dump for a handful of structural regions, if present.
const styleProbe = await page.evaluate(() => {
  const pick = (el) => {
    if (!el) return null;
    const c = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return {
      tag: el.tagName.toLowerCase(),
      font: c.fontFamily,
      fontSize: c.fontSize,
      color: c.color,
      bg: c.backgroundColor,
      maxWidth: c.maxWidth,
      box: { w: Math.round(r.width), h: Math.round(r.height), x: Math.round(r.x) },
    };
  };
  const firstOf = (sels) => {
    for (const s of sels) {
      const el = document.querySelector(s);
      if (el) return { selector: s, ...pick(el) };
    }
    return null;
  };
  return {
    body: pick(document.body),
    header: firstOf(["header", "nav", '[class*="header"]', '[class*="nav"]']),
    main: firstOf(["main", '[class*="content"]', "article"]),
    sidebar: firstOf(['aside', '[class*="sidebar"]', '[class*="toc"]', "nav ul"]),
    input: firstOf(['input[type="search"]', 'input[type="text"]', "input"]),
    codeBlock: firstOf(["pre", "code", '[class*="code"]']),
    linkCount: document.querySelectorAll("a").length,
    headingCount: document.querySelectorAll("h1,h2,h3").length,
  };
});

await page.screenshot({ path: `${REF_DIR}${outName}.png`, fullPage: true });
const report = { url, outName, httpStatus, title, viewport: { width, height }, styleProbe, consoleErrors, pageErrors };
writeFileSync(`${REF_DIR}${outName}.json`, JSON.stringify(report, null, 2));
await browser.close();

console.log(JSON.stringify(report, null, 2));
if (selfcheck && (httpStatus === null || httpStatus >= 400)) {
  console.error(`SELFCHECK FAILED: deepwiki.com unreachable (status=${httpStatus})`);
  process.exit(2);
}
