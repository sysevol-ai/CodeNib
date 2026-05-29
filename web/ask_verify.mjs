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

const q = "How are request interceptors implemented?";
await pg.goto(`${BASE}/${REPO}/ask?q=${encodeURIComponent(q)}`, {
  waitUntil: "networkidle",
  timeout: 60000,
});
await pg
  .waitForSelector(".ask-a, .ask-explain .muted:not(.ask-thinking)", { timeout: 120000 })
  .catch(() => {});
await pg.waitForTimeout(2000);

const hasExplain = !!(await pg.$(".ask-explain"));
const hasCode = !!(await pg.$(".ask-code .codepanel"));
const refs = await pg.$$eval(".ask-ref", (e) => e.length).catch(() => 0);
const answerLen = await pg.$eval(".ask-a", (e) => e.textContent.length).catch(() => 0);
const codeHead = await pg.$eval(".codepanel-head", (e) => e.textContent.trim()).catch(() => "(none)");
const codeLines = await pg.$$eval(".codepanel-gutter div", (e) => e.length).catch(() => 0);
await pg.screenshot({ path: `${OUT}/ask-split.png` });

const refBtns = await pg.$$(".ask-ref");
if (refBtns.length > 1) {
  await refBtns[1].click();
  await pg.waitForTimeout(1800);
  await pg.screenshot({ path: `${OUT}/ask-split-2.png` });
}
console.log(
  JSON.stringify(
    { hasExplain, hasCode, refs, answerLen, codeLines, codeHead: codeHead.slice(0, 70), errors },
    null,
    2
  )
);
await b.close();
