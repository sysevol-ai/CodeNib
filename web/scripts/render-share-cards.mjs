// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

// Render the 1200x630 link-preview images the backend serves from
// <data_dir>/share_cards/. Point it at a running frontend (vite preview with
// the backend behind it); it screenshots /share-card/<repo id> for every
// prepared repository and /share-card/ for the site card.
//
//   node scripts/render-share-cards.mjs http://127.0.0.1:3022 /path/to/data_dir/share_cards [repo_id ...]

import { mkdirSync, renameSync } from "node:fs";
import { join } from "node:path";
import { chromium } from "playwright";

const [, , base, outDir, ...only] = process.argv;
if (!base || !outDir) {
  console.error("usage: render-share-cards.mjs <frontend-url> <out-dir> [repo_id ...]");
  process.exit(2);
}
mkdirSync(outDir, { recursive: true });
const repos = only.length
  ? only
  : (await (await fetch(`${base}/api/repos`)).json()).map((repo) => repo.id);
const targets = [["_site", "/share-card/"], ...repos.map((id) => [id, `/share-card/${id}`])];

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1200, height: 630 } });
let failed = 0;
for (const [name, path] of targets) {
  try {
    await page.goto(base + path, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForSelector("[data-share-ready='true']", { timeout: 90000 });
    const tmp = join(outDir, `.${name}.png`);
    await page.screenshot({ path: tmp, clip: { x: 0, y: 0, width: 1200, height: 630 } });
    renameSync(tmp, join(outDir, `${name}.png`));
    console.log(`ok   ${name}`);
  } catch (error) {
    failed += 1;
    console.log(`FAIL ${name}: ${String(error).split("\n")[0]}`);
  }
}
await browser.close();
process.exit(failed ? 1 : 0);
