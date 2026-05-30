import { chromium } from "playwright";
import { mkdirSync } from "fs";
mkdirSync("/tmp/ask-shots", { recursive: true });
const b = await chromium.launch();
const pg = await b.newPage({ viewport: { width: 1440, height: 1050 } });
const errors = [];
pg.on("pageerror", (e) => errors.push(e.message));
const REPO = "astropy__astropy-12907";
const q = "How does unit conversion work?";
await pg.goto(`http://127.0.0.1:3000/${REPO}/ask?q=${encodeURIComponent(q)}`, {
  waitUntil: "networkidle",
  timeout: 60000,
});
await pg.waitForSelector(".ask-a", { timeout: 140000 }).catch(() => {});
await pg.waitForSelector(".codepane-tab, .codepane-empty", { timeout: 20000 }).catch(() => {});
await pg.waitForTimeout(3500);
const navTabs = await pg.$$eval(".codepane-tab", (e) => e.length).catch(() => 0);
const frags = await pg.$$eval(".frag", (e) => e.length).catch(() => 0);
const hlBlocks = await pg.$$eval(".hl-code", (e) => e.length).catch(() => 0);
const ghLinks = await pg.$$eval(".frag-head a[href*='github']", (e) => e.length).catch(() => 0);
const hlTokens = await pg.$$eval(".frag .hljs span", (e) => e.length).catch(() => 0);
const leftChips = await pg.$$eval(".ask-explain .relevant-file", (e) => e.length).catch(() => 0);
const firstHref = await pg.$eval(".frag-head a", (e) => e.getAttribute("href")).catch(() => null);
await pg.screenshot({ path: "/tmp/ask-shots/ask-frags.png" });
console.log(
  JSON.stringify({ navTabs, frags, hlBlocks, ghLinks, hlTokens, leftChips, firstHref, errors: errors.slice(0, 3) })
);
await b.close();
