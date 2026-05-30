import { chromium } from "playwright";
import { mkdirSync } from "fs";
const OUT = "/tmp/ask-shots";
mkdirSync(OUT, { recursive: true });
const BASE = "http://127.0.0.1:3000";
const REPO = "axios__axios-4731";
const b = await chromium.launch();
const pg = await b.newPage({ viewport: { width: 1440, height: 1000 } });
const errors = [];
pg.on("pageerror", (e) => errors.push(e.message));

// Repo page: mode tabs (no Diagram) + wiki relevant files repo-relative.
await pg.goto(`${BASE}/${REPO}`, { waitUntil: "networkidle", timeout: 60000 });
await pg.waitForTimeout(2500);
const modeTabs = await pg.$$eval(".mode-tab", (e) => e.map((x) => x.textContent.trim()));
const wikiRelFiles = await pg.$$eval(".relevant-files-wiki .relevant-file", (e) =>
  e.slice(0, 4).map((x) => x.textContent.trim())
).catch(() => []);
await pg.screenshot({ path: `${OUT}/repo-page.png` });

// Ask page: code header = GitHub link, no left chip grid, tabs present.
const q = "How are request interceptors implemented?";
await pg.goto(`${BASE}/${REPO}/ask?q=${encodeURIComponent(q)}`, {
  waitUntil: "networkidle",
  timeout: 60000,
});
await pg.waitForSelector(".ask-a", { timeout: 120000 }).catch(() => {});
await pg.waitForTimeout(2500);
const leftChips = await pg.$$eval(".ask-explain .relevant-file", (e) => e.length).catch(() => 0);
const ghHref = await pg.$eval("a.codepanel-name", (e) => e.getAttribute("href")).catch(() => null);
const tabs = await pg.$$eval(".codepanel-tab", (e) => e.length).catch(() => 0);
const codeName = await pg.$eval(".codepanel-name", (e) => e.textContent.trim()).catch(() => "");
await pg.screenshot({ path: `${OUT}/ask-final.png` });

console.log(
  JSON.stringify(
    { modeTabs, wikiRelFiles, leftChips, ghHref, tabs, codeName, errors },
    null,
    2
  )
);
await b.close();
