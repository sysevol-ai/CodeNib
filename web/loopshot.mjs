// Loop screenshot helper — capture a repo page in a given mode (wiki|codemap).
// Usage: node loopshot.mjs <repoId> <mode> <outName|out.png> [width=1440] [height=1000]
// Writes an explicit .png path or ../verification/<outName>.png and prints JSON.
// Run from inside web/ (resolves playwright from web/node_modules).
import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { chromium } from "playwright";

const [, , repoId, mode = "wiki", outName = "shot", w = "1440", h = "1000"] = process.argv;
const base = process.env.WEB_BASE || "http://127.0.0.1:3000";
const url = `${base}/${encodeURIComponent(repoId)}`;
const out = outName.endsWith(".png")
  ? resolve(outName)
  : resolve("../verification", `${outName}.png`);
mkdirSync(dirname(out), { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: +w, height: +h } });
if (process.env.THEME === "dark") {
  await page.addInitScript(() => localStorage.setItem("cm-theme", "dark"));
}
const consoleErrors = [];
const pageErrors = [];
page.on("console", (m) => m.type() === "error" && consoleErrors.push(m.text()));
page.on("pageerror", (e) => pageErrors.push(String(e)));

const report = { url, mode, outPath: out, repoId };
try {
  const resp = await page.goto(url, { waitUntil: "networkidle", timeout: 30000 });
  report.httpStatus = resp ? resp.status() : null;

  if (mode === "codemap") {
    const launch = page.locator(".codegraph-launch");
    if (await launch.count()) {
      await launch.first().click();
      await page
        .waitForSelector(".graph-modal .hier-map, .graph-modal .codemap-unavailable", {
          timeout: 30000,
        })
        .catch(() => {});
      report.hasStructure = (await page.locator(".graph-modal .hier-map").count()) > 0;
      report.graphUnavailable =
        (await page.locator(".graph-modal .codemap-unavailable").count()) > 0;
      report.codemapMeta = (await page.locator(".codemap-meta").first().textContent().catch(() => null)) || null;
      report.graphCoverage =
        (await page.locator(".codemap-coverage").first().textContent().catch(() => null)) || null;
    } else {
      report.codemapLaunch = "absent";
    }
  } else {
    await page.waitForSelector(".wiki-content, .markdown, article", { timeout: 15000 }).catch(() => {});
  }

  await page.waitForTimeout(1500); // let layout settle

  report.horizontalScroll = await page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth
  );
  if (process.env.CLIP) {
    const el = page.locator(process.env.CLIP).first();
    await el.screenshot({ path: out }).catch(async () => {
      await page.screenshot({ path: out, fullPage: true });
    });
  } else {
    await page.screenshot({ path: out, fullPage: true });
  }
} catch (e) {
  report.error = String(e);
} finally {
  report.consoleErrors = consoleErrors.slice(0, 10);
  report.pageErrors = pageErrors.slice(0, 10);
  await browser.close();
}
console.log(JSON.stringify(report, null, 2));
