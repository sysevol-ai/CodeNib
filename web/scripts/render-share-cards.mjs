// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

// Render the 1200x630 link-preview images the backend serves from
// <data_dir>/share_cards/. Point it at a running frontend (vite preview with
// the backend behind it); it screenshots /share-card/<repo id> for every
// prepared repository and /share-card/ for the site card. With --walks it also
// draws one card per walkthrough, <repo id>--walk-<walk id>.png, from
// /share-card/<repo id>?walk=<walk id>.
//
//   node scripts/render-share-cards.mjs http://127.0.0.1:3022 /path/to/data_dir/share_cards [--walks] [repo_id ...]

import { mkdirSync, renameSync } from "node:fs";
import { join } from "node:path";
import { chromium } from "playwright";

const [, , base, outDir, ...rest] = process.argv;
const walks = rest.includes("--walks");
const only = rest.filter((arg) => arg !== "--walks");
if (!base || !outDir) {
  console.error("usage: render-share-cards.mjs <frontend-url> <out-dir> [--walks] [repo_id ...]");
  process.exit(2);
}
mkdirSync(outDir, { recursive: true });
const repos = only.length
  ? only
  : (await (await fetch(`${base}/api/repos`)).json()).map((repo) => repo.id);
const targets = [["_site", "/share-card/"], ...repos.map((id) => [id, `/share-card/${id}`])];
if (walks) {
  for (const id of repos) {
    const response = await fetch(`${base}/api/repos/${encodeURIComponent(id)}/walkthroughs`);
    if (!response.ok) continue;
    for (const walk of (await response.json()).walks ?? []) {
      targets.push([`${id}--walk-${walk.id}`, `/share-card/${id}?walk=${encodeURIComponent(walk.id)}`]);
    }
  }
}

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
