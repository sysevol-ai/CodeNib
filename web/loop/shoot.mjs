// shoot.mjs — capture OUR running app for a loop item's evidence.
//
// Usage:
//   node loop/shoot.mjs <url> <name> [selector]
//   node loop/shoot.mjs http://127.0.0.1:3000/ landing
//   node loop/shoot.mjs http://127.0.0.1:3000/wiki/<id> wiki "aside.toc"
//
// Output: loop/app/<name>.png (full page, or the selector's bounding box if
// given) + prints console/page errors + horizontal-overflow flag as JSON so
// the loop can assert on a clean render (G2/G3: evidence from the real app).
import { chromium } from "playwright";

const url = process.argv[2] ?? "http://127.0.0.1:3000/";
const name = process.argv[3] ?? "app";
const selector = process.argv[4] ?? null;

const browser = await chromium.launch();
const ctx = await browser.newContext({
  viewport: { width: 1440, height: 900 },
  // Cloud egress proxy uses a custom CA Chromium won't trust by default.
  ignoreHTTPSErrors: true,
});
const page = await ctx.newPage();

const consoleErrors = [];
const pageErrors = [];
page.on("console", (m) => m.type() === "error" && consoleErrors.push(m.text()));
page.on("pageerror", (e) => pageErrors.push(String(e)));

let status = null;
try {
  const resp = await page.goto(url, { waitUntil: "networkidle", timeout: 30000 });
  status = resp ? resp.status() : null;
} catch (e) {
  pageErrors.push("NAV_FAIL: " + e.message);
}
await page.waitForTimeout(1500);

const overflow = await page.evaluate(() => ({
  scrollW: document.documentElement.scrollWidth,
  clientW: document.documentElement.clientWidth,
}));

const outPath = `loop/app/${name}.png`;
let captured = true;
if (selector) {
  const el = await page.$(selector);
  if (el) await el.screenshot({ path: outPath });
  else {
    captured = false;
    await page.screenshot({ path: outPath, fullPage: true });
  }
} else {
  await page.screenshot({ path: outPath, fullPage: true });
}
await browser.close();

console.log(
  JSON.stringify(
    {
      url,
      outPath,
      status,
      selector,
      selectorFound: selector ? captured : null,
      horizontalScroll: overflow.scrollW > overflow.clientW + 1,
      consoleErrors,
      pageErrors,
    },
    null,
    2
  )
);
