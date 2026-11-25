// Open the built window in Chromium and WebKit, light and dark, against real
// `codenib wiki` services, and check each screen: no page errors, no sideways
// scroll, no clipped or overlapping text. Writes screenshots for a person to
// look at.
//
//   node tools/check.mjs <out-dir> '<json list of {path,name,port,session}>'
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
// Playwright comes from the repository's web/ dev dependencies.
const require = createRequire(process.env.PLAYWRIGHT_FROM || path.join(here, "../../../web/package.json"));
const { chromium, webkit } = require("playwright");
const mock = readFileSync(path.join(here, "bridge-mock.js"), "utf8");
const [out, reposJson, base = "http://127.0.0.1:8735"] = process.argv.slice(2);
const repos = JSON.parse(reposJson);
const engines = (process.env.ENGINES || "chromium,webkit").split(",");
const schemes = (process.env.SCHEMES || "light,dark").split(",");
const steps = JSON.parse(process.env.STEPS || "null");

async function audit(page, label) {
  const report = await page.evaluate(() => {
    const issues = [];
    if (document.documentElement.scrollWidth > innerWidth + 1) issues.push("sideways scroll");
    const texts = [...document.querySelectorAll("h1,h2,h3,p,li,span,button,a,td,th,code")]
      .filter((el) => el.offsetParent && el.getClientRects().length && el.children.length === 0 && el.textContent.trim() && !el.closest(".sr-only"));
    for (const el of texts) {
      const cs = getComputedStyle(el);
      const fs = parseFloat(cs.fontSize);
      if (fs < 12.5 && !el.closest("[data-sonner-toast],.code-n,kbd,[data-slot=kbd]")) issues.push(`small text ${fs}px: ${el.textContent.trim().slice(0, 30)}`);
      if (cs.overflow === "hidden" && cs.textOverflow !== "ellipsis" && el.scrollWidth > el.clientWidth + 2 && cs.whiteSpace === "nowrap")
        issues.push(`clipped: ${el.textContent.trim().slice(0, 30)}`);
    }
    // Margin notes must not overlap each other.
    const notes = [...document.querySelectorAll(".note")].map((n) => n.getBoundingClientRect()).filter((r) => r.height);
    for (let i = 0; i < notes.length; i++)
      for (let j = i + 1; j < notes.length; j++)
        if (notes[i].bottom > notes[j].top && notes[j].bottom > notes[i].top) issues.push("overlapping notes");
    // A map line may touch only the two boxes it connects.
    for (const route of document.querySelectorAll("polyline.map-route")) {
      const svg = route.ownerSVGElement.getBoundingClientRect();
      const points = route.getAttribute("points").trim().split(/\s+/).map((p) => p.split(",").map(Number));
      const ends = new Set([route.dataset.from, route.dataset.to]);
      const boxes = [...document.querySelectorAll(".area-map [data-map]")].filter((b) => !ends.has(b.dataset.map));
      for (let i = 1; i < points.length; i++) {
        const [x1, y1] = points[i - 1];
        const [x2, y2] = points[i];
        const seg = { left: svg.left + Math.min(x1, x2), right: svg.left + Math.max(x1, x2), top: svg.top + Math.min(y1, y2), bottom: svg.top + Math.max(y1, y2) };
        for (const box of boxes) {
          const r = box.getBoundingClientRect();
          if (seg.left < r.right - 1 && seg.right > r.left + 1 && seg.top < r.bottom - 1 && seg.bottom > r.top + 1)
            issues.push(`map line ${route.dataset.from}>${route.dataset.to} crosses box ${box.dataset.map}`);
        }
      }
    }
    // Boxes must not overlap one another either.
    const placed = [...document.querySelectorAll(".area-map [data-map]")].map((b) => [b.dataset.map, b.getBoundingClientRect()]);
    for (let i = 0; i < placed.length; i++)
      for (let j = i + 1; j < placed.length; j++) {
        const [, a] = placed[i];
        const [, b] = placed[j];
        if (a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom) issues.push(`map boxes ${placed[i][0]} and ${placed[j][0]} overlap`);
      }
    return issues;
  });
  return report.map((r) => `${label}: ${r}`);
}

const problems = [];
for (const name of engines) {
  const engine = { chromium, webkit }[name];
  const browser = await engine.launch();
  for (const scheme of schemes) {
    const context = await browser.newContext({ viewport: { width: 1320, height: 860 }, colorScheme: scheme });
    await context.addInitScript(`window.__MOCK_REPOS__ = ${JSON.stringify(repos)}; window.__MOCK_PICK__ = "/Users/me/code/new-repo";`);
    await context.addInitScript(mock);
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (e) => !/access control checks/.test(e.message) && errors.push(e.message));
    // WebKit reports fetches cut off by a reload as access-control errors.
    page.on("console", (m) => m.type() === "error" && !/access control checks/.test(m.text()) && errors.push(m.text()));
    await page.goto(base);
    for (const step of steps || []) {
      const label = `${name} ${scheme} ${step.name}`;
      if (step.local) await page.evaluate((v) => localStorage.setItem("codenib.last-view", JSON.stringify(v)), step.local);
      if (step.reload) await page.reload();
      if (step.hover) await page.locator(step.hover).first().hover();
      if (step.click) await page.locator(step.click).first().click();
      if (step.type) await page.keyboard.type(step.type);
      if (step.press) await page.keyboard.press(step.press);
      if (step.wait) await page.locator(step.wait).first().waitFor({ timeout: step.timeout || 30000 });
      await page.waitForTimeout(step.settle ?? 500);
      problems.push(...(await audit(page, label)));
      await page.screenshot({ path: `${out}/${name}-${scheme}-${step.name}.png` });
    }
    problems.push(...errors.map((e) => `${name} ${scheme}: page error ${e}`));
    await context.close();
  }
  await browser.close();
}
console.log(problems.length ? problems.join("\n") : "all checks passed");
process.exitCode = problems.length ? 1 : 0;
