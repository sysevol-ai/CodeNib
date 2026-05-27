// Verification harness: navigate, capture console/page errors, screenshot.
// Usage: node verify.mjs <url> <outName> [width] [height]
import { chromium } from "playwright";

const url = process.argv[2] ?? "http://127.0.0.1:3000/";
const outName = process.argv[3] ?? "out";
const width = parseInt(process.argv[4] ?? "1280", 10);
const height = parseInt(process.argv[5] ?? "900", 10);

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width, height } });

const consoleErrors = [];
const consoleWarnings = [];
const pageErrors = [];

page.on("console", (msg) => {
  const t = msg.type();
  if (t === "error") consoleErrors.push(msg.text());
  else if (t === "warning") consoleWarnings.push(msg.text());
});
page.on("pageerror", (err) => pageErrors.push(String(err)));

let httpStatus = null;
try {
  const resp = await page.goto(url, { waitUntil: "networkidle", timeout: 30000 });
  httpStatus = resp ? resp.status() : null;
} catch (e) {
  pageErrors.push("NAV_FAIL: " + e.message);
}

// Give client-rendered widgets (mermaid, fetches) time to settle.
await page.waitForTimeout(1500);

// Detect horizontal overflow (mobile criterion).
const overflow = await page.evaluate(() => {
  const d = document.documentElement;
  return { scrollW: d.scrollWidth, clientW: d.clientWidth };
});

const outPath = `../verification/${outName}.png`;
await page.screenshot({ path: outPath, fullPage: true });
await browser.close();

console.log(
  JSON.stringify(
    {
      url,
      outPath,
      httpStatus,
      viewport: { width, height },
      horizontalScroll: overflow.scrollW > overflow.clientW + 1,
      overflow,
      consoleErrors,
      consoleWarnings,
      pageErrors,
    },
    null,
    2
  )
);
