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
await pg.waitForSelector(".ask-a", { timeout: 120000 }).catch(() => {});
await pg.waitForTimeout(2500);

const relevantFiles = await pg.$$eval(".relevant-file", (e) => e.map((x) => x.textContent.trim())).catch(() => []);
const tabs = await pg.$$eval(".codepanel-tab", (e) => e.map((x) => x.textContent.trim())).catch(() => []);
const codeName = await pg.$eval(".codepanel-name", (e) => e.textContent.trim()).catch(() => "(none)");
const codeLines = await pg.$$eval(".codepanel-gutter div", (e) => e.length).catch(() => 0);
const hasAbsPath = (codeName + relevantFiles.join(" ")).includes("/home/");
await pg.screenshot({ path: `${OUT}/ask-codewindow.png` });

// switch to the 2nd file tab
if (tabs.length > 1) {
  await (await pg.$$(".codepanel-tab"))[1].click();
  await pg.waitForTimeout(1800);
  const codeName2 = await pg.$eval(".codepanel-name", (e) => e.textContent.trim()).catch(() => "");
  await pg.screenshot({ path: `${OUT}/ask-codewindow-2.png` });
  console.log("after tab switch, codeName2:", codeName2);
}

console.log(
  JSON.stringify(
    {
      relevantFiles: relevantFiles.slice(0, 6),
      relevantCount: relevantFiles.length,
      tabsCount: tabs.length,
      codeName,
      codeLines,
      hasAbsPath,
      errors,
    },
    null,
    2
  )
);
await b.close();
