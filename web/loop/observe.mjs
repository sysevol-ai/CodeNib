// observe.mjs — capture DeepWiki reference screenshots + DOM/style dumps.
//
// Usage:
//   node loop/observe.mjs --probe        # just check reachability, exit
//   node loop/observe.mjs                 # capture the default view set
//   node loop/observe.mjs <url> <name>    # capture one arbitrary view
//
// Output: loop/reference/<name>.png (full page), loop/reference/<name>.json
// (per-region bounding boxes + computed styles for major regions). Region
// selectors are best-effort and meant to be refined AFTER the first run, once
// OBSERVATIONS.md records DeepWiki's real DOM. Missing selectors are skipped,
// never fatal — G1 wants evidence, not guesses baked into tooling.
import { chromium } from "playwright";

const REF_DIR = "loop/reference";

// Default reference views. A representative repo wiki is picked at observe time
// from DeepWiki's own landing page if not overridden, so we never hardcode a
// repo that may not exist.
const DEFAULT_VIEWS = [
  { name: "landing", url: "https://deepwiki.com/" },
];

// Region selectors are intentionally generic; refine in a follow-up once the
// real DOM is known. We dump whatever matches.
const REGION_GUESSES = [
  "header",
  "nav",
  "aside",
  "main",
  "[class*='sidebar']",
  "[class*='toc']",
  "[class*='card']",
  "pre",
  "code",
];

function args() {
  const a = process.argv.slice(2);
  return {
    probe: a.includes("--probe"),
    url: a.find((x) => x.startsWith("http")) ?? null,
    name: a.find((x) => !x.startsWith("-") && !x.startsWith("http")) ?? null,
  };
}

async function newBrowser() {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    userAgent:
      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    deviceScaleFactor: 2,
    // The cloud egress proxy presents a custom CA that Chromium doesn't trust;
    // a screenshot harness has no secrets to protect, so accept it.
    ignoreHTTPSErrors: true,
  });
  return { browser, ctx };
}

async function regionDump(page) {
  return page.evaluate((selectors) => {
    const out = {};
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (!el) continue;
      const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      out[sel] = {
        box: { x: r.x, y: r.y, w: r.width, h: r.height },
        font: cs.fontFamily,
        fontSize: cs.fontSize,
        color: cs.color,
        background: cs.backgroundColor,
        padding: cs.padding,
        border: cs.border,
      };
    }
    return out;
  }, REGION_GUESSES);
}

async function capture(ctx, view) {
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  let status = null;
  try {
    const resp = await page.goto(view.url, {
      waitUntil: "networkidle",
      timeout: 45000,
    });
    status = resp ? resp.status() : null;
  } catch (e) {
    await page.close();
    return { name: view.name, url: view.url, status, error: String(e) };
  }
  await page.waitForTimeout(2500);
  await page.screenshot({ path: `${REF_DIR}/${view.name}.png`, fullPage: true });
  const regions = await regionDump(page);
  const links = await page.evaluate(() =>
    [...document.querySelectorAll("a[href]")]
      .map((a) => a.getAttribute("href"))
      .filter((h) => h && !h.startsWith("#"))
      .slice(0, 60)
  );
  await page.close();
  return { name: view.name, url: view.url, status, regions, links, errors };
}

const { probe, url, name } = args();
const { browser, ctx } = await newBrowser();

if (probe) {
  const page = await ctx.newPage();
  let status = null,
    error = null;
  try {
    const resp = await page.goto("https://deepwiki.com/", {
      waitUntil: "domcontentloaded",
      timeout: 20000,
    });
    status = resp ? resp.status() : null;
  } catch (e) {
    error = String(e);
  }
  await browser.close();
  const ok = status && status < 400;
  console.log(JSON.stringify({ probe: true, reachable: ok, status, error }, null, 2));
  process.exit(ok ? 0 : 1);
}

const views = url ? [{ name: name ?? "view", url }] : DEFAULT_VIEWS;
const results = [];
for (const v of views) results.push(await capture(ctx, v));
await browser.close();

const { writeFileSync } = await import("node:fs");
for (const r of results) {
  if (r.regions || r.links) {
    writeFileSync(`${REF_DIR}/${r.name}.json`, JSON.stringify(r, null, 2));
  }
}
console.log(JSON.stringify(results.map((r) => ({ ...r, regions: undefined })), null, 2));
