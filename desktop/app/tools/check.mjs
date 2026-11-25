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
      .filter((el) => el.offsetParent && el.getClientRects().length && el.children.length === 0 && el.textContent.trim());
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
