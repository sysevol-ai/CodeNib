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
        .waitForSelector(
          ".graph-modal .codegraph-canvas, .graph-modal .codemap-unavailable",
          { timeout: 30000 }
        )
        .catch(() => {});
      report.hasGraphCanvas =
        (await page.locator(".graph-modal .codegraph-canvas").count()) > 0;
      report.graphUnavailable =
        (await page.locator(".graph-modal .codemap-unavailable").count()) > 0;
      report.codemapMeta = (await page.locator(".codemap-meta").first().textContent().catch(() => null)) || null;
      report.graphCoverage =
        (await page.locator(".codemap-coverage").first().textContent().catch(() => null)) || null;
      report.chipCount = await page.locator(".codemap-chip").count();
    } else {
      report.codemapLaunch = "absent";
    }
  } else {
    await page.waitForSelector(".wiki-content, .markdown, article", { timeout: 15000 }).catch(() => {});
  }

  await page.waitForTimeout(1500); // let layout settle

  if (process.env.CLICK_EDGE && mode === "codemap") {
    // Drive a real cytoscape edge tap via the test seam, then peek the call site.
    const clicked = await page.evaluate(() => {
      const el = document.querySelector(".codegraph-canvas");
      const cy = el && el.__cy;
      if (!cy) return "no-cy";
      const anchored = cy.edges().filter((e) => e.data("hasAnchor") === 1);
      // Prefer the edge with the most call sites so the multi-site pager shows.
      let edge = anchored[0] || cy.edges()[0];
      anchored.forEach((e) => {
        if ((e.data("anchors") || []).length > (edge.data("anchors") || []).length) edge = e;
      });
      if (!edge) return "no-edge";
      edge.emit("tap");
      return `${edge.data("srcShort")} -> ${edge.data("tgtShort")} (${(edge.data("anchors") || []).length} sites)`;
    });
    report.clickedEdge = clicked;
    await page.waitForSelector(".callsite-peek .hl-code, .callsite-peek .callsite-msg", { timeout: 12000 }).catch(() => {});
    await page.waitForTimeout(700);
    report.peekLoc = (await page.locator(".callsite-loc").first().textContent().catch(() => null)) || null;
    report.hasBand = (await page.locator(".callsite-peek .hl-band").count()) > 0;
  }

  if (process.env.CLICK_NODE && mode === "codemap") {
    // Tap a node → its definition source peek.
    const clicked = await page.evaluate(() => {
      const el = document.querySelector(".codegraph-canvas");
      const cy = el && el.__cy;
      if (!cy) return "no-cy";
      const node = cy.nodes().filter((n) => n.data("root") !== 1)[0] || cy.nodes()[0];
      if (!node) return "no-node";
      node.emit("tap");
      return node.data("short");
    });
    report.clickedNode = clicked;
    await page.waitForSelector(".callsite-peek .hl-code, .callsite-peek .callsite-msg", { timeout: 12000 }).catch(() => {});
    await page.waitForTimeout(700);
    report.peekLoc = (await page.locator(".callsite-loc").first().textContent().catch(() => null)) || null;
    report.hasFocusBtn = (await page.locator(".callsite-focus").count()) > 0;
  }

  if (process.env.CLICK && mode === "codemap") {
    // Click the Nth symbol chip to verify the refocus path (shared with node clicks).
    const chips = page.locator(".codemap-chip");
    const idx = Math.min(+process.env.CLICK, (await chips.count()) - 1);
    report.clickedChip = await chips.nth(idx).textContent().catch(() => null);
    await chips.nth(idx).click();
    await page.waitForSelector(".codegraph-canvas", { timeout: 15000 }).catch(() => {});
    await page.waitForTimeout(2000);
    report.metaAfterClick = (await page.locator(".codemap-meta").first().textContent().catch(() => null)) || null;
  }
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
