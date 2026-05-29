// SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
// SPDX-License-Identifier: Apache-2.0
// Manual verification of the Codemap mode + DeepWiki-style bottom ask bar.
import { chromium } from "playwright";
import { mkdirSync } from "fs";

const OUT = "/tmp/cm-shots";
mkdirSync(OUT, { recursive: true });
const BASE = "http://127.0.0.1:3000";
const REPO = "astropy__astropy-12907";

const b = await chromium.launch();
const pg = await b.newPage({ viewport: { width: 1280, height: 900 } });
const errors = [];
pg.on("console", (m) => m.type() === "error" && errors.push("CONSOLE: " + m.text()));
pg.on("pageerror", (e) => errors.push("PAGEERR: " + e.message));

// 1) Repo wiki page — expect Wiki/Diagram/Codemap tabs + bottom ask bar.
await pg.goto(`${BASE}/${REPO}`, { waitUntil: "networkidle", timeout: 60000 });
await pg.waitForTimeout(1500);
const tabs = await pg.$$eval(".mode-tab", (els) => els.map((e) => e.textContent.trim()));
const hasAskbar = !!(await pg.$(".askbar-input"));
await pg.screenshot({ path: `${OUT}/1-repo-wiki.png` });

// 2) Codemap tab — expect a Mermaid dependency graph + symbol chips.
for (const t of await pg.$$("button.mode-tab")) {
  if ((await t.textContent()).trim() === "Codemap") {
    await t.click();
    break;
  }
}
await pg.waitForSelector(".codemap-meta", { timeout: 30000 }).catch(() => {});
await pg.waitForTimeout(3000);
const codemapMeta = await pg.$eval(".codemap-meta", (e) => e.textContent.trim()).catch(() => "(none)");
const svgCount = await pg.$$eval(".codemap svg", (els) => els.length).catch(() => 0);
const chips = await pg.$$eval(".codemap-chip", (els) => els.length).catch(() => 0);
await pg.screenshot({ path: `${OUT}/2-codemap.png` });

// 3) Bottom ask bar -> dedicated answer page.
await pg.fill(".askbar-input", "How does CompImageHDU update its header data?");
await pg.click(".askbar-send");
await pg.waitForURL(/\/ask\?q=/, { timeout: 15000 }).catch(() => {});
const askURL = pg.url();
await pg.waitForTimeout(1500);
await pg.screenshot({ path: `${OUT}/3-ask-loading.png` });
// Answer needs the LLM; capture whatever resolves within ~110s.
await pg.waitForSelector(".ask-a, .ask-answer .muted:not(.ask-thinking)", { timeout: 110000 }).catch(() => {});
await pg.waitForTimeout(1000);
const answerLen = await pg.$eval(".ask-a", (e) => e.textContent.length).catch(() => 0);
await pg.screenshot({ path: `${OUT}/4-ask-answer.png`, fullPage: true });

console.log(
  JSON.stringify(
    { tabs, hasAskbar, codemapMeta, svgCount, chips, askURL, answerLen, errors },
    null,
    2
  )
);
await b.close();
